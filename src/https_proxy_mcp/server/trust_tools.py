"""The trust tools: per-device installs, the system keychain, and reporting what is trusted.

The asymmetry between the two installs is deliberate (SPEC.md, "Trust"). A simulator's trust
store is per-device, disposable, needs no privilege and is wiped by ``simctl erase``, so
installing there is inherent to ``start_capture`` rather than something to ask about. The macOS
system keychain is none of those: it needs ``sudo`` and it is machine-wide, so it gets its own
tool and is never implicit.

**The system install must never wait for a password.** This server speaks JSON-RPC over stdin.
An interactive ``sudo`` prompt reads from the same descriptor, so it would consume the client's
next message as a password attempt and hang the session with no way out. ``sudo -n`` is
therefore mandatory here, not a nicety: it fails immediately when credentials are not cached,
and the tool hands back the exact command a human can run instead.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from https_proxy_mcp.server.state import CaptureState
from https_proxy_mcp.targets.ios_simulator import IosSimulatorTarget

#: Where a machine-wide authority goes.
SYSTEM_KEYCHAIN = "/Library/Keychains/System.keychain"


def system_install_command(pem: str, *, non_interactive: bool = False) -> list[str]:
    """The command that trusts ``pem`` machine-wide.

    Built in one place so the command that is *attempted* and the command that is *reported to
    the human* cannot drift apart — a suggested command that differs from the one that failed
    would be worse than no suggestion.
    """
    sudo = ["sudo", "-n"] if non_interactive else ["sudo"]
    return [
        *sudo,
        "security",
        "add-trusted-cert",
        "-d",
        "-r",
        "trustRoot",
        "-k",
        SYSTEM_KEYCHAIN,
        pem,
    ]


def register_trust_tools(server: FastMCP, state: CaptureState) -> None:
    """Register the tools that arrange and report trust in the Interception Authority."""

    @server.tool()
    def get_trust_status(target: str = "ios-simulator", udid: str | None = None) -> dict[str, Any]:
        """Report whether a Capture Target trusts the Interception Authority.

        Answerable without a Session, so trust can be checked before committing to a capture.
        For a simulator this reads the device's own trust store rather than inferring anything
        from an earlier install — the store is the only thing that actually knows.
        """
        status = _trust_only(state.build_target(target, udid=udid))
        return {"target": target, "trusted": status.trusted, "detail": status.detail}

    @server.tool()
    def install_authority(
        target: str = "ios-simulator", udid: str | None = None
    ) -> dict[str, Any]:
        """Add the Interception Authority to a Capture Target's trust store.

        Called automatically by ``start_capture`` for simulators, so reach for it directly only
        after a ``diagnose_capture`` of ``untrusted_authority`` — for instance after
        ``simctl erase`` wiped the device's trust store.

        A simulator must be booted: ``simctl keychain`` cannot reach a shut-down device's trust
        store. The returned detail says so when that is the problem.
        """
        capture_target = state.build_target(target, udid=udid)
        status = capture_target.ensure_trusted()
        return {"target": target, "trusted": status.trusted, "detail": status.detail}

    @server.tool()
    def install_authority_system() -> dict[str, Any]:
        """Trust the Interception Authority machine-wide, in the macOS system keychain.

        **Never called for you.** It needs ``sudo`` and it affects every application on the
        machine, not just the app under test, so it is always an explicit decision. Prefer
        ``install_authority`` — a simulator's own trust store is enough for capturing a
        simulator, and it is disposable.

        Only useful when something outside a simulator has to trust the authority: a macOS
        build of the app, or a desktop browser.

        This will not prompt for a password. If ``sudo`` credentials are not already cached the
        call fails harmlessly and returns the exact command to run in a terminal instead.
        """
        target = IosSimulatorTarget(root=state.root, runner=state.runner)
        pem = str(target.authority_pem())

        try:
            state.runner(system_install_command(pem, non_interactive=True))
        except OSError as exc:
            return {
                "installed": False,
                "detail": (
                    "sudo declined without cached credentials, which is expected — this server"
                    f" must never prompt for a password on stdin. Reported: {exc}"
                ),
                "command": " ".join(system_install_command(pem)),
                "authority": pem,
            }

        return {
            "installed": True,
            "detail": f"{pem} is now trusted machine-wide in {SYSTEM_KEYCHAIN}",
            "command": " ".join(system_install_command(pem)),
            "authority": pem,
        }


def _trust_only(capture_target: Any) -> Any:
    """Report trust without installing anything.

    ``get_trust_status`` is a question, and a question that changes the thing it asks about is a
    trap: an agent calling it to check would silently arrange trust and then be unable to tell
    whether the device had it before.
    """
    if isinstance(capture_target, IosSimulatorTarget):
        return capture_target.trust_status()
    return capture_target.ensure_trusted()
