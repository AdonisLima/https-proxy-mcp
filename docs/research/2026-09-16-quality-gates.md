# Quality gates: what exists, and what improving them costs

**Question.** The gates today are `pytest -q`, `ruff check`, `ruff format --check`, and
`mypy --strict` on `src/`, all wired into pre-commit and a macOS CI job. Is there any way to
improve them — a gap they miss, or a gate worth adding?

**Answer, in one paragraph.** Yes, and the biggest gap is that nothing watches the
**dependencies**: two upper bounds in `pyproject.toml` are load-bearing, the lock file is the
source of truth, and no gate asks whether the locked resolution carries known vulnerabilities —
`uv audit` (preview, June 2026) closes that with one CI step and zero new tooling. After that,
the honest finding is that the gates are already strong: the fast suite measures **92% line
coverage** of `src/` with no coverage tooling installed, so a `pytest-cov` floor is cheap
insurance rather than a rescue. The remaining wins are small and specific: `pytest-timeout` so a
hung test costs seconds instead of the 20-minute 10x-billed CI timeout, `pytest-randomly` to
find order coupling (HANDOFF.md records a real flake of exactly that shape), `warn_unreachable`
spelled out in the mypy config, and the ruff `ASYNC` family as a guard on the single event loop
ADR-0001 depends on. Mutation testing and a CI interpreter matrix were evaluated and are
recommended **against** as gates — the reasons are below.

Nothing here modifies code or config. Each option states its conflict with AGENTS.md's
"Decisions already made" and non-negotiables where one exists.

## The gates today, grounded in the repo

- **Tests**: 951 passed, 3 skipped, ~48s full / ~11s fast (`AGENTS.md`, Verify). Machine tests
  are gated on `HTTPS_PROXY_MCP_E2E=1` by design (a marker gate would be silently opt-out-able —
  `pyproject.toml:77-80`).
- **Ruff**: `line-length = 100`, `src = ["src", "tests"]`, lint selects
  `["E", "F", "W", "I", "N", "UP", "B", "SIM", "RUF"]` — already nine families including
  pyupgrade, bugbear, and simplify (`pyproject.toml:110-117`).
- **mypy**: `strict = true`, `files = ["src"]`, with two modules relaxed
  `disallow_untyped_calls = false` as a named boundary against mitmproxy's unannotated API
  (`pyproject.toml:89-108`). The `tests/` exclusion is a **recorded decision** (~870 findings,
  almost all missing `-> None`; a separate deliberate commit if ever wanted) — nothing below
  proposes reopening it.
- **Pre-commit**: three hooks, all `language: system` through `uv run`, so the lock file is the
  single source of tool versions. Tests are deliberately absent ("a gate that gets bypassed is
  worse than one that is honest about its scope" — `.pre-commit-config.yaml:9-11`).
- **CI** (`.github/workflows/ci.yml`): one `macos-15` job (10x-billed; ~200 real macOS minutes
  a month), `timeout-minutes: 20`, `setup-uv` with cache, both supported interpreters installed,
  `uv sync --locked` (so "is the lock in step with the manifest" is **already** a gate —
  `--locked` fails otherwise), then tests, lint, format, types. A separate Linux job scores the
  harness with an exactly pinned `npx harness-score@1.5.1`. Push-only triggers and no
  interpreter matrix are both deliberate cost decisions, recorded in comments.
- **Deliberate non-goals to respect**: SPEC.md drift is historical record; SKILL.md is
  self-contained; `tests/` mypy is out of scope; no write tools in v1.

## Option 1: dependency auditing with `uv audit` (adopt, in CI)

**Recommendation.** Add one CI step, `uv audit`, after `uv sync --locked`. Also consider
`UV_MALWARE_CHECK=1` on syncs once it leaves preview. Alternative if `uv audit`'s preview
status is a concern: `uv run --with pip-audit pip-audit` in the same step.

**Evidence.** `uv audit` "scans your dependencies for known vulnerabilities and 'adverse'
project statuses (such as being deprecated)... a uv-native alternative to pip-audit" and is
uv-locked-resolution aware
([astral.sh/blog/uv-audit](https://astral.sh/blog/uv-audit));
it is in the shipped CLI
([docs.astral.sh/uv/reference/cli/#uv-audit](https://docs.astral.sh/uv/reference/cli/#uv-audit)).
The same post announces an opt-in OSV malware check on sync (`UV_MALWARE_CHECK=1`), and — the
part that matters for a locking installer — notes that quarantined malware disappears from the
index but the lock still points at object storage, which is why the check runs at sync time.
pip-audit is the established alternative
([pypi.org/project/pip-audit](https://pypi.org/project/pip-audit/)).

This is the one gate with nothing equivalent anywhere in the current set, and this repo's
exposure is not abstract: `pyproject.toml:39-53` documents that the dependency ceilings are
load-bearing *because a wheel ships no lock* — the project's own reasoning says dependency
resolution is a quality surface, and today nothing inspects what resolution produced.

**Cost.** One step, seconds of runtime, on the 10x-billed runner only as an audit — or move it
to the Linux harness job at 1x, since it needs no macOS. Caveats: `uv audit` is **preview**
("unstable and there may be breaking changes"), and it adds a network dependency to CI (OSV).

**Conflicts.** None. It strengthens the P5 dependency-decisions without touching them.

## Option 2: coverage measurement with a floor (adopt, CI-only)

**Recommendation.** Add `pytest-cov` to the dev group and a `[tool.coverage]` floor. Measured
today, the **fast** suite (`-m "not integration"`) already covers **92%** of `src/` (2157
statements, 173 missed). Set `fail_under` a couple of points under whatever the *full* suite
measures on first run — CI runs the full suite, so measure that. Keep the report in CI only;
do not add it to pre-commit (the recorded reason tests are not there applies a fortiori to a
slower instrumented run).

**Evidence.** Measured in this working tree on 2026-09-16:
`uv run --with pytest-cov pytest -q -m "not integration" --cov=https_proxy_mcp` → 92% total,
873 passed / 143 deselected in 19.1s (so ~8s instrumentation overhead over the ~11s fast
suite — another reason this belongs in CI, not the commit hook). Per-module, the floor is
`runs/process.py` at 61% (32 missed — the integration-heavy spawning paths the fast suite
deselects) and `server/__init__.py` at 81%; everything else is ≥92%, and thirteen modules are
at 100%. `fail_under` is coverage.py's documented threshold mechanism
([coverage.readthedocs.io/en/latest/config.html](https://coverage.readthedocs.io/en/latest/config.html));
pytest-cov exposes it as `--cov-fail-under`
([pytest-cov.readthedocs.io](https://pytest-cov.readthedocs.io/)).
Coverage's own docs frame what a floor is: a minimum, not a quality signal — "coverage
measurement is typically used to gauge the effectiveness of tests", showing *what* ran, not
whether it was asserted
([coverage.readthedocs.io](https://coverage.readthedocs.io/en/latest/)).

**Cost.** One dev dependency, one config block, one CI flag. The floor must be set from a
measurement, not a round number, and it will need `exclude_lines` conventions (e.g.
`if TYPE_CHECKING:`) — coverage.py ships defaults for the common ones.

**Conflicts.** None. One caution against a *high* floor: `targets/systemproxy.py` and
`targets/simctl.py` hold the machine-mutating paths, and pushing their coverage up means more
tests that fake the machine more elaborately — the suite already fakes deliberately, and the
three live tests are gated by design. A floor near the current measured value avoids creating
pressure against that decision.

## Option 3: `pytest-timeout` (adopt)

**Recommendation.** Add `pytest-timeout` with a generous per-test cap (e.g. 120s, `timeout_method = "thread"` or default signal). The suite spawns real processes and binds real
sockets (`pyproject.toml:76`), `runs/process.py` implements its own timeouts, and CI's only
backstop today is the job-level `timeout-minutes: 20` — on a 10x-billed runner, one hung test
costs 200 charged minutes. A per-test timeout converts that failure mode into a fast, located
failure.

**Evidence.** pytest-timeout "is designed to catch excessively long test durations like
deadlocked or hanging tests" and terminates them per test, with configurable method
([pypi.org/project/pytest-timeout](https://pypi.org/project/pytest-timeout/)).

**Cost.** One dependency, three config lines. The three gated machine tests need a
marker-scoped exemption or a larger cap, since they drive real simulators.

**Conflicts.** None. Note the non-negotiable about the machine tests is unaffected — the env
gate stays.

## Option 4: `pytest-randomly` (adopt as a periodic check; CI optional)

**Recommendation.** Trial run locally first (`uv run --with pytest-randomly pytest -m "not
integration" -p randomly`), fix or seed whatever it finds, then decide whether it belongs in CI
permanently. This suite has 951 tests with real fixtures, session directories, and faked
processes — fertile ground for unnoticed order coupling, and HANDOFF.md §"What did not go as
planned" records a flake of exactly that signature: a live test that "failed in a combined run
and passed on its own immediately afterwards, with nothing changed in between" (the root cause
there turned out to be environmental, but the *detection* problem — order-dependent tests
masquerading as fine — is what randomization addresses).

**Evidence.** pytest-randomly shuffles test order and resets random seeds per run
([github.com/pytest-dev/pytest-randomly](https://github.com/pytest-dev/pytest-randomly)).

**Cost.** If the suite is already order-clean, adoption is one dependency and zero config. If
not, each coupling found is a real defect, found cheaply. Risk: it changes seeding of
`random`, so any test using unseeded randomness needs a fixture.

**Conflicts.** None found. Do not apply to the machine-gated tests (they are three, run
deliberately, and ordered by the skill that runs them).

## Option 5: `warn_unreachable` in the mypy config (adopt; free)

**Recommendation.** Add `warn_unreachable = true` under `[tool.mypy]`.

**Evidence.** `--strict` historically did **not** include every check — `warn_unreachable` was
the notable exclusion ([mypy issue #14143](https://github.com/python/mypy/issues/14143)), with
inclusion in strict slated for mypy 2.0
([mypy issue #18078](https://github.com/python/mypy/issues/18078));
the flag is documented at
[mypy.readthedocs.io/en/stable/command_line.html#cmdoption-mypy-warn-unreachable](https://mypy.readthedocs.io/en/stable/command_line.html#cmdoption-mypy-warn-unreachable).
Measured here on the pinned mypy 2.3.0: `uv run mypy --warn-unreachable` reports "Success: no
issues found in 38 source files", identical to the plain run — so the flag is either already
subsumed by 2.x strict or simply clean, and spelling it out is free insurance either way.
**Do not** extend mypy into `tests/` — that exclusion is a recorded decision
(`pyproject.toml:92-95`).

**Cost.** One line.

**Conflicts.** None.

## Option 6: ruff `ASYNC` family (adopt with per-file-ignores for `tests/`)

**Recommendation.** Add `"ASYNC"` to the lint selection, with `per-file-ignores` for the noisy
rules in `tests/`. The codebase runs the proxy on the server's single event loop (ADR-0001),
which makes "blocking call inside an async function" the specific bug class that stalls capture
for every Exchange — flake8-async's rules (ASYNC220/221/240/251) target exactly that
([flake8-async rules](https://flake8-async.readthedocs.io/en/latest/rules.html);
[ruff's rule index](https://docs.astral.sh/ruff/rules/)).

**Evidence, measured.** `ruff check --extend-select ASYNC,C4,PERF,RET,T20,PTH,BLE,A,TID,SLF,DTZ
--statistics .` on this tree finds **22 violations in 7 rules — none of them in `src/`** except
two deliberate ones (below). The seven ASYNC findings are all in `tests/`
(`test_pending_e2e.py:107,169` ASYNC220, `test_run_process.py:223` ASYNC251,
`test_simulator_e2e.py:117` ASYNC221, `test_tools_trust.py:604` ASYNC240, plus two ASYNC110),
and are mostly *intentional* — tests blocking on purpose to simulate the world. C4, PERF, RET,
TID came back **clean** — the current nine families already catch what those would. So ASYNC's
value here is as a *guard on future `src/` edits*, not as a cleanup pass, and it should be
adopted with test-scoped ignores rather than by churning tests.

**Evaluated and rejected from the same measurement:** `DTZ` fires on
`capture/session.py:98` (`datetime.now()`), but naive local time is load-bearing there —
session ids are microsecond timestamps whose "lexicographic order is chronological" (AGENTS.md,
P1 table); `T20` fires on two `print(..., file=sys.stderr)` calls that are the deliberate
CLI/stderr surface (`server/__init__.py:97`, `.claude/hooks/check_python.py:84`); `PTH` and
`A` find one or two stylistic items. Not worth the ignores.

**Cost.** One word in `select`, one `per-file-ignores` block.

**Conflicts.** None — this is the guard ADR-0001's single-loop architecture never had.

## Option 7: Dependabot for `uv` and `github-actions` ecosystems (adopt, monthly)

**Recommendation.** Add `.github/dependabot.yml` with `package-ecosystem: "uv"` and
`"github-actions"`, monthly cadence. Dependabot supports updating `uv.lock` since March 2025
([docs.astral.sh/uv/guides/integration/dependabot/](https://docs.astral.sh/uv/guides/integration/dependabot/)).
CI triggers on push, and Dependabot pushes branches, so its updates are exercised by the full
gate set without any trigger change.

**Evidence.** As above; the uv ecosystem entry and behaviour are documented in uv's own
integration guide. Dependabot's supported-ecosystems reference confirms `uv`
([docs.github.com](https://docs.github.com/en/code-security/reference/supply-chain-security/supported-ecosystems-and-repositories)).

**Cost and honest trade-offs.** PR noise on a repo whose branches are pushed directly; the two
load-bearing ceilings (`mcp<2`, `mitmproxy>=11,<13`) mean manifest updates will mostly be
within-range patch bumps from the lock — useful, but the interesting updates (major bumps) are
the ones the project has decided not to take, so configure ignore rules for those two majors or
expect recurring declined PRs. This is also the natural companion to Option 1: `uv audit`
tells you a locked version is vulnerable; Dependabot is what keeps the lock fresh enough that
a fix exists without a fire drill.

**Conflicts.** None with recorded decisions; the deliberate upper bounds stay exactly as they
are — Dependabot respects them.

## Option 8: full-length SHA pinning for GitHub Actions (adopt if threat model warrants)

**Recommendation.** Pin `actions/checkout` and `astral-sh/setup-uv` to commit SHAs with a
comment naming the version, and let Dependabot's `github-actions` ecosystem (Option 7) update
the pins.

**Evidence.** GitHub's own hardening guide: third-party actions should be pinned to a
full-length commit SHA, as tags are mutable
([docs.github.com — Security hardening for GitHub Actions](https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions)).
Note the repo already applies the *spirit* of this where it matters most: the harness job pins
`harness-score@1.5.1` exactly, with a comment explaining why (`ci.yml:78-80`) — the action
references are the un-pinned remainder.

**Cost.** Minutes; maintenance is absorbed by Option 7. This is a private repo with
`permissions: contents: read` already set, so the exposure is modest — ranked accordingly.

**Conflicts.** None.

## Option 9: `filterwarnings = ["error", ...]` in pytest (adopt; one line)

**Recommendation.** Prepend `error` to the existing `filterwarnings` list so every warning
except the deliberately narrowed mitmproxy deprecations fails the suite.

**Evidence.** The current config ignores only `DeprecationWarning` matching `.*deprecated.*`
"so every other warning category — ours included — still reaches the report"
(`pyproject.toml:82-87`) — but *reaching the report* and *failing* are different gates, and
nobody reads the warnings summary of a green run. pytest applies filters in order with later
entries taking precedence
([docs.pytest.org — @pytest.mark.filterwarnings / warning capture](https://docs.pytest.org/en/stable/how-to/capture-warnings.html)),
so `["error", "ignore:.*deprecated.*:DeprecationWarning"]` keeps the existing narrowing intact.

**Cost.** One line; may surface a handful of warnings to fix or narrow on first run.

**Conflicts.** None — it strengthens the stated intent of the existing config.

## Considered and set aside

- **Mutation testing (mutmut 3.x, cosmic-ray) as a gate — do not adopt.** The full suite is
  ~48s and the fast suite ~11s; mutation testing multiplies suite time by mutant count, which
  for ~2,200 statements of `src/` means hours per run — unusable as a commit or CI gate.
  mutmut mitigates with incremental runs, test-selection by stack depth, and wildcards
  ([mutmut.readthedocs.io](https://mutmut.readthedocs.io/en/latest/)),
  and cosmic-ray offers distributed execution
  ([cosmic-ray.readthedocs.io](https://cosmic-ray.readthedocs.io/)),
  but both remain an *occasional audit*, not a gate. If ever wanted, scope it to the pure
  modules (`correlate/timeline.py`, `serialize/redact.py` — both at 100% coverage, where
  surviving mutants would be genuinely informative) and run it by hand. Also note mutmut 3
  mutates function bodies only, and requires `fork` (fine on macOS).
- **CI matrix over Python 3.12/3.13 — do not adopt.** `tests/test_distribution.py` already
  installs the built wheel under *every* admitted interpreter inside the single job
  (`ci.yml:41-45`), which is the only place interpreter resolution is genuinely exercised. A
  matrix would double the 10x-billed minutes for coverage the suite already has. Matrix
  mechanics: [docs.github.com — running variations of jobs in a workflow](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations).
- **Generic pre-commit-hooks (check-yaml, trailing-whitespace, large-files) — optional, low
  value.** They are file-content checks with no CI counterpart to drift against, so they do not
  violate the config's recorded single-source-of-versions philosophy
  ([github.com/pre-commit/pre-commit-hooks](https://github.com/pre-commit/pre-commit-hooks));
  but they also find almost nothing in a tree this disciplined. `check-yaml` is the only one
  with a real target (the CI workflow is never validated until pushed).
- **Ruff `S` (bandit) rules — do not adopt.** The codebase deliberately shells out
  (`networksetup`, `simctl`, `sudo -n`, subprocess spawning — most of ADR-0003 and the
  non-negotiables); `S` would produce ignores at the exact call sites that are already the most
  carefully documented in the project.
- **A second type checker (ty/pyright) — defer.** `mypy --strict` plus ruff is coherent and the
  mitmproxy boundary is already negotiated once; a second checker re-opens every one of those
  decisions for marginal signal.

## A finding about the current working tree

While measuring coverage, one test failed **without** any coverage tooling involved and
reproduces on its own: `tests/test_server.py::test_the_usage_guide_gets_every_parameter_name_right`
reports `start_capture` carrying parameters (`lens`, `passthrough`, `scope`, `target`, …) that
`docs/USAGE.md` does not list. That is the test that pins USAGE.md's signatures to the live
schemas — i.e. the gate is doing its job on this branch's uncommitted changes, and the branch
should not be merged while it is red. Worth knowing before any of the above is worked on.

## Suggested next steps, ranked by value-for-effort

1. **Option 1 (`uv audit` in CI).** Minutes to add; closes the only gate category with zero
   coverage today.
2. **Option 5 (`warn_unreachable`).** One line, measured clean.
3. **Option 9 (`filterwarnings` error-first).** One line; may surface a few real warnings.
4. **Option 3 (`pytest-timeout`).** A few lines; caps the worst-case CI cost of a hang at one
   test instead of one job.
5. **Option 6 (ruff `ASYNC` + test ignores).** Small config; guards ADR-0001's event loop.
6. **Option 2 (coverage floor, CI-only).** Half a day to measure the full suite, configure, and
   pick a floor; insurance against regression, not a rescue (92% already).
7. **Option 4 (`pytest-randomly` trial).** One trial run decides; adopt if clean, fix if not.
8. **Option 7 + 8 (Dependabot; SHA pins).** Together: Dependabot keeps pins and lock fresh.
   Modest value on a private repo; schedule monthly to keep noise down.
9. **Mutation testing.** Audit-only, pure modules only, by hand — if ever.

## Sources

- Astral, "Vulnerability and malware checks in uv" — https://astral.sh/blog/uv-audit
- uv CLI reference, `uv audit` — https://docs.astral.sh/uv/reference/cli/#uv-audit
- uv, "Using uv with Dependabot" — https://docs.astral.sh/uv/guides/integration/dependabot/
- GitHub Docs, Dependabot supported ecosystems — https://docs.github.com/en/code-security/reference/supply-chain-security/supported-ecosystems-and-repositories
- pip-audit — https://pypi.org/project/pip-audit/
- Coverage.py, configuration reference (`fail_under`, `exclude_lines`) — https://coverage.readthedocs.io/en/latest/config.html
- Coverage.py, "Excluding code from coverage" — https://coverage.readthedocs.io/en/latest/excluding.html
- pytest-cov — https://pytest-cov.readthedocs.io/
- pytest-timeout — https://pypi.org/project/pytest-timeout/
- pytest-randomly — https://github.com/pytest-dev/pytest-randomly
- mypy command line reference (`--warn-unreachable`) — https://mypy.readthedocs.io/en/stable/command_line.html#cmdoption-mypy-warn-unreachable
- mypy issue #14143, "strict does not include all mypy checks" — https://github.com/python/mypy/issues/14143
- mypy issue #18078, "Enable --warn-unreachable in --strict in Mypy 2.0" — https://github.com/python/mypy/issues/18078
- Ruff rules index — https://docs.astral.sh/ruff/rules/
- flake8-async rules — https://flake8-async.readthedocs.io/en/latest/rules.html
- pytest, warning capture / filterwarnings ordering — https://docs.pytest.org/en/stable/how-to/capture-warnings.html
- GitHub Docs, security hardening for Actions (SHA pinning) — https://docs.github.com/en/actions/security-for-github-actions/security-guides/security-hardening-for-github-actions
- GitHub Docs, job matrices — https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations
- mutmut documentation — https://mutmut.readthedocs.io/en/latest/
- Cosmic Ray documentation — https://cosmic-ray.readthedocs.io/
- pre-commit-hooks — https://github.com/pre-commit/pre-commit-hooks
