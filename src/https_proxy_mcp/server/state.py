"""The server's one piece of mutable state: whether a Session is recording, and which.

Everything else in this package is a pure function of a store. This is where the Session,
the ExchangeStore, the running proxy and the Capture Target are held together, and where the
ordering that matters is written down — bring the proxy up *before* routing traffic at it,
and stop routing *before* taking it down, so nothing is ever aimed at a dead port.

Errors here are :class:`ToolError`, which means an agent reads the message. They are written
to be acted on: "not recording, call start_capture first" beats "NoneType has no attribute",
and an Exchange that was evicted says so rather than looking like one that never existed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError

from https_proxy_mcp.capture import Exchange, ExchangeStore
from https_proxy_mcp.capture.filtering import exchange_matcher
from https_proxy_mcp.capture.reporting import ReportingFilter
from https_proxy_mcp.capture.runtime import ProxyRuntime
from https_proxy_mcp.capture.session import (
    CONFDIR_NAME,
    Session,
    default_root,
    prune_sessions,
)
from https_proxy_mcp.eventsources.explicit import StepLog
from https_proxy_mcp.runs.registry import Run, RunRegistry
from https_proxy_mcp.targets import (
    AuthorityCensus,
    CaptureObservations,
    CaptureTarget,
    build_target,
)
from https_proxy_mcp.targets.ios_simulator import IosSimulatorTarget
from https_proxy_mcp.targets.systemproxy import (
    CommandRunner,
    Ownership,
    ProxyHeldElsewhereError,
    ProxyOwner,
    SystemProxy,
    SystemProxyState,
    resolve_ownership,
    run_command,
    sentinel_path,
)

NOT_RECORDING = "not recording; call start_capture first"


class CaptureState:
    """Holds the running Session, or nothing.

    ``runtime_factory`` exists so the read tools can be tested without binding a port. It is
    a seam, not a backdoor: production always gets :class:`ProxyRuntime`.
    """

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        runtime_factory: Callable[..., Any] = ProxyRuntime,
        runner: CommandRunner = run_command,
    ) -> None:
        self._root = Path(root) if root is not None else default_root()
        self._runtime_factory = runtime_factory
        # Handed to every target that shells out, so a test can drive the whole tool surface
        # without networksetup or simctl touching the machine.
        self._runner = runner
        self._session: Session | None = None
        self._store: ExchangeStore | None = None
        self._runtime: Any | None = None
        self._target: CaptureTarget | None = None
        self._trust: dict[str, Any] | None = None
        self._runs: RunRegistry | None = None
        # The Lens (CONTEXT.md), compiled once at `start` so a bad expression is refused there.
        # Late-binding on the store: it is compiled before the store exists and never called
        # until after, because the only thing that calls it is a listing of what the store holds.
        self._lens: Callable[[Exchange], bool] | None = None
        # Explicit marks belong to the Session rather than to a Run: a human marking Steps by hand
        # has no Run at all, and a Run selects the marks inside its own window.
        self._steps: StepLog | None = None

    # --- accessors ------------------------------------------------------------

    @property
    def recording(self) -> bool:
        return self._session is not None

    @property
    def store(self) -> ExchangeStore:
        """The recording Session's store, or a message saying how to get one."""
        if self._store is None:
            raise ToolError(NOT_RECORDING)
        return self._store

    @property
    def scope(self) -> tuple[str, ...] | None:
        """The recording Session's Scope, or ``None`` when there is none."""
        return self._session.scope if self._session is not None else None

    @property
    def lens(self) -> str | None:
        """The recording Session's Lens expression, or ``None`` when there is none."""
        return self._session.lens if self._session is not None else None

    @property
    def lens_predicate(self) -> Callable[[Exchange], bool] | None:
        """The compiled Lens, for the one caller that is not a listing.

        Every listing gets the Lens through :meth:`reporting`, which is where it belongs. But
        ``wait_for_exchanges`` is exempt from the Scope and the denylist on purpose — an explicit
        question should not be vetoed — and it still has to obey the Lens, so it needs the Lens
        without the rest. Exposed narrowly rather than by relaxing that exemption, because the
        exemption is right and the Lens is the exception to it.
        """
        return self._lens

    @property
    def session(self) -> Session:
        """The recording Session, or a message saying how to get one."""
        if self._session is None:
            raise ToolError(NOT_RECORDING)
        return self._session

    @property
    def runs(self) -> RunRegistry:
        """The Runs of the recording Session."""
        if self._runs is None:
            raise ToolError(NOT_RECORDING)
        return self._runs

    @property
    def steps(self) -> StepLog:
        """The explicit Steps marked during the recording Session."""
        if self._steps is None:
            raise ToolError(NOT_RECORDING)
        return self._steps

    def run_or_session(self, run_id: str | None, *, default_to_latest: bool = True) -> Run:
        """The Run a tool was asked about, naming the alternatives when it does not exist.

        What ``None`` means differs by tool, and deliberately so. A **Timeline** is about a Run, so
        it defaults to the most recent one; an **export** is an artifact of everything captured, so
        it defaults to the whole Session (SPEC.md). Collapsing the two would make one of them
        surprising, and ``export_har`` silently exporting only the last few milliseconds of a Run
        is the more expensive surprise.

        With no Run started at all, both fall back to a stand-in covering the Session — which is
        what lets an agent mark Steps by hand and never call ``run_test``.
        """
        registry = self.runs
        if run_id is not None:
            found = registry.get(run_id)
            if found is None:
                known = ", ".join(item.id for item in registry.all()) or "none yet"
                raise ToolError(f"no Run {run_id!r} in this Session; Runs so far: {known}")
            return found

        if default_to_latest:
            return registry.latest() or self._whole_session_run()
        return self._whole_session_run()

    def _whole_session_run(self) -> Run:
        """A stand-in Run spanning the Session, for a Timeline asked for without one.

        Not registered, and deliberately open-ended: its window starts when the Session did and
        never closes, so everything captured is in scope.
        """
        session = self.session
        return Run(
            id="session",
            dir=session.dir,
            command=(),
            t_start=session.started,
            label=f"whole Session {session.id}",
        )

    @property
    def root(self) -> Path:
        """Where Sessions, the sentinel and the shared confdir live."""
        return self._root

    @property
    def runner(self) -> CommandRunner:
        """How this state shells out. Exposed so the trust tools share the test seam."""
        return self._runner

    def build_target(self, name: str, *, udid: str | None = None) -> CaptureTarget:
        """A Capture Target by name, wired to this state's root and command runner.

        Public because the trust tools need one *without* a Session: an agent should be able to
        ask whether a device trusts the authority, or install it, before committing to a capture.
        """
        try:
            return build_target(name, udid=udid, root=self._root, runner=self._runner)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    def observations(self) -> CaptureObservations:
        """What the store knows and a target cannot: whether anything is arriving.

        ``reported`` is counted against the same narrowing a listing applies, which is what lets
        a diagnosis say "traffic arrived and the Scope hid it" rather than "no traffic".
        """
        if self._store is None:
            return CaptureObservations()

        store = self._store
        reporting = self.reporting()
        reported, _ = reporting.apply(list(store))

        # A handshake failure is only evidence about the app under test when a Scope says which
        # hosts that is. Redirecting the system proxy redirects the whole machine, and the
        # authority is installed into the simulator's trust store alone, so every other
        # application on the developer's Mac fails against the proxy — the live P3 run produced
        # discord.com, figma.com and unleash.codeium.com, none of them remotely related to the
        # capture. In regular proxy mode they are indistinguishable from the app (ADR-0002), and
        # no denylist can help: it would have to enumerate every app a developer might run.
        #
        # So without a Scope there is no attributable evidence, and claiming pinning would fire on
        # a capture that is working perfectly. Failures with no SNI are excluded for the same
        # reason — a failure that cannot be tied to a host cannot be tied to the app either.
        # Collected as hosts rather than failures because the host is the only part either
        # field below uses, and gathering them here is what makes the `if failure.host` guard
        # visible to a type checker at the point of use.
        relevant_hosts = (
            [
                failure.host
                for failure in store.tls_failures
                if failure.host and reporting.allows_host(failure.host)
            ]
            if self.scope is not None
            else []
        )

        return CaptureObservations(
            exchanges=store.recorded_count,
            tls_failures=len(relevant_hosts),
            tls_failures_total=store.tls_failure_count,
            # Deduplicated, order preserved: a pinned app retries, and the same host repeated
            # forty times says nothing the first did not.
            tls_failure_hosts=tuple(dict.fromkeys(relevant_hosts)),
            reported=len(reported),
            # Not a count, and the only thing here a *narrowing* cannot explain: under Passthrough
            # HTTPS traffic outside the Scope was never decrypted, so its absence is not something
            # any listing filter hid. A target cannot know this — it never sees the Scope.
            passthrough=self._session is not None and self._session.passthrough,
        )

    def authority_census(self, target: CaptureTarget | None = None) -> AuthorityCensus:
        """How many booted devices trust the Interception Authority.

        Asked of the target rather than answered here, and asked only of the one target that can
        answer it. ``isinstance`` rather than a protocol method, because a trust store per device
        is an iOS Simulator fact: ``CurlTarget`` has nothing to count, and a protocol method it
        had to stub would be ceremony dressed as a contract.

        An empty census is the answer for every other target, and says nothing — which is correct,
        not a silent failure. Nothing else on this machine has a per-device trust store to read.
        """
        subject = target if target is not None else self._target
        if isinstance(subject, IosSimulatorTarget):
            return subject.authority_census()
        return AuthorityCensus()

    def diagnose(self) -> dict[str, Any]:
        """Why capture is or is not working, as ``diagnose_capture`` reports it.

        Answers rather than raises when nothing is recording. This is the tool an agent reaches
        for when it is already confused, so failing here would answer "why is nothing working?"
        with one more thing that does not work.
        """
        if not self.recording or self._target is None:
            # "Call start_capture" is the wrong advice in exactly one case, and it is the case
            # this tool is reached from: another live process holds the machine's proxy, so
            # start_capture has already been refused and calling it again is refused identically.
            # The cause is on disk and process-independent, so it needs no Session and no target.
            #
            # One read, not two. Asking `_foreign_holder` and then asking `proxy_state` resolved
            # ownership twice, so a holder that exited in between answered `proxy_held_elsewhere`
            # with the dead-owner sentence — a verdict contradicting its own detail.
            proxy, holder = self._proxy_report()
            if holder is not None:
                return {
                    "status": "proxy_held_elsewhere",
                    # Deliberately not NOT_RECORDING: its "call start_capture first" is the advice
                    # this branch exists to withhold. What follows is the sentence `proxy_state`
                    # already composes for this ownership state, rather than a second wording of one
                    # fact — it names the service, the holder's pid and the signal that releases it.
                    "detail": f"not recording here, and {proxy['detail']}",
                    "remediation": (
                        f"`kill {holder.pid}` makes that process restore the machine on its way"
                        " out, after which this refusal no longer applies. It is not released here:"
                        " restoring a redirect this process does not own would leave that process"
                        " recording nothing"
                    ),
                    "target": None,
                    "observed": _observed_dict(CaptureObservations()),
                    # Null, not zero: no Capture Target was built here, so no trust store was
                    # read. See `_census_dict`.
                    **_census_dict(None, capture_target_trusts=False),
                }
            return {
                "status": "no_connect",
                "detail": NOT_RECORDING,
                "remediation": "call start_capture, then drive the app and try again",
                # Null rather than absent, so every verdict answers with the same keys. A caller
                # reading `result["target"]` got a KeyError from exactly the two branches that say
                # something is wrong, which is how an agent concludes nothing is.
                "target": None,
                "observed": _observed_dict(CaptureObservations()),
                **_census_dict(None, capture_target_trusts=False),
            }

        observed = self.observations()
        diagnosis = self._target.diagnose(observed)
        census = self.authority_census()
        return {
            "status": diagnosis.status,
            "detail": diagnosis.detail,
            "remediation": diagnosis.remediation,
            "target": self._session.target if self._session else None,
            # Reported so an agent can check the reasoning rather than having to trust it.
            "observed": _observed_dict(observed),
            # Every verdict carries it, not just `tls_failed`. A second trusting device explains
            # an unexpected row as readily as it explains a handshake failure, and a verdict that
            # mentioned it only when handshakes failed would teach an agent it means nothing else.
            **_census_dict(
                census,
                capture_target_trusts=bool(self._trust and self._trust.get("trusted")),
            ),
        }

    def reporting(
        self,
        *,
        include_denied: bool = False,
        window: tuple[float, float | None] | None = None,
        hosts: tuple[str, ...] | None = None,
    ) -> ReportingFilter:
        """How a listing narrows what it shows: the Run window, the Session's Scope, the denylist.

        Built per call rather than held, because ``include_denied``, ``window`` and ``hosts`` are
        per-call decisions and a cached filter would quietly ignore them. ``hosts`` joins
        ``window`` as one of those: the ``host_filter`` a single call asked for, intersecting
        with the Scope rather than replacing it. The Lens is not one of them: it
        belongs to the Session, and every caller of this method gets it whether or not it knew to
        ask. That is the whole reason it is here and not a parameter on the tools — a narrowing an
        agent has to remember on each call is one it will forget on the call that mattered.
        """
        return ReportingFilter(
            scope=self.scope,
            include_denied=include_denied,
            window=window,
            lens=self._lens,
            hosts=hosts,
        )

    def exchange(self, exchange_id: int) -> Exchange:
        """One Exchange, distinguishing "never existed" from "evicted"."""
        store = self.store
        found = store.get(exchange_id)
        if found is not None:
            return found

        if 0 < exchange_id <= store.recorded_count:
            raise ToolError(
                f"Exchange {exchange_id} was evicted from the in-memory buffer"
                f" ({store.evicted_count} evicted so far); its metadata is still in"
                f" {store.log_path.name} in the Session directory"
            )
        raise ToolError(
            f"no Exchange {exchange_id} in this Session; {store.recorded_count} have been recorded"
        )

    # --- lifecycle ------------------------------------------------------------

    async def start(
        self,
        *,
        target: str,
        port: int,
        scope: list[str] | None,
        udid: str | None = None,
        passthrough: bool = True,
        lens: str | None = None,
        capacity: int = ExchangeStore.DEFAULT_CAPACITY,
    ) -> dict[str, Any]:
        """Begin a Session: prune, prepare the target, start the proxy, then route to it."""
        # Spelled out rather than `self.recording`, which is exactly this expression: the
        # property hides the narrowing that makes `_session.id` below safe to read.
        if self._session is not None:
            raise ToolError(
                f"already recording into Session {self._session.id}; call stop_capture first"
            )

        # Compiled before anything is arranged, so a typo in an expression cannot be discovered
        # after the machine's proxy has been redirected and an authority installed into a device.
        # It closes over `self.store` rather than over a store that does not exist yet; nothing
        # calls it until a listing does, by which time one does.
        lens_predicate: Callable[[Exchange], bool] | None = None
        if lens:
            try:
                lens_predicate = exchange_matcher(
                    lens, lambda exchange, side: self.store.read_body(exchange, side)
                )
            except ValueError as exc:
                raise ToolError(str(exc)) from None

        capture_target = self.build_target(target, udid=udid)

        # prepare() is where a target repairs state a dead predecessor left behind, so it runs
        # before anything else can trip over it — and where it refuses when the predecessor is
        # still alive. Translated here because a Capture Target raises a plain error: the protocol
        # is what makes another target an addition rather than a refactor, and it would stop being
        # that the moment implementing it meant importing the MCP framework.
        try:
            capture_target.prepare()
        except ProxyHeldElsewhereError as exc:
            raise ToolError(str(exc)) from None
        trust = capture_target.ensure_trusted()

        # Passthrough (CONTEXT.md). The Scope's hosts are the ones decrypted; every other connection
        # is forwarded unopened and reaches its destination with the real certificate, so a browser
        # or a second simulator that never trusted the Interception Authority is untouched by a
        # capture it was never part of. Two things are deliberate here:
        #
        # * It takes the **Scope's** hosts rather than a list of its own. An agent keeping two
        #   nearly-identical host lists in step is a trap, and in practice they are always the same.
        # * An empty Scope is not a set of hosts to decrypt. It reports nothing (`reporting.py`),
        #   and passing everything through would record nothing at all — whereas intercepting
        #   everything leaves the traffic reachable by id and in an export.
        intercept_hosts = tuple(scope) if passthrough and scope else None
        session = Session.create(
            self._root,
            target=target,
            port=port,
            scope=scope,
            udid=udid,
            lens=lens,
            # The condition, not the parameter: `passthrough=True` with no Scope decrypts
            # everything, and a manifest claiming otherwise would send a later reader looking for
            # traffic that was recorded all along.
            passthrough=intercept_hosts is not None,
        )
        # Pruned *after* creating, so the invariant is exactly "RETAINED_SESSIONS remain"
        # rather than that many plus the new one. The new Session is the newest, so it is
        # never the one removed.
        prune_sessions(self._root)
        store = ExchangeStore(session.dir, capacity=capacity)
        runtime = self._runtime_factory(
            store,
            port=port,
            confdir=self._root / CONFDIR_NAME,
            intercept_hosts=intercept_hosts,
        )

        try:
            await runtime.start()
            # Only now is there something at the other end to route to.
            capture_target.route_traffic(runtime.port)
        except BaseException:
            capture_target.teardown()
            await runtime.stop()
            store.close()
            raise

        self._session = session
        self._store = store
        self._runtime = runtime
        self._target = capture_target
        self._trust = {"trusted": trust.trusted, "detail": trust.detail}
        self._runs = RunRegistry(session.dir)
        self._steps = StepLog()
        self._lens = lens_predicate

        census = self.authority_census(capture_target)

        return {
            "recording": True,
            "session": session.id,
            "port": runtime.port,
            "host": runtime.host,
            "target": target,
            "scope": list(session.scope) if session.scope is not None else None,
            # Reported so the default engaging is never silent: it changes what the *rest of the
            # machine* sees, which is not something an agent should have to infer from a default.
            "passthrough": session.passthrough,
            # Reported for the same reason `passthrough` is: it changes what every later listing
            # and every Timeline will show, and an agent reading an empty result should be able to
            # see that it asked for one rather than have to remember.
            "lens": session.lens,
            "session_dir": str(session.dir),
            "trust": self._trust,
            **_census_dict(census, capture_target_trusts=trust.trusted),
        }

    async def stop(self) -> dict[str, Any]:
        """End the Session: stop routing first, then take the proxy down."""
        if not self.recording:
            raise ToolError("not recording; there is nothing to stop")

        session, store, runtime, target = self._session, self._store, self._runtime, self._target
        assert session is not None and store is not None

        recorded, evicted = store.recorded_count, store.evicted_count
        runs = len(self._runs.all()) if self._runs is not None else 0

        # Routing stops first, so nothing is ever pointed at a proxy that is going away.
        if target is not None:
            target.teardown()
        if runtime is not None:
            await runtime.stop()
        store.close()

        self._session = self._store = self._runtime = self._target = self._trust = None
        self._runs = self._steps = None
        self._lens = None

        return {
            "recording": False,
            "session": session.id,
            "exchanges": recorded,
            "evicted": evicted,
            "runs": runs,
            "session_dir": str(session.dir),
        }

    def status(self) -> dict[str, Any]:
        """What is happening right now, including the eviction count."""
        if not self.recording:
            return {
                "recording": False,
                "detail": NOT_RECORDING,
                "sessions_root": str(self._root),
                # Reported here as well as while recording, and this is the branch that matters:
                # the machine can be proxied by a process that is gone or by another editor
                # window, and "is my machine still proxied?" is asked precisely when this Session
                # is not the one holding it. The answer comes off disk, so it never needed a
                # Session to be readable.
                "proxy": self.proxy_state(),
            }

        assert self._session is not None and self._store is not None and self._runtime is not None
        return {
            "recording": True,
            "session": self._session.id,
            "started": self._session.started,
            "port": self._runtime.port,
            "host": self._runtime.host,
            "target": self._session.target,
            "scope": list(self._session.scope) if self._session.scope is not None else None,
            "lens": self._session.lens,
            "exchanges": self._store.recorded_count,
            "retained": len(self._store),
            "evicted": self._store.evicted_count,
            "tls_failures": self._store.tls_failure_count,
            "pending": len(self._store.pending),
            "runs": [item.id for item in self.runs.all()],
            # Surfaced because a Step left open is a real and easily-forgotten mistake: every later
            # Timeline shows it running, and nothing else would say why.
            "open_step": self.steps.open_label,
            "session_dir": str(self._session.dir),
            "trust": self._trust,
            "proxy": self.proxy_state(),
        }

    def proxy_state(self) -> dict[str, Any]:
        """Whether the machine's system proxy is currently redirected, to what, and by whom.

        Read from the sentinel rather than from the target, because the sentinel is the thing that
        outlives the process — this is the one piece of Session state that can survive a crash, so
        "is my machine currently proxied?" should be answerable without reading a file by hand.

        The owner is what makes the answer actionable. There is one system proxy on the machine and
        every editor window runs its own server, so a redirect may well belong to a process this
        one cannot see or stop.
        """
        return self._proxy_report()[0]

    def _proxy_report(self) -> tuple[dict[str, Any], ProxyOwner | None]:
        """The redirect report, and the live foreign holder it found, from a single read.

        Two callers want two halves of one answer: ``get_status`` wants the report, and
        ``diagnose_capture`` wants the holder *and* the report's sentence about it. Asking twice
        resolved ownership twice, and a holder that exited between the two reads produced a
        ``proxy_held_elsewhere`` verdict whose detail came from the dead-owner branch. Vanishingly
        unlikely, harmless either way, and an answer contradicting itself is precisely the failure
        this work exists to stop — so the window is closed rather than documented.
        """
        sentinel = sentinel_path(self._root)
        if not sentinel.exists():
            return {"redirected": False, "detail": "the system proxy is not redirected"}, None

        proxy = SystemProxy(sentinel, runner=self._runner)
        record = proxy.read_record()
        if record is None:
            return {
                "redirected": True,
                # Every key the branch below carries, so a caller reads one shape either way.
                # Reporting three keys and omitting two was the whole defect: a KeyError raised in
                # exactly the branch that says "something is wrong here" is how an agent ends up
                # concluding the machine is not proxied. ``service`` and ``owner`` are genuinely
                # unknown — the record is the only thing that knew either, and it will not parse —
                # while the path is known, and is what a human needs to go and look at the file.
                "service": None,
                "sentinel": str(sentinel),
                "owner": None,
                "detail": f"redirected, but the sentinel at {sentinel} could not be read;"
                " the next server start will switch the proxy off to restore connectivity",
            }, None

        owner = record.owner
        ownership = resolve_ownership(owner, runner=self._runner)
        report = {
            "redirected": True,
            "service": record.prior.service,
            "sentinel": str(sentinel),
            # ``ours`` is the field an agent acts on: stop_capture puts back a redirect this
            # process made, and nothing else.
            "owner": None if owner is None else {"pid": owner.pid, "ours": ownership == "ours"},
            "detail": _proxy_detail(record.prior, owner, ownership, recording=self.recording),
        }
        holder = owner if ownership == "live_foreign" else None
        return report, holder


def _proxy_detail(
    prior: SystemProxyState,
    owner: ProxyOwner | None,
    ownership: Ownership,
    *,
    recording: bool,
) -> str:
    """One sentence about a redirect, branched on who owns it.

    The promise that state will be restored on ``stop_capture`` is only made about a record this
    process owns. For a record another live process holds it is simply false — stopping here
    restores nothing, because restoring someone else's redirect is the defect this work removes —
    and prose that is confidently wrong is what sent the developer looking in the wrong place.

    ``recording`` exists for the same reason one layer along. "This Session does not own it" is
    true and useful while a Session is open here, and names something that does not exist when
    there is none — which is the state ``diagnose_capture`` borrows this sentence in, since its
    ``proxy_held_elsewhere`` branch is reached only when nothing is recording. CONTEXT.md's Session
    is a precise word and this branch is the one place the tool was using it loosely.
    """
    routed = f"{prior.service} is routed through the capture port"
    restored_at_the_next_start = f"The next server start restores server={prior.server_summary}"

    # Checked before the ownership states so the rest can name a pid: a record with no owner is
    # the only one that resolves to `absent`, and it has no process to name.
    if owner is None:
        return (
            f"{routed}, and the record does not name the process that did it, so nobody owns it."
            f" {restored_at_the_next_start}"
        )

    if ownership == "ours":
        return (
            f"{routed} by this process (pid {owner.pid});"
            f" it will be restored to server={prior.server_summary} on stop_capture"
        )

    if ownership == "live_foreign":
        held = (
            "This Session does not own it, so stopping here leaves it in place"
            if recording
            else "Nothing here owns it, so there is nothing to stop here"
        )
        return (
            f"{routed} by another https-proxy-mcp process (pid {owner.pid}) that is still running."
            f" {held};"
            f" `kill {owner.pid}` makes that process put the machine back on its way out"
        )

    return (
        f"{routed} by a process that is no longer running (pid {owner.pid}), so it was left behind"
        f" rather than released. {restored_at_the_next_start}"
    )


def _census_dict(census: AuthorityCensus | None, *, capture_target_trusts: bool) -> dict[str, Any]:
    """The census as tool output: two counts, and a sentence only when there is one to say.

    Two counts rather than one, because "3 booted, 1 trusting" and "3 booted, 3 trusting" are
    entirely different situations and a single number tells them apart in neither direction.
    ``devices_note`` is null far more often than not, which is the point — a warning that appears
    every time is one an agent learns to skip.

    ``None`` means *nobody counted*, and renders as null rather than as zero. The distinction is
    the whole reason this takes an optional: the verdicts reached with no Capture Target built have
    no way to look at a trust store, and answering "0 booted" there would be a false statement
    about a machine that may well have three. Every verdict still carries the keys — a caller
    reading `result["devices_booted"]` gets a value from all of them, which is the same promise
    ``target`` and ``observed`` already make.
    """
    return {
        "devices_booted": census.booted if census is not None else None,
        "devices_trusting_authority": census.trusting if census is not None else None,
        "devices_note": (
            census.note(capture_target_trusts=capture_target_trusts) if census is not None else None
        ),
    }


def _observed_dict(observed: CaptureObservations) -> dict[str, Any]:
    """The counts a diagnosis reasoned from, flattened for the wire."""
    return {
        "exchanges": observed.exchanges,
        "reported": observed.reported,
        # Narrowed, and the raw total alongside it: filtering the verdict must not hide that
        # handshakes are failing somewhere.
        "tls_failures": observed.tls_failures,
        "tls_failures_total": observed.tls_failures_total,
        "tls_failure_hosts": list(observed.tls_failure_hosts),
        # The premise of the Passthrough caveat, reported for the same reason as the counts: a
        # verdict that reasons from something invisible cannot be checked, only believed.
        "passthrough": observed.passthrough,
    }
