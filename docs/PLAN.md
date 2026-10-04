# https-proxy-mcp — Build plan (v1)

Six phases. Each has an acceptance criterion you can actually check, and each leaves the tool more useful than the last. Phases 1–2 need no mobile tooling at all, which is deliberate: the core is testable against `curl` before any simulator is involved.

Spec: [SPEC.md](./SPEC.md) · Vocabulary: [CONTEXT.md](../CONTEXT.md) · Decisions: [docs/adr/](./adr/)

## P0 — Scaffold

`uv init`, Python ≥ 3.12, `mitmproxy` and the MCP SDK as dependencies, `pytest`, `ruff`. Package layout:

```
src/https_proxy_mcp/
├── capture/          # addon, ExchangeStore, body spill, NDJSON
├── correlate/        # Step/Exchange overlap, Timeline assembly
├── serialize/        # redaction, truncation, pagination
├── targets/          # CaptureTarget implementations
├── eventsources/     # EventSource implementations
└── server/           # MCP tool registration
```

**Done when:** `uvx --from . https-proxy-mcp` starts and responds to an MCP handshake with zero tools registered.

**Verify at this point:** the actual Python floor required by the resolved mitmproxy version. The spec assumes ≥ 3.12; confirm rather than inherit the assumption.

## P1 — Capture core

Addon writing into `ExchangeStore`: bounded metadata ring buffer (5000), bodies spilled to `bodies/`, append-only `exchanges.ndjson`, monotonic Exchange ids, eviction counter. Redacting serializer with the stable-digest scheme (`<redacted:sha256:xxxxxxxx>`) so repeated tokens are comparable without being visible.

**Done when:** `mitmdump` with the addon loaded, driven by `curl` against a local fixture server, produces correct Exchange metadata and spilled bodies; a unit test asserts no `Authorization` value survives default serialization.

## P2 — Read-only MCP tools

`start_capture` / `stop_capture` / `get_status`, then `list_exchanges`, `get_exchange`, `get_exchange_body`, `search_exchanges`, `tail_exchanges`, `wait_for_exchanges`, `export_curl`. Cursor pagination throughout. `CurlTarget` as a trivial `CaptureTarget` so the whole surface is exercisable without a simulator.

**Done when:** a real MCP client can capture `curl` traffic, page through it, fetch one body slice, and have `wait_for_exchanges` return on a request fired after the call was made.

## P3 — iOS Simulator target

`networksetup` routing with the sentinel protocol (write prior state → `atexit` and signal handlers → repair-on-start). `simctl keychain add-root-cert` for trust, auto-invoked by `start_capture`. `install_authority_system` as a separate explicit tool. `diagnose_capture` distinguishing `no_connect` / `tls_failed` / `untrusted_authority` / `out_of_scope`. Builtin denylist.

**Done when:** capturing a real simulator app's HTTPS traffic end to end — and, tested explicitly, `kill -9` on the server followed by a restart leaves the system proxy correctly restored.

That last check is the one that matters most in this phase. Test it deliberately rather than trusting the handlers.

## P4 — Runs and Timelines

`run_test` process wrapper. `start_step` / `end_step` as the universal Event Source. Maestro Event Source parsing `commands-*.json`, with both output flags pointed at one directory. Correlator implementing owner-plus-overlap. `get_timeline` with summary-first rendering. `export_har`, redacted by default.

**Done when:** a deliberately failing Maestro flow produces a Timeline that shows the failing assertion *and* the Exchange still in flight when it began — the worked example from the spec, reproduced for real.

This is the phase that delivers the reason the project exists. Everything before it is plumbing.

**Met.** `tests/test_maestro_e2e.py` ran against a throwaway simulator: a real failing Maestro flow, exit 1, three Steps parsed from the real artifact, 62 Exchanges captured of which 15 fell inside the Run window, and a Timeline row reading *`assertCondition: "ThisTextIsDefinitelyNotOnScreen"` began while `#46 GET token.safebrowsing.apple/...` (200) was still in flight*. The machine's proxy configuration was byte-identical afterwards.

The overlap there is real but incidental — Safari's own traffic during a 17-second assertion. The deterministic proof of the in-flight path lives in `tests/test_pending_e2e.py`, which drives it through a real proxy with `curl` and a socket that never answers; the first attempt to force it inside the simulator test did not work, and that is recorded in HANDOFF.md's traps.

Open question 2 is answered: a long-lived connection (status `101`, or `text/event-stream`) is owned once by the Step that opened it and never repeated as an Overlap.

## P5 — Hardening and distribution

`SKILL.md`, README, session retention and pruning, `0700` on session directories, adapter contract test suites, PyPI packaging for `uvx`.

**Done when:** installable and usable on a second machine from published artifacts alone.

**Met, with one part explicitly not claimed.** Retention, pruning and `0700` were already done and needed only checking off. The EventSource contract suite was already shared; the CaptureTarget one was not — it was a `CurlTarget` unit file with one test named as though it covered every target — so `tests/test_targets.py` now ends in a suite parametrised over both, asserting only what `targets/base.py` promises.

`SKILL.md` was written after reading all twenty tool docstrings as a group, which is what the surface needed: three tools are the path (`start_capture` → `run_test` → `get_timeline`) and the other seventeen are drill-down. The same reading found `INSTRUCTIONS` never mentioned `run_test` or `get_timeline` at all — the server's own self-description omitted the reason the project exists.

Packaging is executed rather than asserted, in `tests/test_distribution.py`: `uv build`, install the wheel into a fresh virtualenv under **every** interpreter `requires-python` admits, drive the installed console script from a directory outside the repo, and capture real traffic through it. That test earned its keep immediately — with `mcp>=1.29.0` uncapped, the clean install on 3.13 resolved mcp 2.0.0, which removed `mcp.server.fastmcp` outright, and the server could not import. 3.12 had escaped only through a warm cache, not through any constraint. Both dependencies are now bounded above.

**Not claimed:** publication. There is no PyPI release, so "the name resolves" and "the long description renders" are untested, and `uvx https-proxy-mcp` without `--from` finds nothing. That is a deliberate choice rather than a gap in the work — the local proof is as far as one machine goes, and the rest is a decision to publish. Licensed MIT, which is what a release would need first.

## Testing

- **Unit** — ring-buffer eviction, index correctness, redaction, interval overlap arithmetic.
- **Adapter contract suites** — one shared suite every `CaptureTarget` and every `EventSource` must pass. This is what keeps the core honest as adapters multiply, and it is what makes Android in v1.1 an addition rather than a refactor.
- **Integration** — real MCP client against a real server against a fixture HTTP server.
- **Golden-file Timelines** — recorded Step-log and Exchange pairs with expected output, including an overlap case, a Background-only case, and a Step with no Exchanges.

## Risks

| Risk | Severity | Mitigation |
|---|---|---|
| Server dies without restoring the system proxy | **High** — breaks the machine's networking | Three-layer teardown; repair-on-start is the layer that covers `kill -9`. Explicitly tested in P3. |
| Certificate pinning in the app under test | High — no capture for pinned hosts | `diagnose_capture` reports pinning as such rather than "no traffic". Correctly-pinned release builds are out of scope. |
| Session directory holds unredacted secrets | Medium | `0700`, documented, pruned to 10 sessions. |
| No process attribution in regular proxy mode | Medium | Run-window filtering plus denylist; accepted consequence of ADR-0002. |
| `Step` turns out to be Maestro-shaped by accident | Medium | Explicit Steps ship in v1 alongside the Maestro parser, so two Event Sources exist from the start. |
| Context blowout | Medium | Bodies unreachable from `list_exchanges` by construction; summary-first Timelines. |

## Deferred

**v1.1:** Android Emulator target (`subject_hash_old` naming, `-writable-system` vs Magisk); write/interception tools behind `--enable-write-tools`; `--mode local` as an opt-in iOS path once its CoreSimulator scoping is verified.

**Later:** physical devices via WireGuard mode; browser targets; further Event Sources (Detox, Appium, Playwright, XCUITest); MCP notification-based push if client support becomes dependable.

All of the above, plus the deferrals and open questions recorded elsewhere, are collected, sized and ranked in **[docs/v1.1-candidates.md](./v1.1-candidates.md)** — which also recommends a scope. This section stays as the record of what v1 decided to defer; the candidates document is where the thinking about it lives.

## Open questions

The four this plan opened with, kept as the record of what was unknown *before* the work — two of
them were answered by doing it. The maintained list, renumbered and extended to seven, is in
[HANDOFF.md](./HANDOFF.md); source code cites it by number, so that is the one to update.

1. **Can `--mode local:<spec>` scope to the app process inside CoreSimulator?** Only relevant to the deferred opt-in path now, not to v1. **Still open.**
2. ~~**How should WebSocket and long-lived streaming connections appear in an interval model?** They span many Steps by nature, so the overlap model degrades into "overlaps everything".~~ **Answered by P4:** classed long-lived on `101` or `text/event-stream`, owned once by the Step that opened them, never repeated as an Overlap.
3. **Do Detox, Appium, Playwright and XCUITest expose per-Step timings** rich enough for the `Step` contract, or do they need explicit marks? **Still open, and no longer blocking** — `start_step`/`end_step` covers any of them without an adapter.
4. ~~**Whether `simctl keychain` requires a booted simulator** or accepts a shut-down one by UDID — decides whether trust can be arranged before boot.~~ **Answered: it requires a booted device.** A shut-down one fails `code=405, Unable to lookup in current state: Shutdown`.
