# https-proxy-mcp — agent guide

An MCP server that captures an app's HTTPS traffic and interleaves it with the steps of an
end-to-end test, so a failure can be diagnosed by cause rather than by symptom. macOS only:
capture routes traffic with `networksetup` and installs trust with `simctl`.

v1 is feature-complete. What remains is publication (the user's decision, see docs/HANDOFF.md) and
v1.1 (docs/v1.1-candidates.md).

Two defects found by using the tool on a real app have been fixed since v1: a Scope now narrows what
is **decrypted** as well as what is reported (ADR-0007), and the record of the machine's prior proxy
settings names the process that made the change, so two editor windows stop destroying each other
(ADR-0008). Both change behaviour a reader may assume, so read those two before either.

A third followed from using it on two simulators at once: reporting gained a fourth narrowing, the
**Lens** (ADR-0009), because two Capture Targets sending the same traffic to the same hosts cannot
be separated by any narrowing that works on hosts.

## Verify

```bash
uv run pytest -q          # 951 passed, 3 skipped, ~48s
uv run ruff check .
uv run ruff format --check .
uv run mypy               # strict, src/ only
```

`uv run pytest -m "not integration"` runs the fast majority in about eleven seconds.

The 3 skipped are the live machine tests. They are the only things here that change the
developer's machine, so they are gated on an environment variable rather than a marker — see
the non-negotiables below. Read `.claude/skills/live-machine-tests/` before running them.

## Read these first, in this order

0. **[.claude/skills/https-proxy-mcp/SKILL.md](./.claude/skills/https-proxy-mcp/SKILL.md)** — the
   agent-facing prompt, and the shortest description of what this tool is *for*. Three tools are
   the path; seventeen are drill-down.
1. **[CONTEXT.md](./CONTEXT.md)** — the domain language. Use these words exactly.
2. **[docs/USAGE.md](./docs/USAGE.md)** and **[docs/INSTALL.md](./docs/INSTALL.md)** — the human
   guides. USAGE.md is the **accurate** tool reference: its signatures are checked against the
   live schemas by a test, which SPEC.md's table is not.
3. **[docs/SPEC.md](./docs/SPEC.md)** — the v1 spec. A record of *intent*; its tool table has drifted.
4. **[docs/PLAN.md](./docs/PLAN.md)** — six phases with acceptance criteria, all six met.
5. **[docs/adr/](./docs/adr/)** — nine decisions and why. Two deviate from the research that
   preceded the project, two more make a Scope narrow what is *captured*, and the last adds a
   fourth reporting narrowing, so read them before "fixing" anything that looks wrong.
6. **[docs/superpowers/specs/2026-08-05-p4-runs-and-timelines-design.md](./docs/superpowers/specs/2026-08-05-p4-runs-and-timelines-design.md)**
   — P4's design: the four probe findings that shaped it, and the Correlator's rules stated
   precisely. The summaries elsewhere carry the findings; this carries the reasoning, which is
   what you check a summary against when overlap attribution looks wrong.
7. **[docs/HANDOFF.md](./docs/HANDOFF.md)** — current state, what is deliberately not built, open
   questions, and the decisions that belong to the user.

## Non-negotiables

- **Use CONTEXT.md's words.** They were chosen to dodge a real collision — mitmproxy's "flow"
  versus Maestro's "flow". If you find yourself writing "flow", stop. The only exceptions are
  `capture/addon.py` and `capture/filtering.py`, where `flow` names mitmproxy's own object at
  the boundary.
- **SPEC.md has drifted from the code in eleven places and must not be "fixed" to match.** It is
  the v1 specification and a historical record of intent. `docs/USAGE.md` is the API reference.
  The eleven are enumerated once, in
  [.claude/agents/doc-drift-reviewer.md](./.claude/agents/doc-drift-reviewer.md) — that is where a
  review reads them from, so a new divergence is recorded there and nowhere else.
- **`SKILL.md` is deliberately self-contained** — no relative links into this repo — so it works
  copied to `~/.claude/skills/https-proxy-mcp/SKILL.md` on any machine. Do not add links to it.
  It lives at `.claude/skills/https-proxy-mcp/SKILL.md`, where skills go, and there is exactly
  one copy: the install step is a straight `cp` from that path to the same path under `~`.
- **The machine tests are gated on `HTTPS_PROXY_MCP_E2E=1`, not on the `machine` marker.** A
  `-m` on the command line replaces any default expression from `addopts`, so a marker gate
  would let `pytest -m "not integration"` silently opt back in to redirecting the machine's
  networking.
- **Never run an interactive `sudo` from this server.** It speaks JSON-RPC over stdin, so a
  password prompt reads the client's next message as a password attempt and hangs the session
  with no error and no way out. Use `sudo -n`; a test asserts every `sudo` call carries it.
- **`~/.https-proxy-mcp/` should not exist on this machine.** Every test redirects the root to
  `tmp_path`. If you find it, something leaked — look at `sessions/*/session.json` to see which
  target and port created it before removing anything.
- **Per-module traps live beside the module**, in that directory's `CLAUDE.md`. They are the
  hardest-won knowledge in the project and each one cost a real debugging session. See
  [docs/adr/0006-traps-live-beside-their-modules.md](./docs/adr/0006-traps-live-beside-their-modules.md).

## What exists now

| Module | Contents |
|---|---|
| `capture/exchange.py` | `CapturedExchange`, `Exchange`, `PendingExchange`, `BodyRef`, NDJSON round-trip |
| `capture/store.py` | `ExchangeStore` — ring buffer, eviction counter, body spill, append-only log, `0700`, TLS-failure counter, **pending registry** |
| `capture/addon.py` | `CaptureAddon` — the only module that knows mitmproxy's flow shapes |
| `capture/session.py` | Session identity, layout, `$HTTPS_PROXY_MCP_ROOT`, retention to 10 |
| `capture/runtime.py` | `ProxyRuntime` — mitmproxy in this process on the server's event loop |
| `capture/filtering.py` | mitmproxy filter expressions applied to Exchanges |
| `capture/hostsets.py` | What a host entry means, in both forms that need it — the Scope predicate, and mitmproxy's `allow_hosts` patterns |
| `capture/reporting.py` | The builtin denylist, the Scope, the Run window — SPEC.md's three narrowings — and the Lens, which is the fourth (ADR-0009) |
| `correlate/steps.py` | `Step` — the entire contract the Correlator depends on |
| `correlate/timeline.py` | `Occurrence`, owner-plus-overlap arithmetic, `Timeline`. **Pure: no I/O** |
| `runs/registry.py` | `Run`, `RunRegistry` — identity, directory, manifest |
| `runs/process.py` | Spawning the test command, Maestro flag injection, timeouts, output capture |
| `eventsources/` | `EventSource` protocol, `StepLog` (explicit marks), `MaestroEventSource`, and the `ARTIFACT_SOURCES` registry a new source is added to |
| `serialize/redact.py` | Headers, JSON keys, form keys and JWTs → `<redacted:sha256:xxxxxxxx>` |
| `serialize/views.py` | Summaries, previews, slices, pagination, curl rendering |
| `serialize/timeline_views.py` | Summary-first Timeline rendering, and the explanation sentence |
| `serialize/har.py` | HAR 1.2, redacted by default |
| `targets/systemproxy.py` | The Mac's proxy settings, the sentinel protocol that guarantees they come back, and the report of who holds it |
| `targets/simctl.py` | CoreSimulator: which device, install the authority, and *verify* trust |
| `targets/ios_simulator.py` | The iOS Simulator Capture Target, including the five-way diagnosis |
| `targets/` | `CaptureTarget` protocol, `TrustStatus`, `Diagnosis`, `CaptureObservations`, `CurlTarget` |
| `server/` | `CaptureState` plus the **twenty** registered tools |
| `version.py` | The version, read from installed metadata. `pyproject.toml` is the only place one is written |

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
| **`diagnose_capture` will not report `tls_failed` without a Scope** | It looks like the headline feature has been switched off. It has not: an unscoped verdict is *worthless*. See "the finding that shaped P3" in HANDOFF.md. |
| The service to redirect comes from `scutil`, not from `-listnetworkserviceorder` | Ordering looks like the obvious source. On this machine it ranks Thunderbolt Bridge first while the default route goes out `en0` — so ordering proxies an interface carrying no traffic and reports success. |
| `redirect()` **refuses** to run when a sentinel already exists | Looks unhelpfully strict. Overwriting would record the already-proxied state as the state to return to, losing the machine's real configuration for good and making every later restore a no-op that leaves it proxied. `prepare()` repairs first. |
| A corrupt sentinel switches both proxies **off** rather than giving up | Losing fidelity looks worse than doing nothing. It is not: the prior state is already gone, and leaving traffic pointed at a dead proxy reads to the developer as "the network is broken". |
| Trust is **verified** against the device's trust store, not inferred from the install | `simctl` exiting zero means it accepted the file, not that the device trusts it. The store is the only thing that knows. |
| `get_trust_status` does not install anything | It duplicates `install_authority` minus the install, which looks redundant. A question that changes what it asks about is a trap: an agent checking whether a device was *already* trusted would silently arrange trust and never find out. |
| `install_authority_system` uses `sudo -n` and fails rather than prompting | An interactive prompt would hang the session permanently. See the non-negotiables above. |
| `wait_for_exchanges` and `get_exchange` are **exempt** from the denylist and Scope | Inconsistent with the listings. Both are explicit: a filter naming a host, or an id that came from somewhere. Letting the denylist veto a wait turns "wait for this request" into a timeout reporting that it never happened. |
| Session bodies live under `tmp_path` in every test, never the real home | Belt and braces on top of `$HTTPS_PROXY_MCP_ROOT`. Those directories hold real credentials. |

Added by P4:

| Decision | Why it looks odd |
|---|---|
| Pending requests are a **parallel registry**, not Exchanges with a null `t_end` | Looks like duplication. Folding them in would assign ids at request time, inverting the documented completion ordering, and would make `wait_for_exchanges` return before a status code existed — so "wait for `POST /auth`, then check it returned 401" would stop working. `pending` is deliberately absent from `ExchangeStore.__iter__`, and that one omission is what leaves every listing tool untouched by P4. |
| Pending records **survive `close()`** | Looks like a leak. "Two requests were still open when the Session ended" is information, and often the answer to "why did the assertion time out?". A completion arriving after close leaves the record alone too, since the Exchange is not being recorded and clearing it would lose the request entirely. |
| Ownership is **gap-tolerant** — the last Step to have *started*, not one strictly containing the Exchange | Looks sloppy. Maestro's inter-command overhead is milliseconds wide and a tap's request routinely starts just after the tap's own metadata window closed. Requiring containment drops "the tap made this request" into Background for the commonest shape of traffic there is. |
| The Run window narrows by **intersection**, not by start time | Looks like it lets in traffic from before the Run. It lets in exactly the traffic that matters: a request already in flight when the Run began is the case ADR-0004 exists for. |
| A long-lived connection is classed by `101` **or `text/event-stream`**, with no duration threshold | A threshold looks more general. It also needs tuning, and would miss a short WebSocket while firing on a slow upload. Both signals here are exact statements by the protocol. |
| `get_timeline` with no `run_id` means the **latest Run**; `export_har` with none means the **whole Session** | Two defaults for one parameter looks inconsistent. A Timeline is about a Run; an export is an artifact of everything captured. An `export_har` that silently held only the last few milliseconds of a Run is the more expensive surprise. |
| `start_step` with one already open is an **error** | An implicit close looks friendlier. It would have to invent an outcome, and a Step's outcome is the entire reason for recording it — writing "passed" over something that failed is the one unsafe direction. |
| Maestro's `defineVariables` and `applyConfiguration` are **filtered out** of Steps | Dropping data looks wrong. Neither is an action or an assertion (CONTEXT.md) and neither can cause a failure. Filtered by an explicit list of keys, so an *unrecognised* command is kept. |
| An unrecognised Maestro status becomes `unknown`, never `passed` | Looks pedantic. A Timeline claiming a Step succeeded when nobody knows is worse than one admitting ignorance, and Maestro may add a status in any release. |
| A mistyped target option is **refused by the registry** | Looks like ceremony for a fixed call site. Both targets accept `**_options` so one call shape fits all of them, which means neither would notice — and a mistyped `udid` leaves the simulator target choosing a device itself, which is the "capture the wrong app and report success" outcome `start_capture` forbids. |

Added by P5:

| Decision | Why it looks odd |
|---|---|
| Both dependencies carry **upper bounds** | Caps read as timidity. These are measured: mcp 2.0.0 removed `mcp.server.fastmcp` outright and declares `Requires-Python: >=3.10`, so it was an eligible candidate on every interpreter here — and a wheel ships **no lock file**, so a consumer resolves from `Requires-Dist` alone. Lifting either means porting, not relaxing. |
| The version is read from **installed metadata**, never written in the source | A literal would be simpler and is exactly the problem: two places holding one fact, drifting the moment one is bumped alone. A tree with no metadata reports `0+unknown` rather than a plausible number, so a client can tell it is not talking to a release. |
| `server._mcp_server.version` is assigned after construction | Reaching into a private attribute looks fragile. `FastMCP.__init__` genuinely takes no `version`, and the low-level server it wraps reads its own mutable `version` when it builds the handshake. The stdio handshake test asserts through the transport, so an SDK rename fails a test rather than silently reverting to the SDK's version. |
| `SKILL.md` lives under **`.claude/skills/`**, not at the repo root and not inside the wheel | SPEC.md says "ships a SKILL.md", so bundling looks more faithful. Skill files belong in skill directories rather than site-packages, and keeping it out of the package is what lets the wheel-contents test assert "nothing outside the package". It sat at the root through v1 and moved once `.claude/skills/` existed — one file, in the place its own install instructions point at. |
| The wheel's contents are asserted as an **allowlist of two prefixes** | A denylist reads as more targeted. It would have to name `tests/fixtures/maestro-commands-failing-assert.json` — 123 KB of one device's UI tree — and would not name whatever the next one is. |
| The clean-install test is parametrised over **interpreters**, not just run once | Looks like thoroughness for its own sake. It is the only thing in the suite that exercises *resolution*, and one interpreter cannot notice a dependency range that two interpreters disagree about. It is what caught the mcp 2.0.0 break. |
| `requires-python` is pinned to the interpreters under test | Testing configuration looks like ceremony. It closes the loop: lowering the floor without adding the interpreter would ship a promise nobody had tried. |
| `get_status` is no longer what `INSTRUCTIONS` suggests for an empty result | It was the older advice and it is not wrong, just weakest. `get_status` answers "is capture running", the least likely cause of an empty result; `diagnose_capture` is the one that separates them all. |

Added by Passthrough and sentinel ownership (ADR-0007, ADR-0008):

| Decision | Why it looks odd |
|---|---|
| A Scope now narrows what is **captured**, not only what is reported | It reads as the opposite of CONTEXT.md's Scope and of `reporting.py`'s whole premise. It is a second concept — Passthrough — and the old sentence is still true of the default. ADR-0007. |
| `passthrough` defaults to **on** | A default that changes what gets captured looks reckless. It is inert until an agent names a Scope, and the other default silently breaks the developer's browser and every other simulator on the machine. |
| Passthrough is fed by the Scope alone, never by the builtin denylist | The denylist would spare more chatter and is a *reporting* narrowing whose omissions are deliberate — `googleapis.com` is absent because it is many apps' backend. A wrong entry there costs a hidden row; in a transport decision it costs traffic that was never captured. |
| Passthrough covers **TLS only**, and plain HTTP is still intercepted | Reads like a bug, and is the engine being honest: a plain-HTTP request does not name its destination until after the layer is chosen. Measured, not assumed — see ADR-0007's consequences. The two cases have opposite remedies, so both are always stated. |
| The two Scope-exempt tools find nothing for an out-of-scope host | `wait_for_exchanges` and `get_exchange` are exempt on purpose, so this looks like the exemption breaking. Accepted knowingly: it is recoverable by restarting, where the old default was recoverable only by already knowing about it. `diagnose_capture` says so out loud. |
| The sentinel names a pid **and** an opaque process start time | The start time looks redundant next to a liveness check. It is what stops a rebooted machine's id collision from making the leak permanent — with a pid alone, a self-healing bug becomes a permanent one. |
| A live foreign owner is **refused**, never taken over or waited for | Taking over is today's bug with better manners; waiting hangs a JSON-RPC session, which is the failure the `sudo -n` non-negotiable exists to avoid. ADR-0008. |
| There is deliberately **no** force-release tool | Refusing without an escape hatch looks unhelpful. The hatch is `kill <pid>`: the holder's own handlers restore the machine on its way out, and a force-release would restore someone else's recorded state from here. |
| An unreadable process start time means *dead* | Fail-open looks unsafe. Empty comparing equal to empty would make two unrelated processes look like one, and declining a repair is what leaves the machine proxied through a dead port. |
| `get_status` reports the proxy whether or not this Session is recording | Session state that answers with no Session looks wrong. The record is on disk and process-independent, and "is my machine still proxied?" is asked exactly when nothing is recording here. |

Added by the machine-wide TLS-failure correction:

| Decision | Why it looks odd |
|---|---|
| `tls_failed` names the device and then says the count is not necessarily about it | Naming an actor and disclaiming it in the same sentence reads as hedging. The count genuinely cannot be attributed — loopback leaves no peer address — and a diagnosis that names a specific actor is read as evidence about that actor. The confident version is how this repository came to record the app under test as certificate-pinned when it is not. |
| Pinning is the *leading candidate*, not the finding | The verdict exists to detect pinning, so demoting it looks like abandoning the feature. It is not demoted: it is still named first, and the cheaper cause — another client on this Mac reaching the same host — is named beside it because that cause is both commoner and free to rule out. |
| The remediation is three sentences where every other verdict is one | Length is the point. The one-sentence version is what stated a cause as fact; the extra two are the caveat and the check that separates the two causes. |

Added by the harness work:

| Decision | Why it looks odd |
|---|---|
| `mypy` checks `src/` only | Leaving the larger half unchecked looks half-done. Under `--strict` the suite reports ~870 findings, almost all a missing `-> None`; a mechanical edit across 41 files would bury anything real in the same diff. A separate, deliberate commit if it is ever wanted. |
| mitmproxy's unannotated calls are relaxed **per module**, not per call site | Two named modules in `pyproject.toml` rather than eight inline ignores. The boundary with someone else's library is a real thing and is better named once than rediscovered at each call site — the same reasoning CONTEXT.md uses to allow "flow" in exactly two files. |

Added by the Lens (ADR-0009):

| Decision | Why it looks odd |
|---|---|
| The Lens is **Session-level**, where `list_exchanges` takes a per-call `filter` | A parameter on `get_timeline` was the obvious change and was the first one chosen. It is opt-in, and the failure is silent: a Timeline still holding the other simulator's traffic looks exactly like a Timeline. The other three narrowings are Session- or Run-level for the same reason. |
| `wait_for_exchanges` obeys the Lens while staying exempt from the Scope | Reads as the exemption breaking. The exemption's reasoning is that naming a *host* in a filter is explicit; a Lens says which *client* the traffic came from, and a wait that ignored it is answered by the other simulator's login — the defect the "only traffic recorded after this call" rule already exists to prevent. Kept where the reasoning holds, reversed where it does not. |
| `get_exchange` stays exempt from the Lens too | Inconsistent with the line above until you notice the difference: an id came from somewhere. A Lens able to veto a fetch by id would make an Exchange id stop meaning anything. |
| `ReportingFilter` holds a **callable**, not the expression | Looks like indirection for its own sake. Compiling one needs mitmproxy's filter language and a way to read bodies off disk, and `reporting.py` is deliberately free of the engine — the same separation ADR-0007 kept when it declined to fold Passthrough into the Scope. |
| The Lens is compiled **before the target is built** | It closes over a store that does not exist yet, which reads as a bug. It is the ordering that matters: compiled later, a typo is discovered after the machine's proxy has been redirected and an authority installed into a device. |
| A Lens cannot narrow the TLS-failure count | Looks like the narrowing being applied inconsistently. It is a predicate over Exchanges, and a failed handshake never became one. The case a Lens exists for is also the case that inflates that count, and a Lens cannot deflate it — stated rather than quietly true. |
| `start_capture` counts **trust stores**, not traffic | A device census in a traffic tool looks off-topic. Installing the Authority into a second device is what keeps that simulator working, and it is also what puts its bodies unredacted into a Session it is not the Capture Target of. Nothing here can inspect a body and know what is in it; counting trust stores is the honest half, and it appears where the decision is made. |
| `devices_booted` is **null**, not zero, on the verdicts with no target built | Zero is a number and null is an admission. Answering "0 booted" from a branch that never read a trust store would be a false statement about a machine that may well have three. |

## Agent skills

### Issue tracker

Local markdown under `.scratch/<feature-slug>/` — which is gitignored, so issues stay on this
machine and never reach the GitHub remote. See `docs/agents/issue-tracker.md`.

### Triage labels

The five canonical roles, each label string equal to its name, carried on a `Status:` line in the
issue file. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `CONTEXT.md` and `docs/adr/` at the root, plus the per-module `CLAUDE.md` traps. See
`docs/agents/domain.md`.
