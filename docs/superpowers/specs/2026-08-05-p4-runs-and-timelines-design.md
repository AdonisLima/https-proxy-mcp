# P4 — Runs and Timelines: design

The phase that delivers the reason the project exists. Everything before it is plumbing:
P1–P3 can show an agent the traffic, but not *why the test failed*.

Spec: [SPEC.md](../../SPEC.md) · Plan: [PLAN.md](../../PLAN.md) · Vocabulary:
[CONTEXT.md](../../../CONTEXT.md) · Decisions: [docs/adr/](../../adr/)

## What P4 must deliver

Six tools — `run_test`, `list_runs`, `get_timeline`, `start_step`, `end_step`, `export_har` —
a Maestro Event Source, and a Correlator implementing owner-plus-overlap (ADR-0004).

**Acceptance:** a deliberately failing Maestro flow produces a Timeline showing the failing
assertion *and* the Exchange still in flight when it began.

## What the probes found

Four things were established by running Maestro 2.6.1 against a throwaway simulator before
any of this was designed. Two of them change the design, and one of them would have produced
a wrong Timeline that looked like a correlation bug.

1. **`commands-*.json` is not in execution order.** The array from the failing probe flow was
   `[seq 3, seq 0, seq 1, seq 2]` — the FAILED command came first. The *passing* flow was also
   out of order (`1, 2, 0`), so this is not a failure-ordering quirk. **Steps must be ordered by
   `metadata.sequenceNumber`, never by array position.** HANDOFF.md predicted this class of bug:
   "overlap arithmetic off by one interval looks like a correlation bug and is usually an
   ordering assumption".

2. **One failing command carried 33 KB of `error.hierarchyRoot`**, making a two-command flow's
   artifact 123 KB. The entire device UI hierarchy is embedded in every failure. A parser that
   passes the error object through would blow the context window on a single failing Step —
   the exact risk PLAN.md lists as "context blowout". **The parser keeps `error.message` and
   discards `hierarchyRoot`, `stackTrace`, `debugMessage` and `localizedMessage` at the parse
   boundary**, so no downstream code can leak them by accident.

3. **`--test-output-dir` does not contain the debug output.** Its own `--help` says so, and the
   probe confirmed it: the directory was left empty while `commands-*.json` went to
   `--debug-output`. ADR-0003's instruction to point both at one directory stands as a
   *tidiness* measure, but the Timeline depends only on `--debug-output`.

4. **Layout differs by flag.** With `--flatten-debug-output` the file is
   `<dir>/commands-(flow.yaml).json`; without it, `<dir>/.maestro/tests/<timestamp>/commands-*.json`
   — a **hidden** directory plus a timestamped one. Filenames embed the flow name in literal
   parentheses, not a timestamp. So the parser searches **recursively** and the spawner injects
   `--flatten-debug-output` for a deterministic path. Belt and braces: robust to a caller who
   supplies their own flags.

Also confirmed: `metadata.timestamp` is epoch **milliseconds** and marks the command's
**start**; `metadata.duration` is milliseconds; `metadata.status` is `COMPLETED` / `FAILED`;
Maestro exits **1** on a failing flow and **0** on a passing one.

## Three decisions taken before planning

### In-flight Exchanges get their own registry, separate from Exchange ids

The store records on completion, so a request that never comes back is not in it. The
acceptance criterion needs "still in flight", so this must be built.

The tempting fix — assign ids at request time and fill in `t_end` later — was rejected. It
inverts a decision already made and documented ("Exchange ids are ordered by **completion**,
not by start"), and it breaks `wait_for_exchanges`: the wait would return before a status code
existed, so *"tap Login, then wait for `POST /auth`, then check it returned 401"* would stop
working. That tool is what makes end-to-end use practical.

Instead the addon's `request` hook opens a **`PendingExchange`** keyed by mitmproxy's `flow.id`,
and completion removes it. Ids stay completion-ordered; `list_exchanges`, `tail_exchanges`,
`search_exchanges` and `wait_for_exchanges` iterate retained Exchanges and are therefore
**completely unchanged**. Pending entries are capped and counted, like TLS failures — a hung
endpoint hit in a loop must not grow without bound.

Pending entries deliberately **survive `close()`**: "three requests were still open when the
Session ended" is information, not garbage.

### A long-lived connection is listed once, never repeated as an Overlap

Open question 2. A WebSocket overlaps every Step by nature, so the overlap model degrades into
"overlaps everything" and every Step's row fills with the same connection.

Rather than a duration threshold with a magic number, the rule keys on what the protocol
actually says: an Exchange is **long-lived** when its status is `101` (Switching Protocols — the
upgrade handshake) or its response is `text/event-stream` (SSE). Both are exact signals; neither
needs a tunable. A long-lived Exchange is owned by the Step it started under, listed there once
and marked, and **excluded from every later Step's overlap list**. It is never hidden — a
Timeline that omits a connection reads as "there was no traffic", which is the thing this
project most wants not to say.

### The live test uses a throwaway simulator and a hanging sink

The criterion says "reproduced for real". The live test creates and deletes its own simulator —
never a device the developer works in — and drives `com.apple.mobilesafari` through a flow whose
final assertion cannot pass.

Getting a *deterministically* in-flight Exchange needs one more piece. A local **sink** accepts
the TCP connection and never replies. mitmproxy reads the client's request fully (firing the
`request` hook, so the Exchange is registered as pending) and then hangs waiting upstream. The
assertion retries for ~17 seconds, so the Exchange is provably still in flight when it began,
rather than depending on a race between page load and assertion start.

Gated behind `HTTPS_PROXY_MCP_E2E=1`, for the reason P3 recorded: a `-m` marker expression on the
command line *replaces* the default from `addopts`, so a marker could be silently re-enabled by
`pytest -m "not integration"`.

## Architecture

Two new packages, and the split is the same one that already keeps `serialize/views.py`
testable: **the Correlator does no I/O**. It takes Steps, Exchanges and a window, and returns a
Timeline. Everything that touches a disk or a process lives outside it.

```
runs/                        # a Run's identity, directory and process lifetime
├── registry.py              # Run, RunRegistry — runs/<run-id>/run.json under the Session
└── process.py               # async spawn, Maestro flag injection, timeout, output capture

correlate/                   # pure: no filesystem, no subprocess, no mitmproxy
├── steps.py                 # Step — the whole contract the Correlator depends on
└── timeline.py              # owner-plus-overlap arithmetic, Timeline assembly

eventsources/
├── base.py                  # EventSource protocol
├── explicit.py              # StepLog — start_step / end_step, the universal source
└── maestro.py               # commands-*.json → Step, ordered by sequenceNumber

serialize/har.py             # HAR 1.2, redacted by default
server/run_tools.py          # the six tools
```

`Step` stays exactly what SPEC.md promised — `{t_start, duration_ms, label, status, error?}`.
Nothing in it is Maestro-shaped, which is what makes the explicit source and the Maestro source
peers rather than one being a special case of the other.

### The Correlator's rules, stated precisely

- **Ownership.** An Exchange is owned by the Step that was active when it started. When it
  starts in a *gap* between Steps — Maestro's inter-command overhead is milliseconds wide, and a
  tap's request routinely lands just after the tap's own metadata window closed — it is owned by
  the Step that had just finished, because that is the Step that caused it. Without this, the
  Timeline's primary sentence ("the tap made this request") would fall into Background.
- **Overlap.** Every *other* Step whose interval intersects the Exchange's is recorded as an
  Overlap. Long-lived Exchanges are exempt, per the decision above.
- **An unterminated Exchange** (`t_end is None`, from the error hook) and a **pending** one are
  treated as extending to the end of the window, because that is what "never came back" means.
- **Background.** An Exchange inside the Run window but before its first Step is Background.
  Reported separately, never dropped.
- **Zero-duration Steps** are points, and a point can still own an Exchange starting at exactly
  that instant. Tested explicitly, since this is where off-by-one-interval bugs live.

### The Run window becomes the first narrowing

SPEC.md orders the narrowings: Run window, then denylist, then Scope. `ReportingFilter` already
owns the last two, so the window joins it there as a `window` field rather than being
reimplemented in the Timeline code — HANDOFF.md's instruction. `allows_host()` keeps its
host-only signature, because it is also what judges TLS handshake failures, which have no
interval. Default `window=None` means every existing caller is unaffected.

`get_timeline` applies `state.reporting()` deliberately and reports `hidden`, because it does its
own assembly rather than going through `_page()`.

## Tool surface

Six tools, taking the registered set from fourteen to twenty. `tests/test_server.py` asserts the
exact set, so this is a deliberate edit to `REGISTERED_TOOLS` — which is the point of that
assertion.

| Tool | Notes |
|---|---|
| `run_test` | Spawns the command; the Run is exactly spawn-to-exit (ADR-0003). Injects Maestro's flags when the command is Maestro and the caller has not set them. `env` is merged over the environment, not substituted — replacing it would drop `PATH` and break the very tool being wrapped. Returns the exit code, timing and a **tail** of output; the full streams go to files in the Run directory, because Maestro's output is long and returning it would flood the context. |
| `list_runs` | Ordered, with label, exit code and Step counts. |
| `get_timeline` | `verbosity="summary"` by default (ADR-0004 makes Timelines interval-shaped, which costs tokens). `run_id=None` means the whole Session, so a human marking Steps by hand never needs `run_test`. `host_filter` matches by suffix, like a Scope. |
| `start_step` / `end_step` | The universal Event Source. `start_step` with one already open is an **error**, not an implicit close: a Step's status is the entire point, and closing one silently would record an outcome nobody stated. |
| `export_har` | HAR 1.2, redacted by default because the artifact travels. Returns the **path and a count**, never the HAR itself. |

A Run's Steps are the union of artifact-derived Steps and explicit Steps inside its window, with
the Event Source chosen by what is actually in the artifacts directory. No parameter to get
wrong.

## Testing

- **Unit** — overlap arithmetic (gap ownership, zero-duration Steps, unterminated Exchanges,
  long-lived exemption), `sequenceNumber` ordering, HAR shape, redaction at the export boundary.
- **Parse against real data** — `tests/fixtures/maestro-commands-failing-assert.json` is the
  actual 123 KB artifact from the probe, hierarchy and all. A test asserts the parser drops
  `hierarchyRoot` and keeps `error.message`, so the context-blowout guard is structural rather
  than remembered.
- **Golden-file Timelines** — PLAN.md's list: an overlap case, a Background-only case, a Step
  with no Exchanges. Plus the SPEC.md worked example reproduced exactly, which is the criterion's
  logic proved deterministically.
- **Integration** — `run_test` against the `fake_commands.py` helpers already in the suite;
  timeout, non-zero exit, and a command that writes no artifacts at all.
- **Live, gated** — the real failing Maestro flow described above.

Every unit is built test-first with the RED step observed, and acceptance tests are
teeth-checked by deliberately breaking the code, per the method HANDOFF.md records.

## Deliberately not in P4

- **`json_path` on `get_exchange_body`** — still deferred; nothing wants it yet.
- **WebSocket message capture.** The upgrade Exchange is recorded and classed long-lived; the
  individual frames are not captured. That is a v1.1 question, and open question 2 asked how
  streams should *appear* in an interval model, which the long-lived rule answers.
- **`--mode local` process attribution**, which would let `diagnose_capture` report pinning
  without a Scope. Unchanged from P3.
