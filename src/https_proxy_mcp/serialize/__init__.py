"""Serialization boundary: redaction, truncation, pagination.

The single place where captured data is shaped for a model. Redaction happens
here rather than at capture time, so the store stays faithful while nothing
sensitive reaches the agent by default.
"""

from https_proxy_mcp.serialize.redact import digest, redact_body, redact_headers

__all__ = ["digest", "redact_body", "redact_headers"]
