"""The Exchange — one request together with its response, its timing, and its connection.

Two shapes, because the boundary between observing and persisting is worth making explicit:
:class:`CapturedExchange` is what the addon saw, carrying body bytes in memory and no id;
:class:`Exchange` is what the store kept, carrying an id and :class:`BodyRef` pointers to
bodies that now live on disk. The addon never touches the filesystem and the store never
touches mitmproxy.

Both are flat rather than nested. Every consumer downstream — the tool surface, the
correlator — reads these fields constantly, and ``exchange.host`` beating
``exchange.meta.host`` at hundreds of call sites is worth one duplicated field list here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class BodyRef:
    """Where one side's body went, and how faithful what landed there is."""

    #: True size as observed, before any cap was applied.
    size: int
    #: How many bytes actually reached disk.
    stored_bytes: int
    #: Path relative to the Session directory, so a Session stays movable.
    #: ``None`` when there was no body at all, in which case no file was created.
    path: str | None
    #: Whether the cap bit, meaning ``stored_bytes < size``.
    truncated: bool

    @classmethod
    def empty(cls) -> BodyRef:
        """The reference for a side that carried no body."""
        return cls(size=0, stored_bytes=0, path=None, truncated=False)

    def to_log_dict(self) -> dict[str, Any]:
        return {
            "size": self.size,
            "stored_bytes": self.stored_bytes,
            "path": self.path,
            "truncated": self.truncated,
        }

    @classmethod
    def from_log_dict(cls, data: dict[str, Any]) -> BodyRef:
        return cls(
            size=data["size"],
            stored_bytes=data["stored_bytes"],
            path=data["path"],
            truncated=data["truncated"],
        )


@dataclass(frozen=True, slots=True)
class PendingExchange:
    """A request that has been read in full but has no response yet — still in flight.

    Deliberately **not** an :class:`Exchange`. An Exchange carries an id, and ids are assigned on
    completion so that cursor-based tailing means "what finished since I last looked". Handing an
    id out here would invert that ordering and would make ``wait_for_exchanges`` return before a
    status code existed, so *"tap Login, wait for POST /auth, check it returned 401"* — the thing
    that makes end-to-end use practical — would stop working.

    So this is a separate, parallel record. Nothing that lists Exchanges sees it; only a Timeline
    does, where "the assertion began while this request was still outstanding" is the whole point.

    Carries no bodies. The request body has been read, but a pending record exists to answer a
    *timing* question, and keeping bodies here would duplicate the spill the store already does
    on completion.
    """

    #: The addon's handle on the open request, so completion can find and clear it. Opaque to
    #: everything but the addon — in practice mitmproxy's ``flow.id``.
    key: str
    #: Request start, epoch seconds. There is no end; that is what pending means.
    t_start: float
    method: str
    url: str
    host: str
    path: str
    scheme: str

    @property
    def label(self) -> str:
        """How a Timeline names it, given there is no id to name it by."""
        return f"{self.method} {self.host}{self.path}"


@dataclass(frozen=True, slots=True)
class CapturedExchange:
    """What the addon observed, before the store assigned an id or spilled the bodies.

    Timestamps come from the host clock, which is the same clock the test tool runs on —
    device time never enters the join (ADR-0003), so Steps and Exchanges are directly
    comparable as intervals.
    """

    #: Request start, epoch seconds.
    t_start: float
    #: Response end, epoch seconds. ``None`` when the Exchange never completed.
    t_end: float | None
    method: str
    url: str
    host: str
    path: str
    scheme: str
    http_version: str
    #: ``None`` when no response arrived.
    status_code: int | None
    request_headers: list[tuple[str, str]] = field(default_factory=list)
    response_headers: list[tuple[str, str]] = field(default_factory=list)
    request_body: bytes = b""
    response_body: bytes = b""
    server_address: str | None = None
    #: Why the Exchange failed, when it did.
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Exchange:
    """One recorded Exchange: metadata in memory, bodies referenced on disk.

    Headers keep their order and their repeats — ``Set-Cookie`` legitimately appears more
    than once — so they are a list of pairs rather than a mapping.
    """

    #: Monotonic within a Session, assigned in completion order. See ExchangeStore.
    id: int
    t_start: float
    t_end: float | None
    method: str
    url: str
    host: str
    path: str
    scheme: str
    http_version: str
    status_code: int | None
    request_headers: list[tuple[str, str]] = field(default_factory=list)
    response_headers: list[tuple[str, str]] = field(default_factory=list)
    request_body: BodyRef = field(default_factory=BodyRef.empty)
    response_body: BodyRef = field(default_factory=BodyRef.empty)
    server_address: str | None = None
    error: str | None = None

    @property
    def duration_ms(self) -> float | None:
        """Wall-clock duration, or ``None`` for an Exchange that never completed."""
        if self.t_end is None:
            return None
        return (self.t_end - self.t_start) * 1000

    def to_log_dict(self) -> dict[str, Any]:
        """Render for ``exchanges.ndjson``.

        Written out field by field rather than reflected off the dataclass: the log is a
        persisted format, and adding a field to the class should not silently change it.
        """
        return {
            "id": self.id,
            "t_start": self.t_start,
            "t_end": self.t_end,
            "method": self.method,
            "url": self.url,
            "host": self.host,
            "path": self.path,
            "scheme": self.scheme,
            "http_version": self.http_version,
            "status_code": self.status_code,
            "request_headers": [list(header) for header in self.request_headers],
            "response_headers": [list(header) for header in self.response_headers],
            "request_body": self.request_body.to_log_dict(),
            "response_body": self.response_body.to_log_dict(),
            "server_address": self.server_address,
            "error": self.error,
        }

    @classmethod
    def from_log_dict(cls, data: dict[str, Any]) -> Exchange:
        """Rebuild from a log line. JSON has no tuples, so header pairs are restored."""
        return cls(
            id=data["id"],
            t_start=data["t_start"],
            t_end=data["t_end"],
            method=data["method"],
            url=data["url"],
            host=data["host"],
            path=data["path"],
            scheme=data["scheme"],
            http_version=data["http_version"],
            status_code=data["status_code"],
            request_headers=[(name, value) for name, value in data["request_headers"]],
            response_headers=[(name, value) for name, value in data["response_headers"]],
            request_body=BodyRef.from_log_dict(data["request_body"]),
            response_body=BodyRef.from_log_dict(data["response_body"]),
            server_address=data["server_address"],
            error=data["error"],
        )
