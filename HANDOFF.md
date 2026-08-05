# Handoff — 2026-08-05

Picking this up in a fresh session. Everything needed is in the repo; this file says where to start and what not to relitigate.

## State

**P0 and P1 complete and committed.** On `main`, clean tree, no remote configured.

Verify in one command: `uv run pytest -q && uv run ruff check .` → 106 passed, all checks passed.

The 12 integration tests spawn real `mitmdump` and `curl`; `uv run pytest -m "not integration"` skips them and runs in half a second.

## Read these first, in this order

1. **[CONTEXT.md](./CONTEXT.md)** — the domain language. Use these words exactly; they were chosen to dodge a real collision (mitmproxy's "flow" vs Maestro's "flow"). If you find yourself writing "flow", stop. The single exception is `capture/addon.py`, where `flow` names mitmproxy's own object at the boundary.
2. **[SPEC.md](./SPEC.md)** — the v1 spec. Tool surface, semantics, numeric defaults.
3. **[PLAN.md](./PLAN.md)** — six phases with acceptance criteria. You are starting P2.
4. **[docs/adr/](./docs/adr/)** — five decisions and why. Two deviate from the research that preceded the project, so read them before "fixing" anything that looks wrong.

## What P1 built

| Module | Contents |
|---|---|
| `capture/exchange.py` | `CapturedExchange` (what the addon saw, bodies in memory) and `Exchange` (what the store kept, bodies on disk), plus `BodyRef` and the NDJSON round-trip |
| `capture/store.py` | `ExchangeStore` — ring buffer, eviction counter, body spill, append-only log, `0700` directory |
| `capture/addon.py` | `CaptureAddon` — the only module that knows mitmproxy's shapes |
| `serialize/redact.py` | Header, JSON-key and JWT redaction to `<redacted:sha256:xxxxxxxx>` |

## Next: P2 — read-only MCP tools

`start_capture` / `stop_capture` / `get_status`, then the Exchange tools with cursor pagination throughout, and `CurlTarget` as a trivial `CaptureTarget` so the whole surface is exercisable without a simulator.

**Acceptance:** a real MCP client can capture `curl` traffic, page through it, fetch one body slice, and have `wait_for_exchanges` return on a request fired *after* the call was made.

Three things P1 deliberately left for you, because their shape is decided by the tools that consume them:

- **Truncation and pagination** in `serialize/`. The numbers are already in SPEC.md (2048-byte preview, 64 KB slice, 50/200 page size) but nothing implements them yet. `redact_body` returns `bytes` precisely so the caller owns representation — including how a non-UTF-8 body is shown to a model, which is still an open choice.
- **Session identity and retention.** `ExchangeStore` takes a directory and nothing more. Session id generation, `session.json`, the `sessions/` layout and pruning to 10 belong with `start_capture`.
- **In-flight visibility.** Exchanges are recorded on completion, so a request still hanging is not in the store. If `tail_exchanges` should show it, that is an additive change to the store (`record_pending` / `complete`), not a rewrite — but decide it deliberately rather than discovering it.

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

Added by P1:

| Decision | Why it looks odd |
|---|---|
| Exchange ids are ordered by **completion**, not by start | A slow request that started first can carry a higher id. That is the right ordering for cursor-based tailing ("what finished since I last looked"); time questions use `t_start` explicitly and never lean on id order. |
| The whole `Authorization` value is digested, scheme included | `Bearer` is not preserved. The acceptance criterion is that no `Authorization` value survives, and partial redaction of an unknown scheme is how partial leaks happen. |
| `ExchangeStore` has no lock | It assumes the addon and the tool surface share one event loop, which is what ADR-0001's single process buys. Moving the proxy to its own thread means adding a lock here, deliberately. |
| Bodies are stored **decoded**, not as they were on the wire | A gzipped blob on disk answers no debugging question. `get_content(strict=False)` so one malformed `Content-Encoding` cannot end the Session. |
| Body paths on an `Exchange` are relative to the Session directory | Keeps a Session movable and the NDJSON portable. |

## Resolved after P1

**Form-encoded bodies are key-redacted**, and SPEC.md was widened to say so. Key-based redaction had been JSON-only, which left the OAuth 2 password grant — form-encoded by RFC 6749, and one of the likeliest requests to point this tool at — sending `password=…` in clear.

`redact_body` now takes an optional `content_type`, authoritative when given, and *also* treats a form-shaped body as a form without one. The redundancy is deliberate: a caller who forgets the header would otherwise reintroduce exactly the leak this closes. Fields nobody asked to touch are left byte-for-byte alone rather than round-tripped through `urlencode`.

## Traps discovered the hard way

- **`mitmproxy` needs its explicit `>=11` floor.** Loosen it and resolution fails outright — uv backtracks to an ancient release whose `urwid` uses `use_2to3`, which modern setuptools rejects. It is a build error, not a warning.
- **`FastMCP` is the server class, not `MCPServer`.** `from mcp.server import MCPServer` raises `ImportError` in the pinned SDK (1.29.0) even though newer docs name it. Introspect the installed package rather than trusting docs.
- **Ruff 0.16 formats Python blocks inside Markdown.** `*.md` is excluded in `pyproject.toml`; leave it that way or it will rewrite the spec's code samples.
- **`timeout` does not exist on macOS.** Don't reach for it in test scripts.
- **`Message.get_content(strict=True)` — the default — raises `ValueError` on a malformed `Content-Encoding`.** Capture must pass `strict=False`, which falls back to the raw bytes. Otherwise one misbehaving endpoint ends the Session.
- **`mitmproxy.test.tflow` is the right harness for addon unit tests.** Real flow objects, and it makes shapes a live proxy will not produce on demand — a compressed body, a corrupt encoding, a connection that errored before any response — trivial to construct.
- **Invoke mitmdump in tests as `python -c "from mitmproxy.tools.main import mitmdump; mitmdump()"`.** Args after `-c` land in `sys.argv[1:]` where its parser wants them. This uses the same interpreter as the test, so the addon is loaded by a Python that can import the package; depending on the installed console script instead makes the test PATH-sensitive.
- **`curl` needs `--noproxy ""` to proxy localhost.** An empty exclusion list means nothing bypasses. Without it an ambient `NO_PROXY` silently sends the request direct and the proxy records nothing.
- **A `-s` loader script runs at import.** `tests/mitmdump_loader.py` builds its store at module scope, so importing it just to read a constant would demand the env var and create a stray directory. It guards on `os.environ.get`.
- **Pass mitmdump a throwaway `--set confdir=`** in tests, or it writes an Interception Authority into the developer's real `~/.mitmproxy`.

## Open questions (none block P2)

1. Can `--mode local:<spec>` scope to the app process inside CoreSimulator? Only affects the deferred opt-in iOS path.
2. How should WebSocket and long-lived streaming connections appear in an interval model? They overlap everything by nature. Will matter by P4.
3. Do Detox / Appium / Playwright / XCUITest expose per-Step timings rich enough for the `Step` contract, or do they need explicit marks?
4. Does `simctl keychain` require a booted simulator, or accept a shut-down one by UDID? Decides whether trust can be arranged pre-boot. Matters in P3.
5. How should a non-UTF-8 body be represented to a model — base64, a size-and-type marker, or a hex head? `redact_body` returns bytes untouched and leaves the choice to P2.

## Known nit

`serverInfo.version` reports the MCP SDK's version (1.29.0), not ours, because `FastMCP.__init__` takes no `version` parameter. Deferred to P5; fixing it means dropping to the lowlevel `Server` or finding another hook.

## Background research

The knowledge base this project was specced from lives at `~/Documents/Projects/knowledge/traffic-sniffing-mcp` — 7 articles, 26 sources, covering mitmproxy internals, both platform setups, prior art, and the correlation problem. Its `outputs/queries/2026-07-28 mitmproxy-mcp-implementation-plan.md` is the earlier plan that SPEC.md supersedes. Useful for *why* something is true; SPEC.md and the ADRs are authoritative for *what we're building*.

Note two things in that KB are known-stale relative to this repo: it says the SDK class is `MCPServer` in v2 docs, and it ranks clock skew as a top risk — ADR-0003 removes that risk by construction.
