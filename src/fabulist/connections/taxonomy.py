"""Agent taxonomy: types x tasks over the flat ``agents`` slot map (W9.25 §3.2).

The connections document binds a logical *slot* to a connection name
(``agents: {"narrator": "your-model-id"}``, see :mod:`fabulist.connections.registry`).
W9.25 lets that map ALSO carry typed slots — ``completion``,
``completion.narration``, ``completion.extraction``, ``completion.state``,
``embedding`` — so a model can be bound "under the type OR per item as today".
The hierarchy lives HERE, in code, never in the document's shape: one flat map
stays the schema of record, and compatibility is exact (a config with only the
legacy keys resolves byte-identically — no typed key authored means
:func:`resolve_slot` short-circuits at the exact key).

Vocabulary
----------
* **types**: ``completion`` | ``embedding`` | ``decision``. ``decision`` is both
  the type name and the live item key (the decision model is one model), and
  ``embedding`` is a legal type key that no live config authors yet.
* **tasks**: under ``completion`` only — ``narration`` | ``extraction`` |
  ``state``.
* **legacy twins**: ``narrator`` == ``completion.narration`` and ``extractor``
  == ``completion.extraction``. The legacy key is an ALIAS: never removed,
  never rewritten, and inert once its typed twin is authored.
* **item slots**: ``summarizer``, ``captioner``, ``imagegen``, ``embedder`` —
  per-item bindings exactly as today, each with a declared type (+ affinity task
  for display and fallback). ``embedder`` is DECLARED here (``embedding``/no
  affinity) so the memory rebind path resolves through the taxonomy like every
  other slot.

Resolution — ONE rule, both directions
--------------------------------------
:func:`resolve_slot` tries, in order: **both spellings of the name's twin pair
(typed first)**, then the item's **affinity task key** (both of ITS spellings —
the twin rule holds at every level), then the **type key**.
Because the twin pair is symmetric, resolving ``"narrator"`` and resolving
``"completion.narration"`` are the SAME lookup — typed beats twin, so authoring
``completion.narration`` makes the legacy ``narrator`` key inert, and authoring
only ``narrator`` still resolves the typed spelling. A dotted key outside the
declared vocabulary is rejected at load by the registry (typo protection);
undotted keys stay free-form per-item slots.

Pure: no I/O, no environment, no registry import (callers pass the raw
``agents`` mapping). :func:`slot_view` is the read-side payload the connections
API serves and the drawer groups.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from fabulist.prompts.agent_sets import DEFAULT_BINDINGS, INTERACTIONS

__all__ = [
    "INTERACTION_TASKS",
    "ITEM_SLOTS",
    "LEGACY_TWINS",
    "SLOT_LABELS",
    "TASKS",
    "TYPED_SLOTS",
    "TYPES",
    "candidate_slots",
    "canonical",
    "declared_spellings",
    "resolve_interaction_agent",
    "resolve_slot",
    "slot_view",
]

#: The model classes a connection can be bound for.
TYPES: tuple[str, ...] = ("completion", "embedding", "decision")

#: Task names per type. Only ``completion`` has tasks; embedding/decision are
#: single-purpose model classes and take no task layer.
TASKS: dict[str, tuple[str, ...]] = {
    "completion": ("narration", "extraction", "state"),
}

#: The typed slot spellings the ``agents`` map may author. ``decision`` is
#: absent because it is already the item key (the type and the item coincide);
#: ``embedding`` is a declared TYPE spelling no live config authors yet.
TYPED_SLOTS: tuple[str, ...] = (
    "completion",
    "completion.narration",
    "completion.extraction",
    "completion.state",
    "embedding",
)

#: Legacy key -> the typed spelling it aliases. Present in the map means "this
#: alias resolves"; a typed twin being authored makes the legacy key inert.
LEGACY_TWINS: dict[str, str] = {
    "narrator": "completion.narration",
    "extractor": "completion.extraction",
}

#: Per-item slots (the "per item as today" authoring mode): slot ->
#: ``(type, affinity task)``. The affinity is the ancestor key consulted when
#: the item's own key is unauthored; ``None`` means the type key directly.
ITEM_SLOTS: dict[str, tuple[str, str | None]] = {
    "summarizer": ("completion", "extraction"),
    "captioner": ("completion", "extraction"),
    "imagegen": ("completion", None),
    # Declared (R2 fold P2-7): the live embedder key resolves through the
    # taxonomy like every other slot, so the memory rebind path and the drawer
    # read one vocabulary.
    "embedder": ("embedding", None),
}

#: Interaction id -> ``(type, task)``: which slot an interaction belongs to at
#: the taxonomy layer. The preset binding layer (D19) sits ABOVE this and wins
#: when it names a resolvable slot; this table is the layer's own default.
INTERACTION_TASKS: dict[str, tuple[str, str]] = {
    "narrate": ("completion", "narration"),
    "ooc": ("completion", "narration"),
    "assistant": ("completion", "narration"),
    "planner": ("completion", "narration"),
    "npc": ("completion", "narration"),  # narrative-voice work (W9.25 §3.4)
    "extract": ("completion", "extraction"),
    "critic": ("completion", "extraction"),
    "proposals": ("completion", "extraction"),
    "summarize": ("completion", "extraction"),
    "caption": ("completion", "extraction"),
    "bts": ("completion", "state"),
}

#: Display labels for declared slots (the drawer's row names). An undeclared
#: custom key labels itself.
SLOT_LABELS: dict[str, str] = {
    "completion": "Completion",
    "completion.narration": "Narration",
    "completion.extraction": "Extraction",
    "completion.state": "State",
    "embedding": "Embedding",
    "decision": "Decision",
    "summarizer": "Summarizer",
    "captioner": "Captioner",
    "imagegen": "Imagegen",
    "embedder": "Embedder",
}


# --- vocabulary -------------------------------------------------------------------


def declared_spellings() -> frozenset[str]:
    """Every slot spelling the taxonomy declares — the preset-binding known set.

    Typed slots + legacy twins + item slots, plus the type names that are also
    their own item key (``decision``). A preset may bind an interaction to any
    of these even when the document authors no such key: resolution then falls
    through the hierarchy (and raises at call time only if nothing resolves).
    """
    return (
        frozenset(TYPED_SLOTS)
        | frozenset(LEGACY_TWINS)
        | frozenset(ITEM_SLOTS)
        | frozenset(TYPES)
    )


def canonical(name: str) -> str:
    """The canonical spelling of ``name``: a legacy twin maps to its typed twin."""
    return LEGACY_TWINS.get(name, name)


def _twin_pair(name: str) -> tuple[str, ...]:
    """Both spellings of ``name``'s twin pair, TYPED (canonical) first.

    Symmetric by construction: ``"narrator"`` -> ``("completion.narration",
    "narrator")`` and ``"completion.narration"`` -> the same tuple, so both
    spellings are one lookup.
    """
    typed = canonical(name)
    if typed == name:
        return (name, *(legacy for legacy, twin in LEGACY_TWINS.items() if twin == name))
    return (typed, name)


def _type_of(name: str) -> str | None:
    """The type ``name`` belongs to (its own type key when it IS a type)."""
    if name in TYPES:
        return name
    affinity = ITEM_SLOTS.get(name)
    if affinity is not None:
        return affinity[0]
    base = canonical(name)
    if "." in base:
        return base.split(".", 1)[0]
    return None


def candidate_slots(name: str) -> tuple[str, ...]:
    """The ``agents`` keys :func:`resolve_slot` consults for ``name``, in order.

    Item (both twin spellings, typed first) > affinity task (both of ITS twin
    spellings — the twin rule applies at every level, so an authored ``extractor``
    IS an authored extraction-task key) > type. Exposed for tests and for the
    drawer's provenance line; the precedence itself lives only here.
    """
    candidates: list[str] = list(_twin_pair(name))
    affinity = ITEM_SLOTS.get(name)
    if affinity is not None:
        type_name, task_name = affinity
        if task_name is not None:
            candidates.extend(_twin_pair(f"{type_name}.{task_name}"))
        candidates.append(type_name)
    type_name = _type_of(name)
    if type_name is not None:
        candidates.append(type_name)
    seen: set[str] = set()
    ordered: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            ordered.append(candidate)
    return tuple(ordered)


def resolve_slot(agents: Mapping[str, str], name: str) -> str | None:
    """Resolve ``name`` to a connection name through the hierarchy, or ``None``.

    ``agents`` is the registry's raw slot -> connection map. Order: the name's
    twin pair (typed spelling first), the item's affinity task key, the type
    key. ``None`` means "no authored key covers this slot" — the registry raises
    :class:`~fabulist.connections.registry.ConnectionConfigError` naming it.
    """
    for candidate in candidate_slots(name):
        if candidate in agents:
            return agents[candidate]
    return None


def _resolved_slot(agents: Mapping[str, str], name: str) -> tuple[str, str] | None:
    """``(matched slot key, connection name)``, or ``None`` when unresolvable."""
    for candidate in candidate_slots(name):
        if candidate in agents:
            return candidate, agents[candidate]
    return None


def _via(slot: str, matched: str) -> str:
    """Provenance for a slot resolved through ``matched`` (see :func:`slot_view`)."""
    if matched == slot:
        return "authored"
    if matched in LEGACY_TWINS:
        return f"legacy:{matched}"
    return f"type:{matched}"


# --- interaction resolution (the preset layer's default) --------------------------


def resolve_interaction_agent(
    bindings: Mapping[str, Any] | None,
    agents: Mapping[str, str],
    interaction: str,
    fallback: str,
) -> str:
    """Resolve which slot (agent key) serves ``interaction`` — never raises.

    Order (W9.25 §3.2, R3 fold):

    1. the preset's explicit binding for ``interaction``, when it names a slot
       the ``agents`` map resolves (an explicit choice always wins);
    2. the interaction's per-item :data:`DEFAULT_BINDINGS` agent, when that
       agent IS a declared per-item slot (``summarizer``, ``captioner``,
       ``imagegen``, ``embedder``) — the author's per-item choice must not be
       rerouted by an authored task key, which is what "per item as today"
       means (R3: task-before-default silently moved caption/summarize to the
       extraction connection);
    3. the interaction's task slot (:data:`INTERACTION_TASKS`), which itself
       falls through its legacy twin and then the type key;
    4. :data:`DEFAULT_BINDINGS`' agent for the interaction (a twin or typed row
       AND the ``fallback``'s own name) — deferred deliberately: ``bts``'s
       ``extractor`` default is a TWIN, so an authored ``completion.state`` key
       must win over it;
    5. ``fallback`` — the caller's own agent name, exactly as before.

    Returns a slot NAME (not a connection name) so it flows unchanged into
    ``make_agent_caller`` -> ``RegistryModelCaller`` -> ``connection_for`` and
    usage rows label the slot that actually served the call. A binding whose
    spelling resolves to nothing degrades to the next step rather than raising
    mid-turn.
    """
    if isinstance(bindings, Mapping):
        binding = bindings.get(interaction)
        if isinstance(binding, Mapping):
            agent = binding.get("agent")
            if isinstance(agent, str) and agent:
                resolved = _resolved_slot(agents, agent)
                if resolved is not None:
                    return resolved[0]

    default = DEFAULT_BINDINGS.get(interaction)
    default_agent = default.get("agent") if isinstance(default, Mapping) else None

    # R3 fold: a PER-ITEM default agent (summarizer/captioner/imagegen/embedder)
    # is the author's per-item choice and is consulted BEFORE the task key — an
    # authored ``completion.extraction`` must not reroute caption/summarize.
    if isinstance(default_agent, str) and default_agent in ITEM_SLOTS:
        resolved = _resolved_slot(agents, default_agent)
        if resolved is not None:
            return resolved[0]

    task = INTERACTION_TASKS.get(interaction)
    if task is not None:
        type_name, task_name = task
        resolved = _resolved_slot(agents, f"{type_name}.{task_name}")
        if resolved is not None:
            return resolved[0]

    # A twin/typed default defers to the task above (``bts`` -> ``completion.state``).
    if isinstance(default_agent, str) and default_agent:
        resolved = _resolved_slot(agents, default_agent)
        if resolved is not None:
            return resolved[0]
    return fallback


# --- read-side payload (GET /api/connections ``slots``) ---------------------------


def _slot_row(agents: Mapping[str, str], slot: str) -> dict[str, Any]:
    """One declared slot's row: resolved connection + resolution provenance."""
    resolved = _resolved_slot(agents, slot)
    type_name = _type_of(slot)
    task = slot.split(".", 1)[1] if "." in canonical(slot) else None
    return {
        "slot": slot,
        "type": type_name,
        "task": task,
        "label": SLOT_LABELS.get(slot, slot),
        "canonical": canonical(slot),
        "aliases": [legacy for legacy, twin in LEGACY_TWINS.items() if twin == canonical(slot)],
        "connection": resolved[1] if resolved is not None else None,
        "via": _via(slot, resolved[0]) if resolved is not None else None,
    }


def slot_view(agents: Mapping[str, str]) -> list[dict[str, Any]]:
    """The drawer/API slot payload: ordered rows over the SAME resolution rule.

    Rows are grouped by type in :data:`TYPES` order — the type row, then its
    task rows, then the per-item rows belonging to it — so the drawer renders
    Completion / Embedding / Decision headers without re-deriving the
    hierarchy. Authored-but-undeclared custom keys follow as per-item rows
    (``type``/``task`` ``None``): "per item as today" stays first-class and
    visible. ``via`` is ``authored`` (the slot's own key), ``legacy:<key>`` (its
    legacy twin), ``type:<key>`` (an ancestor slot key — a task or type key),
    or ``None`` (unresolved). Legacy keys never get their own row; they appear
    in the typed slot's ``aliases``.
    """
    rows: list[dict[str, Any]] = []
    for type_name in TYPES:
        rows.append(_slot_row(agents, type_name))
        for task_name in TASKS.get(type_name, ()):
            rows.append(_slot_row(agents, f"{type_name}.{task_name}"))
        for item, (item_type, _affinity) in ITEM_SLOTS.items():
            if item_type == type_name:
                rows.append(_slot_row(agents, item))

    declared = declared_spellings()
    for name in sorted(key for key in agents if key not in declared):
        rows.append(
            {
                "slot": name,
                "type": None,
                "task": None,
                "label": SLOT_LABELS.get(name, name),
                "canonical": name,
                "aliases": [],
                "connection": agents[name],
                "via": "authored",
            }
        )
    return rows


# Import-time guard (same discipline as prompts/catalog.py): every interaction
# must declare a taxonomy slot, with a task that exists under its type. Drift is
# a hard error, never a silent "this interaction has no type".
_DECLARED_TASKS: list[str] = [
    interaction
    for interaction, (type_name, task_name) in INTERACTION_TASKS.items()
    if type_name not in TYPES or task_name not in TASKS.get(type_name, ())
]
_MISSING_TASKS: list[str] = [
    interaction for interaction in INTERACTIONS if interaction not in INTERACTION_TASKS
]
_STRAY_TASKS: list[str] = [
    interaction for interaction in INTERACTION_TASKS if interaction not in INTERACTIONS
]
if _DECLARED_TASKS or _MISSING_TASKS or _STRAY_TASKS:  # pragma: no cover - drift only
    raise RuntimeError(
        "fabulist.connections.taxonomy: INTERACTION_TASKS is out of sync with "
        f"prompts.agent_sets.INTERACTIONS; invalid task for {_DECLARED_TASKS or '<none>'}, "
        f"missing {_MISSING_TASKS or '<none>'}, unknown {_STRAY_TASKS or '<none>'}"
    )
