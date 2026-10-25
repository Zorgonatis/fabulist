"""Minimal hand-rolled MCP client (W3.3; decisions D7/D8).

Fabulist is an MCP **client only** — it consumes external servers (e.g. an image
generation server) and never constructs one (D7). This module deliberately avoids
the official ``mcp`` SDK (D8): it is a small asyncio JSON-RPC 2.0 client with two
transports, sufficient for the tools surface we consume.

Transports
----------
``StdioTransport``
    Spawns a subprocess and speaks **newline-delimited JSON** — one JSON object per
    line. This is the framing our client implements.
    *Limitation:* the MCP stdio spec also permits ``Content-Length`` framing
    (LSP-style). This client does **not** parse Content-Length headers; a server that
    emits them will be reported as an invalid-JSON transport error. All current MCP
    servers default to line-delimited JSON, so this is an intentional v1 scope cut.

``HttpTransport``
    Streamable HTTP over a single endpoint: POST the JSON-RPC message, read the
    response. Both ``application/json`` bodies and ``text/event-stream`` SSE
    responses are accepted; for SSE the first ``data:`` line carrying a JSON-RPC
    object is returned. *Limitation:* the response body is read after the request
    completes; a server that keeps the SSE stream open indefinitely is not streamed
    incrementally.

The client performs the ``initialize`` handshake (``protocolVersion``
``2025-06-18``), sends ``notifications/initialized``, then exposes ``list_tools``
and ``call_tool``. Request ids increment monotonically and transport writes are
guarded by a lock, so concurrent calls are safe.
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any

__all__ = [
    "CLIENT_INFO",
    "HTTP_TIMEOUT",
    "McpClient",
    "McpError",
    "PROTOCOL_VERSION",
    "HttpTransport",
    "StdioTransport",
    "image_tool_schema",
]

#: MCP protocol revision this client speaks.
PROTOCOL_VERSION = "2025-06-18"

#: Client identity advertised during the handshake.
CLIENT_INFO: dict[str, str] = {"name": "fabulist", "version": "0.1.0"}

#: Default HTTP request timeout (seconds).
HTTP_TIMEOUT = 30.0

# Property names that unambiguously denote a binary/image argument.
_BINARY_PARAM_NAMES = frozenset({"image", "image_url"})


class McpError(Exception):
    """Raised on a JSON-RPC error response or transport death.

    Attributes:
        code: the JSON-RPC error code, when the server supplied one.
        data: the JSON-RPC error ``data`` member, when present.
    """

    def __init__(self, message: str, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class StdioTransport:
    """Newline-delimited JSON over a spawned subprocess' stdio.

    One JSON object per line on stdin/stdout. A background reader task routes
    responses to waiting callers by ``id``; server-initiated notifications are
    ignored (v1 consumes only request/response tools).
    """

    def __init__(self, cmd: list[str]) -> None:
        self.cmd = list(cmd)
        self._proc: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[Any, asyncio.Future[dict]] = {}
        self._write_lock = asyncio.Lock()

    async def start(self) -> None:
        """Spawn the subprocess and begin reading its stdout."""
        self._proc = await asyncio.create_subprocess_exec(
            *self.cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader_task = asyncio.create_task(self._reader())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        """Consume stderr so a chatty server cannot dead-lock on a full pipe."""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            line = await proc.stderr.readline()
            if not line:
                return

    async def _reader(self) -> None:
        """Route response messages to their pending futures by id."""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        while True:
            line = await proc.stdout.readline()
            if not line:
                self._fail_pending(McpError("stdio transport closed"))
                return
            try:
                message = json.loads(line.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                self._fail_pending(McpError(f"invalid JSON from server: {exc}"))
                return
            mid = message.get("id")
            future = self._pending.pop(mid, None)
            if future is not None and not future.done():
                future.set_result(message)

    def _fail_pending(self, exc: McpError) -> None:
        """Fail every in-flight request; used when the stream dies."""
        for future in self._pending.values():
            if not future.done():
                future.set_exception(exc)
        self._pending.clear()

    async def send(self, message: dict[str, Any]) -> None:
        """Write one newline-delimited JSON message (locked write)."""
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise McpError("stdio transport not started")
        data = (json.dumps(message) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                proc.stdin.write(data)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise McpError(f"stdio transport died: {exc}") from exc

    async def request(self, message: dict[str, Any]) -> dict[str, Any]:
        """Send a request and await its matching response."""
        mid = message.get("id")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict] = loop.create_future()
        self._pending[mid] = future
        try:
            await self.send(message)
        except McpError:
            self._pending.pop(mid, None)
            raise
        return await future

    async def close(self) -> None:
        """Terminate the subprocess and cancel the reader tasks."""
        proc = self._proc
        if self._reader_task is not None:
            self._reader_task.cancel()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
                    pass
        self._fail_pending(McpError("stdio transport closed"))
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        self._proc = None


class HttpTransport:
    """Streamable-HTTP transport over a single JSON-RPC endpoint.

    POSTs each message and reads the response. Accepts ``application/json``
    bodies and ``text/event-stream`` SSE bodies (returns the first JSON-RPC
    ``data:`` payload). The request runs in a worker thread so it never blocks
    the event loop.
    """

    def __init__(self, url: str, headers: dict[str, str] | None = None) -> None:
        self.url = url
        self.headers = dict(headers or {})
        self._write_lock = asyncio.Lock()

    def _base_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        headers.update(self.headers)
        return headers

    def _post(self, message: dict[str, Any], *, expect_response: bool) -> dict[str, Any] | None:
        """Blocking POST executed via ``asyncio.to_thread``."""
        body = json.dumps(message).encode("utf-8")
        request = urllib.request.Request(
            self.url, data=body, headers=self._base_headers(), method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                content_type = response.headers.get_content_type()
                if not expect_response:
                    response.read()
                    return None
                if content_type == "text/event-stream":
                    return self._read_sse(response)
                raw = response.read()
                if not raw:
                    raise McpError("empty HTTP response body")
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001 - best-effort detail
                detail = str(exc)
            raise McpError(f"HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise McpError(f"HTTP transport died: {exc}") from exc

    @staticmethod
    def _read_sse(response: Any) -> dict[str, Any]:
        """Return the first JSON-RPC object carried by an SSE ``data:`` line."""
        for raw_line in response:
            line = raw_line.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                return json.loads(payload)
            except json.JSONDecodeError:
                continue
        raise McpError("SSE response contained no JSON-RPC data event")

    async def start(self) -> None:
        """No-op; the HTTP transport is connectionless."""
        return None

    async def send(self, message: dict[str, Any]) -> None:
        """POST a notification, ignoring the response body."""
        async with self._write_lock:
            await asyncio.to_thread(self._post, message, expect_response=False)

    async def request(self, message: dict[str, Any]) -> dict[str, Any]:
        """POST a request and return the parsed JSON-RPC response."""
        async with self._write_lock:
            result = await asyncio.to_thread(self._post, message, expect_response=True)
        assert result is not None
        return result

    async def close(self) -> None:
        """No-op; nothing is held open."""
        return None


class McpClient:
    """Async context-managed MCP client over any transport.

    Usage::

        async with McpClient(StdioTransport(["my-server"])) as client:
            tools = await client.list_tools()
            result = await client.call_tool("generate_image", {"prompt": "a cat"})
    """

    def __init__(self, transport: StdioTransport | HttpTransport) -> None:
        self.transport = transport
        self._id = 0
        self._id_lock = asyncio.Lock()
        self.initialized = False
        self.server_info: dict[str, Any] = {}

    async def __aenter__(self) -> "McpClient":
        await self.transport.start()
        await self._handshake()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.transport.close()

    async def _next_id(self) -> int:
        """Allocate the next monotonically increasing request id."""
        async with self._id_lock:
            self._id += 1
            return self._id

    async def _rpc(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Issue a request, raising :class:`McpError` on a JSON-RPC error."""
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        message["id"] = await self._next_id()
        response = await self.transport.request(message)
        error = response.get("error")
        if error is not None:
            raise McpError(
                str(error.get("message", "MCP error")),
                code=error.get("code"),
                data=error.get("data"),
            )
        result = response.get("result")
        return result if isinstance(result, dict) else {}

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no id, no response awaited)."""
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self.transport.send(message)

    async def _handshake(self) -> None:
        """Run ``initialize`` then ``notifications/initialized``."""
        result = await self._rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "clientInfo": CLIENT_INFO,
                "capabilities": {},
            },
        )
        self.server_info = result
        await self._notify("notifications/initialized")
        self.initialized = True

    async def list_tools(self) -> list[dict[str, Any]]:
        """Return the server's tools (``name``, ``description``, ``inputSchema``)."""
        result = await self._rpc("tools/list", {})
        tools = result.get("tools")
        return list(tools) if isinstance(tools, list) else []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Call a tool, returning the raw ``{content, isError}`` result.

        Raises :class:`McpError` on a JSON-RPC error or transport death.
        """
        result = await self._rpc(
            "tools/call", {"name": name, "arguments": arguments or {}}
        )
        return result


def _declares_binary_image(param_name: str, schema: dict[str, Any]) -> bool:
    """True if a JSON Schema property is an obvious binary/image argument."""
    if param_name in _BINARY_PARAM_NAMES:
        return True
    if schema.get("format") == "binary":
        return True
    if schema.get("contentEncoding") == "binary":
        return True
    media_type = schema.get("contentMediaType")
    return isinstance(media_type, str) and media_type.startswith("image/")


def image_tool_schema(tools: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Find the tool whose ``inputSchema`` declares an image/binary parameter.

    Returns ``{"name", "inputSchema"}`` for the first matching tool — the mapping
    the narrator's image directive needs — or ``None`` when no tool qualifies.
    A parameter qualifies if it is named ``image``/``image_url``, or its schema
    hints at binary data (``format: "binary"``, ``contentEncoding: "binary"``,
    or an ``image/*`` ``contentMediaType``).
    """
    for tool in tools:
        schema = tool.get("inputSchema")
        if not isinstance(schema, dict):
            continue
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            continue
        for param_name, param_schema in properties.items():
            if isinstance(param_schema, dict) and _declares_binary_image(
                param_name, param_schema
            ):
                return {"name": tool.get("name"), "inputSchema": schema}
    return None
