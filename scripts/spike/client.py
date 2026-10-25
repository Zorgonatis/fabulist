"""HTTP + SSE transport for the RP spike harness (the "flight recorder" side).

:class:`SpikeClient` wraps ``httpx`` and speaks the live Fabulist engine API at
``http://localhost:2727``. It owns exactly four concerns the run loop needs:

1. **Session seeding** — ``POST /api/cards/import`` as ``multipart/form-data``
   with the card file and the seed flag; the response carries ``session_id`` +
   ``title`` (see :mod:`fabulist.api.cards_api`; no personas needed).
2. **Per-session debug capture** — ``PATCH /api/sessions/{id}/agent-debug`` set
   ON; the analysis artifacts depend on the ``agent_call`` captures this gates.
3. **Turns** — ``POST /api/sessions/{id}/turns`` with ``{content, kind}``; the
   response is consumed to completion as an SSE stream (``reasoning`` /
   ``content`` with ``meta.channel`` prose|dsl / ``done`` / ``error``).
4. **Readbacks** — ``GET /events`` (projection), ``GET /inspect/turns`` (per-turn
   usage), ``GET /agent-calls`` (raw capture telemetry).

All requests carry the Vite dev-server ``Origin`` header so the traffic mirrors
the real browser path (the API's CORS middleware allows it). An ``httpx.Client``
may be injected — tests pass a ``MockTransport``-backed client and never touch
the network.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Iterator

import httpx

__all__ = [
    "DEFAULT_BASE_URL",
    "ORIGIN",
    "SeedResult",
    "SpikeClient",
    "SpikeClientError",
    "SpikeTurnError",
    "TurnResult",
    "parse_sse_lines",
]

#: Live engine API base (do NOT start/restart it — the harness only reads/writes turns).
DEFAULT_BASE_URL = "http://localhost:2727"

#: The Vite dev-server origin the API's CORS middleware allows (app.py ``_DEV_ORIGINS``).
ORIGIN = "http://localhost:5173"

#: Media-type guess for the card upload (PNG vs JSON card).
_PNG_SUFFIXES = frozenset({".png"})


class SpikeClientError(RuntimeError):
    """A non-turn HTTP call failed (bad status, transport error, or an error body)."""


class SpikeTurnError(RuntimeError):
    """A turn stream ended with an ``error`` event or without a ``done`` event."""

    def __init__(self, message: str, *, meta: dict | None = None) -> None:
        super().__init__(message)
        self.meta = meta or {}


@dataclass
class SeedResult:
    """``POST /api/cards/import?seed=1`` outcome."""

    session_id: str
    title: str


@dataclass
class TurnResult:
    """Everything one streamed turn produced, split by channel."""

    content: str = ""
    dsl: str = ""
    reasoning: str = ""
    done_meta: dict = field(default_factory=dict)
    error: dict | None = None
    events: list[dict] = field(default_factory=list)

    def append(self, event: dict) -> None:
        """Fold one SSE ``StreamEvent`` into the result."""
        self.events.append(event)
        etype = event.get("type")
        delta = event.get("delta", "")
        if etype == "reasoning":
            self.reasoning += delta
        elif etype == "content":
            channel = (event.get("meta") or {}).get("channel", "prose")
            if channel == "dsl":
                self.dsl += delta
            else:
                self.content += delta
        elif etype == "done":
            self.done_meta = event.get("meta") or {}
        elif etype == "error":
            self.error = event


def parse_sse_lines(lines: Iterable[str]) -> Iterator[tuple[str | None, dict]]:
    """Turn raw SSE lines into ``(event_name, data)`` pairs.

    Implements the WHATWG subset the engine emits: ``event:`` / ``data:`` /
    ``id:`` fields terminated by a blank line, ``:`` comment heartbeats ignored,
    and multi-line ``data:`` joined with ``\\n`` before JSON-decoding. A data
    payload that is not valid JSON is surfaced as ``{"raw": <text>}`` so a
    malformed frame never aborts the read.
    """
    event_name: str | None = None
    data_lines: list[str] = []
    for raw in lines:
        line = raw.rstrip("\r")
        if line == "":
            if data_lines:
                text = "\n".join(data_lines)
                try:
                    data = json.loads(text)
                except json.JSONDecodeError:
                    data = {"raw": text}
                yield event_name, data if isinstance(data, dict) else {"value": data}
            event_name = None
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
        # ``id`` and unknown fields are ignored (resume is not needed here).
    if data_lines:  # stream closed without a trailing blank line
        text = "\n".join(data_lines)
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {"raw": text}
        yield event_name, data if isinstance(data, dict) else {"value": data}


class SpikeClient:
    """Thin, synchronous client for the engine's session/turn API."""

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        card_path: str | Path | None = None,
        client: httpx.Client | None = None,
        timeout: float = 600.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.card_path = Path(card_path) if card_path is not None else None
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client(timeout=timeout)
        self._headers = {"Origin": ORIGIN, "Accept": "application/json"}

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "SpikeClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @staticmethod
    def _fail(resp: httpx.Response) -> None:
        raise SpikeClientError(
            f"HTTP {resp.status_code} for {resp.request.method} "
            f"{resp.request.url}: {resp.text[:400]}"
        )

    # -- seeding -----------------------------------------------------------

    def seed_session(self, card_path: str | Path | None = None) -> SeedResult:
        """Import a card and seed a NEW session from it.

        Posts ``multipart/form-data`` with the card file under the ``file``
        field and ``seed=true``; ``cards_api`` returns ``session_id`` + ``title``.
        """
        path = Path(card_path) if card_path is not None else self.card_path
        if path is None:
            raise SpikeClientError("seed_session: no card path configured")
        path = Path(path)
        if not path.is_file():
            raise SpikeClientError(f"seed_session: card file not found: {path}")
        mime = "image/png" if path.suffix.lower() in _PNG_SUFFIXES else "application/json"
        try:
            resp = self._client.post(
                self._url("/api/cards/import"),
                files={"file": (path.name, path.read_bytes(), mime)},
                data={"seed": "true"},
                headers=self._headers,
            )
        except httpx.HTTPError as exc:
            raise SpikeClientError(f"seed_session transport error: {exc}") from exc
        if resp.status_code >= 400:
            self._fail(resp)
        body = self._json(resp)
        if not body.get("ok"):
            raise SpikeClientError(
                f"seed_session rejected: {body.get('error') or body}"
            )
        session_id = body.get("session_id")
        if not session_id:
            raise SpikeClientError(
                f"seed_session: response missing session_id: {body}"
            )
        return SeedResult(session_id=str(session_id), title=str(body.get("title") or ""))

    def rename_session(self, session_id: str, title: str) -> dict:
        """Set the session title shown in the frontend (append-only revision)."""
        return self._patch(
            f"/api/sessions/{session_id}", json_body={"title": title}
        )

    def set_agent_debug(self, session_id: str, enabled: bool = True) -> dict:
        """Toggle per-session agent-call capture (the analysis depends on it)."""
        return self._patch(
            f"/api/sessions/{session_id}/agent-debug",
            json_body={"enabled": bool(enabled)},
        )

    def post_worldinfo(self, session_id: str, book: list[dict]) -> dict:
        """POST world-info entries (ST-shaped list) onto the session pre-run."""
        return self._post(
            f"/api/sessions/{session_id}/worldinfo", json_body={"book": book}
        )

    # -- turn streaming ----------------------------------------------------

    def stream_turn(
        self,
        session_id: str,
        content: str,
        kind: str = "in_character",
        *,
        on_event: Callable[[dict], None] | None = None,
        timeout: float | None = None,
    ) -> TurnResult:
        """POST one turn and consume its SSE stream to completion.

        ``on_event`` (optional) is invoked with every parsed ``StreamEvent`` in
        arrival order — the run loop uses it for live terminal output. Raises
        :class:`SpikeTurnError` when the stream carries an ``error`` event or
        ends without ``done``.
        """
        body = {"content": content, "kind": kind}
        result = TurnResult()
        headers = {**self._headers, "Accept": "text/event-stream"}
        try:
            with self._client.stream(
                "POST",
                self._url(f"/api/sessions/{session_id}/turns"),
                json=body,
                headers=headers,
                timeout=timeout if timeout is not None else self._client.timeout,
            ) as resp:
                if resp.status_code >= 400:
                    resp.read()
                    self._fail(resp)
                for event_name, data in parse_sse_lines(resp.iter_lines()):
                    event = self._normalize_event(event_name, data)
                    result.append(event)
                    if on_event is not None:
                        on_event(event)
        except httpx.HTTPError as exc:
            raise SpikeTurnError(f"turn stream transport error: {exc}") from exc
        if result.error is not None:
            raise SpikeTurnError(
                f"turn errored: {result.error.get('meta') or result.error}",
                meta=result.error.get("meta") or {},
            )
        if not result.done_meta and not self._saw_done(result):
            raise SpikeTurnError("turn stream ended before a done event")
        return result

    @staticmethod
    def _saw_done(result: TurnResult) -> bool:
        return any(ev.get("type") == "done" for ev in result.events)

    @staticmethod
    def _normalize_event(event_name: str | None, data: dict) -> dict:
        """Ensure a ``type`` key (SSE ``event:`` name is authoritative when present)."""
        event = dict(data)
        if event_name and not event.get("type"):
            event["type"] = event_name
        event.setdefault("type", event_name or "content")
        return event

    # -- readbacks ---------------------------------------------------------

    def get_events(self, session_id: str) -> dict:
        """``GET /events`` — the session projection (narratives, state, users...)."""
        return self._get(f"/api/sessions/{session_id}/events")

    def get_inspect_turns(self, session_id: str) -> dict:
        """``GET /inspect/turns`` — per-turn trace + usage rows."""
        return self._get(f"/api/sessions/{session_id}/inspect/turns")

    def get_agent_calls(self, session_id: str) -> list[dict]:
        """``GET /agent-calls`` — captured agent inputs/outputs (debug telemetry)."""
        body = self._get(f"/api/sessions/{session_id}/agent-calls")
        calls = body.get("calls") if isinstance(body, dict) else None
        return calls if isinstance(calls, list) else []

    def get_agent_debug(self, session_id: str) -> dict:
        return self._get(f"/api/sessions/{session_id}/agent-debug")

    # -- low-level ---------------------------------------------------------

    @staticmethod
    def _json(resp: httpx.Response) -> dict:
        try:
            value = resp.json()
        except (json.JSONDecodeError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _get(self, path: str) -> dict:
        try:
            resp = self._client.get(self._url(path), headers=self._headers)
        except httpx.HTTPError as exc:
            raise SpikeClientError(f"GET {path} transport error: {exc}") from exc
        if resp.status_code >= 400:
            self._fail(resp)
        return self._json(resp)

    def _post(self, path: str, *, json_body: dict) -> dict:
        try:
            resp = self._client.post(self._url(path), json=json_body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise SpikeClientError(f"POST {path} transport error: {exc}") from exc
        if resp.status_code >= 400:
            self._fail(resp)
        return self._json(resp)

    def _patch(self, path: str, *, json_body: dict) -> dict:
        try:
            resp = self._client.patch(self._url(path), json=json_body, headers=self._headers)
        except httpx.HTTPError as exc:
            raise SpikeClientError(f"PATCH {path} transport error: {exc}") from exc
        if resp.status_code >= 400:
            self._fail(resp)
        return self._json(resp)
