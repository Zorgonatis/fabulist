"""Narrator prompt assembly (PLAN W1.2, R2 / D2 / D6).

The narrator pass produces prose + mini-OpenUI DSL and nothing else. All world state
arrives as engine context (the ``<engine_state>`` block) and is captured out-of-band
by the post-turn extractor, so no tracking or state-emission instructions live here.

The narrator SYSTEM prompt itself is compiled from the directive catalog
(:mod:`fabulist.prompts.parts` → :func:`fabulist.prompts.compiler.render_system_prompt`)
— ONE content source for the live narrate path and the greeting/opening path
(the hand-synced text mirror this module used to carry is retired with the
Content Engine consolidation §5.3). What remains here are the assembly
mechanics other surfaces still consume: the engine-state delimiters, the OOC
flag, the neutral facts example, and ``assemble_messages``.

Issue #26 follow-up (provenance contract): the state facts left the final user
message. They are one block of the trailing engine envelope
(:mod:`fabulist.prompts.envelope`), so :func:`render_state_block` now returns
the block BODY and the envelope owns the tag pair and the ground-truth header.
"""

from __future__ import annotations

from typing import Any

from .envelope import ENGINE_STATE_CLOSE, ENGINE_STATE_OPEN, STATE_HEADER
from .envelope import insert_engine_envelope, render_engine_envelope

__all__ = [
    "DEFAULT_FACTS_EXAMPLE",
    "ENGINE_STATE_CLOSE",
    "ENGINE_STATE_OPEN",
    "OOC_FLAG",
    "assemble_messages",
    "render_facts_block",
    "render_state_block",
]

# Delimiters and flag from the shared engine contract.
#: The state block tags live in :mod:`fabulist.prompts.envelope` (the closed
#: family); re-exported here because this module is the historical import site.
ENGINE_STATE_OPEN = ENGINE_STATE_OPEN
ENGINE_STATE_CLOSE = ENGINE_STATE_CLOSE
OOC_FLAG = "[OOC]"

#: The retired ``<state>`` spelling, kept as a name alias only so pre-contract
#: importers fail on the SEMANTICS they can see rather than on an ImportError.
#: The emitted tag is ``<engine_state>`` — the old tag never appears in a prompt.
FACTS_OPEN = ENGINE_STATE_OPEN
FACTS_CLOSE = ENGINE_STATE_CLOSE

# One pipe-YAML line, matching the engine's facts convention (PLAN D2). Deliberately
# neutral: the example teaches only the FORMAT — real facts arrive each turn as
# injected context, and evocative example values read to the model as a line to
# print (the HUD owns state display).
DEFAULT_FACTS_EXAMPLE = "time: 14:35 | date: day 1 | tone: warm"

_VALID_KINDS = ("in_character", "ooc")


def render_state_block(facts: str | None) -> str | None:
    """The ``<engine_state>`` block BODY from raw facts, or ``None``.

    The body is the pipe-YAML facts text; :func:`render_engine_envelope` wraps
    it in ``<engine_state>`` and prepends :data:`fabulist.prompts.envelope.STATE_HEADER`.
    Blank/None yields ``None`` (empty means absent). A pre-tagged or
    pre-headered block is normalized instead of double-wrapped, so callers may
    pass a renderer's output through.
    """
    if facts is None:
        return None
    body = facts.strip()
    if not body:
        return None
    # Normalize a pre-wrapped body: the contract tag, or the retired ``<state>``
    # spelling the rules clock still carries internally (``graph.clock``).
    for open_tag, close_tag in (
        (ENGINE_STATE_OPEN, ENGINE_STATE_CLOSE),
        ("<state>", "</state>"),
    ):
        if body.startswith(open_tag):
            body = body[len(open_tag) :].strip()
            if body.endswith(close_tag):
                body = body[: -len(close_tag)].strip()
            break
    if body.startswith(STATE_HEADER):
        body = body[len(STATE_HEADER) :].strip()
    return body or None


#: Retired name for :func:`render_state_block` (pre-contract importers).
render_facts_block = render_state_block


def assemble_messages(
    system: str,
    history: list[dict[str, Any]],
    facts: str | None,
    user_msg: str,
    kind: str,
) -> list[dict[str, Any]]:
    """Build the narrator message list for one turn.

    Layout: system prompt, prior history, ONE ephemeral ``assistant`` engine
    envelope carrying the state facts, then the player's message (bare) — the
    envelope is inserted IMMEDIATELY BEFORE the final user message (issue #44,
    owner ruling 2026-10-24: the ONE universal carrier). The stable prefix
    survives prefix-caching and the per-turn data rides the envelope; nothing
    engine-authored rides inside the player's message (provenance contract) and
    the envelope is never a ``system`` message (a stock Jinja template
    ``raise_exception``s on any non-first system message).

    ``kind`` is ``"in_character"`` or ``"ooc"``; an OOC turn flags the final user
    message with :data:`OOC_FLAG` and the prompt's OOC clause takes over. The
    envelope rides the OOC turn unchanged — it is the same engine context.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"kind must be one of {_VALID_KINDS}, got {kind!r}")

    messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
    messages.extend(history)

    content = f"{OOC_FLAG} {user_msg}" if kind == "ooc" else user_msg
    messages.append({"role": "user", "content": content})
    envelope = render_engine_envelope(state=render_state_block(facts))
    messages, _def_end, _envelope_at = insert_engine_envelope(
        messages, len(messages), envelope
    )
    return messages
