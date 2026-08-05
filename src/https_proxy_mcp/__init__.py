"""An MCP server that captures HTTPS traffic and aligns it with end-to-end test steps.

Domain vocabulary is defined in CONTEXT.md and used precisely throughout this package.
"""

from https_proxy_mcp.server import build_server, main
from https_proxy_mcp.version import resolve_version

#: Resolved at import from installed metadata; see :mod:`https_proxy_mcp.version`.
__version__ = resolve_version()

__all__ = ["__version__", "build_server", "main"]
