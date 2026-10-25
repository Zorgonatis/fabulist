"""Tests for the async embedder client (W3.2).

Uses ``httpx.MockTransport`` to intercept the real ``httpx.AsyncClient``: the
request shape and auth are asserted at the transport seam, and the client still
exercises its own JSON/response handling. No pytest-asyncio in this suite, so
async scenarios run via ``asyncio.run`` (house convention).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from fabulist.agents.debug import CURRENT_SESSION
from fabulist.log import EventLog
from fabulist.memory import embedder
from fabulist.memory.embedder import EmbedderError, embed_texts


def run(coro):
    """Drive a coroutine to completion (suite has no pytest-asyncio)."""
    return asyncio.run(coro)


@pytest.fixture
def usage_log(monkeypatch, tmp_path):
    """Bind an event log for usage rows and restore "unbound" afterwards."""
    log = EventLog(tmp_path / "events.db")
    embedder.bind_usage_log(log)
    try:
        yield log
    finally:
        embedder.bind_usage_log(None)
        log.close()


def _usage_rows(log, session_id="s-embed"):
    return [
        event.payload
        for event in log.get_events(session_id)
        if event.type == "system" and event.payload.get("event") == "usage"
    ]


@pytest.fixture
def transport_factory(monkeypatch):
    """Install a MockTransport-backed ``httpx.AsyncClient`` factory."""

    def install(handler, captured=None):
        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def fake_client(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(embedder.httpx, "AsyncClient", fake_client)

    return install


def _ok_body(vectors, indices=None):
    data = [
        {"object": "embedding", "index": i, "embedding": vec}
        for i, vec in enumerate(vectors)
    ]
    return {"object": "list", "data": data, "model": "fake"}


def test_embed_texts_request_shape_and_vectors(transport_factory):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["body"] = json.loads(request.content)
        seen["auth"] = request.headers.get("Authorization")
        seen["content_type"] = request.headers.get("Content-Type")
        return httpx.Response(200, json=_ok_body([[0.1, 0.2], [0.3, 0.4]]))

    transport_factory(handler)

    vectors = run(
        embed_texts("http://localhost:4444", "nomic-embed", ["a", "b"])
    )

    assert seen["method"] == "POST"
    assert seen["url"] == "http://localhost:4444/v1/embeddings"
    assert seen["body"] == {"model": "nomic-embed", "input": ["a", "b"]}
    assert seen["auth"] is None  # no api_key -> no bearer
    assert seen["content_type"] == "application/json"
    assert vectors == [[0.1, 0.2], [0.3, 0.4]]


def test_embed_texts_trailing_slash_endpoint(transport_factory):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=_ok_body([[1.0]]))

    transport_factory(handler)
    run(embed_texts("http://localhost:4444/", "m", ["x"]))
    assert seen["url"] == "http://localhost:4444/v1/embeddings"


def test_embed_texts_sends_bearer_when_keyed(transport_factory):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json=_ok_body([[0.0]]))

    transport_factory(handler)
    run(embed_texts("http://host", "m", ["x"], api_key="secret"))
    assert seen["auth"] == "Bearer secret"


def test_embed_texts_orders_vectors_by_index(transport_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [9.0]},
                    {"index": 0, "embedding": [1.0]},
                ]
            },
        )

    transport_factory(handler)
    vectors = run(embed_texts("http://host", "m", ["a", "b"]))
    assert vectors == [[1.0], [9.0]]


def test_embed_texts_error_path(transport_factory):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="model not loaded")

    transport_factory(handler)

    with pytest.raises(EmbedderError) as excinfo:
        run(embed_texts("http://host", "m", ["a"]))

    err = excinfo.value
    assert err.status_code == 500
    assert "model not loaded" in err.body
    assert "/v1/embeddings" in str(err)


# ---------------------------------------------------------------------------
# W1f: every embed records one system/usage row (cost visibility)
# ---------------------------------------------------------------------------


def test_embed_texts_records_usage_row(transport_factory, usage_log):
    def handler(request: httpx.Request) -> httpx.Response:
        body = _ok_body([[0.5], [0.25]])
        body["usage"] = {"prompt_tokens": 7, "total_tokens": 7}
        return httpx.Response(200, json=body)

    transport_factory(handler)
    token = CURRENT_SESSION.set("s-embed")
    try:
        run(embed_texts("http://host", "nomic-embed", ["a", "bb"]))
    finally:
        CURRENT_SESSION.reset(token)

    rows = _usage_rows(usage_log)
    assert len(rows) == 1
    row = rows[0]
    assert row["agent"] == "embedder"
    assert row["model"] == "nomic-embed"
    assert row["session_id"] == "s-embed"
    # The endpoint's own count wins over the char-count fallback.
    assert row["prompt_tokens"] == 7
    assert row["tokens"] == 7
    assert row["completion_tokens"] == 0
    assert row["total_tokens"] == 7
    assert row["ok"] is True
    assert row["error"] is None
    assert row["inputs"] == 2
    assert row["chars"] == 3
    assert row["latency_s"] >= 0.0
    assert row["endpoint"] == "http://host/v1/embeddings"


def test_embed_texts_falls_back_to_char_count_without_usage(
    transport_factory, usage_log
):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body([[1.0], [2.0]]))

    transport_factory(handler)
    token = CURRENT_SESSION.set("s-embed")
    try:
        run(embed_texts("http://host", "m", ["abc", "de"]))
    finally:
        CURRENT_SESSION.reset(token)

    row = _usage_rows(usage_log)[0]
    assert row["tokens"] is None
    assert row["prompt_tokens"] == 5  # char count, no usage block on the wire
    assert row["chars"] == 5


def test_embed_texts_records_failed_call(transport_factory, usage_log):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="embedder warming up")

    transport_factory(handler)
    token = CURRENT_SESSION.set("s-embed")
    try:
        with pytest.raises(EmbedderError):
            run(embed_texts("http://host", "m", ["abc"]))
    finally:
        CURRENT_SESSION.reset(token)

    row = _usage_rows(usage_log)[0]
    assert row["ok"] is False
    assert "EmbedderError" in row["error"]
    assert "503" in row["error"]
    assert row["prompt_tokens"] == 3  # no response body to read a count from


def test_embed_texts_usage_recording_never_breaks_an_embed(
    transport_factory, monkeypatch
):
    """An unusable log handle is swallowed: the vectors still come back."""

    class _BrokenLog:
        def append_event(self, *args, **kwargs):
            raise RuntimeError("log is down")

    monkeypatch.setattr(embedder, "_usage_log", _BrokenLog())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body([[0.5]]))

    transport_factory(handler)
    token = CURRENT_SESSION.set("s-embed")
    try:
        vectors = run(embed_texts("http://host", "m", ["a"]))
    finally:
        CURRENT_SESSION.reset(token)

    assert vectors == [[0.5]]


def test_embed_texts_without_session_records_nothing(transport_factory, usage_log):
    """No session context (e.g. an offline reindex) means no row to place."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_ok_body([[0.5]]))

    transport_factory(handler)
    run(embed_texts("http://host", "m", ["a"]))
    assert _usage_rows(usage_log) == []


# -- W1f follow-up: the per-turn transports record usage too ------------------


def test_embed_inputs_records_usage_row(transport_factory, usage_log):
    """The recall/indexing transport (indexer.embed_inputs) leaves a usage row."""
    from fabulist.memory.indexer import embed_inputs

    def handler(request: httpx.Request) -> httpx.Response:
        body = _ok_body([[0.5]])
        body["usage"] = {"prompt_tokens": 11}
        return httpx.Response(200, json=body)

    transport_factory(handler)
    token = CURRENT_SESSION.set("s-embed")
    try:
        run(embed_inputs("http://host", "nomic-embed", ["turn query"]))
    finally:
        CURRENT_SESSION.reset(token)

    rows = _usage_rows(usage_log)
    assert len(rows) == 1
    row = rows[0]
    assert row["agent"] == "embedder"
    assert row["model"] == "nomic-embed"
    assert row["prompt_tokens"] == 11
    assert row["ok"] is True
    assert row["endpoint"] == "http://host/v1/embeddings"


def test_embed_inputs_records_usage_on_failure(transport_factory, usage_log):
    """A dead embedder surfaces as an ok=False row — and still raises cleanly."""
    from fabulist.memory.indexer import embed_inputs

    def handler(request: httpx.Request) -> httpx.Response:
        # Deliberately NON-JSON body: the status check must run before parsing.
        return httpx.Response(503, text="down")

    transport_factory(handler)
    token = CURRENT_SESSION.set("s-embed")
    try:
        with pytest.raises(EmbedderError):
            run(embed_inputs("http://host", "nomic-embed", ["a"]))
    finally:
        CURRENT_SESSION.reset(token)

    rows = _usage_rows(usage_log)
    assert len(rows) == 1
    assert rows[0]["ok"] is False
    assert "503" in rows[0]["error"]
