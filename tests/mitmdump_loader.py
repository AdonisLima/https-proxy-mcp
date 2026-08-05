"""Addon loader for ``mitmdump -s``, used by the P1 end-to-end test.

Production does not need this: the MCP server owns the proxy in-process (ADR-0001) and
hands the addon the ExchangeStore it already holds. This file exists so the acceptance
criterion can be checked the way it is written — a real ``mitmdump`` with the addon
loaded — which means the store has to be constructed from outside the process, hence the
environment variable.

Deliberately not named ``test_*``, so pytest does not try to collect it.
"""

import os
from pathlib import Path

from https_proxy_mcp.capture import CaptureAddon, ExchangeStore

SESSION_DIR_ENV = "HTTPS_PROXY_MCP_SESSION_DIR"

_session_dir = os.environ.get(SESSION_DIR_ENV)

# The test imports this module to get SESSION_DIR_ENV, and that import must not need the
# variable set or create a stray directory. mitmproxy loading it as a script does set it.
addons = [CaptureAddon(ExchangeStore(Path(_session_dir)))] if _session_dir else []
