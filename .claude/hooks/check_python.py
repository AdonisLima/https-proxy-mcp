#!/usr/bin/env python3
"""PostToolUse feedback for edited Python files: report, never rewrite.

Runs ``ruff check`` and ``ruff format --check --diff`` against the one file that was just
edited and hands the output back. It deliberately does **not** apply either fix.

That restraint is the whole design. This repository spent five phases with eleven files that
``ruff format`` disagreed with, and recorded the reason not to fix them incidentally: a
formatter rewriting files inside a logic commit buries whatever else is in the diff. A hook that
reformatted on every edit would reintroduce that at the worst possible granularity — silently,
inside somebody else's change. So it tells you, and you decide when.

Fails open in every direction: no ruff, no file, not Python, anything unexpected — say nothing.
The cost of a false alarm here is that somebody turns the hook off.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _ruff(project_dir: Path) -> list[str] | None:
    """The ruff to use, preferring the project's own virtualenv over whatever is on PATH."""
    local = project_dir / ".venv" / "bin" / "ruff"
    if local.is_file():
        return [str(local)]
    from shutil import which

    found = which("ruff")
    return [found] if found else None


def problems(path: Path, project_dir: Path) -> list[str]:
    """Ruff's complaints about one file, as lines. Empty when it is happy or cannot be asked."""
    ruff = _ruff(project_dir)
    if ruff is None:
        return []

    found: list[str] = []
    for args, label in (
        (["check", str(path)], "ruff check"),
        (["format", "--check", "--diff", str(path)], "ruff format"),
    ):
        try:
            done = subprocess.run(
                [*ruff, *args],
                capture_output=True,
                text=True,
                timeout=30,
                cwd=project_dir,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if done.returncode != 0:
            output = (done.stdout or done.stderr or "").strip()
            if output:
                found.append(f"{label}:\n{output}")
    return found


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path or not str(file_path).endswith(".py"):
        return 0

    path = Path(file_path)
    if not path.is_file():
        return 0

    project_dir = Path(os.environ.get("CLAUDE_PROJECT_DIR", path.parent)).resolve()
    found = problems(path, project_dir)
    if not found:
        return 0

    print(
        f"{path.name} — reported, not fixed. Apply these deliberately:\n\n" + "\n\n".join(found),
        file=sys.stderr,
    )
    # 2 is the exit code that feeds stderr back rather than discarding it. The edit already
    # happened; this is feedback, not a veto.
    return 2


if __name__ == "__main__":
    sys.exit(main())
