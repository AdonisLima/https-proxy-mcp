---
description: Run the three gated tests that change this Mac's real system proxy
---

Use the `live-machine-tests` skill and follow it exactly. Do not improvise around it.

These three tests redirect the developer's actual system proxy and create a real iOS Simulator
device. Everything else in the suite runs against fakes.

The order is not optional:

1. **Record** the machine's proxy state to `.local/` first. A parser that normalises can hide a
   writer that does not — recording and diffing is the only thing that has ever caught that
   class of bug here, and every test passed while it was live.
2. Confirm a simulator is booted, and Maestro is installed if running the Maestro test.
3. Run the tests.
4. **Diff** the machine's proxy state against what you recorded, and check that
   `~/.https-proxy-mcp/` does not exist.
5. Report what the machine looked like before and after, not just whether the tests passed. A
   green suite does not prove the machine came back.

If anything is left proxied, the recovery commands are in the skill. Do not run
`xcrun simctl keychain <udid> reset` under any circumstances without asking first — it wipes a
device's entire keychain.
