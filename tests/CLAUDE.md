# tests/ — traps and discipline

675 passed, 3 skipped. The 3 are the live machine tests; read
`.claude/skills/live-machine-tests/SKILL.md` before running them, and
`.claude/skills/verification-discipline/SKILL.md` before adding to this suite.

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
- **`pytest-asyncio` may run an async-generator fixture's setup and teardown in different
  tasks.** The anyio cancel scopes inside `stdio_client` reject that: "Attempted to exit cancel
  scope in a different task". Keep an MCP client's whole lifetime inside the test body.
- **`timeout` does not exist on macOS.** Don't reach for it in test scripts.

## Tests that cannot fail

The expensive kind. Each of these was green while proving nothing:

- **`maestro openLink` to a private-IP `http://` URL did not make Safari fetch it.** A hanging
  sink on the host's LAN address was never reached, so `pending` stayed empty and the acceptance
  test passed on incidental Safari traffic instead. The deterministic proof now lives in
  `test_pending_e2e.py`, driven through `curl`. If you want to force real device traffic to hang,
  verify the device actually requests your URL first.
- **A test reading stdin from a subprocess cannot prove stdin was closed**, because pytest has
  already redirected it. It asserts the spawn arguments instead.
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
