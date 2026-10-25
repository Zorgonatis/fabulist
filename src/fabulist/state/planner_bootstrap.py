"""The planner's seed/repair tool phase (W9.31 §3): places first, then repair.

The planner consumes the object stores the way the tracker fills them: before it
writes a plan it runs ONE bounded tool loop over the SAME
:func:`fabulist.state.bts.bts_loop_tools` surface the tracker uses, so it can
seed any state the tracker can write — locations (parents and their children
first), items, threads, relationships and meters (§3.1, owner requirement 1).

Two modes, one discriminator decided by the CALLER (the node owns it —
``load_last_plan(...) is None`` is the bootstrap, §4/D1):

* :data:`BOOTSTRAP` — the session's first planner run. The tracker's whole tool
  set except the clock proposal (:data:`PLANNER_BOOTSTRAP_TOOLS`), the places-first
  directive, :data:`PLANNER_BOOTSTRAP_BUDGET` writes.
* :data:`REPAIR` — every later run. The SAME reads plus the nine MUTATING tools
  only (:data:`REPAIR_TOOL_NAMES`): the four ``establish_*`` tools — and
  ``establish_economy``, an establish spelled otherwise — are absent, so
  "repair what play revealed, never re-seed" is enforced by the tool SET and not
  by wording alone (§3.1/D4). :data:`PLANNER_REPAIR_BUDGET` writes.

The protocol rides a runner-side TAIL message (:data:`BOOTSTRAP_DIRECTIVE` /
:data:`REPAIR_DIRECTIVE` + the entity palette), exactly like
:data:`fabulist.state.bts.BTS_ACTION_PROTOCOL`: AGENTS 6 keeps the seeded prefix
(the preset's ``planner`` flow directive + the world/cast context) byte-stable, and
a stored preset customisation cannot drop the protocol (D2). The tail itself is the
NARRATOR's carrier (issue #67): ONE ephemeral ``assistant`` message inserted
immediately before the final user message, never a second ``system`` message — a
stock Jinja chat template refuses any system message after index 0, which killed
the turn in the seed phase.

The plan itself is NOT produced here (§3.2): the phase's closing summary is
discarded and the unchanged single-call plan ladder runs afterwards in the node.
Total output failure — a transport timeout, or no accepted action with no closing
summary — raises :class:`PlannerBootstrapError`; the node contains it separately
from the plan's own failure (§6/S2), so a failed seed never costs the first turn
its plan.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..prompts.envelope import (
    assert_envelope_not_in_system_scope,
    assert_envelope_not_in_user_scope,
    insert_engine_envelope,
)
from .bts import (
    FOLDED_STATE_TOOL,
    RECENT_DELTAS_TOOL,
    ROSTER_TOOL,
    bts_loop_tools,
    registry_palette,
)
from .entity_tools import (
    ADJUST_ECONOMY_TOOL,
    ESTABLISH_ECONOMY_TOOL,
    ESTABLISH_ITEM_TOOL,
    ESTABLISH_LOCATION_TOOL,
    ESTABLISH_RELATIONSHIP_TOOL,
    ESTABLISH_THREAD_TOOL,
    FLAG_CONTRADICTION_TOOL,
    MOVE_BEING_TOOL,
    RENAME_BEING_TOOL,
    RETIRE_OBJECT_TOOL,
    SET_ATTR_TOOL,
    SET_BODY_TOOL,
    SET_CONDITION_TOOL,
    UPDATE_OBJECT_TOOL,
    EntityTurn,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.agents.loop import LoopTool

__all__ = [
    "BOOTSTRAP",
    "BOOTSTRAP_ASK",
    "BOOTSTRAP_DIRECTIVE",
    "PLANNER_BOOTSTRAP_BUDGET",
    "PLANNER_BOOTSTRAP_TOOLS",
    "PLANNER_REPAIR_BUDGET",
    "PlannerBootstrapError",
    "REPAIR",
    "REPAIR_ASK",
    "REPAIR_DIRECTIVE",
    "REPAIR_TOOL_NAMES",
    "planner_bootstrap_tools",
    "planner_repair_tools",
    "run_planner_tool_phase",
]

#: The two phase modes. They STEER the model (the tail directive, the ask, the
#: tool set); they never discriminate — the plan row's absence does that (§4/D1).
BOOTSTRAP = "bootstrap"
REPAIR = "repair"

#: Write-class calls the bootstrap may spend (§3.4/D5). Places-first seeding needs
#: ~6-8 writes; the budget IS the turn-1 latency knob on the SERIALIZED narrator
#: server (§7), tuned at the live gate. W9.32's two extra seed steps (the purse,
#: a body) are OPTIONAL and spend from this same 8 — a scene that implies money
#: or clothing still has to fit the seed inside one budget, so neither the number
#: nor the settlement path moves.
PLANNER_BOOTSTRAP_BUDGET = 8

#: Write-class calls a repair may spend: rare, small, and it may not re-seed.
PLANNER_REPAIR_BUDGET = 4


class PlannerBootstrapError(RuntimeError):
    """The seed/repair phase produced nothing usable (contained by the node, §6).

    ``transport`` marks the one case that is NOT content-class: the loop was
    ended by a per-connection TIMEOUT, i.e. the provider never answered. The
    planner node routes that to the turn-abort path
    (:func:`fabulist.graph.provider_errors.phase_failure`) instead of containing
    it like a bad reply.
    """

    #: True when the phase failed because the TRANSPORT did not answer.
    transport: bool = False


#: The bootstrap protocol, verbatim-core from the plan §3.3 (req 2/5/6): places
#: first, the world is never a location, invention is allowed, and a duplicate is
#: answered with the EXISTING id — which is what makes a re-run self-correcting
#: (§4: the door refuses and names the id, so the model cites it for the parent
#: link instead of minting a second object). W9.32 §3.3/§5 added the two
#: lettered, skips-allowed seed steps (6a the purse, 6b a body): the planner SEEDS
#: those branches, so the checklist introduces them right after the places/cast
#: steps — lettered on purpose so the required steps keep their numbers, and the
#: worked example skips both to show what "nothing implies it" looks like.
BOOTSTRAP_DIRECTIVE = """SESSION BOOTSTRAP — seed the object stores BEFORE you write the plan. Work the checklist below exactly. The palette (appended after this text) lists everything that already exists; do not recreate anything in it.

1. get_roster, get_folded_state, and get_recent_deltas are free reads; use them if you are unsure of any name or id.
2. ONE tool call per message, always. Each call carries exactly ONE JSON object as its arguments. Never glue two objects together — no }{ inside one call. Three places means three separate calls.
3. The tool that CREATES a place is establish_location. move_being only relocates a being that already exists; it cannot create a place.
4. Places first, top-down. Create the top-level place(s), then their children rooms. You choose the top-level place(s); the world itself is NOT a location and must not be made one.
5. Parent id rule: for a child's parent_id, copy the id the LAST call returned. Never invent an id.
6. Then place beings with move_being, and seed clearly-stated threads and items with establish_thread and establish_item.
Then two OPTIONAL steps, lettered 6a and 6b. They are the only skippable steps in this checklist: when the scene says nothing for one, skip it and go on to step 7.
6a. MONEY (optional — skip it when nothing implies money). Call establish_economy {"denomination":"credits","balance":85} when the world or the scene involves money: a shop, a purse, a fare, a bribe, trade. denomination is the world's own word for money ("credits", "gold", "yen"); balance is the player's opening funds, 0 when no figure is stated. Call it at most once — it seeds the session's one purse.
6b. BODY (optional — skip it when the scene says nothing about a body). Call set_body {"host":"Seraphine","pose":"standing","clothing":["coat"],"surface":"a bruise on the jaw"} when the scenario states what someone is wearing, how they stand, or a visible injury, and give only the fields the scene actually states. One body per being.
7. A duplicate name is refused and the error names the existing id — reuse that id, never retry the same name.
8. Budget: 8 write actions, including anything 6a and 6b spend. Reads are free.
9. After your final tool result, stop calling tools and write one closing sentence.

EXAMPLE. Scene: an old inn, The Crimson Lantern, with a common room and stairs. One being: the player.

You call: establish_location {"name":"The Crimson Lantern","note":"A tall timber inn."} → loc_the_crimson_lantern
You call: establish_location {"name":"Common Room","note":"Trestle tables and a low fire.","parent_id":"loc_the_crimson_lantern"} → loc_common_room
You call: establish_location {"name":"Stair","note":"The main stairs leading up from the common room.","parent_id":"loc_common_room"} → loc_stair
You call: move_being {"who":"player","to":"loc_common_room"} → ok
The scene names no money and says nothing about clothing or a body, so 6a and 6b are both skipped.
You write: Stores seeded for turn 1."""

#: The repair protocol (§3.3/D4). The tool set already withholds ``establish_*``
#: and ``propose_time``; this says so, because a refused call costs a completion
#: out of four.
REPAIR_DIRECTIVE = """REPAIR MODE — the tracker has owned the object stores since turn 1; the establish_* tools are not available, so you cannot create anything new, and propose_time is not offered here either. Use ids exactly as they appear in the palette.

1. get_roster, get_folded_state, and get_recent_deltas are free reads; use them to confirm names and ids before writing.
2. ONE tool call per message. Each call carries exactly ONE JSON object — never glue two objects together.
3. Repair only what play has revealed that the registry now contradicts: update_object, move_being, set_attr, retire_object, rename_being, set_condition, set_body, adjust_economy, or flag_contradiction.
4. move_being only relocates a being. For absence, use to:"off_screen" with at.
5. Never invent an id — copy it from the palette.
6. Budget: 4 write actions. Reads are free.
7. After your final tool result, stop calling tools and write one closing sentence.

EXAMPLE. Play moved Lee from the Entrance Hall to the Kitchen:
You call: move_being {"who":"Lee","to":"loc_kitchen"} → ok
You write: Registry repaired for turn 5."""

#: The asks (the ``user`` turn of the phase). The mode rides both the ask and the
#: tail: the ask says WHAT this pass is for, the tail carries the protocol.
BOOTSTRAP_ASK = """Bootstrap the object stores for turn {turn}. Follow the checklist exactly: top-level places first, then their rooms using the parent id the last call returned, then the beings. One call per message."""

REPAIR_ASK = """Repair the object stores for turn {turn}. Follow the checklist: do not establish anything new; fix only what play has revealed that the registry contradicts, using ids from the palette. One call per message."""

#: What a BOOTSTRAP run may call (§3.1, W9.32 §3.3): the three free reads plus
#: every write tool the tracker has, minus ONE — ``propose_time``. The bootstrap
#: seeds the stores before any scene exists, so it has no prose of this session to
#: cite as a clock move's evidence, and :data:`BOOTSTRAP_DIRECTIVE` gives it no
#: step to spend one on: a time proposal here could only be an unevidenced guess
#: that the validator would refuse, costing a completion out of eight. Time at
#: seed time comes from the plan's own ``datetime`` (``_seed_plan_state``), which
#: is the planner's seeding channel, not the tracker's proposal channel (§4).
#:
#: Listed in :func:`fabulist.state.bts.bts_loop_tools` order (the reads first,
#: then the writes in :data:`fabulist.state.entity_tools.WRITE_TOOL_NAMES`
#: order) — the same shape as :data:`REPAIR_TOOL_NAMES` below, so the two phases
#: read as one surface with two scopes. ``establish_economy`` IS here: the planner
#: is the purse's only seeder (§5).
PLANNER_BOOTSTRAP_TOOLS = (
    FOLDED_STATE_TOOL,
    ROSTER_TOOL,
    RECENT_DELTAS_TOOL,
    ESTABLISH_LOCATION_TOOL,
    ESTABLISH_THREAD_TOOL,
    ESTABLISH_RELATIONSHIP_TOOL,
    ESTABLISH_ITEM_TOOL,
    MOVE_BEING_TOOL,
    SET_ATTR_TOOL,
    UPDATE_OBJECT_TOOL,
    RETIRE_OBJECT_TOOL,
    RENAME_BEING_TOOL,
    FLAG_CONTRADICTION_TOOL,
    SET_CONDITION_TOOL,
    SET_BODY_TOOL,
    ESTABLISH_ECONOMY_TOOL,
    ADJUST_ECONOMY_TOOL,
)

#: What a REPAIR run may call (§3.1/D4): the three free reads plus the nine
#: mutating tools. Never-seed is enforced by this tuple, and it has three named
#: exclusions beyond the four ``establish_*`` verbs:
#:
#: * ``establish_economy`` — an establish that is not spelled ``establish_*``; the
#:   purse is the PLANNER's own singleton (the store refuses a second one while it
#:   is live), so a refresh may move it with ``adjust_economy`` and never seed it.
#: * ``propose_time`` — clock proposals are the TRACKER's post-turn channel,
#:   evidenced by the prose it has just read; a pre-plan refresh holds no prose of
#:   this turn to cite (W9.32 §3.3/§5 — time is not the planner's branch). It is
#:   absent from BOTH phases: :data:`PLANNER_BOOTSTRAP_TOOLS` above carries the
#:   same exclusion for the same reason (and the bootstrap has no prose at all).
#: * ``reconcile`` — the tracker's own cadence pass (W9.29 §12); the planner is
#:   not the reconciliation surface.
#:
#: ``set_condition``/``set_body``/``adjust_economy`` ARE here: all three
#: mint-or-patch, the ``set_attr`` precedent, so a repair corrects what play
#: revealed without creating a new kind of anything. Listed in
#: :func:`bts_loop_tools` order, which is the order the filter yields.
REPAIR_TOOL_NAMES = (
    FOLDED_STATE_TOOL,
    ROSTER_TOOL,
    RECENT_DELTAS_TOOL,
    MOVE_BEING_TOOL,
    SET_ATTR_TOOL,
    UPDATE_OBJECT_TOOL,
    RETIRE_OBJECT_TOOL,
    RENAME_BEING_TOOL,
    FLAG_CONTRADICTION_TOOL,
    SET_CONDITION_TOOL,
    SET_BODY_TOOL,
    ADJUST_ECONOMY_TOOL,
)


def planner_bootstrap_tools(
    log: Any, session_id: str, turn: Any
) -> list["LoopTool"]:
    """The bootstrap phase's tool set: :func:`bts_loop_tools` filtered by name.

    The :func:`planner_repair_tools` precedent — a name-filtered subset, so the
    tool ``fn`` s stay the single source of truth in
    :mod:`fabulist.state.entity_tools` (no re-declaration can drift from the
    tracker's own door). The one name it drops is ``propose_time``
    (:data:`PLANNER_BOOTSTRAP_TOOLS` carries the reasoning).
    """
    return [
        tool
        for tool in bts_loop_tools(log, session_id, turn)
        if tool.name in PLANNER_BOOTSTRAP_TOOLS
    ]


def planner_repair_tools(
    log: Any, session_id: str, turn: Any
) -> list["LoopTool"]:
    """The repair phase's tool set: :func:`bts_loop_tools` filtered by name.

    The :func:`fabulist.state.bts.reconcile_loop_tools` precedent — a
    name-filtered subset, so the tool ``fn`` s stay the single source of truth in
    :mod:`fabulist.state.entity_tools` (no re-declaration can drift from the
    tracker's own door).
    """
    return [
        tool
        for tool in bts_loop_tools(log, session_id, turn)
        if tool.name in REPAIR_TOOL_NAMES
    ]


async def run_planner_tool_phase(
    *,
    caller: Any,
    log: Any,
    session_id: str,
    directive: str,
    context: str,
    turn_seq: int,
    mode: str,
    profile: dict | None,
    return_thinking: bool | None = None,
    timeout_s: float | None = None,
) -> None:
    """Run the ONE bootstrap (or repair) tool phase; discard its closing summary.

    ``ensure_roster`` runs FIRST (D9a). Being resolution is a PURE fold over
    :func:`fabulist.state.cast.roster`, and on a fresh chat nothing has minted
    the card's seed admits yet: the only other producer is
    :func:`fabulist.graph.nodes.make_npc_lanes_node`, which runs AFTER the
    planner (``pre_turn -> planner -> npc_lanes``). Without this call every
    ``move_being``/meter/relationship naming a card character is refused AND a
    spurious ``cast`` proposal is filed for a person the card already names,
    polluting the review home on turn 1. It is idempotent (once the chain carries
    any ``cast_admit`` it is a pure read) and both modes need it.

    ``profile`` is the planner's resolved PLAN transport profile: it is accepted
    so the phase and the node's gate read the same binding, and it is deliberately
    NOT forwarded to the loop — :func:`fabulist.agents.loop.loop_request_profile`
    builds the loop's own profile from ``capabilities`` and the entity tools (the
    plan tool's declaration would be the wrong wire here). A ``None`` profile means
    the connection cannot call functions, the tool loop's precondition (D6).

    ``timeout_s`` is the WALL-CLOCK bound every call in the loop rides, i.e. the
    connection's ``call_timeout_s`` (issue #25), and it is handed to the loop as
    PROVENANCE so its ``agent_loop`` rows report the bound instead of hiding it.
    The ENFORCEMENT is the transport's: ``RegistryModelCaller`` bounds each call
    from send to last frame and raises ``ModelCallDeadlineExceeded`` when a
    stream outlives it (a per-READ timeout cannot do that — every delta resets
    it). The loop re-raises it untouched (it is not a read timeout), the node
    routes it to the turn-abort path, and the reader meets
    "Planning (seed) failed: ModelCallDeadlineExceeded from <model> — …".
    ``window_fn`` is omitted (``None``): the phase has no declared context window
    of its own. ``return_thinking`` is the connection's reasoning-echo setting
    (``None`` = AUTO), so the phase's tool loop repeats a thinking model's
    ``reasoning_content`` back when the provider returned some.

    Raises :class:`PlannerBootstrapError` on total output failure (a timed-out
    loop, or no accepted action with an empty closing summary). Actions already
    appended STAND (each was validated and appended independently — D9
    append-only, no rollback); the node contains the raise and still plans (§6).
    """
    from fabulist.agents.loop import run_agent_loop

    from .cast import ensure_roster

    if profile is None:
        raise PlannerBootstrapError(
            "planner tool phase unavailable: this connection does not support "
            "function calling (W9.31 D6)"
        )
    if mode not in (BOOTSTRAP, REPAIR):
        raise PlannerBootstrapError(f"unknown planner tool phase mode: {mode!r}")

    if timeout_s is None:
        # The caller owns the transport, so it is the one thing here that can name
        # the bound the loop rides (``RegistryModelCaller`` -> the connection's
        # ``call_timeout_s``). An undeclared/unreadable bound stays ``None`` — a
        # missing number is never invented.
        reader = getattr(caller, "call_timeout_s", None)
        if callable(reader):
            try:
                timeout_s = reader()
            except Exception:  # noqa: BLE001 - provenance is never worth a raise
                timeout_s = None

    bootstrap = mode == BOOTSTRAP
    ensure_roster(log, session_id)
    turn = EntityTurn(
        log,
        session_id,
        budget=PLANNER_BOOTSTRAP_BUDGET if bootstrap else PLANNER_REPAIR_BUDGET,
        # The planner IS the seeder/repairer of the object stores (§5's ownership
        # map), so every row its phases append says so: the ledger — the thing
        # that knows who is driving the loop — decides the actor, never the tool
        # (the tracker's own ledger keeps ``bts``).
        actor="planner",
    )

    system = "\n\n".join(
        part for part in (directive.strip(), context.strip()) if part
    )
    ask = (BOOTSTRAP_ASK if bootstrap else REPAIR_ASK).format(turn=turn_seq)
    tail = "{}\n\n{}".format(
        BOOTSTRAP_DIRECTIVE if bootstrap else REPAIR_DIRECTIVE,
        registry_palette(log, session_id),
    )
    # The protocol + palette ride the TAIL (AGENTS 6, the
    # ``BtsRunner._action_call`` position): the seeded prefix stays stable and
    # nothing the model must obey lives where a preset edit could drop it.
    #
    # Issue #67: the tail is the NARRATOR's carrier, not a third system message —
    # ONE ephemeral ``assistant`` message inserted immediately BEFORE the final
    # user message (``prompts.envelope.ENVELOPE_ROLE`` / ``insert_engine_envelope``).
    # A trailing system message is a hard 500 on a stock Jinja template ("System
    # message must be at the beginning"), which killed the turn in the seed phase;
    # folding it into the leading prefix is equally wrong (the protocol must not
    # sit where a preset edit could drop it), and the player's message space is
    # never touched. So: the seed shape is [system, assistant(tail), user(ask)].
    seeded = [
        {"role": "system", "content": system},
        {"role": "user", "content": ask},
    ]
    seeded, _def_end, _tail_at = insert_engine_envelope(seeded, len(seeded), tail)
    # The two provenance rails, reusing the envelope module's own guards: the
    # tail is engine-authored, so it may live in neither the player's message
    # space nor a post-prefix system message (both RAISE, naming the index).
    assert_envelope_not_in_user_scope(seeded, tail)
    assert_envelope_not_in_system_scope(seeded, tail)
    tools = (
        planner_bootstrap_tools(log, session_id, turn)
        if bootstrap
        else planner_repair_tools(log, session_id, turn)
    )
    result = await run_agent_loop(
        caller=caller,
        messages=seeded,
        tools=tools,
        # The loop's trace label is the PHASE's, not the node's: ``agent_loop``
        # rows say which pass wrote the objects (the surface is observability
        # only — ``CURRENT_INTERACTION`` governs usage rows, D8/§3.5).
        surface=f"planner_{mode}",
        log=log,
        session_id=session_id,
        capabilities={"function_calling": True},
        return_thinking=return_thinking,
        timeout_s=timeout_s,
    )
    if result.timed_out:
        error = PlannerBootstrapError(
            "planner tool phase unusable: the model call timed out "
            "(per-connection transport bound; no repair retry follows)"
        )
        # A timeout is the transport not answering, not a content failure: the
        # plan below would hit the same dead connection, so the node aborts.
        error.transport = True
        raise error
    if turn.actions == 0 and not result.text.strip():
        raise PlannerBootstrapError(
            "planner tool phase unusable: no action was recorded and the model "
            "wrote no closing summary"
        )
