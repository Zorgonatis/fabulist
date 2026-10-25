"""Minimal MCP client package (W3.3).

Fabulist is an MCP *client only* (D7); this package speaks to external servers
over stdio or streamable HTTP. See :mod:`fabulist.mcp.client` for the hand-rolled
JSON-RPC 2.0 implementation and its documented framing limitations.
"""

from __future__ import annotations

from .client import (
    CLIENT_INFO,
    HTTP_TIMEOUT,
    PROTOCOL_VERSION,
    HttpTransport,
    McpClient,
    McpError,
    StdioTransport,
    image_tool_schema,
)

__all__ = [
    "CLIENT_INFO",
    "HTTP_TIMEOUT",
    "PROTOCOL_VERSION",
    "HttpTransport",
    "McpClient",
    "McpError",
    "StdioTransport",
    "image_tool_schema",
]
