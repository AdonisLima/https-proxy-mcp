---
name: live-machine-tests
description: Use when running, debugging or recovering from this repository's live machine tests — the three HTTPS_PROXY_MCP_E2E-gated tests in test_simulator_e2e.py and test_maestro_e2e.py that redirect the developer's real Mac system proxy and touch a real iOS Simulator. Also use when the Mac is left proxied through a dead port after a crash, when networksetup state needs restoring by hand, or when ~/.https-proxy-mcp has appeared and should not have.
---

# Running the live machine tests

Three tests in this repository change the developer's actual computer. Everything else runs
against fakes. They are gated on an environment variable rather than a pytest marker, because
a `-m` on the command line replaces any default expression from `addopts` — so a marker gate
would let `pytest -m "not integration"` silently opt back in to redirecting the machine's
networking.

```bash
HTTPS_PROXY_MCP_E2E=1 uv run pytest tests/test_simulator_e2e.py tests/test_maestro_e2e.py -q
```

They need a booted simulator and, for the Maestro one, Maestro installed. They create and delete
their own throwaway device.

## Record the state before you mutate it

This is not ceremony. It is the discipline that found the `Empty` bug, and nothing else would
have: every test passed throughout, because the parser normalised both forms.

```bash
networksetup -getwebproxy "Wi-Fi"            > .local/proxy-state-before.txt
networksetup -getsecurewebproxy "Wi-Fi"     >> .local/proxy-state-before.txt
networksetup -getproxybypassdomains "Wi-Fi" >> .local/proxy-state-before.txt
```

`.local/` is gitignored and already holds the recordings from the P3 and P4 sessions. The
machine's known-good state, verified unchanged at the end of P5: web and secure proxies both
`Enabled: No`, server `127.0.0.1`, port `9090`, no bypass domains.

## Check the machine afterwards, not just the tests

Diff the same three commands against what you recorded. A green suite does not mean the machine
came back — the proxy discipline generalises to **any state the tool wrote that it did not
create**.

Then check the Session root:

```bash
ls ~/.https-proxy-mcp 2>/dev/null && echo "LEAKED"
```

**`~/.https-proxy-mcp/` should not exist on this machine.** Every test redirects the root to
`tmp_path`. If it is there, something leaked — the usual cause is the `stdio_client` environment
trap in `src/https_proxy_mcp/server/CLAUDE.md`, which leaked three times in one session. Read
`sessions/*/session.json` to see which target and port created it **before** removing anything.
Those directories hold unredacted bodies.

## If the machine is left proxied

Repair-on-start handles this: the next server start restores the sentinel. If it cannot:

```bash
networksetup -getwebproxy "Wi-Fi"          # see what it is now
networksetup -setwebproxystate "Wi-Fi" off
networksetup -setsecurewebproxystate "Wi-Fi" off
networksetup -setproxybypassdomains "Wi-Fi" Empty
cat ~/.https-proxy-mcp/proxy-state.json    # what it was before, if the sentinel survives
```

The literal word `Empty` matters — `""` leaves a list holding one empty entry, which is not the
same as a list holding nothing. Order matters too: `-setwebproxy` *enables* the proxy as a side
effect, so when restoring a configured-but-disabled proxy the state command comes **second**.

## Two things never to do

- **`xcrun simctl keychain <udid> reset`** wipes that device's entire keychain. It is the only
  tool that can remove a root certificate, and it is far too blunt. The device
  `iPhone 17 Pro - Sidekick` (`F48AAE9F-4D3F-4817-9050-7B4B0024CF07`) has accumulated six inert
  mitmproxy authorities from P3's first live runs. They are harmless — the matching private keys
  were in pytest `tmp_path` directories that no longer exist. Leave them unless the user asks.
- **An interactive `sudo`.** Not needed here at all: `networksetup` proxy changes require no
  `sudo` for a user in the `admin` group.

## Known stale

`tests/test_simulator_e2e.py` has not been re-run since P4's `build_target` validation and
`trust_status` protocol change. All 66 simulator/trust unit tests pass and P4's live run
exercised the same path, so the risk is low. Re-running costs one proxy mutation — bundle it
with a future live run rather than mutating the machine for it alone.
