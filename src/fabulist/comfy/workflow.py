"""ComfyUI API-format workflow parsing + parameter mapping (V1 image-gen).

A workflow here is the **API format** ComfyUI's ``POST /prompt`` consumes: a
``{node_id: {"class_type": str, "inputs": {...}}}`` mapping (NOT the UI graph
export, which wraps nodes/links differently). The adjustable surface is fixed by
the plan:

* ``seed``      -> a ``KSampler`` ``seed`` (or ``KSamplerAdvanced`` ``noise_seed``;
                   ``ClownsharKSampler_Beta`` from RES4LYF also uses ``seed``)
* ``positive``  -> the ``text`` of the ``CLIPTextEncode`` the sampler's positive
                   conditioner input links to
* ``negative``  -> the same for the sampler's negative conditioner
* ``width`` / ``height`` -> an ``EmptyLatentImage`` node's ``width`` / ``height``

The mapping is intentionally forgiving. A workflow that cannot express a
requested key reports that key as **unmapped** rather than raising: the Studio
shows "not adjustable" and the run proceeds with the workflow's own defaults.
:func:`apply_overrides` returns a NEW workflow dict (the caller's input is never
mutated) plus the list of requested keys it could not place.
"""

from __future__ import annotations

import copy
from typing import Any, NamedTuple

__all__ = [
    "ADJUSTABLE_KEYS",
    "CONDITIONER_CLASSES",
    "LATENT_CLASSES",
    "OverrideResult",
    "ParamMap",
    "SAMPLER_CLASSES",
    "apply_overrides",
    "parse_workflow",
]

#: Node classes carrying a seed input (RES4LYF's ``ClownsharKSampler_Beta``
#: uses ``seed`` like ``KSampler``).
SAMPLER_CLASSES = frozenset({"KSampler", "KSamplerAdvanced", "ClownsharKSampler_Beta"})

#: Node class carrying the latent resolution.
LATENT_CLASSES = frozenset({"EmptyLatentImage"})

#: Node class whose ``text`` input is the positive/negative prompt string.
CONDITIONER_CLASSES = frozenset({"CLIPTextEncode"})

#: The override keys the adjustable strip can send.
ADJUSTABLE_KEYS = ("seed", "positive", "negative", "width", "height")

#: Seed input names, tried in order (``KSamplerAdvanced`` uses ``noise_seed``).
_SEED_INPUTS = ("noise_seed", "seed")


class ParamMap(NamedTuple):
    """Resolved workflow targets for the adjustable keys.

    ``seed`` is a list (a hires-fix workflow has more than one sampler); the
    conditioner/resolution fields are single ``(node_id, input_key)`` pairs taken
    from the first sampler / latent node found.
    """

    seed: list[tuple[str, str]]
    positive: tuple[str, str] | None
    negative: tuple[str, str] | None
    width: tuple[str, str] | None
    height: tuple[str, str] | None

    def mappable(self) -> set[str]:
        """The adjustable keys this workflow can express."""
        names: set[str] = set()
        if self.seed:
            names.add("seed")
        if self.positive:
            names.add("positive")
        if self.negative:
            names.add("negative")
        if self.width:
            names.add("width")
        if self.height:
            names.add("height")
        return names


class OverrideResult(NamedTuple):
    """Outcome of :func:`apply_overrides`.

    ``workflow`` is a NEW dict (input untouched); ``unmapped`` lists the requested
    keys that could not be placed on this workflow, in request order.
    """

    workflow: dict
    unmapped: list[str]


def _input_nodes(workflow: Any) -> dict[str, dict]:
    """Well-formed ``{node_id: node}`` entries of ``workflow`` (string ids only)."""
    if not isinstance(workflow, dict):
        return {}
    nodes: dict[str, dict] = {}
    for node_id, node in workflow.items():
        if isinstance(node_id, str) and isinstance(node, dict):
            nodes[node_id] = node
    return nodes


def _class_type(node: dict) -> str:
    value = node.get("class_type")
    return value if isinstance(value, str) else ""


def _inputs(node: dict) -> dict:
    value = node.get("inputs")
    return value if isinstance(value, dict) else {}


def _linked_node_id(value: Any) -> str | None:
    """The node id of a ComfyUI link value ``[node_id, output_index]``."""
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], str):
        return value[0]
    return None


def _conditioner_text(nodes: dict[str, dict], link: Any) -> tuple[str, str] | None:
    """Resolve a sampler conditioner ``link`` to its ``CLIPTextEncode`` ``text``."""
    node_id = _linked_node_id(link)
    if node_id is None:
        return None
    node = nodes.get(node_id)
    if node is None or _class_type(node) not in CONDITIONER_CLASSES:
        return None
    if "text" not in _inputs(node):
        return None
    return (node_id, "text")


def parse_workflow(workflow: Any) -> ParamMap:
    """Resolve the adjustable-key targets in an API-format ``workflow``.

    Total: a non-dict / malformed workflow yields an empty map (every requested
    key then reports unmapped) rather than raising. Node ids are walked in sorted
    order so the result is deterministic for the same JSON.
    """
    nodes = _input_nodes(workflow)

    seed: list[tuple[str, str]] = []
    positive: tuple[str, str] | None = None
    negative: tuple[str, str] | None = None
    for node_id, node in sorted(nodes.items()):
        if _class_type(node) not in SAMPLER_CLASSES:
            continue
        inputs = _inputs(node)
        for key in _SEED_INPUTS:
            if key in inputs:
                seed.append((node_id, key))
                break
        if positive is None:
            positive = _conditioner_text(nodes, inputs.get("positive"))
        if negative is None:
            negative = _conditioner_text(nodes, inputs.get("negative"))

    width: tuple[str, str] | None = None
    height: tuple[str, str] | None = None
    for node_id, node in sorted(nodes.items()):
        if _class_type(node) not in LATENT_CLASSES:
            continue
        inputs = _inputs(node)
        if width is None and "width" in inputs:
            width = (node_id, "width")
        if height is None and "height" in inputs:
            height = (node_id, "height")

    return ParamMap(seed, positive, negative, width, height)


def apply_overrides(workflow: Any, overrides: Any) -> OverrideResult:
    """Return a NEW workflow with ``overrides`` applied, plus unmapped keys.

    ``overrides`` maps any of :data:`ADJUSTABLE_KEYS` to a value. The input
    ``workflow`` is deep-copied first and never mutated. A requested key with no
    resolved target (or an unknown key) is collected in ``unmapped`` and skipped —
    an unmappable request degrades to "not adjustable", never a broken run.
    """
    new_workflow = copy.deepcopy(workflow) if isinstance(workflow, dict) else {}
    parsed = parse_workflow(workflow)

    targets: dict[str, list[tuple[str, str]]] = {
        "seed": list(parsed.seed),
        "positive": [parsed.positive] if parsed.positive else [],
        "negative": [parsed.negative] if parsed.negative else [],
        "width": [parsed.width] if parsed.width else [],
        "height": [parsed.height] if parsed.height else [],
    }

    unmapped: list[str] = []
    items = overrides.items() if isinstance(overrides, dict) else []
    for key, value in items:
        node_targets = targets.get(key) if key in ADJUSTABLE_KEYS else None
        if not node_targets:
            unmapped.append(key)
            continue
        for node_id, input_key in node_targets:
            node = new_workflow.get(node_id)
            if isinstance(node, dict) and isinstance(node.get("inputs"), dict):
                node["inputs"][input_key] = value
    return OverrideResult(new_workflow, unmapped)
