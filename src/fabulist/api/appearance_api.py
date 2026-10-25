"""Appearance surface router (issue #12 + #13, ADR-027): preset, CSS, wallpaper.

Four first-party presets (``light`` / ``dark`` / ``oled`` / ``savilen``), selected by a setting,
plus exactly TWO customisation surfaces: a stylesheet the reader pastes, applied
as a single ``<style>`` element appended last in ``<head>``, and ONE image — the
wallpaper issue #13 puts behind the prose. No theme editor, no per-component
settings — that was ruled out on the issue.

Where the state lives
---------------------
``data/appearance.json`` (:mod:`fabulist.theme.appearance`) plus, when a
wallpaper is set, ``data/appearance/wallpaper.<ext>`` (:mod:`fabulist.theme.wallpaper`)
— user state, NOT the event log. A theme is not narrative history: it must not
fork with a branch, it is not replayable, and a slider move must never append a
system event to a session's audit trail. This is the ADR-027 carve-out next to
ADR-001's log ownership.

  GET    /api/appearance            -> preset + custom_css + wallpaper + align +
                                       the preset and alignment catalogs, the
                                       guard's rules, and the wallpaper's limits
  PATCH  /api/appearance {preset?, custom_css?, fit?, align?}
                                     -> the same shape, plus `dropped`: every
                                        declaration the guard refused, with a reason
  POST   /api/appearance/wallpaper   multipart `file` -> store the image, answer
                                     the new state (422 with a reason on refusal)
  GET    /api/appearance/wallpaper   the stored bytes, `image/*` + nosniff
  DELETE /api/appearance/wallpaper   remove the image, answer the new state

Every write is validated atomically before anything is stored: an unknown preset
id, a stylesheet past the size cap, a wallpaper that is not PNG/JPEG/GIF/WebP, or
one over 8 MB / 4096px an edge is a ``422`` with the reason in ``detail``, and
nothing on disk changes.

Why the wallpaper is a field here and not its own endpoint family
-------------------------------------------------------------------
A wallpaper IS an appearance: it belongs in the same document (so "which install"
and "which preset" can never disagree), it is re-solved by the same token layer
(``:root[data-wallpaper]`` in app.css), and the drawer that picks it is the
drawer that picks the preset. The three wallpaper routes exist only because
bytes need a door — the STATE is still one document, one read, one patch.

Precedence (owner ruling D-D) — this router is where it is WRITTEN DOWN
----------------------------------------------------------------------
    base (app.css :root) -> preset (data-theme) -> user CSS -> session tone

The tone step is *inline* custom properties on ``<html>``, set by the chat page
from ``GET /api/sessions/{id}/theme``; inline beats any stylesheet rule without
``!important``, so the fiction's palette outranks the reader's furniture. The
guard therefore KEEPS ``!important`` — it is the documented way for a reader to
pin a hue — and the drawer says so in one sentence.

What a pasted stylesheet may and may not do is enforced in
:mod:`fabulist.theme.custom_css` (an allowlist, so a drop is mechanical rather
than advisory) and echoed to the UI in the ``guard`` block of both responses.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel

from fabulist.theme.appearance import (
    ALIGN_LABELS,
    ALIGNMENTS,
    MAX_CUSTOM_CSS_CHARS,
    PRESET_LABELS,
    PRESET_META,
    PRESETS,
    AppearanceError,
    AppearanceStore,
    dropped_wire,
)
from fabulist.theme.custom_css import (
    ALLOWED_PROPERTIES,
    BLOCKED_PROPERTY_FAMILIES,
    MIN_FONT_PX,
    RESERVED_CUSTOM_PROPERTY_PREFIXES,
)
from fabulist.theme.wallpaper import (
    DEFAULT_FIT,
    FITS,
    FORMATS,
    MAX_WALLPAPER_BYTES,
    MAX_WALLPAPER_EDGE,
    WallpaperError,
)

router = APIRouter(tags=["appearance"])

#: One-sentence note the drawer shows under the paste box (owner ruling D-D).
IMPORTANT_NOTE = (
    "The scene's tone colours are applied inline and outrank this box — add "
    "!important to a declaration if you want to pin that colour regardless."
)


def _store(request: Request) -> AppearanceStore:
    """The app's appearance store, created on first use.

    The app factory does not need a line for this: the store is a tiny file
    handle, and tests (or a container) can pre-install one by setting
    ``app.state.appearance`` — that is also how a temporary ``data/`` path is
    injected without touching the process environment.
    """
    store = getattr(request.app.state, "appearance", None)
    if store is None:
        store = AppearanceStore()
        request.app.state.appearance = store
    return store


def _guard_wire() -> dict[str, Any]:
    """The guard's rules, as the drawer renders them (enforced vs advisory)."""
    return {
        "allowed": sorted(ALLOWED_PROPERTIES),
        "blocked": dict(BLOCKED_PROPERTY_FAMILIES),
        "reserved_custom_properties": list(RESERVED_CUSTOM_PROPERTY_PREFIXES),
        "min_font_px": MIN_FONT_PX,
        "max_custom_css_chars": MAX_CUSTOM_CSS_CHARS,
        "important_note": IMPORTANT_NOTE,
    }


def _presets_wire() -> list[dict[str, str]]:
    """The preset catalog in presentation order (light, dark, oled, savilen).

    ``PRESETS`` is derived from ``PRESET_META`` by the issue #37 sort key — the
    greyscale themes first (white -> black), then coloured themes in RGB order —
    so this wire never needs a second, hand-maintained ordering.
    """
    return [
        {
            "id": preset,
            "label": PRESET_LABELS[preset],
            "color_scheme": str(PRESET_META[preset]["scheme"]),
        }
        for preset in PRESETS
    ]


def _alignments_wire() -> list[dict[str, str]]:
    """The alignment catalog for the segmented control (issue #41).

    Shipped like the preset catalog so the drawer renders labels it did not
    author: one row per :data:`ALIGNMENTS` value, in left -> center -> right
    order. ``center`` is the default and the current look.
    """
    return [{"id": align, "label": ALIGN_LABELS[align]} for align in ALIGNMENTS]


#: The wallpaper's own route (the bytes' door — one path, so the drawer, the
#: cached URL and the docs cannot drift apart).
WALLPAPER_ROUTE = "/api/appearance/wallpaper"


def _wallpaper_limits_wire() -> dict[str, Any]:
    """The caps the drawer states before an upload is even attempted.

    Shipped from the backend rather than written in the component for the same
    reason the guard's rules are: one constant, one sentence, no drift between
    what the UI promises and what the upload door enforces.
    """
    return {
        "max_bytes": MAX_WALLPAPER_BYTES,
        "max_bytes_label": f"{MAX_WALLPAPER_BYTES // (1024 * 1024)} MB",
        "max_edge": MAX_WALLPAPER_EDGE,
        "formats": [
            {"label": item.label, "mime": item.mime} for item in FORMATS
        ],
        "fits": list(FITS),
        "default_fit": DEFAULT_FIT,
    }


def _wallpaper_wire(store: AppearanceStore) -> dict[str, Any] | None:
    """The set wallpaper, or ``None`` (the shipped default is "none").

    ``url`` is a PATH, not an absolute URL: the frontend prefixes ``API_BASE``
    (the shell's one rule for reaching the engine). ``version`` is the file's
    mtime in nanoseconds, so a re-upload of the same format busts the browser
    cache without hashing megabytes on every read.
    """
    path = store.wallpaper_file()
    info = store.wallpaper_info()
    if path is None or info is None:
        return None
    try:
        version = path.stat().st_mtime_ns
    except OSError:  # pragma: no cover - the file vanished between the two calls
        return None
    return {
        "name": path.name,
        "url": WALLPAPER_ROUTE,
        "version": str(version),
        "mime": info.format.mime,
        "format": info.format.label,
        "bytes": info.size,
        "width": info.width,
        "height": info.height,
        "fit": store.read().fit,
    }


def _state_wire(store: AppearanceStore) -> dict[str, Any]:
    """The full GET/PATCH body (state + catalogs + guard + wallpaper)."""
    appearance = store.read()
    return {
        "preset": appearance.preset,
        "custom_css": appearance.custom_css,
        "wallpaper": _wallpaper_wire(store),
        "fit": appearance.fit,
        "align": appearance.align,
        "alignments": _alignments_wire(),
        "fits": list(FITS),
        "wallpaper_limits": _wallpaper_limits_wire(),
        "presets": _presets_wire(),
        "guard": _guard_wire(),
    }


class AppearancePatch(BaseModel):
    """Body of ``PATCH /api/appearance``.

    Every field is optional and only the ones PRESENT are applied
    (``model_fields_set``), so the drawer can change the preset without
    re-sending a 20 KB stylesheet or re-uploading an image. ``custom_css=""``
    clears the override; the wallpaper is not cleared here — a wallpaper is bytes,
    and ``DELETE /api/appearance/wallpaper`` is its door.
    """

    preset: str | None = None
    custom_css: str | None = None
    fit: str | None = None
    align: str | None = None


@router.get("/api/appearance")
async def get_appearance(request: Request) -> dict[str, Any]:
    """The current preset, guarded user CSS and wallpaper (plus the catalogs).

    Always answers: a first-run install with no ``data/appearance.json`` gets the
    shipped defaults (dark, no override, no wallpaper), exactly what ``:root``
    already renders — ``wallpaper: null`` is the "none" the drawer shows as an
    unpressed button, not an error state.
    """
    return _state_wire(_store(request))


@router.patch("/api/appearance")
async def patch_appearance(body: AppearancePatch, request: Request) -> dict[str, Any]:
    """Store the preset, the pasted stylesheet and/or the wallpaper's fit.

    ``custom_css`` is reduced to what the layout contract allows before it is
    stored (the file holds exactly what the browser is handed), and every refused
    declaration comes back in ``dropped`` so the drawer can show it. A rejected
    write (unknown preset, oversized CSS, unknown fit, unknown alignment) is a
    ``422`` with nothing written. The image itself never travels through this body
    — bytes go through ``POST /api/appearance/wallpaper``.
    """
    store = _store(request)
    fields = body.model_fields_set
    if not fields:
        raise HTTPException(status_code=422, detail="no appearance fields supplied")
    preset = body.preset if "preset" in fields else None
    custom_css = body.custom_css if "custom_css" in fields else None
    fit = body.fit if "fit" in fields else None
    align = body.align if "align" in fields else None
    try:
        _appearance, guarded = store.patch(
            preset=preset, custom_css=custom_css, fit=fit, align=align
        )
    except AppearanceError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    state = _state_wire(store)
    state["dropped"] = dropped_wire(guarded.dropped) if guarded is not None else []
    return state


@router.post("/api/appearance/wallpaper")
async def upload_wallpaper(
    request: Request,
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """Store the reader's wallpaper; answer the whole appearance state.

    The read is capped at the byte limit + 1, so a 200 MB upload is refused after
    one bounded read instead of being buffered to be measured. Every refusal
    (format, dimensions, bytes) comes back as a ``422`` carrying the reason the
    drawer shows — and because validation runs before any write, a refusal leaves
    the previous wallpaper exactly where it was.
    """
    store = _store(request)
    data = await file.read(MAX_WALLPAPER_BYTES + 1)
    try:
        store.save_wallpaper(data)
    except WallpaperError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _state_wire(store)


@router.get("/api/appearance/wallpaper")
async def get_wallpaper(request: Request) -> Response:
    """Serve the stored wallpaper's bytes (the one thing the drawer does not need).

    ``no-store`` is deliberately NOT used: the URL carries the file's mtime as
    ``?v=``, so the image can be cached immutably and a re-upload still lands. A
    missing image is a ``404`` — the drawer treats that as "no wallpaper", the
    same reading as a cleared field.
    """
    store = _store(request)
    path = store.wallpaper_file()
    info = store.wallpaper_info()
    if path is None or info is None:
        raise HTTPException(status_code=404, detail="no wallpaper is set")
    try:
        data = path.read_bytes()
    except OSError as exc:  # pragma: no cover - the file vanished under the read
        raise HTTPException(status_code=404, detail="no wallpaper is set") from exc
    return Response(
        content=data,
        media_type=info.format.mime,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/api/appearance/wallpaper")
async def delete_wallpaper(request: Request) -> dict[str, Any]:
    """Remove the stored image and clear the field; answer the appearance state."""
    store = _store(request)
    store.remove_wallpaper()
    return _state_wire(store)
