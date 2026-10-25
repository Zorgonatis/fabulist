"""Run-loop tests with an ``httpx.MockTransport`` fake engine + player.

Fakes only: the fake engine serves the real endpoint shapes (seed / title /
agent-debug / turn SSE / events / inspect / agent-calls) and records what the
harness did. No live API or model call is made.
"""

from __future__ import annotations

import csv
import io
import json
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from scripts.spike.run import (  # noqa: E402
    build_dsl_inventory,
    build_turns_view,
    main,
    plan_turn,
    render_telemetry,
    run,
)
from scripts.spike.scenario import Beat, Player, Scenario, load_scenario  # noqa: E402

ENGINE_HOST = "engine.test"
PLAYER_HOST = "player.test"

#: A minimal scenario object for the pure rendering helpers.
_SCENARIO_STUB = Scenario(
    name="dnd-guild",
    title="Spike — Guild RPG",
    flavor="dnd-dungeonmaster",
    card=Path("card.json"),
    turns=2,
    player_model="player-test",
    player=Player(name="Ryn", persona="A curious rogue."),
    beats=(Beat(kind="in_character", content="beat"),),
)


# ---------------------------------------------------------------------------
# fake engine
# ---------------------------------------------------------------------------


class FakeEngine:
    """In-memory stand-in for the live engine API (records + serves readbacks)."""

    def __init__(self, session_id: str = "sess-1", title: str = "Guild RPG") -> None:
        self.session_id = session_id
        self.title = title
        self.seed_calls = 0
        self.renames: list[dict] = []
        self.agent_debug: bool | None = None
        self.worldinfo: list[dict] = []
        self.turns: list[tuple[str, str]] = []
        self.player_requests: list[dict] = []
        self._seq = 0
        self._users: list[dict] = []
        self._narratives: list[dict] = []
        self._agent_calls: list[dict] = []
        self._inspect: list[dict] = []

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def state(self) -> dict:
        return {"loc": "guild", "progress": len(self._narratives)}

    # -- SSE helpers -------------------------------------------------------

    @staticmethod
    def _sse(events: list[dict]) -> httpx.Response:
        chunks = []
        for i, event in enumerate(events, 1):
            chunks.append(
                f"id: {i}\nevent: {event['type']}\ndata: {json.dumps(event)}\n\n"
            )
        return httpx.Response(
            200,
            content="".join(chunks).encode("utf-8"),
            headers={"content-type": "text/event-stream"},
        )

    # -- routing -----------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == PLAYER_HOST:
            return self.player_handler(request)
        path = request.url.path
        method = request.method
        sid = self.session_id

        if path == "/api/cards/import" and method == "POST":
            self.seed_calls += 1
            self._users.append(
                {
                    "seq": self._next(),
                    "type": "user",
                    "payload": {
                        "text": f"[Imported card: {self.title}]",
                        "kind": "in_character",
                    },
                }
            )
            return httpx.Response(
                200,
                json={
                    "ok": True,
                    "card": {"name": self.title},
                    "session_id": sid,
                    "title": self.title,
                },
            )
        if path == f"/api/sessions/{sid}" and method == "PATCH":
            self.renames.append(json.loads(request.content))
            return httpx.Response(200, json={"session_id": sid, "title": self.title})
        if path == f"/api/sessions/{sid}/agent-debug" and method == "PATCH":
            self.agent_debug = json.loads(request.content)["enabled"]
            return httpx.Response(200, json={"session_id": sid, "enabled": self.agent_debug})
        if path == f"/api/sessions/{sid}/worldinfo" and method == "POST":
            body = json.loads(request.content)
            self.worldinfo.append(body)
            return httpx.Response(
                200, json={"session_id": sid, "entries": len(body.get("book", []))}
            )
        if path == f"/api/sessions/{sid}/turns" and method == "POST":
            return self._serve_turn(json.loads(request.content))
        if path == f"/api/sessions/{sid}/events" and method == "GET":
            return httpx.Response(
                200,
                json={
                    "session_id": sid,
                    "title": self.title,
                    "user_messages": list(self._users),
                    "narratives": list(self._narratives),
                    "state": self.state(),
                    "rolls": [],
                    "assets": [],
                },
            )
        if path == f"/api/sessions/{sid}/inspect/turns" and method == "GET":
            return httpx.Response(200, json={"turns": list(self._inspect), "usage": {}})
        if path == f"/api/sessions/{sid}/agent-calls" and method == "GET":
            return httpx.Response(200, json={"calls": list(self._agent_calls)})
        return httpx.Response(404, json={"detail": f"no fake route for {method} {path}"})

    def _serve_turn(self, body: dict) -> httpx.Response:
        content = body["content"]
        kind = body["kind"]
        self.turns.append((content, kind))
        user_seq = self._next()
        self._users.append(
            {"seq": user_seq, "type": "user", "payload": {"text": content, "kind": kind}}
        )
        if kind == "ooc":
            self._agent_calls.append(
                {
                    "agent": "ooc",
                    "seq": self._next(),
                    "response_text": "OOC reply from the engine.",
                    "latency_s": 0.4,
                    "finish": "stop",
                }
            )
            return self._sse(
                [
                    {"type": "content", "delta": "OOC reply from the engine.", "meta": {"channel": "prose"}},
                    {"type": "done", "meta": {"kind": "ooc"}},
                ]
            )

        narrative_seq = self._next()
        self._narratives.append(
            {
                "seq": narrative_seq,
                "type": "narrative",
                "payload": {
                    "prose": f"Prose for: {content}",
                    "dsl": 'root = ScenePane("Guild", [])',
                    "segments": [
                        {"kind": "prose", "text": f"Prose for: {content}"},
                        {"kind": "dsl", "text": 'root = ScenePane("Guild", [])'},
                    ],
                },
            }
        )
        usage_seq = self._next()
        self._agent_calls.append(
            {
                "agent": "bts",
                "seq": self._next(),
                "response_text": "[time: 08:15 | loc: guild]",
                "latency_s": 0.3,
                "finish": "stop",
            }
        )
        self._agent_calls.append(
            {
                "agent": "attribution",
                "seq": self._next(),
                "response_text": '{"speaker": "Ryn"}',
                "latency_s": 0.2,
                "finish": "stop",
            }
        )
        self._inspect.append(
            {
                "narrative_seq": narrative_seq,
                "user_seq": user_seq,
                "tokens": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "total_tokens": 150,
                },
                "calls": [
                    {
                        "agent": "narrator",
                        "model": "fake-model",
                        "seq": usage_seq,
                        "prompt_tokens": 100,
                        "completion_tokens": 50,
                        "total_tokens": 150,
                        "latency_s": 1.5,
                    }
                ],
                "validation_details": [],
                "rolls": [],
                "delta_count": 1,
                "latency_s": 1.5,
                "window": {"low": user_seq, "high": narrative_seq + 10},
            }
        )
        return self._sse(
            [
                {"type": "reasoning", "delta": "weighing the scene"},
                {"type": "content", "delta": f"Prose for: {content}", "meta": {"channel": "prose"}},
                {"type": "content", "delta": 'root = ScenePane("Guild", [])', "meta": {"channel": "dsl"}},
                {"type": "done", "meta": {"finish_reason": "stop"}},
            ]
        )

    def player_handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.player_requests.append(body)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "Player says hi."}}]},
        )


def _client(engine: FakeEngine) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(engine.handler))


# ---------------------------------------------------------------------------
# scenario builders
# ---------------------------------------------------------------------------


def _write_card(tmp_path: Path) -> None:
    (tmp_path / "card.json").write_text('{"name": "Guild RPG"}', encoding="utf-8")


def _scenario(tmp_path: Path, **overrides: Any) -> Path:
    _write_card(tmp_path)
    data: dict[str, Any] = {
        "name": "dnd-guild",
        "title": "Spike — Guild RPG",
        "flavor": "dnd-dungeonmaster",
        "card": "card.json",
        "turns": 2,
        "player_model": "player-test",
        "player": {"name": "Ryn", "persona": "A curious rogue."},
        "beats": [{"kind": "in_character", "content": "I enter the guild."}],
    }
    data.update(overrides)
    path = tmp_path / "scenario.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _connections(tmp_path: Path) -> Path:
    path = tmp_path / "connections.json"
    path.write_text(
        json.dumps(
            {
                "connections": {
                    "player-test": {"base_url": f"http://{PLAYER_HOST}/v1", "model": "fake"}
                },
                "agents": {},
            }
        ),
        encoding="utf-8",
    )
    return path


# ---------------------------------------------------------------------------
# plan precedence
# ---------------------------------------------------------------------------


def test_plan_turn_precedence(tmp_path: Path) -> None:
    path = _scenario(
        tmp_path,
        turns=4,
        beats=[
            {"kind": "in_character", "content": "beat"},
        ],
        ooc_probes=[{"at": 2, "content": "probe"}],
        player={"name": "Ryn", "persona": "x", "directives": [{"at": 3, "note": "note"}]},
    )
    scenario = load_scenario(path)
    assert plan_turn(scenario, 1).source == "beat"
    assert plan_turn(scenario, 2).source == "ooc_probe"
    assert plan_turn(scenario, 2).kind == "ooc"
    assert plan_turn(scenario, 3).source == "sim"
    assert plan_turn(scenario, 3).directive == "note"
    assert plan_turn(scenario, 4).directive is None


# ---------------------------------------------------------------------------
# happy path (beats only -> no player call)
# ---------------------------------------------------------------------------


def test_run_happy_path_exports_artifacts(tmp_path: Path) -> None:
    engine = FakeEngine()
    scenario_path = _scenario(
        tmp_path,
        turns=2,
        beats=[
            {"kind": "in_character", "content": "I enter the guild."},
            {"kind": "in_character", "content": "I greet the clerk."},
        ],
    )
    artifacts_root = tmp_path / "spikes"

    artifact_dir = run(
        scenario_path,
        artifacts_root=artifacts_root,
        http_client=_client(engine),
        out=io.StringIO(),
        timestamp="T0",
    )

    assert artifact_dir == artifacts_root / "dnd-guild-T0"
    assert engine.seed_calls == 1
    assert engine.renames == [{"title": "Spike — Guild RPG"}]
    assert engine.agent_debug is True
    assert engine.player_requests == []  # beats never touch the player model
    assert [kind for _, kind in engine.turns] == ["in_character", "in_character"]

    manifest = json.loads((artifact_dir / "manifest.json").read_text())
    assert manifest["session_id"] == engine.session_id
    assert manifest["turns_done"] == 2
    assert manifest["scenario"] == "dnd-guild"
    assert manifest["scenario_path"].endswith("scenario.yaml")
    assert manifest["started_at"] and manifest["updated_at"]

    transcript = (artifact_dir / "transcript.md").read_text()
    assert "## Turn 1 — in_character (beat)" in transcript
    assert "## Turn 2 — in_character (beat)" in transcript
    assert "I enter the guild." in transcript
    assert "```openui" in transcript
    assert 'root = ScenePane("Guild", [])' in transcript
    assert "Behind the scenes" in transcript
    assert "State deltas" in transcript

    inventory = json.loads((artifact_dir / "dsl_inventory.json").read_text())
    assert len(inventory) == 2
    assert inventory[0]["turn"] == 1
    assert inventory[1]["turn"] == 2
    assert isinstance(inventory[0]["seq"], int)
    assert inventory[0]["raw"] == 'root = ScenePane("Guild", [])'

    telemetry = (artifact_dir / "telemetry.csv").read_text()
    rows = list(csv.DictReader(io.StringIO(telemetry)))
    assert {"turn", "wall_seconds", "agent", "model", "total_tokens"} <= set(rows[0])
    assert [row["turn"] for row in rows] == ["1", "2"]
    assert rows[0]["agent"] == "narrator"
    assert rows[0]["total_tokens"] == "150"


# ---------------------------------------------------------------------------
# ooc probe override
# ---------------------------------------------------------------------------


def test_run_ooc_probe_replaces_sim_turn(tmp_path: Path) -> None:
    engine = FakeEngine()
    scenario_path = _scenario(
        tmp_path,
        turns=2,
        beats=[{"kind": "in_character", "content": "I enter."}],
        ooc_probes=[{"at": 2, "content": "OOC: where are we?"}],
    )

    artifact_dir = run(
        scenario_path,
        artifacts_root=tmp_path / "spikes",
        http_client=_client(engine),
        out=io.StringIO(),
        timestamp="T0",
    )

    assert engine.turns == [
        ("I enter.", "in_character"),
        ("OOC: where are we?", "ooc"),
    ]
    assert engine.player_requests == []  # probes never touch the player model
    transcript = (artifact_dir / "transcript.md").read_text()
    assert "## Turn 2 — ooc (ooc_probe)" in transcript
    assert "OOC reply from the engine." in transcript


def test_run_worldinfo_posted_pre_run(tmp_path: Path) -> None:
    engine = FakeEngine()
    scenario_path = _scenario(
        tmp_path,
        turns=1,
        beats=[{"kind": "in_character", "content": "I enter."}],
        worldinfo=[{"keys": ["guild"], "content": "The guild is old."}],
    )
    run(
        scenario_path,
        artifacts_root=tmp_path / "spikes",
        http_client=_client(engine),
        out=io.StringIO(),
        timestamp="T0",
    )
    assert engine.worldinfo == [
        {"book": [{"keys": ["guild"], "content": "The guild is old."}]}
    ]


# ---------------------------------------------------------------------------
# player-sim turn + directive injection
# ---------------------------------------------------------------------------


def test_run_sim_turn_calls_player_with_directive(tmp_path: Path) -> None:
    engine = FakeEngine()
    scenario_path = _scenario(
        tmp_path,
        turns=2,
        beats=[{"kind": "in_character", "content": "I enter the guild."}],
        player={
            "name": "Ryn",
            "persona": "A curious rogue.",
            "directives": [{"at": 2, "note": "Ask about the quest board."}],
        },
    )

    artifact_dir = run(
        scenario_path,
        connections_path=_connections(tmp_path),
        artifacts_root=tmp_path / "spikes",
        http_client=_client(engine),
        out=io.StringIO(),
        timestamp="T0",
    )

    assert engine.turns[1] == ("Player says hi.", "in_character")
    assert len(engine.player_requests) == 1
    body = engine.player_requests[0]
    assert body["messages"][0]["role"] == "system"
    assert "A curious rogue." in body["messages"][0]["content"]
    assert "Ask about the quest board." in body["messages"][-1]["content"]
    assert any(m["role"] == "assistant" for m in body["messages"])

    transcript = (artifact_dir / "transcript.md").read_text()
    assert "## Turn 2 — in_character (sim)" in transcript


def test_run_player_model_cli_override(tmp_path: Path) -> None:
    engine = FakeEngine()
    scenario_path = _scenario(
        tmp_path,
        turns=2,
        beats=[{"kind": "in_character", "content": "I enter."}],
    )
    conn = _connections(tmp_path)
    artifacts = io.StringIO()
    code = main(
        [
            str(scenario_path),
            "--turns",
            "2",
            "--player-model",
            "player-test",
            "--connections",
            str(conn),
            "--artifacts-dir",
            str(tmp_path / "spikes"),
        ],
        http_client=_client(engine),
        out=artifacts,
    )
    assert code == 0
    assert engine.turns[1] == ("Player says hi.", "in_character")


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


def test_resume_mid_run_reopens_session_without_reseeding(tmp_path: Path) -> None:
    engine = FakeEngine()
    scenario_path = _scenario(
        tmp_path,
        turns=2,
        beats=[
            {"kind": "in_character", "content": "I enter the guild."},
            {"kind": "in_character", "content": "I greet the clerk."},
        ],
    )
    artifacts_root = tmp_path / "spikes"
    client = _client(engine)

    first_dir = run(
        scenario_path,
        turns=1,
        artifacts_root=artifacts_root,
        http_client=client,
        out=io.StringIO(),
        timestamp="T0",
    )
    assert json.loads((first_dir / "manifest.json").read_text())["turns_done"] == 1

    # resume: reopen the session, run only turn 2
    second_dir = run(
        scenario_path,
        turns=2,
        resume=first_dir,
        artifacts_root=artifacts_root,
        http_client=client,
        out=io.StringIO(),
    )
    assert second_dir == first_dir
    assert engine.seed_calls == 1  # never reseeded
    assert [content for content, _ in engine.turns] == [
        "I enter the guild.",
        "I greet the clerk.",
    ]
    manifest = json.loads((first_dir / "manifest.json").read_text())
    assert manifest["turns_done"] == 2
    transcript = (first_dir / "transcript.md").read_text()
    assert "## Turn 1" in transcript and "## Turn 2" in transcript


# ---------------------------------------------------------------------------
# failure modes / CLI codes
# ---------------------------------------------------------------------------


def test_main_bad_scenario_returns_one(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: [unclosed", encoding="utf-8")
    assert main([str(bad)], http_client=_client(FakeEngine()), out=io.StringIO()) == 1


def test_main_turn_error_returns_one(tmp_path: Path) -> None:
    engine = FakeEngine()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/turns"):
            return httpx.Response(500, text="engine exploded")
        return engine.handler(request)

    scenario_path = _scenario(
        tmp_path,
        turns=1,
        beats=[{"kind": "in_character", "content": "I enter."}],
    )
    client = httpx.Client(transport=httpx.MockTransport(handler))
    out = io.StringIO()
    code = main(
        [str(scenario_path), "--artifacts-dir", str(tmp_path / "spikes")],
        http_client=client,
        out=out,
    )
    assert code == 1
    assert "spike aborted" in out.getvalue()


def test_build_dsl_inventory_flattens_turns() -> None:
    turns_view = [
        {"dsl_statements": [{"turn": 1, "seq": 2, "raw": "a = ScenePane()"}]},
        {
            "dsl_statements": [
                {"turn": 2, "seq": 3, "raw": "b = BeatCard()"},
                {"turn": 2, "seq": 3, "raw": "c = MeterBar()"},
            ]
        },
    ]
    inventory = build_dsl_inventory(turns_view)
    assert [item["raw"] for item in inventory] == [
        "a = ScenePane()",
        "b = BeatCard()",
        "c = MeterBar()",
    ]


def test_build_turns_view_state_delta_and_ooc_override() -> None:
    events = {
        "user_messages": [
            {"seq": 1, "type": "user", "payload": {"text": "[Imported card: X]", "kind": "in_character"}},
            {"seq": 2, "type": "user", "payload": {"text": "hello", "kind": "in_character"}},
            {"seq": 5, "type": "user", "payload": {"text": "OOC: recap", "kind": "ooc"}},
        ],
        "narratives": [
            {
                "seq": 3,
                "type": "narrative",
                "payload": {
                    "prose": "reply",
                    "segments": [{"kind": "dsl", "text": "root = ScenePane()"}],
                },
            }
        ],
    }
    inspect = {
        "turns": [
            {
                "narrative_seq": 3,
                "tokens": {},
                "calls": [],
                "validation_details": [],
                "rolls": [],
                "delta_count": 2,
                "latency_s": 1.0,
            }
        ]
    }
    agent_calls = [{"agent": "ooc", "seq": 6, "response_text": "OOC reply"}]

    turns_view = build_turns_view(
        _SCENARIO_STUB,
        events,
        inspect,
        agent_calls,
        states={1: {"a": 1}, 2: {"a": 1, "b": 2}},
        turn_seconds={1: 5.0, 2: 2.0},
    )

    assert len(turns_view) == 2
    assert turns_view[0]["kind"] == "in_character"
    assert turns_view[0]["delta"] == {"a": "->1"}
    assert turns_view[1]["kind"] == "ooc"
    assert turns_view[1]["ooc_reply"] == "OOC reply"
    assert turns_view[1]["delta"] == {"b": "->2"}
    assert build_dsl_inventory(turns_view) == [
        {"turn": 1, "seq": 3, "raw": "root = ScenePane()"}
    ]

    telemetry = render_telemetry(turns_view)
    rows = list(csv.DictReader(io.StringIO(telemetry)))
    assert [row["turn"] for row in rows] == ["1", "2"]
    assert rows[1]["agent"] == "" and rows[1]["wall_seconds"] == "2.000"


def test_resume_inherits_manifest_total_turns(tmp_path: Path) -> None:
    engine = FakeEngine()
    scenario_path = _scenario(
        tmp_path,
        turns=2,
        beats=[
            {"kind": "in_character", "content": "I enter the guild."},
            {"kind": "in_character", "content": "I greet the clerk."},
        ],
    )
    artifacts_root = tmp_path / "spikes"
    client = _client(engine)
    first_dir = run(
        scenario_path,
        turns=1,
        artifacts_root=artifacts_root,
        http_client=client,
        out=io.StringIO(),
        timestamp="T0",
    )
    # No --turns on resume: the manifest's recorded total (1) wins -> no new turns.
    run(
        scenario_path,
        resume=first_dir,
        artifacts_root=artifacts_root,
        http_client=client,
        out=io.StringIO(),
    )
    assert engine.seed_calls == 1
    assert len(engine.turns) == 1
