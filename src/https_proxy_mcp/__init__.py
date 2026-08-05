"""An MCP server that captures HTTPS traffic and aligns it with end-to-end test steps.

Domain vocabulary is defined in CONTEXT.md and used precisely throughout this package.
"""

from https_proxy_mcp.server import build_server, main

__all__ = ["build_server", "main"]
