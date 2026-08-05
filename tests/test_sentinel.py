"""Tests for the three-layer teardown guarantee (SPEC.md, "Teardown must survive being killed").

    1. Prior proxy state is written to a sentinel file (0600) *before* anything is changed.
    2. atexit plus SIGTERM/SIGINT handlers restore on any orderly exit.
    3. Every server start checks for a stale sentinel and restores it before doing anything.

Layer 3 is the one that covers `kill -9`, and it is the reason the protocol is built out of a
file rather than out of handlers. Everything here runs against a stubbed command runner: the
protocol logic is fully testable without redirecting a single packet, which is what makes it
safe to get right before pointing it at the real machine.
"""

import json
import stat

import pytest

from fake_commands import WEB_PROXY_OFF, FakeCommands
from https_proxy_mcp.targets.systemproxy import (
    ProxySetting,
    SystemProxy,
    SystemProxyState,
    resolve_primary_service,
)


@pytest.fixture
def sentinel(tmp_path):
    return tmp_path / "proxy-state.json"


# --- which service to redirect ------------------------------------------------


def test_the_primary_service_is_the_one_carrying_the_default_route():
    """Not the first in service order.

    On the development machine `-listnetworkserviceorder` ranks Thunderbolt Bridge first
    while the default route goes out en0 (Wi-Fi). Redirecting by service order would have
    proxied an interface carrying no traffic and reported success.
    """
    commands = FakeCommands()

    assert resolve_primary_service(commands) == "Wi-Fi"


# --- layer 1: the sentinel is written before anything changes ------------------


def test_redirecting_writes_the_sentinel_before_issuing_any_mutation(sentinel):
    """The ordering *is* the guarantee. A sentinel written afterwards protects nothing,
    because the window it leaves open is exactly when the process is most likely to die.

    Asserted by sampling the sentinel's existence as each command is issued, rather than by
    inspecting command order — the order of the commands cannot show when the file appeared.
    """
    commands = FakeCommands(watch=sentinel)

    SystemProxy(sentinel, runner=commands).redirect("127.0.0.1", 8080)

    assert commands.sentinel_existed_at_first_mutation is True


def test_the_sentinel_records_the_prior_state_not_the_new_one(sentinel):
    """The whole point: what is written down is what the machine looked like *before*."""
    commands = FakeCommands()

    SystemProxy(sentinel, runner=commands).redirect("127.0.0.1", 8080)

    recorded = SystemProxyState.from_dict(json.loads(sentinel.read_text()))
    assert recorded.web == ProxySetting(enabled=False, server="127.0.0.1", port=9090)
    assert recorded.service == "Wi-Fi"


def test_the_sentinel_is_private(sentinel):
    """It names the machine's network configuration, so it is 0600 like SPEC.md says."""
    SystemProxy(sentinel, runner=FakeCommands()).redirect("127.0.0.1", 8080)

    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o600


def test_redirecting_points_both_proxies_at_the_capture_port(sentinel):
    """HTTPS is the point of the tool, so the secure proxy matters at least as much."""
    commands = FakeCommands()

    SystemProxy(sentinel, runner=commands).redirect("127.0.0.1", 8080)

    assert ["-setwebproxy", "Wi-Fi", "127.0.0.1", "8080"] in commands.mutations
    assert ["-setsecurewebproxy", "Wi-Fi", "127.0.0.1", "8080"] in commands.mutations
    assert ["-setwebproxystate", "Wi-Fi", "on"] in commands.mutations
    assert ["-setsecurewebproxystate", "Wi-Fi", "on"] in commands.mutations


def test_redirecting_refuses_to_overwrite_an_existing_sentinel(sentinel):
    """A sentinel already on disk means a previous capture died without restoring.

    Overwriting it would record the *already proxied* state as the state to return to, so the
    machine's real configuration would be lost for good and every later restore would be a
    no-op that leaves it proxied. Repair has to happen first.
    """
    sentinel.write_text("{}")
    proxy = SystemProxy(sentinel, runner=FakeCommands())

    with pytest.raises(RuntimeError, match="repair"):
        proxy.redirect("127.0.0.1", 8080)


# --- restoring ----------------------------------------------------------------


def test_restoring_puts_back_the_captured_state_not_a_default(sentinel):
    """On a machine that was already configured, "off" alone is a silent data loss."""
    commands = FakeCommands()
    proxy = SystemProxy(sentinel, runner=commands)
    proxy.redirect("127.0.0.1", 8080)
    commands.issued.clear()

    proxy.restore()

    assert ["-setwebproxy", "Wi-Fi", "127.0.0.1", "9090"] in commands.mutations
    assert ["-setwebproxystate", "Wi-Fi", "off"] in commands.mutations


def test_restoring_removes_the_sentinel(sentinel):
    """A leftover sentinel would make the next start think it had crashed."""
    proxy = SystemProxy(sentinel, runner=FakeCommands())
    proxy.redirect("127.0.0.1", 8080)

    proxy.restore()

    assert not sentinel.exists()


def test_restoring_without_a_sentinel_does_nothing(sentinel):
    """`CaptureState.start` calls teardown on every failure path, so restoring state that was
    never changed is the common case, not the edge case."""
    commands = FakeCommands()

    assert SystemProxy(sentinel, runner=commands).restore() is None
    assert commands.mutations == []


def test_restoring_twice_is_safe(sentinel):
    proxy = SystemProxy(sentinel, runner=FakeCommands())
    proxy.redirect("127.0.0.1", 8080)

    proxy.restore()
    proxy.restore()


def test_restoring_reports_the_state_it_put_back(sentinel):
    """`repair_system_proxy` turns this into a log line, so an agent can see that a previous
    run died — otherwise a crash is invisible on the next start."""
    proxy = SystemProxy(sentinel, runner=FakeCommands())
    proxy.redirect("127.0.0.1", 8080)

    restored = proxy.restore()

    assert restored is not None
    assert restored.service == "Wi-Fi"


# --- layer 3: repair on start -------------------------------------------------


def test_a_sentinel_from_a_dead_process_is_restored_on_start(sentinel):
    """This is the `kill -9` path, in the only form a unit test can reach it: the sentinel is
    on disk and no handler ever ran, because the process that wrote it is gone."""
    abandoned = SystemProxyState(
        service="Wi-Fi",
        web=ProxySetting(enabled=False, server="127.0.0.1", port=9090),
        secure=ProxySetting(enabled=False, server="127.0.0.1", port=9090),
        bypass=(),
    )
    sentinel.write_text(json.dumps(abandoned.to_dict()))
    commands = FakeCommands()

    restored = SystemProxy(sentinel, runner=commands).repair()

    assert restored == abandoned
    assert ["-setwebproxystate", "Wi-Fi", "off"] in commands.mutations
    assert not sentinel.exists()


def test_repair_on_a_clean_machine_is_a_no_op(sentinel):
    commands = FakeCommands()

    assert SystemProxy(sentinel, runner=commands).repair() is None
    assert commands.mutations == []


def test_repair_restores_a_service_that_is_no_longer_primary(sentinel):
    """The sentinel names its own service, so moving from Wi-Fi to Ethernet between the crash
    and the restart still repairs Wi-Fi — the interface that was actually redirected."""
    sentinel.write_text(
        json.dumps(
            SystemProxyState(
                service="Ethernet",
                web=ProxySetting(enabled=True, server="proxy.corp", port=3128),
                secure=ProxySetting(enabled=False, server="", port=0),
                bypass=(),
            ).to_dict()
        )
    )
    commands = FakeCommands()

    SystemProxy(sentinel, runner=commands).repair()

    assert ["-setwebproxy", "Ethernet", "proxy.corp", "3128"] in commands.mutations


def test_a_corrupt_sentinel_unproxies_the_machine_rather_than_giving_up(sentinel):
    """An unreadable sentinel still proves something was changed. The prior state is gone, so
    fidelity is impossible, but leaving the machine pointed at a dead proxy is the worse
    failure — turning both proxies off restores connectivity."""
    sentinel.write_text("{ this is not json")
    commands = FakeCommands()

    SystemProxy(sentinel, runner=commands).repair()

    assert ["-setwebproxystate", "Wi-Fi", "off"] in commands.mutations
    assert ["-setsecurewebproxystate", "Wi-Fi", "off"] in commands.mutations


def test_a_corrupt_sentinel_is_moved_aside_so_it_is_not_retried(sentinel):
    sentinel.write_text("{ this is not json")

    SystemProxy(sentinel, runner=FakeCommands()).repair()

    assert not sentinel.exists()
    assert sentinel.with_suffix(".corrupt").exists()


# --- layer 2: handlers --------------------------------------------------------


def test_the_registered_handler_restores(sentinel):
    """atexit and the signal handlers share one callback, so testing it directly tests both
    without having to kill the test runner."""
    commands = FakeCommands()
    proxy = SystemProxy(sentinel, runner=commands)
    proxy.redirect("127.0.0.1", 8080)

    proxy.restore_on_exit()

    assert not sentinel.exists()
    assert ["-setwebproxystate", "Wi-Fi", "off"] in commands.mutations


def test_a_failure_while_restoring_on_exit_is_swallowed(sentinel):
    """An exception raised from atexit or a signal handler replaces the real exit reason with
    a traceback from teardown. The sentinel survives, so the next start repairs it."""

    def exploding(argv, *, stdin=None):
        if any(part.startswith("-set") for part in argv):
            raise OSError("networksetup went away")
        return WEB_PROXY_OFF

    proxy = SystemProxy(sentinel, runner=FakeCommands())
    proxy.redirect("127.0.0.1", 8080)
    proxy._runner = exploding

    proxy.restore_on_exit()

    assert sentinel.exists()
