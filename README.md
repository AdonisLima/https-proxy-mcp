# https-proxy-mcp

An MCP server that captures the HTTPS traffic of an app under test and interleaves it with end-to-end test steps, so an agent can diagnose a failure by cause rather than by symptom.

A failing assertion tells you the UI was wrong. It doesn't tell you that `GET /me` returned 401 four hundred milliseconds earlier, or that the request the screen was waiting on hadn't come back yet.

> **Status: P2 read-only tools.** Ten MCP tools work end to end against `curl` — start a
> capture, page through traffic, drill into one Exchange, slice a body, search with mitmproxy
> filter expressions, or block until a request you're waiting for arrives. The iOS Simulator
> target is P3 and Run-scoped Timelines are P4. See [PLAN.md](./PLAN.md) for what lands when.

## Trying it

```bash
uv sync
uv run https-proxy-mcp     # speaks MCP over stdio
```

Point an MCP client at that command, then:

1. `start_capture(target="curl", port=0)` — returns the port it bound.
2. Send traffic through it: `curl -x http://127.0.0.1:<port> --noproxy "" https://example.com`
3. `list_exchanges()` to see it, `get_exchange(id)` for detail, `get_exchange_body(id, side)` for bytes.
4. `stop_capture()`.

Captured Sessions live under `~/.https-proxy-mcp/sessions/`, or `$HTTPS_PROXY_MCP_ROOT` if set. **They hold unredacted bodies**, so they are `0700` and pruned to the 10 most recent.

## Two things worth knowing

**Secrets are redacted to stable digests, not deleted.** Every secret becomes `<redacted:sha256:xxxxxxxx>`, and the same secret always renders to the same marker. So an agent can tell "this is the same token as the last request" or "the token changed after refresh" without the value ever being visible — which is usually the actual question. `include_secrets=true` opts out per call, and the Session directory on disk stays faithful regardless.

**A listing cannot return a body.** Not by default — at all. `list_exchanges` has no parameter that would do it, so bodies need a second call and large ones need an offset. The failure mode for a traffic tool driven by an agent is flooding the context window, and the API is shaped to make that impossible rather than merely discouraged.

## Documentation

| Document | Contents |
|---|---|
| [SPEC.md](./SPEC.md) | The v1 specification — architecture, tool surface, semantics, defaults |
| [CONTEXT.md](./CONTEXT.md) | Domain vocabulary, used precisely throughout the code |
| [PLAN.md](./PLAN.md) | Six-phase build plan with acceptance criteria |
| [HANDOFF.md](./HANDOFF.md) | Current state, what's next, and the traps found the hard way |
| [docs/adr/](./docs/adr/) | Architectural decisions and why they went the way they did |

## Development

```bash
uv sync                            # install dependencies
uv run pytest                      # run everything
uv run pytest -m "not integration" # skip the tests that bind sockets and spawn processes
uv run ruff check .                # lint
uv run ruff format .               # format
```

Requires Python ≥ 3.12 (mitmproxy 12's floor); `uv` provides it.

The `integration` tests are real: they bind ports, run `mitmdump` and `curl`, and drive a server subprocess over stdio. The rest run in about two seconds.

## Scope

**v1:** iOS Simulator capture, read-only traffic access, Run-scoped timelines with Maestro as the first event source, and explicit steps for any other test tool.

**Not v1:** Android Emulator, write/interception tools, physical devices, browsers. **Never:** production traffic monitoring.
