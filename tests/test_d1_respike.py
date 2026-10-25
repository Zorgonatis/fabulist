"""The d1-3b re-spike probe (scripts/spike/d1_respike.py).

Cheap, throwaway-style contract test — same pattern as
``tests/test_d1_systemone.py``: the script is loaded by path, and the HTTP layer
is exercised against a throwaway local server. It pins the *definitions* (the
re-used phrase bank, the fixture ground truth, the question shapes) and the
``run_sanity`` bookkeeping. The live answers are the spike's result, not this
test's.
"""

from __future__ import annotations

import importlib.util
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "spike" / "d1_respike.py"


def _load_module() -> ModuleType:
    spec: ModuleSpec | None = importlib.util.spec_from_file_location("d1_respike", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def spike() -> ModuleType:
    return _load_module()


def test_phrase_bank_is_reused_and_unique(spike):
    assert len(spike.SLOP_PHRASES) == 18
    rule_ids = [p.rule_id for p in spike.SLOP_PHRASES]
    assert len(set(rule_ids)) == len(rule_ids)
    slugs = [spike.slugify(p.phrase) for p in spike.SLOP_PHRASES]
    assert len(set(slugs)) == len(slugs)
    # provenance points at the engine's compiled list, not an invented one
    assert "validator" in spike.SLOP_SOURCE and "Anti-Slop" in spike.SLOP_SOURCE


def test_fixture_ground_truth(spike):
    assert spike.phrase_hits(spike.CLEAN_PROSE) == []
    slop_hits = {rule_id for rule_id, _ in spike.phrase_hits(spike.SLOP_PROSE)}
    assert slop_hits == set(spike.SLOP_FIXTURE_TRUTH["slop"])
    assert len(slop_hits) == 5
    border_hits = {rule_id for rule_id, _ in spike.phrase_hits(spike.BORDERLINE_PROSE)}
    assert border_hits == {"soft_blur"}


def test_fixtures_are_prose_length(spike):
    assert 100 <= len(spike.CLEAN_PROSE.split()) <= 200
    assert 100 <= len(spike.SLOP_PROSE.split()) <= 200
    assert 100 <= len(spike.BORDERLINE_PROSE.split()) <= 200
    for turn in (spike.STALLED_TURN, spike.ADVANCING_TURN, spike.MOOD_TURN):
        assert 100 <= len(turn.split()) <= 160


def test_map_questions_shape(spike):
    questions = spike.build_slop_map_questions()
    assert len(questions) == 18
    for phrase in spike.SLOP_PHRASES:
        qid = spike.slugify(phrase.phrase)
        question = questions[qid]
        assert question["type"] == "noul"
        assert phrase.phrase in question["instructions"]
        assert "trivial variant" in question["instructions"]


def test_single_question_carries_whole_list_and_criteria(spike):
    question = spike.build_slop_single_question()["slop_any"]
    assert question["type"] == "noul"
    assert set(question["criteria"]) == {"true", "false"}
    for phrase in spike.SLOP_PHRASES:
        assert phrase.phrase in question["instructions"]


def test_choice_criteria_gates_none_last(spike):
    criteria = spike.build_slop_choice_question()["slop_class"]["criteria"]
    assert set(criteria) == {"emotion", "physical", "voice", "movement", "none"}
    assert "last resort" in criteria["none"].lower()


def test_movement_question_has_lexical_anchors(spike):
    criteria = spike.MOVEMENT_QUESTION["criteria"]
    assert criteria["true"].startswith("Something changes")
    assert "nothing changes" in criteria["false"]


def test_build_body_shape(spike):
    body = spike.build_body("state text", {"q": {"type": "noul", "instructions": "?"}})
    assert body == {"state": "state text", "questions": {"q": {"type": "noul", "instructions": "?"}}}
    # an empty map is a valid *body*; the server rejects it (probed live)
    assert json.loads(json.dumps(spike.build_body("x", {}))) == {"state": "x", "questions": {}}


class _FakeSystemOne:
    """Throwaway server: replies 200 for the first two POSTs, 400 for the third."""

    def __init__(self):
        self.requests: list[dict] = []
        outer = self
        replies = [
            (200, {"answers": {"repair": {"noul": 0.31}}}),
            (200, {"answers": {"needs_repair": {"noul": 0.31}, "advances": {"noul": 0.71}}}),
            (400, {"error": {"message": "questions must not be empty"}}),
        ]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                outer.requests.append(json.loads(self.rfile.read(length).decode("utf-8")))
                status, body = replies.pop(0)
                payload = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # keep pytest output clean
                return

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.base_url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_run_sanity_handles_criteria_two_questions_and_empty_400(spike, capsys):
    fake = _FakeSystemOne()
    try:
        results = spike.run_sanity(fake.base_url, "d1-3b", 5.0)
    finally:
        fake.close()
    capsys.readouterr()
    assert results["noul_criteria"]["ok"] is True
    assert results["two_question"]["ids"] == ["advances", "needs_repair"]
    assert results["empty_map"]["status"] == 400
    # the router's `model` field rides every request
    assert all(r["model"] == "d1-3b" for r in fake.requests)
    assert fake.requests[-1]["questions"] == {}
