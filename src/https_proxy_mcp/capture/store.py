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
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType

from https_proxy_mcp.capture.exchange import (
    BodyRef,
    CapturedExchange,
    Exchange,
    PendingExchange,
)

#: Bodies land under this subdirectory of the Session directory, named by Exchange id.
BODIES_DIRNAME = "bodies"

#: The append-only metadata log.
LOG_FILENAME = "exchanges.ndjson"

#: Suffix per side of an Exchange.
_SUFFIXES = {"request": "req", "response": "resp"}

#: How many handshake failures are kept. Enough to tell "one host is pinned" from "nothing
#: trusts the authority" without letting a pinned app that retries forever grow without bound.
TLS_FAILURE_CAP = 50

#: How many in-flight requests are tracked at once. Genuinely-open connections are bounded by the
#: OS in practice, but a server that accepts and never answers — polled in a loop — would grow
#: without one. Same reasoning as the handshake cap above.
PENDING_CAP = 500


@dataclass(frozen=True, slots=True)
class TlsFailure:
    """A TLS handshake that died before there was ever a request to record.

    Kept separately from Exchanges because it is not one: no request arrived, so recording it
    as an Exchange would invent traffic. But it is the difference between "the app is pinned"
    and "the app is silent", which is the distinction ``diagnose_capture`` exists to draw.
    """

    t: float
    #: The requested host, from SNI. ``None`` when the client connected by IP and sent none.
    host: str | None
    reason: str


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
        self._tls_failures: list[TlsFailure] = []
        self._tls_failure_count = 0
        # Insertion-ordered, so "oldest first" needs no sort and eviction is the first key.
        self._pending: dict[str, PendingExchange] = {}
        self._pending_dropped = 0
        self._closed = False
        self._dropped_after_close = 0
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
    def tls_failure_count(self) -> int:
        """Every handshake failure seen, including any beyond what is still retained."""
        return self._tls_failure_count

    @property
    def tls_failures(self) -> list[TlsFailure]:
        """The most recent handshake failures, oldest first."""
        return list(self._tls_failures)

    @property
    def dropped_after_close(self) -> int:
        """Hooks that arrived after the Session ended, one per connection still in flight.

        Counted rather than silently discarded: "capture stopped while three connections were
        still open" is worth being able to see, and a silent drop is indistinguishable from
        something never having happened.
        """
        return self._dropped_after_close

    def record_tls_failure(self, host: str | None, reason: str) -> None:
        """Note a handshake that never became an Exchange.

        Capped rather than unbounded: a pinned app typically retries in a tight loop, and the
        hundredth identical failure carries no information the first did not.
        """
        if self._closed:
            self._dropped_after_close += 1
            return

        self._tls_failure_count += 1
        self._tls_failures.append(TlsFailure(t=time.time(), host=host, reason=reason))
        if len(self._tls_failures) > TLS_FAILURE_CAP:
            del self._tls_failures[0]

    @property
    def pending(self) -> tuple[PendingExchange, ...]:
        """Requests read in full that have no response yet, oldest first.

        Deliberately not part of :meth:`__iter__`. Every listing tool iterates the store, so
        keeping these out of that iteration is what leaves ``list_exchanges``,
        ``tail_exchanges``, ``search_exchanges`` and ``wait_for_exchanges`` untouched by P4.
        """
        return tuple(self._pending.values())

    @property
    def pending_dropped(self) -> int:
        """Open requests the cap discarded. Counted, because a silent drop would make a Timeline
        claim nothing was outstanding when something was."""
        return self._pending_dropped

    @property
    def recorded_count(self) -> int:
        """Every Exchange ever recorded in this Session, including evicted ones."""
        return self._recorded

    # --- in-flight requests ---------------------------------------------------

    def open(self, request: PendingExchange) -> None:
        """Note a request that has been read in full but has no response yet.

        Consumes **no** Exchange id. Ids are assigned on completion so that cursor-based tailing
        means "what finished since I last looked"; spending one here would invert that ordering
        and make ``wait_for_exchanges`` return before a status code existed.
        """
        if self._closed:
            self._dropped_after_close += 1
            return

        self._pending[request.key] = request
        while len(self._pending) > PENDING_CAP:
            del self._pending[next(iter(self._pending))]
            self._pending_dropped += 1

    def _complete(self, key: str) -> None:
        """Forget an open request, whether it succeeded or failed.

        A key this store never saw is normal rather than exceptional: anything already in flight
        when capture started delivers a response hook without a matching request hook.
        """
        self._pending.pop(key, None)

    @property
    def evicted_count(self) -> int:
        """How many Exchanges the ring buffer has dropped. Always reported."""
        return self._evicted

    # --- recording ------------------------------------------------------------

    def record(
        self, captured: CapturedExchange, *, pending_key: str | None = None
    ) -> Exchange | None:
        """Assign an id, spill both bodies, append to the log, and retain the metadata.

        ``pending_key`` completes the matching in-flight record, if the caller opened one. It is
        optional because most callers — every test, and anything replaying a log — have no such
        record, and a required argument would let one of them empty the registry by accident.

        Ids are monotonic but ordered by *completion*, not by start: an Exchange is
        recorded once its response is in. So a slow request that started first can carry a
        higher id than a fast one that started later. That is the right ordering for
        cursor-based tailing ("what finished since I last looked"), and time-based
        questions use ``t_start`` explicitly rather than leaning on id order.

        Returns ``None`` for anything arriving after :meth:`close`. That is a normal part of
        shutdown rather than a programming error: connections still in flight when the Session
        ends deliver their hook afterwards, and mitmproxy turns an exception raised from an addon
        into "Addon error: I/O operation on closed file" — one line per open connection, looking
        exactly like a defect. Found during the P3 live run.
        """
        if self._closed:
            # Left pending on purpose. The Exchange is not being recorded, so clearing the open
            # record too would lose the request entirely — and "it was still open when the
            # Session ended" is very nearly true and far more useful than silence.
            self._dropped_after_close += 1
            return None

        if pending_key is not None:
            self._complete(pending_key)

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
        """Stop recording and release the log. Safe to call more than once.

        Idempotent because ``CaptureState.stop`` runs on failure paths as well as on the happy
        one, so it can reach a store that is already closed.
        """
        if self._closed:
            return
        self._closed = True
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
