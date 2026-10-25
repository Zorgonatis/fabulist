"""Turn-topology router (W9.5, R23) — the validated write door for the graph.

The turn topology is preset data (R10) and the editor rewires it, so this router
is the type checker at the door: ``PUT`` refuses a wiring that breaks an
invariant instead of writing it, and ``GET`` serves what is currently carried
plus the canonical wiring and the violations.

Endpoints
---------
  GET /api/prompts/topology -> {"topology", "canonical", "violations"}
  PUT /api/prompts/topology {"topology": {...}}
                            -> {"topology", "violations": []}
                               | 400 {"detail": "<message>", "violations": [...]}

Both routes target the persisted **default** preset (D26): it is the row the
engine's default resolution reads, and the topology rides inside it as preset
data — the write is one ``preset`` append on ``__presets__`` (D9), no side
table. A future per-preset topology editor reuses
:func:`fabulist.prompts.preset_store.set_default_topology` with another id.

Read asymmetry (deliberate)
---------------------------
``GET`` serves the topology the preset CARRIES
(:func:`~fabulist.prompts.preset_store.stored_topology`), not the engine's
effective one, so a hand-edited/broken value is visible (with its violations)
and can be repaired. The engine's own read
(:func:`~fabulist.prompts.preset_store.preset_topology`) validates the same
value and falls back to :data:`~fabulist.graph.graph.CANONICAL_TOPOLOGY` on
any violation — which is why a preset whose ``violations`` are non-empty is a
graph the engine is NOT running until it is fixed.

Wiring (orchestrator-owned, one line)::

    from fabulist.api.topology_api import router as topology_router
    app.include_router(topology_router)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from fabulist.graph.contracts import validate_topology
from fabulist.graph.node_templates import template_behaviours
from fabulist.prompts.preset_store import (
    DEFAULT_PRESET_ID,
    canonical_topology,
    get_preset,
    set_default_topology,
    stored_topology,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


router = APIRouter(tags=["prompts"])

#: Conventional alias (some mounting code imports ``topology_router``).
topology_router = router


class TopologyInput(BaseModel):
    """Body of ``PUT /api/prompts/topology`` — the wiring to persist.

    ``topology`` is the same shape the preset carries
    (``{"nodes": [{"id", ...}], "edges": [{"from", "to", ...}]}``), so what a
    client sends back is what a preset export shows.
    """

    topology: dict[str, Any]


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _carried_topology(log: "EventLog") -> dict[str, Any]:
    """The default row's own topology, else the canonical wiring (pre-boot)."""
    return stored_topology(get_preset(log, DEFAULT_PRESET_ID)) or canonical_topology()


@router.get("/api/prompts/topology")
async def get_topology_route(request: Request) -> dict:
    """The carried topology, the canonical one, and the carried one's violations.

    ``canonical`` is the code-authored reference (never editable) so the editor
    can offer a reset-to-canonical; ``violations`` is empty for a legal graph.
    A node row naming a node TEMPLATE (W9.10) is judged by the behaviour that
    template implements, so a graph assembled from the palette reads as legal.
    """
    log = _log_for(request)
    topology = _carried_topology(log)
    return {
        "topology": topology,
        "canonical": canonical_topology(),
        "violations": validate_topology(topology, template_behaviours(log)),
    }


@router.put("/api/prompts/topology")
async def put_topology_route(body: TopologyInput, request: Request) -> dict:
    """Validate then persist the topology onto the default preset.

    ``400`` with the violations when the wiring breaks an invariant — nothing is
    written then (the log's ``__presets__`` events are unchanged), so the stored
    graph stays the last legal one. A legal topology is appended as one ``preset``
    event and read back, so the response is what a later GET will serve.
    """
    log = _log_for(request)
    violations = validate_topology(body.topology, template_behaviours(log))
    if violations:
        return JSONResponse(
            status_code=400,
            content={
                "detail": (
                    "topology rejected: "
                    + ", ".join(dict.fromkeys(v["code"] for v in violations))
                ),
                "violations": violations,
            },
        )
    row = set_default_topology(log, body.topology)
    return {"topology": stored_topology(row), "violations": []}
