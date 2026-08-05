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

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.server.fastmcp.exceptions import ToolError

from https_proxy_mcp.capture import Exchange, ExchangeStore
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
from https_proxy_mcp.targets import CaptureObservations, CaptureTarget, build_target
from https_proxy_mcp.targets.systemproxy import (
    CommandRunner,
    SystemProxyState,
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
        relevant = (
            [
                failure
                for failure in store.tls_failures
                if failure.host and reporting.allows_host(failure.host)
            ]
            if self.scope is not None
            else []
        )

        return CaptureObservations(
            exchanges=store.recorded_count,
            tls_failures=len(relevant),
            tls_failures_total=store.tls_failure_count,
            # Deduplicated, order preserved: a pinned app retries, and the same host repeated
            # forty times says nothing the first did not.
            tls_failure_hosts=tuple(dict.fromkeys(failure.host for failure in relevant)),
            reported=len(reported),
        )

    def diagnose(self) -> dict[str, Any]:
        """Why capture is or is not working, as ``diagnose_capture`` reports it.

        Answers rather than raises when nothing is recording. This is the tool an agent reaches
        for when it is already confused, so failing here would answer "why is nothing working?"
        with one more thing that does not work.
        """
        if not self.recording or self._target is None:
            return {
                "status": "no_connect",
                "detail": NOT_RECORDING,
                "remediation": "call start_capture, then drive the app and try again",
                "observed": _observed_dict(CaptureObservations()),
            }

        observed = self.observations()
        diagnosis = self._target.diagnose(observed)
        return {
            "status": diagnosis.status,
            "detail": diagnosis.detail,
            "remediation": diagnosis.remediation,
            "target": self._session.target if self._session else None,
            # Reported so an agent can check the reasoning rather than having to trust it.
            "observed": _observed_dict(observed),
        }

    def reporting(
        self,
        *,
        include_denied: bool = False,
        window: tuple[float, float | None] | None = None,
    ) -> ReportingFilter:
        """How a listing narrows what it shows: the Run window, the Session's Scope, the denylist.

        Built per call rather than held, because ``include_denied`` and ``window`` are per-call
        decisions and a cached filter would quietly ignore them.
        """
        return ReportingFilter(scope=self.scope, include_denied=include_denied, window=window)

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
        capacity: int = ExchangeStore.DEFAULT_CAPACITY,
    ) -> dict[str, Any]:
        """Begin a Session: prune, prepare the target, start the proxy, then route to it."""
        if self.recording:
            raise ToolError(
                f"already recording into Session {self._session.id}; call stop_capture first"
            )

        capture_target = self.build_target(target, udid=udid)

        # prepare() is where a target repairs state a dead predecessor left behind, so it runs
        # before anything else can trip over it.
        capture_target.prepare()
        trust = capture_target.ensure_trusted()

        session = Session.create(self._root, target=target, port=port, scope=scope, udid=udid)
        # Pruned *after* creating, so the invariant is exactly "RETAINED_SESSIONS remain"
        # rather than that many plus the new one. The new Session is the newest, so it is
        # never the one removed.
        prune_sessions(self._root)
        store = ExchangeStore(session.dir, capacity=capacity)
        runtime = self._runtime_factory(store, port=port, confdir=self._root / CONFDIR_NAME)

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

        return {
            "recording": True,
            "session": session.id,
            "port": runtime.port,
            "host": runtime.host,
            "target": target,
            "scope": list(session.scope) if session.scope is not None else None,
            "session_dir": str(session.dir),
            "trust": self._trust,
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
        """Whether the machine's system proxy is currently redirected, and to what.

        Read from the sentinel rather than from the target, because the sentinel is the thing that
        outlives the process — this is the one piece of Session state that can survive a crash, so
        "is my machine currently proxied?" should be answerable without reading a file by hand.
        """
        sentinel = sentinel_path(self._root)
        if not sentinel.exists():
            return {"redirected": False, "detail": "the system proxy is not redirected"}

        try:
            prior = SystemProxyState.from_dict(json.loads(sentinel.read_text()))
        except (ValueError, KeyError, TypeError):
            return {
                "redirected": True,
                "detail": f"redirected, but the sentinel at {sentinel} could not be read;"
                " the next server start will switch the proxy off to restore connectivity",
            }

        return {
            "redirected": True,
            "service": prior.service,
            "sentinel": str(sentinel),
            "detail": (
                f"{prior.service} is routed through the capture port; it will be restored to"
                f" server={prior.server_summary} on stop_capture"
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
    }
