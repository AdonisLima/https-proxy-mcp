# tests/ — traps and discipline

3 skipped, and they are the live machine tests; read
`.claude/skills/live-machine-tests/SKILL.md` before running them, and
`.claude/skills/verification-discipline/SKILL.md` before adding to this suite.

The suite's passing count is stated in `../AGENTS.md` and deliberately not repeated here. It was
repeated, went stale by more than two hundred, and nobody noticed — which is the same drift ADR-0006
moved the trap lists to avoid.

## Gating

- **A `-m` on the command line replaces any default marker expression from `addopts`.** So gating
  a machine-mutating test with a marker is unsafe: `pytest -m "not integration"` would silently
  opt back in. The live tests are gated on `HTTPS_PROXY_MCP_E2E=1` for that reason. The `machine`
  marker in `pyproject.toml` is documentation only.

## Harnesses

- **`mitmproxy.test.tflow` is the right harness for addon unit tests.** Real flow objects, and
  shapes a live proxy will not produce on demand.
- **Invoke mitmdump as `python -c "from mitmproxy.tools.main import mitmdump; mitmdump()"`.** Args
  after `-c` land in `sys.argv[1:]` where its parser wants them.
- **Pass mitmdump a throwaway `--set confdir=`**, or it writes an Interception Authority into the
  developer's real `~/.mitmproxy`.
- **`curl` needs `--noproxy ""` to proxy localhost.** Without it an ambient `NO_PROXY` sends the
  request direct and the proxy records nothing.
- **`curl` through the proxy to an `https://` URL fails with exit 60**, `unable to get local
  issuer certificate` — correct, since `CurlTarget` arranges no trust by design. Use `http://`,
  or `--cacert <root>/mitmproxy/mitmproxy-ca-cert.pem`, or `-k`. All three verified.
- **`fixture_server.serve_tls` needs `ssl_verify_upstream_trusted_ca` to be *intercepted*.**
  mitmproxy verifies the upstream certificate, and the fixture's authority is unknown to the
  system, so an intercepted request to it answers `502 Certificate verify failed` with the real
  body nowhere in sight. Point the option at `fixture.authority_pem` — measured: 502 becomes 200
  and the client still sees `mitmproxy` as the issuer, which is the whole point of the assertion.
  A passed-through request needs none of this, because nothing opens the connection.
- **Read a peer certificate immediately after `connect()`, not after `getresponse()`.**
  `http.client` drops its socket when the response says close, and a proxied response does — so
  `connection.sock` is `None` by the time you ask, and the `AttributeError` reads like a failed
  request rather than a late question.
- **`pytest-asyncio` may run an async-generator fixture's setup and teardown in different
  tasks.** The anyio cancel scopes inside `stdio_client` reject that: "Attempted to exit cancel
  scope in a different task". Keep an MCP client's whole lifetime inside the test body.
- **`timeout` does not exist on macOS.** Don't reach for it in test scripts.
- **A test about a *live* foreign owner must read the real process table.** The child in
  `test_kill9_recovery.py` records its own start time from a real `ps`, so a parent answering `ps`
  from `FakeCommands` disagrees with it, the live child resolves to **dead**, and every ownership
  assertion is then about the wrong branch — while still passing, because a dead owner's record is
  repaired and the record does go away. `test_two_windows.py::real_process_table` stubs
  `networksetup` and `scutil` and lets `ps` through, which is read-only.
- **From another process, "the sentinel is gone" is not evidence that anything was restored.** A
  child that unlinked the file and issued no command satisfies it. The child's stubbed runner logs
  every argv to `child-commands.jsonl` for that reason; assert on `child_mutations(tmp_path)`.

## Tests that cannot fail

The expensive kind. Each of these was green while proving nothing:

- **A hanging sink on the host's LAN address is never captured — and the recorded reason for that
  was wrong.** It was written up as "`maestro openLink` to a private-IP URL did not make Safari
  fetch it". Re-probed: Safari *does* fetch it, over `http://` and `https://`, through both
  `simctl openurl` and Maestro's own `openLink`, and the connection arrives at the listener. What
  does not happen is the **proxy** seeing it — macOS bypasses the proxy for local-subnet addresses,
  with an empty bypass list and nothing in `networksetup -getproxybypassdomains` to say so.
  Measured in one Session: the listener logged the connection while `pending` stayed empty, and an
  `https://example.com/` control through the same proxy was captured. The lesson generalises —
  **"the device never sent it" and "the proxy never saw it" are different findings**, and only a
  listener of your own tells them apart. `test_maestro_e2e.py` now hangs its request against an
  RFC 5737 documentation address, which is unroutable *and* not local; `test_pending_e2e.py` still
  proves the mechanism without a simulator, through `curl`.
- **A test reading stdin from a subprocess cannot prove stdin was closed**, because pytest has
  already redirected it. It asserts the spawn arguments instead.
- **`pytest.raises(ToolError)` on the tool surface proves only that *something* refused.** FastMCP
  wraps any exception into that one type, so a test asserting the type alone stays green with the
  refusal it exists to prove deleted — measured, by removing `refuse_if_held()` and watching it pass,
  because `redirect()`'s older refusal was wrapped identically. Assert the message (a pid, a remedy)
  or a side effect. The mechanism is in `../src/https_proxy_mcp/server/CLAUDE.md`; the bare test that
  demonstrated it was folded into its sibling rather than kept as an example.
- **A test that only asserts "no Exchange was recorded" cannot prove Passthrough.** It passes just as
  well if the connection was intercepted and merely not stored, and again if it was dropped outright.
  So `test_passthrough.py` asserts the **issuer the client saw** (which separates intercepted from
  not intercepted) *and* the response body (which separates forwarded from dropped). Neither
  assertion alone is the claim.
- **A green clean-install on one interpreter proves nothing about resolution.**
  `test_distribution.py` is parametrised over every interpreter `requires-python` admits, and
  that is what caught `mcp` 2.0.0 removing `mcp.server.fastmcp` entirely. A wheel ships no lock
  file, so a consumer resolves from `Requires-Dist` alone — which no in-repo test exercises.
  `uv build` is instant and needs no network (~0.04 s), which is why the suite can build a real
  wheel per interpreter rather than mocking one.

## Never write a Session to the real home

Every test redirects the root to `tmp_path`. Those directories hold unredacted bodies. See
`../src/https_proxy_mcp/server/CLAUDE.md` for the `stdio_client` environment trap, which is how
this leaked three times in one session.
