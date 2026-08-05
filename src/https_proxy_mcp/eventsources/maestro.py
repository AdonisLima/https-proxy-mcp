"""The Maestro Event Source: ``commands-*.json`` into Steps.

Everything here is shaped by what a probe found in Maestro 2.6.1's real output rather than by its
documentation, and three of the findings would have produced a wrong or unusable Timeline.

**The array is not in execution order.** A failing two-command flow came back as
``[seq 3, 0, 1, 2]`` — the FAILED assertion first, having run last — and a *passing* flow was also
scrambled (``1, 2, 0``), so this is not a failure-ordering quirk. ``metadata.sequenceNumber`` is
the authority. Ordering by array position puts the failure before the launch that preceded it,
which reads as a correlation bug and is not one.

**A failure carries the entire device UI hierarchy.** ``error.hierarchyRoot`` was 33 KB for one
assertion, making a two-command artifact 123 KB. Add ``stackTrace``, ``debugMessage`` and
``localizedMessage`` and a single failing Step could exhaust a context window — the risk PLAN.md
names outright. Only ``error.message`` survives this module, so nothing downstream can leak the
rest by accident.

**Timestamps are epoch milliseconds.** Every Exchange timestamp is epoch seconds. Missing the
conversion places every Step about fifty thousand years from the traffic it should align with.

The layout also differs by flag: with ``--flatten-debug-output`` the file is
``<dir>/commands-(flow.yaml).json``, and without it it lands under a *hidden*
``<dir>/.maestro/tests/<timestamp>/``. `run_test` injects the flatten flag, but the search here is
recursive so a caller who supplies their own flags still gets a Timeline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from https_proxy_mcp.correlate.steps import Step, StepStatus
from https_proxy_mcp.runs.registry import Run

#: Every artifact holding command results. The name embeds the flow file in literal parentheses —
#: ``commands-(login.yaml).json`` — and one exists per flow, so a Run may have several.
ARTIFACT_GLOB = "commands-*.json"

#: Maestro's own bookkeeping, which is neither an action nor an assertion (CONTEXT.md) and is never
#: the cause of a failure. Filtered by an explicit list, so an unrecognised command is *kept* — a
#: prefix rule would silently swallow whatever Maestro adds next.
BOOKKEEPING_COMMANDS = frozenset({"defineVariablesCommand", "applyConfigurationCommand"})

#: Maestro's status vocabulary, mapped onto the Step contract's.
#:
#: Anything absent becomes ``unknown`` rather than ``passed``. That is the one direction that
#: matters: a Timeline claiming a Step succeeded when nobody knows is worse than one admitting
#: ignorance, and Maestro is free to add a status in any release.
STATUS_MAP: dict[str, StepStatus] = {
    "COMPLETED": "passed",
    "FAILED": "failed",
    "SKIPPED": "skipped",
    "RUNNING": "running",
    "PENDING": "running",
    "WARNED": "passed",
}

#: Payload keys worth putting in a label, most identifying first. Maestro has dozens of command
#: types and gains more; naming the *interesting keys* rather than the command types means a
#: command nobody here has heard of still gets a readable label.
LABEL_KEYS = (
    "textRegex",
    "text",
    "appId",
    "link",
    "url",
    "id",
    "label",
    "path",
    "command",
)


class MaestroEventSource:
    """Reads Steps from Maestro's debug output."""

    name = "maestro"

    def steps(self, run: Run) -> list[Step]:
        """Every Step of this Run, ordered in time.

        Silent rather than raising when there are no artifacts: every source is consulted for
        every Run, and a `curl` command produces no Maestro output at all.
        """
        collected: list[tuple[float, int, Step]] = []

        for artifact in sorted(run.artifacts_dir.rglob(ARTIFACT_GLOB)):
            for entry in _entries(artifact):
                parsed = _step(entry)
                if parsed is None:
                    continue
                step, sequence = parsed
                collected.append((step.t_start, sequence, step))

        # By time first, then sequenceNumber. sequenceNumber is authoritative *within* a flow but
        # restarts per flow, so it cannot order across files; it breaks ties when two commands
        # share a millisecond.
        collected.sort(key=lambda item: (item[0], item[1]))
        return [step for _, _, step in collected]


def _entries(artifact: Path) -> list[dict[str, Any]]:
    """One artifact's command entries, or none if it cannot be read.

    A half-written file is what a killed Maestro leaves behind, and that is exactly when a
    Timeline is most wanted. One unreadable artifact must not take a Run's other Steps with it.
    """
    try:
        loaded = json.loads(artifact.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return []
    if not isinstance(loaded, list):
        return []
    return [entry for entry in loaded if isinstance(entry, dict)]


def _step(entry: dict[str, Any]) -> tuple[Step, int] | None:
    """One command entry as a Step, plus its sequence number. ``None`` if it is not a Step."""
    metadata = entry.get("metadata")
    command = entry.get("command")
    if not isinstance(metadata, dict) or not isinstance(command, dict) or not command:
        return None

    key = next(iter(command))
    if key in BOOKKEEPING_COMMANDS:
        return None

    timestamp = metadata.get("timestamp")
    if not isinstance(timestamp, (int, float)):
        return None

    duration = metadata.get("duration")
    return (
        Step(
            # Epoch milliseconds to epoch seconds, so Steps and Exchanges share a scale.
            t_start=timestamp / 1000,
            duration_ms=float(duration) if isinstance(duration, (int, float)) else 0.0,
            label=_label(key, command[key], metadata),
            status=STATUS_MAP.get(str(metadata.get("status", "")).upper(), "unknown"),
            error=_error(metadata),
        ),
        int(metadata.get("sequenceNumber", 0) or 0),
    )


def _error(metadata: dict[str, Any]) -> str | None:
    """The failure message, and *only* the message.

    ``error`` also carries ``hierarchyRoot`` — the whole device UI tree, 33 KB in the probe —
    plus a Java ``stackTrace``, ``debugMessage`` and ``localizedMessage``. Reaching for the one
    useful field here is what keeps a failing Step from costing a context window.
    """
    error = metadata.get("error")
    if not isinstance(error, dict):
        return None
    message = error.get("message")
    return message if isinstance(message, str) and message else None


def _label(key: str, payload: Any, metadata: dict[str, Any]) -> str:
    """A readable name for a command, without a table of every command type Maestro has.

    The command's own key gives the verb — ``launchAppCommand`` is ``launchApp`` — and the most
    identifying string in its payload gives the object. Keyed on the *payload* rather than on the
    command type so a command nobody here has heard of still reads well.

    ``evaluatedCommand`` is preferred as the source, because it is the payload with variables
    already substituted: ``${APP_ID}`` is not a useful thing to show in a Timeline.
    """
    verb = key.removesuffix("Command")

    evaluated = metadata.get("evaluatedCommand")
    if isinstance(evaluated, dict) and key in evaluated:
        payload = evaluated[key]

    hint = _hint(payload)
    return f"{verb}: {hint}" if hint else verb


def _hint(payload: Any, depth: int = 0) -> str | None:
    """The most identifying string in a command's payload, searched breadth-first by key.

    Bounded depth, because these structures nest — ``condition.visible.textRegex`` is three deep —
    and an unbounded walk over a hostile artifact is a denial of service with extra steps.
    """
    if depth > 4 or not isinstance(payload, dict):
        return None

    for wanted in LABEL_KEYS:
        value = payload.get(wanted)
        if isinstance(value, str) and value:
            # Quoted, because these are user-authored selectors and the quotes are what make
            # `assertCondition: "Home"` read as a value rather than a sentence.
            return f'"{value}"' if wanted in {"textRegex", "text"} else value

    for value in payload.values():
        if isinstance(value, dict):
            found = _hint(value, depth + 1)
            if found is not None:
                return found
    return None
