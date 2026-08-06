# Handoff — 2026-08-05 (P5 complete — v1 feature-complete)

Picking this up in a fresh session. Everything needed is in the repo; this file says where to start and what not to relitigate.

## State

**P0 through P5 complete and committed — every phase in PLAN.md.** On `main`, clean tree, **no remote configured**.

Verify in one command: `uv run pytest -q && uv run ruff check .` → 671 passed, 3 skipped, all checks passed.

v1 is feature-complete. What remains is not a phase: it is **publication** (a decision, see below) and **v1.1** (Android, write tools, `--mode local`). Nothing in the plan is outstanding.

The 3 skipped are the live machine tests. They are the only things in the project that change the developer's machine, so they are gated:

```bash
HTTPS_PROXY_MCP_E2E=1 uv run pytest tests/test_simulator_e2e.py tests/test_maestro_e2e.py -q
```

`uv run pytest -m "not integration"` runs the fast majority in about five seconds. The slowest thing in the ordinary suite is now `tests/test_distribution.py` (~12 s): it builds a real wheel and installs it into a throwaway virtualenv per supported interpreter. It changes nothing outside `tmp_path`.

Try it by hand:

```bash
uv run https-proxy-mcp          # speaks MCP over stdio
uv build                        # wheel + sdist, ~0.04s, no network
```

## Read these first, in this order

0. **[SKILL.md](./SKILL.md)** — the agent-facing prompt, and the shortest description of what this tool is *for* that exists. Read it before the spec if you want the shape in two minutes: three tools are the path, seventeen are drill-down. It is deliberately self-contained (no links into this repo) so it works copied to `~/.claude/skills/https-proxy-mcp/SKILL.md` on any machine — do not add relative links to it.
1. **[CONTEXT.md](./CONTEXT.md)** — the domain language. Use these words exactly; they were chosen to dodge a real collision (mitmproxy's "flow" vs Maestro's "flow"). If you find yourself writing "flow", stop. The exceptions are `capture/addon.py` and `capture/filtering.py`, where `flow` names mitmproxy's own object at the boundary.
2. **[SPEC.md](./SPEC.md)** — the v1 spec. Tool surface, semantics, numeric defaults.
3. **[PLAN.md](./PLAN.md)** — six phases with acceptance criteria, all six now met. Each one records what was actually verified, and P4's and P5's say where the criterion was nearly met for the wrong reason.
4. **[docs/adr/](./docs/adr/)** — five decisions and why. Two deviate from the research that preceded the project, so read them before "fixing" anything that looks wrong.
5. **[docs/superpowers/specs/2026-08-05-p4-runs-and-timelines-design.md](./docs/superpowers/specs/2026-08-05-p4-runs-and-timelines-design.md)** — P4's design, including the four probe findings that shaped it.

## What exists now

| Module | Contents |
|---|---|
| `capture/exchange.py` | `CapturedExchange`, `Exchange`, `PendingExchange`, `BodyRef`, NDJSON round-trip |
| `capture/store.py` | `ExchangeStore` — ring buffer, eviction counter, body spill, append-only log, `0700`, TLS-failure counter, **pending registry** |
| `capture/addon.py` | `CaptureAddon` — the only module that knows mitmproxy's flow shapes |
| `capture/session.py` | Session identity, layout, `$HTTPS_PROXY_MCP_ROOT`, retention to 10 |
| `capture/runtime.py` | `ProxyRuntime` — mitmproxy in this process on the server's event loop |
| `capture/filtering.py` | mitmproxy filter expressions applied to Exchanges |
| `capture/reporting.py` | The builtin denylist, the Scope, and the Run window — SPEC.md's three narrowings |
| `correlate/steps.py` | `Step` — the entire contract the Correlator depends on |
| `correlate/timeline.py` | `Occurrence`, owner-plus-overlap arithmetic, `Timeline`. **Pure: no I/O** |
| `runs/registry.py` | `Run`, `RunRegistry` — identity, directory, manifest |
| `runs/process.py` | Spawning the test command, Maestro flag injection, timeouts, output capture |
| `eventsources/` | `EventSource` protocol, `StepLog` (explicit marks), `MaestroEventSource` |
| `serialize/redact.py` | Headers, JSON keys, form keys and JWTs → `<redacted:sha256:xxxxxxxx>` |
| `serialize/views.py` | Summaries, previews, slices, pagination, curl rendering |
| `serialize/timeline_views.py` | Summary-first Timeline rendering, and the explanation sentence |
| `serialize/har.py` | HAR 1.2, redacted by default |
| `targets/systemproxy.py` | The Mac's proxy settings, and the sentinel protocol that guarantees they come back |
| `targets/simctl.py` | CoreSimulator: which device, install the authority, and *verify* trust |
| `targets/ios_simulator.py` | The iOS Simulator Capture Target, including the five-way diagnosis |
| `targets/` | `CaptureTarget` protocol, `TrustStatus`, `Diagnosis`, `CaptureObservations`, `CurlTarget` |
| `server/` | `CaptureState` plus the **twenty** registered tools |
| `version.py` | The version, read from installed metadata. `pyproject.toml` is the only place one is written |

## Next: publication is a decision, not a phase

**The user chose not to publish**, asked explicitly, in the session that finished P5. Do not treat it as an outstanding task or re-raise it unprompted. If they ask, this is what is already true and what is not:

Already done and verified locally, by `tests/test_distribution.py`:

- `uv build` produces a clean wheel and sdist — package only, no tests, no fixtures, no `__pycache__`.
- The wheel installs into a fresh virtualenv under **every** interpreter `requires-python` admits, and the installed console script serves MCP and captures real traffic from a directory outside the repo.
- Metadata: MIT (`License-Expression` plus the text under `.dist-info/licenses/`), keywords, classifiers including `Operating System :: MacOS :: MacOS X`, and `serverInfo.version` reporting ours.

Not done, and only a package index can do it: that the name resolves, that the long description renders, that `uvx https-proxy-mcp` with no `--from` finds anything. If publication ever happens, TestPyPI first — the local suite cannot tell you a description failed to render.

**v1.1 is the other direction**, per PLAN.md: Android Emulator, write/interception tools behind a flag, `--mode local`. Open question 1 is the interesting one there.

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

Added by P4:

| Decision | Why it looks odd |
|---|---|
| Pending requests are a **parallel registry**, not Exchanges with a null `t_end` | Looks like duplication. Folding them in would assign ids at request time, inverting the documented completion ordering, and would make `wait_for_exchanges` return before a status code existed — so "wait for `POST /auth`, then check it returned 401" would stop working. `pending` is deliberately absent from `ExchangeStore.__iter__`, and that one omission is what leaves every listing tool untouched by P4. |
| Pending records **survive `close()`** | Looks like a leak. "Two requests were still open when the Session ended" is information, and often the answer to "why did the assertion time out?". A completion arriving after close leaves the record alone too, since the Exchange is not being recorded and clearing it would lose the request entirely. |
| Ownership is **gap-tolerant** — the last Step to have *started*, not one strictly containing the Exchange | Looks sloppy. Maestro's inter-command overhead is milliseconds wide and a tap's request routinely starts just after the tap's own metadata window closed. Requiring containment drops "the tap made this request" into Background for the commonest shape of traffic there is. |
| The Run window narrows by **intersection**, not by start time | Looks like it lets in traffic from before the Run. It lets in exactly the traffic that matters: a request already in flight when the Run began is the case ADR-0004 exists for. |
| A long-lived connection is classed by `101` **or `text/event-stream`**, with no duration threshold | A threshold looks more general. It also needs tuning, and would miss a short WebSocket while firing on a slow upload. Both signals here are exact statements by the protocol. Answers open question 2. |
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
| `SKILL.md` lives at the **repo root only**, not inside the wheel | SPEC.md says "ships a SKILL.md", so bundling looks more faithful. Skill files belong in skill directories rather than site-packages, and keeping it out is what lets the wheel-contents test assert "nothing outside the package" — which is the assertion that catches a fixture or a test suite shipping by accident. |
| The wheel's contents are asserted as an **allowlist of two prefixes** | A denylist reads as more targeted. It would have to name `tests/fixtures/maestro-commands-failing-assert.json` — 123 KB of one device's UI tree — and would not name whatever the next one is. |
| The clean-install test is parametrised over **interpreters**, not just run once | Looks like thoroughness for its own sake. It is the only thing in the suite that exercises *resolution*, and one interpreter cannot notice a dependency range that two interpreters disagree about. It is what caught the mcp 2.0.0 break. |
| `requires-python` is pinned to the interpreters under test | Testing configuration looks like ceremony. It closes the loop: lowering the floor without adding the interpreter would ship a promise nobody had tried, which is precisely how the uncapped `mcp` range came to be broken. |
| `get_status` is no longer what `INSTRUCTIONS` suggests for an empty result | It was the older advice and it is not wrong, just weakest. `get_status` answers "is capture running", the least likely of the four causes; `diagnose_capture` is the one that separates all four, and SPEC.md calls it the highest-value tool in the set. |

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

## The finding that shaped P4

Maestro's `commands-*.json` **is not in execution order.** A failing two-command flow came back as `[seq 3, 0, 1, 2]` — the FAILED assertion first, having run last — and a *passing* flow was also scrambled (`1, 2, 0`), so it is not a failure-ordering quirk. `metadata.sequenceNumber` is the authority.

This is worth its own section for the same reason the `Empty` bug was: **every test would have passed.** Ordering by array position produces a Timeline that is internally consistent, renders cleanly, and puts the failure before the launch that preceded it. It reads as a correlation bug and it is not one. The only reason it was caught is that the artifact was probed before anything was designed against it.

The same probe found that one failing command carries **33 KB of `error.hierarchyRoot`** — the entire device UI tree — making a two-command artifact 123 KB. That artifact is checked in at `tests/fixtures/maestro-commands-failing-assert.json`, hierarchy and all, and a test asserts the parser drops it. A second test caps *every* Step's error length structurally, so a new Maestro error field cannot slip through.

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
- **A `-m` on the command line replaces any default marker expression from `addopts`.** So gating a machine-mutating test with a marker is unsafe: `pytest -m "not integration"` would silently opt back in. The live tests are gated on `HTTPS_PROXY_MCP_E2E=1` for that reason.

Added by P4:

- **Maestro's `commands-*.json` is not in execution order.** See "the finding that shaped P4" above. Order by `metadata.sequenceNumber`.
- **`metadata.timestamp` is epoch *milliseconds*** and marks the command's **start**; `duration` is milliseconds too. Missing the conversion puts every Step about fifty thousand years from the traffic it should align with.
- **`--test-output-dir` does not contain the debug output.** Its own `--help` says so and a probe left the directory empty; `commands-*.json` lives under `--debug-output` alone. Pointing both at one directory is still worth doing so artifacts stay together, but only one of them matters.
- **The artifact layout differs by flag.** With `--flatten-debug-output` it is `<dir>/commands-(flow.yaml).json`; without it, `<dir>/.maestro/tests/<timestamp>/commands-*.json` — a **hidden** directory. Filenames embed the flow name in literal parentheses, not a timestamp. The parser searches recursively for both; `run_test` injects the flatten flag anyway.
- **Maestro exits 1 on a failing flow and 0 on a passing one.** Obvious in hindsight; the first probe read `$?` after a pipe to `tail` and got tail's status instead, which said 0 for a flow that had plainly failed. Beware `$?` after a pipeline.
- **A timeout must kill the process *group*.** Maestro launches a JVM. Killing only the wrapper leaves it holding the simulator and the port, so the *next* Run fails for a reason that looks entirely unrelated. `start_new_session=True` plus `os.killpg`, tested with a real orphaned child.
- **`maestro openLink` to a private-IP `http://` URL did not make Safari fetch it.** A hanging sink on the host's LAN address was never reached, so `pending` stayed empty and the acceptance test passed on incidental Safari traffic instead. **The test was green either way** — it was caught only by rebuilding the Timeline from the artifacts afterwards. The deterministic proof now lives in `tests/test_pending_e2e.py`, driven through `curl`. If you want to force real device traffic to hang, verify the device actually requests your URL first.
- **A test that reads stdin from a subprocess cannot prove stdin was closed**, because pytest has already redirected it. That test passed with the precaution removed. It now asserts the spawn arguments instead. Applies to anything about the environment a child inherits.

Added by P5, all found by building the artifact and installing it:

- **A wheel ships no lock file.** `uv.lock` governs development and nothing else; a consumer resolves from `Requires-Dist`. This is the whole reason the caps exist, and it is easy to forget because every in-repo test passes against the locked versions.
- **`mcp>=1.29.0` uncapped resolved mcp 2.0.0 on Python 3.13**, which has no `mcp.server.fastmcp` under that path or any other — the package is restructured around `mcp.server.mcpserver`. The installed server died on import. **3.12 was not safe either**, it merely had a warm cache; mcp 2.0.0 declares `Requires-Python: >=3.10`. A green clean-install on one interpreter proves nothing about resolution.
- **`stdio_client` does not forward the environment.** With `env=None` the SDK hands the child `get_default_environment()` — exactly `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`. So an **exported** `HTTPS_PROXY_MCP_ROOT` never reaches a server launched that way, and it writes Sessions holding unredacted bodies into the developer's real home while looking redirected. Pass `env=` explicitly. This leaked three times in one session before it was noticed — including from the first teeth-check *of the fix*, which ran without redirecting `HOME`.
- **`with_termlog=False` does not make mitmproxy silent.** Its records go through Python's `logging`, and the MCP SDK installs a handler, so proxy chatter still appears. Verified harmless: stdout carries **only** valid JSON-RPC and every line of chatter is on stderr. That closes the open concern in the P1 trap below about the proxy sharing stdout with the protocol.
- **Batch-piping JSON-RPC into the server and closing stdin loses in-flight responses.** A probe that wrote all four messages at once got no response to the `start_capture` in the middle — the shutdown raced the response write. Not a defect; with realistic pacing all three arrive. It reads exactly like a dropped-response bug, so pace a probe before diagnosing one.
- **`uv build` is instant and needs no network** (~0.04 s), which is why the distribution suite can build a real wheel per module rather than mocking one.
- **PEP 639 metadata works through `uv_build`.** `license = "MIT"` plus `license-files = ["LICENSE"]` emits `Metadata-Version: 2.4`, `License-Expression`, and the text under `.dist-info/licenses/LICENSE` — inside `.dist-info/`, which is why it does not disturb the wheel-purity assertion.
- **`ruff format` disagrees with 11 committed files** and has since before P5. `ruff check` is the gate and it passes; `ruff format` was never run across the tree. Leave it or do it deliberately in one commit — doing it incidentally buries whatever else is in the diff.

## Manual recovery

If a crash ever leaves the machine proxied and repair-on-start cannot fix it:

```bash
networksetup -getwebproxy "Wi-Fi"          # see what it is now
networksetup -setwebproxystate "Wi-Fi" off
networksetup -setsecurewebproxystate "Wi-Fi" off
networksetup -setproxybypassdomains "Wi-Fi" Empty
cat ~/.https-proxy-mcp/proxy-state.json    # what it was before, if the sentinel survives
```

The developer machine's known-good state is also recorded in `.local/` (gitignored) by both live-test sessions: web and secure proxies both `Enabled: No`, server `127.0.0.1`, port `9090`, no bypass domains. Verified still true at the end of P5, which mutated nothing.

**`~/.https-proxy-mcp/` should not exist** on this machine. Every test redirects the root to `tmp_path`. If you find it, something leaked — check the `stdio_client` environment trap first, and look at `sessions/*/session.json` to see which target and port created it before removing anything.

## Deliberately not built yet

- **`json_path` on `get_exchange_body`.** In SPEC.md's table. Doing it properly means a JSONPath dependency or a hand-rolled subset. Decide which when something wants it.
- **WebSocket *message* capture.** The upgrade Exchange is recorded and classed long-lived; the individual frames are not. Open question 2 asked how streams should *appear* in an interval model, which the long-lived rule answers; capturing their contents is a separate v1.1 question.
- **Reading Runs back from disk.** `RunRegistry` writes `run.json` for durability but keeps its index in memory, so a Session directory found later is readable by hand and not by `list_runs`. Nothing needs it yet.
- **Publication.** Built and locally verified, not uploaded. The user's explicit choice — see "Next" above.
- **Android Emulator, write tools, `--mode local`.** v1.1, per PLAN.md.
- **`simplify` on `server/run_tools.py` and `serialize/timeline_views.py`.** Named as an optional quality pass for P5 and not taken; nothing about them is known to be wrong.
- **A reverse check on `SKILL.md`.** A test asserts every registered tool is *named* in the skill. The other direction — that the skill names no tool which does not exist — is not asserted, because telling a tool name from a parameter name in prose needs a curated exclusion list costing more upkeep than the bug it catches.

## Open questions

1. Can `--mode local:<spec>` scope to the app process inside CoreSimulator? Now more interesting than it was: it is what would let `diagnose_capture` report pinning without a Scope. Still v1.1.
2. ~~How should WebSocket and long-lived streaming connections appear in an interval model?~~ **Answered by P4:** classed long-lived on `101` or `text/event-stream`, owned once by the Step that opened them, never repeated as an Overlap.
3. Do Detox / Appium / Playwright / XCUITest expose per-Step timings rich enough for the `Step` contract, or do they need explicit marks? Less pressing now: `start_step`/`end_step` covers any of them without an adapter, so this is an enrichment question rather than a blocking one.
4. ~~Does `simctl keychain` require a booted simulator?~~ **Answered: yes.** See the traps list.
5. How should a non-UTF-8 body be represented? Settled for now — described in previews, base64 in slices — but nobody has used it against real binary traffic yet.
6. `run_test` blocks, so explicit Steps cannot be interleaved with it. Maestro's artifacts cover the wrapped case and `start_step`/`end_step` covers the agent-driven case, so nothing is unreachable — but a tool that wanted both would need `run_test` to run in the background. `get_timeline` returns an explanatory `note` when this bites rather than an unexplained empty result.
7. **New:** when should the version stop being `0.1.0`? It is now reported in the handshake, so it means something to a client for the first time, and nothing bumps it. A release would need a decision about what the number tracks; until publication is on the table there is nothing to tie it to.

## How the previous sessions worked

Not written down anywhere else, and it is what produced 671 tests with no known defects.

**Strict TDD, with the RED step actually observed.** Write the test, run it, read the failure, confirm it fails for the intended reason, then implement.

**Probe unknown platform APIs with a throwaway script before designing against them.** P3's whole design came out of four short probes. P4's came out of two, against a throwaway simulator, and they changed the design twice and prevented one wrong Timeline. Each took a minute. Two of them contradicted the documentation.

**Teeth-check acceptance tests by deliberately breaking the code.** Every P4 unit was checked this way — thirty-one deliberate breaks across nine commits, each expected to fail one named test. Two were more valuable than the rest: one break did not fail anything because the patch had not applied (so the check itself needs verifying), and one test turned out unable to fail at all under pytest and was rewritten.

**Record the machine's real state before mutating it, and diff afterwards.** This is how the `Empty` bug was found, and nothing else would have found it — every test passed throughout. Applies to any state the tool writes that it did not create.

**Read the artifacts a green test left behind.** New in P4 and it earned its place immediately: the acceptance test passed, and rebuilding its Timeline from the session directory afterwards showed that the mechanism it was supposed to be exercising had never fired. A green test tells you an assertion held, not that it held for the reason you intended.

**Re-read the code before believing a handoff about it.** P4's review recorded this and P5 hit it again: this file said adapter contract suites existed for both adapter families. The EventSource one was genuinely shared; the CaptureTarget one was a `CurlTarget` unit file whose test named `test_every_target_can_report_trust_without_arranging_it` asserted `hasattr(CurlTarget(), "trust_status")` — one target, and only that the method existed. It read as the shared suite. **A handoff's account of its own work is a hypothesis.** That includes this sentence.

**Check the machine afterwards, not just the tests.** P3 established this for system proxy state and it generalised in P5 to the Session root: the packaging probes left three Sessions in the developer's real home, and no test failed. The proxy discipline — record before, diff after — is really "verify state you wrote that you did not create", and the Session root is such state.

**Build the artifact and install it, do not reason about it.** P5's whole finding came from this. Every in-repo test passed against the locked dependencies throughout; the break existed only in what a *consumer* resolves, and nothing short of a clean virtualenv could see it.

## Decisions that belong to the user, still open

Carried across three sessions now. All three are harmless as they stand, and none should be acted on without being asked.

- **No git remote.** 40+ commits on one disk. The user has been told across three sessions and has not acted, so it is their standing choice. **Do not raise it again unless they bring it up.**
- **Six inert authority certificates in a working simulator.** `iPhone 17 Pro - Sidekick` (`F48AAE9F-4D3F-4817-9050-7B4B0024CF07`) accumulated 6 mitmproxy authorities during P3's first live runs. Harmless — the matching private keys were in pytest `tmp_path` directories that no longer exist. `simctl` cannot remove one root cert; the only tool is `xcrun simctl keychain <udid> reset`, which wipes that device's **entire** keychain. **Do not run it without an explicit request.**
- **The `machine` pytest marker** in `pyproject.toml` is documentation-only — the real gate is `HTTPS_PROXY_MCP_E2E=1`, for the `-m` reason in the traps list. Harmless redundancy; someone may want it gone.
- **`tests/test_simulator_e2e.py` has not been re-run** since P4's `build_target` validation and `trust_status` protocol change. All 66 simulator/trust unit tests pass and P4's live run exercised the same `start_capture` → `build_target` → trust path, so the risk is low. Re-running costs one proxy mutation, so bundle it with a future live run rather than mutating the machine for it alone. P5 needed no live run and did none.
