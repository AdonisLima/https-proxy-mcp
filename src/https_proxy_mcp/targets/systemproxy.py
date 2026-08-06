"""The Mac's system proxy configuration: reading it, recording it, and putting it back.

Routing a Capture Target's egress through the proxy means redirecting the *machine's* HTTP
and HTTPS proxy settings, because CoreSimulator has no proxy setting of its own — it inherits
the host's. That makes this the first module here that can break the developer's machine
rather than merely the tool, and it is why everything in it is organised around one property:

    whatever the machine said before capture is exactly what it says afterwards.

"Restore to defaults" is the wrong model. A machine can already carry a proxy configuration —
the one this was developed on had a *disabled* `127.0.0.1:9090` — and a teardown that only
ran ``-setwebproxystate off`` would look correct there while silently discarding the server
and port. So the whole setting is captured, and the whole setting is restored.

Two ``networksetup`` behaviours shape the restore plan and are easy to get wrong:

- ``-setwebproxy`` **enables** the proxy as a side effect of setting the server and port. To
  restore a configured-but-disabled proxy the state command must therefore come *second*.
  Reversed, it leaves the machine proxied through a port that no longer exists.
- ``-getproxybypassdomains`` answers in prose when the list is empty, so a parser that just
  splits lines invents a bypass domain out of an English sentence.
- Clearing the bypass list needs the literal word ``Empty``. Passing ``""`` leaves an
  ``ExceptionsList`` containing one empty entry where the key previously held nothing, which is
  an unfaithful restore. This one was caught only by diffing the machine's real output after the
  live P3 run: both forms parse back to ``()``, so the round-trip assertion was satisfied while
  the machine had in fact been changed. A parser that normalises can hide a writer that does not.
"""

from __future__ import annotations

import atexit
import contextlib
import json
import os
import signal
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

#: The binaries this module drives. Absolute, because a redirected system proxy is not the
#: place to discover that `PATH` was surprising.
NETWORKSETUP = "/usr/sbin/networksetup"
SCUTIL = "/usr/sbin/scutil"

#: Sentinel filename, alongside ``sessions/`` under the root (SPEC.md, "Session directory").
SENTINEL_NAME = "proxy-state.json"

#: What ``-setproxybypassdomains`` wants in order to clear the list. Not an empty string —
#: see :meth:`SystemProxyState.restore_commands`.
CLEAR_BYPASS = "Empty"

#: A command runner: takes an argv list, returns stdout. The seam that keeps every test in
#: this module off the real machine. Production gets :func:`run_command`.
CommandRunner = Callable[..., str]


@dataclass(frozen=True, slots=True)
class ProxySetting:
    """One of the two proxy settings a network service carries: plain HTTP, or secure.

    ``server`` and ``port`` are kept even when ``enabled`` is false, because that is the
    state this module exists to preserve faithfully.
    """

    enabled: bool
    server: str
    port: int

    @property
    def configured(self) -> bool:
        """Whether there is a server worth restoring, as opposed to only a flag to clear."""
        return bool(self.server)


def parse_proxy_setting(output: str) -> ProxySetting:
    """Read the output of ``networksetup -getwebproxy`` or ``-getsecurewebproxy``.

    The keys are matched exactly rather than by substring, because ``Authenticated Proxy
    Enabled`` also ends in ``Enabled`` and carries a ``0``/``1`` value instead of ``Yes``/
    ``No``. A loose match reads that line instead and inverts the flag.
    """
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()

    port = fields.get("Port", "")
    return ProxySetting(
        enabled=fields.get("Enabled", "No").lower() == "yes",
        server=fields.get("Server", ""),
        port=int(port) if port.isdigit() else 0,
    )


def parse_bypass_domains(output: str) -> tuple[str, ...]:
    """Read the output of ``networksetup -getproxybypassdomains``.

    Lines containing whitespace are dropped, which is what distinguishes the "there aren't
    any" sentence from real entries: a bypass entry is a host, a domain glob or a CIDR block,
    and none of those can contain a space. Keying off the English text alone would break
    under a localised system.
    """
    return tuple(
        line.strip()
        for line in output.splitlines()
        if line.strip() and " " not in line.strip() and "\t" not in line.strip()
    )


@dataclass(frozen=True, slots=True)
class SystemProxyState:
    """Every proxy field of one network service, enough to restore it exactly."""

    service: str
    web: ProxySetting
    secure: ProxySetting
    bypass: tuple[str, ...]

    @property
    def server_summary(self) -> str:
        """A short human rendering of what this state will be restored to.

        Names the enabled flag as well as the address, because "configured but off" is the case
        this whole module exists to preserve and a bare host:port would hide it.
        """
        if not self.web.configured and not self.secure.configured:
            return "no proxy configured"
        address = f"{self.web.server or self.secure.server}:{self.web.port or self.secure.port}"
        enabled = self.web.enabled or self.secure.enabled
        return f"{address} ({'enabled' if enabled else 'disabled'})"

    def restore_commands(self) -> list[list[str]]:
        """The ``networksetup`` argument lists that put this state back, in order.

        Returned as data rather than executed so the plan can be asserted on without touching
        the machine — the ordering between ``-setwebproxy`` and ``-setwebproxystate`` is the
        part most worth testing, and it is invisible if the commands only ever run.
        """
        commands: list[list[str]] = []

        for kind, setting in (("web", self.web), ("secure", self.secure)):
            flag = "-setwebproxy" if kind == "web" else "-setsecurewebproxy"
            state_flag = f"{flag}state"

            # Server and port first: this command turns the proxy on as a side effect, so the
            # state command below is what has the final say.
            if setting.configured:
                commands.append([flag, self.service, setting.server, str(setting.port)])
            commands.append([state_flag, self.service, "on" if setting.enabled else "off"])

        # The literal word "Empty" is how networksetup spells "no bypass domains", per its own
        # help text. Passing "" instead leaves an ExceptionsList holding one empty entry where
        # the key previously held nothing — an unfaithful restore that this module's whole
        # purpose forbids. Found on the real machine after the P3 live run, and missed by the
        # round-trip test because the parser normalises both forms to ().
        commands.append(["-setproxybypassdomains", self.service, *(self.bypass or (CLEAR_BYPASS,))])
        return commands

    def to_dict(self) -> dict[str, object]:
        """The sentinel file's contents.

        Read by a different process than wrote it — the next server start, possibly after a
        crash — so this is the recovery path, not a convenience.
        """
        return {
            "service": self.service,
            "web": {"enabled": self.web.enabled, "server": self.web.server, "port": self.web.port},
            "secure": {
                "enabled": self.secure.enabled,
                "server": self.secure.server,
                "port": self.secure.port,
            },
            "bypass": list(self.bypass),
        }

    @classmethod
    def from_dict(cls, data: dict) -> SystemProxyState:
        """Rebuild state written by :meth:`to_dict`."""
        return cls(
            service=data["service"],
            web=ProxySetting(**data["web"]),
            secure=ProxySetting(**data["secure"]),
            bypass=tuple(data["bypass"]),
        )


def run_command(argv: list[str], *, stdin: str | None = None) -> str:
    """Run a system command and return its stdout, failing with something readable.

    ``CalledProcessError`` renders as an exit code and nothing else, which is useless when the
    command that failed was the one meant to restore the machine's networking. The stderr text
    is what says whether the service name was wrong or the operation was not permitted.
    """
    result = subprocess.run(
        argv,
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(
            f"{' '.join(argv)} failed with exit {result.returncode}:"
            f" {result.stderr.strip() or result.stdout.strip() or 'no output'}"
        )
    return result.stdout


def resolve_primary_service(runner: CommandRunner = run_command) -> str:
    """The name of the network service currently carrying the default route.

    Deliberately *not* the first entry in ``-listnetworkserviceorder``. On the machine this
    was developed on, service order ranks Thunderbolt Bridge first while the default route
    goes out ``en0`` (Wi-Fi), so ordering would have redirected an interface carrying no
    traffic and then reported success.

    ``scutil`` names the primary service by UUID, and only the ``Setup:`` key carries the
    human name that ``networksetup`` accepts, hence the two questions.
    """
    global_ipv4 = runner([SCUTIL], stdin="show State:/Network/Global/IPv4\n")
    service_id = _scutil_value(global_ipv4, "PrimaryService")
    if not service_id:
        raise OSError(
            "no primary network service: the machine appears to have no default route,"
            " so there is nothing to redirect"
        )

    setup = runner([SCUTIL], stdin=f"show Setup:/Network/Service/{service_id}\n")
    name = _scutil_value(setup, "UserDefinedName")
    if not name:
        raise OSError(f"primary network service {service_id} has no name to pass to networksetup")
    return name


def _scutil_value(output: str, key: str) -> str:
    """One value out of scutil's dictionary rendering, which is not JSON and not a plist."""
    for line in output.splitlines():
        name, separator, value = line.partition(":")
        if separator and name.strip() == key:
            return value.strip()
    return ""


class SystemProxy:
    """Redirects the machine's proxy settings, and guarantees they come back.

    The guarantee is a file, not a handler. Handlers cover an orderly exit; only a file on disk
    survives ``kill -9``, a panic or a power cut, which is why :meth:`repair` runs at every
    server start and is the layer that actually does the work in the case that matters.

    ``runner`` is a seam, not a backdoor: production always gets :func:`run_command`.
    """

    def __init__(
        self,
        sentinel_path: Path | str,
        *,
        runner: CommandRunner = run_command,
        service: str | None = None,
    ) -> None:
        self._sentinel = Path(sentinel_path)
        self._runner = runner
        self._service = service
        self._handlers_installed = False

    @property
    def sentinel_path(self) -> Path:
        return self._sentinel

    @property
    def service(self) -> str:
        """The service being redirected, resolved once and remembered."""
        if self._service is None:
            self._service = resolve_primary_service(self._runner)
        return self._service

    # --- reading --------------------------------------------------------------

    def read_state(self, service: str | None = None) -> SystemProxyState:
        """Every proxy field of ``service`` as it stands right now."""
        target = service or self.service
        return SystemProxyState(
            service=target,
            web=parse_proxy_setting(self._runner([NETWORKSETUP, "-getwebproxy", target])),
            secure=parse_proxy_setting(self._runner([NETWORKSETUP, "-getsecurewebproxy", target])),
            bypass=parse_bypass_domains(
                self._runner([NETWORKSETUP, "-getproxybypassdomains", target])
            ),
        )

    # --- layer 1: record, then change -----------------------------------------

    def redirect(self, host: str, port: int) -> SystemProxyState:
        """Point the machine's HTTP and HTTPS proxies at ``host:port``.

        Writes the prior state to the sentinel *first*. Doing it afterwards would leave a
        window where the machine is proxied and nothing on disk says how to undo that — which
        is precisely the window in which a crash is most likely, since the crash-prone work is
        what follows.
        """
        if self._sentinel.exists():
            raise RuntimeError(
                f"a proxy sentinel already exists at {self._sentinel}, which means an earlier"
                " capture died without restoring the system proxy. Overwriting it would record"
                " the already-proxied state as the state to return to and lose the machine's"
                " real configuration for good. Call repair() first."
            )

        prior = self.read_state()
        self._write_sentinel(prior)

        for flag in ("-setwebproxy", "-setsecurewebproxy"):
            self._runner([NETWORKSETUP, flag, prior.service, host, str(port)])
            self._runner([NETWORKSETUP, f"{flag}state", prior.service, "on"])
        return prior

    def _write_sentinel(self, state: SystemProxyState) -> None:
        """Write the sentinel at ``0600``, flushed all the way to disk.

        ``os.replace`` after an explicit fsync, so a crash mid-write cannot leave a truncated
        sentinel — the one file whose corruption costs the machine's real configuration.
        """
        self._sentinel.parent.mkdir(parents=True, exist_ok=True)
        scratch = self._sentinel.with_suffix(".partial")
        with open(scratch, "w") as handle:
            json.dump(state.to_dict(), handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        scratch.chmod(0o600)
        os.replace(scratch, self._sentinel)

    # --- restoring ------------------------------------------------------------

    def restore(self) -> SystemProxyState | None:
        """Put the recorded state back and remove the sentinel.

        Returns what was restored, or ``None`` when there was nothing to restore. That case is
        the common one rather than the edge one: ``CaptureState.start`` calls teardown on every
        failure path, so this runs after starts that never redirected anything.
        """
        if not self._sentinel.exists():
            return None

        try:
            recorded = SystemProxyState.from_dict(json.loads(self._sentinel.read_text()))
        except (ValueError, KeyError, TypeError):
            self._unproxy_after_losing_the_sentinel()
            return None

        for argv in recorded.restore_commands():
            self._runner([NETWORKSETUP, *argv])
        self._sentinel.unlink(missing_ok=True)
        return recorded

    def _unproxy_after_losing_the_sentinel(self) -> None:
        """Fail safe when the sentinel cannot be read.

        An unreadable sentinel still proves the machine was changed, and its prior state is
        gone, so exact restoration is impossible. Turning both proxies off is the lesser
        failure: the alternative leaves traffic pointed at a proxy that no longer exists, which
        looks to the developer like the network is broken. The file is moved aside rather than
        deleted so the evidence survives, and so the next start does not retry it forever.
        """
        service = self.service
        for flag in ("-setwebproxystate", "-setsecurewebproxystate"):
            self._runner([NETWORKSETUP, flag, service, "off"])
        self._sentinel.replace(self._sentinel.with_suffix(".corrupt"))

    # --- layer 3: repair on start ---------------------------------------------

    def repair(self) -> SystemProxyState | None:
        """Restore a sentinel left behind by a process that never got to clean up.

        This is the layer that covers ``kill -9``. It is the same operation as :meth:`restore`;
        the distinction is intent, and that the caller reports what it finds — a repaired
        sentinel is the only visible evidence that an earlier run died.
        """
        return self.restore()

    # --- layer 2: handlers ----------------------------------------------------

    def install_handlers(self) -> None:
        """Restore on any orderly exit: ``atexit``, ``SIGTERM`` and ``SIGINT``.

        The previous handler is chained rather than replaced, and the default disposition is
        re-raised after restoring, so installing this does not stop the process from dying the
        way it was told to. Signal handlers can only be installed from the main thread; that is
        where the MCP server runs, and a failure to install is reported rather than swallowed.
        """
        if self._handlers_installed:
            return

        atexit.register(self.restore_on_exit)
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous = signal.getsignal(signum)
            signal.signal(signum, self._make_signal_handler(signum, previous))
        self._handlers_installed = True

    def _make_signal_handler(self, signum, previous):
        def handler(received, frame):
            self.restore_on_exit()
            if callable(previous) and previous not in (signal.SIG_DFL, signal.SIG_IGN):
                previous(received, frame)
                return
            # Re-raise with the default disposition so the exit status still says what killed
            # the process, rather than turning every signal into a clean exit.
            signal.signal(received, signal.SIG_DFL)
            os.kill(os.getpid(), received)

        return handler

    def restore_on_exit(self) -> None:
        """Restore, swallowing failures. The callback ``atexit`` and the handlers share.

        An exception raised from ``atexit`` or from a signal handler replaces the real reason
        the process is exiting with a traceback from teardown. Worse, it is not even a useful
        one: if restoration failed the sentinel is still on disk, so the next start repairs it.
        """
        # Suppressed deliberately: the sentinel survives a failure here, so layer 3 covers it.
        with contextlib.suppress(Exception):
            self.restore()


def sentinel_path(root: Path | str) -> Path:
    """Where the sentinel lives for a given Session root."""
    return Path(root) / SENTINEL_NAME


def repair_system_proxy(root: Path | str, *, runner: CommandRunner = run_command) -> str | None:
    """Layer 3, as the server calls it at start. Returns a line to report, or ``None``.

    Called before anything else so a crashed predecessor cannot leave the machine proxied
    through a port this process is about to bind for something else.
    """
    proxy = SystemProxy(sentinel_path(root), runner=runner)
    if not proxy.sentinel_path.exists():
        return None

    restored = proxy.repair()
    if restored is None:
        return (
            "an earlier capture died without restoring the system proxy, and its sentinel"
            " could not be read; both proxies have been switched off to restore connectivity"
        )
    return (
        f"an earlier capture died without restoring the system proxy;"
        f" {restored.service} has been restored to its prior settings"
    )
