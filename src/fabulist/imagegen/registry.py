"""Imagegen definition registry (COMFY-PLAN Wave 4, lane V2).

A **definition** is a nameable, reusable image-generation asset: a ComfyUI
API-format ``workflow`` plus the user-supplied ``prompt_template`` the generator
agent renders a request through, plus a parsed ``param_map`` that points the
adjustables (seed / positive / negative / resolution) at the workflow nodes that
own them. It carries no chat or session state (non-negotiable 7: definitions are
edited by plain CRUD with no chat open) — exactly the shape of
:mod:`fabulist.context.books`, on its own reserved anchor.

The event log owns everything (D9), so definitions are **system events on a
reserved anchor session** (:data:`IMAGEGEN_ANCHOR`), folded latest-wins per
``imagegen_id``, mirroring :mod:`fabulist.context.books` and
:mod:`fabulist.personas.registry`.

Event contract (fixed, shared with the engine / frontend lanes):

* Imagegen definition — a ``system`` event on :data:`IMAGEGEN_ANCHOR`::

      {"event": "imagegen_definition", "imagegen_id": str, "name": str,
       "workflow": dict, "prompt_template": str, "param_map": dict,
       "provenance": {"created_at": iso}}

  ``workflow`` is the ComfyUI API-format JSON *verbatim* — ``{node_id: {inputs,
  class_type}}`` — so the engine applies overrides onto a copy without a lossy
  reshuffle. ``param_map`` is :func:`detect_parameters`'s pointer map (below);
  it is stored for audit but recomputed from the workflow when absent, so an
  older row still folds to a usable map. Create and update both append; the
  newest row per ``imagegen_id`` wins.

* Tombstone — a ``system`` event on :data:`IMAGEGEN_ANCHOR`::

      {"event": "imagegen_definition_deleted", "imagegen_id": str}

  Removes the id from the fold (append-only log, D9: nothing is destroyed).

Parameter mapping (the classes the engine and the Studio preview both mirror)
---------------------------------------------------------------------------
ComfyUI JSON is graph-shaped: a sampler's ``positive`` / ``negative`` inputs are
links (``[node_id, output_index]``) to the ``CLIPTextEncode`` nodes that hold the
prompt text. :func:`detect_parameters` walks those links and returns::

    {"seed":       {"node_id": str, "input": "seed" | "noise_seed"},
     "positive":   {"node_id": str, "input": str},
     "negative":   {"node_id": str, "input": str},
     "resolution": {"node_id": str, "width_input": str, "height_input": str}}

Only mappable fields appear; an unmappable field is simply absent, which the
frontend renders as "not adjustable" — never a broken run.

Public API
----------
:data:`IMAGEGEN_ANCHOR`, :data:`SAMPLER_CLASSES`, :class:`ImageGenDefinition`,
:func:`parse_workflow`, :func:`validate_workflow`, :func:`detect_parameters`,
:func:`append_imagegen`, :func:`delete_imagegen`, :func:`list_imagegen`,
:func:`imagegen_by_id`, :func:`imagegen_to_wire`, :func:`wire_to_imagegen`.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog


__all__ = [
    "IMAGEGEN_ANCHOR",
    "IMAGEGEN_DELETED_EVENT",
    "IMAGEGEN_EVENT",
    "SAMPLER_CLASSES",
    "ImageGenDefinition",
    "append_imagegen",
    "delete_imagegen",
    "detect_parameters",
    "imagegen_by_id",
    "imagegen_to_wire",
    "list_imagegen",
    "parse_workflow",
    "validate_workflow",
    "wire_to_imagegen",
]

#: Reserved session id anchoring imagegen definition events (like
#: ``__books__`` / ``__personas__``). Its ``__`` prefix keeps it out of the
#: session list (:func:`fabulist.api.sessions._session_ids`).
IMAGEGEN_ANCHOR = "__imagegen__"

#: System-event kind carrying one imagegen definition (per id, latest wins).
IMAGEGEN_EVENT = "imagegen_definition"

#: System-event kind carrying a tombstone (removes the id from the fold).
IMAGEGEN_DELETED_EVENT = "imagegen_definition_deleted"

#: Actor stamped on human-intent appends (mirrors the books/personas lanes).
USER_ACTOR = "user"

#: ComfyUI sampler node classes :func:`detect_parameters` maps the seed on
#: (RES4LYF's ``ClownsharKSampler_Beta`` uses ``seed`` like ``KSampler``).
SAMPLER_CLASSES = frozenset({"KSampler", "KSamplerAdvanced", "ClownsharKSampler_Beta"})

#: Node class that carries the positive / negative prompt text.
TEXT_ENCODE_CLASS = "CLIPTextEncode"

#: Node class that carries the latent width/height.
LATENT_CLASS = "EmptyLatentImage"


@dataclass(frozen=True)
class ImageGenDefinition:
    """One imagegen definition folded from the ``__imagegen__`` anchor.

    ``workflow`` is the raw ComfyUI API-format JSON; ``param_map`` points the
    adjustables at nodes/inputs in it; ``provenance`` is free-form origin
    metadata and defaults to ``{"created_at": ...}`` on append.
    """

    imagegen_id: str
    name: str
    workflow: dict = field(default_factory=dict)
    prompt_template: str = ""
    param_map: dict = field(default_factory=dict)
    provenance: dict = field(default_factory=dict)


def _now() -> str:
    """UTC ISO-8601 timestamp (mirrors :func:`fabulist.log.repo._now`)."""
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any) -> str:
    """Coerce an optional field to a plain string (``None`` -> ``""``)."""
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


# -- workflow parsing + validation -------------------------------------------


def parse_workflow(raw: Any) -> dict:
    """Return the workflow as an object from raw JSON text *or* a dict.

    Accepts the Studio's textarea contents verbatim (``str``) or an already
    decoded object. Raises :class:`ValueError` with a human-readable reason when
    the text is blank, is not valid JSON, or does not decode to a JSON object —
    so the API door can surface a precise ``422``.
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise ValueError("workflow JSON is empty")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"workflow is not valid JSON: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise ValueError("workflow must be a JSON object mapping node ids to nodes")
    return raw


def _nodes(workflow: dict) -> list[tuple[str, dict]]:
    """``(node_id, node)`` pairs for the node objects in a workflow (order kept)."""
    return [
        (str(node_id), node)
        for node_id, node in workflow.items()
        if isinstance(node, dict)
    ]


def _class_of(node: dict) -> str:
    """The node's ComfyUI ``class_type`` (``""`` when absent/not a string)."""
    class_type = node.get("class_type")
    return class_type if isinstance(class_type, str) else ""


def _inputs_of(node: dict) -> dict:
    """The node's ``inputs`` mapping (``{}`` when absent/not a dict)."""
    inputs = node.get("inputs")
    return inputs if isinstance(inputs, dict) else {}


def find_samplers(workflow: dict) -> list[tuple[str, dict]]:
    """Every sampler node (``KSampler`` / ``KSamplerAdvanced``) in the workflow."""
    return [
        (node_id, node)
        for node_id, node in _nodes(workflow)
        if _class_of(node) in SAMPLER_CLASSES
    ]


def validate_workflow(workflow: dict) -> None:
    """Raise :class:`ValueError` when the workflow has no sampler node.

    A ComfyUI graph that cannot sample cannot generate; requiring at least one
    :data:`SAMPLER_CLASSES` node keeps a random JSON blob out of the catalog
    (the API turns the message into a ``422``).
    """
    if not find_samplers(workflow):
        raise ValueError(
            "workflow has no sampler node (expected KSampler, KSamplerAdvanced, "
            "or ClownsharKSampler_Beta)"
        )


def _link_node_id(value: Any) -> str | None:
    """Resolve a ComfyUI input link (``[node_id, output_index]``) to its node id."""
    if isinstance(value, (list, tuple)) and value:
        head = value[0]
        if isinstance(head, (str, int)):
            return str(head)
    return None


def _text_target(workflow: dict, inputs: dict, key: str) -> dict | None:
    """Map a sampler's ``positive``/``negative`` link to its text node's ``text``."""
    node_id = _link_node_id(inputs.get(key))
    if node_id is None:
        return None
    target = workflow.get(node_id)
    if not isinstance(target, dict) or _class_of(target) != TEXT_ENCODE_CLASS:
        return None
    target_inputs = _inputs_of(target)
    if not isinstance(target_inputs.get("text"), str):
        return None
    return {"node_id": node_id, "input": "text"}


def detect_parameters(workflow: dict) -> dict:
    """Parameter map for a workflow, in the DEFINITION-WIRE shape.

    Deliberately separate from :func:`fabulist.comfy.workflow.parse_workflow`:
    the engine's mapper is submit-time internal (it carries every sampler it can
    patch, as ``(node_id, input)`` pairs), while this one is the display/wire
    contract the Studio pane and the frontend client consume
    (``{node_id, input}`` per parameter). The two were unified once and the
    tests caught the shape loss; keep them separate and let each serve its
    consumer.
    """

    """Map the adjustables (seed / positive / negative / resolution) to the graph.

    Returns only the fields a node/input could be resolved for (see the module
    docstring for the exact shape). Tolerant of malformed nodes: anything
    unresolvable is omitted rather than raising, so a definition always folds.
    """
    param_map: dict = {}
    samplers = find_samplers(workflow)
    if samplers:
        sampler_id, sampler = samplers[0]
        sampler_inputs = _inputs_of(sampler)
        for seed_key in ("seed", "noise_seed"):
            if seed_key in sampler_inputs:
                param_map["seed"] = {"node_id": sampler_id, "input": seed_key}
                break
        positive = _text_target(workflow, sampler_inputs, "positive")
        if positive is not None:
            param_map["positive"] = positive
        negative = _text_target(workflow, sampler_inputs, "negative")
        if negative is not None:
            param_map["negative"] = negative

    for node_id, node in _nodes(workflow):
        if _class_of(node) != LATENT_CLASS:
            continue
        inputs = _inputs_of(node)
        if isinstance(inputs.get("width"), int) and isinstance(inputs.get("height"), int):
            param_map["resolution"] = {
                "node_id": node_id,
                "width_input": "width",
                "height_input": "height",
            }
            break
    return param_map


# -- definitions -------------------------------------------------------------


def _valid_definition(payload: dict) -> tuple[str, str] | None:
    """Extract ``(imagegen_id, name)`` from a payload, or ``None`` when malformed.

    Requires the contract's two mandatory fields to be non-empty strings; bad
    rows are skipped so the permissive ``system`` schema gate can never inject a
    broken definition.
    """
    imagegen_id = payload.get("imagegen_id")
    name = payload.get("name")
    if not isinstance(imagegen_id, str) or not imagegen_id:
        return None
    if not isinstance(name, str) or not name.strip():
        return None
    return imagegen_id, name


def _definition_from_payload(payload: dict) -> ImageGenDefinition | None:
    """Build an :class:`ImageGenDefinition` from one payload (``None`` if bad)."""
    ids = _valid_definition(payload)
    if ids is None:
        return None
    imagegen_id, name = ids
    workflow = payload.get("workflow")
    workflow = dict(workflow) if isinstance(workflow, dict) else {}
    param_map = payload.get("param_map")
    if not isinstance(param_map, dict):
        param_map = detect_parameters(workflow)
    provenance = payload.get("provenance")
    return ImageGenDefinition(
        imagegen_id=imagegen_id,
        name=name,
        workflow=workflow,
        prompt_template=_text(payload.get("prompt_template")),
        param_map=param_map,
        provenance=dict(provenance) if isinstance(provenance, dict) else {},
    )


def append_imagegen(log: "EventLog", data: dict) -> str:
    """Append one definition to :data:`IMAGEGEN_ANCHOR`; return its id.

    ``data`` carries at least ``name`` and ``workflow`` (plus any of
    ``prompt_template`` / ``provenance``). When ``imagegen_id`` is absent a fresh
    ``uuid4().hex`` is generated. Create and update both append (latest-wins per
    id); a missing/empty name raises ``ValueError`` so a nameless definition
    never reaches the catalog. The ``param_map`` is computed here and stored for
    audit. The write is a ``system`` event, never a side-table row (D9).
    """
    definition = dict(data or {})
    name = definition.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("imagegen definition requires a non-empty 'name'")

    imagegen_id = definition.get("imagegen_id")
    if not isinstance(imagegen_id, str) or not imagegen_id:
        imagegen_id = uuid.uuid4().hex

    workflow = definition.get("workflow")
    workflow = dict(workflow) if isinstance(workflow, dict) else {}
    provenance = definition.get("provenance")
    provenance = dict(provenance) if isinstance(provenance, dict) else {}
    provenance.setdefault("created_at", _now())

    log.append_event(
        IMAGEGEN_ANCHOR,
        "system",
        USER_ACTOR,
        {
            "event": IMAGEGEN_EVENT,
            "imagegen_id": imagegen_id,
            "name": name,
            "workflow": workflow,
            "prompt_template": _text(definition.get("prompt_template")),
            "param_map": detect_parameters(workflow),
            "provenance": provenance,
        },
    )
    return imagegen_id


def delete_imagegen(log: "EventLog", imagegen_id: str) -> None:
    """Tombstone a definition (``imagegen_definition_deleted``); the fold drops it.

    Non-destructive (D9): the definition rows stay queryable, the fold simply
    stops returning the id.
    """
    log.append_event(
        IMAGEGEN_ANCHOR,
        "system",
        USER_ACTOR,
        {"event": IMAGEGEN_DELETED_EVENT, "imagegen_id": imagegen_id},
    )


def list_imagegen(log: "EventLog") -> list[ImageGenDefinition]:
    """Fold the ``__imagegen__`` anchor into definitions (latest wins).

    Iterates the anchor's raw rows in seq order (:meth:`EventLog.get_events`)
    and keeps the newest definition per ``imagegen_id``; a tombstone removes the
    id. Malformed rows and non-``system`` events are ignored.
    """
    folded: dict[str, ImageGenDefinition] = {}
    for event in log.get_events(IMAGEGEN_ANCHOR):
        if event.type != "system":
            continue
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        kind = payload.get("event")
        if kind == IMAGEGEN_EVENT:
            definition = _definition_from_payload(payload)
            if definition is not None:
                folded[definition.imagegen_id] = definition
        elif kind == IMAGEGEN_DELETED_EVENT:
            imagegen_id = payload.get("imagegen_id")
            if isinstance(imagegen_id, str) and imagegen_id:
                folded.pop(imagegen_id, None)
    return list(folded.values())


def imagegen_by_id(log: "EventLog", imagegen_id: str) -> ImageGenDefinition | None:
    """One folded definition by id, or ``None`` when absent/tombstoned."""
    for definition in list_imagegen(log):
        if definition.imagegen_id == imagegen_id:
            return definition
    return None


# -- wire conversions --------------------------------------------------------


def imagegen_to_wire(definition: ImageGenDefinition) -> dict:
    """Wire shape of one definition (the frontend/engine contract)."""
    return {
        "imagegen_id": definition.imagegen_id,
        "name": definition.name,
        "workflow": dict(definition.workflow),
        "prompt_template": definition.prompt_template,
        "param_map": dict(definition.param_map),
        "provenance": dict(definition.provenance),
    }


def wire_to_imagegen(data: dict) -> ImageGenDefinition:
    """Parse a wire dict into an :class:`ImageGenDefinition` (raises ``ValueError``).

    Structural only — a ``str`` workflow must already have been decoded with
    :func:`parse_workflow` at the API door. The ``param_map`` is recomputed from
    the workflow here (the API is where the engine-facing map is produced).
    """
    if not isinstance(data, dict):
        raise ValueError("imagegen definition must be an object")
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("imagegen definition requires a non-empty 'name'")
    imagegen_id = data.get("imagegen_id")
    if imagegen_id is not None and (not isinstance(imagegen_id, str) or not imagegen_id):
        raise ValueError("imagegen definition 'imagegen_id' must be a non-empty string")
    workflow = data.get("workflow")
    if workflow is None:
        workflow = {}
    if not isinstance(workflow, dict):
        raise ValueError("imagegen definition 'workflow' must be an object")
    prompt_template = data.get("prompt_template")
    if prompt_template is None:
        prompt_template = ""
    provenance = data.get("provenance")
    if provenance is None:
        provenance = {}
    if not isinstance(provenance, dict):
        raise ValueError("imagegen definition 'provenance' must be an object")
    return ImageGenDefinition(
        imagegen_id=imagegen_id or "",
        name=name,
        workflow=dict(workflow),
        prompt_template=_text(prompt_template),
        param_map=detect_parameters(workflow),
        provenance=dict(provenance),
    )
