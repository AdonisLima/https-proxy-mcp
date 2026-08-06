"""The committed hooks, tested like anything else here.

A gate that fails open looks exactly like a gate that is working: nothing happens, and the
command runs. So the interesting assertions are in both directions — that each hazard is caught,
and that the ordinary commands people run all day are *not*, because a gate with false positives
gets switched off and then protects nothing.

The false-negative cases are the six documented hazards. The false-positive cases are real
commands taken from this project's own history, including `grep -rn sudo src/`, which is a
question about the code rather than an attempt to run anything.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS = REPO_ROOT / ".claude" / "hooks"


def _load(name: str):
    """Import a hook by path — `.claude/hooks/` is deliberately not a package."""
    spec = importlib.util.spec_from_file_location(name, HOOKS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard_bash = _load("guard_bash")
check_python = _load("check_python")


# --- what must be refused -----------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "xcrun simctl keychain F48AAE9F-4D3F-4817-9050-7B4B0024CF07 reset",
        "simctl keychain ABC reset",
        "rm -rf ~/.https-proxy-mcp",
        "rm -rf $HOME/.https-proxy-mcp/sessions",
        "sudo security add-trusted-cert -d root.pem",
        "echo starting && sudo networksetup -setwebproxy Wi-Fi 127.0.0.1 8080",
    ],
)
def test_irreversible_and_hanging_commands_are_denied(command: str):
    verdict = guard_bash.decide(command)
    assert verdict is not None, f"not caught at all: {command}"
    assert verdict[0] == "deny", f"expected deny, got {verdict[0]}: {command}"


@pytest.mark.parametrize(
    "command",
    [
        'networksetup -setwebproxystate "Wi-Fi" off',
        'networksetup -setproxybypassdomains "Wi-Fi" Empty',
        "sudo -n networksetup -setsecurewebproxystate Wi-Fi off",
        "HTTPS_PROXY_MCP_E2E=1 uv run pytest tests/test_simulator_e2e.py -q",
        "xcrun simctl delete hpm-e2e-throwaway",
        "xcrun simctl erase all",
    ],
)
def test_machine_mutating_commands_are_asked_about(command: str):
    verdict = guard_bash.decide(command)
    assert verdict is not None, f"not caught at all: {command}"
    assert verdict[0] == "ask", f"expected ask, got {verdict[0]}: {command}"


# --- what must be left alone --------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        # The one that matters most: asking *about* sudo is not running it. This exact command
        # was run while building the hook, and an earlier draft of the pattern denied it.
        'grep -rn "sudo" src/',
        "grep -rn sudo src/ tests/",
        # Reads, not writes.
        'networksetup -getwebproxy "Wi-Fi"',
        'networksetup -getproxybypassdomains "Wi-Fi"',
        "xcrun simctl list devices -j",
        "xcrun simctl bootstatus ABC -b",
        # `sudo -n` is the documented safe form and must never be blocked.
        "sudo -n security remove-trusted-cert -d root.pem",
        # Ordinary work.
        "uv run pytest -q",
        "uv run ruff check .",
        "uv build",
        "rm -rf /tmp/scratch",
        "git status",
        # The env var appearing without being set to 1.
        "echo $HTTPS_PROXY_MCP_E2E",
    ],
)
def test_ordinary_commands_are_not_touched(command: str):
    assert guard_bash.decide(command) is None, f"false positive on: {command}"


# --- the protocol, end to end -------------------------------------------------


def _run_hook(script: str, payload: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(HOOKS / script)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
    )


@pytest.mark.integration
def test_the_gate_emits_the_decision_claude_code_actually_reads():
    """Unit tests prove the logic; only running the script proves the wire format.

    A hook whose JSON is shaped wrong is indistinguishable from one that allowed the command.
    """
    done = _run_hook(
        "guard_bash.py",
        {"tool_name": "Bash", "tool_input": {"command": "sudo rm -rf /"}},
    )
    assert done.returncode == 0
    emitted = json.loads(done.stdout)["hookSpecificOutput"]
    assert emitted["hookEventName"] == "PreToolUse"
    assert emitted["permissionDecision"] == "deny"
    assert "JSON-RPC" in emitted["permissionDecisionReason"]


@pytest.mark.integration
def test_the_gate_says_nothing_about_a_harmless_command():
    done = _run_hook(
        "guard_bash.py",
        {"tool_name": "Bash", "tool_input": {"command": "uv run pytest -q"}},
    )
    assert done.returncode == 0
    assert done.stdout == ""


@pytest.mark.integration
def test_the_gate_ignores_tools_that_are_not_bash():
    done = _run_hook(
        "guard_bash.py",
        {"tool_name": "Read", "tool_input": {"file_path": "sudo"}},
    )
    assert done.returncode == 0
    assert done.stdout == ""


@pytest.mark.integration
def test_the_gate_fails_open_on_input_it_cannot_parse():
    """Deliberate: a gate that blocks everything when confused teaches people to remove it."""
    done = subprocess.run(
        [sys.executable, str(HOOKS / "guard_bash.py")],
        input="not json at all",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert done.returncode == 0
    assert done.stdout == ""


# --- the feedback hook --------------------------------------------------------


@pytest.mark.integration
def test_lint_problems_are_reported(tmp_path: Path):
    bad = tmp_path / "bad.py"
    bad.write_text("import os\n")  # F401, unused
    found = check_python.problems(bad, REPO_ROOT)
    assert found, "ruff had a complaint and the hook did not pass it on"
    assert any("ruff check" in entry for entry in found)


@pytest.mark.integration
def test_a_clean_file_produces_no_noise(tmp_path: Path):
    good = tmp_path / "good.py"
    good.write_text('"""Fine."""\n\nVALUE = 1\n')
    assert check_python.problems(good, REPO_ROOT) == []


@pytest.mark.integration
def test_the_feedback_hook_never_rewrites_the_file(tmp_path: Path):
    """The entire point of reporting rather than fixing — see HANDOFF.md on the 11-file reformat.

    A hook that reformatted on edit would put formatting churn inside somebody else's commit,
    silently, which is the thing this project decided not to do.
    """
    misformatted = tmp_path / "ugly.py"
    original = "x = {   'a':1,'b':2 }\n"
    misformatted.write_text(original)

    found = check_python.problems(misformatted, REPO_ROOT)

    assert found, "ruff format should have disagreed with this"
    assert misformatted.read_text() == original, "the hook edited the file"


@pytest.mark.integration
def test_a_non_python_edit_is_ignored():
    done = _run_hook(
        "check_python.py",
        {"tool_name": "Write", "tool_input": {"file_path": str(REPO_ROOT / "README.md")}},
    )
    assert done.returncode == 0
    assert done.stderr == ""
