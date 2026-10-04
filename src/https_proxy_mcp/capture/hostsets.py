r"""What a host entry means, in one place, expressed in both of the forms that need it.

An entry names a host and everything under it: ``kivid.com.br`` covers ``api.kivid.com.br`` and
``kivid.com.br`` itself, and never ``notkivid.com.br`` — the boundary is a label, not a substring.
A leading ``*.`` is accepted and means the same thing, because agents write the wildcard whether or
not it is needed and it should mean what it looks like rather than silently matching nothing.

Two callers need that one rule in two different shapes. Reporting asks a boolean question about a
host it already has (:func:`host_matches`, which ``reporting.in_scope`` delegates to). Interception
has to hand the rule to mitmproxy *before* any host exists, as the regexes its ``allow_hosts``
option takes (:func:`allow_host_patterns`). Hence a module of its own rather than a sibling of
``in_scope``: two independent expressions of one rule is the trap ``targets/CLAUDE.md`` records as
costing a real debugging session — a reader that normalises can hide a writer that does not, and
the round-trip assertion passes the whole time. Both functions here start from :func:`_normalise`,
and a table of hosts asserts they agree on every row. Nothing outside this module writes a pattern
by hand.

It is not in ``reporting.py`` because that module's first sentence is what gets *reported* as
distinct from what was captured, which putting interception in it would make false.

The pattern form is ``(?:^|\.)<escaped entry>\.?:\d+$``, and every character of it is load-bearing:

* mitmproxy matches **unanchored** — ``re.search``, in ``mitmproxy/addons/next_layer.py`` — against
  candidate strings of the form ``host:port``, drawn from the peer name, the CONNECT authority, the
  ``Host`` header and the TLS SNI. So the pattern anchors at the port instead. Without ``:\d+$`` an
  entry would match its own name appearing anywhere inside a longer host.
* ``(?:^|\.)`` is the label boundary, and is the whole reason ``notkivid.com.br`` stays out.
* ``\.?`` admits **one** root dot, because a fully qualified name can reach the CONNECT authority or
  the ``Host`` header with its root dot attached, and reporting matches such a host. It is not the
  same rule as the ``rstrip(".")`` :func:`host_matches` applies, which removes a whole *run* of
  dots: ``host_matches("kivid.com.br..")`` is true where the pattern does not match
  ``kivid.com.br..:443``. Nothing produces a doubled root dot — no resolver emits one and no client
  copies one into a ``Host`` header — so the divergence is unreachable rather than harmless, and
  narrowing the predicate to match would change what *reporting* does, which is a separate decision.
  The table in ``tests/test_hostsets.py`` is therefore the guarantee, and it is a guarantee about
  the hosts that occur, not about every string.
* the entry is escaped, because a dot in a host name is a literal and ``re`` would otherwise read
  it as "any character" — which is what lets ``kividxcomxbr`` in.

Case is not normalised in the pattern, because mitmproxy matches with ``re.IGNORECASE``. The entry
is lowercased anyway, so both forms are built from one normalised string rather than from two rules
that happen to agree.
"""

from __future__ import annotations

import re


def _normalise(entry: str) -> str:
    """One entry reduced to the name it stands for.

    Lowercased, stripped of a root dot, and stripped of a leading ``*.``. Both public functions
    start here, and that shared line is the point of the module.
    """
    return entry.lower().rstrip(".").removeprefix("*.")


def host_matches(host: str, entries: tuple[str, ...]) -> bool:
    """Whether ``host`` is covered by any of ``entries`` — the entry itself, or anything under it.

    The host is normalised the same way an entry is, minus the wildcard: a name arriving with its
    root dot attached is the same name.
    """
    name = host.lower().rstrip(".")
    return any(name == entry or name.endswith(f".{entry}") for entry in map(_normalise, entries))


def allow_host_patterns(entries: tuple[str, ...]) -> tuple[str, ...]:
    """``entries`` as the regexes mitmproxy's ``allow_hosts`` option takes — one per entry.

    The same host set as :func:`host_matches` decides, written for an engine that has to choose
    before it has a host to ask about. See this module's docstring for what each part of the
    pattern is doing; no caller should be assembling one of these itself.

    An empty ``entries`` renders no patterns. Deciding what *that* means is the caller's — an empty
    ``allow_hosts`` is how mitmproxy is told to intercept everything, which is not what an empty
    Scope means, and the difference is already modelled where Sessions are started.
    """
    return tuple(rf"(?:^|\.){re.escape(_normalise(entry))}\.?:\d+$" for entry in entries)
