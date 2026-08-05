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
The set of hosts an agent is currently interested in. Narrows what is reported, never what is captured.
_Avoid_: filter, allowlist, include list

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
