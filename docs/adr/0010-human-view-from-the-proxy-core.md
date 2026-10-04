---
status: accepted
---

# The Human View is served by the Session's own proxy core, behind an opt-in flag

`start_capture` gains `web_ui: bool = False, web_port: int = 0`. Asked for, the runtime builds
mitmproxy's web-capable master instead of the dump master — on the same event loop, carrying the
same `CaptureAddon`, recording into the same `ExchangeStore` — and the Session serves the mitmweb
UI on loopback, gated by a per-Session token the response carries as `web_url`. Unasked for, the
path is byte-identical to before: dump master, no web options, no port bound, `web_url` null. The
view dies with `stop_capture`.

This resolves the candidates file's *"`mitmweb` for a human view"* item and corrects its premise.
That item claimed the feature was already free — the UI drives itself from the proxy core, so
point the binary at the same capture. It was never true: the proxy core is an in-process master
(ADR-0001), and a separate `mitmweb` process would be its own, empty core. Nothing outside this
process can attach to the one doing the recording, so the UI had to be served from inside it.

## Considered options

- **A separate `mitmweb` process pointed at the same capture.** The candidates file's premise,
  and impossible. The store lives in this process's memory; the append-only log is a spill for
  durability, not a database a second reader can follow. A second process starts its own master
  and records its own traffic — which is to say, none of this Session's.
- **The constructor swap behind the flag** — `DumpMaster` for `WebMaster` when `web_ui` is on.
  Chosen. Both are the same base `Master` with the same options shape and the same addon
  contract, so the swap is one branch in `ProxyRuntime.start()` and the off path is untouched.
  The whole mechanism stays inside `capture/runtime.py`, with plumbing through the existing
  runtime-factory seam the tests already stub. No new tool: the registered surface stays at
  twenty.
- **A private subclass, `_WebProxyMaster`, that replicates the pinned master's startup body.**
  Ugly, and necessary: the library never stops its own web server. In `mitmweb` the process exits
  first, so the tornado HTTPServer is bound into a local in `WebMaster.running()` and nothing
  keeps it — `Master.shutdown()` only signals. A Session ends without this process ending, which
  would leak a listener per Session. The subclass keeps the reference and `stop()` closes it.
  Copying pinned-library internals is guarded two ways: the dependency carries an upper bound
  (`mitmproxy>=11,<13`, and a wheel ships no lock file, so a consumer resolves from that range
  alone), and a test asserts the web port is released — a library change that moves the mirrored
  internals fails loudly rather than silently leaking.
- **A read-only web mode.** Does not exist in the pinned mitmproxy, and patching its internal
  routes was rejected as effort against an undocumented surface. The write console — intercept,
  modify, replay — is accepted and documented instead: a human poking their own debug Session is
  the tool's owner, and the console is what they reach for when the read-only tools are not
  enough. The documentation states the cost plainly: edits made in the view can make a Timeline
  lie and can break teardown.
- **Narrow the view the way the tool surface is narrowed.** Rejected. The denylist, Scope, Lens
  and Run windows are properties of the tool surface — decisions about what a *model* should see,
  made so a context window is not flooded. A human at a browser has no such constraint, and the
  bodies already sit unredacted in the Session's own directory: the redaction boundary is the
  model and export surface, not the human. An unredacted, unnarrowed view is also the only one
  that can answer "is my app sending the right token?" — the question the model surface is
  deliberately unable to answer. The difference is documented, not reconciled.
- **Let tornado bind the ephemeral port itself.** It would, and the library never reads it back,
  so the reported URL would name port 0. The runtime pre-allocates instead: bind a temporary
  loopback socket to port 0, read the port, close, and configure the master with it. The
  bind–close race is accepted — on losing it, the library's own listen error surfaces and
  `start()`'s existing half-started cleanup runs.
- **Let the library open a browser, as it does by default.** Pinned off
  (`web_open_browser=False`). This server is headless and agent-driven; a browser appearing on
  the developer's screen because an agent called a tool would be the surprise, not the feature.

## Consequences

**A failed web listen leaks the proxy port too, and the fix lives in the same subclass.**
`Master.run()` binds the proxy servers before `running()`, and proxyserver only answers a `mode`
change once its RunningHook has fired — which the raised listen error pre-empts, so the usual
`mode=[]` release never happens. On `OSError` the subclass clears the proxy servers first and
only then re-raises. Found by the occupied-web-port test, not by reading: without it, the failed
start left the capture port bound behind it. An occupied `web_port` therefore fails
`start_capture` cleanly, with no half-started proxy holding anything.

**The view bypasses every reporting narrowing, visibly.** Out-of-Scope plain HTTP is hidden in
the listings and present in the view. That reads as an inconsistency; it is the distinction
between what was captured and what is reported, shown to a human for the first time. Both sides
are stated wherever either is.

**`web_url` is carried everywhere the Session reports itself.** The `start_capture` response
names it, and `get_status` carries it in every branch — the runtime's value while recording,
null when not — per the standing trap that a missing key on a failure branch reads as "nothing
is wrong".

**Vocabulary.** `CONTEXT.md` gains one term — **Human View**: the mitmweb UI served by a
Session's own proxy core. *mitmweb* names the library's own binary, which is the rejected option
above, so the docs needed a word that is not it.

`SPEC.md` says nothing about a web UI and its `start_capture` row has no `web_ui` or `web_port`.
Both are now untrue of the code and both stay: it is a record of intent. The divergence is listed
in `.claude/agents/doc-drift-reviewer.md`, without which every future documentation review
reports it as a fresh finding.
