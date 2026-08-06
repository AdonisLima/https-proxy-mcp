---
name: verification-discipline
description: Use when writing, reviewing or trusting tests in this repository — before adding a test, when a test passes and you want to know whether it could ever fail, when an acceptance criterion is about to be declared met, when probing an unknown platform API, or when a handoff document tells you something about the code that you have not checked. These are the practices that produced 675 tests with no known defects, and each one exists because something green turned out to prove nothing.
---

# How the work here gets verified

Not generic TDD. These are the specific practices this project accumulated, each after something
passed while being wrong.

## Observe the RED, for the intended reason

Write the test, run it, **read the failure**, confirm it fails for the reason you meant, then
implement. A test that fails because of a typo in the fixture is not a test that failed.

## Teeth-check by deliberately breaking the code

Every P4 unit was checked this way: thirty-one deliberate breaks across nine commits, each
expected to fail one *named* test. Two of the thirty-one were worth the whole exercise:

- One break failed nothing, because the patch had not applied — **so the check itself needs
  verifying**.
- One test turned out to be unable to fail at all under pytest, and was rewritten.

Name the test you expect to break before you break the code. If a different one breaks, or none
does, you have learned something.

## Probe unknown platform APIs before designing against them

P3's entire design came out of four short probes; P4's out of two, against a throwaway simulator.
Each took a minute. They changed the design twice and prevented one wrong Timeline. **Two of them
contradicted the documentation** — `--test-output-dir` does not contain the debug output despite
its own `--help`, and Maestro's command artifact is not in execution order.

## Record real state before mutating it, and diff afterwards

This is how the `Empty` bug was found and nothing else would have found it: every test passed
throughout, because `parse_bypass_domains` normalised both forms to `()`. **A parser that
normalises can hide a writer that does not.**

Applies to any state the tool writes that it did not create — the machine's proxy settings, and
the Session root.

## Read the artifacts a green test left behind

New in P4 and it earned its place immediately. The acceptance test passed; rebuilding its
Timeline from the session directory afterwards showed the mechanism it was supposed to exercise
had never fired. Safari had simply never fetched the URL, and the test passed on incidental
traffic.

**A green test tells you an assertion held, not that it held for the reason you intended.**

## Build the artifact and install it — do not reason about it

P5's entire finding came from this. Every in-repo test passed against the locked dependencies
throughout; the break existed only in what a *consumer* resolves. `mcp` uncapped resolved 2.0.0,
which has no `mcp.server.fastmcp` at all, and the installed server died on import. A wheel ships
no lock file. Nothing short of a clean virtualenv could see it — and a green clean-install on
*one* interpreter proves nothing, because one interpreter cannot notice a range two interpreters
disagree about.

## Writing the documentation is a test of the code

Two defects came out of writing `docs/USAGE.md` and `docs/INSTALL.md`, both because stating a
guarantee in prose forces you to check it holds everywhere you have just claimed it does:
`tail_exchanges` ignored the page-size ceiling every other listing enforced, and the README's
quick-start command had never worked.

Neither was findable by reading the code with no claim in hand. Corollary: capture the output you
document from a **real** run. The first attempt at the Timeline example produced an empty one,
and pasting it would have documented a non-result as the feature.

## Re-read the code before believing a handoff about it

P4's review recorded this and P5 hit it again. The handoff said adapter contract suites existed
for both adapter families. One was genuinely shared; the other was a single-target unit file
whose test asserted only that a method existed. It read as the shared suite.

**A handoff's account of its own work is a hypothesis.** That includes the sentence you are
reading.
