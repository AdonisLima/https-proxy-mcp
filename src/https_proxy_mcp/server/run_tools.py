"""Runs and Timelines: the tools that deliver the reason the project exists.

Everything before this phase can show an agent the traffic. These six answer the question that
prompted the project — *why did the test fail?* — by interleaving Steps with the Exchanges that
were happening around them.

Two things here are easy to get wrong and are therefore done deliberately.

``get_timeline`` **assembles its own result** rather than going through ``_page`` in
:mod:`.exchange_tools`, so it does not inherit the denylist and the Scope for free. It applies
``state.reporting()`` explicitly, with the Run window as the first narrowing (SPEC.md's order), and
reports ``hidden`` — a view that quietly drops rows reads as "there was no traffic".

``run_test`` **spawns the test command**, which is unusual for an MCP server and is ADR-0003. It
buys exact Run boundaries and removes clock skew by construction. Tool docstrings are prompts
rather than comments, so they say when to reach for each tool and what its answer means.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from https_proxy_mcp.capture.exchange import Exchange, PendingExchange
from https_proxy_mcp.correlate.steps import Step, StepStatus
from https_proxy_mcp.correlate.timeline import build_timeline
from https_proxy_mcp.eventsources.explicit import StepStateError
from https_proxy_mcp.eventsources.maestro import MaestroEventSource
from https_proxy_mcp.runs.process import execute, maestro_flags, parse_command
from https_proxy_mcp.runs.registry import Run
from https_proxy_mcp.serialize.har import write_har
from https_proxy_mcp.serialize.timeline_views import timeline_view
from https_proxy_mcp.server.state import CaptureState

#: Where ``export_har`` writes when no path is given (SPEC.md's Session layout).
EXPORTS_DIRNAME = "exports"

#: The statuses ``end_step`` accepts. ``running`` is absent on purpose: a Step being ended is by
#: definition no longer running, and ``unknown`` is for an Event Source that met a status it did
#: not recognise, not for an agent that could simply say.
END_STATUSES: tuple[StepStatus, ...] = ("passed", "failed", "skipped")


def register_run_tools(server: FastMCP, state: CaptureState) -> None:
    """Register the tools that run tests and read Timelines."""

    @server.tool()
    async def run_test(
        command: str | list[str],
        label: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_ms: int | None = None,
    ) -> dict[str, Any]:
        """Run an end-to-end test as a Run, so its Steps can be aligned with the traffic.

        The Run is exactly the interval from spawn to exit — no inference and no matching, which is
        why Step and Exchange times are directly comparable. Call ``get_timeline`` afterwards; that
        is where the answer is.

        For ``maestro test ...`` the output flags are set for you and pointed at this Run's own
        artifacts directory, so its Steps are found automatically. Any other command works too:
        mark Steps yourself with ``start_step`` / ``end_step``, or run it with no Steps at all and
        read the traffic as Background.

        ``env`` is added to the environment rather than replacing it, so ``PATH`` survives.
        ``timeout_ms`` kills the whole process group on expiry — a Maestro run leaves a JVM behind
        otherwise. Output is written to the Run directory and only the tail comes back.

        A non-zero exit is a **result**, not an error: a failing test is the normal reason to be
        using this tool.
        """
        try:
            argv = parse_command(command)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

        registry = state.runs
        run = registry.create(command=argv, t_start=time.time(), label=label, cwd=cwd)
        outcome = await execute(
            maestro_flags(argv, run),
            run=run,
            cwd=cwd,
            env=env,
            timeout_ms=timeout_ms,
        )
        completed = registry.complete(
            run, t_end=time.time(), exit_code=outcome.exit_code, timed_out=outcome.timed_out
        )

        steps = _steps_for(state, completed)
        return {
            "run": completed.id,
            "label": completed.display_label,
            "exit_code": completed.exit_code,
            "passed": completed.passed,
            "timed_out": completed.timed_out,
            "duration_ms": completed.duration_ms,
            "steps": len(steps),
            "failed_steps": [item.label for item in steps if item.failed],
            "exchanges": _exchange_count(state, completed),
            "artifacts_dir": str(completed.artifacts_dir),
            "stdout_tail": outcome.stdout_tail,
            "stderr_tail": outcome.stderr_tail,
            **({"error": outcome.error} if outcome.error else {}),
            "next": f"call get_timeline with run_id={completed.id!r} to see what the traffic did",
        }

    @server.tool()
    def list_runs() -> dict[str, Any]:
        """List the Runs of this Session, oldest first, with their outcomes.

        Use it to find the ``run_id`` for ``get_timeline`` when more than one test has run.
        """
        runs = state.runs.all()
        return {
            "runs": [
                {
                    "id": item.id,
                    "label": item.display_label,
                    "started": round(item.t_start, 3),
                    "duration_ms": item.duration_ms,
                    "exit_code": item.exit_code,
                    "passed": item.passed,
                    "timed_out": item.timed_out,
                    "finished": item.finished,
                }
                for item in runs
            ],
            "count": len(runs),
        }

    @server.tool()
    def get_timeline(
        run_id: str | None = None,
        verbosity: str = "summary",
        host_filter: str | None = None,
        include_background: bool = False,
    ) -> dict[str, Any]:
        """Interleave a Run's Steps with the traffic around them. **This is the diagnosis.**

        Each Step reports what it *caused* (``owned``) and what was *already in flight* when it ran
        (``overlapping``). That second one is usually the answer: an assertion failing while the
        request it was waiting on had not come back is the commonest cause there is, and a failing
        Step carries a one-line ``explanation`` saying so.

        Traffic belonging to no Step is Background — polling and telemetry — counted and named by
        host, and listed only with ``include_background=true``. A request still open appears under
        ``pending`` with a null id, because ids are assigned on completion.

        ``run_id`` defaults to the most recent Run, or the whole Session if no test has been run.
        ``verbosity="full"`` adds body sizes and lists Background. ``host_filter`` narrows to one
        host and its subdomains.
        """
        run = state.run_or_session(run_id)
        exchanges, pending, hidden = _traffic(state, run, host_filter)

        timeline = build_timeline(
            steps=_steps_for(state, run),
            exchanges=exchanges,
            pending=pending,
            window=run.window,
        )

        try:
            view = timeline_view(
                timeline,
                run,
                verbosity=verbosity,
                include_background=include_background,
                hidden=hidden,
            )
        except ValueError as exc:
            raise ToolError(str(exc)) from None

        note = _empty_note(state, run, timeline.step_count, len(exchanges))
        return {**view, **({"note": note} if note else {})}

    @server.tool()
    def start_step(label: str) -> dict[str, Any]:
        """Mark the beginning of a Step, for any test tool that is not Maestro.

        This and ``end_step`` are all a Timeline needs — no artifact parsing, no adapter. Use them
        around each action: ``start_step("tapOn: Login")``, do the thing, then ``end_step``.

        Fails if a Step is already open. That is deliberate: closing one implicitly would have to
        invent an outcome, and a Step's outcome is the whole reason for recording it.
        """
        try:
            state.steps.start(label)
        except StepStateError as exc:
            raise ToolError(str(exc)) from None
        return {"open": label, "next": "call end_step when the step finishes"}

    @server.tool()
    def end_step(status: str = "passed", error: str | None = None) -> dict[str, Any]:
        """Close the open Step, recording how it went.

        ``status`` is "passed", "failed" or "skipped". Pass ``error`` when it failed — one line,
        which is what a Timeline shows against it.
        """
        if status not in END_STATUSES:
            raise ToolError(f"status must be one of {', '.join(END_STATUSES)}, not {status!r}")

        try:
            # `status` stays a plain `str` in the signature: it is what the tool's JSON schema
            # advertises, and narrowing it to a Literal would have FastMCP reject a bad value
            # before the friendlier ToolError above ever runs. Typing END_STATUSES as the Literal
            # is what lets that guard, three lines up, do the narrowing here — no cast needed.
            step = state.steps.end(status=status, error=error)
        except StepStateError as exc:
            raise ToolError(str(exc)) from None

        return {
            "label": step.label,
            "status": step.status,
            "duration_ms": step.duration_ms,
        }

    @server.tool()
    def export_har(
        run_id: str | None = None,
        redact: bool = True,
        path: str | None = None,
        host_filter: str | None = None,
    ) -> dict[str, Any]:
        """Write this Session's traffic to a HAR file for a browser or a bug report.

        Redacted by default, because the file travels further than a model does — into a ticket, a
        thread, a branch. Each secret becomes a stable ``<redacted:sha256:…>`` digest, so a
        repeated value is still recognisable as the same one. ``redact=false`` produces a faithful
        export that **contains live credentials**.

        ``run_id`` narrows to one Run's window; omitted, it exports the whole Session. The file
        records which mode produced it, so nobody has to guess later.
        """
        # Whole Session by default, unlike get_timeline: an export is an artifact of everything
        # captured, and one that silently held only the last Run would be the costlier surprise.
        run = state.run_or_session(run_id, default_to_latest=False)
        exchanges, _, hidden = _traffic(state, run, host_filter)

        target = (
            Path(path)
            if path is not None
            else state.session.dir / EXPORTS_DIRNAME / f"{run.id}.har"
        )

        try:
            written = write_har(
                exchanges, read_body=state.store.read_body, path=target, redact=redact
            )
        except OSError as exc:
            raise ToolError(f"could not write the export to {target}: {exc}") from None

        return {**written, "run": run.id, "hidden": hidden}


# --- helpers ------------------------------------------------------------------


def _steps_for(state: CaptureState, run: Run) -> list[Step]:
    """Every Step of this Run, from both Event Sources.

    Both are always consulted and each returns nothing when it has nothing to say, so no caller
    has to decide which applies — a Maestro Run that also marked Steps by hand gets both.
    """
    return [*MaestroEventSource().steps(run), *state.steps.steps(run)]


def _traffic(
    state: CaptureState, run: Run, host_filter: str | None
) -> tuple[list[Exchange], list[PendingExchange], int]:
    """The Exchanges and open requests in scope for this Run, and how many were hidden.

    Narrowed in SPEC.md's order — Run window, then denylist, then Scope — with ``host_filter``
    joining the filter as a further per-call narrowing rather than a replacement: a Scope the
    agent set on the Session still holds, and asking about one host inside it cannot widen it.
    One filter narrows both collections — Exchanges through :meth:`ReportingFilter.allows`,
    requests still in flight through :meth:`ReportingFilter.allows_pending` — so a narrowing
    cannot be applied to one and forgotten for the other.
    """
    reporting = state.reporting(
        window=run.window, hosts=(host_filter,) if host_filter is not None else None
    )
    store = state.store

    everything = list(store)
    exchanges = [item for item in everything if reporting.allows(item)]
    pending = [item for item in store.pending if reporting.allows_pending(item)]

    return exchanges, pending, len(everything) - len(exchanges)


def _empty_note(state: CaptureState, run: Run, steps: int, exchanges: int) -> str | None:
    """Why a Timeline came back empty when the Session is not.

    ``run_test`` blocks, so Steps marked *after* it fall outside its window — correctly, and
    invisibly. Without this an agent that ran a test and then marked Steps by hand gets an empty
    Timeline and nothing at all to explain it, which is the "reads as no traffic" failure this
    project keeps guarding against.
    """
    if steps or exchanges:
        return None

    outside = state.store.recorded_count
    if not outside:
        return None

    return (
        f"nothing fell inside {run.id}'s window, but the Session has recorded {outside}"
        " Exchange(s) outside it. A Run's window is exactly its process lifetime, so anything"
        " before or after it belongs to a different Run — call get_timeline without a run_id"
        " after list_runs, or use start_step/end_step while the traffic is happening."
    )


def _exchange_count(state: CaptureState, run: Run) -> int:
    """How much reportable traffic this Run saw, for ``run_test`` to hand back immediately.

    A count rather than the rows: the answer to "what happened" is ``get_timeline``, and returning
    traffic from ``run_test`` would make the expensive call the accidental one.
    """
    exchanges, _, _ = _traffic(state, run, None)
    return len(exchanges)
