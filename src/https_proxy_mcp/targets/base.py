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


#: The causes that present identically as "no traffic" and must be told apart (SPEC.md). A closed
#: set on purpose: an agent maps a verdict to an action, so a cause with no name of its own gets
#: read out of another one's detail string and acted on wrongly. ``proxy_held_elsewhere`` is the
#: cause SPEC.md's five could not express — the machine is redirected, and not by this process.
DiagnosisStatus = Literal[
    "ok",
    "no_connect",
    "tls_failed",
    "untrusted_authority",
    "out_of_scope",
    "proxy_held_elsewhere",
]


@dataclass(frozen=True, slots=True)
class AuthorityCensus:
    """How many devices on this machine trust the Interception Authority.

    Reported because installing the Authority into more than one device is how a second simulator
    is kept working while a capture runs — and the cost of doing so is invisible from every other
    tool output. Every device that trusts it and is booted has its in-scope HTTPS decrypted into
    whichever Session is recording, bodies and all, whether or not it is that Session's Capture
    Target.

    Counts, never contents. Nothing here can look at a body and know what is in it, and a warning
    that guessed would be the mistake the ``tls_failed`` verdict was corrected for. What it can do
    is read trust stores and say how many there are, at the moment the decision is being made.
    """

    #: Booted devices — the only ones that can be sending anything.
    booted: int = 0
    #: Booted devices whose trust store lists the Interception Authority.
    trusting: int = 0

    def note(self, *, capture_target_trusts: bool = True) -> str | None:
        """The sentence worth saying, or ``None`` when there is nothing to say.

        Silent when this Session's Capture Target is the only booted device that trusts the
        Authority, because then the count says exactly what an agent already assumed.
        """
        others = self.trusting - (1 if capture_target_trusts else 0)
        if others < 1:
            return None
        return (
            f"{others} booted device(s) other than this Session's Capture Target trust the"
            " Interception Authority. Their traffic to in-scope hosts is decrypted and recorded"
            " into this Session, and nothing distinguishes it from the Capture Target's — set a"
            " lens if you need them apart."
        )


@dataclass(frozen=True, slots=True)
class Diagnosis:
    """Why capture is or is not working, and what to do about it.

    The remediation matters as much as the status: there are **six verdicts**, and every one but
    ``ok`` names something that presents identically as "no traffic", so an agent that cannot tell
    them apart will guess. The number is written here because a reader needs it and stale twice is
    twice too often — it said "three" from v1 until this line was pinned to
    :data:`DiagnosisStatus` by a test.
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

    ``passthrough`` is the one field here that is not a count. It travels the same way and for the
    same reason: a target has no idea what the Session's Scope is, and under Passthrough the Scope
    decides what could ever have been recorded. Carried here rather than as a second parameter to
    :meth:`CaptureTarget.diagnose`, because that signature is the protocol every target and the
    suite parametrised over them implements, and a new keyword on it would cost all of them a change
    to say something only this one field says.
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
    #: Whether Passthrough was in force for this Session (CONTEXT.md): only the Scope's hosts had
    #: their HTTPS decrypted, so an HTTPS connection outside it was forwarded without being opened
    #: and could never have been recorded. Plain HTTP is the exception and is intercepted whatever
    #: the Scope says, because the engine chooses a layer before a plain-HTTP request names its
    #: destination. A *deduction* a verdict may state, never a measurement — nothing counts a
    #: passed-through connection, and the engine's option for observing them would keep the whole
    #: machine's traffic in memory, so it stays off.
    passthrough: bool = False


@runtime_checkable
class CaptureTarget(Protocol):
    """What the core needs from anything whose traffic it captures."""

    def prepare(self) -> None:
        """Do whatever must happen before the proxy starts. May be nothing."""

    def ensure_trusted(self) -> TrustStatus:
        """Make the target trust the Interception Authority, and report the result."""

    def trust_status(self) -> TrustStatus:
        """Report whether the target trusts the Interception Authority, **changing nothing**.

        Distinct from :meth:`ensure_trusted` because a question that alters what it asks about is a
        trap: an agent checking whether a device was *already* trusted would silently arrange trust
        and never be able to find out.

        On the protocol rather than discovered by the tool layer. It began as an
        ``isinstance(target, IosSimulatorTarget)`` branch in ``get_trust_status`` — which worked,
        and put a per-target concern where a third target would have inherited
        ``ensure_trusted``'s side effect simply by not knowing to override it. A target with nothing
        to install may return the same answer as ``ensure_trusted``; it just has to say so.
        """

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
