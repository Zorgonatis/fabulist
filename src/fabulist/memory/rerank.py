"""Rerank client for llama.cpp / OpenAI-compatible ``/v1/rerank`` (D25 C3).

Rerank is a REAL but OPTIONAL stage, default OFF. This module is deliberately a
thin transport over the same ``httpx`` dependency the indexer already uses (the
package ships ``httpx`` via the OpenAI SDK / the dev extra — no new dependency).

Live-probed contract (2026-10-10, llama.cpp router on :4445)::

    POST {endpoint}/v1/rerank
    {"model": "qwen3-reranker", "query": "...", "documents": ["...", "..."],
     "top_n": 2}                                    # top_n optional
    -> {"results": [{"index": 1, "relevance_score": 0.87}, ...]}   # score desc

``results`` arrive sorted best-first by the server; this client re-sorts by the
score defensively and normalizes the several score key spellings seen in the
wild (``relevance_score`` / ``score`` / ``relevance``). A non-200 response
raises :class:`RerankError` carrying the status and body — the recall caller
catches it and degrades to vector order (the stage ships dark otherwise).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

__all__ = [
    "DEFAULT_RERANK_MODEL",
    "RerankError",
    "build_rerank_payload",
    "parse_rerank_response",
    "rerank_documents",
]

#: Default model alias for the rerank service (per docs/RESUME.md, :4445).
DEFAULT_RERANK_MODEL = "qwen3-reranker"

#: Score keys accepted, in order of preference.
_SCORE_KEYS = ("relevance_score", "score", "relevance")


class RerankError(RuntimeError):
    """Raised when the rerank endpoint returns a non-200 response."""

    def __init__(self, status_code: int, body: str, url: str = "") -> None:
        self.status_code = status_code
        self.body = body
        self.url = url
        detail = body if len(body) <= 500 else body[:500] + "…"
        super().__init__(f"rerank request to {url} failed ({status_code}): {detail}")


def build_rerank_payload(
    model: str, query: str, documents: list[str], top_n: int | None = None
) -> dict[str, Any]:
    """Return the JSON body for a llama.cpp/OpenAI-compatible rerank call."""
    payload: dict[str, Any] = {
        "model": model,
        "query": query,
        "documents": list(documents),
    }
    if top_n is not None:
        payload["top_n"] = top_n
    return payload


def parse_rerank_response(body: str) -> list[dict[str, Any]]:
    """Parse a rerank body into ``[{"index", "score"}, ...]`` sorted score-desc.

    Documents whose score key is missing/unparseable are skipped; a document
    the server omits is simply absent from the result. Ties keep server order
    (stable sort).

    A body WITHOUT a ``results`` list is NOT an empty result — it is an
    unrecognizable response (an embeddings-only endpoint, a proxy error page,
    …). It raises :class:`RerankError` so the recall policy falls back to
    vector order instead of silently dropping every candidate. An explicit
    ``"results": []`` stays a valid empty ranking.
    """
    data = json.loads(body)
    if not isinstance(data, dict) or not isinstance(data.get("results"), list):
        got = sorted(data) if isinstance(data, dict) else type(data).__name__
        raise RerankError(
            200,
            f"unrecognizable rerank response — no 'results' list (keys: {got}); "
            "endpoint is not a rerank service",
        )
    results = data["results"]
    scored: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        if not isinstance(index, int):
            continue
        score: float | None = None
        for key in _SCORE_KEYS:
            raw = item.get(key)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                score = float(raw)
                break
        if score is None:
            continue
        scored.append({"index": index, "score": score})
    scored.sort(key=lambda entry: entry["score"], reverse=True)
    return scored


async def rerank_documents(
    endpoint: str,
    model: str,
    query: str,
    documents: list[str],
    *,
    top_n: int | None = None,
    api_key: str | None = None,
    timeout_s: float = 30.0,
) -> list[dict[str, Any]]:
    """Rerank ``documents`` against ``query``; return scored, best-first entries.

    ``endpoint`` is the service root (``http://127.0.0.1:4445``); a trailing
    ``/v1`` is tolerated. Empty ``documents`` returns ``[]`` without a request.
    """
    if not documents:
        return []
    base = endpoint.rstrip("/")
    if base.endswith("/v1"):
        base = base[: -len("/v1")]
    url = base + "/v1/rerank"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = build_rerank_payload(model, query, documents, top_n=top_n)
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.post(url, json=payload, headers=headers)
    if response.status_code != 200:
        raise RerankError(response.status_code, response.text, url)
    return parse_rerank_response(response.text)
