---
status: accepted
---

# Traps live beside their modules, not in the handoff

The list of hard-won traps — sixty-odd findings, each one bought with a real debugging session — moved out of `HANDOFF.md` and into a `CLAUDE.md` in each of the six directories the findings are about: `capture/`, `targets/`, `eventsources/`, `runs/`, `server/` and `tests/`. `HANDOFF.md` keeps a pointer and nothing else.

The durable half of `HANDOFF.md` moved too, into a root `AGENTS.md`: the reading order, the module map, and the decisions not to reopen. `CLAUDE.md` at the root is a symlink to `AGENTS.md`, which is how Claude Code reads a file the rest of the ecosystem has standardised on.

The traps were accurate and well written. The problem was location. A trap about `networksetup -setwebproxy` enabling the proxy as a side effect is worth a great deal while editing `systemproxy.py`, and worth nothing at line 200 of a 315-line file whose first thirty lines are dated status. `HANDOFF.md` is read once at the start of a session, if at all; a `CLAUDE.md` beside the code is loaded by the agent precisely when that directory is in play. Same words, delivered when they can still change what happens.

Moving rather than copying is the part that needed deciding. Two copies of a trap list drift, and this project already knows what that costs: `HANDOFF.md` itself recorded that its account of the adapter contract suites was wrong, and concluded that **a handoff's account of its own work is a hypothesis**. Duplicating sixty findings across two files would have manufactured exactly that failure at scale.

## Consequences

There is no longer one place to read every trap at once, which is a real loss for anyone auditing the whole set — the pointer table in `HANDOFF.md` is the index, and `grep -r "^- \*\*" --include=CLAUDE.md src tests` is the concatenation. Six small files must each stay accurate rather than one large one, though each is now short enough that an inaccuracy is visible.

A trap that spans modules has to be filed somewhere and cross-referenced from the other, which is why the `stdio_client` environment trap lives under `server/` and is pointed at from `tests/`, and why the Maestro artifact-layout trap is split between `runs/` (which flags to inject) and `eventsources/` (what the parser must then cope with). Filing a new trap now requires a judgement about where someone would be standing when they need it.

Adding a seventh module means adding a seventh `CLAUDE.md` rather than appending to a list. The nested files also earn the repository its scoped-rules score under `harness-score`, though that is a consequence and not the reason: files with no frontmatter are exempt from the frontmatter check and count as path-scoped by virtue of their location, so the score would have been identical with a single always-on rule at `src/` — which is precisely the arrangement this decision rejects.
