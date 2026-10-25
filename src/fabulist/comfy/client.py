"""ComfyUI HTTP client (V1 image-gen).

Thin ``httpx`` transport over the four ComfyUI routes the lane needs:

* ``POST /prompt``          -> :meth:`ComfyClient.submit` (returns ``prompt_id``)
* ``GET  /history/{id}``    -> :meth:`ComfyClient.poll` (returns the outputs map)
* ``GET  /view``            -> :meth:`ComfyClient.fetch_image` (returns bytes)
* ``GET  /system_stats``    -> :meth:`ComfyClient.system_stats` /
  :meth:`ComfyClient.reachable` (the enabled check)

Errors are typed so the router can turn them into row-level error payloads
instead of a 500. A ``422`` from ``/prompt`` carries ComfyUI's ``node_errors``
verbatim on :class:`ComfySubmitError` — the user needs to see *which node*
rejected the workflow.

``client`` is an injection seam: pass an ``httpx.AsyncClient`` (e.g. backed by
``httpx.MockTransport``) to run the client in tests with no network. When it is
``None`` a short-lived client is created per request.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx

__all__ = [
    "ComfyClient",
    "ComfyError",
    "ComfyResponseError",
    "ComfySubmitError",
    "ComfyTimeout",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_TIMEOUT_S",
    "STATS_TIMEOUT_S",
    "first_image",
]

#: Default poll cap (10 min, per the plan) and the 2 s poll interval.
DEFAULT_TIMEOUT_S = 600.0
DEFAULT_POLL_INTERVAL_S = 2.0

#: ``/system_stats`` health probe timeout (the config endpoint's 2 s budget).
STATS_TIMEOUT_S = 2.0


class ComfyError(RuntimeError):
    """Base class for every ComfyUI transport/mapping failure."""


class ComfySubmitError(ComfyError):
    """``POST /prompt`` was rejected (or answered with no ``prompt_id``).

    ``node_errors`` is ComfyUI's own validation map, kept verbatim so the UI can
    show exactly which node rejected the workflow.
    """

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        node_errors: Any = None,
        detail: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.node_errors = node_errors
        self.detail = detail


class ComfyResponseError(ComfyError):
    """A ComfyUI route answered with a non-2xx status or unusable body."""

    def __init__(
        self, message: str, *, status: int | None = None, detail: Any = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.detail = detail


class ComfyTimeout(ComfyError):
    """``/history`` never reported the prompt as finished within the cap."""


def first_image(outputs: Any) -> dict | None:
    """First ``{filename, subfolder, type}`` image in a ComfyUI ``outputs`` map.

    ComfyUI's history outputs are ``{node_id: {"images": [...]}, ...}``; a
    finished prompt with no image node (or a failed run) yields ``None`` so the
    caller can raise a row-level "no image" error.
    """
    if not isinstance(outputs, dict):
        return None
    for node_output in outputs.values():
        if not isinstance(node_output, dict):
            continue
        images = node_output.get("images")
        if isinstance(images, list):
            for image in images:
                if isinstance(image, dict) and isinstance(image.get("filename"), str):
                    return image
    return None


def _detail_digest(detail: Any, limit: int = 300) -> str:
    """A short, human-usable digest of ComfyUI's error body.

    ``node_errors`` is the diagnostic users need (which node rejected the
    workflow, and why) — surface its node ids/rows directly, else fall back to
    truncated JSON/text. Live-gate finding: the bare status message dropped the
    reason entirely, so a missing-checkpoint failure read as "returned 400".
    """
    if isinstance(detail, dict):
        node_errors = detail.get("node_errors")
        if isinstance(node_errors, dict) and node_errors:
            parts = []
            for node_id, rows in list(node_errors.items())[:4]:
                # Comfy uses two shapes: a list of {"message"|"type"} rows, or a
                # dict {"errors": [ ...same rows... ]}. Normalise both.
                entries = rows
                if isinstance(rows, dict):
                    entries = rows.get("errors")
                reason = ""
                if isinstance(entries, list) and entries and isinstance(entries[0], dict):
                    reason = str(
                        entries[0].get("message") or entries[0].get("type") or ""
                    )
                parts.append(f"node {node_id}: {reason}".strip())
            extra = "" if len(node_errors) <= 4 else f" (+{len(node_errors) - 4} more)"
            return "; ".join(parts) + extra
        text = json.dumps(detail)
    else:
        text = str(detail)
    return text[:limit] + ("…" if len(text) > limit else "")


def _json_or_text(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


class ComfyClient:
    """One ComfyUI endpoint (base URL + poll policy)."""

    def __init__(
        self,
        base_url: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        stats_timeout_s: float = STATS_TIMEOUT_S,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._timeout_s = timeout_s
        self._poll_interval_s = poll_interval_s
        self._stats_timeout_s = stats_timeout_s

    # -- transport ---------------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    @asynccontextmanager
    async def _session(self, timeout: float) -> AsyncIterator[httpx.AsyncClient]:
        """Yield the injected client, or a short-lived one for this request."""
        if self._client is not None:
            yield self._client
            return
        async with httpx.AsyncClient(timeout=timeout) as client:
            yield client

    # -- routes ------------------------------------------------------------
    async def submit(self, workflow: dict) -> str:
        """``POST /prompt``. Returns the ``prompt_id``.

        422 (workflow validation) surfaces ComfyUI's ``node_errors`` verbatim;
        any other error becomes a :class:`ComfySubmitError` too.
        """
        async with self._session(self._timeout_s) as client:
            response = await client.post(
                self._url("/prompt"), json={"prompt": workflow}
            )
        if response.status_code == 422:
            detail = _json_or_text(response)
            node_errors = (
                detail.get("node_errors") if isinstance(detail, dict) else None
            )
            raise ComfySubmitError(
                "ComfyUI rejected the workflow: " + _detail_digest(detail),
                status=422,
                node_errors=node_errors,
                detail=detail,
            )
        if response.status_code >= 400:
            body = _json_or_text(response)
            raise ComfySubmitError(
                f"ComfyUI /prompt returned {response.status_code}: "
                + _detail_digest(body),
                status=response.status_code,
                detail=body,
            )
        data = _json_or_text(response)
        prompt_id = data.get("prompt_id") if isinstance(data, dict) else None
        if not prompt_id:
            raise ComfySubmitError(
                "ComfyUI accepted the prompt but returned no prompt_id", detail=data
            )
        return str(prompt_id)

    async def poll(
        self,
        prompt_id: str,
        timeout: float | None = None,
        *,
        interval: float | None = None,
    ) -> dict:
        """Poll ``GET /history/{id}`` until the prompt's entry appears.

        ComfyUI returns ``{}`` while the prompt is queued/running and
        ``{prompt_id: {...}}`` once it finishes, so the entry's ``outputs`` map is
        returned as-is (possibly empty for a run that produced no image). Raises
        :class:`ComfyTimeout` past the cap.
        """
        cap = self._timeout_s if timeout is None else timeout
        wait = self._poll_interval_s if interval is None else interval
        deadline = time.monotonic() + cap
        while True:
            async with self._session(self._timeout_s) as client:
                response = await client.get(self._url(f"/history/{prompt_id}"))
            if response.status_code >= 400:
                raise ComfyResponseError(
                    f"ComfyUI /history returned {response.status_code}",
                    status=response.status_code,
                    detail=_json_or_text(response),
                )
            history = _json_or_text(response)
            entry = history.get(prompt_id) if isinstance(history, dict) else None
            if isinstance(entry, dict):
                outputs = entry.get("outputs")
                return outputs if isinstance(outputs, dict) else {}
            if time.monotonic() >= deadline:
                raise ComfyTimeout(
                    f"ComfyUI prompt {prompt_id} did not finish within {cap:.0f}s"
                )
            await asyncio.sleep(wait)

    async def fetch_image(
        self, filename: str, subfolder: str = "", type: str = "output"
    ) -> bytes:
        """``GET /view``. Returns the raw image bytes."""
        params = {"filename": filename, "subfolder": subfolder, "type": type}
        async with self._session(self._timeout_s) as client:
            response = await client.get(self._url("/view"), params=params)
        if response.status_code >= 400:
            raise ComfyResponseError(
                f"ComfyUI /view returned {response.status_code}",
                status=response.status_code,
                detail=_json_or_text(response),
            )
        return response.content

    async def system_stats(self) -> dict:
        """``GET /system_stats`` (2 s timeout). The enabled-check probe."""
        async with self._session(self._stats_timeout_s) as client:
            response = await client.get(self._url("/system_stats"))
        if response.status_code >= 400:
            raise ComfyResponseError(
                f"ComfyUI /system_stats returned {response.status_code}",
                status=response.status_code,
                detail=_json_or_text(response),
            )
        data = _json_or_text(response)
        return data if isinstance(data, dict) else {}

    async def reachable(self) -> bool:
        """True when ``/system_stats`` answers; never raises."""
        try:
            await self.system_stats()
            return True
        except Exception:  # noqa: BLE001 - a health probe reports, it never raises
            return False
