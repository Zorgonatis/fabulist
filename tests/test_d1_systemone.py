"""The d1-3b ``/v1/systemone`` spike probe (scripts/spike/d1_systemone.py).

The script is not an importable package member, so it is loaded by path — the
same pattern as ``tests/test_seed_defaults.py``.

What is pinned here is the script's own contract: the six probe definitions with
their question types, the request body it builds, and how it renders a
``/v1/systemone`` response. The live answers themselves are the spike's result
and are probed against :4445 by the orchestrator, not by this test.

The HTTP layer is exercised against a throwaway local server: it pins the wire
shape (path, router ``model`` field, error surfacing), not the model.
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

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "spike" / "d1_systemone.py"


def _load_probe_module() -> ModuleType:
    spec: ModuleSpec | None = importlib.util.spec_from_file_location("d1_systemone", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def probe() -> ModuleType:
    return _load_probe_module()


class _FakeSystemOne:
    """A local stand-in for llama-server: records requests, replies from a queue."""

    def __init__(self, responses):
        self.requests: list[dict] = []
        self.paths: list[str] = []
        self._responses = list(responses)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", "0"))
                outer.requests.append(json.loads(self.rfile.read(length).decode("utf-8")))
                outer.paths.append(self.path)
                status, body = outer._responses.pop(0) if outer._responses else (500, {})
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


@pytest.fixture
def fake_server():
    servers = []

    def start(responses):
        server = _FakeSystemOne(responses)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.close()


def noul_response(question_id: str, probability: float = 0.913) -> dict:
    return {
        "answers": {question_id: {"type": "noul", "noul": probability}},
        "usage": {"input_tokens": 5, "output_tokens": 0},
    }


def test_the_six_probe_names_and_types_match_the_planned_gates(probe):
    assert [(p["name"], p["question"]["type"]) for p in probe.PROBES] == [
        ("critic-verdict", "noul"),
        ("critic-category", "choice"),
        ("spotlight", "choice"),
        ("genre", "choice"),
        ("compaction", "noul"),
        ("memory-worthiness", "noul"),
    ]


def test_every_probe_carries_a_realistic_state_and_an_expectation(probe):
    for item in probe.PROBES:
        assert isinstance(item["state"], str) and len(item["state"]) > 200, item["name"]
        assert isinstance(item["question"]["instructions"], str)
        assert item["question"]["instructions"].endswith("?"), item["name"]
        # `expect` is printed for the reader and never sent to the server
        assert isinstance(item["expect"], str) and item["expect"]


def test_noul_probes_send_instructions_only(probe):
    """noul `criteria` is optional; the spike leaves it off to test the bare shape."""
    for item in probe.PROBES:
        if item["question"]["type"] == "noul":
            assert "criteria" not in item["question"], item["name"]


def test_choice_probes_declare_their_options(probe):
    by_name = {p["name"]: p["question"] for p in probe.PROBES}
    assert list(by_name["critic-category"]["criteria"]) == ["continuity", "style", "pacing", "none"]
    assert list(by_name["spotlight"]["criteria"]) == ["line_1", "line_2", "line_3", "line_4"]
    assert list(by_name["genre"]["criteria"]) == [
        "fantasy",
        "sci-fi",
        "horror",
        "slice-of-life",
        "other",
    ]
    for name in ("critic-category", "spotlight", "genre"):
        assert all(
            isinstance(description, str) and description
            for description in by_name[name]["criteria"].values()
        ), name


def test_request_body_is_state_plus_one_question_per_probe(probe):
    for item in probe.PROBES:
        body = probe.build_body(item)
        assert set(body) == {"state", "questions"}
        assert list(body["questions"]) == [item["name"]]
        assert body["questions"][item["name"]] is item["question"]
        assert body["state"] == item["state"]
        # the body must survive the JSON round trip it takes on the wire
        assert json.loads(json.dumps(body)) == body


def test_post_names_the_model_and_parses_the_answer(probe, fake_server):
    server = fake_server([(200, noul_response("critic-verdict"))])
    probe_definition = probe.PROBES[0]

    response, elapsed_ms = probe.post(
        server.base_url, "d1-3b", probe.build_body(probe_definition), timeout=10
    )

    assert server.paths == ["/v1/systemone"]
    sent = server.requests[0]
    assert sent["model"] == "d1-3b"
    assert sent["state"] == probe_definition["state"]
    assert list(sent["questions"]) == [probe_definition["name"]]
    assert response == noul_response("critic-verdict")
    assert elapsed_ms >= 0


def test_post_raises_probe_error_with_the_server_detail(probe, fake_server):
    server = fake_server([(400, {"error": {"message": '"questions" must be a non-empty object'}})])

    with pytest.raises(probe.ProbeError) as excinfo:
        probe.post(server.base_url, "d1-3b", probe.build_body(probe.PROBES[0]), timeout=10)

    message = str(excinfo.value)
    assert "HTTP 400" in message
    assert "non-empty object" in message


def test_noul_probability_is_rendered_as_the_true_decision(probe):
    assert probe.format_noul({"type": "noul", "noul": 0.913}) == "noul P(true) = 0.9130 -> true"
    assert probe.format_noul({"type": "noul", "noul": 0.087}) == "noul P(true) = 0.0870 -> false"
    assert "no `noul` field" in probe.format_noul({"type": "noul"})


def test_choice_rendering_includes_confidence_and_sorted_probabilities(probe, capsys):
    answer = {
        "type": "choice",
        "choice": "pacing",
        "probabilities": {"pacing": 0.87, "style": 0.09, "continuity": 0.03, "none": 0.01},
        "confidence": 0.84,
    }
    assert probe.format_choice(answer) == "pacing  (confidence 0.8400)"

    probe.print_probabilities(answer)
    lines = [" ".join(line.split()) for line in capsys.readouterr().out.splitlines()]
    assert lines == ["pacing 0.8700", "style 0.0900", "continuity 0.0300", "none 0.0100"]

    probe.print_probabilities({"type": "noul", "noul": 0.9})
    assert capsys.readouterr().out == ""


def test_main_runs_all_six_probes_and_reports_failure(probe, fake_server, capsys):
    server = fake_server([(501, {"error": {"message": "model does not support decisions"}})] * 6)

    exit_code = probe.main(["--base-url", server.base_url, "--model", "d1-3b"])

    out = capsys.readouterr().out
    assert exit_code == 1
    assert len(server.requests) == 6, "a failing probe must not abort the run"
    assert "[6/6] memory-worthiness" in out
    assert out.count("ERROR    : HTTP 501") == 6
    assert "6/6 probes failed" in out
    assert "output_modalities" in out, "a 501 tells the reader to check the decision metadata"


def test_main_prints_each_answer_and_latency(probe, fake_server, capsys):
    choice_response = {
        "answers": {
            "critic-category": {
                "type": "choice",
                "choice": "pacing",
                "probabilities": {"pacing": 0.91, "style": 0.06, "continuity": 0.02, "none": 0.01},
                "confidence": 0.86,
            }
        },
        "usage": {"input_tokens": 412, "output_tokens": 0},
        "model": "d1-3b",
    }
    responses = [
        (200, noul_response("critic-verdict")),
        (200, choice_response),
        (200, noul_response("spotlight")),
        (200, noul_response("genre")),
        (200, noul_response("compaction")),
        (200, noul_response("memory-worthiness")),
    ]
    server = fake_server(responses)

    exit_code = probe.main(["--base-url", server.base_url, "--model", "d1-3b"])

    out = capsys.readouterr().out
    assert exit_code == 0
    assert "answer   : noul P(true) = 0.9130 -> true" in out
    assert "answer   : pacing  (confidence 0.8600)" in out
    assert "critic-category        choice  pacing" in out
    assert "pacing                   0.9100" in out
    assert "latency  : " in out and "input_tokens=412 output_tokens=0" in out
    assert "served by: d1-3b" in out
    assert "expect   : true — the turn slips out of third-limited POV" in out
    assert "probe                  type    answer" in out
