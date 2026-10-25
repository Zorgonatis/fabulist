"""Spec-driven component table for the narrator openui DSL (W2.4; defect fix).

The one authoritative source for "what components exist and in what positional
order" is the generated ``prompts/generated/library.spec.json`` (the
``LibrarySpec`` that the frontend registry mirror and the narrator library doc
are generated from). Flavor entries additionally carry a self-contained JSON
Schema (``jsonSchema``) — the SAME zod source the frontend parser derives from
— so :class:`ComponentSchema.json_schema` lets the validation gate check a
literal argument deeply instead of only its top-level shape. Every engine
consumer — the statement recognizer, the
validation gate, the DSL-strip regex, and (after the orchestrator's swap) the
history projection — reads this table, so the three segmenters/validators can
never drift by hand-synced component lists again.

ONE vocabulary (W9.27): the active component set's ``library.spec.json`` is the
PRIMARY source, read through :class:`fabulist.dsl.store.ComponentLibraryStore`,
which hot-reloads the generated artifacts — a regenerated or swapped set takes
effect on the next turn with no Python edit and no restart. ``flavors.json``
(the schema mirror) and the four infrastructure components are FALLBACKS only:
they are used when the set carries no readable spec (fresh checkout / hermetic
tests), never merged over a real spec.

The ACTIVE set, not the union (issue #15): this table is what the MODEL and the
engine's gate/strip/segmenter know, so it holds exactly the active set's
components (plus the always-merged infrastructure four). The renderer's wider
vocabulary — every component of every shipped set, which is what keeps an old
transcript renderable — is :mod:`fabulist.dsl.sets`' union, and deliberately NOT
merged in here: a component of an inactive set is unknown to the taught
vocabulary (it degrades visibly: prose, raw strip, ``unknown_component``) while
still rendering client-side. Infrastructure is core by definition and is never
removable (:data:`CORE_COMPONENT_NAMES`).

Overrides (tests / deployments) — all re-resolved by the store on every reload:

* ``FABULIST_DSL_SPEC`` — path to a ``library.spec.json``-shaped file.
* ``FABULIST_LIBRARY_ARTIFACT_DIR`` — the active component-set directory.
* ``FABULIST_FLAVORS_JSON`` — path to a ``flavors.json``-shaped mirror, used
  ONLY on the no-spec fallback path (kept for the validation gate's hermetic
  tests; production never needs it because the spec already carries all 20).

The module imports nothing from ``fabulist.graph`` or ``fabulist.prompts`` so
it stays importable from any layer (including the prompt lane's strip swap).

``component_signatures()`` renders that same JSON Schema for the narrator's
last-resort repair prompt, in the library doc's own notation
(``messages: {from: string, text: string}[], options?: {glyph?: string}``)
rather than bare prop names plus ``[]``/``?``. It closes a live defect (session
``38602b82``): the narrator passed ``["u wake up yet?", "now"]`` where
``object[]`` was required. The deterministic gate cannot repair a wrong nested
shape, so the model repair pass runs — and it saw only the validator's error
text, so it returned the same invalid statement, because nothing in its prompt
said what a ``messages`` entry looks like. A signature line that carries the
nested shape is exactly what that pass was missing.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import store
from .store import (
    ARTIFACT_DIR_ENV_VAR,
    FLAVORS_ENV_VAR,
    SPEC_ENV_VAR,
    ComponentSetSnapshot,
    iter_component_entries,
    library_store,
)

__all__ = [
    "ALIASES",
    "ARTIFACT_DIR_ENV_VAR",
    "ComponentSchema",
    "CORE_COMPONENT_NAMES",
    "FLAVORS_ENV_VAR",
    "SPEC_ENV_VAR",
    "component_names",
    "component_schema",
    "component_signatures",
    "flavors_path",
    "reset_spec_cache",
    "spec_path",
    "spec_table",
]

SPEC_FILENAME = store.SPEC_FILENAME
FLAVORS_FILENAME = "flavors.json"

#: The four infrastructure components (``library.ts``). Always known: they fill
#: in when the active spec omits them, and they are the whole table when no spec
#: and no ``flavors.json`` exist (the fallback path — they are never merged OVER
#: a real spec). A prop is ``name:type`` (type defaults to ``string``); a
#: trailing ``?`` on the type marks an optional positional prop (Wave 2b
#: expressive ``options`` object), widening max arity without changing min arity.
_CORE_PROPS: dict[str, tuple[str, ...]] = {
    "ScenePane": ("title:string", "children:component[]", "options:object?"),
    "MeterBar": ("label:string", "value:number", "max:number", "options:object?"),
    "ObjectCard": ("name:string", "kind:string", "description:string", "options:object?"),
    "BeatCard": ("text:string", "options:object?"),
}

#: The never-removable infrastructure components, in ``library.ts`` order. Core
#: by definition (owner ruling, issue #15): every component set carries them, the
#: store may never drop them, and :mod:`fabulist.dsl.sets` checks every declared
#: set for their presence. The engine also force-merges them into the table
#: (:func:`_build_table`) so a set's spec cannot remove one by omission.
CORE_COMPONENT_NAMES: tuple[str, ...] = tuple(_CORE_PROPS)

#: Nested shape expansion budget for :func:`component_signatures`, counted in
#: object levels below a component's own props: level 1 expands a prop object
#: (``options``) or an array's item object (``messages[]``), level 2 expands
#: objects inside those (``messages[].reactions[]``). Deeper than that a shape
#: renders as ``object``/``object[]`` — the repair prompt needs the shapes the
#: gate can reject, not a transcript of the whole schema.
_SIGNATURE_DEPTH = 2
#: Legacy component names still accepted. ``ArtifactCard`` was renamed to
#: ``ObjectCard`` (Wave 3A); old sessions and history carry ``ArtifactCard(...)``
#: statements that must keep validating and rendering as an ``ObjectCard``. Each
#: alias resolves to its canonical schema in :func:`_build_table`, so the
#: recognizer, gate and strip helper all see the legacy name too — but the alias
#: is never enumerated by ``component_signatures`` (it must not be re-taught).
ALIASES: dict[str, str] = {"ArtifactCard": "ObjectCard"}


@dataclass(frozen=True)
class PropInfo:
    """One positional constructor argument of a component."""

    name: str
    type: str = "string"
    required: bool = True


@dataclass(frozen=True)
class ComponentSchema:
    """One known component: ordered props + its accepted positional arity.

    ``bounded`` is ``False`` for entries that only name a component (no prop
    metadata): the name is known but arity is not checked. ``min_props``/
    ``max_props`` (``None`` = unbounded above) are the accepted range; when
    prop metadata exists they default to ``[required, total]``.

    ``json_schema`` is the self-contained JSON Schema the flavor components
    carry (``None`` for the four infrastructure components, whose zod schemas
    live inline in ``library.ts`` and cannot be imported headlessly). The gate
    uses it for its deep per-argument check; it is excluded from equality/hash
    so component schemas still compare by name/props/arity.
    """

    name: str
    props: tuple[PropInfo, ...] = ()
    min_props: int = 0
    max_props: int | None = None
    bounded: bool = True
    json_schema: dict[str, Any] | None = field(default=None, compare=False, hash=False)

    @property
    def prop_names(self) -> tuple[str, ...]:
        return tuple(prop.name for prop in self.props)

    def accepts(self, count: int) -> bool:
        if count < self.min_props:
            return False
        if self.max_props is not None and count > self.max_props:
            return False
        return True

    def describe_arity(self) -> str:
        if self.max_props is None:
            return f"at least {self.min_props}"
        if self.min_props == self.max_props:
            return str(self.min_props)
        return f"{self.min_props}..{self.max_props}"


#: Process-wide cache of the merged table + the store version it was built
#: from. A regenerated or swapped set bumps the version, which is what
#: invalidates this cache without a restart.
_CACHE: dict[str, ComponentSchema] | None = None
_CACHE_VERSION: int | None = None


def spec_path() -> Path:
    """Path to the authoritative ``library.spec.json`` (env override wins)."""
    return store.resolve_spec_path()


def flavors_path() -> Path:
    """Path to the optional ``flavors.json`` mirror (env override wins)."""
    return store.resolve_flavors_path()


def _read_json(path: Path) -> Any:
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _props_from_entry(entry: Any) -> tuple[PropInfo, ...] | None:
    """Ordered props from a spec/flavors entry, or ``None`` when undeclared.

    Accepts a list of prop mappings (``{"name", "type", "required"}``) or bare
    strings. An empty/absent list means "component named, arity unbounded".
    """
    if not isinstance(entry, Mapping):
        return None
    props = entry.get("props")
    if not isinstance(props, (list, tuple)) or not props:
        return None
    out: list[PropInfo] = []
    for prop in props:
        if isinstance(prop, str):
            out.append(PropInfo(prop))
        elif isinstance(prop, Mapping) and isinstance(prop.get("name"), str):
            out.append(
                PropInfo(
                    name=prop["name"],
                    type=str(prop.get("type") or "string"),
                    required=bool(prop.get("required", True)),
                )
            )
    return tuple(out) or None


def _schema_from_entry(name: str, entry: Any) -> ComponentSchema:
    props = _props_from_entry(entry)
    min_props = sum(1 for prop in props if prop.required) if props else 0
    max_props: int | None = len(props) if props else None

    if isinstance(entry, Mapping):
        arity = entry.get("arity")
        if isinstance(arity, int) and not isinstance(arity, bool):
            min_props = max_props = int(arity)
        elif isinstance(arity, (list, tuple)) and len(arity) == 2:
            min_props, max_props = int(arity[0]), int(arity[1])
        else:
            low = entry.get("min_props")
            high = entry.get("max_props")
            if isinstance(low, int) and not isinstance(low, bool):
                min_props = low
            if isinstance(high, int) and not isinstance(high, bool):
                max_props = high

    json_schema = entry.get("jsonSchema") if isinstance(entry, Mapping) else None
    if not isinstance(json_schema, dict):
        json_schema = None

    bounded = props is not None or min_props or max_props is not None
    return ComponentSchema(
        name,
        props or (),
        min_props=min_props,
        max_props=max_props,
        bounded=bounded,
        json_schema=json_schema,
    )


def _load_flavor_entries() -> Iterator[tuple[str, Any]]:
    raw = _read_json(flavors_path())
    if raw is None:
        return
    yield from iter_component_entries(raw, ("flavors", "components"))


def _core_prop_info(spec: str) -> PropInfo:
    """Parse one ``_CORE_PROPS`` entry (``name[:type][?]``) into a :class:`PropInfo`.

    The type defaults to ``string``; a trailing ``?`` on the type token marks the
    prop optional (``value:number``, ``options:object?``).
    """
    name, _, type_token = spec.partition(":")
    optional = type_token.endswith("?")
    prop_type = type_token[:-1] if optional else type_token
    return PropInfo(name, prop_type or "string", required=not optional)


def _core_schema(name: str, props: tuple[str, ...]) -> ComponentSchema:
    prop_infos = tuple(_core_prop_info(prop) for prop in props)
    required_count = sum(1 for info in prop_infos if info.required)
    return ComponentSchema(
        name,
        prop_infos,
        min_props=required_count,
        max_props=len(prop_infos),
        bounded=True,
    )


def _build_table(snapshot: ComponentSetSnapshot) -> dict[str, ComponentSchema]:
    table: dict[str, ComponentSchema] = {}
    if snapshot.spec is not None:
        # ONE vocabulary: the active set's spec is the authority (all 20
        # components, incl. the four infra with coarse props).
        for name, entry in iter_component_entries(snapshot.spec, ("components",)):
            table[name] = _schema_from_entry(name, entry)
        # The infrastructure four stay always-known, but only fill gaps — a spec
        # entry is never clobbered (the pre-W9.27 merge-over-spec is gone).
        for name, props in _CORE_PROPS.items():
            table.setdefault(name, _core_schema(name, props))
    else:
        # Fallback (no readable spec: fresh checkout / hermetic gate tests): the
        # flavors mirror, then the infrastructure four (which win collisions).
        for name, entry in _load_flavor_entries():
            table.setdefault(name, _schema_from_entry(name, entry))
        for name, props in _CORE_PROPS.items():
            table[name] = _core_schema(name, props)
    # Legacy aliases resolve to the canonical schema, keeping the alias's own
    # name so error messages name the component the model actually wrote.
    for alias, canonical in ALIASES.items():
        target = table.get(canonical)
        if target is not None and alias not in table:
            table[alias] = ComponentSchema(
                alias,
                target.props,
                min_props=target.min_props,
                max_props=target.max_props,
                bounded=target.bounded,
            )
    return table


def spec_table() -> Mapping[str, ComponentSchema]:
    """The active component table, cached by store version.

    The store stats the artifacts through :meth:`snapshot`, so a regenerated or
    swapped set rebuilds this table on the next read — no restart, no consumer
    edit.
    """
    global _CACHE, _CACHE_VERSION
    snapshot = library_store().snapshot()
    if _CACHE is None or _CACHE_VERSION != snapshot.version:
        _CACHE = _build_table(snapshot)
        _CACHE_VERSION = snapshot.version
    return _CACHE


def component_schema(name: str) -> ComponentSchema | None:
    """Schema for one component, or ``None`` when unknown."""
    return spec_table().get(name)


def component_names() -> tuple[str, ...]:
    """Every known component name (sorted)."""
    return tuple(sorted(spec_table()))


def component_names_longest_first() -> tuple[str, ...]:
    """Component names sorted longest-first (safe regex alternation order)."""
    return tuple(sorted(spec_table(), key=lambda name: (-len(name), name)))


def _signature_enum(values: Any) -> str:
    """A JSON Schema ``enum`` as the doc's ``"a"|"b"|"c"`` alternation."""
    return "|".join(f'"{value}"' if isinstance(value, str) else str(value) for value in values)


def _signature_type(schema: Any, depth: int) -> str:
    """One prop's type in the library doc's notation, nested shapes expanded.

    ``depth`` is the remaining object-expansion budget (see
    :data:`_SIGNATURE_DEPTH`); arrays are transparent, so ``object[]`` keeps the
    ``[]`` marker with its item shape in front. A shape that is not a resolvable
    JSON Schema (or has run out of depth) degrades to ``object``/``value``
    instead of guessing.
    """
    if not isinstance(schema, Mapping):
        return "value"
    enum = schema.get("enum")
    if isinstance(enum, (list, tuple)) and enum:
        return _signature_enum(enum)
    schema_type = schema.get("type")
    if schema_type == "array":
        items = schema.get("items")
        if not isinstance(items, Mapping):
            return "object[]"
        return f"{_signature_type(items, depth)}[]"
    if schema_type == "object":
        properties = schema.get("properties")
        if isinstance(properties, Mapping) and properties and depth > 0:
            required = schema.get("required")
            names = (
                {name for name in required if isinstance(name, str)}
                if isinstance(required, (list, tuple))
                else set()
            )
            fields = [
                f"{key}{'' if key in names else '?'}: {_signature_type(sub, depth - 1)}"
                for key, sub in properties.items()
            ]
            return "{" + ", ".join(fields) + "}"
        return "object"
    if schema_type == "integer":
        return "number"
    if isinstance(schema_type, str):
        return schema_type
    return "value"


def _legacy_signature(name: str, schema: ComponentSchema) -> str:
    """``Name(prop, prop[], prop?)`` — the type-free line for schema-less entries.

    The four infrastructure components carry no emitted ``jsonSchema``, so their
    line stays exactly as it has always been: prop names, ``[]`` on an array, a
    ``?`` on an optional prop.
    """
    parts: list[str] = []
    for prop in schema.props:
        label = prop.name
        if prop.type.endswith("[]"):
            label += "[]"
        elif not prop.required:
            label += "?"
        parts.append(label)
    return f"{name}({', '.join(parts)})"


def _typed_signature(name: str, schema: ComponentSchema) -> str:
    """``Name(prop: type, opt?: {k: T, k2?: T}[], ...)`` from the JSON Schema.

    Positional order and the optional mark come from the spec's own props; the
    types (and every nested required/optional mark below them) come from
    ``json_schema``. A prop the schema does not describe falls back to its
    coarse spec type.
    """
    properties = (schema.json_schema or {}).get("properties")
    subschemas = properties if isinstance(properties, Mapping) else {}
    parts: list[str] = []
    for prop in schema.props:
        subschema = subschemas.get(prop.name)
        prop_type = (
            _signature_type(subschema, _SIGNATURE_DEPTH)
            if isinstance(subschema, Mapping)
            else prop.type
        )
        label = prop.name if prop.required else f"{prop.name}?"
        parts.append(f"{label}: {prop_type}")
    return f"{name}({', '.join(parts)})"


def component_signatures() -> tuple[str, ...]:
    """Compact call signatures for every known component, in name order.

    Array props carry a ``[]`` marker and optional props a ``?``; entries with a
    JSON Schema additionally carry their types and two levels of nested shape
    (``messages: {from: string, text: string, reactions?: {emoji: string}[]}[]``)
    so the lines double as call templates — the shape the model emits. The four
    schema-less infrastructure components keep their type-free
    ``Name(prop, prop[], prop?)`` line."""
    lines: list[str] = []
    for name in component_names():
        if name in ALIASES:
            # Legacy names are accepted but never re-taught in the repair prompt.
            continue
        schema = spec_table()[name]
        if not schema.bounded:
            lines.append(f"{name}(...)")
        elif schema.json_schema is None:
            lines.append(_legacy_signature(name, schema))
        else:
            lines.append(_typed_signature(name, schema))
    return tuple(lines)


def reset_spec_cache() -> None:
    """Drop the cached table and force the store to re-read (tests swap env)."""
    global _CACHE, _CACHE_VERSION
    _CACHE = None
    _CACHE_VERSION = None
    library_store().reload()
