"""Async embedder client for OpenAI-compatible embedding endpoints (W3.2).

Talks to ``POST {endpoint}/v1/embeddings`` with the OpenAI request shape
``{"model": model, "input": texts}`` (llama.cpp's server implements this route).
Bearer auth is sent only when an ``api_key`` is supplied, so keyless local
servers work unchanged.

``httpx`` is used when importable (the project's dev extra installs it); the
fallback path drives ``urllib`` inside a thread executor so a minimal runtime
without httpx still works. Non-2xx responses raise :class:`EmbedderError` with
the status code and raw body attached.

Every call ALSO records one ``system``/``usage`` row (W1f) — embedding cost was
invisible in ``/api/usage`` while every registry model call was counted. The row
rides the existing usage schema (:func:`fabulist.usage.track.usage_payload`),
carries the endpoint's own ``usage.prompt_tokens`` when it reports one and the
input char count otherwise, and is best-effort by contract: a missing log, a
missing session or a failed append never disturbs the embed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

try:  # pragma: no cover - exercised by import availability, not branches
    import httpx
except ImportError:  # pragma: no cover - fallback path
    httpx = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

__all__ = [
    "EmbedderError",
    "embed_texts",
    "bind_usage_log",
    "record_embed_usage",
]

#: The agent label every embeddings call is recorded under — the same name the
#: call-site map gives it (``prompts/generated/agent-map.json``,
#: ``fabulist.memory.embedder.embed_texts``).
EMBEDDER_AGENT = "embedder"

#: Longest error text persisted on a failed embed's usage row.
_ERROR_LIMIT = 500

#: The log the embed transport records usage rows on. Bound once by the wiring
#: (:func:`fabulist.wiring.make_memorizer`, which owns the memory subsystem's
#: log handle); never bound means "no rows", never "no embedding".
_usage_log: "EventLog | None" = None


def bind_usage_log(log: "EventLog | None") -> None:
    """Bind the log embeds record their ``system``/``usage`` rows on (W1f).

    Pass ``None`` to unbind (tests, the offline reindex script). Usage recording
    is measurement only: with no log bound the embed runs exactly as before.
    """
    global _usage_log
    _usage_log = log


class EmbedderError(RuntimeError):
    """Raised when the embeddings endpoint returns a non-200 response."""

    def __init__(self, status_code: int, body: str, url: str = "") -> None:
        self.status_code = status_code
        self.body = body
        self.url = url
        detail = body if len(body) <= 500 else body[:500] + "…"
        super().__init__(f"embeddings request to {url} failed ({status_code}): {detail}")


def _build_request(
    endpoint: str, model: str, texts: list[str], api_key: str | None
) -> tuple[str, dict[str, Any], dict[str, str]]:
    """Return ``(url, json_payload, headers)`` for an embeddings call."""
    url = endpoint.rstrip("/") + "/v1/embeddings"
    payload: dict[str, Any] = {"model": model, "input": texts}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return url, payload, headers


def _parse_embeddings(body: str) -> list[list[float]]:
    """Extract embedding vectors from an OpenAI-style response body, index-sorted."""
    data = json.loads(body).get("data", [])
    ordered = sorted(data, key=lambda item: item.get("index", 0))
    return [list(item["embedding"]) for item in ordered]


def _usage_prompt_tokens(body: str) -> int | None:
    """The response's own ``usage.prompt_tokens``, or ``None`` when unreported.

    OpenAI-compatible embedding servers may or may not return a ``usage`` block;
    a missing/unparseable one is not an error (the char count stands in).
    """
    try:
        tokens = (json.loads(body).get("usage") or {}).get("prompt_tokens")
    except Exception:  # noqa: BLE001 - the vectors are what the caller needs
        return None
    return int(tokens) if tokens is not None else None


def _input_texts(payload: dict[str, Any]) -> list[str]:
    """The request's plain-text inputs (multimodal items count as no chars)."""
    inputs = payload.get("input")
    if isinstance(inputs, str):
        return [inputs]
    if isinstance(inputs, list):
        return [item for item in inputs if isinstance(item, str)]
    return []


def record_embed_usage(
    *,
    model: str,
    texts: list[str],
    latency_s: float,
    endpoint: str = "",
    prompt_tokens: int | None = None,
    error: BaseException | None = None,
) -> None:
    """Append ONE ``system``/``usage`` row for an embeddings call (W1f).

    Best-effort by contract (D23: measurement must never break generation): no
    bound log, no session context, a schema surprise or a failed append all end
    in a log warning — the embed itself is never lost to its own accounting.

    ``prompt_tokens`` carries the endpoint's own count when it reports one and
    the total input CHAR count otherwise (putting the estimate on the token
    field keeps the ``/api/usage`` aggregation meaningful); the embeddings shape
    has no completion side, so ``completion_tokens`` is always 0. The error
    detail and the input/char counts ride as extras on the SAME row (D4 allows
    extras), so one row is both measurable and interrogatable.
    """
    try:
        from fabulist.agents.debug import CURRENT_SESSION
        from fabulist.usage.track import usage_payload

        log = _usage_log
        session_id = CURRENT_SESSION.get("")
        if log is None or not session_id:
            return
        chars = sum(len(text) for text in texts)
        payload = usage_payload(
            agent=EMBEDDER_AGENT,
            model=str(model),
            session_id=session_id,
            prompt_tokens=int(prompt_tokens if prompt_tokens is not None else chars),
            completion_tokens=0,
            latency_s=float(latency_s),
        )
        payload.update(
            {
                "ok": error is None,
                "error": (
                    None
                    if error is None
                    else f"{type(error).__name__}: {error}"[: _ERROR_LIMIT]
                ),
                "inputs": len(texts),
                "chars": chars,
                "tokens": None if prompt_tokens is None else int(prompt_tokens),
                "endpoint": str(endpoint),
            }
        )
        log.append_event(session_id, "system", EMBEDDER_AGENT, payload)
    except Exception:  # noqa: BLE001 - usage is measurement, never fatal
        logger.warning("embedder usage row append failed", exc_info=True)


def _record_transport_usage(
    url: str,
    payload: dict[str, Any],
    latency_s: float,
    *,
    body: str | None = None,
    error: BaseException | None = None,
) -> None:
    """Record one transport attempt (success or failure) as a usage row."""
    record_embed_usage(
        model=str(payload.get("model") or ""),
        texts=_input_texts(payload),
        latency_s=latency_s,
        endpoint=url,
        prompt_tokens=_usage_prompt_tokens(body) if body is not None else None,
        error=error,
    )


async def _embed_httpx(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_s: float,
) -> list[list[float]]:
    """Run one embeddings request over an ``httpx.AsyncClient``."""
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            response = await client.post(url, json=payload, headers=headers)
        if response.status_code != 200:
            raise EmbedderError(response.status_code, response.text, url)
        vectors = _parse_embeddings(response.text)
    except Exception as error:
        _record_transport_usage(url, payload, time.monotonic() - started, error=error)
        raise
    _record_transport_usage(url, payload, time.monotonic() - started, body=response.text)
    return vectors


def _embed_urllib(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout_s: float,
) -> list[list[float]]:
    """Blocking urllib implementation used only when httpx is unavailable."""
    started = time.monotonic()
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = response.read().decode()
            vectors = _parse_embeddings(body)
    except urllib.error.HTTPError as error:  # non-2xx
        failure = EmbedderError(error.code, error.read().decode(errors="replace"), url)
        _record_transport_usage(url, payload, time.monotonic() - started, error=failure)
        raise failure from error
    except Exception as error:
        _record_transport_usage(url, payload, time.monotonic() - started, error=error)
        raise
    _record_transport_usage(url, payload, time.monotonic() - started, body=body)
    return vectors


async def embed_texts(
    endpoint: str,
    model: str,
    texts: list[str],
    api_key: str | None = None,
    timeout_s: float = 30.0,
) -> list[list[float]]:
    """Embed ``texts`` via ``{endpoint}/v1/embeddings``; return one vector per input.

    Vectors are returned in the response's ``index`` order. A non-200 response
    raises :class:`EmbedderError` carrying the status code and raw body.
    """
    url, payload, headers = _build_request(endpoint, model, texts, api_key)
    if httpx is not None:
        return await _embed_httpx(url, payload, headers, timeout_s)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        None, _embed_urllib, url, payload, headers, timeout_s
    )
