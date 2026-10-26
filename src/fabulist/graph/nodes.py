"""Graph nodes: one factory per turn stage (W9.6).

Each node is a factory returning the async node function, so the injectable
seams (model caller, extractor, log, classifier, message builder) are bound at
graph-build time and the orchestrator can swap in the real implementations
without touching this file.

Node contracts
--------------
``pre_turn``      load prior facts (log first — D11 — then checkpoint cache),
                  classify the fresh user input, advance the clock, compile the
                  injected facts block + the folded plan's render (W9.20: every
                  turn, so the NPC lanes keep a beat on planner-skip turns).
``narrate``       draft → validate → targeted repair, all inside the node (W9.24
                  §3.3): stream the injected `ModelCaller` draft, forward every
                  `StreamEvent`, segment prose vs DSL, run the DSL gate, then run
                  the deterministic validator bundle ENGINE-SIDE. A clean draft
                  is returned as-is (ONE completion — the clean-turn cost is
                  pinned); a flagged draft engages the agent-loop runtime with two
                  tools — ``patch_prose`` (targeted edits over the WORKING-MEMORY
                  prose) and ``patch_dsl`` (statement-level repairs of the DSL
                  channel) — applied pre-commit. The turn itself is
                  committed by the next node (``commit``): no in-node commit
                  exists, so a cancelled turn leaves only its ephemeral partial.
``validate``      DSL findings + validator flags (advisory, never fatal).
``decision_gate`` ONE batched d1-3b ``ask`` -> advisory critic flags.
``critic``        quality gate: ``ok`` / ``diffs`` (revision) / ``fail`` (keep +
                  log — the regeneration it used to request is retired).
``planner``       the steering pass (W9.20 §5): ONE model call fills the structured
                  plan, its event is appended, and the scene clock/weather it
                  sets are seeded into the turn's state before lanes/narrate.
                  Runs only when :func:`make_plan_due_route` says the cadence is
                  due — never per turn.
``bts``           the injected world-state pass, else the `ExtractorFn`;
                  appends the turn's state through `LogPort`.
``env_hud``       one engine-emitted EnvTracker statement (D38).
``attribution``   deterministic speaker attribution + first-sighting colours
                  (no model call: see :mod:`fabulist.state.attribution`).
``done``          terminal marker (writes nothing).

The narrator repairs INSIDE the node (W9.24 §3.3): the draft streams as it always
did, then a model-free validate pass (validator + DSL report + prose_markup
detect) decides whether the runtime engages its repair loop. Repairs are
targeted ``patch_prose`` edits over working-memory prose and ``patch_dsl``
statement ops over the DSL channel — never a second full draft — so a clean
turn costs exactly ONE completion and a defect turn costs the draft plus short
tail iterations over the cached prefix. ``commit``
remains the only writer of the narrative row (and the cancel boundary).

The critic's ``fail`` edge and the ``regen`` stage are RETIRED: a ``fail`` verdict
now keeps the delivered turn with its verdict row (D49's keep behaviour, now the
only behaviour). A stored topology still carrying the dead edge boots unchanged —
the router never returns ``fail``, so the edge is unreachable and no node is
orphaned.

:func:`make_post_turn_node` composes the same stage factories sequentially for
callers that already hold a narrator output in state (direct node calls, tests).

The narrator emits prose + openui DSL only (R2). State never comes from the
narrator: it is injected pre-turn and extracted post-turn.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import dataclasses
import re
import time
import weakref

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.state.attribution import PaletteEntry
    from fabulist.state.proposal_lanes import LaneSpec

from langgraph.config import get_stream_writer

logger = logging.getLogger(__name__)

from .clock import (
    ActionClassifier,
    advance,
    classify_action,
    format_instant,
    parse_facts,
    parse_instant,
    parse_plan_instant,
    render_facts,
    render_state_block,
)
from .planner_schema import planner_tool, validate_planner_payload
from .provider_errors import phase_failure
from .plans import (
    PLAN_ACTOR,
    Plan,
    load_last_plan,
    render_plan_for_prompt,
    save_plan,
)
from .validation import dsl_error_flags, revalidate_with_retry, validate_dsl
from .validator import (
    SEVERITY_WEIGHT,
    ValidatorConfig,
    anchor_for_span,
    validate_narrative,
)
from .types import (
    ExtractorFn,
    LogPort,
    ModelCaller,
    TurnState,
    collect_text,
)

from fabulist.decision import turn_gate_state
from fabulist.decision_queries import (
    MOVEMENT_QUESTION_ID,
    PER_TURN_SECTION,
    TURN_GATE_CHUNK_SIZE,
    chunk_questions,
    flag_issue,
    question_fires,
    question_payloads,
    question_section,
    questions_for_section,
)
from fabulist.agents.loop import (
    LoopInjection,
    LoopTool,
    _echoed_reasoning,
    _injection_messages,
    run_agent_loop,
)
from fabulist.dsl import (
    component_signatures,
    component_stmt_re,
    split_statements,
    statement_re,
)
from fabulist.log.schema_gate import CLOCK_PROPOSAL_EVENT, VALIDATION_NOTICE_EVENT

__all__ = [
    "DEFAULT_CONFIG_SNAPSHOT",
    "DEFAULT_PLANNER_DIRECTIVE",
    "DEFAULT_PLAN_CADENCE",
    "PLANNER_TAIL_TURNS",
    "PATCH_DSL_TOOL",
    "PATCH_PROSE_TOOL",
    "PROSE_MARKUP_FIX",
    "SEGMENT_PREVIEW_CHARS",
    "committed_turn_count",
    "critic_route",
    "default_narrator_messages",
    "facts_from_log",
    "make_attribution_node",
    "make_asset_proposals_node",
    "make_bts_node",
    "make_commit_node",
    "make_critic_node",
    "make_decision_gate_node",
    "make_done_node",
    "make_env_hud_node",
    "make_narrate_node",
    "make_narrator_node",
    "make_plan_due_route",
    "make_planner_node",
    "make_post_turn_node",
    "make_pre_turn_node",
    "make_validate_node",
    "plan_is_due",
    "segment_narrative",
    "turn_gate_flags",
]

#: Placeholder provenance recorded on every narrative event (R10/W3.6). The
#: connections/registry lane supplies the real snapshot later.
DEFAULT_CONFIG_SNAPSHOT: dict[str, Any] = {
    "model": "unknown",
    "sampler": {},
    "prompt_version": 0,
    "facts_hash": None,
}


def _seam_value(seam: Any) -> Any:
    """Resolve a node seam that may be a value or a zero-arg reader.

    The wiring passes the live readers (capability map, connection timeout) as
    callables, so a re-bound preset agent or a hot-reloaded connection applies on
    the NEXT turn with no graph rebuild — the ``_planner_config`` pattern.
    """
    return seam() if callable(seam) else seam


def _stream_writer() -> Callable[[Any], None] | None:
    """The graph's custom stream writer, or ``None`` outside a graph run."""
    try:
        return get_stream_writer()
    except Exception:  # noqa: BLE001 - any contextvar miss means "not in a graph"
        return None


#: In-graph processing steps the user waits on, in execution order (D42). Each
#: maps to ``(short UI label, 1-based index)``; ``total`` is the plan length.
#: The narrator's in-node repair iterations annotate the ``narrate`` SLOT's label
#: ("(repair N)") instead of adding a step (W9.24 §3.3.7) — progress never
#: regresses and the stage total stays 8.
_STAGE_TOTAL = 8
_STAGE_SPECS: dict[str, tuple[str, int]] = {
    # W9.20 §5.6: the planner is the FIRST step of a planner turn (it runs before
    # the lanes and the narration), so it takes index 1 and every other step
    # shifts by one. A planner-skip turn simply starts at ``lanes``.
    "plan": ("Planning the story", 1),
    "lanes": ("Gathering the cast's moves", 2),
    "narrate": ("Writing the scene", 3),
    "dsl": ("Checking the composition", 4),
    "critic": ("Reviewing the turn", 5),
    "world_state": ("Advancing the world", 6),
    "env_hud": ("Updating the HUD", 7),
    "attribution": ("Attributing speakers", 8),
}

#: Public stage-name tuple (W9.1): the flow manifest reads this so the
#: activation map can never drift from what :func:`_emit_stage` writes.
STAGE_NAMES = tuple(_STAGE_SPECS)


def _emit_stage(
    writer: Callable[[Any], None] | None,
    stage: str,
    state: str,
    *,
    label: str | None = None,
    notes: list[str] | None = None,
) -> None:
    """Emit one ``stage`` frame at an in-graph step boundary (D42).

    ``state`` is ``"start"`` or ``"done"``. The payload rides ``meta`` — the
    shape :func:`_emit_revision_frame` established — and carries ``stage`` (the
    node/agent name, the same identity the inspector's per-turn ``calls[].agent``
    uses), ``label``, ``state``, ``index``/``total`` so the client can derive a
    percentage, and ``ts`` (ISO-8601 UTC, matching the log's timestamp format)
    so the client can order a per-turn STEP TRACE without a second channel.
    ``notes`` carries optional validator one-liners attached WHEN the step ran
    (the DSL gate's findings currently); it is omitted when empty so a clean
    step stays a bare boundary. ``writer`` is ``None`` outside a graph run
    (direct node calls in tests), so this is a no-op there.

    Scope note (D42): the fire-and-forget post-turn hooks (compaction / memory
    index / assistant consult, ``api/app.py:_schedule_post_turn``) run AFTER
    ``done`` and never block the user, so they cannot ride this turn stream and
    are deliberately NOT faked as part of the progress. ``done`` means every
    IN-GRAPH step finished — the honest promise.
    """
    if writer is None:
        return
    default_label, index = _STAGE_SPECS[stage]
    meta: dict[str, Any] = {
        "stage": stage,
        "label": label or default_label,
        "state": state,
        "index": index,
        "total": _STAGE_TOTAL,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if notes:
        meta["notes"] = list(notes)
    writer({"type": "stage", "meta": meta})


def _validator_notes(flags: list[dict] | None) -> list[str]:
    """Project validation findings into human one-liners for the step trace.

    Reads the SAME finding rows the inspector surfaces from the
    ``validator_flags`` event (:func:`fabulist.graph.validation.dsl_error_flags`
    shapes ``{rule_id, check, severity, message, evidence, …}``), so the live
    header summary and the post-turn inspector describe a finding identically.
    Order is preserved and exact duplicates are dropped; an empty/absent list
    yields ``[]``.
    """
    notes: list[str] = []
    for flag in flags or []:
        if not isinstance(flag, Mapping):
            continue
        code = str(flag.get("rule_id") or flag.get("check") or "note")
        message = str(flag.get("message") or flag.get("evidence") or "").strip()
        note = f"{code}: {message}" if message else code
        if note not in notes:
            notes.append(note)
    return notes


def facts_from_log(log: LogPort, session_id: str) -> str:
    """Rebuild the prior facts line from the log (D11, log truth).

    W9.32 §6: the facts block is the RENDERED state
    (:func:`fabulist.state.entities.rendered_state`) — the folded clock keys plus
    the registry-derived ``loc``/``weather``/``tone``/``inv``/``genre`` and the
    ``conds`` line — not the raw ``state-delta`` fold the shadow keys used to be
    written into (they are not written at all any more).
    """
    from fabulist.state import entities

    state = entities.rendered_state(log, session_id)
    return render_facts(state) if state else ""


def _sweep_expired_conditions(log: LogPort, session_id: str, facts: str) -> None:
    """Retire every condition the freshly advanced clock has passed (R5, §3.5).

    ``pre_turn`` has just moved the clock, so this is the one point in the turn
    where the engine knows the new instant before any model speaks: a condition
    that expires "21:30" stops being a live fact on the turn whose travel crosses
    21:30, and the removal is an appended ``entity_retire`` (actor ``clock``) —
    never a read-side filter (D9: the log owns the scene). The store's sweep is
    idempotent by tombstone, so running it every turn cannot mint a second
    tombstone for one expiry.

    Contained on purpose: the sweep is bookkeeping BESIDE the turn, so a fold
    without a clock, an unreadable date or a log that cannot answer must never
    fail the turn the player is taking. Anything that goes wrong leaves the
    conditions standing for the next sweep to catch.
    """
    try:
        from fabulist.state import entities

        folded = dict(parse_facts(facts))
        time_value = folded.get("time") or ""
        date_value = folded.get("date") or ""
        if not time_value or not date_value:
            return  # no calendar yet: nothing has been passed
        moment = parse_instant(f"{date_value} {time_value}")
        if moment is None:
            return
        entities.expire_conditions(
            log, session_id, now=format_instant(*moment), actor="clock"
        )
    except Exception:  # noqa: BLE001 - a sweep must never break a turn
        pass


# -- pre_turn ----------------------------------------------------------------


def make_pre_turn_node(
    *,
    log: LogPort,
    classifier: ActionClassifier = classify_action,
):
    """Build the ``pre_turn`` node: classify input, advance the clock, render the plan.

    ``state["facts"]`` is the rules clock's advanced facts block (log truth
    first — D11 — then the checkpoint cache for a cold thread).

    ``state["plan"]`` is the RENDER of the folded current plan
    (:func:`fabulist.graph.plans.load_last_plan` →
    :func:`~fabulist.graph.plans.render_plan_for_prompt`), written EVERY turn.
    That is what keeps the NPC lanes' ``beat`` and the narrator's private block
    fed on the 4-of-5 turns the planner skips (W9.20 §5.5 / M1): the text is
    log-derived now, not the retired ``action:``/``facts:`` stub. Empty string
    on the very first turn, when no plan exists yet — the planner runs then and
    returns the render itself.

    W9.32 R5: the advanced clock is also the expiry sweep — conditions whose
    instant it has passed are retired here, before any model speaks
    (:func:`_sweep_expired_conditions`). The returned keys are unchanged.
    """

    async def pre_turn(state: TurnState) -> dict:
        session_id = state["session_id"]
        action_class = classifier(state["user_content"])
        # Log is truth (D11); the checkpoint's `facts` is only a fallback for a
        # cold thread that has no state-deltas yet.
        prior_facts = facts_from_log(log, session_id) or state.get("facts", "")
        result = advance(action_class, prior_facts)
        # W9.32 R5: the clock just moved, so retire what it has passed. The sweep
        # only appends (it never touches the returned keys), and it is contained
        # — see :func:`_sweep_expired_conditions`.
        _sweep_expired_conditions(log, session_id, result.facts)
        current = load_last_plan(log, session_id)
        plan = render_plan_for_prompt(current) if current is not None else ""
        return {"facts": result.facts, "plan": plan, "action_class": action_class}

    return pre_turn


# -- narrator ----------------------------------------------------------------

#: The statement recognizer comes from the shared spec table
#: (``fabulist.dsl``): every known component name, one source, matching the
#: ``ident = Component(`` shape (positional or named args — the gate checks the
#: content). The same table the validation gate and the strip helper read, so
#: all consumers agree by construction, never by hand-synced lists.
#: (Flavor-probe finding 2026-10-10: a hand-listed regex naming only the five
#: core components stranded every flavor statement in `prose`, which then got
#: pruned from the root as dangling_ref.)


def _component_names() -> tuple[str, ...]:
    """Every component name the segmenter recognizes (core five + flavors)."""
    from fabulist.dsl import component_names

    return component_names()


#: An explicit openui fence opener line (`````openui`` or `````ui``).
_OPEN_FENCE = re.compile(r"```(?:openui|ui)\b[ \t]*\r?\n")

def _collapse_prose(text: str, leaks: list[str] | None = None) -> str:
    """Collapse 3+ newlines to a paragraph break and trim (prose only).

    D80: prose passes through UNTOUCHED apart from the whitespace collapse —
    the owner rejected markup-stripping as a restriction on a confident model.
    Markup in prose is only DETECTED (:func:`_detect_markup_prose`) to feed
    the ``prose_markup`` drift flag (a marker on the transcript, never a
    rewrite).
    """
    _detect_markup_prose(text, leaks)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


#: DETECTED in PROSE only (never inside a DSL statement — a Custom component's
#: ``html`` prop is legitimate and policy-checked elsewhere). D80 removed the
#: W9.22-era strip: the owner ruled the engine must not restrict a model that
#: confidently emits HTML — the fix for markup-in-prose is TEACHING (D78-era
#: library work: card-instructed HTML belongs inside ``Custom(html=, css=)`` /
#: ``Interface(terminal|hologram)``), not output policing. Detection remains so
#: the drift stays observable: the ``prose_markup`` flag surfaces as a quiet
#: transcript marker (item 14) and names the components the narrator should
#: have used. Historical note (ca17f02): the strip existed because markup in
#: prose corrupted attribution, the validator, and promotion evidence — those
#: consumers see the flagged turn via D73's critic path instead.
_STYLE_BLOCK_RE = re.compile(
    r"<(style|script)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL
)
_HTML_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")
_SELF_CLOSING_TAG_RE = re.compile(r"<[a-zA-Z][^>]*/>")

#: The D80 detectors. The anchor span is the FIRST failing candidate in prose
#: order across all three (:func:`_markup_span`).
_MARKUP_PATTERNS = (_STYLE_BLOCK_RE, _SELF_CLOSING_TAG_RE, _HTML_TAG_RE)

#: W9.28 D6: the colour-only dialogue tag. The narrator is TOLD to wrap each
#: spoken line in one, so flagging it as ``prose_markup`` would hand the repair
#: loop an instruction to delete the markup it was asked to write.
_COLOUR_TAGS = frozenset({"span", "font"})
#: The ONLY attributes a colour-only tag may carry (D6b, matched
#: case-insensitively). Anything else — an ``on*`` handler, ``class``, ``id``,
#: ``src``/``href``, ``data-*``, ``title``, ``face`` — makes the tag ordinary
#: markup again.
_COLOUR_ATTRS = frozenset({"style", "color"})

_TAG_NAME_RE = re.compile(r"<\s*(?P<close>/?)\s*(?P<name>[a-zA-Z][a-zA-Z0-9-]*)")
_ATTR_RE = re.compile(
    r"""(?P<name>[a-zA-Z_:][-a-zA-Z0-9_:.]*)
        (?:\s*=\s*
            (?:"(?P<double>[^"]*)"|'(?P<single>[^']*)'|(?P<bare>[^\s"'`=<>]+))
        )?""",
    re.VERBOSE,
)
#: One CSS ``color`` value. Deliberately CLOSED: no ``url(``, no comment, no
#: escape, no second declaration can satisfy it.
_COLOUR_VALUE = r"(?:#[0-9a-fA-F]{3,8}|rgba?\([0-9.,%\s/]+\)|[a-zA-Z]{2,20})"
_STYLE_COLOUR_ONLY_RE = re.compile(
    rf"\s*color\s*:\s*{_COLOUR_VALUE}\s*;?\s*", re.IGNORECASE
)
_COLOUR_VALUE_RE = re.compile(_COLOUR_VALUE)


def _is_colour_markup(tag: str) -> bool:
    """``True`` when ``tag`` is a colour-only dialogue tag (W9.28 D6/D6b).

    The narrator's contract is one palette-coloured span per spoken line, so a
    ``span``/``font`` carrying nothing but that colour is legitimate prose
    markup. The test is PER TAG and deliberately strict — it is a FLAG gate, not
    the DOM boundary (DOMPurify stays the boundary):

    * the tag name is ``span`` or ``font`` (case-insensitive);
    * every attribute is ``style`` or ``color`` — ANY other disqualifies it;
    * ``style`` holds exactly ONE ``color`` declaration and nothing else.

    A colour span whose TEXT carries other markup is still a colour span: the
    nested tag is its own candidate and flags on its own merits.
    """
    match = _TAG_NAME_RE.match(tag or "")
    if match is None or match.group("name").lower() not in _COLOUR_TAGS:
        return False
    body = tag[match.end() :]
    if not body.endswith(">"):
        return False
    body = body[:-1]
    if body.endswith("/"):
        body = body[:-1]
    position = 0
    colour = False
    names: set[str] = set()
    while position < len(body):
        if body[position].isspace():
            position += 1
            continue
        attr = _ATTR_RE.match(body, position)
        if attr is None:
            return False
        name = attr.group("name").lower()
        if name not in _COLOUR_ATTRS or name in names:
            return False
        names.add(name)
        value = attr.group("double")
        if value is None:
            value = attr.group("single")
        if value is None:
            value = attr.group("bare")
        if value is None:
            return False
        if name == "style":
            if not _STYLE_COLOUR_ONLY_RE.fullmatch(value):
                return False
        elif not _COLOUR_VALUE_RE.fullmatch(value.strip()):
            return False
        colour = True
        position = attr.end()
    if match.group("close"):
        return not names
    return colour


def _markup_spans(text: str) -> list[tuple[int, int]]:
    """Every distinct raw-markup run in ``text``, in prose order.

    At one start the LONGEST run wins, so a ``<style>…</style>`` block is
    anchored whole rather than as its opening tag.
    """
    found: set[tuple[int, int]] = set()
    for pattern in _MARKUP_PATTERNS:
        found.update(match.span() for match in pattern.finditer(text))
    return sorted(found, key=lambda span: (span[0], -span[1]))


def _markup_span(text: str) -> tuple[int, int] | None:
    """``(start, end)`` of the FIRST raw-markup run in a prose text, or ``None``.

    The earliest non-whitelisted candidate wins, so the span points at the
    markup the reader would notice first — which is also what the
    ``prose_markup`` flag hands the repair loop as its anchor. The colour-only
    predicate (:func:`_is_colour_markup`) lives HERE, in the single point every
    consumer reads (this anchor, the leak sample and the segmenter's channel),
    so a legitimate colour span can never be anchored, reported or told to be
    removed.
    """
    if not isinstance(text, str) or "<" not in text:
        return None
    for span in _markup_spans(text):
        if not _is_colour_markup(text[span[0] : span[1]]):
            return span
    return None


def _detect_markup_prose(text: str, leaks: list[str] | None = None) -> bool:
    """Report whether a PROSE segment carries raw markup (D80: detect, never strip).

    ``True`` when markup is present; each segment contributes ONE leak sample —
    the opening tag of the SAME filtered candidate :func:`_markup_span` anchors
    (R2 P2-1) — so the drift flag's evidence names what actually appeared, never
    the colour span beside it.
    """
    span = _markup_span(text)
    if span is None:
        return False
    if leaks is not None:
        match = _HTML_TAG_RE.match(text, span[0])
        leaks.append(match.group(0) if match else text[span[0] : span[1]])
    return True


def _untick(line: str) -> str:
    """Strip wholly-wrapping code backticks from a statement line.

    Live-observed: when the library doc's examples showed statements in
    markdown inline-code form, the narrator emitted them the same way —
    `` `status = StatusPanel(...)` `` — which defeated the statement regex.
    Statement extraction tolerates both forms; prose is untouched.
    """
    t = line.strip()
    if len(t) >= 2 and t.startswith("`") and t.endswith("`"):
        inner = t.strip("`").strip()
        if "`" not in inner:
            return inner
    return line


def _segment_unfenced(text: str, leaks: list[str] | None = None) -> list[dict]:
    """Ordered prose/DSL segments from mixed text with no openui fence.

    g4-class narrators emit DSL without ```openui fences, so unfenced component
    statements land in the prose channel. This mirrors the frontend statement
    extractor: a component statement is captured with balanced parens (it may
    span lines) and becomes one ``dsl`` segment; every other line joins the
    surrounding ``prose`` segment. Unlike a two-bucket split, this preserves the
    exact prose / dsl / prose interleaving.
    """
    lines = [_untick(line) for line in text.split("\n")]
    segments: list[dict] = []
    prose_run: list[str] = []

    def flush() -> None:
        if not prose_run:
            return
        collapsed = _collapse_prose("\n".join(prose_run), leaks)
        prose_run.clear()
        if collapsed:
            segments.append({"kind": "prose", "text": collapsed})

    # Read the recogniser at the USE-SITE (once per call): the component
    # vocabulary hot-reloads (W9.27), so an import-time module global would keep
    # classing a swapped-in component as prose — the bad71c7af chain.
    component_stmt = component_stmt_re()
    i = 0
    while i < len(lines):
        if component_stmt.match(lines[i]):
            flush()
            stmt: list[str] = []
            depth = 0
            while i < len(lines):
                stmt.append(lines[i])
                depth += lines[i].count("(") - lines[i].count(")")
                i += 1
                if depth <= 0:
                    break
            segments.append({"kind": "dsl", "text": "\n".join(stmt).strip()})
        else:
            prose_run.append(lines[i])
            i += 1
    flush()
    return segments


def _split_unfenced_statements(text: str) -> tuple[str, str]:
    """Legacy two-bucket view ``(prose, dsl)`` over :func:`_segment_unfenced`."""
    segments = _segment_unfenced(text)
    prose = "\n".join(s["text"] for s in segments if s["kind"] == "prose")
    dsl = "\n".join(s["text"] for s in segments if s["kind"] == "dsl")
    return prose, dsl


def _fence_segments(block: str) -> list[dict]:
    """Split one openui fence body into per-statement ``dsl`` segments."""
    segments = _segment_unfenced(block)
    if segments:
        # Inside a fence everything is DSL: statement splitting is preserved
        # (each component one segment) but non-component text stays DSL too.
        return [{"kind": "dsl", "text": s["text"]} for s in segments]
    text = block.strip()
    return [{"kind": "dsl", "text": text}] if text else []


def _extend_prose(segments: list[dict], text: str) -> None:
    collapsed = _collapse_prose(text)
    if collapsed:
        segments.append({"kind": "prose", "text": collapsed})


def segment_narrative(raw: str, leaks: list[str] | None = None) -> list[dict]:
    """Ordered ``[{"kind": "prose"|"dsl", "text": ...}]`` for a raw stream.

    Fenced openui blocks (`````openui`` / `````ui``) are split into component
    statements (one ``dsl`` segment each); text outside every fence becomes
    prose segments. With no fence, the unfenced statement extractor keeps the
    exact prose/DSL interleaving. Runs of 3+ newlines are collapsed inside
    prose segments only.
    """
    if not raw:
        return []
    fences = list(_OPEN_FENCE.finditer(raw))
    if not fences:
        return _segment_unfenced(raw, leaks)
    segments: list[dict] = []
    pos = 0
    for fence in fences:
        if fence.start() < pos:  # already consumed inside an earlier block
            continue
        _extend_prose(segments, raw[pos : fence.start()])
        close = raw.find("```", fence.end())
        if close == -1:
            block = raw[fence.end() :]
            pos = len(raw)
        else:
            block = raw[fence.end() : close]
            pos = close + 3
        segments.extend(_fence_segments(block))
    _extend_prose(segments, raw[pos:])
    return segments


def default_narrator_messages(state: TurnState) -> list[dict]:
    """Minimal W1.1 message shape; the distilled prompt ships in W1.2."""
    system = (
        "You are the narrator. Emit prose and openui DSL only — never emit "
        "state, environment, or clock lines; state is injected below as facts.\n\n"
        f"<injected_state>\n{state.get('facts', '')}\n</injected_state>\n\n"
        f"<plan>\n{state.get('plan', '')}\n</plan>"
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": state["user_content"]},
    ]


def _repair_messages(statements: Sequence[str], errors: list) -> list[dict]:
    """Constrained re-synthesis prompt for the last-resort DSL repair pass.

    The contract is deliberately NARROWER than the draft: the model sees the
    rejected DSL statements (numbered slots) and the gate's findings — never the
    prose — and answers with a STRUCTURED reply, one corrected statement per
    slot, same count and order. The prose is not the model's to rewrite: it is
    already on screen, and a re-synthesis that regenerates it re-lays-out the
    whole turn (live 54d73764: every panel ended up stacked below the reply).
    """
    lines: list[str] = []
    for error in errors:
        if isinstance(error, Mapping):
            code = str(error.get("code") or "dsl_error")
            message = str(error.get("message") or "")
        else:
            code = str(getattr(error, "code", "dsl_error"))
            message = str(getattr(error, "message", ""))
        lines.append(f"- {code}: {message}")
    slots = "\n".join(f"[{index}] {text}" for index, text in enumerate(statements))
    schema = "\n".join(f"- {line}" for line in component_signatures())
    system = (
        "You are repairing openui DSL statements that a strict validator rejected. "
        "Every component call must be `identifier = Component(args)` with STRICTLY "
        "POSITIONAL arguments in the declared order below. Never use `name=value`. "
        "Declare every child you reference; if you cannot, drop the reference.\n\n"
        "The narration prose is OUT OF SCOPE: it is already written and must never be "
        "restated, rewritten, reordered or re-emitted — you return DSL statements only.\n\n"
        'Reply with ONE JSON object and nothing else: {"statements": ["<statement>", ...]} '
        "with exactly one string per numbered statement below, the SAME count in the "
        "SAME order (slot [i] replaces statement [i]).\n\n"
        "Component signatures (positional order):\n" + schema
    )
    user = (
        "Statements to repair (return the same count, in the same order):\n"
        + (slots or "(none)")
        + "\n\nGate findings:\n"
        + ("\n".join(lines) or "- dsl_error: the composition was rejected")
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_resynthesis_reply(text: str | None) -> list[str] | None:
    """Read ``{"statements": ["…"]}`` from a re-synthesis reply, tolerantly.

    Returns the statement list only when one balanced JSON object names a list of
    strings; a fenced/reasoned reply is accepted (the scan is the critic's own
    brace reader), anything else yields ``None`` so the caller keeps the draft.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    from .quality import _json_object_candidates

    for candidate in _json_object_candidates(text):
        try:
            decoded = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if not isinstance(decoded, dict):
            continue
        statements = decoded.get("statements")
        if isinstance(statements, list) and all(isinstance(item, str) for item in statements):
            return [item.strip() for item in statements]
    return None


def _model_resynthesizer(model: ModelCaller, segments: list[dict]):
    """Last-resort ``resynthesize`` seam: one model call, applied BY POSITION.

    Only reached when the deterministic gate could not produce a valid
    composition (kwargs + dangling refs never reach it). Capped at one attempt
    by :func:`revalidate_with_retry`.

    The reply is a structured statement list: entry ``i`` replaces the text of
    the ``i``-th DSL segment of the DRAFT payload, and every prose segment is
    copied through untouched. Prose is therefore untouchable by construction —
    the model can neither rewrite it nor re-lay-out the turn. A reply whose
    count does not match the slots (or carries no usable JSON) keeps the draft.
    """
    slots = [
        index
        for index, segment in enumerate(segments)
        if isinstance(segment, Mapping) and segment.get("kind") == "dsl"
    ]

    async def resynthesize(errors: list) -> list[dict]:
        from fabulist.agents.debug import CURRENT_INTERACTION

        # A real extra narrator-side model call: relabel its capture so a
        # repaired turn does not read as one more base ``narrate`` pass.
        CURRENT_INTERACTION.set("narrate_resynth")
        draft = [
            dict(segment) if isinstance(segment, Mapping) else segment
            for segment in segments
        ]
        rejected = [str(segments[index].get("text") or "") for index in slots]
        statements = _parse_resynthesis_reply(
            await collect_text(model, _repair_messages(rejected, errors))
        )
        if statements is None or len(statements) != len(slots):
            return draft
        for index, statement in zip(slots, statements):
            draft[index]["text"] = statement
        return draft

    return resynthesize


# -- the narrator repair loop (W9.24 §3.3) -----------------------------------

#: The prose tool the narrator's repair loop declares. Its arguments are the
#: critic's existing edit vocabulary (``[{find, replace, count?}]``) applied by
#: :func:`fabulist.graph.quality.apply_patch` over the WORKING-MEMORY segments —
#: the same prose-only rule the post-commit critic uses (D49), but applied ONE
#: EDIT AT A TIME: an edit whose ``find`` IS prose lands even when a sibling is
#: rejected, so a channel mistake can never discard a valid repair.
PATCH_PROSE_TOOL = "patch_prose"

_PATCH_PROSE_DESCRIPTION = (
    "Repair the drafted turn by targeted find/replace edits. Args: "
    '{"edits": [{"find": "<exact prose text>", "replace": "<replacement>", '
    '"count": 1}]}. Each `find` must be a literal substring of ONE prose block, '
    "appearing exactly `count` times (default 1); a `find` that straddles two "
    "prose blocks is refused. Edits are applied one by one: an edit whose `find` "
    "is prose lands even when a sibling is rejected, and every rejection names its "
    "reason (missing prose / a DSL segment / a boundary) plus the prose segments "
    '(index + opening text) so the next `find` can be copied verbatim from one of '
    "them — an identical retry fails identically. DSL statements cannot be edited "
    "here: a `find` must never be DSL source — repair the DSL with `patch_dsl` — "
    "so reply with no tool call once the prose is clean."
)

_PATCH_PROSE_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "edits": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "find": {"type": "string"},
                    "replace": {"type": "string"},
                    "count": {"type": "integer"},
                },
                "required": ["find", "replace"],
            },
        }
    },
    "required": ["edits"],
}


#: The DSL counterpart: statement-level ops over the ``kind == "dsl"`` channel.
#: The owner ruling behind it — "of course the narrator needs to be able to fix
#: its own DSL! It needs to be able to fix everything it outputs" — is what makes
#: ``patch_prose``'s honest refusal (a `find` that IS DSL source) a complete
#: answer: the channel it names is repairable here. Ops address a STATEMENT, not
#: text: the channel is re-parsed into ``ident = Component(...)`` statements, one
#: op is applied, and the channel is re-validated through the DSL gate
#: (:func:`fabulist.graph.validation.validate_dsl`) before it is kept.
PATCH_DSL_TOOL = "patch_dsl"

#: The ops `patch_dsl` accepts, each applied independently.
DSL_OPS = ("replace", "delete", "insert_after")

_PATCH_DSL_DESCRIPTION = (
    "Repair the drafted turn's openui DSL by STATEMENT-LEVEL ops. Args: "
    '{"ops": [{"op": "replace", "lhs": "<statement name>", "target": "<its exact '
    'current text>", "statement": "<new statement>"}, {"op": "delete", "lhs": '
    '"<statement name>"}, {"op": "insert_after", "lhs": "<anchor statement '
    'name>", "statement": "<new statement>"}]}. The DSL channel is STRUCTURED — '
    "a list of `ident = Component(...)` statements, not arbitrary text — so this "
    "tool never does find/replace inside it: an op names ONE statement by its "
    "`lhs` (case-sensitive) or, failing that, by its exact `target` text. A name "
    "that two statements share is refused and both are listed. Ops are applied one "
    "by one and the channel is re-validated after each: an op that would leave the "
    "DSL unparseable or invalid is rejected with the parse error and the channel is "
    "left unchanged, and every rejection returns the statement inventory (index, "
    "lhs, opening text) so the next op has a real target. PROSE is repaired with "
    "`patch_prose`, never here."
)

_PATCH_DSL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "ops": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": list(DSL_OPS)},
                    "lhs": {"type": "string"},
                    "target": {"type": "string"},
                    "statement": {"type": "string"},
                },
                "required": ["op"],
            },
        }
    },
    "required": ["ops"],
}


@dataclass(frozen=True)
class NarrateRepair:
    """What the repair loop did to one draft (W9.24 §3.3).

    ``segments`` is the working-memory result the commit node will land (the
    draft when nothing was applied); ``passes`` counts the applied patch rounds.
    Findings the loop could not clear are not carried here — the narrate node
    re-reads the DSL gate over the final channel and the POST-commit validate
    node re-derives the validator bundle for the committed segments and records
    it, which is the one place a residual flag is read (R2: fail-open to keep +
    flags, never a hang).
    """

    segments: list[dict]
    passes: int


#: What a ``prose_markup`` flag teaches: the fix is a DSL component, never output
#: policing (D80).
PROSE_MARKUP_FIX = (
    "remove the raw markup from prose, or express it as Custom(html=..., css=...) "
    "in the DSL channel"
)


def _flag_lines(flags: list[dict]) -> list[str]:
    """One entry per finding, rendered so the model knows WHERE and WHAT.

    Each line carries the finding, its ``anchor`` (the exact prose to patch) and
    its ``fix`` (the corrective action) when the flag has them — the repair loop
    steers on these, so a bare diagnostic is never sent.
    """
    lines: list[str] = []
    for flag in flags:
        code = str(flag.get("rule_id") or flag.get("check") or "finding")
        message = str(flag.get("message") or "").strip()
        evidence = str(flag.get("evidence") or "").strip()
        anchor = str(flag.get("anchor") or "").strip()
        fix = str(flag.get("fix") or "").strip()
        line = f"- {code}: {message}" if message else f"- {code}"
        if evidence:
            line += f" (evidence: {evidence})"
        if anchor:
            line += f"\n  anchor (exact prose to patch): {anchor}"
        if fix:
            line += f"\n  fix: {fix}"
        lines.append(line)
    return lines


#: Codes/messages carried per logged flag; the payload stays small enough to ride
#: every repaired turn without bloating the log.
_REPAIR_LOG_FLAG_CAP = 8


def _log_narrate_repair(
    log: LogPort,
    session_id: str,
    *,
    event: str,
    round_index: int,
    flags: list[dict],
    applied: int | None = None,
    tool: str | None = None,
    rejected: list[dict] | None = None,
) -> None:
    """Append ONE bounded ``system``/``narrate_repair_*`` row (never fatal).

    Why: the repair loop's findings went only to the MODEL. A draft rejected four
    times left no trace of what was wrong (the committed `dsl_validation` row
    describes the FINAL channel), so "why is this turn slow / why did it repair"
    was unanswerable from the log. Payload: the round index, the findings
    (``rule_id`` + a short message), what landed, and — on a rejected round — the
    op errors the model was told about.
    """
    if not session_id:
        return
    try:
        log.append_event(
            session_id,
            "system",
            "narrator",
            {
                "event": event,
                "round": int(round_index),
                "flags": [
                    {
                        "code": str(flag.get("rule_id") or flag.get("check") or "finding"),
                        "message": str(flag.get("message") or "")[:200],
                    }
                    for flag in (flags or [])[:_REPAIR_LOG_FLAG_CAP]
                ],
                **({"applied": int(applied)} if applied is not None else {}),
                **({"tool": tool} if tool else {}),
                **(
                    {"rejected": [str(item.get("error") or "")[:200] for item in rejected[:4]]}
                    if rejected
                    else {}
                ),
            },
        )
    except Exception:  # noqa: BLE001 - observability never fails a turn
        logger.warning("could not log narrate repair round", exc_info=True)


def _log_dsl_resynthesis(
    log: LogPort | None,
    session_id: str | None,
    errors: list[dict],
    call_latency_s: float,
    *,
    kept_draft: bool,
) -> None:
    """Append ONE bounded ``system``/``narrator`` ``dsl_resynthesis`` row.

    Why: the last-resort re-synthesis is a real extra model call, and nothing in
    the log said it happened — a turn whose panels came back re-ordered looked
    like the narrator had emitted them that way (live 54d73764). This row names
    the findings that triggered the pass, the call's latency and whether the
    replacement was KEPT or discarded (the draft then stands). Written at the
    resynthesis boundary, so ``call_latency_s`` is the measured call; never fatal.
    """
    if log is None or not session_id:
        return
    try:
        log.append_event(
            session_id,
            "system",
            "narrator",
            {
                "event": "dsl_resynthesis",
                "errors": [
                    {
                        "code": str(error.get("code") or "dsl_error"),
                        "message": str(error.get("message") or "")[:300],
                        "segment_index": error.get("segment_index"),
                    }
                    for error in (errors or [])[:_REPAIR_LOG_FLAG_CAP]
                ],
                "retry_applied": True,
                "call_latency_s": round(float(call_latency_s), 3),
                "kept_draft": bool(kept_draft),
            },
        )
    except Exception:  # noqa: BLE001 - observability never fails a turn
        logger.warning("could not log DSL re-synthesis", exc_info=True)


def _prose_markup_flags(segments: list | None) -> list[dict]:
    """D80 ``prose_markup`` flag: raw markup found in the prose segments.

    Runs the D80 detector (:func:`_detect_markup_prose` — ONE spelling, shared
    with segmentation) over the prose segments that will be committed, so a
    repair that removes the markup clears the flag with it (the narrator repair
    loop re-validates after every patch). The prose itself is never rewritten
    here — the owner rejected output policing; the fix is teaching (the library
    doc names ``Custom(html=, css=)`` / ``Interface(terminal|hologram)``).

    The flag carries the FIRST markup run as its ``anchor`` (with a window of the
    surrounding prose), so the loop can hand the model an exactly-matchable
    ``find`` instead of a description of one.
    """
    leaks: list[str] = []
    anchor = ""
    anchor_index: int | None = None
    for index, segment in enumerate(segments or ()):
        if not isinstance(segment, Mapping) or segment.get("kind") != "prose":
            continue
        text = segment.get("text")
        _detect_markup_prose(text, leaks)
        if anchor_index is None and isinstance(text, str):
            span = _markup_span(text)
            if span is not None:
                anchor_index = index
                anchor = anchor_for_span(text, *span)
    if not leaks:
        return []
    return [
        {
            "rule_id": "prose_markup",
            "check": "format",
            "severity": "warn",
            "evidence": "; ".join(dict.fromkeys(leaks))[:200],
            "source": "engine (narrator contract: prose + DSL only)",
            "segment_index": anchor_index,
            "message": (
                "raw HTML detected in prose (left untouched); emit "
                "Interface(terminal|hologram) or Custom(html=, css=) "
                "instead of markup in prose"
            ),
            "anchor": anchor,
            "fix": PROSE_MARKUP_FIX,
        }
    ]


def _palette_for_turn(log: Any, session_id: str) -> list[PaletteEntry]:
    """The SPEAKING cast's colour palette for this turn, or ``[]`` (W9.28 D2).

    Best-effort by construction: the palette is TEACHING (which hue a speaker's
    line wears), so a cast/roster read that fails leaves the turn unflagged
    instead of failing it — an empty palette flags nothing at all
    (:func:`_attribution_missing_flags`). Built ONCE per repair run, never per
    iteration.
    """
    if not session_id:
        return []
    from fabulist.state.attribution import palette_for_turn

    try:
        return palette_for_turn(log, session_id)
    except Exception:  # noqa: BLE001 - advisory, never fatal
        logger.warning(
            "palette build failed; attribution flags are absent", exc_info=True
        )
        return []


def _attribution_missing_flags(
    segments: list | None, palette: Sequence[PaletteEntry] | None
) -> list[dict]:
    """``attribution_missing`` flags: one per unwrapped spoken line (W9.28 D3).

    The SAME fold runs pre-commit (:func:`_narrate_flags` — where the repair loop
    fixes it) and post-commit (:func:`make_validate_node` — the inspector record),
    so the two can never disagree about what the model left unwrapped. Each flag
    carries the quoted line as its ``anchor`` and a palette span as its ``fix``;
    an empty/absent palette flags nothing (no hue to teach, and no speaker a
    wrapped line could resolve to).
    """
    if not palette:
        return []
    from fabulist.state.attribution import attribution_missing_flags

    return attribution_missing_flags(segments, palette)


def _attribution_unquoted_flags(
    segments: list | None, palette: Sequence[PaletteEntry] | None
) -> list[dict]:
    """``attribution_unquoted`` flags: one per colour span wrapping no quote.

    The same two call sites as :func:`_attribution_missing_flags` —
    :func:`_narrate_flags` pre-commit (where the repair loop quotes the line or
    drops the span) and :func:`make_validate_node` post-commit (the inspector
    record) — so the fold the loop steers on and the fold the critic reads can
    never disagree. Empty palette, no flags: no hue was taught, so no span can
    be a palette colour.
    """
    if not palette:
        return []
    from fabulist.state.attribution import attribution_unquoted_flags

    return attribution_unquoted_flags(segments, palette)


def _mechanical_attribution_wraps(
    segments: list | None, palette: Sequence[PaletteEntry] | None
) -> tuple[list, list]:
    """Wrap the quotes the PROSE itself attributes — zero model calls (issue 31).

    Returns ``(segments, wraps)``: the channel with a palette colour span written
    around every quoted line a speech tag beside it names, and the wraps that
    were written (empty when nothing was mechanical, in which case the caller's
    own list comes back untouched).

    Why this is not the inference ADR-012 deleted: the speaker is a PALETTE
    display name in a tag ADJACENT to the quote — the model's own sentence says
    who is speaking, so the edit is transcription, not judgement
    (:func:`fabulist.state.attribution.mechanical_attribution_wraps`). A pronoun
    tag or an ambiguous lead-in resolves nobody and stays in the flag list for
    the model round, which is where the remaining judgement half belongs
    (W9.28 D3 / ADR-011).

    Advisory by construction, like the palette itself: a raising scan leaves the
    channel exactly as it was rather than failing the turn.
    """
    if not palette or not segments:
        return segments or [], []
    from fabulist.state.attribution import wrap_attributed_quotes

    try:
        return wrap_attributed_quotes(segments, palette)
    except Exception:  # noqa: BLE001 - advisory, never fatal
        logger.warning(
            "mechanical attribution repair failed; the flags stand", exc_info=True
        )
        return segments, []


def _narrate_flags(
    *,
    user_content: str,
    segments: list | None,
    validation_errors: list | None,
    prior_first_words: str | None,
    palette: Sequence[PaletteEntry] | None = None,
) -> list[dict]:
    """The deterministic flag bundle the narrate pass gates on (W9.24 §3.3.2).

    The SAME checks :func:`make_validate_node` folds post-commit — the Dress
    Rehearsal validator (parrot/slop/control/echo_start), the DSL gate's
    findings, the ``prose_markup`` detect and the two attribution folds
    (``attribution_missing`` / ``attribution_unquoted``) — computed BEFORE the
    commit so the loop repairs exactly what would otherwise route the turn into
    the critic. Zero model calls; a raising check leaves that half out
    (advisory, never fatal) rather than failing the turn.

    ``palette`` is the turn's colour palette (:func:`_palette_for_turn`), built
    once by the caller: the flags it produces change as patches land, the cast
    does not.
    """
    flags: list[dict] = []
    try:
        report = validate_narrative(
            user_content,
            segments or [],
            ValidatorConfig(prior_first_words=prior_first_words),
        )
        flags.extend(dataclasses.asdict(flag) for flag in report.flags)
    except Exception:  # noqa: BLE001 - advisory, never fatal
        logger.warning("narrate validate step failed; flags from it are absent", exc_info=True)
    flags.extend(dsl_error_flags(validation_errors or []))
    flags.extend(_prose_markup_flags(segments))
    flags.extend(_attribution_missing_flags(segments, palette))
    flags.extend(_attribution_unquoted_flags(segments, palette))
    return flags


def _dsl_findings(
    segments: list | None, draft_findings: list[dict] | None = None
) -> list[dict]:
    """The DSL findings that describe ``segments`` — re-read from the channel.

    The gate is pure, so this answers "what does the DSL channel hold NOW".
    ``draft_findings`` carries the DRAFT's findings forward where the re-read
    cannot reproduce them: a deterministic repair (kwargs→positional, a dropped
    dangling ref) is already baked into the text, so re-validating it reports
    nothing while the repair itself is still a true statement about the committed
    channel. Only the ``repaired`` (advisory) findings are carried; an unrepaired
    finding the loop fixed must NOT survive, or a valid channel stays badged.
    """
    findings = [dataclasses.asdict(finding) for finding in validate_dsl(segments).errors]
    seen = {(item.get("code"), item.get("statement")) for item in findings}
    for finding in draft_findings or ():
        if not finding.get("repaired"):
            continue
        key = (finding.get("code"), finding.get("statement"))
        if key in seen:
            continue
        seen.add(key)
        findings.append(dict(finding))
    return findings


def _dsl_problem_flags(flags: list[dict]) -> list[dict]:
    """The findings that came from the DSL gate (unrepaired = real problems).

    ``dsl_error_flags`` marks its deterministic repairs ``severity: "info"``; a
    ``warn`` is a statement the gate could not repair, i.e. a DSL problem the
    model must fix with :data:`PATCH_DSL_TOOL`.
    """
    return [
        flag
        for flag in flags
        if flag.get("check") == "dsl_validation" and flag.get("severity") != "info"
    ]


def _flags_payload(flags: list[dict]) -> dict:
    """The flags report the model reads back (tool result and flags pair)."""
    instruction = (
        f"The draft violates the narrator contract. Call {PATCH_PROSE_TOOL} with "
        "targeted find/replace edits that clear these findings; do not rewrite "
        "the turn. Each `find` must be copied verbatim from a finding's anchor "
        "(a leading/trailing `…` is a clip marker, not prose) or from the segment "
        "inventory a failed patch returns. An IDENTICAL retry will not change the "
        "outcome — re-issue with a DIFFERENT `find`, or reply with no tool call "
        "once the prose is clean."
    )
    if _dsl_problem_flags(flags):
        instruction += (
            f" The DSL findings are statements, not prose: repair them with "
            f"{PATCH_DSL_TOOL} (op `replace`/`delete`/`insert_after` on the named "
            "statement) — no `find`/`replace` edit can clear them."
        )
    return {"ok": False, "flags": _flag_lines(flags), "instruction": instruction}


#: Characters of each prose segment's head echoed by a failed patch's inventory.
SEGMENT_PREVIEW_CHARS = 120

#: Characters of an attempted ``find`` echoed back by a failed patch.
FIND_PREVIEW_CHARS = 160


def _preview(text: str, limit: int) -> str:
    """``text`` clipped to ``limit`` characters, ``…`` marking the cut."""
    return text if len(text) <= limit else text[:limit] + "…"


#: Characters of a flag's prose anchor kept in a validation notice row. The
#: anchor is teaching material for LATER turns, never a patch target (the
#: committed turn is never rewritten), so a clip is enough.
VALIDATION_NOTICE_ANCHOR_CHARS = FIND_PREVIEW_CHARS

#: The per-flag fields a validation notice carries — the SAME teaching payload
#: the narrator's repair loop reads (:func:`_flag_lines`): the finding's identity,
#: what it matched, the exact prose it anchors to and the corrective action.
NOTICE_FLAG_FIELDS = ("rule_id", "check", "severity", "message", "evidence", "fix")


def _notice_flags(flags: list[dict]) -> list[dict]:
    """Project validator flags onto the teaching payload a notice row carries.

    One compact dict per finding (non-empty string fields only), mirroring what
    :func:`_flag_lines` hands the repair loop so the post-commit notice and the
    pre-commit steering teach the same thing. The ``anchor`` is CLIPPED
    (:data:`VALIDATION_NOTICE_ANCHOR_CHARS`, ``…`` marking the cut): it names the
    prose that tripped the check, not a find/replace target.
    """
    notice: list[dict] = []
    for flag in flags:
        if not isinstance(flag, Mapping):
            continue
        row = {
            key: flag[key]
            for key in NOTICE_FLAG_FIELDS
            if isinstance(flag.get(key), str) and flag[key]
        }
        anchor = str(flag.get("anchor") or "").strip()
        if anchor:
            row["anchor"] = _preview(anchor, VALIDATION_NOTICE_ANCHOR_CHARS)
        notice.append(row)
    return notice


def _prose_inventory(segments: list | None) -> list[str]:
    """``"<index>: <head of the segment's text>"`` for every PROSE segment.

    The model patches WORKING-MEMORY segments, so it needs the real targets and
    their indices: a rejected ``find`` is almost always a near-miss on wording
    (live: a newline the prose never had).
    """
    inventory: list[str] = []
    for index, segment in enumerate(segments or ()):
        if not isinstance(segment, Mapping) or segment.get("kind") != "prose":
            continue
        text = segment.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        inventory.append(f"{index}: {_preview(text.strip(), SEGMENT_PREVIEW_CHARS)}")
    return inventory


def _patch_failure(
    segments: list | None,
    edits: list,
    failed_index: int | None,
    error: str,
    anchors: list[str],
    *,
    applied: int = 0,
    rejected: list[dict] | None = None,
    prose_head: str = "",
    elsewhere: tuple[int, str] | None = None,
    boundary: tuple[int, int] | None = None,
    boundary_anchors: tuple[str, str] | None = None,
) -> dict:
    """A rejected patch, TAUGHT: what was tried, what to aim at, what to do next.

    A bare ``not present in prose`` teaches nothing — the model retried the same
    ``find`` fourteen times in one live session. Every failure therefore returns
    the attempted ``find`` (clipped), the fact that prose is an ordered list of
    segments, the PROSE segment inventory (index + its first
    :data:`SEGMENT_PREVIEW_CHARS` characters), the flags' current anchors, and
    the directive to re-issue ONE corrected edit.

    Two more rules from that session: ``applied`` / ``rejected`` report the edits
    of THIS call that did land, so a model never re-issues a consumed ``find``;
    and the searched text is quoted as ``searched_prose_head`` — prose only.
    A ``find`` that is DSL source says so (``matches_segment``), quoting neither
    the DSL as prose nor the prose as the offender.
    """
    attempted = ""
    if failed_index is not None and 0 <= failed_index < len(edits):
        raw = edits[failed_index]
        if isinstance(raw, Mapping) and isinstance(raw.get("find"), str):
            attempted = raw["find"]
    payload: dict[str, Any] = {
        "ok": False,
        "error": error,
        "failed_index": failed_index,
        "applied": applied,
        "attempted_find": _preview(attempted, FIND_PREVIEW_CHARS),
        "searched_prose_head": prose_head,
        "prose_is_segmented": (
            "prose is an ordered list of segments; `find` must be a literal substring "
            "of ONE prose segment's text, character for character"
        ),
        "prose_segments": _prose_inventory(segments),
        "anchors": anchors,
        "fix": (
            "re-issue ONE corrected edit whose `find` is copied verbatim from a "
            "`prose_segments` entry above or from a finding's anchor (trim any "
            "trailing `…` clip marker); an identical retry will fail identically"
        ),
    }
    if rejected:
        payload["rejected"] = rejected
    if elsewhere is not None:
        payload["matches_segment"] = {"index": elsewhere[0], "kind": elsewhere[1]}
        payload["fix"] = (
            f"`find` is {elsewhere[1]} source, which this tool cannot edit — repair that "
            f"statement with {PATCH_DSL_TOOL}, and keep the prose edits"
        )
    if boundary is not None and boundary_anchors is not None:
        payload["straddles"] = list(boundary)
        payload["boundary_anchors"] = list(boundary_anchors)
    if applied:
        payload["partial"] = True
        payload["note"] = (
            f"{applied} edit(s) of this call were APPLIED and are already part of the "
            "draft — do not re-issue them (an applied `find` no longer matches); "
            "repair only the rejected edit(s), or reply with no tool call"
        )
    return payload


# -- patch_dsl: the DSL repair counterpart (statement-level) ------------------

#: Characters of each DSL statement's text echoed by the statement inventory.
DSL_STATEMENT_PREVIEW_CHARS = 120

#: ``ident =`` — the statement's LHS name. Deliberately looser than
#: :func:`fabulist.dsl.parse_statement` (which also needs ``Component(``): the
#: statement an op must target is often the MALFORMED one, and a broken statement
#: still has a name to repair it by.
_DSL_LHS_RE = re.compile(r"^\s*(?P<ident>[A-Za-z_]\w*)\s*=")


@dataclass(frozen=True)
class DslStatement:
    """One statement of the DSL channel: where it lives and what names it.

    ``segment_index`` / ``position`` locate it (the segment, and its index inside
    that segment's statement list); ``lhs`` is the assignment name — the handle
    an op targets — and ``text`` the statement exactly as it stands.
    """

    segment_index: int
    position: int
    lhs: str
    text: str


def _statement_lhs(text: str) -> str:
    """The ``ident`` left of ``=``, or ``""`` when the statement has none."""
    match = _DSL_LHS_RE.match(text)
    return match.group("ident") if match else ""


def _statement_key(text: str) -> str:
    """The whitespace-normalised form two statements are compared by."""
    return " ".join(text.split())


def _dsl_statements(segments: list | None) -> list[DslStatement]:
    """Every statement in the DSL channel, in channel order."""
    statements: list[DslStatement] = []
    for segment_index, segment in enumerate(segments or ()):
        if not isinstance(segment, Mapping) or segment.get("kind") != "dsl":
            continue
        text = segment.get("text")
        if not isinstance(text, str):
            continue
        for position, statement in enumerate(split_statements(text)):
            statements.append(
                DslStatement(segment_index, position, _statement_lhs(statement), statement)
            )
    return statements


def _dsl_inventory(segments: list | None) -> list[dict]:
    """``{index, lhs, statement}`` per channel statement — the real targets.

    The model patches STATEMENTS, not text, so a refusal hands it the whole
    inventory: ``lhs`` is the reliable handle and the clipped ``statement`` is
    the recogniser (a `target` must still be the complete statement).
    """
    return [
        {
            "index": index,
            "lhs": statement.lhs,
            "statement": _preview(statement.text, DSL_STATEMENT_PREVIEW_CHARS),
        }
        for index, statement in enumerate(_dsl_statements(segments))
    ]


def _dsl_target(
    statements: list[DslStatement], op: Mapping
) -> tuple[DslStatement | None, str]:
    """Resolve one op's target: ``(statement, "")`` or ``(None, reason)``.

    ``lhs`` is matched first (case-sensitive), then ``target`` — the exact
    statement text, whitespace-normalised. Two statements sharing a name are
    AMBIGUOUS: the op is refused and both channel indices are named in the reason,
    so the model can retarget one by its exact text instead of guessing.
    """
    lhs = op.get("lhs")
    target = op.get("target")
    indexed = list(enumerate(statements))
    if isinstance(lhs, str) and lhs.strip():
        matches = [(index, item) for index, item in indexed if item.lhs == lhs.strip()]
        if not matches:
            return (
                None,
                f"no DSL statement is named {lhs.strip()!r} (lhs names are "
                "case-sensitive; copy one from `dsl_statements`)",
            )
    elif isinstance(target, str) and target.strip():
        key = _statement_key(target)
        matches = [(index, item) for index, item in indexed if _statement_key(item.text) == key]
        if not matches:
            return (
                None,
                "no DSL statement matches `target` exactly (it is compared "
                "whitespace-normalised); target it by its `lhs` name or copy the whole "
                "statement from `dsl_statements`",
            )
    else:
        return None, "each op needs `lhs` (the statement's name) or `target` (its exact text)"
    if len(matches) > 1:
        return (
            None,
            f"{len(matches)} DSL statements are named {lhs.strip()!r} (statements "
            f"{', '.join(str(index) for index, _ in matches)}) — ambiguous, refusing; "
            "retarget ONE of them by its exact `target` text",
        )
    return matches[0][1], ""


def _apply_dsl_op(
    segments: list[dict], op: Mapping
) -> tuple[list[dict] | None, str]:
    """Apply ONE statement-level op; ``(segments, "")`` or ``(None, reason)``.

    A refusal returns ``None`` — the caller keeps its own copy, so a rejected op
    can never leave a half-applied or unparseable channel behind (the candidate
    is only kept when the DSL gate accepts it).

    The gate's own deterministic repairs are kept on success, exactly as the
    narrate node keeps them for the draft it validates: the channel the loop works
    on is always the one the post-commit gate would accept.
    """
    kind = op.get("op")
    if kind not in DSL_OPS:
        return None, f"`op` must be one of {', '.join(DSL_OPS)} (got {kind!r})"
    statements = _dsl_statements(segments)
    target, error = _dsl_target(statements, op)
    if target is None:
        return None, error

    new_statement = ""
    if kind != "delete":
        raw = op.get("statement")
        if not isinstance(raw, str) or not raw.strip():
            return None, f"`statement` must be the new statement text for `{kind}`"
        parts = split_statements(raw)
        if len(parts) != 1:
            return None, (
                "`statement` must be exactly ONE DSL statement "
                f"(`ident = Component(...)`); got {len(parts)}"
            )
        new_statement = parts[0]

    rebuilt = [
        item.text for item in statements if item.segment_index == target.segment_index
    ]
    if kind == "replace":
        rebuilt[target.position] = new_statement
    elif kind == "delete":
        rebuilt.pop(target.position)
    else:
        rebuilt.insert(target.position + 1, new_statement)

    candidate = [dict(segment) for segment in segments]
    if rebuilt:
        candidate[target.segment_index] = {
            **candidate[target.segment_index],
            "text": "\n".join(rebuilt),
        }
    else:
        # An emptied channel segment would be flagged `empty_statement` by the
        # gate, so a delete that clears the last statement drops the segment.
        candidate.pop(target.segment_index)

    report = validate_dsl(candidate)
    if not report.ok:
        return None, "the edited DSL would not validate: " + "; ".join(
            f"{finding.code}: {finding.message}"
            for finding in report.errors
            if not finding.repaired
        )
    return report.segments, ""


def _dsl_patch_failure(
    segments: list | None,
    ops: list,
    failed_index: int | None,
    error: str,
    *,
    applied: int = 0,
    rejected: list[dict] | None = None,
) -> dict:
    """A refused/partial DSL patch, TAUGHT like its prose sibling.

    Mirrors :func:`_patch_failure`: what was tried, the statement inventory the
    next op must aim at (index + ``lhs`` + opening text), how many ops of THIS
    call landed (never re-issue one of those), and the one directive that moves the
    model forward — plus the channel boundary the prose tool needs too, so a model
    that mixed the two tools is told which one owns which channel.
    """
    attempted: dict[str, Any] = {}
    if failed_index is not None and 0 <= failed_index < len(ops):
        raw = ops[failed_index]
        if isinstance(raw, Mapping):
            if isinstance(raw.get("op"), str):
                attempted["op"] = raw["op"]
            for key in ("lhs", "target", "statement"):
                value = raw.get(key)
                if isinstance(value, str):
                    attempted[key] = _preview(value, FIND_PREVIEW_CHARS)
    payload: dict[str, Any] = {
        "ok": False,
        "error": error,
        "failed_index": failed_index,
        "applied": applied,
        "attempted_op": attempted,
        "dsl_is_structured": (
            "the DSL channel is a list of `ident = Component(...)` statements: an op "
            "addresses ONE statement by its `lhs` name or its exact `target` text — "
            "never a find/replace inside the channel"
        ),
        "dsl_statements": _dsl_inventory(segments),
        "prose_is_elsewhere": (
            f"prose is repaired with {PATCH_PROSE_TOOL}; this tool never touches prose"
        ),
        "fix": (
            "re-issue ONE corrected op: `lhs` is a name from `dsl_statements`, "
            "`target` must be the WHOLE statement, and `statement` must be one "
            "complete `ident = Component(...)` line the DSL gate accepts — an "
            "identical retry will fail identically"
        ),
    }
    if rejected:
        payload["rejected"] = rejected
    if applied:
        payload["partial"] = True
        payload["note"] = (
            f"{applied} op(s) of this call were APPLIED and are already part of the "
            "draft — do not re-issue them; repair only the rejected op(s), or reply "
            "with no tool call"
        )
    return payload


async def _repair_narrative(
    *,
    model: ModelCaller,
    log: LogPort,
    state: TurnState,
    messages: list[dict],
    draft: str,
    segments: list[dict],
    capabilities: Mapping[str, bool] | None = None,
    timeout_s: float | None = None,
    window_fn: Callable[[], int | None] | None = None,
    return_thinking: bool | None = None,
    review_reasoning: str | None = None,
) -> NarrateRepair:
    """Draft → validate → targeted patch, in working memory (W9.24 §3.3).

    ``segments`` is the draft the node already streamed; nothing here writes the
    log except the runtime's own ``agent_loop`` trace rows (D9). The draft's
    message list is the loop's PREFIX — every iteration appends at the tail, so
    the stable prefix is byte-identical across iterations (AGENTS 6).

    Two hard rules the loop inherits from the owner rulings:

    * **NO iteration cap.** The model closes out; a model that keeps calling
      ``patch_prose`` / ``patch_dsl`` is its own problem and each call rides the
      connection timeout. The ENGINE never nags: the flags pair is written into
      the seeded tail ONCE (the draft nudge), after which residual findings ride
      the tool results — a completion with no tool call is the model's answer and
      ends the loop (fail-open to keep + flags, R2 — never a hang).
    * **Hang protection is per connection.** ``timeout_s`` is PROVENANCE handed
      to the runtime; the transport enforces the bound, never this function.

    A connection without function calling gets NO repair attempt: the draft is
    kept exactly as today (the clean-turn cost contract, §3.3.1) and the flags
    stay in the log for the critic/inspector. The runtime's own ``looped=False``
    degrade is for callers whose ANSWER is the tool call (BTS, W9.24 §3.4) —
    re-drafting a streamed turn is precisely the cost this epic retires.

    ``window_fn`` is the NARRATE slot's available prompt-token budget reader
    (W9.25 §3.5.3, :func:`fabulist.wiring.narrate_window_budget`), handed to the
    runtime so every ``agent_loop`` row carries passive ``headroom``. ``None``
    (no declared Context Size) keeps the row shape exactly as before, and the
    loop proceeds either way — headroom never stops a run.

    ``return_thinking`` is the narrate connection's "Return thinking" setting,
    handed to the runtime unchanged (``None`` = AUTO): the repair loop's
    assistant ``tool_calls`` messages then repeat a thinking model's own
    ``reasoning_content`` — required by DeepSeek's thinking mode after a tool
    call, invisible to a provider that never sent the field.

    ``review_reasoning`` is the ``reasoning_content`` the node accumulated while
    STREAMING the draft. The seeded flags pair is the engine continuing that same
    assistant turn, so its ``reasoning_content`` is the draft's own reasoning —
    the truthful value, never synthesized. ``None`` means the draft's response
    carried no such field at all (no field is sent); ``""`` means it carried an
    empty one, which is resent verbatim (DeepSeek 400s on the absence, accepts
    the empty value). DeepSeek validates EVERY assistant message that carries
    ``tool_calls`` (engine-authored ones included), so a flagged draft on a
    thinking connection 400s without it. It rides the SAME
    :func:`~fabulist.agents.loop.should_echo_reasoning` gate as the loop's own
    echo.
    """
    from .quality import PatchResult, apply_patch

    session_id = str(state.get("session_id") or "")
    prior = _prior_narratives(log, session_id) if session_id else []
    prior_prose = str(prior[-1].get("payload", {}).get("prose", "")) if prior else ""
    prior_first_words = " ".join(prior_prose.split()[:6]) or None
    # W9.28 §3.4: the palette is built ONCE for the whole repair run — the flags
    # that read it are re-derived after every patch, the cast is not.
    palette = _palette_for_turn(log, session_id)

    def _flags_for(working: list[dict]) -> list[dict]:
        # The DSL findings are RE-DERIVED from the working channel, never carried
        # over from the draft's: a landed `patch_dsl` op must clear the finding it
        # repaired, or the model keeps steering on a flag that no longer exists.
        # The segments handed in are already REPAIRED, so the draft's advisory
        # `repaired` notes are not re-announced to the model.
        return _narrate_flags(
            user_content=str(state.get("user_content") or ""),
            segments=working,
            validation_errors=_dsl_findings(working),
            prior_first_words=prior_first_words,
            palette=palette,
        )

    # MECHANICAL ATTRIBUTION REPAIR (issue 31): a quote the prose itself names a
    # speaker for is deterministic text editing, so the engine writes the colour
    # span NOW — before the loop decides whether a model round is needed at all.
    # A draft whose only findings were unwrapped-but-named lines therefore lands
    # with ZERO repair completions (live: 10+ flags and 40-100 s per round). The
    # flags are re-derived from the REPAIRED channel; the row below records what
    # the mechanical pass cleared.
    draft_segments = segments
    segments, mechanical = _mechanical_attribution_wraps(segments, palette)
    flags = _flags_for(segments)
    if mechanical:
        _log_narrate_repair(
            log,
            session_id,
            event="narrate_repair_mechanical",
            round_index=0,
            flags=_attribution_missing_flags(draft_segments, palette),
            applied=len(mechanical),
        )
    if not flags:
        return NarrateRepair(segments=segments, passes=0)
    # REPAIR OBSERVABILITY (owner 2026-10-21: "I need to know why they're
    # failing"): the draft's findings are otherwise handed ONLY to the model —
    # the post-commit `dsl_validation` row describes the FINAL channel, so a
    # draft that needed four rounds left no record of what was wrong with it.
    # One bounded row per engagement (and one per patch round, below) makes the
    # repair loop reconstructible from the log alone.
    _log_narrate_repair(
        log, session_id, event="narrate_repair_start", round_index=0, flags=flags
    )
    if not (capabilities or {}).get("function_calling"):
        # No function calling on this connection: keep the streamed draft.
        return NarrateRepair(segments=segments, passes=0)

    #: Working memory: the segments the loop patches. Nothing outside this
    #: closure sees a patched turn until the node returns it.
    working: dict[str, Any] = {"segments": [dict(segment) for segment in segments]}
    passes = 0
    base_label = _STAGE_SPECS["narrate"][0]
    writer = _stream_writer()

    async def patch_prose(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal passes
        edits = args.get("edits")
        if not isinstance(edits, list) or not edits:
            return {"ok": False, "error": "`edits` must be a non-empty list"}
        # ONE EDIT AT A TIME. ``apply_patch`` stays whole-patch atomic for the
        # post-commit critic, but this tool must never lose an edit whose `find`
        # IS prose because a sibling aimed at a channel it cannot edit: live
        # 98e092bf spent 158s re-issuing a three-edit patch whose two prose
        # edits were valid every time, each rejection blaming the DSL `find`.
        applied = 0
        rejected: list[dict[str, Any]] = []
        first_failure: PatchResult | None = None
        first_index: int | None = None
        for index, edit in enumerate(edits):
            result = apply_patch(working["segments"], [edit])
            if result.ok:
                working["segments"] = result.segments
                applied += 1
                continue
            if first_failure is None:
                first_failure, first_index = result, index
            find = edit.get("find") if isinstance(edit, Mapping) else None
            rejected.append(
                {
                    "index": index,
                    "error": result.error,
                    "attempted_find": _preview(
                        find if isinstance(find, str) else "", FIND_PREVIEW_CHARS
                    ),
                }
            )
        if applied:
            passes += 1
        if first_failure is None:
            remaining = _flags_for(working["segments"])
            _log_narrate_repair(
                log,
                session_id,
                event="narrate_repair_patch",
                round_index=passes,
                flags=remaining,
                applied=applied,
                tool=PATCH_PROSE_TOOL,
            )
            return {
                "ok": True,
                "applied": applied,
                "remaining_flags": _flag_lines(remaining),
            }
        # Partial or total rejection: the draft now holds every edit that DID
        # land, and the model reads why each other one did not (§3.3.4).
        _log_narrate_repair(
            log,
            session_id,
            event="narrate_repair_patch",
            round_index=passes,
            flags=_flags_for(working["segments"]),
            applied=applied,
            tool=PATCH_PROSE_TOOL,
            rejected=rejected,
        )
        return _patch_failure(
            working["segments"],
            edits,
            first_index,
            first_failure.error,
            anchors=[
                str(flag["anchor"])
                for flag in _flags_for(working["segments"])
                if flag.get("anchor")
            ],
            applied=applied,
            rejected=rejected,
            prose_head=first_failure.prose_head,
            elsewhere=first_failure.elsewhere,
            boundary=first_failure.boundary,
            boundary_anchors=first_failure.boundary_anchors,
        )

    tool = LoopTool(
        name=PATCH_PROSE_TOOL,
        description=_PATCH_PROSE_DESCRIPTION,
        parameters=_PATCH_PROSE_PARAMETERS,
        fn=patch_prose,
    )

    async def patch_dsl(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal passes
        ops = args.get("ops")
        if not isinstance(ops, list) or not ops:
            return {"ok": False, "error": "`ops` must be a non-empty list"}
        # INDEPENDENT ops, like the prose tool's edits: a valid statement op lands
        # even when a sibling names something the channel does not hold, and every
        # op that would break the DSL is rejected on its own (re-validated through
        # the gate) with the parse error as its reason.
        applied = 0
        rejected: list[dict[str, Any]] = []
        first_error = ""
        first_index: int | None = None
        for index, op in enumerate(ops):
            if isinstance(op, Mapping):
                candidate, error = _apply_dsl_op(working["segments"], op)
            else:
                candidate, error = None, "each op must be an object"
            if candidate is not None:
                working["segments"] = candidate
                applied += 1
                continue
            if first_index is None:
                first_index, first_error = index, error
            rejected.append(
                {
                    "index": index,
                    "op": op.get("op") if isinstance(op, Mapping) else None,
                    "error": error,
                }
            )
        if applied:
            passes += 1
        if first_index is None:
            remaining = _flags_for(working["segments"])
            _log_narrate_repair(
                log,
                session_id,
                event="narrate_repair_patch",
                round_index=passes,
                flags=remaining,
                applied=applied,
                tool=PATCH_DSL_TOOL,
            )
            return {
                "ok": True,
                "applied": applied,
                "remaining_flags": _flag_lines(remaining),
            }
        _log_narrate_repair(
            log,
            session_id,
            event="narrate_repair_patch",
            round_index=passes,
            flags=_flags_for(working["segments"]),
            applied=applied,
            tool=PATCH_DSL_TOOL,
            rejected=rejected,
        )
        return _dsl_patch_failure(
            working["segments"],
            ops,
            first_index,
            first_error,
            applied=applied,
            rejected=rejected,
        )

    dsl_tool = LoopTool(
        name=PATCH_DSL_TOOL,
        description=_PATCH_DSL_DESCRIPTION,
        parameters=_PATCH_DSL_PARAMETERS,
        fn=patch_dsl,
    )

    # Engage: the draft the node STREAMED rides its own assistant turn, followed
    # by the synthetic flags pair (the pinned §3.3.2 shape — never a bare tool
    # message; the pair's builder lives in the runtime so this node cannot drift
    # from it). The draft is NOT re-generated: it is already the turn's prose.
    seeded = [
        *messages,
        *_injection_messages(
            draft,
            LoopInjection(content=json.dumps(_flags_payload(flags), ensure_ascii=False)),
            reasoning=_echoed_reasoning(return_thinking, review_reasoning),
        ),
    ]
    # D42: the repair work annotates the narrate SLOT (same index/total — progress
    # never regresses and the stage total stays 8), never a stage of its own.
    _emit_stage(writer, "narrate", "start", label=f"{base_label} (repair 1)")
    try:
        await run_agent_loop(
            caller=model,
            messages=seeded,
            tools=[tool, dsl_tool],
            surface="narrate",
            log=log,
            session_id=session_id,
            timeout_s=timeout_s,
            capabilities=capabilities,
            window_fn=window_fn,
            return_thinking=return_thinking,
        )
    finally:
        _emit_stage(
            writer, "narrate", "done", label=f"{base_label} (repair {max(1, passes)})"
        )

    final = working["segments"]
    return NarrateRepair(segments=final, passes=passes)


def _final_narrative_text(segments: list[dict]) -> str:
    """The committed turn as one stream: segments in document order.

    Blank-line separated because a prose segment is a block and each dsl segment
    is a single statement — the shape the client's ``splitProseAndDsl`` re-splits
    into the same segments (it reads line by line, so the extra blank lines are
    separators, never content).
    """
    return "\n\n".join(segment["text"] for segment in segments)


def _segments_prose(segments: list[dict] | None) -> str:
    """The prose channel of a payload: prose segments joined in document order.

    The DSL gate's prose-invariance guard compares this BEFORE and AFTER the
    last-resort re-synthesis: prose is already on screen and is never the
    replacement's to touch.
    """
    return "\n".join(
        str(segment.get("text") or "")
        for segment in segments or []
        if isinstance(segment, Mapping) and segment.get("kind") == "prose"
    )


def _same_streamed_text(draft: str, final: str) -> bool:
    """True when the streamed draft and the final text agree modulo whitespace.

    The replacement text re-joins segments with blank lines, so byte equality is
    the wrong test: a CLEAN turn (nothing rewritten) must never put a frame on
    the wire — its one-completion contract is pinned. Whitespace-normalized
    comparison keeps that true while still firing on every rewrite, because the
    gate and the repair loop change words, never just spacing.
    """
    return " ".join(draft.split()) == " ".join(final.split())


def make_narrator_node(
    *,
    model: ModelCaller,
    build_messages: Callable[[TurnState], list[dict]] = default_narrator_messages,
    resynthesize=None,
    log: LogPort | None = None,
    loop_capabilities: Any = None,
    loop_timeout_s: Any = None,
    loop_window_fn: Any = None,
    loop_return_thinking: Any = None,
    provider_label_fn: Callable[[], str] | None = None,
):
    """Build the ``narrator`` node: stream the model, accumulate prose + DSL.

    ``resynthesize`` overrides the last-resort DSL repair seam (tests inject a
    fake); by default the node re-synthesizes through ``model`` itself. The
    deterministic gate repairs kwargs/dangling refs with zero model calls, so
    the seam only fires for genuinely unrecoverable DSL.

    ``log`` enables the W9.24 repair loop: after the draft it runs the
    deterministic validate bundle engine-side and, when the draft is flagged,
    drives :func:`_repair_narrative` in working memory (the loop needs the log
    for the prior prose the ``echo_start`` check compares against and for its
    ``agent_loop`` trace rows). Without a log — direct node calls in tests — the
    node is the bare draft generator it always was.

    ``loop_capabilities`` is the connection's capability map (or a zero-arg
    callable returning it, so a re-bound connection is honoured per turn) and
    ``loop_timeout_s`` the transport bound its calls ride, carried on the
    ``agent_loop`` rows as provenance only. ``loop_window_fn`` is the same seam
    for the NARRATE slot's available prompt-token budget: a zero-arg reader
    (``int`` or ``None``) that puts passive ``headroom`` on the loop's rows
    without ever stopping it (W9.25 §3.5.3). ``loop_return_thinking`` is the
    connection's reasoning-echo setting, resolved the same way (``None`` = AUTO).

    ``provider_label_fn`` is the live reader of the model this phase's calls are
    bound to, read when a provider failure HAPPENS (never at build time and never
    once per run): the abort message must name the model that refused, so a
    rebind applies to the label on the same turn it applies to the call.

    A PROVIDER failure anywhere in the phase — the draft, the DSL re-synthesis,
    a repair-loop iteration, or the transport while the request is being built —
    is FATAL and is raised as the phase error
    :mod:`fabulist.graph.provider_errors` builds ("Narration failed: 429 from
    glm-5.3 — …"), so the turn aborts on the engine's error frame naming the
    phase and the model instead of the vendor's raw text. The narrate phase is
    pre-commit (``commit`` is the NEXT node), so aborting costs nothing that was
    kept (ADR-023). Content failures are untouched: they keep today's behaviour.
    """

    async def _draft(state: TurnState) -> dict:
        writer = _stream_writer()
        raw: list[str] = []
        #: The draft's own ``reasoning_content`` (a thinking model's deltas). It
        #: rides the seeded repair pair — DeepSeek validates every assistant
        #: message carrying ``tool_calls`` — and is never synthesized. ``None``
        #: when the response carried no such field; ``""`` when it carried an
        #: empty one (resent verbatim: the vendor 400s on the absence).
        reasoning: list[str] = []
        reasoning_present = False
        outbox: dict = {}

        messages = build_messages(state, out=outbox) if _builder_takes_out(build_messages) else build_messages(state)
        _emit_stage(writer, "narrate", "start")
        async for event in model(messages, True):
            if writer is not None:
                writer(event)
            if event.get("type") == "content":
                # Every content delta, regardless of channel, in arrival order:
                # the prose/dsl interleaving is only recoverable from the raw.
                raw.append(event.get("delta", ""))
            elif event.get("type") == "reasoning":
                reasoning.append(event.get("delta", ""))
                reasoning_present = True
        _emit_stage(writer, "narrate", "done")
        # Issue #44: the reply is parsed WHOLE. There is no prefill to strip and
        # no reply-side byte surgery: the per-turn envelope rides ONE ephemeral
        # ``assistant`` message inserted immediately BEFORE the final user
        # message (``prompts.envelope.insert_engine_envelope``), so the served
        # list ends on the player's turn and the model's reply is its own text.
        joined = "".join(raw)
        # Segmentation keeps every byte (D80 removed the markup strip); the
        # ``prose_markup`` flag reads the committed SEGMENTS, so a repair clears
        # it — see :func:`_prose_markup_flags`.
        segments = segment_narrative(joined)
        # Narrator-side DSL validation gate (W2.4b): deterministic repair
        # (kwargs→positional, dangling-ref drop + flag) first; the model
        # re-synthesis pass only runs when that cannot produce a valid
        # composition (one attempt, capped by revalidate_with_retry). Never
        # blocks the turn: an unrecoverable statement is KEPT VERBATIM as a dsl
        # segment (keep + tag) and its findings ride the state channel so
        # post_turn can log them AND surface them as inspector flags anchored to
        # the segment (D9: advisory, non-destructive — no silent drop).
        _emit_stage(writer, "dsl", "start")
        draft_report = validate_dsl(segments)
        report = draft_report
        resynth_note: str | None = None
        kept_draft = False
        if not draft_report.ok:
            gate_errors = [dataclasses.asdict(error) for error in draft_report.errors]
            synth = resynthesize or _model_resynthesizer(model, segments)
            started = time.monotonic()
            report = await revalidate_with_retry(segments, synth)
            call_latency_s = time.monotonic() - started
            # PROSE-INVARIANCE GUARD: the gate may never rewrite the turn's
            # prose — the draft is already on screen and a re-generated prose
            # re-lays-out the whole reply. A replacement seam that changed the
            # prose is discarded and the draft report stands (tagged in state).
            kept_draft = report.resynth_discarded or not _same_streamed_text(
                _segments_prose(segments), _segments_prose(report.segments)
            )
            if kept_draft:
                report = dataclasses.replace(draft_report, retry_applied=True)
            _log_dsl_resynthesis(
                log,
                state.get("session_id"),
                gate_errors,
                call_latency_s,
                kept_draft=kept_draft,
            )
            resynth_note = (
                "DSL re-synthesis kept the draft (replacement discarded)"
                if kept_draft
                else "DSL re-synthesized (one model pass)"
            )
        # Validator notes attach at validation time (D42): the gate's findings
        # ride the step's `done` frame, so the header summary explains a repair
        # or a kept-and-tagged failure without a second request. Empty on a
        # clean composition; the re-synthesis note names its own outcome.
        notes = _validator_notes(dsl_error_flags(report.errors))
        if resynth_note is not None:
            notes.append(resynth_note)
        _emit_stage(writer, "dsl", "done", notes=notes)
        # The report payload is authoritative and lossless — every segment is kept
        # (valid, repaired, or kept verbatim on failure). No filtering.
        segments = report.segments
        validation_errors = [dataclasses.asdict(e) for e in report.errors]
        # W9.24 §3.3: draft → validate → targeted patch, engine-forced and
        # model-free until a flag says otherwise. A clean draft returns as-is
        # (ONE completion for the whole turn — the pinned clean-turn cost); a
        # flagged draft is repaired in WORKING MEMORY here, and only the final
        # text reaches the commit node. Nothing in this path writes the log but
        # the runtime's own ``agent_loop`` trace.
        if log is not None:
            repaired = await _repair_narrative(
                model=model,
                log=log,
                state=state,
                messages=messages,
                draft=joined,
                segments=segments,
                capabilities=_seam_value(loop_capabilities),
                timeout_s=_seam_value(loop_timeout_s),
                # NOT ``_seam_value``: the window seam IS a zero-arg reader by
                # contract (the loop calls it before every iteration), so it is
                # handed through unwrapped.
                window_fn=loop_window_fn,
                return_thinking=_seam_value(loop_return_thinking),
                review_reasoning=(
                    "".join(reasoning) if reasoning_present else None
                ),
            )
            segments = repaired.segments
            # The state's DSL findings describe the COMMITTED channel: a loop that
            # repaired a statement with ``patch_dsl`` must not leave the draft's
            # finding behind for the post-commit validate node to log against a
            # channel that is now valid (while the repairs the engine itself
            # applied stay on the record).
            validation_errors = _dsl_findings(segments, validation_errors)
        # The browser has been accumulating the DRAFT: the gate and the repair
        # loop rewrite the turn in working memory, so when the committed text
        # differs the client would keep showing the stale, partly-invalid draft
        # until a post-done projection read. ONE full-text content frame with
        # ``meta.replace`` makes it reset the accumulator and re-split (the
        # existing content channel — no new frame type). ONE comparison for BOTH
        # fix kinds, after every rewrite is final.
        final_text = _final_narrative_text(segments)
        if writer is not None and not _same_streamed_text(joined, final_text):
            writer({"type": "content", "delta": final_text, "meta": {"replace": True}})
        prose_concat = "\n".join(s["text"] for s in segments if s["kind"] == "prose")
        dsl_concat = "\n".join(s["text"] for s in segments if s["kind"] == "dsl")
        update = {
            "narrative_parts": [prose_concat] if prose_concat else [],
            "dsl": dsl_concat,
            "segments": segments,
            # Issue #42: the draft completion's own ``reasoning_content`` (the
            # completion that produced this prose) rides to the commit node.
            # ``None`` = the provider never sent the field; ``""`` = it sent an
            # empty one, which is still a PRESENT field on the wire.
            "reasoning": "".join(reasoning) if reasoning_present else None,
            "validation_errors": validation_errors,
            # The DSL gate's last-resort pass, carried on the state's existing
            # ``validation`` record so a checkpoint (and the post-turn validate
            # node) can say whether a model re-synthesis ran and whether its
            # replacement was discarded in favour of the draft.
            "validation": {
                "retry_applied": bool(report.retry_applied),
                "resynth_kept_draft": bool(kept_draft),
            },
        }
        if outbox.get("facts_hash"):
            update["facts_hash"] = outbox["facts_hash"]
        if "recall" in outbox:
            # recorded recall text rides the narrative meta for byte-faithful
            # replay ("" = genuinely no recall this turn)
            update["recall_text"] = outbox["recall"]
        # D25 C4: recall_queries/recall_config ride STATE (the engine unpacks
        # RecallResult before the graph runs); do NOT stamp them from the
        # outbox here — post_turn reads them from state. (An outbox stamp
        # would clobber the engine's values with builder-side empties.)
        if "facts_text" in outbox:
            # Inspector I2: the injected facts TEXT (not just the hash).
            update["facts_text"] = outbox["facts_text"]
        if "regions" in outbox:
            # Inspector I2: per-region prompt sizes for the breakdown bars.
            update["regions"] = outbox["regions"]
        return update

    async def narrator(state: TurnState) -> dict:
        """Draft the turn, classifying a provider failure as this phase's abort."""
        try:
            return await _draft(state)
        except Exception as exc:  # noqa: BLE001 - re-raised below unless provider-class
            # Read HERE, at the failure: the label names the model the call that
            # just failed resolved to (a rebind since the graph was built, or
            # since the turn started, is already in effect).
            _raise_if_provider_failure(
                "Narration", exc, _provider_label(provider_label_fn)
            )
            raise

    return narrator


def _builder_takes_out(build_messages) -> bool:  # type: ignore[no-untyped-def]
    """True when the message builder accepts an ``out`` provenance dict."""
    import inspect

    try:
        params = inspect.signature(build_messages).parameters
    except (TypeError, ValueError):
        return False
    return "out" in params


# -- post_turn ---------------------------------------------------------------


#: How many preceding turns' prose the critic sees (a "short tail", D49).
#: Enough to spot repetition of the last beat; deliberately not the whole log.
CRITIC_PRIOR_TURNS = 2

#: Header stamped on the cast-context block prepended to the extractor/BTS turn
#: text when a studio cast change is in effect this turn (D51). It exists only
#: in that case: with no notice the agents' input is byte-identical to before.
CAST_CONTEXT_HEADER = "CAST CONTEXT (studio cast change this turn):"


def _cast_agent_context(log: LogPort, session_id: str) -> str | None:
    """Card context + cast-change notice for the extractor/BTS turn text (D51).

    The extractor and BTS see only ``(user_msg, prose[, prior_state])`` — no
    session card context and no prompt region — so a Design Studio cast edit
    (surfaced to the narrator as a dynamic notice via
    :func:`fabulist.prompts.cast.cast_delta_notice`) is invisible to them and
    they cannot reason about the changed cast. This assembles that context from
    the log: the CURRENT cast-change notice PLUS the session's card context
    (name / Description / Personality / Scenario, rendered through the same
    ``roster.persona_block`` the narrator's static prefix uses).

    Returns ``None`` — leaving the agent inputs byte-identical to before — when
    there is no cast-change notice. That is the common case: a plain card chat
    (no ``world_ref`` -> the session's own cards ARE its definition) or a world
    chat whose definition still matches its snapshot. Injection is therefore
    additive and inert until an edit actually lands.

    The block is fed into the ``user_msg`` text the caller passes to the
    extractor/BTS ONLY. ``state["user_content"]``, the persisted ``user`` event,
    the validator, the critic, attribution and the narrator prompt path are
    untouched — so the narrated prompt hash and replay stay byte-faithful.
    """
    try:
        from fabulist.prompts.cast import cast_delta_notice

        notice = cast_delta_notice(log, session_id)
    except Exception:  # noqa: BLE001 - the notice is advisory, never fatal
        return None
    if not notice:
        return None

    card_context = ""
    try:
        from fabulist.context.roster import extract_cards, persona_block

        events = log.get_chain_events(session_id)
        # ``first_mes=[]`` suppresses the opening-scene line(s): the scene is
        # already in the prose; the agents need the actor definitions.
        card_context = persona_block(extract_cards(events), []) or ""
    except Exception:  # noqa: BLE001 - context is advisory, never fatal
        card_context = ""

    parts = [notice]
    if card_context:
        parts.append("SESSION CARDS:\n" + card_context)
    # W7: hand the agents the canonical roster names as well, so a rename or a
    # new member resolves against the same identities the engine uses.
    try:
        from fabulist.state.cast import roster

        names = [member.display_name for member in roster(log, session_id)]
        if names:
            parts.append("CAST ROSTER:\n" + "\n".join(f"- {name}" for name in names))
    except Exception:  # noqa: BLE001 - roster context is advisory
        pass
    return "\n\n".join(parts)


async def _enrich_genre(
    decision: Any | None,
    deltas: dict,
    user_msg: str,
    prose: str,
    prior_narratives: list[dict],
    *,
    log: LogPort,
    session_id: str,
) -> None:
    """Give the d1-3b genre its home: the scene's ``genre`` trait (R8).

    Both post-turn paths call this (the BTS path owns its state-delta, the
    extractor path its own), so the derivation happens whatever engine wiring is
    active. Rules: never clobber another owner (the extractor/BTS may have set a
    genre), never fatal (advisory), and the caller already refuses ``other`` and
    answers below :data:`fabulist.decision.GENRE_MIN_CONFIDENCE`.

    W9.32 R8 + §6: genre is a SCENE property, so the derived value is written
    through the store as the scene's ``genre`` trait
    (:func:`_record_scene_genre`) — the trait is the fact's ONLY home. The flat
    ``genre`` key this path used to also stamp into ``deltas`` is retired (W11):
    :func:`fabulist.state.entities.rendered_state` derives the flat key from the
    trait, and falls back to a legacy log's own flat value.
    """
    if decision is None or deltas.get("genre"):
        return
    try:
        genre = await decision.classify_genre(
            _genre_state(user_msg, prose, prior_narratives, deltas)
        )
    except Exception:  # noqa: BLE001 - advisory, never fatal
        genre = None
    if genre:
        _record_scene_genre(log, session_id, genre)


def _record_scene_genre(log: LogPort, session_id: str, genre: str) -> None:
    """Write the derived genre as the scene's trait unless one is already set (R8).

    The scene trait is the ONE home (W9.32 §5 ownership map): the tracker may set
    it with ``set_attr``, the planner and the owner's ✎ door may set it, and the
    derivation only speaks when nobody has — a scene whose genre the tracker
    already decided must not be rewritten by a decision-model side effect on every
    turn (the pre-W9.32 drift this requirement exists to stop). The ``actor`` on
    the row is ``decision``, so the log says who derived it.

    Advisory like its caller: a store that refuses (or a log that cannot fold)
    leaves the scene without a derived genre and the next turn trying again — the
    flat key is a read of this trait now, so there is no second copy to keep in
    step.
    """
    try:
        from fabulist.state import entities

        current = entities.scene_traits(entities.registry(log, session_id)).get(
            "genre", ""
        )
        if current:
            return
        entities.set_attribute(
            log, session_id, entities.SCENE, key="genre", value=genre, actor="decision"
        )
    except Exception:  # noqa: BLE001 - advisory, never fatal
        pass


def _genre_state(
    user_msg: str, prose: str, prior_narratives: list[dict], fold: dict
) -> str:
    """The decision model's view for genre classification.

    Genre is a SCENE property, not a per-turn one: one turn's prose alone reads
    ambiguously (a lamp scene answered ``slice-of-life`` 0.38 with no context;
    with the scene line + the prior turn it answered ``fantasy`` 0.68 — live
    probe 2026-10-13). So the state carries the scene line (time/loc/tone), the
    prior prose tail, and this turn — the same shape the spike's validated
    genre probe used.
    """
    parts: list[str] = []
    scene = " | ".join(
        f"{key}: {fold[key]}" for key in ("time", "loc", "tone") if fold.get(key)
    )
    if scene:
        parts.append(f"<scene_line>{scene}</scene_line>")
    prior = [
        str(n.get("payload", {}).get("prose", ""))
        for n in prior_narratives[-CRITIC_PRIOR_TURNS:]
    ]
    prior = [text for text in prior if text]
    if prior:
        parts.append("<prior>\n" + "\n\n".join(prior) + "\n</prior>")
    parts.append(f"<user_message>{user_msg}</user_message>")
    parts.append(f"<prose>{prose}</prose>")
    return "\n".join(parts)


# --- d1-3b per-turn decision gate -------------------------------------------
# Batched calls carry the OWNER-EDITABLE question bank (the artifact-derived
# slop checks + narrative movement by default; split at 30 questions per POST —
# issue #50; :mod:`fabulist.decision_queries`).
# Answers are mapped to ADVISORY critic flags (they only ADD evidence — the
# critic SKIP pre-gate stays dead). Every failure is fail-open.

#: Flag label for a slop phrase check (parity: one label for the whole bank).
SLOP_FLAG_CHECK = "d1-slop"
#: Flag label for the built-in narrative-movement question (parity with the
#: pre-asset flags; the question id keeps its own name).
MOVEMENT_FLAG_CHECK = "d1-movement"
#: Flag label for any other owner-defined question.
QUESTION_FLAG_CHECK = "d1-question"


def _noul_probability(answer: Any) -> float | None:
    """P(true) from one ``noul`` answer, or ``None`` when missing/ill-typed."""
    if not isinstance(answer, Mapping):
        return None
    value = answer.get("noul")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _flag_check(question: dict) -> str:
    """The ``check`` label for one fired question (pre-asset labels for parity)."""
    if question.get("source") == "slop":
        return SLOP_FLAG_CHECK
    if question.get("id") == MOVEMENT_QUESTION_ID:
        return MOVEMENT_FLAG_CHECK
    return QUESTION_FLAG_CHECK


def _flag_evidence(question: dict, probability: float) -> str:
    """The machine-ish ``evidence`` string (pre-asset wording for parity)."""
    phrase = question.get("phrase")
    if isinstance(phrase, str) and phrase:
        return f"phrase '{phrase}' P={probability}"
    if question.get("id") == MOVEMENT_QUESTION_ID:
        return f"stalled turn: P(advancing)={probability}"
    return f"question '{question['id']}' P={probability}"


def turn_gate_flags(
    answers: Mapping[str, Any], questions: list[dict] | None = None
) -> list[dict]:
    """Map the batched turn-gate answers to advisory critic flags.

    One flag per FIRED ``noul`` question, in bank order, in the pre-R25 shape
    (``check`` / ``severity`` / ``evidence``) plus ``question_id`` and ``issue``
    — the question's own id and its human-readable issue, so the critic acts on
    a precise statement ("no bananas in the scene") rather than on a slug. The
    fire rule and the issue text come from :mod:`fabulist.decision_queries`
    (``polarity`` + ``threshold`` per question); a missing or ill-typed answer,
    a disabled row, and any non-``noul`` question contribute nothing (fail-open).
    ``questions`` defaults to the seeded bank (:data:`DEFAULT_QUESTIONS`).

    Only PER-TURN rows can flag: this gate asks nothing else, so an answer to a row
    another gate owns (``section: "proposal"``) can never mint a flag here
    (``questions_for_section``).
    """
    rows = (
        questions_for_section(None, PER_TURN_SECTION) if questions is None else questions
    )
    flags: list[dict] = []
    for question in rows:
        if question_section(question) != PER_TURN_SECTION:
            continue
        if question.get("type") != "noul" or not question.get("enabled", True):
            continue
        probability = _noul_probability(answers.get(question["id"]))
        if probability is None or not question_fires(question, probability):
            continue
        flags.append(
            {
                "check": _flag_check(question),
                "severity": "warn",
                "evidence": _flag_evidence(question, probability),
                "question_id": question["id"],
                "issue": flag_issue(question),
            }
        )
    return flags


async def _run_turn_gate(
    decision: Any | None,
    user_msg: str,
    prose: str,
    scene_line: str,
    log: Any | None = None,
) -> list[dict]:
    """The d1-3b per-turn ask(s) -> advisory critic flags (issue #50 chunking).

    The bank is read from the log-owned asset, sliced to THIS gate's rows
    (:func:`questions_for_section` over :data:`PER_TURN_SECTION`) — the owner's
    per-turn questions, or the artifact-seeded bank when the asset is
    absent/empty. WHY the slice: the same bank also carries the W9.22 proposal
    rows, and this gate must not pay for (or flag from) a judgment another gate
    consumes.

    The canonical list can exceed what one POST fits, so rows are split into
    batches of at most :data:`TURN_GATE_CHUNK_SIZE` (30) and each batch is one
    ``ask``; the answer maps are merged, and flag order stays bank order. A
    single chunk reproduces the pre-#50 one-POST behaviour exactly. Never fatal
    and never a gate: a disabled caller, any error, or a response without
    usable answers contributes nothing.
    """
    if decision is None or not getattr(decision, "enabled", False):
        return []
    questions = questions_for_section(log, PER_TURN_SECTION)
    state = turn_gate_state(user_msg, prose, scene_line)
    flags: list[dict] = []
    for batch in chunk_questions(questions, TURN_GATE_CHUNK_SIZE):
        payload = question_payloads(batch)
        if not payload:
            continue
        try:
            answers = await decision.ask(state, payload)
        except Exception:  # noqa: BLE001 - the gate is advisory, never fatal
            continue
        if not isinstance(answers, Mapping):
            continue
        flags.extend(turn_gate_flags(answers, batch))
    return flags


# -- post-turn stages (W9.6) -------------------------------------------------
#
# The old single ``post_turn`` node is split into the per-stage factories below
# so the turn runs as real LangGraph nodes (``build_graph``) with an
# event-for-event identical log. The code inside each factory is the pre-split
# code, moved; the only structural change is that the critic's verdict left the
# node as a conditional edge, so state — not a local variable — carries the
# final prose/segments the downstream stages must describe. Since W9.24 the
# critic's ``fail`` arm no longer has an edge at all: the narrator repairs in
# its own node BEFORE the commit, and a post-commit ``fail`` keeps the turn.

#: State update for a critic that could not produce a trusted verdict (the call
#: raised or the reply did not parse). The turn is kept, exactly as a ``fail``
#: keeps it. Nothing in the critic path is fatal (D49).
_NO_VERDICT: dict = {"critic_verdict": {}}


def _prior_narratives(
    log: LogPort, session_id: str, *, exclude_id: Any | None = None
) -> list[dict]:
    """Projection narratives the post-turn agents may read (fork-aware, F2).

    A generated image is appended as a ``narrative`` row (imagegen) but is a
    canon image carrying no prose, not a turn. Filter it ONCE here so neither
    the critic's ``prior_prose`` nor :func:`_genre_state`'s
    ``[-CRITIC_PRIOR_TURNS:]`` tail ever reads an image row.

    ``exclude_id`` drops THIS turn's committed narrative: the stages that used
    to run inside one node read the projection BEFORE the narrative row existed,
    so the split must reproduce that view explicitly.
    """
    from fabulist.media.images import is_image_narrative

    items = log.get_projection(session_id).get("narratives", [])
    return [
        item
        for item in items
        if not is_image_narrative(item)
        and (exclude_id is None or item.get("id") != exclude_id)
    ]


def _narrative_event_ref(event: Any) -> dict:
    """JSON-safe reference to a committed narrative event (id + seq + payload).

    Downstream nodes read the committed turn from state instead of recomputing
    it (D9/D11): the critic's supersede target is the id, the planner's
    ``turn_seq`` is the seq, and the revision's base payload is the payload.
    """
    return {
        "id": getattr(event, "id", None),
        "seq": getattr(event, "seq", 0),
        "payload": getattr(event, "payload", {}) or {},
    }


#: ``system`` payload ``event`` name for a turn that produced no narrative and
#: was therefore NOT committed (issue #46). A system variant of the open
#: ``system`` event: the transcript renders narrative/user rows, so the turn
#: leaves no bubble, while the log still records that the attempt happened and
#: why it was refused (ADR-001: the log owns everything).
TURN_ABORTED_EVENT = "turn_aborted"

#: ``reason`` on :data:`TURN_ABORTED_EVENT` (and on the engine's error frame)
#: when the narrator produced neither prose nor DSL.
EMPTY_CONTENT_REASON = "empty_content"


class EmptyTurnAbort(RuntimeError):
    """Raised from ``commit`` when the narrative payload is empty (issue #46).

    The turn is REFUSED, not rewritten: the user message and any usage/trace
    rows already appended stay (append-only), no ``narrative`` row lands, and
    the tail nodes never run. The engine surfaces it through the SAME error
    frame path as an ADR-023 provider abort, so the client sees one failure and
    the next ``POST /turns`` proceeds normally.
    """

    #: Stable machine reason carried on the engine's error frame.
    reason = EMPTY_CONTENT_REASON

    def __init__(self) -> None:
        super().__init__(
            "turn produced no narrative (empty prose and DSL) — nothing was committed"
        )


def _payload_is_empty(state: TurnState) -> bool:
    """Whether the turn carries no narrative content at all (issue #46).

    EMPTY means neither channel has a non-whitespace character:

    * ``narrative_parts`` joined is blank, AND
    * ``dsl`` is blank, AND
    * no segment carries non-whitespace text.

    The segment check is the load-bearing one: a reasoning-only completion can
    still produce a blank prose segment, and a whitespace-only draft must never
    pass as a turn. A DSL-only turn is NOT empty (the interweave contract allows
    a composition with no prose), and an image narrative never reaches this
    seam (imagegen appends its canon row directly).
    """
    prose = "".join(state.get("narrative_parts") or [])
    # Issue #45 composition: judge emptiness on the STRIPPED prose — a reply
    # that is nothing but a leaked engine wrapper tag strips to empty here
    # and must abort, not commit through _commit_narrative's guard as an
    # empty row (the strip there is the same function, idempotent).
    prose, _leaked = _strip_engine_wrapper_leak(prose)
    dsl = state.get("dsl") or ""
    if str(prose).strip() or str(dsl).strip():
        return False
    segments = state.get("segments") or []
    for segment in segments:
        text = segment.get("text") if isinstance(segment, Mapping) else None
        if str(text or "").strip():
            return False
    return True


def _append_turn_aborted(log: LogPort, state: TurnState) -> None:
    """Append the explicit ``turn_aborted`` row for an empty completion (#46).

    Best-effort: a log that refuses the diagnostic row must still refuse the
    empty narrative (the guard is the refusal, not the audit line). The row
    carries only counts, never the reasoning text — it is a diagnosis, not a
    second copy of the turn.
    """
    prose = "".join(state.get("narrative_parts") or [])
    dsl = state.get("dsl") or ""
    segments = state.get("segments") or []
    reasoning = state.get("reasoning")
    payload: dict[str, Any] = {
        "event": TURN_ABORTED_EVENT,
        "stage": "commit",
        "reason": EMPTY_CONTENT_REASON,
        "detail": "no prose and no DSL — the turn was not committed",
        "prose_chars": len(str(prose)),
        "dsl_chars": len(str(dsl)),
        "segments": len(segments),
    }
    if reasoning is not None:
        payload["reasoning_chars"] = len(str(reasoning))
    try:
        log.append_event(state["session_id"], "system", "engine", payload)
    except Exception:  # noqa: BLE001 - the refusal must survive an audit failure
        logger.exception("could not append %s row", TURN_ABORTED_EVENT)
def _engine_wrapper_tags() -> dict[str, str]:
    """The engine's closed tag family as ``open -> close`` (issue #45).

    Built from the constants of the modules that OWN each tag — the two trust
    bounds (``prompts.wrapper``: ``<preset_def>`` / ``<preset_runtime>``) plus
    the envelope's closed tag family (``prompts.envelope``: state/notice/
    memory/lane/steering/palette). Deliberately NOT a second literal list: a
    rename or a new block updates one module, never a guard that drifts.
    """
    from fabulist.prompts import envelope, wrapper

    return {
        wrapper.DEF_OPEN: wrapper.DEF_CLOSE,
        wrapper.RUNTIME_OPEN: wrapper.RUNTIME_CLOSE,
        envelope.ENGINE_STATE_OPEN: envelope.ENGINE_STATE_CLOSE,
        envelope.ENGINE_NOTICE_OPEN: envelope.ENGINE_NOTICE_CLOSE,
        envelope.ENGINE_MEMORY_OPEN: envelope.ENGINE_MEMORY_CLOSE,
        envelope.ENGINE_LANE_OPEN: envelope.ENGINE_LANE_CLOSE,
        envelope.STEERING_OPEN: envelope.STEERING_CLOSE,
        envelope.ENGINE_PALETTE_OPEN: envelope.ENGINE_PALETTE_CLOSE,
    }


def _strip_wrapper_at_head(
    text: str, open_tag: str, close_tag: str
) -> tuple[str, bool]:
    """Strip ``open_tag`` at the head of ``text`` (issue #45 guard, one tag).

    ``(text, changed)``: the text untouched when it does not begin with the
    tag; otherwise the tag and the whitespace after it removed, plus the
    MATCHING close tag when the model wrapped the whole turn (the remainder
    ends with it). Case-insensitive on the tags — a leak is never guaranteed to
    reproduce the engine's casing — and the close tag is only ever the one
    paired with the open tag actually found.
    """
    head = text.lstrip()
    if head[: len(open_tag)].lower() != open_tag.lower():
        return text, False
    rest = head[len(open_tag) :].lstrip()
    tail = rest.rstrip()
    if tail[-len(close_tag) :].lower() == close_tag.lower():
        rest = tail[: -len(close_tag)].rstrip()
    return rest, True


def _strip_engine_wrapper_leak(text: str) -> tuple[str, str | None]:
    """Defensive strip of an engine wrapper tag leaked at the head of prose.

    Issue #45: on glm-5.3 a turn AFTER the first can echo the engine's own
    bound into the reply (``'<preset_def>\\nYou turn to ask — …'``); the
    segmenter keeps it as prose and the commit would keep it on the transcript.
    The guard is shape-independent and applies to the INCOMING MODEL TEXT only
    (the envelope assembly is untouched — that is #44's seam).

    The rule, exactly:

    * nothing is stripped unless the (whitespace-trimmed) head is a known tag
      from the closed engine family — clean prose is returned unchanged, so a
      turn that never leaked commits BYTE-IDENTICAL text;
    * the leading open tag is removed together with the whitespace after it;
    * its matching CLOSE tag is removed too only when it closes the whole turn
      (the remainder ends with it) — never a different tag, never a close tag
      that happens to end mid-text;
    * a tag anywhere else (mid-text, a later line) is left untouched: this is a
      HEAD guard, not a scrubber.

    Returns ``(text, leaked_tag)``; ``leaked_tag`` is the canonical open-tag
    constant, ``None`` when nothing was stripped — the caller logs exactly ONE
    warning per turn.
    """
    if not text or "<" not in text:
        return text, None
    for open_tag, close_tag in _engine_wrapper_tags().items():
        stripped, changed = _strip_wrapper_at_head(text, open_tag, close_tag)
        if changed:
            return stripped, open_tag
    return text, None


def _strip_engine_wrapper_segments(segments: list[dict], leaked_tag: str) -> list[dict]:
    """Mirror a head/tail wrapper strip onto the committed prose SEGMENTS.

    The payload carries the turn twice — the flat ``prose`` string and the
    structured ``segments`` the transcript renders — so a stripped prose must
    not leave the tag behind in the first (or last) ``prose`` segment. The open
    tag rides the HEAD and the close tag the TAIL, exactly as
    :func:`_strip_wrapper_at_head` treats the flat text; ``dsl`` segments are
    never touched.
    """
    close_tag = _engine_wrapper_tags().get(leaked_tag)
    if close_tag is None:
        return segments
    prose_indexes = [
        index
        for index, segment in enumerate(segments)
        if isinstance(segment, Mapping) and segment.get("kind") == "prose"
    ]
    if not prose_indexes:
        return segments
    cleaned = list(segments)
    first, last = prose_indexes[0], prose_indexes[-1]
    head_text = str(cleaned[first].get("text") or "")
    if head_text[: len(leaked_tag)].lower() == leaked_tag.lower():
        rest = head_text[len(leaked_tag) :].lstrip()
        cleaned[first] = {**cleaned[first], "text": rest}
        last_text = str(cleaned[last].get("text") or "") if last != first else rest
        tail = last_text.rstrip()
        if tail[-len(close_tag) :].lower() == close_tag.lower():
            cleaned[last] = {
                **cleaned[last],
                "text": tail[: -len(close_tag)].rstrip(),
            }
    return cleaned


def _commit_narrative(log: LogPort, snapshot: dict, state: TurnState) -> dict:
    """Commit the turn: ``user`` (dedup) + ``narrative`` with its provenance.

    One fork-aware projection read (F2 + echo check): user dedup walks the
    inherited chain (a regenerated branch's user message came from the base),
    and the validator's prior-prose comes from inherited turns too.

    Commit the turn FIRST: an extractor failure must never discard a narrative
    that already streamed to the user (D4/D10 — the turn is real; extraction is
    repairable after the fact).

    Issue #45: the committed prose goes through the defensive wrapper-leak guard
    (:func:`_strip_engine_wrapper_leak`) here, at the commit seam — a model that
    echoes ``<preset_def>`` / ``<preset_runtime>`` (or any closed-family engine
    tag) at the head of its reply leaves it in prose, so the leading tag (and
    its matching close tag when the whole turn is wrapped) is stripped here, in
    BOTH the flat prose and the segments. ONE warning names the tag with the
    turn's session/seq so the leak stays observable.
    """
    session_id = state["session_id"]
    user_msg = state["user_content"]
    prose = "".join(state.get("narrative_parts", []))
    dsl = state.get("dsl", "")
    segments = state.get("segments") or []
    prose, leaked_tag = _strip_engine_wrapper_leak(prose)
    if leaked_tag is not None:
        segments = _strip_engine_wrapper_segments(segments, leaked_tag)

    projection = log.get_projection(session_id)
    prior_users = projection.get("user_messages", [])
    last_user_text = (
        prior_users[-1].get("payload", {}).get("text") if prior_users else None
    )
    if last_user_text != user_msg:
        log.append_event(session_id, "user", "user", {"text": user_msg})
    turn_snapshot = dict(snapshot)
    if state.get("facts_hash"):
        turn_snapshot["facts_hash"] = state["facts_hash"]
    if state.get("recall_text") is not None:
        # provenance: the recall block this narrative's facts hash folded
        turn_snapshot["recall"] = state["recall_text"]
    if state.get("recall_queries"):
        # D25 C4: exact recall queries for the inspector RAG panel.
        turn_snapshot["recall_queries"] = state["recall_queries"]
    if state.get("recall_config"):
        # D25 C4: resolved recall config for the inspector RAG panel.
        turn_snapshot["recall_config"] = state["recall_config"]
    if state.get("recall_rows"):
        # W5C-1: per-result rows recall surfaced this turn (inspector RAG
        # panel). Empty on legacy/no-recall turns -> key simply absent.
        turn_snapshot["recall_rows"] = state["recall_rows"]
    if state.get("facts_text") is not None:
        # Inspector I2: injected facts text (what the model saw).
        turn_snapshot["facts_text"] = state["facts_text"]
    if state.get("regions") is not None:
        # Inspector I2: per-region prompt sizes.
        turn_snapshot["regions"] = state["regions"]
    payload: dict[str, Any] = {"prose": prose, "dsl": dsl, "segments": segments}
    # Issue #42: persist the reasoning that PRODUCED this narrative so the row is
    # DIAGNOSABLE. It is not a prompt-cost passback: history assembly re-emits the
    # field PRESENT but EMPTY for a prior execution (owner ruling on #29), so the
    # stored text is never billed again. Presence is the fact: ``""`` means the
    # provider sent an empty field; a turn with no stored field stays
    # content-only.
    if state.get("reasoning") is not None:
        payload["reasoning"] = state["reasoning"]
    narrative_event = log.append_event(
        session_id,
        "narrative",
        "narrator",
        payload,
        meta={"config": turn_snapshot},
    )
    if leaked_tag is not None:
        # ONE line per leaked turn: the tag names the leak, session/seq locate
        # the turn it was stripped from. Never fatal — the strip already kept
        # the turn clean (issue #45).
        logger.warning(
            "stripped leaked engine wrapper %s from committed prose "
            "(session=%s seq=%s)",
            leaked_tag,
            session_id,
            getattr(narrative_event, "seq", None),
        )
    update: dict[str, Any] = {"narrative_event": _narrative_event_ref(narrative_event)}
    if leaked_tag is not None:
        # Keep the STATE channels in step with the committed payload: the
        # post-commit stages read ``segments``/``narrative_parts`` (validate,
        # BTS, attribution, env HUD), and a stripped leak must not leave them
        # operating on markup the transcript no longer carries.
        update["segments"] = segments
        update["narrative_parts"] = [prose] if prose else []
    return update


def make_narrate_node(
    *,
    model: ModelCaller,
    log: LogPort,
    build_messages: Callable[[TurnState], list[dict]] = default_narrator_messages,
    resynthesize=None,
    loop_capabilities: Any = None,
    loop_timeout_s: Any = None,
    loop_window_fn: Any = None,
    loop_return_thinking: Any = None,
    provider_label_fn: Callable[[], str] | None = None,
):
    """Build the ``narrate`` node: the narrator pass with its repair loop.

    A thin, named wrapper over :func:`make_narrator_node` for the graph's factory
    table: the generation, the DSL gate and the W9.24 repair loop all live there,
    and the turn COMMIT is the next node (:func:`make_commit_node`) — a node
    boundary, not a statement, is what keeps a cancelled turn from being kept.

    ``loop_capabilities`` / ``loop_timeout_s`` / ``loop_window_fn`` /
    ``loop_return_thinking`` are passed through to the repair loop (see
    :func:`make_narrator_node`); the wiring supplies live readers so a re-bound
    connection applies on the next turn. ``provider_label_fn`` is the same live
    seam for the model a provider-failure abort must name.
    """
    return make_narrator_node(
        model=model,
        build_messages=build_messages,
        resynthesize=resynthesize,
        log=log,
        loop_capabilities=loop_capabilities,
        loop_timeout_s=loop_timeout_s,
        loop_window_fn=loop_window_fn,
        loop_return_thinking=loop_return_thinking,
        provider_label_fn=provider_label_fn,
    )


def make_commit_node(*, log: LogPort, config_snapshot: dict | None = None):
    """Build the ``commit`` node: the turn's ``user`` + ``narrative`` rows.

    This is the block that used to open ``post_turn``: one fork-aware projection
    read (F2), the ``user`` row (echo-deduped, so a branch's inherited base user
    message is not duplicated) and the ``narrative`` row carrying the turn's
    config snapshot.

    It is its own node — not the tail of ``narrate`` — because a node BOUNDARY is
    the only thing that separates a streamed turn from a kept one: the engine
    stops consuming at a cancel and no later node runs, so a cancelled turn
    leaves only its ephemeral partial (W2.2). Committing inside the streaming
    node would keep a turn the user cancelled — which is why the W9.24 repair
    loop repairs in working memory and lands its result HERE, as one row. An
    already-committed turn (``narrative_event`` present — a graph that routed
    through the commit node twice) is a no-op, so the canonical edge can stay
    unconditional.

    REFUSAL (issue #46): a turn whose payload is empty — no prose AND no DSL —
    is not a narrative. Committing it writes an empty row that renders as an
    envelope + "thinking…" and that every later reader (compaction, recall,
    history passback) treats as a scene. The node appends an explicit
    :data:`TURN_ABORTED_EVENT` row and raises :class:`EmptyTurnAbort` instead;
    the node boundary keeps the tail from running against a turn that does not
    exist, and the engine surfaces the abort on its normal error frame.
    """
    snapshot = dict(config_snapshot or DEFAULT_CONFIG_SNAPSHOT)

    async def commit(state: TurnState) -> dict:
        if state.get("narrative_event"):
            # Already committed: the barrier passed for this turn; a second
            # commit would duplicate the narrative row.
            return {}
        if _payload_is_empty(state):
            # Issue #46: an empty completion is an ABORTED turn, never an
            # empty narrative. The append keeps the attempt in the log.
            _append_turn_aborted(log, state)
            raise EmptyTurnAbort()
        update = _commit_narrative(log, snapshot, state)
        # W9.19 (A): the COMMIT BARRIER. From this point the turn is KEPT —
        # the prose exists in the log — so everything the user waits for has
        # happened. The frame tells the pump to release the turn lock (the
        # user can type again) while the tail (validate → critic → bts → …)
        # keeps running and streaming in the background.
        writer = _stream_writer()
        narrative = update.get("narrative_event") or {}
        try:
            writer(
                {
                    "type": "committed",
                    "meta": {
                        "session_id": state.get("session_id"),
                        "narrative_seq": narrative.get("seq"),
                    },
                }
            )
        except Exception:  # noqa: BLE001 - the barrier frame must never fail a turn
            pass
        return update

    return commit


def make_validate_node(*, log: LogPort):
    """Build the ``validate`` node: DSL findings + validator flags (W2.4b/W2.6).

    Advisory housekeeping, never fatal (D9: everything as log events):

    1. ``dsl_validation`` — what the narrator's DSL gate failed/repaired and why.
       A failed statement is kept in the payload (keep + tag); the flag below
       anchors the explanation to its segment. Emitted only when the segments in
       state carry findings (a turn the repair loop cleaned reports none).
    2. ``validator_flags`` — Dress Rehearsal flags, never rewrites. The DSL
       gate's findings AND the ``prose_markup`` detect (:func:`_prose_markup_flags`)
       are folded into the SAME event so the inspector/transcript read model
       surfaces them (the W2.4 "invisible errors" fix).
    3. ``validation_notice`` — the SAME finding list as a teaching row the
       NEXT turns' narrator reads (owner ruling: steer the model toward proper
       output, not just audit it). It names the flagged narrative's ``seq`` so
       the message builder places it as a ``system`` turn immediately after
       that narrative — never the prefix, never ``<recalled>``. One row per
       flagged turn; a clean turn writes none.

    This is the POST-commit record of what the turn's text is. The narrator's
    own validate step (:func:`_narrate_flags`) runs the same deterministic bundle
    PRE-commit, so the repair loop fixes what would otherwise only be reported
    here.

    Returns the flags the critic will see (``critic_flags``) plus the
    ``validation`` record they were built from.
    """

    async def validate(state: TurnState) -> dict:
        session_id = state["session_id"]
        user_msg = state["user_content"]
        segments = state.get("segments") or []
        narrative_event = state.get("narrative_event") or {}
        prior_narratives = _prior_narratives(
            log, session_id, exclude_id=narrative_event.get("id")
        )

        validation_errors = state.get("validation_errors") or []
        if validation_errors:
            log.append_event(
                session_id,
                "system",
                "narrator",
                {"event": "dsl_validation", "errors": validation_errors},
            )

        dsl_flags = dsl_error_flags(validation_errors)
        narrative_flags: list[dict] = []
        narrative_score = 100
        try:
            prior_prose = str(
                (prior_narratives[-1].get("payload", {}).get("prose", ""))
                if prior_narratives
                else ""
            )
            report = validate_narrative(
                user_msg,
                segments,
                ValidatorConfig(
                    prior_first_words=(" ".join(prior_prose.split()[:6]) or None)
                ),
            )
            narrative_flags = [dataclasses.asdict(f) for f in report.flags]
            narrative_score = report.score
            # D80: raw markup in prose is detected from the SEGMENTS, never
            # stripped (the owner rejected output policing; the fix is teaching).
            # One spelling of the check with the narrate node's validate step
            # (:func:`_prose_markup_flags`), and it reads the segments that will
            # be committed — so a repair that removed the markup clears the flag.
            narrative_flags.extend(_prose_markup_flags(segments))
            # W9.28 D3: a spoken line the model left unwrapped is strictly
            # unattributed, and a colour span wrapping prose with no quote is
            # neither speech nor narration. The pre-commit repair loop is where
            # both get fixed; this is the same fold recorded for the inspector
            # and the critic. The palette is built ONCE for both folds.
            palette = _palette_for_turn(log, session_id)
            narrative_flags.extend(_attribution_missing_flags(segments, palette))
            narrative_flags.extend(_attribution_unquoted_flags(segments, palette))
        except Exception as exc:  # advisory only — never block the turn
            log.append_event(
                session_id,
                "system",
                "validator",
                {"event": "validator_failed", "error": str(exc)[:300]},
            )
        combined_flags = narrative_flags + dsl_flags
        score = narrative_score
        if combined_flags:
            score = max(
                0,
                narrative_score
                - sum(SEVERITY_WEIGHT.get(f.get("severity", "warn"), 0) for f in dsl_flags),
            )
            log.append_event(
                session_id,
                "system",
                "validator",
                {"event": "validator_flags", "flags": combined_flags, "score": score},
            )
            # D-F: the teaching half. A notice without a target seq cannot be
            # placed after the turn it refers to, so it is only written when
            # the committed narrative's seq is known (the graph path always
            # has it; a direct node call may not). An empty projection is
            # skipped: the row would teach nothing and the append door rejects
            # it — post-commit housekeeping must never fail the tail.
            narrative_seq = narrative_event.get("seq")
            notice_flags = _notice_flags(combined_flags)
            if (
                notice_flags
                and isinstance(narrative_seq, int)
                and not isinstance(narrative_seq, bool)
                and narrative_seq > 0
            ):
                log.append_event(
                    session_id,
                    "system",
                    "validator",
                    {
                        "event": VALIDATION_NOTICE_EVENT,
                        "seq": narrative_seq,
                        "flags": notice_flags,
                        "score": score,
                    },
                )
        return {
            "critic_flags": combined_flags,
            "validation": {
                "flags": combined_flags,
                "score": score,
                "dsl_errors": validation_errors,
                # The narrator's DSL-gate record rides through: whether a
                # last-resort re-synthesis ran and whether its replacement was
                # discarded in favour of the draft.
                "retry_applied": bool(
                    (state.get("validation") or {}).get("retry_applied")
                ),
                "resynth_kept_draft": bool(
                    (state.get("validation") or {}).get("resynth_kept_draft")
                ),
            },
        }

    return validate


def make_decision_gate_node(*, log: LogPort, decision: Any | None = None):
    """Build the ``decision_gate`` node: ONE batched d1-3b ``ask`` per turn.

    The per-turn advisory gate over the owner-editable question bank (slop
    phrases + narrative movement by default, ``__decision_queries__``). Answers
    become advisory critic flags — ADDED to the deterministic flags the critic
    already sees, never a gate (the verdict pre-gate stays dead). Never fatal: a
    disabled caller / error / no answers changes nothing.

    It is a per-TURN advisory call: ONE ``ask`` runs on the committed turn, and
    the critic reads the flags it produced plus the deterministic ones the
    narrator's validate step already folded into state.
    """

    async def decision_gate(state: TurnState) -> dict:
        prose = "".join(state.get("narrative_parts", []))
        gate_flags = await _run_turn_gate(
            decision, state["user_content"], prose, "", log
        )
        combined = list(state.get("critic_flags") or []) + list(gate_flags)
        if not combined:
            # W9.19 (B): record WHY the critic will not run this turn — a
            # missing critic in the trace must read as a decision, not a bug.
            try:
                log.append_event(
                    state["session_id"],
                    "system",
                    "decision_gate",
                    {"event": "critic_skipped", "signals": 0},
                )
            except Exception:  # noqa: BLE001 - bookkeeping never fails a turn
                pass
        return {
            "gate_flags": gate_flags,
            "critic_flags": combined,
        }

    return decision_gate


def needs_critic_route(state: TurnState) -> str:
    """The decision gate's conditional edge (W9.19 B): ``critic`` | ``planner``.

    The critic is a full model pass (the most expensive call after the
    narrator) and the preset it ports never had one — quality lived in
    directives. It runs ONLY when there is something to judge: a validator
    flag (parrot/echo/slop/control/dsl) or an advisory gate flag (the
    owner-editable question bank). A clean turn takes the keep route, and the
    gate logs a ``critic_skipped`` row so the trace explains the gap instead of
    hiding it.

    The literal ``"planner"`` is the KEEP-route condition VALUE, not a
    destination: since W9.20 the planner runs BEFORE the lanes (a ``pre_turn``
    junction), so a topology maps this value onto the next post-commit node
    (``bts`` in the canonical one). The value is unchanged because stored
    topologies already branch on it; the upgrade rewires the target.
    """
    flags = state.get("critic_flags") or []
    return "critic" if flags else "planner"


def critic_route(state: TurnState) -> str:
    """The critic's conditional-edge condition: ``ok`` | ``diffs`` | ``fail``.

    Shared by the graph's conditional edge and by the legacy ``post_turn``
    composition (:func:`make_post_turn_node`), so both follow ONE routing rule:

    * ``ok`` / ``diffs`` → the keep route (the ``"planner"`` condition value,
      which a W9.20 topology maps to the next post-commit node — the planner
      itself now runs pre-narration);
    * ``fail`` → the keep route too. W9.24 §3.3.7 RETIRED the regeneration: the
      narrator repairs in its own node before the commit, and a post-commit
      ``fail`` keeps the delivered turn with its verdict row (the cap-spent
      behaviour of D49, now the only behaviour). A legacy parse that still says
      ``fail`` therefore routes exactly like ``ok`` — never fatal (D49).

    A stored topology still carrying the dead ``critic -> narrate [fail]`` edge
    boots unchanged: this router never returns ``fail``, so the edge is
    unreachable and nothing is orphaned.

    Post-turn composition note (W9.20 §9): :func:`make_post_turn_node` no longer
    runs a planner stage at all — the planner is a pre-narration step gated by
    :func:`make_plan_due_route`.
    """
    verdict = str((state.get("critic_verdict") or {}).get("verdict") or "ok")
    return verdict if verdict == "diffs" else "ok"


def make_critic_node(*, log: LogPort, critic: Any | None = None):
    """Build the ``critic`` node: run the quality gate, apply its verdict.

    ``ok``    -> log the verdict, keep the turn.
    ``diffs`` -> apply the patch ON TOP (``quality.apply_patch``), ONE EDIT AT A
                 TIME: any edit whose ``find`` is prose lands and the landed
                 subset becomes the ``revision`` superseding the narrative (its
                 frame rides the stream before ``done``), while a rejected edit
                 is logged with its per-edit diagnosis. Only when NO edit lands
                 is nothing revised and the turn kept.
    ``fail``  -> log the verdict and keep the turn. The regeneration the verdict
                 used to request is retired (W9.24 §3.3.7): the narrator's own
                 repair loop runs earlier and cheaper, and the verdict row keeps
                 the defect visible.

    Every stage is non-fatal (the turn already streamed, D49): a raising call, an
    unparseable verdict, or a rejected patch is logged (``critic_failed``) and
    the delivered turn is kept.
    """

    async def critic_node(state: TurnState) -> dict:
        if critic is None:
            return {}
        return await _run_critic_stage(critic, log, state)

    return critic_node


#: Committed turns between planner runs when the preset has no planner config
#: (W9.20 §6 default: ``{"cadence": 5, "steer": "", "fields": []}``).
DEFAULT_PLAN_CADENCE = 5

#: Planner directive used when no prompt surface is wired (direct node calls and
#: tests). B1 passes the planner flow's system prompt through ``directive=``.
DEFAULT_PLANNER_DIRECTIVE = (
    "You are the story planner for a roleplay narrative engine. Read the context "
    "and set the plan the next turns steer by: 4-6 forward story strings, the "
    "in-fiction date/time the scene opens at, the weather and its outlook, and "
    "the facts this plan holds established. Follow the card or world when it "
    "already states a start. Answer with the plan only."
)

#: How much recent narration the planner reads, and how long each turn's prose
#: may be before it is clipped (the planner needs the drift, not the transcript).
PLANNER_TAIL_TURNS = 3
PLANNER_TAIL_CHARS = 1200


def committed_turn_count(log: Any, session_id: str) -> int:
    """Committed (non-ephemeral) narratives on the fork-aware CHAIN.

    The SAME read rule as the plan fold and the due route (W9.20 §2/M2): a
    branch counts the turns it inherited, which is what the cadence compares
    against ``Plan.turn_seq`` — itself a count taken here at generation time.
    """
    get_chain_events = getattr(log, "get_chain_events", None)
    events = (
        get_chain_events(session_id)
        if callable(get_chain_events)
        else log.get_events(session_id)
    )
    return sum(
        1
        for event in events
        if getattr(event, "type", None) == "narrative"
        and not getattr(event, "ephemeral", False)
    )


def plan_is_due(
    *,
    last_plan: Plan | None,
    committed_turns: int,
    cadence: int = DEFAULT_PLAN_CADENCE,
) -> bool:
    """The cadence rule: turn 1 is mandatory, then every ``cadence`` turns.

    ``last_plan is None`` means no plan exists (a fresh session — or a session
    whose only plans carry the retired beats/blocking shape, which
    :func:`~fabulist.graph.plans.load_last_plan` folds as absent), so the
    planner runs before the first narration. Otherwise it runs once
    ``committed_turns - last_plan.turn_seq >= cadence``.
    """
    if last_plan is None:
        return True
    return committed_turns - int(last_plan.turn_seq or 0) >= max(1, int(cadence))


def make_plan_due_route(*, log: Any, cadence: int | Callable[[], int] = DEFAULT_PLAN_CADENCE):
    """Build the ``pre_turn`` junction router: ``"plan"`` | ``"skip"``.

    The return values ARE the junction's edge conditions (a topology carries
    ``pre_turn->planner[plan]`` and ``pre_turn->npc_lanes[skip]``): LangGraph
    looks a branch result up as a plain dict key with no node-name fallback.

    ASYNC on purpose, like :meth:`NpcLaneFanout.route`: the route reads the event
    log (the folded plan, the committed-turn count), and LangGraph runs a SYNC
    path function in a worker thread, where the log's SQLite connection must not
    be used. Awaiting keeps the reads on the loop thread.

    ``cadence`` may be an int or a zero-arg callable returning the ACTIVE
    preset's cadence: pass the live reader so a Studio cadence edit applies on
    the next due evaluation without rebuilding the graph (the planner config is
    deliberately outside the topology digest, W9.20 §10).
    """

    async def plan_due_route(state: TurnState) -> str:
        session_id = state["session_id"]
        resolved = cadence() if callable(cadence) else cadence
        due = plan_is_due(
            last_plan=load_last_plan(log, session_id),
            committed_turns=committed_turn_count(log, session_id),
            cadence=resolved,
        )
        return "plan" if due else "skip"

    return plan_due_route


def _world_objects_block(log: LogPort, session_id: str) -> str:
    """The advisory ``WORLD OBJECTS:`` lines — every live place, path, occupants.

    W9.31 §3.3: after the planner's own seed phase the registry holds the
    skeleton the plan should rest on, so the plan call is shown it. Read-only and
    small: each line is the canonical :func:`entities.location_path` (that is
    where the parent chains live) plus the occupants as DISPLAY names — the same
    resolution the sidebar uses, with the raw key as the fallback so a being the
    roster does not know is never dropped.

    The off-screen bucket is skipped: it is not a place, and who is off screen is
    presence's question, not the plan's. A registry holding no place but the
    bucket renders NO block — on turn 1 that is simply nothing to say yet.

    Never raises on its own account; the caller's ``try`` is the boundary.
    """
    from fabulist.state.cast import member_by_id, roster
    from fabulist.state.entities import (
        OFF_SCREEN_LOCATION_ID,
        PLAYER,
        location_path,
        occupants,
        registry,
    )

    reg = registry(log, session_id)
    place_ids = [
        loc_id for loc_id in reg.locations if loc_id != OFF_SCREEN_LOCATION_ID
    ]
    if not place_ids:
        return ""
    members = roster(log, session_id)

    def name_of(key: str) -> str:
        if key == PLAYER:
            return PLAYER
        member = member_by_id(members, key)
        return member.display_name if member is not None else key

    lines: list[str] = []
    for loc_id in place_ids:
        place = location_path(reg, loc_id) or loc_id
        names = [name_of(who) for who in occupants(reg, loc_id)]
        lines.append(f"- {place}: {', '.join(names)}" if names else f"- {place}")
    return "WORLD OBJECTS:\n" + "\n".join(lines)


def _planner_context(log: LogPort, session_id: str, state: TurnState) -> str:
    """The planner's context block: current state, cast, world, recent turns.

    Built from the existing seams the other agents use — the fork-aware folded
    state (plus this turn's rules-clock facts, which pre_turn advanced but whose
    state-delta lands after the plan), the card persona block, the world's LIVE
    definition, and the last :data:`PLANNER_TAIL_TURNS` narratives. Advisory
    throughout: a failing source is simply skipped, never fatal.

    The world part is :func:`fabulist.context.facts.session_world_definition_block`
    — the same builder the narrator's ``### World`` prefix uses — because the
    planner previously saw no world material at all: its cast read is the
    session's card events, and a card-style world carries no cards (the
    townhouse IS the ``{{char}}``, its ``description`` the whole scenario), so the
    planner invented a setting from nothing (live defect).

    ``variables`` (D43) is resolved once here for the whole block: both the card
    text and the world description are user-authored ST text carrying
    ``{{user}}`` / ``{{char}}`` / ``{{persona}}`` / ``{{world}}``, and an
    unresolved token reaches the model literally. A resolver failure leaves the
    map ``None`` and the render stays literal-token, as before.

    W9.31 §3.3 adds the ``WORLD OBJECTS:`` block (:func:`_world_objects_block`):
    the live registry's places and occupants, so a plan written AFTER the seed
    phase rests on the skeleton instead of inventing one beside it.
    """
    parts: list[str] = []

    variables: dict | None
    try:
        from fabulist.prompts.variables import resolve_variables

        variables = resolve_variables(
            log, session_id, str(state.get("user_content") or "")
        )
    except Exception:  # noqa: BLE001 - context is advisory, never fatal
        variables = None

    from fabulist.state import entities

    # W9.32 §6: the rendered state (registry-derived loc/weather/tone/inv/genre +
    # the ``conds`` line), with this turn's advanced clock overlaid — the plan is
    # written BEFORE the turn's state-delta lands.
    fold = entities.rendered_state(
        log, session_id, extra_facts=parse_facts(state.get("facts", ""))
    )
    if fold:
        parts.append("CURRENT STATE:\n" + render_facts(fold))

    try:
        from fabulist.context.roster import extract_cards, persona_block

        # ``first_mes=[]``: the opening scene is already in the tail prose.
        block = persona_block(
            extract_cards(log.get_chain_events(session_id)), [], variables=variables
        )
        if block:
            parts.append("CAST AND WORLD:\n" + block)
    except Exception:  # noqa: BLE001 - context is advisory, never fatal
        pass

    try:
        from fabulist.context.facts import session_world_definition_block

        world = session_world_definition_block(
            log, log.get_chain_events(session_id), variables
        )
        if world:
            parts.append("WORLD:\n" + world)
    except Exception:  # noqa: BLE001 - context is advisory, never fatal
        pass

    try:
        objects = _world_objects_block(log, session_id)
        if objects:
            parts.append(objects)
    except Exception:  # noqa: BLE001 - context is advisory, never fatal
        pass

    try:
        tail = _prior_narratives(log, session_id)[-PLANNER_TAIL_TURNS:]
        lines = [
            " ".join(str(item.get("payload", {}).get("prose", "")).split())[:PLANNER_TAIL_CHARS]
            for item in tail
        ]
        lines = [line for line in lines if line]
        if lines:
            parts.append("RECENT TURNS:\n" + "\n\n".join(lines))
    except Exception:  # noqa: BLE001 - context is advisory, never fatal
        pass

    return "\n\n".join(parts)


def _planner_messages(
    *,
    directive: str,
    context: str,
    prior: Plan | None,
    steer: str,
    fields: Any,
    profile: dict | None,
    turn_seq: int,
) -> list[dict]:
    """The planner's one model call: directive + context + the ask.

    Content mode (``profile is None``, no function calling) states the JSON
    schema in the system message so the same ``structured_result`` ladder can
    parse the reply; with function calling the tool carries it instead. On a
    refresh the existing strings ride the ask so the model replaces the SET
    rather than inventing a fresh story (§5/7: strings persist across the N
    turns between runs).
    """
    import json

    system_parts = [directive.strip()]
    if profile is None:
        schema = planner_tool(fields)["function"]["parameters"]
        system_parts.append(
            "Answer with ONE JSON object matching this schema, no prose:\n"
            + json.dumps(schema, indent=2)
        )
    if context:
        system_parts.append(context)

    ask: list[str] = []
    if prior is not None:
        ask.append(
            f"Refresh the plan for turn {turn_seq}. Keep the strings that still "
            "play out; replace the ones that resolved or stalled."
        )
        ask.append("Current strings:\n" + "\n".join(f"- {s}" for s in prior.strings))
        if prior.established:
            ask.append(
                "Established so far:\n" + "\n".join(f"- {e}" for e in prior.established)
            )
    else:
        ask.append(
            f"Set the plan for turn {turn_seq}: the scene this story opens on and "
            "the strings the next turns should steer toward."
        )
    if steer.strip():
        ask.append("Steer: " + steer.strip())

    return [
        {"role": "system", "content": "\n\n".join(system_parts)},
        {"role": "user", "content": "\n\n".join(ask)},
    ]


def _seed_plan_state(
    log: LogPort, session_id: str, state: TurnState, plan: Plan
) -> dict:
    """Persist the plan's scene seeds and return the re-rendered ``facts``.

    Both halves matter (W9.20 §5.4 / B2):

    * a ``state-delta`` — log truth, so the fold carries the planned weather
      (with the forecast beside it, the BTS env-line notation
      ``weather: <now> | forecast: <ahead>``) and, on the FIRST plan only, the
      planned opening DATETIME (``time``/``date``/``day`` — W9.32 R4: the plan
      opens the session's calendar, not just the time of day, so day 1 renders
      the date the world starts on);
    * an updated ``facts`` render returned as node state — the post-commit BTS
      clock re-stamp reads ``state["facts"]`` at bts time
      (:func:`_run_world_state` receives ``parse_facts(state["facts"])``), so
      without this the turn would be stamped with pre_turn's default ``08:00``
      and epoch date instead of the planned opening instant.

    The datetime is seeded ONLY when the folded chain state has no ``time``
    (turn 1). The rules clock owns time afterwards (D3): the planner sets the
    opening clock, it never moves it; ``parse_plan_instant`` folds the free-text
    ``datetime`` into the clock's own three keys (and falls back to the epoch
    date for a date-less plan, exactly what the clock would have defaulted to).
    """
    fold = dict(log.get_projection(session_id).get("state") or {})
    seeds: dict[str, str] = {}
    weather = (plan.weather or "").strip()
    if weather:
        seeds["weather"] = weather
    forecast = (plan.forecast or "").strip()
    if forecast:
        seeds["forecast"] = forecast
    if not str(fold.get("time") or "").strip():
        seeded = parse_plan_instant(plan.datetime)
        if seeded:
            seeded_time, seeded_date, seeded_day = seeded
            seeds["time"] = seeded_time
            seeds["date"] = seeded_date
            # str: the clock fold stores `day` as text (``advance`` renders the
            # facts block), so the projection keeps ONE spelling for the key.
            seeds["day"] = str(seeded_day)
    if not seeds:
        return {}
    log.append_event(
        session_id,
        "state-delta",
        PLAN_ACTOR,
        seeds,
        meta={"source": "planner"},
    )
    facts = dict(parse_facts(state.get("facts") or ""))
    facts.update(seeds)
    return {"facts": render_state_block(facts)}


def _provider_label(reader: Callable[[], str] | None) -> str:
    """The model a phase's calls are bound to, for the abort message ("")."""
    if reader is None:
        return ""
    try:
        return str(reader() or "")
    except Exception:  # noqa: BLE001 - a label read never breaks the phase it labels
        logger.debug("provider label read failed", exc_info=True)
        return ""


def _raise_if_provider_failure(
    phase: str, exc: BaseException, provider: str
) -> None:
    """Abort the turn when ``exc`` is a provider/transport failure.

    Content failures (empty/unparseable/schema-invalid replies, a 400/422 naming
    THIS call's payload) return here and are contained by the caller exactly as
    before — the previous plan keeps steering. A provider failure
    (429/401/403/5xx, connection/transport) is raised as the phase error
    :mod:`fabulist.graph.provider_errors` builds, so it reaches the client as the
    engine's error frame naming the phase instead of surfacing later, at a phase
    that only inherited it.
    """
    fatal = phase_failure(phase, exc, provider=provider)
    if fatal is not None:
        raise fatal from exc


def make_planner_node(
    *,
    log: LogPort,
    caller: Any | None = None,
    directive: str = DEFAULT_PLANNER_DIRECTIVE,
    planner_config: Callable[[], Mapping] | None = None,
    request_profile_fn: Callable[[], dict | None] | None = None,
    provider_label_fn: Callable[[], str] | None = None,
    return_thinking_fn: Callable[[], bool | None] | None = None,
):
    """Build the ``planner`` node: the seed phase + the steering pass.

    W9.31 §3.2: TWO contained phases, in order.

    1. the SEED phase (:func:`fabulist.state.planner_bootstrap.
       run_planner_tool_phase`) — one bounded entity-tool loop that seeds the
       object stores, run ONLY when the connection does function calling
       (``profile is not None``): the session BOOTSTRAP on the first run
       (``prior is None``), a REPAIR on every later one. Its own try, a SIBLING
       of the plan's (§6): a failed seed never costs the plan, so the first
       prompt still gets steering even when the registry stays empty (S2).
    2. the PLAN phase — ONE model call, then three effects:

       a. the plan event is appended (``created_from="graph"``), so the next
          ``load_last_plan`` — and the narrator's ``plan=`` seam — sees it;
       b. the scene seeds land as a ``state-delta`` AND as re-rendered ``facts``
          (:func:`_seed_plan_state`);
       c. ``state["plan"]`` carries the plan's render for this turn.

    Best-effort by contract for CONTENT failures: an unparseable or
    schema-invalid payload, an empty reply, or a failed append is logged as
    ``plan_refresh_failed`` and the PREVIOUS plan keeps steering — a bad plan
    never fails the turn. A SEED failure appends the same event with an additive
    ``stage`` (``"bootstrap"`` / ``"repair"``), so a failed seed and a failed
    plan are told apart; the plan phase's own row stays byte-identical (no
    ``stage``).

    NOT best-effort for PROVIDER failures (:mod:`fabulist.graph.provider_errors`):
    a 429/401/403/5xx or a connection/transport error means the provider never
    answered, so the plan below (and the narrator after it) will meet the same
    wall. Containing it hid the cause behind whatever broke next, so the node
    RAISES a :class:`~fabulist.graph.provider_errors.PhaseCallError` instead and
    the turn aborts on the engine's error frame, naming the phase.

    ``planner_config`` is the live reader of the active preset's planner config
    (``{"cadence", "steer", "fields"}``) — a callable, so a Studio edit applies
    on the next run without rebuilding the graph. ``caller`` is the narrator
    binding (§1/11 for now); with no caller the node is inert.
    ``provider_label_fn`` is the live reader of the model the FAILING call was
    bound to (``"glm-4.6"``), read at the failure — never once per run and never
    at build time, so a rebind is named correctly on the very turn it applies.
    ``return_thinking_fn`` is the same live-reader seam for the connection's
    reasoning-echo setting: the seed phase's tool loop repeats a thinking model's
    ``reasoning_content`` back when the provider returned some (``None`` = AUTO).
    """

    async def planner(state: TurnState) -> dict:
        if caller is None:
            return {}

        def _return_thinking() -> bool | None:
            """The connection's reasoning-echo setting (``None`` = AUTO).

            A lookup failure answers ``None``: AUTO is what every tool-loop model
            wants by default, so a broken settings read must never cost the seed.
            """
            if return_thinking_fn is None:
                return None
            try:
                return return_thinking_fn()
            except Exception:  # noqa: BLE001 - AUTO is the safe answer
                return None

        session_id = state["session_id"]
        writer = _stream_writer()
        update: dict = {}
        _emit_stage(writer, "plan", "start")
        # -- phase 1: seed (bootstrap) or repair (W9.31 §3.2, §6) ----------
        # Its OWN try, BEFORE (not inside) the plan's: a raise here — a
        # ``PlannerBootstrapError``, or any transport error the loop re-raises —
        # is contained here and the plan below still runs. ``stage`` doubles as
        # the mode the phase would have run with, so the failure row says which
        # phase fell over even when it fell over before the loop started.
        from fabulist.state.planner_bootstrap import (
            BOOTSTRAP,
            REPAIR,
            run_planner_tool_phase,
        )

        stage = BOOTSTRAP
        try:
            # The probe is a settings read, not a model call; the plan phase below
            # resolves its own profile again so a resolution failure still reports
            # through the plan's original handler.
            profile = request_profile_fn() if request_profile_fn else None
            if profile is not None:
                prior = load_last_plan(log, session_id)
                stage = BOOTSTRAP if prior is None else REPAIR
                await run_planner_tool_phase(
                    caller=caller,
                    log=log,
                    session_id=session_id,
                    directive=directive,
                    context=_planner_context(log, session_id, state),
                    turn_seq=committed_turn_count(log, session_id),
                    mode=stage,
                    profile=profile,
                    return_thinking=_return_thinking(),
                )
        except Exception as exc:  # noqa: BLE001 - a failed seed never costs the plan
            # …unless the provider never answered: then the plan below rides the
            # same dead connection, so the turn must abort HERE, naming this half.
            # The label is read NOW: the abort names the model the failing call
            # resolved to, not a snapshot taken before the phase ran.
            _raise_if_provider_failure(
                "Planning (seed)", exc, _provider_label(provider_label_fn)
            )
            logger.warning("planner %s phase failed", stage, exc_info=True)
            log.append_event(
                session_id,
                "system",
                PLAN_ACTOR,
                {
                    "event": "plan_refresh_failed",
                    "error": str(exc)[:500],
                    "stage": stage,
                },
            )
        # -- phase 2: the unchanged plan call ----------------------------
        try:
            from fabulist.state.structured import StructuredFailure, structured_result

            config = dict(planner_config() or {}) if planner_config else {}
            fields = config.get("fields") or []
            steer = str(config.get("steer") or "")
            turn_seq = committed_turn_count(log, session_id)
            prior = load_last_plan(log, session_id)
            profile = request_profile_fn() if request_profile_fn else None
            messages = _planner_messages(
                directive=directive,
                context=_planner_context(log, session_id, state),
                prior=prior,
                steer=steer,
                fields=fields,
                profile=profile,
                turn_seq=turn_seq,
            )
            content, tool_args, finish_reason = await _drain_lane(caller, messages, profile)
            parsed = structured_result(
                content=content, tool_args=tool_args, finish_reason=finish_reason
            )
            if isinstance(parsed, StructuredFailure):
                raise ValueError(f"structured: {parsed.reason}")
            plan = validate_planner_payload(
                parsed, fields, turn_seq=turn_seq, created_from="graph"
            )
            save_plan(log, session_id, plan)
            update.update(_seed_plan_state(log, session_id, state, plan))
            update["plan"] = render_plan_for_prompt(plan)
        except Exception as exc:  # noqa: BLE001 - a bad plan never fails a turn
            _raise_if_provider_failure(
                "Planning", exc, _provider_label(provider_label_fn)
            )
            log.append_event(
                session_id,
                "system",
                PLAN_ACTOR,
                {
                    "event": "plan_refresh_failed",
                    "error": str(exc)[:500],
                },
            )
        _emit_stage(writer, "plan", "done")
        return update

    return planner


def make_bts_node(
    *,
    extractor: ExtractorFn,
    log: LogPort,
    world_state: Any | None = None,
    decision: Any | None = None,
):
    """Build the ``bts`` node: the world-state pass (or the extractor fallback).

    Wave 6 (D21/B4/D25): when ``world_state`` is supplied it OWNS the
    state-delta — the extractor's delta emission is retired (no parallel state
    systems) and ``world_state`` writes the flat fold keys (``loc``/``weather``/
    ``tone``/``inv``) plus the turn's ``entity_summary``. The rules clock
    stays authoritative for ``time``. With ``world_state=None`` the node keeps
    its pre-Wave-6 extractor behaviour byte-for-byte. W9.29 §6/W4 retired the
    compat ``world_state`` render this node used to append for the not-yet-flipped
    readers.

    D51: when a studio cast edit landed, the cast-change notice + the session's
    card context are prepended to the text the extractor/BTS see; absent a
    notice the input is ``user_msg`` verbatim.
    """

    async def bts(state: TurnState) -> dict:
        session_id = state["session_id"]
        user_msg = state["user_content"]
        prose = "".join(state.get("narrative_parts", []))
        narrative_event = state.get("narrative_event") or {}
        prior_narratives = _prior_narratives(
            log, session_id, exclude_id=narrative_event.get("id")
        )

        deltas: dict = {}
        action_class = state.get("action_class", "") or ""
        # The clock computed this turn's time (and calendar date) in pre_turn;
        # fold them into the persisted state-delta so the log (truth) carries
        # them forward — without this the projection forgets the date every
        # turn and the calendar can never advance past epoch day 1.
        clock_fold = parse_facts(state.get("facts", ""))
        clock_time = clock_fold.get("time")
        bts_result: dict | None = None
        world_state_event: dict = {}

        # D51: when a studio cast edit landed, prepend the cast-change notice +
        # the session's card context to the text the extractor/BTS see, so they
        # can reason about the changed cast. Absent a notice this is ``user_msg``
        # verbatim (byte-identical to the pre-D51 input) and the narrator prompt
        # path / persisted user event are untouched.
        cast_context = _cast_agent_context(log, session_id)
        agent_user_msg = (
            f"{CAST_CONTEXT_HEADER}\n{cast_context}\n\n{user_msg}"
            if cast_context
            else user_msg
        )

        if world_state is not None:
            # D21/B4: BTS owns world-state; the extractor's delta emission is
            # retired. action_class is the rules classifier's (D3).
            _emit_stage(_stream_writer(), "world_state", "start")
            deltas, bts_result, _commit = await _run_world_state(
                world_state,
                log,
                session_id,
                user_msg,
                prose,
                action_class,
                clock_fold,
                cast_context=cast_context,
                decision=decision,
                prior_narratives=prior_narratives,
                narrative_seq=(narrative_event or {}).get("seq"),
            )
            _emit_stage(_stream_writer(), "world_state", "done")
            # W9.29 §6/W4: the compat ``world_state`` render is RETIRED, so this
            # channel carries nothing for a registry-era turn — readers are
            # registry-first and a legacy session reads its own historical rows
            # (§11.1). The key stays declared for the historian's window union.
        else:
            try:
                outcome = extractor(agent_user_msg, prose)
                if inspect.isawaitable(outcome):  # sync and async extractors both legal
                    outcome = await outcome
                if not isinstance(outcome, dict):  # ExtractorResult dataclass et al.
                    outcome = dataclasses.asdict(outcome)
                result = outcome
                deltas = dict(result.get("deltas") or {})
                action_class = result.get("action_class") or action_class
            except Exception as exc:  # extractor failure is logged, not fatal
                log.append_event(
                    session_id,
                    "system",
                    "extractor",
                    {"event": "extraction_failed", "error": str(exc)[:500]},
                )
            if clock_time and "time" not in deltas:
                deltas["time"] = clock_time
            for key in ("date", "day"):
                # Same authority class as time (D3): the calendar clock owns
                # date/day; the extractor only ever echoes them.
                value = clock_fold.get(key)
                if value and key not in deltas:
                    deltas[key] = value
            # d1-3b genre (scene-level; see _enrich_genre) — the trait, not a
            # flat key (W9.32 §6).
            await _enrich_genre(
                decision,
                deltas,
                user_msg,
                prose,
                prior_narratives,
                log=log,
                session_id=session_id,
            )
            log.append_event(
                session_id,
                "state-delta",
                "extractor",
                deltas,
                meta={"action_class": action_class},
            )

        return {
            "deltas": deltas,
            "action_class": action_class,
            "bts_result": bts_result or {},
            "world_state_event": world_state_event,
        }

    return bts


def make_env_hud_node(*, log: LogPort):
    """Build the ``env_hud`` node: one engine-emitted EnvTracker HUD (D38).

    Fork F2=A: one engine-assembled OpenUI statement per kept turn, latest-wins
    in the header. The fold already carries this turn's state (the state-delta
    was appended by ``bts``), and a BTS failure still ticks the station — the
    forecast simply falls down its ladder. A HUD miss must never break the turn
    (D9).

    W9.29 §8 — the registry is the identity-stable source for three of the five
    slots (the statement SHAPE is untouched, five positional args as always):

    * ``loc`` is the player's canonical path
      (:func:`fabulist.state.entities.location_path` over the assignment), so a
      parent chain renders ``upstairs hall > bathroom``; an unassigned player
      keeps today's flat-fold value;
    * ``weather``/``tone`` are the scene traits
      (:func:`fabulist.state.entities.flat_render`), the flat fold beneath;
    * rung 2 of the forecast ladder is the registry's scene-trait ``forecast``,
      passed as the retired ``bts_result`` slot's successor. Rung 1 is
      unchanged and untouchable: the fold's ``forecast`` key is the D45 manual
      pin, and ``flat_render`` deliberately omits it (review R2) so a
      model-written trait can never overwrite a human's choice.
    """

    async def env_hud(state: TurnState) -> dict:
        session_id = state["session_id"]
        _emit_stage(_stream_writer(), "env_hud", "start")
        try:
            from fabulist.state import entities
            from fabulist.state.env_hud import env_hud_statement, latest_env_hud

            registry = entities.registry(log, session_id)
            fold = log.get_projection(session_id).get("state") or {}
            # Registry facts win per key; the flat fold keeps every slot the
            # registry has nothing to say about (and its ``forecast`` pin, which
            # flat_render never returns).
            facts = {**fold, **entities.flat_render(registry)}
            log.append_event(
                session_id,
                "system",
                "clock",
                {
                    "event": "env_hud",
                    "statement": env_hud_statement(
                        facts,
                        {
                            "forecast": entities.scene_traits(registry).get(
                                "forecast", ""
                            )
                            or _legacy_forecast(state.get("bts_result"))
                        },
                        latest_env_hud(log, session_id),
                    ),
                },
            )
        except Exception as exc:  # never fatal
            log.append_event(
                session_id,
                "system",
                "clock",
                {"event": "env_hud_failed", "error": str(exc)[:500]},
            )
        _emit_stage(_stream_writer(), "env_hud", "done")
        return {}

    return env_hud


def _legacy_forecast(bts_result: Any) -> str:
    """A pre-W9.29 producer's own ``forecast`` field, or ``""``.

    The action-loop runner (the production seam) no longer reports a forecast —
    the registry's scene trait is the tracker's answer now — but a legacy
    ``world_state`` callable (a test double, a pre-regime seam) still may, and
    its value is the ladder's rung 2 when the registry has no trait. Read
    tolerantly: a dataclass or a dict, never a raise.
    """
    if bts_result is None:
        return ""
    value = getattr(bts_result, "forecast", None)
    if value is None and isinstance(bts_result, dict):
        value = bts_result.get("forecast")
    return str(value or "").strip()


def make_attribution_node(*, log: LogPort, attribution: Any | None = None):
    """Build the ``attribution`` node: first-sighting speaker colours (B5).

    ``attribution`` is the post-turn speaker seam — the palette-backed markup
    pass (:class:`fabulist.state.attribution.MarkupAttributionRunner`), which
    costs no model call. Best-effort, unchanged: a raising seam appends
    ``attribution_failed`` and never breaks the turn, and an unresolvable line
    is simply omitted.
    """

    async def attribution_node(state: TurnState) -> dict:
        if attribution is None:
            return {}
        session_id = state["session_id"]
        prose = "".join(state.get("narrative_parts", []))
        _emit_stage(_stream_writer(), "attribution", "start")
        rows = await _run_attribution(
            attribution, log, session_id, state["user_content"], prose
        )
        _emit_stage(_stream_writer(), "attribution", "done")
        return {"attribution_rows": rows}

    return attribution_node


# -- npc_lanes (W9.13 fan-out) ------------------------------------------------

#: The lane JSON contract's line sanitiser: a lane may not inject the
#: composition surface or a markdown fence (a lane is a decision pass, not a
#: second narrator — ``validate_dsl`` must never see lane text).
_LANE_FENCE_RE = re.compile(r"```")
_LANE_STATEMENT_RE = re.compile(r"\b[A-Z][A-Za-z0-9_]*\s*=\s*[A-Z][A-Za-z0-9_]*\s*\(")


def sanitize_lane_line(text: Any) -> str:
    """Strip a wrapping quote pair; drop a fenced or UI-statement line."""
    from fabulist.prompts.npc import MAX_LINE_CHARS

    line = " ".join(str(text or "").split())
    if not line or _LANE_FENCE_RE.search(line) or _LANE_STATEMENT_RE.search(line):
        return ""
    if len(line) > 1 and line[0] == line[-1] and line[0] in "\"'\u201c\u201d\u2018\u2019":
        line = line[1:-1].strip()
    return line[:MAX_LINE_CHARS].strip()


def validate_lane_payload(payload: Any) -> dict | None:
    """The lane's shape boundary: ``{intent, narrative, lines}`` or ``None``.

    ``structured_result`` already refused the transport failure classes; this is
    the per-agent schema check the W8.5 contract leaves to the parser. A shape
    problem is a skipped lane, never a failed turn.
    """
    from fabulist.prompts.npc import MAX_INTENT_CHARS, MAX_LINES

    if not isinstance(payload, Mapping):
        return None
    intent = " ".join(str(payload.get("intent") or "").split())[:MAX_INTENT_CHARS]
    narrative = " ".join(str(payload.get("narrative") or "").split())[:MAX_INTENT_CHARS]
    raw_lines = payload.get("lines")
    if raw_lines is None:
        raw_lines = []
    if not isinstance(raw_lines, list):
        return None
    lines: list[dict] = []
    for row in raw_lines:
        if isinstance(row, Mapping):
            text, tone = row.get("text"), row.get("tone")
        else:
            text, tone = row, ""
        cleaned = sanitize_lane_line(text)
        if cleaned:
            lines.append({"text": cleaned, "tone": " ".join(str(tone or "").split())[:40]})
        if len(lines) >= MAX_LINES:
            break
    if not intent and not narrative and not lines:
        return None
    return {"intent": intent, "narrative": narrative, "lines": lines}


#: Cap on the raw capture a shape-failed lane carries onto its row (W25.5,
#: defect D-A): enough of the model's returned text to post-mortem the failure,
#: never a whole runaway completion stored in the log.
LANE_RAW_MAX_CHARS = 800


class _LaneShapeError(ValueError):
    """A lane reply parsed but failed :func:`validate_lane_payload`'s shape check.

    Carries the model's returned text so the fan-in flush can put a CLIPPED copy
    on the ``npc_lane`` row (W25.5, defect D-A): the row used to carry only
    ``{ok: false, reason: "shape"}``, which left the live failure
    unpost-mortemable. The clipped text is a diagnostic, never a re-parse input.
    """

    def __init__(self, raw: str) -> None:
        super().__init__("shape")
        self.raw = raw


def _clip_lane_raw(raw: str) -> str:
    """The first :data:`LANE_RAW_MAX_CHARS` of ``raw`` (no ellipsis, no rewrite)."""
    return str(raw or "")[:LANE_RAW_MAX_CHARS]


def render_private_block(context: Mapping) -> str:
    """The lane's private sections (identity anchor, own state, witness, beat).

    Small on purpose: the shared prefix already carries the whole cast and the
    transcript, so this block adds only what a single character would know — and
    it is append-only after the shared prefix (the cache contract, D2).
    """
    from fabulist.prompts.npc import MAX_INTENT_CHARS

    sections: list[str] = []
    name = str(context.get("name") or "")
    aliases = [str(alias) for alias in context.get("aliases") or [] if str(alias).strip()]
    anchor = f"You are {name}"
    if aliases:
        anchor += f" (also called {', '.join(aliases)})"
    sections.append(anchor + ".")

    categories = context.get("categories") or {}
    if isinstance(categories, Mapping) and categories:
        rows: list[str] = []
        for category, attrs in categories.items():
            if isinstance(attrs, Mapping):
                pairs = "; ".join(f"{key}: {value}" for key, value in attrs.items())
            else:
                pairs = str(attrs)
            if pairs:
                rows.append(f"- {category}: {pairs}")
        if rows:
            sections.append("How you currently are:\n" + "\n".join(rows))

    witnessed = context.get("witnessed")
    if witnessed:
        present_turns = ",".join(str(turn) for turn in witnessed)
        sections.append(f"You were in the scene for turn(s) {present_turns} of the recent turns above.")

    beat = " ".join(str(context.get("beat") or "").split())[:MAX_INTENT_CHARS]
    if beat:
        sections.append(f"This beat: {beat}")

    last_intent = " ".join(str(context.get("last_intent") or "").split())[:MAX_INTENT_CHARS]
    if last_intent:
        sections.append(f"Last beat you were trying to: {last_intent}")
    return "\n\n".join(sections)


def render_lane_block(npc_lanes: Mapping | None) -> str:
    """The narrator's ONE bottom block, or ``""`` when there are no lanes.

    Empty with no lanes is the load-bearing property: a zero-lane turn's narrator
    prompt stays byte-identical to the pre-W9.13 turn.

    Issue #33: the header frames the rows as the TRACKED cast — a dynamic list
    — never as "the characters this turn may show"; a summoned character the
    rows do not list may still appear and speak, and the full palette colour
    slot list rides the header so an untracked speaker can be coloured ahead
    of cast admission (the SPEAKER PALETTE block lists only tracked members).
    """
    if not npc_lanes:
        return ""
    rows: list[str] = []
    for lane in npc_lanes.values():
        if not isinstance(lane, Mapping):
            continue
        name = str(lane.get("name") or "")
        intent = " ".join(str(lane.get("intent") or "").split())
        narrative = " ".join(str(lane.get("narrative") or "").split())
        lines = [str(row.get("text") or "") for row in lane.get("lines") or [] if isinstance(row, Mapping)]
        parts = [f"- {name}"]
        if intent:
            parts.append(f"intends: {intent}")
        if narrative:
            parts.append(f"does: {narrative}")
        if lines:
            parts.append("says aloud (verbatim): " + " / ".join(f'"{line}"' for line in lines))
        else:
            parts.append("says nothing this beat")
        rows.append(" \u2014 ".join(parts))
    if not rows:
        return ""
    from fabulist.api.cast_api import SPEAKER_PALETTE, SPEAKER_PALETTE_NAMES

    colours = ", ".join(
        f"{label} {hex_colour}"
        for label, hex_colour in zip(SPEAKER_PALETTE_NAMES, SPEAKER_PALETTE)
    )
    return (
        "NPC intentions this turn \u2014 the tracked cast's decided beats, "
        "synthesized into the scene, never quoted from this block. The tracked "
        "cast is a dynamic list, not the whole cast of the scene: a character "
        "this block does not list may still be present, may enter, and may "
        "answer the player directly \u2014 this block never limits who may "
        "appear or speak. Each listed character's quoted line is stitched "
        "verbatim (wording and punctuation) inside the prose; a listed "
        "character marked silent stays silent. Do not invent a second line for "
        "a character who spoke here.\n"
        "To colour a spoken line for a character with no palette entry yet, "
        "wrap it in any palette colour not already used in the SPEAKER PALETTE "
        f"block: {colours}.\n"
        + "\n".join(rows)
    )


#: W9.17: the lane CONCURRENCY bound, one pool per (event loop, width).
#:
#: MODULE-level on purpose: a ``Send`` fans out into independent node
#: invocations, so a per-turn pool would let two concurrent turns put 2 x width
#: requests in flight. The width comes from the topology node's ``max_lanes``
#: data; the binding stays lazy (``asyncio.Semaphore`` binds to the running loop
#: on first use) because the tests drive the graph through one loop per run.
_LANE_POOLS: "weakref.WeakKeyDictionary[Any, dict[int, asyncio.Semaphore]]" = (
    weakref.WeakKeyDictionary()
)


def _lane_pool(width: int) -> asyncio.Semaphore:
    """The process-wide lane pool of ``width`` for the RUNNING event loop."""
    loop = asyncio.get_running_loop()
    pools = _LANE_POOLS.get(loop)
    if pools is None:
        pools = {}
        _LANE_POOLS[loop] = pools
    pool = pools.get(width)
    if pool is None:
        pool = asyncio.Semaphore(width)
        pools[width] = pool
    return pool


class NpcLaneFanout:
    """The W9.17 NPC fan-out: a REAL LangGraph ``Send`` fan-out per member.

    Owner spec (W9.13, now real topology): **every** active cast member gets a
    lane every turn — the lane SET is not capped; ``max_lanes`` bounds
    CONCURRENCY, not the set. A lane returns ``{intent, narrative, lines}``; the
    narrator synthesises intents/narrative into the turn and stitches the lines
    verbatim (D5). Best-effort throughout: a failed lane is skipped and
    recorded, never raised into the turn.

    One factory builds the fan-out's halves because they share one closure (the
    lane caller, the lane flow, the pool width). ``build_graph`` reads them off
    this object: it calls it as the ``npc_lanes`` node, wires :meth:`route` as
    that node's conditional edge, adds :meth:`lane` as ``npc_lane``, and attaches
    :meth:`join` to the node after the fan-in (see the module notes in
    ``graph.py``).

    * :meth:`__call__` — DISPATCHER (node ``npc_lanes``): resolve the ACTIVE
      cast through the ladder, apply the session's dispatch policy (W9.24 §3.5,
      :func:`fabulist.state.presence.select_dispatch_members`), write the
      per-member rows to ``lane_plan`` and the fan-out's bookkeeping to
      ``lane_meta``, reset ``npc_lanes``. No lane model call (the ``decision``
      strategy spends ONE d1-3b ask, not a lane).
    * :meth:`route` — the conditional-edge router: build the shared narrator
      prefix ONCE per turn (cache contract: the STABLE PREFIX is built once and
      reused by every lane) and return one ``Send("npc_lane", payload)`` per row.
      An empty list means "no lanes": the builder takes this node's plain-edge
      target instead.
    * :meth:`lane` — node ``npc_lane``: ONE member. A ``Send`` task does not see
      the graph state, so the payload carries everything it needs. It runs under
      :func:`_lane_pool` and returns its PARTIAL channels.
    * :meth:`join` — the fan-in flush: append one ``npc_lane`` row per member in
      DISPATCHER order (never completion order), then the ``npc_lanes`` summary
      the lane panel reads. Idempotent within a turn via ``lane_meta["emitted"]``.
    """

    #: The node id this fan-out routes ``Send`` tasks to. Declared so
    #: ``build_graph`` can refuse a topology whose fan-out edge points elsewhere
    #: (the router names it, so a mismatch would only fail at run time).
    fanout_node = "npc_lane"

    def __init__(
        self,
        *,
        log: LogPort,
        build_messages: Callable[[TurnState], list[dict]],
        caller: Any | None = None,
        config: Mapping | None = None,
        lane_messages: Callable[[TurnState, Mapping], list[dict]] | None = None,
        request_profile_fn: Callable[[], dict | None] | None = None,
        decision: Any | None = None,
        provider_label_fn: Callable[[], str] | None = None,
    ) -> None:
        self.log = log
        self.build_messages = build_messages
        self.caller = caller
        self.lane_messages = lane_messages
        self.request_profile_fn = request_profile_fn
        #: The model the lanes are bound to, read once per fan-out for the abort
        #: message a provider failure raises (``providers_errors``).
        self.provider_label_fn = provider_label_fn
        # W9.24 §3.5: the shared d1-3b caller the ``decision`` dispatch strategy
        # asks. ``None`` keeps the strategy fail-open (today's lane set), so a
        # graph built without the wiring still turns.
        self.decision = decision
        nodes_config = dict(config or {})
        self.pool_size = max(1, int(nodes_config.get("max_lanes") or 0) or 4)
        self.presence_window = int(nodes_config.get("presence_window") or 0) or 3

    # -- the dispatcher node -------------------------------------------------
    async def __call__(self, state: TurnState) -> dict:
        log = self.log
        session_id = state["session_id"]
        writer = _stream_writer()
        _emit_stage(writer, "lanes", "start")
        meta: dict[str, Any] = {
            "source": "none",
            "candidates": 0,
            "ran": 0,
            "max_lanes": self.pool_size,
            "batches": 0,
            "skipped": [],
            "members": [],
            # The direct-address floor's members (owner ruling): candidates the
            # turn's player message NAMED, so they lane whatever the strategy
            # answered. Always a subset of ``members``; ``[]`` when none.
            "forced": [],
            # The fan-in join's per-turn idempotency flag (the critic's ``fail``
            # edge re-enters ``narrate``, which carries the join).
            "emitted": False,
        }
        try:
            from fabulist.state.presence import (
                LaneConfig,
                active_members,
                direct_address_members,
                last_intents,
                member_categories,
                npc_dispatch_policy,
                select_dispatch_members,
                witnessed_turns,
            )

            # W9.24 §3.5: the session's dispatch policy, folded BEFORE the
            # ladder so every return path reports it. ``strategy`` names the
            # policy and ``max_dispatch`` its SET bound (0 = unbounded);
            # ``dispatched`` is set once the selection ran (absent = this turn
            # never reached it, so no summary row claims a zero).
            policy = npc_dispatch_policy(log, session_id)
            meta.update(
                strategy=policy.strategy,
                max_dispatch=policy.max_dispatch,
            )

            # Seed admits are minted LAZILY (``ensure_roster`` is idempotent:
            # once the chain carries any ``cast_admit`` it is a pure read). On a
            # fresh chat NOTHING has called it yet at dispatch time — the first
            # call otherwise lands post-narrate — so the bare ``roster`` fold is
            # empty exactly when the SEED FLOOR (presence rung 3) needs the
            # card/world members. Mint them HERE, before the ladder runs
            # (live W9.21: turn 1 of a world chat laned "no active cast"
            # without this, the unit test having hand-called ensure_roster).
            from fabulist.state.cast import ensure_roster

            ensure_roster(log, session_id)
            members, source = active_members(
                log,
                session_id,
                cfg=LaneConfig(
                    max_lanes=self.pool_size, presence_window=self.presence_window
                ),
            )
            meta.update(source=source, candidates=len(members))
            if not members or self.caller is None:
                _emit_stage(writer, "lanes", "done", notes=["no active cast"])
                return {"npc_lanes": {}, "lane_meta": meta, "lane_plan": []}

            prior_intents = last_intents(log, session_id)
            beat = str(state.get("plan") or "")
            # The turn's own player message: the decision ask's state line and
            # the direct-address floor both read it (presence.py).
            user_content = str(state.get("user_content") or "")

            # The policy narrows the ladder's set; it never widens it —
            # eligibility stays a tier fact — and an empty answer is a legal
            # turn (the narrator's zero-lane path). The direct-address floor
            # unions back in whatever the player named, for every strategy.
            dispatched = await select_dispatch_members(
                log,
                session_id,
                members,
                self.decision,
                policy,
                beat=beat,
                user_content=user_content,
            )
            meta.update(
                dispatched=len(dispatched),
                # Every member the floor named IS dispatched (the floor is a
                # union), so this is the payload's "forced" set in candidate
                # order — the inspector's answer to "why did this lane run
                # when the model rated it near zero".
                forced=direct_address_members(log, session_id, members, user_content),
            )
            if not dispatched:
                # No Send and no lane rows: the fan-in ``join`` still writes the
                # summary (it is attached to narrate, which this turn reaches).
                return {"npc_lanes": {}, "lane_meta": meta, "lane_plan": []}

            from fabulist.state.cast import member_by_id, roster

            cast = roster(log, session_id)
            rows: list[dict] = []
            for member_id in dispatched:
                member = member_by_id(cast, member_id)
                if member is None:
                    continue
                rows.append(
                    {
                        "member_id": member_id,
                        "name": member.display_name,
                        "aliases": list(member.aliases),
                        "categories": member_categories(log, session_id, member_id),
                        "witnessed": witnessed_turns(
                            log, session_id, member_id, window=self.presence_window
                        ),
                        "beat": beat,
                        "last_intent": prior_intents.get(member_id, ""),
                    }
                )
            meta.update(
                members=[row["member_id"] for row in rows],
                batches=-(-len(rows) // self.pool_size) if rows else 0,
            )
            return {"npc_lanes": {}, "lane_meta": meta, "lane_plan": rows}
        except Exception as exc:  # noqa: BLE001 - the fan-out never fails a turn
            meta["error"] = str(exc)[:500]
            _emit_stage(writer, "lanes", "done", notes=["fan-out failed (non-fatal)"])
            return {"npc_lanes": {}, "lane_meta": meta, "lane_plan": []}

    # -- the conditional-edge router ----------------------------------------
    async def route(self, state: TurnState) -> list:
        """One ``Send`` per planned member; ``[]`` when the turn has no lanes.

        ASYNC on purpose: the shared prefix is the NARRATOR's prompt, built by a
        builder that reads the event log, and LangGraph runs a sync path function
        in a worker thread — where the log's SQLite connection must not be used.
        Awaiting keeps the build on the loop thread (and the build happens once
        per dispatch: the prompt rides the Send payload, a task input, so it
        never enters the checkpoint).

        Issue #56.1: the PLANNED set is recorded here, before any lane runs, so
        the live preview can draw the turn's real members while they are still
        in flight. The lane rows themselves cannot be written early — they are
        appended at the join in DISPATCHER order, never completion order — so
        without this row the drawer had nothing to read until the fan-in, and
        showed the PREVIOUS turn's members for the whole of the current one.
        The record happens AFTER the shared prefix is built, so the narrator's
        prompt can never see it (the same rule the join's rows follow).
        """
        from langgraph.types import Send

        rows = [row for row in state.get("lane_plan") or [] if isinstance(row, Mapping)]
        if self.caller is None:
            # Nothing can run: the stage is declared but unwired (direct node
            # calls / tests). Recording a plan nothing will ever settle would
            # leave the preview permanently "in flight".
            return []
        if not rows:
            self._record_plan(state, [])
            return []
        shared = self.build_messages(state)
        profile = self.request_profile_fn() if self.request_profile_fn else None
        self._record_plan(state, rows)
        return [
            Send(
                "npc_lane",
                {
                    "session_id": state["session_id"],
                    "member": dict(row),
                    # Built ONCE above and read-only afterwards (the vision path
                    # mutates its own return value; lane code must never extend
                    # this in place).
                    "shared": shared,
                    "max_lanes": self.pool_size,
                    "profile": profile,
                },
            )
            for row in rows
        ]

    # -- the per-member node ------------------------------------------------
    def _record_plan(self, state: TurnState, rows: list[Mapping]) -> None:
        """Append the turn's PLANNED lane set (issue #56.1, the live preview).

        One ``npc_lanes_plan`` system row, in dispatcher order, carrying each
        member's id and display name — the facts the drawer draws an arm from.
        It is a DIFFERENT event name from the join's ``npc_lanes`` summary so no
        existing fold (``lane_history``, the presence walk, the cast readers)
        can mistake a plan for a completed set: this row says "these are the
        members this turn is about to lane", and the summary says "this is what
        they answered".

        A turn the POLICY emptied (candidates but zero dispatched) records an
        EMPTY plan: a zero-lane turn is a legal answer, and the preview must be
        able to tell it from a stale previous set. A turn whose ladder found no
        candidate at all records nothing — exactly like the join, which writes no
        summary then either.

        Best-effort: a log hiccup here must never fail a turn.
        """
        meta = state.get("lane_meta") or {}
        session_id = state.get("session_id")
        if not session_id or meta.get("emitted"):
            return
        if not rows and "dispatched" not in meta:
            return
        members = [
            {
                "member_id": str(row.get("member_id") or ""),
                "name": str(row.get("name") or ""),
            }
            for row in rows
            if str(row.get("member_id") or "")
        ]
        try:
            self.log.append_event(
                session_id,
                "system",
                "npc",
                {
                    "event": "npc_lanes_plan",
                    "source": str(meta.get("source") or "none"),
                    "strategy": str(meta.get("strategy") or ""),
                    "lanes": [member["member_id"] for member in members],
                    "members": members,
                    "candidates": int(meta.get("candidates") or 0),
                    "dispatched": int(meta.get("dispatched") or 0),
                    "max_lanes": int(meta.get("max_lanes") or self.pool_size),
                },
            )
        except Exception:  # noqa: BLE001 - the preview never fails a turn
            pass

    async def lane(self, payload: dict) -> dict:
        """Run ONE member's lane; return its partial channels (never raises).

        Reads only the Send payload (a fan-out task sees no graph state) and
        reports its outcome twice: the lane row itself in ``npc_lanes`` (what the
        narrator renders) and the bookkeeping in ``lane_meta`` (``ran`` +
        ``skipped``, which the reducers sum/concatenate). A FAILURE returns no
        ``npc_lanes`` key at all — an empty dict would reset a sibling's merge.
        """
        context = dict(payload.get("member") or {})
        member_id = str(context.get("member_id") or "")
        ok, reason, lane, raw = await self._call_lane(payload, context)
        if not ok:
            skipped = {
                "member_id": member_id,
                "name": context.get("name", ""),
                "reason": reason,
            }
            if raw:
                # Shape failures only: the clipped model text rides to the row.
                skipped["raw"] = raw
            return {"lane_meta": {"ran": 0, "skipped": [skipped]}}
        return {
            "npc_lanes": {member_id: {"name": context.get("name", ""), **lane}},
            "lane_meta": {"ran": 1},
        }

    async def _call_lane(
        self, payload: Mapping, context: Mapping
    ) -> tuple[bool, str, dict, str]:
        """One member's model call inside the lane pool: ``(ok, reason, lane, raw)``.

        The pool is held for the CALL only, and a CONTENT failure here never
        raises into the graph: a lane that could not answer is a skipped lane.
        ``raw`` is the CLIPPED model text a shape failure carries ("" for every
        other failure) so the flush can record what the model actually said.

        A PROVIDER failure (429/401/403/5xx, connection/transport) is the one
        exception: it aborts the turn instead of dropping the lane, because every
        other lane and the narrator ride the same dead connection — skipping one
        lane would leave the reader to meet the wall at the narrator with no
        phase named (:mod:`fabulist.graph.provider_errors`).
        """
        pool = _lane_pool(int(payload.get("max_lanes") or self.pool_size))
        try:
            async with pool:
                return True, "", await self._request(payload, context), ""
        except _LaneShapeError as exc:  # a parsed reply that failed the shape
            return False, str(exc)[:300], {}, _clip_lane_raw(exc.raw)
        except Exception as exc:  # noqa: BLE001 - a lane never fails the turn
            _raise_if_provider_failure(
                "NPC lanes", exc, _provider_label(self.provider_label_fn)
            )
            return False, str(exc)[:300], {}, ""

    async def _request(self, payload: Mapping, context: Mapping) -> dict:
        """Append the lane's private tail to the shared prefix, call, validate."""
        from fabulist.state.structured import StructuredFailure, structured_result

        shared = list(payload.get("shared") or [])
        tail = (
            self.lane_messages(dict(payload), context)
            if self.lane_messages is not None
            else default_npc_lane_messages(context)
        )
        # Issue #27: the lane tail rides INSIDE the engine's runtime bound —
        # the ONE wrapper mechanism, applied HERE at the assembly seam that
        # positions the lane messages (ADR-019: bounds are applied at seams,
        # never baked into content constants). The guard line the shared
        # prefix carries names <preset_runtime> as trusted, so the lane's own
        # engine instruction is acknowledged as engine-sourced instead of
        # sitting outside every bound — which had the guard reading our ask as
        # an injection attempt until the lane broke its JSON contract. The
        # shared prefix is untouched: the private instruction stays per-turn
        # tail content appended after the narrator's own runtime blocks,
        # never a prefix rewrite (ADR-005). Covers BOTH tail sources — the
        # code default and the preset ``npc`` flow.
        from fabulist.prompts.wrapper import wrap_runtime

        tail = [
            {**message, "content": wrap_runtime(message.get("content"))}
            for message in tail
        ]
        messages = [*shared, *tail]
        content, tool_args, finish_reason = await _drain_lane(
            self.caller, messages, payload.get("profile")
        )
        parsed = structured_result(
            content=content, tool_args=tool_args, finish_reason=finish_reason
        )
        if isinstance(parsed, StructuredFailure):
            raise ValueError(f"structured: {parsed.reason}")
        lane = validate_lane_payload(parsed)
        if lane is None:
            # W25.5 (defect D-A): keep what the model actually returned so the
            # failure is post-mortemable from the log. Content-mode is the lane
            # contract; a tool-call answer is the fallback spelling of the same
            # text (``structured_result`` parses whichever arrived).
            raise _LaneShapeError(content or tool_args)
        return lane

    # -- the fan-in flush --------------------------------------------------
    def join(self, state: TurnState) -> dict:
        """Write the fan-out's log rows at the JOIN, in dispatcher member order.

        A fan-out has no control flow of its own: the only place that runs with
        EVERY lane's result in state is the node after the fan-in, so
        ``build_graph`` attaches this to that node (``narrate`` in the canonical
        topology), before the narrator's own model call.

        Rows follow ``lane_meta["members"]`` — the dispatcher's selected set in
        candidate order, never completion order — and the ``npc_lanes`` summary
        is appended after them, which is what the lane panel reads
        (``GET /api/sessions/{id}/lanes``). Idempotent per turn (``emitted``), so
        the critic's ``fail`` edge coming back through ``narrate`` cannot
        double-write.

        A turn the POLICY emptied (candidates but zero dispatched — W9.24 §3.5)
        still writes the summary, with ``lanes: []`` and ``candidates > 0``: a
        zero-lane turn is a legal ANSWER, and an inspector must be able to tell
        it from a chat that has no cast. A chat whose ladder found no candidate
        at all writes nothing, exactly as before.
        """
        meta = state.get("lane_meta") or {}
        session_id = state.get("session_id")
        members = [str(member) for member in (meta.get("members") or [])]
        if not session_id or meta.get("emitted"):
            return {}
        if not members and "dispatched" not in meta:
            # The dispatcher never got to the policy (no active cast, no lane
            # caller): nothing claims a zero-lane verdict, so write nothing.
            return {}
        lanes = state.get("npc_lanes") or {}
        skipped = list(meta.get("skipped") or [])
        for member_id in members:
            lane = lanes.get(member_id)
            if isinstance(lane, Mapping):
                _append_lane_row(
                    self.log,
                    session_id,
                    {
                        "event": "npc_lane",
                        "member_id": member_id,
                        "name": lane.get("name", ""),
                        "ok": True,
                        "intent": lane.get("intent", ""),
                        "narrative": lane.get("narrative", ""),
                        "lines": list(lane.get("lines") or []),
                    },
                )
                continue
            failed_row = {
                "event": "npc_lane",
                "member_id": member_id,
                "name": _skipped_name(skipped, member_id),
                "ok": False,
                "reason": _skipped_reason(skipped, member_id),
            }
            raw = _skipped_raw(skipped, member_id)
            if raw:
                # Only a shape failure carries the clipped model text (W25.5).
                failed_row["raw"] = raw
            _append_lane_row(self.log, session_id, failed_row)
        _append_lane_event(self.log, session_id, members, meta)
        _emit_stage(
            _stream_writer(),
            "lanes",
            "done",
            notes=[f"{meta.get('ran', 0)} lanes in {meta.get('batches', 0)} batch(es)"],
        )
        return {"lane_meta": {"emitted": True}}


def _append_lane_row(log: LogPort, session_id: str, payload: dict) -> None:
    """Append ONE per-member lane row (best-effort: a log hiccup is not fatal)."""
    try:
        log.append_event(session_id, "system", "npc", payload)
    except Exception:  # noqa: BLE001 - the fan-out never fails a turn
        pass


def _skipped_row(skipped: list, member_id: str) -> Mapping:
    """The skipped-row a failed lane recorded for ``member_id`` (or ``{}``)."""
    for row in skipped:
        if isinstance(row, Mapping) and str(row.get("member_id")) == member_id:
            return row
    return {}


def _skipped_reason(skipped: list, member_id: str) -> str:
    return str(_skipped_row(skipped, member_id).get("reason") or "")


def _skipped_raw(skipped: list, member_id: str) -> str:
    """The clipped raw text a shape-failed lane recorded ("" when none)."""
    return str(_skipped_row(skipped, member_id).get("raw") or "")


def _skipped_name(skipped: list, member_id: str) -> str:
    return str(_skipped_row(skipped, member_id).get("name") or "")


def make_npc_lanes_node(
    *,
    log: LogPort,
    build_messages: Callable[[TurnState], list[dict]],
    caller: Any | None = None,
    config: Mapping | None = None,
    lane_messages: Callable[[TurnState, Mapping], list[dict]] | None = None,
    request_profile_fn: Callable[[], dict | None] | None = None,
    decision: Any | None = None,
    provider_label_fn: Callable[[], str] | None = None,
) -> NpcLaneFanout:
    """Build the ``npc_lanes`` DISPATCHER node — the head of the ``Send`` fan-out.

    The returned :class:`NpcLaneFanout` is callable AS the node (so
    ``build_graph(npc_lanes=...)`` and the direct ``await node(state)`` seam are
    unchanged) and carries the fan-out's other halves — :meth:`NpcLaneFanout.route`
    and :meth:`NpcLaneFanout.lane` — because they share this closure. See the
    class docstring for the per-half contract. ``decision`` is the shared d1-3b
    caller the ``decision`` dispatch strategy asks (W9.24 §3.5); without it that
    strategy fails open to today's lane set. ``provider_label_fn`` is the live
    reader of the model the lanes are bound to, for a provider failure's abort
    message.
    """
    return NpcLaneFanout(
        log=log,
        build_messages=build_messages,
        caller=caller,
        config=config,
        lane_messages=lane_messages,
        request_profile_fn=request_profile_fn,
        decision=decision,
        provider_label_fn=provider_label_fn,
    )


def _append_lane_event(
    log: LogPort, session_id: str, member_ids: list[str], meta: Mapping
) -> None:
    """Record ONE ``npc_lanes`` summary row (the lane set used + its outcome).

    Carries the turn's dispatch POLICY (W9.24 §3.5: ``strategy`` +
    ``max_dispatch``) next to the ladder's ``candidates`` and the
    ``dispatched``/``ran`` counts, so the inspector can answer "why did only
    two of five lane" without re-deriving the fold. ``forced`` (additive, owner
    ruling 2026-10-21) names the members the DIRECT-ADDRESS floor dispatched —
    candidates the turn's player message named — so a lane row the decision
    model rated near zero is explainable from the summary alone. A pre-change
    payload simply has no ``forced`` key and reads as "none" (``.get`` on the
    read side, never a required field).

    Free of anything the narrator's message builder scans (no ``card``, no
    ``text``) so appending it before ``narrate`` cannot perturb the narrator's
    rebuild.
    """
    log.append_event(
        session_id,
        "system",
        "npc",
        {
            "event": "npc_lanes",
            "source": str(meta.get("source") or "none"),
            "strategy": str(meta.get("strategy") or ""),
            "max_dispatch": int(meta.get("max_dispatch") or 0),
            "lanes": list(member_ids),
            "candidates": int(meta.get("candidates") or 0),
            "dispatched": int(meta.get("dispatched") or 0),
            "forced": [str(member_id) for member_id in (meta.get("forced") or [])],
            "ran": int(meta.get("ran") or 0),
            "max_lanes": int(meta.get("max_lanes") or 0),
            "skipped": list(meta.get("skipped") or []),
        },
    )


async def _drain_lane(
    caller: Any, messages: list[dict], request_profile: dict | None
) -> tuple[str, str, str | None]:
    """Drain one lane call into ``(content, tool_args, finish_reason)``.

    Tier A tool-call deltas win over content (the ``collect_structured`` rule);
    a ``finish_reason=length`` error frame is surfaced so ``structured_result``
    refuses a truncated payload. No ``tool_choice`` is forced anywhere (R29 is
    an open defect), so this is correct with or without function calling.
    """
    tool_parts: list[str] = []
    content_parts: list[str] = []
    finish_reason: str | None = None
    kwargs = {} if request_profile is None else {"request_profile": request_profile}
    async for event in caller(messages, True, **kwargs):
        kind = event.get("type")
        if kind == "tool_call":
            tool_parts.append(event.get("delta", ""))
        elif kind == "content":
            content_parts.append(event.get("delta", ""))
        elif kind == "error":
            meta = event.get("meta") or {}
            if meta.get("finish_reason"):
                finish_reason = str(meta["finish_reason"])
    return "".join(content_parts), "".join(tool_parts), finish_reason


def default_npc_lane_messages(context: Mapping) -> list[dict]:
    """The default lane tail: the brief on the assistant carrier + the JSON ask.

    Issue #73: this tail is APPENDED after the narrator's history, so the brief
    must not be a ``system`` message (the central guard refuses a non-leading
    system row, and a stock Jinja template 500s on it). It rides
    ``ENVELOPE_ROLE`` through the SAME helper the preset-compiled ``npc`` flow
    uses (:func:`fabulist.prompts.modules.carry_lane_brief`), so the two paths
    cannot disagree about the shape.

    The wiring replaces this with the preset-compiled ``npc`` flow; keeping a
    default here means the node is testable (and usable) without a preset.
    """
    from fabulist.prompts.modules import carry_lane_brief
    from fabulist.prompts.npc import build_lane_ask, build_lane_system

    name = str(context.get("name") or "")
    return carry_lane_brief(
        [
            {
                "role": "system",
                "content": build_lane_system(name, render_private_block(context)),
            },
            {"role": "user", "content": build_lane_ask(name)},
        ]
    )


class AssetProposalFanout:
    """The W9.22 §3 proposal fan-out: three decision-gated, type-scoped lanes.

    The stage replaces the tail's single unwired box with a REAL ``Send``
    fan-out — one lane per DATA TYPE (world / actors / books), in parallel
    (doctrine 5 / req. 18) — and it is built from four halves that share one
    closure, exactly like :class:`NpcLaneFanout`:

    * :meth:`__call__` — DISPATCHER (node ``asset_proposals``): the DOUBLE GATE.
      It computes the turn's touched-entity evidence and, only if that is
      non-empty, asks the decision model ONE cheap should-we-propose question.
      Either "no" returns an empty plan: no ``Send``, no generator call, no
      decision call beyond the one it had to make to say no, and NOTHING
      appended to the log (plan §3 / reviewer finding 9: the tail is on the
      turn's critical path). It writes ``proposal_plan`` + ``proposal_meta``
      and calls no content model.
    * :meth:`route` — the conditional-edge router: one ``Send`` per planned
      data type, each carrying the lane's OWN messages (the preset ``proposals``
      flow, or the code default), the digest it may amend, and the resolved
      per-item question. ASYNC for the ``NpcLaneFanout.route`` reason: the
      digest and the flow both read the log, which must happen on the loop
      thread.
    * :meth:`lane` — node ``asset_proposal_lane``: ONE extractor-class generator
      call (doctrine 4: authoring is generation, never the decision model), then
      a per-item decision-model judgment. It returns its judged rows; it does
      not write the log.
    * :meth:`join` — the fan-in flush: ONE ``asset_proposal`` event per kept row
      and ONE ``asset_proposals`` summary row, appended in DISPATCHER order
      (never completion order — the W9.17 rule). Idempotent via
      ``proposal_meta["emitted"]``.

    Best-effort throughout: a failed lane appends its reason to the summary and
    never fails the turn; nothing here ever writes an asset (the review home
    disposes — doctrine 3).
    """

    #: The node id this fan-out routes ``Send`` tasks to. Declared so
    #: ``build_graph`` can refuse a topology whose fan-out edge points elsewhere
    #: (the router names it, so a mismatch would only fail at run time).
    fanout_node = "asset_proposal_lane"

    def __init__(
        self,
        *,
        log: LogPort,
        caller: Any | None = None,
        decision: Any | None = None,
        lane_messages: Callable[[Mapping], list[dict]] | None = None,
    ) -> None:
        from fabulist.state.proposal_lanes import PROPOSAL_LANES, ProposalGate

        self.log = log
        #: The lane's GENERATOR (extractor-class). ``None`` = no lane may run.
        self.caller = caller
        #: The two judgments' decision seam (d1-3b). ``None`` = the gate answers
        #: "no", so nothing is ever proposed — the safe direction here.
        self.gate = ProposalGate(decision)
        #: The lane's prompt flow (the preset's ``proposals`` interaction).
        self.lane_messages = lane_messages
        #: The lane SET is exactly the three data types, so the pool width IS
        #: the set: a wider pool cannot be used and a narrower one would
        #: serialize the stage's whole point (doctrine 5 / req. 18).
        self.pool_size = max(1, len(PROPOSAL_LANES))

    # -- the dispatcher node -------------------------------------------------
    async def __call__(self, state: TurnState) -> dict:
        """Gate the stage ONCE, then plan one row per data type (no model call).

        Returns the plan rows and the bookkeeping. The five "no" answers are
        distinguishable in ``proposal_meta["gate"]`` (``no_evidence`` / ``error``
        / ``no_generator`` / ``gate_off`` / ``verdict_no``) plus ``unsure`` for a
        verdict that was never really given (confidence below
        :data:`fabulist.state.proposal_lanes.GATE_CONFIDENCE_FLOOR`) so an
        inspector can tell a quiet turn from a broken one or an unjudged window —
        but none of them appends a log row, which is the zero-cost contract this
        stage is measured on.
        """
        from fabulist.state.proposal_lanes import (
            PROPOSAL_LANES,
            gate_questions,
            gate_state,
            proposal_evidence,
            touched_for,
        )

        session_id = state["session_id"]
        meta: dict[str, Any] = {
            "gate": "no_evidence",
            "lanes": [],
            "touched": 0,
            "emitted": False,
        }
        try:
            evidence = proposal_evidence(self.log, session_id, state)
        except Exception:  # noqa: BLE001 - evidence is a read, never fatal
            logger.warning(
                "proposal evidence failed for %s", session_id, exc_info=True
            )
            return {"proposal_plan": [], "proposal_meta": {**meta, "gate": "error"}}
        meta["touched"] = evidence.count()
        # Gate (a): an untouched turn costs NOTHING — not even the verdict ask.
        if not evidence.touched:
            return {"proposal_plan": [], "proposal_meta": meta}
        # No generator wired (a graph built without the proposal binding): a
        # verdict would have nothing to unlock, so do not ask one.
        if self.caller is None:
            return {
                "proposal_plan": [],
                "proposal_meta": {**meta, "gate": "no_generator"},
            }
        questions = gate_questions(self.log)
        if questions is None or not self.gate.enabled:
            return {"proposal_plan": [], "proposal_meta": {**meta, "gate": "gate_off"}}
        # Gate (b): ONE cheap choice question over the evidence and the window.
        # The prose rides through ONE STRING PER TURN: the gate renders a line
        # each (D81), so a pre-joined blob would read as a single turn.
        parts = list(state.get("narrative_parts") or [])
        verdict = await self.gate.should_propose(
            questions, gate_state(evidence, parts)
        )
        if verdict.unsure:
            # The answer was in the model's guessing regime (confidence below the
            # floor): the window was never really judged, so it is neither a
            # "no" nor a run — no lane may start, and the run says so.
            return {"proposal_plan": [], "proposal_meta": {**meta, "gate": "unsure"}}
        if not verdict.proceed:
            return {"proposal_plan": [], "proposal_meta": {**meta, "gate": "verdict_no"}}
        plan = [
            {
                "lane": spec.lane,
                "kind": spec.kind,
                "touched": list(touched_for(spec, evidence)),
                # Resolved ONCE here: every lane judges with the wording this
                # turn gated on (an owner edit mid-turn cannot split the rubric).
                "judge_question": dict(questions.item),
                "judge_threshold": questions.item_threshold,
            }
            for spec in PROPOSAL_LANES
        ]
        meta["gate"] = "proceeded"
        meta["lanes"] = [row["lane"] for row in plan]
        return {"proposal_plan": plan, "proposal_meta": meta}

    # -- the conditional-edge router ----------------------------------------
    async def route(self, state: TurnState) -> list:
        """One ``Send`` per planned data type; ``[]`` when the gate said no.

        ASYNC on purpose (the :meth:`NpcLaneFanout.route` reason): the digest and
        the preset flow both read the event log, and LangGraph runs a sync path
        function in a worker thread — where the log's SQLite connection must not
        be used. The digest is read HERE, after the verdict, so a gated-off turn
        pays neither the fold nor the prompt.
        """
        from langgraph.types import Send

        from fabulist.prompts.proposals import build_lane_payload, build_lane_system
        from fabulist.state.proposal_lanes import (
            MAX_ITEMS,
            lane_spec,
            patch_fields,
            stored_digest,
        )

        plan = [
            row
            for row in state.get("proposal_plan") or []
            if isinstance(row, Mapping)
        ]
        if not plan or self.caller is None:
            return []
        prose = "".join(state.get("narrative_parts") or [])
        sends: list = []
        for row in plan:
            spec = lane_spec(row.get("lane"))
            if spec is None:
                continue
            digest = stored_digest(self.log, spec)
            blocks = {
                "system": build_lane_system(
                    lane_label=spec.label,
                    kind=spec.kind,
                    fields=patch_fields(spec.kind),
                    max_items=MAX_ITEMS,
                ),
                "payload": build_lane_payload(
                    lane=spec.lane,
                    kind=spec.kind,
                    touched=row.get("touched") or [],
                    digest=digest,
                    prose=prose,
                ),
            }
            sends.append(
                Send(
                    "asset_proposal_lane",
                    {
                        "session_id": state["session_id"],
                        "lane": spec.lane,
                        "kind": spec.kind,
                        "messages": self._messages(blocks),
                        "judge_question": dict(row.get("judge_question") or {}),
                        "judge_threshold": row.get("judge_threshold"),
                        # The digest is ALSO the write scope: a lane may amend
                        # only assets it was shown (the parser resolves targets
                        # against this list and refuses an invented id).
                        "targets": [
                            {"id": cell["id"], "name": cell["name"]}
                            for cell in digest
                        ],
                        "prose": prose,
                    },
                )
            )
        return sends

    def _messages(self, blocks: Mapping[str, str]) -> list[dict]:
        """The lane's messages: the preset ``proposals`` flow, else the default.

        A broken flow never kills a lane (the ``_npc_flow_messages`` rule): the
        code default is the same text the flow's default rows carry, so the lane
        still runs — and a lane that cannot run at all would silently stop
        proposing, which is the failure this stage exists to avoid.
        """
        from fabulist.prompts.proposals import default_lane_messages

        default = default_lane_messages(str(blocks["system"]), str(blocks["payload"]))
        if self.lane_messages is None:
            return default
        try:
            messages = self.lane_messages(dict(blocks))
        except Exception:  # noqa: BLE001 - a broken flow never kills a lane
            logger.warning(
                "proposals flow fell back to the code default", exc_info=True
            )
            return default
        return messages or default

    # -- the per-data-type node ---------------------------------------------
    async def lane(self, payload: dict) -> dict:
        """Run ONE data type's lane; return its partial channels (never raises).

        One extractor-class GENERATOR call (the authoring half of doctrine 4),
        then one decision-model judgment per authored row (the judging half).
        Kept rows are returned for the fan-in to append; judged-out rows are
        counted and dropped SILENTLY (no event — the plan's contract). A failed
        lane returns no ``proposal_rows`` key at all (an empty one would reset a
        sibling lane's merge) and records its reason for the summary.
        """
        from fabulist.state.proposal_lanes import lane_spec, parse_proposal_batch
        from fabulist.state.structured import StructuredFailure, structured_result

        lane_id = str(payload.get("lane") or "")
        spec = lane_spec(lane_id)
        if spec is None:
            return self._failed(lane_id, "unknown lane")
        try:
            pool = _lane_pool(self.pool_size)
            async with pool:
                content, tool_args, finish_reason = await _drain_lane(
                    self.caller, list(payload.get("messages") or []), None
                )
        except Exception as exc:  # noqa: BLE001 - a lane never fails the turn
            return self._failed(spec.lane, str(exc)[:300])
        try:
            parsed = structured_result(
                content=content, tool_args=tool_args, finish_reason=finish_reason
            )
            if isinstance(parsed, StructuredFailure):
                raise ValueError(f"structured: {parsed.reason}")
            rows, dropped = parse_proposal_batch(
                parsed, spec, payload.get("targets") or []
            )
        except Exception as exc:  # noqa: BLE001 - a lane never fails the turn
            return self._failed(spec.lane, str(exc)[:300])
        kept, judged_out = await self._judge(spec, payload, rows)
        return {
            "proposal_rows": {spec.lane: kept},
            "proposal_meta": {
                "dropped": [
                    *({"lane": spec.lane, **row} for row in dropped),
                    *judged_out,
                ]
            },
        }

    async def _judge(
        self, spec: "LaneSpec", payload: Mapping, rows: list[dict]
    ) -> tuple[list[dict], list[dict]]:
        """Judge every authored row; ``(kept, dropped)`` (doctrine 4).

        One should-this-propose question per row, over the row + its evidence +
        the turn: does the patch reflect what the story established, and would it
        corrupt the owner's asset? A row is kept when P(propose) clears the
        question's own threshold (owner-set, ``DEFAULT_THRESHOLD`` otherwise); an
        unanswerable judgment is a DROP, never a kept row — a proposal nobody
        judged would put an unchecked patch in front of the owner.
        """
        from fabulist.decision_queries import DEFAULT_THRESHOLD
        from fabulist.state.proposal_lanes import judgment_state

        question = payload.get("judge_question")
        question = dict(question) if isinstance(question, Mapping) else {}
        try:
            threshold = float(payload.get("judge_threshold", DEFAULT_THRESHOLD))
        except (TypeError, ValueError):
            threshold = DEFAULT_THRESHOLD
        prose = str(payload.get("prose") or "")
        kept: list[dict] = []
        dropped: list[dict] = []
        for row in rows:
            probability = await self.gate.judge_item(
                question, judgment_state(row, prose)
            )
            if probability is None:
                dropped.append({"lane": spec.lane, "reason": "no_verdict"})
            elif probability < threshold:
                dropped.append({"lane": spec.lane, "reason": "verdict_no"})
            else:
                kept.append({**row, "signal": probability})
        return kept, dropped

    @staticmethod
    def _failed(lane: str, reason: str) -> dict:
        """A failed lane's partial return: no rows, one summary reason."""
        return {"proposal_meta": {"failed": [{"lane": lane, "reason": reason}]}}

    # -- the fan-in flush --------------------------------------------------
    def join(self, state: TurnState) -> dict:
        """Append the judged rows and the ONE summary, in DISPATCHER order.

        A fan-out has no control flow of its own, so ``build_graph`` attaches
        this to the first node after the fan-in (the canonical ``done``), where
        every lane's result is in state. The rows follow
        ``proposal_meta["lanes"]`` — the dispatcher's data-type order, never
        completion order — and the summary lands last so the inspector reads the
        stage's outcome as one row.

        Nothing is appended when the double gate dispatched no lane: a quiet turn
        must leave the log byte-identical to a turn without the stage (that is
        what the zero-cost contract is checked on). Idempotent per turn via
        ``emitted``, like every fan-in in this graph.
        """
        from fabulist.state.proposal_lanes import proposal_event_payload, summary_payload

        meta = state.get("proposal_meta") or {}
        session_id = state.get("session_id")
        lanes = [str(lane) for lane in (meta.get("lanes") or [])]
        if not session_id or not lanes or meta.get("emitted"):
            return {}
        rows = state.get("proposal_rows") or {}
        for lane in lanes:
            for row in rows.get(lane) or []:
                _append_event_quietly(
                    self.log, session_id, proposal_event_payload(row)
                )
        _append_event_quietly(self.log, session_id, summary_payload(meta, rows))
        return {"proposal_meta": {"emitted": True}}


def _append_event_quietly(log: LogPort, session_id: str, payload: dict) -> None:
    """Append one ``system`` row; a log hiccup is not the turn's problem.

    The same discipline as :func:`_append_lane_row`: the proposal stage is tail
    housekeeping (D9/D10), so a refused append costs that row, never the turn.
    """
    try:
        log.append_event(session_id, "system", "bts", payload)
    except Exception:  # noqa: BLE001 - the fan-out never fails a turn
        pass


def make_asset_proposals_node(
    *,
    log: LogPort,
    caller: Any | None = None,
    decision: Any | None = None,
    lane_messages: Callable[[Mapping], list[dict]] | None = None,
) -> AssetProposalFanout:
    """Build the ``asset_proposals`` DISPATCHER node — the proposal fan-out.

    The returned :class:`AssetProposalFanout` is callable AS the node AND carries
    the fan-out's other halves (:meth:`AssetProposalFanout.route`,
    :meth:`AssetProposalFanout.lane`, :meth:`AssetProposalFanout.join`), because
    they share one closure — the ``make_npc_lanes_node`` contract. The wiring
    supplies all three seams (the ``proposals`` generator binding, the ONE shared
    decision caller, the preset ``proposals`` flow); a graph built without them
    dispatches ZERO lanes and appends nothing, which is what keeps every
    pre-W9.22 turn byte-identical.
    """
    return AssetProposalFanout(
        log=log, caller=caller, decision=decision, lane_messages=lane_messages
    )


def make_done_node():
    """Build the ``done`` node: the turn's terminal graph node.

    It writes nothing: every log row the turn owns was appended by the stage
    that owns it. The persisted ``turn_stages`` step trace is folded from the
    STREAMED ``stage`` frames after the graph finishes
    (``api/app.py:_drive_turn`` — ``_record_turn_stages``), so it cannot be
    produced from graph state and deliberately stays there.
    """

    async def done(state: TurnState) -> dict:  # noqa: ARG001 - terminal marker
        return {}

    return done


def make_post_turn_node(
    *,
    extractor: ExtractorFn,
    log: LogPort,
    config_snapshot: dict | None = None,
    world_state: Any | None = None,
    attribution: Any | None = None,
    critic: Any | None = None,
    decision: Any | None = None,
):
    """Build the sequential post-turn sweep over the SAME stage factories.

    The live turn runs these stages as real LangGraph nodes (``build_graph``,
    W9.6). This factory stays for callers that already hold a narrator output in
    state (direct node calls, tests, seams): it composes the identical stage
    factories in canonical topology order over one dict, once. It used to loop on
    the critic's ``fail``; W9.24 §3.3.7 retired that arm, so the sweep is linear
    in both places.

    Verdict contract: ``ok`` keeps the turn; ``diffs`` applies a patch ON TOP as
    a ``revision`` event (D10 — never an in-place rewrite); ``fail`` keeps the
    turn and logs the verdict. Nothing in the critic path is fatal — the turn
    already streamed (D49).
    """
    commit = make_commit_node(log=log, config_snapshot=config_snapshot)
    validate = make_validate_node(log=log)
    decision_gate = make_decision_gate_node(log=log, decision=decision)
    critic_node = make_critic_node(log=log, critic=critic)
    bts = make_bts_node(
        extractor=extractor, log=log, world_state=world_state, decision=decision
    )
    env_hud = make_env_hud_node(log=log)
    attribution_node = make_attribution_node(log=log, attribution=attribution)

    async def post_turn(state: TurnState) -> dict:
        updates: dict = {}

        def merged() -> TurnState:
            return {**state, **updates}

        def apply(update: dict) -> None:
            updates.update(update)

        apply(await commit(merged()))
        apply(await validate(merged()))
        apply(await decision_gate(merged()))
        apply(await critic_node(merged()))
        apply(await bts(merged()))
        apply(await env_hud(merged()))
        apply(await attribution_node(merged()))
        return {
            "deltas": updates.get("deltas", {}),
            "action_class": updates.get("action_class", state.get("action_class", "")),
        }

    return post_turn


async def _run_world_state(
    world_state: Any,
    log: LogPort,
    session_id: str,
    user_msg: str,
    prose: str,
    action_class: str,
    clock_facts: dict,
    *,
    cast_context: str | None = None,
    decision: Any | None = None,
    prior_narratives: list[dict] | None = None,
    narrative_seq: int | None = None,
) -> tuple[dict, dict | None, Any]:
    """Run the BTS action loop and commit what it recorded (W9.29 §6).

    The tracker no longer reports a payload: it CALLS write tools
    (:mod:`fabulist.state.entity_tools`) and each validated action is appended by
    the engine as it lands (D9). This node therefore does the THREE jobs the
    payload used to do:

    1. **fold the registry** (fork-aware) after the loop returned;
    2. append the per-turn ``entity_summary`` row (the turn's action counts — the
       observability seam the sidebar and the palette footer read);
    3. append the ``state-delta`` carrying the CLOCK stamp (and, on a legacy
       producer, whatever deltas that producer still reports).

    W9.32 §6 (W11) RETIRES THE FLAT SHADOW WRITES: the delta no longer merges
    :func:`fabulist.state.entities.flat_render`, because ``loc``/``weather``/
    ``tone``/``inv`` are DERIVED from the registry at every read
    (:func:`fabulist.state.entities.rendered_state`) — one copy of the fact, no
    shadow to drift. The clock keys keep their write path untouched: this row is
    time's only storage (§3.5).

    The W2-W3 COMPAT ``world_state`` render is RETIRED (§6: it retires in W4). Every
    reader is registry-first now — presence (:func:`fabulist.state.presence`),
    the HUD (:mod:`fabulist.state.env_hud`), the narrator's facts section
    (:func:`fabulist.state.bts.render_bts_section`), the planner line and the
    sidebar — and a legacy session still reads its own historical ``world_state``
    rows through the forward-only shim (§11.1), so nothing needs a fresh one.

    THE CAST DOOR SURVIVES (§6, review R1): ``apply_bts_cast_proposals`` still runs
    every turn, fed from the turn's MENTIONED-NAME SET
    (:func:`fabulist.state.entity_tools.mentioned_names` — the beings the loop's
    actions dealt with) — resolve -> alias -> D79 proposal, descriptor ->
    provisional. ``rename_being`` actions reach ``patch_member`` inside the loop, so
    a person's rename no longer needs the retired ``references[]`` channel.

    Failure  -> ``system``/``world_state_failed`` (stage ``registry``, or the
    error's own stage for the retired Tier D floor) and NO world update this turn;
    the clock's time is still persisted so it keeps ticking. Actions the loop
    appended before a failure STAND (each was validated; D9 append-only).

    TIME (W9.32 §3.5, the D3 partial reversal): the rules clock's fold is still the
    default stamp, and the ONE way a tracker moves it is an accepted proposal —
    ``propose_time`` validates against this turn's ``(time, date)`` and appends a
    ``clock_proposal`` audit row, which is read back here (rows appended after this
    turn's mark, newest wins) and folded into the same ``state-delta``. The audit
    row is provenance, never a second storage path, so the next ``pre_turn``
    continues from the stamped fold. A failed loop applies nothing: the stamp is
    read only after the whole try body succeeded, so a proposal accepted before a
    crash keeps the fold's time (the tree can never show an unapplied instant as
    applied).

    Returns ``(deltas, bts_result, world_state_event)`` — the third value is always
    ``None`` now (the compat row that carried it is retired); it stays in the
    signature so the turn state's channel keeps its shape for the historian's own
    window union (which reads the log's LEGACY rows, §11.1).
    """
    from fabulist.agents.debug import CURRENT_SESSION as _CURRENT_SESSION
    from fabulist.state import entities
    from fabulist.state.bts import BtsResult

    deltas: dict = {}
    bts_result: dict | None = None
    accepted_proposal: dict | None = None
    clock_time = clock_facts.get("time")
    # The mark that tells this turn's rows from every earlier turn's: a proposal
    # only counts when it was appended by the loop we are about to run (§3.5).
    mark = _event_mark(log, session_id)
    # D51: the notice + card context ride the BTS model input only.
    model_user_msg = (
        f"{CAST_CONTEXT_HEADER}\n{cast_context}\n\n{user_msg}"
        if cast_context
        else user_msg
    )
    turn_seq = narrative_seq if isinstance(narrative_seq, int) else _latest_narrative_seq(
        log, session_id
    )
    try:
        # The registry render IS the prior state (the palette rides the runner's
        # tail), so nothing is pushed here: the payload's PRIOR block is retired
        # with the payload (§7).
        #
        # W9.29 §6: BTS now WRITES (one append per validated action), and the
        # runner resolves its session from ``CURRENT_SESSION`` (the pump installs
        # it for the turn — the same contextvar the usage rows read). The node
        # KNOWS the session, so it states it here rather than trusting the
        # ambient value: a write pass that resolved the wrong session would
        # append to another chat's log, and that must not be spellable.
        token = _CURRENT_SESSION.set(session_id)
        try:
            result = await world_state(
                model_user_msg,
                prose,
                None,
                checkpoint=False,
                # W9.32 §3.5: the live tools judge an expiry or a time proposal
                # against THIS turn's instant. An absent/unreadable clock passes
                # nothing at all (the ledger falls back to the folded projection),
                # which is also what keeps a seam without the keyword working.
                **(
                    {"clock": (clock_time, clock_facts["date"])}
                    if clock_time and clock_facts.get("date")
                    else {}
                ),
            )
        finally:
            _CURRENT_SESSION.reset(token)
        if isinstance(result, BtsResult):
            result = dataclasses.asdict(result)
        if not isinstance(result, dict):
            raise TypeError(
                f"world-state result must be a dict/BtsResult, got {type(result)}"
            )
        bts_result = result
        entities.append_entity_summary(
            log,
            session_id,
            actions=result.get("actions"),
            rejected=result.get("rejected"),
            flags=result.get("flags"),
            seq=turn_seq,
        )
        # W7/W8.6 + W9.29 §6 (review R1): every person the turn dealt with enters
        # through the SAME admit path a card-captured member uses. The feed is the
        # turn's MENTIONED-NAME SET — the beings the loop's write tools resolved
        # (``mentioned``, which the runner builds from its ledger with
        # ``entity_tools.mentioned_names``) — so a new person still resolves ->
        # aliases -> files a D79 proposal, and a rename still reaches
        # ``patch_member`` inside the loop. The retired compat render used to add
        # the names it placed; the registry palette is what shows those to the
        # model now, and a being nobody touched this turn needs no admit.
        try:
            from fabulist.state.bts import REFERENCE_IDENTITY, apply_bts_cast_proposals

            mentioned = [
                str(name).strip()
                for name in (result.get("mentioned") or ())
                if str(name).strip()
            ]
            apply_bts_cast_proposals(
                log,
                session_id,
                {
                    "references": [
                        {"kind": REFERENCE_IDENTITY, "name": name}
                        for name in mentioned
                    ],
                    "state": {name: {} for name in mentioned},
                },
            )
        except Exception:  # noqa: BLE001
            pass
        # W9.32 §6 (W11): the shadow keys are NOT merged in any more — they are
        # derived at every read (``entities.rendered_state``), so a fresh turn's
        # delta carries the clock stamp below and nothing else. A pre-regime
        # producer that still reports its own flat deltas is passed through
        # unchanged (the legacy lane, §11.1).
        deltas = dict(result.get("deltas") or {})
        # D3: the clock is authoritative for time; the payload can never set it.
        # A tracker moves time only by PROPOSING it (validated inside the loop), so
        # whatever this turn's rows recorded is read back below — never echoed in.
        deltas.pop("time", None)
        # Last statement of the try body on purpose: a proposal counts only when
        # the whole loop committed successfully. A raise anywhere above leaves
        # `accepted_proposal` at None and the clock fold stamps the turn.
        accepted_proposal = _accepted_clock_proposal(log, session_id, after=mark)
    except Exception as exc:  # BTS failure is logged, never fatal (D9)
        log.append_event(
            session_id,
            "system",
            "bts",
            {
                "event": "world_state_failed",
                "stage": str(getattr(exc, "stage", "registry")),
                "error": str(exc)[:800],
            },
        )
    # D3 (partly reversed by W9.32 §3.5/R4): one stamp, chosen once. An ACCEPTED
    # proposal takes the whole triple (time/date/day — the clock's own keys, folded
    # identically), otherwise the rules clock's advanced fold does. Either way this
    # state-delta is the only storage path for time, so the next `pre_turn`
    # advances FROM whichever instant landed here.
    if accepted_proposal and accepted_proposal.get("time"):
        stamp = {
            key: accepted_proposal[key]
            for key in ("time", "date", "day")
            if accepted_proposal.get(key)
        }
        # The audit row types `day` as an int; the clock fold spells it as text
        # (``advance`` renders its facts block), and one key keeps one spelling.
        if "day" in stamp:
            stamp["day"] = str(stamp["day"])
    else:
        stamp = {
            "time": clock_time,
            **{key: clock_facts.get(key) for key in ("date", "day")},
        }
    for key, value in stamp.items():
        if value and key not in deltas:
            deltas[key] = value
    # d1-3b genre (scene-level; see _enrich_genre) — W9.32 §6: the derivation
    # writes the scene TRAIT and no longer a flat ``genre`` key, so this delta
    # carries nothing but the clock stamp.
    await _enrich_genre(
        decision,
        deltas,
        user_msg,
        prose,
        prior_narratives,
        log=log,
        session_id=session_id,
    )
    log.append_event(
        session_id,
        "state-delta",
        "bts",
        deltas,
        meta={"action_class": action_class, "source": "bts", "checkpoint": False},
    )
    return deltas, bts_result, None


def _latest_narrative_seq(log: LogPort, session_id: str) -> int | None:
    """The newest committed narrative's seq (the turn these actions belong to)."""
    latest: int | None = None
    for event in log.get_chain_events(session_id):
        if event.type == "narrative" and not getattr(event, "ephemeral", False):
            if latest is None or event.seq > latest:
                latest = event.seq
    return latest


def _event_mark(log: LogPort, session_id: str) -> int | None:
    """The newest event id in the session's chain, or ``None`` when unreadable.

    ``Event.id`` is monotonic, so an id above this mark was appended by the turn
    that is running now — which is exactly what tells an ACCEPTED clock proposal
    from one accepted on an earlier turn (§3.5). ``None`` disables the proposal
    channel for the turn rather than guessing: a read that cannot answer must not
    turn an old proposal into today's time.
    """
    events = _chain_rows(log, session_id)
    if events is None:
        return None
    return max((_event_id(event) for event in events), default=0)


def _accepted_clock_proposal(
    log: LogPort, session_id: str, *, after: int | None
) -> dict | None:
    """The newest ``clock_proposal`` row appended after ``after`` (§3.2/§3.5).

    One row per accepted proposal (``entity_tools.propose_time`` appends it after
    the engine validated the instant against this turn's clock), so the turn's
    stamp is the LAST one of the turn — a tracker that proposes twice in one loop
    gets its final word, in log order. ``None`` means the tracker said nothing
    about time and the rules clock stays the mover (D3's default, unchanged).
    """
    if after is None:
        return None
    events = _chain_rows(log, session_id)
    if events is None:
        return None
    newest: dict | None = None
    for event in events:
        if _event_id(event) <= after:
            continue
        if getattr(event, "type", "") != "system":
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, Mapping):
            continue
        if payload.get("event") != CLOCK_PROPOSAL_EVENT:
            continue
        newest = dict(payload)
    return newest


def _chain_rows(log: LogPort, session_id: str) -> list[Any] | None:
    """The session's rows by ascending id (fork-aware when the log offers it).

    ``None`` when the seam exposes no enumerable read at all — the caller must then
    leave the clock alone. Contained: a provenance read is never allowed to break
    the turn it is describing.
    """
    getter = getattr(log, "get_chain_events", None) or getattr(log, "get_events", None)
    if not callable(getter):
        return None
    try:
        return sorted(getter(session_id), key=_event_id)
    except Exception:  # noqa: BLE001 - see the docstring
        return None


def _event_id(event: Any) -> int:
    """One row's monotonic id, tolerant of a seam row without one (sorts first)."""
    try:
        return int(getattr(event, "id", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _critic_state_line(log: LogPort, session_id: str, state: TurnState) -> str:
    """The session's current state line for the critic's evidence bundle.

    The RENDERED state (:func:`fabulist.state.entities.rendered_state` — the
    registry-derived loc/weather/tone/inv/genre and the ``conds`` line), overlaid
    with this turn's rules-clock facts (time / date / day) — the clock advanced in
    pre_turn but its delta is only written later in ``post_turn``, so without the
    overlay the critic would see last turn's time. Rendered as a pipe-YAML line.
    """
    from fabulist.state import entities

    return render_facts(
        entities.rendered_state(
            log, session_id, extra_facts=parse_facts(state.get("facts", ""))
        )
    )


def _emit_revision_frame(
    session_id: str, target_id: int, revision: Any, revised: dict, verdict: str, reason: str
) -> None:
    """Emit the ``revision`` frame on the turn stream, before the engine's ``done``.

    The custom stream writer is only reachable inside a graph run; outside one
    (direct node calls in tests) this is a no-op. The frame carries the rebuilt
    payload (prose + dsl + segments) plus the supersede/ revision ids so the
    client can re-render the turn with no reload (D41).
    """
    writer = _stream_writer()
    if writer is None:
        return
    writer(
        {
            "type": "revision",
            "meta": {
                "session_id": session_id,
                "target_id": target_id,
                "revision_id": getattr(revision, "id", None),
                "verdict": verdict,
                "reason": reason,
                "prose": revised["prose"],
                "dsl": revised["dsl"],
                "segments": revised["segments"],
            },
        }
    )


def _commit_critic_revision(
    log: LogPort,
    session_id: str,
    target_id: int,
    base_payload: dict,
    prose: str,
    dsl: str,
    segments: list[dict],
    reason: str,
    verdict: str,
) -> Any:
    """Append a revision superseding ``target_id`` and emit its live frame.

    The revised payload keeps every original narrative key and replaces both
    ``prose`` and ``segments`` (the transcript renders ``segments``, so a stale
    one would show the old text). Returns the appended revision event so a
    follow-on attempt can supersede it (the payload's keys stay the narrative's
    via ``base_payload``).
    """
    revised = {**dict(base_payload or {})}
    revised["prose"] = prose
    revised["dsl"] = dsl
    revised["segments"] = segments
    revision = log.append_revision(
        session_id,
        target_id,
        actor="critic",
        payload={"revised": revised, "reason": reason},
    )
    _emit_revision_frame(session_id, target_id, revision, revised, verdict, reason)
    return revision


async def _run_critic_stage(
    critic: Any,
    log: LogPort,
    state: TurnState,
) -> dict:
    """Run the quality gate and turn its tri-state verdict into a state update.

    The verdict itself is applied here, as it always was: ``ok`` logs and keeps
    the turn, ``diffs`` applies the patch and commits a ``revision``, and
    ``fail`` logs and KEEPS the turn (W9.24 §3.3.7 retired the regeneration that
    used to follow it — the narrator's own repair loop runs before the commit,
    and a defect only the critic can see now lands kept, with its verdict row).

    ``diffs`` applies its ``edits`` INDEPENDENTLY (the narrator repair loop's
    ``patch_prose`` rule): every edit that lands becomes part of the ``revision``,
    and every edit that does not is reported in its own ``critic_failed`` row
    (stage ``apply``) with the teaching payload — the clipped attempted ``find``,
    the prose segment inventory, the flag anchors and the note that an identical
    retry fails identically. A verdict where nothing lands revises nothing; the
    turn is kept and the verdict logged, exactly as a rejected patch always was.

    Every stage is non-fatal (the turn already streamed, D49): a raising call, an
    unparseable verdict, or a rejected edit is logged (``critic_failed``) and the
    delivered turn is kept. Contradictory-DSL/DSL edits are out of scope — the DSL
    channel has its own gate and ``apply_patch`` only touches prose segments.

    ``decision`` does NOT gate the critic (see the spike note on the pre-gate):
    the decision caller supplies the genre fact instead (``bts`` node).
    """
    from .quality import (
        VERDICT_DIFFS,
        VERDICT_OK,
        apply_patch,
        parse_critic_output,
    )

    session_id = state["session_id"]
    user_msg = state["user_content"]
    prose = "".join(state.get("narrative_parts", []))
    segments = state.get("segments") or []
    flags = list(state.get("critic_flags") or [])
    narrative_event = state.get("narrative_event") or {}
    prior_narratives = _prior_narratives(
        log, session_id, exclude_id=narrative_event.get("id")
    )
    def _log(payload: dict) -> None:
        log.append_event(session_id, "system", "critic", payload)

    evidence = {
        "user_message": user_msg,
        "prose": prose,
        "flags": list(flags),
        "prior_prose": [
            str(n.get("payload", {}).get("prose", ""))
            for n in prior_narratives[-CRITIC_PRIOR_TURNS:]
        ],
        "state_line": _critic_state_line(log, session_id, state),
    }
    # D42: the critic step brackets its model call. Nothing follows a verdict
    # into the narrator: the repair loop is the narrate node's business.
    critic_writer = _stream_writer()
    _emit_stage(critic_writer, "critic", "start")

    try:
        raw = await critic(evidence)
    except Exception as exc:  # a raising critic is non-fatal (D49)
        _emit_stage(critic_writer, "critic", "done")
        _log({"event": "critic_failed", "stage": "call", "error": str(exc)[:800]})
        return _NO_VERDICT
    _emit_stage(critic_writer, "critic", "done")

    verdict = parse_critic_output(raw)
    if verdict.parse_failed:
        _log(
            {
                "event": "critic_failed",
                "stage": "parse",
                "error": str(verdict.reason)[:800],
            }
        )
        return _NO_VERDICT

    #: The same fields the ``critic_verdict`` row carries (the log owns the
    #: event — D9); state keeps them so the routing edge can read the verdict.
    #: ``attempt`` stays 0 on every row: the retry counter it used to stamp died
    #: with the regeneration (W9.24 §3.3.7), and old readers key nothing on it.
    record = {
        "verdict": verdict.verdict,
        "attempt": 0,
        "reason": verdict.reason,
        "edits": [],
    }

    if verdict.verdict == VERDICT_OK:
        _log({"event": "critic_verdict", **record})
        return {"critic_verdict": record}

    if verdict.verdict == VERDICT_DIFFS:
        # ONE EDIT AT A TIME — the narrator repair loop's rule (:func:`patch_prose`)
        # now governs the critic too. ``apply_patch`` stays whole-patch atomic;
        # what changes is the CALL: a rejected edit costs only itself, so a valid
        # correction can no longer be discarded by a sibling. Live evidence: a
        # ``find`` that copied a seeded line number was rejected and took a
        # valid, verbatim prose repair down with it.
        edits = [
            {"find": edit.find, "replace": edit.replace, "count": edit.count}
            for edit in verdict.edits
        ]
        working = list(segments)
        landed: list[dict] = []
        prose = ""
        rejected: list[dict] = []
        anchors = [
            str(flag.get("anchor"))
            for flag in flags
            if isinstance(flag, Mapping) and flag.get("anchor")
        ]
        for index, edit in enumerate(edits):
            result = apply_patch(working, [edit])
            if result.ok:
                working, prose = result.segments, result.prose
                landed.append(edit)
                continue
            # The same teaching payload the repair loop hands its model
            # (:func:`_patch_failure`): the clipped attempted `find`, the prose
            # segment inventory, the flag anchors and the "an identical retry
            # will fail identically" note — here read by the inspector.
            rejected.append(
                _patch_failure(
                    working,
                    edits,
                    index,
                    str(result.error),
                    anchors,
                    applied=len(landed),
                    prose_head=result.prose_head,
                    elsewhere=result.elsewhere,
                    boundary=result.boundary,
                    boundary_anchors=result.boundary_anchors,
                )
            )
        if rejected:
            _log(
                {
                    "event": "critic_failed",
                    "stage": "apply",
                    "error": str(rejected[0]["error"])[:800],
                    "landed": len(landed),
                    "rejected": rejected,
                }
            )
        if not landed:
            # Nothing landed: the turn is left byte-identical (today's behaviour
            # for a rejected patch) and the verdict row records the attempt.
            _log({"event": "critic_verdict", **record})
            return {"critic_verdict": record}
        applied = {**record, "edits": landed}
        _log({"event": "critic_verdict", **applied})
        new_dsl = "\n".join(s["text"] for s in working if s["kind"] == "dsl")
        _commit_critic_revision(
            log,
            session_id,
            narrative_event.get("id"),
            narrative_event.get("payload") or {},
            prose,
            new_dsl,
            working,
            verdict.reason or "critic repair",
            VERDICT_DIFFS,
        )
        # The projection now renders the patched turn; downstream stages must
        # fold/BTS/attribute THAT text, not the superseded draft.
        return {
            "critic_verdict": applied,
            "narrative_parts": [prose] if prose else [],
            "dsl": new_dsl,
            "segments": working,
        }

    # FAIL (the parser guarantees this is the only remaining trusted verdict).
    # KEEP the delivered turn and log the verdict: the regeneration the old path
    # requested is RETIRED (W9.24 §3.3.7). This is D49's cap-spent behaviour,
    # now unconditional — the honest trade the epic records as R1, with the
    # verdict row keeping the defect visible.
    _log({"event": "critic_verdict", **record})
    return {"critic_verdict": record}


async def _run_attribution(
    attribution: Any,
    log: LogPort,
    session_id: str,
    user_msg: str,
    prose: str,
) -> list[dict[str, str]]:
    """Run the attribution pass, log the event, assign first-sighting colours.

    Best-effort (B5): a failed call appends ``attribution_failed`` and never
    breaks the turn. Per-quote resolution failures simply omit that entry.
    Returns the persisted rows (``[]`` on failure) so the node can carry them in
    state.
    """
    try:
        rows = await attribution(user_msg, prose)
    except Exception as exc:  # never fatal
        log.append_event(
            session_id,
            "system",
            "attribution",
            {"event": "attribution_failed", "stage": "parse", "error": str(exc)[:800]},
        )
        return []

    clean: list[dict[str, str]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        speaker = str(row.get("speaker") or "").strip()
        quote = row.get("quote")
        if not speaker or not isinstance(quote, str) or not quote.strip():
            continue
        # W7: the row carries the canonical speaker_id when the runner resolved
        # one; otherwise admit the name here so the persisted row still joins to
        # a member. Best-effort — an admit failure keeps the name-only row.
        entry: dict[str, str] = {"speaker": speaker, "quote": quote}
        speaker_id = row.get("speaker_id")
        if not (isinstance(speaker_id, str) and speaker_id):
            try:
                from fabulist.state.cast import admit_member

                speaker_id = admit_member(
                    log,
                    session_id,
                    speaker,
                    provenance="extractor",
                    admitted_by="attribution",
                )
            except Exception:  # noqa: BLE001 - identity is best-effort
                speaker_id = None
        if isinstance(speaker_id, str) and speaker_id:
            entry["speaker_id"] = speaker_id
        clean.append(entry)
    log.append_event(
        session_id,
        "system",
        "attribution",
        {"event": "attribution", "attributions": clean},
    )
    if clean:
        _assign_speaker_colors(log, session_id, clean)
    return clean


def _assign_speaker_colors(
    log: LogPort, session_id: str, rows: list[dict[str, str]]
) -> None:
    """Assign first-sighting colours for attributed speakers (D25/B5 + W7).

    The user character (the session persona's name) gets the reserved near-white
    by ROLE; when no persona is assigned there is nothing to tint as the user,
    so only speakers are coloured. Colours are the engine registry's, never the
    model's. W7: the colour is keyed by the speaker's immutable ``member_id``
    (resolved/admitted through the canonical cast), so a later rename keeps the
    same hue; the name is still stored for the legacy name-keyed read.
    """
    from fabulist.api.cast_api import (
        assign_member_color,
        assign_speaker_color,
        list_speaker_colors,
    )
    from fabulist.state.cast import admit_member

    try:
        existing = list_speaker_colors(log, session_id)
    except Exception:  # noqa: BLE001 - colouring is best-effort
        return

    persona_name = _session_persona_name(log, session_id)
    if persona_name and persona_name not in existing:
        try:
            assign_speaker_color(log, session_id, persona_name, is_user=True)
            existing = list_speaker_colors(log, session_id)
        except Exception:  # noqa: BLE001
            pass

    for row in rows:
        name = row["speaker"]
        if name == persona_name:
            continue
        try:
            member_id = row.get("speaker_id") or admit_member(
                log,
                session_id,
                name,
                provenance="extractor",
                admitted_by="attribution",
            )
        except Exception:  # noqa: BLE001 - identity is best-effort
            continue
        try:
            assign_member_color(log, session_id, member_id, name)
        except Exception:  # noqa: BLE001 - colouring is best-effort
            pass


def _session_persona_name(log: LogPort, session_id: str) -> str | None:
    """The session persona's display name, or ``None`` (no persona assigned)."""
    try:
        from fabulist.personas.registry import list_personas, persona_for_chat

        persona_id = persona_for_chat(log, session_id)
        if not persona_id:
            return None
        for persona in list_personas(log):
            if persona.persona_id == persona_id:
                return persona.name
    except Exception:  # noqa: BLE001 - persona lookup is best-effort
        return None
    return None
