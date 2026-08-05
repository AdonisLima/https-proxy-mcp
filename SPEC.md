# https-proxy-mcp — Specification (v1)

An MCP server that captures the HTTPS traffic of an app under test and interleaves it with end-to-end test steps, so an agent can diagnose a failure by cause rather than by symptom.

Domain vocabulary is defined in [CONTEXT.md](./CONTEXT.md) and used precisely throughout. Architectural decisions and their rationale live in [docs/adr/](./docs/adr/).

## The problem

A failing E2E assertion tells you the UI was wrong. It does not tell you that `GET /me` returned 401 four hundred milliseconds earlier, or that the request the screen was waiting on had not yet come back. An agent driving a mobile app can see screenshots and view hierarchies; it is blind to the network layer where the cause almost always lives.

## Scope of v1

**In:** iOS Simulator capture, read-only traffic access, Run-scoped Timelines with Maestro as the first Event Source, and explicit Steps for any other test tool.

**Out:** Android Emulator (v1.1 — its system-CA procedure is the most brittle part of the domain and must not gate the first release), write/interception tools (v1.1, behind a flag), physical devices, browsers, and production traffic monitoring of any kind.

## Architecture

The core knows nothing about iOS or about Maestro. Both sit behind adapters, so a second platform or a second test tool is an addition rather than a change.

```
┌─ CORE ───────────────────────────────────────────────────┐
│  CaptureAddon ──▶ ExchangeStore ──▶ Serializer           │
│                   metadata in memory  redact/truncate/   │
│                   bodies on disk      paginate           │
│                                                          │
│  Correlator: overlap(Steps, Exchanges) ──▶ Timeline      │
│                                                          │
│  MCP server (stdio)                                      │
└──────────────────────────────────────────────────────────┘
        ▲                              ▲
   CaptureTarget                  EventSource
   ios-simulator (v1)             maestro (v1)
   android-emulator, browser      explicit Steps (v1)
   physical device                detox, appium, playwright
```

### Adapter contracts

```python
class CaptureTarget(Protocol):
    def prepare(self) -> None: ...
    def ensure_trusted(self) -> TrustStatus: ...
    def route_traffic(self, port: int) -> None: ...
    def diagnose(self) -> Diagnosis: ...
    def teardown(self) -> None: ...

class EventSource(Protocol):
    def steps(self, run: Run) -> list[Step]: ...
```

`Step` is the entire contract the Correlator depends on: `{t_start, duration_ms, label, status, error?}`. Nothing in it is Maestro-shaped.

## Semantics

### Run boundaries come from process lifetime

`run_test` spawns the test command; the Run is exactly the interval from spawn to exit. No inference, no matching heuristic.

This has a consequence worth stating plainly: **clock skew does not apply to this design.** The Maestro CLI runs on the host, and so does the proxy, so Step timestamps and Exchange timestamps come from the same clock. Device time never enters the join. (The KB that informed this project ranked skew as its second-highest risk; that ranking assumed device-sourced events.)

Because the MCP builds the command line, it also owns Maestro's output flags and points `--debug-output` and `--test-output-dir` at the **same** directory — which structurally eliminates the documented footgun where splitting them silently strands the `commands-*.json` the Timeline depends on.

### Attribution is by overlap, not assignment

Steps and Exchanges are both intervals. An Exchange is *owned* by the Step that was active when it started — that Step caused it — but every later Step it overlaps is recorded as an Overlap.

This is the whole point of the model. Given:

```
Step  1  tapOn: Login          [0 ────── 80]
Step  2  assertVisible: Home            [80 ─────────────────── 5000] FAILED
Exch  A  POST /auth            [12 ──────────── 912]
```

Exchange A is owned by Step 1 and overlaps Step 2. A flat one-Step-per-Exchange model would report either "the tap made a request" or "the assert made a request", and both lose the sentence that actually explains the failure: *the assertion started while the request it depended on was still in flight.*

Exchanges matching no Step are reported as Background, never dropped.

### Progressive disclosure is enforced by the API shape

`list_exchanges` cannot return bodies — there is no parameter that would make it. Bodies require a second call, and large ones require an offset. This is a deliberate constraint: the failure mode for a traffic tool driven by an agent is flooding the context window, and the API should make that impossible rather than merely discouraged.

| Default | Value |
|---|---|
| `list_exchanges` page size | 50 (max 200) |
| Body preview in `get_exchange` | 2048 bytes, with an explicit truncation marker |
| `get_exchange_body` slice | 64 KB per call |
| `wait_for_exchanges` timeout | 15 s (max 120 s) |
| Metadata ring buffer | 5000 Exchanges, oldest evicted, eviction count always reported |
| Body spill cap | 10 MB per side; beyond that, truncated and marked |
| Session retention | 10 most recent, older pruned on start |

### Redaction happens where data leaves

Redaction applies at two boundaries: **into the model**, and **out of the machine**. The Session directory on local disk holds faithful bodies, because "is my app sending the right token?" is a real debugging question and a scrubbed store cannot answer it.

Redacted by default:

- Headers: `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key`, `X-Auth-Token`
- JSON body keys matching `(token|secret|password|api[-_]?key|refresh|access_token|id_token)`
- JWT-shaped values (`eyJ…`)

Each redacted value is replaced with `<redacted:sha256:xxxxxxxx>` — a stable short digest, so an agent can tell *"this is the same token as the previous request"* or *"this token changed after refresh"* without ever seeing it. That distinction is usually the whole question.

`include_secrets: true` on any call opts out. `export_har` redacts by default because the artifact travels.

### Noise is filtered by time first, host second

Capture is unconditional — everything through the proxy is recorded. Reporting is narrowed by, in order:

1. **The Run window.** Anything outside it cannot pollute that Run's Timeline.
2. **A builtin denylist** of OS and browser chatter: Apple/iCloud domains, OCSP and CRL endpoints, `gstatic`, crash and telemetry ingest endpoints. Deliberately conservative — `googleapis.com` is *not* denied, because for many apps that is the backend.
3. **An optional `Scope`**, if the agent sets one.

This matters because of a property of the chosen capture path: in regular proxy mode every connection arrives from localhost, so there is **no process attribution**. Simulator traffic and Chrome traffic are indistinguishable at the proxy. Time-and-host filtering is not a convenience here, it is the only available mechanism.

### Teardown must survive being killed

While capturing, the Mac's system proxy is redirected. Leaving it that way breaks the machine's networking, so restoration is a three-layer guarantee:

1. Prior proxy state is written to a sentinel file (`0600`) **before** anything is changed.
2. `atexit` plus `SIGTERM`/`SIGINT` handlers restore on any orderly exit.
3. Every server start checks for a stale sentinel and restores it **before** doing anything else.

Layer 3 is what covers `kill -9`, a panic, and a power cut. Layers 1 and 2 alone would not.

## Tool surface (v1, read-only)

### Session lifecycle

| Tool | Parameters | Returns |
|---|---|---|
| `start_capture` | `target="ios-simulator"`, `port=8080`, `udid=None`, `scope=None` | session id, port, target detail, trust status |
| `stop_capture` | — | Exchange count, Session directory path |
| `get_status` | — | recording state, port, target, Session id, Exchange and eviction counts, Scope, proxy state, trust status |
| `diagnose_capture` | — | one of `ok` / `no_connect` / `tls_failed` / `untrusted_authority` / `out_of_scope`, plus remediation |

`diagnose_capture` is the highest-value tool in the set and is absent from all surveyed prior art. Three very different causes present identically as "no traffic": nothing reached the proxy, TLS failed because the authority is untrusted, or the Scope excluded it. Distinguishing them is a 40-minute human debugging session collapsed into one call.

### Trust

| Tool | Parameters | Notes |
|---|---|---|
| `install_authority` | `target="ios-simulator"`, `udid=None` | Called automatically by `start_capture` for simulators |
| `install_authority_system` | — | macOS system keychain. **Never implicit** — requires `sudo` and is machine-wide |
| `get_trust_status` | `udid=None` | — |

The asymmetry is deliberate: a simulator trust store is per-device, disposable, needs no privilege, and is wiped by `simctl erase`, so installing there is inherent to the request. The system keychain is none of those things.

### Exchanges

| Tool | Parameters |
|---|---|
| `list_exchanges` | `limit=50`, `cursor`, `host`, `method`, `status`, `since`, `until`, `run`, `include_background=False` |
| `get_exchange` | `exchange_id`, `max_body_bytes=2048` |
| `get_exchange_body` | `exchange_id`, `side`, `offset=0`, `length=65536`, `json_path=None` |
| `search_exchanges` | `filter` (mitmproxy filter expression), `limit=50`, `cursor` |
| `tail_exchanges` | `cursor=None`, `filter=None`, `limit=50` |
| `wait_for_exchanges` | `filter`, `timeout_ms=15000`, `min_count=1` |
| `export_curl` | `exchange_id` |

`search_exchanges` exposes mitmproxy's own filter expression language rather than inventing a query syntax — it is documented, already tested, and already what the capture layer consumes.

`wait_for_exchanges` is what makes E2E usable: *"tap Login, then wait for `POST /auth`"* is one call, not a polling loop. Combined with `tail_exchanges` it covers live observation without any push mechanism. Push is deliberately not attempted — see [ADR-0005](./docs/adr/0005-pull-based-live-access.md).

### Runs and Timelines

| Tool | Parameters |
|---|---|
| `run_test` | `command`, `label=None`, `cwd=None`, `env=None`, `timeout_ms=None` |
| `list_runs` | — |
| `get_timeline` | `run_id`, `verbosity="summary"`, `host_filter=None`, `include_background=False` |
| `start_step` / `end_step` | `label` / `status="passed"`, `error=None` |
| `export_har` | `run_id=None`, `redact=True`, `path=None` |

`start_step` / `end_step` are the universal Event Source. Any test tool that can shell out — or a human exploring by hand — can mark Steps, with no artifact parsing and no adapter. Maestro's artifact parser is the *enrichment* path, not the foundation.

## Session directory

```
~/.https-proxy-mcp/
├── proxy-state.json               # sentinel, 0600, prior system proxy state
└── sessions/<session-id>/         # 0700
    ├── session.json               # id, started, target, port, scope
    ├── exchanges.ndjson           # metadata append log (durability + tailable)
    ├── bodies/<exchange-id>.{req,resp}
    ├── runs/<run-id>/
    │   ├── run.json               # label, command, start, end, exit code
    │   └── artifacts/             # test tool output; Maestro's two flags both point here
    └── exports/
```

The directory holds unredacted bodies and is therefore `0700` and documented as sensitive.

## Runtime

Python, single process — the MCP server and the mitmproxy addon share one process and one `ExchangeStore`, so the addon holds native flow objects with no IPC and no file round-trip for live queries. See [ADR-0001](./docs/adr/0001-python-monolith.md).

- Python ≥ 3.12, managed by `uv` (system Python is 3.9.6 and too old). Verify the floor against the resolved mitmproxy version at scaffold time.
- `mitmproxy` as a library dependency, not an external binary.
- Distributed for `uvx https-proxy-mcp`; stdio transport only in v1.
- Ships a `SKILL.md`: choose a target, run `diagnose_capture` before believing an empty result, prefer `wait_for_exchanges` over polling, drill down rather than dumping.
