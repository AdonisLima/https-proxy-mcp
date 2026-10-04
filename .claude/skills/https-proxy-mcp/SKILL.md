---
name: https-proxy-mcp
description: Diagnose a failing mobile end-to-end test by looking at the network traffic underneath it. Use when an E2E test or a UI assertion fails and the cause might be a request — a 401, a slow response, a request still in flight when the assertion ran — or when you need to see what HTTPS calls an iOS Simulator app is actually making, inspect a request or response body, or check whether the app sent the right token. Covers capturing traffic through a local proxy, running the test through the proxy so steps and requests line up on one timeline, and telling "no traffic" apart from "untrusted certificate" apart from "certificate pinning". Triggers include "why did this test fail", "what request did the app make", "show me the network traffic", "is the app sending the right token", "the screen is blank and I don't know why". Not for production traffic monitoring, load testing, or modifying requests in flight.
---

# https-proxy-mcp

A failing assertion tells you the UI was wrong. It does not tell you that `GET /me` returned
401 four hundred milliseconds earlier, or that the request the screen was waiting on had not
come back yet. This server captures the app's HTTPS traffic and interleaves it with the test's
own steps, so you can answer _why_ instead of _what_.

Twenty tools. Three of them are the path; the rest are drill-down and diagnosis.

## The three calls that matter

```
start_capture(target="ios-simulator", udid="…", scope=["api.example.com"])
run_test(command="maestro test login.yaml")        # the Run is exactly this process's lifetime
get_timeline()                                     # this is the diagnosis
```

That is the whole happy path. `get_timeline` is where the answer is — do not stop at `run_test`'s
summary, and do not start paging through `list_exchanges` before you have read the Timeline. Use
`target="curl"` to try the surface out with no simulator involved.

**Name a `scope` if you know the app's hosts.** It is not only a filter: with `passthrough` left on
(the default) it is also the set of hosts whose HTTPS gets decrypted, so every other HTTPS
connection on the machine is forwarded untouched and the developer's browser and any other simulator
carry on working. Name no `scope` and everything is intercepted, which breaks their browser for as
long as you are recording. See "What this changes on the machine" below.

**Want to watch the capture yourself?** `start_capture(web_ui=True)` serves the Human View — the
mitmweb UI from the Session's own proxy core, live on loopback — and the response carries its
token-gated URL to open in a browser; `web_port` pins it, where `0` (the default) picks a free one.
It is the core's view, so nothing narrows it and bodies are unredacted, and it is a write console
whose edits can make a Timeline lie. It dies with `stop_capture`.

`run_test` sets Maestro's output flags for you and points them at the Run's own directory, so
Maestro's steps are found automatically. A non-zero exit is a **result**, not an error: a failing
test is the normal reason to be here.

## Reading a Timeline

Each Step reports two things, and the distinction is the entire point:

- **`owned`** — traffic that Step _caused_. The request started while it was running.
- **`overlapping`** — traffic that was _already in flight_ when the Step began.

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
be satisfied by the _previous_ login. A timeout is a result, not an error — `timed_out: true`
means it never happened.

One limit worth knowing: `run_test` blocks, so you cannot interleave `start_step` with it. Use one
or the other per Run. If you mark Steps after `run_test` returns they fall outside that Run's
window, and `get_timeline` returns a `note` explaining it rather than an unexplained empty result.

## When it looks like there is no traffic

**Call `diagnose_capture` before concluding anything.** Six verdicts, and everything but `ok`
presents identically as "no traffic". This is the only tool that tells them apart:

| Verdict                | Means                                                                                                                                                         |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `proxy_held_elsewhere` | Another https-proxy-mcp process still running holds the Mac's proxy, so the machine's traffic is going to _its_ capture port. Says which pid, and the remedy. |
| `no_connect`           | Nothing reached the proxy. Routing is not in place, the simulator is not booted, or the app has not made a request yet.                                       |
| `untrusted_authority`  | The device does not trust the interception certificate, so TLS fails before a request exists. Fix with `install_authority`.                                   |
| `tls_failed`           | Handshakes failing while the certificate is trusted. Certificate pinning is the leading candidate, but the count is machine-wide: another simulator or a browser on this Mac reaching an in-scope host fails identically. |
| `out_of_scope`         | Traffic was captured and your `scope` or the builtin denylist reported none of it.                                                                            |
| `ok`                   | Exchanges are arriving.                                                                                                                                       |

`tls_failed` is only reported when you set a `scope` on `start_capture`, and that is deliberate
rather than broken. Redirecting the proxy redirects the **whole Mac**, so every other app on the
machine is proxied too and fails its handshake — it has no reason to trust a certificate installed
only into the simulator. Without a scope naming the app's hosts there is no evidence, only noise.
Set `scope=["api.example.com"]` and the count is at least about the right hosts.

A Scope narrows **hosts, not clients**. Another simulator or a browser on this Mac talking to a host
you named is counted too — one such request flips a healthy capture to `tls_failed`. So read the
verdict as "something on this machine failed", not "your app is pinned": narrow the scope, or quit
the other clients, and see whether the count stops rising.

`no_connect` and `out_of_scope` say one more thing when `observed.passthrough` is true, and it
changes what an empty result means:

- An **HTTPS** host outside your `scope` was forwarded without being opened. It was never captured,
  so `wait_for_exchanges` on it will block to its timeout and `get_exchange` has nothing to fetch —
  an empty result is **not** evidence that the app made no request. Restart with a wider `scope`, or
  with `passthrough=false`.
- A **plain HTTP** host outside your `scope` _was_ captured. Passthrough covers TLS only, because the
  proxy must decide whether to open a connection before a plain HTTP request has named its
  destination. So those rows are merely hidden: `get_exchange` fetches them by id.

That distinction is deduced from your `scope`, not measured. Nothing counts a passed-through
connection, deliberately: the proxy option that would allow it keeps every such connection's
contents in memory, which on a machine-wide proxy is the whole machine's traffic.

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
the same marker. So you can answer _"is this the same token as the last request?"_ and _"did the
token change after refresh?"_ without ever seeing the value — which is usually the actual
question. `include_secrets=true` opts out per call; ask for it only when the value itself is what
you are checking.

`export_curl` redacts by default, which means **the default command will not run**. Pass
`include_secrets=true` for one that reproduces the request.

## What this changes on the machine

CoreSimulator has no proxy setting of its own — it inherits the host's. So `start_capture` with
the `ios-simulator` target **redirects your Mac's system proxy**, which affects every application
until `stop_capture`. The prior settings are written to disk before anything changes, restored on
exit, and restored by the next server start if this one is killed outright.

**A `scope` is how you keep that from hurting.** Without one, every HTTPS connection on the machine
is served a certificate signed by an authority that only the one named simulator trusts — so the
developer's browser shows a certificate warning for sites they never asked about, and every other
booted simulator loses network access. With one, and `passthrough` left on, only those hosts are
decrypted and everything else reaches its destination untouched. Prefer naming a `scope`, and say
what you did: `start_capture`'s result reports `passthrough`.

**That works because the other things are on other hosts.** A second simulator running the *same*
app against the *same* backend is inside your `scope` by definition, so nothing above protects it
and nothing above hides it. See the next section — it is a different problem with a different fix,
and the `scope` advice fails silently there.

**Only one process may hold the machine's proxy.** Every editor window runs its own copy of this
server against one shared record of the prior settings, so `start_capture` is **refused** — naming
the holder's pid — while another live process holds it. Nothing here force-releases it, because the
holder's own handlers restore the machine when it is signalled: `kill <pid>` is the remedy and the
message says so. `get_status` reports `proxy.owner` and whether that owner is this process, whether
or not this Session is recording; `owner.ours` is the field to act on, because `stop_capture` puts
back a redirect this process made and nothing else.

Consequences to expect and not report as bugs: with no `scope`, other apps' TLS handshakes fail
against the proxy; and `diagnose_capture` will not attribute pinning without a `scope` (above).

**Always call `stop_capture` when you are done.** It releases the port and puts the proxy back.

The Session directory (`~/.https-proxy-mcp/sessions/`, or `$HTTPS_PROXY_MCP_ROOT`) holds
**unredacted** bodies on local disk, so it is `0700` and pruned to the 10 most recent. Redaction
applies where data leaves — to you, and to `export_har`.

## Two simulators on one Mac, running the same app

The commonest way to get a wrong answer out of this tool. Both simulators are routed through the
one capture proxy, an Exchange records the address it was going **to** and never the one it came
**from**, and `get_timeline` attributes by time. So the other simulator's requests land inside your
Run's window and are reported as traffic your Step caused. It reads as evidence. It is not.

It breaks in the other direction too: the Interception Authority is installed into one device, so
the other simulator's HTTPS to a host in your `scope` fails its handshake and its app stops
working — and that failure is indistinguishable from certificate pinning.

Neither is fixed by narrowing the `scope`, because the hosts are the same.

Fix both, in this order:

1. **`install_authority(udid="...")` for the other device**, so it keeps working. `start_capture`
   and `diagnose_capture` then report `devices_booted`, `devices_trusting_authority` and a
   `devices_note`. That note is null unless a booted device *other than* your Capture Target trusts
   the Authority — when it is not null, that device's bodies are being written unredacted into
   *your* Session directory. Nothing here can look at a body and know what is in it; it can only
   count trust stores and tell you.
2. **Give each build a header naming itself, and set a `lens`.** A request that states which client
   sent it is the only evidence able to separate them:

```
start_capture(target="ios-simulator", udid="...", scope=["api.example.com"],
              lens='~hq "X-Sim: sidekick"')
```

A `lens` is a filter expression — the language `search_exchanges` takes — set **once on the
Session**, so `get_timeline`, every listing and `wait_for_exchanges` all obey it. That is the point:
a narrowing you have to remember per call is the one you forget on the call that mattered.

- `wait_for_exchanges` is exempt from the denylist and the `scope`, and is **not** exempt from the
  `lens`. A wait that ignored it is answered by the other simulator's login.
- `get_exchange` is exempt from all of them. An id came from somewhere.
- A `lens` cannot narrow the **TLS failure count** or a **pending** request. Neither is an
  Exchange, so there is nothing to test the expression against, and `tls_failed` stays as
  machine-wide as it already says.
- **Quote a value with a space in it.** `~hq X-Sim: sidekick` parses and then matches *nothing*,
  hiding the entire Session — which looks exactly like a capture that recorded nothing.
  `~hq "X-Sim: sidekick"` is the form that works.
- Polarity is your call. Naming your own simulator excludes anything unheadered, including an SDK
  that does not use the app's HTTP client; excluding the *other* one keeps it. The first risks
  losing the cause, the second risks importing one. A tight `scope` makes the difference small.
- A bad expression is refused by `start_capture`, before anything on the machine is changed.

## The rest of the surface

| Tool                       | When                                                                                                                     |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| `get_status`               | Is capture running, how many Exchanges, is the proxy redirected and by which process                                     |
| `stop_capture`             | Always, when done                                                                                                        |
| `list_runs`                | Find a `run_id` when more than one test has run                                                                          |
| `tail_exchanges`           | Watch traffic arrive; pass back `next_cursor`. To _block_, use `wait_for_exchanges`                                      |
| `export_har`               | A HAR for a browser or a bug report. Redacted by default; whole Session unless you pass `run_id`                         |
| `install_authority`        | Only after `diagnose_capture` says `untrusted_authority` — `start_capture` does it for you                               |
| `get_trust_status`         | Ask whether a device already trusts the certificate, changing nothing                                                    |
| `install_authority_system` | Machine-wide trust, needs `sudo`, never implicit. Only for a macOS app or desktop browser — a simulator does not need it |

## Running the server

Not on PyPI. Point your MCP client at a checkout or a built wheel:

```jsonc
{
  "mcpServers": {
    "https-proxy-mcp": {
      "command": "uvx",
      "args": ["--from", "/path/to/https-proxy-mcp", "https-proxy-mcp"],
    },
  },
}
```

`uv build` produces a wheel you can point `--from` at instead. macOS only — capture routes traffic
with `networksetup` and installs trust with `simctl`. Requires Python ≥ 3.12, which `uv` provides.

To use this skill on another machine, copy this one file to
`~/.claude/skills/https-proxy-mcp/SKILL.md`. It depends on nothing else.
