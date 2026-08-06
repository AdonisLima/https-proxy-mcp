"""P5's acceptance criterion, executed rather than asserted in prose.

PLAN.md asks for "installable and usable on a second machine from published artifacts alone",
and it is the easiest criterion in the plan to declare met without having installed anything.
There is no second machine here, so this stands in for one as closely as one machine can: ``uv
build`` produces the artifacts, the wheel is installed into a **fresh virtualenv**, and the
server is driven through *that* virtualenv's own console script from a working directory that is
not the repository. Nothing of the source tree is reachable, so anything the installed artifact
forgot to carry fails here rather than on somebody else's laptop.

*Usable*, not merely startable, is the point of the last test: it captures real traffic through
the installed server and reads an Exchange back. A handshake alone would pass on a build that
shipped the tool surface and none of the capture layer.

What none of this can prove is the part that needs a package index — that the name resolves,
that the long description renders, that ``uvx https-proxy-mcp`` with no ``--from`` finds
anything. That is publication rather than packaging, and it is recorded as outstanding in
HANDOFF.md instead of being quietly implied by a green suite here.
"""

from __future__ import annotations

import os
import subprocess
import zipfile
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from https_proxy_mcp import __version__
from test_server import REGISTERED_TOOLS

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Everything a wheel is allowed to contain: the package, and the metadata directory.
PACKAGE_PREFIX = "https_proxy_mcp/"
METADATA_MARKER = ".dist-info/"

#: Every interpreter ``requires-python`` admits, oldest first. Installing under each is what
#: makes the resolution honest: the dependency ranges, not the lock file, are what a consumer
#: gets. Necessarily a snapshot at the open end — an interpreter that does not exist yet cannot
#: be tested, and `requires-python` promises it anyway.
SUPPORTED_PYTHONS = ("3.12", "3.13")


def run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    """A subprocess whose failure output is visible.

    ``check=True`` alone reports the exit code and swallows the message that says why, which for
    a failing install is the only useful part.
    """
    completed = subprocess.run(argv, capture_output=True, text=True, **kwargs)
    if completed.returncode != 0:
        raise AssertionError(
            f"{' '.join(argv)} exited {completed.returncode}\n"
            f"stdout: {completed.stdout}\nstderr: {completed.stderr}"
        )
    return completed


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> Path:
    """The wheel ``uv build`` produces, built once and never into the repository's own ``dist``."""
    out = tmp_path_factory.mktemp("dist")
    run(["uv", "build", "--out-dir", str(out)], cwd=REPO_ROOT)

    built = sorted(out.glob("*.whl"))
    assert len(built) == 1, [path.name for path in out.iterdir()]
    return built[0]


@pytest.fixture(scope="module")
def wheel_names(wheel) -> list[str]:
    with zipfile.ZipFile(wheel) as archive:
        return archive.namelist()


def test_the_wheel_ships_the_package_and_nothing_else(wheel_names):
    """Asserted as "nothing outside these two prefixes", not as a denylist of things noticed.

    A wheel that carries the test suite also carries
    ``tests/fixtures/maestro-commands-failing-assert.json`` — 123 KB of one device's UI tree,
    checked in deliberately and of no use to anyone installing this. A denylist would have to
    name it, and would not name whatever the next one is.
    """
    stray = [
        name
        for name in wheel_names
        if not name.startswith(PACKAGE_PREFIX) and METADATA_MARKER not in name
    ]

    assert stray == []


def test_the_wheel_carries_no_build_residue(wheel_names):
    assert [name for name in wheel_names if "__pycache__" in name or name.endswith(".pyc")] == []


def test_the_wheel_carries_every_module_the_server_imports(wheel_names):
    """The whole package, not just the top level. A missing subpackage is an ImportError on
    somebody else's machine and nothing at all on this one, where the source tree is importable.
    """
    shipped = {name for name in wheel_names if name.endswith(".py")}
    expected = {
        f"{PACKAGE_PREFIX}{path.relative_to(REPO_ROOT / 'src' / 'https_proxy_mcp')}"
        for path in (REPO_ROOT / "src" / "https_proxy_mcp").rglob("*.py")
    }

    assert expected - shipped == set()


def read_metadata_file(wheel: Path, names: list[str], suffix: str) -> str:
    """One file out of the wheel's ``.dist-info``, or a failure that says which one is missing.

    A bare ``next()`` raises ``StopIteration`` here, and a test that reports StopIteration has
    told you nothing about what the wheel got wrong.
    """
    matching = [name for name in names if name.endswith(suffix)]
    assert matching, f"the wheel declares no {suffix}; it contains {sorted(names)}"

    with zipfile.ZipFile(wheel) as archive:
        return archive.read(matching[0]).decode()


def test_the_wheel_declares_the_console_script(wheel, wheel_names):
    """``uvx https-proxy-mcp`` resolves the command from here. A wheel without it installs a
    library that nobody can start, and the handshake test below is what would notice."""
    declared = read_metadata_file(wheel, wheel_names, "entry_points.txt")

    assert "https-proxy-mcp = https_proxy_mcp:main" in declared


def test_the_wheel_carries_its_licence_text_and_not_just_a_label(wheel, wheel_names):
    """A licence a consumer cannot read is a compliance gap, not a metadata field.

    ``license-files`` is what puts the text itself in the artifact; the SPDX expression alone
    names a licence whose terms travel nowhere. Both are asserted because they are set separately
    and either can be dropped without the other complaining.
    """
    metadata = read_metadata_file(wheel, wheel_names, "METADATA")

    assert "License-Expression: MIT" in metadata
    assert [name for name in wheel_names if name.endswith("licenses/LICENSE")]


def test_the_interpreters_under_test_are_the_ones_the_package_promises(wheel, wheel_names):
    """Guards the guard.

    ``requires-python`` is a promise to a consumer and the resolution test below is the only
    thing that checks it. Lower the floor without adding the interpreter here and the new promise
    ships unverified — which is precisely how ``mcp>=1.29.0`` came to be broken on an interpreter
    nobody had installed it under.
    """
    metadata = read_metadata_file(wheel, wheel_names, "METADATA")
    declared = [
        line.removeprefix("Requires-Python:").strip()
        for line in metadata.splitlines()
        if line.startswith("Requires-Python:")
    ]

    assert declared == [f">={SUPPORTED_PYTHONS[0]}"]


def test_the_wheel_reports_our_version_in_its_metadata(wheel, wheel_names):
    """The metadata is where ``__version__`` comes from once installed, so a wheel whose version
    disagrees with the running package would make ``serverInfo.version`` a fiction."""
    metadata = read_metadata_file(wheel, wheel_names, "METADATA")

    assert f"Version: {__version__}" in metadata


@pytest.mark.parametrize("python", SUPPORTED_PYTHONS)
async def test_the_built_wheel_installs_clean_and_captures_traffic(wheel, tmp_path, python):
    """The criterion itself: a fresh virtualenv, the installed console script, real traffic.

    Parametrised over every interpreter ``requires-python`` admits, and that is not thoroughness
    for its own sake — it is the only thing here that tests **resolution**. A wheel carries no
    lock file, so a consumer resolves from ``Requires-Dist`` alone, and an unbounded dependency
    can hand two machines two different libraries. It did: with ``mcp>=1.29.0`` uncapped this
    test passed on 3.12 and died on 3.13 with ``No module named 'mcp.server.fastmcp'``, because
    the resolver chose mcp 2.0.0 there. 3.12 was not safe either — mcp 2.0.0 declares
    ``Requires-Python: >=3.10``, so it was an eligible candidate on both and 3.12 merely got
    lucky with a warm cache.

    The client's whole lifetime stays inside the test body. pytest-asyncio may run an
    async-generator fixture's setup and teardown in different tasks, and the anyio cancel scopes
    inside ``stdio_client`` reject that — "Attempted to exit cancel scope in a different task".
    """
    venv = tmp_path / "second-machine"
    run(["uv", "venv", "--python", python, str(venv)])
    run(["uv", "pip", "install", "--python", str(venv / "bin" / "python"), str(wheel)])

    script = venv / "bin" / "https-proxy-mcp"
    assert script.exists(), sorted(path.name for path in (venv / "bin").iterdir())

    params = StdioServerParameters(
        command=str(script),
        args=[],
        # Session bodies hold real credentials, so they go under tmp_path here as everywhere.
        env={**os.environ, "HTTPS_PROXY_MCP_ROOT": str(tmp_path / "root")},
        # Not the repository. If anything is picked up from the source tree, this is what stops
        # the test passing for the wrong reason.
        cwd=str(tmp_path),
    )

    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        init = await session.initialize()
        tools = await session.list_tools()

        assert init.serverInfo.name == "https-proxy-mcp"
        assert init.serverInfo.version == __version__
        assert {tool.name for tool in tools.tools} == REGISTERED_TOOLS

        started = await session.call_tool("start_capture", {"target": "curl", "port": 0})
        port = started.structuredContent["port"]

        # --noproxy "" is required: an ambient NO_PROXY sends this direct and records nothing.
        proxied = ["-x", f"http://127.0.0.1:{port}", "--noproxy", ""]
        run(["curl", "-sS", "-o", "/dev/null", *proxied, "http://example.com"])

        listed = await session.call_tool("list_exchanges", {})
        await session.call_tool("stop_capture", {})

    hosts = [row["host"] for row in listed.structuredContent["exchanges"]]
    assert "example.com" in hosts
