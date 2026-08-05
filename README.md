# https-proxy-mcp

An MCP server that captures the HTTPS traffic of an app under test and interleaves it with end-to-end test steps, so an agent can diagnose a failure by cause rather than by symptom.

A failing assertion tells you the UI was wrong. It doesn't tell you that `GET /me` returned 401 four hundred milliseconds earlier, or that the request the screen was waiting on hadn't come back yet.

> **Status: P3 iOS Simulator capture.** Fourteen MCP tools, working end to end against a real
> booted simulator — start a capture and the Mac's system proxy is redirected and the
> Interception Authority installed into the device for you, then page through traffic, drill
> into one Exchange, slice a body, search with mitmproxy filter expressions, or block until the
> request you're waiting for arrives. Run-scoped Timelines are P4. See [PLAN.md](./PLAN.md).

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

For a simulator, boot one first and then `start_capture(target="ios-simulator", udid="...")`.
Pass `udid` whenever more than one simulator is booted — guessing would capture the wrong app's
traffic and report success. If nothing shows up, call `diagnose_capture` rather than guessing:
it separates an untrusted authority from certificate pinning from an app that simply has not
made a request yet.

Captured Sessions live under `~/.https-proxy-mcp/sessions/`, or `$HTTPS_PROXY_MCP_ROOT` if set. **They hold unredacted bodies**, so they are `0700` and pruned to the 10 most recent.

## Two things worth knowing

**Secrets are redacted to stable digests, not deleted.** Every secret becomes `<redacted:sha256:xxxxxxxx>`, and the same secret always renders to the same marker. So an agent can tell "this is the same token as the last request" or "the token changed after refresh" without the value ever being visible — which is usually the actual question. `include_secrets=true` opts out per call, and the Session directory on disk stays faithful regardless.

**Capturing a simulator changes your Mac, and puts it back.** CoreSimulator has no proxy
setting of its own — it inherits the host's — so `start_capture` redirects the machine's system
proxy, which affects every application until `stop_capture`. Your prior settings are written to
`~/.https-proxy-mcp/proxy-state.json` *before* anything changes, restored on exit, and restored
by the next server start if this one is killed outright. `get_status` tells you whether the
proxy is currently redirected.

One consequence is worth knowing: in regular proxy mode there is no process attribution, so
your other applications go through the proxy too and their TLS handshakes fail against it —
they have no reason to trust the authority, which is installed only into the simulator. That is
harmless and expected. It also means a failed handshake can only be attributed to the app under
test when you set a `scope` naming its hosts, which is what `diagnose_capture` needs before it
will report pinning.

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

# The one suite that changes your machine's networking. Creates and deletes its own
# throwaway simulator; gated so it can never run by accident.
HTTPS_PROXY_MCP_E2E=1 uv run pytest tests/test_simulator_e2e.py
```

Requires Python ≥ 3.12 (mitmproxy 12's floor); `uv` provides it.

The `integration` tests are real: they bind ports, run `mitmdump` and `curl`, drive a server subprocess over stdio, and `kill -9` one to prove the system proxy comes back. The rest run in about two seconds.

`tests/test_simulator_e2e.py` is the only file that touches your real network configuration, and it is gated behind an environment variable rather than a pytest marker — a command-line `-m` replaces any default marker expression, so `-m "not integration"` would otherwise have silently opted back in.

## Scope

**v1:** iOS Simulator capture, read-only traffic access, Run-scoped timelines with Maestro as the first event source, and explicit steps for any other test tool.

**Not v1:** Android Emulator, write/interception tools, physical devices, browsers. **Never:** production traffic monitoring.
