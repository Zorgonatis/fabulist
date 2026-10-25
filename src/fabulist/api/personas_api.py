"""Persona registry router (D16: personas as log-owned entities).

Thin transport over :mod:`fabulist.personas.registry`. The log owns
everything (D9): definitions are ``system`` events on the reserved
``__personas__`` anchor session and chat assignments are ``system`` events on
the chat session — this router never writes a side table.

Endpoints
---------
  GET    /api/personas                       -> {"personas": [wire, ...]}
  POST   /api/personas {name, book_ids, ...} -> {"persona": wire}
  GET    /api/personas/{persona_id}          -> {"persona": wire} | 404
  POST   /api/personas/{persona_id}/image    raw image bytes or a data URL
                                             -> store anchor asset + latest-wins
                                                persona append -> {"persona": wire,
                                                "image_asset", "mime"} | 404
  GET    /api/personas/{persona_id}/image    -> image bytes (stored mime) | 404
  DELETE /api/personas/{persona_id}          -> tombstone -> {"persona_id", "deleted": true} | 404
  POST   /api/sessions/{sid}/persona {persona_id}
                                             -> {"session_id", "persona_id"} | 404
  DELETE /api/sessions/{sid}/persona         -> explicit clear (``persona_id: null``
                                             + ``cleared: true``; do NOT inherit
                                             the pinned default)
                                             -> {"session_id", "persona_id": None}
  GET    /api/sessions/{sid}/chat-config     -> {"persona_id", "memory_enabled"}

Wiring (orchestrator-owned; the ``app.state`` seam needs no ``app.py``
signature change)::

    from fabulist.api.personas_api import router as personas_router
    app.include_router(personas_router)

Like :mod:`fabulist.api.chat_config_api` / :mod:`fabulist.api.state_api`, the
router reads the live :class:`~fabulist.log.EventLog` from
``request.app.state.log`` (the app factory stores it there) and reuses
:func:`fabulist.api.attachments._session_exists` for the 404 door.

NOTE: ``GET /api/sessions/{sid}/chat-config`` is also provided by
:mod:`fabulist.api.chat_config_api` (the memory-door owner). Both fold the same
``chat_persona`` / ``chat_memory`` rows and return the identical shape; the
orchestrator mounts one or the other (duplicate registration would just make
FastAPI's first matching route win).
"""

from __future__ import annotations

import base64
import binascii
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

from fabulist.api.attachments import _session_exists
from fabulist.media.images import (
    MAX_IMAGE_BYTES,
    find_definition_asset,
    store_definition_image,
)
from fabulist.personas.registry import (
    Persona,
    append_persona,
    assign_persona,
    clear_persona,
    chat_config_for,
    delete_persona,
    list_personas,
    persona_for_chat,
    set_persona_image,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


router = APIRouter(tags=["personas"])


class PersonaBody(BaseModel):
    """Body of ``POST /api/personas`` — a persona definition.

    ``name`` is required; every other field is optional and defaults to ``""``
    (or an empty collection). An explicit ``persona_id`` updates that persona in
    place (latest-wins); omitted, the registry generates one.

    D31 carry-overs: ``book_ids`` (world-info books this cast member carries),
    ``alternate_greetings`` / ``extensions`` / ``provenance`` (the card's
    full-fidelity fields, ``provenance`` = ``{creator, character_version,
    origin, imported_at}``). All default empty, so a minimal body is unchanged.
    """

    name: str
    persona_id: str | None = None
    description: str = ""
    personality: str = ""
    scenario: str = ""
    first_mes: str = ""
    book_ids: list[str] = []
    alternate_greetings: list[str] = []
    extensions: dict = {}
    provenance: dict = {}
    image_asset: str | None = None


class PersonaAssignment(BaseModel):
    """Body of ``POST /api/sessions/{sid}/persona`` — the persona to assign."""

    persona_id: str


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _wire(persona: Persona) -> dict:
    """Wire shape of one persona (the frontend/other-lane contract)."""
    return {
        "persona_id": persona.persona_id,
        "name": persona.name,
        "description": persona.description,
        "personality": persona.personality,
        "scenario": persona.scenario,
        "first_mes": persona.first_mes,
        # D31 carry-overs (always present, empty on old-log personas).
        "book_ids": list(persona.book_ids),
        "alternate_greetings": list(persona.alternate_greetings),
        "extensions": dict(persona.extensions),
        "provenance": dict(persona.provenance),
        "image_asset": persona.image_asset,
    }


def _decode_data_url(text: str) -> tuple[bytes, str]:
    """Parse ``data:<mime>;base64,<payload>`` into ``(bytes, mime)``."""
    header, sep, payload = text.partition(",")
    if not sep or not header.startswith("data:") or ";base64" not in header:
        raise ValueError("not a base64 data URL")
    mime = header[len("data:") :].split(";", 1)[0].strip()
    try:
        return base64.b64decode(payload, validate=True), mime
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"invalid base64 payload: {exc}") from exc


async def read_definition_image(request: Request) -> tuple[bytes, str]:
    """Read + validate a definition-image body (raw bytes or a data URL).

    Shared by the persona and world image routes. Returns ``(data, mime)``.
    ``415`` for a non ``image/*`` mime or an undecodable data URL, ``422`` for
    an empty body, and ``413`` above :data:`fabulist.media.images.MAX_IMAGE_BYTES`.
    """
    raw = await request.body()
    content_type = (
        (request.headers.get("content-type") or "").split(";")[0].strip().lower()
    )
    stripped = raw.strip()
    if stripped.startswith(b"data:"):
        try:
            data, mime = _decode_data_url(stripped.decode("ascii"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(
                status_code=415, detail=f"invalid image data URL: {exc}"
            ) from None
    else:
        data, mime = raw, content_type
    if not mime.startswith("image/"):
        raise HTTPException(
            status_code=415,
            detail=f"image mime must be image/*, got {mime or 'none'!r}",
        )
    if not data:
        raise HTTPException(status_code=422, detail="empty image body")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"image is {len(data)} bytes, over the "
                f"{MAX_IMAGE_BYTES // (1024 * 1024)} MB limit"
            ),
        )
    return data, mime


def definition_image_response(item: dict) -> Response:
    """Serve one projected definition asset item as its raw bytes + mime."""
    payload = item.get("payload", {})
    data = base64.b64decode(payload.get("bytes_b64") or "")
    return Response(
        content=data,
        media_type=payload.get("mime") or "application/octet-stream",
    )


@router.get("/api/personas")
async def get_personas(request: Request) -> dict:
    """List all personas folded from the ``__personas__`` anchor (latest wins)."""
    return {"personas": [_wire(persona) for persona in list_personas(_log_for(request))]}


@router.post("/api/personas")
async def create_persona(body: PersonaBody, request: Request) -> dict:
    """Append a persona definition (create or update) and echo it back.

    ``422`` when ``name`` is empty/whitespace; an existing ``persona_id``
    updates in place (append-only log, latest-wins — D9/D10).
    """
    log = _log_for(request)
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="persona 'name' must be non-empty")
    persona_id = append_persona(log, body.model_dump())
    persona = next(
        (p for p in list_personas(log) if p.persona_id == persona_id), None
    )
    if persona is None:  # pragma: no cover - the append just landed
        raise HTTPException(status_code=500, detail="persona append did not persist")
    return {"persona": _wire(persona)}


@router.post("/api/personas/{persona_id}/image")
async def set_persona_image_route(
    persona_id: str, request: Request
) -> dict:
    """Attach an image to a persona (sessionless anchor asset; latest-wins).

    Body is raw image bytes (``Content-Type: image/*``) or a base64 data URL.
    Stores the bytes as a content-addressed asset on the reserved anchor
    (:func:`fabulist.media.images.store_definition_image`) and re-appends the
    persona with ``image_asset`` set (append-only, D9). ``404`` for an unknown
    persona; ``415``/``422``/``413`` for a bad body.
    """
    log = _log_for(request)
    target = next(
        (p for p in list_personas(log) if p.persona_id == persona_id), None
    )
    if target is None:
        raise HTTPException(status_code=404, detail="unknown persona")
    data, mime = await read_definition_image(request)
    asset_id = store_definition_image(
        log, data, mime, note=f"persona image: {persona_id}"
    )
    persona = set_persona_image(log, persona_id, asset_id)
    if persona is None:  # pragma: no cover - the existence check just passed
        raise HTTPException(status_code=404, detail="unknown persona")
    return {"persona": _wire(persona), "image_asset": asset_id, "mime": mime}


@router.get("/api/personas/{persona_id}/image")
async def get_persona_image_route(persona_id: str, request: Request) -> Response:
    """Serve a persona's stored image bytes; ``404`` for unknown / no image."""
    log = _log_for(request)
    persona = next(
        (p for p in list_personas(log) if p.persona_id == persona_id), None
    )
    if persona is None:
        raise HTTPException(status_code=404, detail="unknown persona")
    if not persona.image_asset:
        raise HTTPException(status_code=404, detail="persona has no image")
    item = find_definition_asset(log, persona.image_asset)
    if item is None:
        raise HTTPException(status_code=404, detail="image asset not found")
    return definition_image_response(item)


@router.get("/api/personas/{persona_id}")
async def get_persona(persona_id: str, request: Request) -> dict:
    """Return one persona by id, or ``404`` when unknown."""
    log = _log_for(request)
    for persona in list_personas(log):
        if persona.persona_id == persona_id:
            return {"persona": _wire(persona)}
    raise HTTPException(status_code=404, detail="unknown persona")


@router.delete("/api/personas/{persona_id}")
async def delete_persona_route(persona_id: str, request: Request) -> dict:
    """Tombstone a persona (append-only ``persona_deleted``); ``404`` unknown.

    Non-destructive (D9/D10): the definition rows stay queryable and only the
    derived catalog drops the id (:func:`list_personas` folds latest-wins). A
    later ``POST /api/personas`` with the same ``persona_id`` resurrects it.
    """
    log = _log_for(request)
    if not any(persona.persona_id == persona_id for persona in list_personas(log)):
        raise HTTPException(status_code=404, detail="unknown persona")
    delete_persona(log, persona_id)
    return {"persona_id": persona_id, "deleted": True}


@router.post("/api/sessions/{session_id}/persona")
async def set_session_persona(
    session_id: str, body: PersonaAssignment, request: Request
) -> dict:
    """Assign a persona to a chat (latest-wins ``chat_persona`` event).

    ``404`` for an unknown session or an unknown ``persona_id`` (the registry
    validates the id before appending).
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    try:
        assign_persona(log, session_id, body.persona_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from None
    return {"session_id": session_id, "persona_id": body.persona_id}


@router.delete("/api/sessions/{session_id}/persona")
async def delete_session_persona(session_id: str, request: Request) -> dict:
    """Explicitly clear a chat's persona -> sandbox (an explicit "no persona").

    Owner ruling: clear means NO PERSONA, not "use the default again". The
    ``chat_persona`` row carries ``persona_id: null`` plus ``{"cleared": true}``
    so a chat cleared while a default is pinned stays persona-less instead of
    re-inheriting the pin (see :func:`fabulist.personas.registry.clear_persona`).
    Non-destructive: the row is appended, prior assignments stay queryable (D9).
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    clear_persona(log, session_id)
    return {"session_id": session_id, "persona_id": None}


#: (GET /api/sessions/{sid}/chat-config lives in chat_config_api.py — the
#: canonical memory/chat-config surface. Duplicated route removed at
#: integration to avoid FastAPI shadow-mounting both.)

