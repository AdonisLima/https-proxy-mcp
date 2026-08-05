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

## Never covered by a test

`_make_signal_handler` and the signal disposition it restores are not exercised anywhere. Treat
edits there as unverified, and prefer keeping a redundant guard over removing one.
