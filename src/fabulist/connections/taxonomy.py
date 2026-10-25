"""Agent taxonomy: types x tasks over the flat ``agents`` slot map (W9.25 §3.2).

The connections document binds a logical *slot* to a connection name
(``agents: {"narrator": "your-model-id"}``, see :mod:`fabulist.connections.registry`).
W9.25 lets that map ALSO carry typed slots — ``completion``,
``completion.narration``, ``completion.extraction``, ``completion.state``,
``embedding`` — so a model can be bound "under the type OR per item as today".
The hierarchy lives HERE, in code, never in the document's shape: one flat map
stays the schema of record, and compatibility is exact: every key a live
config authors keeps resolving to the same connection, and no typed key
authored means :func:`resolve_slot` short-circuits at the name's own spelling
(the one intended exception is read-side — a task-less TYPE row reports the
item slot that actually serves it, see below — and it never changes which
connection a key a config authored resolves to).

Vocabulary
----------
* **types**: ``completion`` | ``embedding`` | ``decision``. ``decision`` is both
  the type name and the live item key (the decision model is one model), and
  ``embedding`` is a legal type key that live configs rarely author — the live
  binding is the ``embedder`` item key, which IS what serves the type.
* **tasks**: under ``completion`` only — ``narration`` | ``extraction`` |
  ``state``.
* **legacy twins**: ``narrator`` == ``completion.narration`` and ``extractor``
  == ``completion.extraction``. The legacy key is an ALIAS: never removed,
  never rewritten, and inert once its typed twin is authored.
* **item slots**: ``summarizer``, ``captioner``, ``imagegen``, ``planner``,
  ``npc``, ``embedder`` — per-item bindings exactly as today, each with a
  declared type (+ affinity task for display and fallback). ``embedder`` is
  DECLARED here (``embedding``/no affinity) so the memory rebind path resolves
  through the taxonomy like every other slot. ``planner`` and ``npc`` (issues
  #55b/#55c) are the same shape: one key per ROLE, affinity ``completion`` /
  ``narration``, so an authored key beats the narration task and an unauthored
  one falls through to it — a legacy/typed narration binding keeps serving both
  exactly as before.

Resolution — ONE rule, both directions
--------------------------------------
:func:`resolve_slot` tries, in order: **both spellings of the name's twin pair
(typed first)**, then the item's **affinity task key** (both of ITS spellings —
the twin rule holds at every level), then the **type key**. A TYPE that has NO
task layer (``embedding``, ``decision`` — ``TASKS`` names none) additionally
falls DOWN to the affinity-``None`` item slots declared under it, in
``ITEM_SLOTS`` declaration order: its own exact key is tried FIRST, so an
authored typed key always wins, and an item key (``embedder``) is the fallback
rather than a dead end. That downward leg exists because the item slot is what
actually serves the type: authoring only ``embedder`` means embedding calls
WORK, so the type row must not read "unresolved" (a false dead reading caused a
production incident). Types WITH a task layer (``completion``) deliberately get
no downward leg: its item slots (``summarizer``/``captioner``/``imagegen``) are
optional extras, and letting ``completion`` resolve through ``imagegen`` would
report the completion type as served by an image model. This fallback is
read-side and call-side consistent — :func:`resolve_slot`, :func:`slot_view`
and :func:`~fabulist.connections.registry.configuration_report` all agree — and
it never outranks an authored key: the type's own spelling is tried first.

Because the twin pair is symmetric, resolving ``"narrator"`` and resolving
``"completion.narration"`` are the SAME lookup — typed beats twin, so authoring
``completion.narration`` makes the legacy ``narrator`` key inert, and authoring
only ``narrator`` still resolves the typed spelling. A dotted key outside the
declared vocabulary is rejected at load by the registry (typo protection);
undotted keys stay free-form per-item slots.

Read side — ONE row per ROLE
The drawer does not render one row per authored spelling; it renders one row per
RESOLVED ROLE, under the role's canonical spelling. Two spellings are aliases of
one role, never two rows:

* a **legacy twin** (``narrator`` -> ``completion.narration``), and
* the **item slot that serves a task-less type** (``embedder`` -> the
  ``embedding`` role). Its key never gets a row of its own: it IS what the
  embedding call sites ask for, so folding it in is what keeps a bound role from
  *showing* unbound under its alias while the engine happily uses the other
  spelling (the sandbox evidence: ``embedder`` -> ``sidecar-embed`` with usage
  rows while ``embedding`` displayed unset).

Each row therefore carries the EFFECTIVE binding — the connection the engine
resolves when it does that work — plus ``write_slot`` (the call-path spelling a
rebind writes, so writing it always takes effect, see :func:`binding_key`) and
``inert``: every authored spelling of the role that LOST precedence, with its
own connection. A conflict is surfaced, never hidden.

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
    "SLOT_GROUPS",
    "SLOT_LABELS",
    "TASKS",
    "TYPED_SLOTS",
    "TYPES",
    "binding_key",
    "candidate_slots",
    "canonical",
    "declared_spellings",
    "folded_item_slots",
    "resolve_interaction_agent",
    "resolve_slot",
    "role_spellings",
    "serving_item_slots",
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
    # Issue #55b: the planner (steering plane) is its own per-item ROLE, so the
    # connections pane maps it like the others. Affinity ``narration`` is what
    # keeps every existing config serving the planner unchanged: unauthored, the
    # row resolves through the narration task (``narrator`` on a legacy document,
    # ``completion.narration`` when typed) — exactly the D19 default
    # ``planner -> narrator``. Authored, the planner key wins because the planner
    # interaction is resolved per call (``PresetAgentCaller("planner", ...)`` ->
    # :func:`resolve_interaction_agent`, see the interaction-name leg below).
    "planner": ("completion", "narration"),
    # Issue #55c: the NPC fan-out's LANE GROUP. One key binds the whole lane set
    # (every active cast member's decision pass); the lanes are narrative-voice
    # work, so the affinity is the narration task. Per-member granularity stays a
    # preset-layer concern (``DEFAULT_BINDINGS['npc']['overrides']``) — this slot
    # is the group knob, not a wildcard per member.
    "npc": ("completion", "narration"),
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

#: Rows that render in a group of their OWN instead of their type's group.
#: Slot -> group label. The NPC fan-out is the case this exists for (issue #55c):
#: it is ONE binding that covers every active cast member, so the pane shows it as
#: a lane GROUP ("Npc lanes") rather than as one more row among the completion
#: extras. Grouping is presentation only — resolution is untouched.
SLOT_GROUPS: dict[str, str] = {
    "npc": "NPC lanes",
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
    # Issue #55a: the AGENT role is an image PROMPTER (it renders diffusion
    # prompts; ComfyUI does the diffusion). The slot KEY stays ``imagegen`` —
    # renaming a key would break every authored config, so the rename is
    # display-only (alias discipline).
    "imagegen": "Image Prompter",
    # Issue #55b/#55c: the two per-item roles that are also D19 interactions.
    "planner": "Planner",
    "npc": "NPC lanes",
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


def serving_item_slots(type_name: str) -> tuple[str, ...]:
    """The item slots declared under ``type_name`` with NO affinity task.

    Declaration order, as :data:`ITEM_SLOTS` states it. These are the keys that
    serve the TYPE itself (``embedding`` -> ``embedder``); an item slot with an
    affinity (``summarizer``/``captioner`` -> ``completion.extraction``) belongs
    to a TASK and never represents its type.
    """
    return tuple(
        item
        for item, (item_type, affinity) in ITEM_SLOTS.items()
        if item_type == type_name and affinity is None
    )


def folded_item_slots(type_name: str) -> tuple[str, ...]:
    """Item slots that ARE their type's row — folded into it, never a row.

    Only a TASK-LESS type has them (``embedding`` -> ``embedder``): that item is
    what serves the type, so the two spellings are ONE role and the drawer must
    render one row, not two. A type WITH a task layer keeps its affinity-``None``
    items as separate roles on purpose (``imagegen`` is an optional extra, not
    the ``completion`` type) — the same gate :func:`candidate_slots` uses for the
    downward leg.
    """
    return () if TASKS.get(type_name) else serving_item_slots(type_name)


def binding_key(name: str) -> str:
    """The ``agents`` key that binds ROLE ``name`` where the engine looks.

    Almost always the canonical spelling: a twin resolves typed-first, so writing
    the typed key takes effect and shadows an authored legacy key. The exception
    is a role served by an item slot of a TASK-LESS type: its call sites ask for
    THAT name (``connection_for("embedder")``) and a name's own key is tried
    before its type, so ``embedder`` is the spelling that wins there. Writing
    ``embedding`` instead would be shadowed by an authored ``embedder`` and
    silently lose — a rebind that reports success and changes nothing.

    Always a declared spelling (so ``PATCH /api/connections/agents/{key}``
    accepts it) and one whose write the registry resolves exactly as today.
    """
    base = canonical(name)
    if base in TYPES and not TASKS.get(base):
        serving = serving_item_slots(base)
        if serving:
            return serving[0]
    return base


def role_spellings(name: str) -> tuple[str, ...]:
    """Every declared ``agents`` spelling that binds the role ``name``.

    Canonical spelling first, then its legacy twin(s), then the item slots folded
    into a task-less type. This is the set :func:`slot_view` diffs against the
    effective one to report ``inert`` spellings — an authored alias that loses
    precedence stays visible instead of silently disappearing.
    """
    base = canonical(name)
    spellings: list[str] = [base]
    spellings.extend(legacy for legacy, twin in LEGACY_TWINS.items() if twin == base)
    spellings.extend(folded_item_slots(base))
    seen: set[str] = set()
    ordered: list[str] = []
    for spelling in spellings:
        if spelling not in seen:
            seen.add(spelling)
            ordered.append(spelling)
    return tuple(ordered)


def candidate_slots(name: str) -> tuple[str, ...]:
    """The ``agents`` keys :func:`resolve_slot` consults for ``name``, in order.

    Item (both twin spellings, typed first) > affinity task (both of ITS twin
    spellings — the twin rule applies at every level, so an authored ``extractor``
    IS an authored extraction-task key) > type > (for a TYPE with no task layer)
    the item slots that serve that type: ``embedding`` -> ``("embedding",
    "embedder")``, ``decision`` -> ``("decision",)``. The type's OWN key comes
    first, so an authored typed spelling always beats the item fallback.
    Exposed for tests and for the drawer's provenance line; the precedence
    itself lives only here.
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
    # Downward leg, TASK-LESS TYPES only (a type with a task layer keeps its
    # single-purpose item slots as optional extras, not as its own fallback).
    if name in TYPES and not TASKS.get(name):
        candidates.extend(serving_item_slots(name))
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
    key, then — for a task-less TYPE only — the item slots declared under it
    (``embedding`` -> ``embedder``). ``None`` means "no authored key covers this
    slot" — the registry raises
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
    """Provenance for a slot resolved through ``matched`` (see :func:`slot_view`).

    ``authored`` (the slot's own key) | ``legacy:<key>`` (its legacy twin) |
    ``type:<key>`` (an ancestor slot key) | ``item:<slot>`` (a TYPE served by the
    item slot declared under it, e.g. ``embedding`` -> ``item:embedder``).
    """
    if matched == slot:
        return "authored"
    if matched in LEGACY_TWINS:
        return f"legacy:{matched}"
    if matched in ITEM_SLOTS:
        return f"item:{matched}"
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
    1b. the interaction's OWN per-item slot, when the document authors it
       (``planner``, ``npc`` — issues #55b/#55c). An authored key is an explicit
       choice by the document's author, so it outranks the task leg below; an
       unauthored one falls through and nothing changes;
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

    # Issue #55b/#55c: an interaction that OWNS a per-item slot of its own name
    # (``planner`` -> :data:`ITEM_SLOTS` ``planner``, ``npc`` -> ``npc``) is
    # served by that key the moment the document authors it. This is what makes
    # the connections drawer's rebind EFFECTIVE: the planner lane and the NPC
    # fan-out resolve their agent per call through this function, so an authored
    # key wins and an unauthored one changes nothing (the task leg below still
    # resolves the narration binding, which is today's ``planner -> narrator``).
    # Checked on the raw key, never through ``_resolved_slot``: that would match
    # any authored narration key and turn the fallback into a hard choice.
    if interaction in ITEM_SLOTS and interaction in agents:
        return interaction

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
    """One ROLE's row: the effective binding, its provenance, its inert spellings.

    The role is resolved through :func:`binding_key` — the spelling the engine's
    own call sites use — so the row's ``connection`` is the connection the work
    ACTUALLY runs on, not the one a type-first reading would prefer. ``inert``
    lists the authored spellings of the role that lost precedence (each with its
    own connection), which is how a conflict stays visible.
    """
    call_slot = binding_key(slot)
    resolved = _resolved_slot(agents, call_slot)
    matched = resolved[0] if resolved is not None else None
    type_name = _type_of(slot)
    task = slot.split(".", 1)[1] if "." in canonical(slot) else None
    legacy = [legacy for legacy, twin in LEGACY_TWINS.items() if twin == canonical(slot)]
    return {
        "slot": slot,
        "type": type_name,
        "task": task,
        "label": SLOT_LABELS.get(slot, slot),
        # The drawer's header for this row's section: its type, unless the role is
        # declared as its own group (``npc`` -> "Npc lanes").
        "group": SLOT_GROUPS.get(slot) or type_name,
        "canonical": canonical(slot),
        # Legacy twins + the item slots folded into a task-less type: the other
        # spellings of THIS role (never rows of their own).
        "aliases": [*legacy, *folded_item_slots(canonical(slot))],
        "connection": resolved[1] if resolved is not None else None,
        "via": _via(slot, matched) if matched is not None else None,
        # The `agents` key a rebind of this row writes (declared spelling, always
        # effective — see ``binding_key``).
        "write_slot": call_slot,
        # Authored spellings of this role that lose precedence. Unlike ``via`` (a
        # single provenance string), this names the LOSER and the connection it
        # was pointed at, so the drawer can show the conflict instead of hiding it.
        "inert": [
            {"slot": spelling, "connection": agents[spelling]}
            for spelling in role_spellings(slot)
            if spelling != matched and spelling in agents
        ],
    }


def slot_view(agents: Mapping[str, str]) -> list[dict[str, Any]]:
    """The drawer/API slot payload: ONE row per resolved ROLE, ordered by type.

    Rows are grouped by type in :data:`TYPES` order — the type row, then its
    task rows, then the per-item rows belonging to it — so the drawer renders
    Completion / Embedding / Decision headers without re-deriving the
    hierarchy (a row declared in :data:`SLOT_GROUPS` — ``npc`` — names its own
    group instead, so the lane group renders as its own section). Authored-but-
    undeclared custom keys follow as per-item rows (``type``/``task`` ``None``): "per item as today" stays first-class and
    visible.

    Exactly one row per role: a legacy key never gets a row (it rides the typed
    row's ``aliases``), and neither does an item slot that SERVES its task-less
    type (``embedder`` folds into the ``embedding`` row — that row then reports
    ``via: item:embedder``). ``via`` is ``authored`` (the role's own key),
    ``legacy:<key>`` (its legacy twin), ``type:<key>`` (an ancestor slot key — a
    task or type key), ``item:<slot>`` (a task-less TYPE served by the item slot
    declared under it, e.g. ``embedding`` -> ``item:embedder``), or ``None``
    (unresolved). ``connection`` is the EFFECTIVE binding (what the engine's own
    call path resolves), ``write_slot`` the key a rebind writes, and ``inert``
    the authored spellings that lost precedence.
    """
    rows: list[dict[str, Any]] = []
    for type_name in TYPES:
        rows.append(_slot_row(agents, type_name))
        for task_name in TASKS.get(type_name, ()):
            rows.append(_slot_row(agents, f"{type_name}.{task_name}"))
        folded = folded_item_slots(type_name)
        for item, (item_type, _affinity) in ITEM_SLOTS.items():
            if item_type == type_name and item not in folded:
                rows.append(_slot_row(agents, item))

    declared = declared_spellings()
    for name in sorted(key for key in agents if key not in declared):
        rows.append(
            {
                "slot": name,
                "type": None,
                "task": None,
                "label": SLOT_LABELS.get(name, name),
                "group": None,
                "canonical": name,
                "aliases": [],
                "connection": agents[name],
                "via": "authored",
                "write_slot": name,
                "inert": [],
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
