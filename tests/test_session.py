"""Unit tests for Session identity, directory layout and retention.

A Session is one capture lifetime (CONTEXT.md). ExchangeStore deliberately knows only a
directory; everything about *which* directory, and about not accumulating them forever,
lives here.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from https_proxy_mcp.capture.session import (
    RETAINED_SESSIONS,
    ROOT_ENV,
    SESSIONS_DIRNAME,
    Session,
    default_root,
    prune_sessions,
    session_ids,
)


def at(second: int, microsecond: int = 0) -> datetime:
    """A fixed clock reading, so id ordering is asserted rather than raced."""
    return datetime(2026, 8, 5, 14, 30, second, microsecond)


# --- creation -----------------------------------------------------------------


def test_a_session_directory_is_created_under_the_sessions_root(tmp_path):
    session = Session.create(tmp_path, target="curl", port=8080)

    assert session.dir == tmp_path / SESSIONS_DIRNAME / session.id
    assert session.dir.is_dir()


def test_a_missing_root_is_created(tmp_path):
    Session.create(tmp_path / "fresh" / "root", target="curl", port=8080)

    assert (tmp_path / "fresh" / "root" / SESSIONS_DIRNAME).is_dir()


def test_the_session_directory_is_private(tmp_path):
    """It will hold unredacted bodies (SPEC.md, "Session directory")."""
    session = Session.create(tmp_path, target="curl", port=8080)

    assert session.dir.stat().st_mode & 0o777 == 0o700


def test_the_sessions_root_is_private(tmp_path):
    """Listing which Sessions exist is itself worth not leaking."""
    Session.create(tmp_path, target="curl", port=8080)

    assert (tmp_path / SESSIONS_DIRNAME).stat().st_mode & 0o777 == 0o700


def test_the_id_encodes_its_creation_time(tmp_path):
    session = Session.create(tmp_path, target="curl", port=8080, now=at(22, 481267))

    assert session.id == "20260805T143022-481267"


def test_ids_sort_chronologically(tmp_path):
    """Retention sorts by id, so the id has to carry the ordering itself."""
    earlier = Session.create(tmp_path, target="curl", port=8080, now=at(10))
    later = Session.create(tmp_path, target="curl", port=8080, now=at(20))

    assert earlier.id < later.id


def test_ids_are_ordered_within_the_same_second(tmp_path):
    """Microsecond resolution, so two Sessions a moment apart still order correctly."""
    earlier = Session.create(tmp_path, target="curl", port=8080, now=at(10, 1))
    later = Session.create(tmp_path, target="curl", port=8080, now=at(10, 2))

    assert earlier.id < later.id


def test_a_colliding_session_id_fails_loudly(tmp_path):
    """Silently sharing a directory would interleave two Sessions' Exchanges."""
    Session.create(tmp_path, target="curl", port=8080, now=at(10))

    with pytest.raises(FileExistsError):
        Session.create(tmp_path, target="curl", port=8080, now=at(10))


# --- session.json -------------------------------------------------------------


def test_the_manifest_records_the_session(tmp_path):
    session = Session.create(
        tmp_path, target="ios-simulator", port=9090, scope=["api.example.com"], now=at(10)
    )

    manifest = json.loads((session.dir / "session.json").read_text())

    assert manifest == {
        "id": "20260805T143010-000000",
        "started": session.started,
        "target": "ios-simulator",
        "port": 9090,
        "scope": ["api.example.com"],
    }


def test_an_absent_scope_is_recorded_as_null(tmp_path):
    session = Session.create(tmp_path, target="curl", port=8080)

    assert json.loads((session.dir / "session.json").read_text())["scope"] is None


def test_the_start_time_is_an_epoch_timestamp(tmp_path):
    session = Session.create(tmp_path, target="curl", port=8080, now=at(10))

    assert session.started == at(10).timestamp()


def test_scope_is_exposed_as_a_tuple_so_it_cannot_be_mutated(tmp_path):
    session = Session.create(tmp_path, target="curl", port=8080, scope=["a.example.com"])

    assert session.scope == ("a.example.com",)


# --- listing ------------------------------------------------------------------


def test_sessions_are_listed_newest_first(tmp_path):
    for second in (10, 30, 20):
        Session.create(tmp_path, target="curl", port=8080, now=at(second))

    assert session_ids(tmp_path) == [
        "20260805T143030-000000",
        "20260805T143020-000000",
        "20260805T143010-000000",
    ]


def test_listing_an_untouched_root_is_empty_rather_than_an_error(tmp_path):
    assert session_ids(tmp_path / "nothing-here") == []


def test_stray_files_are_not_listed_as_sessions(tmp_path):
    Session.create(tmp_path, target="curl", port=8080, now=at(10))
    (tmp_path / SESSIONS_DIRNAME / ".DS_Store").write_text("")

    assert session_ids(tmp_path) == ["20260805T143010-000000"]


# --- retention ----------------------------------------------------------------


def test_retention_defaults_to_ten():
    assert RETAINED_SESSIONS == 10


def test_pruning_keeps_the_most_recent_sessions(tmp_path):
    for second in range(1, 6):
        Session.create(tmp_path, target="curl", port=8080, now=at(second))

    prune_sessions(tmp_path, keep=2)

    assert session_ids(tmp_path) == ["20260805T143005-000000", "20260805T143004-000000"]


def test_pruning_reports_what_it_removed(tmp_path):
    for second in range(1, 4):
        Session.create(tmp_path, target="curl", port=8080, now=at(second))

    assert prune_sessions(tmp_path, keep=1) == [
        "20260805T143002-000000",
        "20260805T143001-000000",
    ]


def test_pruning_below_the_limit_removes_nothing(tmp_path):
    Session.create(tmp_path, target="curl", port=8080, now=at(10))

    assert prune_sessions(tmp_path, keep=10) == []
    assert session_ids(tmp_path) == ["20260805T143010-000000"]


def test_pruning_removes_the_whole_session_tree(tmp_path):
    old = Session.create(tmp_path, target="curl", port=8080, now=at(10))
    (old.dir / "bodies").mkdir()
    (old.dir / "bodies" / "1.resp").write_bytes(b"a secret body")
    Session.create(tmp_path, target="curl", port=8080, now=at(20))

    prune_sessions(tmp_path, keep=1)

    assert not old.dir.exists()


def test_pruning_an_untouched_root_is_harmless(tmp_path):
    assert prune_sessions(tmp_path / "nothing-here", keep=10) == []


# --- where Sessions live ------------------------------------------------------


def test_the_root_defaults_under_the_home_directory(monkeypatch):
    monkeypatch.delenv(ROOT_ENV, raising=False)

    assert default_root() == Path.home() / ".https-proxy-mcp"


def test_the_root_can_be_overridden_by_environment(monkeypatch, tmp_path):
    """Captured bodies are unredacted, so being able to put them elsewhere matters."""
    monkeypatch.setenv(ROOT_ENV, str(tmp_path / "elsewhere"))

    assert default_root() == tmp_path / "elsewhere"


def test_the_root_is_resolved_per_call_not_at_import(monkeypatch, tmp_path):
    monkeypatch.setenv(ROOT_ENV, str(tmp_path / "first"))
    first = default_root()
    monkeypatch.setenv(ROOT_ENV, str(tmp_path / "second"))

    assert (first, default_root()) == (tmp_path / "first", tmp_path / "second")
