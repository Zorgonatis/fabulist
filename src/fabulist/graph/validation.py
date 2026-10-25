"""W2.4b narrator-side DSL validation gate.

The narrator pass emits ordered ``segments`` (``[{"kind": "prose"|"dsl",
"text": ...}]``). Before a ``dsl`` segment is allowed to reach the renderer it
must pass a cheap, deterministic schema gate: the segment must be a
``identifier = Component(...)`` statement naming a *known* component, its
arguments must be positional in the component's declared order and match the
component's argument schemas, its delimiters/quotes must balance, and it must
not smuggle raw HTML/script through the openui channel.

The deep argument check reads the self-contained JSON Schema each flavor
component carries in the generated spec (``ComponentSchema.json_schema``) and
tests each positional **literal** argument against that prop's subschema. This
is the same zod source the frontend parser validates against, so the engine now
sees what the frontend sees: ``PostIt(["a string"], {glyph: "…"})`` — notes as
an array of strings where the schema wants ``{text}`` objects — is caught here
instead of sailing through to the renderer. The check is deliberately
conservative: an argument that is not a plain literal (a bare ref/expression)
is never flagged, a union/``$ref`` subschema is skipped rather than guessed,
and the four infrastructure components (no emitted schema) keep the coarse
prop-type check.

**Deterministic repair first, model re-synthesis last.** The gate repairs the
two failure classes the live RP runs actually produced, with *zero* model calls:

* **named-argument (kwargs) statements** (``DialogueSpotlight(speaker="…")``) — the
  shipped ``@openuidev/lang-core`` parser rejects these at render
  (``excess-args``/``null-required``) while the old comma-counting gate could
  not even see them. They are mechanically converted to positional order via the
  spec prop table (:func:`fabulist.dsl.positionalize`).
* **dangling child refs** (``root = ScenePane(t, [status, interface, …])``
  referencing identifiers no segment declares) — the phantom refs are dropped
  from the root's child list **with an explicit flag** (never a silent prune),
  and a ref to a statement that FAILED the gate is never pruned (the text
  really is there). Pruning that would empty the child list is **REFUSED**: a
  root whose children were ALL undeclared refs is a wrong composition, not a
  tidy-up, so the statement is kept as authored and the ``empty_after_prune``
  finding is a HARD failure that takes a repair round (issue #30 — as an
  ``info`` repair it committed a hollow ``ScenePane`` as a successful turn).
  When the repair budget is spent the finding stays on the record as a
  ``warn``-severity flag on that segment, so a hollow panel can never ship
  silently.
* **Custom statements** are exempt from the generic raw-HTML scan — their whole
  point *is* raw markup — and are checked by
  :mod:`fabulist.dsl.custom_policy` instead. A policy violation takes the
  normal failure flag (so :func:`revalidate_with_retry` still gets its one
  re-synthesis) and, still failing afterwards, is rewritten in place to a
  deterministic ``Document("scrap", …)`` paper-scrap: the statement stays
  renderable and the log stays truthful (never a silent drop).

**Keep and tag, never silently drop.** An unrecoverable statement is KEPT
VERBATIM in the committed payload as a ``dsl`` segment; nothing is removed and
the turn never blocks. The failure is reported as a validator flag anchored to
that segment's index, so the chat message carries a visible "this panel failed
validation" tag explaining the component, argument and expected-vs-received
shape. (:func:`revalidate_with_retry` still gets one constrained re-synthesis
pass; when that too fails, the keep-and-tag result stands.)

Two entry points:

* :func:`validate_dsl` — pure/sync, the gate. Returns a
  :class:`DslValidationReport`; ``ok`` is true when every DSL segment survived
  (possibly repaired in place). ``segments`` is always the authoritative
  post-repair payload.
* :func:`revalidate_with_retry` — the same gate plus one re-synthesis pass.

Known components come from the shared spec table
(:mod:`fabulist.dsl.spec`) — the generated ``library.spec.json`` plus the
``flavors.json`` mirror — so this gate, the segmenter and the strip helper can
never drift by hand-synced lists.

This module is deliberately independent of the graph/nodes modules so it can be
imported by the narrator pipeline without a cycle.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fabulist.dsl import custom_policy as _custom_policy
from fabulist.dsl import spec as _dsl_spec
from fabulist.dsl import (
    ComponentSchema,
    Statement,
    parse_statement,
    positionalize,
    prune_child_refs,
    split_args,
    split_statements,
)

__all__ = [
    "ComponentSpec",
    "DslError",
    "DslValidationReport",
    "component_specs",
    "dsl_error_flags",
    "flavors_path",
    "known_component_names",
    "reset_component_specs_cache",
    "revalidate_with_retry",
    "validate_dsl",
]

#: Env override for the flavor-schema path (intended for tests / deployments).
#: Kept as a module attribute so tests can ``monkeypatch.setenv(validation.FLAVORS_ENV_VAR, …)``.
FLAVORS_ENV_VAR = _dsl_spec.FLAVORS_ENV_VAR

#: Backwards-compatible alias: the spec table's schema is the gate's ComponentSpec.
ComponentSpec = ComponentSchema

#: Segment kinds the payload carries. Anything not ``dsl`` is treated as prose.
_DSL = "dsl"


# ---------------------------------------------------------------------------
# component schema (delegated to fabulist.dsl.spec)
#
# The gate holds NO cache of its own (W9.27 C3): the component table lives in
# ``fabulist.dsl.spec``, cached by the component store's version, so a
# regenerated or mid-session-swapped set reaches the gate with no restart and no
# reset call. A second cache here would shadow the store's reload — the exact
# defect that made Wave A's smoke test need ``reset_component_specs_cache()``.
# ---------------------------------------------------------------------------


def flavors_path():
    """Path to the W2.4 flavor schema JSON (env override wins)."""
    return _dsl_spec.flavors_path()


def component_specs() -> Mapping[str, ComponentSchema]:
    """The resolved component table — the store's live, version-keyed table."""
    return _dsl_spec.spec_table()


def known_component_names() -> set[str]:
    """Names the gate accepts (core four + spec + loaded flavors)."""
    return set(component_specs())


def flavor_names() -> tuple[str, ...]:
    """Every known component name (sorted, cached)."""
    try:
        return tuple(sorted(component_specs().keys()))
    except Exception:  # noqa: BLE001 - unreadable mirror -> no flavors
        return ()


def reset_component_specs_cache() -> None:
    """Re-read the active component set (tests/deployments swap the artifacts).

    The gate keeps no state to drop; this delegates to the store-backed spec
    cache so existing callers/tests keep one reset seam.
    """
    _dsl_spec.reset_spec_cache()


# ---------------------------------------------------------------------------
# report types
# ---------------------------------------------------------------------------


@dataclass
class DslError:
    """One gate finding on a DSL segment (an unrecoverable failure or a repair).

    ``segment_index`` indexes the input ``segments`` list; ``code`` is a stable
    machine key; ``message`` is human-facing; ``statement`` is the offending
    text (trimmed) when it was extractable. ``repaired`` is true for advisory
    findings where the gate itself fixed the composition (kwargs converted,
    dangling refs dropped) — those do NOT make :attr:`DslValidationReport.ok`
    false and never trigger a model re-synthesis. An unrepaired finding is a
    failure: its segment is kept verbatim (keep + tag) and surfaced as a flag.
    ``empty_after_prune`` is deliberately an UNREPAIRED finding: refusing to
    prune the last children leaves the composition wrong, so the turn is sent
    back for another repair round rather than committing a hollow pane.
    """

    segment_index: int
    code: str
    message: str
    statement: str | None = None
    repaired: bool = False


@dataclass
class DslValidationReport:
    """Outcome of one validation pass.

    ``segments`` is the authoritative, ordered post-repair payload: prose
    segments and valid DSL segments pass through unchanged (valid DSL may have
    been repaired in place), and each unrecoverable DSL segment is kept VERBATIM
    as a ``dsl`` segment (keep-and-tag — nothing is dropped). A ``Custom``
    statement whose raw-HTML policy is violated is rewritten in place to a
    ``Document("scrap", …)`` paper-scrap, and ``ok`` is still false so the
    re-synthesis pass still runs (the scrap is the deterministic floor, not a
    successful repair). ``ok`` is true when no segment failed; ``needs_retry``
    mirrors it for the last-resort model pass. ``retry_applied`` is only true on
    a report produced by :func:`revalidate_with_retry`; ``resynth_discarded``
    marks the case where that pass produced a replacement which was then thrown
    away (its DSL slots do not line up with the draft's, or it changed nothing),
    so the report describes the DRAFT.
    """

    ok: bool
    errors: list[DslError] = field(default_factory=list)
    segments: list[dict] = field(default_factory=list)
    retry_applied: bool = False
    resynth_discarded: bool = False

    @property
    def needs_retry(self) -> bool:
        """True when an unrecoverable segment remains (model retry warranted)."""
        return not self.ok

    @property
    def advisories(self) -> list[DslError]:
        """Repairs the deterministic pass made (kwargs / dangling)."""
        return [e for e in self.errors if e.repaired]


# ---------------------------------------------------------------------------
# lexical checks
# ---------------------------------------------------------------------------

#: Event-handler attributes, ``javascript:`` URIs, and HTML comments.
_HTML_INLINE_RE = re.compile(r"<!--|\bon[a-z]+\s*=|javascript\s*:", re.IGNORECASE)
#: A well-formed-ish tag: ``<div>``, ``</script>``, ``<img src=...>``. Requires
#: the tag name to start immediately after ``<`` (no whitespace), so prose/math
#: like ``a < b`` is not flagged.
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9-]*(?:\s[^>]*)?>", re.IGNORECASE)
#: Unclosed openers for the dangerous tags (``<script`` without ``>``).
_HTML_OPENER_RE = re.compile(
    r"<\s*/?\s*(?:script|style|iframe|svg|img|object|embed|link|meta)\b",
    re.IGNORECASE,
)

_PAIRS = {")": "(", "]": "[", "}": "{"}
_OPENERS = "([{"
_CLOSERS = ")]}"
_QUOTES = "\"'`"


#: A bare numeric literal (integer or decimal, optional sign).
_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?$")

#: JSON-Schema keys that make a subschema unresolvable for the deep literal
#: check (a union or conditional). The gate SKIPS these rather than guess.
_UNRESOLVABLE_SCHEMA_KEYS = ("anyOf", "oneOf", "allOf", "$ref", "not", "if", "then", "else")

#: A bare DSL object key (``{glyph: …}``); quoted keys are decoded first.
_BARE_KEY_RE = re.compile(r"^[A-Za-z_]\w*$")

#: Returned by :func:`_parse_literal` for an argument that is NOT a plain literal
#: (a bare identifier/ref/expression). Callers skip it — never flag it.
_NOT_LITERAL = object()

#: Prop type -> the raw argument shape it expects. Types outside the map
#: (``any``, ``color``, ``select`` …) are not policed — the gate only flags a
#: shape it is certain about, so it can never reject a valid call.
_EXPECTED_SHAPES: dict[str, str] = {
    "string": "string",
    "number": "number",
    "integer": "number",
    "int": "number",
    "float": "number",
    "boolean": "boolean",
    "bool": "boolean",
    "object": "object",
}


def _arg_shape(arg: str) -> str | None:
    """Classify one raw positional argument's literal shape, or ``None``.

    Only shapes the gate can *prove* are returned: a quoted string, an array,
    an object, a bare number or a bare boolean. A bare identifier (a child ref)
    or any other expression classifies as ``None`` and is never flagged.
    """
    text = arg.strip()
    if not text:
        return None
    head = text[0]
    if head in "\"'":
        return "string"
    if head == "[":
        return "array"
    if head == "{":
        return "object"
    if _NUMBER_RE.match(text):
        return "number"
    if text in ("true", "false"):
        return "boolean"
    return None


def _expected_shape(prop_type: str) -> str | None:
    """The raw shape a declared prop type expects, or ``None`` when unpoliced."""
    text = (prop_type or "").strip().lower()
    if not text:
        return None
    if text.endswith("[]"):
        return "array"
    return _EXPECTED_SHAPES.get(text)


def _split_key_value(entry: str) -> tuple[str, str] | None:
    """Split one object-literal entry at its top-level ``:`` (or ``None``).

    The DSL object grammar is JSON-like but with BARE keys (``{glyph: "📝"}``);
    the separator is the first ``:`` outside quotes and nesting.
    """
    depth = 0
    quote: str | None = None
    escape = False
    for position, ch in enumerate(entry):
        if escape:
            escape = False
            continue
        if quote is not None:
            if ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in _QUOTES:
            quote = ch
        elif ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            depth -= 1
        elif ch == ":" and depth == 0:
            return entry[:position], entry[position + 1 :]
    return None


def _parse_literal(text: str):  # noqa: ANN202 - Any | sentinel
    """Parse a DSL literal to a Python value, or :data:`_NOT_LITERAL`.

    Deliberately conservative: only the literal subset the DSL grammar emits is
    parsed — double/single-quoted strings, numbers, booleans, arrays and objects
    with BARE keys. A bare identifier/ref/expression (``root = ScenePane(t,
    [term, mind])``) is NOT a literal, so the caller skips it and the gate can
    never flag a resolved reference.
    """
    stripped = (text or "").strip()
    if not stripped:
        return _NOT_LITERAL
    head = stripped[0]
    if head in _QUOTES:
        if len(stripped) < 2 or stripped[-1] != head:
            return _NOT_LITERAL
        decoded = _literal_arg(stripped)
        return decoded if decoded is not None else _NOT_LITERAL
    if stripped in ("true", "false"):
        return stripped == "true"
    if _NUMBER_RE.match(stripped):
        return float(stripped) if "." in stripped else int(stripped)
    if head == "[" and stripped.endswith("]"):
        inner = stripped[1:-1].strip()
        if not inner:
            return []
        items: list = []
        for item in split_args(inner):
            value = _parse_literal(item)
            if value is _NOT_LITERAL:
                return _NOT_LITERAL
            items.append(value)
        return items
    if head == "{" and stripped.endswith("}"):
        inner = stripped[1:-1].strip()
        if not inner:
            return {}
        mapping: dict = {}
        for entry in split_args(inner):
            pair = _split_key_value(entry)
            if pair is None:
                return _NOT_LITERAL
            key_text, value_text = pair
            key = _literal_arg(key_text.strip())
            if key is None:
                key = key_text.strip()
                if not _BARE_KEY_RE.match(key):
                    return _NOT_LITERAL
            value = _parse_literal(value_text)
            if value is _NOT_LITERAL:
                return _NOT_LITERAL
            mapping[key] = value
        return mapping
    return _NOT_LITERAL


def _value_desc(value: object) -> str:
    """The shape name of a parsed literal (for the received-side diagnostic)."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    if value is None:
        return "null"
    return type(value).__name__


def _schema_desc(schema: object) -> str:
    """A compact human description of a JSON subschema (expected-side diagnostic).

    ``{"type":"array","items":{"type":"object","properties":{"text":…}}}``
    renders ``object[] of {text}``; a bare object renders ``{a, b}``.
    """
    if not isinstance(schema, Mapping):
        return "value"
    schema_type = schema.get("type")
    if schema_type == "array":
        items = schema.get("items")
        if isinstance(items, Mapping) and items.get("type") == "object":
            props = items.get("properties")
            keys = ", ".join(props) if isinstance(props, Mapping) else ""
            return f"object[] of {{{keys}}}" if keys else "object[]"
        return f"{_schema_desc(items)}[]"
    if schema_type == "object":
        props = schema.get("properties")
        if isinstance(props, Mapping) and props:
            return "{" + ", ".join(props) + "}"
        return "object"
    if schema_type == "integer":
        return "number"
    if isinstance(schema_type, str):
        return schema_type
    return "value"


def _deep_mismatch(value: object, schema: object, *, element: bool = False) -> str | None:
    """The received-shape phrase for a literal/schema contradiction, or ``None``.

    Conservative by construction: a subschema the gate cannot resolve (a union,
    ``$ref`` …) is skipped, and only demonstrable mismatches are reported — a
    wrong primitive type, an array element of the wrong type, or a missing
    required object key. ``additionalProperties`` is intentionally NOT policed
    (zod strips extra keys, so the frontend parser accepts them too).
    """
    if not isinstance(schema, Mapping):
        return None
    if any(key in schema for key in _UNRESOLVABLE_SCHEMA_KEYS):
        return None
    schema_type = schema.get("type")
    if not isinstance(schema_type, str):
        return None

    def received() -> str:
        got = _value_desc(value)
        return f"a {got} element" if element else f"a {got} literal"

    if schema_type == "string":
        return None if isinstance(value, str) else received()
    if schema_type in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return received()
        if schema_type == "integer" and not isinstance(value, int):
            return received()
        return None
    if schema_type == "boolean":
        return None if isinstance(value, bool) else received()
    if schema_type == "null":
        return None if value is None else received()
    if schema_type == "array":
        if not isinstance(value, list):
            return received()
        items = schema.get("items")
        if isinstance(items, Mapping):
            for item in value:
                sub = _deep_mismatch(item, items, element=True)
                if sub is not None:
                    return sub
        return None
    if schema_type == "object":
        if not isinstance(value, dict):
            return received()
        required = schema.get("required")
        if isinstance(required, list):
            for key in required:
                if key not in value:
                    return f"an object missing required key {key!r}"
        props = schema.get("properties")
        if isinstance(props, Mapping):
            for key, subschema in props.items():
                if key in value:
                    sub = _deep_mismatch(value[key], subschema)
                    if sub is not None:
                        return sub
        return None
    return None


def _prop_subschema(schema: ComponentSchema, prop: object) -> Mapping | None:
    """The JSON subschema for one prop, or ``None`` when the spec has none."""
    component_schema = schema.json_schema
    if not isinstance(component_schema, Mapping):
        return None
    props = component_schema.get("properties")
    if not isinstance(props, Mapping):
        return None
    sub = props.get(getattr(prop, "name", ""))
    return sub if isinstance(sub, Mapping) else None


def _slot_accepts_object(schema: ComponentSchema, prop: object) -> bool:
    """True when a ``{…}`` literal is legal at this slot (an object-typed prop)."""
    sub = _prop_subschema(schema, prop)
    if sub is not None and sub.get("type") == "object":
        return True
    return _expected_shape(getattr(prop, "type", "")) == "object"


def _type_mismatch(statement: Statement, schema: ComponentSchema) -> str | None:
    """Describe a positional/type mismatch, or ``None`` when the shapes agree.

    Two failure classes:

    * a **literal argument** whose value contradicts its prop's schema — the
      deep JSON-Schema check for spec'd (flavor) components, the coarse
      prop-type check for the four infrastructure components (which carry no
      emitted schema);
    * the trailing **options object** parked on any position but the options
      prop (the ``{glyph: …})`` 'blend' signature).

    The gate is deliberately conservative — bare refs/expressions are never
    flagged (the literal parser rejects them) and an unresolvable subschema is
    skipped — and it never repairs: a mismatch takes the keep-and-tag path.
    """
    props = schema.props
    if not props:
        return None
    options_index = next((i for i, prop in enumerate(props) if prop.name == "options"), None)
    for index, arg in enumerate(statement.args):
        shape = _arg_shape(arg)
        if shape is None:
            continue
        # Blend signature: an options object parked off its trailing slot. Only
        # a slot that does NOT itself accept an object can hold a misplaced
        # options object (SocialPost's `stats`/`reply` objects are legit objects).
        if (
            shape == "object"
            and options_index is not None
            and index != options_index
            and (index >= len(props) or not _slot_accepts_object(schema, props[index]))
        ):
            name = props[index].name if index < len(props) else f"arg {index + 1}"
            return (
                f"{statement.component} has an options object in position {index + 1} "
                f"({name}); the options object must be the trailing argument"
            )
        if index >= len(props):
            continue
        prop = props[index]
        subschema = _prop_subschema(schema, prop)
        if subschema is not None:
            value = _parse_literal(arg)
            if value is _NOT_LITERAL:
                continue
            got = _deep_mismatch(value, subschema)
            if got is not None:
                return (
                    f"{statement.component} arg {index + 1} ({prop.name}) expects "
                    f"{_schema_desc(subschema)}, got {got}"
                )
            continue
        expected = _expected_shape(prop.type)
        if expected is not None and shape != expected:
            return (
                f"{statement.component} arg {index + 1} ({prop.name}) expects "
                f"{prop.type}, got a {shape} literal"
            )
    return None


def _find_html(text: str) -> str | None:
    """Return the offending HTML snippet, or ``None`` when clean."""
    for pattern in (_HTML_INLINE_RE, _HTML_TAG_RE, _HTML_OPENER_RE):
        match = pattern.search(text)
        if match:
            return match.group(0)
    return None


def _check_balance(text: str) -> str | None:
    """Return an error code for unbalanced delimiters/quotes, else ``None``."""
    stack: list[str] = []
    quote: str | None = None
    escape = False
    for ch in text:
        if escape:
            escape = False
            continue
        if quote is not None:
            if ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in _QUOTES:
            quote = ch
        elif ch in _OPENERS:
            stack.append(ch)
        elif ch in _CLOSERS:
            if not stack or stack[-1] != _PAIRS[ch]:
                return "unbalanced_parens"
            stack.pop()
    if quote is not None:
        return "unbalanced_quotes"
    if stack:
        return "unbalanced_parens"
    return None


# ---------------------------------------------------------------------------
# Custom-statement helpers (raw-HTML escape hatch; policy in custom_policy)
# ---------------------------------------------------------------------------

#: DSL string-literal escapes the decoders/encoders round-trip.
_STRING_ESCAPES = {"\\": "\\", '"': '"', "'": "'", "n": "\n", "t": "\t", "r": "\r"}


def _literal_arg(arg: str) -> str | None:
    """Decode a quoted DSL string literal to its content, or ``None``.

    Only the three quote styles the DSL grammar accepts are decoded; an
    identifier/expression arg returns ``None`` (there is no literal content to
    police), so the gate never invents text it did not see.
    """
    text = (arg or "").strip()
    if len(text) < 2 or text[0] not in _QUOTES or text[-1] != text[0]:
        return None
    body = text[1:-1]
    out: list[str] = []
    index = 0
    while index < len(body):
        ch = body[index]
        if ch == "\\" and index + 1 < len(body):
            nxt = body[index + 1]
            out.append(_STRING_ESCAPES.get(nxt, nxt))
            index += 2
            continue
        out.append(ch)
        index += 1
    return "".join(out)


def _escape_dsl_string(text: str) -> str:
    """Escape ``text`` for embedding inside a double-quoted DSL string literal."""
    return (
        (text or "")
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )


def _custom_scrap_statement(statement: Statement, html: str, css: str) -> str:
    """The deterministic paper-scrap rewrite for an unrecoverable Custom.

    Reuses the existing ``Document`` component as the paper-scrap sink (no new
    frontend surface): ``Document("scrap", <title>, <source>, "unrendered custom
    panel")`` where ``source`` is the raw html+css (first 600 chars,
    DSL-escaped). The scrap keeps the Custom statement's identifier, so a
    ``ScenePane`` referencing it still resolves.
    """
    title = _literal_arg(statement.args[0]) if statement.args else None
    source = html if not css else f"{html}\n{css}"
    return (
        f'{statement.ident} = Document('
        f'"scrap", "{_escape_dsl_string(title or statement.component)}", '
        f'"{_escape_dsl_string(source[:600])}", "unrendered custom panel")'
    )


def _violation_code(violation: str) -> str:
    """The policy class of a ``custom_policy`` violation string (its first token)."""
    return violation.split(":", 1)[0].strip()


def _known_list(specs: Mapping[str, ComponentSchema]) -> str:
    return ", ".join(sorted(specs))


# ---------------------------------------------------------------------------
# per-segment processing (deterministic repair)
# ---------------------------------------------------------------------------


def _segment_statements(
    text: str, specs: Mapping[str, ComponentSchema], index: int
) -> tuple[str, list[DslError], bool]:
    """Process one DSL segment; return ``(final_text, errors, failed)``.

    Deterministically repairs kwargs statements (positionally) and reports every
    finding. ``failed`` is true when at least one statement was unrecoverable
    (the caller keeps the whole segment VERBATIM as a ``dsl`` segment and tags
    it — nothing is dropped).

    ``Custom`` statements are excluded from the generic raw-HTML scan (their
    payload is raw markup by design) and are checked by
    :mod:`fabulist.dsl.custom_policy` instead: a policy violation is flagged and
    the statement is rewritten in place to a ``Document`` paper-scrap (never a
    silent drop). A segment with no ``Custom`` statement keeps the original
    whole-segment scan byte-for-byte.
    """
    stripped = text.strip()
    if not stripped:
        return text, [DslError(index, "empty_statement", "empty DSL segment", None)], True

    statements = split_statements(stripped)
    if not statements:
        return text, [DslError(index, "empty_statement", "empty DSL segment", stripped)], True

    parsed = [parse_statement(stmt) for stmt in statements]
    custom_name = _custom_policy.CUSTOM_COMPONENT
    exempt_custom = custom_name in specs
    has_custom = exempt_custom and any(
        stmt is not None and stmt.component == custom_name for stmt in parsed
    )

    if has_custom:
        # The Custom statements are exempt; every other statement still gets the
        # generic scan (per statement, since the Custom ones must be skipped).
        for stmt, statement in zip(statements, parsed):
            if statement is not None and statement.component == custom_name:
                continue
            snippet = _find_html(stmt)
            if snippet is not None:
                return (
                    text,
                    [
                        DslError(
                            index,
                            "html_content",
                            f"raw HTML/script is not allowed in DSL (found {snippet!r})",
                            stmt,
                        )
                    ],
                    True,
                )
    else:
        html = _find_html(stripped)
        if html is not None:
            return (
                text,
                [
                    DslError(
                        index,
                        "html_content",
                        f"raw HTML/script is not allowed in DSL (found {html!r})",
                        stripped,
                    )
                ],
                True,
            )

    balance_error = _check_balance(stripped)
    if balance_error is not None:
        message = (
            "unbalanced quotes in DSL segment"
            if balance_error == "unbalanced_quotes"
            else "unbalanced parentheses/brackets in DSL segment"
        )
        return text, [DslError(index, balance_error, message, stripped)], True

    errors: list[DslError] = []
    final: list[str] = []
    failed = False
    for stmt, statement in zip(statements, parsed):
        if statement is None:
            errors.append(
                DslError(
                    index,
                    "malformed_statement",
                    "not a `identifier = Component(...)` statement",
                    stmt,
                )
            )
            failed = True
            continue

        schema = specs.get(statement.component)
        if schema is None:
            errors.append(
                DslError(
                    index,
                    "unknown_component",
                    f"unknown component {statement.component!r} (known: {_known_list(specs)})",
                    stmt,
                )
            )
            failed = True
            continue

        if statement.has_named_args:
            repaired = positionalize(statement, schema)
            if repaired is None:
                errors.append(
                    DslError(
                        index,
                        "named_args",
                        f"{statement.component} uses key=value args that do not map to its "
                        "declared positional props",
                        stmt,
                    )
                )
                failed = True
                continue
            errors.append(
                DslError(
                    index,
                    "named_args",
                    f"converted {statement.component} key=value args to positional order",
                    stmt,
                    repaired=True,
                )
            )
            reparsed = parse_statement(repaired)
            if reparsed is None:
                final.append(repaired)
                continue
            statement = reparsed
        else:
            if not schema.accepts(len(statement.args)):
                errors.append(
                    DslError(
                        index,
                        "arity_mismatch",
                        f"{statement.component} expects {schema.describe_arity()} positional "
                        f"prop(s), got {len(statement.args)}",
                        stmt,
                    )
                )
                failed = True
                continue
            mismatch = _type_mismatch(statement, schema)
            if mismatch is not None:
                errors.append(DslError(index, "type_mismatch", mismatch, stmt))
                failed = True
                continue

        if exempt_custom and statement.component == custom_name:
            html_value = _literal_arg(statement.args[1]) if len(statement.args) > 1 else None
            css_value = _literal_arg(statement.args[2]) if len(statement.args) > 2 else None
            violations = _custom_policy.validate_custom(html_value or "", css_value or "")
            if violations:
                for violation in violations:
                    errors.append(
                        DslError(index, _violation_code(violation), violation, statement.text)
                    )
                final.append(_custom_scrap_statement(statement, html_value or "", css_value or ""))
                continue
        final.append(statement.text)

    if failed:
        return text, errors, True
    return "\n".join(final), errors, False


def validate_dsl(segments: list[dict] | None) -> DslValidationReport:
    """Validate a narrator payload's segments, repair where deterministic, else keep + tag.

    Pure and synchronous. Prose segments (or any non-``dsl`` kind) pass through
    untouched; each unrecoverable ``dsl`` segment is KEPT VERBATIM as a ``dsl``
    segment (nothing is removed) and recorded in ``errors`` — the caller folds
    those findings into validator flags anchored to the segment index. A repaired
    segment (kwargs→positional, dangling refs dropped) is kept and gets an
    advisory ``DslError`` with ``repaired=True`` — the composition stays valid
    and no model retry fires. The one prune that is NOT a repair is a prune that
    would empty the child list: ``empty_after_prune`` is unrepaired, the authored
    statement is kept, and ``ok`` is false so the turn takes a repair round.
    """
    specs = component_specs()
    processed: dict[int, tuple[str, list[DslError], bool]] = {}

    # Phase A — repair kwargs + collect the set of declared identifiers.
    # Both child-orders occur in the wild (root FIRST or root LAST), so the
    # declaration set is collected across the whole reply before pruning.
    # A FAILED segment still declares its identifiers: its text is kept verbatim
    # (Phase C) and the narrator's repair loop may fix the statement in place, so
    # a child ref to it is NOT dangling — pruning it would corrupt a composition
    # the repair then leaves childless (live 286c897b: Entrance arity error →
    # `root = ScenePane("…", [])` with nothing to restore it). Only text with no
    # parsable statement contributes no identifier.
    declared: set[str] = set()
    for index, segment in enumerate(segments or []):
        if not isinstance(segment, Mapping) or segment.get("kind") != _DSL:
            continue
        text = segment.get("text")
        if not isinstance(text, str):
            continue
        final, errors, failed = _segment_statements(text, specs, index)
        processed[index] = (final, errors, failed)
        # On failure ``final`` omits the failing statement; the ORIGINAL text is
        # the verbatim form that gets kept, so declarations come from there.
        for stmt in split_statements(text if failed else final):
            statement = parse_statement(stmt)
            if statement is not None and statement.component in specs:
                declared.add(statement.ident)

    # Phase B — prune dangling ScenePane child refs among the survivors.
    #
    # The prune is ORDER-CRITICAL (issue #30): dropping the phantom refs is what
    # CREATES the empty pane, so "every child was undeclared" must never be
    # taken as a tidy-up. When the prune would empty the child list, the prune is
    # REFUSED: `root = ScenePane(t, [ghost])` stays exactly as authored and the
    # finding is a HARD failure (``repaired=False`` → ``ok`` false → a repair
    # round), never the ``info`` repair that once committed a hollow panel as a
    # successful turn. Keeping the authored text is also what keeps the finding
    # OBSERVABLE after the fact: the undeclared refs are still in the channel, so
    # re-validating the committed segments re-derives it (the repair loop and the
    # post-commit validate node both re-read the channel, not this report).
    for index, (final, errors, failed) in list(processed.items()):
        if failed:
            continue
        rebuilt: list[str] = []
        dropped_all: list[str] = []
        emptied_by: list[str] = []
        for stmt in split_statements(final):
            new_text, dropped, empty = prune_child_refs(stmt, declared)
            if empty:
                # Refuse the prune-to-empty: the statement is kept as authored so
                # the composition is flagged for repair, not degraded to a hollow
                # pane (the frontend's raw fallback then shows the statement
                # itself, next to the warn-severity finding, instead of an empty
                # ScenePane that reads as a successful panel).
                emptied_by.extend(dropped)
                rebuilt.append(stmt)
                continue
            rebuilt.append(new_text)
            dropped_all.extend(dropped)
        if not dropped_all and not emptied_by:
            continue
        original = ""
        for i, segment in enumerate(segments or []):
            if i == index and isinstance(segment, Mapping):
                original = str(segment.get("text", ""))
                break
        errors = list(errors)
        if dropped_all:
            errors.append(
                DslError(
                    index,
                    "dangling_ref",
                    f"dropped undeclared child refs: {', '.join(dropped_all)}",
                    original or final,
                    repaired=True,
                )
            )
        if emptied_by:
            errors.append(
                DslError(
                    index,
                    "empty_after_prune",
                    "every child ref ("
                    f"{', '.join(emptied_by)}) is undeclared; refusing to prune the "
                    "root to an empty pane — declare those children or compose the "
                    "root from identifiers this turn declares",
                    original or final,
                    repaired=False,
                )
            )
        processed[index] = ("\n".join(rebuilt), errors, failed)

    # Phase C — build the ordered, authoritative output payload. A failed DSL
    # segment is KEPT VERBATIM (keep + tag): the panel stays in the payload, the
    # frontend's own raw-fallback renders it, and the anchored validator flag
    # explains why. Nothing is filtered out.
    errors_all: list[DslError] = []
    out: list[dict] = []
    for index, segment in enumerate(segments or []):
        if not isinstance(segment, Mapping):
            # Unreachable from the segmenter (it emits mappings); a genuinely
            # malformed input has no statement text to preserve, so it falls
            # back to the empty prose marker the frontend already guards.
            errors_all.append(
                DslError(index, "malformed_segment", f"segment is not a mapping: {segment!r}")
            )
            out.append({"kind": "prose", "text": ""})
            continue

        kind = segment.get("kind", "prose")
        if kind != _DSL:
            out.append(dict(segment))
            continue

        text = segment.get("text", "")
        if not isinstance(text, str):
            errors_all.append(
                DslError(index, "malformed_segment", "dsl segment text must be a string")
            )
            out.append({"kind": "prose", "text": ""})
            continue

        final, errors, failed = processed[index]
        errors_all.extend(errors)
        if failed:
            # Keep the segment's ORIGINAL text verbatim as a dsl segment.
            out.append({**dict(segment), "text": text})
        else:
            out.append({**dict(segment), "text": final} if final != text else dict(segment))

    ok = not any(not e.repaired for e in errors_all)
    return DslValidationReport(ok=ok, errors=errors_all, segments=out)


def dsl_error_flags(errors: list | None) -> list[dict]:
    """Project DSL gate findings into the inspector's flag shape.

    Accepts :class:`DslError` objects or their ``dataclasses.asdict`` form (the
    narrator node serializes findings into graph state). The shape matches
    :meth:`fabulist.graph.validator.ValidatorFlag.as_dict` so the ``/inspect``
    read model (and the transcript badges) surface DSL gate failures alongside the
    Dress-Rehearsal flags — the W2.4 "invisible errors" fix. Every unrepaired
    finding is a ``warn`` (never downgraded out of the UI); ``segment_index``
    anchors it to the message segment so the tag renders on that panel.
    """
    out: list[dict] = []
    for item in errors or []:
        if isinstance(item, DslError):
            code, message, statement = item.code, item.message, item.statement
            segment_index, repaired = item.segment_index, item.repaired
        elif isinstance(item, Mapping):
            code = str(item.get("code") or "dsl_error")
            message = str(item.get("message") or "")
            statement = item.get("statement")
            segment_index = item.get("segment_index")
            repaired = bool(item.get("repaired"))
        else:
            continue
        out.append(
            {
                "rule_id": code,
                "check": "dsl_validation",
                "severity": "info" if repaired else "warn",
                "evidence": statement or message,
                "source": "dsl_gate",
                "segment_index": segment_index,
                "message": message,
            }
        )
    return out


def _dsl_slots(segments: list[dict] | None) -> list[int]:
    """Indexes of the DSL segments, in document order — the layout ordinals."""
    return [
        index
        for index, segment in enumerate(segments or [])
        if isinstance(segment, Mapping) and segment.get("kind") == _DSL
    ]


def _dsl_texts(segments: list[dict] | None) -> list[str]:
    """The DSL statement texts of a payload, in document order."""
    return [
        str(segment.get("text") or "")
        for segment in segments or []
        if isinstance(segment, Mapping) and segment.get("kind") == _DSL
    ]


async def revalidate_with_retry(
    segments: list[dict] | None,
    resynthesize: Callable[[list[DslError]], Awaitable[list[dict]]],
) -> DslValidationReport:
    """One constrained re-synthesis pass — the *last resort* after deterministic repair.

    The first validation runs as in :func:`validate_dsl` (which already applied
    the deterministic kwargs/dangling repairs). When it is clean the report is
    returned unchanged (``retry_applied`` false) — so the known failure classes
    never cost a model call. Otherwise ``resynthesize`` is awaited with the
    collected errors and must return a full replacement ``segments`` list, which
    is re-validated; the returned report carries ``retry_applied=True`` and the
    *final* ``ok``.

    The replacement is POSITION-PRESERVING by contract: its DSL segments must
    sit at the same ordinals as the draft's (same count, same order). A seam
    that re-lays-out the payload — panels moved to the bottom, prose re-flowed —
    is DISCARDED (``resynth_discarded`` true) and the draft report stands. A
    replacement that changes no DSL text at all counts as discarded too: it
    repaired nothing.

    ``resynthesize`` is the only impure seam — the orchestrator wires it to a
    model call, tests inject a fake.
    """
    report = validate_dsl(segments)
    if report.ok:
        return report

    replacement = await resynthesize(list(report.errors))
    if not isinstance(replacement, list) or _dsl_slots(replacement) != _dsl_slots(segments):
        return DslValidationReport(
            ok=report.ok,
            errors=report.errors,
            segments=report.segments,
            retry_applied=True,
            resynth_discarded=True,
        )
    retry_report = validate_dsl(replacement)
    return DslValidationReport(
        ok=retry_report.ok,
        errors=retry_report.errors,
        segments=retry_report.segments,
        retry_applied=True,
        resynth_discarded=_dsl_texts(replacement) == _dsl_texts(segments),
    )
