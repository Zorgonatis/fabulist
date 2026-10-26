"""Issue #44 — ONE system message in the prefix + ONE universal envelope carrier.

Owner ruling (2026-10-24, the revised shape this module pins):

* the frozen prefix is ONE ``system`` message: the compiled directive block, the
  session-static persona/cast block and the library spec, in served order,
  behind the renderer's own section divider. Stock Jinja chat templates (the
  Qwen family) ``raise_exception`` on ANY non-first system message, which is
  what killed the deployed worker at message index 1.
* the per-turn ``<preset_runtime>`` envelope is ONE **ephemeral** ``assistant``
  message injected, at assembly time, IMMEDIATELY BEFORE the final user message.
  It is never persisted, never enters the log, and never appears in a later
  turn's history — it is rebuilt from scratch every turn.
* there is **no carrier field**: no per-connection switch, no trailing-system
  variant, no assistant-prefill mode and no reply-side byte-stripping. ONE
  universal carrier.
* the envelope is never a ``system`` message and never a ``user`` message — the
  assembled order is pinned end-to-end through the live wiring builder.

The prefix stays byte-identical turn-to-turn (ADR-005: the prefix is a cache
contract).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from fabulist.log import EventLog
from fabulist.prompts import preset_store as store
from fabulist.prompts.envelope import (
    ENVELOPE_ROLE,
    insert_engine_envelope,
    render_engine_envelope,
)
from fabulist.prompts.wrapper import (
    DEF_CLOSE,
    DEF_OPEN,
    GUARD_LINE,
    PREFIX_DIVIDER,
    RUNTIME_CLOSE,
    RUNTIME_OPEN,
    fold_system_prefix,
)

ENVELOPE_MARKERS = (
    "<engine_state>",
    "<engine_memory>",
    "<engine_lane>",
    "<steering>",
    "<engine_palette>",
)


@pytest.fixture(autouse=True)
def _anti_injection_on(monkeypatch: pytest.MonkeyPatch):
    """Issue #77: these pins describe the ON-state prompt bytes.

    The guard line, both bounds and the full framing ARE the ON state; the
    gate's OFF state has its own pins in ``test_anti_injection_toggle.py``.
    Flipping the shipped default keeps the REAL persisted read in charge (no
    seam is stubbed) — a fresh log then folds ON.
    """
    monkeypatch.setattr("fabulist.state.settings.ANTI_INJECTION_DEFAULT", True)


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _turn_state(sid: str, content: str = "I look around.") -> dict:
    from fabulist.graph.types import initial_turn_state

    state = initial_turn_state(sid, content, "narrate")
    state["facts"] = "time: 09:00 | loc: the door"
    state["recall"] = "RECALL-MARKER"
    return state


def _seed_turn(log: EventLog, sid: str) -> None:
    log.append_event(sid, "user", "user", {"text": "hello"})
    log.append_event(
        sid,
        "narrative",
        "narrator",
        {"prose": "The door creaks open.", "dsl": "", "segments": []},
    )


def _builder(log: EventLog):
    from fabulist.wiring import make_narrator_message_builder

    return make_narrator_message_builder(log)


def _roles(messages: list[dict]) -> list[str]:
    return [m["role"] for m in messages]


def _final_user_index(messages: list[dict]) -> int:
    return max(i for i, m in enumerate(messages) if m["role"] == "user")


def _payload(content: object) -> str:
    """The envelope body a wrapped message carries, or ``""``."""
    text = str(content)
    if f"{RUNTIME_OPEN}\n" not in text:
        return ""
    return text.split(f"{RUNTIME_OPEN}\n", 1)[1].rsplit(f"\n{RUNTIME_CLOSE}", 1)[0]


def _seeded_log(tmp_path: Path) -> tuple[EventLog, str]:
    log = EventLog(tmp_path / "carrier.db")
    store.ensure_default_preset(log)
    sid = "s"
    log.append_event(sid, "system", "system", {"card": {"name": "Lyra"}})
    _seed_turn(log, sid)
    return log, sid


# ---------------------------------------------------------------------------
# 1. the prefix is ONE system message
# ---------------------------------------------------------------------------


def test_prefix_is_one_system_message(tmp_path: Path):
    """Directive + persona/cast block + library spec fold into ONE system turn."""
    log, sid = _seeded_log(tmp_path)
    try:
        messages = _builder(log)(_turn_state(sid))

        assert _roles(messages)[0] == "system"
        prefix = str(messages[0]["content"])
        # The guard opens the ONE prefix message; the def bound closes it.
        assert prefix.startswith(GUARD_LINE)
        assert f"{DEF_OPEN}\n" in prefix and prefix.rstrip().endswith(DEF_CLOSE)
        # Every frozen-prefix block is present, each exactly once, in served
        # order: the compiled directive block (library folded in behind its
        # section divider), then the session-static persona/cast block spliced
        # ahead of history.
        assert "You are the Content Engine" in prefix
        assert "### Character: Lyra" in prefix
        assert "# Scene Panels" in prefix
        for block in ("### Character: Lyra", "# Scene Panels"):
            assert prefix.count(block) == 1
        assert prefix.index("You are the Content Engine") < prefix.index(
            "# Scene Panels"
        )
        assert prefix.index("# Scene Panels") < prefix.index("### Character: Lyra")
        # No second system message BEFORE the player's turn: the next message is
        # history, not another system block (this is the stock-template failure).
        first_non_system = next(
            i for i, m in enumerate(messages) if m["role"] != "system"
        )
        assert first_non_system == 1
        # [system(prefix), history…, assistant(envelope), user]
        assert _roles(messages) == ["system", "assistant", "assistant", "user"]

        # The flow compiler emits the fold itself (library row -> system row),
        # behind the renderer's own section divider — no new separator invented.
        merged = fold_system_prefix(
            [
                {"role": "system", "content": "A"},
                {"role": "system", "content": "B"},
                {"role": "user", "content": "u"},
            ],
            (3, 2),
        )[0]
        assert merged[0] == {"role": "system", "content": f"A{PREFIX_DIVIDER}B"}
        assert _roles(merged) == ["system", "user"]
    finally:
        log.close()


def test_frozen_prefix_is_byte_identical_across_turns(tmp_path: Path):
    """ADR-005: the prefix never moves turn-to-turn."""
    log, sid = _seeded_log(tmp_path)
    try:
        builder = _builder(log)
        turn_one = builder(_turn_state(sid, "I look around."))
        turn_two = builder(_turn_state(sid, "I say nothing for a while."))

        assert turn_one[0] == turn_two[0]
        assert turn_one[0]["role"] == "system"
        # Per-turn data rides the ephemeral envelope: the prefix carries neither
        # turn's player text, the turn's facts, nor the composed envelope. (The
        # tag NAMES are taught inside the prefix — that is the frozen contract;
        # the tag INSTANCES and the payloads ride the envelope.)
        prefix = str(turn_one[0]["content"])
        assert "I look around." not in prefix
        assert "I say nothing for a while." not in prefix
        assert "time: 09:00" not in prefix
        assert "RECALL-MARKER" not in prefix
    finally:
        log.close()


# ---------------------------------------------------------------------------
# 2. the assembled shape: [system(prefix), history…, assistant(envelope), user]
# ---------------------------------------------------------------------------


def test_the_envelope_is_an_assistant_message_immediately_before_the_user(
    tmp_path: Path,
):
    """The ONE universal carrier: assistant role, immediately before the ask."""
    log, sid = _seeded_log(tmp_path)
    try:
        messages = _builder(log)(_turn_state(sid))

        # The last two messages are the envelope then the player's own turn.
        assert _roles(messages) == ["system", "assistant", "assistant", "user"]
        user_at = _final_user_index(messages)
        assert user_at == len(messages) - 1, "the player's turn CLOSES the list"

        envelope_message = messages[user_at - 1]
        assert envelope_message["role"] == "assistant"
        assert envelope_message["role"] == ENVELOPE_ROLE
        body = str(envelope_message["content"])
        assert body.startswith(f"{RUNTIME_OPEN}\n")
        assert body.rstrip().endswith(RUNTIME_CLOSE)
        raw = _payload(body)
        assert "<engine_state>" in raw and "RECALL-MARKER" in raw
        # Contract order inside the one envelope (state before memory before
        # steering; palette LAST).
        present = [marker for marker in ENVELOPE_MARKERS if marker in raw]
        assert present == sorted(present, key=raw.index)
        # Nothing after the player's message: no trailing tail at all.
        assert messages[user_at + 1 :] == []
    finally:
        log.close()


def test_no_system_message_after_the_first_anywhere(tmp_path: Path):
    """A stock Jinja template 500s on any non-first system message."""
    log, sid = _seeded_log(tmp_path)
    try:
        messages = _builder(log)(_turn_state(sid))
        assert [i for i, m in enumerate(messages) if m["role"] == "system"] == [0]
        assert sum(str(m["content"]).count(GUARD_LINE) for m in messages) == 1
    finally:
        log.close()


def test_the_player_message_is_bare_inside_the_def_bound(tmp_path: Path):
    """Provenance: no engine bytes inside the player's own message."""
    log, sid = _seeded_log(tmp_path)
    try:
        state = _turn_state(sid)
        state["plan"] = "PLAN (turn 1)\nSTRINGS: the door opens"
        messages = _builder(log)(state)
        user_at = _final_user_index(messages)
        content = str(messages[user_at]["content"])
        assert f"{DEF_OPEN}\n" in content and content.rstrip().endswith(DEF_CLOSE)
        assert content.index("I look around.") > content.index(DEF_OPEN)
        for marker in ("<engine_state>", "<engine_memory>", "<steering>", "<engine_"):
            assert marker not in content
        assert "RECALL-MARKER" not in content
        assert "time: 09:00" not in content
    finally:
        log.close()


def test_an_envelope_absent_turn_emits_no_envelope(tmp_path: Path):
    """Empty means absent: a lean turn has no envelope message at all."""
    log = EventLog(tmp_path / "lean.db")
    try:
        sid = "s"
        lean = _turn_state(sid)
        lean.update({"facts": "", "recall": "", "plan": ""})
        messages = _builder(log)(lean)
        assert _roles(messages) == ["system", "user"]
        assert messages[-1]["role"] == "user"
        assert all(
            f"\n{RUNTIME_OPEN}\n" not in str(m["content"]) for m in messages
        )
    finally:
        log.close()


# ---------------------------------------------------------------------------
# 3. no persistence: the envelope never becomes a history turn
# ---------------------------------------------------------------------------


def test_the_envelope_is_never_persisted_across_turns(tmp_path: Path):
    """Turn 2's history carries ONLY the real reply — never the injected envelope."""
    log, sid = _seeded_log(tmp_path)
    try:
        builder = _builder(log)
        turn_one = builder(_turn_state(sid, "First ask."))
        envelope_one = _payload(
            str(messages_content(turn_one, at=len(turn_one) - 2))
        )
        assert "RECALL-MARKER" in envelope_one

        # The turn commits: one user row and ONE narrative row (the reply).
        log.append_event(sid, "user", "user", {"text": "First ask."})
        log.append_event(
            sid,
            "narrative",
            "narrator",
            {
                "prose": "The latch gives way.",
                "dsl": "",
                "reasoning_content": "she considers the door",
                "segments": [],
                "meta": {"config": {}},
            },
        )

        turn_two = builder(_turn_state(sid, "Second ask."))
        # The injected envelope is EPHEMERAL: never a log row, so it can never
        # reappear as history. Every history assistant message is a PERSISTED
        # reply, carrying no runtime envelope at all.
        user_at = _final_user_index(turn_two)
        # Everything between the prefix and the injected envelope is HISTORY.
        history_assistants = [
            str(m["content"])
            for m in turn_two[1 : user_at - 1]
            if m["role"] == "assistant"
        ]
        # The persisted replies only (both turns' prose are in the log)…
        assert len(history_assistants) == 2
        assert all("RECALL-MARKER" not in text for text in history_assistants)
        assert all(f"{RUNTIME_OPEN}\n" not in text for text in history_assistants)
        assert "The latch gives way." in history_assistants[-1]
        # …and the prior turn's envelope payload appears in exactly ONE message:
        # THIS turn's own envelope, rebuilt, sitting immediately before the ask.
        carrying = [
            m for m in turn_two if "RECALL-MARKER" in str(m.get("content", ""))
        ]
        assert len(carrying) == 1
        assert carrying[0]["role"] == "assistant"
        assert carrying[0] is turn_two[user_at - 1]
        # And the log holds no envelope row at all.
        for event in log.get_events(sid):
            payload = json.dumps(event.payload if hasattr(event, "payload") else {})
            assert RUNTIME_OPEN not in payload
    finally:
        log.close()


def messages_content(messages: list[dict], *, at: int) -> object:
    return messages[at]["content"]


# ---------------------------------------------------------------------------
# 4. mechanical edges at the assembly seam
# ---------------------------------------------------------------------------


def test_consecutive_assistant_rows_assemble_in_order(tmp_path: Path):
    """Prev reply (assistant) -> injected envelope (assistant) -> user."""
    log, sid = _seeded_log(tmp_path)
    try:
        messages = _builder(log)(_turn_state(sid))
        roles = _roles(messages)
        # History's last row is the prior REPLY; the envelope follows it directly.
        envelope_at = len(messages) - 2
        assert roles[envelope_at - 1] == "assistant"
        assert roles[envelope_at] == "assistant"
        assert roles[envelope_at + 1] == "user"
        assert f"{RUNTIME_OPEN}\n" in str(messages[envelope_at]["content"])
        assert _payload(str(messages[envelope_at - 1]["content"])) == ""
    finally:
        log.close()


def test_history_ending_on_a_user_row_still_assembles_envelope_then_user(
    tmp_path: Path,
):
    """A deleted reply leaves history ending on the player's row (edge case 5)."""
    log = EventLog(tmp_path / "deleted-tail.db")
    try:
        store.ensure_default_preset(log)
        sid = "s"
        log.append_event(sid, "system", "system", {"card": {"name": "Lyra"}})
        # Two user rows, no reply between them: the prior turn's answer is gone.
        log.append_event(sid, "user", "user", {"text": "hello"})
        log.append_event(sid, "user", "user", {"text": "hello again"})

        state = _turn_state(sid, "Third ask.")
        messages = _builder(log)(state)
        user_at = _final_user_index(messages)
        # Assembled order still ends [assistant(envelope), user].
        assert messages[user_at]["role"] == "user"
        assert "Third ask." in str(messages[user_at]["content"])
        assert messages[user_at - 1]["role"] == "assistant"
        assert _payload(str(messages[user_at - 1]["content"]))
        # And history's deleted-tail user row sits before the envelope.
        assert messages[user_at - 2]["role"] == "user"
        assert [i for i, m in enumerate(messages) if m["role"] == "system"] == [0]
    finally:
        log.close()


def test_insert_engine_envelope_places_the_message_positionally():
    """The helper is positional (``def_end - 1``), never a role scan."""
    messages = [
        {"role": "system", "content": "PREFIX"},
        {"role": "user", "content": "history user row"},
        {"role": "user", "content": "the actual ask"},
    ]
    out, def_end, envelope_at = insert_engine_envelope(messages, 3, "ENVELOPE")
    assert [m["role"] for m in out] == ["system", "user", "assistant", "user"]
    assert out[envelope_at] == {"role": "assistant", "content": "ENVELOPE"}
    assert out[def_end - 1] == {"role": "user", "content": "the actual ask"}
    # The input is never mutated.
    assert messages[2] == {"role": "user", "content": "the actual ask"}

    # A blank envelope means NO message at all (never an empty block).
    lean, lean_def_end, lean_at = insert_engine_envelope(messages, 3, None)
    assert lean == messages and lean_at is None and lean_def_end == 3
    assert insert_engine_envelope(messages, 3, "   ")[2] is None

    # A list with no final user message takes the envelope at the END — still an
    # assistant message, never a system one.
    no_user = [{"role": "system", "content": "PREFIX"}]
    tail_out, _end, tail_at = insert_engine_envelope(no_user, len(no_user), "E")
    assert tail_out[tail_at]["role"] == "assistant"
    assert tail_at == len(tail_out) - 1


# ---------------------------------------------------------------------------
# 5. the envelope is never in player OR system scope
# ---------------------------------------------------------------------------


def test_the_envelope_never_rides_a_user_or_system_message(tmp_path: Path):
    from fabulist.prompts.envelope import (
        assert_envelope_not_in_system_scope,
        assert_envelope_not_in_user_scope,
    )

    log, sid = _seeded_log(tmp_path)
    try:
        messages = _builder(log)(_turn_state(sid))
        user_at = _final_user_index(messages)
        raw = _payload(str(messages[user_at - 1]["content"]))
        assert raw and "RECALL-MARKER" in raw
        for message in messages:
            content = str(message.get("content", ""))
            if message["role"] == "user":
                assert "RECALL-MARKER" not in content and raw not in content
            if message["role"] == "system":
                assert raw not in content
                assert "RECALL-MARKER" not in content

        # The seam itself refuses a violation (belt + braces: a bug is loud).
        assert_envelope_not_in_user_scope(
            [{"role": "system", "content": "P"}, {"role": "user", "content": "clean"}],
            raw,
        )
        with pytest.raises(ValueError, match="never rides a user message"):
            assert_envelope_not_in_user_scope(
                [
                    {"role": "system", "content": "P"},
                    {"role": "user", "content": f"engine data: {raw}"},
                ],
                raw,
            )
        with pytest.raises(ValueError, match="is an assistant message"):
            assert_envelope_not_in_system_scope(
                [
                    {"role": "system", "content": "P"},
                    {"role": "system", "content": raw},
                ],
                raw,
            )
    finally:
        log.close()


# ---------------------------------------------------------------------------
# 6. the carrier field DIES — and so does the reply-stripping it needed
# ---------------------------------------------------------------------------


def test_connection_has_no_carrier_field_any_more(tmp_path: Path):
    from fabulist.connections.registry import (
        CONNECTION_PATCH_FIELDS,
        Connection,
        ConnectionConfigError,
        Registry,
        validate_connection_patch,
    )

    base = {"base_url": "http://localhost:4455/v1", "model": "m"}
    conn = Registry.from_dict(
        {"connections": {"c": base}, "agents": {"narrator": "c"}}
    ).connection_for("narrator")
    assert not hasattr(conn, "carrier")
    assert "carrier" not in CONNECTION_PATCH_FIELDS
    assert "carrier" not in {f.name for f in Connection.__dataclass_fields__.values()}
    # A PATCH naming it is refused like any other unknown field.
    with pytest.raises(ConnectionConfigError, match="not patchable"):
        validate_connection_patch("c", {"carrier": "assistant-prefill"})
    # A document that still carries the removed key loads (it is inert, not a
    # crash): the old value simply has nowhere to go.
    legacy = Registry.from_dict(
        {
            "connections": {"c": {**base, "carrier": "assistant-prefill"}},
            "agents": {"narrator": "c"},
        }
    )
    assert not hasattr(legacy.connection_for("narrator"), "carrier")


def test_carrier_vocabulary_and_reply_stripping_are_gone():
    import fabulist.prompts.envelope as envelope

    for name in (
        "CARRIER_MODES",
        "CARRIER_TRAILING_SYSTEM",
        "CARRIER_ASSISTANT_PREFILL",
        "DEFAULT_CARRIER",
        "apply_envelope_carrier",
        "carrier_of",
        "clean_carrier",
        "assistant_prefill",
        "strip_assistant_prefill",
    ):
        assert not hasattr(envelope, name), name
    import fabulist.wiring as wiring

    assert not hasattr(wiring, "narrator_carrier")
    import inspect

    assert "carrier_fn" not in inspect.signature(
        wiring.make_narrator_message_builder
    ).parameters
    import fabulist.graph.nodes as nodes

    assert not hasattr(nodes, "strip_assistant_prefill")
    assert not hasattr(nodes, "assistant_prefill")


def test_connection_http_surface_exposes_no_carrier(tmp_path: Path):
    """GET lists typed fields WITHOUT a carrier; PATCH refuses one (400)."""
    import httpx

    from fabulist.api import create_app
    from fabulist.api.connections_api import router
    from fabulist.api.fake import FakeTurnEngine
    from fabulist.connections.registry import RegistryStore

    config = tmp_path / "connections.json"
    config.write_text(
        json.dumps(
            {
                "connections": {
                    "local": {
                        "base_url": "http://localhost:4455/v1",
                        "model": "qwen3.5-4b",
                    }
                },
                "agents": {"narrator": "local"},
            }
        ),
        encoding="utf-8",
    )
    store_obj = RegistryStore(config, env_path=tmp_path / ".env")
    app = create_app(FakeTurnEngine(), tmp_path / "events.db")
    app.include_router(router)
    app.state.registry = store_obj

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            listed = await c.get("/api/connections")
            rejected = await c.patch(
                "/api/connections/local", json={"carrier": "assistant-prefill"}
            )
            created = await c.post(
                "/api/connections/other",
                json={
                    "base_url": "http://localhost:4455/v1",
                    "model": "m",
                    "carrier": "assistant-prefill",
                },
            )
            return listed, rejected, created

    try:
        listed, rejected, created = run(scenario())
        assert listed.status_code == 200
        assert "carrier" not in listed.json()["connections"]["local"]
        # Refused at the door: the field is unknown to the PATCH body (422 from
        # the extra="forbid" model) and to the create body alike.
        assert rejected.status_code == 422
        assert "carrier" in json.dumps(rejected.json())
        assert created.status_code == 422
    finally:
        app.state.log.close()


# ---------------------------------------------------------------------------
# 7. the narrate node parses the whole reply (no reply-side stripping)
# ---------------------------------------------------------------------------


def test_the_narrate_node_parses_the_whole_reply(tmp_path: Path):
    from fabulist.graph.nodes import make_narrator_node

    log, sid = _seeded_log(tmp_path)

    class EchoingModel:
        """Returns prose that mentions the tags — it must be parsed WHOLE."""

        def __init__(self):
            self.calls = []
            self.prose = "She says <preset_runtime> is a strange thing to say."

        async def __call__(self, messages, stream=True, request_profile=None):
            self.calls.append(messages)
            # The served list ENDS on the player's turn, not on an envelope.
            assert messages[-1]["role"] == "user"
            assert messages[-2]["role"] == "assistant"
            yield {"type": "content", "delta": self.prose}

    try:
        model = EchoingModel()
        node = make_narrator_node(model=model, build_messages=_builder(log))
        state = run(node(_turn_state(sid)))
        prose = "".join(state.get("narrative_parts") or [])
        assert prose == model.prose
    finally:
        log.close()


# ---------------------------------------------------------------------------
# 8. the anti-injection toggle gates INSTRUCTION framing, never the envelope
# ---------------------------------------------------------------------------


def test_the_toggle_gates_instruction_framing_not_the_envelope(tmp_path: Path):
    """Issue #77: guard + bounds + clause render-gate; the ENVELOPE does not.

    The envelope MESSAGE is still injected immediately before the ask in both
    states — it is the toggle's target that narrowed to instruction framing
    (guard line, both bounds, the two-bound framing paragraph, the clause).
    Under OFF the same engine blocks arrive unwrapped.
    """
    from fabulist.api.settings import SETTINGS_SESSION_ID
    from fabulist.state.settings import (
        ANTI_INJECTION_FLAG,
        SETTINGS_EVENT,
    )

    log, sid = _seeded_log(tmp_path)
    try:
        builder = _builder(log)
        on = builder(_turn_state(sid))  # module pin: the ON state

        log.append_event(
            SETTINGS_SESSION_ID,
            "system",
            "user",
            {"event": SETTINGS_EVENT, "features": {ANTI_INJECTION_FLAG: False}},
        )
        off = builder(_turn_state(sid))

        # Instruction framing is gone on the OFF prefix …
        assert "## Engine context" not in str(off[0]["content"])
        assert GUARD_LINE not in str(off[0]["content"])
        assert DEF_OPEN not in str(off[0]["content"])
        # … and intact when ON.
        assert "## Engine context" in str(on[0]["content"])
        assert str(on[0]["content"]).startswith(GUARD_LINE)
        assert f"{DEF_OPEN}\n" in str(on[0]["content"])

        # The ENVELOPE message — same role, same position, same blocks — is
        # present in both states; only its wrapper tags gate.
        user_at_off, user_at_on = _final_user_index(off), _final_user_index(on)
        assert off[user_at_off - 1]["role"] == "assistant"
        assert on[user_at_on - 1]["role"] == "assistant"
        off_tail = str(off[user_at_off - 1]["content"])
        on_tail = str(on[user_at_on - 1]["content"])
        assert RUNTIME_OPEN not in off_tail and RUNTIME_CLOSE not in off_tail
        assert off_tail.startswith(f"{RUNTIME_OPEN}\n") is False
        assert _payload(on_tail) == off_tail
        # The engine blocks themselves survive in both states.
        assert "<engine_state>" in off_tail
    finally:
        log.close()
