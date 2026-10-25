"""Card export router (SURFACE #13): session card -> V2 JSON or PNG download.

The log owns the card (import appends it verbatim); export is a projection
back to the V2 envelope. Import/export together close the data-freedom loop —
cards can leave this engine and return to ST without hand-editing.

  GET /api/sessions/{session_id}/export/card      -> chara_card_v2 JSON
  GET /api/sessions/{session_id}/export/card.png  -> PNG with the card in its
                                                     ``chara`` tEXt chunk

The PNG route rewraps the session's card image (the ``card_asset`` ref
recorded by the cards router on a PNG import, D28 — a pointer into the
sessionless definition store) so the exported file keeps the character's art;
it rewrites only the ``chara`` chunk with the *current* card dict. A card with
no stored image (JSON import) exports onto a synthesized minimal carrier PNG.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from fabulist.api.attachments import _session_exists
from fabulist.cards.export import card_to_png, card_to_v2
from fabulist.media.images import find_asset, find_definition_asset

router = APIRouter(tags=["cards"])


def _latest_card(log: Any, session_id: str) -> tuple[dict | None, str | None]:
    """The session's latest imported card dict and its ``card_asset`` ref.

    Latest card wins: a session may have been re-imported, and the most
    recently appended card system event is the current truth (D9). The image
    ref is the *latest one seen* rather than the latest event's — an edit event
    that carries no new image must keep the last imported art. Returns
    ``(None, None)`` when the session carries no card.
    """
    card: dict | None = None
    card_asset: str | None = None
    for event in log.get_events(session_id):
        if event.type == "system" and isinstance(event.payload.get("card"), dict):
            card = event.payload["card"]
            asset = event.payload.get("card_asset")
            if isinstance(asset, str) and asset:
                card_asset = asset
    return card, card_asset


def _stored_card_image(log: Any, session_id: str, card_asset: str | None) -> bytes | None:
    """Decode the stored card image bytes for ``card_asset``, or ``None``.

    The ref is resolved through the session's own assets first, then through the
    sessionless DEFINITION store: a card import keeps its artwork ONCE, as the
    persona's ``image_asset`` (a card-import now stores no session copy), while
    sessions imported before that change still carry the old session-scoped
    asset — so both old and new logs export their art. A missing/undecodable
    asset degrades to ``None`` (the caller synthesizes a carrier) rather than
    failing the export.
    """
    if not card_asset:
        return None
    asset = find_asset(log, session_id, card_asset) or find_definition_asset(
        log, card_asset
    )
    if asset is None:
        return None
    encoded = asset.get("payload", {}).get("bytes_b64")
    if not encoded:
        return None
    try:
        return base64.b64decode(encoded)
    except (binascii.Error, ValueError):
        return None


def _png_filename(name: Any) -> str:
    """A safe ``<name>.png`` download name (ASCII-only, no header breakers)."""
    text = name if isinstance(name, str) else ""
    safe = "".join(
        c if (c.isascii() and (c.isalnum() or c in "-_")) else "_" for c in text
    ).strip("_")
    return f"{safe or 'card'}.png"


@router.get("/api/sessions/{session_id}/export/card")
async def export_card(session_id: str, request: Request) -> JSONResponse:
    """Return the session's (latest) imported card as a chara_card_v2 JSON."""
    log = request.app.state.log
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    card = None
    for event in log.get_events(session_id):
        if event.type == "system" and isinstance(event.payload.get("card"), dict):
            card = event.payload["card"]  # latest import wins
    if card is None:
        raise HTTPException(status_code=404, detail="session has no imported card")
    return JSONResponse(card_to_v2(card), headers={"Content-Disposition": 'attachment; filename="card.json"'})


@router.get("/api/sessions/{session_id}/export/card.png")
async def export_card_png(session_id: str, request: Request) -> Response:
    """Return the session's card as a PNG with the card in its ``chara`` chunk.

    The current card dict is rendered through :func:`card_to_v2`; the session's
    stored card image (if any) is rewrapped, otherwise a minimal carrier PNG is
    synthesized. Served as a downloadable ``image/png``.
    """
    log = request.app.state.log
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    card, card_asset = _latest_card(log, session_id)
    if card is None:
        raise HTTPException(status_code=404, detail="session has no imported card")
    png = card_to_png(card, _stored_card_image(log, session_id, card_asset))
    filename = _png_filename(card.get("name"))
    return Response(
        content=png,
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
