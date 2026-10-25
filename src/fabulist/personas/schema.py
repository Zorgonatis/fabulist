"""Structured persona canon (PLAN.md **D36**).

An identity-block persona schema — an identity block plus a recursive body tree —
is adapted for Fabulist actors and hosted inside the ST v2 card envelope at
``data.extensions.fabulist.persona``. Full-fidelity Layer 0 preserves
``extensions`` verbatim (D28), and the D16/D31 persona event carries the same
``extensions`` into the log, so the schema stays **structured** in storage while
its compact text rendering joins the character description block at prompt
assembly (see :mod:`fabulist.context.facts` / :mod:`fabulist.context.roster`).

Design notes (boring on purpose):

* **Lenient** — every field is optional; a missing or oddly-typed value never
  raises. Unknown *top-level* keys are preserved verbatim in
  :attr:`PersonaSchema.extra` (D28 spirit) so a hand-authored schema round-trips.
* **Recursive free-form body** — :class:`BodyNode` holds ordered entries where a
  value is either a string leaf or a nested :class:`BodyNode`. A leaf with no
  value (``uncalloused``) renders as a bare descriptor.
* **Pure** — parsing and rendering read no storage; the raw dict is never
  rewritten. The prompt render is a deterministic function of the schema.

Public API
----------
:data:`PERSONA_EXTENSION_NAMESPACE`, :data:`PERSONA_EXTENSION_KEY`,
:data:`PERSONA_HEADER`, :class:`BodyNode`,
:class:`PersonaSchema`, :func:`schema_from_extensions`, :func:`render`,
:func:`render_identity_core`, :func:`persona_lines_from_card`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "PERSONA_EXTENSION_KEY",
    "PERSONA_EXTENSION_NAMESPACE",
    "PERSONA_HEADER",
    "BodyNode",
    "PersonaSchema",
    "persona_lines_from_card",
    "render",
    "render_identity_core",
    "schema_from_extensions",
]

#: ``extensions`` namespace owned by Fabulist (ST reserves ``extensions`` for
#: consumer data; see :data:`fabulist.cards.v2.CardV2.extensions`).
PERSONA_EXTENSION_NAMESPACE = "fabulist"

#: Key under the namespace holding the persona schema object.
PERSONA_EXTENSION_KEY = "persona"

#: First line of every rendered persona block; marks it as one canonical block
#: inside the character description context.
PERSONA_HEADER = "Persona canon:"

#: Known top-level schema keys; everything else is preserved in ``extra``.
_KNOWN_KEYS = frozenset(
    {
        "name",
        "age_band",
        "archetype",
        "gender",
        "ethnicity",
        "description",
        "personality",
        "likes",
        "dislikes",
        "body",
    }
)

#: Separator for list-valued fields (``; `` avoids ambiguity with in-value
#: commas, e.g. ethnicity notes).
_JOINER = "; "


def _text(value: Any) -> str:
    """Coerce an optional scalar to a plain string (``None`` -> ``""``).

    A list/tuple joins with ``; `` so a field authored as a list still renders
    (the schema is untrusted free-form input).
    """
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return _JOINER.join(_text(item) for item in value)
    return value if isinstance(value, str) else str(value)


def _str_list(value: Any) -> list[str]:
    """Coerce an optional field to ``list[str]`` (missing -> ``[]``).

    A bare scalar becomes a one-element list, so ``archetype: mage`` and
    ``archetype: [mage]`` render identically.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [_text(item) for item in value]
    return [_text(value)]


def _leaf(value: Any) -> str:
    """Render one body leaf value as text (``None`` -> ``""`` = bare label)."""
    if value is None or isinstance(value, str):
        return value or ""
    if isinstance(value, (list, tuple)):
        return _JOINER.join(_leaf(item) for item in value)
    return str(value)


def _label(key: str) -> str:
    """Humanize a snake_case schema key for the canonical render."""
    return key.replace("_", " ").strip().capitalize()


@dataclass(frozen=True)
class BodyNode:
    """One node in the recursive, free-form body tree.

    ``fields`` preserves author order; each value is either a string leaf or a
    nested :class:`BodyNode`. A ``str`` leaf with an empty value renders as a
    bare descriptor (``Uncalloused``).
    """

    fields: dict[str, "str | BodyNode"] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Any) -> "BodyNode":
        """Parse a mapping into a node; non-mappings yield an empty node."""
        fields: dict[str, str | BodyNode] = {}
        if isinstance(raw, Mapping):
            for key, value in raw.items():
                name = str(key)
                if isinstance(value, Mapping):
                    fields[name] = cls.from_mapping(value)
                else:
                    fields[name] = _leaf(value)
        return cls(fields)

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict view (nested nodes recursed), preserving author order."""
        return {
            key: value.to_dict() if isinstance(value, BodyNode) else value
            for key, value in self.fields.items()
        }


@dataclass(frozen=True)
class PersonaSchema:
    """Structured actor canon folded out of ``extensions.fabulist.persona``.

    Every field is optional. Unknown top-level keys survive in
    :attr:`extra` (D28 spirit), so :meth:`to_dict` round-trips a hand-authored
    schema (minus empty defaulted fields).
    """

    name: str = ""
    age_band: str = ""
    archetype: list[str] = field(default_factory=list)
    gender: str = ""
    ethnicity: str = ""
    description: str = ""
    personality: list[str] = field(default_factory=list)
    likes: list[str] = field(default_factory=list)
    dislikes: list[str] = field(default_factory=list)
    body: BodyNode = field(default_factory=BodyNode)
    extra: dict = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Any) -> "PersonaSchema":
        """Fold a raw schema mapping into a :class:`PersonaSchema`.

        Non-object input yields an empty schema. Unknown keys are preserved in
        :attr:`extra`; malformed known values degrade rather than raise.
        """
        data = dict(raw) if isinstance(raw, Mapping) else {}
        return cls(
            name=_text(data.get("name")),
            age_band=_text(data.get("age_band")),
            archetype=_str_list(data.get("archetype")),
            gender=_text(data.get("gender")),
            ethnicity=_text(data.get("ethnicity")),
            description=_text(data.get("description")),
            personality=_str_list(data.get("personality")),
            likes=_str_list(data.get("likes")),
            dislikes=_str_list(data.get("dislikes")),
            body=BodyNode.from_mapping(data.get("body")),
            extra={key: value for key, value in data.items() if key not in _KNOWN_KEYS},
        )

    def is_empty(self) -> bool:
        """True when the schema carries no canon at all (nothing to render)."""
        return not (
            self.name
            or self.age_band
            or self.archetype
            or self.gender
            or self.ethnicity
            or self.description
            or self.personality
            or self.likes
            or self.dislikes
            or self.body.fields
            or self.extra
        )

    def to_dict(self) -> dict[str, Any]:
        """Plain-dict view: non-empty known fields plus preserved unknown keys."""
        out: dict[str, Any] = {}
        if self.name:
            out["name"] = self.name
        if self.age_band:
            out["age_band"] = self.age_band
        if self.archetype:
            out["archetype"] = list(self.archetype)
        if self.gender:
            out["gender"] = self.gender
        if self.ethnicity:
            out["ethnicity"] = self.ethnicity
        if self.description:
            out["description"] = self.description
        if self.personality:
            out["personality"] = list(self.personality)
        if self.likes:
            out["likes"] = list(self.likes)
        if self.dislikes:
            out["dislikes"] = list(self.dislikes)
        if self.body.fields:
            out["body"] = self.body.to_dict()
        out.update(self.extra)
        return out


def schema_from_extensions(extensions: Any) -> PersonaSchema | None:
    """Extract the persona schema from a card/persona ``extensions`` object.

    Reads ``extensions.fabulist.persona``. A missing namespace/key, a non-object
    value, or an empty object means "no persona schema" and returns ``None`` —
    so an old log/card without the extension folds and renders exactly as before.
    """
    if not isinstance(extensions, Mapping):
        return None
    namespace = extensions.get(PERSONA_EXTENSION_NAMESPACE)
    if not isinstance(namespace, Mapping):
        return None
    raw = namespace.get(PERSONA_EXTENSION_KEY)
    if not isinstance(raw, Mapping) or not raw:
        return None
    return PersonaSchema.from_mapping(raw)


# --- canonical rendering ----------------------------------------------------


def _identity_lines(schema: PersonaSchema, *, include_preferences: bool) -> list[str]:
    """Shared ``Label: value`` identity lines (order-stable)."""
    lines: list[str] = []
    if schema.name:
        lines.append(f"Name: {schema.name}")
    if schema.age_band:
        lines.append(f"Age band: {schema.age_band}")
    if schema.archetype:
        lines.append(f"Archetype: {_JOINER.join(schema.archetype)}")
    if schema.gender:
        lines.append(f"Gender: {schema.gender}")
    if schema.ethnicity:
        lines.append(f"Ethnicity: {schema.ethnicity}")
    if schema.description:
        lines.append(f"Description: {schema.description}")
    if schema.personality:
        lines.append(f"Personality: {_JOINER.join(schema.personality)}")
    if include_preferences:
        if schema.likes:
            lines.append(f"Likes: {_JOINER.join(schema.likes)}")
        if schema.dislikes:
            lines.append(f"Dislikes: {_JOINER.join(schema.dislikes)}")
    return lines


def _node_lines(node: BodyNode, depth: int) -> list[str]:
    """Render one body node at ``depth`` (2-space indent per level)."""
    pad = "  " * depth
    lines: list[str] = []
    for key, value in node.fields.items():
        if isinstance(value, BodyNode):
            lines.append(f"{pad}{_label(key)}:")
            lines.extend(_node_lines(value, depth + 1))
        elif value:
            lines.append(f"{pad}{_label(key)}: {value}")
        else:
            lines.append(f"{pad}{_label(key)}")
    return lines


def _body_summary(node: BodyNode) -> str:
    """One-line summary: top-level body leaves only, nested groups elided."""
    parts: list[str] = []
    for key, value in node.fields.items():
        if isinstance(value, BodyNode):
            continue
        label = _label(key)
        parts.append(f"{label}: {value}" if value else label)
    return _JOINER.join(parts)


def render(schema: PersonaSchema) -> str:
    """Full-tree canonical rendering of ``schema`` (``""`` when empty).

    Identity lines first, then the complete recursive body tree, then any
    preserved unknown keys. This is the default form: complete authored canon
    every turn; which details matter is the narrator's call at render time.
    """
    if schema.is_empty():
        return ""
    lines = [PERSONA_HEADER]
    lines.extend(_identity_lines(schema, include_preferences=True))
    if schema.body.fields:
        lines.append("Body:")
        lines.extend(_node_lines(schema.body, 1))
    if schema.extra:
        lines.extend(_node_lines(BodyNode.from_mapping(schema.extra), 0))
    return "\n".join(lines)


def render_identity_core(schema: PersonaSchema) -> str:
    """Identity-only rendering with a one-line body summary (``""`` when empty).

    Name/age/archetype/gender/ethnicity/description/personality and the
    top-level body leaves on one line — the lower-fidelity option for the
    owner when the full tree costs too many tokens.
    """
    if schema.is_empty():
        return ""
    lines = [PERSONA_HEADER]
    lines.extend(_identity_lines(schema, include_preferences=False))
    summary = _body_summary(schema.body)
    if summary:
        lines.append(f"Body: {summary}")
    return "\n".join(lines)


def persona_lines_from_card(card: Any) -> list[str]:
    """Rendered persona lines for a card dict, or ``[]`` when it carries none.

    The single seam the character-block renderers call: reads
    ``card["extensions"]`` via :func:`schema_from_extensions` and returns the
    full-tree render split into lines. A card without the extension returns
    ``[]``, leaving the block byte-identical.
    """
    if not isinstance(card, Mapping):
        return []
    schema = schema_from_extensions(card.get("extensions"))
    if schema is None:
        return []
    text = render(schema)
    return text.splitlines() if text else []
