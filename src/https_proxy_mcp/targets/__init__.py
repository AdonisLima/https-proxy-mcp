"""Capture Target implementations.

Each target arranges routing and trust for one kind of thing under test, and can
diagnose why capture is not working. v1 ships an iOS Simulator target and a
trivial local target used to exercise the core without any mobile tooling.
"""

from collections.abc import Callable

from https_proxy_mcp.targets.base import (
    AuthorityCensus,
    CaptureObservations,
    CaptureTarget,
    Diagnosis,
    DiagnosisStatus,
    TrustStatus,
)
from https_proxy_mcp.targets.curl import CurlTarget
from https_proxy_mcp.targets.ios_simulator import IosSimulatorTarget

#: Every target ``start_capture`` accepts by name. Typed as a factory rather than a class
#: because ``CaptureTarget`` is a Protocol: what the registry promises is that calling the value
#: yields one, not that the value is a subclass of anything.
_TARGETS: dict[str, Callable[..., CaptureTarget]] = {
    CurlTarget.name: CurlTarget,
    IosSimulatorTarget.name: IosSimulatorTarget,
}


#: The options :func:`build_target` will pass on. Validated here because every target accepts
#: ``**_options`` so that one call shape fits all of them — which also means **no target would
#: notice a typo**. A mistyped ``udid`` is not cosmetic: the simulator target falls back to
#: choosing a device itself, and ``start_capture`` documents capturing the wrong app and reporting
#: success as the one outcome it must never produce. So the uniform call shape is kept and the
#: check moved to the single place all construction goes through.
TARGET_OPTIONS = frozenset({"udid", "root", "runner", "simctl", "system_proxy"})


def known_targets() -> list[str]:
    """Target names ``start_capture`` accepts."""
    return sorted(_TARGETS)


def build_target(name: str, **options: object) -> CaptureTarget:
    """Construct a Capture Target by name, naming the alternatives if it is not one.

    ``options`` are the per-target settings ``start_capture`` collects — ``udid`` and the
    Session ``root`` — plus the test seams. Every target accepts the same call and ignores what it
    does not need, so adding a target does not mean touching the caller.
    """
    try:
        factory = _TARGETS[name]
    except KeyError:
        raise ValueError(
            f"unknown target {name!r}; available targets are {', '.join(known_targets())}"
        ) from None

    unknown = sorted(set(options) - TARGET_OPTIONS)
    if unknown:
        raise ValueError(
            f"unknown target option(s) {', '.join(unknown)};"
            f" targets accept {', '.join(sorted(TARGET_OPTIONS))}"
        )

    return factory(**options)


__all__ = [
    "AuthorityCensus",
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
