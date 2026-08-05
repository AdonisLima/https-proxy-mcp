"""Tests for narrowing what is *reported*, which is never the same as what was captured.

Capture is unconditional — everything through the proxy is recorded (CONTEXT.md, SPEC.md).
What these tests govern is the second question: of everything recorded, what does an agent get
shown. Two mechanisms, one shape:

- a **builtin denylist** of OS chatter, which exists because regular proxy mode has no process
  attribution (ADR-0002), so simulator traffic and Safari traffic are indistinguishable at the
  proxy and the only available filters are time and host;
- an optional **Scope**, the set of hosts the agent said it cares about.

The denylist is deliberately conservative. `googleapis.com` is *not* on it, because for a great
many apps that is the backend, and a denylist that hides the traffic you are debugging is worse
than no denylist at all.
"""

import pytest

from https_proxy_mcp.capture.exchange import Exchange
from https_proxy_mcp.capture.reporting import ReportingFilter, is_denied


def exchange(host: str, exchange_id: int = 1) -> Exchange:
    """A metadata-only Exchange; nothing here reads a body."""
    return Exchange(
        id=exchange_id,
        t_start=1000.0,
        t_end=1000.5,
        method="GET",
        url=f"https://{host}/thing",
        host=host,
        path="/thing",
        scheme="https",
        http_version="HTTP/2.0",
        status_code=200,
        request_headers=[],
        response_headers=[],
        request_body=None,
        response_body=None,
        server_address=None,
        error=None,
    )


# --- the denylist -------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "gs-loc.apple.com",
        "configuration.apple.com",
        "p52-content.icloud.com",
        "ocsp.digicert.com",
        "crl3.digicert.com",
        "fonts.gstatic.com",
        "settings.crashlytics.com",
    ],
)
def test_os_chatter_is_denied(host):
    assert is_denied(host) is True


@pytest.mark.parametrize(
    "host",
    [
        "www.googleapis.com",
        "firestore.googleapis.com",
        "api.example.com",
        "my-app.herokuapp.com",
        "localhost",
    ],
)
def test_application_traffic_is_not_denied(host):
    """The conservative half of the bargain, and the more important one: for many apps
    googleapis.com *is* the backend."""
    assert is_denied(host) is False


def test_a_denied_domain_matches_its_subdomains_but_not_a_lookalike():
    """Suffix matching has to respect label boundaries, or `notapple.com` is denied too."""
    assert is_denied("anything.apple.com") is True
    assert is_denied("notapple.com") is False
    assert is_denied("apple.com.evil.test") is False


# --- the Scope ---------------------------------------------------------------


def test_no_scope_reports_everything_not_denied():
    reporting = ReportingFilter()

    assert reporting.allows(exchange("api.example.com")) is True
    assert reporting.allows(exchange("gs-loc.apple.com")) is False


def test_a_scope_keeps_only_the_hosts_it_names():
    reporting = ReportingFilter(scope=("api.example.com",))

    assert reporting.allows(exchange("api.example.com")) is True
    assert reporting.allows(exchange("cdn.example.com")) is False


def test_a_scope_entry_covers_subdomains():
    reporting = ReportingFilter(scope=("example.com",))

    assert reporting.allows(exchange("api.example.com")) is True
    assert reporting.allows(exchange("example.com")) is True
    assert reporting.allows(exchange("counterexample.com")) is False


def test_a_wildcard_scope_entry_is_accepted():
    """Agents write `*.example.com` whether or not it is required, so it has to mean what it
    looks like rather than matching nothing."""
    reporting = ReportingFilter(scope=("*.example.com",))

    assert reporting.allows(exchange("api.example.com")) is True


def test_a_scope_overrides_the_denylist():
    """If the agent explicitly named a host, hiding it would be the tool second-guessing an
    instruction — and the denylist's guesses are much weaker than the agent's statement."""
    reporting = ReportingFilter(scope=("configuration.apple.com",))

    assert reporting.allows(exchange("configuration.apple.com")) is True


def test_including_denied_traffic_shows_the_chatter():
    reporting = ReportingFilter(include_denied=True)

    assert reporting.allows(exchange("gs-loc.apple.com")) is True


# --- applying it -------------------------------------------------------------


def test_applying_reports_what_survived_and_counts_what_did_not():
    """The count is not decoration. A tool that quietly drops rows reads as "there was no
    traffic", which is the single most misleading thing this tool could say."""
    reporting = ReportingFilter()
    exchanges = [
        exchange("api.example.com", 1),
        exchange("gs-loc.apple.com", 2),
        exchange("ocsp.digicert.com", 3),
    ]

    kept, hidden = reporting.apply(exchanges)

    assert [item.id for item in kept] == [1]
    assert hidden == 2


def test_applying_an_empty_scope_list_reports_nothing():
    """An empty Scope is a Scope naming no hosts, not an absent one. `None` means no Scope."""
    reporting = ReportingFilter(scope=())

    kept, hidden = reporting.apply([exchange("api.example.com", 1)])

    assert kept == []
    assert hidden == 1
