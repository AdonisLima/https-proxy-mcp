"""Capture layer: the mitmproxy addon, the ExchangeStore, and body spilling.

Owns the in-memory metadata ring buffer, the on-disk body files, and the
append-only NDJSON log. Knows nothing about MCP or about any Capture Target.
"""
