"""Schema gate for event payloads (PLAN §4, first half of the append chokepoint).

PLAN §4 says "agents propose events; only the dispatcher appends (schema +
permission gate at one chokepoint)". Today engines call
:meth:`fabulist.log.repo.EventLog.append_event` directly, so this module is the
*schema* half of that future gate: the orchestrator will call
:func:`validate_event` inside ``append_event`` so a malformed payload is rejected
before it reaches the log. It does **not** write anything and is not yet wired in.

Design rules
------------
* **Ground truth, not guesswork.** Every required key below is taken from a real
  producer or test (citations inline). Where the payload is internally open-ended
  (``state-delta`` folds arbitrary extracted facts; ``system`` carries wildly
  different housekeeping shapes), the schema requires nothing and documents why.
* **Extras are always allowed** (D4: the log is editable and readers are
  forward-compatible). A future publisher may add keys without breaking the gate;
  only missing/wrong-typed *declared* keys fail.
* **``bool`` is not ``int``.** ``isinstance(True, int)`` is ``True`` in Python, so
  booleans are tested before integers; a bool never satisfies an ``int`` field
  (nor an ``int`` a ``bool`` field).
* **``None`` as a schema type means "any".** Used for payloads whose contents are
  intentionally opaque.

Deviations from the task sketch (grounded in real code, per the "be faithful"
instruction) are documented on each ``Schema`` below.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .degradation import DEGRADATION_EVENTS


class LogSchemaError(ValueError):
    """Payload rejected by the append schema gate."""
from typing import Any

__all__ = [
    "Schema",
    "EVENT_SCHEMAS",
    "USAGE_EVENT",
    "USAGE_SCHEMA",
    "PROMPT_BUDGET_EVENT",
    "PROMPT_BUDGET_SCHEMA",
    "SPEAKER_COLOR_EVENT",
    "SPEAKER_COLOR_SCHEMA",
    "SPEAKER_COLOR_ASSIGNERS",
    "WORLD_STATE_EVENT",
    "WORLD_STATE_SCHEMA",
    "ATTRIBUTION_EVENT",
    "ATTRIBUTION_SCHEMA",
    "ACTOR_ASSESSED_EVENT",
    "ACTOR_ASSESSED_SCHEMA",
    "ACTOR_PROMOTED_EVENT",
    "ACTOR_PROMOTED_SCHEMA",
    "ACTOR_VERDICTS",
    "ASSET_PROPOSAL_ACTIONS",
    "ASSET_PROPOSAL_EVENT",
    "ASSET_PROPOSAL_KINDS",
    "ASSET_PROPOSAL_ORIGINS",
    "ASSET_PROPOSAL_FIELD_TYPES",
    "ASSET_PROPOSAL_LIST_FIELDS",
    "ASSET_PROPOSAL_NESTED_FIELDS",
    "ASSET_PROPOSAL_OP_ACTIONS",
    "ASSET_PROPOSAL_PATCH_FIELDS",
    "ASSET_PROPOSAL_SCHEMA",
    "ENTRY_SHOWN_KEYS",
    "SESSION_ACTOR_EVENT",
    "SESSION_ACTOR_SCHEMA",
    "PROPOSAL_DECISION_EVENT",
    "PROPOSAL_DECISIONS",
    "PROPOSAL_DECISION_SCHEMA",
    "PROPOSAL_UNDO_EVENT",
    "PROPOSAL_UNDO_SCHEMA",
    "validate_actor_assessed_payload",
    "validate_actor_promoted_payload",
    "entry_projection",
    "list_item_path",
    "validate_asset_proposal_payload",
    "validate_proposal_decision_payload",
    "validate_proposal_undo_payload",
    "validate_session_actor_payload",
    "CAST_ADMIT_EVENT",
    "CAST_ALIAS_EVENT",
    "CAST_AMBIGUOUS_EVENT",
    "CAST_LINK_EVENT",
    "CAST_PATCH_EVENT",
    "CAST_RETIRE_EVENT",
    "validate_cast_admit_payload",
    "validate_cast_alias_payload",
    "validate_cast_ambiguous_payload",
    "validate_cast_link_payload",
    "validate_cast_patch_payload",
    "validate_cast_retire_payload",
    "WORLD_STATE_FAILED_EVENT",
    "ATTRIBUTION_FAILED_EVENT",
    "validate_world_state_payload",
    "validate_attribution_payload",
    "validate_event",
    "validate_usage_payload",
    "validate_prompt_budget_payload",
    "validate_speaker_color_payload",
    "AGENT_CALL_EVENT",
    "AGENT_CALL_SCHEMA",
    "ASSISTANT_REPLY_EVENT",
    "ASSISTANT_REPLY_SCHEMA",
    "SIDEKICK_REPLY_EVENT",
    "SIDEKICK_REPLY_SCHEMA",
    "IMAGE_CAPTION_EVENT",
    "IMAGE_CAPTION_SCHEMA",
    "STT_FAILURE_EVENT",
    "INDEX_FAILURE_EVENT",
    "COMPACTION_FAILED_EVENT",
    "POST_TURN_FAILED_EVENT",
    "FAILURE_EVENTS",
    "STAGE_FAILURE_SCHEMA",
    "DEGRADATION_EVENTS",
    "DEGRADATION_SCHEMA",
    "validate_degradation_payload",
    "CONTEXT_LOW_EVENT",
    "CONTEXT_LOW_SCHEMA",
    "validate_context_low_payload",
    "COMPACTION_MARKER_EVENT",
    "COMPACTION_MARKER_SCHEMA",
    "validate_compaction_marker_payload",
    "validate_agent_call_payload",
    "validate_assistant_reply_payload",
    "validate_sidekick_reply_payload",
    "validate_image_caption_payload",
    "validate_stage_failure_payload",
    "HISTORIAN_RUN_EVENT",
    "HISTORIAN_RUN_SCHEMA",
    "HISTORIAN_RUN_STATUSES",
    "HISTORIAN_RUN_TRIGGERS",
    "validate_historian_run_payload",
    "AGENT_LOOP_EVENT",
    "AGENT_LOOP_KINDS",
    "AGENT_LOOP_SCHEMA",
    "validate_agent_loop_payload",
    "VALIDATION_NOTICE_EVENT",
    "VALIDATION_NOTICE_SCHEMA",
    "validate_validation_notice_payload",
    "SET_SWAP_EVENT",
    "writable_field",
    "narrative_config_snapshot",
]


@dataclass(frozen=True)
class Schema:
    """Declared shape of one event type's payload.

    ``required`` maps key -> Python type (``int``/``str``/``bool``/``float``/
    ``dict``/``list``), or ``None`` meaning "any value". ``optional`` maps keys
    that, when present, must match the declared type. Any key not named in either
    mapping is accepted unchanged.
    """

    required: dict[str, type | None] = field(default_factory=dict)
    optional: dict[str, type | None] = field(default_factory=dict)


#: The schema for every event type in :data:`fabulist.log.EVENT_TYPES`.
EVENT_SCHEMAS: dict[str, Schema] = {
    # user — engine.py:162/185 {"text": content}; app.py:174 adds "kind";
    # cards_api.py:257-260 seeds {"text", "kind"}. "image_asset_id" is the
    # documented forward-compat slot for an attached-image turn (optional).
    "user": Schema(
        # text optional: housekeeping user rows exist in log-level tests
        required={},
        optional={"text": str, "kind": str, "image_asset_id": str},
    ),
    # narrative — nodes.py:323 writes {"prose", "dsl", "segments"}; engine.py:164
    # (cancelled partial) writes {"prose", "dsl"} only, so dsl/segments are
    # optional. "config" is the provenance snapshot (see
    # narrative_config_snapshot); "reasoning" is the optional raw reasoning trace.
    "narrative": Schema(
        # prose optional: segments-only turns are legal (interweave contract)
        required={},
        optional={
            "prose": str,
            "dsl": str,
            "segments": list,
            "reasoning": str,
            "config": dict,
            # Generated-image narrative keys (imagegen_api.generate_image): the
            # canonical image narrative carries no prose — image_asset_id points
            # at the ``asset`` event holding the bytes, kind is "image", prompt
            # is the effective positive prompt; legacy rows used ``text``.
            "image_asset_id": str,
            "kind": str,
            "prompt": str,
            "text": str,
        },
    ),
    # revision — repo.EventLog.append_revision (repo.py:~110) *enforces* a
    # "revised" key and stores the target in the row's supersedes column, not the
    # payload (see tests/test_log.py:30-32 and tests/test_forks.py:98-103 which
    # both use {"revised": {...}, "reason": ...}). The task sketch's
    # target/new_content keys do not exist in the real payloads, so the real
    # convention is encoded here.
    "revision": Schema(
        required={"revised": dict},
        optional={"reason": str},
    ),
    # state-delta — nodes.py:421 appends the extractor's arbitrary deltas dict
    # (tests/test_log.py:48, test_graph.py delta.payload["tone"]/["loc"]/["time"]).
    # There is no fixed key set: any extracted fact name is legal, so nothing is
    # required and nothing is typed.
    "state-delta": Schema(required={}, optional={}),
    # roll — graph/engine.py:148 appends ``r.as_payload()`` (graph/rolls.py):
    # ``{"dice": str, "result": int, "detail": str, "ask": str?}``.
    # ``dice``/``result`` are the load-bearing pair (tests/test_log.py:169 seeds
    # the pre-detail shape, which still passes); S4e/HB1 adds the optional
    # face-breakdown ``detail``, and the owner steer (2026-10-10) adds the
    # optional ``ask`` (the roll's short purpose). Unknown extras remain
    # allowed (D4).
    "roll": Schema(
        required={"dice": str, "result": int},
        optional={"detail": str, "ask": str},
    ),
    # asset — media/images.py store_image() writes all five keys (asset_id, kind,
    # mime, bytes_b64, sha256, note). Only the four that make an asset resolvable
    # are required; note/sha256 are optional because a caller may omit them.
    "asset": Schema(
        # pointer-only asset events are legal; full uploads carry the rest
        required={
            "asset_id": str,
            "kind": str,
        },
        optional={"mime": str, "bytes_b64": str, "note": str, "sha256": str},
    ),
    # summary — W9.26 §2.3 step 7 (v2) writes {"text", "covers_seqs", "reason",
    # "trigger", "stats"} plus "absorbs" ONLY when a prior summary exists.
    # "covers_seqs" stays the ONLY required key (the inclusive [first, last]
    # turn-pair seq span). "absorbs" is `[session_id, seq]` of the summary this
    # row absorbs: OMITTED when there is none — a present ``null`` fails here
    # (an ``optional`` entry type-checks a PRESENT key), which is the pinned
    # typing trap §2.9 names. The task also allows a "content" alternative to
    # "text"; a plain required-map cannot express "text or content", so both
    # are type-checked optionals and a stricter one-of check is a future
    # refinement.
    "summary": Schema(
        required={"covers_seqs": list},
        optional={
            "text": str,
            "content": str,
            "reason": str,
            "trigger": str,
            # {calls: int, prompt_tokens: int, model: str} — the run's cost
            # ledger. Left as an open dict: the three keys are documented on
            # the producer (graph/compaction.py) and extras stay allowed (D4).
            "stats": dict,
            "absorbs": list,
        },
    ),
    # ooc — engine.py:182 writes {"text": <joined prose>}. Kept permissive (only
    # "text" is typed, nothing required) because OOC replies are plain chat and a
    # future variant must not be gated.
    "ooc": Schema(required={}, optional={"text": str}),
    # system — intentionally opaque. Real payloads span session_created
    # (app.py:330), session_renamed/session_hidden (api/sessions.py:179-205),
    # plan (graph/plans.py:102), dsl_validation/validator_flags/extraction_failed
    # (nodes.py:335-406), image_caption (media/images.py:187), assistant replies
    # (api/assistants_api.py), settings toggles (api/settings.py:216) and even
    # a *bare list* (tests/test_assistants_api.py). Requiring or typing any key
    # would reject a legal system event, so the gate requires nothing.
    "system": Schema(required={}, optional={}),
}


#: ``system`` payload ``event`` name carrying one agent/model call's usage
#: (I1, D23/D25: per-call usage is LOG PERSISTENCE, not process RAM). The
#: payload is a *variant* of the open ``system`` event, so its shape is typed
#: here and dispatched from :func:`validate_event` rather than by narrowing the
#: intentionally-opaque ``system`` schema (which would risk rejecting a legal
#: housekeeping payload).
USAGE_EVENT = "usage"

#: Declared shape of a usage payload. ``agent``/``model``/``session_id`` are
#: always present (a usage row is meaningless without them); token fields are
#: non-negative ints; ``latency_s`` is a float. ``seq`` is the optional seq
#: *context* (the caller may tie a call to a turn); extras stay allowed (D4).
USAGE_SCHEMA = Schema(
    required={
        "event": str,
        "agent": str,
        "model": str,
        "session_id": str,
        "prompt_tokens": int,
        "completion_tokens": int,
        "total_tokens": int,
        "latency_s": float,
    },
    optional={"seq": int},
)


def validate_usage_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``usage`` payload against :data:`USAGE_SCHEMA`.

    Returns a list of human-readable errors (empty means acceptable). The
    ``event`` key must literally equal :data:`USAGE_EVENT` so a caller cannot
    pass a differently-named system payload here by mistake.
    """
    if not isinstance(payload, dict) or payload.get("event") != USAGE_EVENT:
        return [f"expected event='{USAGE_EVENT}'"]
    return _validate_against(USAGE_SCHEMA, payload)


#: ``system`` payload ``event`` name carrying ONE over-budget prompt estimate
#: (W9.25 §3.5). Producer: :func:`fabulist.graph.compaction.warn_prompt_budget`,
#: called by the streaming caller at the moment it sends a prompt and by the
#: extractor's direct call. A typed variant of the open ``system`` event, like
#: ``usage`` — extras stay allowed (D4), the four measured numbers are declared.
PROMPT_BUDGET_EVENT = "prompt_budget"

#: ``system`` payload ``event`` name carrying one context-gauge reading
#: (W9.26 §2.2). Producer: the context monitor's fifth post-turn hook
#: (``graph/context_monitor.py``, Lane D). A typed variant of the open
#: ``system`` event, like ``usage`` — the reading is measured (⚑R1a), so the
#: numbers are declared and an unmeasured row is rejected.
CONTEXT_LOW_EVENT = "context_low"

#: Declared shape of a ``context_low`` payload.
#:
#: ``ratio`` is **FLOAT-ONLY** (W9.26 §2.9): the gate's :func:`_matches` is
#: single-type, so a producer that computes the ratio must emit a float — the
#: over-budget sentinel is ``1.0``, never the int ``1``. ``prompt_tokens`` is
#: the measured ``usage`` row's number, ``budget_tokens`` the usable budget it
#: was measured against, ``basis`` names the measurement (``measured``).
#:
#: ``over_budget``/``noop`` are OMITTED unless true (never emitted ``false``);
#: ``validate_context_low_payload`` enforces the omission so a `false` flag
#: cannot quietly become a second row variant.
CONTEXT_LOW_SCHEMA = Schema(
    required={
        "event": str,
        "agent": str,
        "ratio": float,
        "prompt_tokens": int,
        "budget_tokens": int,
        "basis": str,
    },
    optional={"over_budget": bool, "noop": bool, "seq": int},
)


def validate_context_low_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``context_low`` payload (W9.26 §2.2).

    Beyond the declared types this refuses the two traps the design names: a
    non-float ``ratio`` (the schema does that) and a present-but-false
    ``over_budget``/``noop`` flag (omitted-unless-true is the contract, so the
    row variants stay distinguishable by key PRESENCE).
    """
    if not isinstance(payload, dict) or payload.get("event") != CONTEXT_LOW_EVENT:
        return [f"expected event='{CONTEXT_LOW_EVENT}'"]
    errors = _validate_against(CONTEXT_LOW_SCHEMA, payload)
    for flag in ("over_budget", "noop"):
        value = payload.get(flag)
        if isinstance(value, bool) and value is not True:
            errors.append(f"key '{flag}' is omitted unless true (never emitted false)")
    return errors


#: ``system`` payload ``event`` name carrying ONE era marker (W9.26 §2.3 step 9).
#: Producer: the compact service, appended to the NEW (successor) session right
#: after the fork. ``summary_ref`` is ``[session_id, seq]`` of the summary the
#: era starts from; ``source_session`` is the compacted parent; ``covers_seqs``
#: is the covered turn-pair span (the break label); ``stats`` is the run ledger
#: (``{calls, prompt_tokens, model}``). A session's ``kind`` (user vs compaction)
#: derives from this row's PRESENCE among its OWN rows — never from a column.
COMPACTION_MARKER_EVENT = "compaction"

#: Declared shape of a ``compaction`` marker payload. Every key but ``stats`` is
#: load-bearing for the transcript (the break cannot be labeled or the notice
#: fetched without them); ``stats`` is the Inspector's expandable detail and is
#: typed-only.
COMPACTION_MARKER_SCHEMA = Schema(
    required={
        "event": str,
        "source_session": str,
        "summary_ref": list,
        "covers_seqs": list,
        "trigger": str,
    },
    optional={"stats": dict},
)


def validate_compaction_marker_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``compaction`` marker (W9.26 §2.3 step 9)."""
    if not isinstance(payload, dict) or payload.get("event") != COMPACTION_MARKER_EVENT:
        return [f"expected event='{COMPACTION_MARKER_EVENT}'"]
    errors = _validate_against(COMPACTION_MARKER_SCHEMA, payload)
    ref = payload.get("summary_ref")
    if isinstance(ref, list) and len(ref) != 2:
        errors.append("key 'summary_ref' expects a [session_id, seq] pair")
    return errors

#: Declared shape of a prompt_budget payload: the estimate, the declared window it
#: was measured against, the response length reserved inside that window, and the
#: overshoot. A row missing any of them cannot be acted on (the owner reads the
#: numbers to decide: raise Context Size, lower Response Length, compact sooner).
PROMPT_BUDGET_SCHEMA = Schema(
    required={
        "event": str,
        "agent": str,
        "est_prompt_tokens": int,
        "context_window": int,
        "response_length": int,
        "over_tokens": int,
    },
    optional={},
)


def validate_prompt_budget_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``prompt_budget`` payload (W9.25 §3.5)."""
    if not isinstance(payload, dict) or payload.get("event") != PROMPT_BUDGET_EVENT:
        return [f"expected event='{PROMPT_BUDGET_EVENT}'"]
    return _validate_against(PROMPT_BUDGET_SCHEMA, payload)


#: ``system`` payload ``event`` name carrying one speaker's assigned colour
#: (D25 colour-dialogue registry, B5 backend half; Wave 4b H5). Like ``usage``
#: this is a *typed variant* of the intentionally-open ``system`` event, so its
#: shape is dispatched from :func:`validate_event` rather than by narrowing the
#: opaque system schema (which would risk rejecting housekeeping payloads).
SPEAKER_COLOR_EVENT = "speaker_color"

#: Who may have assigned the colour. ``engine`` = automatic first-sighting
#: registry; ``user`` = an explicit picker re-assignment (B5/H5).
SPEAKER_COLOR_ASSIGNERS = frozenset({"engine", "user"})

#: Declared shape of a speaker_colour payload. ``name`` is the speaker/cast
#: name; ``color`` is the assigned hex; ``assigned_by`` is ``engine``/``user``.
#: Extras stay allowed (D4).
SPEAKER_COLOR_SCHEMA = Schema(
    required={"event": str, "name": str, "color": str, "assigned_by": str},
    optional={},
)


def validate_speaker_color_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``speaker_color`` payload.

    Returns a list of human-readable errors (empty means acceptable). The
    ``event`` key must literally equal :data:`SPEAKER_COLOR_EVENT`, and
    ``assigned_by`` must be one of :data:`SPEAKER_COLOR_ASSIGNERS`.
    """
    if not isinstance(payload, dict) or payload.get("event") != SPEAKER_COLOR_EVENT:
        return [f"expected event='{SPEAKER_COLOR_EVENT}'"]
    errors = _validate_against(SPEAKER_COLOR_SCHEMA, payload)
    assigned_by = payload.get("assigned_by")
    if assigned_by not in SPEAKER_COLOR_ASSIGNERS:
        errors.append("key 'assigned_by' expects one of engine|user")
    return errors


# -- other typed ``system`` variants (stable one-producer shapes) -------------
#
# Like ``usage``/``speaker_color`` these are payloads that carry a fixed
# ``event`` discriminator and a stable shape written by exactly one producer.
# They are validated here and dispatched from :func:`validate_event` so a
# malformed payload is rejected at the append door instead of landing as an
# unreadable row. The bare/opaque ``system`` path stays permissive BY DESIGN
# (see ``EVENT_SCHEMAS["system"]``) and is never routed through these.

#: ``system`` payload ``event`` carrying one captured agent call (D17).
#: Producer: ``fabulist.agents.debug`` ``DebugCapture``/``append_call_event``
#: (``wire`` dict ~agents/debug.py:213-228).
AGENT_CALL_EVENT = "agent_call"

#: Declared shape of an agent_call payload. The producer always emits every
#: key; ``finish`` is ``str | None`` so it is declared as ``required`` with the
#: ``None`` ("any") type — its *presence* is enforced, its value is not, because
#: a provider that never reported a finish reason legitimately stores ``None``.
AGENT_CALL_SCHEMA = Schema(
    required={
        "event": str,
        "agent": str,
        "messages": list,
        "response_text": str,
        "reasoning_text": str,
        "finish": None,
        "latency_s": float,
        "facts_hash": str,
        "meta": dict,
    },
    optional={},
)


def validate_agent_call_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``agent_call`` payload (D17 capture)."""
    if not isinstance(payload, dict) or payload.get("event") != AGENT_CALL_EVENT:
        return [f"expected event='{AGENT_CALL_EVENT}'"]
    return _validate_against(AGENT_CALL_SCHEMA, payload)


#: ``system`` payload ``event`` carrying one assistant's reply.
#: Producer: ``api/assistants_api.py`` ``consult_assistant``.
ASSISTANT_REPLY_EVENT = "assistant_reply"
ASSISTANT_REPLY_SCHEMA = Schema(
    required={"event": str, "assistant": str, "text": str},
    optional={},
)


#: Legacy (pre-rename) reply event name. Accepted on read and validated so an
#: existing ``sidekick_reply`` row stays legal; the engine never writes it.
SIDEKICK_REPLY_EVENT = "sidekick_reply"
SIDEKICK_REPLY_SCHEMA = Schema(
    required={"event": str, "sidekick": str, "text": str},
    optional={},
)


def validate_assistant_reply_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``assistant_reply`` payload."""
    if not isinstance(payload, dict) or payload.get("event") != ASSISTANT_REPLY_EVENT:
        return [f"expected event='{ASSISTANT_REPLY_EVENT}'"]
    return _validate_against(ASSISTANT_REPLY_SCHEMA, payload)


def validate_sidekick_reply_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``sidekick_reply`` payload (accepted-legacy)."""
    if not isinstance(payload, dict) or payload.get("event") != SIDEKICK_REPLY_EVENT:
        return [f"expected event='{SIDEKICK_REPLY_EVENT}'"]
    return _validate_against(SIDEKICK_REPLY_SCHEMA, payload)


#: ``system`` payload ``event`` carrying one image caption.
#: Producer: ``media/images.py`` ``caption`` (~:192).
IMAGE_CAPTION_EVENT = "image_caption"
IMAGE_CAPTION_SCHEMA = Schema(
    required={"event": str, "asset": str, "text": str},
    optional={},
)


def validate_image_caption_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``image_caption`` payload."""
    if not isinstance(payload, dict) or payload.get("event") != IMAGE_CAPTION_EVENT:
        return [f"expected event='{IMAGE_CAPTION_EVENT}'"]
    return _validate_against(IMAGE_CAPTION_SCHEMA, payload)


# -- stage-failure variants (S4c: failures must be visible in the log) --------
#
# Audit 2026-10-10 (silent-failure class): several post-turn paths swallowed a
# failure into the server journal only, so the log (D9: source of truth) missed
# it and the inspector could not show it. Each failing stage now appends one of
# these typed ``system`` variants. They are NOT fatal to the turn: the append is
# best-effort at the call site.

#: Speech-to-text failed for an uploaded audio asset
#: (``api/attachments.py`` ``schedule_transcription``).
STT_FAILURE_EVENT = "stt_failure"

#: BTS world-state could not be parsed after its repair retry
#: (``graph/nodes.py`` post-turn world-state node). The turn keeps the last
#: good world-state (degrade = no update), but the failure is visible (D9).
WORLD_STATE_FAILED_EVENT = "world_state_failed"

#: Speaker attribution could not be parsed after its repair retry
#: (``graph/nodes.py`` post-turn attribution node). Best-effort (B5): the turn
#: is unaffected, but the failure is visible (D9).
ATTRIBUTION_FAILED_EVENT = "attribution_failed"

#: Post-turn vector indexing failed (memory hook; prepared ``api/app.py``
#: ``_schedule_post_turn`` patch).
INDEX_FAILURE_EVENT = "index_failure"

#: Compaction produced no summary (empty/refusal text or a raising hook)
#: (``graph/compaction.py``; prepared ``api/app.py`` patch).
COMPACTION_FAILED_EVENT = "compaction_failed"

#: A POST-TURN HOOK failed, named for the hook that failed (W9.26 §2.8/§2.9).
#: The honest-failure row: a monitor crash must never be filed as
#: ``compaction_failed`` (that name means the compact pipeline failed, and the
#: forced-compaction attempt counter reads it). The `hook` key names the
#: culprit (``context_monitor``/``memory_hook``/...).
POST_TURN_FAILED_EVENT = "post_turn_failed"

#: The per-domain failure variants share one shape.
FAILURE_EVENTS = frozenset(
    {
        STT_FAILURE_EVENT,
        INDEX_FAILURE_EVENT,
        COMPACTION_FAILED_EVENT,
        WORLD_STATE_FAILED_EVENT,
        ATTRIBUTION_FAILED_EVENT,
        POST_TURN_FAILED_EVENT,
    }
)

#: Declared shape of a stage-failure payload. ``stage`` names the failing step
#: (e.g. ``decode``/``transcribe``/``memory_hook``) and ``error`` is the
#: truncated failure text. ``seq`` is optional seq context; ``asset_id`` (stt)
#: and ``covers_seqs`` (compaction) are per-domain context, present only where
#: the producer knows them; ``hook`` names the post-turn hook that failed
#: (``post_turn_failed``).
STAGE_FAILURE_SCHEMA = Schema(
    required={"event": str, "stage": str, "error": str},
    optional={"seq": int, "asset_id": str, "covers_seqs": list, "hook": str},
)


def validate_stage_failure_payload(payload: dict) -> list[str]:
    """Validate one stage-failure ``system`` variant (S4c).

    Accepts any event name in :data:`FAILURE_EVENTS`; other system payloads are
    left to the opaque ``system`` path (never routed here).
    """
    if not isinstance(payload, dict) or payload.get("event") not in FAILURE_EVENTS:
        return [f"expected event in {sorted(FAILURE_EVENTS)}"]
    return _validate_against(STAGE_FAILURE_SCHEMA, payload)


# -- degradation notices (issue #34) ------------------------------------------
#
# Best-effort services around a turn (recall, the lane strategy, the post-turn
# hooks, the memory indexer) degrade instead of failing the turn. Each swallow
# site now leaves ONE typed ``system`` row the Inspector can render, so the
# degradation is visible instead of stderr-only. The shape differs from a stage
# failure on purpose: a notice says *which step* degraded (``stage``) and *what
# to check* (``reason``), not a raw error blob — and it carries optional seq
# context (``turn``) so a reader can anchor it to a turn.
DEGRADATION_SCHEMA = Schema(
    required={"event": str, "stage": str, "reason": str},
    optional={"turn": int, "seq": int, "hook": str},
)


def validate_degradation_payload(payload: dict) -> list[str]:
    """Validate one degradation ``system`` variant (issue #34).

    Accepts any event name in :data:`DEGRADATION_EVENTS`; other system payloads
    are left to the opaque ``system`` path (never routed here).
    """
    if not isinstance(payload, dict) or payload.get("event") not in DEGRADATION_EVENTS:
        return [f"expected event in {sorted(DEGRADATION_EVENTS)}"]
    return _validate_against(DEGRADATION_SCHEMA, payload)


#: ``system`` payload ``event`` carrying one BTS world-state update (D21).
#: Producer: ``fabulist.graph.nodes`` post-turn world-state node (``bts``
#: interaction). ``kind`` is ``delta``/``checkpoint``; ``state`` is the nested
#: ``{category: {character: {key: value}}}`` map; ``venue``/``off_screen``/
#: ``threads``/``tension`` are the sim/arc/pressure lanes. Extras allowed (D4).
WORLD_STATE_EVENT = "world_state"
WORLD_STATE_SCHEMA = Schema(
    required={"event": str, "kind": str, "state": dict},
    optional={
        "venue": dict,
        "off_screen": list,
        "threads": list,
        "tension": dict,
        "raw": str,
    },
)


def validate_world_state_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``world_state`` payload (D21)."""
    if not isinstance(payload, dict) or payload.get("event") != WORLD_STATE_EVENT:
        return [f"expected event='{WORLD_STATE_EVENT}'"]
    return _validate_against(WORLD_STATE_SCHEMA, payload)


#: ``system`` payload ``event`` carrying one turn's speaker attributions (D25/B5).
#: Producer: ``fabulist.graph.nodes`` post-turn attribution node.
#: ``attributions`` is ``[{speaker, quote}, ...]``; the colour registry is the
#: engine's, never this payload's (W9.28: the model emits the colour in PROSE).
ATTRIBUTION_EVENT = "attribution"
ATTRIBUTION_SCHEMA = Schema(
    required={"event": str, "attributions": list},
    optional={},
)


def validate_attribution_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``attribution`` payload (D25/B5)."""
    if not isinstance(payload, dict) or payload.get("event") != ATTRIBUTION_EVENT:
        return [f"expected event='{ATTRIBUTION_EVENT}'"]
    return _validate_against(ATTRIBUTION_SCHEMA, payload)


# -- canonical cast identity (W7) --------------------------------------------
#
# The W7 cast roster is an append-only event log of typed ``system`` variants
# produced by exactly one writer (:mod:`fabulist.state.cast`). They are
# validated here so a malformed row is rejected at the append door instead of
# landing as an unreadable member (D4 extras stay allowed).
#
# The names are duplicated as literals (rather than imported from
# ``state.cast``) so this low-level gate never depends on the state layer.

#: A member enters the roster (the single admit path).
CAST_ADMIT_EVENT = "cast_admit"
CAST_ADMIT_SCHEMA = Schema(
    required={
        "event": str,
        "member_id": str,
        "name": str,
        "provenance": str,
        "admitted_by": str,
    },
    optional={
        "source_id": str,
        "source_seq": int,
        "aliases": list,
        "role": str,
        "provisional": bool,
        "descriptor_key": str,
    },
)


def validate_cast_admit_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``cast_admit`` payload (W7)."""
    if not isinstance(payload, dict) or payload.get("event") != CAST_ADMIT_EVENT:
        return [f"expected event='{CAST_ADMIT_EVENT}'"]
    return _validate_against(CAST_ADMIT_SCHEMA, payload)


#: A new surface form maps to an existing member (union fold).
CAST_ALIAS_EVENT = "cast_alias"
CAST_ALIAS_SCHEMA = Schema(
    required={"event": str, "member_id": str, "alias": str},
    optional={},
)


def validate_cast_alias_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``cast_alias`` payload (W7)."""
    if not isinstance(payload, dict) or payload.get("event") != CAST_ALIAS_EVENT:
        return [f"expected event='{CAST_ALIAS_EVENT}'"]
    return _validate_against(CAST_ALIAS_SCHEMA, payload)


#: A rename / metadata correction (latest-wins; demotes the old display name).
CAST_PATCH_EVENT = "cast_patch"
CAST_PATCH_SCHEMA = Schema(
    required={"event": str, "member_id": str},
    optional={"display_name": str, "role": str, "provenance": str, "reason": str},
)


def validate_cast_patch_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``cast_patch`` payload (W7 ADDENDUM 1)."""
    if not isinstance(payload, dict) or payload.get("event") != CAST_PATCH_EVENT:
        return [f"expected event='{CAST_PATCH_EVENT}'"]
    return _validate_against(CAST_PATCH_SCHEMA, payload)


#: A descriptor ("the woman") -> member link; live only while present.
CAST_LINK_EVENT = "cast_link"
CAST_LINK_SCHEMA = Schema(
    required={"event": str, "descriptor_key": str, "member_id": str},
    optional={"source": str},
)


def validate_cast_link_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``cast_link`` payload (W7 ADDENDUM 2)."""
    if not isinstance(payload, dict) or payload.get("event") != CAST_LINK_EVENT:
        return [f"expected event='{CAST_LINK_EVENT}'"]
    return _validate_against(CAST_LINK_SCHEMA, payload)


#: Tombstone; a later admit resurrects (D10 append-only).
CAST_RETIRE_EVENT = "cast_retire"
CAST_RETIRE_SCHEMA = Schema(
    required={"event": str, "member_id": str}, optional={"reason": str}
)


def validate_cast_retire_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``cast_retire`` payload (W7)."""
    if not isinstance(payload, dict) or payload.get("event") != CAST_RETIRE_EVENT:
        return [f"expected event='{CAST_RETIRE_EVENT}'"]
    return _validate_against(CAST_RETIRE_SCHEMA, payload)


#: Visibility for a proposal that could not be resolved uniquely (never merged).
CAST_AMBIGUOUS_EVENT = "cast_ambiguous"
CAST_AMBIGUOUS_SCHEMA = Schema(
    required={"event": str, "proposed": str}, optional={"candidates": list}
)


def validate_cast_ambiguous_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``cast_ambiguous`` payload (W7)."""
    if not isinstance(payload, dict) or payload.get("event") != CAST_AMBIGUOUS_EVENT:
        return [f"expected event='{CAST_AMBIGUOUS_EVENT}'"]
    return _validate_against(CAST_AMBIGUOUS_SCHEMA, payload)


# -- W9.22 §2: actors & amendments (promotion gate + review home) -------------
#
# The promotion gate (:mod:`fabulist.state.promotion`) writes one EVENT per
# judgment, all of them ``system`` variants with a fixed one-producer shape —
# pinned here so a malformed row is refused at the append door instead of landing
# as an unreadable verdict (D4 extras stay allowed). The shapes are the cross-lane
# CONTRACT: lane 1a reads ``actor_assessed.member_id`` to close the lane bridge,
# the review home (Wave 4) reads ``asset_proposal`` to render the owner's queue,
# and ``session_actor`` is the promoted actor's definition home (finding 8 — a
# session cast member's stub, the ``session_books`` precedent).

#: One promotion JUDGMENT. ``verdict`` is the gate's decision, ``signal`` the
#: decision model's probability of the ``promoted`` criterion that produced it.
#: Producer: ``state/promotion`` ``PromotionGate._assess``.
ACTOR_ASSESSED_EVENT = "actor_assessed"
ACTOR_ASSESSED_SCHEMA = Schema(
    required={"event": str, "member_id": str, "verdict": str, "signal": float},
    optional={},
)

#: The two verdicts. ``promoted`` re-tiers the member to an actor; ``presence``
#: mutes it from the lanes permanently (both close the bridge — presence's
#: ``assessed_members`` deliberately ignores the value).
ACTOR_VERDICTS = frozenset({"promoted", "presence"})


def validate_actor_assessed_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``actor_assessed`` payload (W9.22 §2).

    The ``verdict`` must be one of :data:`ACTOR_VERDICTS`: a third spelling
    would be a silent no-op on the lane side (the bridge closes on any row) and
    a lie in the review home, so it is refused at the door.
    """
    if not isinstance(payload, dict) or payload.get("event") != ACTOR_ASSESSED_EVENT:
        return [f"expected event='{ACTOR_ASSESSED_EVENT}'"]
    errors = _validate_against(ACTOR_ASSESSED_SCHEMA, payload)
    if payload.get("verdict") not in ACTOR_VERDICTS:
        errors.append("key 'verdict' expects one of presence|promoted")
    return errors


#: A high-signal promotion: the member is now actor tier.
#: Producer: ``state/promotion`` ``_append_promoted``.
ACTOR_PROMOTED_EVENT = "actor_promoted"
ACTOR_PROMOTED_SCHEMA = Schema(
    required={"event": str, "member_id": str, "name": str, "signal": float},
    optional={},
)


def validate_actor_promoted_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``actor_promoted`` payload (W9.22 §2)."""
    if not isinstance(payload, dict) or payload.get("event") != ACTOR_PROMOTED_EVENT:
        return [f"expected event='{ACTOR_PROMOTED_EVENT}'"]
    return _validate_against(ACTOR_PROMOTED_SCHEMA, payload)


#: A promoted actor's stub DEFINITION (``{name, description, aliases}``).
#: Producer: ``state/promotion`` ``_append_promoted``; the definition is assembled
#: deterministically from the BTS temporal bucket, never generated (doctrine 4).
SESSION_ACTOR_EVENT = "session_actor"
SESSION_ACTOR_SCHEMA = Schema(
    required={"event": str, "member_id": str, "definition": dict},
    optional={},
)


def validate_session_actor_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``session_actor`` payload (W9.22 §2).

    Only the definition's PRESENCE is pinned (it is one object, not a shape this
    gate owns); a missing or non-object definition is the failure modes worth
    refusing, since every reader keys on ``member_id`` + the object.
    """
    if not isinstance(payload, dict) or payload.get("event") != SESSION_ACTOR_EVENT:
        return [f"expected event='{SESSION_ACTOR_EVENT}'"]
    return _validate_against(SESSION_ACTOR_SCHEMA, payload)


#: An amendment PROPOSAL the owner disposes of (doctrine 3).
#: Producers: the promotion gate today (low-signal stub); the BTS proposal stage
#: (Wave 3) later. ``target`` is the existing asset for amend/remove and ``null``
#: for add — REQUIRED but nullable, so the shape never varies by action.
#: ``signal`` is the gate's judgment probability, ``null`` for a proposal that
#: was never judged.
ASSET_PROPOSAL_EVENT = "asset_proposal"
ASSET_PROPOSAL_SCHEMA = Schema(
    required={
        "event": str,
        "kind": str,
        "action": str,
        "target": None,
        "patch": dict,
        "origin": str,
        "signal": None,
    },
    #: The justification line (W9.22 follow-up) is OPTIONAL at the door so rows
    #: written before it existed still append and fold; both writers supply it
    #: (proposal_lanes for bts rows, promotion.py for auto-promotions) because
    #: the review home's judgment needs the WHY, not just the diff. Bounded by
    #: the emitters (proposal_lanes.MAX_ITEM_EVIDENCE_CHARS).
    #:
    #: ``target_name`` is the READABLE label for ``target`` (L4 contract
    #: amendment): ``target`` carries an opaque asset id, and the owner cannot
    #: read a card that says ``amend 4f1c9a…``. It is optional (rows written
    #: before it existed keep working) and NEVER an input to an apply — the store
    #: resolves the name from the catalog itself where it can, so a wrong or
    #: missing label never changes what an accept writes.
    #:
    #: ``ops`` is the DIFF-FIRST operation list (W9.23 §3.4): an amend row may
    #: carry an ordered list of ``add``/``modify``/``delete`` operations over
    #: dotted paths instead of a whole-value ``patch``. Additive on the wire and
    #: in the log: a row without it folds exactly as it always did, which is what
    #: keeps every pre-W9.23 amend readable and appliable.
    optional={"evidence": str, "target_name": str, "ops": list, "target_new": bool},
)

#: The asset classes a proposal may touch (Wave 3 adds no fourth without the
#: owner: PLAN §2 non-goals). ``cast`` is the one addition (L4): a candidate cast
#: member the owner gates — it is NOT an asset class (there is no cast
#: definition file: membership is ``cast_admit``/``cast_alias`` rows), which is
#: why :mod:`fabulist.state.proposals` applies it through the cast layer's own
#: helpers instead of an append API, and why ``aliases`` (a CAST-layer fact, see
#: :data:`ASSET_PROPOSAL_PATCH_FIELDS`) finally has a proposal kind to live in.
ASSET_PROPOSAL_KINDS = frozenset({"actor", "world", "book", "cast"})

#: What the proposal does to the asset. ``add`` has no target; ``amend`` /
#: ``remove`` name one.
ASSET_PROPOSAL_ACTIONS = frozenset({"add", "amend", "remove"})

#: The per-operation vocabulary of an amend row's ``ops`` list (W9.23 §3.4.1).
#: ``add`` writes a fact at a path that does not exist yet, ``modify`` replaces an
#: existing one (carrying the value it replaces) and ``delete`` removes EXACTLY
#: its path. There is deliberately no ``move``/``reorder``: a reorder is a
#: ``modify`` pair, and an op the store cannot apply exactly must not exist.
ASSET_PROPOSAL_OP_ACTIONS = frozenset({"add", "modify", "delete"})

#: Who minted the proposal: the BTS proposal stage (``bts``) or the promotion
#: gate (``promotion``) — the review home groups AUTO vs USER-triggered and the
#: owner needs to know which machine proposed it.
ASSET_PROPOSAL_ORIGINS = frozenset({"bts", "promotion"})

#: The definition fields a patch may WRITE, per kind — exactly the fields the
#: asset append APIs persist (:func:`fabulist.worlds.registry.append_world`,
#: :func:`fabulist.context.books.append_book`,
#: :func:`fabulist.personas.registry.append_persona`). They are the teeth of the
#: amend rule: a patch naming no writable field would APPLY AS A NO-OP, i.e. the
#: owner would accept nothing at all, so it is refused at the append door.
#:
#: ``aliases`` is deliberately absent from ``actor``. A promotion stub carries
#: it (BTS-reported surface forms) but the persona fold has no such field:
#: aliases are a CAST-layer fact (``cast_alias`` rows on a session), never an
#: asset field, so an ``aliases``-only amend is a no-op and is refused. Its
#: proper home is the ``cast`` kind below. Extra keys stay accepted in an ``add``
#: (D4 forward compatibility) — only the "nothing writable" amend case fails.
#:
#: Q1 (W9.23 §3.5): the GREETING fields (``first_mes``/``alternate_greetings``)
#: are OUT of proposal scope — a greeting is the owner's voice, not a fact the
#: story discovered, so the gate refuses an amend that names them. They stay on
#: the persona fold (an accepted add may still carry them as D4 extras) but are
#: no longer part of the lane vocabulary. The same holds for the WORLD fold,
#: which now persists the world's opening under those names (issue #51): that
#: opening is the owner's voice too, so neither field is writable by an amend.
#:
#: COVERAGE (L4 audit, and the list the lanes are taught): every field the three
#: asset append APIs persist is writable here — ``world``/
#: :func:`fabulist.worlds.registry.append_world`, ``actor``/
#: :func:`fabulist.personas.registry.append_persona` (including the custom
#: ``extensions`` dict), ``book``/:func:`fabulist.context.books.append_book`.
#: The ids (``world_id``/``persona_id``/``book_id``) are identity and are never
#: writable — an amend may not re-home an asset. Fields a fold does NOT persist
#: are not writable either, because such a patch would apply as a silent no-op:
#: today that is the Card V2 tail the persona fold drops (``mes_example``,
#: ``system_prompt``, ``post_history_instructions``, ``creator_notes``).
ASSET_PROPOSAL_PATCH_FIELDS: dict[str, frozenset[str]] = {
    "actor": frozenset(
        {
            "name",
            "description",
            "personality",
            "scenario",
            "book_ids",
            "extensions",
            "provenance",
            "image_asset",
        }
    ),
    "world": frozenset(
        {
            "name",
            "description",
            "scenario",
            "cast",
            "book_ids",
            "provenance",
            "image_asset",
        }
    ),
    "book": frozenset({"name", "entries", "provenance"}),
    "cast": frozenset({"name", "aliases", "origin"}),
}

#: The writable fields whose LIST ITEMS are addressable by stable key
#: (``<field>.<item id>``, W9.23 §3.4.2). Narrow on purpose: only a book's
#: ``entries`` carry a stable key today (``append_book`` mints ``entry_id``), so
#: only they can be named one-item-at-a-time. ``cast``/``book_ids`` have no key
#: story, so they change whole-field as an explicit ``old -> new`` list — which
#: is displayed and approved as a delta, never as an omission-deletion.
ASSET_PROPOSAL_LIST_FIELDS: dict[str, frozenset[str]] = {
    "book": frozenset({"entries"}),
}

#: The entry keys a book entry op is SHOWN and COMPARED on (W9.23 §3.4.5). The
#: lane slice shows exactly these (plus the stable ``id``, which is addressing
#: and never a value), and the store verifies a modify/delete's ``old`` against
#: the entry PROJECTED to them — so a flag the slice never showed
#: (``constant``/``order``/``case_sensitive``/ST extras) can never be changed by
#: an op, and a MODIFY merges ``new`` over the RAW entry so those flags survive
#: byte-identical.
ENTRY_SHOWN_KEYS: tuple[str, ...] = ("comment", "keys", "content")


def entry_projection(entry: object) -> dict:
    """The SHOWN view of one entry: the three keys an op is shown and compared on.

    Both sides of a comparison are pushed through here, so an author who echoes
    the shown slice (including its ``id``) and one who sends only the three keys
    compare the same. A missing key stays missing on both sides, so an ``old``
    that silently drops a shown field still fails the comparison (the refusal the
    owner wants) rather than matching by omission.
    """
    if not isinstance(entry, dict):
        return {}
    return {key: entry[key] for key in ENTRY_SHOWN_KEYS if key in entry}

#: The writable fields that may ALSO be addressed NESTED (``<field>.<key>``).
#: Coverage widening (L4): the mapping-valued fields are where a definition keeps
#: the things nobody can currently propose — above all a persona's CUSTOM
#: extensions (``extensions.myExt``). Without this, the only way to touch one
#: custom key was to propose the whole ``extensions`` dict, which on accept
#: REPLACES every sibling key: the vocabulary was "complete" on paper and
#: destructive in practice. The store's apply path writes one nested key at a
#: time, so a patch naming ``extensions.mood`` leaves ``extensions.card`` alone.
ASSET_PROPOSAL_NESTED_FIELDS: dict[str, frozenset[str]] = {
    "actor": frozenset({"extensions", "provenance"}),
    "world": frozenset({"provenance"}),
    "book": frozenset({"provenance"}),
}

#: The SHAPE of a writable field's value (the gate's type teeth, L4). A bare type
#: is an exact instance check; ``("list", item)`` is a homogeneous list;
#: ``("maybe", type)`` allows ``None`` as well (an image reference is
#: clearable). Fields not named here are extras (D4) and are not type-checked.
#:
#: Why this exists: the live defect "a book add carried prose stuffed into
#: ``entries`` as a bare string" was ACCEPTED by this gate, applied by
#: ``append_book`` (which silently drops non-dict entries) and folded back as an
#: empty book — the owner accepted a proposal that wrote nothing. A wrong-shaped
#: value is now refused at the append door instead.
ASSET_PROPOSAL_FIELD_TYPES: dict[str, dict[str, object]] = {
    "actor": {
        "name": str,
        "description": str,
        "personality": str,
        "scenario": str,
        "book_ids": ("list", str),
        "extensions": dict,
        "provenance": dict,
        "image_asset": ("maybe", str),
    },
    "world": {
        "name": str,
        "description": str,
        "scenario": str,
        "cast": ("list", str),
        "book_ids": ("list", str),
        "provenance": dict,
        "image_asset": ("maybe", str),
    },
    # ``entries`` is the SillyTavern world-info ENTRY list: one OBJECT per entry
    # (``keys``/``content``/``constant``/``comment``…), never prose.
    "book": {"name": str, "entries": ("list", dict), "provenance": dict},
    "cast": {"name": str, "aliases": ("list", str), "origin": str},
}


def writable_field(kind: str, name: str) -> bool:
    """True when a patch key may WRITE an asset of ``kind``.

    A key is writable when it names a field of
    :data:`ASSET_PROPOSAL_PATCH_FIELDS`, a ``<field>.<key>`` path whose ROOT is
    a mapping field of :data:`ASSET_PROPOSAL_NESTED_FIELDS`, or (W9.23 §3.4.2) a
    ``<list field>.<stable id>`` path whose ROOT is a list field of
    :data:`ASSET_PROPOSAL_LIST_FIELDS`. Every segment must be non-empty, so
    ``extensions.`` and ``.mood`` name nothing; a list item is addressed by
    exactly ONE key segment, so ``entries.a.b`` names nothing either.
    """
    key = str(name or "")
    if key in ASSET_PROPOSAL_PATCH_FIELDS.get(kind, frozenset()):
        return True
    root, separator, rest = key.partition(".")
    if not separator or not rest:
        return False
    if any(not segment for segment in key.split(".")):
        return False
    if root in ASSET_PROPOSAL_NESTED_FIELDS.get(kind, frozenset()):
        return True
    return (
        root in ASSET_PROPOSAL_LIST_FIELDS.get(kind, frozenset())
        and "." not in rest
    )


def list_item_path(kind: str, name: str) -> tuple[str, str] | None:
    """``(list field, item key)`` when ``name`` addresses a list ITEM, else ``None``.

    The one spelling of the ``<list field>.<stable id>`` addressing class
    (W9.23 §3.4.2): the schema gate's writability check, the lane parser's op
    checks, and the store's apply path all read it here, so an item path means
    the same thing at every seam.
    """
    root, separator, rest = str(name or "").partition(".")
    if not separator or not rest or "." in rest:
        return None
    if root not in ASSET_PROPOSAL_LIST_FIELDS.get(kind, frozenset()):
        return None
    return root, rest


def _shape_label(expected: type | None) -> str:
    """Readable field-shape wording for the patch type errors."""
    return {
        str: "a string",
        dict: "an object",
        list: "a list",
        int: "an integer",
        float: "a number",
        bool: "a boolean",
    }.get(expected, _expected_name(expected))


def _shape_errors(kind: str, patch: dict) -> list[str]:
    """Type-check the patch's DECLARED fields (extras are left alone, D4)."""
    shapes = ASSET_PROPOSAL_FIELD_TYPES.get(kind, {})
    errors: list[str] = []
    for name, value in patch.items():
        expected = shapes.get(name)
        if expected is None:
            continue
        errors.extend(_value_errors(f"patch.{name}", expected, value))
    return errors


def _value_errors(label: str, expected: object, value: object) -> list[str]:
    """Type errors for ONE value against one declared shape (``[]`` when fine).

    Shared by the whole-value ``patch`` checks and the op ``old``/``new`` checks
    (W9.23 §3.4.7), so a scalar field is type-checked the same way whichever
    shape carries it. ``extras`` (a path the shapes map does not declare) are
    left alone (D4) — the caller decides whether to look the shape up.
    """
    if isinstance(expected, tuple):
        marker, item = expected
        if marker == "maybe" and value is None:
            return []
        if marker == "maybe":
            expected = item
        elif marker == "list":
            if not isinstance(value, list) or any(
                not _matches(entry, item) for entry in value
            ):
                item_label = "objects" if item is dict else "strings"
                return [f"key '{label}' expects a list of {item_label}"]
            return []
    if not _matches(value, expected):
        return [
            f"key '{label}' expects {_shape_label(expected)}, "
            f"got {_shape_label(type(value))}"
        ]
    return []


def _op_errors(kind: str, ops: list) -> list[str]:
    """Validate an amend row's ``ops`` list (W9.23 §3.4.7).

    Every operation is pinned before it can reach the log, because an op the
    store cannot apply EXACTLY is worse than no op at all (the owner would
    approve a delta that never lands):

    * a well-formed dict whose ``op`` is one of the three verbs and whose
      ``path`` is writable for the row's kind;
    * the verb's key contract — ``add`` needs ``new`` and forbids ``old``,
      ``modify`` needs both, ``delete`` needs ``old`` and forbids ``new``;
    * ``name`` may be renamed (a non-empty string) but never deleted;
    * paths are unique within a row (two ops on one path would make the second
      one's ``old`` a lie about the value the first one wrote);
    * an item path (``entries.<id>``) carries an entry OBJECT on both sides;
      ``old``/``new`` type-check against the declared shape at the path root.
    """
    errors: list[str] = []
    seen: set[str] = set()
    shapes = ASSET_PROPOSAL_FIELD_TYPES.get(kind, {})
    for index, op in enumerate(ops):
        label = f"ops[{index}]"
        if not isinstance(op, dict):
            errors.append(f"key '{label}' expects an object")
            continue
        action = op.get("op")
        path = op.get("path")
        if action not in ASSET_PROPOSAL_OP_ACTIONS:
            errors.append(f"key '{label}.op' expects one of add|delete|modify")
        if not isinstance(path, str) or not path.strip():
            errors.append(f"key '{label}.path' must name a writable path")
            continue
        path = path.strip()
        if not writable_field(kind, path):
            errors.append(f"key '{label}.path' names no writable field")
        elif path in seen:
            errors.append(f"key '{label}.path' repeats an earlier path")
        seen.add(path)
        has_old = "old" in op
        has_new = "new" in op
        if action == "add":
            if not has_new:
                errors.append(f"key '{label}' requires 'new' for op='add'")
            if has_old:
                errors.append(f"key '{label}' forbids 'old' for op='add'")
        elif action == "modify":
            if not has_old or not has_new:
                errors.append(f"key '{label}' requires 'old' and 'new' for op='modify'")
        elif action == "delete":
            if not has_old:
                errors.append(f"key '{label}' requires 'old' for op='delete'")
            if has_new:
                errors.append(f"key '{label}' forbids 'new' for op='delete'")
        if path == "name":
            if action == "delete":
                errors.append(f"key '{label}' may not delete 'name'")
            elif action == "modify":
                new = op.get("new")
                if not isinstance(new, str) or not new.strip():
                    errors.append(f"key '{label}.new' must rename to a non-empty name")
        item = list_item_path(kind, path)
        if item is not None:
            for side in ("old", "new"):
                if side in op and not isinstance(op[side], dict):
                    errors.append(f"key '{label}.{side}' expects an entry object")
        expected = shapes.get(path)
        if expected is not None:
            for side in ("old", "new"):
                if side in op:
                    errors.extend(
                        _value_errors(f"{label}.{side}", expected, op[side])
                    )
    return errors


def validate_asset_proposal_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``asset_proposal`` payload (W9.22 §2).

    Every enumerated value is pinned: an unknown ``kind``/``action``/``origin``
    would be unrenderable in the review home and un-appliable by the asset folds,
    so it is refused at the append door rather than queued as a dead item.

    The action-specific half is what makes the item APPLIABLE
    (:mod:`fabulist.state.proposals`):

    * ``add`` creates a definition, so ``target`` must be ``null`` and the patch
      must name the new asset (all three append APIs raise on an empty name);
    * ``amend`` / ``remove`` name the existing asset in ``target`` (the shape
      never varies by action — ``target`` is required but nullable), and an
      amend patch must carry at least one writable field, otherwise accepting it
      would write nothing.

    A patch VALUE is type-checked for every declared writable field
    (:data:`ASSET_PROPOSAL_FIELD_TYPES`), and a named writable field may also be
    addressed NESTED (:func:`writable_field`) so custom persona extensions can be
    proposed without replacing every sibling key, or as a LIST ITEM
    (``entries.<entry_id>``) where the field has stable item keys.

    DIFF-FIRST amend rows (W9.23 §3.4.7) carry ``ops`` instead of a whole-value
    ``patch``: an amend is either an operation list or a legacy patch, NEVER both
    (a row carrying both would have two conflicting statements about what the
    accept writes). Every op is validated by :func:`_op_errors` — the full
    per-verb key contract, unique paths, a non-empty rename, and entry-OBJECT
    values on item paths — so the store's apply can refuse a stale ``old``
    without ever having to guess a shape.

    ``kind="cast"`` is the ONE kind that is not an asset class: it admits a cast
    candidate (``add`` only, ``target`` null) and the store applies it through
    :func:`fabulist.state.cast.admit_from_proposal` — so its patch must name the
    member (``name``), its surface forms (``aliases``) and the admitting origin
    (``origin``)."""
    if not isinstance(payload, dict) or payload.get("event") != ASSET_PROPOSAL_EVENT:
        return [f"expected event='{ASSET_PROPOSAL_EVENT}'"]
    errors = _validate_against(ASSET_PROPOSAL_SCHEMA, payload)
    kind = payload.get("kind")
    action = payload.get("action")
    if kind not in ASSET_PROPOSAL_KINDS:
        errors.append("key 'kind' expects one of actor|book|cast|world")
    if action not in ASSET_PROPOSAL_ACTIONS:
        errors.append("key 'action' expects one of add|amend|remove")
    if payload.get("origin") not in ASSET_PROPOSAL_ORIGINS:
        errors.append("key 'origin' expects one of bts|promotion")
    if "target_new" in payload and payload.get("target_new") is not False:
        # W9.30 §3.2: the born-parented marker is set by the parser, never the
        # model, and it is a BOOKS-AMEND-only concept — the one shape whose
        # accept may materialize a missing parent (§4.1). Anywhere else the
        # flag would be a second, contradictory statement of what accept writes.
        if kind != "book" or action != "amend":
            errors.append(
                "key 'target_new' is only legal for kind='book' action='amend'"
            )

    patch = payload.get("patch")
    patch = patch if isinstance(patch, dict) else {}
    ops = payload.get("ops")
    ops = ops if isinstance(ops, list) else None
    target = payload.get("target")
    kind_name = kind if isinstance(kind, str) else ""
    if ops is not None:
        if action != "amend":
            # Only an amend is expressed as a delta: an ``add`` has nothing to
            # diff against and a ``remove`` deletes the whole asset, so an op
            # list on either would silently change what the action means.
            errors.append("key 'ops' is only legal for action='amend'")
        elif patch:
            errors.append(
                "key 'patch' must be empty when 'ops' carries the amendment"
            )
        else:
            errors.extend(_op_errors(kind_name, ops))
    if kind_name == "cast":
        # A cast member is not a stored definition, so there is nothing to amend
        # or remove: the only cast proposal is "admit this candidate".
        if action != "add":
            errors.append("key 'action' must be 'add' for kind='cast'")
        for required in ("name", "aliases", "origin"):
            if required not in patch:
                errors.append(
                    f"key 'patch' must carry '{required}' for kind='cast'"
                )
    if action == "add":
        if target is not None:
            errors.append("key 'target' must be null for action='add'")
        name = patch.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append("key 'patch' must name the new asset for action='add'")
    elif action in ASSET_PROPOSAL_ACTIONS:
        if not isinstance(target, str) or not target.strip():
            errors.append(
                f"key 'target' must name the existing asset for action='{action}'"
            )
        if action == "amend" and ops is None and not any(
            writable_field(kind_name, key) for key in patch
        ):
            errors.append(
                "key 'patch' must carry at least one writable field for "
                "action='amend'"
            )
    errors.extend(_shape_errors(kind_name, patch))
    return errors


#: One proposal DECISION the owner took (Doctrine 3: the owner disposes).
#: Producer: :mod:`fabulist.state.proposals` (``accept_proposal`` /
#: ``reject_proposal`` / ``dismiss_proposal`` / ``accept_many``).
#:
#: The row is a SNAPSHOT, not a pointer: it carries the resolved
#: ``kind``/``action``/``target`` and the ``pre_image`` captured at ACCEPT time
#: (finding 13 — worlds/books/personas fold whole definitions latest-wins, so an
#: UNDO needs the definition the accept overwrote) plus ``applied``, the
#: definition the accept wrote. Reject/dismiss write no asset, so both are
#: ``null`` there — a decision row that claims an apply it never made would put a
#: dead UNDO in the review home.
PROPOSAL_DECISION_EVENT = "proposal_decision"
PROPOSAL_DECISION_SCHEMA = Schema(
    required={
        "event": str,
        "proposal_id": str,
        "decision": str,
        "kind": str,
        "action": str,
        "target": None,
        "pre_image": None,
        "applied": None,
    },
    optional={"target_new": bool},
)

#: The three terminal decisions. ``reject`` = "no, do not do that"; ``dismiss`` =
#: "not now". Both leave the live set; neither touches an asset.
PROPOSAL_DECISIONS = frozenset({"accept", "reject", "dismiss"})


def validate_proposal_decision_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``proposal_decision`` payload (W9.22 §4).

    Beyond the enumerated values this pins the UNDO contract: an accepted
    ``add`` records what it created (no pre-image — nothing existed), an accepted
    ``amend``/``remove`` records the pre-image it overwrote (and the amended
    definition; a remove leaves nothing behind), and a reject/dismiss records
    neither. The gate therefore guarantees that every accepted decision in the
    log is undoable and that no declining decision pretends otherwise.
    """
    if not isinstance(payload, dict) or payload.get("event") != PROPOSAL_DECISION_EVENT:
        return [f"expected event='{PROPOSAL_DECISION_EVENT}'"]
    errors = _validate_against(PROPOSAL_DECISION_SCHEMA, payload)
    decision = payload.get("decision")
    action = payload.get("action")
    if decision not in PROPOSAL_DECISIONS:
        errors.append("key 'decision' expects one of accept|dismiss|reject")
    if payload.get("kind") not in ASSET_PROPOSAL_KINDS:
        errors.append("key 'kind' expects one of actor|book|cast|world")
    if action not in ASSET_PROPOSAL_ACTIONS:
        errors.append("key 'action' expects one of add|amend|remove")

    pre_image = payload.get("pre_image")
    applied = payload.get("applied")
    if decision != "accept":
        if pre_image is not None or applied is not None:
            errors.append(
                "keys 'pre_image'/'applied' must be null unless decision='accept'"
            )
    elif action == "add":
        if pre_image is not None:
            errors.append("key 'pre_image' must be null for an accepted add")
        if not isinstance(applied, dict):
            errors.append("key 'applied' must record the created definition")
    elif action in ASSET_PROPOSAL_ACTIONS:
        if not isinstance(pre_image, dict) and not (
            action == "amend"
            and pre_image is None
            and payload.get("target_new") is True
        ):
            # W9.30 §4.1: a born-parented amend ACCEPTS with no pre-image —
            # nothing existed. The marker is the only license (the ordinary
            # amend-against-missing still refuses at apply; the 441 pin).
            errors.append("key 'pre_image' must record the overwritten definition")
        if action == "amend" and not isinstance(applied, dict):
            errors.append("key 'applied' must record the amended definition")
        if action == "remove" and applied is not None:
            errors.append("key 'applied' must be null for an accepted remove")
    return errors


#: A compensating UNDO of one accepted decision (finding 13: undo is an APPEND,
#: never a rewrite — D9/D10). Producer: ``fabulist.state.proposals``
#: ``undo_decision``; it restores the decision's captured pre-image through the
#: same asset append API, so the asset's own history stays intact and readable
#: and this row is only the "that apply was taken back" fact.
PROPOSAL_UNDO_EVENT = "proposal_undo"
PROPOSAL_UNDO_SCHEMA = Schema(required={"event": str, "decision_id": str}, optional={})


def validate_proposal_undo_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``proposal_undo`` payload (W9.22 §4)."""
    if not isinstance(payload, dict) or payload.get("event") != PROPOSAL_UNDO_EVENT:
        return [f"expected event='{PROPOSAL_UNDO_EVENT}'"]
    return _validate_against(PROPOSAL_UNDO_SCHEMA, payload)


# -- the historian's run trace (W9.23 §3.6.3) --------------------------------
#
# The historian runs the proposal lanes OFF-TURN, so the turn's ``turn_stages``
# trace cannot carry its outcome. Its own two rows are the observable trace: a
# ``started`` row and the ``finished`` row with the run's counts. The CLOCK the
# due rule keys on is the last run that CONSUMED its window (a settled, non-missed
# row — a failed run retries on the next committed turn instead of backing off a
# cycle; historian §3.6.3), never the latest row of any status.
#
#: ``system`` payload ``event`` carrying one historian run's start or outcome.
#: Producer: ``fabulist.state.historian`` (``HistorianRunner.run``).
HISTORIAN_RUN_EVENT = "historian_run"

#: The lifecycle statuses. ``failed`` is a runner-level failure (the lanes are
#: best-effort and record their own reasons inside a ``finished`` row's
#: ``failed_lanes``; this one means the run itself could not complete).
HISTORIAN_RUN_STATUSES = frozenset({"started", "finished", "failed"})

#: What asked for the run — the cadence clock, or the owner's run-now door.
HISTORIAN_RUN_TRIGGERS = frozenset({"cadence", "manual"})

#: The dispatcher's gate answers, carried onto the ``finished`` row so an
#: inspector can tell a quiet window from a broken one. ``unsure`` is the distinct
#: outcome of a should-we-propose answer whose confidence fell below the floor
#: (:data:`fabulist.state.proposal_lanes.GATE_CONFIDENCE_FLOOR`): no lane ran,
#: and unlike ``verdict_no`` the window was never really judged.
HISTORIAN_RUN_GATES = frozenset(
    {
        "proceeded",
        "no_evidence",
        "gate_off",
        "verdict_no",
        "unsure",
        "no_generator",
        "error",
    }
)

HISTORIAN_RUN_SCHEMA = Schema(
    required={"event": str, "status": str, "trigger": str, "turn_seq": int},
    optional={
        "window_turns": int,
        "gate": str,
        "filed": int,
        "dropped": int,
        "failed_lanes": list,
        "duration_ms": int,
        "error": str,
        "promotions": int,
    },
)


def validate_historian_run_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``historian_run`` payload (W9.23 §3.6.3).

    Three statuses, each with its own keys: ``started`` opens the trace
    (``turn_seq`` + ``window_turns``, no counts yet — it is NOT the clock),
    ``finished`` carries the dispatcher's ``gate`` and the run's counts,
    ``failed`` carries ``error``.
    Each status's own keys are checked because a count that never arrives would
    render as a silent zero in the drawer.
    """
    if not isinstance(payload, dict) or payload.get("event") != HISTORIAN_RUN_EVENT:
        return [f"expected event='{HISTORIAN_RUN_EVENT}'"]
    errors = _validate_against(HISTORIAN_RUN_SCHEMA, payload)
    status = payload.get("status")
    if status not in HISTORIAN_RUN_STATUSES:
        errors.append("key 'status' expects one of started|finished|failed")
    if payload.get("trigger") not in HISTORIAN_RUN_TRIGGERS:
        errors.append("key 'trigger' expects one of cadence|manual")
    if status == "started" and "window_turns" not in payload:
        errors.append("missing required key 'window_turns' for status='started'")
    if status == "finished":
        for key in ("gate", "filed", "dropped", "failed_lanes", "duration_ms"):
            if key not in payload:
                errors.append(f"missing required key '{key}' for status='finished'")
        if payload.get("gate") not in HISTORIAN_RUN_GATES:
            errors.append(
                "key 'gate' expects one of proceeded|no_evidence|gate_off|"
                "verdict_no|unsure|no_generator|error"
            )
    if status == "failed" and "error" not in payload:
        errors.append("missing required key 'error' for status='failed'")
    return errors


# -- the agent-loop runner's iteration trace (W9.24 §3.2) -------------------
#
# Every iteration of the agent-loop runtime (``fabulist.agents.loop``) appends
# one of these rows: the RUNNER is engine code, so the engine appends (D9 — the
# model never writes the log) and the row is the audit trail of what the model
# asked for and what the engine did about it. The rows are diagnostics, not
# narrative corrections: repairs land as revisions through ``append_revision``
# (D10), never here.
#
#: ``system`` payload ``event`` carrying one agent-loop iteration row.
#: Producer: ``fabulist.agents.loop.run_agent_loop``.
AGENT_LOOP_EVENT = "agent_loop"

#: What the row measured. ``completion`` = one model call drained;
#: ``tool_call`` = the call the model made (or the engine injected);
#: ``tool_result`` = the engine-side execution outcome; ``final`` = the loop's
#: answer row; ``timeout`` = the iteration a transport timeout ended.
AGENT_LOOP_KINDS = frozenset(
    {"completion", "tool_call", "tool_result", "final", "timeout"}
)

AGENT_LOOP_SCHEMA = Schema(
    required={"event": str, "surface": str, "iteration": int, "kind": str},
    optional={
        "latency_s": float,
        "tool": str,
        "args": dict,
        "result": dict,
        "text": str,
        "injected": bool,
        "timed_out": bool,
        "finish_reason": str,
        "error": str,
        "timeout_s": float,
        # W9.25 §3.5.3: passive headroom (available prompt tokens − estimate).
        # Negative is legal and meaningful: the loop PROCEEDS past zero by owner
        # ruling (no stop ships; W9.26 owns the compaction-aware design).
        "headroom": int,
    },
)


def validate_agent_loop_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``agent_loop`` payload (W9.24 §3.2).

    Five kinds, each with its own keys, because a row that silently omits what
    it measured reads as a clean iteration: a ``tool_call`` without a ``tool``
    names nothing, a ``final`` without ``timed_out`` hides whether a transport
    timeout ended the loop, and a ``completion``/``tool_result`` without
    ``latency_s`` loses the only cost signal these rows carry.
    """
    if not isinstance(payload, dict) or payload.get("event") != AGENT_LOOP_EVENT:
        return [f"expected event='{AGENT_LOOP_EVENT}'"]
    errors = _validate_against(AGENT_LOOP_SCHEMA, payload)
    kind = payload.get("kind")
    if kind not in AGENT_LOOP_KINDS:
        errors.append(
            "key 'kind' expects one of completion|tool_call|tool_result|final|timeout"
        )
    iteration = payload.get("iteration")
    if isinstance(iteration, int) and not isinstance(iteration, bool) and iteration < 1:
        errors.append("key 'iteration' expects a 1-based iteration number")
    if kind in {"completion", "tool_result"} and "latency_s" not in payload:
        errors.append(f"missing required key 'latency_s' for kind='{kind}'")
    if kind == "tool_call" and "tool" not in payload:
        errors.append("missing required key 'tool' for kind='tool_call'")
    if kind == "final" and "timed_out" not in payload:
        errors.append("missing required key 'timed_out' for kind='final'")
    if kind == "timeout" and "error" not in payload:
        errors.append("missing required key 'error' for kind='timeout'")
    return errors


# -- the validate stage's teaching notice (D-F) ----------------------------
#
# A committed turn the validate stage flagged (Dress Rehearsal flags, the DSL
# gate's findings, the prose-markup detect) already records a ``validator_flags``
# audit row. This is the SAME finding list as a TEACHING row the narrator reads
# on later turns: a ``system`` turn placed in the prompt immediately after the
# narrative it refers to (owner ruling — steer the model toward proper output
# instead of only auditing it). The engine appends it (D9); the model never
# writes the log.
#
#: ``system`` payload ``event`` carrying one flagged turn's teaching notice.
#: Producer: ``fabulist.graph.nodes.make_validate_node``.
VALIDATION_NOTICE_EVENT = "validation_notice"

# -- W9.29 entity objects: the registry event family (plan §5) ----------------
#
# One writer (``fabulist.state.entities``); every action is one typed variant of
# the open ``system`` event, id-addressed, so a fold replays without the
# whole-payload overwrite semantics the ``world_state`` delta carried. The
# payload sets here are the plan §5 table exactly as the writers emit them
# (pinned by ``tests/test_entities.py::test_appended_payloads_match_the_plan_table``).
# ``value`` / ``subject`` / ``at`` are declared any: a meter's VALUE semantics
# (numeric, in the catalog range) belong to the catalog at the tool door, not to
# this shape gate (D69's boundary discipline: the gate never invents a second
# schema).
ENTITY_ESTABLISH_EVENT = "entity_establish"
ENTITY_SET_EVENT = "entity_set"
ENTITY_UPDATE_EVENT = "entity_update"
ENTITY_MOVE_EVENT = "entity_move"
ENTITY_RETIRE_EVENT = "entity_retire"
ENTITY_SUMMARY_EVENT = "entity_summary"
ENTITY_CATALOG_EVENT = "entity_catalog"

#: The entity family's event names (one dispatch test, seven writers).
_ENTITY_EVENTS = frozenset(
    {
        ENTITY_ESTABLISH_EVENT,
        ENTITY_SET_EVENT,
        ENTITY_UPDATE_EVENT,
        ENTITY_MOVE_EVENT,
        ENTITY_RETIRE_EVENT,
        ENTITY_SUMMARY_EVENT,
        ENTITY_CATALOG_EVENT,
    }
)

#: The object kinds an establish row may create (plan §3; ``loc_off_screen``
#: is a location, never a kind of its own). W9.32 adds the three state kinds:
#: ``condition`` (host + key + severity/source/expires_at), ``body`` (one per
#: being: pose/clothing/surface) and ``economy`` (one per session: denomination
#: + balance). ``name`` stays REQUIRED for every kind — the writers synthesize it
#: (condition → the condition key, body → the being's display, economy → the
#: denomination), so the gate needs no kind-conditional rule.
ENTITY_KINDS = frozenset(
    {"location", "thread", "relationship", "item", "condition", "body", "economy"}
)

ENTITY_ESTABLISH_SCHEMA = Schema(
    required={"event": str, "kind": str, "id": str, "name": str},
    optional={
        "parent_id": str,
        "from": str,
        "to": str,
        "holder": str,
        "note": str,
        "detail": str,
        "seq": int,
        # W9.32: condition / body / economy fields (additive optional keys —
        # every pre-W9.32 row stays valid, D4 discipline).
        "host": str,
        "key": str,
        "severity": str,
        "source": str,
        "expires_at": str,
        "pose": str,
        "clothing": list,
        "surface": str,
        "denomination": str,
        "balance": int,
    },
)
ENTITY_SET_SCHEMA = Schema(
    required={"event": str, "id": str, "host": str, "key": str},
    optional={"value": None, "subject": None, "seq": int},
)
ENTITY_UPDATE_SCHEMA = Schema(
    required={"event": str, "id": str, "patch": dict},
    optional={"seq": int},
)
ENTITY_MOVE_SCHEMA = Schema(
    required={"event": str, "who": str, "to": str},
    optional={"at": None, "seq": int},
)
ENTITY_RETIRE_SCHEMA = Schema(
    required={"event": str, "id": str},
    optional={"reason": str, "seq": int},
)
ENTITY_SUMMARY_SCHEMA = Schema(
    required={"event": str, "seq": int, "actions": int, "rejected": int, "flags": int},
    optional={},
)
ENTITY_CATALOG_SCHEMA = Schema(required={"event": str, "rows": list}, optional={})

_ENTITY_SCHEMAS = {
    ENTITY_ESTABLISH_EVENT: ENTITY_ESTABLISH_SCHEMA,
    ENTITY_SET_EVENT: ENTITY_SET_SCHEMA,
    ENTITY_UPDATE_EVENT: ENTITY_UPDATE_SCHEMA,
    ENTITY_MOVE_EVENT: ENTITY_MOVE_SCHEMA,
    ENTITY_RETIRE_EVENT: ENTITY_RETIRE_SCHEMA,
    ENTITY_SUMMARY_EVENT: ENTITY_SUMMARY_SCHEMA,
    ENTITY_CATALOG_EVENT: ENTITY_CATALOG_SCHEMA,
}


def validate_entity_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``entity_*`` payload (W9.29 §5).

    The family shares one door: the ``event`` name picks the schema (the plan
    table), and an establish row's ``kind`` must be one of :data:`ENTITY_KINDS` —
    anything else would fold as an object nothing can address.
    """
    if not isinstance(payload, dict):
        return ["entity payload must be an object"]
    variant = payload.get("event")
    schema = _ENTITY_SCHEMAS.get(variant)
    if schema is None:
        return [f"unknown entity event: {variant!r}"]
    errors = _validate_against(schema, payload)
    if variant == ENTITY_ESTABLISH_EVENT:
        kind = payload.get("kind")
        if kind not in ENTITY_KINDS:
            errors.append(
                f"key 'kind' expects one of {sorted(ENTITY_KINDS)}, got {kind!r}"
            )
    return errors

#: ``system`` payload ``event`` carrying one ACCEPTED model time proposal
#: (W9.32 §3.2). Producer: ``fabulist.state.entity_tools``' ``propose_time``.
#: The row is the AUDIT record — the evidence string plus the instant the engine
#: accepted — never a storage path: live time still folds through the
#: clock-owned ``state-delta`` keys, so one clock has one home (D3 reversal §4).
CLOCK_PROPOSAL_EVENT = "clock_proposal"
CLOCK_PROPOSAL_SCHEMA = Schema(
    required={"event": str, "time": str, "date": str, "day": int, "evidence": str},
    optional={"advanced_minutes": int, "seq": int},
)


def validate_clock_proposal_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``clock_proposal`` payload (W9.32 §3.2).

    Evidence is the point of the row: a time move with no prose behind it is
    exactly what the guardrails exist to prevent, so an empty string is an
    error rather than a silent no-op.
    """
    if not isinstance(payload, dict) or payload.get("event") != CLOCK_PROPOSAL_EVENT:
        return [f"expected event='{CLOCK_PROPOSAL_EVENT}'"]
    errors = _validate_against(CLOCK_PROPOSAL_SCHEMA, payload)
    evidence = payload.get("evidence")
    if isinstance(evidence, str) and not evidence.strip():
        errors.append("key 'evidence' expects the prose that motivated the move")
    return errors


#: Declared shape of a notice payload. ``seq`` is the flagged NARRATIVE row's
#: seq (the notice's natural position in the transcript and the prompt is
#: directly after that row), ``flags`` is the teaching list — one dict per
#: finding, ``check``/``severity``/``anchor``/``fix`` load-bearing, extras
#: allowed (D4). ``score`` is the validator score the flags produced.
VALIDATION_NOTICE_SCHEMA = Schema(
    required={"event": str, "seq": int, "flags": list},
    optional={"score": int},
)


def validate_validation_notice_payload(payload: dict) -> list[str]:
    """Validate one ``system``/``validation_notice`` payload (D-F).

    A notice that names no narrative (``seq``) cannot be placed after the turn
    it refers to, and a notice with no findings teaches nothing — both are
    rejected rather than logged as silent no-ops.
    """
    if not isinstance(payload, dict) or payload.get("event") != VALIDATION_NOTICE_EVENT:
        return [f"expected event='{VALIDATION_NOTICE_EVENT}'"]
    errors = _validate_against(VALIDATION_NOTICE_SCHEMA, payload)
    seq = payload.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool) and seq < 1:
        errors.append("key 'seq' expects the flagged narrative's 1-based seq")
    flags = payload.get("flags")
    if isinstance(flags, list):
        if not flags:
            errors.append("key 'flags' expects at least one finding")
        elif not all(isinstance(flag, dict) for flag in flags):
            errors.append("key 'flags' expects a list of finding objects")
    return errors


#: ``system`` payload ``event`` recording a component-set swap (W9.27 C2).
#: Producer: the selection door (``POST /api/sessions/{sid}/component-set``) —
#: a per-session LOG fact (issues #16/#24); the narrator builder RESOLVES it at
#: its next ``build()`` (:func:`fabulist.wiring.resolve_component_set`) and the
#: store writes no rows (D9). It carries what changed (``previous_set_id``,
#: ``set_id``, ``name``, the vocabulary ``version``) plus the model-facing
#: ``notice`` the dynamic ``<recalled>`` tail renders, frozen at selection so a
#: later rewording cannot rewrite what an earlier turn's prompt carried.
#: ``system`` payloads are permissive by design (extras pass), so this is a
#: NAME, not a schema.
SET_SWAP_EVENT = "set_swapped"


def _expected_name(expected: type | None) -> str:
    """Human-readable name for a schema type (``None`` renders as ``any``)."""
    if expected is None:
        return "any"
    return expected.__name__


def _matches(value: Any, expected: type | None) -> bool:
    """True when ``value`` satisfies ``expected`` under bool-not-int strictness."""
    if expected is None:
        return True
    if expected is bool:
        return isinstance(value, bool)
    if expected is int:
        # isinstance(True, int) is True; exclude booleans explicitly.
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, expected)


def validate_event(event_type: str, payload: dict) -> list[str]:
    """Validate one payload against its event schema.

    Returns a list of human-readable errors; an empty list means the payload is
    acceptable. Unknown ``event_type`` yields a single error. Missing declared
    keys yield ``missing required key 'k'``; wrong-typed values yield
    ``key 'k' expects <t>, got <t>``. Keys not declared in the schema are always
    allowed (D4 forward compatibility).
    """
    if event_type == "system" and isinstance(payload, dict) and payload.get("event") == USAGE_EVENT:
        # A usage payload is a typed variant of the open system event (I1).
        return validate_usage_payload(payload)

    if (
        event_type == "system"
        and isinstance(payload, dict)
        and payload.get("event") == PROMPT_BUDGET_EVENT
    ):
        # An over-budget prompt estimate is a typed variant too (W9.25 §3.5).
        return validate_prompt_budget_payload(payload)

    if (
        event_type == "system"
        and isinstance(payload, dict)
        and payload.get("event") == SPEAKER_COLOR_EVENT
    ):
        # A speaker colour is a typed variant of the open system event (D25).
        return validate_speaker_color_payload(payload)

    if (
        event_type == "system"
        and isinstance(payload, dict)
        and payload.get("event") == CONTEXT_LOW_EVENT
    ):
        # The context gauge's reading is a typed variant too (W9.26 §2.2).
        return validate_context_low_payload(payload)

    if (
        event_type == "system"
        and isinstance(payload, dict)
        and payload.get("event") == COMPACTION_MARKER_EVENT
    ):
        # The era marker appended to a successor session (W9.26 §2.3 step 9).
        return validate_compaction_marker_payload(payload)

    if event_type == "system" and isinstance(payload, dict):
        # Other typed variants of the open system event with a stable one-
        # producer shape: a malformed payload is rejected at append. Payloads
        # whose ``event`` is not one of these fall through to the opaque
        # system schema (permissive by design).
        variant = payload.get("event")
        if variant == AGENT_CALL_EVENT:
            return validate_agent_call_payload(payload)
        if variant == ASSISTANT_REPLY_EVENT:
            return validate_assistant_reply_payload(payload)
        if variant == SIDEKICK_REPLY_EVENT:
            return validate_sidekick_reply_payload(payload)
        if variant == IMAGE_CAPTION_EVENT:
            return validate_image_caption_payload(payload)
        if variant == WORLD_STATE_EVENT:
            return validate_world_state_payload(payload)
        if variant == ATTRIBUTION_EVENT:
            return validate_attribution_payload(payload)
        if variant == CAST_ADMIT_EVENT:
            return validate_cast_admit_payload(payload)
        if variant == CAST_ALIAS_EVENT:
            return validate_cast_alias_payload(payload)
        if variant == CAST_PATCH_EVENT:
            return validate_cast_patch_payload(payload)
        if variant == CAST_LINK_EVENT:
            return validate_cast_link_payload(payload)
        if variant == CAST_RETIRE_EVENT:
            return validate_cast_retire_payload(payload)
        if variant == CAST_AMBIGUOUS_EVENT:
            return validate_cast_ambiguous_payload(payload)
        # W9.22 §2: the promotion gate's four actor/amendment rows.
        if variant == ACTOR_ASSESSED_EVENT:
            return validate_actor_assessed_payload(payload)
        if variant == ACTOR_PROMOTED_EVENT:
            return validate_actor_promoted_payload(payload)
        if variant == SESSION_ACTOR_EVENT:
            return validate_session_actor_payload(payload)
        if variant == ASSET_PROPOSAL_EVENT:
            return validate_asset_proposal_payload(payload)
        # W9.22 §4: the review home's decision + undo rows.
        if variant == PROPOSAL_DECISION_EVENT:
            return validate_proposal_decision_payload(payload)
        if variant == PROPOSAL_UNDO_EVENT:
            return validate_proposal_undo_payload(payload)
        # W9.23 §3.6.3: the historian's off-turn run trace.
        if variant == HISTORIAN_RUN_EVENT:
            return validate_historian_run_payload(payload)
        # W9.24 §3.2: the agent-loop runtime's per-iteration trace.
        if variant == AGENT_LOOP_EVENT:
            return validate_agent_loop_payload(payload)
        # D-F: the validate stage's teaching notice for a flagged turn.
        if variant == VALIDATION_NOTICE_EVENT:
            return validate_validation_notice_payload(payload)
        # W9.32: the engine's audit row for an accepted model time proposal.
        if variant == CLOCK_PROPOSAL_EVENT:
            return validate_clock_proposal_payload(payload)
        # W9.29: the entity registry family (one writer, one schema per variant).
        if variant in _ENTITY_EVENTS:
            return validate_entity_payload(payload)
        if variant in FAILURE_EVENTS:
            return validate_stage_failure_payload(payload)

    schema = EVENT_SCHEMAS.get(event_type)
    if schema is None:
        return [f"unknown event type: {event_type}"]

    return _validate_against(schema, payload)


def _validate_against(schema: Schema, payload: dict) -> list[str]:
    """Validate ``payload`` against one :class:`Schema` (shared by all types).

    A non-dict payload is accepted (returns ``[]``): the ``system`` event is
    documented to legitimately carry a *bare list* (tests/test_assistants_api),
    so the gate must not tighten that (D4 forward compatibility). Typed checks
    only apply to dict payloads.
    """
    if not isinstance(payload, dict):
        return []

    errors: list[str] = []
    for key, expected in schema.required.items():
        if key not in payload:
            errors.append(f"missing required key '{key}'")
        elif not _matches(payload[key], expected):
            errors.append(
                f"key '{key}' expects {_expected_name(expected)}, "
                f"got {type(payload[key]).__name__}"
            )

    for key, expected in schema.optional.items():
        if key in payload and not _matches(payload[key], expected):
            errors.append(
                f"key '{key}' expects {_expected_name(expected)}, "
                f"got {type(payload[key]).__name__}"
            )

    return errors


def narrative_config_snapshot(
    model: str,
    sampler: str,
    directive_version: str,
    facts_hash: str = "",
) -> dict[str, str]:
    """Build the canonical provenance block stored on a narrative payload.

    All four fields are strings so the snapshot serializes identically wherever
    it is embedded. ``facts_hash`` may legitimately be empty (no facts folded
    yet); every other field is coerced with ``str`` so a numeric model id or a
    mapping-style sampler cannot produce a type-variant snapshot.
    """
    return {
        "model": str(model),
        "sampler": str(sampler),
        "directive_version": str(directive_version),
        "facts_hash": str(facts_hash),
    }
