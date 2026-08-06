# runs/ — traps

Spawning somebody else's test runner and deciding when it finished.

- **A timeout must kill the process *group*.** Maestro launches a JVM. Killing only the wrapper
  leaves it holding the simulator and the port, so the *next* Run fails for a reason that looks
  entirely unrelated. `start_new_session=True` plus `os.killpg`, tested with a real orphaned
  child.
- **Maestro exits 1 on a failing flow and 0 on a passing one.** Obvious in hindsight; the first
  probe read `$?` after a pipe to `tail` and got tail's status instead, which said 0 for a flow
  that had plainly failed. **Beware `$?` after a pipeline.** A non-zero exit is a *result* here,
  not an error — a failing test is the normal reason to be using this tool.
- **`--test-output-dir` does not contain the debug output.** Its own `--help` says so and a probe
  left the directory empty; `commands-*.json` lives under `--debug-output` alone. Pointing both
  at one directory is still worth doing so artifacts stay together, but only one of them matters.
  The layout the parser then has to cope with is in `../eventsources/CLAUDE.md`.
- **A test that reads stdin from a subprocess cannot prove stdin was closed**, because pytest has
  already redirected it. That test passed with the precaution removed. It asserts the spawn
  arguments instead. Applies to anything about the environment a child inherits.

`RunRegistry` writes `run.json` for durability but keeps its index in memory, so a Session
directory found later is readable by hand and not by `list_runs`. That is deliberate and
unbuilt rather than broken — see `docs/v1.1-candidates.md`.
