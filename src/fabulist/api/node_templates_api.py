"""Node-template router (W9.10, R19/R21): the palette library as an asset.

Node templates are session-independent definitions (AGENTS.md NN7): the editor
can list, create and edit them with NO chat open and no session — the exact
contract of personas/worlds/books. The log owns them (D9): every route derives
from the ``__node_templates__`` anchor events and every write appends one
(:mod:`fabulist.graph.node_templates`).

Endpoints
---------
  GET    /api/node-templates            -> {templates: [...], klasses: [...]}
  POST   /api/node-templates {..}       -> {template}   (id minted when absent)
  PATCH  /api/node-templates/{id} {..}  -> {template} | 404  (partial merge)
  DELETE /api/node-templates/{id}       -> {deleted: id} | 404 | 409 referenced

The delete guard is the interesting one: a template a STORED topology references
is refused (``409`` naming the preset ids) rather than left dangling, because
the topology rows are references — deleting the row would leave a graph the
builder cannot resolve. A refused delete writes nothing.

``GET`` seeds the built-in library first (:func:`ensure_node_templates`) so the
palette is never empty, and ``klasses`` carries the grouped-by-klass order the
palette renders (the vocabulary is
:data:`~fabulist.graph.node_templates.NODE_TEMPLATE_KLASSES`).

Wiring (orchestrator-owned, one line)::

    from fabulist.api.node_templates_api import router as node_templates_router
    app.include_router(node_templates_router)
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from fabulist.graph.contracts import NODE_CONTRACTS
from fabulist.graph.node_templates import (
    NODE_TEMPLATE_KLASSES,
    append_node_template,
    delete_node_template,
    get_node_template,
    list_node_templates,
    referencing_presets,
    template_to_wire,
    update_node_template,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


router = APIRouter(tags=["prompts"])

#: Conventional alias (some mounting code imports ``node_templates_router``).
node_templates_router = router


class NodeTemplateInput(BaseModel):
    """Body of ``POST``/``PATCH /api/node-templates`` — one template definition.

    ``implements`` is REQUIRED on create (it is the behaviour the graph
    executes) and is validated against
    :data:`~fabulist.graph.contracts.NODE_CONTRACTS`: a template that
    implements nothing runnable is a row the builder would refuse. ``id`` is
    optional — a fresh one is minted — and an existing one is a latest-wins
    replace.
    """

    id: str | None = None
    name: str = ""
    klass: str = "model_pass"
    implements: str = ""
    interaction: str | None = None
    step_id: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    description: str = ""


class NodeTemplatePatch(BaseModel):
    """Body of ``PATCH /api/node-templates/{template_id}`` — partial merge.

    Any omitted field keeps the stored value (``exclude_unset``).
    """

    name: str | None = None
    klass: str | None = None
    implements: str | None = None
    interaction: str | None = None
    step_id: str | None = None
    config: dict[str, Any] | None = None
    description: str | None = None


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _validated(body: NodeTemplateInput) -> dict[str, Any]:
    """Project a request body into the store payload; 422 on an unrunnable row.

    ``klass`` must be one of the four primitives and ``implements`` must name a
    canonical node contract — the same acceptance the builder needs, refused at
    the door so a bad template is never stored.
    """
    if not body.name.strip():
        raise HTTPException(status_code=422, detail={"reason": "name must be non-empty"})
    if body.klass not in NODE_TEMPLATE_KLASSES:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": f"unknown klass: {body.klass!r}",
                "known_klasses": list(NODE_TEMPLATE_KLASSES),
            },
        )
    if body.implements not in NODE_CONTRACTS:
        raise HTTPException(
            status_code=422,
            detail={
                "reason": (
                    f"template implements {body.implements!r}, which is not a node "
                    "contract the graph can run"
                ),
                "known_implements": list(NODE_CONTRACTS),
            },
        )
    return {
        "name": body.name.strip(),
        "klass": body.klass,
        "implements": body.implements,
        "interaction": body.interaction or None,
        "step_id": body.step_id or None,
        "config": dict(body.config),
        "description": body.description,
    }


@router.get("/api/node-templates")
async def list_node_templates_route(request: Request) -> dict:
    """The palette library, grouped-ready: rows + the klass order.

    Built-ins are seeded first (idempotent), so the palette has content on a
    fresh install. ``klasses`` is the grouping vocabulary, in the order the
    palette renders it; a template's ``klass`` is always one of them.
    """
    log = _log_for(request)
    return {
        "templates": [template_to_wire(t) for t in list_node_templates(log)],
        "klasses": list(NODE_TEMPLATE_KLASSES),
    }


@router.post("/api/node-templates")
async def create_node_template_route(body: NodeTemplateInput, request: Request) -> dict:
    """Create (or replace, when ``id`` is given) one template."""
    log = _log_for(request)
    payload = _validated(body)
    created = append_node_template(log, payload, template_id=body.id)
    return {"template": template_to_wire(created)}


@router.get("/api/node-templates/{template_id}")
async def get_node_template_route(template_id: str, request: Request) -> dict:
    """Read one template; 404 when the library has no such row."""
    template = get_node_template(_log_for(request), template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="unknown node template")
    return {"template": template_to_wire(template)}


@router.patch("/api/node-templates/{template_id}")
async def patch_node_template_route(
    template_id: str, body: NodeTemplatePatch, request: Request
) -> dict:
    """Merge the supplied fields over one template; 404/422 as documented."""
    log = _log_for(request)
    existing = get_node_template(log, template_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="unknown node template")
    patch = body.model_dump(exclude_unset=True)
    merged = NodeTemplateInput(
        **{**template_to_wire(existing), **{k: v for k, v in patch.items() if v is not None}}
    )
    try:
        updated = update_node_template(log, template_id, _validated(merged))
    except LookupError:
        raise HTTPException(status_code=404, detail="unknown node template") from None
    except ValueError as exc:
        raise HTTPException(status_code=422, detail={"reason": str(exc)}) from exc
    return {"template": template_to_wire(updated)}


@router.delete("/api/node-templates/{template_id}")
async def delete_node_template_route(template_id: str, request: Request) -> dict:
    """Tombstone one template; 409 while a stored topology references it.

    The refusal names the presets that use it, so the owner knows what to rewire
    first. Nothing is written on a refusal.
    """
    log = _log_for(request)
    if get_node_template(log, template_id) is None:
        raise HTTPException(status_code=404, detail="unknown node template")
    referenced = referencing_presets(log, template_id)
    if referenced:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": (
                    f"node template {template_id!r} is referenced by a stored "
                    "topology; rewire it before deleting"
                ),
                "presets": referenced,
            },
        )
    delete_node_template(log, template_id)
    return {"deleted": template_id}
