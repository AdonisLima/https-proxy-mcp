# The MCP invokes the test command

`run_test(command, …)` spawns the end-to-end test as a subprocess, and the Run is exactly the interval from spawn to exit. An MCP server that runs your test suite is unusual enough to be worth recording.

The alternative — observing tests that the user runs independently, then matching them to traffic by timestamp — requires inferring where a Run began and ended. Owning the process removes the inference entirely.

## Consequences

Three things fall out, and they are the reason for the decision:

- **Exact Run boundaries.** No matching heuristic, no fuzzy window at the edges.
- **No clock skew.** The test tool and the proxy both run on the host, so Step and Exchange timestamps share one clock. Device time never enters the join. The preceding research ranked clock skew as its second-highest risk on the assumption that events came from the device; this design removes the risk rather than mitigating it.
- **The MCP owns the test tool's output flags.** For Maestro it points `--debug-output` and `--test-output-dir` at the same directory, which structurally eliminates a documented footgun: split them, and only `maestro.log` goes to the former, stranding the `commands-*.json` that Timelines depend on.

The cost is that the server spawns arbitrary commands, which is a capability worth being conscious of. Runner-agnosticism is preserved by taking a command rather than hardcoding Maestro, and tools that cannot be wrapped can still emit explicit Steps instead.
