"""Calling tools the way a client does, for tests that drive the surface without a transport.

``FastMCP.call_tool`` hands back ``(content_blocks, structured_result)`` for a tool annotated
to return a dict. Tests want the structured half — asserting against re-parsed JSON text
would be testing the serializer twice and reading worse.

Deliberately not named ``test_*`` so pytest does not collect it.
"""

from __future__ import annotations

import json
from typing import Any


async def call(server: Any, name: str, **arguments: Any) -> Any:
    """Invoke a tool through FastMCP's own dispatch, returning its structured result."""
    result = await server.call_tool(name, arguments)

    if isinstance(result, tuple):
        _content, structured = result
        return structured
    # A tool without a structured return still reports through a text block.
    return json.loads(result[0].text)
