"""Capture layer: the mitmproxy addon, the ExchangeStore, and body spilling.

Owns the in-memory metadata ring buffer, the on-disk body files, and the
append-only NDJSON log. Knows nothing about MCP or about any Capture Target.
"""

from https_proxy_mcp.capture.addon import CaptureAddon
from https_proxy_mcp.capture.exchange import BodyRef, CapturedExchange, Exchange
from https_proxy_mcp.capture.store import ExchangeStore

__all__ = ["BodyRef", "CaptureAddon", "CapturedExchange", "Exchange", "ExchangeStore"]
