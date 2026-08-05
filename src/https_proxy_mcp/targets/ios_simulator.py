"""The iOS Simulator Capture Target: the one that changes the developer's machine.

Routing is the host's system proxy, because CoreSimulator has no proxy setting of its own — it
inherits the Mac's. That is what makes this target the risky one, and why the routing half
lives in :mod:`.systemproxy` behind a sentinel protocol designed around surviving ``kill -9``.

Trust is per-device and disposable, so installing the Interception Authority is inherent to
the request rather than something to ask permission for (SPEC.md, "Trust"). It is also
*verified* rather than assumed: ``simctl`` exiting zero means it accepted the file, not that
the device now trusts it, and the device's own trust store is the only thing that knows.

The diagnosis is the highest-value part. Three very different causes present identically as
"no traffic", so each is pinned to a distinct status with a remediation an agent can act on:

===========================  ==========================================================
``no_connect``               routing absent, device not booted, or simply nothing sent
``untrusted_authority``      the device does not list the authority
``tls_failed``               handshakes are failing *despite* trust — the pinning signature
``out_of_scope``             Exchanges were recorded and the Scope reported none of them
``ok``                       Exchanges are arriving
===========================  ==========================================================
"""

from __future__ import annotations

from pathlib import Path

from https_proxy_mcp.capture.session import CONFDIR_NAME, default_root
from https_proxy_mcp.targets.base import CaptureObservations, Diagnosis, TrustStatus
from https_proxy_mcp.targets.simctl import Simctl, SimulatorDevice, ensure_authority
from https_proxy_mcp.targets.systemproxy import (
    CommandRunner,
    SystemProxy,
    run_command,
    sentinel_path,
)


class IosSimulatorTarget:
    """Captures an iOS Simulator's traffic by redirecting the host's system proxy."""

    name = "ios-simulator"

    def __init__(
        self,
        *,
        udid: str | None = None,
        root: Path | str | None = None,
        runner: CommandRunner = run_command,
        simctl: Simctl | None = None,
        system_proxy: SystemProxy | None = None,
        **_options: object,
    ) -> None:
        self._udid = udid
        self._root = Path(root) if root is not None else default_root()
        self._simctl = simctl if simctl is not None else Simctl(runner=runner)
        self._proxy = (
            system_proxy
            if system_proxy is not None
            else SystemProxy(sentinel_path(self._root), runner=runner)
        )
        self._confdir = self._root / CONFDIR_NAME
        self._device: SimulatorDevice | None = None
        self._routed_port: int | None = None

    # --- lifecycle ------------------------------------------------------------

    def prepare(self) -> None:
        """Repair a sentinel left behind by a process that never cleaned up.

        Layer 3 of the teardown guarantee, reached through the target. It has to happen here
        rather than only at server start, because :meth:`route_traffic` deliberately refuses to
        run while a stale sentinel exists — the alternative is recording an already-proxied
        machine as the state to return to and losing the real configuration for good.
        """
        self._proxy.repair()

    def ensure_trusted(self) -> TrustStatus:
        """Install the Interception Authority into the device, then check that it took.

        The authority is generated here if the confdir has none, because this runs *before* the
        proxy starts and on a first ever run there would otherwise be nothing to install. The
        proxy is handed the same confdir, so it adopts this authority rather than making a
        second one the device has never heard of.

        A failure is reported rather than raised: a Session with an untrusted authority still
        captures plain HTTP, and ``diagnose_capture`` can then say exactly what is wrong. Making
        it fatal would turn a diagnosable state into an unexplained start failure.
        """
        pem = ensure_authority(self._confdir)

        try:
            device = self._resolve()
        except LookupError as exc:
            return TrustStatus(trusted=False, detail=str(exc))

        try:
            self._simctl.install_authority(device.udid, pem)
        except (RuntimeError, OSError) as exc:
            return TrustStatus(trusted=False, detail=str(exc))

        if self._simctl.trusts(device, pem):
            return TrustStatus(
                trusted=True,
                detail=f"{device.describe()} trusts the Interception Authority from {pem}",
            )
        return TrustStatus(
            trusted=False,
            detail=(
                f"{device.describe()} does not list the Interception Authority as trusted after"
                f" installing it from {pem}. HTTPS will fail; plain HTTP is still captured."
            ),
        )

    def route_traffic(self, port: int) -> None:
        """Point the Mac's HTTP and HTTPS proxies at the capture port.

        The simulator inherits this, and so does everything else on the machine — regular proxy
        mode has no process attribution (ADR-0002), which is why reporting is narrowed by time
        and host instead.
        """
        self._proxy.redirect("127.0.0.1", port)
        self._proxy.install_handlers()
        self._routed_port = port

    def teardown(self) -> None:
        """Put the system proxy back. Safe twice, and safe when nothing was ever changed."""
        self._proxy.restore()
        self._routed_port = None

    # --- diagnosis ------------------------------------------------------------

    def diagnose(self, observed: CaptureObservations | None = None) -> Diagnosis:
        """Say which of the five causes is in play, and what to do about it."""
        seen = observed if observed is not None else CaptureObservations()

        if not self._proxy.sentinel_path.exists():
            return Diagnosis(
                status="no_connect",
                detail="the system proxy is not redirected, so nothing is being sent to the"
                " capture port",
                remediation="call start_capture with target='ios-simulator' first",
            )

        try:
            device = self._resolve()
        except LookupError as exc:
            return Diagnosis(
                status="no_connect",
                detail=str(exc),
                remediation="boot the simulator you want to capture, or pass udid= to name it",
            )

        if not device.booted:
            return Diagnosis(
                status="no_connect",
                detail=f"{device.describe()} is not running, so it is sending no traffic",
                remediation=f"boot it with `xcrun simctl boot {device.udid}`",
            )

        trusted = self._simctl.trusts(device, self._confdir / "mitmproxy-ca-cert.pem")

        # Ordered by what the agent should act on first. An untrusted authority explains
        # handshake failures, so it is reported ahead of them rather than alongside.
        if not trusted:
            return Diagnosis(
                status="untrusted_authority",
                detail=(
                    f"{device.describe()} does not trust the Interception Authority"
                    + (
                        f", and {seen.tls_failures} TLS handshake(s) have already failed"
                        if seen.tls_failures
                        else "; HTTPS from it will fail"
                    )
                ),
                remediation="call install_authority to add it to this device's trust store",
            )

        if seen.tls_failures:
            hosts = ", ".join(host for host in seen.tls_failure_hosts if host) or "unknown hosts"
            return Diagnosis(
                status="tls_failed",
                detail=(
                    f"{seen.tls_failures} TLS handshake(s) failed for {hosts} even though"
                    f" {device.describe()} trusts the Interception Authority"
                ),
                remediation=(
                    "this is the signature of certificate pinning: the app is rejecting the"
                    " authority on purpose. A debug build with pinning disabled can be"
                    " captured; a correctly-pinned release build cannot"
                ),
            )

        if seen.exchanges and seen.reported == 0:
            return Diagnosis(
                status="out_of_scope",
                detail=(
                    f"{seen.exchanges} Exchange(s) were recorded but the Scope and denylist"
                    " report none of them"
                ),
                remediation="widen or clear the scope, or pass include_denied=True to see the"
                " traffic that was filtered out",
            )

        if seen.exchanges:
            return Diagnosis(
                status="ok",
                detail=f"routing and trust are in place and {seen.exchanges} Exchange(s) have"
                " been recorded",
            )

        return Diagnosis(
            status="no_connect",
            detail=(
                f"{device.describe()} is routed through the capture port and trusts the"
                " Interception Authority, but nothing has arrived yet"
            ),
            remediation=(
                "drive the app so it makes a request, then call wait_for_exchanges. If it stays"
                " empty, the app may be using a network stack that ignores the system proxy"
            ),
        )

    # --- internals ------------------------------------------------------------

    def _resolve(self) -> SimulatorDevice:
        """The device this target is about, looked up once and remembered.

        Cached because a Session's device does not change under it, and because resolution runs
        on every diagnosis — re-listing every simulator each time would make ``get_status``
        noticeably slow for no benefit.
        """
        if self._device is None:
            self._device = self._simctl.resolve(self._udid)
        return self._device
