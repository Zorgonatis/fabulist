"""Per-interaction display catalog (W6.3): the single authoring source.

The prompt pipeline is DATA (D18): every interaction in
:data:`fabulist.prompts.agent_sets.INTERACTIONS` owns a flow (its ordered
prompt blocks), a default agent binding and a place on the turn stream. What
the code never spelled out in one place was the *human* description of each
interaction — what it is called, what it does, and when it runs.

This module is that one place. It carries, per interaction, a display
``title``, a one-line ``blurb`` and the engine-facing ``trigger`` (the timing +
call-site description). Everything that needs to describe a step reads it here:

* :mod:`scripts.generate_agent_map` reads ``trigger`` for the D46 agent map so
  the generated artifact and this catalog can never drift;
* ``GET /api/prompts/steps`` (``fabulist.api.prompts_api``) surfaces
  ``title`` / ``blurb`` / ``trigger`` for the prompt-manager UI.

Coverage is enforced at import: every id in
:data:`~fabulist.prompts.agent_sets.INTERACTIONS` MUST have an entry, so a new
interaction fails loudly here instead of silently rendering a nameless step.
"""

from __future__ import annotations

from dataclasses import dataclass

from .agent_sets import INTERACTIONS

__all__ = ["INTERACTION_CATALOG", "InteractionMeta", "interaction_meta"]


@dataclass(frozen=True)
class InteractionMeta:
    """Display + timing metadata for one interaction (prompt-manager step)."""

    id: str
    title: str
    blurb: str
    trigger: str


#: The catalog, keyed by interaction id. ``trigger`` carries the same engine
#: timing/call-site description :mod:`scripts.generate_agent_map` emits as the
#: agent-map ``trigger`` field (authored here, cited by the generator).
INTERACTION_CATALOG: dict[str, InteractionMeta] = {
    "narrate": InteractionMeta(
        id="narrate",
        title="Narrator turn",
        blurb="The in-character turn: prose plus OpenUI panels.",
        trigger=(
            "pre-turn in-character turn: ONE streaming draft per send/regenerate; "
            "on validator flags the narrator repairs in-loop with targeted "
            "patch_prose / patch_dsl edits before the turn commits (W9.24/W9.25; graph narrator node)"
        ),
    ),
    "ooc": InteractionMeta(
        id="ooc",
        title="Out-of-character reply",
        blurb="Answers an [OOC] message directly; the scene does not advance.",
        trigger=(
            "out-of-character turn ([OOC]-flagged user message); never enters the "
            "graph, no clock/scene advance"
        ),
    ),
    "extract": InteractionMeta(
        id="extract",
        title="State extractor",
        blurb="Turns the committed narrative into a world-state delta.",
        trigger=(
            "post-turn, after the narrative commits; only reached when world-state "
            "tracking is disabled (bts_enabled=False)"
        ),
    ),
    "summarize": InteractionMeta(
        id="summarize",
        title="History summarizer",
        blurb=(
            "Folds the whole history into one continuity summary and forks the "
            "chat into a new era."
        ),
        trigger=(
            "the compact service (CompactService.compact) — [Compact] button, or "
            "the context monitor forcing at 90% of the usable budget (W9.26); "
            "chunked map-reduce against the summarizer's own window"
        ),
    ),
    "caption": InteractionMeta(
        id="caption",
        title="Image captioner",
        blurb="Describes an uploaded image or clip so the scene can reference it.",
        trigger="media attachment upload (POST /api/sessions/{id}/attachments)",
    ),
    "assistant": InteractionMeta(
        id="assistant",
        title="Assistant consult",
        blurb="A side assistant's reply, manual or via the per-turn hook.",
        trigger=(
            "manual consult (POST .../assistants) and the D38 per-turn assistant "
            "hook (fire-and-forget, after the turn commits)"
        ),
    ),
    "bts": InteractionMeta(
        id="bts",
        title="Behind-the-scenes world tracker",
        blurb=(
            "Post-turn world tracker: records the turn's changes as validated "
            "write actions against the identity-stable entity registry."
        ),
        trigger=(
            "post-turn, after the narrative commits (BtsRunner; bts_enabled). A "
            "tool loop: the model reads the registry / cast roster / recent turns, "
            "then changes the world with one validated action per call "
            "(establish, move, set, update, retire, rename, flag) inside a per-turn "
            "budget, and closes the turn with a completion that calls no tool. The "
            "engine appends each action and commits one world_state render plus the "
            "flat state-delta (W9.29)"
        ),
    ),
    "critic": InteractionMeta(
        id="critic",
        title="Turn critic",
        blurb="Post-turn quality gate: ok or revision diffs.",
        trigger=(
            "post-turn, in-graph at the tail of post_turn immediately after the "
            "narrative event is appended (D49); the turn lock is held until it "
            "returns. A fail verdict keeps the turn and logs its verdict — the "
            "regeneration it used to request is retired (W9.24)"
        ),
    ),
    "planner": InteractionMeta(
        id="planner",
        title="Story planner",
        blurb=(
            "Sets the plan the next turns steer by: forward story strings, the "
            "scene's date/time, weather and the facts the plan holds established."
        ),
        trigger=(
            "in-graph at the plan junction before the lanes and the narration; "
            "runs only when no plan exists yet (turn 1) or the preset's cadence "
            "says a refresh is due — never per turn (W9.20)"
        ),
    ),
    "npc": InteractionMeta(
        id="npc",
        title="NPC intent lane",
        blurb=(
            "One active character decides what they try to do and say this beat; "
            "the narrator synthesises every lane into the turn."
        ),
        trigger=(
            "pre-turn, in-graph at the npc_lanes node between pre_turn and narrate; "
            "one call per dispatched cast member — narrative-voice lane on the "
            "narration task (W9.25); max_lanes bounds concurrency, the dispatch "
            "policy bounds the set"
        ),
    ),
    "proposals": InteractionMeta(
        id="proposals",
        title="Asset proposal lane",
        blurb=(
            "One data type's amendments for review: a world, an actor or a book "
            "change with its evidence, filed for the owner to accept or dismiss."
        ),
        trigger=(
            "post-turn, OFF-TURN: the historian agent — cadence like the "
            "planner, default every 10 turns, or on demand — runs the three "
            "type lanes after the turn settles; never in the turn flow "
            "(W9.23 R3)"
        ),
    ),
}

#: Import-time coverage guard: a new INTERACTIONS entry without a catalog row
#: is a hard error (no nameless step, no silent agent-map drift).
_MISSING: list[str] = [
    interaction for interaction in INTERACTIONS if interaction not in INTERACTION_CATALOG
]
if _MISSING:  # pragma: no cover - exercised only when the sets drift
    raise RuntimeError(
        "fabulist.prompts.catalog is missing entries for interaction(s): "
        f"{', '.join(_MISSING)}; author title/blurb/trigger for each"
    )


def interaction_meta(interaction: str) -> InteractionMeta | None:
    """The catalog entry for ``interaction``, or ``None`` when unknown."""
    return INTERACTION_CATALOG.get(interaction)
