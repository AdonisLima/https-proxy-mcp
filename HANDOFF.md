# Handoff — 2026-07-30

Picking this up in a fresh session. Everything needed is in the repo; this file says where to start and what not to relitigate.

## State

**P0 complete and committed.** Six commits on `main`, clean tree, no remote configured.

```
ea66f07 feat: serve MCP over stdio with an empty tool surface
10e7543 chore: scaffold uv package on python 3.12
b860135 docs: plan v1 in six phases with acceptance criteria
1bf4a21 docs: specify v1 architecture, tool surface, and semantics
cf37b47 docs: record five architectural decisions for v1
d2d3429 docs: define domain language for capture, testing, and correlation
```

Verify in one command: `uv run pytest -q && uv run ruff check .` → 4 passed, all checks passed.

## Read these first, in this order

1. **[CONTEXT.md](./CONTEXT.md)** — the domain language. Use these words exactly; they were chosen to dodge a real collision (mitmproxy's "flow" vs Maestro's "flow"). If you find yourself writing "flow", stop.
2. **[SPEC.md](./SPEC.md)** — the v1 spec. Tool surface, semantics, numeric defaults.
3. **[PLAN.md](./PLAN.md)** — six phases with acceptance criteria. You are starting P1.
4. **[docs/adr/](./docs/adr/)** — five decisions and why. Two deviate from the research that preceded the project, so read them before "fixing" anything that looks wrong.

## Next: P1 — capture core

No mobile tooling required. Build against `curl` and a local fixture server.

- `capture/` — mitmproxy addon; `ExchangeStore` with a bounded metadata ring buffer (5000), body spill to disk, append-only `exchanges.ndjson`, monotonic Exchange ids, eviction counter.
- `serialize/` — redacting serializer with the stable-digest scheme `<redacted:sha256:xxxxxxxx>`.

**Acceptance:** `mitmdump` with the addon loaded, driven by `curl` against a fixture server, produces correct Exchange metadata and spilled bodies; a unit test asserts no `Authorization` value survives default serialization.

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

## Traps discovered the hard way

- **`mitmproxy` needs its explicit `>=11` floor.** Loosen it and resolution fails outright — uv backtracks to an ancient release whose `urwid` uses `use_2to3`, which modern setuptools rejects. It is a build error, not a warning.
- **`FastMCP` is the server class, not `MCPServer`.** `from mcp.server import MCPServer` raises `ImportError` in the pinned SDK (1.29.0) even though newer docs name it. Introspect the installed package rather than trusting docs.
- **Ruff 0.16 formats Python blocks inside Markdown.** `*.md` is excluded in `pyproject.toml`; leave it that way or it will rewrite the spec's code samples.
- **`timeout` does not exist on macOS.** Don't reach for it in test scripts.

## Open questions (none block P1)

1. Can `--mode local:<spec>` scope to the app process inside CoreSimulator? Only affects the deferred opt-in iOS path.
2. How should WebSocket and long-lived streaming connections appear in an interval model? They overlap everything by nature. Will matter by P4.
3. Do Detox / Appium / Playwright / XCUITest expose per-Step timings rich enough for the `Step` contract, or do they need explicit marks?
4. Does `simctl keychain` require a booted simulator, or accept a shut-down one by UDID? Decides whether trust can be arranged pre-boot. Matters in P3.

## Known nit

`serverInfo.version` reports the MCP SDK's version (1.29.0), not ours, because `FastMCP.__init__` takes no `version` parameter. Deferred to P5; fixing it means dropping to the lowlevel `Server` or finding another hook.

## Background research

The knowledge base this project was specced from lives at `~/Documents/Projects/knowledge/traffic-sniffing-mcp` — 7 articles, 26 sources, covering mitmproxy internals, both platform setups, prior art, and the correlation problem. Its `outputs/queries/2026-07-28 mitmproxy-mcp-implementation-plan.md` is the earlier plan that SPEC.md supersedes. Useful for *why* something is true; SPEC.md and the ADRs are authoritative for *what we're building*.

Note two things in that KB are known-stale relative to this repo: it says the SDK class is `MCPServer` in v2 docs, and it ranks clock skew as a top risk — ADR-0003 removes that risk by construction.
