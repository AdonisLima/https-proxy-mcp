# Handoff — 2026-08-17 (v1 feature-complete; Passthrough and sentinel ownership landed)

Picking this up in a fresh session. Start with [AGENTS.md](../AGENTS.md) — it is loaded
automatically and says where everything is. This file is current state and open decisions only.

## What moved out of this file

Six sections that were durable knowledge or repeatable procedure rather than session state
now live where they are actually needed, and this file no longer repeats them:

| Was here | Now |
|---|---|
| Read these first, in this order | [AGENTS.md](../AGENTS.md) |
| What exists now (the module map) | [AGENTS.md](../AGENTS.md) |
| Decisions already made — do not reopen | [AGENTS.md](../AGENTS.md) |
| Traps discovered the hard way | the `CLAUDE.md` beside each module — `capture/`, `targets/`, `eventsources/`, `runs/`, `server/`, `tests/` |
| Manual recovery | [.claude/skills/live-machine-tests/](../.claude/skills/live-machine-tests/SKILL.md) |
| How the previous sessions worked | [.claude/skills/verification-discipline/](../.claude/skills/verification-discipline/SKILL.md) |

The reasoning is in [ADR-0006](./adr/0006-traps-live-beside-their-modules.md). The short
version: a trap about `networksetup` is worth having when you are editing `systemproxy.py`, and
worth nothing at the bottom of a file nobody opens. `AGENTS.md` is loaded on every request;
`CLAUDE.md` at the root is a symlink to it, which is how Claude Code reads it.

What stays here is what a *handoff* is for: current state, what is deliberately unbuilt, open
questions, and the decisions that are the user's to make.

## State

**P0 through P5 complete and committed — every phase in PLAN.md.** The remote is
`https://github.com/AdonisLima/https-proxy-mcp.git` (private).

Verify, or just run `/verify`. The suite's current size is stated in
[AGENTS.md](../AGENTS.md#verify) and nowhere else, because two copies of one number is how one of
them goes stale:

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy               # strict, src/ only
```

v1 is feature-complete. What remains is not a phase: it is **publication** (a decision, see below) and **v1.1** (Android, write tools, `--mode local`). Nothing in the plan is outstanding.

**Two defects found by using the tool on a real app have been fixed since**, on the
`passthrough-and-sentinel-ownership` branch, together with the visibility gap that made them cost a
whole debugging session to recognise. Both were in the two places SPEC.md calls safety-critical.

- **Passthrough** ([ADR-0007](./adr/0007-passthrough-scoped-interception.md)). A `scope` now
  also decides which hosts get decrypted, so a scoped Session stops serving `mitmproxy`-signed
  certificates to the developer's browser and to every other simulator on the machine.
  `start_capture` gains `passthrough: bool = True`, inert with no `scope`, and reports which way it
  went. New module: `capture/hostsets.py`.
- **The sentinel names its owner**
  ([ADR-0008](./adr/0008-the-sentinel-names-its-owner.md)). Every editor window runs its own
  server against one shared record of the machine's prior proxy settings, and three call sites acted
  on any record they found. They now branch on ownership: a live foreign owner is refused, left
  alone and reported. `diagnose_capture` gains a sixth verdict, `proxy_held_elsewhere`, and
  `get_status` answers the proxy question whether or not this Session is recording.

Both records list what was rejected as well as what was chosen — take-over, waiting, a
force-release tool, letting the denylist feed Passthrough — because those are the alternatives a
future reader will independently think of.

**One acceptance criterion was retired rather than met, deliberately.** The design asserted that
widening the `DiagnosisStatus` `Literal` would make `mypy --strict` name every site that must handle
the new verdict. That premise is false here: nothing in `src/` dispatches on `DiagnosisStatus`, so
there is no exhaustiveness site for the checker to bite on, and no change could make it speak short
of a `match`/`assert_never` whose only caller would be the criterion itself. The operative half —
no site silently falling through — does hold and was proved by disabling the fix. What a widened
`Literal` *can* enforce here is the prose, so `docs/USAGE.md`, the tool description and the target
module are pinned to `get_args(DiagnosisStatus)` by a test instead. The false premise is struck
through in the design document; do not re-derive it.

### What the hand-verification actually measured

Run on this machine on 2026-08-17 against a real server over stdio, scoped to `kivid.com.br` with
the default `passthrough`. Two things no automated test can assert — a browser and a second
simulator — plus two windows. **Measurements, not expectations.**

- **The host browses normally, and the certificate is what proves it.** A client that reads the
  macOS system proxy fetched `https://translate.google.com/` through the capture port and got
  **200**, with its certificate accepted against an ordinary trust store. At the proxy, issuer by
  issuer: `translate.google.com` → *Google Trust Services*; `b2c-api-uat.kivid.com.br` →
  *mitmproxy*. Interception is confined to the Scope in both directions.
  **`curl` is useless for this** — it honours `http_proxy`/`https_proxy` and ignores the macOS
  system-proxy dictionary, which is the asymmetry that made the original failure look like a healthy
  API. Use something that reads the system configuration.
- **An untrusted simulator keeps its network, which is the decisive result.** A device verified as
  **not** trusting the Interception Authority loaded `https://example.com/` in Safari with no warning
  at all while the machine was fully proxied. Only an unopened connection can do that. This is
  exactly where the reported interstitial used to appear.
- **Two windows no longer destroy each other.** With one server holding the redirect, a second
  server's startup repair left it alone and said so on stderr; `start_capture` was refused naming
  that pid; `get_status` reported `owner: {pid: …, ours: false}` and made no promise to restore;
  `diagnose_capture` returned `proxy_held_elsewhere` with the pid and the remedy.
- **Teardown is exact.** A field-for-field `diff` of `-getwebproxy`, `-getsecurewebproxy` and
  `-getproxybypassdomains` across **both** network services, against a pre-flight baseline, came back
  identical — including the configured-but-disabled `127.0.0.1:9090` this machine carries, which is
  the case the whole sentinel protocol exists for.

### What did not go as planned

Four things, recorded rather than smoothed over.

1. **This handoff recorded the app under test as certificate-pinned. That was wrong, and the thing
   at fault was the verdict.** *(Corrected 2026-08-18; the original claim is kept below because the
   reasoning that produced it was sound and still needs answering.)*

   What was recorded on 2026-08-17: with capture live and the device trusting the Authority,
   `diagnose_capture` answered `tls_failed` for `b2c-api-uat.kivid.com.br`, read as the pinning
   signature P3 built the verdict for, so the check *"the app's own traffic still appears in a
   listing"* was filed as unmeetable without a de-pinned build.

   What was measured on 2026-08-18: a full login flow was captured and **decrypted** under
   `scope=["kivid.com.br"]` with Passthrough on — `/v2/public/users/<redacted CPF>/next-step` → 200,
   `POST /v2/auth/token` → 200, `/v2/auth/userinfo`, `/v2/users/me`, three `POST /graphql`, all 200
   — and `diagnose_capture` answered **`ok`**, `exchanges: 22, reported: 11, tls_failures: 0`. The
   11 unreported rows are Metro's plain-HTTP bundle traffic on `localhost`, which is item 2 below
   visible in one line. Separately, the user's own Sessions from 2026-08-16 already held **189
   decrypted exchanges** to that same host. A pinned app produces none of that. **The checklist item
   is met and no de-pinned build is needed.**

   **The defect, reproduced deliberately.** The `tls_failed` count is machine-wide. One capture
   Session, two legs, nothing about the device changed in between: leg A drove the real app on a
   device that trusts the Authority — `ok`, 9 in-scope exchanges, `tls_failures: 0`. Leg B had one
   unrelated client on the same Mac make **one** request to the same in-scope host
   (`python3 -c "import urllib.request; urllib.request.urlopen('https://<host>/')"` — `urllib`
   honours the macOS system proxy, `curl` does not), and the verdict flipped to `tls_failed`,
   naming a device that was working perfectly at that moment. Every client reaches the proxy over
   loopback, so there is no peer address that could separate a simulator from a browser: the count
   **cannot** be made per-client, and only the wording could be fixed. It has been — the verdict now
   states the count is machine-wide and offers pinning as the leading candidate rather than as fact,
   pinned by three tests that go red if either claim returns.

   The field report is still not settled, in the other direction now: it attributed the app's
   `A TLS error caused the secure connection to fail` to the stale system proxy, which `scutil`
   proved independently real. Pinning is no longer available as a co-cause.
2. **Passthrough scopes TLS interception only, and plain HTTP is still intercepted.** All eleven
   Exchanges captured were Metro bundler traffic over plain HTTP from `localhost`, which the Scope
   never named. The cause is in mitmproxy's own layer decision: `_ignore_connection` answers "do not
   ignore" when it has no host name to test, and a plain-HTTP proxy request does not name its
   destination until the request line is parsed — after the layer has been chosen. This contradicted
   prose this branch had already committed, in `CONTEXT.md` and in the design document, and both were
   corrected rather than repeated. It is harmless for the reported symptom and arguably useful, but
   the two cases have opposite remedies, so both are now always stated.
3. **One gated live machine test flaked; all three are now scoped, and the flake's likeliest cause
   is gone.** `test_a_failing_maestro_flow_produces_an_explanatory_timeline` failed in a combined
   run and passed on its own immediately afterwards, with nothing changed in between. The plausible
   cause was in the run's own log: the live tests ran **unscoped**, so Passthrough was inert, and
   the proxy logged client handshake failures for `discord.com`, `www.figma.com`,
   `us.i.posthog.com` and — the interesting one — `api.copilot.mobile.dev`, **Maestro's own
   endpoint**. The harness's network calls were being broken by the capture it had just arranged.

   Fixed on 2026-08-17, and the route there is the interesting part. `test_simulator_e2e.py`'s two
   tests took `scope=["example.com"]` and passed. The Maestro test could not simply be scoped: the
   overlap it proves was Safari's incidental chatter, which a Scope forwards unopened, and naming
   `apple.com` to keep it brought 50 failed handshakes from the Mac and the machine's other
   simulators into scope, turning the verdict into `tls_failed`. So it now **arranges** its own
   overlap — it opens `http://192.0.2.1/never-answers`, an RFC 5737 documentation address, before
   the failing assertion, and the request is still outstanding when the assertion runs. A scoped
   run logs **zero** handshake failures. Two things had to be measured to get there, and both
   contradicted what was written down: Safari *does* fetch a private-IP URL, and a request to the
   host's own LAN address never reaches the proxy because macOS bypasses local-subnet addresses
   with an empty bypass list. Both are now in the trap files.
4. **Seven orphaned servers were alive**, three of them the exact processes the field report named,
   still running 28 hours later. None held a port or a sentinel, so they were inert — but seven
   servers sharing one record is a much larger version of the condition this branch fixes, and it is
   evidence that servers are not reaped when their client goes away. They were killed with
   `SIGTERM` at the user's request, which is the correct signal because the handlers run and restore
   on the way out where `kill -9` would skip them; all had no record to restore and the machine's
   state was identical before and after. **A measurement trap found while looking for them:**
   `ps -o pid,lstart,command | grep <name>` truncates near 80 columns and this repo's path pushes the
   process name past the cut, so they were reported three times as absent. `pgrep -fl` sees them.

**Machine afterwards:** the recorded three-command state diffed identical, the sentinel was absent,
no server was left alive, no Session leaked into the real root, and the throwaway device was cleaned
up.

**`~/.https-proxy-mcp/` exists on this machine**, against the non-negotiable — a `mitmproxy/`
confdir plus **five** Sessions dated 2026-08-14 to 2026-08-17. They are not all the same thing, and
the difference matters, because the non-negotiable's point is that if this root exists something
leaked and somebody should look:

- **Four are the developer's own hand-driven use of the tool**, and none of them came from this
  branch: no `session.json` among them carries the `passthrough` key ticket 03 added, and all four
  predate the branch's first commit. They are the Sessions the field report was written from.
- **The fifth, `20260817T175017-684759`, is this branch's own doing** — the hand-verification in
  `.scratch/passthrough-and-sentinel-ownership/ticket-07-observations.md`. It carries
  `"passthrough": true`, `scope: ["kivid.com.br"]`, target Main, and eleven Exchanges, all of them
  plain-HTTP Metro bundler traffic. It was written to the real root deliberately: the whole point of
  that verification was the real machine, so redirecting the root would have tested something else.

Check this by measurement rather than by memory —
`grep -l passthrough ~/.https-proxy-mcp/sessions/*/session.json` names exactly the Sessions this
branch created, and an earlier draft of this paragraph said "four" and called it verified, which is
the defect class this branch exists to remove.

**Nothing was removed** — those directories hold unredacted bodies, including real UAT credentials, so
the non-negotiable says look before deleting and what to do with them is the user's call. There is no
`proxy-state.json`, so the machine is not currently proxied.

The 3 skipped are the live machine tests. They are the only things in the project that change the developer's machine, so they are gated — the runbook is `/live-e2e`, or
[.claude/skills/live-machine-tests/](../.claude/skills/live-machine-tests/SKILL.md):

```bash
HTTPS_PROXY_MCP_E2E=1 uv run pytest tests/test_simulator_e2e.py tests/test_maestro_e2e.py -q
```

`uv run pytest -m "not integration"` runs the fast majority in about eleven seconds. The slowest thing in the ordinary suite is `tests/test_distribution.py` — 6.2s per interpreter, because it builds a real wheel and installs it into a throwaway virtualenv for each. It changes nothing outside `tmp_path`.

**The harness is new as of this session** and is described in AGENTS.md rather than here: a
committed gate on the six documented hazards (`.claude/hooks/`, 33 tests), three skills, three
commands, a doc-drift reviewer, `mypy --strict` on `src/`, pre-commit, and CI. `harness-score`
reports 108/108, L4, and CI gates on `--min-level 4` so it cannot quietly decay.

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

**v1.1 is the other direction**, and it now has its own document: **[docs/v1.1-candidates.md](./v1.1-candidates.md)** collects every deferral, open question and ADR consequence from the five files they were scattered across, sizes each one, and recommends a scope. Start there rather than re-deriving the list.

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

## Deliberately not built yet

**[docs/v1.1-candidates.md](./v1.1-candidates.md).** Every item that used to be listed here
is there, sized and ranked, with a recommended scope — `json_path` (§9), WebSocket message
capture (§6), reading Runs back from disk (§8), Android, write tools and `--mode local`
(§1–§3), and the `simplify` pass, publication and the `SKILL.md` reverse check under §10
Housekeeping.

The list was duplicated in both files, which is how one of them goes stale. That document is
the maintained one; this section is a pointer and nothing else.

## Open questions

1. Can `--mode local:<spec>` scope to the app process inside CoreSimulator? Still v1.1, and it now buys **less** than it did: ADR-0007 delivered the certificate half without it, so what remains is pinning attribution on an unscoped Session, plain HTTP, and the availability blast radius. Sized again in [docs/v1.1-candidates.md §3](./v1.1-candidates.md) rather than here.
2. ~~How should WebSocket and long-lived streaming connections appear in an interval model?~~ **Answered by P4:** classed long-lived on `101` or `text/event-stream`, owned once by the Step that opened them, never repeated as an Overlap.
3. Do Detox / Appium / Playwright / XCUITest expose per-Step timings rich enough for the `Step` contract, or do they need explicit marks? Less pressing now: `start_step`/`end_step` covers any of them without an adapter, so this is an enrichment question rather than a blocking one.
4. ~~Does `simctl keychain` require a booted simulator?~~ **Answered: yes.** See `src/https_proxy_mcp/targets/CLAUDE.md`.
5. How should a non-UTF-8 body be represented? Settled for now — described in previews, base64 in slices — but nobody has used it against real binary traffic yet.
6. `run_test` blocks, so explicit Steps cannot be interleaved with it. Maestro's artifacts cover the wrapped case and `start_step`/`end_step` covers the agent-driven case, so nothing is unreachable — but a tool that wanted both would need `run_test` to run in the background. `get_timeline` returns an explanatory `note` when this bites rather than an unexplained empty result.
7. **New:** when should the version stop being `0.1.0`? It is now reported in the handshake, so it means something to a client for the first time, and nothing bumps it. A release would need a decision about what the number tracks; until publication is on the table there is nothing to tie it to.

## Decisions that belong to the user, still open

One of the four is now resolved. The rest are harmless as they stand, and none should be acted on without being asked.

- ~~**No git remote.**~~ **Resolved.** The user supplied one —
  `https://github.com/AdonisLima/https-proxy-mcp.git`, private — during the harness session, and
  chose **private with `macos-15` runners** for CI over making the repository public. That is a
  cost decision worth recording: macOS runners bill against the free allowance at **10×**, so the
  2,000 monthly minutes are about 200 real macOS minutes, and a ~2-minute run costs ~20 of them.
  Roughly 100 runs a month. The harness job runs on `ubuntu-latest` at 1× for exactly this
  reason. If the run count ever becomes the binding constraint, making the repository public
  removes it entirely — standard runners are free for public repositories.
- **Six inert authority certificates in a working simulator.** `iPhone 17 Pro - Sidekick` (`F48AAE9F-4D3F-4817-9050-7B4B0024CF07`) accumulated 6 mitmproxy authorities during P3's first live runs. Harmless — the matching private keys were in pytest `tmp_path` directories that no longer exist. `simctl` cannot remove one root cert; the only tool is `xcrun simctl keychain <udid> reset`, which wipes that device's **entire** keychain. **Do not run it without an explicit request** — the committed PreToolUse gate now denies that command outright, with the reason, rather than relying on this paragraph being read.
- **The `machine` pytest marker** in `pyproject.toml` is documentation-only — the real gate is `HTTPS_PROXY_MCP_E2E=1`, for the `-m` reason in `tests/CLAUDE.md`. Harmless redundancy; someone may want it gone.
- **`tests/test_simulator_e2e.py` has not been re-run** since P4's `build_target` validation and `trust_status` protocol change. All 66 simulator/trust unit tests pass and P4's live run exercised the same `start_capture` → `build_target` → trust path, so the risk is low. Re-running costs one proxy mutation, so bundle it with a future live run rather than mutating the machine for it alone. P5 needed no live run and did none.
