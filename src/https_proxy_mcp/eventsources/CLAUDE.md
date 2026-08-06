# eventsources/ — traps

Reading someone else's artifacts. Two of these were caught only because the artifact was probed
before anything was designed against it, and **every test would have passed either way**.

- **Maestro's `commands-*.json` is not in execution order.** A failing two-command flow came back
  as `[seq 3, 0, 1, 2]` — the FAILED assertion first, having run last — and a *passing* flow was
  also scrambled (`1, 2, 0`), so it is not a failure-ordering quirk. `metadata.sequenceNumber` is
  the authority.

  Worth its own paragraph because ordering by array position produces a Timeline that is
  internally consistent, renders cleanly, and puts the failure before the launch that preceded
  it. It reads as a correlation bug and it is not one.
- **`metadata.timestamp` is epoch *milliseconds*** and marks the command's **start**; `duration`
  is milliseconds too. Missing the conversion puts every Step about fifty thousand years from the
  traffic it should align with.
- **The artifact layout differs by flag.** With `--flatten-debug-output` it is
  `<dir>/commands-(flow.yaml).json`; without it,
  `<dir>/.maestro/tests/<timestamp>/commands-*.json` — a **hidden** directory. Filenames embed
  the flow name in literal parentheses, not a timestamp. The parser searches recursively for
  both; `run_test` injects the flatten flag anyway (see `../runs/CLAUDE.md`).
- **One failing command carries 33 KB of `error.hierarchyRoot`** — the entire device UI tree —
  making a two-command artifact 123 KB. That artifact is checked in at
  `tests/fixtures/maestro-commands-failing-assert.json`, hierarchy and all, and a test asserts
  the parser drops it. A second test caps *every* Step's error length structurally, so a new
  Maestro error field cannot slip through.
- **An unrecognised Maestro status becomes `unknown`, never `passed`.** Maestro may add a status
  in any release, and a Timeline claiming a Step succeeded when nobody knows is worse than one
  admitting ignorance.
- **`defineVariables` and `applyConfiguration` are filtered out** by an explicit list of keys, so
  an *unrecognised* command is kept rather than dropped. Neither is an action or an assertion
  (CONTEXT.md) and neither can cause a failure.

Adding an adapter for another test tool? `start_step`/`end_step` already covers any of them with
no adapter at all, so an EventSource is an enrichment rather than a prerequisite.
