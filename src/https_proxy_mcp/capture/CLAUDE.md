# capture/ — traps

Hard-won, each one from a real debugging session. This is also one of the two directories where
mitmproxy's word **flow** is allowed (see CONTEXT.md), because here it names mitmproxy's own
object at the boundary.

- **`mitmproxy` needs its explicit `>=11` floor.** Loosen it and resolution fails outright — uv
  backtracks to an ancient release whose `urwid` uses `use_2to3`, which modern setuptools
  rejects. It is a build error, not a warning.
- **Clearing mitmproxy's `mode` option is what releases the listener.** `master.shutdown()` stops
  the master and leaves the port bound. `options.update(mode=[])` first, then `shutdown()`.
- **`DumpMaster` runs fine as a task on an already-running event loop.** `master.run()` is a
  coroutine; `asyncio.create_task` it. This is what makes ADR-0001's single process work.
- **`mode=["regular@127.0.0.1:0"]` binds an ephemeral port on loopback only**, and
  `addons.get("proxyserver").listen_addrs()` reports what it got. Never hard-code a port.
- **`Message.get_content(strict=True)` — the default — raises `ValueError` on a malformed
  `Content-Encoding`.** Capture must pass `strict=False`, so one bad header cannot end a Session.
- **The store gets written to after `close()`.** Connections still in flight when `stop_capture`
  runs deliver their hook afterwards, and mitmproxy renders the exception as
  `Addon error: I/O operation on closed file` — one line per open connection, looking exactly
  like a defect. `record()` ignores late arrivals and counts them in `dropped_after_close`.
- **A blocking `subprocess.run` starves the proxy.** `ProxyRuntime` runs mitmproxy as a task on
  the *same* event loop the tool surface uses (ADR-0001), so a synchronous child in an async
  probe blocks the thing that is supposed to answer its request, and curl hangs until its own
  timeout. Use `asyncio.create_subprocess_exec` and await it. Only bites hand-written probes;
  the test suite already does this correctly.
- **`with_termlog=False` does not make mitmproxy silent.** Its records go through Python's
  `logging`, and the MCP SDK installs a handler, so proxy chatter still appears. Verified
  harmless: stdout carries **only** valid JSON-RPC and every line of chatter is on stderr.
- **Keep the proxy off stdout anyway.** A stray flushed `print` does *not* corrupt MCP framing —
  tested deliberately; the client parses stdout line by line and skips what it cannot parse. But
  an interleaved write *inside* a JSON-RPC line would corrupt it. No test enforces this.

`mypy` relaxes `disallow_untyped_calls` for `addon.py` and `runtime.py` only — mitmproxy ships
`py.typed` but leaves `options.update`, `addons.add`, `addons.get`, `master.shutdown` and
`headers.items` unannotated. Every other strictness setting still applies here. The declaration
is in `pyproject.toml`.
