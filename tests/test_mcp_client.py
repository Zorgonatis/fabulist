"""Tests for the minimal MCP client (W3.3).

No ``pytest-asyncio`` in this repo, so async scenarios run via ``asyncio.run``
inside sync tests — matching ``tests/test_graph.py``.

Two fakes are exercised:

* a tiny **stdio server** script (canned initialize/tools list/tools call) spawned
  by :class:`StdioTransport` from a temp ``.py`` file; and
* a local **HTTP server** run in a thread, serving both a JSON body and an
  SSE (``text/event-stream``) body.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fabulist.mcp.client import (
    CLIENT_INFO,
    PROTOCOL_VERSION,
    HttpTransport,
    McpClient,
    McpError,
    StdioTransport,
    image_tool_schema,
)


def run(coro):
    """Run a coroutine in a fresh event loop (repo convention)."""
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Fake stdio server
# --------------------------------------------------------------------------- #

FAKE_SERVER = r'''
"""Canned MCP stdio server: newline-delimited JSON on stdin/stdout."""
import json
import sys

TOOLS = [
    {
        "name": "generate_image",
        "description": "Generate an image from a prompt.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "image": {"type": "string", "format": "binary"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "lookup",
        "description": "Text-only lookup.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
        },
    },
]


def reply(mid, result):
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
    sys.stdout.flush()


def error(mid, code, message):
    sys.stdout.write(
        json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}) + "\n"
    )
    sys.stdout.flush()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    method = msg.get("method")
    mid = msg.get("id")

    if method == "initialize":
        reply(mid, {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0.0.1"},
        })
    elif method == "notifications/initialized":
        continue  # notification: no response
    elif method == "tools/list":
        reply(mid, {"tools": TOOLS})
    elif method == "tools/call":
        params = msg.get("params", {})
        name = params.get("name")
        args = params.get("arguments", {})
        if name == "generate_image":
            reply(mid, {
                "content": [{"type": "text", "text": "made:" + args.get("prompt", "")}],
                "isError": False,
            })
        elif name == "boom":
            error(mid, -32602, "tool exploded")
        else:
            error(mid, -32601, "unknown tool: " + str(name))
    else:
        error(mid, -32601, "method not found: " + str(method))
'''

DEAD_SERVER = "import sys\nsys.exit(0)\n"


def _write_script(tmp_path: Path, source: str, name: str = "server.py") -> Path:
    path = tmp_path / name
    path.write_text(source)
    return path


# --------------------------------------------------------------------------- #
# Stdio transport tests
# --------------------------------------------------------------------------- #


def test_stdio_full_handshake_and_list_tools(tmp_path):
    script = _write_script(tmp_path, FAKE_SERVER)

    async def scenario():
        transport = StdioTransport([sys.executable, str(script)])
        async with McpClient(transport) as client:
            assert client.initialized is True
            assert client.server_info["serverInfo"]["name"] == "fake"
            return await client.list_tools()

    tools = run(scenario())
    assert [t["name"] for t in tools] == ["generate_image", "lookup"]
    assert tools[0]["description"] == "Generate an image from a prompt."
    assert "inputSchema" in tools[0]


def test_stdio_call_tool_roundtrip(tmp_path):
    script = _write_script(tmp_path, FAKE_SERVER)

    async def scenario():
        async with McpClient(StdioTransport([sys.executable, str(script)])) as client:
            return await client.call_tool("generate_image", {"prompt": "a red fox"})

    result = run(scenario())
    assert result == {
        "content": [{"type": "text", "text": "made:a red fox"}],
        "isError": False,
    }


def test_stdio_ids_increment_monotonically(tmp_path):
    script = _write_script(tmp_path, FAKE_SERVER)

    async def scenario():
        async with McpClient(StdioTransport([sys.executable, str(script)])) as client:
            # initialize + initialized consumed id 1; next is 2, 3.
            await client.list_tools()
            await client.list_tools()
            return client._id

    assert run(scenario()) == 3


def test_stdio_error_propagation(tmp_path):
    script = _write_script(tmp_path, FAKE_SERVER)

    async def scenario():
        async with McpClient(StdioTransport([sys.executable, str(script)])) as client:
            await client.call_tool("boom", {})

    try:
        run(scenario())
    except McpError as exc:
        assert exc.code == -32602
        assert "exploded" in str(exc)
    else:
        raise AssertionError("expected McpError")


def test_stdio_transport_death_raises_mcperror(tmp_path):
    script = _write_script(tmp_path, DEAD_SERVER, name="dead.py")

    async def scenario():
        async with McpClient(StdioTransport([sys.executable, str(script)])) as client:
            await client.list_tools()

    try:
        run(scenario())
    except McpError as exc:
        assert "closed" in str(exc) or "died" in str(exc)
    else:
        raise AssertionError("expected McpError on transport death")


def test_stdio_concurrent_calls_are_safe(tmp_path):
    script = _write_script(tmp_path, FAKE_SERVER)

    async def scenario():
        async with McpClient(StdioTransport([sys.executable, str(script)])) as client:
            calls = [
                client.call_tool("generate_image", {"prompt": f"n{i}"})
                for i in range(5)
            ]
            return await asyncio.gather(*calls)

    results = run(scenario())
    texts = sorted(r["content"][0]["text"] for r in results)
    assert texts == [f"made:n{i}" for i in range(5)]


# --------------------------------------------------------------------------- #
# HTTP transport tests
# --------------------------------------------------------------------------- #


def _dispatch(req: dict) -> dict | None:
    """Shared canned JSON-RPC dispatch for the fake HTTP server."""
    method = req.get("method")
    mid = req.get("id")
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "http-fake", "version": "0.0.1"},
            },
        }
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {"tools": [{"name": "ping", "inputSchema": {"type": "object"}}]},
        }
    if method == "tools/call":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {"content": [{"type": "text", "text": "pong"}], "isError": False},
        }
    return {"jsonrpc": "2.0", "id": mid, "result": {}}


class _JsonHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length))
        if req.get("method") == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        payload = json.dumps(_dispatch(req)).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence test output
        pass


class _SseHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802 - stdlib naming
        length = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(length))
        if req.get("method") == "notifications/initialized":
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = ("data: " + json.dumps(_dispatch(req)) + "\n\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _Server:
    """Context manager running a ThreadingHTTPServer in a daemon thread."""

    def __init__(self, handler):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self):
        self._thread.start()
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/mcp"

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def test_http_transport_json_round_trip():
    with _Server(_JsonHandler) as url:

        async def scenario():
            async with McpClient(HttpTransport(url)) as client:
                tools = await client.list_tools()
                result = await client.call_tool("ping", {})
                return client.server_info, tools, result

        server_info, tools, result = run(scenario())
    assert server_info["serverInfo"]["name"] == "http-fake"
    assert [t["name"] for t in tools] == ["ping"]
    assert result["content"][0]["text"] == "pong"


def test_http_transport_sse_round_trip():
    with _Server(_SseHandler) as url:

        async def scenario():
            async with McpClient(HttpTransport(url)) as client:
                return await client.list_tools()

        tools = run(scenario())
    assert [t["name"] for t in tools] == ["ping"]


def test_http_transport_custom_headers_are_sent():
    seen: dict = {}

    class _HeaderHandler(_JsonHandler):
        def do_POST(self):  # noqa: N802
            seen.update(self.headers)
            super().do_POST()

    with _Server(_HeaderHandler) as url:

        async def scenario():
            async with McpClient(
                HttpTransport(url, headers={"Authorization": "Bearer tok"})
            ) as client:
                return await client.list_tools()

        run(scenario())
    assert seen.get("Authorization") == "Bearer tok"


# --------------------------------------------------------------------------- #
# image_tool_schema
# --------------------------------------------------------------------------- #


def test_image_tool_schema_finds_binary_image_param():
    tools = [
        {
            "name": "lookup",
            "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
        {
            "name": "generate_image",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string"},
                    "image": {"type": "string", "format": "binary"},
                },
            },
        },
    ]
    found = image_tool_schema(tools)
    assert found is not None
    assert found["name"] == "generate_image"
    assert "inputSchema" in found


def test_image_tool_schema_matches_image_url_and_media_type():
    tools = [
        {
            "name": "a",
            "inputSchema": {
                "type": "object",
                "properties": {"image_url": {"type": "string"}},
            },
        },
        {
            "name": "b",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "blob": {"type": "string", "contentMediaType": "image/png"}
                },
            },
        },
    ]
    assert image_tool_schema(tools)["name"] == "a"
    assert image_tool_schema(tools[1:])["name"] == "b"


def test_image_tool_schema_returns_none_without_binary_param():
    tools = [
        {
            "name": "lookup",
            "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }
    ]
    assert image_tool_schema(tools) is None
    assert image_tool_schema([]) is None


def test_client_info_matches_spec():
    assert CLIENT_INFO == {"name": "fabulist", "version": "0.1.0"}
    assert PROTOCOL_VERSION == "2025-06-18"
