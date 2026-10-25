"""Player sim tests: prompt assembly, DSL stripping, directive injection, retry.

Fakes only — ``httpx.MockTransport`` stands in for the player model endpoint;
no live API or model is touched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.spike.player import (  # noqa: E402
    PLAYER_FORMAT_INSTRUCTION,
    PLAYER_MAX_TOKENS,
    PlayerConnection,
    PlayerSim,
    PlayerSimError,
    build_history,
    load_player_connection,
)

PERSONA = "Ryn is a curious rogue who asks too many questions and trusts no one."
CONNECTION = PlayerConnection(
    name="player-test",
    base_url="http://player.test/v1",
    model="fake-model",
    api_key="secret",
    timeout=30.0,
)


def _completion(text: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"role": "assistant", "content": text}}]},
    )


def _sim(handler) -> PlayerSim:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return PlayerSim(CONNECTION, name="Ryn", persona=PERSONA, client=client)


# ---------------------------------------------------------------------------
# prompt assembly
# ---------------------------------------------------------------------------


def test_system_prompt_contains_persona_and_instruction() -> None:
    sim = _sim(lambda request: _completion("hi"))
    prompt = sim.system_prompt()
    assert PERSONA in prompt
    assert "Ryn" in prompt
    assert PLAYER_FORMAT_INSTRUCTION in prompt


def test_build_messages_shape_and_directive_injection() -> None:
    sim = _sim(lambda request: _completion("hi"))
    history = [
        {"role": "user", "content": "I open the door."},
        {"role": "assistant", "content": "The tavern roars."},
    ]
    messages = sim.build_messages(history, directive="Check the quest board.")

    assert messages[0]["role"] == "system"
    assert PERSONA in messages[0]["content"]
    assert messages[1:3] == history
    assert messages[-1]["role"] == "user"
    assert "Check the quest board." in messages[-1]["content"]
    assert "Ryn" in messages[-1]["content"]


def test_build_messages_without_directive_has_no_note() -> None:
    sim = _sim(lambda request: _completion("hi"))
    messages = sim.build_messages([], directive=None)
    assert "Director note" not in messages[-1]["content"]


# ---------------------------------------------------------------------------
# transcript projection (prose only)
# ---------------------------------------------------------------------------


def test_build_history_strips_dsl_from_narratives() -> None:
    user_messages = [
        {"seq": 1, "type": "user", "payload": {"text": "I draw my sword.", "kind": "in_character"}}
    ]
    narratives = [
        {
            "seq": 2,
            "type": "narrative",
            "payload": {
                "prose": "Steel sings.\n```openui\nroot = ScenePane(\"Fight\", [])\n```\nBlood wells.",
                "dsl": 'root = ScenePane("Fight", [])',
            },
        }
    ]
    history = build_history(user_messages, narratives)
    assert history[0] == {"role": "user", "content": "I draw my sword."}
    assert history[1]["role"] == "assistant"
    assert "ScenePane" not in history[1]["content"]
    assert "```" not in history[1]["content"]
    assert "Steel sings." in history[1]["content"]
    assert "Blood wells." in history[1]["content"]


def test_build_history_strips_unfenced_statements() -> None:
    narratives = [
        {
            "seq": 1,
            "type": "narrative",
            "payload": {
                "prose": 'Prose here.\ncard = ArtifactCard("a", "b", "c")\nroot = ScenePane("S", [card])',
                "dsl": "",
            },
        }
    ]
    history = build_history([], narratives)
    assert history == [{"role": "assistant", "content": "Prose here."}]


def test_build_history_orders_by_seq_and_limits() -> None:
    users = [{"seq": 3, "type": "user", "payload": {"text": "u3"}}]
    narratives = [
        {"seq": 4, "type": "narrative", "payload": {"prose": "n4"}},
        {"seq": 2, "type": "narrative", "payload": {"prose": "n2"}},
    ]
    history = build_history(users, narratives)
    assert [m["content"] for m in history] == ["n2", "u3", "n4"]
    assert build_history(users, narratives, limit=2) == history[-2:]


def test_build_history_uses_text_when_prose_absent() -> None:
    narratives = [{"seq": 1, "type": "narrative", "payload": {"text": "fallback text"}}]
    assert build_history([], narratives) == [
        {"role": "assistant", "content": "fallback text"}
    ]


# ---------------------------------------------------------------------------
# generation (MockTransport)
# ---------------------------------------------------------------------------


def test_generate_posts_expected_body_and_returns_content() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _completion("  I push open the door.  ")

    sim = _sim(handler)
    result = sim.generate(
        [{"role": "assistant", "content": "The tavern roars."}],
        directive="Read the quest board aloud.",
    )

    assert result == "I push open the door."
    assert len(captured) == 1
    request = captured[0]
    assert str(request.url) == "http://player.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer secret"
    body = json.loads(request.content)
    assert body["model"] == "fake-model"
    assert body["temperature"] == 0.9
    assert body["max_tokens"] == PLAYER_MAX_TOKENS
    assert body["stream"] is False
    roles = [m["role"] for m in body["messages"]]
    assert roles == ["system", "assistant", "user"]
    assert "Read the quest board aloud." in body["messages"][-1]["content"]


def test_generate_retries_once_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, text="boom")
        return _completion("second try")

    sim = _sim(handler)
    assert sim.generate([]) == "second try"
    assert calls["n"] == 2


def test_generate_aborts_after_one_retry() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="always down")

    sim = _sim(handler)
    with pytest.raises(PlayerSimError, match="failed after one retry"):
        sim.generate([])
    assert calls["n"] == 2


def test_generate_aborts_on_unusable_completion() -> None:
    sim = _sim(lambda request: httpx.Response(200, json={"choices": []}))
    with pytest.raises(PlayerSimError):
        sim.generate([])


# ---------------------------------------------------------------------------
# connection resolution (read-only connections.json)
# ---------------------------------------------------------------------------


def test_load_player_connection_resolves_api_key_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {
        "connections": {
            "glm-api": {
                "base_url": "https://api.example/v4",
                "model": "glm-5.1",
                "api_key_env": "SPIKE_TEST_KEY",
                "sampling": {"temperature": 0.85, "max_tokens": 20000},
                "timeout": 600,
            }
        },
        "agents": {"narrator": "glm-api"},
    }
    path = tmp_path / "connections.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("SPIKE_TEST_KEY", "resolved-key")

    connection = load_player_connection(path, "glm-api")
    assert connection.base_url == "https://api.example/v4"
    assert connection.model == "glm-5.1"
    assert connection.api_key == "resolved-key"
    assert connection.timeout == 600.0


def test_load_player_connection_unknown_name(tmp_path: Path) -> None:
    config = {
        "connections": {"local": {"base_url": "http://x/v1", "model": "m"}},
        "agents": {},
    }
    path = tmp_path / "connections.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    with pytest.raises(PlayerSimError, match="not found"):
        load_player_connection(path, "ghost")


def test_load_player_connection_missing_file(tmp_path: Path) -> None:
    with pytest.raises(PlayerSimError, match="not found"):
        load_player_connection(tmp_path / "nope.json", "x")
