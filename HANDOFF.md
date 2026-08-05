# Handoff — 2026-08-05 (P3 complete)

Picking this up in a fresh session. Everything needed is in the repo; this file says where to start and what not to relitigate.

## State

**P0, P1, P2 and P3 complete and committed.** On `main`, clean tree, no remote configured.

Verify in one command: `uv run pytest -q && uv run ruff check .` → 465 passed, 2 skipped, all checks passed.

The 2 skipped are the live simulator acceptance tests. They are the only thing in the project that changes the developer's machine, so they are gated:

```bash
HTTPS_PROXY_MCP_E2E=1 uv run pytest tests/test_simulator_e2e.py -q     # ~60s, creates its own simulator
```

62 tests are marked `integration` — they bind sockets, spawn `mitmdump`, `curl` and a real MCP server subprocess, and `kill -9` one. `uv run pytest -m "not integration"` runs the other 390 in about five seconds.

Try it by hand:

```bash
uv run https-proxy-mcp          # speaks MCP over stdio
```

## Read these first, in this order

1. **[CONTEXT.md](./CONTEXT.md)** — the domain language. Use these words exactly; they were chosen to dodge a real collision (mitmproxy's "flow" vs Maestro's "flow"). If you find yourself writing "flow", stop. The exceptions are `capture/addon.py` and `capture/filtering.py`, where `flow` names mitmproxy's own object at the boundary.
2. **[SPEC.md](./SPEC.md)** — the v1 spec. Tool surface, semantics, numeric defaults.
3. **[PLAN.md](./PLAN.md)** — six phases with acceptance criteria. You are starting P4.
4. **[docs/adr/](./docs/adr/)** — five decisions and why. Two deviate from the research that preceded the project, so read them before "fixing" anything that looks wrong.

## What exists now

| Module | Contents |
|---|---|
| `capture/exchange.py` | `CapturedExchange` (what the addon saw) and `Exchange` (what the store kept), `BodyRef`, NDJSON round-trip |
| `capture/store.py` | `ExchangeStore` — ring buffer, eviction counter, body spill and read-back, append-only log, `0700`, TLS-failure counter |
| `capture/addon.py` | `CaptureAddon` — the only module that knows mitmproxy's flow shapes |
| `capture/session.py` | Session identity, layout, `$HTTPS_PROXY_MCP_ROOT`, retention to 10 |
| `capture/runtime.py` | `ProxyRuntime` — mitmproxy in this process on the server's event loop |
| `capture/filtering.py` | mitmproxy filter expressions applied to Exchanges |
| `capture/reporting.py` | The builtin denylist and the Scope — what is *reported*, never what is captured |
| `serialize/redact.py` | Headers, JSON keys, form keys and JWTs → `<redacted:sha256:xxxxxxxx>` |
| `serialize/views.py` | Summaries, previews, slices, pagination, curl rendering |
| `targets/systemproxy.py` | The Mac's proxy settings, and the sentinel protocol that guarantees they come back |
| `targets/simctl.py` | CoreSimulator: which device, install the authority, and *verify* trust |
| `targets/ios_simulator.py` | The iOS Simulator Capture Target, including the five-way diagnosis |
| `targets/` | `CaptureTarget` protocol, `TrustStatus`, `Diagnosis`, `CaptureObservations`, `CurlTarget` |
| `server/` | `CaptureState` plus the fourteen registered tools |

## Next: P4 — Runs and Timelines

`run_test` process wrapper. `start_step` / `end_step` as the universal Event Source. Maestro Event Source parsing `commands-*.json`, with both output flags pointed at one directory. Correlator implementing owner-plus-overlap. `get_timeline` with summary-first rendering. `export_har`, redacted by default.

**Acceptance:** a deliberately failing Maestro flow produces a Timeline showing the failing assertion *and* the Exchange still in flight when it began — the worked example from SPEC.md, reproduced for real.

This is the phase that delivers the reason the project exists. Everything before it is plumbing.

What P3 leaves you:

- **`ReportingFilter` is the "narrow what is reported" mechanism**, and the Run window is meant to join it. SPEC.md orders the narrowings: Run window first, then denylist, then Scope. The first is the one still missing, and it belongs in `capture/reporting.py` next to the other two rather than in the Timeline code.
- **`CaptureObservations` is how the core tells a target what it cannot see.** If P4 needs to hand the correlator something similar, follow the same shape — a frozen dataclass passed in, not a reach-back into state.
- **`_page()` in `server/exchange_tools.py` applies narrowing for every listing**, so a Timeline that reuses it inherits the denylist and Scope for free. If `get_timeline` does its own assembly, it needs to apply `state.reporting()` deliberately.
- **`store.tls_failures` records handshakes that never became Exchanges.** A Timeline may want them: "the assertion failed and the connection it needed never completed a handshake" is exactly the kind of sentence this project exists to produce.

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

Added by P1 and P2:

| Decision | Why it looks odd |
|---|---|
| Exchange ids are ordered by **completion**, not by start | A slow request that started first can carry a higher id. That is the right ordering for cursor-based tailing ("what finished since I last looked"); time questions use `t_start` explicitly and never lean on id order. |
| The whole `Authorization` value is digested, scheme included | `Bearer` is not preserved. Partial redaction of an unknown scheme is how partial leaks happen. |
| `ExchangeStore` has no lock | It assumes the addon and the tool surface share one event loop, which is what ADR-0001's single process buys. Moving the proxy to its own thread means adding a lock here, deliberately. |
| Bodies are stored **decoded**, not as they were on the wire | A gzipped blob on disk answers no debugging question. `get_content(strict=False)` so one malformed `Content-Encoding` cannot end the Session. |
| Redaction runs **before** truncation and slicing | Cutting first leaves the tail of a straddling secret visible. So `get_exchange_body` offsets index the *redacted* body, and `total` is its length. |
| A body with nothing sensitive comes back as its **original bytes** | Re-serializing unchanged JSON would silently reformat every body a model sees, so it would no longer match what the app sent. |
| A binary body is **described** in a preview, not base64'd | 2 KB of base64 is noise nobody can read. `get_exchange_body` still returns the bytes when asked. |
| `export_curl` redacts by default, so the default **does not run** | Consistent with every other boundary. Reproducing a request is an explicit `include_secrets` opt-in. |
| `wait_for_exchanges` counts only traffic recorded **after** the call | Otherwise "tap Login, then wait for `POST /auth`" is satisfied by the previous login, which makes the tool useless. |
| Exchanges are rebuilt into throwaway mitmproxy flows to be filtered | SPEC.md chose mitmproxy's filter language; its matchers want a flow and the store holds Exchanges. Bodies are read only when the expression mentions one. |
| Session ids are microsecond timestamps | Lexicographic order is chronological, so retention opens no files — a Session with a corrupt manifest still prunes correctly. |

Added by P3:

| Decision | Why it looks odd |
|---|---|
| **`diagnose_capture` will not report `tls_failed` without a Scope** | It looks like the headline feature has been switched off. It has not: an unscoped verdict is *worthless*. See "the finding that shaped P3" below. |
| The service to redirect comes from `scutil`, not from `-listnetworkserviceorder` | Ordering looks like the obvious source. On this machine it ranks Thunderbolt Bridge first while the default route goes out `en0` — so ordering proxies an interface carrying no traffic and reports success. |
| `redirect()` **refuses** to run when a sentinel already exists | Looks unhelpfully strict. Overwriting would record the already-proxied state as the state to return to, losing the machine's real configuration for good and making every later restore a no-op that leaves it proxied. `prepare()` repairs first. |
| A corrupt sentinel switches both proxies **off** rather than giving up | Losing fidelity looks worse than doing nothing. It is not: the prior state is already gone, and leaving traffic pointed at a dead proxy reads to the developer as "the network is broken". |
| Trust is **verified** against the device's trust store, not inferred from the install | `simctl` exiting zero means it accepted the file, not that the device trusts it. The store is the only thing that knows. |
| `get_trust_status` does not install anything | It duplicates `install_authority` minus the install, which looks redundant. A question that changes what it asks about is a trap: an agent checking whether a device was *already* trusted would silently arrange trust and never find out. |
| `install_authority_system` uses `sudo -n` and fails rather than prompting | Refusing to do the thing looks unhelpful. See the traps list — an interactive prompt would hang the session permanently. |
| `wait_for_exchanges` and `get_exchange` are **exempt** from the denylist and Scope | Inconsistent with the listings. Both are explicit: a filter naming a host, or an id that came from somewhere. Letting the denylist veto a wait turns "wait for this request" into a timeout reporting that it never happened. |
| Session bodies live under `tmp_path` in every test, never the real home | Belt and braces on top of `$HTTPS_PROXY_MCP_ROOT`. Those directories hold real credentials. |

## The finding that shaped P3

Worth its own section, because it is a property of the domain rather than a detail of the code, and because the obvious "improvement" is to undo it.

Redirecting the system proxy redirects **the whole machine**. The Interception Authority is installed into the simulator's trust store alone. So every other application on the developer's Mac is proxied too, does not trust the authority, and fails its TLS handshake — and in regular proxy mode every connection arrives from localhost, so those failures are indistinguishable from the app under test's (ADR-0002).

The live P3 run made this concrete. With one simulator fetching one URL, the failing hosts were:

```
discord.com
s3-figma-force-client-reloads-production.figma.com
unleash.codeium.com
configuration.apple.com, wps.apple.com, gateway.icloud.com, gdmf.apple.com, mesu.apple.com
```

Only the last group is OS chatter the denylist can name. The first three are whatever the developer happens to have open, and no denylist can enumerate that.

Consequences, all of them load-bearing:

- **Counting raw handshake failures made `diagnose_capture` report `tls_failed` permanently**, on a capture that was working perfectly. That is the worst available failure for the tool SPEC.md calls the highest-value one.
- **Pinning is only attributable inside a Scope.** With one, the agent has said which hosts are the app; without one there is no evidence, only noise. So unscoped captures never claim pinning — they surface the count, note that the failures are unattributable, and name the Scope as the thing that would make them interpretable.
- **This is SPEC.md's own sentence coming true**: host filtering "is not a convenience here, it is the only available mechanism".

If P4 or later adds process attribution — `--mode local`, open question 1 — this constraint relaxes and `tls_failed` can become unconditional. Until then, do not "fix" it.

## Traps discovered the hard way

Carried forward from P1 and P2:

- **`mitmproxy` needs its explicit `>=11` floor.** Loosen it and resolution fails outright — uv backtracks to an ancient release whose `urwid` uses `use_2to3`, which modern setuptools rejects. It is a build error, not a warning.
- **`FastMCP` is the server class, not `MCPServer`.** `from mcp.server import MCPServer` raises `ImportError` in the pinned SDK (1.29.0) even though newer docs name it. Introspect the installed package rather than trusting docs.
- **Clearing mitmproxy's `mode` option is what releases the listener.** `master.shutdown()` stops the master and leaves the port bound. `options.update(mode=[])` first, then `shutdown()`.
- **`DumpMaster` runs fine as a task on an already-running event loop.** `master.run()` is a coroutine; `asyncio.create_task` it. This is what makes ADR-0001's single process work.
- **`mode=["regular@127.0.0.1:0"]` binds an ephemeral port on loopback only**, and `addons.get("proxyserver").listen_addrs()` reports what it got. Never hard-code a port.
- **A stray line on stdout does *not* corrupt MCP framing.** Tested with a deliberate flushed `print`: the client parses stdout line by line and skips what it cannot parse. Keep the proxy silent anyway — an interleaved write *inside* a JSON-RPC line would corrupt it. No test enforces this.
- **`Message.get_content(strict=True)` — the default — raises `ValueError` on a malformed `Content-Encoding`.** Capture must pass `strict=False`.
- **`mitmproxy.test.tflow` is the right harness for addon unit tests.** Real flow objects, and shapes a live proxy will not produce on demand.
- **`pytest-asyncio` may run an async-generator fixture's setup and teardown in different tasks.** The anyio cancel scopes inside `stdio_client` reject that: "Attempted to exit cancel scope in a different task". Keep an MCP client's whole lifetime inside the test body.
- **`FastMCP.call_tool` returns `(content_blocks, structured_result)`** for a tool annotated to return a dict — not a bare content list.
- **Invoke mitmdump in tests as `python -c "from mitmproxy.tools.main import mitmdump; mitmdump()"`.** Args after `-c` land in `sys.argv[1:]` where its parser wants them.
- **`curl` needs `--noproxy ""` to proxy localhost.** Without it an ambient `NO_PROXY` sends the request direct and the proxy records nothing.
- **Pass mitmdump a throwaway `--set confdir=`** in tests, or it writes an Interception Authority into the developer's real `~/.mitmproxy`.
- **Ruff 0.16 formats Python blocks inside Markdown.** `*.md` is excluded in `pyproject.toml`; leave it that way.
- **`timeout` does not exist on macOS.** Don't reach for it in test scripts.

Added by P3, all found by running against the real thing:

- **`networksetup -setwebproxy` *enables* the proxy as a side effect** of setting server and port. To restore a configured-but-disabled proxy the state command must come *second*. Reversed, it leaves the machine proxied through a dead port — the exact failure the sentinel exists to prevent.
- **Clearing the bypass list needs the literal word `Empty`, not `""`.** Passing `""` leaves an `ExceptionsList` holding one empty entry where the key previously held nothing. This one is worth dwelling on: the round-trip test passed the whole time, because `parse_bypass_domains` normalises both forms to `()`. **A parser that normalises can hide a writer that does not** — it was caught only by recording the machine's raw output before the run and diffing it afterwards. Do that for any new field here.
- **`Authenticated Proxy Enabled: 0` also ends in `Enabled`**, and carries `0`/`1` rather than `Yes`/`No`. A substring match reads that line last and inverts the flag.
- **`-getproxybypassdomains` answers in prose when the list is empty.** A line-splitting parser invents a bypass domain out of an English sentence.
- **The primary network service comes from `scutil`, not service order.** `show State:/Network/Global/IPv4` gives a UUID; only `Setup:/Network/Service/<uuid>` carries the name `networksetup` accepts.
- **`networksetup` proxy changes need no `sudo`** for a user in the `admin` group, and do not prompt. Verified by setting the already-off state to off.
- **`simctl keychain add-root-cert` requires a *booted* device.** A shut-down one fails with `code=405, Unable to lookup in current state: Shutdown`, exit 149. This answers what was open question 4. Installing twice is harmless, which is what lets `start_capture` install unconditionally.
- **Trust is observable, and the schema differs by runtime.** The authority's DER digest appears in the device's `TrustStore.sqlite3`. iOS 26 keys `tsettings` on a **`sha256`** column under `data/private/var/protected/trustd/private/`; the older and far more widely documented layout uses **`sha1`** under `data/Library/Keychains/`. Read both, or every device on the other runtime reports as untrusted.
- **`sqlite3.connect` on a missing path *creates* an empty database.** That turns a read-only trust check into a write inside the device's keychain directory — it happened while probing by hand, and produced a 0-byte file that then looked like a corrupt store. Open with `file:...?mode=ro` and check existence first.
- **`simctl create` writes a runtime notice to stderr.** `$(xcrun simctl create ... 2>&1)` puts that notice into the UDID and every later command fails with `Invalid device`. Keep the streams apart.
- **`simctl list devices -j` reports `dataPath`.** Use it rather than rebuilding the path from the UDID.
- **Never run an interactive `sudo` from this server.** It speaks JSON-RPC over stdin, so a password prompt reads the client's next message as a password attempt and hangs the session with no error and no way out. `install_authority_system` uses `sudo -n` and hands back the command for a human when credentials are not cached. A test asserts every `sudo` call carries `-n`.
- **The store gets written to after `close()`.** Connections still in flight when `stop_capture` runs deliver their hook afterwards, and mitmproxy renders the exception as `Addon error: I/O operation on closed file` — one line per open connection, looking exactly like a defect. `record()` now ignores late arrivals and counts them in `dropped_after_close`.
- **A `-m` on the command line replaces any default marker expression from `addopts`.** So gating a machine-mutating test with a marker is unsafe: `pytest -m "not integration"` would silently opt back in. `tests/test_simulator_e2e.py` is gated on `HTTPS_PROXY_MCP_E2E=1` for that reason.

## Manual recovery

If a crash ever leaves the machine proxied and repair-on-start cannot fix it:

```bash
networksetup -getwebproxy "Wi-Fi"          # see what it is now
networksetup -setwebproxystate "Wi-Fi" off
networksetup -setsecurewebproxystate "Wi-Fi" off
networksetup -setproxybypassdomains "Wi-Fi" Empty
cat ~/.https-proxy-mcp/proxy-state.json    # what it was before, if the sentinel survives
```

## Deliberately not built yet

- **`json_path` on `get_exchange_body`.** In SPEC.md's table. Doing it properly means a JSONPath dependency or a hand-rolled subset. Decide which when something wants it.
- **In-flight Exchanges.** Recording happens on completion, so a request still hanging is not in the store. `tail_exchanges` shows completions, not attempts. **P4 will probably want this** — "the Exchange still in flight when the assertion began" is the phase's acceptance criterion, and the store currently cannot answer it for a request that never finished. Additive (`record_pending` / `complete`), not a rewrite, but decide it deliberately and early.
- **The Run window as a narrowing.** `ReportingFilter` has the denylist and the Scope; SPEC.md's first narrowing is the Run window, which arrives with P4.
- **`serverInfo.version`** still reports the MCP SDK's version (1.29.0), not ours, because `FastMCP.__init__` takes no `version` parameter. Deferred to P5.
- **Android Emulator, write tools, `--mode local`.** v1.1, per PLAN.md.

## Open questions

1. Can `--mode local:<spec>` scope to the app process inside CoreSimulator? Now more interesting than it was: it is what would let `diagnose_capture` report pinning without a Scope. Still v1.1.
2. How should WebSocket and long-lived streaming connections appear in an interval model? They overlap everything by nature. **Will matter in P4.**
3. Do Detox / Appium / Playwright / XCUITest expose per-Step timings rich enough for the `Step` contract, or do they need explicit marks?
4. ~~Does `simctl keychain` require a booted simulator?~~ **Answered: yes.** See the traps list.
5. How should a non-UTF-8 body be represented? Settled for now — described in previews, base64 in slices — but nobody has used it against real binary traffic yet.

## Background research

The knowledge base this project was specced from lives at `~/Documents/Projects/knowledge/traffic-sniffing-mcp` — 7 articles, 26 sources, covering mitmproxy internals, both platform setups, prior art, and the correlation problem. Its `outputs/queries/2026-07-28 mitmproxy-mcp-implementation-plan.md` is the earlier plan that SPEC.md supersedes. Useful for *why* something is true; SPEC.md and the ADRs are authoritative for *what we're building*.

Three things in that KB are known-stale relative to this repo: it says the SDK class is `MCPServer` in v2 docs, it ranks clock skew as a top risk (ADR-0003 removes that by construction), and any trust-store guidance it carries will describe the `sha1` layout without mentioning that iOS 26 uses `sha256`.

## How the previous sessions worked

Not written down anywhere else, and it is what produced 465 tests with no known defects.

**Strict TDD, with the RED step actually observed.** Write the test, run it, read the failure, confirm it fails for the intended reason, then implement.

**Probe unknown platform APIs with a throwaway script before designing against them.** P3's whole design came out of four short probes: reading the real `networksetup` output, `scutil` for the primary service, `simctl keychain` against a shut-down device, and the trust store's schema. Each took a minute and each changed the design. Two of them contradicted the documentation.

**Teeth-check acceptance tests by deliberately breaking the code.** Making `repair()` a no-op failed three of the seven `kill -9` tests; making the proxy parser match loosely inverted the enabled flag; moving the sentinel write after the mutations failed the ordering test. A test that has never been seen to fail is not yet known to test anything.

**Record the machine's real state before mutating it, and diff afterwards.** This is how the `Empty` bug was found, and nothing else would have found it — every test passed throughout. Applies to any state the tool writes that it did not create.

**The live acceptance test earns its keep.** Its first run found three defects and one design constraint that 450 stubbed tests had not. Run it before believing a phase that touches the platform is done.
