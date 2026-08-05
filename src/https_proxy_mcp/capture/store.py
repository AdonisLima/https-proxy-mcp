"""The ExchangeStore: bounded metadata in memory, faithful bodies and a log on disk.

Three jobs, split by what each is good at:

* **A bounded ring buffer** of Exchange metadata, so a long Session cannot exhaust memory.
  Evictions are counted and always reported, because an agent must be able to tell "there
  is nothing older" from "there was something older and it is gone".
* **Body spill to disk**, so bodies never sit in memory and are never reachable from a
  listing by construction — the failure mode for an agent-driven traffic tool is flooding
  the context window, and the API shape should make that impossible (SPEC.md).
* **An append-only NDJSON log**, which outlives the ring buffer. It is the durability
  story and it is tailable by anything that is not this process.

The store is deliberately **not** a redaction boundary. What lands here is faithful,
because "is my app sending the right token?" is a real debugging question and a scrubbed
store cannot answer it. The directory is therefore ``0700`` and documented as sensitive;
secrets stop at :mod:`https_proxy_mcp.serialize`.

Not internally synchronised: it assumes the addon and the tool surface share one event
loop, which ADR-0001's single process is what buys. A design that moves the proxy onto its
own thread needs a lock added here, deliberately.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import TracebackType

from https_proxy_mcp.capture.exchange import BodyRef, CapturedExchange, Exchange

#: Bodies land under this subdirectory of the Session directory, named by Exchange id.
BODIES_DIRNAME = "bodies"

#: The append-only metadata log.
LOG_FILENAME = "exchanges.ndjson"

#: Suffix per side of an Exchange.
_SUFFIXES = {"request": "req", "response": "resp"}


class ExchangeStore:
    """Records Exchanges into a Session directory, bounding what it keeps in memory."""

    DEFAULT_CAPACITY = 5000
    DEFAULT_BODY_CAP_BYTES = 10 * 1024 * 1024

    def __init__(
        self,
        session_dir: Path | str,
        *,
        capacity: int = DEFAULT_CAPACITY,
        body_cap_bytes: int = DEFAULT_BODY_CAP_BYTES,
    ) -> None:
        self._dir = Path(session_dir)
        self._bodies_dir = self._dir / BODIES_DIRNAME
        self._bodies_dir.mkdir(parents=True, exist_ok=True)
        # mkdir's mode is subject to umask; chmod is not, and this directory holds secrets.
        self._dir.chmod(0o700)
        self._bodies_dir.chmod(0o700)

        self._capacity = capacity
        self._body_cap_bytes = body_cap_bytes
        self._retained: dict[int, Exchange] = {}
        self._recorded = 0
        self._evicted = 0
        self._log_path = self._dir / LOG_FILENAME
        self._log = self._log_path.open("a", encoding="utf-8")

    # --- properties -----------------------------------------------------------

    @property
    def dir(self) -> Path:
        """The Session directory. Body paths on an Exchange are relative to it."""
        return self._dir

    @property
    def log_path(self) -> Path:
        return self._log_path

    @property
    def recorded_count(self) -> int:
        """Every Exchange ever recorded in this Session, including evicted ones."""
        return self._recorded

    @property
    def evicted_count(self) -> int:
        """How many Exchanges the ring buffer has dropped. Always reported."""
        return self._evicted

    # --- recording ------------------------------------------------------------

    def record(self, captured: CapturedExchange) -> Exchange:
        """Assign an id, spill both bodies, append to the log, and retain the metadata.

        Ids are monotonic but ordered by *completion*, not by start: an Exchange is
        recorded once its response is in. So a slow request that started first can carry a
        higher id than a fast one that started later. That is the right ordering for
        cursor-based tailing ("what finished since I last looked"), and time-based
        questions use ``t_start`` explicitly rather than leaning on id order.
        """
        self._recorded += 1
        exchange_id = self._recorded

        exchange = Exchange(
            id=exchange_id,
            t_start=captured.t_start,
            t_end=captured.t_end,
            method=captured.method,
            url=captured.url,
            host=captured.host,
            path=captured.path,
            scheme=captured.scheme,
            http_version=captured.http_version,
            status_code=captured.status_code,
            request_headers=list(captured.request_headers),
            response_headers=list(captured.response_headers),
            request_body=self._spill(exchange_id, "request", captured.request_body),
            response_body=self._spill(exchange_id, "response", captured.response_body),
            server_address=captured.server_address,
            error=captured.error,
        )

        self._append_to_log(exchange)
        self._retained[exchange_id] = exchange
        self._evict_overflow()
        return exchange

    def _spill(self, exchange_id: int, side: str, body: bytes) -> BodyRef:
        """Write one side's body to disk, capped, and describe what happened."""
        if not body:
            return BodyRef.empty()

        stored = body[: self._body_cap_bytes]
        relative = f"{BODIES_DIRNAME}/{exchange_id}.{_SUFFIXES[side]}"
        (self._dir / relative).write_bytes(stored)

        return BodyRef(
            size=len(body),
            stored_bytes=len(stored),
            path=relative,
            truncated=len(stored) < len(body),
        )

    def _append_to_log(self, exchange: Exchange) -> None:
        """Append one line and flush, so a tailer sees it without waiting on a buffer."""
        self._log.write(json.dumps(exchange.to_log_dict(), ensure_ascii=False) + "\n")
        self._log.flush()

    def _evict_overflow(self) -> None:
        """Drop the oldest retained Exchanges until the buffer is within capacity."""
        while len(self._retained) > self._capacity:
            del self._retained[next(iter(self._retained))]
            self._evicted += 1

    # --- reading --------------------------------------------------------------

    def get(self, exchange_id: int) -> Exchange | None:
        """The retained Exchange with this id, or ``None`` if unknown or evicted."""
        return self._retained.get(exchange_id)

    def read_body(self, exchange: Exchange, side: str) -> bytes:
        """The spilled body for one side of an Exchange, or no bytes if there was none.

        Takes an Exchange rather than an id so it also works for one rehydrated from the
        log after the ring buffer has evicted it — the file outlives the metadata.
        """
        if side not in _SUFFIXES:
            raise ValueError(f"side must be one of {sorted(_SUFFIXES)}, got {side!r}")

        body = exchange.request_body if side == "request" else exchange.response_body
        if body.path is None:
            return b""
        return (self._dir / body.path).read_bytes()

    def __len__(self) -> int:
        """How many Exchanges are currently retained, not how many were recorded."""
        return len(self._retained)

    def __iter__(self) -> Iterator[Exchange]:
        """Retained Exchanges, oldest first."""
        return iter(list(self._retained.values()))

    # --- lifecycle ------------------------------------------------------------

    def close(self) -> None:
        self._log.close()

    def __enter__(self) -> ExchangeStore:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
