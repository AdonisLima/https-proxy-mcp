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
from https_proxy_mcp.capture.reporting import ReportingFilter
from https_proxy_mcp.capture.runtime import ProxyRuntime
from https_proxy_mcp.capture.session import (
    CONFDIR_NAME,
    Session,
    default_root,
    prune_sessions,
)
from https_proxy_mcp.targets import CaptureObservations, CaptureTarget, build_target
from https_proxy_mcp.targets.systemproxy import CommandRunner, run_command

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
        reported, _ = self.reporting().apply(list(store))
        failures = store.tls_failures
        return CaptureObservations(
            exchanges=store.recorded_count,
            tls_failures=store.tls_failure_count,
            # Deduplicated, order preserved: a pinned app retries, and the same host repeated
            # forty times says nothing the first did not.
            tls_failure_hosts=tuple(
                dict.fromkeys(failure.host for failure in failures if failure.host)
            ),
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

    def reporting(self, *, include_denied: bool = False) -> ReportingFilter:
        """How a listing narrows what it shows: the Session's Scope, plus the denylist.

        Built per call rather than held, because ``include_denied`` is a per-call decision and a
        cached filter would quietly ignore it.
        """
        return ReportingFilter(scope=self.scope, include_denied=include_denied)

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

        # Routing stops first, so nothing is ever pointed at a proxy that is going away.
        if target is not None:
            target.teardown()
        if runtime is not None:
            await runtime.stop()
        store.close()

        self._session = self._store = self._runtime = self._target = self._trust = None

        return {
            "recording": False,
            "session": session.id,
            "exchanges": recorded,
            "evicted": evicted,
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
            "session_dir": str(self._session.dir),
            "trust": self._trust,
        }


def _observed_dict(observed: CaptureObservations) -> dict[str, Any]:
    """The counts a diagnosis reasoned from, flattened for the wire."""
    return {
        "exchanges": observed.exchanges,
        "reported": observed.reported,
        "tls_failures": observed.tls_failures,
        "tls_failure_hosts": list(observed.tls_failure_hosts),
    }
