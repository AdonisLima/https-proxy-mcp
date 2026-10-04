"""The documentation this work is not finished without, asserted rather than proofread.

This project has more prose than code and its claims are load-bearing — a guarantee stated in a
docstring is what an agent believes, so a stale one is a defect with a slow fuse. The tests here
are the ones that can be executed: does a decision record exist for each behaviour change, does
any *live* document still assert the thing the code stopped doing, and does the reviewer that
hunts documentation drift know about the drift this branch deliberately introduced.

Two directions matter equally, and the second is the one that is easy to lose:

* A **live** claim — the module docstring that owns the reporting narrowings, the parameter
  description an agent reads, the guide, the candidates document that recommends future work —
  must match the code.
* A **record of intent** — ``SPEC.md``, ``PLAN.md`` — must *not*. Passthrough made SPEC.md's
  "capture is unconditional" untrue of the code, and that is drift, which this project allows and
  records. So the assertions below check that SPEC.md still says the old thing, and that the
  reviewer's own list of known divergences has grown to cover it. Without the second half, every
  future review reports the same finding forever.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

AGENTS = ROOT / "AGENTS.md"
CONTEXT = ROOT / "CONTEXT.md"
SPEC = ROOT / "SPEC.md"
HANDOFF = ROOT / "HANDOFF.md"
ADR_DIR = ROOT / "docs" / "adr"
REVIEWER = ROOT / ".claude" / "agents" / "doc-drift-reviewer.md"
CANDIDATES = ROOT / "docs" / "v1.1-candidates.md"
USAGE = ROOT / "docs" / "USAGE.md"
INSTALL = ROOT / "docs" / "INSTALL.md"
README = ROOT / "README.md"
SKILL = ROOT / ".claude" / "skills" / "https-proxy-mcp" / "SKILL.md"
REPORTING = ROOT / "src" / "https_proxy_mcp" / "capture" / "reporting.py"
SESSION_TOOLS = ROOT / "src" / "https_proxy_mcp" / "server" / "session_tools.py"
EXCHANGE_TOOLS = ROOT / "src" / "https_proxy_mcp" / "server" / "exchange_tools.py"

#: Counts are written as words in this project's prose, so a test that couples a number to the
#: thing it counts has to spell it. Deliberately small: a range that needs extending is a signal
#: worth stopping on rather than a gap to paper over.
NUMBER_WORDS = {4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}

#: The claim Passthrough falsified, in the three forms this repository wrote it in.
UNCONDITIONAL = (
    "Capture is unconditional",
    "Capture is **unconditional**",
    "Capture itself is never narrowed",
)


def word(count: int) -> str:
    return NUMBER_WORDS[count]


def collapsed(document: Path) -> str:
    """A document with its line wrapping removed, so a phrase can be asserted across a line break.

    Every count in this file is written into a paragraph somewhere, and where the wrap happens to
    fall is not a thing worth failing a test over.
    """
    return re.sub(r"\s+", " ", document.read_text())


def adr(number: str) -> str:
    """One decision record's text, found by its number rather than its title.

    Lower-cased, because the phrases asserted against it are arguments rather than sentences and
    where one happens to start a bullet is not the subject.
    """
    found = sorted(ADR_DIR.glob(f"{number}-*.md"))
    assert found, f"ADR-{number} does not exist"
    return found[0].read_text().lower()


# --- the two decision records -------------------------------------------------


def test_the_reading_order_counts_the_decision_records_the_repository_has():
    """AGENTS.md is loaded on every request and points at ``docs/adr/`` with a count.

    The count is the part that rots: adding a record is two edits and the second is easy to skip,
    after which the guide tells a reader there is less to read than there is. Coupled to the
    directory rather than proofread, and it fails in both directions — a record added without the
    count, and a count raised without the record.
    """
    records = sorted(ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md"))

    assert f"{word(len(records))} decisions and why" in collapsed(AGENTS)


def test_the_passthrough_record_says_what_was_rejected_and_not_only_what_was_chosen():
    """A record of the chosen option alone is what gets "fixed" back.

    Every phrase below is an alternative that was considered and turned down, and each is one a
    future reader will independently think of: one parameter instead of two concepts, a default
    that does not engage, the denylist feeding the transport decision, counting what was passed
    through, and installing the Interception Authority on the Mac to silence the browser warning.
    A record that has lost one of them has lost the argument against it.
    """
    record = adr("0007")

    for rejected in (
        "redefine scope",  # one concept instead of two
        "second host list",  # two lists an agent would have to keep in step
        "denylist",  # letting a reporting narrowing decide a transport question
        "default it off",
        "show_ignored_hosts",  # counting what was passed through
        "install_authority_system",  # silencing the browser by trusting the Authority instead
    ):
        assert rejected in record, rejected


def test_the_passthrough_record_names_the_regression_it_accepted():
    """Two tools are exempt from the Scope on purpose, and Passthrough makes them find nothing for
    a host outside it — a wait blocks to its timeout instead of being vetoed. An accepted
    regression that is not written down is indistinguishable from one nobody noticed."""
    record = adr("0007")

    assert "wait_for_exchanges" in record
    assert "get_exchange" in record


def test_the_ownership_record_says_what_was_rejected_and_not_only_what_was_chosen():
    """Same requirement, and here the rejected options are the tempting ones: take the redirect
    over, wait for the holder, offer a tool that forces a release, identify the owner by process
    id alone, or give every editor window its own root directory."""
    record = adr("0008")

    for rejected in (
        "take the record over",
        "wait for the holder",
        "force-release",
        "session id",  # a better message at the cost of the target contract
        "reboot",  # why a process id alone is not enough
        "root directory",  # per-window isolation, which makes the sharing problem worse
    ):
        assert rejected in record, rejected


def test_the_ownership_record_names_the_escape_hatch_it_relies_on():
    """Refusing without an escape hatch would be a diagnosis with no remedy. The hatch is a signal
    to the holder, which makes *it* restore the machine — the reason no force-release tool
    exists."""
    record = adr("0008")

    assert "kill" in record
    assert "sigterm" in record


# --- the live claim, and the record of intent that keeps it --------------------


@pytest.mark.parametrize(
    "document",
    [
        REPORTING,
        SESSION_TOOLS,
        EXCHANGE_TOOLS,
        USAGE,
        INSTALL,
        README,
        CONTEXT,
        CANDIDATES,
        SKILL,
    ],
    ids=lambda path: path.name,
)
def test_no_live_document_still_says_capture_is_unconditional(document):
    """The claim Passthrough falsified, hunted where it is a *live* statement.

    These are the documents a reader acts on: the module docstring that owns the reporting
    narrowings, the two tool surfaces an agent reads before calling anything, the three human
    guides, the glossary, the candidates document that recommends future work, and the shipped
    skill. Under Passthrough only the Scope's hosts are decrypted, so "everything through the
    proxy is recorded" is no longer true of any of them.
    """
    text = document.read_text()

    for claim in UNCONDITIONAL:
        assert claim not in text, claim


def test_the_specification_is_not_edited_to_match_the_code():
    """The other direction, and the one this project's non-negotiables exist to protect.

    SPEC.md is the v1 specification and a historical record of intent. It says capture is
    unconditional, its ``start_capture`` row has no ``passthrough``, and its diagnosis lists five
    verdicts. All three are now untrue of the code, all three stay. A reader who "fixes" them
    fails here, which is the only way that intent survives being obviously wrong.
    """
    spec = SPEC.read_text()

    assert "Capture is unconditional" in spec
    assert "passthrough" not in spec
    assert "proxy_held_elsewhere" not in spec


def test_the_glossary_admits_the_one_thing_passthrough_does_not_cover():
    """Measured by hand in ticket 07, and it contradicts what this branch had already written.

    Passthrough scopes **TLS** interception. A plain-HTTP proxy request carries no host name until
    its request line is parsed, which happens after the engine has chosen a layer, so mitmproxy
    cannot pass it through and intercepts it — all eleven Exchanges of the hand-verification were
    plain-HTTP bundler traffic from a host the Scope never named. "Traffic in Passthrough is
    invisible to this tool, and unaffected by it" was therefore false as written, and the glossary
    is where the exception has to live because every other document quotes this one.
    """
    context = CONTEXT.read_text()
    entry = context.split("**Passthrough**:", 1)[1].split("_Avoid_", 1)[0]

    assert "invisible to this tool, and unaffected by it" not in context
    assert "plain HTTP" in entry


def test_the_candidates_document_no_longer_argues_from_the_claim_it_quoted():
    """``docs/v1.1-candidates.md`` recommends what to build next, which makes it a live document.

    Its case for ``--mode local`` rests on three consequences of having no process attribution,
    and Passthrough has already addressed one of them without it. Leaving the argument as written
    would recommend a day's probe for a benefit half of which is already delivered.
    """
    section = CANDIDATES.read_text().split("## 3. `--mode local`", 1)[1].split("\n## ", 1)[0]

    assert "Passthrough" in section


# --- the reviewer's own list of known divergences ------------------------------

#: How the reviewer enumerates a known SPEC.md divergence: a numbered item, indented under the
#: bullet that introduces the list.
KNOWN_DRIFT = re.compile(r"^\s+\d+\. ", re.MULTILINE)


def known_drifts() -> list[str]:
    """The divergences the doc-drift reviewer is told not to report, one per numbered item."""
    section = REVIEWER.read_text().split("## What is authoritative", 1)[1].split("\n## ", 1)[0]
    return KNOWN_DRIFT.findall(section)


def test_the_reviewer_counts_the_divergences_it_lists():
    """The reviewer's list and its own summary of the list, coupled.

    This is the file that decides whether a future review reports Passthrough as a defect. Its
    count is prose and its list is data, and the two disagreeing is how an entry gets appended
    without being read.
    """
    assert f"in {word(len(known_drifts()))} known places" in collapsed(REVIEWER)


def test_the_agent_guide_and_the_reviewer_agree_on_how_far_the_specification_has_drifted():
    """AGENTS.md carries the same count as a non-negotiable, because that is the copy an agent
    reads before it opens anything. Two copies of one number is exactly the drift this project
    keeps paying for, so they are asserted equal rather than maintained by hand."""
    assert f"drifted from the code in {word(len(known_drifts()))} places" in collapsed(AGENTS)


def test_the_reviewer_knows_about_every_divergence_this_branch_introduced():
    """Named, not merely counted. Each of these is a finding a review would otherwise report on
    every future run: the specification says capture is unconditional, its ``start_capture`` has
    no ``passthrough``, it lists five verdicts where the code has six, and its teardown guarantee
    promises a repair that ownership now declines for a live foreign owner."""
    reviewer = REVIEWER.read_text()

    for divergence in ("unconditional", "passthrough", "proxy_held_elsewhere", "owner"):
        assert divergence in reviewer, divergence


# --- the decisions table and the module map -----------------------------------


def test_the_decisions_table_carries_a_row_for_each_behaviour_change():
    """AGENTS.md's "do not reopen" table is the shortest path to not undoing this work, and it is
    phrased as "why this looks odd" precisely because both changes do. Scoped interception looks
    like the headline feature switched off; refusing a second window looks unhelpfully strict."""
    sections = AGENTS.read_text().split("Added by Passthrough and sentinel ownership", 1)

    assert len(sections) == 2, "the decisions table has no section for this work"
    rows = sections[1].split("\n## ", 1)[0]
    assert "Passthrough" in rows
    assert "owner" in rows


def test_the_module_map_names_every_module_in_the_package():
    """AGENTS.md's "What exists now" is how an agent decides where a change belongs, so a module
    missing from it is a module the next reader reimplements somewhere else.

    The table names some rows as a whole directory, so either form counts — but only in the
    backticked form the table itself writes, because the bare string ``capture/`` is a substring of
    every path under it and would let a new module in that directory pass unnamed.
    """
    agents = AGENTS.read_text()
    package = ROOT / "src" / "https_proxy_mcp"

    missing = [
        str(module.relative_to(package))
        for module in sorted(package.rglob("*.py"))
        if module.name != "__init__.py"
        and f"`{module.relative_to(package)}`" not in agents
        and f"`{module.parent.relative_to(package)}/`" not in agents
    ]

    assert missing == []


# --- the handoff --------------------------------------------------------------


def test_the_handoff_records_what_the_hand_verification_measured():
    """The one thing in this work that no automated test can assert: a browser and a second
    simulator, checked by hand. The handoff is written from what was measured, and the half that
    is easy to drop is the half that did not go as planned.

    This test used to assert ``"pinned"`` on the strength of a docstring claiming the app under
    test *was* certificate-pinned. It is not, and the assertion would have passed either way —
    ``"pinned"`` appears in both the claim and its retraction. That is the shape to watch for: a
    keyword assertion guards that a *subject* is discussed, never that the claim about it is
    true, so it kept a falsehood green for a day. What is pinned now is the retraction itself.
    """
    handoff = HANDOFF.read_text()

    for measured in (
        "translate.google.com",  # the passed-through host, identified by its issuer
        "Sidekick",  # the device that does not trust the Authority and kept its network
        "flake",  # the gated live test that failed once and passed alone
    ):
        assert measured in handoff, measured


def test_the_handoff_retracts_the_pinning_conclusion_rather_than_deleting_it():
    """The app under test was recorded as certificate-pinned, on the word of a `tls_failed`
    verdict whose count is machine-wide. A login flow was later captured and decrypted from the
    same build, so the conclusion was false.

    Deleting it would leave the next session free to re-derive it from the same verdict. The
    handoff has to carry the correction *and* enough of the measurement to stop that, so this
    asserts on the evidence rather than on the word "pinned", which survives either version.
    """
    handoff = HANDOFF.read_text()

    assert "decrypted" in handoff  # the flow that disproved it
    assert "machine-wide" in handoff  # the defect that produced it
    assert "189" in handoff  # the evidence that was on disk the whole time


def test_the_handoff_names_the_surface_this_work_added():
    """A handoff that describes the change without naming its surface leaves the next session
    grepping for it."""
    handoff = HANDOFF.read_text()

    assert "passthrough" in handoff
    assert "proxy_held_elsewhere" in handoff
