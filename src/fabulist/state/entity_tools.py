"""The BTS write-action tool surface (W9.29 §6): one validated call per change.

Requirement 9 makes the tracker's protocol a **tool loop**: the free-form ``[BTS]``
notation is retired as a write path, and every change to the world is one tool
call the engine validates, appends and answers. This module is that door — fifteen
thin ``LoopTool`` wrappers over the ONE writer module (:mod:`fabulist.state.entities`),
:mod:`fabulist.state.entity_catalog` (the range check), the existing cast rename
door (:func:`fabulist.state.cast.patch_member`) and the D79 proposal store — plus
the eleventh, :data:`RECONCILE_TOOL`, which the loop carries ONLY on a
reconciliation turn (§12: the model states what it believes, the engine files one
flag per disagreement, the registry is never touched).

Why a tool loop (and not a payload)
-----------------------------------
A payload is written once and can only be *rejected as a whole*; a call can be
refused with a **readable, corrective message** the model reads back and fixes
in-loop (the ``patch_prose`` steering precedent). So every tool answers a dict:
``{"ok": True, ...ids/prior}`` on success, ``{"ok": False, "error": "<text>"}`` on
rejection — and a rejection NEVER raises (a raise would end the run, which is
exactly what the model must be able to survive). The error texts are the plan
§6 list, verbatim where they exist: the unknown catalog key, the range, the
unknown object, the ambiguous name, the occupied location and the budget.

The budget (decision point §14.1)
---------------------------------
:data:`ENTITY_ACTION_BUDGET` write-class calls per turn. EVERY non-read call
charges it — including a rejected call, ``flag_contradiction`` and the
reconciliation pass — because the budget is a cost knob on the extractor
connection (each call is one completion), not a ration of story. Once spent,
every further write answers :data:`ENTITY_BUDGET_ERROR` and appends nothing; the
model's closing summary is then the turn's end.

The W9.32 doors (session tree, §3.3)
------------------------------------
Five more tools make the world addressable beyond kinds-and-meters: a CONDITION
(:func:`set_condition` — with a clock-time expiry the engine's sweep retires), a
BODY per being (:func:`set_body` — pose, clothing layers, visible surface), the
session's one ECONOMY (:func:`establish_economy` seeds it, :func:`adjust_economy`
moves it), and a CLOCK PROPOSAL (:func:`propose_time` — the tracker may argue with
the rules table; the proposal is validated here and audited as one
``system``/``clock_proposal`` row, never a time storage path). The first four are
user-writable as well (:data:`USER_WRITABLE_TOOLS`, the ✎ door's scope);
``propose_time`` is deliberately not — the owner's clock edit stays the flat
``POST /state {key:"time"}`` door (D45).

The reconciliation pass (§12, decision point §14.7 — INCLUDE)
-------------------------------------------------------------
:data:`RECONCILE_TOOL` exists on a cadence (:data:`fabulist.state.bts.
RECONCILE_CADENCE`, the retired checkpoint's slot), never per turn: the model
dumps everything it believes (who is where, which meter holds what value, which
thread is in what status) and :func:`reconcile_drift` diffs that against the
folded registry. Every disagreement becomes ONE ``system``/``validation_notice``
row (``check: "reconciliation_drift"``, the §10 shape) anchored on the turn's
narrative. FLAGS ONLY: the pass never writes an object (req. 12), so the
corrective action stays the tracker's next normal turn or the owner's door.

Identity at the being door (§6, req. 4, review R1)
--------------------------------------------------
``who``/``host`` accept a roster ``member_id`` **or** a name, resolved ROSTER-FIRST
(W9.32 R3): an explicit id, then the reserved literals, then the roster's own
names, and only then the player keys — so a real cast member who happens to share
the persona's name resolves to the member instead of being shadowed by the player
(:func:`_lookup_being` carries the full order and the User-as-Actor remap). An
identity nobody has accepted is filed through the EXISTING user-gated cast door
(:func:`fabulist.state.proposals.emit_cast_proposal`) and the call is refused
with the text that says so — **no assignment is written for a person who is not
a member**, because a being IS a ``member_id`` (§3). ``player`` (and the
persona's name, and the ``PLAYER_SURFACES`` words) resolve to the reserved player
key; ``scene`` is the scene host. ``rename_being`` routes through
:func:`fabulist.state.cast.patch_member`, which keeps the ``member_id`` (one
colour, one attribution history) and REFUSES a name another member already
carries — never a merge.

Locations are id-or-name (:func:`fabulist.state.entities.resolve_location`): an
ambiguous name is an ERROR listing both ids, never a silent pick (req. 4 — the
palette is the conflation killer, and a guess would defeat it).

Hygiene: nothing here imports the API, the graph layer is reached only lazily
(:func:`_clock`, for the pure instant helpers that have no state-layer twin), and
every append rides a writer (D9 — the model never writes the log).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Sequence

from . import entity_catalog, entities
from .cast import canonical_key
from .entities import EntityError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.agents.loop import LoopTool

logger = logging.getLogger(__name__)

__all__ = [
    "ADJUST_ECONOMY_TOOL",
    "BELIEF_KINDS",
    "ENTITY_ACTION_BUDGET",
    "ENTITY_BUDGET_ERROR",
    "ESTABLISH_ECONOMY_TOOL",
    "ESTABLISH_ITEM_TOOL",
    "ESTABLISH_LOCATION_TOOL",
    "ESTABLISH_RELATIONSHIP_TOOL",
    "ESTABLISH_THREAD_TOOL",
    "EntityTurn",
    "FLAG_CONTRADICTION_TOOL",
    "MOVE_BEING_TOOL",
    "PROPOSE_TIME_TOOL",
    "RECONCILE_TOOL",
    "RECONCILIATION_DRIFT_CHECK",
    "RENAME_BEING_TOOL",
    "RETIRE_OBJECT_TOOL",
    "SET_ATTR_TOOL",
    "SET_BODY_TOOL",
    "SET_CONDITION_TOOL",
    "UPDATE_OBJECT_TOOL",
    "USER_WRITABLE_TOOLS",
    "WRITE_TOOL_NAMES",
    "budget_error",
    "entity_action_tools",
    "mentioned_names",
    "reconcile_drift",
]

#: Write-class calls allowed per turn (plan §6 / decision point §14.1). The
#: budget counts EVERY non-read call — a rejection and a contradiction flag
#: included — and it is the cost knob on the extractor connection.
#: A surface shaped like an engine-minted member id (``cm_`` + hex) is an
#: INFERENCE, never a name the tracker saw. Refused at the cast-proposal door.
_ID_SHAPE = re.compile(r"^[a-z]{2,4}_[a-z0-9_]+$", re.IGNORECASE)

ENTITY_ACTION_BUDGET = 10


def budget_error(budget: int) -> str:
    """What a spent budget answers (plan §6, verbatim at the default 10).

    ONE format for every ledger: a phase that passes its own budget
    (:class:`EntityTurn`'s ``budget`` argument — the planner's bootstrap/repair,
    W9.31 §3.4) must not be told "10" by a constant that hardcoded it.
    """
    return (
        f"action budget ({budget}) spent this turn — stop calling "
        "tools and reply with your closing summary"
    )


#: What a spent budget answers at the default budget (plan §6, verbatim).
ENTITY_BUDGET_ERROR = budget_error(ENTITY_ACTION_BUDGET)

#: Attempts a ledger may spend BEYOND its write budget (owner ruling 2026-10-21:
#: "we need to allow it to make mistakes — that's the nature of agentic"). A
#: REFUSED write (validation, duplicate name, malformed args) costs an ATTEMPT
#: but never a write, so a model that errs twice still lands its full budget of
#: productive writes. The attempt cap is what bounds a model that loops on the
#: same mistake: ``budget + ENTITY_ATTEMPT_SLACK`` invocations, then the ledger
#: answers :func:`attempt_error`.
ENTITY_ATTEMPT_SLACK = 4


def attempt_error(attempts: int) -> str:
    """What an exhausted attempt allowance answers (a mistake bound, not a budget)."""
    return (
        f"action attempts used up ({attempts}) — this pass is done; check the "
        "palette and close with one sentence"
    )

# -- the wire names (§6's table; the tool name IS the action) -----------------

ESTABLISH_LOCATION_TOOL = "establish_location"
ESTABLISH_THREAD_TOOL = "establish_thread"
ESTABLISH_RELATIONSHIP_TOOL = "establish_relationship"
ESTABLISH_ITEM_TOOL = "establish_item"
MOVE_BEING_TOOL = "move_being"
SET_ATTR_TOOL = "set_attr"
UPDATE_OBJECT_TOOL = "update_object"
RETIRE_OBJECT_TOOL = "retire_object"
RENAME_BEING_TOOL = "rename_being"
FLAG_CONTRADICTION_TOOL = "flag_contradiction"

# -- the W9.32 §3.3 doors (conditions, bodies, economy, the clock proposal) ----

SET_CONDITION_TOOL = "set_condition"
SET_BODY_TOOL = "set_body"
ESTABLISH_ECONOMY_TOOL = "establish_economy"
ADJUST_ECONOMY_TOOL = "adjust_economy"
PROPOSE_TIME_TOOL = "propose_time"

#: The reconciliation pass's ONE tool (W9.29 §12): the model dumps what it
#: BELIEVES and the engine diffs it against the registry. Offered only on a
#: reconciliation turn (:data:`fabulist.state.bts.RECONCILE_CADENCE`), it never
#: writes the registry — every disagreement becomes one ``validation_notice`` row.
RECONCILE_TOOL = "reconcile"

#: The belief kinds ``reconcile`` accepts (§12 names exactly these three: who is
#: where, meter values, thread statuses). Anything else is an unreadable belief —
#: a readable error, never a silent skip: a pass that quietly ignored half its
#: input would report "no drift" for a registry it never compared.
BELIEF_KINDS = ("location", "meter", "thread")

#: The ``check`` value on a reconciliation flag (plan §10/§12: the same
#: validation-notice shape the contradiction flag uses, its own check id).
RECONCILIATION_DRIFT_CHECK = "reconciliation_drift"

#: Every ALWAYS-OFFERED write-class call (§6) — the budget's unit. Reads are free
#: and live in :func:`fabulist.state.bts.bts_loop_tools` (``get_roster`` /
#: ``get_folded_state`` / ``get_recent_deltas``). :data:`RECONCILE_TOOL` is a
#: write-class call by the same rule (it appends notice rows), even though it never
#: touches the registry; it is offered on reconciliation turns only, so it is
#: deliberately NOT in this tuple, which the user write door
#: (``POST /api/sessions/{id}/entities``) also scopes to.
WRITE_TOOL_NAMES = (
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
    # W9.32 §3.3 — the session-tree doors, appended so the original ten keep
    # their order (the served tool list is pinned against this tuple).
    SET_CONDITION_TOOL,
    SET_BODY_TOOL,
    ESTABLISH_ECONOMY_TOOL,
    ADJUST_ECONOMY_TOOL,
    PROPOSE_TIME_TOOL,
)

#: The subset of :data:`WRITE_TOOL_NAMES` the USER's ✎ door
#: (``POST /api/sessions/{id}/entities``) may run — everything but the clock
#: proposal. Time is the ENGINE's channel (D3, reopened only as far as the
#: tracker's validated proposals, W9.32 §4): a sidebar edit must not argue with
#: the clock table, and the owner's manual override is its own flat door
#: (``POST /state {key:"time"}``, D45). Stays a TUPLE (a filter, not a set
#: difference) so the door's "unknown op" listing keeps the declared order.
USER_WRITABLE_TOOLS = tuple(
    name for name in WRITE_TOOL_NAMES if name != PROPOSE_TIME_TOOL
)


class EntityTurn:
    """One turn's action ledger: the budget, the counts, the mentioned names.

    The runner creates ONE of these per turn and every write tool charges it, so
    the budget and the observability row (:func:`entities.append_entity_summary`,
    appended by the node) describe the same calls. It is also where the turn's
    **mentioned-name set** accumulates: the people the loop's actions dealt with,
    which the node hands to the D79 cast door
    (:func:`fabulist.state.bts.apply_bts_cast_proposals`) so the identity path
    keeps working now that ``references[]`` is gone from the write surface (§6,
    review R1).

    ``narrative_seq`` is resolved lazily once per turn — the seq every writer
    stamps on its event for transcript anchoring (§5), and the anchor a
    contradiction flag needs (a notice with no narrative cannot be placed).

    ``reconcile`` says this turn is a RECONCILIATION turn (§12, decided by the
    runner's cadence at request time): only then does the loop carry
    :data:`RECONCILE_TOOL`, and only then does the tail directive ask for it.

    ``budget`` is the write-class allowance this ledger enforces
    (:data:`ENTITY_ACTION_BUDGET` for the tracker; the planner's phases pass
    their own, W9.31 §3.4). ``spend`` answers the refusal text for the REAL
    budget, so the model is never told a number the ledger does not use.

    ``clock`` is THIS turn's ``(time, date)`` — time first — threaded by the
    runner from the clock node's facts (W9.32 §3.5). The tools that judge an
    instant (:func:`propose_time`, :func:`set_condition`) validate against it,
    NEVER against the projection they can read: mid-loop that fold still holds
    the PREVIOUS turn's time, so a stale baseline would accept a proposal that
    runs backwards. A ledger built WITHOUT it — the user ✎ door
    (``POST …/entities``) and the planner bootstrap, both of which act outside
    the live loop — validates against the folded projection, where that IS the
    current clock, and ``propose_time`` says so in its refusal.
    ``time_proposal`` records the proposal this turn ACCEPTED
    (``time``/``date``/``day``/``advanced_minutes``/``evidence``) so the BTS node
    can fold it over the clock keys (§3.5); it stays ``None`` otherwise.

    ``actor`` is WHO DRIVES this loop — the actor every row it causes is stamped
    with (:data:`fabulist.state.entities.DEFAULT_ACTOR`, ``bts``, for the
    tracker). The LEDGER knows which pass it belongs to; a tool does not, so no
    tool chooses an actor: each write hands ``actor=turn.actor`` to its store
    writer. That is what keeps the audit trail able to tell the tracker's turns
    from the planner's seed/repair phases (``planner`` — W9.32 §5's ownership map
    names the planner the ONLY seeder, and rows that lie about their author make
    the claim unverifiable). The user ✎ door
    (:func:`fabulist.api.state_api.edit_entity`) keeps this default and re-stamps
    ``user`` in its log wrapper instead.
    """

    def __init__(
        self,
        log: Any,
        session_id: str,
        *,
        reconcile: bool = False,
        budget: int = ENTITY_ACTION_BUDGET,
        max_attempts: int | None = None,
        clock: tuple[str, str] | None = None,
        time_proposal: dict[str, Any] | None = None,
        actor: str = entities.DEFAULT_ACTOR,
    ) -> None:
        self.log = log
        self.session_id = session_id
        #: Is this turn's reconciliation pass due? (Offered-tool switch, §12.)
        self.reconcile = reconcile
        #: Write-class calls this ledger allows.
        self.budget = budget
        #: Tool INVOCATIONS allowed, refusals included. Default: the write budget
        #: plus :data:`ENTITY_ATTEMPT_SLACK` — mistakes are free, loops are not.
        self.max_attempts = (
            max_attempts if max_attempts is not None else budget + ENTITY_ATTEMPT_SLACK
        )
        #: Write-class calls attempted (successes and refusals alike).
        self.attempts = 0
        #: Write-class calls that were accepted (the turn's real changes).
        self.actions = 0
        #: Write-class calls that were refused (budget refusals included).
        self.rejected = 0
        #: Contradiction flags filed (a subset of ``actions``).
        self.flags = 0
        #: Write-class calls charged against the budget (accepted or not).
        self.writes = 0
        #: Being names the turn's actions used, first-appearance order.
        self.mentions: list[str] = []
        #: Beliefs the reconciliation pass stated (the manual door's "checked N").
        self.beliefs = 0
        #: The drift rows the pass filed, in belief order (the flags themselves
        #: are already in the log; this is the same list the tool returned).
        self.drift: list[dict[str, Any]] = []
        #: This turn's ``(time, date)`` from the clock node's facts (``None`` =
        #: no live clock: validate against the folded projection).
        self.clock = clock
        #: The clock proposal this turn accepted, or ``None`` (see the class docs).
        self.time_proposal = time_proposal
        #: Who drives this loop — the actor every row it appends is stamped with
        #: (``bts`` for the tracker, ``planner`` for the bootstrap/repair phases).
        self.actor = actor
        self._narrative_seq: int | None = None
        self._seq_read = False

    # -- accounting ---------------------------------------------------------

    def spend(self) -> str | None:
        """Charge one ATTEMPT; refuse only when a ceiling is reached.

        The write budget counts SUCCESSES (``settle`` below), so a refused call
        never eats the allowance the owner granted for productive work — that is
        the "allow it to make mistakes" ruling. The attempt ceiling
        (``max_attempts``) is the loop bound that replaces the old
        charged-before-you-try behavior.
        """
        if self.writes >= self.budget:
            self.rejected += 1
            return budget_error(self.budget)
        if self.attempts >= self.max_attempts:
            self.rejected += 1
            return attempt_error(self.max_attempts)
        self.attempts += 1
        return None

    def settle(self, ok: bool) -> None:
        """Settle one completed attempt: only SUCCESS spends a write."""
        if ok:
            self.writes += 1

    def mention(self, *surfaces: object) -> None:
        """Record being names the turn used (the cast door's input this turn)."""
        for surface in surfaces:
            text = str(surface or "").strip()
            if text and text not in self.mentions:
                self.mentions.append(text)

    # -- transcript anchoring ----------------------------------------------

    def narrative_seq(self) -> int | None:
        """The newest narrative's seq in the fork-aware chain (cached per turn).

        BTS runs POST-turn, so the newest narrative IS the turn the actions
        belong to. ``None`` when the chain holds no narrative at all — a flag then
        cannot be placed, and the tool says so instead of appending a row the
        projection would drop.
        """
        if self._seq_read:
            return self._narrative_seq
        self._seq_read = True
        latest = 0
        for event in _chain_events(self.log, self.session_id):
            if event.type == "narrative" and not getattr(event, "ephemeral", False):
                latest = max(latest, event.seq)
        self._narrative_seq = latest or None
        return self._narrative_seq


def _chain_events(log: Any, session_id: str) -> list[Any]:
    """Fork-aware chain (the same walk :mod:`fabulist.state.entities` folds)."""
    get_chain = getattr(log, "get_chain_events", None)
    if callable(get_chain):
        return list(get_chain(session_id))
    return list(log.get_events(session_id))


# -- identity at the being door ---------------------------------------------


def _player_keys(log: Any, session_id: str) -> frozenset[str]:
    """Canonical keys that name the PLAYER (never a cast member to move/set).

    :mod:`fabulist.state.presence` owns the surface list and the persona read;
    the door reuses both rather than re-deriving "who is the player".
    """
    from .presence import PLAYER_SURFACES, _player_name

    keys = {str(surface) for surface in PLAYER_SURFACES}
    name = _player_name(log, session_id)
    if name:
        key = canonical_key(name)
        if key:
            keys.add(key)
    return frozenset(keys)


def _emit_cast_proposal(
    log: Any, session_id: str, name: str, aliases: Sequence[str] = ()
) -> bool:
    """File ONE ``cast`` proposal for a person nobody has accepted (§6, D79).

    Best-effort like every door in :mod:`fabulist.state.bts`: a refused filing
    is not the turn's problem, and the caller's error text then points the model
    at ``get_roster`` instead of promising a review entry.
    """
    # A surface shaped like a member id is never a display name (live gate: the
    # model inferred "cm_kell" from get_roster's id pattern and the door filed it
    # as the candidate's NAME). The caller gets False so its error text can tell
    # the model to pass the plain name.
    if _ID_SHAPE.match(str(name or "").strip()):
        logger.warning("refusing an id-shaped cast surface: %r", name)
        return False
    try:
        from .proposals import emit_cast_proposal
    except ImportError:  # pragma: no cover - the L4 lane owns this symbol
        logger.warning("cast proposals are unavailable: %r was not filed", name)
        return False
    try:
        emit_cast_proposal(log, session_id, name, list(aliases), "bts")
    except Exception:  # noqa: BLE001 - a refused filing is not the turn's problem
        logger.warning("could not file a cast proposal for %r", name, exc_info=True)
        return False
    return True


def _being_key(member: Any) -> str:
    """The LIVE-STATE key for a resolved roster member (the §5 User-as-Actor remap).

    A member whose ``provenance`` is ``user`` IS the player's participation layer
    (W9.22 admits exactly one, named for the persona on purpose). Live state keys
    on ``player`` for that member or one person would hold two registry keys with
    two independent assignments — and presence answers "in scene" by comparing
    ``assignment[who] == assignment[player]``, so the two keys would drift apart.
    """
    if getattr(member, "provenance", "") == "user":
        return entities.PLAYER
    return member.member_id


def _lookup_being(log: Any, session_id: str, who: object) -> tuple[str | None, str]:
    """``(being key, "")`` for a member id / name / the player, else ``(None, text)``.

    Resolution order (W9.32 R3, the roster-first fix — every step is a pin):

    1. ``member_by_id`` — an explicit roster id always wins.
    2. the exact reserved literals: ``player`` -> :data:`entities.PLAYER` and
       ``scene`` -> :data:`entities.SCENE`. The PLAYER literal is matched on its
       DECLARED spelling, not on its canonical key, because step 3 must still
       reach a cast member who is literally named "Player" (``who:"Player"`` ->
       that member, ``who:"player"`` -> the player); every other spelling of the
       player — "PLAYER", "User", the persona's name — is caught by step 4.
       ``scene`` is matched canonically: no being owns that name and
       ``set_condition{host:"scene"}`` must reach the scene host.
    3. ``member_by_name`` — the exact display name, then the alias index.
    4. the remaining player keys (:func:`_player_keys`: the persona's display
       name plus :data:`fabulist.state.presence.PLAYER_SURFACES`) -> the player.

    Steps 3 and 4 IN THAT ORDER are the fix: a member the roster really holds
    beats the persona guess, so a cast member named like the persona is no longer
    shadowed by the player (the W9.32 bug) — while a chat whose roster holds no
    such member still resolves the persona's name to the player.

    **User-as-Actor:** a member whose provenance is ``user`` maps to
    :data:`entities.PLAYER` (steps 1 and 3, via :func:`_being_key`) — that member
    is the player's participation layer, so live state never splits across two
    keys.

    No side effects: the caller decides whether an unknown surface is a proposal
    (move/set/relationship — the person the tracker just met) or a rename target
    (where the proposal names the NEW spelling and the surface becomes its alias).
    """
    text = str(who or "").strip()
    if not text:
        return None, "a being is required — a member id, a name, or 'player'"
    from .cast import member_by_id, member_by_name, roster

    members = roster(log, session_id)
    member = member_by_id(members, text)
    if member is not None:
        return _being_key(member), ""
    if text == entities.PLAYER:
        return entities.PLAYER, ""
    if canonical_key(text) == canonical_key(entities.SCENE):
        # The scene is a legal meter/condition HOST (§3.4) and never a person.
        return entities.SCENE, ""
    member = member_by_name(members, text)
    if member is not None:
        return _being_key(member), ""
    if canonical_key(text) in _player_keys(log, session_id):
        return entities.PLAYER, ""
    return (
        None,
        f"'{text}' is not a cast member — cite a roster id or name, or "
        "who:'player' for the user's persona",
    )


def _resolve_being(
    log: Any, session_id: str, who: object, *, consequence: str
) -> tuple[str | None, str]:
    """Resolve a being; an unknown identity is PROPOSED, then refused (§6).

    ``consequence`` finishes the refusal for the caller ("not movable until
    accepted", "no attribute set until accepted", ...). The proposal is the D79
    user-gated door — the owner's review home carries the person the tracker met,
    and NO assignment is written for them.
    """
    key, refusal = _lookup_being(log, session_id, who)
    if key is not None:
        return key, ""
    text = str(who or "").strip()
    if not text:
        return None, refusal
    if _emit_cast_proposal(log, session_id, text):
        return (
            None,
            f"'{text}' is not a cast member yet — proposed for owner review; {consequence}",
        )
    return (
        None,
        f"'{text}' is not a cast member — call get_roster for the ids you may use",
    )


def _holder_key(log: Any, session_id: str, surface: str | None) -> str | None:
    """A holder surface normalised to a being KEY where the roster can resolve it.

    A holder is a being key EVERYWHERE else in the store — ``flat_render`` compares
    ``item.holder`` against :data:`entities.PLAYER` and the tree's ``live.items``
    compares it against the member id — so a display name written here is a holder
    no key-based read matches. This is the same door ``establish_item`` walks
    (:func:`_lookup_being`, the pure resolution half of :func:`_resolve_being`);
    the store stays PERMISSIVE, so an unresolvable surface passes through
    unchanged as it always did — the door only normalises what it can resolve.

    :func:`_lookup_being` rather than :func:`_resolve_being` on purpose: an item
    patch that names a holder must not file a cast proposal for a surface that is
    not a person, and must not turn a permissive patch into a refusal.
    """
    if surface is None:
        return None
    key, _refusal = _lookup_being(log, session_id, surface)
    return key or surface


def _mention_being(turn: EntityTurn, log: Any, session_id: str, key: str) -> None:
    """Record the display name behind a resolved being key (the cast door's input)."""
    if key in (entities.PLAYER, entities.SCENE):
        return
    from .cast import member_by_id, roster

    member = member_by_id(roster(log, session_id), key)
    turn.mention(member.display_name if member is not None else key)


# -- the turn's clock (W9.32 §3.5) -------------------------------------------


def _clock() -> Any:
    """The pure instant helpers of :mod:`fabulist.graph.clock`, imported LAZILY.

    The clock module itself is pure (no model, no I/O), but it lives in the graph
    package, whose ``__init__`` imports the turn engine — and the engine's nodes
    import THIS state layer. A module-level import would close that cycle, so the
    two tools that judge an instant take the helpers at call time.
    """
    from fabulist.graph import clock

    return clock


def _folded_clock(turn: EntityTurn) -> tuple[str, str]:
    """The projection's folded ``(time, date)`` — the clock OUTSIDE the live loop.

    Fork- and revision-aware like every state-layer read
    (``log.get_projection``). It is the right baseline for a ledger built between
    turns (the user ✎ door, the planner bootstrap) and the WRONG one for the
    tracker's own turn, where it is still one turn behind — that is what
    ``EntityTurn.clock`` exists for. An unreadable projection reads as no clock
    at all, so the validators use their own defaults instead of failing a turn.
    """
    try:
        state = turn.log.get_projection(turn.session_id).get("state") or {}
    except Exception:  # noqa: BLE001 - an unreadable fold is not a tool failure
        logger.warning(
            "could not fold the clock for session %r", turn.session_id, exc_info=True
        )
        return "", ""
    return str(state.get("time") or "").strip(), str(state.get("date") or "").strip()


def _turn_clock(turn: EntityTurn) -> tuple[str, str, bool]:
    """``(time, date, from_fold)`` — the baseline every instant write is judged on.

    ``EntityTurn.clock`` is THIS turn's pair, threaded by the runner (W9.32 §3.5);
    the projection a tool can read already holds the PREVIOUS turn's time. The
    fold is used only when the ledger carries no readable clock, and
    ``from_fold`` lets ``propose_time`` say which baseline its refusal used.
    """
    pair = turn.clock
    if pair is not None:
        try:
            time_text = str(pair[0] or "").strip()
            date_text = str(pair[1] or "").strip()
        except (TypeError, IndexError, KeyError):
            logger.warning("the ledger's clock is not a (time, date) pair: %r", pair)
            time_text = ""
        if time_text:
            return time_text, date_text, False
    time_text, date_text = _folded_clock(turn)
    return time_text, date_text, True


def _current_instant(turn: EntityTurn) -> tuple[date, int] | None:
    """The live instant an expiry is judged against, or ``None`` without a clock.

    ``None`` means the ledger and the fold together carry no readable
    ``(time, date)`` — there is then no instant for an expiry to be "in the past"
    of, and the store's own format check is the only one that applies.
    """
    time_text, date_text, _from_fold = _turn_clock(turn)
    if not time_text or not date_text:
        return None
    return _clock().parse_instant(f"{date_text} {time_text}")


def _expiry_instant(text: str) -> tuple[date, int] | None:
    """Parse an ``expires_at`` in the WRITER's own wire format, else ``None``.

    The store accepts exactly ``entities.INSTANT_FORMAT``
    (``YYYY-MM-DD HH:MM``), so this door parses the same format and leaves any
    other spelling to the store's refusal — a door that accepted more than the
    writer does would validate an expiry against the clock and then fail the
    write for its shape.
    """
    try:
        parsed = datetime.strptime(text, entities.INSTANT_FORMAT)
    except (TypeError, ValueError):
        return None
    return parsed.date(), parsed.hour * 60 + parsed.minute


def _reject(error: object) -> dict[str, Any]:
    return {"ok": False, "error": str(error)}


def _accept(**fields: Any) -> dict[str, Any]:
    return {"ok": True, **fields}


def _text(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    return value.strip() if isinstance(value, str) else ""


def _optional(args: dict[str, Any], name: str) -> str | None:
    """An optional string argument: absent/blank -> ``None`` (never an empty patch)."""
    value = args.get(name)
    if value is None:
        return None
    text = value.strip() if isinstance(value, str) else str(value).strip()
    return text or None


def _location_ref(reg: entities.Registry, ref: str) -> str | None:
    """The location id an ``at`` reference names, or ``None`` when absent."""
    if not ref:
        return None
    return entities.resolve_location(reg, ref, allow_off_screen=False)


# -- the tools ---------------------------------------------------------------


def entity_action_tools(turn: EntityTurn) -> list["LoopTool"]:
    """The fifteen write tools of §6 and W9.32 §3.3, each charging ``turn``.

    Every ``fn`` is ``async`` on purpose: the loop runs a sync ``fn`` in a worker
    thread, and the ``EventLog``'s sqlite connection is bound to the thread that
    opened it — the writes must stay on the loop thread (the read tools' rule).

    Every writer call here passes ``actor=turn.actor``: attribution follows the
    LEDGER (:class:`EntityTurn`), never the tool, so the tracker's turns (``bts``)
    and the planner's seed/repair phases (``planner``) audit apart.
    """
    from fabulist.agents.loop import LoopTool

    log = turn.log
    session_id = turn.session_id

    def guarded(action: Any) -> Any:
        """Charge the budget, run one writer call, never raise.

        One wrapper for every write tool keeps the accounting honest (the budget
        is charged before the call; the outcome is counted exactly once from the
        result's ``ok``) and turns the readable rejection classes into the tool
        result the model corrects from: :class:`entities.EntityError` (the writer
        door) and ``ValueError`` (the cast rename door / the catalog). An
        unexpected exception is a bug, not model drift, so it is logged and
        answered rather than killing the turn.
        """

        async def run(args: dict[str, Any]) -> dict[str, Any]:
            refusal = turn.spend()
            if refusal is not None:
                return _reject(refusal)
            try:
                result = action(args)
            except (EntityError, ValueError) as exc:
                turn.rejected += 1
                return _reject(exc)
            except Exception as exc:  # noqa: BLE001 - the model reads it back
                turn.rejected += 1
                logger.warning("entity action %r failed", action, exc_info=True)
                return _reject(f"{type(exc).__name__}: {exc}")
            ok = bool(result.get("ok"))
            turn.settle(ok)
            if ok:
                turn.actions += 1
            else:
                turn.rejected += 1
            return result

        return run

    # -- establish ---------------------------------------------------------

    def establish_location(args: dict[str, Any]) -> dict[str, Any]:
        location_id = entities.establish_location(
            log,
            session_id,
            _text(args, "name"),
            parent_id=_optional(args, "parent_id"),
            note=_text(args, "note"),
            root=args.get("root") is True,
            seq=turn.narrative_seq(),
            actor=turn.actor,
        )
        return _accept(id=location_id)

    def establish_thread(args: dict[str, Any]) -> dict[str, Any]:
        thread_id = entities.establish_thread(
            log,
            session_id,
            _text(args, "name"),
            detail=_text(args, "detail"),
            seq=turn.narrative_seq(),
            actor=turn.actor,
        )
        return _accept(id=thread_id)

    def establish_relationship(args: dict[str, Any]) -> dict[str, Any]:
        source, refusal = _resolve_being(
            log,
            session_id,
            _text(args, "from"),
            consequence="no relationship until accepted",
        )
        if refusal:
            return _reject(refusal)
        target, refusal = _resolve_being(
            log,
            session_id,
            _text(args, "to"),
            consequence="no relationship until accepted",
        )
        if refusal:
            return _reject(refusal)
        relationship_id = entities.establish_relationship(
            log,
            session_id,
            source,
            target,
            note=_text(args, "note"),
            seq=turn.narrative_seq(),
            actor=turn.actor,
        )
        _mention_being(turn, log, session_id, source)
        _mention_being(turn, log, session_id, target)
        return _accept(id=relationship_id)

    def establish_item(args: dict[str, Any]) -> dict[str, Any]:
        holder, refusal = _resolve_being(
            log,
            session_id,
            _text(args, "holder"),
            consequence="no item until accepted",
        )
        if refusal:
            return _reject(refusal)
        item_id = entities.establish_item(
            log,
            session_id,
            _text(args, "name"),
            holder=holder,
            note=_text(args, "note"),
            seq=turn.narrative_seq(),
            actor=turn.actor,
        )
        _mention_being(turn, log, session_id, holder)
        return _accept(id=item_id)

    # -- move --------------------------------------------------------------

    def move_being(args: dict[str, Any]) -> dict[str, Any]:
        who_surface = _text(args, "who")
        who, refusal = _resolve_being(
            log,
            session_id,
            who_surface,
            consequence="not movable until accepted",
        )
        if refusal:
            return _reject(refusal)
        if who == entities.SCENE:
            return _reject("the scene is not a being — move_being a member or 'player'")
        reg = entities.registry(log, session_id)
        to_ref = _text(args, "to")
        target = entities.resolve_location(reg, to_ref)
        at = _location_ref(reg, _text(args, "at"))
        being, stored = entities.move_being(
            log,
            session_id,
            who,
            target,
            at=at,
            seq=turn.narrative_seq(),
            actor=turn.actor,
        )
        _mention_being(turn, log, session_id, being)
        result = _accept(who=being, to=stored)
        if stored == entities.OFF_SCREEN:
            result["at"] = at
        return result

    # -- set ---------------------------------------------------------------

    def set_attr(args: dict[str, Any]) -> dict[str, Any]:
        catalog = entity_catalog.catalog_for(log)
        key = _text(args, "key")
        row = entity_catalog.row_for(key, catalog)
        if row is None:
            # The catalog's own unknown-key text (plan §6, verbatim): listing the
            # keys the tracker may use IS the correction.
            return _reject(entity_catalog.range_check(key, args.get("value"), catalog)[2])
        if row["host"] == entity_catalog.HOST_SCENE:
            host = entities.SCENE
        else:
            host, refusal = _resolve_being(
                log,
                session_id,
                _text(args, "host"),
                consequence="no attribute set until accepted",
            )
            if refusal:
                return _reject(refusal)
        subject: str | None = None
        if row["host"] == entity_catalog.HOST_RELATIONAL:
            subject, refusal = _resolve_being(
                log,
                session_id,
                _text(args, "subject"),
                consequence="no attribute set until accepted",
            )
            if refusal:
                return _reject(refusal)
        elif _text(args, "subject"):
            return _reject(
                f"{row['key']} is the host's own state — drop the subject "
                "(only relational attributes are set toward someone)"
            )
        value = args.get("value")
        if row["type"] == entity_catalog.METER:
            ok, coerced, message = entity_catalog.range_check(key, value, catalog)
            if not ok:
                return _reject(message)
            value = coerced
        meter_id, prior = entities.set_attribute(
            log,
            session_id,
            host,
            key=key,
            value=value,
            subject=subject,
            seq=turn.narrative_seq(),
            actor=turn.actor,
        )
        _mention_being(turn, log, session_id, host)
        if subject:
            _mention_being(turn, log, session_id, subject)
        return _accept(id=meter_id, prior=prior)

    # -- update / retire ---------------------------------------------------

    def update_object(args: dict[str, Any]) -> dict[str, Any]:
        object_id = entities.update_object(
            log,
            session_id,
            _text(args, "id"),
            name=_optional(args, "name"),
            note=_optional(args, "note"),
            status=_optional(args, "status"),
            detail=_optional(args, "detail"),
            holder=_holder_key(log, session_id, _optional(args, "holder")),
            parent_id=_optional(args, "parent_id"),
            seq=turn.narrative_seq(),
            actor=turn.actor,
        )
        return _accept(id=object_id)

    def retire_object(args: dict[str, Any]) -> dict[str, Any]:
        object_id = entities.retire_object(
            log,
            session_id,
            _text(args, "id"),
            reason=_text(args, "reason"),
            seq=turn.narrative_seq(),
            actor=turn.actor,
        )
        return _accept(id=object_id)

    # -- rename ------------------------------------------------------------

    def rename_being(args: dict[str, Any]) -> dict[str, Any]:
        """``patch_member`` — one ``member_id`` keeps its colour and history.

        NEVER a merge: a name another member already carries is refused by the
        cast layer (a ``cast_ambiguous`` row, no patch). An unknown surface is the
        W7 ADDENDUM 1 proposal case — the successor of the report path's
        ``references[].rename_to``: the NEW name goes to the owner, the old
        spelling rides as its alias (review R1).
        """
        new_name = _text(args, "to")
        if not new_name:
            return _reject("a rename needs the new name in 'to'")
        who_surface = _text(args, "who")
        who, _ = _lookup_being(log, session_id, who_surface)
        if who is None:
            if _emit_cast_proposal(log, session_id, new_name, [who_surface]):
                turn.mention(new_name)
                return _reject(
                    f"'{who_surface}' is not a cast member yet — '{new_name}' "
                    "proposed for owner review; the rename applies once accepted"
                )
            return _reject(
                f"'{who_surface}' is not a cast member — call get_roster for the "
                "ids you may rename"
            )
        if who == entities.PLAYER:
            return _reject(
                "the player is not a cast member — the persona owns that name"
            )
        member_id = _patch_member(log, session_id, who, new_name, _text(args, "reason"))
        _mention_being(turn, log, session_id, member_id)
        turn.mention(new_name)
        return _accept(member_id=member_id)

    # -- flag --------------------------------------------------------------

    def flag_contradiction(args: dict[str, Any]) -> dict[str, Any]:
        """One ``system``/``validation_notice`` row on the turn's narrative (D-F).

        The tracker is the writer of record (req. 12): prose never writes state, so
        a disagreement between the registry and the prose is RECORDED here and the
        registry is left alone. The flag renders as a transcript marker after its
        turn; the corrective write (a real ``move_being``) is the model's own next
        action, in the open.
        """
        anchor = _text(args, "anchor") or _text(args, "prose_says")
        if not anchor:
            return _reject("a flag needs an anchor — the prose phrase that disagrees")
        narrative_seq = turn.narrative_seq()
        if narrative_seq is None:
            return _reject("no narrative to anchor the flag to — nothing was filed")
        registry_says = _text(args, "registry_says")
        prose_says = _text(args, "prose_says")
        what = _text(args, "what")
        row: dict[str, Any] = {
            "check": "registry_contradiction",
            "severity": "warn",
            "anchor": anchor,
            "fix": (
                f"registry holds {registry_says}; move_being if the move really happened"
                if registry_says
                else "read the registry before correcting it — no silent overwrite"
            ),
        }
        for name, value in (
            ("what", what),
            ("registry_says", registry_says),
            ("prose_says", prose_says),
        ):
            if value:
                row[name] = value
        log.append_event(
            session_id,
            "system",
            "bts",
            {"event": "validation_notice", "seq": narrative_seq, "flags": [row]},
        )
        turn.flags += 1
        return _accept(flagged=True)

    # -- condition / body (W9.32 §3.1, R5/R6) -------------------------------

    def set_condition(args: dict[str, Any]) -> dict[str, Any]:
        """Upsert a ``cd_`` condition on a being or on the scene (§3.1, R5).

        The host goes through the being door (``scene`` included), the severity
        enum and the instant FORMAT stay the store's own refusals (one validation
        path per kind), and the expiry is judged against THIS TURN's clock — the
        fold inside a tool is one turn stale mid-loop, so a wound expiring at
        21:30 would otherwise pass against yesterday's 20:00.
        """
        host, refusal = _resolve_being(
            log,
            session_id,
            _text(args, "host"),
            consequence="no condition until accepted",
        )
        if refusal:
            return _reject(refusal)
        expires_at = _optional(args, "expires_at")
        if expires_at:
            current = _current_instant(turn)
            expiry = _expiry_instant(expires_at)
            if current is not None and expiry is not None and expiry <= current:
                return _reject(
                    f"the expiry {expires_at} is not after the current instant "
                    f"{_clock().format_instant(*current)} — an expiry has to be in "
                    "the future (the clock retires a condition whose instant it "
                    "has reached); leave expires_at out for a permanent one"
                )
        condition = entities.set_condition(
            log,
            session_id,
            host,
            _text(args, "key"),
            severity=_optional(args, "severity"),
            source=_text(args, "source"),
            expires_at=expires_at,
            note=_text(args, "note"),
            seq=turn.narrative_seq(),
            actor=turn.actor,
        )
        _mention_being(turn, log, session_id, host)
        return _accept(
            id=condition.id,
            host=condition.host,
            severity=condition.severity,
            expires_at=condition.expires_at,
        )

    def set_body(args: dict[str, Any]) -> dict[str, Any]:
        """Upsert the being's ONE body (§3.1, R6).

        The values are handed to the writer UNCOERCED on purpose: the store's
        contract is ``None`` = leave alone, an explicit ``""``/``[]`` = clear that
        field (undressing someone is a real edit), and ``clothing`` takes either an
        ordered layer list or one comma-joined line. The scene has no body — that
        refusal is the writer's, not a second rule here.
        """
        host, refusal = _resolve_being(
            log,
            session_id,
            _text(args, "host"),
            consequence="no body until accepted",
        )
        if refusal:
            return _reject(refusal)
        body = entities.set_body(
            log,
            session_id,
            host,
            pose=args.get("pose"),
            clothing=args.get("clothing"),
            surface=args.get("surface"),
            seq=turn.narrative_seq(),
            actor=turn.actor,
        )
        _mention_being(turn, log, session_id, host)
        return _accept(
            id=body.id,
            host=body.host,
            pose=body.pose,
            clothing=list(body.clothing),
            surface=body.surface,
        )

    # -- economy (W9.32 §3.1, R7) ------------------------------------------

    def establish_economy(args: dict[str, Any]) -> dict[str, Any]:
        """Seed the session's one purse (§3.1, R7); the planner is the seeder of record.

        A second establish is refused by the store naming the live ``eco_`` id, so
        changing currency stays an explicit retire-then-establish (O3) instead of a
        silent second purse. The row is stamped with THIS LEDGER's actor
        (:attr:`EntityTurn.actor`), so the planner's own seed says ``planner`` and a
        tracker re-seed (the post-retire path O3 allows) says ``bts`` — the store's
        default (``planner``, the seeder of record) stays for callers outside a tool
        loop.
        """
        balance = args.get("balance")
        economy = entities.establish_economy(
            log,
            session_id,
            _text(args, "denomination"),
            balance=0 if balance is None else balance,
            seq=turn.narrative_seq(),
            actor=turn.actor,
        )
        return _accept(
            id=economy.id,
            denomination=economy.denomination,
            balance=economy.balance,
        )

    def adjust_economy(args: dict[str, Any]) -> dict[str, Any]:
        """Move the balance by ``amount`` (negative spends); the store owns the refusals.

        No economy yet, a zero amount, a foreign denomination and a result below
        zero are all the writer's corrective texts — surfacing them unchanged keeps
        one validation path for the tools, the ✎ door and the store.
        """
        economy = entities.adjust_economy(
            log,
            session_id,
            args.get("amount"),
            denomination=_optional(args, "denomination"),
            seq=turn.narrative_seq(),
            actor=turn.actor,
        )
        return _accept(
            id=economy.id,
            denomination=economy.denomination,
            balance=economy.balance,
        )

    # -- clock proposal (W9.32 §3.3/§3.4, R4) --------------------------------

    def propose_time(args: dict[str, Any]) -> dict[str, Any]:
        """Propose a new clock instant with prose evidence; audit an accepted one.

        Judged against THIS TURN's ``(time, date)`` (:func:`_turn_clock`): the fold
        a tool can read is the PREVIOUS turn's clock, so a stale baseline would
        accept a proposal that runs backwards. Outside the live loop (the user ✎
        door, the planner bootstrap) the ledger carries no clock and the folded
        projection IS the current clock — the refusal says when that was the
        baseline. On accept ONE ``system``/``clock_proposal`` row records the
        instant, the evidence and the step (§3.2: audit + tree provenance, never a
        time storage path) and the verdict lands on ``turn.time_proposal`` for the
        BTS node to fold (§3.5). A refusal appends nothing.
        """
        evidence = _text(args, "evidence")
        current_time, current_date, from_fold = _turn_clock(turn)
        verdict = _clock().validate_time_proposal(
            current_time,
            current_date,
            _text(args, "to"),
            evidence=evidence,
            # `is True`, never `bool(...)`: a model that sends the STRING "false"
            # (or 1) has not stated a sleep, and only a stated sleep may lift the
            # six-hour jump bound. Same guard as `root=args.get("root") is True`.
            stated_sleep=args.get("sleep") is True,
        )
        if not verdict.ok:
            reason = verdict.reason
            if from_fold:
                reason = (
                    f"{reason} (this ledger carries no live clock — the folded "
                    "projection's time was the baseline)"
                )
            return _reject(reason)
        log.append_event(
            session_id,
            "system",
            "bts",
            {
                "event": "clock_proposal",
                "time": verdict.time,
                "date": verdict.date,
                "day": verdict.day,
                "evidence": evidence,
                "advanced_minutes": verdict.advanced_minutes,
            },
        )
        turn.time_proposal = {
            "time": verdict.time,
            "date": verdict.date,
            "day": verdict.day,
            "advanced_minutes": verdict.advanced_minutes,
            "evidence": evidence,
        }
        return _accept(
            time=verdict.time,
            date=verdict.date,
            day=verdict.day,
            advanced_minutes=verdict.advanced_minutes,
            reason=verdict.reason,
        )

    # -- reconcile ---------------------------------------------------------

    def reconcile(args: dict[str, Any]) -> dict[str, Any]:
        """Diff the model's stated BELIEFS against the registry; file one flag each.

        §12's flags-only pass: ONE ``system``/``validation_notice`` row per
        disagreement (``check: "reconciliation_drift"``), the turn's narrative as
        the anchor, and the registry untouched — the pass never writes state
        (req. 12). The result echoes the disagreements so the model can see, in the
        same loop, what it half-remembered; the corrective write is its own next
        normal action.
        """
        narrative_seq = turn.narrative_seq()
        if narrative_seq is None:
            return _reject("no narrative to anchor the reconciliation to — nothing was filed")
        beliefs = args.get("beliefs")
        rows = reconcile_drift(log, session_id, beliefs)
        # ONE flag per disagreement for the WHOLE pass, not per tool call: the live
        # model calls reconcile several times in one pass and restates the same
        # drifts each time (19 flags / 2 subjects observed), so the ledger's own
        # ``drift`` is the dedupe scope. A subject already flagged is not re-filed;
        # the model still hears it back in the result so it can correct itself.
        already = {str(row.get("what")) for row in turn.drift}
        fresh = [row for row in rows if str(row.get("what")) not in already]
        for row in fresh:
            log.append_event(
                session_id,
                "system",
                "bts",
                {"event": "validation_notice", "seq": narrative_seq, "flags": [row]},
            )
        turn.flags += len(fresh)
        turn.beliefs += len(beliefs)
        turn.drift.extend(fresh)
        return _accept(
            drift=len(fresh),
            disagreements=[
                f"{row['what']}: the registry holds {row['registry_says']}"[:240]
                for row in rows
            ],
        )

    tools = [
        LoopTool(
            name=ESTABLISH_LOCATION_TOOL,
            description=(
                "Add a room or area to the registry — only a place the registry does "
                "not already list. Returns its loc_ id; cite that id from then on."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The room/area name."},
                    "parent_id": {
                        "type": "string",
                        "description": "The loc_ id of the area this room sits in.",
                    },
                    "root": {
                        "type": "boolean",
                        "description": (
                            "Only for a genuinely separate building/area. Leave it "
                            "out: an unparented room nests under the session's "
                            "existing place automatically."
                        ),
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional one-line detail worth remembering.",
                    },
                },
                "required": ["name"],
            },
            fn=guarded(establish_location),
        ),
        LoopTool(
            name=ESTABLISH_THREAD_TOOL,
            description=(
                "Open a plot thread. Returns its th_ id; change its status later with "
                "update_object."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The thread's name."},
                    "detail": {
                        "type": "string",
                        "description": "Optional one-line detail of where it stands.",
                    },
                },
                "required": ["name"],
            },
            fn=guarded(establish_thread),
        ),
        LoopTool(
            name=ESTABLISH_RELATIONSHIP_TOOL,
            description=(
                "Record the prose edge between two beings (who they are to each "
                "other). Numbers belong on set_attr, never here. Returns its rel_ id."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "from": {
                        "type": "string",
                        "description": "The being it starts from (member id or name).",
                    },
                    "to": {
                        "type": "string",
                        "description": (
                            "The being it points at (member id, name, or 'player')."
                        ),
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional one-line description of the edge.",
                    },
                },
                "required": ["from", "to"],
            },
            fn=guarded(establish_relationship),
        ),
        LoopTool(
            name=ESTABLISH_ITEM_TOOL,
            description=(
                "Add an inventory item and say who holds it. Returns its itm_ id; a "
                "transfer is update_object with a new holder."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The item's name."},
                    "holder": {
                        "type": "string",
                        "description": "Who has it (member id, name, or 'player').",
                    },
                    "parent_id": {
                        "type": "string",
                        "description": (
                            "A location only: move it inside this loc_ id (re-parent "
                            "an existing place)."
                        ),
                    },
                    "note": {"type": "string", "description": "Optional one-line detail."},
                },
                "required": ["name", "holder"],
            },
            fn=guarded(establish_item),
        ),
        LoopTool(
            name=MOVE_BEING_TOOL,
            description=(
                "Move one being to a location id/name, or to 'off_screen'. A move IS "
                "the presence change: the scene is whoever shares the player's "
                "location. Pass 'at' only when moving off_screen — the location the "
                "off-screen crowd is at."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "who": {
                        "type": "string",
                        "description": "The being (member id, name, or 'player').",
                    },
                    "to": {
                        "type": "string",
                        "description": (
                            "A location id/name from the registry, or 'off_screen'."
                        ),
                    },
                    "at": {
                        "type": "string",
                        "description": "off_screen moves only: the location they are at.",
                    },
                },
                "required": ["who", "to"],
            },
            fn=guarded(move_being),
        ),
        LoopTool(
            name=SET_ATTR_TOOL,
            description=(
                "Set one catalog attribute — a metered number inside its range, or a "
                "free-text trait. The SAME attribute keeps the same mt_ id every "
                "turn: set 'hostility' again to change its value, never to add a "
                "second one."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": (
                            "Whose state it is (member id, name, or 'player'); scene "
                            "attributes ignore this."
                        ),
                    },
                    "subject": {
                        "type": "string",
                        "description": (
                            "Who it is toward — required for relational attributes "
                            "(hostility, trust, ...), omitted for the host's own."
                        ),
                    },
                    "key": {
                        "type": "string",
                        "description": "The catalog key, e.g. hostility, tension, mood.",
                    },
                    "value": {
                        "type": "string",
                        "description": (
                            "The new value: digits for a meter ('42'), free text for "
                            "a trait ('wary')."
                        ),
                    },
                },
                "required": ["key", "value"],
            },
            fn=guarded(set_attr),
        ),
        LoopTool(
            name=UPDATE_OBJECT_TOOL,
            description=(
                "Patch an existing object in place — its id never changes: rename a "
                "location, change a thread's status, move an item to another holder."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The object's id."},
                    "name": {"type": "string", "description": "New name."},
                    "note": {"type": "string", "description": "New note."},
                    "status": {
                        "type": "string",
                        "enum": ["new", "active", "paused", "resolved"],
                        "description": "A thread's status.",
                    },
                    "detail": {"type": "string", "description": "A thread's detail line."},
                    "holder": {
                        "type": "string",
                        "description": (
                            "An item's new holder (member id, name, or 'player')."
                        ),
                    },
                    "parent_id": {
                        "type": "string",
                        "description": (
                            "A LOCATION only: move it inside this loc_ id (re-parent an "
                            "existing place — how a stray root is nested)."
                        ),
                    },
                },
                "required": ["id"],
            },
            fn=guarded(update_object),
        ),
        LoopTool(
            name=RETIRE_OBJECT_TOOL,
            description=(
                "Retire an object that is gone for good (a place left behind, a "
                "resolved thread). A location somebody is standing in cannot be "
                "retired — move them first."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "The object's id."},
                    "reason": {"type": "string", "description": "Optional why."},
                },
                "required": ["id"],
            },
            fn=guarded(retire_object),
        ),
        LoopTool(
            name=RENAME_BEING_TOOL,
            description=(
                "Rename a cast member in place: the same member id, colour and "
                "history, with the old spelling kept as an alias. Use this instead "
                "of proposing the person again. Never merges two people."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "who": {
                        "type": "string",
                        "description": "The member (id, current name, or alias).",
                    },
                    "to": {"type": "string", "description": "The name they go by now."},
                    "reason": {
                        "type": "string",
                        "description": "Optional: why the name changed.",
                    },
                },
                "required": ["who", "to"],
            },
            fn=guarded(rename_being),
        ),
        LoopTool(
            name=FLAG_CONTRADICTION_TOOL,
            description=(
                "Record that the prose contradicts the registry (the registry says "
                "one place/state, the prose another). This files a warning on the "
                "turn and does NOT change the registry — if the change really "
                "happened, make it with a real action."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "what": {"type": "string", "description": "What disagrees."},
                    "registry_says": {
                        "type": "string",
                        "description": "What the registry holds (id and name).",
                    },
                    "prose_says": {
                        "type": "string",
                        "description": "What the prose wrote.",
                    },
                    "anchor": {
                        "type": "string",
                        "description": "The prose phrase that shows the disagreement.",
                    },
                },
                "required": ["anchor"],
            },
            fn=guarded(flag_contradiction),
        ),
        LoopTool(
            name=SET_CONDITION_TOOL,
            description=(
                "Put a condition on a being or on the scene (a wound, a chill in the "
                "room, a spell) — one condition per host and key: set 'wary' again "
                "to change it, never to add a second one. An expiry is a clock "
                "instant in the FUTURE; leave it out for a permanent condition."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": (
                            "Who carries it (member id, name, or 'player'), or "
                            "'scene' for a condition of the place itself."
                        ),
                    },
                    "key": {
                        "type": "string",
                        "description": (
                            "The condition's own word, e.g. bleeding, wary, hushed."
                        ),
                    },
                    "severity": {
                        "type": "string",
                        "enum": sorted(entities.CONDITION_SEVERITIES),
                        "description": "How bad it is. Defaults to moderate.",
                    },
                    "source": {
                        "type": "string",
                        "description": "What inflicted it, in a few words.",
                    },
                    "expires_at": {
                        "type": "string",
                        "description": (
                            'When it passes, as "YYYY-MM-DD HH:MM" later than the '
                            "scene's current time; omit for permanent."
                        ),
                    },
                    "note": {
                        "type": "string",
                        "description": "Optional one-line detail.",
                    },
                },
                "required": ["host", "key"],
            },
            fn=guarded(set_condition),
        ),
        LoopTool(
            name=SET_BODY_TOOL,
            description=(
                "Set a being's body — how they stand, what they wear, what shows on "
                "them. One body per being: the same call changes it, never adds a "
                "second. The scene has no body."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "host": {
                        "type": "string",
                        "description": "The being (member id, name, or 'player').",
                    },
                    "pose": {
                        "type": "string",
                        "description": "How they hold themselves, e.g. standing, curled up.",
                    },
                    "clothing": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Clothing layers, outermost first.",
                    },
                    "surface": {
                        "type": "string",
                        "description": (
                            "What shows on them: dirt, blood, a fading bruise."
                        ),
                    },
                },
                "required": ["host"],
            },
            fn=guarded(set_body),
        ),
        LoopTool(
            name=ESTABLISH_ECONOMY_TOOL,
            description=(
                "Seed the session's ONE purse: the currency's name and the opening "
                "balance. Refused while an economy is already live (adjust_economy "
                "moves it; retire it first to change the currency)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "denomination": {
                        "type": "string",
                        "description": "The currency, e.g. pounds, dollars, credits.",
                    },
                    "balance": {
                        "type": "integer",
                        "description": (
                            "The opening balance — 0 when the story states none."
                        ),
                    },
                },
                "required": ["denomination"],
            },
            fn=guarded(establish_economy),
        ),
        LoopTool(
            name=ADJUST_ECONOMY_TOOL,
            description=(
                "Move the session's balance: a positive amount is a gain, a negative "
                "one a spend. The balance may never go below zero and the amount "
                "must be in the purse's own denomination."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "amount": {
                        "type": "integer",
                        "description": (
                            "The change: 5 earns 5, -5 spends 5. It is never 0."
                        ),
                    },
                    "denomination": {
                        "type": "string",
                        "description": (
                            "Optional: the purse's currency, when you are quoting it."
                        ),
                    },
                },
                "required": ["amount"],
            },
            fn=guarded(adjust_economy),
        ),
        LoopTool(
            name=PROPOSE_TIME_TOOL,
            description=(
                "Propose a new clock time when the prose really moved it (a wait, a "
                "journey, a sleep). Absolute ('14:35', '2025-06-03 07:00', 'Monday, "
                "June 03, 2025 at 07:00') or a delta ('+90', '+2h', '2h30'). Never "
                "earlier than the current time; at most 6 hours unless sleep: true. "
                "The evidence is the prose that shows the time passing."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "to": {
                        "type": "string",
                        "description": "The new time, absolute or a delta from now.",
                    },
                    "evidence": {
                        "type": "string",
                        "description": "The prose that shows the time passing.",
                    },
                    "sleep": {
                        "type": "boolean",
                        "description": (
                            "True when the prose covers a sleep or a long wait (that "
                            "is what allows a jump past the 6-hour bound)."
                        ),
                    },
                },
                "required": ["to", "evidence"],
            },
            fn=guarded(propose_time),
        ),
    ]
    if turn.reconcile:
        tools.append(
            LoopTool(
                name=RECONCILE_TOOL,
                description=(
                    "Reconciliation pass: state EVERYTHING you believe about the "
                    "scene (who is where, which meters sit at what value, which "
                    "threads sit in what status) and the engine reports where the "
                    "registry disagrees. It files a warning per disagreement and "
                    "changes NOTHING — make any real correction with the normal "
                    "actions afterwards."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "beliefs": {
                            "type": "array",
                            "description": (
                                "One object per belief: "
                                '{"kind": "location", "who": "Yara", "at": "bathroom"} | '
                                '{"kind": "meter", "host": "Seraphine", "subject": '
                                '"player", "key": "hostility", "value": 42} | '
                                '{"kind": "thread", "name": "the slow courtship", '
                                '"status": "active"}'
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "kind": {
                                        "type": "string",
                                        "enum": list(BELIEF_KINDS),
                                    },
                                    "who": {"type": "string"},
                                    "at": {
                                        "type": "string",
                                        "description": "A location id/name, or 'off_screen'.",
                                    },
                                    "host": {"type": "string"},
                                    "subject": {"type": "string"},
                                    "key": {"type": "string"},
                                    "value": {
                                        "type": "string",
                                        "description": "The value you believe it holds.",
                                    },
                                    "name": {"type": "string"},
                                    "status": {"type": "string"},
                                },
                                "required": ["kind"],
                            },
                        }
                    },
                    "required": ["beliefs"],
                },
                fn=guarded(reconcile),
            )
        )
    return tools


#: What a reconciliation flag says the correction is. Flags are the whole output
#: of the pass (req. 12: no auto-fix, no silent overwrite); the corrective write is
#: the tracker's next normal turn or the owner's sidebar door.
RECONCILE_FIX = (
    "the registry is the record — read get_folded_state; correct it with a real "
    "action if the story moved it"
)


def _drift_row(what: str, anchor: str, registry_says: str) -> dict[str, Any]:
    """One disagreement in the §10 validation-notice shape (§12's check id)."""
    return {
        "check": RECONCILIATION_DRIFT_CHECK,
        "severity": "warn",
        "what": what,
        "anchor": anchor,
        "registry_says": registry_says,
        "fix": RECONCILE_FIX,
    }


def _placement(reg: entities.Registry, where: object) -> str:
    """A being's assignment as a flag renders it: ``bathroom (loc_4b7e)``.

    The off-screen bucket renders with its own reference (``off_screen (at: hall)``)
    so a belief about the absence bucket can be compared without reading ids.
    """
    target = str(where or "").strip()
    if not target:
        return "no location"
    if target == entities.OFF_SCREEN:
        reference = reg.off_screen_ref
        path = entities.location_path(reg, reference) if reference else ""
        return f"off_screen (at: {path})" if path else "off_screen"
    path = entities.location_path(reg, target) or target
    return f"{path} ({target})"


def _same_attribute(left: object, right: object) -> bool:
    """Do a believed value and a stored value agree? (number vs text, tolerance)."""
    left_number = isinstance(left, (int, float)) and not isinstance(left, bool)
    right_number = isinstance(right, (int, float)) and not isinstance(right, bool)
    if left_number and right_number:
        return float(left) == float(right)
    if left_number or right_number:
        # One side is text: compare the NUMBER the text spells (the model states
        # "42" for a meter it was shown as 42).
        try:
            return float(str(left).strip()) == float(str(right).strip())
        except (TypeError, ValueError):
            return False
    return str(left or "").strip().casefold() == str(right or "").strip().casefold()


def _live_meter(
    reg: entities.Registry, host: str, subject: str | None, key: str
) -> entities.Meter | None:
    """The registry's meter for the identity ``(host, subject, key)`` (§3.4)."""
    for meter in reg.meters.values():
        if meter.host == host and meter.subject == subject and meter.key == key:
            return meter
    return None


def reconcile_drift(
    log: Any, session_id: str, beliefs: object
) -> list[dict[str, Any]]:
    """Every disagreement between the model's stated BELIEFS and the registry (§12).

    The pass's whole engine half: it reads the folded registry, compares each
    belief, and returns one flag row per disagreement — in belief order, so the
    transcript shows them as the model stated them. It appends NOTHING and writes
    NOTHING (the tool door appends the notices; req. 12: the pass never rewrites
    the world).

    Three belief shapes, one per kind:

    * ``{"kind": "location", "who": "Yara", "at": "bathroom"}``
    * ``{"kind": "meter", "host": "Seraphine", "subject": "player",
      "key": "hostility", "value": 42}`` (``subject`` omitted for a self meter)
    * ``{"kind": "thread", "name": "the slow courtship", "status": "active"}``

    An UNREADABLE belief (no ``kind``, a kind outside :data:`BELIEF_KINDS`, a
    missing required field) raises :class:`EntityError` naming its index: the model
    corrects it in-loop, the same steering every write tool uses. An unknown NAME
    is not an error — a belief about a person, place or thread the registry does
    not hold IS drift, and it is exactly what the pass exists to surface.
    """
    if not isinstance(beliefs, (list, tuple)) or not beliefs:
        raise EntityError(
            "reconcile needs 'beliefs' — a non-empty list of what you believe "
            '(e.g. {"kind": "location", "who": "Yara", "at": "bathroom"})'
        )
    from .cast import member_by_id, roster

    reg = entities.registry(log, session_id)
    try:
        members = roster(log, session_id)
    except Exception:  # noqa: BLE001 - rendering falls back to the keys
        logger.warning("reconcile could not read the roster", exc_info=True)
        members = []

    def name_of(key: str) -> str:
        if key == entities.PLAYER:
            return entities.PLAYER
        member = member_by_id(members, key)
        return member.display_name if member is not None else key

    rows: list[dict[str, Any]] = []
    for index, belief in enumerate(beliefs):
        if not isinstance(belief, dict):
            raise EntityError(f"beliefs[{index}]: expected an object")
        kind = str(belief.get("kind") or "").strip().lower()
        if kind not in BELIEF_KINDS:
            raise EntityError(
                f"beliefs[{index}]: 'kind' must be one of "
                f"{', '.join(BELIEF_KINDS)}"
            )
        if kind == "location":
            rows.extend(_location_drift(index, belief, log, session_id, reg, name_of))
        elif kind == "meter":
            rows.extend(_meter_drift(index, belief, log, session_id, reg, name_of))
        else:
            rows.extend(_thread_drift(index, belief, reg))
    # ONE row per DISAGREEMENT (live gate: a model that restates the same belief
    # twice would file two identical notices; the transcript should show one).
    # The SUBJECT is the disagreement axis, never the anchor phrasing: the live
    # model restated one drift four times with four different anchor spellings
    # ("stress Yara = 100", "stress Yara=100", ...), so keying on the anchor left
    # the duplicates in. First row wins (its anchor is the one shown).
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for row in rows:
        # ``what`` is host-qualified at the source ("stress Yara", "Seraphine's
        # location"), so the SUBJECT alone is the axis: the model restating one
        # drift — by name or by id, with any anchor spelling — is ONE flag.
        key = str(row.get("what"))
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique


def _location_drift(
    index: int,
    belief: dict[str, Any],
    log: Any,
    session_id: str,
    reg: entities.Registry,
    name_of: Any,
) -> list[dict[str, Any]]:
    """One ``location`` belief -> at most one flag ("who is where")."""
    who = _text(belief, "who")
    where = _text(belief, "at")
    if not who or not where:
        raise EntityError(f"beliefs[{index}]: a location belief needs 'who' and 'at'")
    what = f"{who}'s location"
    key, _ = _lookup_being(log, session_id, who)
    if key is None or key == entities.SCENE:
        return [
            _drift_row(
                what,
                f"{who} is at {where}",
                f"the registry has no such being — no assignment is held for {who!r}",
            )
        ]
    try:
        target = entities.resolve_location(reg, where)
    except EntityError:
        # The believed place is not a registry location: the belief still names a
        # PLACEMENT, and the useful comparison is against the assignment (§12's
        # "the registry says bathroom, the model half-remembers kitchen").
        target = where
    current = reg.assignments.get(key)
    if _same_placement(reg, current, target):
        return []
    return [
        _drift_row(
            what,
            f"{who} is at {where}",
            f"{name_of(key)} is at {_placement(reg, current)}",
        )
    ]


def _same_placement(
    reg: entities.Registry, current: object, believed: object
) -> bool:
    """Does a believed location NAME/ID agree with the registry's assignment?

    Deliberately name-tolerant in the same way the tools' location door is: an id,
    a room name, or an off-screen spelling are all accepted. An unestablished name
    never agrees — a belief about a place the registry does not hold, or about a
    being the registry has not placed, IS drift (that is the pass's whole point).
    """
    stored = str(current or "").strip()
    text = str(believed or "").strip()
    if not stored or not text:
        return False
    if stored == text:
        return True
    wanted = canonical_key(text)
    if stored == entities.OFF_SCREEN:
        # Every off-screen spelling normalises to the bucket (§3.1).
        return wanted in {
            canonical_key(entities.OFF_SCREEN),
            canonical_key(entities.OFF_SCREEN_NAME),
            canonical_key(entities.OFF_SCREEN_LOCATION_ID),
        }
    location = reg.locations.get(stored)
    return location is not None and canonical_key(location.name) == wanted


def _meter_drift(
    index: int,
    belief: dict[str, Any],
    log: Any,
    session_id: str,
    reg: entities.Registry,
    name_of: Any,
) -> list[dict[str, Any]]:
    """One ``meter`` belief -> at most one flag (a value that disagrees)."""
    host_surface = _text(belief, "host")
    attribute = _text(belief, "key")
    if not attribute:
        raise EntityError(f"beliefs[{index}]: a meter belief needs 'key'")
    host, _ = _lookup_being(log, session_id, host_surface or entities.SCENE)
    if host is None:
        return [
            _drift_row(
                f"{attribute} of {host_surface}",
                f"{host_surface} has {attribute} {belief.get('value')}",
                f"the registry has no such being — {host_surface!r}",
            )
        ]
    subject: str | None = None
    subject_surface = _text(belief, "subject")
    if host != entities.SCENE and subject_surface:
        subject, _ = _lookup_being(log, session_id, subject_surface)
        if subject is None:
            return [
                _drift_row(
                    f"{attribute} of {host_surface}",
                    f"{host_surface} {attribute} toward {subject_surface}",
                    f"the registry has no such being — {subject_surface!r}",
                )
            ]
    toward = f"->{name_of(subject)}" if subject else ""
    what = f"{attribute} {name_of(host)}{toward}"
    live = _live_meter(reg, host, subject, attribute)
    believed = belief.get("value")
    if live is None:
        return [
            _drift_row(
                what,
                f"{what} = {believed}",
                f"the registry holds no {attribute} for that host/subject",
            )
        ]
    if _same_attribute(live.value, believed):
        return []
    return [
        _drift_row(
            what,
            f"{what} = {believed}",
            f"{what} = {live.value} ({live.id})",
        )
    ]


def _thread_drift(
    index: int, belief: dict[str, Any], reg: entities.Registry
) -> list[dict[str, Any]]:
    """One ``thread`` belief -> at most one flag (status, or a thread nobody holds)."""
    name = _text(belief, "name") or _text(belief, "id")
    if not name:
        raise EntityError(f"beliefs[{index}]: a thread belief needs 'name'")
    believed = _text(belief, "status")
    what = f"thread {name!r}"
    thread = None
    for candidate in reg.threads.values():
        if name in (candidate.id, candidate.name):
            thread = candidate
            break
    if thread is None:
        folded = name.casefold()
        for candidate in reg.threads.values():
            if candidate.name.strip().casefold() == folded:
                thread = candidate
                break
    if thread is None:
        return [
            _drift_row(
                what,
                f"thread {name!r} is {believed or 'open'}",
                "the registry holds no such thread",
            )
        ]
    if not believed or believed.casefold() == thread.status.casefold():
        return []
    return [
        _drift_row(
            what,
            f"thread {thread.name!r} is {believed}",
            f"thread {thread.name!r} is {thread.status} ({thread.id})",
        )
    ]


def _patch_member(
    log: Any, session_id: str, member_id: str, new_name: str, reason: str
) -> str:
    """The cast rename door: same ``member_id``, old spelling demoted to an alias.

    Imported lazily (the cast layer is the identity owner; this module is a
    caller) and deliberately NOT wrapped in a merge: the cast layer refuses a
    colliding name with a readable ``ValueError`` that the tool door hands back.
    """
    from .cast import patch_member

    return patch_member(
        log, session_id, member_id, display_name=new_name, reason=reason
    )


def mentioned_names(turn: EntityTurn, extra: Sequence[object] = ()) -> list[str]:
    """The turn's mentioned-name set: the actions' names plus any extra surfaces.

    The node hands this to :func:`fabulist.state.bts.apply_bts_cast_proposals`, so
    the D79 identity door (resolve -> alias -> proposal) keeps receiving every
    person the tracker dealt with now that the report payload's ``references[]``
    is gone (§6, review R1).
    """
    names = list(turn.mentions)
    for surface in extra:
        text = str(surface or "").strip()
        if text and text not in names:
            names.append(text)
    return names
