"""Two editor windows, as two real processes: what each of them may do to the other's redirect.

Every editor window on the machine runs its own copy of this server, and they all resolve to the
same default root, so they all share one record of the machine's prior proxy settings. Three call
sites acted on any record they found — the repair at server start, ``prepare()`` before a Session,
and the restore on stop — which is how one window switched another window's routing off and left
it reporting that it was recording while receiving nothing.

``test_sentinel.py`` asserts the ownership arithmetic against a fake process table, and cannot
assert the case that matters here: the whole point of a live foreign owner is that another process
is genuinely alive, which a single-process fake can only claim. So this file borrows the
crash-recovery child from ``test_kill9_recovery.py`` — a real process that redirects through a
**stubbed** command runner and then waits. Nothing here touches the developer's networking; the
only real binary either side runs is ``ps``, which reads the process table and changes nothing.

There is deliberately no force-release tool. Signalling the holder makes *it* restore the machine
on its way out, which is both sufficient and more correct than restoring another owner's recorded
state from this process — that being the same disregard for ownership that caused the defect.

The refusal reaches the caller as a tool error, translated by the core from the plain error a
Capture Target raises. That the target layer keeps no dependency on the tool framework is asserted
with the rest of the target contract, in ``test_targets.py``, since it needs no second process.
"""

from __future__ import annotations

import contextlib
import json
import signal
import subprocess

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from fake_commands import FakeCommands
from https_proxy_mcp.capture.session import SESSIONS_DIRNAME
from https_proxy_mcp.server import build_server, repair_before_serving
from https_proxy_mcp.server.state import CaptureState
from https_proxy_mcp.targets import build_target
from https_proxy_mcp.targets.systemproxy import SENTINEL_NAME
from stub_runtime import StubRuntime
from test_kill9_recovery import child_mutations, kill9, start_child
from tool_calls import call

pytestmark = pytest.mark.integration


def real_process_table(commands: FakeCommands):
    """A runner that stubs ``networksetup`` and ``scutil`` but reads the real process table.

    The child records its own start time from a real ``ps``, so ownership can only be resolved
    against the real one: :data:`fake_commands.PROCESS_START` would disagree with it, the live
    child would resolve to *dead*, and every assertion here would be about the wrong branch while
    still passing for the wrong reason.

    Non-zero exits answer empty rather than raising, matching the child's own stub. Either way a
    start time that cannot be read means *unknown*, and unknown resolves to dead.
    """

    def runner(argv, *, stdin=None):
        if argv[0].endswith("ps"):
            return subprocess.run(argv, capture_output=True, text=True, check=False).stdout
        return commands(argv, stdin=stdin)

    return runner


@pytest.fixture
def commands():
    """The stub every call in *this* process goes through, and the record of what it was asked."""
    return FakeCommands()


@pytest.fixture
def holder(tmp_path):
    """A live foreign owner: a real second process holding the (stubbed) system proxy.

    Killed rather than terminated on the way out, because SIGTERM is what two of these tests
    measure — a signal in teardown would race the assertion it exists to make.
    """
    child = start_child(tmp_path)
    yield child
    if child.poll() is None:
        kill9(child)


@pytest.fixture
def state(tmp_path, commands):
    """This window: a server whose root is the same one the holder wrote its record into."""
    return CaptureState(tmp_path, runtime_factory=StubRuntime, runner=real_process_table(commands))


@pytest.fixture
def server(state):
    return build_server(state=state)


def record(tmp_path) -> dict:
    return json.loads((tmp_path / SENTINEL_NAME).read_text())


# --- a window starting up ------------------------------------------------------


def test_a_starting_server_leaves_a_live_holders_redirect_untouched(tmp_path, holder, commands):
    """The first of the three careless call sites. This ran against live captures: every window
    on the machine calls it, and it used to restore and delete whatever record it found."""
    before = record(tmp_path)

    repair_before_serving(tmp_path, runner=real_process_table(commands))

    assert commands.mutations == []
    assert record(tmp_path) == before
    assert holder.poll() is None, "the holder died, so this proved nothing about a live owner"


def test_a_starting_server_reports_the_live_holder_it_found(tmp_path, holder, commands):
    """Reported rather than silently skipped: "another window holds your machine's proxy" is the
    fact that ends the investigation, and it is invisible from inside this process."""
    message = repair_before_serving(tmp_path, runner=real_process_table(commands))

    assert message is not None
    assert f"pid {holder.pid}" in message
    assert f"kill {holder.pid}" in message


# --- a window stopping --------------------------------------------------------


def test_a_stop_leaves_a_live_holders_redirect_untouched(tmp_path, holder, commands):
    """The third call site, reached exactly as ``stop_capture`` reaches it: the target's teardown.

    Restoring here would put the machine back while the holder is still routing traffic at a proxy
    it believes it owns — the failure direction that leaves a Session reporting that it is
    recording while nothing arrives.
    """
    before = record(tmp_path)
    target = build_target("ios-simulator", root=tmp_path, runner=real_process_table(commands))

    target.teardown()

    assert commands.mutations == []
    assert record(tmp_path) == before
    assert holder.poll() is None


# --- a window starting a Session ----------------------------------------------


async def test_a_session_start_is_refused_while_a_live_holder_has_the_proxy(server, holder):
    """The second call site, and the one that has to refuse rather than tidy up.

    Taking the redirect over is the original defect with better manners, and waiting for the
    holder would hang a JSON-RPC session — which this project treats as the worst available
    failure, for the same reason ``sudo`` here is always ``-n``.
    """
    with pytest.raises(ToolError):
        await server.call_tool("start_capture", {"target": "ios-simulator", "port": 0})


async def test_the_refusal_names_the_holder_and_the_signal_that_releases_it(server, holder):
    """A diagnosis with no remedy is half an answer. The remedy is a signal to the named process,
    because that makes the holder restore the machine itself."""
    with pytest.raises(ToolError) as refusal:
        await server.call_tool("start_capture", {"target": "ios-simulator", "port": 0})

    assert f"pid {holder.pid}" in str(refusal.value)
    assert f"kill {holder.pid}" in str(refusal.value)


async def test_the_refusal_happens_before_anything_is_arranged(server, tmp_path, holder, commands):
    """*Where* the refusal happens, which the message alone does not say.

    Refusing at ``prepare()`` is refusing before a Session exists. A refusal further along — and
    the machine has always had one, since ``redirect()`` will not overwrite a record — has already
    created a Session directory and, worse, **pruned** the retained ones, which are the holder's:
    two windows share the root, so a start this one never completed would have deleted the other
    one's oldest artifacts. So the assertion is that nothing was created rather than only that
    nothing was recorded.
    """
    before = record(tmp_path)

    with pytest.raises(ToolError):
        await server.call_tool("start_capture", {"target": "ios-simulator", "port": 0})

    assert not (tmp_path / SESSIONS_DIRNAME).exists()
    assert commands.mutations == []
    assert record(tmp_path) == before
    assert holder.poll() is None
    assert (await call(server, "get_status"))["recording"] is False


async def test_no_tool_on_the_surface_releases_a_redirect_this_process_does_not_own(
    server, tmp_path, holder, commands
):
    """There is deliberately no force-release tool, and this is the sweep that says so.

    Every registered tool is called with no arguments; the ones that need a Session or an argument
    refuse, which is the point — none of them may touch the holder's record on the way. The exact
    tool set is pinned by ``test_server.py::test_the_tool_surface_is_registered``, so a tool that
    did offer to force a release could not appear unnoticed either.
    """
    before = record(tmp_path)

    for tool in await server.list_tools():
        # Broad on purpose: what is under test is the side effect, not each tool's own contract.
        with contextlib.suppress(Exception):
            await server.call_tool(tool.name, {})

    assert commands.mutations == []
    assert record(tmp_path) == before
    assert holder.poll() is None


# --- the escape hatch ---------------------------------------------------------


def test_signalling_the_holder_makes_it_restore_the_machine(tmp_path, holder):
    """Why no force-release tool is needed. The holder installed handlers, so the signal a
    developer would reach for anyway makes it put the machine back on its way out — its own
    recorded prior state, restored by the process that recorded it.

    Asserted on the commands the holder issued, not merely on its sentinel being gone: a child
    that deleted the file and restored nothing would satisfy that, and the prior state here is the
    configured-but-disabled proxy this machine really carried, which needs both commands in order.
    """
    holder.send_signal(signal.SIGTERM)
    holder.wait(timeout=10)

    mutations = child_mutations(tmp_path)
    for flag in ("-setwebproxy", "-setsecurewebproxy"):
        assert [flag, "Wi-Fi", "127.0.0.1", "9090"] in mutations
        assert [f"{flag}state", "Wi-Fi", "off"] in mutations
        assert mutations.index([flag, "Wi-Fi", "127.0.0.1", "9090"]) < mutations.index(
            [f"{flag}state", "Wi-Fi", "off"]
        )
    assert not (tmp_path / SENTINEL_NAME).exists()


def test_a_repair_after_the_holder_has_gone_finds_nothing_to_do(tmp_path, holder, commands):
    """The other half of the escape hatch: once the holder has released it, the next window starts
    clean rather than reporting a repair it did not have to make."""
    holder.send_signal(signal.SIGTERM)
    holder.wait(timeout=10)

    assert repair_before_serving(tmp_path, runner=real_process_table(commands)) is None
    assert commands.mutations == []


def test_an_uncatchable_kill_leaves_the_record_behind_for_the_next_start(
    tmp_path, holder, commands
):
    """The existing guarantee, re-asserted under ownership and across two processes.

    ``kill -9`` runs no handler, so the holder's record stays on disk naming a process that is now
    gone — and a dead owner is exactly the case the repair layer already existed for. Ownership had
    to leave this direction alone, because withholding a repair is how a self-healing bug becomes a
    permanent one; the id-reuse half of that argument is asserted against a fake process table in
    ``test_sentinel.py``, which can pin a start time this test cannot.
    """
    kill9(holder)
    assert record(tmp_path)["owner"]["pid"] == holder.pid

    message = repair_before_serving(tmp_path, runner=real_process_table(commands))

    assert message is not None and "died without restoring" in message
    assert ["-setwebproxy", "Wi-Fi", "127.0.0.1", "9090"] in commands.mutations
    assert not (tmp_path / SENTINEL_NAME).exists()
