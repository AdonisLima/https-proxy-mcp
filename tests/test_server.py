"""The server's shape: identity, the registered tool surface, and its self-description.

The empty-tool-list assertion that stood here through P0 has done its job and is gone — it
was written to fail loudly at exactly this point. What replaces it is the P2 surface, plus two
structural guards that are cheap to check here and expensive to notice broken anywhere else.
"""

import re
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path
from typing import get_args

import pytest

from https_proxy_mcp import __version__, build_server
from https_proxy_mcp.server import SERVER_NAME
from https_proxy_mcp.targets.base import DiagnosisStatus
from https_proxy_mcp.version import UNKNOWN_VERSION, resolve_version

#: Every tool the v1 read-only surface registers. Asserted exactly rather than by subset, so
#: adding one is a deliberate act and removing one cannot pass unnoticed.
REGISTERED_TOOLS = {
    "start_capture",
    "stop_capture",
    "get_status",
    "diagnose_capture",
    "get_trust_status",
    "install_authority",
    "install_authority_system",
    "list_exchanges",
    "get_exchange",
    "get_exchange_body",
    "search_exchanges",
    "tail_exchanges",
    "wait_for_exchanges",
    "export_curl",
    # Runs and Timelines (P4).
    "run_test",
    "list_runs",
    "get_timeline",
    "start_step",
    "end_step",
    "export_har",
}


@pytest.fixture
async def tools():
    return await build_server().list_tools()


def test_server_builds_with_expected_identity():
    assert build_server().name == SERVER_NAME


def test_the_server_reports_its_own_version_not_the_sdk_s():
    """``serverInfo.version`` is how a client tells one build of this server from another.

    ``FastMCP.__init__`` accepts no ``version``, so left alone the low-level server falls back to
    ``importlib.metadata.version("mcp")`` and every release of this project claims to be the
    SDK's — a number that changes when the dependency does and says nothing about us.

    Asserted through ``create_initialization_options`` because that is the exact value the
    handshake puts on the wire, rather than an attribute that might not reach it.
    """
    options = build_server()._mcp_server.create_initialization_options()

    assert options.server_version == __version__
    assert options.server_version != distribution_version("mcp")


def test_a_source_tree_with_no_metadata_says_it_does_not_know(monkeypatch):
    """An uninstalled tree has no version, and saying so beats inventing one.

    The alternative is a literal in the source next to the one in ``pyproject.toml``, which is
    two sources of truth for the same fact and drifts the moment one is bumped alone. Here
    ``pyproject.toml`` stays the only place a version is written, and a tree without metadata is
    visibly not a release.
    """

    def missing(_name: str) -> str:
        raise PackageNotFoundError(_name)

    monkeypatch.setattr("https_proxy_mcp.version.distribution_version", missing)

    assert resolve_version() == UNKNOWN_VERSION


async def test_the_tool_surface_is_registered(tools):
    assert {tool.name for tool in tools} == REGISTERED_TOOLS


async def test_every_tool_describes_when_to_use_it(tools):
    """A tool with no description is a tool an agent has to guess at."""
    undescribed = [tool.name for tool in tools if not (tool.description or "").strip()]

    assert undescribed == []


async def test_listing_exchanges_has_no_parameter_that_could_return_a_body(tools):
    """Progressive disclosure enforced at the schema, not by asking nicely.

    ``list_exchanges`` must offer no way to ask for body content, so an agent cannot flood its
    own context in a single call (SPEC.md). Adding such a parameter should fail here.
    """
    listing = next(tool for tool in tools if tool.name == "list_exchanges")

    parameters = set(listing.inputSchema["properties"])

    assert not {name for name in parameters if "body" in name or "secret" in name}


async def test_the_available_targets_are_named_in_the_start_tool():
    """Written in at registration, so it cannot drift from the registry as P3 adds targets."""
    tools = await build_server().list_tools()
    start = next(tool for tool in tools if tool.name == "start_capture")

    assert "curl" in (start.description or "")


async def test_the_instructions_tell_a_client_where_to_start():
    assert "start_capture" in (build_server().instructions or "")


async def test_the_instructions_name_the_path_the_project_exists_for():
    """`INSTRUCTIONS` is the whole prompt a client sees before it calls anything, and it described
    the Exchange tools alone. An agent reading only that would page through traffic and never
    learn that running the test through `run_test` and reading `get_timeline` is the point — which
    is the same omission SKILL.md exists to prevent, one layer further in.
    """
    instructions = build_server().instructions or ""

    assert "run_test" in instructions
    assert "get_timeline" in instructions


# --- the shipped skill -------------------------------------------------------
#
# SKILL.md is the third place this server describes itself, after INSTRUCTIONS and the twenty tool
# docstrings. It is the only one that can go stale silently: the docstrings live next to the code
# they describe and INSTRUCTIONS is a few lines, but a prompt in a separate file at the repo root
# has nothing tying it to the surface it documents. These two tests are that tie.

SKILL = (
    Path(__file__).resolve().parent.parent / ".claude" / "skills" / "https-proxy-mcp" / "SKILL.md"
)


def test_the_skill_has_the_frontmatter_a_harness_loads_it_by():
    """`description` is the trigger — a skill nothing matches against is a skill nobody loads."""
    head = SKILL.read_text().split("---")[1]

    assert "name: https-proxy-mcp" in head
    assert "description:" in head


def test_the_skill_names_every_registered_tool():
    """Twenty tools is more surface than one prompt holds by accident.

    Asserted in this direction — every tool appears — because that is the failure that hurts: a
    tool nobody documented is a tool an agent never calls, and no amount of reading SKILL.md
    reveals it is missing. The reverse check (no *unknown* tool named) is deliberately not made
    here; distinguishing a tool name from a parameter name in prose needs a curated exclusion list
    whose upkeep would cost more than the bug it catches.
    """
    skill = SKILL.read_text()

    assert sorted(tool for tool in REGISTERED_TOOLS if tool not in skill) == []


def test_the_skill_names_every_parameter_start_capture_takes():
    """The one tool whose parameters decide what the whole Session does.

    A parameter the skill omits is a capability an agent never reaches for — and ``passthrough``
    is worse than that, because its default *changes what is captured*. An agent that does not
    know the word cannot explain an empty listing and cannot turn it off.
    """
    skill = SKILL.read_text()

    for parameter in (
        "target",
        "port",
        "scope",
        "udid",
        "passthrough",
        "lens",
        "web_ui",
        "web_port",
    ):
        assert parameter in skill, parameter


def test_the_skill_lists_every_verdict_the_diagnosis_can_return():
    """Backticked, for the same reason the guide's check is: ``ok`` occurs inside ordinary words.

    The skill is the copy that leaves this repository — it is installed standalone on another
    machine — so a verdict it omits is one an agent meets in a result with nowhere to look it up.
    """
    skill = SKILL.read_text()

    missing = [verdict for verdict in get_args(DiagnosisStatus) if f"`{verdict}`" not in skill]

    assert sorted(missing) == []


def test_the_skill_carries_no_relative_link_into_this_repository():
    """It is copied to ``~/.claude/skills/https-proxy-mcp/SKILL.md`` on any machine, where every
    path in this repository resolves to nothing. A relative link is therefore a defect rather than
    a style preference, which is why it is asserted rather than reviewed."""
    links = re.findall(r"]\((\.{1,2}/[^)]*)\)", SKILL.read_text())

    assert links == []


# --- the guides ---------------------------------------------------------------
#
# USAGE.md documents every tool's parameters, which is the single most rot-prone thing written about
# this project: a renamed parameter breaks the guide silently, and the reader finds out by having a
# call rejected. So the reference is checked against the live schemas rather than proofread.

USAGE = Path(__file__).resolve().parent.parent / "docs" / "USAGE.md"
INSTALL = Path(__file__).resolve().parent.parent / "docs" / "INSTALL.md"

#: ``name(a, b, c)`` inside backticks, which is how the reference writes a signature.
SIGNATURE = re.compile(r"`(\w+)\(([^)]*)\)`")


def documented_signatures() -> dict[str, set[str]]:
    """Tool name to parameter names, as USAGE.md's reference section states them.

    Scoped to that one section deliberately. The prose and the recipes are full of real calls with
    real argument *values*, and treating those as the reference would make this test fail on an
    example rather than on a mistake.
    """
    body = USAGE.read_text().split("## Tool reference", 1)[1].split("\n## ", 1)[0]
    return {
        name: {param.strip() for param in params.split(",") if param.strip()}
        for name, params in SIGNATURE.findall(body)
    }


async def test_the_usage_guide_documents_every_tool(tools):
    documented = documented_signatures()

    assert sorted(tool.name for tool in tools if tool.name not in documented) == []


async def test_the_usage_guide_gets_every_parameter_name_right(tools):
    """Both directions at once: a parameter the guide invented, and one it never mentions.

    Invented names are the worse half — a reader copies the call and it is rejected — but an omitted
    one is a capability nobody knows exists, which is the same failure as an undocumented tool.
    """
    documented = documented_signatures()

    actual = {tool.name: set(tool.inputSchema["properties"]) for tool in tools}
    wrong = {
        name: {"documented": documented[name], "actual": parameters}
        for name, parameters in actual.items()
        if name in documented and documented[name] != parameters
    }

    assert wrong == {}


async def test_the_usage_guide_lists_every_verdict_the_diagnosis_can_return(tools):
    """The verdicts are a closed set in the code and prose everywhere else, which is where one gets
    left behind. A verdict the reference omits is one a reader meets in a result and cannot look up,
    and its remediation is the only reason the verdict exists.

    Both the guide and the tool's own description are checked, because they are read by different
    audiences: the guide by the developer, the description by the agent choosing what to call.
    """
    usage = USAGE.read_text()
    described = next(tool.description or "" for tool in tools if tool.name == "diagnose_capture")
    verdicts = get_args(DiagnosisStatus)

    # Backticked rather than bare, because `ok` is a substring of "look", "token" and "book": the
    # bare form is sharp for the five distinctive names and vacuous for the sixth, which is exactly
    # the one a reader is likeliest to meet.
    assert sorted(verdict for verdict in verdicts if f"`{verdict}`" not in usage) == []
    assert sorted(verdict for verdict in verdicts if f"`{verdict}`" not in described) == []


#: Counts written as words, so prose that counts something can be coupled to the thing it counts.
NUMBER_WORDS = {3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight"}

#: Any of those words in front of "causes", which is the *other* convention this set was counted
#: by. Two conventions for one closed set is worse than either — a reader who meets both cannot
#: tell which set is meant, and the two differ by exactly ``ok``.
COUNTED_CAUSES = re.compile(
    rf"({'|'.join(NUMBER_WORDS.values())})\s+(?:very different\s+)?causes", re.IGNORECASE
)

SRC = Path(__file__).resolve().parent.parent / "src" / "https_proxy_mcp"

#: The module whose docstring and ``diagnose`` both enumerate the verdicts in prose.
IOS_TARGET = SRC / "targets" / "ios_simulator.py"

#: Where the verdicts are *declared*, and the one place a reader looks up what a verdict is. Its
#: docstring counted them too, and said "three" from v1 until it was added here — the count was
#: stale through two widenings because nothing coupled it to the type three lines below it.
BASE_TARGET = SRC / "targets" / "base.py"


async def test_every_place_that_counts_the_verdicts_counts_the_ones_the_type_declares(tools):
    """One closed set, one count, one noun.

    The verdicts were counted in two conventions at once — "five very different causes", which
    excludes ``ok``, and "the six causes", which includes it — so a reader who met both could not
    tell which set was meant. Settled on **verdicts**, counted from
    ``get_args(DiagnosisStatus)``, which is the only definition there is. The second assertion is
    what retires the other convention rather than merely outnumbering it.
    """
    expected = f"{NUMBER_WORDS[len(get_args(DiagnosisStatus))]} verdicts"
    described = next(tool.description or "" for tool in tools if tool.name == "diagnose_capture")

    for text in (USAGE.read_text(), described, IOS_TARGET.read_text(), BASE_TARGET.read_text()):
        # Wrapping collapsed first: all three of these are hard-wrapped prose, and where the break
        # falls in "six verdicts" is not what is under test.
        flat = re.sub(r"\s+", " ", text).lower()
        assert expected in flat
        assert COUNTED_CAUSES.search(flat) is None


def test_the_install_guide_covers_the_things_that_actually_go_wrong():
    """Not a word count — these are the specific failures this project has already hit, and an
    install guide that omits them sends the reader hunting through the per-module CLAUDE.md
    files, which are written for someone editing the code rather than installing it."""
    install = INSTALL.read_text()

    for topic in ("HTTPS_PROXY_MCP_ROOT", "--noproxy", "mcp.server.fastmcp", "diagnose_capture"):
        assert topic in install, topic


async def test_no_resources_or_prompts_registered_yet():
    server = build_server()
    assert await server.list_resources() == []
    assert await server.list_prompts() == []
