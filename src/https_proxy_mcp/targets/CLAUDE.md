# targets/ — traps

Everything here touches the developer's real machine or a real simulator. All of it was found by
running against the real thing, and several of these passed every test while being wrong.

## The Mac's proxy settings

- **`networksetup -setwebproxy` *enables* the proxy as a side effect** of setting server and
  port. To restore a configured-but-disabled proxy the state command must come *second*.
  Reversed, it leaves the machine proxied through a dead port — the exact failure the sentinel
  exists to prevent.
- **Clearing the bypass list needs the literal word `Empty`, not `""`.** Passing `""` leaves an
  `ExceptionsList` holding one empty entry where the key previously held nothing. Worth dwelling
  on: the round-trip test passed the whole time, because `parse_bypass_domains` normalises both
  forms to `()`. **A parser that normalises can hide a writer that does not** — it was caught
  only by recording the machine's raw output before the run and diffing it afterwards. Do that
  for any new field here.
- **`Authenticated Proxy Enabled: 0` also ends in `Enabled`**, and carries `0`/`1` rather than
  `Yes`/`No`. A substring match reads that line last and inverts the flag.
- **`-getproxybypassdomains` answers in prose when the list is empty.** A line-splitting parser
  invents a bypass domain out of an English sentence.
- **The primary network service comes from `scutil`, not service order.**
  `show State:/Network/Global/IPv4` gives a UUID; only `Setup:/Network/Service/<uuid>` carries
  the name `networksetup` accepts.
- **`networksetup` proxy changes need no `sudo`** for a user in the `admin` group, and do not
  prompt. Verified by setting the already-off state to off.
- **`curl` cannot verify anything about the macOS system proxy.** It honours `http_proxy` /
  `https_proxy` and ignores the system-proxy dictionary entirely, so it reaches an API directly and
  reports it healthy while every GUI application on the machine is routed at a dead port. That
  asymmetry is what made the original field report look like an application bug and cost four wrong
  hypotheses. To check what the machine is actually doing, use `scutil --proxy`, or a client that
  reads the system configuration. This is also why the repository's own quick-start uses `curl -x`
  explicitly rather than relying on routing.
- **A start time read for the sentinel's owner is compared, never parsed.** `ps -o lstart= -p <pid>`
  returns an opaque padded string and exits non-zero for a process that is gone. Two rules follow and
  both are load-bearing: an *unreadable* start time means **unknown**, and unknown resolves to
  **dead**, so repair proceeds and connectivity is restored; and empty must never compare equal to
  empty, or two unrelated processes look like one. The reason the field exists at all is process-id
  reuse across a reboot — with a liveness check alone, a record that outlives a restart names an id
  now belonging to something unrelated, liveness says "alive", the repair is declined, and a
  self-healing bug becomes permanent.
- **The machine bypasses the proxy for local-subnet addresses, and nothing says so.**
  `-getproxybypassdomains` answers "There aren't any bypass domains set" while a request to this
  machine's own LAN address goes direct. A redirect being in place is therefore not evidence that
  a given request will be captured, and a test pointing a device at a listener on the host will
  never see that request in the proxy. Measured: the listener accepted the connection, `pending`
  stayed empty, and an `https://example.com/` control in the same Session was captured. An address
  outside the local subnet goes through — RFC 5737's documentation range is the useful one, being
  reserved and unroutable, so a request to it stays pending for as long as you need.
- **A record that cannot be parsed outranks ownership.** Ownership cannot be read out of something
  that will not parse, so the existing fail-safe stands: switch both proxies off and move the file
  aside. Fidelity is already lost at that point; connectivity is not.

## CoreSimulator and trust

- **`simctl keychain add-root-cert` requires a *booted* device.** A shut-down one fails with
  `code=405, Unable to lookup in current state: Shutdown`, exit 149. Installing twice is
  harmless, which is what lets `start_capture` install unconditionally.
- **Trust is observable, and the schema differs by runtime.** The authority's DER digest appears
  in the device's `TrustStore.sqlite3`. iOS 26 keys `tsettings` on a **`sha256`** column under
  `data/private/var/protected/trustd/private/`; the older and far more widely documented layout
  uses **`sha1`** under `data/Library/Keychains/`. Read both, or every device on the other
  runtime reports as untrusted.
- **`sqlite3.connect` on a missing path *creates* an empty database.** That turns a read-only
  trust check into a write inside the device's keychain directory — it happened while probing by
  hand, and produced a 0-byte file that then looked like a corrupt store. Open with
  `file:...?mode=ro` and check existence first.
- **`simctl create` writes a runtime notice to stderr.** `$(xcrun simctl create ... 2>&1)` puts
  that notice into the UDID and every later command fails with `Invalid device`. Keep the
  streams apart.
- **`simctl list devices -j` reports `dataPath`.** Use it rather than rebuilding the path from
  the UDID.
- **`install_authority_system` must use `sudo -n`.** An interactive prompt hangs the whole
  session — the mechanism is in `../server/CLAUDE.md`. A test asserts every `sudo` call carries
  `-n`.
- **The TLS-failure count is machine-wide and no version of it can be per-client.** Every client
  reaches the proxy over loopback, so the peer address separates nothing: a simulator, a second
  simulator and the developer's browser are the same address. A Scope narrows **hosts, not
  clients** — another client on this Mac talking to a host you named is counted too, and *one*
  such request is enough to flip a healthy capture to `tls_failed`. Reproduced as a negative
  control: leg A drove the real app on a trusting device (`ok`, `tls_failures: 0`), leg B ran a
  single `urllib.request.urlopen` against the same in-scope host, and the verdict flipped and
  named the device that was working. This is not hypothetical damage — it is how this repository
  came to record the app under test as certificate-pinned when it is not, a claim that then
  reached five documents. So `tls_failed` states the count is machine-wide in the sentence
  itself, and offers pinning as the leading candidate rather than as fact. Three tests go red if
  either claim is tightened back; do not tighten them.

## Barely covered by a test

`_make_signal_handler` has exactly one test, and it reaches only the branch that chains to a
**callable** previous disposition — `test_the_signal_handler_leaves_a_live_foreign_record_alone`.
The branch after it, which restores the default disposition and re-raises, is still exercised
nowhere and cannot easily be: re-raising takes the test runner down with it. Treat edits to that
half as unverified, and prefer keeping a redundant guard over removing one.
