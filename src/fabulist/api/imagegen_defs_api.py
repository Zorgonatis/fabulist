"""Imagegen definitions router (COMFY-PLAN Wave 4, lane V2).

Definitions-only, session-free CRUD (non-negotiable 7): a Studio definition
(workflow + prompt template) is edited with NO chat open and no session
required. The log owns the definition (D9) — rows fold the reserved
``__imagegen__`` anchor session (see :mod:`fabulist.imagegen.registry`):

  GET    /api/imagegen/definitions            -> {"definitions": [wire, ...]}
  POST   /api/imagegen/definitions {name, workflow} -> {"definition": wire}
  DELETE /api/imagegen/definitions/{id}       -> {"imagegen_id", "deleted"} | 404

``POST`` accepts the workflow as raw JSON text (the Studio textarea contents) or
as an already-decoded object, validates that it parses and contains at least one
sampler node (``422`` with the reason otherwise), and echoes the folded wire
form. An explicit ``imagegen_id`` updates in place (latest-wins append).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from fabulist.imagegen.registry import (
    append_imagegen,
    delete_imagegen,
    imagegen_by_id,
    imagegen_to_wire,
    list_imagegen,
    parse_workflow,
    validate_workflow,
    wire_to_imagegen,
)
from fabulist.log import EventLog

router = APIRouter(tags=["imagegen"])

#: Conventional alias (the app factory mounts ``imagegen_defs_api.router``).
imagegen_router = router


class ImageGenDefinitionBody(BaseModel):
    """Body of ``POST /api/imagegen/definitions`` — a definition to persist.

    ``name`` and ``workflow`` are required; ``workflow`` is the ComfyUI
    API-format JSON, accepted as either a JSON ``str`` or a decoded object. An
    explicit ``imagegen_id`` updates in place (latest-wins). ``prompt_template``
    is the user-supplied generator system prompt.
    """

    name: str
    workflow: Any
    imagegen_id: str | None = None
    prompt_template: str = ""
    provenance: dict = {}


def _log_for(request: Request) -> EventLog:
    return request.app.state.log


def _persist_definition(log: EventLog, body: ImageGenDefinitionBody) -> dict:
    """Validate + append a definition and echo the folded wire form.

    ``422`` when ``name`` is empty/whitespace, the workflow text does not parse
    to a JSON object, or the object contains no sampler node. An existing
    ``imagegen_id`` updates in place (latest-wins, D9/D10).
    """
    if not body.name.strip():
        raise HTTPException(status_code=422, detail="name must be non-empty")
    try:
        workflow = parse_workflow(body.workflow)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        validate_workflow(workflow)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        wire = imagegen_to_wire(
            wire_to_imagegen({**body.model_dump(), "workflow": workflow})
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    imagegen_id = append_imagegen(log, wire)
    definition = imagegen_by_id(log, imagegen_id)
    if definition is None:  # pragma: no cover - the append just landed
        raise HTTPException(status_code=500, detail="definition append did not persist")
    return imagegen_to_wire(definition)


@router.get("/api/imagegen/definitions")
async def get_definitions(request: Request) -> dict:
    """List all imagegen definitions folded from the ``__imagegen__`` anchor."""
    return {
        "definitions": [
            imagegen_to_wire(definition)
            for definition in list_imagegen(_log_for(request))
        ]
    }


@router.post("/api/imagegen/definitions")
async def create_definition(body: ImageGenDefinitionBody, request: Request) -> dict:
    """Append a definition (create or update) and echo it back.

    ``422`` when ``name`` is empty/whitespace, the workflow does not parse, or it
    contains no sampler node; an existing ``imagegen_id`` updates in place.
    """
    return {"definition": _persist_definition(_log_for(request), body)}


@router.delete("/api/imagegen/definitions/{imagegen_id}")
async def remove_definition(imagegen_id: str, request: Request) -> dict:
    """Tombstone a definition; ``404`` when unknown. Rows survive (D9)."""
    log = _log_for(request)
    if imagegen_by_id(log, imagegen_id) is None:
        raise HTTPException(status_code=404, detail="unknown imagegen definition")
    delete_imagegen(log, imagegen_id)
    return {"imagegen_id": imagegen_id, "deleted": True}
