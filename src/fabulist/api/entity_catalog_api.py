"""Entity-catalog router (W9.29 §4): the BTS attribute universe as an asset.

Thin transport over :mod:`fabulist.state.entity_catalog`. The log owns the
catalog (D9): it is one ``system`` event on the reserved ``__entity_catalog__``
anchor session — this router never writes a side table, and no chat/session is
involved (non-negotiable 7: asset CRUD works with no chat open).

Endpoints
---------
  GET /api/prompts/entity-catalog  -> {"rows": [row, ...]}
  PUT /api/prompts/entity-catalog  {"rows": [row, ...]}
                                   -> {"rows": [row, ...]} | 400 {"detail": "<message>"}

The GET serves the EFFECTIVE catalog: the stored asset when the owner has saved
one, the built-in code seed (:data:`fabulist.state.entity_catalog.DEFAULT_ROWS`)
when the anchor holds nothing. That is the same read the tracker's tool door
makes (:func:`~fabulist.state.entity_catalog.catalog_for`), so the pane shows
exactly the keys ``set_attr`` accepts — and the first PUT stores the seed as the
owner's own.

PUT is REPLACE semantics (the whole catalog, latest wins — the decision bank's
contract): the body is ``{"rows": [...]}``, an empty list is a legal write (the
engine then falls back to the code seed, exactly as before the asset existed),
and any invalid row is a ``400`` whose message names the row by index
(:func:`~fabulist.state.entity_catalog.validate_rows`).

Wiring (the ``app.state`` seam needs no signature change)::

    from fabulist.api.entity_catalog_api import entity_catalog_router
    app.include_router(entity_catalog_router)

Like :mod:`fabulist.api.decision_queries_api`, the router reads the live
:class:`~fabulist.log.EventLog` from ``request.app.state.log``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request

from fabulist.state.entity_catalog import catalog_for, replace_rows

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


entity_catalog_router = APIRouter(tags=["entity-catalog"])

#: Conventional alias (some mounting code imports ``router``).
router = entity_catalog_router

#: The asset path. It hangs off ``/api/prompts`` because the catalog IS a prompt
#: asset (the tracker's attribute universe), not session state.
ENTITY_CATALOG_PATH = "/api/prompts/entity-catalog"


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


@entity_catalog_router.get(ENTITY_CATALOG_PATH)
async def get_entity_catalog(request: Request) -> dict:
    """The effective catalog (the code seed while the owner has saved nothing)."""
    return {"rows": catalog_for(_log_for(request))}


@entity_catalog_router.put(ENTITY_CATALOG_PATH)
async def put_entity_catalog(request: Request, body: dict) -> dict:
    """Replace the whole catalog; ``400`` when any row fails validation.

    Every rule lives in :func:`fabulist.state.entity_catalog.validate_rows` (the
    same one the fold applies) — the router owns no second schema.
    """
    log = _log_for(request)
    rows = body.get("rows")
    if not isinstance(rows, list):
        raise HTTPException(status_code=400, detail="'rows' must be a list")
    try:
        replace_rows(log, rows)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"rows": catalog_for(log)}
