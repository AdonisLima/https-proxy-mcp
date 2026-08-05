# https-proxy-mcp

An MCP server that captures the HTTPS traffic of an app under test and interleaves it with end-to-end test steps, so an agent can diagnose a failure by cause rather than by symptom.

A failing assertion tells you the UI was wrong. It doesn't tell you that `GET /me` returned 401 four hundred milliseconds earlier, or that the request the screen was waiting on hadn't come back yet.

> **Status: P0 scaffold.** The server builds and speaks MCP over stdio with no tools registered. See [PLAN.md](./PLAN.md) for what lands when.

## Documentation

| Document | Contents |
|---|---|
| [SPEC.md](./SPEC.md) | The v1 specification — architecture, tool surface, semantics, defaults |
| [CONTEXT.md](./CONTEXT.md) | Domain vocabulary, used precisely throughout the code |
| [PLAN.md](./PLAN.md) | Six-phase build plan with acceptance criteria |
| [docs/adr/](./docs/adr/) | Architectural decisions and why they went the way they did |

## Development

```bash
uv sync              # install dependencies
uv run pytest        # run tests
uv run ruff check .  # lint
uv run ruff format . # format
```

Requires Python ≥ 3.12 (mitmproxy 12's floor); `uv` provides it.

## Scope

**v1:** iOS Simulator capture, read-only traffic access, Run-scoped timelines with Maestro as the first event source, and explicit steps for any other test tool.

**Not v1:** Android Emulator, write/interception tools, physical devices, browsers. **Never:** production traffic monitoring.
