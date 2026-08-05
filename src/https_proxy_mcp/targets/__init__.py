"""Capture Target implementations.

Each target arranges routing and trust for one kind of thing under test, and can
diagnose why capture is not working. v1 ships an iOS Simulator target and a
trivial local target used to exercise the core without any mobile tooling.
"""

from https_proxy_mcp.targets.base import CaptureTarget, Diagnosis, DiagnosisStatus, TrustStatus
from https_proxy_mcp.targets.curl import CurlTarget

#: Every target ``start_capture`` accepts by name. The iOS Simulator joins in P3.
_TARGETS: dict[str, type] = {
    CurlTarget.name: CurlTarget,
}


def known_targets() -> list[str]:
    """Target names ``start_capture`` accepts."""
    return sorted(_TARGETS)


def build_target(name: str) -> CaptureTarget:
    """Construct a Capture Target by name, naming the alternatives if it is not one."""
    try:
        return _TARGETS[name]()
    except KeyError:
        raise ValueError(
            f"unknown target {name!r}; available targets are {', '.join(known_targets())}"
        ) from None


__all__ = [
    "CaptureTarget",
    "CurlTarget",
    "Diagnosis",
    "DiagnosisStatus",
    "TrustStatus",
    "build_target",
    "known_targets",
]
