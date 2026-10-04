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

The record also names the process that wrote it, because there is one system proxy on the machine
and every editor window runs its own copy of this server, all resolving to the same default root.
Without an owner every process repairs, restores and deletes any record it finds: one window
starting up switches off another window's routing, leaving a Session that reports it is recording
while receiving nothing, and a window that has stopped can leave the machine routed through a proxy
nobody is listening on. So each of those paths asks who owns the record first, and a live foreign
owner's record is left entirely alone. Starting a Session is the one path that says so out loud:
it is refused, naming the holder and the signal that releases it, because proceeding would either
take the redirect over — the original defect with better manners — or bind a port for a Session
that can never see traffic.
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
from types import FrameType
from typing import Any, Literal

#: The binaries this module drives. Absolute, because a redirected system proxy is not the
#: place to discover that `PATH` was surprising.
NETWORKSETUP = "/usr/sbin/networksetup"
SCUTIL = "/usr/sbin/scutil"
PS = "/bin/ps"

#: Sentinel filename, alongside ``sessions/`` under the root (SPEC.md, "Session directory").
SENTINEL_NAME = "proxy-state.json"

#: What ``-setproxybypassdomains`` wants in order to clear the list. Not an empty string —
#: see :meth:`SystemProxyState.restore_commands`.
CLEAR_BYPASS = "Empty"

#: A command runner: takes an argv list, returns stdout. The seam that keeps every test in
#: this module off the real machine. Production gets :func:`run_command`.
CommandRunner = Callable[..., str]

#: Who the sentinel belongs to, resolved against the process table. Three of the four mean "go
#: ahead": only a live foreign owner's record is left alone, and ``absent`` — a record that names
#: no owner, or one whose owner cannot be read out of it at all — is treated exactly like ``dead``,
#: because connectivity is the fail-safe this module already points at.
Ownership = Literal["ours", "dead", "absent", "live_foreign"]


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
class ProxyOwner:
    """The process that redirected the machine's proxy, as the sentinel records it.

    ``started`` is an opaque string from ``ps -o lstart=``, compared for **equality only** and
    never parsed. It is what closes process-id reuse: with a liveness check alone, a record that
    survives a reboot names an id that now belongs to something unrelated, liveness answers
    "alive", repair is declined, and a self-healing bug becomes a permanent one — strictly worse
    than the defect being fixed.
    """

    pid: int
    started: str

    def to_dict(self) -> dict[str, object]:
        return {"pid": self.pid, "started": self.started}

    @classmethod
    def from_dict(cls, data: Any) -> ProxyOwner | None:
        """Rebuild an owner, or ``None`` when the record does not carry a usable one.

        Tolerant rather than strict, because the alternative to "no owner" is not an error the
        developer can act on: an unreadable owner resolves to dead, so the machine's networking
        comes back either way.
        """
        if not isinstance(data, dict):
            return None
        try:
            return cls(pid=int(data["pid"]), started=str(data.get("started", "")))
        except (KeyError, TypeError, ValueError):
            return None


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
    def from_dict(cls, data: dict[str, Any]) -> SystemProxyState:
        """Rebuild state written by :meth:`to_dict`."""
        return cls(
            service=data["service"],
            web=ProxySetting(**data["web"]),
            secure=ProxySetting(**data["secure"]),
            bypass=tuple(data["bypass"]),
        )


@dataclass(frozen=True, slots=True)
class ProxyRecord:
    """A sentinel that could be read: the state to put back, and who is holding it there.

    ``owner`` is ``None`` for a record that names none. There is no such record on disk anywhere,
    so this is not a migration path — it is the fail-safe, and it points the same way as every
    other unknown here.
    """

    prior: SystemProxyState
    owner: ProxyOwner | None


class ProxyHeldElsewhereError(RuntimeError):
    """Another live process holds the machine's proxy, so this one may not take it.

    Deliberately a plain exception. The core translates it into whatever its callers read — this
    module is reached through the ``CaptureTarget`` protocol, and a target that had to import the
    MCP framework in order to say "the machine's proxy is taken" would make the framework a
    dependency of every future target, Android included.

    Carries the holder as well as the message, because a caller that wants to *report* the pid
    rather than quote a sentence should not have to parse one back out.
    """

    def __init__(self, holder: ProxyOwner) -> None:
        self.holder = holder
        super().__init__(
            "the system proxy is already redirected by another https-proxy-mcp process"
            f" (pid {holder.pid}) that is still running, so nothing has been started here."
            " Taking the redirect over would leave that process recording nothing, and waiting"
            f" for it would hang this session. `kill {holder.pid}` sends SIGTERM, which makes that"
            " process restore the machine on its way out, after which this refusal no longer"
            " applies — whatever else this call needs is a separate matter and will say so. An"
            " uncatchable `kill -9` leaves its record behind for the next server start to repair"
        )


@dataclass(frozen=True, slots=True)
class ProxyReport:
    """Who holds the machine's proxy, and the one sentence that says so.

    The report a server turns into ``get_status``'s ``proxy`` field and ``diagnose_capture``'s
    held verdict. Composed here, beside the sentinel it describes, because the ownership
    semantics, the ownership resolution and the prose about both are one fact — the server
    layer that used to assemble it had to import this module's internals to say anything.

    ``holder`` is the live foreign process holding the record, when there is one: the half of
    the answer ``diagnose_capture`` needs that the prose only quotes. It is deliberately never
    serialised — ``owner`` already carries what a client acts on, and a wire field repeating
    one pid in a second shape invites reading the wrong one.

    ``ours`` is the field an agent acts on: stop_capture puts back a redirect this process
    made, and nothing else.
    """

    redirected: bool
    detail: str
    service: str | None
    sentinel: str | None
    owner: ProxyOwner | None
    ours: bool
    holder: ProxyOwner | None

    def to_dict(self) -> dict[str, object]:
        """The report as tool output, with absent-versus-null as the wire format.

        Three shapes, and which one a branch emits is part of the contract. A machine that is
        not redirected reports only ``redirected`` and ``detail`` — there is no service,
        sentinel or owner to be null *about*. A record that will not parse carries every key
        the readable branch carries, with ``service`` and ``owner`` nulled: those two are
        genuinely unknown — the record is the only thing that knew either, and it will not
        parse — while the path is known, and is what a human needs to go and look at the file.
        Reporting some keys and omitting others was the whole defect: a KeyError raised in
        exactly the branch that says "something is wrong here" is how an agent ends up
        concluding the machine is not proxied.
        """
        if not self.redirected:
            return {"redirected": False, "detail": self.detail}
        return {
            "redirected": True,
            "service": self.service,
            "sentinel": self.sentinel,
            "owner": None if self.owner is None else {"pid": self.owner.pid, "ours": self.ours},
            "detail": self.detail,
        }


def release_instruction(pid: int) -> str:
    """The one sentence that says how a redirect held by another process is released.

    Every branch that reports a live foreign holder ends in this instruction — the diagnose
    remediation, the iOS target's held verdict, and proxy repair — and it is the actionable
    kernel of the whole ownership model, so it is composed exactly once. The drift this
    prevents already happened: the sentence existed in more than one wording and one instance
    had to be corrected. Call-site suffixes — what a retried call still needs, why nothing is
    released from here — stay at the call sites. The refusal exception's message keeps its own
    wording: there ``kill`` names the signal rather than standing alone as the instruction, a
    different grammatical context.
    """
    return f"`kill {pid}` makes that process restore the machine on its way out"


def _proxy_detail(
    prior: SystemProxyState,
    owner: ProxyOwner | None,
    ownership: Ownership,
    *,
    recording: bool,
) -> str:
    """One sentence about a redirect, branched on who owns it.

    The promise that state will be restored on ``stop_capture`` is only made about a record this
    process owns. For a record another live process holds it is simply false — stopping here
    restores nothing, because restoring someone else's redirect is the defect this work removes —
    and prose that is confidently wrong is what sent the developer looking in the wrong place.

    ``recording`` exists for the same reason one layer along. "This Session does not own it" is
    true and useful while a Session is open here, and names something that does not exist when
    there is none — which is the state ``diagnose_capture`` borrows this sentence in, since its
    ``proxy_held_elsewhere`` branch is reached only when nothing is recording. CONTEXT.md's Session
    is a precise word and this branch is the one place the tool was using it loosely.
    """
    routed = f"{prior.service} is routed through the capture port"
    restored_at_the_next_start = f"The next server start restores server={prior.server_summary}"

    # Checked before the ownership states so the rest can name a pid: a record with no owner is
    # the only one that resolves to `absent`, and it has no process to name.
    if owner is None:
        return (
            f"{routed}, and the record does not name the process that did it, so nobody owns it."
            f" {restored_at_the_next_start}"
        )

    if ownership == "ours":
        return (
            f"{routed} by this process (pid {owner.pid});"
            f" it will be restored to server={prior.server_summary} on stop_capture"
        )

    if ownership == "live_foreign":
        held = (
            "This Session does not own it, so stopping here leaves it in place"
            if recording
            else "Nothing here owns it, so there is nothing to stop here"
        )
        return (
            f"{routed} by another https-proxy-mcp process (pid {owner.pid}) that is still running."
            f" {held}; `kill {owner.pid}` makes that process put the machine back on its way out"
        )

    return (
        f"{routed} by a process that is no longer running (pid {owner.pid}), so it was left behind"
        f" rather than released. {restored_at_the_next_start}"
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


def read_process_start(pid: int, runner: CommandRunner = run_command) -> str:
    """The opaque start time of ``pid``, or ``""`` when it cannot be read.

    ``ps`` exits non-zero for a process that is gone, which :func:`run_command` raises as an
    ``OSError``. Empty therefore means *unknown*, and unknown must never compare equal to
    unknown — two unrelated processes would look like one.
    """
    try:
        return runner([PS, "-o", "lstart=", "-p", str(pid)]).strip()
    except OSError:
        return ""


def resolve_ownership(
    owner: ProxyOwner | None,
    *,
    runner: CommandRunner = run_command,
) -> Ownership:
    """Which of the four states a recorded owner is in, asked of the process table.

    Both halves of the identity have to agree. A matching id with a *different* start time is a
    reused id and resolves to ``dead`` — including when the id is our own, which after a reboot is
    a real possibility rather than a hypothetical one.
    """
    if owner is None:
        return "absent"

    started = read_process_start(owner.pid, runner)
    if not started or not owner.started or started != owner.started:
        return "dead"
    return "ours" if owner.pid == os.getpid() else "live_foreign"


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

        The owner is written here rather than by :meth:`SystemProxyState.to_dict`, because it is
        not part of the prior state: it says who changed the machine, not what the machine looked
        like. ``from_dict`` keys on its own fields, so it reads such a record unchanged.
        """
        document = state.to_dict()
        document["owner"] = self._identity().to_dict()

        self._sentinel.parent.mkdir(parents=True, exist_ok=True)
        scratch = self._sentinel.with_suffix(".partial")
        with open(scratch, "w") as handle:
            json.dump(document, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        scratch.chmod(0o600)
        os.replace(scratch, self._sentinel)

    def _identity(self) -> ProxyOwner:
        """This process, as the sentinel will record it.

        The start time goes through the same runner seam as everything else here, which is what
        keeps every test of the ownership rules off the real machine.
        """
        pid = os.getpid()
        return ProxyOwner(pid=pid, started=read_process_start(pid, self._runner))

    # --- ownership ------------------------------------------------------------

    def read_record(self) -> ProxyRecord | None:
        """The sentinel's prior state and owner, or ``None`` when it cannot be read.

        Ownership cannot be read out of a record that cannot be *parsed*, so a corrupt sentinel
        has no owner at all — which is what keeps the existing fail-safe ahead of the ownership
        rules rather than behind them. An ``owner`` key on an otherwise unreadable record is not
        enough to make it someone's property.
        """
        if not self._sentinel.exists():
            return None

        try:
            document = json.loads(self._sentinel.read_text())
            prior = SystemProxyState.from_dict(document)
        except (ValueError, KeyError, TypeError):
            return None
        return ProxyRecord(prior=prior, owner=ProxyOwner.from_dict(document.get("owner")))

    def ownership(self) -> Ownership:
        """Who the sentinel on disk belongs to. ``absent`` when there is nothing to attribute.

        The four-state answer, and deliberately **not** what the call sites use: they branch on
        :meth:`foreign_holder`, which collapses the three safe states into ``None`` so a caller
        cannot forget one. This exists so the four states can be asserted separately — the
        distinction between ``dead`` and ``live_foreign`` is the whole fix, and a test that could
        only see "safe or not" would not notice the two being confused.
        """
        record = self.read_record()
        return "absent" if record is None else resolve_ownership(record.owner, runner=self._runner)

    def foreign_holder(self) -> ProxyOwner | None:
        """The live process holding the machine's proxy, when it is not this one.

        The one query the careless call sites needed and did not have. ``None`` covers every case
        in which acting is safe, so a caller branches once rather than enumerating four states.
        """
        record = self.read_record()
        if record is None:
            return None
        if resolve_ownership(record.owner, runner=self._runner) != "live_foreign":
            return None
        return record.owner

    def refuse_if_held(self) -> None:
        """Raise if a live foreign process holds the record; do nothing at all otherwise.

        The one path that refuses rather than skipping. Repair, stop and exit all *decline to act*
        on a record they do not own, which is silent because there is nothing the caller wanted to
        do that it now cannot. Starting a Session is different: proceeding means either taking the
        redirect over — the original defect with better manners — or binding a port and reporting a
        Session that will never see traffic. Both are worse than a message.
        """
        holder = self.foreign_holder()
        if holder is not None:
            raise ProxyHeldElsewhereError(holder)

    # --- the report of who holds it ----------------------------------------------

    def report(self, *, recording: bool) -> ProxyReport:
        """Whether the machine's proxy is redirected, to what, and by whom — from one read.

        Two callers want two halves of one answer: ``get_status`` wants the report, and
        ``diagnose_capture`` wants the holder *and* the report's sentence about it. Asking
        twice resolved ownership twice, and a holder that exited between the two reads produced
        a ``proxy_held_elsewhere`` verdict whose detail came from the dead-owner branch.
        Vanishingly unlikely, harmless either way, and an answer contradicting itself is
        precisely the failure this work exists to stop — so the sentinel is read once and
        ownership resolved once (one ``ps`` invocation), and the sentence and the holder always
        come from the same read.

        ``recording`` is the only server-side fact the prose needs, and crosses as a bool:
        which of the two live-foreign sentences to say (see :func:`_proxy_detail`).
        """
        if not self._sentinel.exists():
            return ProxyReport(
                redirected=False,
                detail="the system proxy is not redirected",
                service=None,
                sentinel=None,
                owner=None,
                ours=False,
                holder=None,
            )

        record = self.read_record()
        if record is None:
            return ProxyReport(
                redirected=True,
                detail=f"redirected, but the sentinel at {self._sentinel} could not be read;"
                " the next server start will switch the proxy off to restore connectivity",
                service=None,
                sentinel=str(self._sentinel),
                owner=None,
                ours=False,
                holder=None,
            )

        ownership = resolve_ownership(record.owner, runner=self._runner)
        return ProxyReport(
            redirected=True,
            detail=_proxy_detail(record.prior, record.owner, ownership, recording=recording),
            service=record.prior.service,
            sentinel=str(self._sentinel),
            owner=record.owner,
            ours=ownership == "ours",
            holder=record.owner if ownership == "live_foreign" else None,
        )

    # --- restoring ------------------------------------------------------------

    def restore(self) -> SystemProxyState | None:
        """Put the recorded state back and remove the sentinel.

        Returns what was restored, or ``None`` when there was nothing to restore. That case is
        the common one rather than the edge one: ``CaptureState.start`` calls teardown on every
        failure path, so this runs after starts that never redirected anything.

        A record held by a live foreign owner is one of those nothings, and is the whole point of
        the owner field: restoring here would put the machine back while another process is still
        routing traffic at a proxy it believes it owns, leaving that Session reporting that it is
        recording while receiving nothing.
        """
        if not self._sentinel.exists():
            return None

        record = self.read_record()
        if record is None:
            self._unproxy_after_losing_the_sentinel()
            return None

        if resolve_ownership(record.owner, runner=self._runner) == "live_foreign":
            return None

        for argv in record.prior.restore_commands():
            self._runner([NETWORKSETUP, *argv])
        self._sentinel.unlink(missing_ok=True)
        return record.prior

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

        A dead owner is exactly the case this layer already existed for, so crash recovery is
        unchanged by ownership. A *live* owner is not a crash at all, and is left alone; callers
        that need to say so ask :meth:`foreign_holder` first.
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

    def _make_signal_handler(
        self,
        signum: int,
        previous: Callable[[int, FrameType | None], Any] | int | None,
    ) -> Callable[[int, FrameType | None], None]:
        def handler(received: int, frame: FrameType | None) -> None:
            self.restore_on_exit()
            # The two checks are in this order so the comparison happens before `callable()`
            # narrows `previous` to a function type, which would make comparing it against the
            # Handlers enum provably non-overlapping. Both are side-effect free, so the order
            # is presentational: the guard itself is unchanged, and deliberately kept even
            # though a Handlers member is never callable. One test now reaches this factory and
            # exercises the chaining branch below; the default-disposition branch after it still
            # has no coverage, because re-raising would take the test runner down with it.
            if previous not in (signal.SIG_DFL, signal.SIG_IGN) and callable(previous):
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

    A predecessor that is still *running* is not a crash, and taking its redirect away is the
    defect the owner field exists to stop — every editor window on the machine starts one of
    these, so this ran against live captures. It is reported rather than silently skipped,
    because "another window holds your machine's proxy" is the fact that ends the investigation.
    """
    proxy = SystemProxy(sentinel_path(root), runner=runner)
    if not proxy.sentinel_path.exists():
        return None

    holder = proxy.foreign_holder()
    if holder is not None:
        return (
            f"the system proxy is redirected by another https-proxy-mcp process (pid {holder.pid})"
            f" that is still running, so it has been left alone; {release_instruction(holder.pid)}"
        )

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
