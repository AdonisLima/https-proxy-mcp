# Regular proxy mode for iOS, not mitmproxy's local mode

v1 captures iOS Simulator traffic by pointing the macOS system proxy at mitmproxy via `networksetup`, and installing the interception authority with `simctl`. mitmproxy's `--mode local` — transparent per-process capture — is deferred to an opt-in path.

This reverses the recommendation in the research that preceded the project, which preferred local mode for avoiding a global proxy change.

## Considered Options

- **`--mode local` as primary.** Nothing global to restore, so a crash leaves no mess. Rejected for v1 on two grounds: it is implemented as a code-signed macOS Network Extension, which is a heavier install than a proxy toggle and normally requires user approval in System Settings; and the per-process scoping that is its entire advantage is unverified for an app running inside CoreSimulator. Paying the system-extension cost and *still* capturing the whole host is a plausible outcome.
- **Both, with auto-detect and fallback.** Rejected: two capture paths and a fallback matrix in the riskiest component of a first release.

Meanwhile the case against regular mode weakened during research: the full scriptable `networksetup` sequence *including teardown* is documented, and the Metro bundler collision has a documented fix (add the Metro host to the proxy bypass list) rather than being fatal.

## Consequences

Two properties follow and are handled explicitly in the spec. The Mac is globally proxied while capturing, so teardown becomes safety-critical — hence the sentinel-file protocol with repair-on-start. And because every proxied connection arrives from localhost, **there is no process attribution**: simulator traffic is indistinguishable from browser traffic at the proxy, so noise must be filtered by Run window and host, never by process.
