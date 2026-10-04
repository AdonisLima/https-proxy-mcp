---
status: accepted
---

# The record of prior proxy settings names the process that made the change

The sentinel that holds the machine's prior proxy settings gains one key beside them:

```json
{ "service": "Wi-Fi", "web": {}, "secure": {}, "bypass": [],
  "owner": { "pid": 39564, "started": "Sun Aug 16 17:40:12 2026" } }
```

`started` comes from `ps -o lstart= -p <pid>` through the command-runner seam the rest of that module uses, and is compared for **equality only** — never parsed. Ownership resolves to one of four states, and the three call sites that used to act on any record they found now branch on it: a process repairs, restores and takes over only what belongs to it or to something demonstrably dead. A live foreign owner is refused at `prepare()`, left alone by the repair at server start and by every restore, and reported by a new `foreign_holder()` query for callers that need to say so.

The defect was reported from real use and had cost a full debugging session and four wrong hypotheses. Every editor window on the machine runs its own copy of this server, all resolving to the same default root, so all of them shared one record. A window starting up switched off another window's routing and deleted its record, leaving a Session that reported `recording: true` while receiving nothing; a window that had stopped could leave the machine routed at a proxy nobody was listening on. Both directions were reachable, and the field report's four concurrent processes were exactly this condition.

## Considered options

- **Take the record over when it belongs to someone else.** This is what the code already did, and it is the defect. Doing it deliberately would be the same bug with better manners: whichever process loses the race is left recording nothing and reporting success.
- **Wait for the holder to finish.** Rejected because it hangs a JSON-RPC session, which this project already treats as the worst available failure — it is the reasoning behind the `sudo -n` non-negotiable. A refusal that names the holder is actionable; a hang is not.
- **A tool that force-releases a held proxy.** It looks necessary the moment refusal is chosen, and the escape hatch already exists and is better. `install_handlers` registers `SIGTERM`, `SIGINT` and `atexit`, so `kill <pid>` makes the *holder* restore the machine on its way out — its own recorded state, restored by the process that recorded it. A force-release would restore someone else's recorded state from this process, which is precisely the disregard for ownership that caused the defect. The refusal message names the signal, and notes that an uncatchable `kill -9` leaves the record behind for the next server start to repair.
- **Identify the owner by process id alone, with a liveness check.** Rejected, and this is the decision most likely to look redundant. With a pid-only check, a record that survives a reboot names an id now belonging to something unrelated, liveness answers "alive", repair is declined — and a self-healing bug becomes permanent, which is strictly worse than the bug being fixed. The start time closes that. It is opaque on purpose: parsing it would invent a second thing that can be wrong.
- **Treat an unreadable start time as a match.** Rejected in the same direction: empty must never compare equal to empty, or two unrelated processes look like one. Unknown resolves to "the owner is gone", so repair proceeds and connectivity is restored — the same fail-safe direction the module already takes for a record it cannot parse.
- **Let ownership win over a corrupt record.** Rejected: ownership cannot be read out of something that will not parse, so the existing behaviour stands — switch both proxies off and move the file aside. Fidelity is already lost at that point; connectivity is not.
- **Record a Session id in the sentinel as well.** It would make a better message. It also costs a change to `route_traffic(port)` — the `CaptureTarget` protocol — and therefore to every target and the suite parametrised over them. The process id is the actionable fact and `ps -p <pid>` supplies the rest.
- **Give every window its own root directory instead.** It looks like it would solve the sharing problem and makes it worse: there is one system proxy on the machine, so two isolated roots means two processes each believing they are alone with it.
- **Add a version marker to the sentinel for the new key.** Unnecessary: the prior-state reader keys on explicit fields and ignores the addition, and a probe confirmed there was no sentinel on disk anywhere to migrate. A record found *without* an owner is treated as a dead owner and repaired, which keeps the fail-safe pointing where it already pointed.

## Consequences

`get_status` now reports the proxy state whether or not this Session is recording — the record is process-independent, so that was always answerable and declining to answer it was the reason the original investigation took as long as it did. The report names the owner and whether it is this process, and its sentence branches on ownership: "it will be restored on `stop_capture`" is a false promise about a record this process does not own, and would otherwise have gone on being confidently wrong.

`diagnose_capture` gains a sixth verdict, `proxy_held_elsewhere`, ordered ahead of `no_connect`. It is a more specific explanation of the same observation with an entirely different remedy, and without it an agent arriving from a refused `start_capture` was told "not recording; call start_capture first" — advice that sends it straight back into the refusal.

The refusal reaches a caller as a tool error translated by the core from a plain exception the target raises, so Capture Targets keep no dependency on the MCP framework. With the pinned SDK that translation is not load-bearing, because the framework wraps any exception into the same client-visible message; it is kept as insurance against an SDK that stops wrapping, and nothing pins it.

Recovery from a crash is unchanged, which was a constraint rather than a bonus: a dead owner is the case the repair machinery already existed for, and withholding a repair is how a self-healing bug becomes permanent.

`SPEC.md`'s teardown guarantee — "every server start checks for a stale sentinel and restores it before doing anything else" — is now qualified by ownership. It stays as written, being a record of intent, and the divergence is listed in `.claude/agents/doc-drift-reviewer.md` so a future review does not report it.
