---
description: Refresh HANDOFF.md's state section from reality rather than from memory
allowed-tools: Bash(uv run:*), Bash(git:*), Bash(npx harness-score:*)
---

Rewrite the parts of `HANDOFF.md` that describe the current state, using measurements you take
now. Do not edit the parts that record history or decisions.

Measure first — every number in that file should come from a command you just ran, not from what
the file already claims:

```bash
uv run pytest -q                        # test count, skip count, wall time
time uv run pytest -q -m "not integration"   # the fast-suite claim
git log --oneline -8
git status --short
npx harness-score@1.5.1 --quiet --json | head -20
```

Then update, in `HANDOFF.md` only:

- The heading's date and phase summary.
- **State**: counts, timings, branch, remote, whether the tree is clean.
- **Open questions**: strike through any the session answered, with what the answer was. Keep
  the numbering stable — answered questions stay in place struck through, they do not get
  deleted and renumbered.
- **Decisions that belong to the user, still open**: remove what the user has now decided,
  recording what they chose and why where it will be read again.

Leave alone: "The finding that shaped P3/P4", the pointer table, and anything in `AGENTS.md`,
the nested `CLAUDE.md` files, or the ADRs. Those are not state.

Two rules from this project's own history apply to you here:

- **A handoff's account of its own work is a hypothesis.** If the file asserts something about
  the code, check the code before carrying the claim forward.
- Do not re-raise a decision the file records as the user's standing choice unless they have
  brought it up.
