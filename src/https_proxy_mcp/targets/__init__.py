"""Capture Target implementations.

Each target arranges routing and trust for one kind of thing under test, and can
diagnose why capture is not working. v1 ships an iOS Simulator target and a
trivial local target used to exercise the core without any mobile tooling.
"""

from https_proxy_mcp.targets.base import (
    CaptureObservations,
    CaptureTarget,
    Diagnosis,
    DiagnosisStatus,
    TrustStatus,
)
from https_proxy_mcp.targets.curl import CurlTarget
from https_proxy_mcp.targets.ios_simulator import IosSimulatorTarget

#: Every target ``start_capture`` accepts by name.
_TARGETS: dict[str, type] = {
    CurlTarget.name: CurlTarget,
    IosSimulatorTarget.name: IosSimulatorTarget,
}


def known_targets() -> list[str]:
    """Target names ``start_capture`` accepts."""
    return sorted(_TARGETS)


def build_target(name: str, **options: object) -> CaptureTarget:
    """Construct a Capture Target by name, naming the alternatives if it is not one.

    ``options`` are the per-target settings ``start_capture`` collects — ``udid`` and the
    Session ``root``. Every target accepts the same call and ignores what it does not need, so
    adding a target does not mean touching the caller.
    """
    try:
        factory = _TARGETS[name]
    except KeyError:
        raise ValueError(
            f"unknown target {name!r}; available targets are {', '.join(known_targets())}"
        ) from None
    return factory(**options)


__all__ = [
    "CaptureObservations",
    "CaptureTarget",
    "CurlTarget",
    "Diagnosis",
    "DiagnosisStatus",
    "IosSimulatorTarget",
    "TrustStatus",
    "build_target",
    "known_targets",
]
