"""Multi-character roster context (PLAN W3.7 v1: multi-card sessions).

A session may be seeded from more than one character card. Where the single-card
path (:func:`fabulist.wiring.session_persona_block`) injects exactly one
``### Character:`` block, this module generalises it:

  * :func:`extract_cards` pulls the card dicts back out of the event log, in
    ``seq`` order, from every ``system`` event that carries a dict ``card``.
  * :func:`persona_block` renders those cards into the persona text injected
    into the per-turn facts block. ONE card reproduces the wiring format
    byte-for-byte; two or more cards render a ``### Cast`` header with one
    ``#### {name}`` subsection per character.
  * :func:`roster_line` renders the short ``Cast: A, B, C`` one-liner that
    rides elsewhere in the prompt; it stays ``None`` for single-character
    sessions so the existing format sees no roster noise.

Design notes (boring on purpose):

  * The render is pure: cards in, text out. The log stays the source of truth
    (D9); nothing here reads or writes storage beyond ``extract_cards`` walking
    a supplied event list.
  * Greetings come from the card *events* (``payload["first_mes"]``), passed as
    ``first_mes`` aligned by index. When no list is supplied the card's own
    ``first_mes`` field is used; when a list is supplied it is authoritative
    (a missing index means "no greeting"). Only the FIRST card's opening scene
    renders in the cast form — the scene opened with that character.
  * The cast block renders EVERY card in full, in ``cards`` order — there is no
    budget cap and no truncation marker. The block is part of the STABLE prompt
    prefix (AGENTS.md cache contract), so the render is deterministic and
    byte-stable for a given session; silently dropping a card made the narrator
    invent the missing character from the short ``Cast:`` roster line alone.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Sequence

from fabulist.personas.schema import persona_lines_from_card

__all__ = [
    "extract_cards",
    "greeting_variants",
    "persona_block",
    "roster_line",
    "selected_greeting",
]

#: ``(label, card-key)`` field order shared by both render forms. Mirrors the
#: wiring module's ordered tuple exactly (do not import it — keep the contract
#: local so :mod:`fabulist.context.roster` has no wiring dependency).
_FIELDS: tuple[tuple[str, str], ...] = (
    ("Description", "description"),
    ("Personality", "personality"),
    ("Scenario", "scenario"),
)

#: Label for the first card's opening-scene line.
_OPENING_LABEL = "Opening scene (already established): "

#: Fallback name when a card omits one (matches the wiring module).
_UNKNOWN = "Unknown"


def extract_cards(events: Sequence[Any]) -> list[dict]:
    """Return the card dicts carried by ``system`` events, in ``seq`` order.

    A card rides as ``event.payload["card"]`` (W2.10b import). Non-``system``
    events, payloads without a dict ``card``, and events without a usable
    ``seq`` (stable-sorted last) are ignored. The returned dicts are the same
    objects stored in the events — callers must not mutate them.
    """

    found: list[tuple[int, int, dict]] = []
    for index, event in enumerate(events):
        if getattr(event, "type", None) != "system":
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            continue
        card = payload.get("card")
        if not isinstance(card, dict):
            continue
        seq = getattr(event, "seq", None)
        # Events without a seq keep their input order after the seq'd ones.
        order = seq if isinstance(seq, int) else index + (1 << 30)
        found.append((order, index, card))
    found.sort(key=lambda item: (item[0], item[1]))
    return [card for _, _, card in found]


def selected_greeting(events: Sequence[Any]) -> int:
    """Greeting index carried by the session's latest ``greeting_selected`` event.

    D30: the Opening block's swipe selection is an append-only system event
    ``{"event": "greeting_selected", "index": n}``. The last valid index wins.
    A session with no such event (every legacy session) returns ``0``, so the
    rendered persona block is byte-identical to the pre-D30 pipeline.
    """

    index = 0
    for event in events:
        if getattr(event, "type", None) != "system":
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            continue
        if payload.get("event") != "greeting_selected":
            continue
        value = payload.get("index")
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            index = value
    return index


def greeting_variants(card: dict, first_mes: str | None = None) -> list[str]:
    """The swipe cycle for a card: ``[first_mes, *alternate_greetings]``.

    ``first_mes`` overrides variant 0 when supplied (callers pass the seed
    event's ``first_mes`` so the rendered text matches the stored greeting);
    otherwise the card's own ``first_mes`` is used. Alternate greetings that
    are missing or non-string degrade to ``""`` rather than raising — a card is
    untrusted input. The list is never empty (variant 0 always exists).
    """

    if first_mes is None:
        first_mes = card.get("first_mes")
    variants = [str(first_mes or "")]
    alternates = card.get("alternate_greetings")
    if isinstance(alternates, list):
        variants.extend(str(alt or "") for alt in alternates)
    return variants


def _render(text: str, variables: Mapping[str, str] | None) -> str:
    """Resolve fixed-vocabulary ``{{macro}}`` tokens in card text (D43).

    Card body text is user-authored under SillyTavern conventions, where
    ``{{user}}`` / ``{{char}}`` / ``{{persona}}`` / ``{{world}}`` are normal and
    MUST resolve. The guard short-circuits on an absent/empty map, so a block
    rendered with no variables is byte-identical to the pre-macro render; the
    renderer function import stays local (``prompts.surfaces`` is a leaf, so no
    module-level cycle). Unknown tokens stay literal.
    """

    if not variables:
        return text
    from fabulist.prompts.surfaces import render_surface_text

    # Any EMPTY value stays literal (the module's rule: an unresolvable
    # token is visible, not silently dropped) so a blank substitution cannot
    # garble the sentence. NOTE: user is never empty in production -
    # fabulist.prompts.variables.resolve_variables supplies the literal
    # "user" when no persona is in effect (PART 2), so {{user}} renders "user"
    # while an absent world/persona still stays literal.
    resolved = {name: value for name, value in variables.items() if value}
    if not resolved:
        return text

    return render_surface_text(text, resolved)


def _name(card: dict, variables: Mapping[str, str] | None = None) -> str:
    """Card name, or the wiring module's ``Unknown`` fallback."""

    return _render(str(card.get("name") or _UNKNOWN), variables)


def _field_lines(
    card: dict, variables: Mapping[str, str] | None = None
) -> list[str]:
    """``Description:/Personality:/Scenario:`` lines for non-empty fields.

    D36: a card carrying ``extensions.fabulist.persona`` appends its rendered
    persona canon to the same block; a card without it is unchanged. Every
    inserted field value (and persona canon line) resolves fixed-vocabulary
    macros against ``variables`` (D43); ``None``/``{}`` is byte-identical.
    """

    lines: list[str] = []
    for label, key in _FIELDS:
        value = str(card.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {_render(value, variables)}")
    lines.extend(_render(line, variables) for line in persona_lines_from_card(card))
    return lines


def _greeting(
    card: dict,
    index: int,
    first_mes: Sequence[str] | None,
    variables: Mapping[str, str] | None = None,
) -> str:
    """Greeting for ``card`` at ``index``, or ``""`` when absent/empty.

    When ``first_mes`` is given it is authoritative and aligned by index (a
    missing index means no greeting); otherwise the card's own ``first_mes``
    field is used. The greeting is card body text, so it renders macros (D43).
    """

    if first_mes is None:
        raw: Any = card.get("first_mes")
    else:
        raw = first_mes[index] if index < len(first_mes) else None
    return _render(str(raw or "").strip(), variables)


def _single_block(
    card: dict,
    greeting: str,
    variables: Mapping[str, str] | None = None,
) -> str:
    """The exact single-card block produced by the wiring module."""

    lines = [f"### Character: {_name(card, variables)}"]
    lines.extend(_field_lines(card, variables))
    if greeting:
        lines.append(f"{_OPENING_LABEL}{greeting}")
    return "\n".join(lines)


def _cast_block(
    cards: Sequence[dict],
    first_mes: Sequence[str] | None,
    variables: Mapping[str, str] | None = None,
) -> str:
    """``### Cast`` header plus one ``#### {name}`` subsection per card.

    Every card renders in full, in ``cards`` order: no budget cap and no
    truncation marker. The render is pure and deterministic (same cards in ->
    same bytes out), which keeps the cast block a stable prompt prefix
    (AGENTS.md cache contract). Truncating dropped whole characters while the
    state block still named them, so the narrator invented them.
    """

    lines = ["### Cast"]
    for index, card in enumerate(cards):
        lines.append(f"#### {_name(card, variables)}")
        lines.extend(_field_lines(card, variables))
        # Only the first card opened the scene; later greetings are omitted.
        if index == 0:
            greeting = _greeting(card, index, first_mes, variables)
            if greeting:
                lines.append(f"{_OPENING_LABEL}{greeting}")
    return "\n".join(lines)


def persona_block(
    cards: Sequence[dict],
    first_mes: Sequence[str] | None = None,
    *,
    variables: Mapping[str, str] | None = None,
) -> str | None:
    """Render the persona block for a roster of cards, or ``None`` when empty.

    ONE card reproduces :func:`fabulist.wiring.session_persona_block` exactly
    (``### Character:`` + field lines + opening scene). Two or more render the
    ``### Cast`` form with the first card's opening scene only, every card in
    full. ``first_mes`` aligns to ``cards`` by index.

    ``variables`` is the fixed-vocabulary macro map (D43): when supplied, every
    inserted card field / name / greeting resolves ``{{user}}`` / ``{{char}}`` /
    ``{{persona}}`` / ``{{world}}``; unknown tokens stay literal. ``None``/``{}``
    leaves the render byte-identical to the pre-macro block.
    """

    if not cards:
        return None
    if len(cards) == 1:
        card = cards[0]
        return _single_block(
            card, _greeting(card, 0, first_mes, variables), variables
        )
    return _cast_block(cards, first_mes, variables)


def roster_line(cards: Sequence[dict]) -> str | None:
    """``Cast: Name A, Name B, ...`` for a multi-card roster, else ``None``.

    Single-character sessions return ``None`` so the existing prompt format is
    untouched; the roster line is noise until there is an actual roster.
    """

    if len(cards) < 2:
        return None
    return "Cast: " + ", ".join(_name(card) for card in cards)
