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
  "session_dir": "…/sessions/20260805T232937-174020",
  "trust": { "trusted": true, "detail": "nothing to trust: curl is pointed at the proxy with -x, …" }
}
```

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

Capture is **unconditional** — everything through the proxy is recorded. Reporting is narrowed, in
this order:

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

`include_denied=true` on any listing shows what was removed; `hidden` always says how many.

Two tools are **exempt** from the denylist and Scope: `wait_for_exchanges` and `get_exchange`. Both
are explicit — a filter naming a host, or an id that came from somewhere — and letting the denylist
veto a wait would turn "wait for this request" into a timeout reporting that it never happened.

## When nothing appears

**Call `diagnose_capture` before concluding there was no traffic.** Four very different causes
present identically from the outside, and this is the only tool that tells them apart:

| Verdict | Means | Do |
|---|---|---|
| `no_connect` | Nothing reached the proxy | Check routing, that the simulator is booted, or that the app has made a request at all |
| `untrusted_authority` | The device does not trust the authority, so TLS fails before a request exists | `install_authority` |
| `tls_failed` | Handshakes failing *even though* the authority is trusted | Certificate pinning, not a setup mistake. Pinned release builds are out of scope |
| `out_of_scope` | Traffic was captured; your Scope or the denylist reported none of it | Widen `scope`, or pass `include_denied=true` |
| `ok` | Exchanges are arriving | — |

It shows the counts the verdict came from, so the reasoning can be checked rather than taken on
faith:

```json
{
  "status": "ok",
  "detail": "curl needs no routing or trust setup; if no Exchanges are appearing, check that curl was given -x http://127.0.0.1:<port>",
  "remediation": null,
  "target": "curl",
  "observed": { "exchanges": 1, "reported": 1, "tls_failures": 0, "tls_failures_total": 0, "tls_failure_hosts": [] }
}
```

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

So pinning is only attributable **inside a Scope**: with one, you have said which hosts are the app.
Without one, unscoped captures surface the count, note that the failures are unattributable, and
name the Scope as the thing that would make them interpretable.

`get_status` answers a different and narrower question — is capture running at all, and how many
Exchanges have been seen:

```json
{
  "recording": true, "session": "20260805T232946-242740", "port": 53430,
  "target": "curl", "scope": null,
  "exchanges": 1, "retained": 1, "evicted": 0, "tls_failures": 0, "pending": 0,
  "runs": [], "open_step": null,
  "trust": { "trusted": true, "detail": "…" },
  "proxy": { "redirected": false, "detail": "the system proxy is not redirected" }
}
```

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
| `start_capture(target, port, scope, udid)` | `target="curl"`, `port=8080`. `port=0` binds an ephemeral one and reports it. Required before any other traffic tool |
| `stop_capture()` | Releases the port and undoes any routing. Always call it |
| `get_status()` | Recording state, counts, Scope, proxy state, trust, `open_step` |
| `diagnose_capture()` | The five verdicts above, plus the counts they came from |

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
| `wait_for_exchanges(filter, timeout_ms, min_count)` | `timeout_ms=15000` (max 120000), `min_count=1`. Counts only new traffic |
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
