"""Serialization boundary: redaction, truncation, pagination.

The single place where captured data is shaped for a model. Redaction happens
here rather than at capture time, so the store stays faithful while nothing
sensitive reaches the agent by default.
"""

from https_proxy_mcp.serialize.redact import digest, redact_body, redact_headers
from https_proxy_mcp.serialize.views import (
    body_preview,
    body_slice,
    curl_command,
    exchange_detail,
    exchange_summary,
    paginate,
)

__all__ = [
    "body_preview",
    "body_slice",
    "curl_command",
    "digest",
    "exchange_detail",
    "exchange_summary",
    "paginate",
    "redact_body",
    "redact_headers",
]
