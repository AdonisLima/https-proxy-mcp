# Usage

What to do with the twenty tools, and how to read what they return.

Every output below is **real** — captured from a live Session against a local fixture server, not
composed for the guide. Timings and ports vary; shapes do not.

For getting it running, see [INSTALL.md](./INSTALL.md). For the domain words used precisely
throughout — Exchange, Step, Run, Scope, Timeline — see [CONTEXT.md](../CONTEXT.md).

- [The three calls that matter](#the-three-calls-that-matter)
- [Reading a Timeline](#reading-a-timeline)
- [Without a test runner](#without-a-test-runner)
- [Inspecting traffic](#inspecting-traffic)
- [Waiting for a request](#waiting-for-a-request)
- [Cutting the noise](#cutting-the-noise)
- [Watching the capture yourself](#watching-the-capture-yourself)
- [When nothing appears](#when-nothing-appears)
- [Secrets](#secrets)
- [Exporting](#exporting)
- [Tool reference](#tool-reference)
- [Recipes](#recipes)

## The three calls that matter

```
start_capture(target="ios-simulator", udid="F48AAE9F-…", scope=["api.example.com"])
run_test(command="maestro test login.yaml")
get_timeline()
```

Everything else in the surface is drill-down or diagnosis. `get_timeline` is where the answer is —
do not stop at `run_test`'s summary, and do not start paging `list_exchanges` before reading the
Timeline.

`start_capture` returns the port it bound and what it arranged:

```json
{
  "recording": true,
  "session": "20260805T232937-174020",
  "port": 53387,
  "host": "127.0.0.1",
  "target": "curl",
  "scope": null,
  "passthrough": false,
  "lens": null,
  "web_url": null,
  "session_dir": "…/sessions/20260805T232937-174020",
  "trust": { "trusted": true, "detail": "nothing to trust: curl is pointed at the proxy with -x, …" },
  "devices_booted": 0,
  "devices_trusting_authority": 0,
  "devices_note": null
}
```

`devices_note` is non-null only when a booted device *other than* this Session's Capture Target
trusts the Interception Authority. That device's traffic to in-scope hosts is decrypted into this
Session too, and nothing at the proxy distinguishes it — see [A `lens`](#a-lens-when-the-host-cannot-tell-them-apart).

Pass `port=0` to have one chosen and reported back, which is the right default when you do not care.

With `target="curl"` nothing is routed or trusted for you — that is what makes it the target for
trying the surface out with no simulator involved. Send traffic yourself, and note that an `https://`
URL needs curl to trust the authority (`--cacert ~/.https-proxy-mcp/mitmproxy/mitmproxy-ca-cert.pem`,
or `-k`) or it fails with `curl: (60)` before the proxy sees a request:

```bash
curl -x http://127.0.0.1:<port> --noproxy "" http://example.com
```

`run_test` spawns your test command, and the Run is **exactly** the interval from spawn to exit. No
inference, no matching heuristic — which is also why Step and Exchange times are directly
comparable, with no clock skew to correct: both clocks are the host's.

For `maestro test …` the output flags are set for you and pointed at that Run's own artifacts
directory, so Maestro's Steps are found automatically. Any other command works too. A non-zero exit
is a **result**, not an error — a failing test is the normal reason to be here.

`timeout_ms` kills the whole process group, which matters because Maestro launches a JVM: killing
only the wrapper leaves it holding the simulator and the port, and the *next* Run then fails for a
reason that looks entirely unrelated.

## Reading a Timeline

Each Step reports two things, and the distinction is the whole point of the tool:

- **`owned`** — traffic that Step *caused*. The request started while it was running.
- **`overlapping`** — traffic that was *already in flight* when the Step began.

Here is a real Timeline for a login that failed. One request took two seconds; the tap started it,
and the assertion began while it was still outstanding:

```json
{
  "steps": [
    {
      "label": "tapOn: Login",
      "status": "passed",
      "t_start": 1785983377.321,
      "duration_ms": 402.756,
      "owned": [
        { "id": 1, "t_start": 1785983377.328, "duration_ms": 2005.382,
          "method": "GET", "host": "127.0.0.1", "path": "/me", "status": 200 }
      ]
    },
    {
      "label": "assertVisible: \"Home\"",
      "status": "failed",
      "t_start": 1785983377.724,
      "duration_ms": 1614.002,
      "error": "Element not found: Home",
      "overlapping": [
        { "id": 1, "t_start": 1785983377.328, "duration_ms": 2005.382,
          "method": "GET", "host": "127.0.0.1", "path": "/me", "status": 200 }
      ],
      "explanation": "assertVisible: \"Home\" began while #1 GET 127.0.0.1/me (200) was still in flight"
    }
  ],
  "background": { "count": 0, "hosts": [] },
  "pending": [],
  "counts": { "steps": 2, "exchanges": 1, "background": 0, "pending": 0, "hidden": 0 }
}
```

**`explanation` on a failing Step is the diagnosis.** Read it first. A flat one-Step-per-Exchange
model would have reported either "the tap made a request" or "the assert made a request", and both
lose the sentence that actually explains the failure.

Note that Exchange #1 appears **twice** — owned by Step 1, overlapping Step 2. That is deliberate,
not double-counting.

The rest of the payload:

| Field | Means |
|---|---|
| `run` | Which Run this is, its exit code, whether it passed. `id: "session"` means the whole Session, because no test has been run. |
| `window` | The interval the Timeline covers. `end: null` while capture is still running. |
| `background` | Traffic belonging to no Step — polling, telemetry. Counted and named by host always; listed only with `include_background=true`. |
| `pending` | Requests still open, with a **null id**, because ids are assigned on completion. Two of these when a test timed out is often the whole answer. |
| `counts.hidden` | Rows the denylist or your Scope removed. A view that silently drops rows reads as "there was no traffic". |

`get_timeline()` with no argument means the **most recent Run**, or the whole Session if no test has
run. `verbosity="full"` adds body sizes and lists Background. `host_filter="api.example.com"`
narrows to one host and its subdomains — as a *further* narrowing, so a Scope you set on the Session
still holds.

Ordering caveat worth knowing: **Exchange ids are ordered by completion, not by start.** A slow
request that began first can carry a higher id. Time questions use `t_start`.

### A Timeline that comes back empty

If the Session has traffic but the Timeline does not, you get a `note` explaining why rather than an
unexplained empty result. The usual cause: `run_test` blocks, so Steps you mark *after* it returns
fall outside that Run's window. Use `run_test` **or** explicit Steps per Run, not both.

## Without a test runner

No test command to wrap, or a runner that is not Maestro? Mark the Steps yourself. This is the
universal Event Source — no adapter, no artifact parsing, and it produces the identical Timeline
above:

```
start_step("tapOn: Login")
  … drive the app …
end_step(status="passed")

start_step('assertVisible: "Home"')
  … check the screen …
end_step(status="failed", error="Element not found: Home")
```

`status` is `"passed"`, `"failed"` or `"skipped"`. `start_step` **fails if a Step is already open** —
closing one implicitly would have to invent an outcome, and the outcome is the entire reason for
recording it. `get_status` reports `open_step` if you lose track.

## Inspecting traffic

The API enforces progressive disclosure rather than asking for it: **`list_exchanges` has no
parameter that could return a body.** Work inward.

### 1. List

```json
{
  "exchanges": [
    { "id": 1, "t_start": 1785983377.328, "duration_ms": 2005.382,
      "method": "GET", "host": "127.0.0.1", "path": "/me", "status": 200,
      "request_bytes": 0, "response_bytes": 70 }
  ],
  "returned": 1, "matched": 1, "hidden": 0, "limit": 50,
  "next_cursor": null, "evicted": 0, "recorded": 1
}
```

Narrow with `host` (exact, case-insensitive), `method`, `status`, or the `since`/`until`
epoch-second window. Page with `cursor` from the previous response; a `next_cursor` of `null` means
that was the end.

`evicted` and `hidden` are always reported, because *"there is nothing older"* and *"there was
something older and it is gone"* are different answers and only one means stop looking.

### 2. Get one

```json
{
  "id": 1, "method": "GET", "url": "http://127.0.0.1:53428/me", "status": 200,
  "duration_ms": 2003.185, "http_version": "HTTP/1.1",
  "request_headers": [["Host", "127.0.0.1:53428"], ["User-Agent", "curl/8.7.1"]],
  "response_headers": [["Content-Type", "application/json"], ["Content-Length", "70"]],
  "request_body": { "size": 0, "shown_bytes": 0, "truncated": false, "encoding": "text", "preview": "" },
  "response_body": {
    "size": 68, "shown_bytes": 68, "truncated": false, "encoding": "text",
    "preview": "{\"user\":{\"id\":42,\"name\":\"Ada\"},\"token\":\"<redacted:sha256:04d5a59f>\"}"
  }
}
```

The first 2048 bytes of each body, with an explicit truncation marker. Raise `max_body_bytes` if you
must, but prefer step 3.

### 3. Slice a body

```
get_exchange_body(exchange_id=1, side="response", offset=0, length=65536)
```

`side` is `"request"` or `"response"`. Keep reading while `has_more` is true, advancing `offset` by
`length`. A body that is not text comes back base64-encoded, flagged by `encoding`.

**Offsets index the redacted body**, so they stay consistent across a paged read — and `total` is
the redacted length. Redaction runs before truncation deliberately: cutting first would leave the
tail of a straddling secret visible.

Two more shapes worth expecting. A body with nothing sensitive in it comes back as its **original
bytes**, not re-serialized — so what you see matches what the app actually sent, byte for byte. And
a binary body is **described** in a preview rather than base64'd into it, because 2 KB of base64 is
noise nobody can read; ask for the bytes explicitly when you want them.

### Searching

`search_exchanges` takes **mitmproxy's own filter expression language**, not a new one:

| Operator | Matches |
|---|---|
| `~u /auth` | URL |
| `~m POST` | method |
| `~c 401` | status code |
| `~d api.example.com` | domain |
| `~h Authorization` | header |
| `~b` / `~bq` / `~bs` | body — either / request / response |
| `&` `\|` `!` | and / or / not |

```
search_exchanges(filter="~d api.example.com & ~c 401")
search_exchanges(filter="~m POST & ~u /token")
```

Body operators read from disk, so prefer a metadata filter when one will do. An invalid expression
is reported as an error you can fix, even when there is no traffic to test it against.

## Waiting for a request

This is what makes end-to-end use practical — *"tap Login, then wait for `POST /auth`"* is one call,
not a polling loop:

```
wait_for_exchanges(filter="~u /auth & ~m POST", timeout_ms=15000, min_count=1)
```

**Only traffic recorded after the call counts.** An identical request a moment earlier will not
satisfy the wait, because otherwise "wait for the login request" would be answered by the *previous*
login and the tool would be useless.

A timeout is a **result**, not an error: `timed_out: true` and the answer is that it never happened.
Ceiling is 120 s.

`tail_exchanges` is the other half — everything since a cursor, for watching traffic as it arrives.
Unlike `list_exchanges` its `next_cursor` is always usable, so an empty result hands back the cursor
you gave it. To *block* until something specific shows up, use `wait_for_exchanges` rather than
looping on this.

## Cutting the noise

Reporting is narrowed, in this order:

1. **The Run window.** Anything outside it cannot pollute that Run's Timeline. Narrowed by
   intersection, not by start time, so a request already in flight when the Run began is included —
   which is the case the whole overlap model exists for.
2. **A builtin denylist** of OS and platform chatter. Exactly: `apple.com`, `icloud.com`,
   `cdn-apple.com`, `mzstatic.com`, `apple-dns.net`, `itunes.com`, `gstatic.com`,
   `crashlytics.com`, `app-measurement.com`, `sentry.io`, `bugsnag.com`, `datadoghq.com`,
   `newrelic.com`, plus any host whose first label is `ocsp` or `crl`. Suffixes match on label
   boundaries, so `apple.com` covers `gs-loc.apple.com` but not `notapple.com`. Deliberately
   conservative — `googleapis.com` is **not** denied, because for many apps that is the backend.
3. **Your `scope`**, if you set one.
4. **Your `lens`**, if you set one — a filter expression, applied to everything else.

`include_denied=true` on any listing puts the **denylist's** rows back. It does not lift your
`scope`, which is what you asked for rather than what this tool assumed — widen that by starting a
new Session. `hidden` always says how many rows the two removed together.

Two tools are **exempt** from the denylist and Scope: `wait_for_exchanges` and `get_exchange`. Both
are explicit — a filter naming a host, or an id that came from somewhere — and letting the denylist
veto a wait would turn "wait for this request" into a timeout reporting that it never happened.
The Lens is the exception: `wait_for_exchanges` **does** obey it, and only `get_exchange` is exempt
from all four. See below for why.

### A `lens`: when the host cannot tell them apart

A `scope` separates traffic by **host**. Sometimes there is no host that separates it. The case
that forces this: **two iOS Simulators booted on one Mac, running the same app against the same
backend.** CoreSimulator has no proxy setting of its own, so both are routed through the one
capture proxy; an Exchange records the address it was *going to*, never the one it came *from*; and
`get_timeline` attributes by time. So the other simulator's requests appear inside your Run's
window and are reported as your Step's overlapping traffic — a causal claim about a request you
did not make.

Neither `scope` nor `passthrough` helps, because both narrow by host and the hosts are the same.

A `lens` does, by not inferring anything. Have each build stamp a header on its own requests, and
name it once:

```
start_capture(target="ios-simulator", udid="F48AAE9F-…", scope=["api.example.com"],
              lens='~hq "X-Sim: sidekick"')
```

**Quote any value with a space in it.** `~hq X-Sim: sidekick` unquoted still *parses*, and then
matches nothing — a Lens that silently hides everything is much worse than one that is refused.

- It is set **on the Session**, not per call, so `get_timeline`, `list_exchanges`,
  `search_exchanges`, `tail_exchanges` and `wait_for_exchanges` all obey it. A narrowing you have
  to remember on each call is the one you forget on the call that mattered.
- `wait_for_exchanges` is **not** exempt from it, though it is exempt from the Scope. The
  exemption's reasoning is that naming a host in a filter is explicit and should not be vetoed;
  a Lens says which *client* the traffic came from, and a wait that ignored it is answered by the
  other simulator's login.
- `get_exchange` **is** exempt. An id came from somewhere.
- It cannot narrow a **TLS handshake failure count** or a **pending** request. Neither is an
  Exchange, so there is nothing to test the expression against. The `tls_failed` count stays
  machine-wide, exactly as `diagnose_capture` says.
- Choose the polarity deliberately. `~hq "X-Sim: sidekick"` **excludes** anything unheadered —
  traffic from an SDK that does not use your HTTP client. `!(~hq "X-Sim: main")` keeps it. The first
  risks missing a cause; the second risks importing one. A tight `scope` shrinks the difference to
  near nothing, because unheadered third-party traffic is usually to a host you never named.
- A bad expression is refused by `start_capture`, before anything on the machine is changed.

The other half of that setup is trust: the Interception Authority is installed into **one** device,
so the other simulator's HTTPS to an in-scope host fails its handshake and its app breaks. Install
it there too with `install_authority(udid=…)`. `start_capture` and `diagnose_capture` then report
`devices_trusting_authority`, because the cost of doing so — that device's bodies land unredacted
in this Session's directory — is otherwise invisible.

### What a `scope` narrows besides reporting

With `passthrough` left on, a `scope` also decides which hosts get **decrypted**, so this section's
narrowings are no longer the whole story:

- An **HTTPS** host outside the `scope` was forwarded without being opened. It is not hidden, it was
  never captured, and `get_exchange` cannot fetch what does not exist. That is the accepted cost of
  leaving the developer's browser and every other simulator alone, and `diagnose_capture` says so
  when a listing comes back empty. Recover by restarting with a wider `scope`, or with
  `passthrough=false`.
- A **plain HTTP** host outside the `scope` was captured anyway. Passthrough covers TLS only: the
  proxy has to decide whether to open a connection before a plain HTTP request has named its
  destination, and only a `CONNECT` authority and a TLS SNI arrive early enough to test. So those
  rows are hidden by the `scope` in the ordinary way and `get_exchange` still fetches them by id.

The distinction matters because the two look identical in a listing and have opposite remedies:
one needs a new Session, the other needs an id.

## Watching the capture yourself

Everything above is the model's view: summary-first, redacted, narrowed by design. Sometimes the
human wants to watch the capture directly — scroll the requests, open a body, follow along while
an agent drives the app. Pass `web_ui=True` and the Session's own proxy core serves the **Human
View** — the mitmweb UI, live, on loopback — from the same core that records into the Session:

```
start_capture(target="ios-simulator", udid="…", scope=["api.example.com"], web_ui=True)
```

The response carries the URL, gated by a per-Session token, as `web_url`
(`http://127.0.0.1:<port>/?token=…`). Open it in a browser and requests appear as they happen —
what you scroll and what a Timeline says are two readings of one event stream. `web_port=0` (the
default) picks a free port and the response says which, so two Sessions in a row never collide;
pin `web_port` when a bookmarked URL should survive across Sessions. An occupied `web_port` fails
the start cleanly — no half-started proxy is left holding the capture port. `get_status` carries
`web_url` while recording, so the URL is recoverable after the start response is lost, and the
view dies with `stop_capture`.

Two things the view is **not**, both deliberate:

- **It is the core's view, not the tool surface's.** No denylist, `scope`, `lens` or Run window
  narrows it — out-of-`scope` plain HTTP that the listings hide is visible there — and bodies are
  unredacted. That is not a hole in the redaction boundary: the boundary is the model and export
  surface, and the bodies already sit unredacted in the Session's own directory. It is what lets
  you answer "is my app sending the right token?" yourself, the question the redacted surface
  deliberately cannot.
- **It is a write console.** Intercept, modify, replay — the pinned mitmproxy has no read-only
  web mode, so the console is accepted and documented rather than locked down. Edits made there
  can make a Timeline lie and can break teardown: a Session you edited by hand may stop cleanly
  but read wrong.

## When nothing appears

**Call `diagnose_capture` before concluding there was no traffic.** Six verdicts, and everything but
`ok` presents identically from the outside. This is the only tool that tells them apart:

| Verdict | Means | Do |
|---|---|---|
| `proxy_held_elsewhere` | The Mac's proxy is redirected by **another** https-proxy-mcp process that is still running, so the machine's traffic is going to its capture port | `kill <pid>` the process it names — that makes it restore the machine on its way out, after which this refusal no longer applies. `start_capture` still needs a booted device and a trusted authority, and says so if either is missing |
| `no_connect` | Nothing reached the proxy | Check routing, that the simulator is booted, or that the app has made a request at all |
| `untrusted_authority` | The device does not trust the authority, so TLS fails before a request exists | `install_authority` |
| `tls_failed` | Handshakes failing while the authority is trusted. The count is **machine-wide**: every client reaches the proxy over loopback, so it cannot say which one failed | Certificate pinning is the leading candidate, and a correctly-pinned release build is out of scope. Rule out the cheaper cause first — another simulator or a browser on this Mac reaching an in-scope host fails identically, because only the capture target was given the Authority |
| `out_of_scope` | Traffic was captured; your Scope or the denylist reported none of it | Widen `scope` for a new Session, or pass `include_denied=true` for the denylist's rows |
| `ok` | Exchanges are arriving | — |

It shows the counts the verdict came from, so the reasoning can be checked rather than taken on
faith:

```json
{
  "status": "ok",
  "detail": "curl needs no routing or trust setup; if no Exchanges are appearing, check that curl was given -x http://127.0.0.1:<port>",
  "remediation": null,
  "target": "curl",
  "observed": { "exchanges": 1, "reported": 1, "tls_failures": 0, "tls_failures_total": 0, "tls_failure_hosts": [], "passthrough": false }
}
```

Every verdict answers with those same keys. When nothing is recording, `target` is `null` and the
counts are zero — which is worth knowing because `proxy_held_elsewhere` is normally met in exactly
that state, having arrived from a refused `start_capture`.

`no_connect` and `out_of_scope` say one more thing when `observed.passthrough` is true: only the
Scope's hosts had their HTTPS decrypted, so an HTTPS connection to any other host was forwarded
without being opened and could never have been recorded — an empty result is **not** evidence that
the app made no request. That is deduced from your `scope`, not measured: nothing counts a
passed-through connection, deliberately. To capture such a host over HTTPS, restart with a Scope
that names it, or with `passthrough=false`. Plain HTTP is the exception the same sentence names: it
is captured from every host, so a plain-HTTP row is hidden rather than missing and `get_exchange`
fetches it by id.

### Why `tls_failed` needs a Scope

This looks like the headline feature being switched off. It is not.

Redirecting the system proxy redirects the **whole Mac**. The authority is installed into the
simulator alone, so every other application on your machine is proxied too, does not trust it, and
fails its TLS handshake — and in regular proxy mode every connection arrives from localhost, so
those failures are **indistinguishable** from the app under test's.

In a real run with one simulator fetching one URL, the failing hosts included `discord.com`,
`unleash.codeium.com` and a Figma CDN — whatever happened to be open. No denylist can enumerate
that. Counting raw handshake failures reported `tls_failed` permanently, on a capture that was
working perfectly.

So the count is only worth a verdict **inside a Scope**: with one, you have said which hosts are
the app's, so the failures being counted are at least about the right hosts. Without one, unscoped
captures surface the count, note that the failures are unattributable, and name the Scope as the
thing that would make them interpretable.

A Scope narrows **hosts, not clients**, and that limit is real rather than theoretical. Another
simulator, or a browser on this Mac, talking to a host you named is counted too: it arrives over
the same loopback, and it has no reason to trust an authority installed only into the capture
target. One such request is enough to flip a healthy capture to `tls_failed` — reproduced
deliberately on 2026-08-18, against a device that was working perfectly at that moment. That is why
the verdict says the count is machine-wide rather than naming your device. Narrow the Scope, or
quit the other clients, and see whether the count stops rising.

`get_status` answers a different and narrower question — is capture running at all, and how many
Exchanges have been seen:

```json
{
  "recording": true, "session": "20260805T232946-242740", "port": 53430,
  "target": "curl", "scope": null,
  "exchanges": 1, "retained": 1, "evicted": 0, "tls_failures": 0, "pending": 0,
  "runs": [], "open_step": null, "web_url": null,
  "trust": { "trusted": true, "detail": "…" },
  "proxy": { "redirected": false, "detail": "the system proxy is not redirected" }
}
```

### Is my machine still proxied, and by whom?

`proxy` is answered whether or not this Session is recording, because it is read from a record on
disk rather than from Session state. That matters: the question is asked precisely when nothing is
recording here, and every editor window on the machine runs its own copy of this server against the
same record.

```json
{
  "recording": false,
  "detail": "not recording; call start_capture first",
  "sessions_root": "…",
  "proxy": {
    "redirected": true,
    "service": "Wi-Fi",
    "sentinel": "…/proxy-state.json",
    "owner": { "pid": 89858, "ours": false },
    "detail": "Wi-Fi is routed through the capture port by another https-proxy-mcp process (pid 89858) that is still running. Nothing here owns it, so there is nothing to stop here; `kill 89858` makes that process restore the machine on its way out"
  }
}
```

`owner.ours` is the field to act on. `stop_capture` puts back a redirect **this** process made and
nothing else, so for someone else's the remedy is the signal named in `detail` — the holder installs
`SIGTERM`, `SIGINT` and exit handlers, so signalling it makes *it* restore the machine on its way
out. There is deliberately no tool that force-releases another process's redirect: doing that from
here would restore state this process never recorded, which is the disregard for ownership that
caused the defect in the first place. An uncatchable `kill -9` leaves the record behind, and the next
server start repairs it.

Starting a Session while another live process holds the redirect is **refused**, naming that pid,
rather than taken over or waited for. A takeover would leave the other window recording nothing; a
wait would hang this session.

## Secrets

Every secret is replaced with a **stable short digest**, not deleted:

```
"token": "<redacted:sha256:04d5a59f>"
```

The same secret always renders to the same marker. So you can answer *"is this the same token as the
last request?"* and *"did the token change after refresh?"* without the value ever being visible —
which is usually the actual question.

Redacted by default:

- Headers `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`, `X-Api-Key`, `X-Auth-Token`
- Body keys matching `(token|secret|password|api[-_]?key|refresh|access_token|id_token)`, at any
  depth in JSON and in form-encoded bodies
- JWT-shaped values (`eyJ…`) under any key, in any text body

Form-encoded bodies are included for a specific reason: the OAuth 2 password grant is form-encoded
by RFC 6749, so `POST /token` — one of the likeliest requests to point this at — would otherwise
send `password=…` in clear. A body that is form-*shaped* is treated as one even with no
`Content-Type`, because forgetting a header should not be able to cause a leak.

The whole `Authorization` value is digested, **scheme included** — `Bearer` is not preserved, because
partial redaction of an unknown scheme is how partial leaks happen.

Not covered: `multipart/form-data`, whose parts carry their own headers and boundary framing.

`include_secrets=true` opts out per call. Ask for it when the value itself is what you are checking.

**On disk it is different.** The Session directory holds faithful, unredacted bodies, because "is my
app sending the right token?" is a real question and a scrubbed store cannot answer it. Redaction
applies where data *leaves*: into the model, and out of the machine.

## Exporting

### One request as curl

```json
{
  "exchange_id": 1,
  "runnable": false,
  "command": "curl -X GET http://127.0.0.1:53428/me -H 'Host: …' -H 'User-Agent: curl/8.7.1'"
}
```

Redacted by default, which means **the default command will not run** — `runnable` says so. Pass
`include_secrets=true` for one that reproduces the request.

### HAR

```
export_har()                      # the whole Session
export_har(run_id="run-…")        # one Run's window
export_har(redact=False)          # faithful — contains live credentials
```

Redacted by default because the file travels further than a model does, into a ticket or a thread.
The file records which mode produced it, so nobody has to guess later.

Note the asymmetric default: `get_timeline` with no `run_id` means the **latest Run**, `export_har`
with none means the **whole Session**. A Timeline is about a Run; an export is an artifact of
everything captured, and an `export_har` that silently held only the last few milliseconds would be
the more expensive surprise.

## Tool reference

Twenty tools. Signatures are the real ones; defaults are shown where they exist.

### Session lifecycle

| Tool | Notes |
|---|---|
| `start_capture(target, port, scope, udid, passthrough, lens, web_ui, web_port)` | `target="curl"`, `port=8080`, `passthrough=True`. `port=0` binds an ephemeral one and reports it. With a `scope`, `passthrough` decrypts only those hosts' HTTPS and forwards every other TLS connection untouched; plain HTTP is captured regardless. `lens` is a filter expression narrowing every later report, including `get_timeline` and `wait_for_exchanges`. `web_ui=True` serves the Human View — mitmweb from the Session's own core, token-gated, unnarrowed and unredacted, a write console; `web_port=0` picks a free port and the response names it in `web_url`. Refused while another live process holds the machine's proxy. Required before any other traffic tool |
| `stop_capture()` | Releases the port and undoes any routing. Always call it |
| `get_status()` | Recording state, counts, Scope, Lens, trust, `open_step`, and `proxy` — which includes the redirect's `owner` and is answered even when not recording |
| `diagnose_capture()` | The six verdicts above, plus the counts they came from and the device census |

### Trust

| Tool | Notes |
|---|---|
| `install_authority(target, udid)` | `target="ios-simulator"`. Called for you by `start_capture`; reach for it after `untrusted_authority`. Device must be booted |
| `get_trust_status(target, udid)` | Asks without arranging. Reads the device's own trust store — the only thing that actually knows |
| `install_authority_system()` | macOS system keychain. Needs `sudo`, machine-wide, never implicit, never prompts |

### Exchanges

| Tool | Notes |
|---|---|
| `list_exchanges(limit, cursor, host, method, status, since, until, include_denied)` | `limit=50` (max 200). Cannot return bodies, by construction |
| `get_exchange(exchange_id, max_body_bytes, include_secrets)` | `max_body_bytes=2048` |
| `get_exchange_body(exchange_id, side, offset, length, include_secrets)` | `side` is `"request"`/`"response"`. `length=65536` |
| `search_exchanges(filter, limit, cursor, include_denied)` | mitmproxy filter expressions |
| `tail_exchanges(cursor, filter, limit, include_denied)` | `limit=50` (max 200). `next_cursor` always usable, so an empty result hands back the cursor you gave it |
| `wait_for_exchanges(filter, timeout_ms, min_count)` | `timeout_ms=15000` (max 120000), `min_count=1`. Counts only new traffic. Exempt from the denylist and Scope; **not** exempt from the Lens |
| `export_curl(exchange_id, include_secrets)` | Redacted default does not run |

### Runs and Timelines

| Tool | Notes |
|---|---|
| `run_test(command, label, cwd, env, timeout_ms)` | `env` adds to the environment rather than replacing it, so `PATH` survives. `timeout_ms` kills the process group |
| `get_timeline(run_id, verbosity, host_filter, include_background)` | `verbosity="summary"` or `"full"`. No `run_id` means the latest Run |
| `list_runs()` | Oldest first, with outcomes. Find a `run_id` |
| `start_step(label)` | Errors if a Step is already open |
| `end_step(status, error)` | `status="passed"`, `"failed"` or `"skipped"` |
| `export_har(run_id, redact, path, host_filter)` | `redact=True`. No `run_id` means the whole Session |

### Defaults and limits

| | |
|---|---|
| Page size, every listing | 50, max 200 — `list_exchanges`, `search_exchanges` and `tail_exchanges` alike. Asking for more is clamped, and the response's `limit` says what you actually got |
| Body preview in `get_exchange` | 2048 bytes, with a truncation marker |
| `get_exchange_body` slice | 64 KB per call |
| `wait_for_exchanges` timeout | 15 s, max 120 s |
| Metadata ring buffer | 5000 Exchanges, oldest evicted, `evicted` always reported |
| Body spill cap | 10 MB per side, then truncated and marked |
| Session retention | 10 most recent, older pruned on start |

## Recipes

**Why did my login test fail?**

```
start_capture(target="ios-simulator", udid="…", scope=["api.example.com"])
run_test(command="maestro test login.yaml")
get_timeline()                              # read `explanation` on the failing Step first
get_exchange(<the id it named>)             # then the request itself
stop_capture()
```

**Is the app sending the right token?**

```
search_exchanges(filter="~h Authorization")
get_exchange(<id>)                          # compare the <redacted:sha256:…> digests across requests
get_exchange(<id>, include_secrets=True)    # only if you need the value itself
```

Two requests showing the same digest carry the same token; a changed digest means it was refreshed.
Usually that is the whole question, and you never need the value.

**The screen is blank and I do not know why.**

```
get_status()                                # is capture even running?
diagnose_capture()                          # if it is, why is nothing arriving?
list_exchanges(status=500)                  # or 401, or whatever you suspect
get_timeline(include_background=True)       # is it polling something that never answers?
```

Also check `pending` in the Timeline. A request that never came back is invisible to
`list_exchanges` — the store records on completion — and is often exactly the answer.

**Tap something and check what it fired.**

```
start_step("tapOn: Checkout")
wait_for_exchanges(filter="~u /orders & ~m POST", timeout_ms=10000)
end_step(status="passed")
```

**Hand a request to a backend engineer.**

```
export_curl(<id>, include_secrets=True)     # they can run this
export_har(run_id="run-…")                  # redacted, safe for a ticket
```
