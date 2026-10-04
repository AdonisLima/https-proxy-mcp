# server/ — traps

The MCP surface. Twenty registered tools, and a transport that is easy to break in ways that do
not look like breakage.

- **`FastMCP` is the server class, not `MCPServer`.** `from mcp.server import MCPServer` raises
  `ImportError` in the pinned SDK even though newer docs name it. Introspect the installed
  package rather than trusting docs.
- **`FastMCP.call_tool` returns `(content_blocks, structured_result)`** for a tool annotated to
  return a dict — not a bare content list.
- **Never run an interactive `sudo` from here.** This server speaks JSON-RPC over stdin, so a
  password prompt reads the client's next message as a password attempt and hangs the session
  with no error and no way out. `install_authority_system` uses `sudo -n` and hands the command
  back for a human when credentials are not cached. A test asserts every `sudo` call carries
  `-n`.
- **`stdio_client` does not forward the environment.** With `env=None` the SDK hands the child
  `get_default_environment()` — exactly `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER`. So an
  **exported** `HTTPS_PROXY_MCP_ROOT` never reaches a server launched that way, and it writes
  Sessions holding unredacted bodies into the developer's real home while looking redirected.
  Pass `env=` explicitly. This leaked three times in one session before it was noticed —
  including from the first teeth-check *of the fix*, which ran without redirecting `HOME`.
- **Batch-piping JSON-RPC into the server and closing stdin loses in-flight responses.** A probe
  that wrote all four messages at once got no response to the `start_capture` in the middle — the
  shutdown raced the response write. Not a defect; with realistic pacing all three arrive. It
  reads exactly like a dropped-response bug, so pace a probe before diagnosing one.
- **Every browse tool goes through `_listing`.** The narrow → clamp → counts invariant has failed
  twice in exactly the bypassed shape, both times in the one browse tool that assembled its
  listing inline: `tail_exchanges` honoured any `limit` up to the whole ring buffer while the
  listings clamped to `MAX_PAGE_SIZE`, and before that it skipped the denylist narrowing the
  listings applied. SPEC.md's claim is that flooding the context is *impossible* rather than
  discouraged, so each was a hole in exactly that guarantee, reachable by asking for the other
  browse tool. The ceiling itself lives in `page_after` in the serialize layer; the clamp and
  both `hidden` populations live in `_listing`. Found by writing the guide, not by reading the
  code.
- **`list_exchanges` must never be able to return bodies.** There is no parameter that would do
  it, deliberately — that is the API enforcing what guidance cannot.
- **FastMCP wraps *any* exception a tool raises into a `ToolError`.** So
  `pytest.raises(ToolError)` on the tool surface cannot tell your new refusal from one the code
  already had, and a test that asserts only the type stays green when the thing it exists to prove is
  deleted. Measured: removing `refuse_if_held()` from `IosSimulatorTarget.prepare()` left such a test
  passing, because `redirect()`'s pre-existing refusal was wrapped identically — and removing the
  explicit `ProxyHeldElsewhereError` → `ToolError` translation in `CaptureState.start` left the
  client-visible message byte-identical. Assert on the **message** (a pid, a remedy) or on a side
  effect, and treat the explicit translation as insurance against an SDK that stops wrapping rather
  than as something a test pins.
- **A diagnosis must answer with the same keys from every branch.** The two not-recording branches
  omitted `target` while the recording branch carried it, so a caller reading it got a `KeyError`
  from exactly the branches that exist to say something is wrong — which is how an agent concludes
  nothing is wrong. `proxy_state()` had the same shape hole in its unparseable-record branch. Both
  now report null rather than nothing.

`server._mcp_server.version` is assigned after construction because `FastMCP.__init__` takes no
`version` and the low-level server it wraps reads its own mutable attribute when building the
handshake. The stdio handshake test asserts through the transport, so an SDK rename fails a test
rather than silently reverting to the SDK's own version.
