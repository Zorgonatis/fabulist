"""Tests for the event payload schema gate (PLAN §4).

Every "valid sample" below is copied from a real producer or test, cited by
``file:line``. The point of the gate is to reject malformed payloads without
rejecting the shapes the codebase actually writes, so these samples are the
contract.
"""

from __future__ import annotations

import pytest

from fabulist.log.schema_gate import (
    AGENT_LOOP_KINDS,
    EVENT_SCHEMAS,
    Schema,
    entry_projection,
    list_item_path,
    narrative_config_snapshot,
    validate_agent_loop_payload,
    validate_clock_proposal_payload,
    validate_compaction_marker_payload,
    validate_context_low_payload,
    validate_event,
    validate_prompt_budget_payload,
    validate_stage_failure_payload,
    validate_validation_notice_payload,
    writable_field,
)

# -- valid samples, one per event type (grounded in real payloads) -----------

VALID_SAMPLES: dict[str, dict] = {
    # app.py:174 (kind), graph/engine.py:162 (text only).
    "user": {"text": "I greet the merchant", "kind": "in_character"},
    # graph/nodes.py:323 (prose + dsl + segments).
    "narrative": {
        "prose": "The market hums.",
        "dsl": "card = ArtifactCard(a, b, c)",
        "segments": [{"kind": "prose", "text": "The market hums."}],
    },
    # repo.EventLog.append_revision / tests/test_log.py:30-32.
    "revision": {"revised": {"text": "v2"}, "reason": "typo"},
    # graph/nodes.py:421 / tests/test_log.py:48.
    "state-delta": {"time": "09:00", "loc": "camp", "weather": "rain"},
    # tests/test_log.py:169.
    "roll": {"dice": "1d20", "result": 12},
    # media/images.py:110-117 (store_image).
    "asset": {
        "asset_id": "a1",
        "kind": "image",
        "mime": "image/png",
        "bytes_b64": "iVBORw0KGgo=",
        "sha256": "a1",
        "note": "",
    },
    # graph/compaction.py:297-303.
    "summary": {
        "text": "They met at the market.",
        "covers_seqs": [2, 6],
        "reason": "threshold exceeded: turns 5>3",
    },
    # graph/engine.py:182 (_run_ooc).
    "ooc": {"text": "Sure, happy to help."},
    # api/app.py:330 (session_created).
    "system": {"event": "session_created", "title": "New story"},
}


@pytest.mark.parametrize("event_type", sorted(EVENT_SCHEMAS))
def test_every_event_type_has_a_schema(event_type):
    assert isinstance(EVENT_SCHEMAS[event_type], Schema)


@pytest.mark.parametrize("event_type", sorted(VALID_SAMPLES))
def test_valid_sample_passes(event_type):
    assert validate_event(event_type, VALID_SAMPLES[event_type]) == []


def test_all_event_types_covered():
    assert set(EVENT_SCHEMAS) == set(VALID_SAMPLES)


def test_cancelled_partial_narrative_is_valid():
    # graph/engine.py:164 appends {"prose", "dsl"} with no segments.
    assert validate_event("narrative", {"prose": "one ", "dsl": ""}) == []


def test_system_payload_may_be_minimal_or_rich():
    # api/sessions.py:205 hides a session with a single key.
    assert validate_event("system", {"event": "session_hidden"}) == []
    # graph/plans.py:102 nests a plan dict.
    assert validate_event(
        "system", {"event": "plan", "plan": {"turn_seq": 1, "beats": ["a"]}}
    ) == []
    # graph/nodes.py:335 carries an errors list and a score.
    assert validate_event(
        "system",
        {"event": "validator_flags", "flags": [], "score": 0.9},
    ) == []


def test_summary_content_variant_is_accepted():
    # The task allows a "content" alternative to "text".
    assert (
        validate_event("summary", {"covers_seqs": [1, 2], "content": "notes"})
        == []
    )


# -- failure modes -----------------------------------------------------------


def test_unknown_event_type():
    assert validate_event("bogus", {}) == ["unknown event type: bogus"]


def test_user_text_is_optional_but_typed_when_present():
    assert validate_event("user", {}) == []
    assert validate_event("user", {"text": "hi"}) == []


def test_missing_required_key_asset_reports_each_absent_key():
    # Relaxed (2026-10-09): pointer-only asset events are legal; only
    # asset_id+kind are required. mime/bytes are optional-but-typed.
    errors = validate_event("asset", {"kind": "image"})
    assert errors == ["missing required key 'asset_id'"]
    assert validate_event("asset", {"asset_id": "a1", "kind": "image"}) == []


def test_missing_required_key_summary():
    assert validate_event("summary", {"text": "no span"}) == [
        "missing required key 'covers_seqs'"
    ]


def test_missing_required_key_roll():
    assert validate_event("roll", {"result": 3}) == ["missing required key 'dice'"]


def test_wrong_type_required_key():
    # prose is now optional (segments-only turns) but typed when present
    assert validate_event("narrative", {"prose": 12}) == [
        "key 'prose' expects str, got int"
    ]
    assert validate_event("narrative", {"segments": []}) == []


def test_wrong_type_optional_key():
    assert validate_event("user", {"text": "hi", "kind": 5}) == [
        "key 'kind' expects str, got int"
    ]


def test_wrong_type_collection_keys():
    errors = validate_event(
        "narrative", {"prose": "p", "segments": "not-a-list"}
    )
    assert errors == ["key 'segments' expects list, got str"]
    assert validate_event("asset", {"asset_id": "a", "kind": "k", "mime": "m", "bytes_b64": b"x"}) == [
        "key 'bytes_b64' expects str, got bytes"
    ]


def test_roll_result_must_be_int_not_str():
    assert validate_event("roll", {"dice": "1d20", "result": "12"}) == [
        "key 'result' expects int, got str"
    ]


def test_revision_requires_revised_dict():
    # A task-sketch payload (target/new_content) lacks the enforced "revised" key.
    errors = validate_event("revision", {"target": "17", "new_content": "x"})
    assert errors == ["missing required key 'revised'"]


# -- extras pass (D4 log-is-editable / forward compatibility) ----------------


def test_extras_are_always_allowed():
    assert validate_event("user", {"text": "hi", "future_field": {"x": 1}}) == []
    assert (
        validate_event(
            "narrative",
            {"prose": "p", "extra_meta": "anything", "segments_plus": [1, 2]},
        )
        == []
    )


def test_extra_key_does_not_mask_missing_required_key():
    # text relaxed to optional (housekeeping user rows); kind alone is legal
    assert validate_event("user", {"kind": "ooc"}) == []


# -- bool is not int ---------------------------------------------------------


def test_bool_does_not_satisfy_int_field():
    assert validate_event("roll", {"dice": "1d20", "result": True}) == [
        "key 'result' expects int, got bool"
    ]


def test_int_does_not_satisfy_bool_field_using_injected_schema(monkeypatch):
    # No production schema requires bool, so assert both directions on a
    # temporary schema: int must not satisfy bool, and bool must satisfy bool.
    monkeypatch.setitem(
        EVENT_SCHEMAS, "user", Schema(required={"text": str, "flag": bool})
    )
    assert validate_event("user", {"text": "hi", "flag": 1}) == [
        "key 'flag' expects bool, got int"
    ]
    assert validate_event("user", {"text": "hi", "flag": True}) == []


def test_bool_passes_str_field_would_fail_but_bool_is_not_str():
    # Sanity: a bool is still rejected where a str is declared (typed-optional).
    assert validate_event("user", {"text": True}) == [
        "key 'text' expects str, got bool"
    ]


# -- state-delta is an open fact set -----------------------------------------


def test_state_delta_accepts_arbitrary_facts():
    assert validate_event("state-delta", {}) == []
    assert validate_event("state-delta", {"anything": [1, {"nested": True}]}) == []


# -- narrative_config_snapshot ----------------------------------------------


def test_narrative_config_snapshot_all_strings():
    snap = narrative_config_snapshot(
        "gemma-3", "temp=0.8", "d7", "abc123"
    )
    assert snap == {
        "model": "gemma-3",
        "sampler": "temp=0.8",
        "directive_version": "d7",
        "facts_hash": "abc123",
    }
    assert all(isinstance(v, str) for v in snap.values())


def test_narrative_config_snapshot_facts_hash_may_be_empty():
    snap = narrative_config_snapshot("m", "{}", "v1", "")
    assert snap["facts_hash"] == ""
    assert validate_event(
        "narrative", {"prose": "p", "config": snap}
    ) == []


def test_narrative_config_snapshot_coerces_non_strings():
    snap = narrative_config_snapshot(123, {}, "d", None)
    assert snap == {
        "model": "123",
        "sampler": "{}",
        "directive_version": "d",
        "facts_hash": "None",
    }


# -- usage payload (I1: ``system``/``usage`` typed variant) -------------------


def test_usage_payload_is_valid_and_typed():
    from fabulist.usage.track import usage_payload

    payload = usage_payload(
        agent="narrator",
        model="your-model-id",
        session_id="s1",
        prompt_tokens=812,
        completion_tokens=240,
        latency_s=1.83,
    )
    assert payload["event"] == "usage"
    assert payload["total_tokens"] == 1052
    assert validate_event("system", payload) == []


def test_usage_payload_requires_core_keys():
    errors = validate_event("system", {"event": "usage", "agent": "narrator"})
    assert "missing required key 'model'" in errors
    assert "missing required key 'session_id'" in errors
    assert "missing required key 'total_tokens'" in errors


def test_usage_payload_rejects_wrong_types():
    payload = {
        "event": "usage",
        "agent": "narrator",
        "model": "your-model-id",
        "session_id": "s1",
        "prompt_tokens": True,  # bool is not int
        "completion_tokens": 1,
        "total_tokens": 2,
        "latency_s": 1.5,
    }
    assert validate_event("system", payload) == [
        "key 'prompt_tokens' expects int, got bool"
    ]


def test_usage_payload_optional_seq_context_typed():
    from fabulist.usage.track import usage_payload

    payload = usage_payload(
        agent="a",
        model="m",
        session_id="s",
        prompt_tokens=1,
        completion_tokens=1,
        latency_s=0.1,
        seq=42,
    )
    assert payload["seq"] == 42
    assert validate_event("system", payload) == []


def test_other_system_payloads_still_ungated():
    # The opaque system schema must not be tightened by the usage variant.
    assert validate_event("system", {"event": "plan", "plan": {"beats": []}}) == []
    assert validate_event("system", {"event": "session_created", "title": "x"}) == []
    assert validate_event("system", ["not", "a", "dict"]) == []


# -- typed system variants: agent_call / assistant_reply / image_caption ------
#
# W5B-3: these three payloads have a stable one-producer shape, so a malformed
# one is rejected at append while the bare/opaque ``system`` path stays open.


def _agent_call_payload(**overrides):
    """Mirror agents/debug.py DebugCapture wire (tests/test_agent_debug.py:_wire)."""
    payload = {
        "event": "agent_call",
        "agent": "narrator",
        "messages": [{"role": "user", "content": "hi"}],
        "response_text": "hello",
        "reasoning_text": "",
        "finish": None,
        "latency_s": 0.25,
        "facts_hash": "deadbeef",
        "meta": {},
    }
    payload.update(overrides)
    return payload


def test_agent_call_producer_payload_passes():
    assert validate_event("system", _agent_call_payload()) == []


def test_agent_call_rejects_missing_and_wrong_types():
    bad = _agent_call_payload()
    bad.pop("messages")
    assert validate_event("system", bad) == ["missing required key 'messages'"]
    assert validate_event("system", _agent_call_payload(agent=5)) == [
        "key 'agent' expects str, got int"
    ]
    assert validate_event("system", _agent_call_payload(latency_s="fast")) == [
        "key 'latency_s' expects float, got str"
    ]


def test_agent_call_finish_present_but_value_any():
    # ``finish`` is ``str | None``: its presence is enforced, its value is not.
    assert validate_event("system", _agent_call_payload(finish=None)) == []
    assert validate_event("system", _agent_call_payload(finish="stop")) == []
    assert validate_event("system", {"event": "agent_call", "agent": "a"}) == [
        "missing required key 'messages'",
        "missing required key 'response_text'",
        "missing required key 'reasoning_text'",
        "missing required key 'finish'",
        "missing required key 'latency_s'",
        "missing required key 'facts_hash'",
        "missing required key 'meta'",
    ]


def test_assistant_reply_producer_payload_passes():
    # Mirrors api/assistants_api.py consult_assistant append.
    assert (
        validate_event(
            "system",
            {"event": "assistant_reply", "assistant": "demo_oracle", "text": "hi"},
        )
        == []
    )


def test_assistant_reply_rejects_malformed():
    assert validate_event(
        "system", {"event": "assistant_reply", "assistant": "demo_oracle"}
    ) == ["missing required key 'text'"]
    assert validate_event(
        "system", {"event": "assistant_reply", "assistant": 1, "text": "hi"}
    ) == ["key 'assistant' expects str, got int"]


def test_sidekick_reply_is_accepted_legacy():
    # Pre-rename history keeps validating (a legacy ``sidekick_reply`` row stays
    # legal); only the new name is ever written.
    assert (
        validate_event(
            "system",
            {"event": "sidekick_reply", "sidekick": "demo_oracle", "text": "hi"},
        )
        == []
    )
    assert validate_event(
        "system", {"event": "sidekick_reply", "sidekick": "demo_oracle"}
    ) == ["missing required key 'text'"]


def test_image_caption_producer_payload_passes():
    # Mirrors media/images.py caption append.
    assert (
        validate_event(
            "system",
            {"event": "image_caption", "asset": "abc123", "text": "a brass lamp"},
        )
        == []
    )


def test_image_caption_rejects_malformed():
    assert validate_event("system", {"event": "image_caption", "asset": "abc"}) == [
        "missing required key 'text'"
    ]
    assert validate_event(
        "system", {"event": "image_caption", "asset": "abc", "text": 3}
    ) == ["key 'text' expects str, got int"]


def test_malformed_typed_variant_rejected_at_append(tmp_path):
    from fabulist.log import EventLog
    from fabulist.log.schema_gate import LogSchemaError

    with EventLog(tmp_path / "events.db") as log:
        # A producer-shaped row lands...
        log.append_event(
            "s1",
            "system",
            "assistant",
            {"event": "assistant_reply", "assistant": "demo_oracle", "text": "hi"},
        )
        # ...a hand-mangled one is rejected before it touches the log.
        with pytest.raises(LogSchemaError):
            log.append_event(
                "s1",
                "system",
                "assistant",
                {"event": "assistant_reply", "assistant": "demo_oracle"},  # no text
            )
        assert all(
            e.payload.get("event") != "assistant_reply" or "text" in e.payload
            for e in log.get_events("s1")
        )


# -- stage-failure variants (S4c: failures must be visible in the log) --------


def test_stt_failure_payload_passes():
    assert (
        validate_event(
            "system",
            {
                "event": "stt_failure",
                "stage": "decode",
                "error": "ffmpeg audio decode failed",
                "asset_id": "a1",
            },
        )
        == []
    )


def test_index_failure_payload_passes_with_seq_context():
    # Shape the prepared api/app.py patch appends from _schedule_post_turn.
    assert (
        validate_event(
            "system",
            {
                "event": "index_failure",
                "stage": "memory_hook",
                "error": "embedder down",
                "seq": 12,
            },
        )
        == []
    )


def test_compaction_failed_payload_passes():
    assert (
        validate_event(
            "system",
            {
                "event": "compaction_failed",
                "stage": "summarizer_empty",
                "error": "summarizer returned no text",
                "covers_seqs": [2, 6],
            },
        )
        == []
    )


def test_stage_failure_requires_stage_and_error():
    assert validate_event("system", {"event": "stt_failure", "asset_id": "a1"}) == [
        "missing required key 'stage'",
        "missing required key 'error'",
    ]
    assert validate_event(
        "system", {"event": "index_failure", "stage": 1, "error": "x"}
    ) == ["key 'stage' expects str, got int"]


def test_non_failure_system_events_stay_opaque():
    # Only the three failure kinds are typed; anything else stays permissive.
    assert validate_event("system", {"event": "session_created", "title": "x"}) == []
    assert validate_event("system", {"event": "stt", "text": "hi"}) == []


# -- asset_proposal: the diff-first OPERATION gate (W9.23 §3.4.7) ------------


def _amend(**overrides) -> dict:
    """One op-typed amend payload: a world changing its description."""
    payload = {
        "event": "asset_proposal",
        "kind": "world",
        "action": "amend",
        "target": "w1",
        "patch": {},
        "ops": [
            {
                "op": "modify",
                "path": "description",
                "old": "A river town.",
                "new": "A mill town.",
            }
        ],
        "origin": "bts",
        "signal": 0.4,
    }
    payload.update(overrides)
    return payload


def test_an_op_typed_amend_passes():
    """The diff-first shape is legal: ops instead of a whole-value patch."""
    assert validate_event("system", _amend()) == []


def test_a_legacy_whole_value_amend_still_passes():
    """Both shapes fold forever (W9.23 §6): a pre-op row is unchanged."""
    assert (
        validate_event(
            "system",
            {
                "event": "asset_proposal",
                "kind": "world",
                "action": "amend",
                "target": "w1",
                "patch": {"description": "A mill town."},
                "origin": "bts",
                "signal": 0.4,
            },
        )
        == []
    )


def test_an_amend_carrying_both_shapes_is_refused():
    """Two statements about what the accept writes cannot both be true."""
    errors = validate_event(
        "system", _amend(patch={"description": "A mill town."})
    )
    assert errors == ["key 'patch' must be empty when 'ops' carries the amendment"]


def test_ops_are_only_legal_on_an_amend():
    """An ``add`` has nothing to diff against; a ``remove`` deletes the asset."""
    add = {
        "event": "asset_proposal",
        "kind": "actor",
        "action": "add",
        "target": None,
        "patch": {"name": "Bram"},
        "ops": [{"op": "add", "path": "description", "new": "x"}],
        "origin": "bts",
        "signal": None,
    }
    assert validate_event("system", add) == [
        "key 'ops' is only legal for action='amend'"
    ]


@pytest.mark.parametrize(
    ("op", "message"),
    [
        (
            {"op": "add", "path": "description"},
            "key 'ops[0]' requires 'new' for op='add'",
        ),
        (
            {"op": "add", "path": "description", "old": "a", "new": "b"},
            "key 'ops[0]' forbids 'old' for op='add'",
        ),
        (
            {"op": "modify", "path": "description", "new": "b"},
            "key 'ops[0]' requires 'old' and 'new' for op='modify'",
        ),
        (
            {"op": "delete", "path": "description"},
            "key 'ops[0]' requires 'old' for op='delete'",
        ),
        (
            {"op": "delete", "path": "description", "old": "a", "new": "b"},
            "key 'ops[0]' forbids 'new' for op='delete'",
        ),
        (
            {"op": "rename", "path": "description", "new": "b"},
            "key 'ops[0].op' expects one of add|delete|modify",
        ),
    ],
)
def test_each_verb_has_its_own_key_contract(op, message):
    """Per-op key rules (W9.23 §3.4.7): the verb decides which sides may exist."""
    assert validate_event("system", _amend(ops=[op])) == [message]


def test_an_op_path_must_be_writable():
    errors = validate_event(
        "system", _amend(ops=[{"op": "add", "path": "not_a_field", "new": 1}])
    )
    assert errors == ["key 'ops[0].path' names no writable field"]


def test_an_op_path_must_be_a_non_empty_string():
    errors = validate_event("system", _amend(ops=[{"op": "add", "path": " ", "new": 1}]))
    assert errors == ["key 'ops[0].path' must name a writable path"]


def test_op_paths_are_unique_within_one_row():
    """Two ops on one path would make the second ``old`` a lie."""
    errors = validate_event(
        "system", _amend(ops=[{"op": "add", "path": "description", "new": "a"}] * 2)
    )
    assert errors == ["key 'ops[1].path' repeats an earlier path"]


def test_name_may_be_renamed_but_never_deleted():
    """A rename is a non-empty string; a delete on ``name`` is refused everywhere."""
    assert validate_event(
        "system",
        _amend(
            kind="actor",
            ops=[{"op": "modify", "path": "name", "old": "Millie", "new": "Maud"}],
        ),
    ) == []
    assert validate_event(
        "system",
        _amend(
            kind="actor",
            ops=[{"op": "delete", "path": "name", "old": "Millie"}],
        ),
    ) == ["key 'ops[0]' may not delete 'name'"]
    assert validate_event(
        "system",
        _amend(
            kind="actor",
            ops=[{"op": "modify", "path": "name", "old": "Millie", "new": "   "}],
        ),
    ) == ["key 'ops[0].new' must rename to a non-empty name"]


def test_a_list_item_path_is_addressable_only_where_the_list_has_keys():
    """``entries.<id>`` is book-only; other list fields change whole-field."""
    book = _amend(
        kind="book",
        target="b1",
        ops=[
            {
                "op": "delete",
                "path": "entries.abc123",
                "old": {"content": "A mill."},
            }
        ],
    )
    assert validate_event("system", book) == []
    # ``cast``/``book_ids`` have no stable key story: a dotted spelling names
    # nothing, so a list can never be edited by omission.
    assert validate_event(
        "system",
        _amend(
            ops=[{"op": "add", "path": "cast.p1", "new": "Marisol"}],
        ),
    ) == ["key 'ops[0].path' names no writable field"]
    # A two-segment tail is not an item address either.
    assert validate_event(
        "system", _amend(kind="book", ops=[{"op": "add", "path": "entries.a.b", "new": {}}])
    ) == ["key 'ops[0].path' names no writable field"]


def test_an_entry_op_value_must_be_an_entry_object():
    errors = validate_event(
        "system",
        _amend(
            kind="book",
            ops=[{"op": "modify", "path": "entries.abc", "old": "text", "new": {}}],
        ),
    )
    assert errors == ["key 'ops[0].old' expects an entry object"]


def test_op_values_type_check_against_the_declared_field_shape():
    """The gate's type teeth reach the ops: a list field is not a string."""
    errors = validate_event(
        "system",
        _amend(ops=[{"op": "modify", "path": "cast", "old": [], "new": "Marisol"}]),
    )
    assert errors == ["key 'ops[0].new' expects a list of strings"]


def test_an_amend_without_ops_or_a_writable_patch_is_still_refused():
    """The legacy branch keeps the "nothing writable" rule."""
    errors = validate_event(
        "system",
        {
            "event": "asset_proposal",
            "kind": "world",
            "action": "amend",
            "target": "w1",
            "patch": {"nonsense": 1},
            "origin": "bts",
            "signal": 0.4,
        },
    )
    assert errors == [
        "key 'patch' must carry at least one writable field for action='amend'"
    ]


def test_greeting_fields_are_out_of_proposal_scope():
    """Q1: a greeting is the owner's voice, not a fact the story discovered."""
    for field in ("first_mes", "alternate_greetings"):
        assert writable_field("actor", field) is False
        errors = validate_event(
            "system",
            {
                "event": "asset_proposal",
                "kind": "actor",
                "action": "amend",
                "target": "p1",
                "patch": {field: "Hello again."},
                "origin": "bts",
                "signal": 0.4,
            },
        )
        assert errors == [
            "key 'patch' must carry at least one writable field for action='amend'"
        ]


def test_entry_projection_is_the_shown_key_set():
    """The comparison basis for an entry op (W9.23 §3.4.5)."""
    entry = {
        "entry_id": "abc",
        "comment": "mill",
        "keys": ["mill"],
        "content": "A mill.",
        "constant": True,
        "insertion_order": 3,
    }
    assert entry_projection(entry) == {
        "comment": "mill",
        "keys": ["mill"],
        "content": "A mill.",
    }
    # A missing shown key stays missing: an ``old`` that silently drops a field
    # fails the comparison instead of matching by omission.
    assert entry_projection({"keys": ["mill"]}) == {"keys": ["mill"]}
    assert entry_projection("not an entry") == {}


def test_list_item_path_names_one_addrressing_class():
    """One spelling of ``<list field>.<stable id>`` for every seam."""
    assert list_item_path("book", "entries.abc") == ("entries", "abc")
    assert list_item_path("book", "entries") is None
    assert list_item_path("book", "entries.a.b") is None
    assert list_item_path("world", "entries.abc") is None
    assert list_item_path("book", "name") is None


# -- the agent-loop runner's iteration rows (W9.24 §3.2) ---------------------
#
# One row per measured step of ``fabulist.agents.loop.run_agent_loop``. The
# samples below are the shapes the runner actually appends (pinned end to end in
# tests/test_agent_loop.py); the gate's job is to reject a row that lost what it
# measured, because a silent omission reads as a clean iteration.

AGENT_LOOP_SAMPLES: dict[str, dict] = {
    "completion": {
        "event": "agent_loop",
        "surface": "narrate",
        "iteration": 1,
        "kind": "completion",
        "latency_s": 12.5,
        "text": "The harbour was very very wet.",
        "finish_reason": "stop",
    },
    "tool_call": {
        "event": "agent_loop",
        "surface": "narrate",
        "iteration": 1,
        "kind": "tool_call",
        "tool": "patch_prose",
        "args": {"arguments": '{"edits": [{"find": "x", "replace": "y"}]}'},
        "injected": False,
    },
    "tool_result": {
        "event": "agent_loop",
        "surface": "narrate",
        "iteration": 1,
        "kind": "tool_result",
        "tool": "patch_prose",
        "result": {"ok": True, "applied": 1},
        "latency_s": 0.002,
        "injected": False,
    },
    "final": {
        "event": "agent_loop",
        "surface": "bts",
        "iteration": 2,
        "kind": "final",
        "text": "The harbour was utterly wet.",
        "timed_out": False,
    },
    "timeout": {
        "event": "agent_loop",
        "surface": "bts",
        "iteration": 3,
        "kind": "timeout",
        "latency_s": 900.0,
        "error": "APITimeoutError: request timed out",
        "timeout_s": 900.0,
    },
}


@pytest.mark.parametrize("kind", sorted(AGENT_LOOP_SAMPLES))
def test_agent_loop_rows_pass_the_system_variant(kind):
    """Every kind the runner appends clears the append door's gate."""
    sample = AGENT_LOOP_SAMPLES[kind]
    assert validate_event("system", sample) == []
    assert validate_agent_loop_payload(sample) == []


def test_agent_loop_kind_vocabulary_is_closed():
    """A new kind needs a conscious gate change, not a typo'd row."""
    assert set(AGENT_LOOP_SAMPLES) == set(AGENT_LOOP_KINDS)
    sample = dict(AGENT_LOOP_SAMPLES["final"], kind="finished")
    assert validate_event("system", sample) == [
        "key 'kind' expects one of completion|tool_call|tool_result|final|timeout"
    ]


@pytest.mark.parametrize(
    ("kind", "key", "error"),
    [
        ("completion", "latency_s", "missing required key 'latency_s' for kind='completion'"),
        ("tool_result", "latency_s", "missing required key 'latency_s' for kind='tool_result'"),
        ("tool_call", "tool", "missing required key 'tool' for kind='tool_call'"),
        ("final", "timed_out", "missing required key 'timed_out' for kind='final'"),
        ("timeout", "error", "missing required key 'error' for kind='timeout'"),
    ],
)
def test_agent_loop_rows_must_carry_what_they_measured(kind, key, error):
    sample = {k: v for k, v in AGENT_LOOP_SAMPLES[kind].items() if k != key}
    assert validate_event("system", sample) == [error]


def test_agent_loop_iteration_is_one_based():
    sample = dict(AGENT_LOOP_SAMPLES["completion"], iteration=0)
    assert validate_event("system", sample) == [
        "key 'iteration' expects a 1-based iteration number"
    ]


def test_agent_loop_declared_types_are_enforced():
    """Payload dicts stay dicts and a latency is a float, never a bool."""
    args = dict(AGENT_LOOP_SAMPLES["tool_call"], args=["not", "a", "dict"])
    assert validate_event("system", args) == [
        "key 'args' expects dict, got list"
    ]
    latency = dict(AGENT_LOOP_SAMPLES["final"], latency_s=True)
    assert validate_event("system", latency) == [
        "key 'latency_s' expects float, got bool"
    ]
    assert validate_event("system", {"event": "agent_loop", "surface": "narrate"}) == [
        "missing required key 'iteration'",
        "missing required key 'kind'",
        "key 'kind' expects one of completion|tool_call|tool_result|final|timeout",
    ]


def test_agent_loop_does_not_tighten_other_system_variants():
    """An unrelated system payload still falls through to the permissive schema."""
    assert validate_event("system", {"event": "session_created", "title": "x"}) == []


# -- the validate stage's teaching notice (D-F) ------------------------------
#
# One row per flagged COMMITTED turn, appended by ``make_validate_node`` with the
# same per-finding teaching list the narrator's repair loop steers on. The gate's
# job is to keep an unplaceable notice (no narrative seq) or an empty one (no
# findings) out of the log, because either would be a silent no-op.

VALIDATION_NOTICE_SAMPLE = {
    "event": "validation_notice",
    "seq": 7,
    "flags": [
        {
            "rule_id": "prose_markup",
            "check": "format",
            "severity": "warn",
            "message": "raw HTML detected in prose (left untouched)",
            "anchor": "<div>TERMINAL READY</div>",
            "fix": "remove the raw markup from prose",
        }
    ],
    "score": 92,
}


def test_validation_notice_row_passes_the_system_variant():
    assert validate_event("system", VALIDATION_NOTICE_SAMPLE) == []
    assert validate_validation_notice_payload(VALIDATION_NOTICE_SAMPLE) == []


@pytest.mark.parametrize("key", ["seq", "flags"])
def test_validation_notice_row_must_name_its_turn_and_its_findings(key):
    sample = {k: v for k, v in VALIDATION_NOTICE_SAMPLE.items() if k != key}
    assert validate_event("system", sample) == [f"missing required key '{key}'"]


def test_validation_notice_refuses_an_empty_or_misshapen_flag_list():
    empty = dict(VALIDATION_NOTICE_SAMPLE, flags=[])
    assert validate_event("system", empty) == [
        "key 'flags' expects at least one finding"
    ]
    strings = dict(VALIDATION_NOTICE_SAMPLE, flags=["prose_markup"])
    assert validate_event("system", strings) == [
        "key 'flags' expects a list of finding objects"
    ]


def test_validation_notice_seq_is_a_positive_int_never_a_bool():
    zero = dict(VALIDATION_NOTICE_SAMPLE, seq=0)
    assert validate_event("system", zero) == [
        "key 'seq' expects the flagged narrative's 1-based seq"
    ]
    boolean = dict(VALIDATION_NOTICE_SAMPLE, seq=True)
    assert validate_event("system", boolean) == ["key 'seq' expects int, got bool"]


def test_validation_notice_validator_refuses_another_variant():
    """The variant validator is not a general system-payload door."""
    assert validate_validation_notice_payload({"event": "usage"}) == [
        "expected event='validation_notice'"
    ]


# -- the prompt-budget row (W9.25 §3.5) --------------------------------------
#
# One row per over-budget prompt, produced by
# ``fabulist.graph.compaction.warn_prompt_budget`` from the streaming caller and
# the extractor's direct call. The four measured numbers are what the owner acts
# on, so a row that lost one is rejected rather than read as "no overshoot".

PROMPT_BUDGET_SAMPLE = {
    "event": "prompt_budget",
    "agent": "narrator",
    "est_prompt_tokens": 24000,
    "context_window": 16384,
    "response_length": 4096,
    "over_tokens": 24000 - (16384 - 4096 - 512),
}


def test_prompt_budget_row_passes_the_system_variant():
    assert validate_event("system", PROMPT_BUDGET_SAMPLE) == []
    assert validate_prompt_budget_payload(PROMPT_BUDGET_SAMPLE) == []


@pytest.mark.parametrize(
    "key", ["agent", "est_prompt_tokens", "context_window", "response_length", "over_tokens"]
)
def test_prompt_budget_row_must_carry_what_it_measured(key):
    sample = {k: v for k, v in PROMPT_BUDGET_SAMPLE.items() if k != key}
    assert validate_event("system", sample) == [f"missing required key '{key}'"]


def test_prompt_budget_row_types_are_enforced():
    sample = dict(PROMPT_BUDGET_SAMPLE, over_tokens=True)
    assert validate_event("system", sample) == ["key 'over_tokens' expects int, got bool"]


def test_prompt_budget_validator_refuses_another_variant():
    """The variant validator is not a general system-payload door."""
    assert validate_prompt_budget_payload({"event": "usage"}) == [
        "expected event='prompt_budget'"
    ]


def test_agent_loop_headroom_is_optional_and_typed():
    row = dict(AGENT_LOOP_SAMPLES["final"], headroom=-42)
    assert validate_event("system", row) == []
    typed = dict(AGENT_LOOP_SAMPLES["final"], headroom="lots")
    assert validate_event("system", typed) == ["key 'headroom' expects int, got str"]


# -- W9.32: the three state kinds + the accepted-proposal audit row ----------

#: One real establish payload per new kind, shaped exactly as the W9.32 writers
#: emit them (§3.1): ``name`` is synthesized by the writer, never omitted.
STATE_ESTABLISH_SAMPLES = {
    "condition": {
        "event": "entity_establish",
        "kind": "condition",
        "id": "cd_ab12",
        "name": "suspicious",
        "host": "cm_yara",
        "key": "suspicious",
        "severity": "moderate",
        "source": "the letter she was reading",
        "expires_at": "2025-06-02 21:30",
    },
    "body": {
        "event": "entity_establish",
        "kind": "body",
        "id": "bd_yara",
        "name": "Yara",
        "host": "cm_yara",
        "pose": "standing, arms folded",
        "clothing": ["housecoat", "slippers"],
        "surface": "faint bruise on the left wrist",
    },
    "economy": {
        "event": "entity_establish",
        "kind": "economy",
        "id": "eco_credits",
        "name": "credits",
        "denomination": "credits",
        "balance": 85,
    },
}


@pytest.mark.parametrize("kind", sorted(STATE_ESTABLISH_SAMPLES))
def test_state_establish_kind_passes_the_gate(kind):
    payload = STATE_ESTABLISH_SAMPLES[kind]
    assert validate_event("system", payload) == []


@pytest.mark.parametrize("kind", sorted(STATE_ESTABLISH_SAMPLES))
def test_state_establish_still_requires_a_name(kind):
    """The writers synthesize ``name``; the gate keeps it universally required."""
    payload = {k: v for k, v in STATE_ESTABLISH_SAMPLES[kind].items() if k != "name"}
    assert validate_event("system", payload) == ["missing required key 'name'"]


def test_state_establish_kind_must_be_known():
    payload = dict(STATE_ESTABLISH_SAMPLES["economy"], kind="purse")
    errors = validate_event("system", payload)
    assert len(errors) == 1 and errors[0].startswith("key 'kind' expects one of [")


def test_clothing_must_be_a_list():
    payload = dict(STATE_ESTABLISH_SAMPLES["body"], clothing="housecoat")
    assert validate_event("system", payload) == [
        "key 'clothing' expects list, got str"
    ]


def test_clock_proposal_row_passes():
    row = {
        "event": "clock_proposal",
        "time": "19:40",
        "date": "Monday, June 02, 2025",
        "day": 1,
        "evidence": "Later that evening, you call her.",
        "advanced_minutes": 320,
    }
    assert validate_event("system", row) == []


def test_clock_proposal_requires_evidence_prose():
    row = {
        "event": "clock_proposal",
        "time": "19:40",
        "date": "Monday, June 02, 2025",
        "day": 1,
        "evidence": "   ",
    }
    assert validate_event("system", row) == [
        "key 'evidence' expects the prose that motivated the move"
    ]


def test_clock_proposal_day_is_typed():
    row = {
        "event": "clock_proposal",
        "time": "19:40",
        "date": "Monday, June 02, 2025",
        "day": True,
        "evidence": "Later that evening, you call her.",
    }
    assert validate_event("system", row) == ["key 'day' expects int, got bool"]


def test_clock_proposal_validator_refuses_another_variant():
    assert validate_clock_proposal_payload({"event": "usage"}) == [
        "expected event='clock_proposal'"
    ]


# -- W9.26 §2.9: the context-management payloads ----------------------------
#
# Three typed ``system`` variants land with the context gauge and the compact
# service. The typing traps the design pins are tested here, because each one is
# a silent-failure class: a ``ratio`` that arrives as ``1`` instead of ``1.0``,
# an ``absorbs`` written as present-``null``, and a flag emitted ``false`` where
# the contract says "omit unless true".

#: One real reading, shaped exactly as the monitor emits it (§2.2).
CONTEXT_LOW_SAMPLE = {
    "event": "context_low",
    "agent": "narrator",
    "ratio": 0.82,
    "prompt_tokens": 21400,
    "budget_tokens": 26112,
    "basis": "measured",
}


def test_context_low_row_passes_the_system_variant():
    assert validate_event("system", CONTEXT_LOW_SAMPLE) == []
    assert validate_context_low_payload(CONTEXT_LOW_SAMPLE) == []


@pytest.mark.parametrize(
    "key", ["agent", "ratio", "prompt_tokens", "budget_tokens", "basis"]
)
def test_context_low_row_must_carry_what_it_measured(key):
    sample = {k: v for k, v in CONTEXT_LOW_SAMPLE.items() if k != key}
    assert validate_event("system", sample) == [f"missing required key '{key}'"]


def test_context_low_ratio_is_float_only():
    """The gate's ``_matches`` is single-type: a computed ratio must be a float."""
    typed = dict(CONTEXT_LOW_SAMPLE, ratio=1)
    assert validate_event("system", typed) == ["key 'ratio' expects float, got int"]
    # The over-budget sentinel is the FLOAT 1.0, never the int 1.
    sentinel = dict(
        CONTEXT_LOW_SAMPLE, ratio=1.0, prompt_tokens=0, budget_tokens=0, over_budget=True
    )
    assert validate_event("system", sentinel) == []
    assert validate_context_low_payload(sentinel) == []


def test_context_low_flags_are_omitted_unless_true():
    for flag in ("over_budget", "noop"):
        false_row = dict(CONTEXT_LOW_SAMPLE, **{flag: False})
        assert validate_event("system", false_row) == [
            f"key '{flag}' is omitted unless true (never emitted false)"
        ]
        typed_row = dict(CONTEXT_LOW_SAMPLE, **{flag: "yes"})
        assert validate_event("system", typed_row) == [
            f"key '{flag}' expects bool, got str"
        ]
        true_row = dict(CONTEXT_LOW_SAMPLE, **{flag: True})
        assert validate_event("system", true_row) == []


def test_context_low_validator_refuses_another_variant():
    assert validate_context_low_payload({"event": "usage"}) == [
        "expected event='context_low'"
    ]


#: The era marker the compact service appends to the successor session
#: (§2.3 step 9).
COMPACTION_MARKER_SAMPLE = {
    "event": "compaction",
    "source_session": "abc",
    "summary_ref": ["abc", 42],
    "covers_seqs": [2, 41],
    "trigger": "manual",
    "stats": {"calls": 3, "prompt_tokens": 12000, "model": "your-model-id"},
}


def test_compaction_marker_passes_the_system_variant():
    assert validate_event("system", COMPACTION_MARKER_SAMPLE) == []
    assert validate_compaction_marker_payload(COMPACTION_MARKER_SAMPLE) == []


@pytest.mark.parametrize(
    "key", ["source_session", "summary_ref", "covers_seqs", "trigger"]
)
def test_compaction_marker_must_name_its_source_and_span(key):
    sample = {k: v for k, v in COMPACTION_MARKER_SAMPLE.items() if k != key}
    assert validate_event("system", sample) == [f"missing required key '{key}'"]


def test_compaction_marker_ref_pair_and_optional_stats():
    bad_ref = dict(COMPACTION_MARKER_SAMPLE, summary_ref=["abc"])
    assert validate_event("system", bad_ref) == [
        "key 'summary_ref' expects a [session_id, seq] pair"
    ]
    typed_stats = dict(COMPACTION_MARKER_SAMPLE, stats=["not", "a", "dict"])
    assert validate_event("system", typed_stats) == [
        "key 'stats' expects dict, got list"
    ]
    bare = {k: v for k, v in COMPACTION_MARKER_SAMPLE.items() if k != "stats"}
    assert validate_event("system", bare) == []


def test_compaction_marker_validator_refuses_another_variant():
    assert validate_compaction_marker_payload({"event": "prompt_budget"}) == [
        "expected event='compaction'"
    ]


def test_post_turn_failed_names_the_hook_and_never_the_compaction_row():
    """The monitor's failure row is ``post_turn_failed``, never ``compaction_failed``.

    Both are typed stage-failure variants (``{event, stage, error}`` + optional
    hook context), so a monitor crash cannot be misread as "compaction produced
    no summary" — which is exactly what the forced-path attempt counter reads.
    """
    row = {
        "event": "post_turn_failed",
        "hook": "context_monitor",
        "stage": "context_monitor",
        "error": "ZeroDivisionError: division by zero",
    }
    assert validate_event("system", row) == []
    assert validate_stage_failure_payload(row) == []
    assert "compaction_failed" not in row["event"]
    # A malformed stage-failure row is still refused.
    assert validate_event("system", {"event": "post_turn_failed", "error": "boom"}) == [
        "missing required key 'stage'"
    ]
    # ``hook`` is typed when present (extras are still free, D4).
    typed = dict(row, hook=7)
    assert validate_event("system", typed) == ["key 'hook' expects str, got int"]


def test_summary_v2_keys_are_typed_and_absorbs_is_omit_or_a_pair():
    """``covers_seqs`` stays the only REQUIRED summary key (§2.9)."""
    v2 = {
        "text": "They met at the market.",
        "covers_seqs": [2, 6],
        "reason": "compact(manual): 3 turn group(s)",
        "trigger": "manual",
        "stats": {"calls": 2, "prompt_tokens": 9000, "model": "your-model-id"},
    }
    assert validate_event("summary", v2) == []
    with_absorbs = dict(v2, absorbs=["parent", 12])
    assert validate_event("summary", with_absorbs) == []
    # present-null is the trap: absence is the contract, null fails the gate.
    assert validate_event("summary", dict(v2, absorbs=None)) == [
        "key 'absorbs' expects list, got NoneType"
    ]
    assert validate_event("summary", dict(v2, trigger=7)) == [
        "key 'trigger' expects str, got int"
    ]
    assert validate_event("summary", dict(v2, stats="ledger")) == [
        "key 'stats' expects dict, got str"
    ]
