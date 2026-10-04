"""P3's acceptance criterion: `kill -9` the server, restart, and the system proxy is restored.

PLAN.md's risk table rates "server dies without restoring the system proxy" as the only **High**
severity item that breaks the machine rather than the tool, and says to test it deliberately
rather than trusting the handlers. So this file uses a real child process and a real ``SIGKILL``:
no handler can run, nothing is unwound, and the only thing left is the sentinel on disk.

The child redirects through a **stubbed** command runner, so a genuine kill -9 is exercised
without touching the developer's networking. What is being proved is the protocol — that the
sentinel is complete and that a later start restores the *captured prior state* rather than an
assumed-empty default. That distinction matters on this machine specifically: it already carried
a disabled 127.0.0.1:9090 proxy, so a teardown that only switched the proxy off would pass a
naive test while silently discarding the server and port.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time

import pytest

from fake_commands import FakeCommands
from https_proxy_mcp.server import repair_before_serving
from https_proxy_mcp.targets.systemproxy import SENTINEL_NAME, ProxySetting, SystemProxyState

pytestmark = pytest.mark.integration

#: What the machine looked like before capture: configured, but switched off. Restoring this
#: correctly needs -setwebproxy *and then* -setwebproxystate off, in that order.
PRIOR = SystemProxyState(
    service="Wi-Fi",
    web=ProxySetting(enabled=False, server="127.0.0.1", port=9090),
    secure=ProxySetting(enabled=False, server="127.0.0.1", port=9090),
    bypass=(),
)

#: A child that redirects the (stubbed) system proxy and then waits to be killed. It uses the
#: real SystemProxy, so the sentinel it leaves behind is the real thing.
CHILD = '''
import subprocess, sys, time, json
from pathlib import Path
from https_proxy_mcp.targets.systemproxy import SystemProxy

root = Path(sys.argv[1])
prior = json.loads(Path(sys.argv[2]).read_text())

def runner(argv, *, stdin=None):
    """Stands in for networksetup and scutil so the real machine is never touched."""
    # Every command is logged where the parent can read it, because from another process the only
    # other evidence of what this child did is whether its sentinel is still there — and a child
    # that deleted the file without restoring anything would satisfy that.
    with open(root / "child-commands.jsonl", "a") as handle:
        handle.write(json.dumps(argv) + "\\n")
    # `ps` is the exception, and deliberately real: the sentinel records who wrote it, and a
    # canned start time would make the record indistinguishable from one this parent wrote. Reading
    # the process table changes nothing on the machine.
    if argv[0].endswith("ps"):
        return subprocess.run(argv, capture_output=True, text=True).stdout
    if argv[0].endswith("scutil"):
        if stdin and "Setup:/Network/Service/" in stdin:
            return "<dictionary> {\\n  UserDefinedName : Wi-Fi\\n}\\n"
        return "<dictionary> {\\n  PrimaryService : ABC\\n}\\n"
    if "-getwebproxy" in argv or "-getsecurewebproxy" in argv:
        side = "web" if "-getwebproxy" in argv else "secure"
        setting = prior[side]
        return (
            "Enabled: %s\\nServer: %s\\nPort: %s\\nAuthenticated Proxy Enabled: 0\\n"
            % ("Yes" if setting["enabled"] else "No", setting["server"], setting["port"])
        )
    if "-getproxybypassdomains" in argv:
        return "There aren't any bypass domains set on Wi-Fi.\\n"
    return ""

proxy = SystemProxy(root / "proxy-state.json", runner=runner)
proxy.redirect("127.0.0.1", 8080)
proxy.install_handlers()

# Tell the parent the machine is now "proxied", then wait to be killed.
print("redirected", flush=True)
while True:
    time.sleep(0.05)
'''


def start_child(tmp_path) -> subprocess.Popen:
    """Spawn the child and wait until it reports that it has redirected."""
    script = tmp_path / "child.py"
    script.write_text(CHILD)
    prior_file = tmp_path / "prior.json"
    prior_file.write_text(json.dumps(PRIOR.to_dict()))

    child = subprocess.Popen(
        [sys.executable, str(script), str(tmp_path), str(prior_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout is not None
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        line = child.stdout.readline()
        if line.strip() == "redirected":
            return child
        if child.poll() is not None:
            raise AssertionError(f"child died early: {child.stderr.read() if child.stderr else ''}")
    raise AssertionError("child never reported that it had redirected")


def kill9(child: subprocess.Popen) -> None:
    """SIGKILL, which no handler can intercept. That is the whole point of the exercise."""
    os.kill(child.pid, signal.SIGKILL)
    child.wait(timeout=10)
    assert child.returncode in (-signal.SIGKILL, 137), child.returncode


def child_mutations(tmp_path) -> list[list[str]]:
    """The machine-changing commands the child issued, in order, stripped of the binary path.

    The same view :attr:`FakeCommands.mutations` gives in-process, read back out of the log the
    child's stubbed runner wrote. It is what lets a test assert that the child *put the prior
    state back* rather than only that its sentinel went away.
    """
    log = tmp_path / "child-commands.jsonl"
    issued = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return [argv[1:] for argv in issued if any(part.startswith("-set") for part in argv)]


# --- the criterion ------------------------------------------------------------


def test_a_killed_server_leaves_a_complete_sentinel_behind(tmp_path):
    """Layers 1 and 2 have both failed by construction here: the file is all that is left."""
    child = start_child(tmp_path)
    kill9(child)

    sentinel = tmp_path / SENTINEL_NAME
    assert sentinel.exists()
    assert SystemProxyState.from_dict(json.loads(sentinel.read_text())) == PRIOR


def test_the_sentinel_names_the_process_that_wrote_it(tmp_path):
    """Written by one process and read by another, which is the only condition under which the
    owner is worth anything. A same-process test cannot tell a recorded identity from this one."""
    child = start_child(tmp_path)
    kill9(child)

    owner = json.loads((tmp_path / SENTINEL_NAME).read_text())["owner"]
    assert owner["pid"] == child.pid
    assert owner["started"]


def test_a_restart_restores_the_system_proxy_after_a_kill_9(tmp_path):
    """The acceptance criterion, end to end: kill -9, then start again, and the machine's proxy
    is put back."""
    child = start_child(tmp_path)
    kill9(child)
    commands = FakeCommands()

    message = repair_before_serving(tmp_path, runner=commands)

    assert message is not None
    assert "died without restoring" in message
    assert not (tmp_path / SENTINEL_NAME).exists()


def test_the_restart_restores_the_captured_state_not_an_empty_default(tmp_path):
    """The assertion the machine this was built on demanded.

    It already carried a disabled 127.0.0.1:9090 proxy. A teardown that only ran
    `-setwebproxystate off` would satisfy an assertion against an assumed-empty baseline while
    throwing the server and port away, so the check is against the *captured* prior state — and
    against the ordering, since -setwebproxy turns the proxy back on as a side effect.
    """
    child = start_child(tmp_path)
    kill9(child)
    commands = FakeCommands()

    repair_before_serving(tmp_path, runner=commands)

    for flag in ("-setwebproxy", "-setsecurewebproxy"):
        assert [flag, "Wi-Fi", "127.0.0.1", "9090"] in commands.mutations
        assert [f"{flag}state", "Wi-Fi", "off"] in commands.mutations
        assert commands.mutations.index([flag, "Wi-Fi", "127.0.0.1", "9090"]) < (
            commands.mutations.index([f"{flag}state", "Wi-Fi", "off"])
        )


def test_a_second_restart_has_nothing_left_to_do(tmp_path):
    """Repair is not idempotent by accident — the sentinel is removed, so it cannot re-restore
    stale state over a later, deliberate configuration change."""
    child = start_child(tmp_path)
    kill9(child)
    repair_before_serving(tmp_path, runner=FakeCommands())

    commands = FakeCommands()
    assert repair_before_serving(tmp_path, runner=commands) is None
    assert commands.mutations == []


def test_a_clean_start_reports_nothing(tmp_path):
    """No sentinel means no crash, and a server that announced a repair every time it started
    would train an agent to ignore the message."""
    assert repair_before_serving(tmp_path, runner=FakeCommands()) is None


# --- layer 2 still works, for the orderly case --------------------------------


def test_sigterm_restores_without_needing_a_restart(tmp_path):
    """Layer 3 is the safety net, not the mechanism. A polite shutdown should clean up on its
    own, otherwise every ordinary stop would leave a sentinel for the next start to find."""
    child = start_child(tmp_path)

    child.send_signal(signal.SIGTERM)
    child.wait(timeout=10)

    assert not (tmp_path / SENTINEL_NAME).exists()


def test_a_process_killed_by_sigterm_still_dies_of_sigterm(tmp_path):
    """The handler restores and then re-raises with the default disposition. Swallowing the
    signal would make the server unkillable by ordinary means."""
    child = start_child(tmp_path)

    child.send_signal(signal.SIGTERM)
    child.wait(timeout=10)

    assert child.returncode in (-signal.SIGTERM, 143), child.returncode
