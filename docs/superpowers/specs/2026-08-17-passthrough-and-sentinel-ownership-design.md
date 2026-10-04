# Passthrough and sentinel ownership: design

Two defects found by using the tool on a real app, plus the visibility gap that made them cost a
whole debugging session to recognise. Both are in the two places SPEC.md calls safety-critical:
the machine-wide proxy redirect, and the teardown that undoes it.

Spec: [SPEC.md](../../../SPEC.md) · Vocabulary: [CONTEXT.md](../../../CONTEXT.md) · Decisions:
[docs/adr/](../../adr/) · Field report: `~/https-proxy-leaves-system-proxy-on.md`

## What this fixes

Three defects, in the order they bite a developer.

**1. Interception is machine-wide even when the Scope is narrow.** `capture/runtime.py:92-96`
starts mitmproxy with `mode=regular@…` and no `allow_hosts`, and `capture/reporting.py:3` states
the consequence plainly: *"Capture is unconditional: everything through the proxy is recorded."*
So while a Session records, every HTTPS connection on the Mac is served a leaf certificate signed
by the Interception Authority — the developer's browser, and every simulator on the machine. But
`install_authority` installs that Authority into exactly **one** device, the one named by `udid`.
Everything else gets `ERR_CERT_AUTHORITY_INVALID`. Reported as "I can't browse the internet and
other simulators lose access", with a Chrome interstitial naming `mitmproxy` as the issuer for
`translate.google.com`.

**2. The sentinel has no owner, and every Claude Code window runs its own server.**
`~/.claude.json` registers this server under `/mcpServers` with `env: {}`, so every window in
every project spawns one, all sharing `~/.https-proxy-mcp/proxy-state.json`, all resolving to the
same default root. `repair()` and `restore()` key on nothing but the file existing
(`targets/systemproxy.py:345`, `:382`), so three call sites will act on a **live** capture's
sentinel:

| Call site | Effect on another window's live capture |
|---|---|
| `server/__init__.py:98` `repair_before_serving` | A new window starting up un-routes the running capture and deletes its sentinel |
| `server/state.py:298` `prepare()` | Same, then binds the port — which fails if the live one holds it, and `except BaseException: teardown()` leaves the machine un-routed |
| `server/state.py:351` `teardown()` | `stop_capture` restores and deletes *another* window's sentinel, leaving it recording with routing off |

Both failure directions follow: a Session reporting `recording: true` that receives nothing, and a
machine left proxied by a window you cannot see or stop. The field report's four concurrent
processes (13:56, 14:32, 16:26, 17:40) are this condition. Note the report's own hypothesis —
*"whether it should disarm the system proxy when it stops recording"* — is not the defect. It
already disarms. The defect is that a **different** process arms and disarms out of turn.

**3. `get_status` will not say whether the machine is proxied.** `server/state.py:370-375`: the
not-recording branch returns `recording`, `detail`, `sessions_root` and stops, while the recording
branch returns `proxy: proxy_state()`. `proxy_state()` reads the sentinel, which is
process-independent — it can answer for a redirect this process does not own. So the one question
that would have ended the investigation in one call is the one the tool declines to answer when
not recording, which is exactly when it is asked.

## What the probes found

Four things established by running the real thing before designing against it. Two of them changed
the design.

1. **`allow_hosts` does what is needed, and the certificate proves it.** A live mitmdump with
   `allow_hosts=(^|\.)example\.com:[0-9]+$`, driven through `openssl s_client -proxy`:

   ```
   example.com          issuer= /CN=mitmproxy/O=mitmproxy             ← intercepted
   translate.google.com issuer= /C=US/O=Google Trust Services/CN=WR2  ← real certificate
   ```

   The out-of-scope host returned 200 with **no Authority installed anywhere**.
   `mitmproxy/addons/next_layer.py:245` matches with unanchored `re.search` against a candidate
   list of `host:port` strings drawn from the peer name, the CONNECT authority, the `Host` header
   and the TLS SNI. In regular proxy mode the authority carries the hostname, so the name is
   always available — which is what makes this viable here and would not be in transparent mode.

2. **Passthrough cannot be observed, and must not be.** `next_layer.py:129` builds ignored
   connections as `TCPLayer(context, ignore=not ctx.options.show_ignored_hosts)`. So there *is* an
   option that would let the addon count them — and its own help warns that it will keep every
   ignored connection's contents in memory, *"which can greatly increase memory usage."* On a
   machine-wide proxy
   that is the entire machine's traffic buffered in the MCP server. **So `diagnose_capture` can
   never observe that a host was passed through.** It can only deduce it from "Passthrough is on
   and this host is not in the Scope", and the design says exactly that rather than pretending to
   measure it.

3. **Process identity needs no new dependency and no parsing.** `ps -o lstart= -p <pid>` returns a
   stable string and exits non-zero for a dead pid. Compared for equality only — never parsed —
   it closes pid reuse. This matters more than it looks: with a pid-only check, a sentinel that
   survives a reboot names a pid now belonging to something unrelated, liveness says "alive", we
   decline to repair, and **a self-healing bug becomes permanent**. That is strictly worse than
   the bug being fixed.

4. **There is no sentinel on disk on this machine.** So the owner field is mandatory from the
   start with no migration path to write. A sentinel found *without* one is treated as a dead
   owner and repaired, which keeps the fail-safe pointing the same way it does today.

## Vocabulary

`CONTEXT.md` defined **Scope** as narrowing *"what is reported, never what is captured"*, and that
"never" is load-bearing in seven files. Rather than retire a sentence that is still the honest
description of regular proxy mode — and that P3 and P4 both reason from — a second term is added
and Scope keeps its meaning, gaining one clause. **Already written:**

> **Scope**: The set of hosts an agent is currently interested in. Narrows what is reported;
> narrows what is intercepted only under Passthrough.
>
> **Passthrough**: The condition of a ~~connection~~ **TLS connection** being forwarded without
> being decrypted, so the Capture Target sees the real certificate rather than one signed by the
> Interception Authority. ~~Traffic in Passthrough is invisible to this tool, and unaffected by
> it.~~ *Avoid*: ignore (mitmproxy's word for the same option), bypass (the Mac's proxy settings
> have a bypass list of their own, which is a different mechanism at a different layer), tunnel,
> allowlist.

**The struck sentence was wrong, and the hand-verification is what found it.** Passthrough scopes
**TLS** interception only. `next_layer.py`'s `_ignore_connection` returns `False` when it has no
hostname to test, and a plain-HTTP proxy request does not name its destination until the request
line is parsed — which happens *after* the layer is chosen. Only a CONNECT authority and a TLS SNI
arrive early enough. So plain HTTP is intercepted whatever the Scope says: in the live run every
recorded Exchange was plain-HTTP Metro bundler traffic from `localhost`, a host the Scope never
named. Harmless for the reported symptom, since plain HTTP has no certificate to reject, but the
two cases have opposite remedies — an out-of-scope HTTPS row needs a new Session, an out-of-scope
plain-HTTP row needs an id — so `CONTEXT.md` now states both and so does every document that
quotes it. The same correction applies to "everything else is forwarded without being opened" in
**What this fixes** and in the decision table below: read "every other TLS connection".

## Decisions

| Decision | Why, and why it looks odd |
|---|---|
| A second concept, not a redefined Scope | One parameter would be simpler. But `reporting.py`'s reader needs "capture is unconditional" to be true to understand why narrowing exists at all, and the sentence is still correct for the default. |
| `passthrough: bool = True`, inert with no Scope | A default that changes behaviour looks reckless. With no Scope there is nothing to pass through, so it only ever engages once the agent has said which hosts it cares about — and the alternative default silently breaks the developer's browser and every other simulator. |
| Passthrough retires the Scope exemptions for out-of-scope hosts | `wait_for_exchanges` and `get_exchange` are deliberately exempt from the Scope. Under Passthrough they find nothing, because the traffic was never decrypted — so a wait blocks to its timeout rather than being vetoed. Accepted knowingly: recoverable by restarting with `passthrough=False`, whereas today's default is recoverable only by already knowing about it. `diagnose_capture` says so out loud. |
| Only the Scope feeds Passthrough — never the builtin denylist | Passing OS chatter through would fix more, unscoped. But the denylist is a *reporting* narrowing whose entries stay fetchable by id on purpose, and `reporting.py` records that its **omissions** are the delicate part: `googleapis.com` is absent because for many apps it is the backend. `icloud.com` has that problem for a CloudKit app, and a wrong entry in a reporting filter costs a hidden row while a wrong entry in a transport decision costs traffic that was never captured. |
| Host-set semantics move to their own module | `in_scope` could just gain a sibling. Two independent matchers for one host-set is the trap `targets/CLAUDE.md` already records as costing a real session — *"a parser that normalises can hide a writer that does not"* — and `reporting.py`'s first sentence is "what gets reported, as distinct from what was captured", which putting interception in it would make false. |
| Owner is pid **plus** process start time | The start time looks redundant next to a liveness check. It is what stops a rebooted machine's pid collision from making the leak permanent (probe 3). |
| No Session id in the sentinel | It would give a better message. It also costs a change to `route_traffic(port)` — the `CaptureTarget` protocol — and therefore to every target and the suite parametrised over them. The pid is the actionable fact; `ps -p <pid>` supplies the rest. |
| A live foreign owner is **refused**, never taken over | Taking over is today's bug with better manners. Waiting hangs a JSON-RPC session, which this project already treats as the worst available failure — the reasoning behind the `sudo -n` non-negotiable. Refusing also makes the three careless call sites consistent with `redirect()`, which was already careful. |
| No force-release tool | An escape hatch looks necessary once we refuse. It already exists and is better: `install_handlers` registers SIGTERM, SIGINT and `atexit`, so `kill <pid>` makes the holder restore the machine on its way out. A force-release would restore *someone else's* recorded state from our process — the same disrespect for an owner that caused the bug. |
| A corrupt sentinel still wins over ownership | Ownership cannot be read out of a sentinel that cannot be parsed, so the existing fail-safe stands: switch both proxies off, move the file aside. Fidelity is already lost at that point; connectivity is not. |
| A sixth `diagnose_capture` verdict, `proxy_held_elsewhere` | Five verdicts are documented in `SPEC.md:141`. This cause's remediation shares nothing with the other five, and the verdicts exist so an agent can act — "another process holds your machine's proxy, here is its pid" should not have to be read out of a `no_connect` detail string. |
| Passthrough is proven by the **certificate**, not by absence | Asserting "no Exchange recorded" also passes if mitmproxy intercepted and we merely failed to store it. The issuer is the only thing that separates *not intercepted* from *intercepted and not recorded*, and the former is the whole claim Passthrough makes. |

**What Passthrough does not fix.** Out-of-scope connections are still *forwarded by* the proxy, so
the machine's traffic still flows through this Python process and still stops if it dies. Passthrough
shrinks the certificate blast radius, not the availability blast radius. Defect 2 and 3 are what
address that, which is why all three land together.

## Implementation

Dependency-ordered; each step green before the next.

**1. `capture/hostsets.py` — new, no behaviour change.** `host_matches(host, entries)` carries the
semantics currently in `in_scope` (entry covers subdomains, leading `*.` accepted).
`allow_host_patterns(entries)` renders the same semantics as mitmproxy regexes —
`(?:^|\.)<escaped entry>:\d+$`, anchored at the port because the candidate strings are `host:port`.
`reporting.py:68`'s `in_scope` becomes a thin delegate, keeping both existing call sites
(`reporting.py:132`, `server/run_tools.py:290,297`) untouched.
*Test:* a table of hosts run through both functions, asserting they agree — the equivalence is the
point of the module.

**2. `capture/runtime.py`.** `ProxyRuntime.__init__` gains `intercept_hosts: tuple[str, ...] | None
= None`; when set, `options.update(allow_hosts=list(allow_host_patterns(intercept_hosts)))`.
`None` means intercept everything, which is today's behaviour.
*Test:* the option is set from the hosts, and absent when `None`.

**3. Plumbing and default.** `Session.create` and `session.json` gain `passthrough`;
`CaptureState.start` gains `passthrough: bool = True` and computes
`intercept_hosts = tuple(scope) if passthrough and scope else None`; `start_capture` gains the
parameter, and its docstring at `server/session_tools.py:40` — *"`scope` narrows what later calls
report, never what is captured"* — is corrected. `tests/stub_runtime.py` accepts the new keyword.
`start_capture`'s result reports `passthrough`, so the default engaging is never silent.

**4. The Passthrough integration test.** `localhost` and `127.0.0.1` are both loopback and
distinguishable in the candidate list, so one fixture serves as both an intercepted and a
passed-through host. Two assertions: over plain HTTP, the passed-through request returns the right
body *and* records no Exchange (ruling out "dropped"); and against a TLS fixture — a throwaway leaf
minted with `mitmproxy.certs`, already a dependency — the client sees the fixture's own issuer for
the passed-through host and the Authority's for the intercepted one (ruling out "intercepted and
not recorded"). Needs no `networksetup`, so it is an ordinary integration test, not gated on
`HTTPS_PROXY_MCP_E2E`.

**5. `targets/systemproxy.py` — ownership.** The sentinel gains one key beside the existing prior
state, written by `_write_sentinel` and ignored by `SystemProxyState.from_dict`'s explicit keys:

```json
{ "service": "Wi-Fi", "web": {…}, "secure": {…}, "bypass": [],
  "owner": { "pid": 39564, "started": "Sun Aug 16 17:40:12 2026" } }
```

`started` comes from `ps -o lstart= -p <pid>` through the existing runner seam, and is compared for
equality only. A new `foreign_holder()` returns the recorded owner when it is alive and is not us;
`restore()` becomes a no-op returning `None` in that case, which leaves every existing signature
intact. The behaviour matrix:

| | owner is us | owner is dead / absent | owner is a live foreigner |
|---|---|---|---|
| `repair_before_serving` | restore | restore, report on stderr | **change nothing**, report the pid on stderr |
| `prepare()` | restore | restore | **raise**, naming the pid and `kill <pid>` |
| `restore()` / `teardown()` / `restore_on_exit()` | restore | restore | change nothing |

*Tests:* a live foreign owner is not repaired and not restored; a dead owner is; a **same pid with a
different start time** is treated as dead (probe 3); our own sentinel restores as today; a corrupt
sentinel still fails safe.

**6. `targets/base.py` and `targets/ios_simulator.py`.** `DiagnosisStatus` gains
`proxy_held_elsewhere`. ~~It is a `Literal`, so `mypy --strict` will name every site that must
handle it.~~ **That was wrong, and ticket 06 disproved it rather than inheriting it:** nothing in
`src/` dispatches on `DiagnosisStatus`, so there is no exhaustiveness site for the checker to bite
on, and no change could make it speak short of adding a `match`/`assert_never` whose only caller
would be the checklist item itself. The real fall-through was found by reading, and the prose that
enumerates the verdicts — `docs/USAGE.md` and the tool description — is pinned to
`get_args(DiagnosisStatus)` by a test instead, which is what a widened `Literal` can actually be
made to enforce here. `diagnose()` gains the branch ahead of its current `no_connect`, and appends the
Passthrough caveat to `no_connect` and `out_of_scope` when Passthrough is on. `prepare()` raises on
a foreign holder; `CaptureState.start` translates that to a `ToolError` so targets keep no MCP
import.

**7. Visibility.** `status()`'s not-recording branch gains `proxy`. `proxy_state()` gains `owner`
(the pid, and whether it is this process) and branches its sentence: *"it will be restored to
server=… on stop_capture"* is a false promise for a sentinel we do not own, and would have kept
being wrong in the new world unless it branches.

## Verification

- `uv run pytest -q` — 675 passing now; this adds tests, breaks none knowingly.
- `uv run ruff check .`, `ruff format --check .`, `uv run mypy` (strict, `src/`).
- The three gated live machine tests. Read `.claude/skills/live-machine-tests/SKILL.md` first.
- **The real check, on this machine:** capture scoped to `kivid.com.br` with the default
  `passthrough`, then load `translate.google.com` in Chrome and use a second simulator. Both must
  be unaffected, and the app's own traffic must still appear in `list_exchanges`. This is the
  reported symptom, and only this answers it.
- **The ownership check:** two Claude Code windows. Start a capture in one; in the other,
  `get_status` must report the redirect and its owning pid, `start_capture` must refuse naming that
  pid, and `stop_capture` in the first must restore the machine exactly.

**Both were done by hand and the results are recorded in `HANDOFF.md`, not here** — a design
document is intent and the handoff is measurement. In short: the host browsed normally and the
issuers proved it; a simulator that does **not** trust the Authority kept its network, which is the
decisive result; the ownership refusal named the right pid and teardown diffed identical
field-for-field. Two things did not go to plan. `diagnose_capture` answered `tls_failed` for the
app's host and this was read as certificate pinning, so "the app's own traffic must still appear"
was filed as unmeetable with that build — **that reading was wrong**, and on 2026-08-18 a full login
flow was captured and decrypted from the same build. The verdict's count is machine-wide, which is a
defect in its wording rather than a Passthrough failure; `HANDOFF.md` carries the negative control
that proved it. And the claim that everything outside the Scope is forwarded unopened turned out to
be false for plain HTTP; see the correction under **Vocabulary**.

## Paper trail

`CLAUDE.md` is a symlink to `AGENTS.md`, so that is one edit.

- **ADR-0007** Passthrough: scoped interception. **ADR-0008** the sentinel names its owner.
- **`.claude/agents/doc-drift-reviewer.md:16`** — the four known SPEC.md drifts are enumerated
  *there*. Unless the new ones are added, the reviewer reports "capture is unconditional" and the
  five-verdict list as real findings on every future review.
- **`AGENTS.md`** — the "drifted in four places" non-negotiable, and new decisions-table rows.
- **`docs/USAGE.md`** — the authoritative reference, whose signatures a test checks against the
  live schemas.
- **`SKILL.md`** — `passthrough`, and the new verdict. No relative links into the repo.
- **`HANDOFF.md`** — current state and what these two ADRs settle.
- **`docs/v1.1-candidates.md:160`** quotes *"host filtering is not a convenience here, it is the
  only available mechanism"* while arguing for `--mode local`. Passthrough weakens that argument,
  and that document is a live recommendation rather than a historical record, so it is corrected.
- **`SPEC.md` and `PLAN.md` are not touched.** Both are records of intent.

**Done, and it grew.** The reviewer's list went from four divergences to **eight**: capture is no
longer unconditional, `start_capture` gained a parameter SPEC.md's row does not list, the verdicts
are six not five, and the teardown guarantee's layer 3 is now qualified by ownership. The list is
now enumerated as numbered items and a test couples its length to the count both it and `AGENTS.md`
state, because a count kept in prose in two files is how one of them goes stale. `tests/CLAUDE.md`
and the per-module trap files gained what this work learned the hard way, and the suite's size is
now stated in `AGENTS.md` alone for the same reason.
