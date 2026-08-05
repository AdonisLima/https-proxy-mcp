---
name: https-proxy-mcp
description: Diagnose a failing mobile end-to-end test by looking at the network traffic underneath it. Use when an E2E test or a UI assertion fails and the cause might be a request — a 401, a slow response, a request still in flight when the assertion ran — or when you need to see what HTTPS calls an iOS Simulator app is actually making, inspect a request or response body, or check whether the app sent the right token. Covers capturing traffic through a local proxy, running the test through the proxy so steps and requests line up on one timeline, and telling "no traffic" apart from "untrusted certificate" apart from "certificate pinning". Triggers include "why did this test fail", "what request did the app make", "show me the network traffic", "is the app sending the right token", "the screen is blank and I don't know why". Not for production traffic monitoring, load testing, or modifying requests in flight.
---

# https-proxy-mcp

A failing assertion tells you the UI was wrong. It does not tell you that `GET /me` returned
401 four hundred milliseconds earlier, or that the request the screen was waiting on had not
come back yet. This server captures the app's HTTPS traffic and interleaves it with the test's
own steps, so you can answer *why* instead of *what*.

Twenty tools. Three of them are the path; the rest are drill-down and diagnosis.

## The three calls that matter

```
start_capture(target="ios-simulator", udid="…")   # or target="curl" to try it with no simulator
run_test(command="maestro test login.yaml")        # the Run is exactly this process's lifetime
get_timeline()                                     # this is the diagnosis
```

That is the whole happy path. `get_timeline` is where the answer is — do not stop at `run_test`'s
summary, and do not start paging through `list_exchanges` before you have read the Timeline.

`run_test` sets Maestro's output flags for you and points them at the Run's own directory, so
Maestro's steps are found automatically. A non-zero exit is a **result**, not an error: a failing
test is the normal reason to be here.

## Reading a Timeline

Each Step reports two things, and the distinction is the entire point:

- **`owned`** — traffic that Step *caused*. The request started while it was running.
- **`overlapping`** — traffic that was *already in flight* when the Step began.

`overlapping` is usually the answer. An assertion failing while the request it depended on had
not returned is the commonest cause of a flaky mobile test there is, and a failing Step carries a
one-line `explanation` saying so:

```
[failed] assertVisible: "Home" (17418ms)
   => began while #46 GET api.example.com/me (401) was still in flight
```

Traffic belonging to no Step is **Background** — polling, telemetry — counted and named by host,
and shown only with `include_background=true`. A request that never came back appears under
`pending` with a null id, because ids are assigned on completion. Two requests still open when a
test timed out is often the whole explanation.

`get_timeline()` with no argument means the most recent Run. Use `verbosity="full"` for body sizes
and Background, and `host_filter="api.example.com"` to narrow to one host and its subdomains.

## When you are driving the app yourself

No test command to wrap? Mark the steps by hand and you get the same Timeline with no adapter and
no artifact parsing:

```
start_step("tapOn: Login")   →   tap it   →   end_step(status="passed")
```

`start_step` fails if a Step is already open — closing one implicitly would have to invent an
outcome, and the outcome is the reason for recording it.

To wait for a specific request rather than poll for it:

```
wait_for_exchanges(filter="~u /auth & ~m POST", timeout_ms=15000)
```

Only traffic recorded **after** the call counts, so "tap Login, then wait for `POST /auth`" cannot
be satisfied by the *previous* login. A timeout is a result, not an error — `timed_out: true`
means it never happened.

One limit worth knowing: `run_test` blocks, so you cannot interleave `start_step` with it. Use one
or the other per Run. If you mark Steps after `run_test` returns they fall outside that Run's
window, and `get_timeline` returns a `note` explaining it rather than an unexplained empty result.

## When it looks like there is no traffic

**Call `diagnose_capture` before concluding anything.** Four very different causes present
identically as "no traffic", and this is the only tool that tells them apart:

| Verdict | Means |
|---|---|
| `no_connect` | Nothing reached the proxy. Routing is not in place, the simulator is not booted, or the app has not made a request yet. |
| `untrusted_authority` | The device does not trust the interception certificate, so TLS fails before a request exists. Fix with `install_authority`. |
| `tls_failed` | Handshakes failing *even though* the certificate is trusted — the signature of certificate pinning, not a setup mistake. |
| `out_of_scope` | Traffic was captured and your `scope` or the builtin denylist reported none of it. |
| `ok` | Exchanges are arriving. |

`tls_failed` is only reported when you set a `scope` on `start_capture`, and that is deliberate
rather than broken. Redirecting the proxy redirects the **whole Mac**, so every other app on the
machine is proxied too and fails its handshake — it has no reason to trust a certificate installed
only into the simulator. Without a scope naming the app's hosts there is no evidence, only noise.
Set `scope=["api.example.com"]` and pinning becomes attributable.

## Do not flood your own context

`list_exchanges` **cannot** return bodies — there is no parameter that would do it. That is the
API enforcing what guidance cannot. Work inward:

1. `list_exchanges(host="api.example.com", status=401)` — compact rows, 50 per page.
2. `get_exchange(id)` — one Exchange in full, with the first 2 KB of each body.
3. `get_exchange_body(id, side="response", offset=…)` — the bytes, 64 KB per call, only if you
   still need them.

`search_exchanges` takes **mitmproxy's own filter language**, not a new one: `~u /auth` (URL),
`~m POST`, `~c 401`, `~d api.example.com`, `~b` (body), combined with `&`, `|`, `!`. Body
operators read from disk, so prefer a metadata filter when one will do.

Every listing reports `evicted` and `hidden`. "There is nothing older" and "there was something
older and it is gone" are different answers, and only one means stop looking.

## Secrets come back as stable digests

Every secret is replaced with `<redacted:sha256:xxxxxxxx>`, and the same secret always renders to
the same marker. So you can answer *"is this the same token as the last request?"* and *"did the
token change after refresh?"* without ever seeing the value — which is usually the actual
question. `include_secrets=true` opts out per call; ask for it only when the value itself is what
you are checking.

`export_curl` redacts by default, which means **the default command will not run**. Pass
`include_secrets=true` for one that reproduces the request.

## What this changes on the machine

CoreSimulator has no proxy setting of its own — it inherits the host's. So `start_capture` with
the `ios-simulator` target **redirects your Mac's system proxy**, which affects every application
until `stop_capture`. The prior settings are written to disk before anything changes, restored on
exit, and restored by the next server start if this one is killed outright. `get_status` says
whether the proxy is currently redirected.

Consequences to expect and not report as bugs: other apps' TLS handshakes fail against the proxy,
and `diagnose_capture` will not attribute pinning without a `scope` (above).

**Always call `stop_capture` when you are done.** It releases the port and puts the proxy back.

The Session directory (`~/.https-proxy-mcp/sessions/`, or `$HTTPS_PROXY_MCP_ROOT`) holds
**unredacted** bodies on local disk, so it is `0700` and pruned to the 10 most recent. Redaction
applies where data leaves — to you, and to `export_har`.

## The rest of the surface

| Tool | When |
|---|---|
| `get_status` | Is capture running, how many Exchanges, is the proxy redirected |
| `stop_capture` | Always, when done |
| `list_runs` | Find a `run_id` when more than one test has run |
| `tail_exchanges` | Watch traffic arrive; pass back `next_cursor`. To *block*, use `wait_for_exchanges` |
| `export_har` | A HAR for a browser or a bug report. Redacted by default; whole Session unless you pass `run_id` |
| `install_authority` | Only after `diagnose_capture` says `untrusted_authority` — `start_capture` does it for you |
| `get_trust_status` | Ask whether a device already trusts the certificate, changing nothing |
| `install_authority_system` | Machine-wide trust, needs `sudo`, never implicit. Only for a macOS app or desktop browser — a simulator does not need it |

## Running the server

Not on PyPI. Point your MCP client at a checkout or a built wheel:

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

`uv build` produces a wheel you can point `--from` at instead. macOS only — capture routes traffic
with `networksetup` and installs trust with `simctl`. Requires Python ≥ 3.12, which `uv` provides.

To use this skill on another machine, copy this one file to
`~/.claude/skills/https-proxy-mcp/SKILL.md`. It depends on nothing else.
