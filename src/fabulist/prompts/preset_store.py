"""Preset store (D18): prompt presets as event-log data on ``__presets__``.

The event log owns everything (D9): a *preset* is a named, editable pipeline —
ordered prompt modules + per-interaction flows + agent bindings (D19) + the
preset-carried directive rows. The store never keeps a side table: every write appends a
system event to the reserved :data:`PRESETS_ANCHOR` session and every read folds
those events *latest-wins per ``preset_id``* (the same shape
:mod:`fabulist.api.assistants_api` uses for ``__assistants__`` and
:mod:`fabulist.api.settings` uses for ``__settings__``).

Storage contract (fixed)
------------------------
Two system-event kinds ride the anchor session::

    {event: "preset", preset_id, name,
     modules: [PromptModule dicts],
     flows: {interaction: [PromptModule dicts]},
     agent_bindings: {interaction: {agent: str, overrides: dict}},
     planner: {cadence: int, steer: str, fields: [planner field dicts]},
     version: 1}

The ``planner`` block (W9.20) is the preset-carried steering-plane config:
its cadence in committed turns, its free-text steer, and the preset-defined
extra schema fields. It is emitted ONLY when it differs from the default
shape (:func:`default_planner_config`), so a preset written before W9.20 — and
every unedited preset — stays diff-free; an absent key reads as the defaults
(:func:`planner_config`), which is why no migration is needed.

    historian: {cadence: int}    # W9.23 §3.6: the off-turn proposal cadence

The ``historian`` block follows the ``planner`` block's contract exactly: it is
emitted ONLY when it differs from the default (:func:`default_historian_config`)
and an absent key reads as the default (:func:`historian_config`).

    {event: "preset_active", preset_id | null}   # latest wins; null/absent = default

A ``preset`` event for an existing ``preset_id`` supersedes it (append-only,
D10): PUT is implemented as just another append. ``preset_active`` names the
one active preset; no event (or a ``null`` payload) means the byte-faithful
default pipeline (``wiring`` compiles from the compiler path, untouched).

Per-interaction flows + back-compat (D18 completion)
-----------------------------------------------------
``flows`` is the per-interaction module map (``narrate`` / ``ooc`` / ``extract``
/ ``summarize`` / ``caption`` / ``assistant``, see
:data:`fabulist.prompts.modules.INTERACTIONS`). The payload keeps **both**
``modules`` and ``flows``: ``modules`` is the canonical ``narrate`` flow
(``modules == flows["narrate"]``), so the landed UI/API clients that read/write
only ``modules`` keep working unchanged. Reading a preset that carries only
``modules`` normalizes ``flows["narrate"] = modules`` (see
:meth:`Preset.__post_init__`).

Retired fields (read-tolerant, never originated). The pre-W6.3 ``toggles``
(``{directive_id: bool}``) and ``prompt_overrides`` (``ooc_system`` /
``extractor_system``) keys are no longer created by the store: the single
directive ``enabled`` source is now the preset's directive rows and the retired
override shim is gone (see :func:`compile_preset_flow`). A legacy payload still
folds on read (D4) and :func:`preset_to_json` re-emits either key ONLY while it
is non-empty, so ``scripts/migrate_directive_toggles.py`` /
``scripts/migrate_prompt_overrides.py`` can read the legacy value back over HTTP
and fold it into the durable row; a write that carries neither key drops them.

Module storage vs. engine seam
------------------------------
The contract stores modules as JSON ``list[dict]``. The sibling engine module
:func:`fabulist.prompts.modules.compile_preset_messages` consumes modules by
*attribute* (``module.type`` / ``module.enabled`` / ``module.content``), and
:func:`fabulist.wiring.make_narrator_message_builder.build` passes
``preset.modules`` straight through to it. :class:`PromptModuleDict` bridges the
two: it *is* a ``dict`` (JSON/round-trip faithful) that also answers attribute
access, so the production builder path works without copying or coercion.

Mapping seam (agent bindings, D19)
----------------------------------
:func:`fabulist.prompts.agent_sets.bindings_for` reads the active preset as a
mapping (``preset.get("agent_bindings")``). :class:`Preset` therefore registers
itself as a :class:`collections.abc.Mapping` over its dataclass fields, so the
live D19 path resolves bindings from the active preset; :meth:`Preset.get` and
``__getitem__`` read the fields. This is additive — attribute access (the wiring
seam) is unaffected.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import factory
from .modules import (
    DEFAULT_FLOWS,
    INTERACTIONS,
    PromptModule,
    compile_flow,
    from_default_flow,
)
from .parts import (
    Directive,
    normalize_directive_rows,
)
from .surfaces import factory_surfaces, normalize_surfaces

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

__all__ = [
    "ACTIVE_EVENT",
    "DEFAULT_PRESET_ID",
    "DELETE_EVENT",
    "DefaultSeedResult",
    "PLANNER_DEFAULT_CADENCE",
    "PLANNER_FIELD_TYPES",
    "PRESETS_ANCHOR",
    "PRESET_EVENT",
    "Preset",
    "PromptModuleDict",
    "active_preset",
    "append_preset",
    "commit_default_to_factory",
    "compile_preset_flow",
    "default_factory_status",
    "default_historian_config",
    "default_planner_config",
    "default_preset",
    "delete_preset",
    "ensure_default_preset",
    "get_preset",
    "historian_config",
    "list_presets",
    "normalize_historian_config",
    "normalize_planner_config",
    "planner_config",
    "preset_directives",
    "preset_from_json",
    "preset_to_json",
    "preset_topology",
    "reset_default_preset",
    "seed_from_default",
    "set_active",
    "set_default_historian",
    "set_default_planner",
    "set_default_topology",
    "stored_topology",
]

#: Reserved session id anchoring preset events (mirrors ``__settings__`` /
#: ``__assistants__``; hidden from the session list by the ``__`` filter in
#: :func:`fabulist.api.sessions._session_ids`).
PRESETS_ANCHOR = "__presets__"

#: System-event kind carrying one preset definition (latest wins per id).
PRESET_EVENT = "preset"

#: System-event kind naming the active preset (``preset_id`` may be null).
ACTIVE_EVENT = "preset_active"

#: System-event kind tombstoning a preset (latest wins; a later ``preset`` event
#: for the same id re-creates it). Used to make presets deletable while the
#: default preset stays undeletable.
DELETE_EVENT = "preset_deleted"

#: The well-known id of the persisted default preset (D26). Boot auto-seeds it
#: from the factory image ONLY when missing; no-active resolution falls back to
#: it; it cannot be deleted.
DEFAULT_PRESET_ID = "default"

#: W9.20: the planner (steering plane) re-runs every N committed turns by
#: default. The preset carries the value (:func:`planner_config`); an absent
#: ``planner`` block reads as this default.
PLANNER_DEFAULT_CADENCE = 5

#: W9.23 §3.6: the historian (the off-turn proposal lanes) re-runs every N
#: committed turns by default. Same storage contract as the planner: the preset
#: carries the value (:func:`historian_config`) and an absent ``historian`` block
#: reads as this default. The due RULE's own default is
#: :data:`fabulist.state.historian.DEFAULT_HISTORIAN_CADENCE` — the same
#: preset-default / rule-default split the planner carries, kept because this
#: module is imported before the graph layer.
HISTORIAN_DEFAULT_CADENCE = 10

#: W9.20: the types a preset-defined planner extra field may declare. The
#: planner's response schema is dynamic — the request's field list defines it —
#: and these three cover the scalars a JSON plan payload can carry.
PLANNER_FIELD_TYPES: frozenset[str] = frozenset({"string", "number", "boolean"})

#: A preset's stored module fields, in serialization order.
_MODULE_FIELDS = ("id", "name", "type", "role", "content", "enabled")

#: Read-side rename shim (assistant rename): a persisted preset written before the
#: rename carries the interaction key ``sidekick`` in ``flows`` / ``agent_bindings``.
#: Reads normalize it to the canonical ``assistant`` so the SAME preset keeps
#: compiling with ZERO data migration. Only the new spelling is ever written.
_LEGACY_INTERACTION_ALIASES: dict[str, str] = {"sidekick": "assistant"}

#: Legacy per-flow module-id prefix -> canonical prefix. The pre-rename assistant
#: flow's rows were ``sidekick_system`` / ``sidekick_history`` / ``sidekick_user``;
#: reads rewrite the prefix to ``assistant_``.
_LEGACY_MODULE_ID_PREFIXES: tuple[tuple[str, str], ...] = (("sidekick_", "assistant_"),)

#: W9.20: a planner extra field's ``id`` must be a plain identifier — it becomes
#: a keyword in the planner's response schema and a key in the stored plan
#: payload, so anything else is refused at the write door.
_PLANNER_ID_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _normalize_interaction(interaction: str) -> str:
    """Canonical interaction key (the ``sidekick`` -> ``assistant`` read shim)."""
    return _LEGACY_INTERACTION_ALIASES.get(interaction, interaction)


def _normalize_module_id(module_id: str) -> str:
    """Rewrite a legacy assistant module id (``sidekick_*``) to its new spelling."""
    for legacy, canonical in _LEGACY_MODULE_ID_PREFIXES:
        if module_id.startswith(legacy):
            return canonical + module_id[len(legacy) :]
    return module_id


# -- planner config (W9.20) --------------------------------------------------


def default_planner_config() -> dict[str, Any]:
    """A FRESH copy of the default planner config (never a shared object).

    The default is a value, not an inherit signal: a preset that carries no
    ``planner`` row behaves exactly like this dict (see
    :func:`planner_config`), so no migration and no factory-version bump.
    """
    return {"cadence": PLANNER_DEFAULT_CADENCE, "steer": "", "fields": []}


def _planner_field_row(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one preset-defined planner extra field (``{id,title,type,
    hint,required}``).

    Well-typed values are kept, missing/badly typed ones fall back to the field
    defaults; unknown extra keys are preserved (the log is forward-compatible,
    D4). ``required`` is only kept when it already IS a bool — a coerced
    ``bool("false")`` would silently flip the owner's intent (that value is
    reported by :func:`normalize_planner_config` instead).
    """
    required = raw.get("required")
    row: dict[str, Any] = {
        "id": str(raw.get("id") or ""),
        "title": str(raw.get("title") or ""),
        "type": str(raw.get("type") or "string"),
        "hint": str(raw.get("hint") or ""),
        "required": required if isinstance(required, bool) else False,
    }
    for key, value in raw.items():
        if key not in row:
            row[key] = value
    return row


def _normalize_planner(raw: Any) -> dict[str, Any]:
    """The stored shape of a planner config: defaults filled, keys typed.

    Tolerant by design (the same discipline as ``normalize_surfaces``): an
    absent / non-mapping value reads as the defaults and a badly typed key
    falls back to its default, so a reader never has to guard. ``fields`` rows
    keep their declared order; non-mapping rows are dropped and each retained
    row is normalized by :func:`_planner_field_row`. Idempotent.

    This is the READ/serialize normalizer. Value validation (cadence range,
    duplicate / non-identifier field ids, unknown types) is
    :func:`normalize_planner_config`'s job — the write door.
    """
    config = default_planner_config()
    if not isinstance(raw, Mapping):
        return config
    cadence = raw.get("cadence")
    if isinstance(cadence, int) and not isinstance(cadence, bool):
        config["cadence"] = cadence
    steer = raw.get("steer")
    if isinstance(steer, str):
        config["steer"] = steer
    fields = raw.get("fields")
    if isinstance(fields, (list, tuple)):
        config["fields"] = [
            _planner_field_row(row) for row in fields if isinstance(row, Mapping)
        ]
    return config


def planner_config(preset: Any) -> dict[str, Any]:
    """The preset's planner config with the defaults merged in — the ONE read.

    An absent ``planner`` row is indistinguishable from the default dict (that
    is the storage contract: no migration, no factory-version bump), and a
    partial config (say only ``cadence``) reads with the other keys defaulted.
    Tolerant of ``None``, a preset mapping/export payload, or a preset object
    without the field. Returns a fresh dict, so a caller mutating it can never
    reach the stored preset.
    """
    raw: Any = None
    if isinstance(preset, Mapping):
        raw = preset.get("planner")
    if raw is None:
        raw = getattr(preset, "planner", None)
    return _normalize_planner(raw)


def normalize_planner_config(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Validate a planner config; return ``(normalized config, errors)``.

    ``config`` is ALWAYS the usable, default-filled shape
    (:func:`_normalize_planner`), so a caller can store it even while
    reporting errors; ``errors`` is empty for a valid value and names each
    broken rule otherwise (``"cadence must be >= 1, got 0"``). The rules:

    * ``cadence`` — an integer >= 1 (the planner re-runs every N committed turns;
      0 would mean every turn);
    * ``steer`` — a string;
    * ``fields`` — a list of ``{id, title, type, hint, required}`` rows, with a
      unique identifier-safe ``id`` (``[A-Za-z_][A-Za-z0-9_]*``), a ``type`` in
      :data:`PLANNER_FIELD_TYPES`, and a boolean ``required``.

    This is the write-door contract (wave B / the API call it before writing);
    it never raises and never mutates ``raw``.
    """
    errors: list[str] = []
    config = _normalize_planner(raw)
    if raw is None:
        return config, errors
    if not isinstance(raw, Mapping):
        return config, [f"planner must be an object, got {type(raw).__name__}"]
    cadence = raw.get("cadence", PLANNER_DEFAULT_CADENCE)
    if isinstance(cadence, bool) or not isinstance(cadence, int):
        errors.append(f"cadence must be an integer, got {cadence!r}")
    elif cadence < 1:
        errors.append(f"cadence must be >= 1, got {cadence}")
    steer = raw.get("steer", "")
    if not isinstance(steer, str):
        errors.append(f"steer must be a string, got {type(steer).__name__}")
    fields = raw.get("fields", [])
    if not isinstance(fields, (list, tuple)):
        errors.append(f"fields must be a list, got {type(fields).__name__}")
        return config, errors
    seen: set[str] = set()
    for index, row in enumerate(fields):
        if not isinstance(row, Mapping):
            errors.append(f"fields[{index}] must be an object")
            continue
        field_id = row.get("id")
        if not isinstance(field_id, str) or not _PLANNER_ID_RE.fullmatch(field_id):
            errors.append(
                f"fields[{index}].id must be an identifier "
                f"([A-Za-z_][A-Za-z0-9_]*), got {field_id!r}"
            )
        elif field_id in seen:
            errors.append(f"fields[{index}].id duplicates {field_id!r}")
        else:
            seen.add(field_id)
        row_type = row.get("type", "string")
        if not isinstance(row_type, str) or row_type not in PLANNER_FIELD_TYPES:
            errors.append(
                f"fields[{index}].type must be one of "
                f"{sorted(PLANNER_FIELD_TYPES)}, got {row_type!r}"
            )
        if "required" in row and not isinstance(row["required"], bool):
            errors.append(
                f"fields[{index}].required must be a boolean, "
                f"got {row['required']!r}"
            )
    return config, errors


# -- historian config (W9.23 §3.6) -------------------------------------------


def default_historian_config() -> dict[str, Any]:
    """A FRESH copy of the default historian config (never a shared object).

    A value, not an inherit signal (the planner's contract): a preset carrying
    no ``historian`` row behaves exactly like this dict, so an unedited install
    needs no migration and the factory image stays diff-free.
    """
    return {"cadence": HISTORIAN_DEFAULT_CADENCE}


def _normalize_historian(raw: Any) -> dict[str, Any]:
    """The stored shape of a historian config: defaults filled, cadence typed.

    Tolerant by design (the planner normalizer's discipline): an absent or
    non-mapping value reads as the defaults and a badly typed cadence falls back
    to its default, so a reader never has to guard. Idempotent. Value validation
    is :func:`normalize_historian_config`'s job — the write door.
    """
    config = default_historian_config()
    if not isinstance(raw, Mapping):
        return config
    cadence = raw.get("cadence")
    if isinstance(cadence, int) and not isinstance(cadence, bool):
        config["cadence"] = cadence
    return config


def historian_config(preset: Any) -> dict[str, Any]:
    """The preset's historian config with the defaults merged in — the ONE read.

    An absent ``historian`` row is indistinguishable from the default dict (the
    storage contract: no migration), so the engine's live cadence reader and
    ``GET /api/prompts/historian`` go through here. Tolerant of ``None``, a
    preset mapping/export payload, or a preset object without the field. Returns
    a fresh dict, so a caller mutating it can never reach the stored preset.
    """
    raw: Any = None
    if isinstance(preset, Mapping):
        raw = preset.get("historian")
    if raw is None:
        raw = getattr(preset, "historian", None)
    return _normalize_historian(raw)


def normalize_historian_config(raw: Any) -> tuple[dict[str, Any], list[str]]:
    """Validate a historian config; return ``(normalized config, errors)``.

    ``config`` is ALWAYS the usable, default-filled shape, so a caller can store
    it even while reporting errors. The one rule: ``cadence`` is an integer >= 1
    (the historian re-runs every N committed turns; 0 would mean every turn).

    This is the write-door contract (``PUT /api/prompts/historian`` calls it
    before writing); it never raises and never mutates ``raw``.
    """
    errors: list[str] = []
    config = _normalize_historian(raw)
    if raw is None:
        return config, errors
    if not isinstance(raw, Mapping):
        return config, [f"historian must be an object, got {type(raw).__name__}"]
    cadence = raw.get("cadence", HISTORIAN_DEFAULT_CADENCE)
    if isinstance(cadence, bool) or not isinstance(cadence, int):
        errors.append(f"cadence must be an integer, got {cadence!r}")
    elif cadence < 1:
        errors.append(f"cadence must be >= 1, got {cadence}")
    return config, errors


class PromptModuleDict(dict):
    """A stored :class:`~fabulist.prompts.modules.PromptModule` as a ``dict``.

    It *is* a ``dict`` (so JSON export/import and the contract's ``list[dict]``
    shape are exact) that also answers attribute access. This bridges the two
    seams that meet at ``preset.modules``: the log/wire layer treats modules as
    plain dicts, while :func:`fabulist.prompts.modules.compile_preset_messages`
    reads ``module.type`` / ``module.enabled`` / ``module.content`` and
    :func:`fabulist.wiring.make_narrator_message_builder.build` forwards them
    untouched.
    """

    def __getattr__(self, name: str) -> Any:
        # Only called when normal attribute lookup fails (dict has no such
        # attribute), so stored module fields resolve as attributes.
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def _module_to_dict(module: PromptModule | Mapping[str, Any]) -> PromptModuleDict:
    """Normalize a ``PromptModule`` (or dict) into a :class:`PromptModuleDict`.

    Missing optional fields get the module defaults (``role='system'``,
    ``content=''``, ``enabled=True``) so a hand-authored or imported row still
    compiles. Unknown extra keys are preserved (log is forward-compatible, D4).
    """
    if isinstance(module, PromptModule):
        raw: dict[str, Any] = {
            "id": module.id,
            "name": module.name,
            "type": module.type,
            "role": module.role,
            "content": module.content,
            "enabled": module.enabled,
        }
    else:
        raw = {str(key): value for key, value in dict(module).items()}
    normalized = PromptModuleDict()
    normalized["id"] = _normalize_module_id(str(raw.get("id") or uuid.uuid4().hex))
    normalized["name"] = str(raw.get("name") or "")
    normalized["type"] = str(raw.get("type") or "text")
    normalized["role"] = str(raw.get("role") or "system")
    normalized["content"] = str(raw.get("content") or "")
    normalized["enabled"] = bool(raw.get("enabled", True))
    for key, value in raw.items():
        if key not in normalized:
            normalized[key] = value
    return normalized


@dataclass(frozen=True)
class Preset(Mapping[str, Any]):
    """One named prompt pipeline: modules + toggles + overrides + bindings.

    Frozen so a folded read cannot be mutated in place (the log stays truth,
    D9). It is also a :class:`collections.abc.Mapping` over its field names so
    :func:`fabulist.prompts.agent_sets.bindings_for` can read
    ``preset.get("agent_bindings")``; direct attribute access (the wiring seam)
    is unchanged.
    """

    preset_id: str
    name: str
    modules: list[PromptModuleDict] = field(default_factory=list)
    flows: dict[str, list[PromptModuleDict]] = field(default_factory=dict)
    toggles: dict[str, bool] = field(default_factory=dict)
    prompt_overrides: dict[str, str] = field(default_factory=dict)
    agent_bindings: dict[str, Any] = field(default_factory=dict)
    version: int = 1
    #: D26: the persisted default row carries ``is_default=True`` and the factory
    #: version it was seeded from (``0`` = unknown / pre-D26). Ordinary presets
    #: keep the defaults.
    is_default: bool = False
    factory_version: int = 0
    #: D27 token surfaces (library doc / recalled copy / regexes / history
    #: knobs / directive text). Empty -> every loader reads its code constant.
    surfaces: dict[str, Any] = field(default_factory=dict)
    #: Ordered directive rows (preset-carried catalog). Empty -> INHERIT the
    #: code catalog (:data:`fabulist.prompts.parts.DEFAULT_DIRECTIVES`);
    #: every existing preset stays diff-free until the owner materializes it.
    directives: list[dict[str, Any]] = field(default_factory=list)
    #: Issue #50: the canonical anti-slop phrase list (``{"version", "source",
    #: "sections": [...]}``). The artifact is the list's home; this field is
    #: what lets the factory commit path carry an owner's edits through a
    #: preset save without dropping them. Empty -> no list (there is no code
    #: list to inherit).
    anti_slop: dict[str, Any] = field(default_factory=dict)
    #: The turn topology (W9.6). Empty -> INHERIT the canonical topology
    #: authored in code (:data:`fabulist.graph.graph.CANONICAL_TOPOLOGY`), the
    #: same seed-only-if-missing discipline as ``directives``.
    topology: dict[str, Any] = field(default_factory=dict)
    #: W9.20 planner (steering plane): ``{"cadence": int, "steer": str,
    #: "fields": [{"id", "title", "type", "hint", "required"}]}``. Unlike
    #: ``topology``/``directives`` the default is a VALUE, not an inherit
    #: signal: an absent row behaves exactly like this dict, so readers go
    #: through :func:`planner_config` and nothing needs a migration.
    planner: dict[str, Any] = field(default_factory=default_planner_config)
    #: W9.23 §3.6 historian (the off-turn proposal lanes): ``{"cadence": int}``.
    #: Same value-not-inherit contract as ``planner`` — an absent row behaves
    #: exactly like :func:`default_historian_config`, so nothing migrates.
    historian: dict[str, Any] = field(default_factory=default_historian_config)

    def __post_init__(self) -> None:
        """Canonicalize ``flows``: ``modules`` is the ``narrate`` flow.

        Back-compat: a preset carrying only ``modules`` (every landed client)
        gets ``flows["narrate"] = modules``. When only a flow carries narrate
        rows, ``modules`` is filled from ``flows["narrate"]``. The frozen
        dataclass is updated with ``object.__setattr__``; the two fields share
        one list object so ``modules == flows["narrate"]`` always holds.
        """
        modules = self.modules
        flows = {key: list(rows) for key, rows in self.flows.items()}
        if modules:
            flows["narrate"] = modules
        elif flows.get("narrate"):
            modules = flows["narrate"]
        else:
            flows["narrate"] = modules
        object.__setattr__(self, "modules", modules)
        object.__setattr__(self, "flows", flows)

    # -- Mapping over dataclass fields (agent_sets D19 seam) ----------------
    def __getitem__(self, key: str) -> Any:
        if key in self.__dataclass_fields__:
            return getattr(self, key)
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(self.__dataclass_fields__)

    def __len__(self) -> int:
        return len(self.__dataclass_fields__)


# -- serialization -----------------------------------------------------------


def preset_to_json(preset: Preset) -> dict[str, Any]:
    """Export shape of a preset: the exact ``preset`` event payload.

    Modules are emitted as plain dicts (each :class:`PromptModuleDict` copies to
    a ``dict``), so the result is JSON-serializable and round-trips through
    :func:`preset_from_json`. ``directives`` is emitted ONLY when non-empty —
    an empty list means "inherit the code catalog", and omitting the key keeps
    unedited presets and the factory artifact diff-free.
    """
    payload: dict[str, Any] = {
        "event": PRESET_EVENT,
        "preset_id": preset.preset_id,
        "name": preset.name,
        "modules": [dict(module) for module in preset.modules],
        "flows": {
            interaction: [dict(module) for module in rows]
            for interaction, rows in preset.flows.items()
        },
        "agent_bindings": dict(preset.agent_bindings),
        "version": preset.version,
        "is_default": preset.is_default,
        "factory_version": preset.factory_version,
        "surfaces": normalize_surfaces(preset.surfaces),
    }
    # Issue #50: emit the canonical list only when present (absent = no list).
    from fabulist.antislop import normalize_anti_slop

    anti_slop = normalize_anti_slop(preset.anti_slop)
    if anti_slop:
        payload["anti_slop"] = anti_slop
    if preset.directives:
        payload["directives"] = [dict(row) for row in preset.directives]
    # Same discipline as ``directives``: an empty topology means "inherit the
    # canonical one", so unedited presets stay diff-free.
    if preset.topology:
        payload["topology"] = {
            "nodes": [dict(node) for node in preset.topology.get("nodes") or []],
            "edges": [dict(edge) for edge in preset.topology.get("edges") or []],
        }
    # Same diff-free discipline, one step further: ``planner`` is emitted ONLY
    # when it differs from the default shape, because an absent key already
    # reads as that shape (:func:`planner_config`). An unedited preset — and
    # every preset written before W9.20 — exports byte-identically to today.
    planner = _normalize_planner(preset.planner)
    if planner != default_planner_config():
        payload["planner"] = planner
    # The historian's block follows the same emit-only-when-differs rule: an
    # absent key already reads as the default (:func:`historian_config`).
    historian = _normalize_historian(preset.historian)
    if historian != default_historian_config():
        payload["historian"] = historian
    # Retired-but-read-tolerant keys (W6.3): emitted ONLY when a legacy payload
    # still carries them, so the migrations can read them back over HTTP and
    # fold/clear them. Nothing originates new values — a write with neither set
    # drops the keys. See the module docstring.
    if preset.toggles:
        payload["toggles"] = dict(preset.toggles)
    if preset.prompt_overrides:
        payload["prompt_overrides"] = dict(preset.prompt_overrides)
    return payload


def _flows_from_json(raw: Any) -> dict[str, list[PromptModuleDict]]:
    """Normalize a stored ``flows`` mapping into ``{interaction: [module dicts]}``.

    Tolerant by design: a missing / non-mapping value yields ``{}`` (the caller's
    :meth:`Preset.__post_init__` then restores ``flows["narrate"]`` from
    ``modules``), non-list interaction values are skipped, and each retained row
    is normalized by :func:`_module_to_dict`. A legacy ``sidekick`` key is folded
    onto its canonical ``assistant`` key (the new key wins when both are
    present), so a pre-rename preset compiles unchanged.
    """
    flows: dict[str, list[PromptModuleDict]] = {}
    legacy: dict[str, list[PromptModuleDict]] = {}
    if not isinstance(raw, Mapping):
        return flows
    for interaction, rows in raw.items():
        if not isinstance(rows, (list, tuple)):
            continue
        key = _normalize_interaction(str(interaction))
        normalized = [
            _module_to_dict(row)
            for row in rows
            if isinstance(row, (Mapping, PromptModule))
        ]
        if key == str(interaction):
            flows[key] = normalized
        else:
            legacy[key] = normalized
    for key, rows in legacy.items():
        flows.setdefault(key, rows)
    return flows


def preset_from_json(data: Mapping[str, Any]) -> Preset:
    """Build a :class:`Preset` from an export payload (inverse of to_json).

    Tolerant by design: a missing ``preset_id`` is minted, absent sub-mappings
    default to ``{}``, and each module row is normalized. Extra keys are
    ignored (only the declared surface is carried).
    """
    preset_id = data.get("preset_id")
    if not isinstance(preset_id, str) or not preset_id:
        preset_id = uuid.uuid4().hex
    modules = [
        _module_to_dict(row) for row in (data.get("modules") or []) if isinstance(row, (Mapping, PromptModule))
    ]
    flows = _flows_from_json(data.get("flows"))
    toggles_raw = data.get("toggles")
    toggles = (
        {str(key): bool(value) for key, value in toggles_raw.items()}
        if isinstance(toggles_raw, Mapping)
        else {}
    )
    overrides_raw = data.get("prompt_overrides")
    overrides = (
        {str(key): str(value) for key, value in overrides_raw.items()}
        if isinstance(overrides_raw, Mapping)
        else {}
    )
    bindings_raw = data.get("agent_bindings")
    bindings: dict[str, Any] = {}
    if isinstance(bindings_raw, Mapping):
        legacy_bindings: dict[str, Any] = {}
        for interaction, binding in bindings_raw.items():
            key = _normalize_interaction(str(interaction))
            if key == str(interaction):
                bindings[key] = binding
            else:
                legacy_bindings[key] = binding
        for key, binding in legacy_bindings.items():
            bindings.setdefault(key, binding)
    surfaces = normalize_surfaces(data.get("surfaces"))
    # Absent / non-list / empty -> []: an EMPTY list means "inherit the code
    # catalog" (see :func:`preset_directives`), so nothing is ever baked here.
    directives = normalize_directive_rows(data.get("directives"))
    # Issue #50: the artifact-owned anti-slop list rides the preset payload so
    # a commit round-trips it; absent/empty stays empty.
    from fabulist.antislop import normalize_anti_slop

    anti_slop = normalize_anti_slop(data.get("anti_slop"))
    topology_raw = data.get("topology")
    topology: dict[str, Any] = (
        _normalize_topology(topology_raw) if isinstance(topology_raw, Mapping) else {}
    )
    version_raw = data.get("version")
    version = version_raw if isinstance(version_raw, int) and not isinstance(version_raw, bool) else 1
    factory_version_raw = data.get("factory_version")
    factory_version = (
        factory_version_raw
        if isinstance(factory_version_raw, int) and not isinstance(factory_version_raw, bool)
        else 0
    )
    return Preset(
        preset_id=preset_id,
        name=str(data.get("name") or ""),
        modules=modules,
        flows=flows,
        toggles=toggles,
        prompt_overrides=overrides,
        agent_bindings=bindings,
        version=version,
        is_default=bool(data.get("is_default", False)),
        factory_version=factory_version,
        surfaces=surfaces,
        directives=directives,
        anti_slop=anti_slop,
        topology=topology,
        planner=_normalize_planner(data.get("planner")),
        historian=_normalize_historian(data.get("historian")),
    )


def preset_directives(preset: Any) -> list[Directive] | None:
    """The preset-carried directive catalog, or ``None`` = inherit the code one.

    ``None`` (never ``[]``) is the inherit signal — an empty-list base would
    render ZERO directives for every existing preset. Returns the normalized
    rows as frozen :class:`~fabulist.prompts.parts.Directive` blocks, in the
    stored list order (the order the preset renders). Tolerant of ``None``, a
    mapping payload, or a preset without the field.
    """
    if preset is None:
        return None
    raw: Any = None
    if isinstance(preset, Mapping):
        raw = preset.get("directives")
    if raw is None:
        raw = getattr(preset, "directives", None)
    if not isinstance(raw, (list, tuple)) or not raw:
        return None
    known = frozenset(
        {
            "id",
            "name",
            "content",
            "priority",
            "tier",
            "core",
            "protected",
            "enabled",
            "description",
        }
    )
    out: list[Directive] = []
    for row in normalize_directive_rows(raw):
        out.append(Directive(**{key: row[key] for key in known if key in row}))
    return out


def _normalize_topology(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The stored shape of a topology: ``{"nodes": [dict], "edges": [dict]}``.

    Tolerant by design (a topology is preset data and may be hand-authored):
    non-mapping rows are dropped, a missing/non-list ``nodes``/``edges`` reads
    as empty — which :func:`fabulist.graph.contracts.validate_topology` then
    rejects. Unknown extra keys on a node/edge row are preserved (D4).
    """
    nodes = raw.get("nodes")
    edges = raw.get("edges")
    return {
        "nodes": (
            [dict(node) for node in nodes if isinstance(node, Mapping)]
            if isinstance(nodes, (list, tuple))
            else []
        ),
        "edges": (
            [dict(edge) for edge in edges if isinstance(edge, Mapping)]
            if isinstance(edges, (list, tuple))
            else []
        ),
    }


def canonical_topology() -> dict[str, Any]:
    """The code-authored turn topology (W9.6) — the inherit default.

    Imported lazily: :mod:`fabulist.graph` pulls in the engine and this module
    is imported early by ``wiring``; a module-level import would close the cycle
    (``graph`` -> ... -> ``prompts`` -> ``preset_store`` -> ``graph``).
    """
    from fabulist.graph.graph import CANONICAL_TOPOLOGY

    return {
        "nodes": [dict(node) for node in CANONICAL_TOPOLOGY["nodes"]],
        "edges": [dict(edge) for edge in CANONICAL_TOPOLOGY["edges"]],
    }


def preset_topology(
    preset: Any, templates: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The preset's turn topology, or the canonical one when it carries none.

    ``{}`` (never ``None``) is the inherit signal, mirroring
    :func:`preset_directives`: a preset without an authored topology — every
    preset written before W9.6 — builds the canonical graph, so nothing changes
    for it. Tolerant of ``None``, a mapping payload, or a preset without the
    field.

    A carried topology that BREAKS an invariant
    (:func:`fabulist.graph.contracts.validate_topology` — e.g. a hand-edited
    log naming a dropped node) falls back to the canonical one too, so a bad
    value can never stop the engine booting: ``build_graph`` raises on an
    unknown node id, and this is the read that keeps it from ever seeing one.
    Nothing is written and nothing raises; the write door
    (:mod:`fabulist.api.topology_api`) is what refuses such a value.

    ``templates`` is the node-template library (``{template id: implements}``,
    see :func:`fabulist.graph.node_templates.template_behaviours`): a topology
    whose nodes are template references is judged by the behaviour each template
    implements (W9.10), so the engine builds it rather than falling back. Callers
    that cannot reach the log pass ``None``, which keeps a template node
    unresolved — and therefore a reason to fall back, never a silent guess.
    """
    raw: Any = None
    if isinstance(preset, Mapping):
        raw = preset.get("topology")
    if raw is None:
        raw = getattr(preset, "topology", None)
    if isinstance(raw, Mapping) and raw.get("nodes"):
        topology = _normalize_topology(raw)
        # W9.23 §3.6.4: a save made while the W9.22 proposal stage existed is
        # STRIPPED first — the stage's node ids are unknown now, so validating
        # the stored shape as written would fall back to the WHOLE canonical
        # topology and discard the owner's other edits. The strip refuses
        # (ValueError) rather than guess at a hand-edited shape, and a refusal
        # is the same canonical fallback a broken save always took (D74).
        try:
            topology = _strip_retired_stage(topology)
        except ValueError:
            logger.warning(
                "stored topology refused by the W9.23 proposal-stage strip; "
                "using the canonical topology",
                exc_info=True,
            )
            return canonical_topology()
        if not _topology_violations(topology, templates):
            return topology
    return canonical_topology()


def stored_topology(preset: Any) -> dict[str, Any]:
    """The topology a preset CARRIES (no validation, no fallback); ``{}`` if none.

    The editor's read (:mod:`fabulist.api.topology_api`) needs the value as
    written so a broken one can be seen and repaired, while
    :func:`preset_topology` is the engine's read (validated, canonical
    fallback).
    """
    raw: Any = None
    if isinstance(preset, Mapping):
        raw = preset.get("topology")
    if raw is None:
        raw = getattr(preset, "topology", None)
    if isinstance(raw, Mapping) and raw.get("nodes"):
        return _normalize_topology(raw)
    return {}


def _strip_retired_stage(topology: Mapping[str, Any]) -> dict[str, Any]:
    """Apply the compile-time stored-topology strips (W9.23 §3.6.4).

    One strip today: the retired W9.22 proposal stage
    (:func:`fabulist.graph.graph.strip_legacy_topology`), whose node ids no
    longer exist. The import is lazy for the reason documented on
    :func:`canonical_topology`. A shape the strip will not touch raises
    :class:`ValueError`, which the caller turns into the canonical fallback.
    """
    from fabulist.graph.graph import strip_legacy_topology

    return strip_legacy_topology(topology)


def _topology_violations(
    topology: Mapping[str, Any], templates: Mapping[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Invariant violations for ``topology`` (``templates`` = the W9.10 library).

    The import is lazy for the reason documented on :func:`canonical_topology`:
    a module-level :mod:`fabulist.graph` import would close the cycle
    ``graph -> ... -> prompts -> preset_store -> graph``.
    """
    from fabulist.graph.contracts import validate_topology

    return validate_topology(topology, templates)


# -- folds -------------------------------------------------------------------


def _fold_presets(log: "EventLog") -> "dict[str, Preset]":
    """Fold ``preset``/``preset_deleted`` events into ``{preset_id: Preset}``.

    Insertion order is preserved (first definition first) so listing is stable;
    a later ``preset`` event for the same id replaces the earlier one *value*
    without moving it (latest wins, D10). A ``preset_deleted`` tombstone removes
    the id; a later ``preset`` event re-creates it (walk order, not set order).
    """
    found: dict[str, Preset] = {}
    for event in log.get_events(PRESETS_ANCHOR):
        if event.type != "system":
            continue
        payload = event.payload
        if not isinstance(payload, dict):
            continue
        kind = payload.get("event")
        if kind == PRESET_EVENT:
            preset = preset_from_json(payload)
            found[preset.preset_id] = preset
        elif kind == DELETE_EVENT:
            preset_id = payload.get("preset_id")
            if isinstance(preset_id, str):
                found.pop(preset_id, None)
    return found


def _active_id(log: "EventLog") -> str | None:
    """Latest ``preset_active`` id, or ``None`` for the default pipeline."""
    active: str | None = None
    for event in log.get_events(PRESETS_ANCHOR):
        if event.type != "system":
            continue
        payload = event.payload
        if not isinstance(payload, dict) or payload.get("event") != ACTIVE_EVENT:
            continue
        value = payload.get("preset_id")
        active = value if isinstance(value, str) and value else None
    return active


def list_presets(log: "EventLog") -> list[Preset]:
    """All presets, in definition order (newest value per ``preset_id``)."""
    return list(_fold_presets(log).values())


def get_preset(log: "EventLog", preset_id: str) -> Preset | None:
    """One preset by id, or ``None`` when never defined."""
    return _fold_presets(log).get(preset_id)


def active_preset(log: "EventLog") -> Preset | None:
    """The preset a turn compiles through (D26 no-active resolution).

    With no ``preset_active`` pointer the **persisted default** is returned when
    it exists (boot-seeded from the factory image), so editing the default IS
    editing default behavior. When no default row exists either — a pre-boot log
    or a hand-built test log — ``None`` is returned and the caller compiles the
    code default flow (the factory image is the source of truth, D26).

    An ``active`` id that names no known preset degrades to the default (a stale
    pointer must never break a live turn); a deleted default id also degrades to
    ``None``.
    """
    active = _active_id(log)
    if active is not None:
        preset = get_preset(log, active)
        if preset is not None:
            return preset
    return get_preset(log, DEFAULT_PRESET_ID)


# -- writes ------------------------------------------------------------------


def append_preset(log: "EventLog", data: Mapping[str, Any]) -> str:
    """Append one ``preset`` event; return its ``preset_id`` (minted if absent).

    ``data`` is the export shape (``preset_from_json``); the canonical payload
    is what lands in the log, so a later fold returns the same value.
    """
    preset_id = data.get("preset_id")
    if not isinstance(preset_id, str) or not preset_id:
        preset_id = uuid.uuid4().hex
    preset = preset_from_json({**dict(data), "preset_id": preset_id})
    log.append_event(PRESETS_ANCHOR, "system", "user", preset_to_json(preset))
    return preset_id


def set_default_topology(log: "EventLog", topology: Mapping[str, Any]) -> Preset:
    """Persist ``topology`` onto the persisted DEFAULT preset (append, D9/D10).

    The one write behind ``PUT /api/prompts/topology``. Same event discipline as
    every other preset edit: the whole row is re-appended with the new topology
    (the prior value stays queryable), so the topology is preset DATA (R10), not
    a side table. An absent default row is created from the factory image (the
    payload boot would have written) instead of being refused, so the write door
    needs no prior boot.

    This is a STORE write, not a gate: callers validate first
    (:func:`fabulist.graph.contracts.validate_topology`);
    :func:`preset_topology` is the read-side guard that falls back to the
    canonical topology for anything invalid, so a bad value can never reach
    ``build_graph``.
    """
    existing = get_preset(log, DEFAULT_PRESET_ID)
    if existing is not None:
        payload = preset_to_json(existing)
    else:
        image = factory.factory_image()
        payload = dict(image.payload)
        payload.setdefault("name", "default")
        payload["factory_version"] = image.factory_version
    payload["preset_id"] = DEFAULT_PRESET_ID
    payload["is_default"] = True
    payload["topology"] = _normalize_topology(topology)
    append_preset(log, payload)
    row = get_preset(log, DEFAULT_PRESET_ID)
    assert row is not None  # just appended
    return row


def set_default_planner(log: "EventLog", config: Mapping[str, Any]) -> Preset:
    """Persist the planner config onto the persisted DEFAULT preset (append, D9).

    The one write behind ``PUT /api/prompts/planner`` (W9.20 §7). Same event
    discipline and the same seed-only-if-missing shape as
    :func:`set_default_topology`: the whole default row is re-appended with the
    new planner block (the prior value stays queryable), and an absent default
    row is created from the factory image instead of being refused, so the
    write door needs no prior boot.

    This is a STORE write, not a gate: callers validate first
    (:func:`normalize_planner_config`); :func:`planner_config` is the read-side
    normalizer that fills the defaults for anything absent, so a partial value
    can never reach a reader unguarded.
    """
    existing = get_preset(log, DEFAULT_PRESET_ID)
    if existing is not None:
        payload = preset_to_json(existing)
    else:
        image = factory.factory_image()
        payload = dict(image.payload)
        payload.setdefault("name", "default")
        payload["factory_version"] = image.factory_version
    payload["preset_id"] = DEFAULT_PRESET_ID
    payload["is_default"] = True
    payload["planner"] = _normalize_planner(config)
    append_preset(log, payload)
    row = get_preset(log, DEFAULT_PRESET_ID)
    assert row is not None  # just appended
    return row


def set_default_historian(log: "EventLog", config: Mapping[str, Any]) -> Preset:
    """Persist the historian config onto the persisted DEFAULT preset (append, D9).

    The one write behind ``PUT /api/prompts/historian`` (W9.23 §3.6.7), the
    :func:`set_default_planner` twin: the whole default row is re-appended with
    the new ``historian`` block (the prior value stays queryable), and an absent
    default row is created from the factory image instead of being refused, so
    the write door needs no prior boot.

    This is a STORE write, not a gate: callers validate first
    (:func:`normalize_historian_config`); :func:`historian_config` is the
    read-side normalizer that fills the default for anything absent.
    """
    existing = get_preset(log, DEFAULT_PRESET_ID)
    if existing is not None:
        payload = preset_to_json(existing)
    else:
        image = factory.factory_image()
        payload = dict(image.payload)
        payload.setdefault("name", "default")
        payload["factory_version"] = image.factory_version
    payload["preset_id"] = DEFAULT_PRESET_ID
    payload["is_default"] = True
    payload["historian"] = _normalize_historian(config)
    append_preset(log, payload)
    row = get_preset(log, DEFAULT_PRESET_ID)
    assert row is not None  # just appended
    return row


def set_active(log: "EventLog", preset_id: str | None) -> None:
    """Point the active pointer at ``preset_id`` (or clear it with ``None``).

    Raises :class:`ValueError` when a non-null id names no known preset, so a
    typo is rejected at the door instead of leaving a dangling pointer. The
    write is itself an append (``preset_active``), latest wins.
    """
    if preset_id is not None and get_preset(log, preset_id) is None:
        raise ValueError(f"unknown preset_id: {preset_id!r}")
    log.append_event(
        PRESETS_ANCHOR,
        "system",
        "user",
        {"event": ACTIVE_EVENT, "preset_id": preset_id},
    )


def delete_preset(log: "EventLog", preset_id: str) -> None:
    """Tombstone a preset (latest wins; a later append re-creates it).

    The persisted **default** preset is undeletable (D26): passing
    :data:`DEFAULT_PRESET_ID` raises :class:`ValueError` (the API maps that to
    ``403``). An unknown/never-defined id raises :class:`ValueError` too (mapped
    to ``404``) so a typo does not silently write a tombstone. The active
    pointer is left alone; :func:`active_preset` degrades gracefully if it
    pointed at the deleted id.
    """
    if preset_id == DEFAULT_PRESET_ID:
        raise ValueError("default preset cannot be deleted")
    if get_preset(log, preset_id) is None:
        raise ValueError(f"unknown preset_id: {preset_id!r}")
    log.append_event(
        PRESETS_ANCHOR,
        "system",
        "user",
        {"event": DELETE_EVENT, "preset_id": preset_id},
    )


def seed_from_default(log: "EventLog", name: str) -> str:
    """Create a new preset seeded from today's pipeline; return its id.

    Every interaction flow is seeded from
    :data:`~fabulist.prompts.modules.DEFAULT_FLOWS` via
    :func:`~fabulist.prompts.modules.from_default_flow` (fresh copies with
    unique row ids, so the new preset is immediately editable without aliasing
    the defaults). ``modules`` is the ``narrate`` flow (``from_default``) and
    ``agent_bindings`` starts empty (the engine falls back to today's wiring,
    D19).
    """
    flows = {
        interaction: [
            _module_to_dict(module) for module in from_default_flow(interaction)
        ]
        for interaction in INTERACTIONS
    }
    modules = flows["narrate"]
    return append_preset(
        log,
        {
            "name": name,
            "modules": [dict(module) for module in modules],
            "flows": {
                interaction: [dict(module) for module in rows]
                for interaction, rows in flows.items()
            },
            "agent_bindings": {},
            "version": 1,
            "surfaces": factory_surfaces(),
        },
    )


# -- persisted default (D26) -------------------------------------------------


@dataclass(frozen=True)
class DefaultSeedResult:
    """Outcome of :func:`ensure_default_preset` (boot auto-seed).

    ``created`` is True only on the first seed (row was missing).
    ``factory_changed`` is True when the persisted default was seeded from an
    OLDER factory version than the running code — the deliberate-adoption
    signal, never a silent overwrite.
    """

    preset_id: str
    created: bool
    factory_changed: bool
    seeded_version: int
    factory_version: int
    source: str


@dataclass(frozen=True)
class CommitResult:
    """Outcome of :func:`commit_default_to_factory` (artifact write)."""

    artifact_path: str
    factory_version: int
    preset: Preset


def _default_seed_result(
    log: "EventLog",
    *,
    created: bool,
    image: "factory.FactoryImage",
) -> "DefaultSeedResult":
    """Build the boot-seed result from the current default row + factory image."""
    row = get_preset(log, DEFAULT_PRESET_ID)
    seeded_version = row.factory_version if row is not None else image.factory_version
    current = factory.current_factory_version()
    return DefaultSeedResult(
        preset_id=DEFAULT_PRESET_ID,
        created=created,
        factory_changed=seeded_version < current,
        seeded_version=seeded_version,
        factory_version=current,
        source=image.source,
    )


def ensure_default_preset(
    log: "EventLog", *, image: "factory.FactoryImage | None" = None
) -> "DefaultSeedResult":
    """Boot hook: auto-seed the persisted default ONLY when it is missing (D26).

    If the default row exists it is **never** overwritten — an engine upgrade
    that advances :data:`factory.FACTORY_VERSION` only surfaces
    ``factory_changed=True`` for the drawer to offer review/reset. ``image`` is
    injectable for tests; the default resolves :func:`factory.factory_image`
    (committed artifact when present, else the code constants).
    """
    resolved = image or factory.factory_image()
    existing = get_preset(log, DEFAULT_PRESET_ID)
    if existing is not None:
        return _default_seed_result(log, created=False, image=resolved)
    _write_default_from_image(log, resolved)
    return _default_seed_result(log, created=True, image=resolved)


def _write_default_from_image(
    log: "EventLog", image: "factory.FactoryImage"
) -> None:
    """Append a ``preset`` event for the default row from a factory image.

    The canonical turn topology (W9.6) is seeded here — SEED-ONLY-IF-MISSING:
    an image that already carries a topology (a committed artifact, or a row
    written by a later preset wave) wins and is never overwritten.
    """
    payload = dict(image.payload)
    payload["preset_id"] = DEFAULT_PRESET_ID
    payload["is_default"] = True
    payload["factory_version"] = image.factory_version
    payload.setdefault("topology", canonical_topology())
    append_preset(log, payload)


def default_preset(log: "EventLog") -> Preset | None:
    """The persisted default preset row, or ``None`` when never seeded."""
    return get_preset(log, DEFAULT_PRESET_ID)


def default_factory_status(log: "EventLog") -> dict[str, Any]:
    """Drawer-facing factory status for the persisted default (D26).

    ``present`` False means the row has not been seeded yet (pre-boot);
    ``factory_changed`` True means the row was seeded by an older factory
    version than the running code and the owner should review/reset.
    """
    row = default_preset(log)
    current = factory.current_factory_version()
    if row is None:
        return {
            "present": False,
            "factory_changed": False,
            "seeded_version": None,
            "factory_version": current,
        }
    return {
        "present": True,
        "factory_changed": row.factory_version < current,
        "seeded_version": row.factory_version,
        "factory_version": current,
    }


def reset_default_preset(
    log: "EventLog", *, image: "factory.FactoryImage | None" = None
) -> Preset:
    """Re-seed the default row from the factory image (user-invoked only, D26).

    Overwrites the owner's edits by appending a superseding ``preset`` event
    (D9/D10). The credited factory version is the image's, so a reset onto an
    artifact that lags the code constant keeps the ``factory_changed`` notice.
    """
    resolved = image or factory.factory_image()
    _write_default_from_image(log, resolved)
    row = get_preset(log, DEFAULT_PRESET_ID)
    assert row is not None  # just wrote it
    return row


def commit_default_to_factory(
    log: "EventLog", *, path: "str | None" = None
) -> "CommitResult":
    """Export the CURRENT persisted default to the repo factory artifact (D26).

    Writes ``prompts/factory-defaults.json`` atomically (schema = the default
    preset export shape + ``factory_version`` + header comment), then re-stamps
    the persisted row to the current code factory version so the
    ``factory_changed`` notice clears (the committed image now IS this row).
    Raises :class:`LookupError` when there is no persisted default to commit.
    """
    row = default_preset(log)
    if row is None:
        raise LookupError("no persisted default preset to commit")
    current = factory.current_factory_version()
    written = factory.save_artifact(
        preset_to_json(row), factory_version=current, path=path
    )
    payload = preset_to_json(row)
    payload["factory_version"] = current
    append_preset(log, payload)
    restamped = get_preset(log, DEFAULT_PRESET_ID)
    assert restamped is not None  # just appended
    return CommitResult(
        artifact_path=str(written), factory_version=current, preset=restamped
    )


def compile_preset_flow(
    preset: Preset, interaction: str, parts: dict
) -> list[dict]:
    """Compile one interaction flow from a preset.

    The preset's ``flows[interaction]`` rows win when the flow has been
    customized; otherwise the default flow
    (:data:`~fabulist.prompts.modules.DEFAULT_FLOWS`) renders, with
    ``parts["system"]`` supplying the interaction's compiled default prompt. A
    flow row of type ``system`` whose stored ``content`` is non-empty is the
    one authoring seam for that interaction's system prompt
    (:func:`~fabulist.prompts.modules.compile_flow` renders that body). The
    retired ``prompt_overrides`` shim is no longer consulted (W6.3).

    ``parts`` carries the engine-resolved structural content for that
    interaction (``system`` / ``history`` / ``facts`` / ``user_content`` /
    ``turn_payload`` / ``payload`` / ``variables`` as applicable).
    """
    # No active preset (None) is the NORMAL daily-driver state — the default
    # flow renders. Found live: a 500 on photo caption upload with no preset
    # active; preset=None reached the .flows attribute access and crashed.
    if preset is None:
        return compile_flow(interaction, DEFAULT_FLOWS[interaction], dict(parts))
    modules = preset.flows.get(interaction) or DEFAULT_FLOWS[interaction]
    return compile_flow(interaction, modules, dict(parts))
