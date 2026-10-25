"""Tests for the rerank client (D25 C3).

The rerank endpoint is faked with ``httpx.MockTransport`` (house convention) so
the request body and the several response score-key spellings are asserted at
the wire seam. No network: the client builds the same ``/v1/rerank`` payload the
live llama.cpp router was probed with.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from fabulist.memory import rerank
from fabulist.memory.rerank import (
    DEFAULT_RERANK_MODEL,
    RerankError,
    build_rerank_payload,
    parse_rerank_response,
    rerank_documents,
)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def transport_factory(monkeypatch):
    """Install a MockTransport-backed ``httpx.AsyncClient`` for rerank."""

    def install(handler):
        transport = httpx.MockTransport(handler)
        real_client = httpx.AsyncClient

        def fake_client(*args, **kwargs):
            kwargs["transport"] = transport
            return real_client(*args, **kwargs)

        monkeypatch.setattr(rerank.httpx, "AsyncClient", fake_client)

    return install


def _ok(results):
    return httpx.Response(200, json={"model": "qwen3-reranker", "results": results})


# --- pure helpers ----------------------------------------------------------


def test_build_rerank_payload_optional_top_n():
    base = build_rerank_payload("m", "q", ["a", "b"])
    assert base == {"model": "m", "query": "q", "documents": ["a", "b"]}
    assert build_rerank_payload("m", "q", ["a"], top_n=1)["top_n"] == 1


def test_parse_rerank_sorts_desc_and_accepts_score_spellings():
    body = json.dumps(
        {
            "results": [
                {"index": 2, "relevance_score": 0.1},
                {"index": 0, "score": 0.9},
                {"index": 1, "relevance": 0.5},
            ]
        }
    )
    parsed = parse_rerank_response(body)
    assert [p["index"] for p in parsed] == [0, 1, 2]
    assert parsed[0]["score"] == 0.9


def test_parse_rerank_skips_malformed_items():
    body = json.dumps({"results": [{"index": "x", "score": 1}, {"score": 1}, {"index": 3}]})
    assert parse_rerank_response(body) == []


# --- transport -------------------------------------------------------------


def test_rerank_documents_hits_v1_rerank_and_strips_v1(transport_factory):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return _ok([{"index": 1, "relevance_score": 0.8}, {"index": 0, "relevance_score": 0.2}])

    transport_factory(handler)
    rows = run(
        rerank_documents(
            "http://127.0.0.1:4445/v1", "qwen3-reranker", "the cloak",
            ["doc a", "doc b"], top_n=2, timeout_s=5,
        )
    )
    assert captured["url"] == "http://127.0.0.1:4445/v1/rerank"
    assert captured["body"] == {
        "model": "qwen3-reranker",
        "query": "the cloak",
        "documents": ["doc a", "doc b"],
        "top_n": 2,
    }
    assert [r["index"] for r in rows] == [1, 0]


def test_rerank_documents_non_200_raises(transport_factory):
    transport_factory(lambda request: httpx.Response(500, text="boom"))
    with pytest.raises(RerankError) as excinfo:
        run(rerank_documents("http://h", "m", "q", ["a"]))
    assert excinfo.value.status_code == 500
    assert "boom" in excinfo.value.body


def test_rerank_documents_empty_documents_no_request(monkeypatch):
    def explode(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("no request for empty documents")

    monkeypatch.setattr(rerank.httpx, "AsyncClient", explode)
    assert run(rerank_documents("http://h", "m", "q", [])) == []


def test_default_model_alias():
    assert DEFAULT_RERANK_MODEL == "qwen3-reranker"


# --- malformed-response guard (owner flip: rerank default ON) -----------------------

def test_parse_response_without_results_raises():
    """A 200 body with no 'results' list is an error, not an empty ranking.

    Regression: an embeddings-only endpoint (the reranker fallback target when
    no reranker connection exists) answers /v1/rerank with an embeddings-shaped
    body. The old parser returned [] and recall silently dropped EVERY
    candidate. It must raise so the policy falls back to vector order.
    """
    import pytest

    from fabulist.memory.rerank import RerankError, parse_rerank_response

    with pytest.raises(RerankError, match="no 'results' list"):
        parse_rerank_response('{"object": "list", "data": [{"embedding": [0.1]}]}')
    with pytest.raises(RerankError, match="no 'results' list"):
        parse_rerank_response('"not a dict"')


def test_parse_response_with_empty_results_list_is_valid():
    from fabulist.memory.rerank import parse_rerank_response

    assert parse_rerank_response('{"results": []}') == []
