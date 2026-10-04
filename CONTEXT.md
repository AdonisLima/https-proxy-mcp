# HTTPS Proxy MCP

Gives an AI agent access to the HTTPS traffic of an application under test, and aligns that traffic with the steps of an end-to-end test so failures can be diagnosed by cause rather than by symptom.

## Language

### Capture

**Session**:
One capture lifetime — from the moment interception begins to the moment it ends. Owns every observed Exchange, including traffic belonging to no test.
_Avoid_: capture, recording, connection

**Exchange**:
One request together with its response, its timing, and the connection it travelled over. The unit of captured traffic.
_Avoid_: flow (mitmproxy's word for this, and Maestro's word for a test file — ambiguous in both directions), request (names the pair after one half), transaction, call

**Capture Target**:
The thing whose traffic is being intercepted, together with whatever must be arranged for interception to work — routing its egress and making it trust the interception authority. An iOS Simulator is one; a browser or a physical device would be others.
_Avoid_: device, platform, source

**Interception Authority**:
The certificate authority whose signature the Capture Target must trust in order for its encrypted traffic to be readable. Generated locally and unique to the machine.
_Avoid_: cert, CA cert, root (all ambiguous between the authority and the leaf certificates it signs)

**Scope**:
The set of hosts an agent is currently interested in. Narrows what is reported; narrows what is intercepted only under Passthrough.
_Avoid_: filter, allowlist, include list

**Passthrough**:
The condition of a TLS connection being forwarded without being decrypted, so the Capture Target sees the real certificate rather than one signed by the Interception Authority. Such a connection is invisible to this tool and unaffected by it — which is the point: the machine is proxied as a whole, so everything the agent has not asked about should reach its destination untouched. It applies to TLS alone. A plain HTTP request is intercepted whatever the Scope says, because the proxy must choose whether to open a connection before a plain HTTP request has named its destination; only a CONNECT authority and a TLS SNI arrive early enough to be tested. So a plain HTTP host outside the Scope is hidden from reporting rather than absent from the Session, which is a different answer to "why is there no traffic from that host?" and the reason both are stated.
_Avoid_: ignore (mitmproxy's word for the same option), bypass (the Mac's proxy settings have a bypass list of their own, which is a different mechanism at a different layer), tunnel, allowlist

**Lens**:
A standing predicate over Exchanges that narrows what a Session reports, written as a filter
expression and named once, when capture begins. Where a Scope narrows by host, a Lens narrows by
anything a request or a response says about itself — most usefully by something a client stamped
on its own requests, which is the only evidence able to separate two Capture Targets sending the
same traffic to the same hosts. It applies wherever a Scope does, and to the wait as well. It
cannot narrow a TLS handshake failure or a request still in flight, because neither of those is an
Exchange and there is nothing to test the expression against.
_Avoid_: filter (a Scope already lists that word as one to avoid, and a second use would make the
two unstateable apart), query, view (a Timeline is already the view over a Session), device filter
(it narrows by what a request states about itself, never by anything the proxy worked out)

**Human View**:
The mitmweb UI served by a Session's own proxy core, live on loopback behind a per-Session token.
The core's view, not the tool surface's: no denylist, Scope, Lens or Run window narrows it, and
bodies are unredacted. It is also a write console, and edits made in it can make a Timeline lie
and can break teardown.
_Avoid_: mitmweb (the library's name for the same UI run as its own process — a second process
would be its own empty core, which is the option ADR-0010 rejected), web UI, preview

### Testing

**Run**:
One execution of an end-to-end test, existing as a named window inside a Session. A Session may contain many Runs, and traffic may fall outside all of them.
_Avoid_: test, flow (Maestro's word for a test file), execution, suite

**Step**:
One action or assertion within a Run, such as a tap or a visibility check, with its own start, duration, and outcome.
_Avoid_: command (Maestro's word), action, instruction

**Event Source**:
The origin of a Run's Steps. Either a test tool's own artifacts, read after the fact, or explicit marks emitted while the Run is in progress.
_Avoid_: reporter, parser, adapter (too generic — this project has two adapter families)

### Correlation

**Timeline**:
The time-ordered interleaving of a Run's Steps with the Exchanges that occurred during it. A view over a Session, never a stored thing in its own right.
_Avoid_: trace, log, report

**Overlap**:
The condition of an Exchange still being in progress while a later Step is running. The fact that most often explains a failure, and the reason a Timeline cannot be a flat list.
_Avoid_: in-flight, concurrent, pending

**Background**:
Traffic that falls inside a Session but outside every Step of a Run — polling, telemetry, prefetching. Reported separately rather than attributed or discarded.
_Avoid_: noise, unmatched, orphan
