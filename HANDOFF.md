# Handoff — 2026-08-05 (P5 complete — v1 feature-complete)

Picking this up in a fresh session. Everything needed is in the repo; this file says where to start and what not to relitigate.

## What moved out of this file

Four sections that were durable knowledge rather than session state now live where they are
actually needed, and this file no longer repeats them:

| Was here | Now |
|---|---|
| Read these first, in this order | [AGENTS.md](./AGENTS.md) |
| What exists now (the module map) | [AGENTS.md](./AGENTS.md) |
| Decisions already made — do not reopen | [AGENTS.md](./AGENTS.md) |
| Traps discovered the hard way | the `CLAUDE.md` beside each module — `capture/`, `targets/`, `eventsources/`, `runs/`, `server/`, `tests/` |

The reasoning is in [ADR-0006](./docs/adr/0006-traps-live-beside-their-modules.md). The short
version: a trap about `networksetup` is worth having when you are editing `systemproxy.py`, and
worth nothing at the bottom of a file nobody opens. `AGENTS.md` is loaded on every request;
`CLAUDE.md` at the root is a symlink to it, which is how Claude Code reads it.

What stays here is what a *handoff* is for: current state, what is deliberately unbuilt, open
questions, and the decisions that are the user's to make.

## State

**P0 through P5 complete and committed — every phase in PLAN.md.** Clean tree. The remote is
`https://github.com/AdonisLima/https-proxy-mcp.git` (private).

Verify: `uv run pytest -q && uv run ruff check . && uv run ruff format --check . && uv run mypy`
→ 675 passed, 3 skipped, all checks passed, no type errors.

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

## Next: publication is a decision, not a phase

**The user chose not to publish**, asked explicitly, in the session that finished P5. Do not treat it as an outstanding task or re-raise it unprompted. If they ask, this is what is already true and what is not:

Already done and verified locally, by `tests/test_distribution.py`:

- `uv build` produces a clean wheel and sdist — package only, no tests, no fixtures, no `__pycache__`.
- The wheel installs into a fresh virtualenv under **every** interpreter `requires-python` admits, and the installed console script serves MCP and captures real traffic from a directory outside the repo.
- Metadata: MIT (`License-Expression` plus the text under `.dist-info/licenses/`), keywords, classifiers including `Operating System :: MacOS :: MacOS X`, and `serverInfo.version` reporting ours.

Not done, and only a package index can do it: that the name resolves, that the long description renders, that `uvx https-proxy-mcp` with no `--from` finds anything. If publication ever happens, TestPyPI first — the local suite cannot tell you a description failed to render.

**v1.1 is the other direction**, and it now has its own document: **[docs/v1.1-candidates.md](./docs/v1.1-candidates.md)** collects every deferral, open question and ADR consequence from the five files they were scattered across, sizes each one, and recommends a scope. Start there rather than re-deriving the list.

Its short version: ship the three cheap additions first (a tool for the pending registry, reading Runs back from disk, `json_path`), then write/interception tools behind the flag, and run `--mode local` as a one-day **probe** rather than a feature — the answer to open question 1 changes what `diagnose_capture` can promise. Android is the biggest capability and the biggest risk, and SPEC.md's assessment that its system-CA procedure is the most brittle part of the domain has not been revisited; it deserves its own release.

Three candidates in that document are **new** rather than carried over, found by reading the v1 code: the pending registry reaches a caller only through `get_timeline` and a bare count in `get_status`, `RunRegistry` has no read path at all, and `capture/addon.py` has no `websocket_message` hook.

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

Not written down anywhere else, and it is what produced 675 tests with no known defects.

**Strict TDD, with the RED step actually observed.** Write the test, run it, read the failure, confirm it fails for the intended reason, then implement.

**Probe unknown platform APIs with a throwaway script before designing against them.** P3's whole design came out of four short probes. P4's came out of two, against a throwaway simulator, and they changed the design twice and prevented one wrong Timeline. Each took a minute. Two of them contradicted the documentation.

**Teeth-check acceptance tests by deliberately breaking the code.** Every P4 unit was checked this way — thirty-one deliberate breaks across nine commits, each expected to fail one named test. Two were more valuable than the rest: one break did not fail anything because the patch had not applied (so the check itself needs verifying), and one test turned out unable to fail at all under pytest and was rewritten.

**Record the machine's real state before mutating it, and diff afterwards.** This is how the `Empty` bug was found, and nothing else would have found it — every test passed throughout. Applies to any state the tool writes that it did not create.

**Read the artifacts a green test left behind.** New in P4 and it earned its place immediately: the acceptance test passed, and rebuilding its Timeline from the session directory afterwards showed that the mechanism it was supposed to be exercising had never fired. A green test tells you an assertion held, not that it held for the reason you intended.

**Re-read the code before believing a handoff about it.** P4's review recorded this and P5 hit it again: this file said adapter contract suites existed for both adapter families. The EventSource one was genuinely shared; the CaptureTarget one was a `CurlTarget` unit file whose test named `test_every_target_can_report_trust_without_arranging_it` asserted `hasattr(CurlTarget(), "trust_status")` — one target, and only that the method existed. It read as the shared suite. **A handoff's account of its own work is a hypothesis.** That includes this sentence.

**Check the machine afterwards, not just the tests.** P3 established this for system proxy state and it generalised in P5 to the Session root: the packaging probes left three Sessions in the developer's real home, and no test failed. The proxy discipline — record before, diff after — is really "verify state you wrote that you did not create", and the Session root is such state.

**Build the artifact and install it, do not reason about it.** P5's whole finding came from this. Every in-repo test passed against the locked dependencies throughout; the break existed only in what a *consumer* resolves, and nothing short of a clean virtualenv could see it.

**Writing the documentation is a test of the code.** Two defects came out of writing `docs/USAGE.md` and `docs/INSTALL.md`, both because stating a guarantee in prose forces you to check it holds everywhere you have just claimed it does: `tail_exchanges` ignored the page-size ceiling every other listing enforced, and the README's quick-start command had never worked. Neither was findable by reading the code with no claim in hand. Corollary: capture the output you document from a **real** run — the first attempt at the Timeline example produced an empty one, and pasting it would have documented a non-result as the feature.

## Decisions that belong to the user, still open

Carried across three sessions now. All three are harmless as they stand, and none should be acted on without being asked.

- ~~**No git remote.**~~ **Resolved.** The user supplied one —
  `https://github.com/AdonisLima/https-proxy-mcp.git`, private — during the harness session, and
  chose **private with `macos-15` runners** for CI over making the repository public. That is a
  cost decision worth recording: macOS runners bill against the free allowance at **10×**, so the
  2,000 monthly minutes are about 200 real macOS minutes, and a ~2-minute run costs ~20 of them.
  Roughly 100 runs a month. The harness job runs on `ubuntu-latest` at 1× for exactly this
  reason. If the run count ever becomes the binding constraint, making the repository public
  removes it entirely — standard runners are free for public repositories.
- **Six inert authority certificates in a working simulator.** `iPhone 17 Pro - Sidekick` (`F48AAE9F-4D3F-4817-9050-7B4B0024CF07`) accumulated 6 mitmproxy authorities during P3's first live runs. Harmless — the matching private keys were in pytest `tmp_path` directories that no longer exist. `simctl` cannot remove one root cert; the only tool is `xcrun simctl keychain <udid> reset`, which wipes that device's **entire** keychain. **Do not run it without an explicit request.**
- **The `machine` pytest marker** in `pyproject.toml` is documentation-only — the real gate is `HTTPS_PROXY_MCP_E2E=1`, for the `-m` reason in the traps list. Harmless redundancy; someone may want it gone.
- **`tests/test_simulator_e2e.py` has not been re-run** since P4's `build_target` validation and `trust_status` protocol change. All 66 simulator/trust unit tests pass and P4's live run exercised the same `start_capture` → `build_target` → trust path, so the risk is low. Re-running costs one proxy mutation, so bundle it with a future live run rather than mutating the machine for it alone. P5 needed no live run and did none.
