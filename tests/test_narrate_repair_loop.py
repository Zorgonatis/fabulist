"""The narrator repair loop (W9.24 §3.3) — the unit half of acceptance §8.3.

The narrate node's internals are draft → engine-forced validate → targeted
patch, all inside the node and all in WORKING MEMORY: the streamed draft streams
as it always did, and the commit node lands ONE narrative row carrying the final
text. What these tests pin:

* **the clean-turn cost contract** — a clean draft costs exactly ONE completion
  and puts nothing on the wire but the prompt the pre-W9.24 narrator sent (the
  prompt-cache/prefix and the draft are byte-identical to the builder's own
  output), with no ``agent_loop`` rows and no repair frames;
* **the engagement shape** — a flagged draft engages the loop through the
  synthetic flags PAIR (an assistant ``tool_calls`` message with id
  ``engine_flags`` plus its ``role: "tool"`` result), never a bare tool message;
* **targeted repair** — patches go through ``quality.apply_patch`` over prose
  segments, re-validate after every patch, and the FINAL text is what the node
  returns (the model's own closing text is never adopted as a rewrite);
* **the DSL counterpart** — ``patch_dsl`` repairs the CHANNEL the prose tool
  honestly refuses: `replace`/`delete`/`insert_after` land one statement at a
  time, an op that would break the DSL is rejected with the gate's parse error
  and the channel is left untouched, an ambiguous LHS is listed instead of
  guessed, and prose is never touched by it;
* **the log contract** — one ``narrative`` row, no narrator-actor ``revision``
  event pre-commit, the repair frames ephemeral (stream only);
* **the retired regen** — a critic ``fail`` keeps the turn, and a connection
  without function calling keeps the draft (no second completion);
* **cancel safety** — a cancel mid-repair leaves only the ephemeral partial.

The fakes are streaming callers with the W1.1 event protocol, so the loop's own
decisions are what is under test; the log is a REAL ``EventLog``, so every row
also passes the append door's schema gate.
"""

from __future__ import annotations

import asyncio
import json

from fabulist.graph import GraphTurnEngine, build_graph
from fabulist.graph.nodes import PATCH_DSL_TOOL, PATCH_PROSE_TOOL, make_narrate_node

FUNCTION_CALLING = {"function_calling": True}

SESSION_ID = "s-repair"
USER = "I wait under the awning."

#: A draft carrying a banned phrase (validator ``slop`` → ``shivers_down``), so
#: the deterministic validate step flags it with zero model cost.
SLOP_DRAFT = "The hall was quiet. A shiver down her spine said otherwise."
CLEAN_DRAFT = "Rain pooled in the gutter and the lantern guttered out."
PATCHED_PROSE = "The hall was quiet. She held still and listened."

#: The ONE targeted edit that clears the slop flag.
SLOP_PATCH = {
    "find": "A shiver down her spine said otherwise.",
    "replace": "She held still and listened.",
}


# -- fakes -------------------------------------------------------------------


class RepairCaller:
    """A streaming caller over scripted replies; records every message list.

    A reply is ``{"content": str, "tool_args": str, "tool": str}``:
    ``tool_args`` makes it a function call, named by the ``meta`` the real
    transport stamps on every fragment (the loop declares TWO tools —
    ``patch_prose``/``patch_dsl`` — so a call is attributed by name, never by
    position). ``reasoning`` streams provider reasoning deltas ahead of the
    content/tool frames, the order a thinking model emits them in.
    """

    def __init__(self, replies: list[dict]) -> None:
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def __call__(self, messages, stream=True, request_profile=None):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "stream": stream,
                "request_profile": request_profile,
            }
        )
        reply = self.replies.pop(0)
        # PRESENCE, not truthiness — the transport's rule: a reply that carries
        # ``reasoning: ""`` said "this turn's reasoning is empty", which is not
        # the same statement as "no reasoning field at all".
        if "reasoning" in reply:
            yield {"type": "reasoning", "delta": reply["reasoning"] or ""}
        if reply.get("content"):
            yield {"type": "content", "delta": reply["content"], "meta": {"channel": "prose"}}
        if reply.get("tool_args"):
            event: dict = {"type": "tool_call", "delta": reply["tool_args"]}
            if reply.get("tool"):
                event["meta"] = {"name": reply["tool"]}
            yield event


def patch_reply(*edits: dict) -> dict:
    return {
        "tool": PATCH_PROSE_TOOL,
        "tool_args": json.dumps({"edits": list(edits)}),
    }


def dsl_reply(*ops: dict) -> dict:
    return {
        "tool": PATCH_DSL_TOOL,
        "tool_args": json.dumps({"ops": list(ops)}),
    }


def _state(**overrides) -> dict:
    state = {
        "session_id": SESSION_ID,
        "user_content": USER,
        "facts": "",
        "plan": "",
        "kind": "in_character",
    }
    state.update(overrides)
    return state


def _narrate(log, caller, *, capabilities=FUNCTION_CALLING, timeout_s=None, return_thinking=None):
    return make_narrate_node(
        model=caller,
        log=log,
        loop_capabilities=capabilities,
        loop_timeout_s=timeout_s,
        loop_return_thinking=return_thinking,
    )


def _agent_loop_rows(log, session_id: str = SESSION_ID) -> list[dict]:
    return [
        event.payload
        for event in log.get_events(session_id)
        if event.type == "system" and event.payload.get("event") == "agent_loop"
    ]


def _kinds(rows: list[dict]) -> list[str]:
    return [row["kind"] for row in rows]


def _prose(out: dict) -> str:
    return "\n".join(
        segment["text"] for segment in out["segments"] if segment["kind"] == "prose"
    )


def _flag_codes(flags: list[dict]) -> list[str]:
    return [str(flag.get("rule_id") or flag.get("check")) for flag in flags]


# -- the clean-turn cost contract (§8.3a) ------------------------------------


def test_clean_draft_costs_one_completion_and_engages_nothing(tmp_path):
    """ONE completion, no ``agent_loop`` row, no repair frame — pinned."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "clean.db")
    try:
        caller = RepairCaller([{"content": CLEAN_DRAFT}])
        out = asyncio.run(_narrate(log, caller)(_state()))

        assert len(caller.calls) == 1
        assert _prose(out) == CLEAN_DRAFT
        assert out["validation_errors"] == []
        assert _agent_loop_rows(log) == []
        # No request profile rides the clean path: the draft call is the narrator
        # call, byte-identical to the pre-W9.24 one (no tools, no tool_choice).
        assert caller.calls[0]["request_profile"] is None
        assert caller.calls[0]["stream"] is True
    finally:
        log.close()


def test_clean_turn_prompt_is_the_builders_own_output(tmp_path):
    """The draft prompt is untouched on a clean turn (byte-identical pin).

    The loop only ever APPENDS (AGENTS 6) — so the very first completion must see
    exactly the message list the node's builder produced, with nothing injected.
    """
    from fabulist.log import EventLog
    from fabulist.graph.nodes import default_narrator_messages

    log = EventLog(tmp_path / "prompt.db")
    try:
        state = _state()
        expected = default_narrator_messages(state)
        caller = RepairCaller([{"content": CLEAN_DRAFT}])
        asyncio.run(_narrate(log, caller)(state))

        assert caller.calls[0]["messages"] == expected
    finally:
        log.close()


# -- engagement: the synthetic flags pair (§3.2) ----------------------------


def test_flagged_draft_engages_the_loop_with_the_pinned_flags_pair(tmp_path):
    """The nudge is an assistant tool_calls message + its ``role: "tool"`` result."""
    from fabulist.agents.loop import ENGINE_FLAGS_CALL_ID, ENGINE_FLAGS_TOOL
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "pair.db")
    try:
        caller = RepairCaller(
            [
                {"content": SLOP_DRAFT},
                patch_reply(SLOP_PATCH),
                {"content": "Done."},
            ]
        )
        out = asyncio.run(_narrate(log, caller)(_state()))

        second = caller.calls[1]["messages"]
        # (a) the draft the model is being asked to repair, as its own turn.
        assert second[-3] == {"role": "assistant", "content": SLOP_DRAFT}
        # (b) the assistant tool_calls message naming the engine's synthetic call.
        nudge = second[-2]
        assert nudge["role"] == "assistant"
        assert nudge["tool_calls"][0]["id"] == ENGINE_FLAGS_CALL_ID
        assert nudge["tool_calls"][0]["function"]["name"] == ENGINE_FLAGS_TOOL
        assert nudge["tool_calls"][0]["function"]["arguments"] == "{}"
        # (c) the matching tool result — NEVER a bare tool message.
        result = second[-1]
        assert result["role"] == "tool"
        assert result["tool_call_id"] == ENGINE_FLAGS_CALL_ID
        assert "shivers_down" in result["content"]
        # The flags pair is the ONLY thing the engine injected: the prefix is
        # byte-identical across iterations (the prompt-cache contract).
        assert second[: len(second) - 3] == caller.calls[0]["messages"]
        # The final text is the PATCHED work, and the flags are gone.
        assert _prose(out) == PATCHED_PROSE
        assert "shiver" not in _prose(out).lower()
    finally:
        log.close()


def test_every_iteration_lands_as_agent_loop_rows(tmp_path):
    """D9: the runner (engine code) appends one row per measured step."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "rows.db")
    try:
        caller = RepairCaller(
            [
                {"content": SLOP_DRAFT},
                patch_reply(SLOP_PATCH),
                {"content": "Done."},
            ]
        )
        asyncio.run(_narrate(log, caller, timeout_s=900.0)(_state()))

        rows = _agent_loop_rows(log)
        assert _kinds(rows) == [
            "completion",  # the model's first repair attempt
            "tool_call",  # its patch
            "tool_result",
            "completion",  # the model closes out
            "final",
        ]
        patches = [row for row in rows if row["kind"] == "tool_call"]
        assert [row["tool"] for row in patches] == [PATCH_PROSE_TOOL]
        assert rows[-1]["surface"] == "narrate"
        assert rows[-1]["timed_out"] is False
        assert all("latency_s" in row for row in rows if row["kind"] == "completion")
        # The DRAFT is the node's own call (its usage row + agent capture), and
        # the flags pair is seeded into the tail rather than injected by the
        # runtime — so the loop trace begins at the first repair attempt. That is
        # the honest record: no second draft was ever generated.
        assert 1 not in [row["iteration"] for row in rows if row["kind"] == "final"]
    finally:
        log.close()


# -- targeted repair, never a rewrite ---------------------------------------


def test_model_closing_text_is_never_adopted_as_a_rewrite(tmp_path):
    """Repairs are PATCHES (W9.24 §3.3.3) — a prose answer is not a new draft."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "rewrite.db")
    try:
        caller = RepairCaller(
            [
                {"content": SLOP_DRAFT},
                patch_reply(SLOP_PATCH),
                {"content": "A completely different scene, rewritten from scratch."},
            ]
        )
        out = asyncio.run(_narrate(log, caller)(_state()))
        assert "completely different scene" not in _prose(out)
        assert _prose(out) == PATCHED_PROSE
    finally:
        log.close()


def test_rejected_patch_is_reported_back_and_leaves_the_draft_untouched(tmp_path):
    """An unmatched ``find`` rejects the whole patch (``apply_patch`` is atomic)."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "reject.db")
    try:
        caller = RepairCaller(
            [
                {"content": SLOP_DRAFT},
                patch_reply({"find": "text that is not in the prose", "replace": "x"}),
                {"content": "Done."},
            ]
        )
        out = asyncio.run(_narrate(log, caller)(_state()))

        results = [
            row
            for row in _agent_loop_rows(log)
            if row["kind"] == "tool_result" and not row.get("injected")
        ]
        assert results and results[0]["result"]["ok"] is False
        assert "not present in prose" in results[0]["result"]["error"]
        # The draft survives byte-identically (keep + flags, never a half edit).
        assert _prose(out) == SLOP_DRAFT
    finally:
        log.close()


def test_prose_markup_flag_clears_when_a_patch_removes_the_markup(tmp_path):
    """The D80 detect reads the SEGMENTS, so a repair clears the flag with it."""
    from fabulist.log import EventLog
    from fabulist.graph.nodes import _prose_markup_flags

    log = EventLog(tmp_path / "markup.db")
    try:
        marked = '(FACTS)\n<div style="color: #0f0;">TERMINAL READY</div>\nHobbs waits.'
        caller = RepairCaller(
            [
                {"content": marked},
                patch_reply(
                    {
                        "find": '<div style="color: #0f0;">TERMINAL READY</div>',
                        "replace": "TERMINAL READY",
                    }
                ),
                {"content": "Done."},
            ]
        )
        state = _state()
        out = asyncio.run(_narrate(log, caller)(state))

        assert "<div" not in _prose(out)
        assert _prose_markup_flags(out["segments"]) == []
        # ...and the pre-repair draft DID trip it (the loop engaged because it
        # would otherwise have routed the turn into the critic).
        assert any(
            row["kind"] == "completion" and row["iteration"] == 1
            for row in _agent_loop_rows(log)
        )
    finally:
        log.close()


# -- the attribution contract rides the same loop (W9.28 D3/§3.4) ------------


def _seed_palette(log, name: str = "Mara"):
    """Admit one card member and return the turn's palette (W9.28 D1/D2).

    ``palette_for_turn`` reads the cast off the REAL log, so a repair-loop pin
    for ``attribution_missing`` seeds one member rather than faking the seam.
    """
    from fabulist.state.attribution import palette_for_turn
    from fabulist.state.cast import admit_member

    admit_member(log, SESSION_ID, name, provenance="card", admitted_by="system")
    return palette_for_turn(log, SESSION_ID)


#: One unwrapped spoken line — strictly unattributed (Q2 = B).
UNWRAPPED_DRAFT = 'Mara leans on the counter. "You\'re late." She grins.'


def test_attribution_missing_engages_the_loop_and_a_wrap_clears_it(tmp_path):
    """The flag IS the fallback: anchored, model-visible, and cleared by a wrap."""
    from fabulist.log import EventLog
    from fabulist.graph.nodes import _attribution_missing_flags
    from fabulist.state.attribution import parse_attribution

    log = EventLog(tmp_path / "attribution.db")
    try:
        palette = _seed_palette(log)
        assert palette, "a seeded card member must yield a palette entry"
        entry = palette[0]
        wrapped = f'<span style="color:{entry.hex}">"You\'re late."</span>'

        caller = RepairCaller(
            [
                {"content": UNWRAPPED_DRAFT},
                patch_reply({"find": '"You\'re late."', "replace": wrapped}),
                {"content": "Done."},
            ]
        )
        out = asyncio.run(_narrate(log, caller)(_state()))

        # What the model read: the finding, the exact prose to patch, the fix.
        payload = _injected_payload(caller)
        finding = next(
            line for line in payload["flags"] if "attribution_missing" in line
        )
        assert '"You\'re late."' in finding.split("anchor (exact prose to patch):")[1]
        assert entry.hex in finding and entry.display_name in finding
        assert "fix:" in finding

        # The wrap landed, the flag cleared, and the line is now attributed.
        assert wrapped in _prose(out)
        assert _attribution_missing_flags(out["segments"], palette) == []
        assert [row["speaker"] for row in parse_attribution(_prose(out), palette)] == [
            entry.display_name
        ]
        # The colour span itself is NOT prose markup (D6) — no second flag.
        assert all("prose_markup" not in line for line in payload["flags"])
    finally:
        log.close()


def test_the_palette_is_built_once_per_repair_run(tmp_path, monkeypatch):
    """R2 P2: the frame builds the palette ONCE; iteration re-derives flags only."""
    import fabulist.state.attribution as attribution

    from fabulist.graph.nodes import _attribution_missing_flags
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "palette-once.db")
    try:
        palette = _seed_palette(log)
        entry = palette[0]
        calls: list[str] = []
        real = attribution.palette_for_turn

        def counting(log_, session_id):
            calls.append(session_id)
            return real(log_, session_id)

        monkeypatch.setattr(attribution, "palette_for_turn", counting)

        two_lines = (
            'Mara leans on the counter. "You\'re late." She grins. '
            '"Borin sent me."'
        )
        caller = RepairCaller(
            [
                {"content": two_lines},
                patch_reply(
                    {
                        "find": '"You\'re late."',
                        "replace": f'<span style="color:{entry.hex}">"You\'re late."</span>',
                    }
                ),
                patch_reply(
                    {
                        "find": '"Borin sent me."',
                        "replace": f'<span style="color:{entry.hex}">"Borin sent me."</span>',
                    }
                ),
                {"content": "Done."},
            ]
        )
        out = asyncio.run(_narrate(log, caller)(_state()))

        # Two patched rounds re-derived the flags at least three times, yet the
        # palette was read exactly once for the whole run.
        assert calls == [SESSION_ID]
        assert len(_agent_loop_rows(log)) > 2
        assert _attribution_missing_flags(out["segments"], palette) == []
    finally:
        log.close()


def test_the_validate_node_records_attribution_missing_post_commit(tmp_path):
    """The inspector/critic read the SAME fold the loop repaired against."""
    from fabulist.graph.nodes import make_validate_node
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "validate-attribution.db")
    try:
        _seed_palette(log)
        state = {
            "session_id": SESSION_ID,
            "user_content": USER,
            "segments": [{"kind": "prose", "text": UNWRAPPED_DRAFT}],
            "narrative_event": {},
        }
        out = asyncio.run(make_validate_node(log=log)(state))

        codes = [flag["rule_id"] for flag in out["critic_flags"]]
        assert "attribution_missing" in codes
        rows = [
            event
            for event in log.get_events(SESSION_ID)
            if event.payload.get("event") == "validator_flags"
        ]
        assert [flag["rule_id"] for flag in rows[-1].payload["flags"]] == [
            "attribution_missing"
        ]
        # Nothing failed: the palette build is best-effort, never a validator error.
        assert not [
            event
            for event in log.get_events(SESSION_ID)
            if event.payload.get("event") == "validator_failed"
        ]
    finally:
        log.close()


#: Speech the model COLOURED but did not quote — the parser has no anchor, so
#: only the unquoted fold can see it (the owner-spotted hole).
UNQUOTED_SPEECH = "Get on or get out of the way."


def test_attribution_unquoted_engages_the_loop_and_a_quote_clears_it(tmp_path):
    """Colour with no quote: steered on, then cleared once the line is quoted."""
    from fabulist.graph.nodes import _attribution_unquoted_flags
    from fabulist.log import EventLog
    from fabulist.state.attribution import parse_attribution

    log = EventLog(tmp_path / "unquoted.db")
    try:
        palette = _seed_palette(log)
        entry = palette[0]
        span = f'<span style="color:{entry.hex}">'
        draft = f"Mara leans on the counter. {span}{UNQUOTED_SPEECH}</span>"
        quoted = f'{span}"{UNQUOTED_SPEECH}"</span>'

        caller = RepairCaller(
            [
                {"content": draft},
                patch_reply(
                    {
                        "find": f"{span}{UNQUOTED_SPEECH}</span>",
                        "replace": quoted,
                    }
                ),
                {"content": "Done."},
            ]
        )
        out = asyncio.run(_narrate(log, caller)(_state()))

        # The draft DID trip the flag — the loop engaged because of it.
        assert _attribution_unquoted_flags(
            [{"kind": "prose", "text": draft}], palette
        )
        # What the model read: the span's prose as the anchor, its hue in the fix.
        payload = _injected_payload(caller)
        assert len(payload["flags"]) == 1
        finding = payload["flags"][0]
        assert "attribution_unquoted" in finding
        assert UNQUOTED_SPEECH in finding.split("anchor (exact prose to patch):")[1]
        assert entry.hex in finding and entry.display_name in finding
        assert "remove the span" in finding

        # The quote landed: the flag cleared and the line is now attributed.
        assert quoted in _prose(out)
        assert _attribution_unquoted_flags(out["segments"], palette) == []
        assert [row["speaker"] for row in parse_attribution(_prose(out), palette)] == [
            entry.display_name
        ]
    finally:
        log.close()


def test_a_patch_that_removes_the_span_clears_the_unquoted_flag(tmp_path):
    """The other taught fix: mis-coloured narration loses the span."""
    from fabulist.graph.nodes import _attribution_unquoted_flags
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "unquoted-removed.db")
    try:
        palette = _seed_palette(log)
        entry = palette[0]
        span = f'<span style="color:{entry.hex}">'
        narration = "She waits by the door."
        draft = f"Mara leans on the counter. {span}{narration}</span>"

        caller = RepairCaller(
            [
                {"content": draft},
                patch_reply({"find": f"{span}{narration}</span>", "replace": narration}),
                {"content": "Done."},
            ]
        )
        out = asyncio.run(_narrate(log, caller)(_state()))

        assert span not in _prose(out)
        assert _attribution_unquoted_flags(out["segments"], palette) == []
        # ...and the pre-repair draft DID trip it (the loop ran for this flag).
        assert any(
            row["kind"] == "completion" and row["iteration"] == 1
            for row in _agent_loop_rows(log)
        )
    finally:
        log.close()


def test_the_validate_node_records_attribution_unquoted_post_commit(tmp_path):
    """The inspector/critic read the SAME fold the loop repaired against."""
    from fabulist.graph.nodes import make_validate_node
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "validate-unquoted.db")
    try:
        palette = _seed_palette(log)
        text = (
            "Mara leans on the counter. "
            f'<span style="color:{palette[0].hex}">She waits by the door.</span>'
        )
        state = {
            "session_id": SESSION_ID,
            "user_content": USER,
            "segments": [{"kind": "prose", "text": text}],
            "narrative_event": {},
        }
        out = asyncio.run(make_validate_node(log=log)(state))

        assert "attribution_unquoted" in [
            flag["rule_id"] for flag in out["critic_flags"]
        ]
        rows = [
            event
            for event in log.get_events(SESSION_ID)
            if event.payload.get("event") == "validator_flags"
        ]
        assert [flag["rule_id"] for flag in rows[-1].payload["flags"]] == [
            "attribution_unquoted"
        ]
    finally:
        log.close()


# -- the ladder's degrade: no function calling, no extra cost ----------------


def test_a_connection_without_function_calling_keeps_the_draft(tmp_path):
    """The clean-turn cost contract holds off Tier A too: no repair completion."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "nocaps.db")
    try:
        caller = RepairCaller([{"content": SLOP_DRAFT}])
        out = asyncio.run(_narrate(log, caller, capabilities=None)(_state()))

        assert len(caller.calls) == 1  # the draft, and nothing else
        assert _prose(out) == SLOP_DRAFT  # keep + flags, exactly as before
        assert _agent_loop_rows(log) == []
    finally:
        log.close()


# -- the graph: one narrative row, no pre-commit revision event -------------


class _GraphCaller:
    """Narrator caller for the graph tests: scripted replies, then a fallback."""

    def __init__(self, replies: list[dict], fallback: str = "Quiet.") -> None:
        self.replies = list(replies)
        self.fallback = fallback
        self.calls: list[dict] = []

    async def __call__(self, messages, stream=True, request_profile=None):
        self.calls.append({"messages": messages, "request_profile": request_profile})
        reply = self.replies.pop(0) if self.replies else {"content": self.fallback}
        if reply.get("content"):
            yield {"type": "content", "delta": reply["content"]}
        if reply.get("tool_args"):
            event: dict = {"type": "tool_call", "delta": reply["tool_args"]}
            if reply.get("tool"):
                event["meta"] = {"name": reply["tool"]}
            yield event


class _FlaggingGate:
    """Decision seam that fires every ``noul`` question (keeps the critic honest)."""

    enabled = True

    async def ask(self, state, questions):
        items = questions.items() if isinstance(questions, dict) else ()
        return {
            qid: {"noul": 1.0}
            for qid, row in items
            if isinstance(row, dict) and row.get("type") == "noul"
        }


class _CleanGate:
    enabled = True

    async def ask(self, state, questions):
        return {}


def _extractor(user_msg, prose):
    return {"deltas": {}, "action_class": "dialogue"}


def _graph(log, model, **kwargs):
    return build_graph(
        model=model,
        extractor=_extractor,
        log=log,
        narrate_loop_capabilities=kwargs.pop("capabilities", FUNCTION_CALLING),
        **kwargs,
    )


async def _events(graph, log, model, session_id: str = SESSION_ID):
    engine = GraphTurnEngine(graph=graph, log=log, model=model)
    async for event in engine.run_turn(session_id, USER, "in_character"):
        yield event


def test_a_defect_turn_lands_one_narrative_row_with_no_revision_event(tmp_path):
    """§8.3b: the repair is in working memory — nothing log-level is revised."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "graph.db")
    try:
        model = _GraphCaller(
            [
                {"content": SLOP_DRAFT},
                patch_reply(SLOP_PATCH),
                {"content": "Done."},
            ],
            fallback="Hobbs sets down the mug.",
        )
        events = asyncio.run(_collect(_events(_graph(log, model), log, model)))

        rows = log.get_events(SESSION_ID)
        narratives = [event for event in rows if event.type == "narrative"]
        assert len(narratives) == 1
        assert narratives[0].payload["prose"] == PATCHED_PROSE
        # No narrator-actor revision event exists pre- or post-commit: the only
        # revision writer left is the critic's diffs patch, and none ran.
        assert [event for event in rows if event.type == "revision"] == []
        # ONE narrate-class completion for the clean part + the repair iterations.
        assert len(model.calls) == 3
        # The repair frames are EPHEMERAL: they ride the stream, never the log.
        repair_frames = [
            event
            for event in events
            if event["type"] == "stage"
            and "(repair" in str(event["meta"].get("label"))
        ]
        assert repair_frames, "the narrate slot carries the repair annotation"
        assert all(frame["meta"]["stage"] == "narrate" for frame in repair_frames)
        assert all(frame["meta"]["index"] == 3 for frame in repair_frames)
        assert all(frame["meta"]["total"] == 8 for frame in repair_frames)
        assert not [
            event
            for event in rows
            if event.payload.get("event") == "turn_stages"
            and any("regen" in str(step.get("name")) for step in event.payload["steps"])
        ]
        assert events[-1]["type"] == "done"
    finally:
        log.close()


async def _collect(agen):
    return [event async for event in agen]


def test_a_clean_graph_turn_costs_one_narrate_completion(tmp_path):
    """The graph-level clean-turn pin: usage rows say ONE narrate-class call."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "cleangraph.db")
    try:
        model = _GraphCaller([], fallback=CLEAN_DRAFT)
        events = asyncio.run(_collect(_events(_graph(log, model), log, model)))

        assert len(model.calls) == 1
        rows = log.get_events(SESSION_ID)
        assert [event for event in rows if event.type == "system" and event.payload.get("event") == "agent_loop"] == []
        narrative = [event for event in rows if event.type == "narrative"]
        assert len(narrative) == 1
        assert narrative[0].payload["prose"] == CLEAN_DRAFT
        assert events[-1]["type"] == "done"
    finally:
        log.close()


def test_cancel_mid_repair_leaves_only_the_ephemeral_partial(tmp_path):
    """§8.3e: the repair window is still PRE-commit, so cancel keeps nothing."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "cancel.db")
    try:
        model = _GraphCaller(
            [
                {"content": SLOP_DRAFT},
                patch_reply(SLOP_PATCH),
                {"content": "Done."},
            ],
            fallback="Hobbs sets down the mug.",
        )
        graph = _graph(log, model)

        async def scenario():
            engine = GraphTurnEngine(graph=graph, log=log, model=model)
            streamed = []
            async for event in engine.run_turn(SESSION_ID, USER, "in_character"):
                streamed.append(event)
                if event["type"] == "stage" and "(repair" in str(
                    event["meta"].get("label")
                ):
                    engine.cancel()
            return streamed

        streamed = asyncio.run(scenario())
        assert streamed[-1] == {"type": "done", "meta": {"cancelled": True}}
        # Nothing was committed: no narrative row, no projection turn — only the
        # engine's own ephemeral partial.
        rows = log.get_events(SESSION_ID)
        narratives = [event for event in rows if event.type == "narrative"]
        assert all(event.ephemeral for event in narratives)
        assert log.get_projection(SESSION_ID)["narratives"] == []
        assert [event for event in rows if event.type == "revision"] == []
    finally:
        log.close()


# -- the retired regen, at the graph level ----------------------------------


def test_critic_fail_keeps_the_turn_and_does_not_re_enter_narrate(tmp_path):
    """§8.3c: a critic FAIL logs and keeps — ONE narrate pass, ONE narrative."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "fail.db")
    try:
        model = _GraphCaller([], fallback=CLEAN_DRAFT)

        async def critic(evidence):
            return json.dumps(
                {"verdict": "fail", "reason": "stalls", "edits": [], "regen_feedback": "advance it"}
            )

        graph = _graph(log, model, critic=critic, decision=_FlaggingGate())
        events = asyncio.run(_collect(_events(graph, log, model)))

        assert len(model.calls) == 1  # no re-run
        rows = log.get_events(SESSION_ID)
        assert len([event for event in rows if event.type == "narrative"]) == 1
        assert [event for event in rows if event.type == "revision"] == []
        verdicts = [e for e in rows if e.payload.get("event") == "critic_verdict"]
        assert [event.payload["verdict"] for event in verdicts] == ["fail"]
        assert events[-1]["type"] == "done"
    finally:
        log.close()


# -- the NARRATE slot's passive headroom (W9.25 §3.5.3) ---------------------
#
# W25.5 supplies the production ``window_fn``: the NARRATE slot's available
# prompt-token budget, resolved live through the wiring
# (``narrate_window_budget`` — ``context_window − response_length −
# RESERVE_TOKENS``). The loop's rows carry it as passive ``headroom`` and the
# loop PROCEEDS regardless: no stop condition, no ``window_exhausted``.

#: The fake "declared Context Size" the wiring would have resolved, already
#: through the pinned formula (the number is what matters, not its provenance).
DECLARED_WINDOW_BUDGET = 4096


def test_repair_loop_rows_carry_decreasing_headroom_when_the_slot_declares_a_window(
    tmp_path,
):
    """A declared window puts ``headroom`` on every row, shrinking per iteration."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "headroom.db")
    try:
        model = _GraphCaller(
            [
                {"content": SLOP_DRAFT},
                patch_reply(SLOP_PATCH),
                {"content": "Done."},
            ],
            fallback="Hobbs sets down the mug.",
        )
        events = asyncio.run(
            _collect(
                _events(
                    _graph(log, model, narrate_loop_window=lambda: DECLARED_WINDOW_BUDGET),
                    log,
                    model,
                )
            )
        )

        rows = _agent_loop_rows(log)
        assert rows
        by_iteration: dict[int, int] = {}
        for row in rows:
            assert "headroom" in row, row
            by_iteration.setdefault(row["iteration"], row["headroom"])
        headrooms = [by_iteration[iteration] for iteration in sorted(by_iteration)]
        assert len(headrooms) >= 2
        # The append-only tail grows every iteration, so the headroom shrinks.
        assert all(
            earlier > later for earlier, later in zip(headrooms, headrooms[1:])
        )
        # NOTHING STOPPED: the turn still committed the patched prose.
        narrative = [
            event for event in log.get_events(SESSION_ID) if event.type == "narrative"
        ]
        assert [event.payload["prose"] for event in narrative] == [PATCHED_PROSE]
        assert events[-1]["type"] == "done"
        assert not [row for row in rows if row["kind"] == "window_exhausted"]
    finally:
        log.close()


def test_repair_loop_rows_carry_no_headroom_without_a_declared_window(tmp_path):
    """No declared Context Size keeps the pre-W9.25 row shape byte-for-byte."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "noheadroom.db")
    try:
        model = _GraphCaller(
            [
                {"content": SLOP_DRAFT},
                patch_reply(SLOP_PATCH),
                {"content": "Done."},
            ],
            fallback="Hobbs sets down the mug.",
        )
        asyncio.run(_collect(_events(_graph(log, model), log, model)))

        rows = _agent_loop_rows(log)
        assert rows  # the loop ran; it simply had no window to measure against
        assert all("headroom" not in row for row in rows)
    finally:
        log.close()


# -- the validate node's markup flag (the W1 live-probe defect, pinned) ------


def test_markup_in_prose_flags_prose_markup_without_validator_failed(tmp_path):
    """A markup turn flags ``prose_markup`` and NEVER logs ``validator_failed``.

    The defect this pins: the validate node used to reference the narrator's
    strip-time ``prose_leaks`` list, which lives in a different scope — so the
    branch raised ``NameError`` every time it ran, the turn swallowed it as
    ``validator_failed``, and the D80 markup flag never reached the critic. The
    detect now reads the committed SEGMENTS (one spelling with the narrate
    node's validate step), so the flag fires and nothing failed.
    """
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "markup-flag.db")
    try:
        marked = 'Hobbs glances up.\n<div style="color:#0f0;">TERMINAL READY</div>'
        model = _GraphCaller([], fallback=marked)
        events = asyncio.run(
            _collect(_events(_graph(log, model, capabilities=None), log, model))
        )

        rows = log.get_events(SESSION_ID)
        assert not [
            event
            for event in rows
            if event.payload.get("event") == "validator_failed"
        ]
        validator_rows = [
            event for event in rows if event.payload.get("event") == "validator_flags"
        ]
        assert validator_rows, "the validate node recorded no flags at all"
        codes = [
            flag.get("rule_id")
            for flag in validator_rows[0].payload["flags"]
        ]
        assert "prose_markup" in codes
        # D80: detected, never stripped — the markup survives byte-identically.
        narrative = [event for event in rows if event.type == "narrative"]
        assert "<div" in narrative[0].payload["prose"]
        assert events[-1]["type"] == "done"
    finally:
        log.close()


# -- teaching steering: anchor + fix on every flag, and a patch failure that
# -- shows the model where the prose actually is ------------------------------

#: The live failure's shape: markup in prose plus a DSL statement, so the prose
#: inventory has a non-contiguous index to report.
LIVE_LIKE_DRAFT = (
    "Hobbs sets the mug down.\n\n```openui\n"
    'panel = Custom("<b>ok</b>", "")\n```\n\n'
    "<details>SHOP STATUS</details>\nHe waits."
)

#: The live model's own near-miss: a newline the prose never had.
LIVE_LIKE_FIND = "<details>\n<summary>SHOP STATUS</summary>"


def _injected_payload(caller: RepairCaller) -> dict:
    """The flags pair's tool result as the model read it (iteration 1)."""
    return json.loads(caller.calls[1]["messages"][-1]["content"])


def _tool_result(caller: RepairCaller, call_index: int) -> dict:
    """The tool result the model read after its ``call_index``-th completion."""
    return json.loads(caller.calls[call_index]["messages"][-1]["content"])


def test_the_flags_pair_renders_anchor_and_fix_per_finding(tmp_path):
    """Steering: every finding names the exact prose AND the corrective action."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "anchored.db")
    try:
        caller = RepairCaller(
            [{"content": LIVE_LIKE_DRAFT}, {"content": "Done."}]
        )
        asyncio.run(_narrate(log, caller)(_state()))

        payload = _injected_payload(caller)
        finding = next(line for line in payload["flags"] if "prose_markup" in line)
        assert "anchor (exact prose to patch):" in finding
        assert "<details>SHOP STATUS</details>" in finding
        assert "fix:" in finding and "Custom(html=..., css=...)" in finding
        # The injected notice forbids the identical retry that burned 158s live.
        assert "IDENTICAL retry will not change the outcome" in payload["instruction"]
        assert "DIFFERENT" in payload["instruction"]
    finally:
        log.close()


def test_a_failed_patch_teaches_the_prose_segment_inventory(tmp_path):
    """A rejection returns the real targets — never a bare "not present"."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "inventory.db")
    try:
        caller = RepairCaller(
            [
                {"content": LIVE_LIKE_DRAFT},
                patch_reply({"find": LIVE_LIKE_FIND, "replace": "SHOP STATUS"}),
                {"content": "Done."},
            ]
        )
        out = asyncio.run(_narrate(log, caller)(_state()))

        result = _tool_result(caller, 2)
        assert result["ok"] is False
        assert result["attempted_find"] == LIVE_LIKE_FIND
        assert "not present in prose" in result["error"]
        assert "ordered list of segments" in result["prose_is_segmented"]
        # Prose segments only, with their real indices: the dsl statement at 1 is
        # not a patch target, and 2 is where the markup lives.
        assert [entry.split(":", 1)[0] for entry in result["prose_segments"]] == ["0", "2"]
        assert result["prose_segments"][0].startswith("0: Hobbs sets the mug down.")
        assert "<details>SHOP STATUS</details>" in result["prose_segments"][1]
        # The current anchors ride along as ready-made `find` targets.
        assert any("<details>SHOP STATUS</details>" in a for a in result["anchors"])
        # ...and the directive names the ONE correction and the futility of a retry.
        assert "re-issue ONE corrected edit" in result["fix"]
        assert "identical retry will fail identically" in result["fix"]

        # The teaching result is what the LOG records too (unclipped here).
        rows = [
            row
            for row in _agent_loop_rows(log)
            if row["kind"] == "tool_result" and not row.get("injected")
        ]
        assert rows[0]["result"]["prose_segments"] == result["prose_segments"]
        # The draft survives byte-identically (keep + flags, never a half edit).
        assert _prose(out) == "Hobbs sets the mug down.\n<details>SHOP STATUS</details>\nHe waits."
    finally:
        log.close()


def test_a_patch_targeted_at_the_shown_anchor_clears_the_flag(tmp_path):
    """Steering pin: the anchor is copy-pastable prose, not a paraphrase."""
    from fabulist.log import EventLog
    from fabulist.graph.nodes import _prose_markup_flags

    log = EventLog(tmp_path / "anchorfind.db")
    try:
        marked = "<details>SHOP STATUS</details>\nHe waits."
        anchor = _prose_markup_flags([{"kind": "prose", "text": marked}])[0]["anchor"]
        assert anchor in marked

        caller = RepairCaller(
            [
                {"content": marked},
                patch_reply({"find": anchor, "replace": "He waits."}),
                {"content": "Done."},
            ]
        )
        out = asyncio.run(_narrate(log, caller)(_state()))
        assert _prose(out) == "He waits."
        assert _prose_markup_flags(out["segments"]) == []
    finally:
        log.close()


# -- the live-turn fixture: a prose `find` is never lost to a DSL edit -------
#
# A production log (one narrative event): the draft's own
# segments, and the exact three-edit patch the model re-issued 14 times (158s).
# Edits 0 and 1 aimed at markup that IS prose segment 4, verbatim; edit 2 aimed
# at the DSL root. The tool applied the patch WHOLE-PATCH ATOMICALLY, so the two
# valid prose edits never landed — and every rejection read
#
#     `find` not present in prose: 'root = ScenePane("Hobbs & Sons …'
#
# i.e. it quoted DSL source as the prose it had searched. The model had no way
# to learn that its prose edits were fine or that the DSL channel is unpatchable,
# so it re-sent the identical patch until the turn committed the raw markup.

FIXTURE_SEGMENTS = [
    {
        "kind": "prose",
        "text": "The memory of a bright, white-lit harbour jewellers—glass cases, white gloves, the soft chime of a bell—fades under the weight of a wet quay morning. The air inside Hobbs & Sons is a still soup of lamp oil, old rope and a thin chemical cleaner the owner calls \"Night Rain.\" It smells more like a wet dog in a laundry. A neon \"OPEN\" sign buzzes with a jagged electric hum, throwing a flickering pink glow over shelves crowded with rusted tools and chipped enamel.\n\nHobbs sits behind a scarred glass counter, his big frame spilling over the edges of a creaking chair. He doesn't stand. He doesn't smile. He simply watches you with one clear, dark eye while the other stays a clouded grey.",
    },
    {
        "kind": "dsl",
        "text": "hobbs = Entrance(\"Hobbs\", \"Owner of Hobbs & Sons\", \"A tall grey heron in an oilskin coat, tapping a claw on glass.\", {markers: \"Species: Heron; Eye: One clouded; Attire: Oilskin coat\"})",
    },
    {
        "kind": "prose",
        "text": "The damp clings to the skin like a cold sheet. A lamp overhead sways on its hook, doing nothing but pushing the grey air from one corner of the room to the other. Hobbs leans back, his feathers scraping against the chair, and taps one thick claw on the counter—*click, click, click*.\n\n\"Bout time you showed up. Hope you weren't expectin' a fancy glass case and a mint on your pillow, 'cause all we keep here is dust and things folks were too broke to hold onto.\"",
    },
    {
        "kind": "dsl",
        "text": "root = ScenePane(\"Hobbs & Sons — The Wet Morning\", [hobbs])",
    },
    {
        "kind": "prose",
        "text": "<details>\n<summary>SHOP STATUS</summary>\n\n- Cash: $4,250\n- Inventory: 47 items ($12,400 value)\n- Outstanding Loans: $1,850\n- Pending: None\n- Trust: Neutral\n</details>\n\n<div style=\"background: #222; color: #0f0; font-family: 'Courier New', monospace; padding: 15px; border: 2px solid #444; border-radius: 5px; width: fit-content;\">\n  <strong>HOBB'S LEDGER v1.0</strong><br>\n  -----------------------------------<br>\n  CASH: $4,250.00<br>\n  STOCK: 47 units<br>\n  LOANS: $1,850.00<br>\n  STATUS: OPEN<br>\n  -----------------------------------<br>\n  <span style=\"color: #f00;\">ALERT: Roof Leak Over Counter 3</span>\n</div>",
    },
]

FIXTURE_EDITS = [
    {
        "find": "<details>\n<summary>SHOP STATUS</summary>\n\n- Cash: $4,250\n- Inventory: 47 items ($12,400 value)\n- Outstanding Loans: $1,850\n- Pending: None\n- Trust: Neutral\n</details>",
        "replace": "status = Custom(\"SHOP STATUS\", \"<ul><li>Cash: $4,250</li><li>Inventory: 47 items ($12,400 value)</li><li>Outstanding Loans: $1,850</li><li>Pending: None</li><li>Trust: Neutral</li></ul>\", \"background: var(--bg-panel); color: var(--text); padding: 10px; border: 1px solid var(--line);\")",
        "count": 1,
    },
    {
        "find": "<div style=\"background: #222; color: #0f0; font-family: 'Courier New', monospace; padding: 15px; border: 2px solid #444; border-radius: 5px; width: fit-content;\">\n  <strong>HOBB'S LEDGER v1.0</strong><br>\n  -----------------------------------<br>\n  CASH: $4,250.00<br>\n  STOCK: 47 units<br>\n  LOANS: $1,850.00<br>\n  STATUS: OPEN<br>\n  -----------------------------------<br>\n  <span style=\"color: #f00;\">ALERT: Roof Leak Over Counter 3</span>\n</div>",
        "replace": "term = Interface(\"terminal\", \"HOBB'S LEDGER v1.0\", [\"CASH: $4,250.00\", \"STOCK: 47 units\", \"LOANS: $1,850.00\", \"STATUS: OPEN\", \"-----------------------------------\", \"ALERT: Roof Leak Over Counter 3\"], {glyph: \"📟\"})",
        "count": 1,
    },
    {
        "find": "root = ScenePane(\"Hobbs & Sons — The Wet Morning\", [hobbs])",
        "replace": "root = ScenePane(\"Hobbs & Sons — The Wet Morning\", [hobbs, status, term])",
        "count": 1,
    },
]

#: Two prose segments either side of a DSL statement, with the markup the D80
#: flag fires on in the last of them.
MARKUP_LAST_SEGMENTS = [
    {"kind": "prose", "text": "Hobbs sets the mug down and says nothing at all."},
    {"kind": "dsl", "text": 'root = ScenePane("shop", [hobbs])'},
    {"kind": "prose", "text": "<details>SHOP STATUS</details>\nHe waits."},
]

#: The same markup in a MIDDLE prose segment (a third prose segment follows).
MARKUP_MIDDLE_SEGMENTS = [
    {"kind": "prose", "text": "Hobbs sets the mug down and says nothing at all."},
    {"kind": "dsl", "text": 'root = ScenePane("shop", [hobbs])'},
    {"kind": "prose", "text": "He checks <details>SHOP STATUS</details> twice."},
    {"kind": "prose", "text": "Then he waits."},
]


def _draft_text(segments: list[dict]) -> str:
    """The raw draft those segments were cut from (DSL keeps its fences)."""
    parts = [
        f"```openui\n{segment['text']}\n```" if segment["kind"] == "dsl" else segment["text"]
        for segment in segments
    ]
    return "\n\n".join(parts)


def _repair_over(log, caller, segments: list[dict]):
    """Drive ``_repair_narrative`` over EXACT segments (the fixture's own input)."""
    from fabulist.graph.nodes import _repair_narrative

    return asyncio.run(
        _repair_narrative(
            model=caller,
            log=log,
            state=_state(),
            messages=[{"role": "user", "content": USER}],
            draft=_draft_text(segments),
            segments=[dict(segment) for segment in segments],
            capabilities=FUNCTION_CALLING,
        )
    )


def _first_tool_result(log) -> dict:
    """The tool result the model read after its first ``patch_prose`` call."""
    rows = [
        row
        for row in _agent_loop_rows(log)
        if row["kind"] == "tool_result" and not row.get("injected")
    ]
    assert rows, "the repair loop never called patch_prose"
    return rows[0]["result"]


def test_the_fixture_patch_lands_its_prose_edits_and_refuses_only_the_dsl_edit(tmp_path):
    """A rejected sibling never discards a ``find`` that IS in a prose segment."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "fixture.db")
    try:
        caller = RepairCaller([patch_reply(*FIXTURE_EDITS), {"content": "Done."}])
        out = _repair_over(log, caller, FIXTURE_SEGMENTS)

        result = _first_tool_result(log)
        # The two prose edits LANDED (they were valid every one of the 14 retries).
        assert result["applied"] == 2
        assert result["partial"] is True
        assert [entry["index"] for entry in result["rejected"]] == [2]
        assert out.passes == 1
        edited = out.segments[4]["text"]
        assert "<details>" not in edited and "<div " not in edited
        assert edited.startswith('status = Custom("SHOP STATUS"')
        assert 'term = Interface("terminal"' in edited
        # Nothing else moved: the other prose segments and the DSL are byte-equal.
        assert out.segments[0] == FIXTURE_SEGMENTS[0]
        assert out.segments[2] == FIXTURE_SEGMENTS[2]
        assert out.segments[1] == FIXTURE_SEGMENTS[1]
        assert out.segments[3] == FIXTURE_SEGMENTS[3]

        # ...and the sibling rejection is DIAGNOSED as a channel error, never as
        # missing prose: that mis-attribution is what bought the 14 retries.
        assert "not present in prose" not in result["error"]
        assert result["matches_segment"] == {"index": 3, "kind": "dsl"}
        assert "dsl segment at index 3" in result["error"]
        assert "not present in prose" not in result["rejected"][0]["error"]
        # The edits already in the draft are named as such, so the identical
        # retry is visibly pointless (its `find` is consumed).
        assert "do not re-issue them" in result["note"]
        assert result["searched_prose_head"].startswith(
            "The memory of a bright, white-lit harbour jewellers"
        )
        assert "ScenePane" not in result["searched_prose_head"]
    finally:
        log.close()


def test_a_find_in_the_last_prose_segment_patches(tmp_path):
    """No refusal when the ``find`` sits in the final prose segment."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "lastseg.db")
    try:
        caller = RepairCaller(
            [
                patch_reply({"find": "<details>SHOP STATUS</details>", "replace": "He checks the till."}),
                {"content": "Done."},
            ]
        )
        out = _repair_over(log, caller, MARKUP_LAST_SEGMENTS)

        result = _first_tool_result(log)
        assert result["ok"] is True and result["applied"] == 1
        assert out.segments[2]["text"] == "He checks the till.\nHe waits."
        assert out.segments[0] == MARKUP_LAST_SEGMENTS[0]
    finally:
        log.close()


def test_a_find_in_a_middle_prose_segment_patches(tmp_path):
    """No refusal when the ``find`` sits in a prose segment that is not the last."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "midseg.db")
    try:
        caller = RepairCaller(
            [
                patch_reply({"find": "<details>SHOP STATUS</details>", "replace": "the till ledger"}),
                {"content": "Done."},
            ]
        )
        out = _repair_over(log, caller, MARKUP_MIDDLE_SEGMENTS)

        result = _first_tool_result(log)
        assert result["ok"] is True and result["applied"] == 1
        assert out.segments[2]["text"] == "He checks the till ledger twice."
        assert out.segments[0] == MARKUP_MIDDLE_SEGMENTS[0]
        assert out.segments[3] == MARKUP_MIDDLE_SEGMENTS[3]
    finally:
        log.close()


def test_a_cross_boundary_find_is_refused_with_an_anchor_per_side(tmp_path):
    """A ``find`` spanning two prose segments says so — never "missing"."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "crossing.db")
    try:
        # Prose segments are joined with "\n", so a find copied across the
        # boundary matches the joined channel and no single segment.
        crossing = 'nothing at all.\n<details>SHOP STATUS</details>'
        caller = RepairCaller(
            [patch_reply({"find": crossing, "replace": "x"}), {"content": "Done."}]
        )
        out = _repair_over(log, caller, MARKUP_LAST_SEGMENTS)

        result = _first_tool_result(log)
        assert result["ok"] is False and result["applied"] == 0
        assert "straddles prose segments 0 and 2" in result["error"]
        assert "inside ONE segment" in result["error"]
        assert result["straddles"] == [0, 2]
        left_anchor, right_anchor = result["boundary_anchors"]
        # Each anchor is copyable prose: a literal substring of its neighbour.
        assert left_anchor in MARKUP_LAST_SEGMENTS[0]["text"]
        assert right_anchor in MARKUP_LAST_SEGMENTS[2]["text"]
        assert left_anchor in result["error"] and right_anchor in result["error"]
        # The draft is untouched (a boundary find is never applied across it).
        assert out.segments == MARKUP_LAST_SEGMENTS
    finally:
        log.close()


def test_a_dsl_find_is_refused_as_dsl_never_as_missing_prose(tmp_path):
    """The failure quotes the searched PROSE head, and names the DSL it matched."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "dslfind.db")
    try:
        dsl_find = 'root = ScenePane("shop", [hobbs])'
        caller = RepairCaller(
            [patch_reply({"find": dsl_find, "replace": "x"}), {"content": "Done."}]
        )
        out = _repair_over(log, caller, MARKUP_MIDDLE_SEGMENTS)

        result = _first_tool_result(log)
        assert result["ok"] is False
        assert "not present in prose" not in result["error"]
        assert "dsl segment at index 1" in result["error"]
        assert result["matches_segment"] == {"index": 1, "kind": "dsl"}
        assert "this tool cannot edit" in result["fix"]
        # ...and the refusal names the tool that CAN repair that channel.
        assert "patch_dsl" in result["fix"]
        # The text quoted back as searched is prose — the DSL never masquerades.
        assert result["searched_prose_head"].startswith("Hobbs sets the mug down")
        assert "ScenePane" not in result["searched_prose_head"]
        assert out.segments == MARKUP_MIDDLE_SEGMENTS
    finally:
        log.close()


# -- patch_dsl: the counterpart that CAN repair the DSL channel --------------
#
# Owner ruling: "of course the narrator needs to be able to fix its own DSL! It
# needs to be able to fix everything it outputs." ``patch_prose``'s honest
# refusal ("`find` is dsl source, which this tool cannot edit") is only half an
# answer without a tool that CAN repair that channel — the live model kept
# reaching for `root = ScenePane(...)` with find/replace edits. The
# ops are STATEMENT-level: the channel is parsed into `ident = Component(...)`
# statements, one op lands at a time, and the DSL gate re-validates the channel
# before the op is kept.

DSL_PROSE = "Hobbs sets the mug down and says nothing at all."
VALID_HOBBS = 'hobbs = Entrance("Hobbs", "Owner", "A tall heron.")'
VALID_ROOT = 'root = ScenePane("shop", [hobbs])'
DSL_CHANNEL = f"{VALID_HOBBS}\n{VALID_ROOT}"
SHOP_PANEL = 'status = Custom("SHOP STATUS", "<ul><li>Cash: $4,250</li></ul>")'
LONG_PANEL = 'status = Custom("SHOP STATUS", "' + "<li>Cash: $4,250</li>" * 12 + '")'

#: A draft whose DSL names a component the spec table does not have: the gate
#: keeps the statement verbatim and flags it, so the loop engages on the DSL
#: ALONE (the prose is clean).
DSL_BROKEN_SEGMENTS = [
    {"kind": "prose", "text": DSL_PROSE},
    {"kind": "dsl", "text": f'hobbs = Nope("Hobbs")\n{VALID_ROOT}'},
]

#: The same channel plus ONE extra broken statement — the delete target.
DSL_EXTRA_SEGMENTS = [
    {"kind": "prose", "text": DSL_PROSE},
    {"kind": "dsl", "text": f'{DSL_CHANNEL}\nextra = Nope("Ghost")'},
]

#: A valid channel with two statements sharing a name (the ambiguity case); the
#: slop in the prose is what engages the loop here.
AMBIGUOUS_SEGMENTS = [
    {"kind": "prose", "text": SLOP_DRAFT},
    {
        "kind": "dsl",
        "text": 'panel = Custom("A", "<b>a</b>")\npanel = Custom("B", "<b>b</b>")\n'
        'root = ScenePane("shop", [panel])',
    },
]


def test_patch_dsl_replaces_a_statement_by_lhs_and_revalidates(tmp_path):
    """`replace` lands, and the finding it repaired leaves the residual report."""
    from fabulist.graph.validation import validate_dsl
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "dslreplace.db")
    try:
        caller = RepairCaller(
            [
                dsl_reply({"op": "replace", "lhs": "hobbs", "statement": VALID_HOBBS}),
                {"content": "Done."},
            ]
        )
        out = _repair_over(log, caller, DSL_BROKEN_SEGMENTS)

        result = _first_tool_result(log)
        assert result["ok"] is True and result["applied"] == 1
        # Both tools ride the wire on the SAME loop (a call is attributed by the
        # name the transport stamps, never by position).
        assert [
            tool["function"]["name"]
            for tool in caller.calls[1]["request_profile"]["tools"]
        ] == [PATCH_PROSE_TOOL, PATCH_DSL_TOOL]
        # The DSL findings are re-derived from the WORKING channel: the repaired
        # statement's `unknown_component` is gone, so the model stops.
        assert result["remaining_flags"] == []
        assert out.segments[1]["text"] == DSL_CHANNEL
        assert out.segments[0] == DSL_BROKEN_SEGMENTS[0]
        assert out.passes == 1
        assert validate_dsl(out.segments).ok is True
    finally:
        log.close()


def test_patch_dsl_deletes_a_statement_by_lhs(tmp_path):
    """`delete` lands and the channel keeps its remaining statements in order."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "dsldelete.db")
    try:
        caller = RepairCaller(
            [
                dsl_reply({"op": "delete", "lhs": "extra"}),
                {"content": "Done."},
            ]
        )
        out = _repair_over(log, caller, DSL_EXTRA_SEGMENTS)

        result = _first_tool_result(log)
        assert result["ok"] is True and result["applied"] == 1
        assert out.segments[1]["text"] == DSL_CHANNEL
        assert out.segments[0] == DSL_EXTRA_SEGMENTS[0]
    finally:
        log.close()


def test_patch_dsl_insert_after_lands_and_leaves_prose_flags_alone(tmp_path):
    """`insert_after` anchors on an LHS name; a prose finding is not its business."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "dslinsert.db")
    try:
        caller = RepairCaller(
            [
                dsl_reply({"op": "insert_after", "lhs": "hobbs", "statement": SHOP_PANEL}),
                {"content": "Done."},
            ]
        )
        out = _repair_over(
            log,
            caller,
            [
                {"kind": "prose", "text": SLOP_DRAFT},
                {"kind": "dsl", "text": DSL_CHANNEL},
            ],
        )

        result = _first_tool_result(log)
        assert result["ok"] is True and result["applied"] == 1
        assert out.segments[1]["text"] == f"{VALID_HOBBS}\n{SHOP_PANEL}\n{VALID_ROOT}"
        # The prose finding is still there — this tool never claims that channel.
        assert any("shivers_down" in line for line in result["remaining_flags"])
        assert out.segments[0] == {"kind": "prose", "text": SLOP_DRAFT}
    finally:
        log.close()


def test_patch_dsl_refuses_an_op_that_would_break_the_dsl(tmp_path):
    """A breaking op is rejected with the gate's parse error; channel unchanged."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "dslbreak.db")
    try:
        caller = RepairCaller(
            [
                dsl_reply(
                    {"op": "replace", "lhs": "hobbs", "statement": 'hobbs = ScenePane("shop")'}
                ),
                {"content": "Done."},
            ]
        )
        out = _repair_over(log, caller, DSL_BROKEN_SEGMENTS)

        result = _first_tool_result(log)
        assert result["ok"] is False and result["applied"] == 0
        assert "would not validate" in result["error"]
        assert "arity_mismatch" in result["error"]
        # Nothing was written: the draft is byte-identical and no pass counted.
        assert out.segments == DSL_BROKEN_SEGMENTS
        assert out.passes == 0
        # The next attempt still has real targets to aim at.
        assert [entry["lhs"] for entry in result["dsl_statements"]] == ["hobbs", "root"]
    finally:
        log.close()


def test_patch_dsl_refuses_an_ambiguous_lhs_and_retargets_by_exact_text(tmp_path):
    """Two statements sharing a name refuse + list; the exact text then lands."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "dslambiguous.db")
    try:
        caller = RepairCaller(
            [
                dsl_reply({"op": "delete", "lhs": "panel"}),
                dsl_reply({"op": "delete", "target": 'panel = Custom("A", "<b>a</b>")'}),
                {"content": "Done."},
            ]
        )
        out = _repair_over(log, caller, AMBIGUOUS_SEGMENTS)

        first = _first_tool_result(log)
        assert first["ok"] is False and first["applied"] == 0
        assert "ambiguous" in first["error"]
        assert "statements 0, 1" in first["error"]
        assert [entry["lhs"] for entry in first["dsl_statements"]] == [
            "panel",
            "panel",
            "root",
        ]

        # The retarget by EXACT statement text (whitespace-normalised) lands.
        assert out.segments[1]["text"] == (
            'panel = Custom("B", "<b>b</b>")\nroot = ScenePane("shop", [panel])'
        )
        assert out.segments[0] == AMBIGUOUS_SEGMENTS[0]
        # Only the LANDING call counts as a pass (the refusal changed nothing).
        assert out.passes == 1
    finally:
        log.close()


def test_patch_dsl_lands_valid_ops_and_rejects_invalid_ones(tmp_path):
    """The fixture-patch semantics, DSL side: a valid op never falls with a bad one."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "dslmixed.db")
    try:
        caller = RepairCaller(
            [
                dsl_reply(
                    {"op": "replace", "lhs": "hobbs", "statement": VALID_HOBBS},
                    {"op": "delete", "lhs": "ghost"},
                    {"op": "insert_after", "lhs": "hobbs", "statement": SHOP_PANEL},
                ),
                {"content": "Done."},
            ]
        )
        out = _repair_over(log, caller, DSL_BROKEN_SEGMENTS)

        result = _first_tool_result(log)
        assert result["ok"] is False
        assert result["applied"] == 2 and result["partial"] is True
        assert result["failed_index"] == 1
        assert [entry["index"] for entry in result["rejected"]] == [1]
        assert result["rejected"][0]["op"] == "delete"
        assert "no DSL statement is named 'ghost'" in result["rejected"][0]["error"]
        assert "do not re-issue them" in result["note"]
        # Both valid ops are IN the draft; the rejected one changed nothing.
        assert out.segments[1]["text"] == f"{VALID_HOBBS}\n{SHOP_PANEL}\n{VALID_ROOT}"
        assert out.segments[0] == DSL_BROKEN_SEGMENTS[0]
        assert out.passes == 1
    finally:
        log.close()


def test_patch_dsl_failure_payload_shape(tmp_path):
    """The rejection carries the statement inventory the next op must aim at."""
    from fabulist.log import EventLog
    from fabulist.graph.nodes import DSL_STATEMENT_PREVIEW_CHARS

    log = EventLog(tmp_path / "dslinventory.db")
    try:
        segments = [
            {"kind": "prose", "text": DSL_PROSE},
            {
                "kind": "dsl",
                "text": f'{LONG_PANEL}\nroot = ScenePane("shop", [status])'
                '\nextra = Nope("Ghost")',
            },
        ]
        caller = RepairCaller(
            [
                dsl_reply({"op": "replace", "lhs": "nope", "statement": VALID_HOBBS}),
                {"content": "Done."},
            ]
        )
        out = _repair_over(log, caller, segments)

        result = _first_tool_result(log)
        assert result["ok"] is False and result["applied"] == 0
        assert result["failed_index"] == 0
        assert result["rejected"][0]["op"] == "replace"
        assert "no DSL statement is named 'nope'" in result["error"]
        # What was tried, verbatim enough to re-aim.
        assert result["attempted_op"] == {
            "op": "replace",
            "lhs": "nope",
            "statement": VALID_HOBBS,
        }
        inventory = result["dsl_statements"]
        assert [entry["index"] for entry in inventory] == [0, 1, 2]
        assert [entry["lhs"] for entry in inventory] == ["status", "root", "extra"]
        assert all(set(entry) == {"index", "lhs", "statement"} for entry in inventory)
        # Long statements are CLIPPED (the `…` marker), short ones are whole.
        assert len(inventory[0]["statement"]) == DSL_STATEMENT_PREVIEW_CHARS + 1
        assert inventory[0]["statement"].endswith("…")
        assert inventory[1]["statement"] == 'root = ScenePane("shop", [status])'
        # The channel boundary, so a model mixing the two tools is told which
        # tool owns which channel.
        assert "never a find/replace inside the channel" in result["dsl_is_structured"]
        assert "patch_prose" in result["prose_is_elsewhere"]
        assert "re-issue ONE corrected op" in result["fix"]
        assert out.segments == segments
    finally:
        log.close()


def test_patch_dsl_never_touches_prose(tmp_path):
    """A landed DSL op leaves every prose byte identical."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "dslprose.db")
    try:
        caller = RepairCaller(
            [dsl_reply({"op": "delete", "lhs": "extra"}), {"content": "Done."}]
        )
        out = _repair_over(log, caller, DSL_EXTRA_SEGMENTS)

        assert _first_tool_result(log)["applied"] == 1
        assert out.segments[0] == DSL_EXTRA_SEGMENTS[0]
        assert out.segments[0]["text"] == DSL_PROSE
    finally:
        log.close()


def test_prose_text_handed_to_patch_dsl_is_refused_and_prose_is_untouched(tmp_path):
    """Prose is not a DSL statement: it is refused as malformed, never written."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "dslproseasstmt.db")
    try:
        caller = RepairCaller(
            [
                dsl_reply({"op": "replace", "lhs": "hobbs", "statement": DSL_PROSE}),
                {"content": "Done."},
            ]
        )
        out = _repair_over(log, caller, DSL_EXTRA_SEGMENTS)

        result = _first_tool_result(log)
        assert result["ok"] is False and result["applied"] == 0
        assert "malformed_statement" in result["error"]
        assert out.segments == DSL_EXTRA_SEGMENTS
    finally:
        log.close()


def test_the_flags_pair_names_patch_dsl_when_the_dsl_report_has_problems(tmp_path):
    """Steering: a DSL finding points at the tool that can repair it — and only then."""
    from fabulist.graph.nodes import _flags_payload
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "dslflagspair.db")
    try:
        caller = RepairCaller([{"content": "Done."}])
        _repair_over(log, caller, DSL_BROKEN_SEGMENTS)

        payload = json.loads(caller.calls[0]["messages"][-1]["content"])
        assert any("unknown_component" in line for line in payload["flags"])
        assert "patch_dsl" in payload["instruction"]
        assert "no `find`/`replace` edit can clear them" in payload["instruction"]

        prose_only = _flags_payload(
            [
                {
                    "rule_id": "shivers_down",
                    "check": "narrative",
                    "severity": "warn",
                    "message": "banned phrase",
                }
            ]
        )
        assert "patch_dsl" not in prose_only["instruction"]
    finally:
        log.close()


# -- issue #30: a prune-created empty pane takes a repair round ----------------
#
# The gate refuses to prune a root's LAST children (that prune is what created
# the hollow pane), so "every child was undeclared" arrives at this loop as an
# UNREPAIRED finding. These two tests pin both halves: the loop engages on it
# alone, and when the model does not repair it, the turn keeps the authored
# statement plus the finding — never an emptied `ScenePane(title, [])`.

HOLLOW_CHILD_ROOT = 'root = ScenePane("Kitchen — house business, whispered", [chline])'
HOLLOW_SEGMENTS = [
    {"kind": "prose", "text": "The kitchen narrows and the house business goes quiet."},
    {"kind": "dsl", "text": HOLLOW_CHILD_ROOT},
]


def test_a_root_whose_children_were_all_invalid_bounces_into_a_patch_dsl_round(tmp_path):
    """One repair round, engaged by the empty-after-prune finding ALONE."""
    from fabulist.graph.validation import validate_dsl
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "hollowround.db")
    try:
        caller = RepairCaller(
            [
                dsl_reply(
                    {
                        "op": "insert_after",
                        "lhs": "root",
                        "statement": 'chline = BeatCard("House business, whispered.")',
                    }
                ),
                {"content": "Done."},
            ]
        )
        out = _repair_over(log, caller, HOLLOW_SEGMENTS)

        # The loop ENGAGED (a bounce) and the finding it was steered by is the
        # gate's own hard one, not a prose flag.
        start = next(
            event.payload
            for event in log.get_events(SESSION_ID)
            if event.type == "system"
            and event.payload.get("event") == "narrate_repair_start"
        )
        assert [flag["code"] for flag in start["flags"]] == ["empty_after_prune"]
        assert "chline" in start["flags"][0]["message"]

        result = _first_tool_result(log)
        assert result["ok"] is True and result["applied"] == 1
        assert result["remaining_flags"] == []
        assert out.passes == 1
        # The declared child restored the composition; the pane was never emptied.
        assert "chline = BeatCard(" in out.segments[1]["text"]
        assert "[]" not in out.segments[1]["text"]
        assert validate_dsl(out.segments).ok is True
    finally:
        log.close()


def test_an_unrepaired_hollow_root_is_kept_with_its_finding_never_emptied(tmp_path):
    """Budget spent: kept verbatim + flagged — never a silent hollow panel."""
    from fabulist.graph.validation import dsl_error_flags, validate_dsl
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "hollowkeep.db")
    try:
        caller = RepairCaller([{"content": "Nothing to fix."}])
        out = _repair_over(log, caller, HOLLOW_SEGMENTS)

        assert out.passes == 0  # the model declined; the loop fails OPEN
        # Nothing dropped and nothing emptied: the authored statement stands.
        assert out.segments[1]["text"] == HOLLOW_CHILD_ROOT
        assert "[]" not in out.segments[1]["text"]
        # ... and the failure is on the record as a warn-severity finding, which
        # the post-commit validate node logs and badges on that segment.
        findings = validate_dsl(out.segments).errors
        assert [(f.code, f.repaired) for f in findings] == [("empty_after_prune", False)]
        assert [f["severity"] for f in dsl_error_flags(findings)] == ["warn"]
    finally:
        log.close()


# -- the validate stage's teaching notice (D-F) ------------------------------
#
# The owner ruling: the flags the validate stage finds must reach the NARRATION
# model as a system turn, at the spot the flagged turn naturally sits — so a
# flagged committed turn writes ONE ``validation_notice`` row (naming the
# narrative's seq) with the same teaching payload the repair loop reads. A clean
# turn writes none: the notice is teaching, not a tax on every turn.

MARKED_PROSE = 'Hobbs glances up.\n<div style="color:#0f0;">TERMINAL READY</div>'


def _validate_state(log, session_id: str, prose: str):
    """A committed-turn state for a direct validate-node call."""
    segments = [{"kind": "prose", "text": prose}]
    narrative = log.append_event(
        session_id, "narrative", "narrator", {"prose": prose, "segments": segments}
    )
    return {
        "session_id": session_id,
        "user_content": USER,
        "segments": segments,
        "narrative_event": {
            "id": narrative.id,
            "seq": narrative.seq,
            "payload": {"prose": prose},
        },
    }


def _notice_rows(log, session_id: str) -> list:
    return [
        event
        for event in log.get_events(session_id)
        if event.payload.get("event") == "validation_notice"
    ]


def test_a_flagged_turn_writes_one_validation_notice_with_the_flag_payload(tmp_path):
    """Pin (a): one notice row, carrying the teaching fields, targeting its turn."""
    from fabulist.graph.nodes import make_validate_node
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "notice.db")
    try:
        sid = "s-notice"
        log.append_event(sid, "user", "user", {"text": USER})
        state = _validate_state(log, sid, MARKED_PROSE)
        asyncio.run(make_validate_node(log=log)(state))

        notices = _notice_rows(log, sid)
        assert len(notices) == 1, "one row per flagged turn"
        payload = notices[0].payload
        # The notice names the narrative it sits after — that seq IS the
        # position the builder interleaves on.
        assert payload["seq"] == state["narrative_event"]["seq"]
        flags = payload["flags"]
        assert any(flag.get("check") == "format" for flag in flags)
        markup = next(flag for flag in flags if flag.get("check") == "format")
        assert markup["severity"] == "warn"
        assert "<div" in markup["anchor"]
        assert markup["fix"]
        # The audit row is still written exactly once beside it.
        audits = [
            event
            for event in log.get_events(sid)
            if event.payload.get("event") == "validator_flags"
        ]
        assert len(audits) == 1
        # The notice follows the narrative in the events read (seq order).
        rows = log.get_events(sid)
        narrative_index = next(
            index for index, event in enumerate(rows) if event.type == "narrative"
        )
        assert rows.index(notices[0]) > narrative_index
    finally:
        log.close()


def test_a_clean_turn_writes_no_validation_notice(tmp_path):
    """Pin (a), clean half: a clean turn appends none."""
    from fabulist.graph.nodes import make_validate_node
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "clean-notice.db")
    try:
        sid = "s-clean"
        log.append_event(sid, "user", "user", {"text": USER})
        state = _validate_state(log, sid, CLEAN_DRAFT)
        asyncio.run(make_validate_node(log=log)(state))

        assert _notice_rows(log, sid) == []
        assert log.get_events(sid)
    finally:
        log.close()


def test_a_notice_anchor_is_clipped_to_the_preview_length():
    """The row's anchor teaches; it is not a patch target, so it is clipped."""
    from fabulist.graph.nodes import (
        VALIDATION_NOTICE_ANCHOR_CHARS,
        _notice_flags,
    )

    long_anchor = "line of flagged prose. " * 40
    (flag,) = _notice_flags(
        [
            {
                "rule_id": "prose_markup",
                "check": "format",
                "severity": "warn",
                "anchor": long_anchor,
                "fix": "remove the markup",
            }
        ]
    )
    assert len(flag["anchor"]) == VALIDATION_NOTICE_ANCHOR_CHARS + 1  # the "…"
    assert flag["anchor"].endswith("…")
    assert flag["fix"] == "remove the markup"


def test_the_repair_loop_logs_why_the_draft_was_rejected(tmp_path):
    """Owner 2026-10-21: "I need to know why they're failing".

    The findings used to go ONLY to the model, so a draft that needed several
    rounds left no trace (the committed `dsl_validation` row describes the FINAL
    channel). One bounded row per engagement and one per patch round makes the
    loop reconstructible from the log.
    """
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "why.db")
    try:
        caller = RepairCaller(
            [
                {"content": SLOP_DRAFT},
                patch_reply(SLOP_PATCH),
                {"content": "Done."},
            ]
        )
        asyncio.run(_narrate(log, caller)(_state()))

        rows = [
            event.payload
            for event in log.get_events(SESSION_ID)
            if isinstance(event.payload, dict)
            and str(event.payload.get("event") or "").startswith("narrate_repair")
        ]
        assert rows, "no narrate_repair_* rows were written"
        start = next(row for row in rows if row["event"] == "narrate_repair_start")
        assert start["round"] == 0
        codes = [flag["code"] for flag in start["flags"]]
        assert codes, "the triggering findings must be recorded"
        assert any("shivers_down" in str(flag) for flag in start["flags"]) or codes
        patch = next(row for row in rows if row["event"] == "narrate_repair_patch")
        assert patch["tool"] in {"patch_prose", "patch_dsl"}
        assert isinstance(patch.get("applied"), int)
    finally:
        log.close()


# -- "Return thinking" on the seeded pair: the draft's own reasoning ---------
#
# DeepSeek's thinking mode validates EVERY assistant message that carries
# ``tool_calls`` — the engine's synthetic flags pair included. The truthful value
# for that pair is the reasoning the provider returned while STREAMING the draft
# the pair is nudging (the same assistant turn), never a synthesized one. It
# rides the ONE ``return_thinking`` gate the loop's own echo uses.

DRAFT_REASONING = "The slop check will fire; keep the beat, drop the cliche."


def _reasoning_repair_caller() -> RepairCaller:
    """A thinking model: reasoning + the flagged draft, then reasoning + a patch."""
    return RepairCaller(
        [
            {"reasoning": DRAFT_REASONING, "content": SLOP_DRAFT},
            {"reasoning": "Patch the flagged line.", **patch_reply(SLOP_PATCH)},
            {"reasoning": "Nothing left to patch.", "content": "Done."},
        ]
    )


def test_flagged_repair_sends_the_drafts_reasoning_on_the_seeded_pair(tmp_path):
    """AUTO: the pair's ``tool_calls`` message carries the draft's reasoning."""
    from fabulist.agents.loop import ENGINE_FLAGS_CALL_ID
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "thinking.db")
    try:
        caller = _reasoning_repair_caller()
        out = asyncio.run(_narrate(log, caller)(_state()))

        first = caller.calls[1]["messages"]
        draft_turn, nudge, result = first[-3], first[-2], first[-1]
        # BOTH assistant turns of a tools request must carry the field (live
        # 400 on DeepSeek-V4.1-Flash, issue #42): the draft gets the vendor's
        # empty value, the tool_calls turn gets the paid text.
        assert draft_turn["role"] == "assistant"
        assert draft_turn["content"] == SLOP_DRAFT
        assert draft_turn["reasoning_content"] == ""
        assert nudge["tool_calls"][0]["id"] == ENGINE_FLAGS_CALL_ID
        assert nudge["reasoning_content"] == DRAFT_REASONING
        assert result["role"] == "tool"
        assert "reasoning_content" not in result
        # The repair still lands: the draft was patched, not kept.
        assert _prose(out) == PATCHED_PROSE

        # Iteration 2's own completion carried reasoning, and its assistant
        # tool_calls message echoes that too (the loop's normal echo).
        second = caller.calls[2]["messages"]
        model_call = second[-2]
        assert model_call["role"] == "assistant"
        assert model_call["tool_calls"][0]["function"]["name"] == PATCH_PROSE_TOOL
        assert model_call["reasoning_content"] == "Patch the flagged line."
    finally:
        log.close()


def test_a_present_empty_draft_reasoning_rides_the_seeded_pair(tmp_path):
    """The provider reasoned nothing while drafting and SAID so (empty string).

    The pair is the engine continuing that draft's turn, so the field must be
    there with the provider's own (empty) value: omitting it is exactly the 400
    ("must be passed back to the API") this echo exists to prevent, and inventing
    a value is not the engine's business.
    """
    from fabulist.agents.loop import ENGINE_FLAGS_CALL_ID
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "thinking-empty.db")
    try:
        caller = RepairCaller(
            [
                {"reasoning": "", "content": SLOP_DRAFT},
                patch_reply(SLOP_PATCH),
                {"content": "Done."},
            ]
        )
        out = asyncio.run(_narrate(log, caller)(_state()))

        first = caller.calls[1]["messages"]
        draft_turn, nudge, result = first[-3], first[-2], first[-1]
        assert draft_turn["role"] == "assistant"
        assert draft_turn["content"] == SLOP_DRAFT
        assert draft_turn["reasoning_content"] == ""
        assert nudge["tool_calls"][0]["id"] == ENGINE_FLAGS_CALL_ID
        assert "reasoning_content" in nudge
        assert nudge["reasoning_content"] == ""
        assert "reasoning_content" not in result
        # The repair still lands.
        assert _prose(out) == PATCHED_PROSE
        # Iteration 2's completion carried no field, but the REQUEST already does
        # (the pair's turns): presence is all-or-nothing per request at the vendor,
        # so that turn carries the field EMPTY (filled, not invented) instead of
        # making the request MIXED (a 400 with ``tools``).
        model_call = caller.calls[2]["messages"][-2]
        assert model_call["role"] == "assistant"
        assert model_call["reasoning_content"] == ""
    finally:
        log.close()


def test_return_thinking_false_suppresses_it_on_the_seeded_pair(tmp_path):
    """Explicit OFF sends no ``reasoning_content`` anywhere, pair included."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "thinking-off.db")
    try:
        caller = RepairCaller(
            [
                {"reasoning": DRAFT_REASONING, "content": SLOP_DRAFT},
                patch_reply(SLOP_PATCH),
                {"content": "Done."},
            ]
        )
        out = asyncio.run(_narrate(log, caller, return_thinking=False)(_state()))

        for call in caller.calls:
            assert all(
                "reasoning_content" not in message for message in call["messages"]
            ), call["messages"]
        assert _prose(out) == PATCHED_PROSE
    finally:
        log.close()


def test_non_reasoning_provider_seeds_no_field_anywhere(tmp_path):
    """AUTO + a provider with no reasoning deltas = the pre-change message lists."""
    from fabulist.log import EventLog

    log = EventLog(tmp_path / "no-reasoning.db")
    try:
        caller = RepairCaller(
            [
                {"content": SLOP_DRAFT},
                patch_reply(SLOP_PATCH),
                {"content": "Done."},
            ]
        )
        asyncio.run(_narrate(log, caller)(_state()))

        first = caller.calls[1]["messages"]
        draft_turn, nudge, result = first[-3], first[-2], first[-1]
        assert draft_turn == {"role": "assistant", "content": SLOP_DRAFT}
        assert set(nudge) == {"role", "content", "tool_calls"}
        assert result["role"] == "tool"
        for call in caller.calls:
            assert all(
                "reasoning_content" not in message for message in call["messages"]
            )
    finally:
        log.close()
