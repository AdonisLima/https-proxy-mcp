# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the
codebase. This is a **single-context** repo: one `CONTEXT.md` and one `docs/adr/` at the root.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root — the domain language.
- **`docs/adr/`** — read the ADRs that touch the area you're about to work in.
- **`src/https_proxy_mcp/<module>/CLAUDE.md`** — per-module trap files. Read the one beside any
  module you're about to change; each records a real debugging session. See ADR-0006.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest
creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and
`/improve-codebase-architecture`) creates them lazily when terms or decisions actually get resolved.

## File structure

```
/
├── CONTEXT.md
├── docs/adr/
│   ├── 0001-python-monolith.md
│   └── …
└── src/https_proxy_mcp/
    ├── capture/CLAUDE.md          ← per-module traps
    ├── targets/CLAUDE.md
    └── …
```

A root `CONTEXT-MAP.md` would signal a multi-context repo with one `CONTEXT.md` per context. This
repo has none, and adding one is a deliberate restructure, not a default.

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test
name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly
avoids — here that matters more than usual, since the vocabulary was chosen to dodge a real
collision between mitmproxy's "flow" and Maestro's "flow".

If the concept you need isn't in the glossary yet, that's a signal — either you're inventing language
the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0004 (overlap attribution) — but worth reopening because…_
