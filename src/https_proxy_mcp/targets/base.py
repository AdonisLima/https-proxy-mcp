"""The Capture Target contract, and the two verdicts a target can return.

A Capture Target is the thing whose traffic is being intercepted, together with whatever
must be arranged for interception to work — routing its egress and making it trust the
Interception Authority (CONTEXT.md). The core depends on this protocol and nothing else,
which is what makes the iOS Simulator in P3 and Android in v1.1 additions rather than
refactors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class TrustStatus:
    """Whether the target trusts the Interception Authority, and how it is known."""

    trusted: bool
    detail: str


#: The causes that present identically as "no traffic" and must be told apart (SPEC.md).
DiagnosisStatus = Literal["ok", "no_connect", "tls_failed", "untrusted_authority", "out_of_scope"]


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """Why capture is or is not working, and what to do about it.

    The remediation matters as much as the status: three very different causes present
    identically as "no traffic", and an agent that cannot tell them apart will guess.
    """

    status: DiagnosisStatus
    detail: str
    remediation: str | None = None


@dataclass(frozen=True, slots=True)
class CaptureObservations:
    """What the core can see about arriving traffic, which a target cannot see for itself.

    A target knows whether it arranged routing and trust; it has no idea whether anything came
    through. Only the store knows that, so the counts are handed *to*
    :meth:`CaptureTarget.diagnose` rather than discovered by it. Without them the two most
    common outcomes — "nothing reached the proxy" and "traffic arrived but the Scope hid it" —
    are indistinguishable.
    """

    #: Exchanges recorded this Session.
    exchanges: int = 0
    #: TLS handshakes that died before becoming an Exchange, counting only hosts this Session
    #: actually reports. Non-zero with a trusted authority is the signature of pinning.
    #:
    #: Narrowed rather than raw, and the live P3 run is why. Apple pins its own services, so a
    #: booted simulator fails handshakes for configuration.apple.com and friends continuously,
    #: whatever the app under test is doing. Judging on the raw count reported ``tls_failed``
    #: permanently on a working capture.
    tls_failures: int = 0
    #: Every handshake failure, narrowed or not, so filtering the verdict does not hide evidence.
    tls_failures_total: int = 0
    #: The reported hosts those handshakes were for, which is what separates one pinned endpoint
    #: from a wholesale trust failure.
    tls_failure_hosts: tuple[str, ...] = ()
    #: How many Exchanges survive the Scope and the denylist. ``None`` when nothing narrows.
    reported: int | None = None


@runtime_checkable
class CaptureTarget(Protocol):
    """What the core needs from anything whose traffic it captures."""

    def prepare(self) -> None:
        """Do whatever must happen before the proxy starts. May be nothing."""

    def ensure_trusted(self) -> TrustStatus:
        """Make the target trust the Interception Authority, and report the result."""

    def route_traffic(self, port: int) -> None:
        """Send the target's egress through the proxy on ``port``."""

    def diagnose(self, observed: CaptureObservations | None = None) -> Diagnosis:
        """Explain why capture is or is not working.

        ``observed`` carries the counts only the store knows. Optional so a target can be asked
        what it thinks before a Session exists, but a target that ignores it cannot distinguish
        "nothing arrived" from "the Scope hid it".
        """

    def teardown(self) -> None:
        """Undo :meth:`route_traffic`. Must be safe to call twice, and safe to call after a
        failed start — restoring state that was never changed is the common case."""
