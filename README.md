# https-proxy-mcp

An MCP server that captures the HTTPS traffic of an app under test and interleaves it with end-to-end test steps, so an agent can diagnose a failure by cause rather than by symptom.

A failing assertion tells you the UI was wrong. It doesn't tell you that `GET /me` returned 401 four hundred milliseconds earlier, or that the request the screen was waiting on hadn't come back yet.

> **Status: P5 Hardening and distribution — v1 feature-complete.** Twenty MCP tools, working end
> to end against a real booted simulator. Start a capture and the Mac's system proxy is redirected
> and the Interception Authority installed into the device for you; page through traffic, drill
> into one Exchange, slice a body, search with mitmproxy filter expressions, or block until the
> request you are waiting for arrives. Then run your end-to-end test through `run_test` and ask
> `get_timeline` why it failed. Builds to a wheel, ships [SKILL.md](./SKILL.md), and is not
> published to PyPI. See [PLAN.md](./PLAN.md).

## Trying it

```bash
uv sync
uv run https-proxy-mcp     # speaks MCP over stdio
```

Or point an MCP client at a checkout — there is no PyPI release, so `--from` takes a path:

```jsonc
{
  "mcpServers": {
    "https-proxy-mcp": {
      "command": "uvx",
      "args": ["--from", "/path/to/https-proxy-mcp", "https-proxy-mcp"]
    }
  }
}
```

`uv build` produces a wheel and an sdist you can point `--from` at instead. Both are verified by
`tests/test_distribution.py`, which installs the wheel into a clean virtualenv under every
supported interpreter and drives a real capture through the installed console script.

Point an MCP client at that command, then:

1. `start_capture(target="curl", port=0)` — returns the port it bound.
2. Send traffic through it: `curl -x http://127.0.0.1:<port> --noproxy "" http://example.com`
3. `list_exchanges()` to see it, `get_exchange(id)` for detail, `get_exchange_body(id, side)` for bytes.
4. `stop_capture()`.

Full setup — client config, simulator, troubleshooting — is in [docs/INSTALL.md](./docs/INSTALL.md).

Then the part the project exists for. Run a test and read the Timeline:

```
run_test(command="maestro test login.yaml")
get_timeline()
```

Each Step reports what it **caused** and what was **already in flight** when it ran. That second
one is usually the answer, and a failing Step says it in a sentence:

```
[failed] assertCondition: "Home" (17418ms)
   => began while #46 GET api.example.com/auth (401) was still in flight
```

Maestro's output flags are set for you and pointed at the Run's own directory, so its Steps are
found automatically. Any other test tool works too — wrap each action in `start_step` /
`end_step` and you get the same Timeline with no adapter and no parsing.

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

**Traffic is attributed by overlap, not assignment.** An Exchange is *owned* by the Step that was
active when it started — that Step caused it — and every other Step it overlaps is recorded too. A
flat one-Step-per-Exchange model would report either "the tap made a request" or "the assert made a
request", and both lose the sentence that actually explains the failure. A request that never came
back appears as `pending`, because the store records on completion and "still waiting" is a
different answer from "finished". See [ADR-0004](./docs/adr/0004-overlap-attribution.md).

**A listing cannot return a body.** Not by default — at all. `list_exchanges` has no parameter that would do it, so bodies need a second call and large ones need an offset. The failure mode for a traffic tool driven by an agent is flooding the context window, and the API is shaped to make that impossible rather than merely discouraged.

## Documentation

| Document | Contents |
|---|---|
| [docs/INSTALL.md](./docs/INSTALL.md) | Installation: requirements, MCP client config, simulator setup, troubleshooting |
| [docs/USAGE.md](./docs/USAGE.md) | Usage: worked examples with real output, and the full twenty-tool reference |
| [SKILL.md](./SKILL.md) | The agent-facing prompt: which three tools matter, and how to read a Timeline |
| [SPEC.md](./SPEC.md) | The v1 specification — architecture, tool surface, semantics, defaults |
| [CONTEXT.md](./CONTEXT.md) | Domain vocabulary, used precisely throughout the code |
| [PLAN.md](./PLAN.md) | Six-phase build plan with acceptance criteria |
| [docs/v1.1-candidates.md](./docs/v1.1-candidates.md) | Everything deliberately not built, sized and ranked, with a recommended v1.1 scope |
| [HANDOFF.md](./HANDOFF.md) | Current state, what's next, and the traps found the hard way |
| [docs/adr/](./docs/adr/) | Architectural decisions and why they went the way they did |

`SKILL.md` is deliberately self-contained — no links back into this repo — so it works copied to
`~/.claude/skills/https-proxy-mcp/SKILL.md` on any machine.

## Development

```bash
uv sync                            # install dependencies
uv run pytest                      # run everything
uv run pytest -m "not integration" # skip the tests that bind sockets and spawn processes
uv run ruff check .                # lint
uv run ruff format .               # format

# The two suites that change your machine's networking. Each creates and deletes its own
# throwaway simulator; gated so they can never run by accident.
HTTPS_PROXY_MCP_E2E=1 uv run pytest tests/test_simulator_e2e.py tests/test_maestro_e2e.py
```

Requires Python ≥ 3.12 (mitmproxy 12's floor); `uv` provides it.

The `integration` tests are real: they bind ports, run `mitmdump` and `curl`, drive a server subprocess over stdio, `kill -9` one to prove the system proxy comes back, and build the wheel and install it into throwaway virtualenvs. The rest run in about two seconds.

Both dependencies carry **upper bounds**, and they earn them. A wheel ships no lock file, so a
consumer resolves from `Requires-Dist` alone — and with `mcp` uncapped, a clean install on Python
3.13 resolved mcp 2.0.0, which removed `mcp.server.fastmcp` entirely and left the server unable to
import. Lifting either ceiling means porting to the new API, not relaxing a bound.

`tests/test_simulator_e2e.py` and `tests/test_maestro_e2e.py` are the only files that touch your real network configuration, and both are gated behind an environment variable rather than a pytest marker — a command-line `-m` replaces any default marker expression, so `-m "not integration"` would otherwise have silently opted back in.

`tests/test_pending_e2e.py` proves the in-flight path — a request that never answers — through a real proxy with `curl` and a socket that accepts and never replies. It needs no simulator and changes nothing, so it runs in the ordinary suite.

## Scope

**v1:** iOS Simulator capture, read-only traffic access, Run-scoped Timelines with Maestro as the first Event Source, and explicit Steps for any other test tool.

**Not v1:** Android Emulator, write/interception tools, physical devices, browsers. **Never:** production traffic monitoring.

macOS only, and not softenable: capture routes traffic with `networksetup` and installs trust with `simctl`.

## License

[MIT](./LICENSE).
