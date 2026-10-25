"""GM dice rolls: the PLAN §4 pre-turn "GM rolls" seam.

PLAN §4 lists a pre-turn ``GM rolls`` step alongside the clock and RAG fetch,
and PLAN §4 tools include an internal ``roll()``. The narrator pass is prose +
openui DSL only (AGENTS non-negotiable 3), so dice are *not* left to the model:
this module deterministically finds roll requests in the incoming turn text and
rolls them, so the caller can append ``roll`` events (schema-gated) and inject
the results as facts before the narrator writes.

What matches (ST-ish conventions)
---------------------------------
Two independent request forms are recognised, and results are returned in the
order the requests appear in the text:

* **OOC command** — ``/roll <expr>`` anywhere in the text, e.g.
  ``"/roll 2d6+1"``. The command word is case-insensitive (``/ROLL``). The
  leading expression token after ``/roll`` is consumed; trailing prose is kept
  as the request's ``ask`` (``"/roll 2d6 for persuasion"`` -> ``"for
  persuasion"``).
* **Inline bracketed token** — ``[<expr>]``, optionally prefixed with a ``roll``
  or ``/roll`` keyword: ``"[2d6+1]"``, ``"[roll 2d6]"``, ``"[/roll 1d20]"``.
  The leading expression token inside the brackets is used and any trailing
  prose (e.g. ``"[roll 2d6 for damage]"``) is captured as the ``ask``.

An OOC ``/roll`` that sits *inside* a bracketed span is not double-counted; the
bracket handler owns it. Invalid candidates (``[hello]``, ``[2x6]``, ``[0d6]``,
``/roll nope``) are skipped silently — :func:`parse_roll_requests` never raises.
Identical expressions are preserved (each is a distinct request).

Expression grammar
------------------
``NdM`` terms with an optional ``+K`` / ``-K`` constant and multiple terms::

    2d6        d20       1d8-2       2d6+1d4+2      3d6+2-1

``N`` defaults to 1 (``"d20"`` == ``"1d20"``), ``d`` is case-insensitive, and
``M``/``N`` must be >= 1. A bare constant (``"5"``) is not a roll; at least one
dice term is required. Guardrails cap a single term at :data:`MAX_DICE_PER_TERM`
dice of at most :data:`MAX_SIDES` sides each so a hostile string cannot request
an unbounded roll.

Determinism
-----------
:func:`roll_dice` takes an optional ``rng``; the same seed yields the same
results. When ``rng`` is ``None`` the module-level :mod:`random` functions are
used (non-deterministic, fine for live play). :class:`RollResult.as_payload`
and :func:`roll_payload` emit the schema-gated shape the append gate requires
for ``roll`` (``{"dice": str, "result": int}``) plus the additive ``detail``
face breakdown (S4e/HB1, audit dead-end #6) so replay shows the faces, not just
the total.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass

__all__ = [
    "MAX_DICE_PER_TERM",
    "MAX_SIDES",
    "RollResult",
    "parse_roll_requests",
    "roll_dice",
    "roll_payload",
    "rolls_for_turn",
]

#: Guardrail: most dice a single ``NdM`` term may request.
MAX_DICE_PER_TERM = 100

#: Guardrail: most sides a single die may have.
MAX_SIDES = 1000

#: Guardrail: most characters kept from a request's trailing ``ask`` prose.
_ASK_MAX = 80

#: ``/roll <expr>`` OOC command; group 1 is the leading expression run.
_OOC_RE = re.compile(r"/roll\b\s*([0-9dD+\- ]+)", re.IGNORECASE)

#: A single bracketed region (non-nested).
_BRACKET_RE = re.compile(r"\[([^\[\]]+)\]")

#: Full-expression validity check (whitespace already stripped, lowercased).
_EXPR_RE = re.compile(
    r"^(?P<body>[+-]?\d*d\d+(?:[+-](?:\d*d\d+|\d+))*)$",
    re.IGNORECASE,
)

#: Term splitter used only on text already proven valid by ``_EXPR_RE``.
_TOKEN_RE = re.compile(r"([+-]?)(\d*d\d+|\d+)", re.IGNORECASE)

#: One ``NdM`` dice term.
_DICE_RE = re.compile(r"^(?P<count>\d*)d(?P<sides>\d+)$", re.IGNORECASE)

#: Leading run of expression characters, used to peel an expression off prose.
_LEADING_RE = re.compile(r"[0-9dD+\- ]+")

#: Optional ``roll`` / ``/roll`` keyword at the head of a bracket's content.
_KEYWORD_RE = re.compile(r"^/?\s*roll\b", re.IGNORECASE)


@dataclass(frozen=True)
class _Term:
    """One parsed term: a signed dice group or a signed constant."""

    sign: int  # +1 or -1
    dice: bool
    count: int
    sides: int
    value: int
    raw: str


@dataclass(frozen=True)
class RollResult:
    """The outcome of one dice expression.

    ``dice`` is the canonical expression (lowercased, whitespace-free), e.g.
    ``"2d6+1"``. ``result`` is the integer total; ``detail`` is the human-facing
    breakdown, e.g. ``"2d6[3,4]+1 = 8"``. ``ask`` is the short purpose carried
    by the request's trailing prose (``"[roll 2d6 for damage]"`` -> ``"for
    damage"``), or ``None`` when the request had none.
    """

    dice: str
    result: int
    detail: str
    ask: str | None = None

    def as_payload(self) -> dict[str, str | int]:
        """Return the schema-gated ``roll`` payload.

        Required keys stay ``dice`` + ``result`` (the gate's contract); the
        additive ``detail`` breakdown (S4e/HB1) keeps the individual faces the
        die showed, which the old payload dropped at persist (audit dead-end
        #6). The optional ``ask`` (owner steer 2026-10-10) carries the roll's
        short purpose for the transcript dice chip. Extras are allowed by the
        gate (D4); ``ask`` is omitted when there was no trailing prose.
        """
        payload: dict[str, str | int] = {
            "dice": self.dice,
            "result": self.result,
            "detail": self.detail,
        }
        if self.ask is not None:
            payload["ask"] = self.ask
        return payload


def _parse_expression(expr: str) -> list[_Term] | None:
    """Parse ``expr`` into terms, or ``None`` when it is not a valid roll.

    Whitespace is stripped and ``d`` is lowercased before validation, so
    ``" 2D6 + 1 "`` parses as ``"2d6+1"``. Returns ``None`` (never raises) for
    malformed expressions, non-positive counts/sides, and expressions with no
    dice term.
    """
    text = "".join(expr.split()).lower()
    if not text or _EXPR_RE.match(text) is None:
        return None

    terms: list[_Term] = []
    saw_dice = False
    for token in _TOKEN_RE.finditer(text):
        sign = -1 if token.group(1) == "-" else 1
        body = token.group(2)
        dice_match = _DICE_RE.match(body)
        if dice_match is not None:
            count_str = dice_match.group("count")
            count = int(count_str) if count_str else 1
            sides = int(dice_match.group("sides"))
            if (
                count < 1
                or sides < 1
                or count > MAX_DICE_PER_TERM
                or sides > MAX_SIDES
            ):
                return None
            raw = f"{count_str}d{sides}" if count_str else f"d{sides}"
            terms.append(_Term(sign, True, count, sides, 0, raw))
            saw_dice = True
        else:
            value = int(body)
            terms.append(_Term(sign, False, 0, 0, value, str(value)))

    if not saw_dice:
        return None
    return terms


def _canonical(terms: list[_Term]) -> str:
    """Reconstruct the canonical (lowercased, whitespace-free) expression."""
    parts: list[str] = []
    for index, term in enumerate(terms):
        if index == 0:
            prefix = "-" if term.sign < 0 else ""
        else:
            prefix = "-" if term.sign < 0 else "+"
        parts.append(prefix + term.raw)
    return "".join(parts)


def _render_detail(
    terms: list[_Term], faces_per_term: list[list[int]], total: int
) -> str:
    """Render the ``"2d6[3,4]+1 = 8"`` detail string."""
    parts: list[str] = []
    for index, (term, faces) in enumerate(zip(terms, faces_per_term)):
        if term.dice:
            rendered = f"{term.raw}[{','.join(str(f) for f in faces)}]"
        else:
            rendered = term.raw
        if index == 0:
            prefix = "-" if term.sign < 0 else ""
        else:
            prefix = "-" if term.sign < 0 else "+"
        parts.append(prefix + rendered)
    return "".join(parts) + f" = {total}"


def _capture_ask(rest: str) -> str | None:
    """Collapse and clip the trailing prose that follows a request expression.

    The transcript dice chip shows the roll's *purpose* (owner steer
    2026-10-10), so ``"[roll 2d6 for damage]"`` keeps ``"for damage"``. Text is
    whitespace-collapsed; an over-length ask is truncated on a word boundary at
    :data:`_ASK_MAX` characters and ``None`` is returned when there is no prose.
    """
    ask = " ".join(rest.split())
    if not ask:
        return None
    if len(ask) > _ASK_MAX:
        clipped = ask[:_ASK_MAX]
        cut = clipped.rfind(" ")
        if cut > 0:
            clipped = clipped[:cut]
        ask = clipped.rstrip()
    return ask or None


def _candidate(content: str, *, strip_keyword: bool) -> tuple[str, str | None] | None:
    """Canonicalise one request candidate, or ``None`` when it is not a roll.

    When ``strip_keyword`` is set the leading ``roll`` / ``/roll`` keyword is
    removed first. Only the leading run of expression characters is parsed; any
    trailing prose is returned (collapsed/clipped by :func:`_capture_ask`) as
    the request's ``ask`` — the previous behavior silently discarded it.
    """
    text = content.strip()
    if strip_keyword:
        text = _KEYWORD_RE.sub("", text, count=1).strip()
    match = _LEADING_RE.match(text)
    if match is None:
        return None
    terms = _parse_expression(match.group(0))
    if terms is None:
        return None
    return _canonical(terms), _capture_ask(text[match.end():])


def _collect_requests(text: str) -> list[tuple[int, str, str | None]]:
    """Find ``(position, expression, ask)`` triples in order of appearance."""
    if not text:
        return []

    bracket_spans = [(m.start(), m.end()) for m in _BRACKET_RE.finditer(text)]
    found: list[tuple[int, str, str | None]] = []

    for match in _OOC_RE.finditer(text):
        # A bracketed form owns any /roll that sits inside its span.
        if any(start <= match.start() < end for start, end in bracket_spans):
            continue
        candidate = _candidate(match.group(1), strip_keyword=False)
        if candidate is not None:
            # OOC form: the ask is the prose AFTER the matched /roll command.
            expr, _inline_ask = candidate
            found.append((match.start(), expr, _capture_ask(text[match.end():])))

    for match in _BRACKET_RE.finditer(text):
        candidate = _candidate(match.group(1), strip_keyword=True)
        if candidate is not None:
            expr, ask = candidate
            found.append((match.start(), expr, ask))

    found.sort(key=lambda item: item[0])
    return found


def parse_roll_requests(text: str) -> list[str]:
    """Find every valid dice expression in ``text`` (in order of appearance).

    Recognises OOC ``/roll <expr>`` commands and inline ``[<expr>]`` /
    ``[roll <expr>]`` bracketed tokens (see module docstring). Invalid
    candidates are skipped silently and an empty list is returned when nothing
    matches; this function never raises. Trailing prose is captured as each
    request's ``ask`` (see :func:`rolls_for_turn`).
    """
    return [expr for _, expr, _ in _collect_requests(text)]


def roll_dice(
    expr: str, *, rng: random.Random | None = None, ask: str | None = None
) -> RollResult:
    """Roll ``expr`` deterministically under ``rng`` and return the outcome.

    ``rng`` may be any object exposing ``randint(a, b)`` (a
    :class:`random.Random` or a test double); the same seed yields the same
    result. ``None`` uses the module-level :mod:`random` functions. ``ask``
    carries the request's short purpose when the caller parsed one. Raises
    :class:`ValueError` for an invalid expression — the caller-facing parse
    path (:func:`parse_roll_requests`) skips those instead.
    """
    terms = _parse_expression(expr)
    if terms is None:
        raise ValueError(f"invalid dice expression: {expr!r}")

    randint = (rng if rng is not None else random).randint
    total = 0
    faces_per_term: list[list[int]] = []
    for term in terms:
        if term.dice:
            faces = [randint(1, term.sides) for _ in range(term.count)]
            faces_per_term.append(faces)
            total += term.sign * sum(faces)
        else:
            faces_per_term.append([])
            total += term.sign * term.value

    return RollResult(
        dice=_canonical(terms),
        result=total,
        detail=_render_detail(terms, faces_per_term, total),
        ask=ask,
    )


def rolls_for_turn(
    text: str, rng: random.Random | None = None
) -> list[RollResult]:
    """Parse every roll request in ``text`` and roll each one, in order.

    Each result carries the request's ``ask`` (trailing prose) when the form
    supplied any, so the transcript dice chip can show what the roll was for.
    """
    return [
        roll_dice(expr, rng=rng, ask=ask)
        for _, expr, ask in _collect_requests(text)
    ]


def roll_payload(result: RollResult) -> dict[str, str | int]:
    """Return the schema-gated ``roll`` payload for ``result`` (incl. extras)."""
    return result.as_payload()
