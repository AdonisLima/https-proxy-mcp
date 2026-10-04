"""One definition of what a host entry means, asserted through both of the forms that need it.

A host entry covers itself and its subdomains, and a leading ``*.`` is accepted and means the same
thing. Two things need that rule in two different shapes: reporting asks a boolean question about a
host it already has, and interception has to hand the rule to mitmproxy as regexes *before* any
host exists.

Two independent expressions of one rule is the trap `targets/CLAUDE.md` records as costing a real
debugging session — **a parser that normalises can hide a writer that does not**, and the
round-trip assertion passes the whole time. So the central test here is a table of hosts run
through *both* forms, asserting they agree on every row. The rows that earn their place are the
nearly-right ones: a bare suffix with no label boundary, a host carrying a root dot, and a host
whose dots have been replaced by other characters — which is what a forgotten `re.escape` lets in.

The pattern side is evaluated the way mitmproxy evaluates it: an unanchored, case-insensitive
`re.search` over candidate strings of the form `host:port` (`mitmproxy/addons/next_layer.py`). That
is a reproduction of the engine's rule, not the engine — the engine itself is driven with real
traffic elsewhere. What it proves here is that the two forms agree about every host in the table,
which is a claim about the hosts that occur rather than about every possible string: they do diverge
at a doubled root dot, which nothing emits, and `hostsets.py` says why that is left alone.

`reporting.in_scope`'s two existing callers are covered by `test_reporting.py` and
`test_tools_runs.py`. **No existing test in either file was altered** — `test_tools_runs.py` gained
one, for the subdomain half of the rule its caller had never covered — so their staying green is
what says making `in_scope` a delegate changed nothing either caller can observe.
"""

import re

import pytest

from https_proxy_mcp.capture import reporting
from https_proxy_mcp.capture.hostsets import allow_host_patterns, host_matches

#: One entry, written the four ways an agent might write it. All four name the same host set: the
#: wildcard is accepted, case is not significant, and neither is a root dot.
SPELLINGS = ["kivid.com.br", "*.kivid.com.br", "KIVID.COM.BR", "kivid.com.br."]

#: Hosts, and whether ``kivid.com.br`` covers them. Read as the table both forms must agree on.
HOSTS = [
    ("kivid.com.br", True),  # the entry itself, not only what is under it
    ("api.kivid.com.br", True),
    ("api.stage.kivid.com.br", True),  # any depth, not just one label
    ("KIVID.COM.BR", True),
    ("kivid.com.br.", True),  # a root dot: reporting matches it, so interception must too
    ("notkivid.com.br", False),  # the entry as a bare suffix, with no label boundary
    ("notkivid.com.br.", False),  # the same near miss, carrying a root dot
    ("kivid.com.br.evil.test", False),  # the entry as a prefix of somebody else's domain
    ("kivid.com", False),
    ("com.br", False),
    ("kividxcomxbr", False),  # the entry's dots as wildcards, which is a missing re.escape
]

#: Two ports, because the pattern must key on a port's *shape* and never on a particular number.
PORTS = [443, 8080]


def engine_allows(patterns: tuple[str, ...], host: str, port: int) -> bool:
    """Whether mitmproxy would decrypt ``host`` on ``port``, given ``patterns`` as ``allow_hosts``.

    Mirrors `next_layer.py`: an unanchored, case-insensitive `re.search` over candidates of the
    form ``host:port``, allowed if any pattern matches any candidate.
    """
    return any(
        re.search(pattern, f"{host}:{port}", re.IGNORECASE) is not None for pattern in patterns
    )


# --- the equivalence, which is the module's reason to exist --------------------


@pytest.mark.parametrize("port", PORTS)
@pytest.mark.parametrize("entry", SPELLINGS)
@pytest.mark.parametrize(("host", "covered"), HOSTS)
def test_the_predicate_and_the_rendered_patterns_agree_on_every_host(host, covered, entry, port):
    """The whole point. Every spelling of the entry, every host in the table, both forms.

    A disagreement here is the drift this module exists to prevent: a Scope that reports a host's
    traffic while never decrypting it, or decrypts a host it then hides.
    """
    entries = (entry,)

    assert host_matches(host, entries) is covered
    assert engine_allows(allow_host_patterns(entries), host, port) is covered


def test_several_entries_are_a_union_in_both_forms():
    """One pattern per entry, and the engine's `any` over patterns has to mean the same as the
    predicate's `any` over entries — including for a host under the wrong one of them."""
    entries = ("kivid.com.br", "api.example.test")
    patterns = allow_host_patterns(entries)

    for host, covered in [
        ("api.kivid.com.br", True),
        ("api.example.test", True),
        ("other.example.test", False),
    ]:
        assert host_matches(host, entries) is covered
        assert engine_allows(patterns, host, 443) is covered


# --- the rendered form --------------------------------------------------------


def test_the_rendered_pattern_is_the_form_the_engine_matches():
    """Pinned, because every character was established against the real engine.

    Anchored at the port because the candidates are ``host:port`` and the search is unanchored; a
    label boundary in front, which is what keeps `notkivid.com.br` out; an optional root dot,
    mirroring the ``rstrip(".")`` the predicate applies to a host; and the entry escaped, because a
    dot in a host name is a literal.
    """
    assert allow_host_patterns(("kivid.com.br", "example.test")) == (
        r"(?:^|\.)kivid\.com\.br\.?:\d+$",
        r"(?:^|\.)example\.test\.?:\d+$",
    )


def test_every_spelling_of_one_entry_renders_the_same_pattern():
    """Stronger than equivalent verdicts, and cheaper to read: the same written pattern, so nobody
    has to compare two regexes by eye to believe the wildcard is accepted."""
    bare = allow_host_patterns(("kivid.com.br",))

    assert allow_host_patterns(("*.kivid.com.br",)) == bare
    assert allow_host_patterns(("KIVID.COM.BR",)) == bare
    assert allow_host_patterns(("kivid.com.br.",)) == bare


def test_no_entries_renders_no_patterns():
    """An empty `allow_hosts` is how mitmproxy is told to intercept everything, so the difference
    between "no Scope" and "a Scope naming no hosts" stays a decision for the caller — which is
    where it is already modelled — rather than something this renderer invents."""
    assert allow_host_patterns(()) == ()


# --- the reporting predicate, which now delegates ------------------------------


@pytest.mark.parametrize("entry", SPELLINGS)
@pytest.mark.parametrize(("host", "covered"), HOSTS)
def test_the_reporting_predicate_answers_exactly_what_the_rule_says(host, covered, entry):
    """The Scope's verdicts, unchanged. The table encodes what `in_scope` already did."""
    assert reporting.in_scope(host, (entry,)) is covered


def test_the_reporting_predicate_carries_no_copy_of_the_rule(monkeypatch):
    """In exactly one place is a structural claim, so assert it structurally.

    Neutralise the one definition and the reporting predicate has to go with it. A second copy
    living in `reporting.py` would keep answering correctly here, and nothing would notice until
    the day the two copies drifted — which is the whole failure being designed out.
    """
    monkeypatch.setattr(reporting, "host_matches", lambda host, entries: True)

    assert reporting.in_scope("notkivid.com.br", ("kivid.com.br",)) is True
