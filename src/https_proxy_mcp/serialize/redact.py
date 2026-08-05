"""Redaction of secrets, with stable digests so repeated secrets stay comparable.

Redaction happens here rather than at capture time. The Session directory keeps faithful
bodies on local disk, because "is my app sending the right token?" is a real debugging
question that a scrubbed store cannot answer; this module is the boundary crossed on the
way *to a model* or *out of the machine* (SPEC.md, "Redaction happens where data leaves").

Every redacted value becomes ``<redacted:sha256:xxxxxxxx>`` — a short digest of the value
itself. That is what lets an agent conclude "same token as the previous request" or "the
token changed after refresh" without the value ever being visible, which is usually the
whole question. The digest is unsalted precisely so it is stable across Sessions and runs.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import unquote_plus

#: Header names whose values are secrets. Compared case-insensitively (SPEC.md).
REDACTED_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "x-api-key",
        "x-auth-token",
    }
)

#: JSON keys whose values are secrets. Matched as a substring, so ``refreshToken`` and
#: ``clientSecret`` are caught alongside the bare forms.
SECRET_KEY_PATTERN = re.compile(
    r"token|secret|password|api[-_]?key|refresh|access_token|id_token",
    re.IGNORECASE,
)

#: JWT-shaped values, wherever they appear and whatever key they sit under.
#: Segmentation is required: redacting every base64 blob starting with ``eyJ`` would gut
#: legitimate payloads, and "JWT-shaped" means dotted segments.
JWT_PATTERN = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}(?:\.[A-Za-z0-9_-]+)?")

FORM_MEDIA_TYPE = "application/x-www-form-urlencoded"

#: A body that is unambiguously form-encoded: ``key=value`` pairs joined by ``&``, where
#: every key is made only of characters that survive form encoding. That last constraint is
#: what keeps prose out — "the password was = wrong" has spaces before its equals sign, so
#: it cannot match, while ``password=lovelace`` can.
FORM_SHAPE = re.compile(r"[A-Za-z0-9%._~*+-]+=[^&]*(?:&[A-Za-z0-9%._~*+-]+=[^&]*)*")


def digest(value: str) -> str:
    """Return the stable redaction marker for ``value``."""
    return f"<redacted:sha256:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:8]}>"


def redact_headers(
    headers: Iterable[tuple[str, str]], *, include_secrets: bool = False
) -> list[tuple[str, str]]:
    """Redact sensitive header values, preserving names, order and repeats.

    Header names are kept as sent: the name is not the secret, and seeing *which* header
    carried a token is diagnostically useful. Repeats matter — ``Set-Cookie`` legitimately
    appears more than once and each occurrence is redacted on its own.
    """
    if include_secrets:
        return list(headers)
    return [
        (name, digest(value) if name.lower() in REDACTED_HEADERS else value)
        for name, value in headers
    ]


def redact_body(
    body: bytes, *, content_type: str | None = None, include_secrets: bool = False
) -> bytes:
    """Redact secrets in a body, returning bytes so the caller owns representation.

    JSON is walked structurally and form-encoded bodies pair by pair, so a redacted body
    keeps the shape it arrived in. Anything else that decodes as text is scanned for
    JWT-shaped values. Bodies that are not valid UTF-8 are returned untouched: there is
    nothing text-shaped to redact, and decoding them would corrupt the bytes.

    ``content_type`` is authoritative when given, and a form-shaped body is redacted as a
    form even without it. That belt-and-braces is deliberate: a caller who forgets to pass
    the header would otherwise leak, which is the same failure this exists to prevent.

    Not covered: ``multipart/form-data``. Its parts carry their own headers and boundary
    framing, and nothing in v1 needs it yet.
    """
    if include_secrets or not body:
        return body

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body

    if _is_form_content_type(content_type):
        return _redact_form(text).encode("utf-8")

    try:
        parsed = json.loads(text)
    except ValueError:
        pass
    else:
        return json.dumps(_redact_json(parsed), ensure_ascii=False).encode("utf-8")

    if FORM_SHAPE.fullmatch(text):
        return _redact_form(text).encode("utf-8")

    return _redact_jwts(text).encode("utf-8")


def _is_form_content_type(content_type: str | None) -> bool:
    """Whether a Content-Type names form encoding, ignoring parameters and case."""
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower() == FORM_MEDIA_TYPE


def _redact_form(text: str) -> str:
    """Redact secret-shaped fields of a form body, leaving every other byte alone.

    Rebuilt from the original substrings rather than round-tripped through ``parse_qsl``
    and ``urlencode``, which would re-encode fields nobody asked to touch. Keys and values
    are decoded only to decide and to digest, so ``api%5Fkey`` still matches and the digest
    of a percent-encoded secret matches the same secret seen in a header.
    """
    redacted = []
    for pair in text.split("&"):
        key, separator, value = pair.partition("=")
        if separator and SECRET_KEY_PATTERN.search(unquote_plus(key)):
            redacted.append(f"{key}{separator}{digest(unquote_plus(value))}")
        else:
            redacted.append(_redact_jwts(pair))
    return "&".join(redacted)


def _redact_json(value: Any) -> Any:
    """Walk parsed JSON, redacting values under secret-shaped keys and any JWT found."""
    if isinstance(value, dict):
        return {
            key: _redact_secret(item) if SECRET_KEY_PATTERN.search(key) else _redact_json(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        return _redact_jwts(value)
    return value


def _redact_secret(value: Any) -> str:
    """Redact a value found under a secret-shaped key, whatever its type.

    A string is digested directly so the marker matches the same secret seen in a header.
    Anything structured is digested via its JSON form, so a secret nested under a matching
    key cannot escape by not being a string.
    """
    if isinstance(value, str):
        return digest(value)
    return digest(json.dumps(value, separators=(",", ":"), sort_keys=True))


def _redact_jwts(text: str) -> str:
    """Replace every JWT-shaped substring in place, leaving the surrounding text intact."""
    return JWT_PATTERN.sub(lambda match: digest(match.group(0)), text)
