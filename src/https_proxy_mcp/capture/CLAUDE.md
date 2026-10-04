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
- **Never write a mitmproxy `allow_hosts` pattern by hand.** `hostsets.py` renders them from the
  same definition the Scope predicate answers from, and a table of hosts asserts the two agree.
  Two expressions of one host-set rule is the trap `../targets/CLAUDE.md` records — a reader that
  normalises hides a writer that does not — and here it would surface as a Scope that reports a
  host's traffic while never decrypting it. The pattern's optional trailing dot is not
  decoration: a candidate string can reach the engine with its root dot still attached. It admits
  **one** such dot where the predicate's `rstrip(".")` strips a whole run, so the two forms do
  diverge at `kivid.com.br..` — a string nothing emits. The equivalence is asserted over the hosts
  that occur, not over every string, and the table in `tests/test_hostsets.py` is where that
  promise actually lives.
- **`allow_hosts` scopes TLS interception only — plain HTTP is intercepted whatever it says.**
  `next_layer.py`'s `_ignore_connection` returns `False` when it has no hostname to test, and for a
  plain-HTTP proxy request the destination is not known until the request line is parsed, which is
  *after* the layer has been chosen. Only a CONNECT authority and a TLS SNI arrive early enough.
  Found by hand-verifying a live scoped capture: all eleven Exchanges recorded were plain-HTTP Metro
  bundler traffic from `localhost`, a host the Scope never named, which read at first as
  `allow_hosts` not being applied at all. It was applied — `translate.google.com` came back with its
  real issuer in the same run. Consequence worth carrying into any prose you write here: an
  out-of-scope **HTTPS** host was never captured and cannot be fetched by id, while an out-of-scope
  **plain-HTTP** host was captured and is merely hidden by reporting. Opposite remedies, identical
  appearance in a listing.
- **A filter expression whose value contains a space must be quoted, or it matches nothing.**
  `~hq X-Sim: sidekick` does not fail — `flowfilter.parse` accepts it and the result matches
  neither a request carrying that header nor one that does not. `~hq "X-Sim: sidekick"` is the
  form that works. Measured against both, because the failure is silent: an unquoted Lens hides
  the whole Session and looks exactly like a capture that recorded nothing. Anywhere this project
  writes a header-matching example, it writes the quotes.

- **Keep the proxy off stdout anyway.** A stray flushed `print` does *not* corrupt MCP framing —
  tested deliberately; the client parses stdout line by line and skips what it cannot parse. But
  an interleaved write *inside* a JSON-RPC line would corrupt it. No test enforces this.

- **Requests still in flight narrow through `ReportingFilter.allows_pending`, beside the
  Exchange path** — same window-first-then-host order, no Lens call (a Lens is a predicate over
  Exchanges, and a pending request never became one). Add a fifth reporting narrowing in
  `reporting.py` alone; there is no second copy of the window arithmetic to keep in step.

`mypy` relaxes `disallow_untyped_calls` for `addon.py` and `runtime.py` only — mitmproxy ships
`py.typed` but leaves `options.update`, `addons.add`, `addons.get`, `master.shutdown` and
`headers.items` unannotated. Every other strictness setting still applies here. The declaration
is in `pyproject.toml`.
