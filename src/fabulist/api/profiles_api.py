"""Experience-profile router (D33): export / import a portable experience bundle.

Thin transport over :mod:`fabulist.profiles`. The event log owns everything
(D9): export derives from the anchor folds, import appends supersede rows to the
same anchors — no side table.

Endpoints
---------
  GET  /api/profiles/export?world=<id>&preset=<id>&assistants=1&name=<str>
       -> the v1 envelope as a JSON download (``Content-Disposition:
          attachment``). ``preset`` is required; ``world`` and ``assistants`` are
          opt-in. An unknown world/preset id is a precise ``404``.
  GET  /api/profiles/export.png?world=<id>&preset=<id>&assistants=1&name=<str>
       -> the SAME envelope as ``image/png`` (item G / owner ruling #7): the
          profile's assigned image as the PNG body + the envelope JSON in a
          ``fabulist_profile`` tEXt chunk. Same query params, different
          representation. ``404`` when no layer has a resolvable image;
          ``422`` when the assigned image is not a PNG.
  POST /api/profiles/import   (body = the envelope, or a profile PNG)
       -> ``{imported, skipped, warnings}`` (the import result). A PNG body
          (detected by its signature) is decoded through
          :func:`fabulist.profiles.png.profile_from_png` and then imports
          exactly like the JSON body; a PNG without the chunk is a ``422``.

Wiring (orchestrator-owned; the ``app.state`` seam needs no ``app.py`` signature
change)::

    from fabulist.api.profiles_api import router as profiles_router
    app.include_router(profiles_router)

Like the world/persona routers, this reads the live
:class:`~fabulist.log.EventLog` from ``request.app.state.log``.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from fabulist.cards import PNG_SIGNATURE
from fabulist.profiles.exporter import export_profile
from fabulist.profiles.image import assigned_image
from fabulist.profiles.importer import import_profile, persist_profile_image
from fabulist.profiles.png import profile_from_png, profile_image_png, profile_to_png

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

profiles_router = APIRouter(tags=["profiles"])

#: Conventional alias (the app factory mounts ``profiles_api.router``).
router = profiles_router


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _filename(name: str, suffix: str = "profile.json") -> str:
    """A safe ``<slug>.<suffix>`` download name (ASCII, no header breakers)."""
    slug = "".join(
        c if (c.isascii() and (c.isalnum() or c in "-_")) else "-" for c in name
    ).strip("-_")
    return f"{slug or 'profile'}.{suffix}"


@profiles_router.get("/api/profiles/export")
async def export_profile_route(
    request: Request,
    world: str | None = Query(None, description="world id to bundle (optional)"),
    preset: str = Query(..., description="preset id to bundle (required)"),
    assistants: int = Query(0, description="1 to include the assistant roster"),
    name: str | None = Query(None, description="profile name (defaults to the world's)"),
) -> JSONResponse:
    """Export a v1 experience-profile envelope as a JSON attachment (D33).

    ``404`` with a precise detail when ``world``/``preset`` names no known
    anchor. Reads only anchor sessions (never a chat).
    """
    log = _log_for(request)
    try:
        envelope = export_profile(
            log,
            name=name,
            world_id=world,
            preset_id=preset,
            include_assistants=bool(assistants),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return JSONResponse(
        envelope,
        headers={"Content-Disposition": f'attachment; filename="{_filename(envelope["name"])}"'},
    )


@profiles_router.get("/api/profiles/export.png")
async def export_profile_png_route(
    request: Request,
    world: str | None = Query(None, description="world id to bundle (optional)"),
    preset: str = Query(..., description="preset id to bundle (required)"),
    assistants: int = Query(0, description="1 to include the assistant roster"),
    name: str | None = Query(None, description="profile name (defaults to the world's)"),
) -> Response:
    """Export the profile envelope as a PNG: image body + JSON in a text chunk.

    The PNG **mirrors** ``GET /api/profiles/export`` — same query params, same
    envelope, different representation (a profile is a derived resource, not a
    stored entity, so there is no per-profile path segment).

    The body is the profile's assigned image, i.e. the **top layer's** image:
    ``world.image_asset``, else the first cast persona in ``world.cast`` order
    whose ``image_asset`` resolves (:mod:`fabulist.profiles.image`). The full
    envelope JSON rides in a ``fabulist_profile`` ``tEXt`` chunk, written by
    the card surface's chunk framing (:mod:`fabulist.profiles.png`).

    ``404`` with a precise detail when ``world``/``preset`` names no known
    anchor, and also when NO layer has a resolvable image — a profile without an
    assigned image is NOT exported as a fabricated carrier PNG (owner ruling:
    404, not a 1x1 placeholder). ``422`` when the assigned image exists but is
    not a PNG (definition images keep their uploaded mime): the chunk cannot be
    written into a JPEG/WebP body, and there is deliberately no transcode path —
    re-attach the image as a PNG instead.
    """
    log = _log_for(request)
    try:
        envelope = export_profile(
            log,
            name=name,
            world_id=world,
            preset_id=preset,
            include_assistants=bool(assistants),
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    image = assigned_image(log, envelope)
    if image is None:
        raise HTTPException(
            status_code=404,
            detail="the profile has no assigned image (no world/cast image_asset resolves)",
        )
    data, mime = image
    if mime != "image/png":
        raise HTTPException(
            status_code=422,
            detail=(
                f"the profile's assigned image is not a PNG (mime {mime!r}); "
                "PNG export reuses the image as-is and has no transcode path"
            ),
        )
    try:
        png = profile_to_png(envelope, data)
    except ValueError as exc:  # CardError: corrupt/truncated assigned image
        raise HTTPException(
            status_code=422,
            detail=f"the profile's assigned image is not a readable PNG: {exc}",
        ) from exc
    return Response(
        content=png,
        media_type="image/png",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{_filename(envelope["name"], "profile.png")}"'
            )
        },
    )


@profiles_router.post("/api/profiles/import")
async def import_profile_route(request: Request) -> dict:
    """Import a v1 experience-profile envelope; return the import result (D33).

    Each entity is superseded only when its canonical signature differs from the
    local fold (idempotent re-import), agent bindings naming unknown local agents
    fall back to :data:`~fabulist.prompts.agent_sets.DEFAULT_BINDINGS`, and a
    preset claiming the default id is imported under a new id. A malformed
    envelope is a ``422``.

    The body is either the envelope as JSON (unchanged) or a profile PNG —
    detected by the PNG signature, not the content type — whose
    ``fabulist_profile`` chunk is decoded and then travels the identical import
    path. A PNG without that chunk (or with a corrupt/unreadable one) is a
    precise ``422``.

    A profile PNG's **body is the profile's assigned image**, so the import also
    stores those bytes as a sessionless definition asset and points the assigned
    layer (world, else first cast member) at the local ref — otherwise the image
    would be lost (the exported envelope carries only the exporting log's
    content address) and the re-exported PNG would 404.
    """
    log = _log_for(request)
    raw = await request.body()
    image_ref: str | None = None
    if raw.startswith(PNG_SIGNATURE):
        try:
            envelope = profile_from_png(raw)
            # The PNG body is the assigned image; persist it before the supersede
            # so the imported refs resolve on this log (D33/G). Stripping the
            # envelope chunk recovers the image's own bytes, so the stored ref is
            # the one the envelope already carries (it stays content-addressed
            # and a JSON re-import of the same envelope remains a no-op).
            image_ref = persist_profile_image(
                log,
                envelope,
                profile_image_png(raw),
                "image/png",
                note=f"profile image: {envelope.get('name', '')}",
            )
        except ValueError as exc:  # CardError/ProfilePngError: corrupt PNG body
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    else:
        try:
            envelope = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"profile body is neither a PNG nor valid JSON: {exc}",
            ) from exc
        if not isinstance(envelope, dict):
            raise HTTPException(
                status_code=422, detail="a profile envelope must be a JSON object"
            )
    try:
        result = import_profile(log, envelope)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if image_ref is not None:
        result["image_asset"] = image_ref
    return result
