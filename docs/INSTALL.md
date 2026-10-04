# Installation

Getting `https-proxy-mcp` running, in an MCP client, against a real iOS Simulator.

For what to do with it once it runs, see [USAGE.md](./USAGE.md). For the agent-facing prompt, see
[SKILL.md](../.claude/skills/https-proxy-mcp/SKILL.md).

## Requirements

| | |
|---|---|
| **macOS** | Required, and not softenable. Capture routes traffic with `networksetup` and installs trust with `simctl`. There is no Linux or Windows path. |
| **Python ≥ 3.12** | mitmproxy 12's floor. You do not need to install it — `uv` fetches it. |
| [**uv**](https://docs.astral.sh/uv/) | `brew install uv`, or `curl -LsSf https://astral.sh/uv/install.sh \| sh`. |
| **Xcode** | Only for the `ios-simulator` target, for `simctl` and a bootable simulator. The `curl` target needs none of it. |
| [**Maestro**](https://maestro.mobile.dev/) | Optional. Steps are parsed from its artifacts automatically; without it, `start_step` / `end_step` give the same Timeline. |

You do **not** need to install mitmproxy. It is a library dependency, not an external binary.

## There is no PyPI release

`uvx https-proxy-mcp` will not find anything. Every command below therefore points `--from` at a
path — a checkout or a built wheel. This is a deliberate choice, not an oversight; see
[HANDOFF.md](./HANDOFF.md).

## Install

### From a checkout

```bash
git clone <this repo> https-proxy-mcp   # or copy the directory
cd https-proxy-mcp
uv sync                                 # fetches Python 3.12 and the dependencies
uv run https-proxy-mcp                  # speaks MCP over stdio; Ctrl-C to stop
```

`uv sync` installs from `uv.lock`, so you get the exact versions this project is tested against.

### From a built wheel

Useful for moving it to another machine without the repo:

```bash
uv build                    # writes dist/https_proxy_mcp-<version>-py3-none-any.whl and an sdist
uvx --from ./dist/https_proxy_mcp-0.1.0-py3-none-any.whl https-proxy-mcp
```

A wheel carries **no lock file**, so this path resolves dependencies fresh from the declared
ranges. Both ranges are capped above for that reason — an uncapped `mcp` resolved 2.0.0, which
removed the API this server is built on. If you widen a cap, expect to port code.

## Configure your MCP client

The server speaks **stdio only**. There is no HTTP transport in v1.

### Claude Code

```bash
claude mcp add https-proxy-mcp -- uvx --from /absolute/path/to/https-proxy-mcp https-proxy-mcp
```

### Claude Desktop, or any client with a JSON config

```jsonc
{
  "mcpServers": {
    "https-proxy-mcp": {
      "command": "uvx",
      "args": ["--from", "/absolute/path/to/https-proxy-mcp", "https-proxy-mcp"]
    }
  }
}
```

Use an **absolute** path. Clients launch servers from an unpredictable working directory.

If `uvx` is not on your client's `PATH` — GUI apps often have a minimal one — give the full path
(`which uvx`), or use the checkout's own interpreter:

```jsonc
{
  "command": "/absolute/path/to/https-proxy-mcp/.venv/bin/https-proxy-mcp",
  "args": []
}
```

### Pointing the Session directory elsewhere

`$HTTPS_PROXY_MCP_ROOT` overrides `~/.https-proxy-mcp`. Set it in the client's `env` block, **not**
in your shell: many clients (and the MCP Python SDK's own `stdio_client`) hand the server a
minimal environment of `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM` and `USER` only, so an exported
variable never arrives.

```jsonc
"env": { "HTTPS_PROXY_MCP_ROOT": "/tmp/https-proxy-mcp" }
```

## Verify it works

Without any MCP client, and without touching your network settings:

```bash
cd /path/to/https-proxy-mcp
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"probe","version":"0"}}}' \
  | uv run https-proxy-mcp 2>/dev/null
```

You should get one line of JSON whose `result.serverInfo` reads
`{"name": "https-proxy-mcp", "version": "0.1.0"}`. If the version says `1.29.0` you are talking to
something built before the version fix; if you get nothing at all, drop the `2>/dev/null` and read
the traceback.

Then, from your client, the end-to-end check that needs no simulator:

```
start_capture(target="curl", port=0)     → returns the port it bound
```
```bash
curl -x http://127.0.0.1:<port> --noproxy "" http://example.com
```
```
list_exchanges()                          → one row for example.com
stop_capture()
```

`--noproxy ""` matters: an ambient `NO_PROXY` sends the request direct and the proxy records
nothing.

**Note the `http://`.** With the `curl` target nothing arranges trust for you — that is the point of
it — so an `https://` URL fails with `curl: (60) SSL certificate problem: unable to get local issuer
certificate`, because curl has no reason to trust the interception authority. Three ways to check
HTTPS instead:

```bash
curl --cacert ~/.https-proxy-mcp/mitmproxy/mitmproxy-ca-cert.pem \
     -x http://127.0.0.1:<port> --noproxy "" https://example.com   # trust it properly
curl -k -x http://127.0.0.1:<port> --noproxy "" https://example.com   # or skip verification
```

The authority file appears the first time capture starts. This is a `curl`-target concern only: with
the `ios-simulator` target, `start_capture` installs the authority into the device for you.

## iOS Simulator setup

Boot a simulator first — `simctl` cannot reach a shut-down device's trust store:

```bash
xcrun simctl list devices | grep Booted     # find the UDID
open -a Simulator                           # or boot one from Xcode
```

Then:

```
start_capture(target="ios-simulator", udid="F48AAE9F-…", scope=["api.example.com"])
```

That one call redirects your Mac's system proxy **and** installs the Interception Authority into
the device. You do not call `install_authority` yourself unless something wiped the trust store.

**Pass `udid` whenever more than one simulator is booted.** Guessing would capture the wrong app's
traffic and report success, so the server refuses to guess.

**Set `scope` to your app's hosts.** It does two things beyond noise reduction, and both matter more
than the filtering:

- With `passthrough` left on (the default), a `scope` is also the set of hosts whose **HTTPS gets
  decrypted**. Everything else on the machine is forwarded untouched, so your browser and any other
  booted simulator keep working. Name no scope and every HTTPS connection on the Mac is served a
  certificate that only the one named simulator trusts — which is a browser full of certificate
  warnings and every other simulator offline.
- Certificate pinning can only be attributed inside a scope, so without one `diagnose_capture` will
  not report pinning.

See [USAGE.md](./USAGE.md#when-nothing-appears).

### What this changes on your Mac, and how it comes back

CoreSimulator has no proxy setting of its own — it inherits the host's. So capture redirects the
**machine's** system proxy, affecting every application until `stop_capture`. A `scope` is what
keeps that from hurting; see above.

Restoration is a three-layer guarantee: your prior settings are written to
`~/.https-proxy-mcp/proxy-state.json` before anything changes, restored by exit handlers, and
restored by the next server start if this one is killed outright. `get_status` reports whether the
proxy is currently redirected — whether or not it is *this* server that is recording — and names the
process that redirected it.

**Only one process may hold that redirect.** Every editor window runs its own copy of this server
against that one file, so `start_capture` is refused, naming the holder's pid, while another live
process holds it. `kill <pid>` makes that process put your machine back on its way out; nothing here
force-releases someone else's redirect.

Expect, and do not report as bugs: with **no** `scope`, your other apps' TLS handshakes failing
against the proxy (they have no reason to trust a certificate installed only into the simulator);
and `diagnose_capture` declining to name pinning without a `scope`.

If a crash ever leaves the machine proxied and the next start cannot repair it, the runbook is
[.claude/skills/live-machine-tests/](../.claude/skills/live-machine-tests/SKILL.md) — four
`networksetup` commands.

### Trusting the authority outside a simulator

Only needed for a macOS build of your app or a desktop browser:

```
install_authority_system()
```

Machine-wide, needs `sudo`, and **never** implicit. It will not prompt: this server speaks
JSON-RPC on stdin, so a password prompt would read your client's next message as a password
attempt and hang the session. Without cached credentials it fails harmlessly and hands back the
exact command to run in a terminal.

## Installing the skill

`SKILL.md` is the agent-facing prompt — which three tools matter, how to read a Timeline. It is
self-contained, so one copy is the whole installation:

```bash
mkdir -p ~/.claude/skills/https-proxy-mcp
cp /path/to/https-proxy-mcp/.claude/skills/https-proxy-mcp/SKILL.md \
   ~/.claude/skills/https-proxy-mcp/SKILL.md
```

This works on any machine, including one without the repo.

## Development install

```bash
uv sync
uv run pytest -q                      # 675 passed, 3 skipped
uv run pytest -m "not integration"    # the fast majority, ~5s
uv run ruff check .
```

The `integration` tests are real: they bind ports, run `mitmdump` and `curl`, drive a server
subprocess over stdio, `kill -9` one to prove the system proxy comes back, and build the wheel and
install it into throwaway virtualenvs. None of them touch your network settings or your real
`~/.https-proxy-mcp`.

The 3 skipped ones do change your machine, and are gated on an environment variable rather than a
pytest marker — a command-line `-m` replaces any default marker expression, so `-m "not
integration"` would have silently opted back in:

```bash
HTTPS_PROXY_MCP_E2E=1 uv run pytest tests/test_simulator_e2e.py tests/test_maestro_e2e.py
```

Each creates and deletes its own throwaway simulator.

## Uninstalling

```bash
rm -rf ~/.https-proxy-mcp                       # Sessions, and the generated authority
rm -rf ~/.claude/skills/https-proxy-mcp         # the skill, if you installed it
```

Then remove the entry from your MCP client's config. Nothing else is left behind — `uvx` caches
under `~/.cache/uv`, and the system proxy is restored on every exit.

The Session directory holds **unredacted** request and response bodies, including credentials. It
is `0700` and pruned to the 10 most recent Sessions, but if you have captured production traffic,
deleting it is the only thing that removes those bodies.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `uvx https-proxy-mcp` finds nothing | There is no PyPI release. Use `--from <path>`. |
| `No module named 'mcp.server.fastmcp'` | An uncapped `mcp` resolved 2.0.0, which removed it. Reinstall from a build of this tree; the range is capped at `<2`. |
| Client shows the server as failed, no detail | Run the verify command above by hand. The client hides stderr; the traceback is on it. |
| `spawn uvx ENOENT` | `uvx` is not on the client's `PATH`. Use an absolute path or the checkout's `.venv/bin/https-proxy-mcp`. |
| Sessions appear in `~/.https-proxy-mcp` despite setting `HTTPS_PROXY_MCP_ROOT` | You exported it instead of putting it in the client's `env` block. The server never received it. |
| `install_authority` fails with `code=405 … Shutdown` | The simulator is not booted. `simctl keychain` cannot reach a shut-down device. |
| `curl: (60) SSL certificate problem` through the proxy | Expected with the `curl` target on an `https://` URL — nothing arranged trust. Use `--cacert ~/.https-proxy-mcp/mitmproxy/mitmproxy-ca-cert.pem`, or `-k`, or a plain `http://` URL. |
| Capture starts but nothing arrives | Call `diagnose_capture`. Four different causes look identical from the outside. |
| Every app on the Mac loses HTTPS while capturing | Expected. The proxy is machine-wide; only the simulator trusts the authority. `stop_capture` ends it. |
| Resolution fails complaining about `use_2to3` | Something loosened mitmproxy's `>=11` floor. uv backtracks to a release whose `urwid` modern setuptools rejects. |
