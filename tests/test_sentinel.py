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
import os
import signal
import stat

import pytest

from fake_commands import PROCESS_START, WEB_PROXY_OFF, FakeCommands
from https_proxy_mcp.targets.systemproxy import (
    ProxyHeldElsewhereError,
    ProxyOwner,
    ProxySetting,
    SystemProxy,
    SystemProxyState,
    release_instruction,
    repair_system_proxy,
    resolve_primary_service,
)

#: The prior state every hand-written sentinel below records. Configured but off, which is what
#: the development machine really carried — restoring it needs the server *and* the flag.
PRIOR = SystemProxyState(
    service="Wi-Fi",
    web=ProxySetting(enabled=False, server="127.0.0.1", port=9090),
    secure=ProxySetting(enabled=False, server="127.0.0.1", port=9090),
    bypass=(),
)


@pytest.fixture
def sentinel(tmp_path):
    return tmp_path / "proxy-state.json"


def write_sentinel(path, owner: dict[str, object] | None, *, prior: SystemProxyState = PRIOR):
    """A sentinel as some other process would have left it, owner and all.

    Written as a document rather than through :meth:`SystemProxy.redirect` because the owner is
    the whole subject: these tests need a record naming a process that is *not* this one, which
    a redirect from this process can never produce.
    """
    document: dict[str, object] = dict(prior.to_dict())
    if owner is not None:
        document["owner"] = owner
    path.write_text(json.dumps(document))


def unreadable_start_time(commands: FakeCommands):
    """A runner where reading a process start time fails the way it really does.

    ``ps -o lstart= -p <pid>`` exits 1 with no output for a process that is gone, which
    :func:`run_command` turns into an ``OSError``. Everything else is answered as usual, so the
    mutations a restore issues are still recorded.
    """

    def runner(argv, *, stdin=None):
        if argv[0].endswith("ps"):
            raise OSError("ps: no such process")
        return commands(argv, stdin=stdin)

    return runner


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


# --- ownership ----------------------------------------------------------------
#
# Every editor window on the machine runs its own copy of this server, and they all resolve to the
# same default root, so they all share one sentinel. Without an owner any process repairs, restores
# and deletes any record it finds — which switches off another window's routing and leaves a Session
# reporting that it is recording while receiving nothing.


def test_the_sentinel_records_the_process_that_wrote_it(sentinel):
    """The identity is the whole fix: a record that names nobody belongs to everybody."""
    commands = FakeCommands()

    SystemProxy(sentinel, runner=commands).redirect("127.0.0.1", 8080)

    owner = json.loads(sentinel.read_text())["owner"]
    assert owner["pid"] == os.getpid()
    assert owner["started"] == PROCESS_START.strip()


def test_the_start_time_is_read_through_the_command_runner(sentinel):
    """The same seam as every other read here, which is what keeps these tests off the machine."""
    commands = FakeCommands()

    SystemProxy(sentinel, runner=commands).redirect("127.0.0.1", 8080)

    assert ["/bin/ps", "-o", "lstart=", "-p", str(os.getpid())] in commands.issued


def test_the_start_time_is_compared_for_equality_and_never_parsed(sentinel):
    """It is an opaque string. Parsing it would add a way for the comparison to fail on a format
    this code was never shown, and the only question ever asked of it is "the same one?".

    Two halves, because the first alone does not earn the name: a lenient parser that mapped every
    format it could not read onto one sentinel value would keep it green. The second half asserts
    that two *different* unreadable strings do **not** compare equal, which is the thing such a
    parser could not do.
    """
    commands = FakeCommands({("/bin/ps",): "not a date at all"})
    write_sentinel(sentinel, {"pid": os.getpid(), "started": "not a date at all"})

    assert SystemProxy(sentinel, runner=commands).ownership() == "ours"

    disagreeing = FakeCommands({("/bin/ps",): "another thing entirely"})
    assert SystemProxy(sentinel, runner=disagreeing).ownership() == "dead"


def test_our_own_record_is_ours(sentinel):
    commands = FakeCommands()
    proxy = SystemProxy(sentinel, runner=commands)
    proxy.redirect("127.0.0.1", 8080)

    assert proxy.ownership() == "ours"
    assert proxy.foreign_holder() is None


def test_a_record_whose_owner_is_gone_is_dead(sentinel):
    """`ps` exits non-zero for a process that no longer exists, which is the crash case."""
    commands = FakeCommands()
    write_sentinel(sentinel, {"pid": 4242, "started": PROCESS_START.strip()})

    proxy = SystemProxy(sentinel, runner=unreadable_start_time(commands))

    assert proxy.ownership() == "dead"


def test_a_record_with_no_owner_at_all_is_absent_and_still_repaired(sentinel):
    """There is no sentinel on disk anywhere, so there is no migration to write — but a record
    written by an older build must not become one nobody is allowed to clean up."""
    commands = FakeCommands()
    write_sentinel(sentinel, None)

    proxy = SystemProxy(sentinel, runner=commands)

    assert proxy.ownership() == "absent"
    assert proxy.repair() == PRIOR
    assert not sentinel.exists()


def test_a_live_foreign_process_holds_its_own_record(sentinel):
    commands = FakeCommands()
    write_sentinel(sentinel, {"pid": os.getpid() + 1, "started": PROCESS_START.strip()})

    proxy = SystemProxy(sentinel, runner=commands)

    assert proxy.ownership() == "live_foreign"
    holder = proxy.foreign_holder()
    assert holder == ProxyOwner(pid=os.getpid() + 1, started=PROCESS_START.strip())


def test_the_same_process_id_with_a_different_start_time_is_dead(sentinel):
    """Our own id, recorded by something that is not us — a reboot away from being real."""
    commands = FakeCommands()
    write_sentinel(sentinel, {"pid": os.getpid(), "started": "Sun Aug 16 09:00:00 2026"})

    assert SystemProxy(sentinel, runner=commands).ownership() == "dead"


def test_a_reused_process_id_is_dead_rather_than_a_live_owner(sentinel):
    """The reason the start time exists at all (probe 3).

    A record that survives a reboot names an id that now belongs to something unrelated. With a
    liveness check alone that process answers "alive", the record is respected forever, and a
    self-healing bug becomes a permanent one — strictly worse than the defect being fixed.
    """
    commands = FakeCommands({("/bin/ps",): "Mon Aug 17 09:15:00 2026\n"})
    write_sentinel(sentinel, {"pid": 4242, "started": "Sun Aug 16 09:00:00 2026"})

    proxy = SystemProxy(sentinel, runner=commands)

    assert proxy.ownership() == "dead"
    assert proxy.repair() == PRIOR
    assert not sentinel.exists()


def test_an_unreadable_start_time_restores_connectivity_rather_than_withholding_it(sentinel):
    """Unknown resolves to dead, which is the same direction this module already fails in for a
    record it cannot parse at all: lost fidelity beats a machine that cannot reach the network."""
    commands = FakeCommands({("/bin/ps",): ""})
    write_sentinel(sentinel, {"pid": 4242, "started": PROCESS_START.strip()})

    proxy = SystemProxy(sentinel, runner=commands)

    assert proxy.ownership() == "dead"
    assert proxy.repair() == PRIOR


def test_an_empty_start_time_never_matches_an_empty_start_time(sentinel):
    """Both sides unknown must not look like a match, or two unrelated processes are one."""
    commands = FakeCommands({("/bin/ps",): ""})
    write_sentinel(sentinel, {"pid": os.getpid(), "started": ""})

    assert SystemProxy(sentinel, runner=commands).ownership() == "dead"


# --- what each path does with a record it does not own ------------------------


def test_a_start_is_refused_and_promises_only_that_the_refusal_lifts(sentinel):
    """The fourth path, and the only one that refuses rather than declining quietly.

    The other three have nothing the caller wanted that they now cannot do, so they are silent.
    Starting a Session is different, and the message is the entire remedy — there is deliberately
    no force-release tool, because the holder is the only process that can put the machine back
    correctly. So the promise has to be exact: signalling the holder lifts *this* refusal, and
    whatever else the retried call needs will say so for itself. Asserted here as well as through
    ``test_two_windows.py``'s real second process, because that harness reads the message for the
    pid and the signal, and the strength of the promise could be reverted underneath it.
    """
    commands = FakeCommands()
    holder = os.getpid() + 1
    write_sentinel(sentinel, {"pid": holder, "started": PROCESS_START.strip()})

    with pytest.raises(ProxyHeldElsewhereError) as refusal:
        SystemProxy(sentinel, runner=commands).refuse_if_held()

    # Carried as a value as well as quoted in the message, so a caller reporting the pid does not
    # have to parse one back out of prose.
    assert refusal.value.holder == ProxyOwner(pid=holder, started=PROCESS_START.strip())
    assert f"kill {holder}" in str(refusal.value)
    assert "succeeds" not in str(refusal.value)
    assert "no longer applies" in str(refusal.value)
    assert commands.mutations == []
    assert sentinel.exists()


def test_repair_at_start_leaves_a_live_foreign_record_alone(sentinel, tmp_path):
    """A new window starting up must not un-route the window that is recording."""
    commands = FakeCommands()
    write_sentinel(sentinel, {"pid": os.getpid() + 1, "started": PROCESS_START.strip()})

    message = repair_system_proxy(tmp_path, runner=commands)

    assert commands.mutations == []
    assert sentinel.exists()
    assert message is not None and str(os.getpid() + 1) in message


def test_restore_on_stop_leaves_a_live_foreign_record_alone(sentinel):
    """`stop_capture` reaches this through the target's teardown. Restoring here would put the
    machine back while another process is still routing traffic at a proxy it owns."""
    commands = FakeCommands()
    write_sentinel(sentinel, {"pid": os.getpid() + 1, "started": PROCESS_START.strip()})

    assert SystemProxy(sentinel, runner=commands).restore() is None
    assert commands.mutations == []
    assert sentinel.exists()


def test_restoring_on_exit_leaves_a_live_foreign_record_alone(sentinel):
    """atexit and both signal handlers share this callback, so this covers the exit and the
    signal paths — the same argument the handler test above makes."""
    commands = FakeCommands()
    write_sentinel(sentinel, {"pid": os.getpid() + 1, "started": PROCESS_START.strip()})

    SystemProxy(sentinel, runner=commands).restore_on_exit()

    assert commands.mutations == []
    assert sentinel.exists()


def test_the_signal_handler_leaves_a_live_foreign_record_alone(sentinel):
    """The signal path, through the handler that is actually installed.

    Driven with a callable previous disposition on purpose: the handler chains to it and returns,
    where the default disposition would re-raise and take the test runner down with it. That branch
    is why this can be asserted at all, and it is the only coverage `_make_signal_handler` has.
    """
    commands = FakeCommands()
    write_sentinel(sentinel, {"pid": os.getpid() + 1, "started": PROCESS_START.strip()})
    proxy = SystemProxy(sentinel, runner=commands)
    chained = []

    handler = proxy._make_signal_handler(
        signal.SIGTERM, lambda received, frame: chained.append(received)
    )
    handler(signal.SIGTERM, None)

    assert commands.mutations == []
    assert sentinel.exists()
    # The signal still does what it was going to do, which is the other half of this handler.
    assert chained == [signal.SIGTERM]


def test_a_dead_owners_record_is_still_repaired_at_the_next_start(sentinel, tmp_path):
    """Crash recovery is unchanged: a dead owner is the case this machinery already existed for."""
    commands = FakeCommands()
    write_sentinel(sentinel, {"pid": 4242, "started": PROCESS_START.strip()})

    message = repair_system_proxy(tmp_path, runner=unreadable_start_time(commands))

    assert message is not None and "died without restoring" in message
    assert ["-setwebproxy", "Wi-Fi", "127.0.0.1", "9090"] in commands.mutations
    assert not sentinel.exists()


def test_a_corrupt_record_naming_a_live_owner_still_unproxies_the_machine(sentinel, tmp_path):
    """Ownership cannot be read out of a record that cannot be parsed, so the existing fail-safe
    wins: an owner key is not enough to make an unreadable record someone's property."""
    sentinel.write_text(
        json.dumps({"owner": {"pid": os.getpid() + 1, "started": PROCESS_START.strip()}})
    )
    commands = FakeCommands()

    repair_system_proxy(tmp_path, runner=commands)

    assert ["-setwebproxystate", "Wi-Fi", "off"] in commands.mutations
    assert ["-setsecurewebproxystate", "Wi-Fi", "off"] in commands.mutations
    assert not sentinel.exists()
    assert sentinel.with_suffix(".corrupt").exists()


# --- the report of who holds it -------------------------------------------------
#
# `report()` composes, from one sentinel read and one ownership resolution, what `get_status`
# and `diagnose_capture` both need: the sentence and the holder it names. Pinned here directly
# as well as through the tool surface in `test_tools_trust.py`, because composing the report
# beside the sentinel made this unit seam the natural home for the four ownership states and
# the absent-vs-null discipline.


def test_a_report_for_a_machine_that_is_not_redirected(sentinel):
    """Absent, not null: with no redirect there is no service, sentinel or owner to be null
    *about*, so the report carries exactly two keys."""
    report = SystemProxy(sentinel, runner=FakeCommands()).report(recording=False)

    assert report.redirected is False
    assert report.holder is None
    assert report.to_dict() == {
        "redirected": False,
        "detail": "the system proxy is not redirected",
    }


def test_a_report_on_a_record_this_process_owns(sentinel):
    """``ours`` is the field an agent acts on: stop_capture puts back a redirect this process
    made, and nothing else."""
    commands = FakeCommands()
    proxy = SystemProxy(sentinel, runner=commands)
    proxy.redirect("127.0.0.1", 8080)

    report = proxy.report(recording=True)

    assert report.ours is True
    assert report.owner is not None and report.owner.pid == os.getpid()
    # The holder is set only for a live foreign owner — the three safe states never carry one.
    assert report.holder is None
    assert "on stop_capture" in report.detail
    assert report.to_dict()["owner"] == {"pid": os.getpid(), "ours": True}


def test_a_report_on_a_record_a_dead_process_left_behind(sentinel):
    """A crash leftover: reported as not ours, so an agent does not read it as a Session it can
    stop, and no holder — there is no live process to name."""
    commands = FakeCommands()
    write_sentinel(sentinel, {"pid": 4242, "started": PROCESS_START.strip()})

    report = SystemProxy(sentinel, runner=unreadable_start_time(commands)).report(recording=False)

    assert report.ours is False
    assert report.holder is None
    assert "no longer running" in report.detail
    assert report.to_dict()["owner"] == {"pid": 4242, "ours": False}


def test_a_report_on_a_record_that_names_no_owner(sentinel):
    """The absent state: nobody is reported rather than somebody invented, which is the same
    fail-safe direction the repair path takes."""
    write_sentinel(sentinel, None)

    report = SystemProxy(sentinel, runner=FakeCommands()).report(recording=False)

    assert report.owner is None
    assert report.ours is False
    assert report.holder is None
    assert "does not name the process" in report.detail
    assert report.to_dict()["owner"] is None


def test_a_report_on_a_live_foreign_record_carries_the_holder(sentinel):
    """The one state with a holder: ``diagnose_capture`` needs the process, not just the prose
    that quotes it."""
    commands = FakeCommands()
    holder = os.getpid() + 1
    write_sentinel(sentinel, {"pid": holder, "started": PROCESS_START.strip()})

    report = SystemProxy(sentinel, runner=commands).report(recording=False)

    assert report.holder == ProxyOwner(pid=holder, started=PROCESS_START.strip())
    assert report.ours is False
    assert report.to_dict()["owner"] == {"pid": holder, "ours": False}
    # The holder is never serialised: `owner` already carries what a client acts on, and one
    # pid in two wire shapes invites reading the wrong one.
    assert "holder" not in report.to_dict()


def test_the_live_foreign_sentence_branches_on_whether_a_session_is_recording(sentinel):
    """The only server-side fact the prose needs, crossing the seam as a bool.

    "This Session does not own it" names something that does not exist when nothing is
    recording here — which is the state ``diagnose_capture`` borrows the sentence in.
    """
    commands = FakeCommands()
    write_sentinel(sentinel, {"pid": os.getpid() + 1, "started": PROCESS_START.strip()})
    proxy = SystemProxy(sentinel, runner=commands)

    recording = proxy.report(recording=True)
    idle = proxy.report(recording=False)

    assert "This Session does not own it, so stopping here leaves it in place" in recording.detail
    assert "Nothing here owns it, so there is nothing to stop here" in idle.detail


def test_a_report_resolves_ownership_exactly_once(sentinel):
    """One sentinel read and one ``ps`` invocation per report, so the sentence and the holder
    always come from the same read and the verdict cannot contradict its own detail."""
    commands = FakeCommands()
    write_sentinel(sentinel, {"pid": os.getpid() + 1, "started": PROCESS_START.strip()})

    SystemProxy(sentinel, runner=commands).report(recording=False)

    reads = [argv for argv in commands.issued if argv[0].endswith("ps")]
    assert len(reads) == 1


def test_a_report_on_a_record_it_cannot_read_nulls_what_is_unknown(sentinel):
    """Every key the readable branch carries, so a caller reads one shape either way.

    The defect was absent keys: a KeyError raised in exactly the branch that says something is
    wrong. What cannot be known is null; the path is reported, because it is what a human needs
    to go and look at the file.
    """
    sentinel.write_text("{ this is not json")

    report = SystemProxy(sentinel, runner=FakeCommands()).report(recording=False)
    reported = report.to_dict()

    assert reported["redirected"] is True
    assert reported["service"] is None
    assert reported["owner"] is None
    assert reported["sentinel"] == str(sentinel)
    assert "could not be read" in reported["detail"]


def test_the_release_instruction_is_one_sentence_with_the_pid_in_it():
    """The actionable kernel of the ownership model, composed once for every call site — the
    multi-wording drift this prevents already had to be fixed once."""
    assert (
        release_instruction(4242)
        == "`kill 4242` makes that process restore the machine on its way out"
    )
