#!/usr/bin/env python3
"""PreToolUse gate for Bash commands in this repository.

Six hazards, all of them documented in this project because each one has already cost somebody
something. Three are irreversible or hang the session outright and are denied; three change the
developer's real machine and are put to them as a question.

The decision logic is a pure function so it can be tested without spawning anything — see
``tests/test_hooks.py``. A guardrail nobody has watched fail is not a guardrail, and this one
fails *open* on any unexpected input, which is the safe direction for a hook but the dangerous
direction for a test suite: it means a broken matcher looks exactly like a quiet one.

Depends on nothing outside the standard library, and runs under the system ``python3``, so the
hook costs no virtualenv resolution on every Bash call.
"""

from __future__ import annotations

import json
import re
import sys

#: Matched at a command position — start of input, or after a separator — so that a command
#: merely *mentioning* one of these words is not caught. `grep -rn sudo src/` is a question about
#: the code, not an attempt to run it.
_AT_COMMAND_START = r"(?:^|[;&|]\s*|\n\s*|\$\(\s*|`\s*)"

DENY: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\bsimctl\b[^|;&]*\bkeychain\b[^|;&]*\breset\b"),
        "`simctl keychain reset` wipes that device's ENTIRE keychain — it is the only tool that "
        "can remove a root certificate and it is far too blunt. The Sidekick device carries six "
        "inert mitmproxy authorities from P3 whose private keys no longer exist; they are "
        "harmless. Ask the user before running this.",
    ),
    (
        re.compile(r"\brm\b[^|;&]*(?:~|\$HOME|/Users/[^/\s]+)/\.https-proxy-mcp"),
        "Do not delete ~/.https-proxy-mcp before looking at it. That directory should not exist "
        "on this machine — every test redirects the root to tmp_path — so its presence means "
        "something leaked, and it holds UNREDACTED bodies. Read sessions/*/session.json to find "
        "which target and port created it first. The usual cause is the stdio_client environment "
        "trap in src/https_proxy_mcp/server/CLAUDE.md.",
    ),
    (
        re.compile(_AT_COMMAND_START + r"sudo\b(?!\s+-n\b)"),
        "Never run an interactive `sudo` from this project. The server speaks JSON-RPC over "
        "stdin, so a password prompt reads the client's next message as a password attempt and "
        "hangs the session with no error and no way out. Use `sudo -n`; a test asserts every "
        "sudo call in the source carries it.",
    ),
]

ASK: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(_AT_COMMAND_START + r"(?:sudo\s+-n\s+)?networksetup\s+-set"),
        "This changes the real Mac's network configuration, which affects every application on "
        'it. Record the current state first (`networksetup -getwebproxy "Wi-Fi"` and friends '
        "into .local/) so it can be diffed afterwards — that discipline is the only thing that "
        "has ever caught this class of bug here. Note `-setwebproxy` ENABLES the proxy as a side "
        "effect, so when restoring, the state command comes second.",
    ),
    (
        re.compile(r"\bHTTPS_PROXY_MCP_E2E\s*=\s*1\b"),
        "This runs the live machine tests, which redirect this Mac's system proxy and create a "
        "real simulator device. Follow .claude/skills/live-machine-tests/SKILL.md: record the "
        "machine's proxy state before, and diff it afterwards. A green suite does not prove the "
        "machine came back.",
    ),
    (
        re.compile(r"\bsimctl\b[^|;&]*\b(?:delete|erase)\b"),
        "This destroys a simulator device or its contents. The E2E tests create and remove their "
        "own throwaway device; if this is not that device, check which one it is first.",
    ),
]


def decide(command: str) -> tuple[str, str] | None:
    """``("deny"|"ask", reason)`` for a hazardous command, or ``None`` to stay out of the way.

    Deny is checked first: a command that is both irreversible and machine-mutating should be
    refused rather than offered.
    """
    for pattern, reason in DENY:
        if pattern.search(command):
            return "deny", reason
    for pattern, reason in ASK:
        if pattern.search(command):
            return "ask", reason
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        # Fail open. A gate that blocks every command because it could not parse its own input
        # would be worse than no gate: it teaches people to disable it.
        return 0

    if payload.get("tool_name") != "Bash":
        return 0

    command = (payload.get("tool_input") or {}).get("command") or ""
    verdict = decide(command)
    if verdict is None:
        return 0

    decision, reason = verdict
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": reason,
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
