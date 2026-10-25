"""Attachment (image/a/v) router (W2.8 + W3.8 v1).

The event log owns the bytes (D9): this router stores an uploaded image as an
``asset`` event and its caption as an ``image_caption`` ``system`` event. It
never writes a side table and never mutates history.

  POST /api/sessions/{id}/attachments   multipart ``file`` (+ optional ``note``)
                                        -> store the image, run the captioner,
                                           return the asset with its caption
  GET  /api/sessions/{id}/attachments   list stored assets with folded captions

Captioner seam
--------------
The captioner is a vision model resolved from the connections registry. It is
injected through ``request.app.state.captioner`` so tests pass a fake and the
orchestrator wires the real one. Wiring (orchestrator-owned)::

    from fabulist.api.attachments import router
    from fabulist.wiring import RegistryModelCaller
    app.include_router(router)
    app.state.captioner = RegistryModelCaller("captioner", registry)

When ``app.state.captioner`` is absent the upload endpoint returns ``503`` —
the captioner is what turns bytes into turn-visible text, so a POST without it
cannot succeed.

Like :mod:`fabulist.api.sessions`, the router reads the live
:class:`~fabulist.log.EventLog` from ``request.app.state.log`` (the app factory
stores it there), so no signature change is needed in ``app.py``.
"""

from __future__ import annotations

import logging

import sqlite3
from typing import TYPE_CHECKING

import base64

from fastapi.responses import Response
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from fabulist.log.schema_gate import STT_FAILURE_EVENT
from fabulist.media import CAPTION_EVENT, caption, store_image
from fabulist.media.images import find_definition_asset
from fabulist.media.av import MediaTooLarge, sniff_kind, store_media, validate_size

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.graph.types import ModelCaller
    from fabulist.log import EventLog


router = APIRouter(tags=["attachments"])

#: Default mime when an upload carries no content type.
_DEFAULT_MIME = "application/octet-stream"


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _captioner_for(request: Request) -> "ModelCaller":
    """Return the app's captioner ModelCaller, or 503 when unwired."""
    captioner = getattr(request.app.state, "captioner", None)
    if captioner is None:
        raise HTTPException(status_code=503, detail="captioner unavailable")
    return captioner


def _session_exists(log: "EventLog", session_id: str) -> bool:
    """True when the log knows ``session_id``.

    Reads the ``sessions`` bookkeeping table (written on a session's first
    event, and on fork creation) through a short-lived secondary connection, so
    a freshly forked branch with no own rows still counts as existing. Mirrors
    :func:`fabulist.api.sessions._session_ids`.
    """
    if log.get_events(session_id):
        return True
    conn = sqlite3.connect(log.path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()
    return row is not None


def _captions(log: "EventLog", session_id: str) -> dict[str, str]:
    """Fold ``image_caption`` system rows into ``{asset_ref: text}``.

    Later rows win (append-only log, latest caption for an asset is current);
    the original rows stay queryable.
    """
    captions: dict[str, str] = {}
    for event in log.get_events(session_id):
        if event.type != "system":
            continue
        if event.payload.get("event") != CAPTION_EVENT:
            continue
        ref = event.payload.get("asset")
        if isinstance(ref, str):
            captions[ref] = event.payload.get("text", "")
    return captions


logger_stt = logging.getLogger(__name__)

#: Cap on the error text carried by a stage-failure event (mirrors the stderr
#: tail kept by ``media.stt``); the traceback still lives in the server journal.
_FAILURE_ERROR_LIMIT = 800

#: Strong refs for fire-and-forget transcription tasks — asyncio keeps only
#: weak task references, so unreferenced tasks get GC'd mid-flight silently.
_STT_TASKS: set = set()


def _append_stt_failure(
    log, session_id: str, asset_id: str, stage: str, exc: BaseException
) -> None:
    """Record a non-fatal STT failure as a ``system``/``stt_failure`` event.

    Audit 2026-10-10 (silent-failure class): the failed transcript used to leave
    only a server-journal warning, so the log (D9 source of truth) and the
    inspector missed it. Best-effort: a failure to record must never itself
    surface (the surrounding task is fire-and-forget anyway).
    """
    try:
        log.append_event(
            session_id,
            "system",
            "stt",
            {
                "event": STT_FAILURE_EVENT,
                "stage": stage,
                "error": str(exc)[:_FAILURE_ERROR_LIMIT],
                "asset_id": asset_id,
            },
        )
    except Exception:  # noqa: BLE001 - failure reporting is best-effort
        logger_stt.exception("could not record stt failure for %s", asset_id)


def schedule_transcription(log, session_id: str, asset_id: str, data: bytes, mime: str) -> None:
    """Transcribe an audio asset in the background; append one stt system event.

    W3.8 v1: the transcript makes spoken audio content visible to the narrator
    (audio does not ride turns as content parts — narrators do not listen).
    A failure appends a ``system``/``stt_failure`` event (S4c) and is otherwise
    inert (the asset stays embedded-only); never fatal to the turn.
    """
    import asyncio

    async def _run() -> None:
        try:
            from fabulist.media.stt import MediaSttError, transcribe

            result = await asyncio.to_thread(transcribe, data, mime)
            if result.text.strip():
                log.append_event(
                    session_id,
                    "system",
                    "stt",
                    {"event": "stt", "asset_id": asset_id, "text": result.text,
                     "language": result.language, "duration_s": result.duration_s},
                )
        except MediaSttError as exc:
            logger_stt.warning("stt failed for %s", asset_id, exc_info=True)
            _append_stt_failure(log, session_id, asset_id, exc.stage, exc)
        except Exception as exc:  # noqa: BLE001 - housekeeping must never surface
            logger_stt.exception("stt task failed for %s", asset_id)
            _append_stt_failure(log, session_id, asset_id, "stt", exc)
        finally:
            _STT_TASKS.discard(task)

    task = asyncio.create_task(_run())
    _STT_TASKS.add(task)


def _asset_view(item: dict, caption_text: str = "", *, inline: bool = False) -> dict:
    """Project a log ``asset`` item to the wire shape.

    ``inline=True`` (list endpoint) adds a ``data_url`` so the chat UI can
    render the image on the user's message without a second fetch — the app
    is single-user local; base64-in-JSON is the boring correct move.
    """
    payload = item.get("payload", {})
    view = {
        "asset_id": payload.get("asset_id"),
        "kind": payload.get("kind", "image"),
        "mime": payload.get("mime", ""),
        "sha256": payload.get("sha256", ""),
        "note": payload.get("note", ""),
        "caption": caption_text,
        "seq": item.get("seq"),
        "ts": item.get("ts"),
    }
    if inline and payload.get("bytes_b64"):
        view["data_url"] = f"data:{view['mime']};base64,{payload['bytes_b64']}"
    return view


@router.post("/api/sessions/{session_id}/attachments")
async def upload_attachment(
    session_id: str,
    request: Request,
    file: UploadFile = File(...),
    note: str = Form(""),
) -> dict:
    """Store an uploaded image (captioned) or audio/video (as-is).

    Images keep the W2.8 contract: caption via the captioner, native VLM
    content-part at turn time. Audio/video (user directive 2026-10-09:
    "we'll allow inline video, audio") are stored as assets with NO caption
    step — the multimodal embedder indexes them for recall (R7/R12); STT
    parsing is W3.8.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    # W5-S2: the upload/caption path is not driven through the engine, so the
    # turn's CURRENT_SESSION contextvar is unset here; set it before the
    # captioner call so an agent-debug capture lands on THIS session.
    from fabulist.agents.debug import CURRENT_SESSION

    CURRENT_SESSION.set(session_id)

    data = await file.read()
    mime = file.content_type or _DEFAULT_MIME
    kind = sniff_kind(data, mime)
    if kind == "unknown":
        raise HTTPException(415, detail=f"unrecognized media (mime {mime!r})")
    try:
        validate_size(kind, len(data))
    except MediaTooLarge as exc:
        raise HTTPException(413, detail=str(exc)) from exc

    if kind == "image":
        captioner = _captioner_for(request)
        asset_event = store_image(log, session_id, data, mime, note=note)
        caption_event = await caption(
            log,
            session_id,
            asset_event.payload["asset_id"],
            captioner,
            messages_builder=getattr(request.app.state, "caption_flow", None),
        )
        caption_text = caption_event.payload.get("text", "")
    else:
        asset_event = store_media(log, session_id, data, mime, kind, note=note)
        caption_text = ""
        if kind == "audio":
            schedule_transcription(log, session_id, asset_event.payload["asset_id"], data, mime)
    return {
        "session_id": session_id,
        "asset": _asset_view(
            {**asset_event.to_dict(), "payload": asset_event.payload},
            caption_text,
        ),
    }


@router.get("/api/sessions/{session_id}/attachments/{asset_id}/raw")
async def raw_attachment(
    session_id: str, asset_id: str, request: Request
) -> Response:
    """Serve one asset's bytes (inline <video>/<audio> sources; no auth —
    single-user local app, same trust boundary as the SSE stream).

    A ref that is not a session asset falls back to the sessionless definition
    store: card art is a persona image the seed event references, so the card
    details view still renders it.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    for item in log.get_projection(session_id).get("assets", []):
        payload = item.get("payload", {})
        if payload.get("asset_id") == asset_id:
            return _asset_bytes_response(payload)
    definition = find_definition_asset(log, asset_id)
    if definition is not None:
        return _asset_bytes_response(definition.get("payload", {}))
    raise HTTPException(status_code=404, detail="unknown asset")


def _asset_bytes_response(payload: dict) -> Response:
    """One asset payload's bytes + its stored mime, as an HTTP response."""
    return Response(
        content=base64.b64decode(payload.get("bytes_b64") or ""),
        media_type=payload.get("mime") or "application/octet-stream",
    )


def _card_asset_ids(log: "EventLog", session_id: str) -> set[str]:
    """The ``card_asset`` refs of this session's imported cards.

    Walks the same system rows as :func:`fabulist.api.export_api._latest_card`
    (any ``system`` payload carrying a ``card`` dict) and collects the payload's
    image ref. Card art is a DEFINITION image: the seed event only points at it,
    so it is not a chat attachment and must never render as a user bubble. Keyed
    on the ref, never on the ``note`` text — a genuine upload whose note happens
    to say "card" still attaches.
    """
    refs: set[str] = set()
    for event in log.get_events(session_id):
        if event.type != "system" or not isinstance(event.payload.get("card"), dict):
            continue
        ref = event.payload.get("card_asset")
        if isinstance(ref, str) and ref:
            refs.add(ref)
    return refs


@router.get("/api/sessions/{session_id}/attachments")
async def list_attachments(session_id: str, request: Request) -> dict:
    """List the session's stored assets, each with its latest caption.

    Card art is excluded: its bytes live in the definition store and the seed
    event only references them (:func:`_card_asset_ids`), so a card import
    cannot look like an image the player attached.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    captions = _captions(log, session_id)
    card_assets = _card_asset_ids(log, session_id)
    attachments = [
        _asset_view(
            item, captions.get(item.get("payload", {}).get("asset_id"), ""), inline=True
        )
        for item in log.get_projection(session_id).get("assets", [])
        if item.get("payload", {}).get("asset_id") not in card_assets
    ]
    return {"session_id": session_id, "attachments": attachments}
