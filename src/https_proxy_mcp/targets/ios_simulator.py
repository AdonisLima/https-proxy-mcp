"""The iOS Simulator Capture Target: the one that changes the developer's machine.

Routing is the host's system proxy, because CoreSimulator has no proxy setting of its own — it
inherits the Mac's. That is what makes this target the risky one, and why the routing half
lives in :mod:`.systemproxy` behind a sentinel protocol designed around surviving ``kill -9``.

Trust is per-device and disposable, so installing the Interception Authority is inherent to
the request rather than something to ask permission for (SPEC.md, "Trust"). It is also
*verified* rather than assumed: ``simctl`` exiting zero means it accepted the file, not that
the device now trusts it, and the device's own trust store is the only thing that knows.

The diagnosis is the highest-value part. Six verdicts, and everything but ``ok`` presents
identically as "no traffic", so each is pinned to a distinct status with a remediation an agent
can act on:

===========================  ==========================================================
``proxy_held_elsewhere``     the machine is redirected, and by another live process
``no_connect``               routing absent, device not booted, or simply nothing sent
``untrusted_authority``      the device does not list the authority
``tls_failed``               handshakes are failing *despite* trust — the pinning signature
``out_of_scope``             Exchanges were recorded and the Scope reported none of them
``ok``                       Exchanges are arriving
===========================  ==========================================================

Under Passthrough two of those verdicts also carry a caveat: only the Scope's hosts had their
HTTPS decrypted, so an absence outside it is not evidence of anything. It is stated as the
deduction it is — nothing counts a passed-through connection, deliberately (see
:mod:`..capture.runtime`) — and it says HTTPS rather than everything, because plain HTTP is
intercepted whatever the Scope says.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from https_proxy_mcp.capture.session import CONFDIR_NAME, default_root
from https_proxy_mcp.targets.base import (
    AuthorityCensus,
    CaptureObservations,
    Diagnosis,
    TrustStatus,
)
from https_proxy_mcp.targets.simctl import Simctl, SimulatorDevice, ensure_authority
from https_proxy_mcp.targets.systemproxy import (
    CommandRunner,
    SystemProxy,
    release_instruction,
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

    # --- lifecycle ------------------------------------------------------------

    def prepare(self) -> None:
        """Refuse if another live process holds the machine's proxy; otherwise repair.

        The refusal comes first, and it is here rather than at :meth:`route_traffic` because this
        is the last point at which a Session can be declined before anything has been *arranged*.
        By the time routing happens a Session directory exists, retention has already run against
        the shared root — which holds the *other* window's Sessions, so a start this window never
        completes can prune them — and a port is bound. ``ensure_trusted`` sits in between and
        creates nothing either, but it installs the Interception Authority into the device, which
        is a change to something outside this process. ``route_traffic`` still refuses on a record's
        mere existence, but that refusal is a backstop written for a stale file: it names no
        process, and it advises calling ``repair`` — which for a live holder does nothing.

        With no live holder this is layer 3 of the teardown guarantee, reached through the target.
        It has to happen here as well as at server start, because :meth:`route_traffic` deliberately
        refuses to run while a stale sentinel exists — the alternative is recording an
        already-proxied machine as the state to return to and losing the real configuration for
        good.
        """
        self._proxy.refuse_if_held()
        self._proxy.repair()

    def authority_pem(self) -> Path:
        """The Interception Authority's PEM, generating one if there is none yet."""
        return ensure_authority(self._confdir)

    def authority_census(self) -> AuthorityCensus:
        """How many booted devices trust the Interception Authority.

        The same trust stores :meth:`trust_status` reads, asked for every booted device rather
        than for one. Not part of the ``CaptureTarget`` protocol: only a target with a trust store
        per device can answer it, and a protocol method every other target had to stub would be
        ceremony rather than a contract.

        Anything unreadable counts as nothing and is not reported as a failure. This is a courtesy
        beside the answer an agent actually asked for, and both directions of getting it wrong are
        worse than silence: failing ``start_capture`` over a census would be absurd, and guessing a
        number would be the mistake ``tls_failed`` was corrected for. A zero census says nothing at
        all, which is what :meth:`AuthorityCensus.note` does with it.
        """
        try:
            booted = [device for device in self._simctl.devices() if device.booted]
        except (OSError, ValueError, KeyError):
            return AuthorityCensus()

        pem = self.authority_pem()
        trusting = 0
        for device in booted:
            try:
                trusting += 1 if self._simctl.trusts(device, pem) else 0
            except (OSError, sqlite3.Error):
                continue
        return AuthorityCensus(booted=len(booted), trusting=trusting)

    def trust_status(self) -> TrustStatus:
        """Report trust without changing it.

        Separate from :meth:`ensure_trusted` because ``get_trust_status`` is a question, and a
        question that alters what it asks about is a trap: an agent checking whether a device was
        already trusted would silently arrange trust and never find out.
        """
        pem = self.authority_pem()
        try:
            device = self._resolve()
        except LookupError as exc:
            return TrustStatus(trusted=False, detail=str(exc))

        trusted = self._simctl.trusts(device, pem)
        return TrustStatus(
            trusted=trusted,
            detail=(
                f"{device.describe()} {'trusts' if trusted else 'does not trust'} the"
                f" Interception Authority at {pem}"
            ),
        )

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
        pem = self.authority_pem()

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

    def teardown(self) -> None:
        """Put the system proxy back. Safe twice, and safe when nothing was ever changed."""
        self._proxy.restore()

    # --- diagnosis ------------------------------------------------------------

    def diagnose(self, observed: CaptureObservations | None = None) -> Diagnosis:
        """Say which of the six verdicts applies, and what to do about it."""
        seen = observed if observed is not None else CaptureObservations()

        if not self._proxy.sentinel_path.exists():
            return Diagnosis(
                status="no_connect",
                detail="the system proxy is not redirected, so nothing is being sent to the"
                " capture port",
                remediation="call start_capture with target='ios-simulator' first",
            )

        # Ahead of every "nothing arrived" verdict below, because it explains the same observation
        # more specifically and its remediation shares nothing with theirs: there is one system
        # proxy on the machine, so a redirect owned by another live process is sending the machine's
        # traffic to *that* process's capture port. Ordered after the branch above only for
        # readability — a record that does not exist has no holder, so the two cannot both apply.
        holder = self._proxy.foreign_holder()
        if holder is not None:
            return Diagnosis(
                status="proxy_held_elsewhere",
                detail=(
                    "the system proxy is redirected by another https-proxy-mcp process"
                    f" (pid {holder.pid}) that is still running, so the machine's traffic is going"
                    " to that process's capture port rather than to this one"
                ),
                remediation=(
                    f"{release_instruction(holder.pid)}, after which this refusal no longer"
                    " applies — start_capture here still needs a booted device and a trusted"
                    " authority, and will say so if either is missing. Nothing is changed from"
                    " here: restoring a redirect this process does not own is what left the"
                    " other Session reporting that it was recording while receiving nothing"
                ),
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
            # The count is machine-wide and cannot be made per-client: every client reaches the
            # proxy over loopback, so there is no peer address that separates a simulator from a
            # browser. Saying "even though <device> trusts" asserted the failing client *was*
            # that device, and a negative control on 2026-08-18 showed it wrong — one request
            # from an unrelated process on this Mac flipped a healthy capture to this verdict.
            return Diagnosis(
                status="tls_failed",
                detail=(
                    f"{seen.tls_failures} TLS handshake(s) failed for {hosts}. This count is"
                    " machine-wide: any client on this machine that reached an in-scope host is"
                    f" counted, not only {device.describe()}, which does trust the Interception"
                    " Authority"
                ),
                remediation=(
                    "if the failures are the app's, this is the signature of certificate"
                    " pinning — a debug build with pinning disabled can be captured, a"
                    " correctly-pinned release build cannot. Rule out the cheaper cause first:"
                    " another simulator or a browser on this machine talking to the same host"
                    " fails identically, because only the capture target was given the"
                    " Authority. Narrow the Scope, or quit the other clients, and see whether"
                    " the count stops rising"
                ),
            )

        if seen.exchanges and seen.reported == 0:
            return Diagnosis(
                status="out_of_scope",
                detail=(
                    f"{seen.exchanges} Exchange(s) were recorded but the Scope and denylist"
                    " report none of them" + _passthrough_caveat(seen)
                ),
                remediation="widen or clear the scope, or pass include_denied=True to see the"
                " traffic that was filtered out",
            )

        if seen.exchanges:
            return Diagnosis(
                status="ok",
                detail=f"routing and trust are in place and {seen.exchanges} Exchange(s) have"
                " been recorded" + _unattributed_failures(seen),
                remediation=_scope_hint(seen),
            )

        return Diagnosis(
            status="no_connect",
            detail=(
                f"{device.describe()} is routed through the capture port and trusts the"
                " Interception Authority, but nothing has arrived yet"
                + _unattributed_failures(seen)
                + _passthrough_caveat(seen)
            ),
            remediation=(
                "drive the app so it makes a request, then call wait_for_exchanges. If it stays"
                " empty, the app may be using a network stack that ignores the system proxy."
                + (_scope_hint(seen) or "")
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


def _unattributed_failures(seen: CaptureObservations) -> str:
    """A note about handshake failures that could not be attributed, or nothing.

    Reported even though they do not affect the verdict, because staying silent about them is how
    a real pinning problem would go unnoticed. What they cannot do is *accuse*: the system proxy
    is machine-wide, so most of them belong to the developer's other applications.
    """
    unattributed = seen.tls_failures_total - seen.tls_failures
    if unattributed <= 0:
        return ""
    return (
        f". {unattributed} TLS handshake(s) also failed for hosts outside this Session's Scope;"
        " with the system proxy redirected machine-wide those are usually the developer's other"
        " applications, which do not trust the Interception Authority and have no reason to"
    )


def _passthrough_caveat(seen: CaptureObservations) -> str:
    """What Passthrough lets an empty result mean, or nothing when it is not in force.

    Appended to the two verdicts an agent reaches when a listing came back empty, and phrased as
    the **deduction** it is. Nothing counts a passed-through connection and nothing should: the
    engine's option for observing them keeps every such connection's contents in memory, which on a
    machine-wide proxy is the whole machine's traffic (see :mod:`..capture.runtime`). So this
    reasons from "Passthrough is on and this host is not in the Scope" and claims no measurement it
    has not made.

    It is what turns the accepted regression into an explanation. The two tools deliberately exempt
    from the Scope — a wait naming a host, and a fetch by id — find nothing for a host outside it,
    because the traffic was never decrypted, so a wait blocks to its timeout instead of being
    vetoed. Without this sentence an agent reads that timeout as "the app made no request".

    It says **HTTPS** rather than everything, and that word was bought with a hand-verification.
    Passthrough scopes TLS interception only: mitmproxy picks a layer before it knows where a
    plain-HTTP proxy request is going, since only a CONNECT authority and a TLS SNI carry a name
    that early, so plain HTTP is opened whatever the Scope says. Over-claiming here would send an
    agent away from a plain-HTTP request that is in the store and merely not reported.
    """
    if not seen.passthrough:
        return ""
    return (
        ". Passthrough is in force, so only the Scope's hosts had their HTTPS decrypted: an HTTPS"
        " connection to any other host was forwarded without being opened and could never have"
        " been recorded, so an absence here is not evidence that the app made no request. That is"
        " deduced from the Scope rather than measured — nothing counts a passed-through connection."
        " Plain HTTP is the exception and is still captured from every host, so a plain-HTTP"
        " request outside the Scope is hidden by reporting rather than missing and get_exchange"
        " still fetches it by id. To capture an out-of-scope host over HTTPS, restart with a Scope"
        " that names it, or with passthrough=False"
    )


def _scope_hint(seen: CaptureObservations) -> str | None:
    """Point at the one thing that would turn an uninterpretable count into a verdict.

    A Scope narrows *hosts*, not clients — it is what makes the count worth a verdict, not what
    makes it the app's. Nothing can make it the app's; see the ``tls_failed`` branch above.
    """
    if seen.tls_failures_total <= seen.tls_failures:
        return None
    return (
        " To judge whether the app under test is pinned, restart with a scope naming its hosts."
        " Outside a Scope the count is every host on this machine, so it says nothing; inside one"
        " it is at least about the right hosts, though still counted across every client, because"
        " the proxy cannot tell which process a connection came from."
    )
