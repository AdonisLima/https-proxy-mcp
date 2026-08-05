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


@runtime_checkable
class CaptureTarget(Protocol):
    """What the core needs from anything whose traffic it captures."""

    def prepare(self) -> None:
        """Do whatever must happen before the proxy starts. May be nothing."""

    def ensure_trusted(self) -> TrustStatus:
        """Make the target trust the Interception Authority, and report the result."""

    def route_traffic(self, port: int) -> None:
        """Send the target's egress through the proxy on ``port``."""

    def diagnose(self) -> Diagnosis:
        """Explain why capture is or is not working."""

    def teardown(self) -> None:
        """Undo :meth:`route_traffic`. Must be safe to call twice, and safe to call after a
        failed start — restoring state that was never changed is the common case."""
