"""The Session — one capture lifetime, and the directory it owns.

:class:`ExchangeStore` knows only a directory. This module decides *which* directory, and
keeps them from accumulating forever. The split is deliberate: the store is about
recording, and a Session is about identity and lifetime.

The directory holds unredacted bodies, so everything here is ``0700`` and pruned. Retention
is a privacy feature as much as a disk one — an old Session is a pile of real tokens.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

#: Overrides where Sessions live. Set it to keep captured traffic — which is unredacted —
#: somewhere other than the home directory, and so tests never write to a real one.
ROOT_ENV = "HTTPS_PROXY_MCP_ROOT"


def default_root() -> Path:
    """Where Sessions live: ``$HTTPS_PROXY_MCP_ROOT``, else under the home directory.

    Resolved per call rather than at import, so setting the variable takes effect for a
    server started afterwards without depending on module import order.
    """
    override = os.environ.get(ROOT_ENV)
    return Path(override) if override else Path.home() / ".https-proxy-mcp"


SESSIONS_DIRNAME = "sessions"

#: Where mitmproxy keeps its Interception Authority. Shared across Sessions, so the authority a
#: Capture Target was told to trust stays valid for the next one — regenerating it per Session
#: would silently invalidate every device's trust store.
CONFDIR_NAME = "mitmproxy"

MANIFEST_FILENAME = "session.json"

#: How many Sessions survive a prune (SPEC.md, "Session retention").
RETAINED_SESSIONS = 10

#: Sortable to the microsecond, so lexicographic order *is* chronological order and
#: retention needs to read no files to know what is oldest.
ID_FORMAT = "%Y%m%dT%H%M%S-%f"


@dataclass(frozen=True, slots=True)
class Session:
    """One capture lifetime: an id, a private directory, and how capture was set up."""

    id: str
    dir: Path
    #: Epoch seconds, matching Exchange timestamps so the two are directly comparable.
    started: float
    target: str
    port: int
    #: Hosts the agent is interested in. Narrows what is *reported*, never what is
    #: captured (CONTEXT.md). ``None`` means everything is reported.
    scope: tuple[str, ...] | None

    @classmethod
    def create(
        cls,
        root: Path | str,
        *,
        target: str,
        port: int,
        scope: list[str] | None = None,
        now: datetime | None = None,
    ) -> Session:
        """Create the Session directory and write its manifest.

        Fails loudly if the directory already exists rather than adopting it: two Sessions
        sharing one directory would interleave their Exchanges and their ids.
        """
        started = now or datetime.now()
        session_id = started.strftime(ID_FORMAT)
        sessions_root = Path(root) / SESSIONS_DIRNAME
        sessions_root.mkdir(parents=True, exist_ok=True)
        # mkdir's mode is subject to umask; chmod is not, and these directories hold secrets.
        sessions_root.chmod(0o700)

        session_dir = sessions_root / session_id
        session_dir.mkdir(exist_ok=False)
        session_dir.chmod(0o700)

        session = cls(
            id=session_id,
            dir=session_dir,
            started=started.timestamp(),
            target=target,
            port=port,
            scope=tuple(scope) if scope is not None else None,
        )
        (session_dir / MANIFEST_FILENAME).write_text(json.dumps(session.to_manifest(), indent=2))
        return session

    def to_manifest(self) -> dict[str, object]:
        """The contents of ``session.json``. Not the directory path — that is where it is."""
        return {
            "id": self.id,
            "started": self.started,
            "target": self.target,
            "port": self.port,
            "scope": list(self.scope) if self.scope is not None else None,
        }


def session_ids(root: Path | str) -> list[str]:
    """Every Session id under ``root``, newest first.

    Ordering comes from the id itself, so nothing has to be opened — a Session whose
    manifest is missing or corrupt still sorts and still prunes.
    """
    sessions_root = Path(root) / SESSIONS_DIRNAME
    if not sessions_root.is_dir():
        return []
    return sorted(
        (entry.name for entry in sessions_root.iterdir() if entry.is_dir()),
        reverse=True,
    )


def prune_sessions(root: Path | str, keep: int = RETAINED_SESSIONS) -> list[str]:
    """Delete all but the ``keep`` most recent Sessions. Returns the ids removed."""
    sessions_root = Path(root) / SESSIONS_DIRNAME
    doomed = session_ids(root)[keep:]
    for session_id in doomed:
        shutil.rmtree(sessions_root / session_id)
    return doomed
