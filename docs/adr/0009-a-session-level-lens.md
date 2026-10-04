---
status: accepted
---

# Reporting gains a fourth narrowing, the Lens, and it is the one thing a wait obeys

`start_capture` gains `lens: str | None = None`, a filter expression in the language
`search_exchanges` already speaks. It is held on the Session, compiled once, and carried by
`ReportingFilter` alongside the Run window, the builtin denylist and the Scope — so every listing
and every Timeline inherits it without asking. `wait_for_exchanges` obeys it too, though it is
exempt from the other three. `get_exchange` remains exempt from all four.

The gap it closes was reported from real use, and no existing narrowing can close it. Two iOS
Simulators booted on one Mac, running the same build of the same app against the same backend:
CoreSimulator has no proxy setting of its own, so both are routed through the one capture proxy; an
Exchange records the address it was going *to* and never the one it came *from*
(`capture/exchange.py`); and a Timeline attributes by time. The other simulator's requests
therefore land inside a Run's window and are reported as traffic that Run's Steps caused — a
confident causal claim about a request nobody in that Run made. A Scope cannot help, because a
Scope narrows hosts and the hosts are identical. Neither can Passthrough, for the same reason.

What can help is evidence the proxy did not have to deduce: a debug build that stamps a header on
its own requests states which client sent them. `~hq "X-Sim: sidekick"` then separates what
nothing at the proxy could.

## Considered options

- **A `filter` parameter on `get_timeline`, matching the one `list_exchanges` already takes.** The
  obvious change, and the first one chosen. Rejected on the grounds that it is opt-in: a narrowing
  an agent has to remember on every call is the one it forgets on the call that mattered, and the
  failure is silent — a Timeline with the other simulator's traffic in it looks exactly like a
  Timeline. The three existing narrowings are all Session- or Run-level for the same reason, and a
  fourth that worked differently would be the asymmetry, not the consistency.
- **Attribute the client, rather than have the client attribute itself.** Feasible, and traced
  before it was rejected: `client_conn.peername` gives the ephemeral port, `lsof` maps it to a pid,
  and each booted device has a `launchd_sim` whose command line carries its UDID. It fixes only
  half the problem — the other simulator still fails its handshakes unless the Interception
  Authority is installed there too — and costs an `lsof` per connection, a race against a socket
  that must still be open, a new persisted field in a format written out by hand precisely so
  additions are not silent, and a name in `CONTEXT.md` for traffic originating somewhere that is
  not this Session's Capture Target. It is recorded in `docs/v1.1-candidates.md` with the chain
  written down, so it is not rediscovered.
- **Reuse the word Scope, or the word filter.** Rejected in both directions. Scope's own glossary
  entry lists *filter* as a word to avoid, so spending it here would leave the two unstateable
  apart; and widening Scope to mean "hosts, or anything else" would make `reporting.py`'s subject
  — what is reported as distinct from what was captured — unsayable, in the same way ADR-0007
  declined to fold Passthrough into it.
- **Let `wait_for_exchanges` stay exempt, consistently with the Scope and the denylist.** This is
  the decision most likely to look like an inconsistency, and it is the reverse. The exemption's
  reasoning is that a filter naming a host is *explicit*, and an explicit question should not be
  vetoed by a narrowing set earlier. A Lens is not about the question; it is about which client the
  traffic came from. An exempt wait is satisfied by the other simulator's login — which is exactly
  the defect the "only traffic recorded after this call counts" rule exists to prevent, arriving by
  a different route. So the exemption is kept where its reasoning holds and reversed where it does
  not, and `state.lens_predicate` exists to hand the Lens over without the rest.
- **Keep `get_exchange` obeying it too, for uniformity.** Rejected: an id came from somewhere. The
  argument that made the two tools exempt together still applies to this one, and a Lens that could
  veto a fetch by id would make an Exchange id stop meaning anything.
- **Hold the compiled predicate in `reporting.py`.** Rejected: compiling one needs mitmproxy's
  filter language and a way to read bodies off disk, and that module is deliberately free of the
  engine. `ReportingFilter` takes a `Callable[[Exchange], bool]` instead, and the compiling happens
  where the Session starts.
- **Compile it lazily, at the first listing.** Rejected because of when the failure would land: by
  then the machine's proxy has been redirected and the Interception Authority installed into a
  device. It is compiled before the target is even built, closing over a store that does not exist
  yet — nothing calls it until a listing does, by which time one does.

## Consequences

**A Lens narrows nothing that is not an Exchange.** A TLS handshake failure never became one, and a
pending request has not yet, so neither can be tested against an expression. The `tls_failed` count
stays exactly as machine-wide as the verdict already says it is, and `ReportingFilter.allows_host`
is deliberately untouched. That is a real limit, not an oversight: the case a Lens exists for is
also the case that inflates that count, and a Lens cannot deflate it.

**Quoting is load-bearing and its failure is silent.** `~hq X-Sim: sidekick` *parses* — so it is
accepted — and then matches neither a request carrying the header nor one that does not, hiding the
whole Session. `~hq "X-Sim: sidekick"` is the form that works. Measured both ways and recorded in
`capture/CLAUDE.md`; every header-matching example this project writes carries the quotes.

**Polarity is the caller's decision and both directions cost something.** A Lens that names the
Capture Target excludes anything unheadered, including traffic from an SDK that does not use the
app's own HTTP client; one that excludes the *other* simulator keeps it. The first risks losing the
cause, the second risks importing one. A tight Scope shrinks the difference to nearly nothing,
since unheadered third-party traffic usually goes to a host the Scope never named.

**This decision is only half the fix, and the other half has its own cost.** A Lens stops the other
simulator polluting a diagnosis; it does nothing about that simulator's HTTPS failing, because
interception happens before reporting and the Authority is installed into one device. Installing it
into both keeps that simulator working and puts its bodies, unredacted, into a Session directory it
is not the Capture Target of. Nothing here can look at a body and know what is in it. What it can
do is count trust stores, so `start_capture` and `diagnose_capture` now report `devices_booted` and
`devices_trusting_authority`, and a `devices_note` that is null unless a booted device other than
the Capture Target trusts the Authority. Counts, never contents — a warning that guessed would be
the mistake the `tls_failed` verdict was corrected for.

`SPEC.md` says reporting is narrowed three ways and its `start_capture` row has no `lens`. Both are
now untrue of the code and both stay: it is a record of intent. The divergence is listed in
`.claude/agents/doc-drift-reviewer.md`, without which every future documentation review reports it
as a fresh finding.
