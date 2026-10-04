"""What gets *reported*, as distinct from what was captured.

Capture is unconditional: everything through the proxy is recorded. Reporting is narrowed, in
the order SPEC.md gives — the Run window first, then a builtin denylist of OS chatter, then an
optional Scope. All three live here, so a Timeline inherits them the same way a listing does
rather than reimplementing the arithmetic next to the correlator.

The reason narrowing exists at all is a property of the chosen capture path rather than a
convenience. In regular proxy mode every connection arrives from localhost, so there is **no
process attribution** (ADR-0002): the simulator's traffic and the developer's browser traffic
are indistinguishable at the proxy. Time and host are the only filters available.

The denylist is deliberately conservative, and the omissions matter more than the entries.
``googleapis.com`` is **not** denied, because for a great many apps that is the backend — and a
denylist that hides the traffic being debugged is worse than having none. Nothing is ever
silently dropped either: every caller gets the count of what was hidden, because a tool that
quietly removes rows reads as "there was no traffic", which is the most misleading thing this
tool could say.
"""

from __future__ import annotations

from dataclasses import dataclass

from https_proxy_mcp.capture.exchange import Exchange
from https_proxy_mcp.capture.hostsets import host_matches

#: Domains whose traffic is OS or platform chatter rather than the app under test. Matched on
#: label boundaries, so ``apple.com`` covers ``gs-loc.apple.com`` but not ``notapple.com``.
DENIED_SUFFIXES = (
    # Apple platform services: push, activation, time, location, config, asset CDNs.
    "apple.com",
    "icloud.com",
    "cdn-apple.com",
    "mzstatic.com",
    "apple-dns.net",
    "itunes.com",
    # Google's static asset hosts. Note googleapis.com is deliberately absent.
    "gstatic.com",
    # Crash and telemetry ingest, which is high volume and never the thing being debugged.
    "crashlytics.com",
    "app-measurement.com",
    "sentry.io",
    "bugsnag.com",
    "datadoghq.com",
    "newrelic.com",
)

#: Certificate revocation checking, which every TLS stack does and no app asked for. Matched on
#: the *first label* because the domains vary wildly — ocsp.digicert.com, crl3.digicert.com,
#: ocsp2.apple.com — while the label naming convention is near universal.
DENIED_LABEL_PREFIXES = ("ocsp", "crl")


def is_denied(host: str) -> bool:
    """Whether ``host`` is builtin chatter rather than traffic worth reporting."""
    name = host.lower().rstrip(".")

    first_label = name.split(".", 1)[0]
    if any(first_label.startswith(prefix) for prefix in DENIED_LABEL_PREFIXES):
        return True

    return any(
        name == suffix or name.endswith(f".{suffix}")  # label boundary, not bare substring
        for suffix in DENIED_SUFFIXES
    )


def in_scope(host: str, scope: tuple[str, ...]) -> bool:
    """Whether ``host`` is one of the hosts the agent said it cared about.

    A thin delegate to :func:`~https_proxy_mcp.capture.hostsets.host_matches`, which owns what a
    host entry means — the entry covers its subdomains, and a leading ``*.`` is accepted and means
    the same thing. The rule left this module once something other than reporting needed it: it is
    also what decides which hosts are *intercepted*, and two copies of it would be free to drift
    into disagreeing about the same host. The name stays here because a Scope is a reporting
    narrowing, and both of its callers read better for it.

    ``is_denied`` above still carries its own copy of the covers-its-subdomains arithmetic, which is
    deliberate rather than an oversight: the denylist is a fixed list of bare suffixes with no
    ``*.`` spelling and no root dot, it feeds reporting only, and so it cannot produce the
    reporting-versus-interception drift :mod:`~https_proxy_mcp.capture.hostsets` exists to prevent.
    """
    return host_matches(host, scope)


@dataclass(frozen=True, slots=True)
class ReportingFilter:
    """The narrowing applied to a listing: the Run window, the Scope, and whether chatter is
    included."""

    #: Hosts the agent is interested in. ``None`` means no Scope at all; an empty tuple is a
    #: Scope that names no hosts, and reports nothing. The two are genuinely different.
    scope: tuple[str, ...] | None = None
    #: Opt back in to the builtin denylist's traffic.
    include_denied: bool = False
    #: The Run's interval as ``(start, end)`` epoch seconds, or ``None`` for no window at all.
    #: An ``end`` of ``None`` is a Run still in progress, which reports everything since its start.
    window: tuple[float, float | None] | None = None

    @property
    def narrows(self) -> bool:
        """Whether this filter can hide anything, which is what decides if a count is worth
        reporting to the caller."""
        return self.scope is not None or self.window is not None or not self.include_denied

    def allows_time(self, exchange: Exchange) -> bool:
        """Whether this Exchange happened during the Run.

        Narrows by **intersection**, not by start time. A request already in flight when the Run
        began is precisely the traffic a Timeline most wants — it overlaps the Run's first Steps,
        and an assertion failing because of it is the case ADR-0004 exists for. Testing
        ``t_start`` alone would throw it away.

        An Exchange with no end never finished, so it cannot have finished before the window.
        """
        if self.window is None:
            return True

        start, end = self.window
        if end is not None and exchange.t_start > end:
            return False
        return exchange.t_end is None or exchange.t_end >= start

    def allows_host(self, host: str) -> bool:
        """Whether traffic for ``host`` is reported at all.

        Separate from :meth:`allows` because it is also what decides which *TLS handshake
        failures* count — those never become Exchanges, so there is no Exchange to ask about.

        A Scope beats the denylist: if the agent named a host explicitly, hiding it would be the
        tool second-guessing an instruction, and the denylist's guess is far weaker than the
        agent's statement.
        """
        if self.scope is not None:
            return in_scope(host, self.scope)
        if self.include_denied:
            return True
        return not is_denied(host)

    def allows(self, exchange: Exchange) -> bool:
        """Whether this Exchange is reported.

        Window first, then host, in the order SPEC.md gives. Both must pass: the right host at
        the wrong time is still somebody else's Run.
        """
        return self.allows_time(exchange) and self.allows_host(exchange.host)

    def apply(self, exchanges: list[Exchange]) -> tuple[list[Exchange], int]:
        """The Exchanges that are reported, and how many were hidden."""
        kept = [item for item in exchanges if self.allows(item)]
        return kept, len(exchanges) - len(kept)
