"""Tests for the post-turn state extractor (W1.3).

Reasoning tags below are built from ``chr(60)`` because the literal control-token
sequences mangle when passed through tooling; the code under test builds them the
same way.
"""

from __future__ import annotations

import json
import re
import asyncio
import threading
import time
from pathlib import Path

import httpx2
import pytest

from fabulist.connections import get_client, load_registry
from fabulist.log import EventLog
from fabulist.theme.tokens import CURATED_PALETTE
from fabulist.state import (
    ACTION_CLASSES,
    DELTA_KEYS,
    EXTRACTOR_SYSTEM_PROMPT,
    EXTRACTOR_TOOL_NAME,
    AsyncExtractor,
    DeltaParseError,
    ExtractionError,
    ExtractorResult,
    FallbackExtractor,
    InvalidActionClass,
    LLMExtractor,
    RuleBasedExtractor,
    build_extractor_request_profile,
    extractor_tool,
    parse_action_class,
    parse_delta,
    parse_extractor_structured,
    strip_reasoning,
)

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "connections.example.json"

LT = chr(60)
GT = chr(62)
THINK_OPEN = LT + "think" + GT
THINK_CLOSE = LT + "/think" + GT
REASONING_OPEN = LT + "reasoning" + GT
REASONING_CLOSE = LT + "/reasoning" + GT


# --- parse_delta: happy paths ----------------------------------------------


def test_parse_delta_bare_line():
    assert parse_delta("Δ[time: 14:35 | loc: night market]") == {
        "time": "14:35",
        "loc": "night market",
    }


def test_parse_delta_embedded_in_prose():
    text = "Here is the change.\nΔ[weather: light rain]\nHope that helps."
    assert parse_delta(text) == {"weather": "light rain"}


def test_parse_delta_inside_code_fence():
    text = "```yaml\nΔ[tone: warm | loc: night market]\n```"
    assert parse_delta(text) == {"tone": "warm", "loc": "night market"}


def test_parse_delta_normalizes_arrow_spacing():
    assert parse_delta("Δ[tone: tense → warm]") == {"tone": "tense→warm"}
    assert parse_delta("Δ[tone: tense→warm]") == {"tone": "tense→warm"}


def test_parse_delta_empty_is_empty_dict():
    assert parse_delta("Δ[]") == {}


def test_parse_delta_skips_chunks_without_colon():
    assert parse_delta("Δ[time: 14:35 | stray | loc: market]") == {
        "time": "14:35",
        "loc": "market",
    }


def test_parse_delta_absent_raises():
    with pytest.raises(DeltaParseError):
        parse_delta("no delta line here at all")


def test_parse_delta_error_is_value_error():
    with pytest.raises(ValueError):
        parse_delta("plain prose")


# --- strip_reasoning --------------------------------------------------------


def test_strip_reasoning_removes_think_tags():
    text = "A  " + THINK_OPEN + "hidden" + THINK_CLOSE + " B"
    assert strip_reasoning(text) == "A   B"


def test_strip_reasoning_removes_think_fence():
    text = "keep\n```think\nsecret reasoning\n```\ntail"
    out = strip_reasoning(text)
    assert "secret reasoning" not in out
    assert "keep" in out and "tail" in out


def test_strip_reasoning_removes_reasoning_block():
    text = REASONING_OPEN + "x" + REASONING_CLOSE + "done"
    assert strip_reasoning(text).strip() == "done"


def test_parse_delta_ignores_delta_inside_think_block():
    text = THINK_OPEN + "Δ[time: 00:00]" + THINK_CLOSE + "Δ[time: 14:35]"
    assert parse_delta(text) == {"time": "14:35"}


# --- parse_action_class -----------------------------------------------------


def test_parse_action_class_valid():
    assert parse_action_class("Δ[]\naction: travel") == "travel"


@pytest.mark.parametrize("action_class", sorted(ACTION_CLASSES))
def test_parse_action_class_accepts_allowed_set(action_class):
    assert parse_action_class(f"action: {action_class}") == action_class


def test_parse_action_class_invalid_raises():
    with pytest.raises(InvalidActionClass):
        parse_action_class("Δ[]\naction: frobnicate")


def test_parse_action_class_missing_raises():
    with pytest.raises(InvalidActionClass):
        parse_action_class("Δ[]")


# --- ExtractorResult --------------------------------------------------------


def test_extractor_result_carries_confidence():
    result = ExtractorResult(deltas={"tone": "warm"}, action_class="dialogue", confidence=0.7)
    assert result.deltas == {"tone": "warm"}
    assert result.action_class == "dialogue"
    assert result.confidence == 0.7


# --- LLMExtractor: real registry + mock transport --------------------------


def _chat_response(content: str) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "cmpl-1",
            "object": "chat.completion",
            "created": 0,
            "model": "your-model-id",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


def _scripted_extractor(contents: list[str]):
    """Return (extractor, captured_bodies) using a scripted mock transport.

    Each call returns the next content; the last content repeats if called again.
    """
    captured: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(json.loads(request.content.decode()))
        content = contents[min(len(captured) - 1, len(contents) - 1)]
        return _chat_response(content)

    registry = load_registry(EXAMPLE_CONFIG)
    http_client = httpx2.Client(
        transport=httpx2.MockTransport(handler),
        base_url="http://localhost:4444/v1",
    )
    return LLMExtractor(registry=registry, http_client=http_client), captured, registry


def test_llm_extractor_parses_happy_path():
    extractor, captured, _ = _scripted_extractor(
        ["Δ[time: 14:35 | tone: tense→warm]\naction: dialogue"]
    )

    result = extractor("I greet the merchant", "The night market hums.")

    assert result == ExtractorResult(
        deltas={"time": "14:35", "tone": "tense→warm"},
        action_class="dialogue",
        confidence=1.0,
    )
    assert len(captured) == 1


def test_llm_extractor_captures_genre_from_a_reply():
    # D44: genre rides the extractor exactly like tone; a scripted model reply
    # naming it lands as a delta (the sidebar's facts['genre'] row then lights up).
    extractor, captured, _ = _scripted_extractor(
        ["Δ[genre: cyberpunk noir]\naction: other"]
    )

    result = extractor("I step into the alley", "Neon bleeds into the rain.")

    assert result.deltas == {"genre": "cyberpunk noir"}
    assert result.confidence == 1.0
    assert len(captured) == 1


def test_llm_extractor_builds_request_through_registry():
    extractor, captured, _ = _scripted_extractor(["Δ[]\naction: other"])

    extractor("I wait", "Silence.")

    body = captured[0]
    assert body["model"] == "your-model-id"  # extractor -> the example's local entry
    assert body["temperature"] == 0.1  # D2: extraction is low-variance, always (eval: 0.85 spread 30-40%)
    assert body["messages"][0] == {"role": "system", "content": EXTRACTOR_SYSTEM_PROMPT}
    user_block = body["messages"][1]["content"]
    assert "I wait" in user_block
    assert "Silence." in user_block


def test_llm_extractor_empty_delta_round_trips():
    extractor, _, _ = _scripted_extractor(["Δ[]\naction: rest"])

    result = extractor("I sleep", "The fire dies.")

    assert result.deltas == {}
    assert result.action_class == "rest"


def test_llm_extractor_repairs_malformed_output_then_succeeds():
    extractor, captured, _ = _scripted_extractor(
        [
            "I could not find any delta.",  # first: no Δ line
            "Δ[loc: night market]\naction: travel",  # repair: good
        ]
    )

    result = extractor("I go to the night market", "I arrive.")

    assert result.deltas == {"loc": "night market"}
    assert result.action_class == "travel"
    assert result.confidence == 0.5  # repaired output is less trusted
    assert len(captured) == 2
    # the repair turn appends an assistant reply and a corrective user message
    repaired_messages = captured[1]["messages"]
    assert repaired_messages[-1]["role"] == "user"
    assert "Δ[" in repaired_messages[-1]["content"]


def test_llm_extractor_repairs_invalid_action_class():
    extractor, captured, _ = _scripted_extractor(
        [
            "Δ[]\naction: frobnicate",
            "Δ[]\naction: task",
        ]
    )

    result = extractor("I fiddle", "Nothing happens.")

    assert result.action_class == "task"
    assert len(captured) == 2


def test_llm_extractor_second_failure_raises_with_raw_output():
    extractor, captured, _ = _scripted_extractor(
        ["garbage one", "still garbage two"]
    )

    with pytest.raises(ExtractionError) as excinfo:
        extractor("hello", "world")

    assert excinfo.value.raw == "still garbage two"
    assert len(captured) == 2


def test_llm_extractor_uses_injected_client_directly():
    handler_calls: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        handler_calls.append(json.loads(request.content.decode()))
        return _chat_response("Δ[weather: light rain]\naction: travel")

    registry = load_registry(EXAMPLE_CONFIG)
    client = get_client(
        "extractor",
        registry=registry,
        http_client=httpx2.Client(
            transport=httpx2.MockTransport(handler),
            base_url="http://localhost:4444/v1",
        ),
    )
    extractor = LLMExtractor(client=client, registry=registry)

    result = extractor("I walk into the rain", "Rain soaks the street.")

    assert result.deltas == {"weather": "light rain"}
    assert len(handler_calls) == 1


# --- RuleBasedExtractor -----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I sleep until dawn.", "rest"),
        ("I go to the docks.", "travel"),
        ("I ask the merchant about the lantern.", "dialogue"),
        ("I look around the stall.", "inspection"),
        ("I pick up the lantern.", "task"),
        ("Hello there.", "other"),
    ],
)
def test_rule_based_action_class(text, expected):
    result = RuleBasedExtractor()(text, "")
    assert result.action_class == expected


def test_rule_based_take_adds_item():
    result = RuleBasedExtractor()("I take the lantern.", "")
    assert result.deltas == {"inv": "+lantern"}
    assert result.confidence == 1.0


def test_rule_based_drop_removes_item():
    result = RuleBasedExtractor()("I drop the key.", "")
    assert result.deltas == {"inv": "-key"}


def test_rule_based_travel_sets_loc():
    result = RuleBasedExtractor()("I go to the night market.", "")
    assert result.deltas == {"loc": "night market"}
    assert result.action_class == "travel"


def test_rule_based_conservative_without_explicit_pattern():
    result = RuleBasedExtractor()("I nod slowly.", "")
    assert result.deltas == {}
    assert result.confidence == 0.5


def test_rule_based_reads_narrative_for_patterns():
    result = RuleBasedExtractor()("", "She grabs the lantern and leaves.")
    assert result.deltas == {"inv": "+lantern"}


# --- Conventions v2: atomic values + taught tone register -------------------
#
# REGRESSION CAUSE (recorded): the pre-v2 prompt taught ``tone: tense→warm``
# arrow transitions. ``get_projection(sid)["state"]`` folds state-delta payloads
# latest-wins per key WITHOUT interpreting arrows, so arrows accumulated in every
# folded value (``tense→warm→playful``) and ``style_tokens()`` — which
# case-insensitively exact-matches ``tone`` against CURATED_PALETTE — could never
# theme an arrowed tone (unknown -> neutral forever). Conventions v2 removes the
# arrow rule and teaches the palette-backed register instead.

#: Captures the register run inside the prompt's tone line, up to its parenthetical.
_TONE_REGISTER_RE = re.compile(
    r"tone is ONE word from this register when it fits: (.+?) "
    r"\(other single words allowed"
)


def _taught_tone_register() -> str:
    """Return the tone register the prompt actually teaches (fail if absent)."""
    match = _TONE_REGISTER_RE.search(EXTRACTOR_SYSTEM_PROMPT)
    assert match is not None, "extractor prompt no longer teaches a tone register"
    return match.group(1)


def test_prompt_teaches_atomic_values_and_register_line():
    # (a) both the no-arrow prohibition and the register line are present.
    assert "ATOMIC CURRENT-VALUE SNAPSHOTS" in EXTRACTOR_SYSTEM_PROMPT
    assert (
        "never put arrows/transition syntax (→, ->) inside values"
        in EXTRACTOR_SYSTEM_PROMPT
    )
    assert _taught_tone_register(), "register line is empty"


def test_prompt_no_longer_teaches_arrow_transitions():
    # the exact old teaching line must be gone (it caused folded-state pollution).
    assert "a transition is an arrow" not in EXTRACTOR_SYSTEM_PROMPT


def test_taught_register_matches_curated_palette():
    # (b) the prompt register == the theme palette keys -> vocabulary cannot drift.
    assert _taught_tone_register() == " | ".join(sorted(CURATED_PALETTE))
    assert set(_taught_tone_register().split(" | ")) == set(CURATED_PALETTE)


def test_parse_delta_clean_tone_value():
    # (c) a clean single-word tone still parses to itself, unchanged.
    assert parse_delta("Δ[tone: warm]") == {"tone": "warm"}


def test_parse_delta_arrowed_tone_is_accepted_legacy():
    # (d) RECORDED-BUG GUARD -- accepted legacy behavior, cite the current code:
    # parse_delta only normalizes whitespace around '→' (``_ARROW_RE``); it does
    # NOT strip or interpret the arrow, so the value passes through verbatim as
    # 'tense→warm'. Old logs may carry arrows, so parsing must not crash. This is
    # today's behavior, deliberately preserved (Conventions v2 is prompt-only).
    assert parse_delta("Δ[tone: tense→warm]") == {"tone": "tense→warm"}
    assert parse_delta("Δ[tone: tense → warm]") == {"tone": "tense→warm"}


# --- W5A-1: delta vocabulary + value gate (S4b) -----------------------------
#
# READ before gating: state-delta payloads fold into one projection state mapping
# (``EventLog.get_projection``: ``state.update(payload)``, latest wins per key);
# ``prompts/variables.py`` reads exactly time/loc/weather/tone/inv out of it, the
# clock round-trips the same keys, and ``theme.tokens.style_tokens`` themes
# ``tone``. The fold is deliberately open (human state edits also append
# state-delta rows), so the vocabulary gate lives at the extractor boundary.


def test_delta_keys_is_the_eight_key_fold():
    # date/day joined the fold with the calendar clock (mirror of `time`:
    # accepted in the vocabulary, clock stays the authoritative stamper); genre
    # joined with D44 (BTS owns no genre category, so the extractor is its source).
    assert DELTA_KEYS == (
        "time",
        "loc",
        "weather",
        "tone",
        "inv",
        "date",
        "day",
        "genre",
    )


def test_parse_delta_captures_genre():
    # D44: genre is freeform like loc/weather — gated into the vocabulary, not
    # structurally constrained (any register word or phrase is legal).
    assert parse_delta("Δ[genre: urban fantasy]") == {"genre": "urban fantasy"}


def test_prompt_teaches_genre_key():
    # The vocabulary line is derived from DELTA_KEYS; the genre hint is explicit.
    assert "genre" in DELTA_KEYS
    assert "genre is one short descriptor" in EXTRACTOR_SYSTEM_PROMPT


def test_validate_date_and_day_values():
    assert parse_delta("Δ[date: Monday, June 02, 2025 | day: 1]") == {
        "date": "Monday, June 02, 2025",
        "day": "1",
    }
    with pytest.raises(DeltaParseError):
        parse_delta("Δ[date: June 2]")  # not the full DATE_FORMAT rendering
    with pytest.raises(DeltaParseError):
        parse_delta("Δ[day: 0]")  # day is 1-based
    with pytest.raises(DeltaParseError):
        parse_delta("Δ[day: third]")


def test_prompt_teaches_exactly_the_fold_vocabulary():
    # The prompt line is derived from DELTA_KEYS, so prompt and gate cannot drift.
    assert (
        f"keys MUST come from this vocabulary: {', '.join(DELTA_KEYS)}"
        in EXTRACTOR_SYSTEM_PROMPT
    )


def test_parse_delta_all_five_legal_keys_unchanged():
    assert parse_delta(
        "Δ[time: 14:35 | loc: night market | weather: light rain | "
        "tone: warm | inv: +lantern]"
    ) == {
        "time": "14:35",
        "loc": "night market",
        "weather": "light rain",
        "tone": "warm",
        "inv": "+lantern",
    }


def test_parse_delta_rejects_hallucinated_key():
    # Audit dead-end #6: a hallucinated 'location' used to persist into the fold.
    with pytest.raises(DeltaParseError) as excinfo:
        parse_delta("Δ[location: whereabouts]")
    assert "location" in str(excinfo.value)


def test_parse_delta_rejects_hallucinated_key_beside_legal_keys():
    with pytest.raises(DeltaParseError):
        parse_delta("Δ[time: 14:35 | whereabouts: docks]")


def test_parse_delta_rejects_empty_value():
    with pytest.raises(DeltaParseError):
        parse_delta("Δ[loc: ]")


@pytest.mark.parametrize("bad", ["afternoon", "afternoon→17:20", "25:00", "12:60", "noon"])
def test_parse_delta_rejects_non_clock_time(bad):
    with pytest.raises(DeltaParseError):
        parse_delta(f"Δ[time: {bad}]")


def test_parse_delta_accepts_clock_time():
    assert parse_delta("Δ[time: 06:40]") == {"time": "06:40"}
    assert parse_delta("Δ[time: 0:05]") == {"time": "0:05"}


@pytest.mark.parametrize("bad", ["torch", "lantern torch", "+lantern torch"])
def test_parse_delta_rejects_unsigned_inventory(bad):
    with pytest.raises(DeltaParseError):
        parse_delta(f"Δ[inv: {bad}]")


def test_parse_delta_accepts_signed_inventory_merge():
    assert parse_delta("Δ[inv: +lantern | inv: -torch]") == {"inv": "+lantern -torch"}


def test_parse_delta_does_not_gate_multiword_loc_and_weather():
    # 'night market' / 'light rain' are the canonical multi-word values; downstream
    # carries them, so the gate must not reject them.
    assert parse_delta("Δ[loc: night market | weather: light rain]") == {
        "loc": "night market",
        "weather": "light rain",
    }


# --- W5A-1: the gate routes through the existing repair-retry path ----------


def test_llm_extractor_repairs_hallucinated_key_then_succeeds():
    extractor, captured, _ = _scripted_extractor(
        [
            "Δ[location: whereabouts]\naction: travel",  # hallucinated key
            "Δ[loc: whereabouts]\naction: travel",  # repair: legal key (freeform value)
        ]
    )
    result = extractor("I look around", "The street is empty.")
    assert result.deltas == {"loc": "whereabouts"}
    assert result.confidence == 0.5
    assert len(captured) == 2


def test_llm_extractor_hallucinated_key_final_failure_raises():
    extractor, captured, _ = _scripted_extractor(
        [
            "Δ[location: whereabouts]\naction: travel",
            "Δ[location: whereabouts]\naction: travel",
        ]
    )
    with pytest.raises(ExtractionError):
        extractor("I look around", "The street is empty.")
    assert len(captured) == 2


# --- W5A-2: async extractor (S5) --------------------------------------------


def _result_extractor(user_msg: str, narrative: str) -> ExtractorResult:
    return ExtractorResult(deltas={"loc": "docks"}, action_class="travel")


def test_async_extractor_awaits_result():
    result = asyncio.run(AsyncExtractor(_result_extractor)("I go", "I arrive."))
    assert result.deltas == {"loc": "docks"}
    assert result.action_class == "travel"


def test_async_extractor_concurrent_calls_interleave():
    # If asyncio.to_thread serialized the calls, the barrier would time out
    # (BrokenBarrierError): call #1 would never meet call #2 in flight. Passing
    # proves the event loop is free while each sync extraction runs off-thread.
    barrier = threading.Barrier(2, timeout=5)

    class _Blocking:
        def __call__(self, user_msg, narrative):
            barrier.wait()
            return ExtractorResult(deltas={}, action_class="other")

    async def main():
        extractor = AsyncExtractor(_Blocking())
        return await asyncio.gather(extractor("a", "b"), extractor("c", "d"))

    results = asyncio.run(main())
    assert [r.action_class for r in results] == ["other", "other"]


# --- W5A-3: RuleBasedExtractor wired as the extraction_failed fallback ------


def test_rule_based_deltas_stay_inside_the_fold_vocabulary():
    result = RuleBasedExtractor()("I take the lantern and go to the docks.", "")
    assert set(result.deltas) <= set(DELTA_KEYS)


def test_fallback_extractor_uses_default_rule_based_fallback():
    def _boom(user_msg, narrative):
        raise ExtractionError("no delta", raw="garbage")

    result = FallbackExtractor(_boom)("I take the lantern.", "")
    assert result.deltas == {"inv": "+lantern"}
    assert result.action_class == "task"


def test_fallback_extractor_returns_primary_result_when_it_succeeds():
    primary = ExtractorResult(deltas={"tone": "warm"}, action_class="dialogue")
    assert FallbackExtractor(lambda u, n: primary)("hi", "warm light") is primary


def test_fallback_extractor_notes_source_in_the_log(tmp_path):
    from fabulist.agents.debug import CURRENT_SESSION

    log = EventLog(tmp_path / "log.db")

    def _boom(user_msg, narrative):
        raise ExtractionError("unparseable after repair", raw="garbage")

    token = CURRENT_SESSION.set("s-fallback")
    try:
        FallbackExtractor(_boom, log=log)("I take the lantern.", "")
        events = [e for e in log.get_events("s-fallback") if e.type == "system"]
    finally:
        CURRENT_SESSION.reset(token)

    assert len(events) == 1
    assert events[0].payload["event"] == "extraction_failed"
    assert events[0].payload["fallback"] == "rules"
    assert events[0].payload["deltas"] == {"inv": "+lantern"}


def test_fallback_extractor_emits_no_event_without_log():
    def _boom(user_msg, narrative):
        raise ExtractionError("x", raw="")

    assert FallbackExtractor(_boom)("I take the lantern.", "").deltas == {"inv": "+lantern"}


def test_llm_extractor_acall_parses_happy_path():
    extractor, captured, _ = _scripted_extractor(
        ["Δ[time: 14:35]\naction: dialogue"]
    )
    result = asyncio.run(extractor.acall("I wait", "The clock ticks."))
    assert result == ExtractorResult(
        deltas={"time": "14:35"}, action_class="dialogue", confidence=1.0
    )
    assert len(captured) == 1


def test_llm_extractor_acall_repairs_then_succeeds():
    extractor, captured, _ = _scripted_extractor(
        ["no delta here", "Δ[loc: docks]\naction: travel"]
    )
    result = asyncio.run(extractor.acall("I go", "I arrive."))
    assert result.deltas == {"loc": "docks"}
    assert result.confidence == 0.5
    assert len(captured) == 2


def test_fallback_extractor_acall_prefers_primary_acall():
    class _Primary:
        async def acall(self, user_msg, narrative):
            return ExtractorResult(deltas={"loc": "docks"}, action_class="travel")

    assert asyncio.run(FallbackExtractor(_Primary()).acall("a", "b")).deltas == {
        "loc": "docks"
    }


def test_fallback_extractor_acall_falls_back_on_primary_acall_error():
    class _Primary:
        async def acall(self, user_msg, narrative):
            raise ExtractionError("unparseable after repair", raw="garbage")

    assert asyncio.run(FallbackExtractor(_Primary()).acall("I take the lantern.", "")).deltas == {
        "inv": "+lantern"
    }


def test_async_fallback_composition_emits_event_on_loop(tmp_path):
    # End-to-end production shape: AsyncExtractor(FallbackExtractor(primary)).
    # The async fallback must append its log event on the loop thread — the
    # EventLog sqlite connection is bound to it (check_same_thread default), so a
    # naive whole-extractor-to_thread wrapper would silently lose this event.
    from fabulist.agents.debug import CURRENT_SESSION

    log = EventLog(tmp_path / "log.db")

    class _Primary:
        async def acall(self, user_msg, narrative):
            raise ExtractionError("unparseable after repair", raw="garbage")

    token = CURRENT_SESSION.set("s-async")
    try:
        extractor = AsyncExtractor(FallbackExtractor(_Primary(), log=log))
        result = asyncio.run(extractor("I take the lantern.", ""))
        events = [e for e in log.get_events("s-async") if e.type == "system"]
    finally:
        CURRENT_SESSION.reset(token)

    assert result.deltas == {"inv": "+lantern"}
    assert [e.payload["event"] for e in events] == ["extraction_failed"]
    assert events[0].payload["fallback"] == "rules"


def test_async_fallback_composition_with_real_llm_primary(tmp_path):
    # The exact production composition (see docs/W5A-extractor-wiring.patch):
    # AsyncExtractor(FallbackExtractor(LLMExtractor, RuleBasedExtractor, log=log)).
    # Two garbage replies exhaust the repair retry -> ExtractionError -> the
    # deterministic rule pass carries the inventory, and the log gets the
    # extraction_failed event with its fallback source.
    from fabulist.agents.debug import CURRENT_SESSION

    primary, captured, _ = _scripted_extractor(["garbage", "still garbage"])
    log = EventLog(tmp_path / "log.db")

    token = CURRENT_SESSION.set("s-real")
    try:
        extractor = AsyncExtractor(FallbackExtractor(primary, log=log))
        result = asyncio.run(extractor("I take the lantern.", "She pockets it."))
    finally:
        CURRENT_SESSION.reset(token)

    assert len(captured) == 2  # LLM tried once + one repair, both unparseable
    assert result.deltas == {"inv": "+lantern"}
    assert result.action_class == "task"
    events = [e for e in log.get_events("s-real") if e.type == "system"]
    assert [e.payload["event"] for e in events] == ["extraction_failed"]
    assert events[0].payload["fallback"] == "rules"


# --- W8.x: structured (Tier A) contract -------------------------------------
#
# The structured transport mirrors the BTS lane: a tool declaration carrying the
# fields the extractor emits today, a capability-resolved request profile, and a
# JSON-schema-authority parser that reuses the notation path's own gates
# (DELTA_KEYS + _validate_value). The persisted state-delta payload must stay
# byte-compatible, and an unusable payload must ride the EXISTING
# ExtractionError -> FallbackExtractor path (never a new error class).


def _tool_call_response(arguments: str) -> httpx2.Response:
    return httpx2.Response(
        200,
        json={
            "id": "cmpl-1",
            "object": "chat.completion",
            "created": 0,
            "model": "your-model-id",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": EXTRACTOR_TOOL_NAME,
                                    "arguments": arguments,
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )


def _profile_extractor(responses: list[httpx2.Response]):
    """LLMExtractor wired with a forced-tool_choice Tier A profile + mock transport."""
    captured: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured.append(json.loads(request.content.decode()))
        return responses[min(len(captured) - 1, len(responses) - 1)]

    registry = load_registry(EXAMPLE_CONFIG)
    http_client = httpx2.Client(
        transport=httpx2.MockTransport(handler),
        base_url="http://localhost:4444/v1",
    )
    extractor = LLMExtractor(
        registry=registry,
        http_client=http_client,
        profile_fn=lambda: build_extractor_request_profile(
            {"function_calling": True, "tool_choice_forced": True}
        ),
    )
    return extractor, captured


@pytest.mark.parametrize(
    "payload",
    [
        {"deltas": {}, "action_class": "rest"},
        {"deltas": {"time": "14:35", "loc": "night market"}, "action_class": "travel"},
        {"deltas": {"tone": "warm"}, "action_class": "dialogue"},
        {"deltas": {"inv": "+lantern -torch"}, "action_class": "task"},
        {"deltas": {"genre": "cyberpunk noir"}, "action_class": "other"},
    ],
)
def test_parse_extractor_structured_matches_parse_delta(payload):
    # Parity: the JSON path and the notation path must produce the same
    # ExtractorResult for the same content, so the fold stays byte-compatible.
    notation = "Δ[" + " | ".join(f"{k}: {v}" for k, v in payload["deltas"].items()) + "]"
    notation += f"\naction: {payload['action_class']}"
    assert parse_extractor_structured(payload) == ExtractorResult(
        deltas=parse_delta(notation),
        action_class=parse_action_class(notation),
        confidence=1.0,
    )


def test_parse_extractor_structured_missing_deltas_is_empty_delta():
    result = parse_extractor_structured({"action_class": "inspection"})
    assert result.deltas == {}
    assert result.action_class == "inspection"


def test_parse_extractor_structured_repeated_key_merges():
    result = parse_extractor_structured(
        {"deltas": {"inv": "+lantern"}, "action_class": "task"}
    )
    assert result.deltas == {"inv": "+lantern"}


def test_parse_extractor_structured_rejects_unknown_key():
    with pytest.raises(DeltaParseError):
        parse_extractor_structured(
            {"deltas": {"whereabouts": "docks"}, "action_class": "travel"}
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"deltas": {"time": "half past two"}, "action_class": "other"},
        {"deltas": {"inv": "lantern"}, "action_class": "task"},
        {"deltas": {"loc": ""}, "action_class": "travel"},
    ],
)
def test_parse_extractor_structured_rejects_bad_values(payload):
    with pytest.raises(DeltaParseError):
        parse_extractor_structured(payload)


def test_parse_extractor_structured_rejects_unknown_action():
    with pytest.raises(InvalidActionClass):
        parse_extractor_structured({"deltas": {}, "action_class": "frobnicate"})


def test_extractor_tool_carries_delta_vocabulary():
    tool = extractor_tool()
    params = tool["function"]["parameters"]
    assert tool["function"]["name"] == EXTRACTOR_TOOL_NAME
    assert set(params["required"]) == {"deltas", "action_class"}
    assert set(params["properties"]["action_class"]["enum"]) == set(ACTION_CLASSES)
    description = params["properties"]["deltas"]["description"]
    assert all(key in description for key in DELTA_KEYS)


def test_build_extractor_request_profile_tiers():
    # No function-calling -> None (today's notation path byte-for-byte).
    assert build_extractor_request_profile({"function_calling": False}) is None
    assert build_extractor_request_profile({}) is None
    forced = build_extractor_request_profile(
        {"function_calling": True, "tool_choice_forced": True}
    )
    # W9.14: "required" is the enforced form (the named shape is a hint).
    assert forced["tool_choice"] == "required"
    auto = build_extractor_request_profile({"function_calling": True})
    assert auto["tool_choice"] == "auto"


def test_llm_extractor_structured_path_calls_tool_and_parses():
    extractor, captured = _profile_extractor(
        [_tool_call_response(json.dumps({"deltas": {"time": "14:35"}, "action_class": "dialogue"}))]
    )

    result = extractor("I greet the merchant", "The night market hums.")

    assert result == ExtractorResult(
        deltas={"time": "14:35"}, action_class="dialogue", confidence=1.0
    )
    assert len(captured) == 1
    body = captured[0]
    assert body["tool_choice"] == "required"
    assert body["tools"][0]["function"]["name"] == EXTRACTOR_TOOL_NAME
    assert body["temperature"] == 0.1


def test_llm_extractor_structured_empty_then_repairs():
    extractor, captured = _profile_extractor(
        [
            _tool_call_response(""),  # empty args -> boundary failure
            _tool_call_response(json.dumps({"deltas": {}, "action_class": "rest"})),
        ]
    )

    result = extractor("I sleep", "The fire dies.")

    assert result.deltas == {}
    assert result.action_class == "rest"
    assert result.confidence == 0.5
    assert len(captured) == 2
    # the structured repair turn carries the structured instruction, not the notation one
    assert captured[1]["messages"][-1]["role"] == "user"
    assert "function" in captured[1]["messages"][-1]["content"]


def test_llm_extractor_structured_degrades_to_notation_content():
    # A model that ignores the tool and answers in the notation must still parse
    # (Tier A -> D degrade, D69: the notation floor is permanent).
    extractor, _ = _profile_extractor(
        [_chat_response("Δ[weather: light rain]\naction: travel")]
    )

    result = extractor("I walk into the rain", "Rain soaks the street.")

    assert result.deltas == {"weather": "light rain"}
    assert result.action_class == "travel"


def test_llm_extractor_structured_failure_raises_extraction_error():
    extractor, captured = _profile_extractor(
        [_chat_response("prose one"), _chat_response("prose two")]
    )

    with pytest.raises(ExtractionError):
        extractor("hello", "world")

    assert len(captured) == 2


def test_llm_extractor_structured_failure_falls_back_to_rules():
    primary, _ = _profile_extractor(
        [_chat_response("prose one"), _chat_response("prose two")]
    )
    extractor = FallbackExtractor(primary)

    result = extractor("I take the lantern.", "She pockets it.")

    assert result.deltas == {"inv": "+lantern"}


def test_llm_extractor_without_profile_stays_on_notation():
    # Byte-for-byte: no profile -> the request carries NO tools/tool_choice.
    extractor, captured, _ = _scripted_extractor(["Δ[]\naction: other"])

    extractor("I wait", "Silence.")

    assert "tools" not in captured[0]
    assert "tool_choice" not in captured[0]
