# Handoff — 2026-08-05

Picking this up in a fresh session. Everything needed is in the repo; this file says where to start and what not to relitigate.

## State

**P0, P1 and P2 complete and committed.** On `main`, clean tree, no remote configured.

Verify in one command: `uv run pytest -q && uv run ruff check .` → 318 passed, all checks passed.

53 tests are marked `integration` — they bind real sockets, spawn `mitmdump`, `curl`, and a real MCP server subprocess. `uv run pytest -m "not integration"` runs the other 265 in about two seconds.

Try it by hand:

```bash
uv run https-proxy-mcp          # speaks MCP over stdio; start_capture, then curl -x
```

## Read these first, in this order

1. **[CONTEXT.md](./CONTEXT.md)** — the domain language. Use these words exactly; they were chosen to dodge a real collision (mitmproxy's "flow" vs Maestro's "flow"). If you find yourself writing "flow", stop. The single exception is `capture/addon.py` and `capture/filtering.py`, where `flow` names mitmproxy's own object at the boundary.
2. **[SPEC.md](./SPEC.md)** — the v1 spec. Tool surface, semantics, numeric defaults.
3. **[PLAN.md](./PLAN.md)** — six phases with acceptance criteria. You are starting P3.
4. **[docs/adr/](./docs/adr/)** — five decisions and why. Two deviate from the research that preceded the project, so read them before "fixing" anything that looks wrong.

## What exists now

| Module | Contents |
|---|---|
| `capture/exchange.py` | `CapturedExchange` (what the addon saw) and `Exchange` (what the store kept), `BodyRef`, NDJSON round-trip |
| `capture/store.py` | `ExchangeStore` — ring buffer, eviction counter, body spill and read-back, append-only log, `0700` |
| `capture/addon.py` | `CaptureAddon` — the only module that knows mitmproxy's flow shapes |
| `capture/session.py` | Session identity, layout, `$HTTPS_PROXY_MCP_ROOT`, retention to 10 |
| `capture/runtime.py` | `ProxyRuntime` — mitmproxy in this process on the server's event loop |
| `capture/filtering.py` | mitmproxy filter expressions applied to Exchanges |
| `serialize/redact.py` | Headers, JSON keys, form keys and JWTs → `<redacted:sha256:xxxxxxxx>` |
| `serialize/views.py` | Summaries, previews, slices, pagination, curl rendering |
| `targets/` | `CaptureTarget` protocol, `TrustStatus`, `Diagnosis`, and `CurlTarget` |
| `server/` | `CaptureState` plus the ten registered tools |

## Next: P3 — iOS Simulator target

The riskiest phase, and the one PLAN.md is most emphatic about:

- `networksetup` routing with the sentinel protocol (write prior state → `atexit` and signal handlers → repair-on-start).
- `simctl keychain add-root-cert` for trust, auto-invoked by `start_capture`.
- `install_authority_system` as a separate explicit tool — never implicit, since it needs `sudo` and is machine-wide.
- `diagnose_capture` distinguishing `no_connect` / `tls_failed` / `untrusted_authority` / `out_of_scope`.
- Builtin denylist of OS chatter.

**Acceptance:** capturing a real simulator app's HTTPS traffic end to end — and, tested explicitly, `kill -9` on the server followed by a restart leaves the system proxy correctly restored.

That last check is the one that matters most. Test it deliberately rather than trusting the handlers; PLAN.md's risk table rates "server dies without restoring the system proxy" as the only **High** severity item that breaks the machine rather than the tool.

What P2 already laid down for you:

- **The `CaptureTarget` protocol is settled and has a working implementation.** `targets/base.py` defines `prepare` / `ensure_trusted` / `route_traffic` / `diagnose` / `teardown`, and `CurlTarget` shows the shape. Register the simulator in `_TARGETS` in `targets/__init__.py` and `start_capture` accepts it with no other change — its tool docstring lists available targets from the registry, so that stays true by itself.
- **`Diagnosis` already carries the five statuses and a remediation field.** `diagnose_capture` is a tool wrapping `target.diagnose()`, not a new mechanism. Note the one thing a target cannot see: whether traffic is arriving. `CurlTarget.diagnose` is deliberately honest about that; the simulator's will need the store's Exchange count to tell `out_of_scope` from `no_connect`, so plan to pass it what it needs.
- **`teardown` is already specified as idempotent and safe after a failed start.** `CaptureState.start` calls it on any failure path, so the sentinel protocol must tolerate restoring state that was never changed.

## Decisions already made — do not reopen

The full list with rationale is in the ADRs and SPEC. The ones most likely to get second-guessed:

| Decision | Why it looks odd |
|---|---|
| Python, not TypeScript | Both siblings in `common/` are TS. ADR-0001. |
| Regular proxy mode, not `--mode local` | The research recommended local mode. ADR-0002 reverses it. |
| The MCP spawns your test command | Unusual for an MCP server. ADR-0003 — it buys exact Run boundaries and removes clock skew entirely. |
| Overlap attribution, not one-Step-per-Exchange | Looks over-engineered until you hit the failing-assertion case. ADR-0004. |
| No write/interception tools in v1 | Prior art all has them. Deliberate; v1.1 behind a flag. |
| Raw bodies on local disk, redacted at the model and export boundaries | Looks inconsistent until you need to answer "is my app sending the right token?". |

Added by P1 and P2:

| Decision | Why it looks odd |
|---|---|
| Exchange ids are ordered by **completion**, not by start | A slow request that started first can carry a higher id. That is the right ordering for cursor-based tailing ("what finished since I last looked"); time questions use `t_start` explicitly and never lean on id order. |
| The whole `Authorization` value is digested, scheme included | `Bearer` is not preserved. Partial redaction of an unknown scheme is how partial leaks happen. |
| `ExchangeStore` has no lock | It assumes the addon and the tool surface share one event loop, which is what ADR-0001's single process buys. Moving the proxy to its own thread means adding a lock here, deliberately. |
| Bodies are stored **decoded**, not as they were on the wire | A gzipped blob on disk answers no debugging question. `get_content(strict=False)` so one malformed `Content-Encoding` cannot end the Session. |
| Redaction runs **before** truncation and slicing | Cutting first leaves the tail of a straddling secret visible. So `get_exchange_body` offsets index the *redacted* body, and `total` is its length — offsets are into whatever representation you asked for. |
| A body with nothing sensitive comes back as its **original bytes** | Re-serializing unchanged JSON would silently reformat every body a model sees, so it would no longer match what the app sent. |
| A binary body is **described** in a preview, not base64'd | 2 KB of base64 is noise nobody can read. `get_exchange_body` still returns the bytes when asked. |
| `export_curl` redacts by default, so the default **does not run** | Consistent with every other boundary. Reproducing a request is an explicit `include_secrets` opt-in. |
| `wait_for_exchanges` counts only traffic recorded **after** the call | Otherwise "tap Login, then wait for `POST /auth`" is satisfied by the previous login, which makes the tool useless. |
| Exchanges are rebuilt into throwaway mitmproxy flows to be filtered | SPEC.md chose mitmproxy's filter language; its matchers want a flow and the store holds Exchanges. Bodies are read only when the expression mentions one. |
| Session ids are microsecond timestamps | Lexicographic order is chronological, so retention opens no files — a Session with a corrupt manifest still prunes correctly. |

## Traps discovered the hard way

- **`mitmproxy` needs its explicit `>=11` floor.** Loosen it and resolution fails outright — uv backtracks to an ancient release whose `urwid` uses `use_2to3`, which modern setuptools rejects. It is a build error, not a warning.
- **`FastMCP` is the server class, not `MCPServer`.** `from mcp.server import MCPServer` raises `ImportError` in the pinned SDK (1.29.0) even though newer docs name it. Introspect the installed package rather than trusting docs.
- **Clearing mitmproxy's `mode` option is what releases the listener.** `master.shutdown()` stops the master and leaves the port bound, so `stop_capture` then `start_capture` on the same port fails. `options.update(mode=[])` first, then `shutdown()`.
- **`DumpMaster` runs fine as a task on an already-running event loop.** `master.run()` is a coroutine; `asyncio.create_task` it. This is what makes ADR-0001's single process work.
- **`mode=["regular@127.0.0.1:0"]` binds an ephemeral port on loopback only**, and `addons.get("proxyserver").listen_addrs()` reports what it got. That is what makes the tests hermetic — never hard-code a port.
- **A stray line on stdout does *not* corrupt MCP framing.** Tested with a deliberate flushed `print`: the client parses stdout line by line and skips what it cannot parse. Keep the proxy silent anyway — an interleaved write *inside* a JSON-RPC line would corrupt it, and the two writers hold separate buffers on one descriptor. But no test enforces this, so do not rely on one catching it.
- **`Message.get_content(strict=True)` — the default — raises `ValueError` on a malformed `Content-Encoding`.** Capture must pass `strict=False`. Otherwise one misbehaving endpoint ends the Session.
- **`mitmproxy.test.tflow` is the right harness for addon unit tests.** Real flow objects, and it makes shapes a live proxy will not produce on demand — a compressed body, a corrupt encoding, a connection that errored before any response — trivial to construct.
- **`pytest-asyncio` may run an async-generator fixture's setup and teardown in different tasks.** The anyio cancel scopes inside `stdio_client` reject that outright: "Attempted to exit cancel scope in a different task". Keep an MCP client's whole lifetime inside the test body, via an `@asynccontextmanager` rather than a fixture.
- **`FastMCP.call_tool` returns `(content_blocks, structured_result)`** for a tool annotated to return a dict — not a bare content list. Over the wire, the client's `CallToolResult` carries the same thing as `.structuredContent`.
- **Invoke mitmdump in tests as `python -c "from mitmproxy.tools.main import mitmdump; mitmdump()"`.** Args after `-c` land in `sys.argv[1:]` where its parser wants them, and it uses the same interpreter as the test.
- **`curl` needs `--noproxy ""` to proxy localhost.** An empty exclusion list means nothing bypasses. Without it an ambient `NO_PROXY` silently sends the request direct and the proxy records nothing.
- **Pass mitmdump a throwaway `--set confdir=`** in tests, or it writes an Interception Authority into the developer's real `~/.mitmproxy`. Generating one is also most of the cost of starting a capture, so share a confdir across tests in a module.
- **Ruff 0.16 formats Python blocks inside Markdown.** `*.md` is excluded in `pyproject.toml`; leave it that way or it will rewrite the spec's code samples.
- **`timeout` does not exist on macOS.** Don't reach for it in test scripts.

## Deliberately not built yet

- **`json_path` on `get_exchange_body`.** It is in SPEC.md's table. Doing it properly means a JSONPath dependency or a hand-rolled subset, and P2's criterion did not need it, so it was left rather than half-done. Decide which when something wants it.
- **In-flight Exchanges.** Recording happens on completion, so a request still hanging is not in the store. `tail_exchanges` therefore shows completions, not attempts. Adding it is additive to the store (`record_pending` / `complete`), not a rewrite — but decide it deliberately.
- **`Scope` is recorded and reported, but does not yet narrow anything.** `start_capture` stores it in `session.json` and `get_status` echoes it; no read tool consults it. Wire it into the listing tools alongside P3's denylist, since both are the same "narrow what is reported" mechanism.
- **`serverInfo.version`** still reports the MCP SDK's version (1.29.0), not ours, because `FastMCP.__init__` takes no `version` parameter. Deferred to P5; fixing it means dropping to the lowlevel `Server` or finding another hook.

## Open questions (none block P3)

1. Can `--mode local:<spec>` scope to the app process inside CoreSimulator? Only affects the deferred opt-in iOS path.
2. How should WebSocket and long-lived streaming connections appear in an interval model? They overlap everything by nature. Will matter by P4.
3. Do Detox / Appium / Playwright / XCUITest expose per-Step timings rich enough for the `Step` contract, or do they need explicit marks?
4. Does `simctl keychain` require a booted simulator, or accept a shut-down one by UDID? Decides whether trust can be arranged pre-boot. **Answer this first in P3.**
5. How should a non-UTF-8 body be represented? Settled for now — described in previews, base64 in slices — but nobody has used it against real binary traffic yet.

## Background research

The knowledge base this project was specced from lives at `~/Documents/Projects/knowledge/traffic-sniffing-mcp` — 7 articles, 26 sources, covering mitmproxy internals, both platform setups, prior art, and the correlation problem. Its `outputs/queries/2026-07-28 mitmproxy-mcp-implementation-plan.md` is the earlier plan that SPEC.md supersedes. Useful for *why* something is true; SPEC.md and the ADRs are authoritative for *what we're building*.

Note two things in that KB are known-stale relative to this repo: it says the SDK class is `MCPServer` in v2 docs, and it ranks clock skew as a top risk — ADR-0003 removes that risk by construction.
