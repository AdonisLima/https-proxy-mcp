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
"""

from __future__ import annotations

from dataclasses import dataclass


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

        # A single empty argument is how networksetup spells "no bypass domains".
        commands.append(["-setproxybypassdomains", self.service, *(self.bypass or ("",))])
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
