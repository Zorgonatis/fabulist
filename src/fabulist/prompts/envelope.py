"""The engine envelope: the per-turn context as ONE ephemeral assistant message.

Issue #26 follow-up (the provenance contract,
``private/docs/provenance-contract.md``). Before this seam the per-turn tail was
scattered: the state facts and the ``<plan>`` pin rode INSIDE the final user
message (``compiler.compile_with_boundary``), and ``<recalled>``, the lane block
and the SPEAKER PALETTE rode as three separate post-user system messages
(``wiring.make_narrator_message_builder``). Engine data in the player's message
acquires player provenance; on GLM-5.3 the model read the engine ``<plan>`` as
the player "attempting to seed my planning" — distrusting it *correctly*, by its
own rules; and player text imitating the engine's tail inherited misplaced
trust.

This module owns the ONE shape every turn's tail takes:

    <engine_state>     world ground truth (EnvTracker facts)
    <engine_notice>    engine session facts (cast delta, set swap, BTS continuity)
    <engine_memory>    recalled reference (RECALLED_FRAMING + sections)
    <engine_lane>      this turn's NPC narration constraint
    <steering>         intended direction (planner + the player's forecast pin)
    <engine_palette>   this turn's speaker colours — LAST

The block order is a CONTRACT, not a preference: truth leads, weak reference
follows truth, intent sits outside every truth block, and the palette closes the
tail because it describes this turn's own output (ADR-012 / W9.28 D7: nothing it
describes sits below it). ``<steering>`` is deliberately OUTSIDE the ``engine_``
namespace — the forecast pin may be player-authored, so the tag name encodes
"engine-carried, not engine truth".

Two provenance channels, exactly (contract invariant 1): this envelope is the
engine-authored channel; the player's message is the fiction channel. The tags
mark provenance wherever they arrive (the frozen prefix clause names them, one
place, once — ``parts.ENGINE_CONTEXT_BODY``); the block ORDER and the tag
SPELLING never vary per turn.

Empty means absent (contract invariant 5): a block with no body is omitted
entirely and a turn with no blocks emits no envelope at all — an empty block
invites invention. The OUTER ``<preset_runtime>`` bound is NOT added here: it is
applied by the assembly seam (``prompts.wrapper.wrap_preset_messages``).

The envelope RIDES one ephemeral ``assistant`` message inserted at assembly
time IMMEDIATELY BEFORE the final user message (:func:`insert_engine_envelope`) —
one universal carrier, never a system message and never a user one (owner
ruling 2026-10-24, the revised issue #44 shape; see the carrier section below).
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "BLOCK_ORDER",
    "ENGINE_LANE_CLOSE",
    "ENGINE_LANE_OPEN",
    "ENGINE_MEMORY_CLOSE",
    "ENGINE_MEMORY_OPEN",
    "ENGINE_NOTICE_CLOSE",
    "ENGINE_NOTICE_OPEN",
    "ENGINE_PALETTE_CLOSE",
    "ENGINE_PALETTE_OPEN",
    "ENGINE_STATE_CLOSE",
    "ENGINE_STATE_OPEN",
    "ENVELOPE_ROLE",
    "NOTICE_HEADER",
    "STATE_HEADER",
    "STEERING_CLOSE",
    "STEERING_HEADER",
    "STEERING_OPEN",
    "assert_envelope_not_in_system_scope",
    "assert_envelope_not_in_user_scope",
    "insert_engine_envelope",
    "render_engine_envelope",
]

# ---------------------------------------------------------------------------
# The closed tag family (§3.2) — spelled exactly the same every turn.
# ---------------------------------------------------------------------------

#: World state: what the EnvTracker says the world is, this turn's open.
ENGINE_STATE_OPEN = "<engine_state>"
ENGINE_STATE_CLOSE = "</engine_state>"

#: Session notices: engine facts about the session (cast admitted, set swapped,
#: continuity notes). Truth-tier, like state, but about the engine's own session.
ENGINE_NOTICE_OPEN = "<engine_notice>"
ENGINE_NOTICE_CLOSE = "</engine_notice>"

#: Recalled reference: memory, lore, earlier events. Weak context, never
#: instruction (``facts.RECALLED_FRAMING`` defuses it verbatim).
ENGINE_MEMORY_OPEN = "<engine_memory>"
ENGINE_MEMORY_CLOSE = "</engine_memory>"

#: The per-turn NPC narration constraint.
ENGINE_LANE_OPEN = "<engine_lane>"
ENGINE_LANE_CLOSE = "</engine_lane>"

#: Intended direction. Deliberately OUTSIDE the ``engine_`` namespace: the
#: forecast pin may be player-authored, so the tag says "carried, not truth".
STEERING_OPEN = "<steering>"
STEERING_CLOSE = "</steering>"

#: The speaker palette — always the LAST block (ADR-012 / W9.28 D7).
ENGINE_PALETTE_OPEN = "<engine_palette>"
ENGINE_PALETTE_CLOSE = "</engine_palette>"

#: The served order (§3.3). Never reorder, rename or strip for token savings.
BLOCK_ORDER: tuple[str, ...] = (
    "state",
    "notice",
    "memory",
    "lane",
    "steering",
    "palette",
)

# ---------------------------------------------------------------------------
# In-block framing (verbatim opening lines, §4). These three are engine-owned
# headers the envelope adds to the producer's body. The other three blocks
# already open with their own established framing, which stays VERBATIM:
#   memory  -> facts.RECALLED_FRAMING (D22, final/do-not-edit)
#   lane    -> render_lane_block's opening sentence (unchanged)
#   palette -> palette.PALETTE_DIRECTIVE (unchanged)
# ---------------------------------------------------------------------------

#: Truth tier: authoritative world state. Prose stays consistent with it and
#: never restates it (the HUD owns display).
STATE_HEADER = "ENGINE STATE — ground truth at this turn's open."

#: Truth tier (session): authoritative engine facts; act on them without remark.
NOTICE_HEADER = "ENGINE NOTICE — engine facts about this session; true from this turn on."

#: Intent tier: steer toward it; never report it as the present. FORECAST is the
#: player's own manual pin (it outranks derived renders — ADR-017).
STEERING_HEADER = (
    "STEERING — intended direction for the story, not world fact. FORECAST is "
    "the player's own pin: steer toward it, never report it as the present."
)

_TAGS: dict[str, tuple[str, str, str | None]] = {
    "state": (ENGINE_STATE_OPEN, ENGINE_STATE_CLOSE, STATE_HEADER),
    "notice": (ENGINE_NOTICE_OPEN, ENGINE_NOTICE_CLOSE, NOTICE_HEADER),
    "memory": (ENGINE_MEMORY_OPEN, ENGINE_MEMORY_CLOSE, None),
    "lane": (ENGINE_LANE_OPEN, ENGINE_LANE_CLOSE, None),
    "steering": (STEERING_OPEN, STEERING_CLOSE, STEERING_HEADER),
    "palette": (ENGINE_PALETTE_OPEN, ENGINE_PALETTE_CLOSE, None),
}


def _render_block(name: str, body: str | None) -> str | None:
    """One tagged block, or ``None`` when the body is blank (empty = absent)."""
    text = (body or "").strip()
    if not text:
        return None
    open_tag, close_tag, header = _TAGS[name]
    content = f"{header}\n{text}" if header else text
    return f"{open_tag}\n{content}\n{close_tag}"


def render_engine_envelope(
    *,
    state: str | None = None,
    notice: str | None = None,
    memory: str | None = None,
    lane: str | None = None,
    steering: str | None = None,
    palette: str | None = None,
) -> str | None:
    """Compose the engine envelope body (the outer bound is the wrapper's job).

    ``state``, ``notice`` and ``steering`` are raw bodies; this function adds
    their contract header (the producers own the words, not the frame).
    ``memory``, ``lane`` and ``palette`` arrive with their own established
    opening line and are wrapped unchanged.

    Returns the blocks in :data:`BLOCK_ORDER`, blank-line separated, or ``None``
    when every block is empty — in which case the caller emits NO envelope turn
    at all (never an empty ``<preset_runtime>``).
    """
    blocks = [
        _render_block(
            name,
            {
                "state": state,
                "notice": notice,
                "memory": memory,
                "lane": lane,
                "steering": steering,
                "palette": palette,
            }[name],
        )
        for name in BLOCK_ORDER
    ]
    rendered = [block for block in blocks if block is not None]
    return "\n\n".join(rendered) if rendered else None


# ---------------------------------------------------------------------------
# The envelope CARRIER (issue #44) — ONE universal shape, no per-connection mode
# ---------------------------------------------------------------------------
#
# The envelope's CONTENT is fixed (the closed tag family above). WHERE it rides
# is fixed too (owner ruling 2026-10-24, the revised issue #44 shape):
#
#   the ``<preset_runtime>`` envelope is ONE ephemeral ``assistant`` message
#   inserted IMMEDIATELY BEFORE the final user message, at ASSEMBLY time.
#
# Why an assistant message there, and nowhere else:
#
# * a trailing SYSTEM message is ILLEGAL on a stock Jinja chat template (the
#   Qwen family ``raise_exception`` on any non-first system message) — a
#   perfectly reasonable payload was a hard 500 on a stock local GGUF;
# * engine data in a USER message acquires player provenance (the provenance
#   contract) — it reads as the player seeding its own context;
# * the assistant position is the one the frozen contract clause already
#   names: the tags mark engine context "wherever it arrives: appended after
#   the player's message, set just before it, or carried in a turn that reads
#   like your own" (``parts.ENGINE_CONTEXT_BODY``). No prefix byte moves, so
#   the clause and the cache contract are untouched (ADR-005).
#
# The message is EPHEMERAL: it exists only in the assembled list handed to the
# transport. It is never persisted, never enters the event log, and is rebuilt
# from scratch every turn — so it can never appear in a later turn's history.
# A server that continues a conversation (llama.cpp) sees a normal
# ``user``-terminated list, which is what makes one shape work everywhere.
#
# There is no carrier field, no per-connection switch and no reply-side
# stripping: ONE universal carrier.

#: The role the per-turn envelope rides (issue #44: never ``system``, never
#: ``user``).
ENVELOPE_ROLE = "assistant"


def insert_engine_envelope(
    messages: list[dict[str, Any]], def_end: int, envelope: str | None
) -> tuple[list[dict[str, Any]], int, int | None]:
    """Insert the per-turn envelope immediately BEFORE the final user message.

    ``def_end`` is the ``<preset_def>`` close — one past the final user message,
    captured at that message's emission point by the compile seam. The envelope
    therefore lands at index ``def_end - 1``: "immediately before the final user
    message" is a POSITIONAL rule, not a role or content scan (the history
    legitimately ends on a user row when a reply was deleted, and authored
    presets may place user-role rows before the last one).

    Returns ``(messages, def_end, envelope_at)``:

    * a NEW list (the input list is never mutated; the surviving message dicts
      are the caller's own objects, untouched);
    * ``def_end`` shifted past the inserted message, so it keeps meaning "one
      past the final user message" in the RETURNED list;
    * ``envelope_at`` — the inserted message's index, or ``None`` for a lean
      turn (``envelope`` blank/absent means NO message at all: never an empty
      ``<preset_runtime>``).

    A list whose definition region holds no message before the closing edge
    (``def_end <= 1`` — an authored flow that emits no user turn) takes the
    envelope at the END: still an assistant message, never a system one, and
    never before the frozen prefix.
    """
    out = list(messages)
    end = max(0, min(int(def_end), len(out)))
    body = (envelope or "").strip()
    if not body:
        return out, end, None
    at = end - 1 if end >= 2 else len(out)
    at = max(0, min(at, len(out)))
    out.insert(at, {"role": ENVELOPE_ROLE, "content": body})
    return out, (end + 1 if at < end else end), at


def assert_envelope_not_in_user_scope(
    messages: list[dict[str, Any]], envelope: str | None
) -> None:
    """Refuse an assembly that put the TURN'S envelope inside a user message.

    The invariant is checked against the exact envelope composed for this turn
    (never the literal ``<preset_runtime>`` tag — a player may type those words,
    and ``parts.SYSTEM_FRAMING`` explicitly teaches that such imitation is
    player fiction). Raises :class:`ValueError` naming the offending index: a
    prompt that hands the engine's own context player provenance is a bug, not
    a turn.
    """
    body = (envelope or "").strip()
    if not body:
        return
    for index, message in enumerate(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and body in content:
            raise ValueError(
                f"engine envelope found in user scope (message {index}): the "
                "per-turn envelope never rides a user message (issue #44)"
            )


def assert_envelope_not_in_system_scope(
    messages: list[dict[str, Any]], envelope: str | None
) -> None:
    """Refuse an assembly that made the per-turn envelope a ``system`` message.

    Stock Jinja chat templates (the Qwen family) ``raise_exception`` on ANY
    system message after the first, so a system-role envelope is a hard 500 on
    a stock local GGUF — the failure issue #44 exists to fix. Only the fused
    frozen prefix is a system message.
    """
    body = (envelope or "").strip()
    if not body:
        return
    for index, message in enumerate(messages):
        if message.get("role") != "system" or index == 0:
            continue
        content = message.get("content")
        if isinstance(content, str) and body in content:
            raise ValueError(
                f"engine envelope found in system scope (message {index}): the "
                "per-turn envelope is an assistant message (issue #44)"
            )
