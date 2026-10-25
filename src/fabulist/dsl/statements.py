"""Spec-driven openui statement recognition + deterministic repair.

An openui statement is ``identifier = Component(args)``. Two argument forms
occur in the wild:

* **positional** — ``ArtifactCard("Compass", "Relic", "Points at the liar.")``
  (the only form the shipped ``@openuidev/lang-core`` parser accepts); and
* **named / kwargs** — ``ArtifactCard(name="Compass", ...)`` — which the parser
  rejects at render (``excess-args`` + ``null-required``) and which the old gate
  could not even see (it counted commas).

This module owns recognition of both forms, the top-level lexical splitters the
whole engine shares, the spec-driven kwargs→positional transform, and the
dangling-child-ref prune for ``ScenePane`` root statements. Everything here is
pure and synchronous; the spec table comes from :mod:`.spec`.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from .spec import (
    ComponentSchema,
    component_names_longest_first,
)

__all__ = [
    "Statement",
    "component_stmt_re",
    "is_named_arg",
    "is_statement",
    "parse_statement",
    "positionalize",
    "prune_child_refs",
    "split_args",
    "split_statements",
    "statement_like_re",
    "statement_re",
]

# ---------------------------------------------------------------------------
# lexical helpers (single source for the engine's DSL splitters)
# ---------------------------------------------------------------------------

_PAIRS = {")": "(", "]": "[", "}": "{"}
_OPENERS = "([{"
_CLOSERS = ")]}"
_QUOTES = "\"'`"

#: ``ident = Component(`` — the statement *shape*, component-agnostic.
_ASSIGNED_HEAD_RE = re.compile(
    r"^\s*(?P<ident>[A-Za-z_]\w*)\s*=\s*(?P<component>[A-Za-z_]\w*)\s*\("
)

_STATEMENT_RE = re.compile(
    r"^\s*(?P<ident>[A-Za-z_]\w*)\s*=\s*"
    r"(?P<component>[A-Za-z_]\w*)\s*"
    r"\((?P<args>.*)\)\s*$",
    re.DOTALL,
)

#: ``name=`` at the start of an argument (a negative lookahead keeps ``==`` out).
# Named-arg forms: kwargs ``name=value`` AND the colon form ``name: value``
# (g4-class models emit both; the colon form appeared after the generated doc
# briefly demonstrated it as a negative example — repair both, steer neither).
_NAME_ARG_RE = re.compile(r"^\s*(?P<name>[A-Za-z_]\w*)\s*(?:=(?!=)|:(?!:))\s*")
_BARE_IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")

_cached_re: re.Pattern[str] | None = None
_cached_names: tuple[str, ...] | None = None
_cached_like_re: re.Pattern[str] | None = None
_cached_like_names: tuple[str, ...] | None = None


def _alternation(names: Iterable[str]) -> str:
    """Regex alternation over ``names``, longest-first (``Artifact`` cannot
    swallow ``ArtifactCard``)."""
    ordered = tuple(sorted(set(names), key=lambda name: (-len(name), name)))
    return "|".join(re.escape(name) for name in ordered)


def _compile_statement_re(alternation: str, *, assigned: bool) -> re.Pattern[str]:
    head = r"^\s*[A-Za-z_]\w*\s*=\s*" if assigned else r"^\s*(?:[A-Za-z_]\w*\s*=\s*)?"
    return re.compile(rf"{head}(?:{alternation})\s*\(")


def split_statements(text: str) -> list[str]:
    """Split a DSL blob into statements at top-level newlines.

    Newlines inside parentheses/brackets/braces or quoted strings do not split,
    so a multi-line component call stays one statement.
    """
    statements: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    escape = False
    for ch in text:
        if escape:
            buf.append(ch)
            escape = False
            continue
        if quote is not None:
            buf.append(ch)
            if ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in _QUOTES:
            quote = ch
            buf.append(ch)
        elif ch in _OPENERS:
            depth += 1
            buf.append(ch)
        elif ch in _CLOSERS:
            depth -= 1
            buf.append(ch)
        elif ch == "\n" and depth <= 0:
            statements.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    statements.append("".join(buf))
    return [s.strip() for s in statements if s.strip()]


def split_args(inner: str) -> list[str]:
    """Split top-level comma-separated args (respecting nesting and quotes)."""
    args: list[str] = []
    buf: list[str] = []
    depth = 0
    quote: str | None = None
    escape = False
    for ch in inner:
        if escape:
            buf.append(ch)
            escape = False
            continue
        if quote is not None:
            buf.append(ch)
            if ch == "\\":
                escape = True
            elif ch == quote:
                quote = None
            continue
        if ch in _QUOTES:
            quote = ch
            buf.append(ch)
        elif ch in _OPENERS:
            depth += 1
            buf.append(ch)
        elif ch in _CLOSERS:
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    args.append("".join(buf))
    return [a.strip() for a in args if a.strip()]


def is_named_arg(arg: str) -> bool:
    """True when a split argument is ``name=value`` (kwargs) rather than a value."""
    return bool(_NAME_ARG_RE.match(arg))


# ---------------------------------------------------------------------------
# statement recognition
# ---------------------------------------------------------------------------


def statement_re(names: Iterable[str] | None = None) -> re.Pattern[str]:
    """Recognizer for assigned statements naming a *known* component.

    Names are sorted longest-first so ``ArtifactCard`` cannot be matched as
    ``Artifact`` before the alternation backtracks. The default cache is keyed on
    the CURRENT name set, so a hot-reloaded vocabulary (a regenerated or swapped
    component set, W9.27) rebuilds the regex instead of serving the first-import
    name list forever.
    """
    global _cached_re, _cached_names
    if names is not None:
        return _compile_statement_re(_alternation(names), assigned=True)
    current = component_names_longest_first()
    if _cached_re is None or _cached_names != current:
        _cached_names = current
        _cached_re = _compile_statement_re(_alternation(current), assigned=True)
    return _cached_re


def statement_like_re(names: Iterable[str] | None = None) -> re.Pattern[str]:
    """Recognizer for a component statement, ASSIGNED **or BARE**.

    ``statement_re`` requires ``ident =`` because a parse needs an identifier.
    SEGMENTATION does not: a live narrator emitted ``DialogueSpotlight("Hobbs",
    …)`` with no assignment, and both segmenters classed the line as prose, so
    the panel never left the paragraph (owner-reported 2026-10-17). The openui
    parser still skips an unassigned line, so the render side synthesises an
    identifier (`DslFallback`); this recogniser only decides what IS DSL.

    Like :func:`statement_re`, the default cache is invalidated when the current
    component name set changes (hot reload).
    """
    global _cached_like_re, _cached_like_names
    if names is not None:
        return _compile_statement_re(_alternation(names), assigned=False)
    current = component_names_longest_first()
    if _cached_like_re is None or _cached_like_names != current:
        _cached_like_names = current
        _cached_like_re = _compile_statement_re(_alternation(current), assigned=False)
    return _cached_like_re


def component_stmt_re() -> re.Pattern[str]:
    """The CURRENT component-statement recognizer (assigned or bare).

    Call it at the USE-SITE; never bind it at import time. The vocabulary
    hot-reloads (W9.27), so a module-level binding freezes the first-import
    component set — a swapped-in component would then be classed as prose by our
    own segmenter, which is the ``bad71c7af`` chain exactly (taught and gated,
    still raw in the transcript).
    """
    return statement_like_re()


def is_statement(line: str) -> bool:
    """True when ``line`` begins an assigned statement of a known component."""
    return bool(statement_re().match(line))


@dataclass(frozen=True)
class Statement:
    """A parsed ``ident = Component(args)`` statement."""

    ident: str
    component: str
    args: tuple[str, ...]
    text: str

    @property
    def named_args(self) -> tuple[tuple[str, str], ...]:
        """``(prop_name, raw_value)`` for every ``name=value`` argument."""
        out: list[tuple[str, str]] = []
        for arg in self.args:
            match = _NAME_ARG_RE.match(arg)
            if match:
                out.append((match.group("name"), arg[match.end() :].strip()))
        return tuple(out)

    @property
    def has_named_args(self) -> bool:
        return bool(self.named_args)


def parse_statement(text: str, *, known: Mapping | Iterable[str] | None = None) -> Statement | None:
    """Parse one statement, or ``None`` when the shape is not ``ident = Comp(...)``.

    ``known`` (an iterable of names or a mapping) restricts the component to the
    known set when supplied.
    """
    match = _STATEMENT_RE.match(text)
    if match is None:
        return None
    component = match.group("component")
    if known is not None:
        names = known.keys() if isinstance(known, Mapping) else known
        if component not in set(names):
            return None
    return Statement(
        ident=match.group("ident"),
        component=component,
        args=tuple(split_args(match.group("args"))),
        text=text,
    )


# ---------------------------------------------------------------------------
# deterministic repair #1: kwargs -> positional (spec prop order)
# ---------------------------------------------------------------------------


def positionalize(statement: Statement, schema: ComponentSchema) -> str | None:
    """Rewrite a kwargs (or mixed) statement positionally, or ``None``.

    The transform is mechanical: values are placed in the component's declared
    prop order. It succeeds only when every named arg maps to a declared prop
    and every required prop receives a value; otherwise it returns ``None`` (the
    caller keeps the statement verbatim + flags it, and the model's last-resort
    re-synthesis may run). Pure positional statements are returned unchanged.
    """
    if not statement.has_named_args:
        return statement.text

    positional: list[str] = []
    named: dict[str, str] = {}
    for arg in statement.args:
        match = _NAME_ARG_RE.match(arg)
        if match:
            named[match.group("name")] = arg[match.end() :].strip()
        else:
            positional.append(arg)

    prop_names = set(schema.prop_names)
    if any(name not in prop_names for name in named):
        return None

    values: list[str] = []
    for index, prop in enumerate(schema.props):
        if index < len(positional):
            values.append(positional[index])
        elif prop.name in named:
            values.append(named.pop(prop.name))
        elif prop.required:
            return None
        else:
            # Optional prop omitted: a later provided value cannot be expressed
            # positionally, so bail rather than silently drop it.
            if any(later.name in named for later in schema.props[index:]):
                return None
            break

    if len(positional) > len(schema.props) or named:
        return None
    return f"{statement.ident} = {statement.component}({', '.join(values)})"


# ---------------------------------------------------------------------------
# deterministic repair #2: drop undeclared bare child refs
# ---------------------------------------------------------------------------


def prune_child_refs(
    text: str,
    declared: frozenset[str] | set[str] | Iterable[str],
    *,
    component: str = "ScenePane",
    child_index: int = 1,
) -> tuple[str, list[str], bool]:
    """Drop undeclared *bare identifier* refs from a component's child list.

    Live-observed (sol T2–T15): the narrator composed
    ``root = ScenePane(t, [status, interface, subtext])`` referencing children it
    never declared. (A declaration that merely FAILED the gate still counts —
    validate_dsl collects idents from failed segments' original text — so only a
    ref whose ident appears in no segment text reaches this prune.) The parser then
    fails the whole statement and the renderer shows an empty box. Only bare
    identifiers are dropped — inline component calls, strings and nested lists
    are preserved, so the *mixed* child lists the old regex missed (sol T4's
    ``[ArtifactCard(...), Interface, DialogueSpotlight]``) are handled too.

    Returns ``(new_text, dropped, empty_after_prune)``. ``new_text`` is the
    original text when nothing was dropped.

    ``empty_after_prune`` is True when EVERY child of the list was a dangling
    bare ref — i.e. ``new_text`` is ``Component(title, [])``. The caller must
    NOT accept that text: pruning the last child leaves a HOLLOW panel, and a
    gate that committed it shipped an empty ``ScenePane`` as a successful turn
    (issue #30 — the root's only child was an undeclared ``chline``, the prune
    emptied the pane, ``empty_after_prune`` was filed as an ``info`` repair and
    the turn kept a panel with nothing in it). "Every child was undeclared" is
    a WRONG COMPOSITION, not a tidy-up: the flag exists so the caller can REFUSE
    the prune, keep the statement as authored (so the finding re-derives from
    the text) and take a repair round. ``new_text`` is still returned as the
    hypothetical prune, so the caller can name exactly which refs were dangling.
    """
    statement = parse_statement(text)
    if statement is None or statement.component != component:
        return text, [], False
    if len(statement.args) <= child_index:
        return text, [], False

    arg = statement.args[child_index].strip()
    if not (arg.startswith("[") and arg.endswith("]")):
        return text, [], False

    declared_set = set(declared)
    items = split_args(arg[1:-1])
    had_children = bool(items)
    kept: list[str] = []
    dropped: list[str] = []
    for item in items:
        stripped = item.strip()
        if _BARE_IDENT_RE.match(stripped) and stripped not in declared_set:
            dropped.append(stripped)
        else:
            kept.append(stripped)
    if not dropped:
        return text, [], False

    new_args = list(statement.args)
    new_args[child_index] = "[" + ", ".join(kept) + "]"
    new_text = f"{statement.ident} = {statement.component}({', '.join(new_args)})"
    return new_text, dropped, had_children and not kept
