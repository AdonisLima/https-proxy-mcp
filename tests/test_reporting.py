"""Tests for narrowing what is *reported*, which is never the same as what was captured.

What these tests govern is the second question: of everything recorded, what does an agent get
shown. What was recorded in the first place is a separate decision and lives elsewhere — under
Passthrough a Scope also narrows that (ADR-0007), and nothing in this module knows or should.
Two mechanisms, one shape:

- a **builtin denylist** of OS chatter, which exists because regular proxy mode has no process
  attribution (ADR-0002), so simulator traffic and Safari traffic are indistinguishable at the
  proxy and the only available filters are time and host;
- an optional **Scope**, the set of hosts the agent said it cares about.

The denylist is deliberately conservative. `googleapis.com` is *not* on it, because for a great
many apps that is the backend, and a denylist that hides the traffic you are debugging is worse
than no denylist at all.
"""

from dataclasses import replace

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


# --- the Run window -----------------------------------------------------------
#
# SPEC.md orders the narrowings: the Run window first, then the denylist, then the Scope. The
# first arrives with P4, and it belongs here beside the other two rather than in the Timeline
# code, so a Timeline inherits it the same way every listing does.


def timed(exchange_id: int, start: float, end: float | None) -> Exchange:
    """An Exchange placed in time, on a host nothing else narrows."""
    item = exchange("api.example.com", exchange_id)
    return replace(item, t_start=start, t_end=end)


def test_no_window_reports_traffic_from_any_time():
    reporting = ReportingFilter()

    assert reporting.allows(timed(1, 10.0, 11.0)) is True


def test_an_exchange_inside_the_window_is_reported():
    reporting = ReportingFilter(window=(100.0, 200.0))

    assert reporting.allows(timed(1, 150.0, 160.0)) is True


def test_an_exchange_wholly_before_the_window_is_not():
    reporting = ReportingFilter(window=(100.0, 200.0))

    assert reporting.allows(timed(1, 10.0, 20.0)) is False


def test_an_exchange_wholly_after_the_window_is_not():
    reporting = ReportingFilter(window=(100.0, 200.0))

    assert reporting.allows(timed(1, 300.0, 310.0)) is False


def test_an_exchange_straddling_the_start_of_the_window_is_reported():
    """The window narrows by *intersection*, not by start time.

    A request that was already in flight when the Run began is exactly the traffic a Timeline
    most wants: it overlaps the Run's first Steps, and an assertion failing because of it is the
    case ADR-0004 exists for. Filtering on `t_start` alone would discard it.
    """
    reporting = ReportingFilter(window=(100.0, 200.0))

    assert reporting.allows(timed(1, 90.0, 150.0)) is True


def test_an_exchange_straddling_the_end_of_the_window_is_reported():
    reporting = ReportingFilter(window=(100.0, 200.0))

    assert reporting.allows(timed(1, 150.0, 250.0)) is True


def test_an_exchange_that_never_completed_is_reported_if_it_started_before_the_window_ended():
    """No end means it was still outstanding, so it cannot have finished before the window."""
    reporting = ReportingFilter(window=(100.0, 200.0))

    assert reporting.allows(timed(1, 150.0, None)) is True
    assert reporting.allows(timed(2, 90.0, None)) is True
    assert reporting.allows(timed(3, 250.0, None)) is False


def test_an_open_ended_window_reports_everything_from_its_start():
    """A Run still in progress has no end yet, and its Timeline should show what is happening."""
    reporting = ReportingFilter(window=(100.0, None))

    assert reporting.allows(timed(1, 5000.0, 5001.0)) is True
    assert reporting.allows(timed(2, 10.0, 20.0)) is False


def test_the_window_and_the_host_narrowings_both_apply():
    """Right host, wrong time is still hidden — and vice versa."""
    reporting = ReportingFilter(window=(100.0, 200.0))
    denied_host_in_window = replace(exchange("gs-loc.apple.com", 1), t_start=150.0, t_end=160.0)

    assert reporting.allows(denied_host_in_window) is False
    assert reporting.allows(timed(2, 150.0, 160.0)) is True


def test_a_window_alone_counts_as_narrowing():
    """`narrows` decides whether a `hidden` count is worth reporting, so a window has to
    register — otherwise a Timeline could drop rows and claim it narrowed nothing."""
    assert ReportingFilter(window=(100.0, 200.0), include_denied=True).narrows is True
    assert ReportingFilter(include_denied=True).narrows is False


def test_allows_host_ignores_the_window():
    """`allows_host` also judges TLS handshake failures, which never became Exchanges and have
    no interval to test. It must stay answerable from a host alone."""
    reporting = ReportingFilter(window=(100.0, 200.0))

    assert reporting.allows_host("api.example.com") is True
    assert reporting.allows_host("gs-loc.apple.com") is False
