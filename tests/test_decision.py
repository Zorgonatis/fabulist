"""``fabulist.decision`` — the d1-3b decision-model client (wire seam).

Exercises the client against an injected ``httpx.MockTransport`` (no live model):
the ``/v1/systemone`` request shape, the ``noul`` answer parse, the choice
answer passthrough for future gates, and the "any failure means ``None`` /
disabled" contract that keeps a missing or broken binding = feature-off.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx

from fabulist.decision import DecisionCaller


def run(coro):
    return asyncio.run(coro)


class FakeRegistry:
    """Registry-like object: ``connection_for(name)`` -> connection or raise."""

    def __init__(self, connections):
        self._connections = connections

    def connection_for(self, agent_name):
        if agent_name not in self._connections:
            raise KeyError(agent_name)
        return self._connections[agent_name]


class RaisingRegistry:
    """A registry whose resolution fails with a non-KeyError (config error)."""

    def connection_for(self, agent_name):
        raise RuntimeError("no active registry")


def _connection(base_url="http://127.0.0.1:4445/v1", model="d1-3b"):
    return SimpleNamespace(base_url=base_url, model=model)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _answers_handler(payload, seen=None):
    def handler(request):
        if seen is not None:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=payload)

    return handler


# --- resolution / disabling -------------------------------------------------


def test_missing_binding_disables_caller():
    caller = DecisionCaller(FakeRegistry({}))
    assert caller.enabled is False
    assert run(caller.verdict("state", "q")) is None
    assert run(caller.ask("state", {"x": {"type": "noul", "instructions": "q"}})) is None


def test_resolution_error_disables_caller():
    caller = DecisionCaller(RaisingRegistry())
    assert caller.enabled is False
    assert run(caller.verdict("state", "q")) is None


def test_connection_without_base_url_or_model_disables_caller():
    assert DecisionCaller(
        FakeRegistry({"decision": _connection(base_url="")})
    ).enabled is False
    assert DecisionCaller(
        FakeRegistry({"decision": _connection(model="")})
    ).enabled is False


def test_binding_resolves_to_enabled_caller():
    caller = DecisionCaller(FakeRegistry({"decision": _connection()}))
    assert caller.enabled is True


# --- request shape + noul parse ---------------------------------------------


def test_verdict_posts_systemone_and_parses_noul():
    seen = {}
    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}),
        client=_client(_answers_handler({"answers": {"verdict": {"noul": 0.87}}}, seen)),
    )
    probability = run(caller.verdict("the turn", "Does this turn fail quality checks?"))

    assert probability == 0.87
    assert seen["url"] == "http://127.0.0.1:4445/v1/systemone"
    assert seen["body"] == {
        "model": "d1-3b",
        "state": "the turn",
        "questions": {
            "verdict": {
                "type": "noul",
                "instructions": "Does this turn fail quality checks?",
            }
        },
    }


def test_endpoint_adds_v1_for_bare_base_url():
    seen = {}
    caller = DecisionCaller(
        FakeRegistry({"decision": _connection("http://127.0.0.1:4445")}),
        client=_client(_answers_handler({"answers": {"verdict": {"noul": 0.1}}}, seen)),
    )
    run(caller.verdict("s", "q"))
    assert seen["url"] == "http://127.0.0.1:4445/v1/systemone"


def test_endpoint_tolerates_trailing_slash():
    seen = {}
    caller = DecisionCaller(
        FakeRegistry({"decision": _connection("http://127.0.0.1:4445/v1/")}),
        client=_client(_answers_handler({"answers": {"verdict": {"noul": 0.1}}}, seen)),
    )
    run(caller.verdict("s", "q"))
    assert seen["url"] == "http://127.0.0.1:4445/v1/systemone"


def test_ask_returns_answers_mapping_for_choice_question():
    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}),
        client=_client(
            _answers_handler(
                {"answers": {"cat": {"choice": "pacing", "probabilities": {"pacing": 0.7}}}}
            )
        ),
    )
    answers = run(
        caller.ask(
            "state",
            {"cat": {"type": "choice", "instructions": "which?", "criteria": {}}},
        )
    )
    assert answers == {"cat": {"choice": "pacing", "probabilities": {"pacing": 0.7}}}


# --- failure -> None --------------------------------------------------------


def test_http_error_returns_none():
    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}),
        client=_client(lambda request: httpx.Response(500, text="boom")),
    )
    assert run(caller.verdict("state", "q")) is None


def test_timeout_returns_none():
    def handler(request):
        raise httpx.TimeoutException("timed out", request=request)

    caller = DecisionCaller(FakeRegistry({"decision": _connection()}), client=_client(handler))
    assert run(caller.verdict("state", "q")) is None


def test_connection_error_returns_none():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    caller = DecisionCaller(FakeRegistry({"decision": _connection()}), client=_client(handler))
    assert run(caller.verdict("state", "q")) is None


def test_missing_answer_returns_none():
    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}),
        client=_client(_answers_handler({"answers": {}})),
    )
    assert run(caller.verdict("state", "q")) is None


def test_answers_not_a_mapping_returns_none():
    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}),
        client=_client(_answers_handler({"answers": "nope"})),
    )
    assert run(caller.verdict("state", "q")) is None


def test_non_numeric_noul_returns_none():
    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}),
        client=_client(_answers_handler({"answers": {"verdict": {"noul": "high"}}})),
    )
    assert run(caller.verdict("state", "q")) is None


def test_bool_noul_is_rejected():
    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}),
        client=_client(_answers_handler({"answers": {"verdict": {"noul": True}}})),
    )
    assert run(caller.verdict("state", "q")) is None


def test_non_json_body_returns_none():
    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}),
        client=_client(lambda request: httpx.Response(200, text="<html>not json</html>")),
    )
    assert run(caller.verdict("state", "q")) is None


# --- classify_genre (the wired use) ------------------------------------------------


def _genre_client(choice, probability):
    payload = {
        "answers": {
            "genre": {
                "type": "choice",
                "choice": choice,
                "probabilities": {choice: probability},
            }
        }
    }
    return _client(_answers_handler(payload))


def test_classify_genre_returns_the_confident_choice():
    """A confident non-'other' answer is the only thing that yields a genre."""
    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}), client=_genre_client("fantasy", 0.723)
    )
    assert run(caller.classify_genre("scene")) == "fantasy"


def test_classify_genre_rejects_other_and_weak_answers():
    other = DecisionCaller(
        FakeRegistry({"decision": _connection()}), client=_genre_client("other", 0.9)
    )
    weak = DecisionCaller(
        FakeRegistry({"decision": _connection()}), client=_genre_client("fantasy", 0.31)
    )
    assert run(other.classify_genre("s")) is None
    assert run(weak.classify_genre("s")) is None


def test_classify_genre_returns_none_on_transport_error():
    def boom(request):
        raise httpx.ConnectError("router down")

    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}), client=_client(boom)
    )
    assert run(caller.classify_genre("s")) is None


# --- observer seam: the decision call becomes visible/queryable ------------


def test_observer_receives_one_record_on_success():
    """One plain dict per ask; tokens come from the response ``usage`` block."""
    seen = []
    payload = {
        "answers": {"verdict": {"noul": 0.8}},
        "usage": {"prompt_tokens": 11, "completion_tokens": 3},
    }
    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}),
        client=_client(_answers_handler(payload)),
        observer=seen.append,
    )
    result = run(caller.ask("state", {"verdict": {"type": "noul", "instructions": "q"}}))

    assert result == {"verdict": {"noul": 0.8}}
    assert len(seen) == 1
    record = seen[0]
    assert record["ok"] is True
    assert record["model"] == "d1-3b"
    assert record["questions"] == ["verdict"]
    assert record["answers"] == {"verdict": {"noul": 0.8}}
    assert record["error"] is None
    assert record["input_tokens"] == 11
    assert record["output_tokens"] == 3
    assert isinstance(record["latency_s"], float) and record["latency_s"] >= 0.0


def test_observer_tokens_absent_when_response_has_no_usage():
    """The systemone route emits no usage today -> token fields stay ``None``."""
    seen = []
    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}),
        client=_client(_answers_handler({"answers": {"verdict": {"noul": 0.3}}})),
        observer=seen.append,
    )
    run(caller.verdict("s", "q"))
    assert seen[0]["input_tokens"] is None
    assert seen[0]["output_tokens"] is None


def test_observer_receives_a_failure_record_on_http_error():
    seen = []
    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}),
        client=_client(lambda request: httpx.Response(500, text="boom")),
        observer=seen.append,
    )
    assert run(caller.verdict("state", "q")) is None

    assert len(seen) == 1
    assert seen[0]["ok"] is False
    assert seen[0]["answers"] is None
    assert seen[0]["error"] and "500" in seen[0]["error"]


def test_observer_receives_a_failure_record_on_bad_shape():
    seen = []
    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}),
        client=_client(_answers_handler({"answers": "nope"})),
        observer=seen.append,
    )
    assert run(caller.verdict("s", "q")) is None
    assert seen[0]["ok"] is False
    assert seen[0]["error"]


def test_observer_not_called_when_disabled():
    """Feature-off (no binding) = no call = no observer row."""
    seen = []
    caller = DecisionCaller(FakeRegistry({}), observer=seen.append)
    assert run(caller.verdict("s", "q")) is None
    assert seen == []


def test_raising_observer_is_swallowed():
    """An observer fault never changes the ask() result (module contract)."""

    def boom(record):
        raise RuntimeError("observer exploded")

    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}),
        client=_client(_answers_handler({"answers": {"verdict": {"noul": 0.5}}})),
        observer=boom,
    )
    assert run(caller.verdict("s", "q")) == 0.5


def test_async_observer_is_awaited():
    """A coroutine observer is awaited, not left as an un-awaited coroutine."""
    captured = []

    async def observer(record):
        await asyncio.sleep(0)
        captured.append(record)

    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}),
        client=_client(_answers_handler({"answers": {"verdict": {"noul": 0.5}}})),
        observer=observer,
    )
    assert run(caller.verdict("s", "q")) == 0.5
    assert len(captured) == 1
    assert captured[0]["ok"] is True


# --- wiring observer -> one usage row the inspector already reads ----------


def test_wired_decision_call_persists_one_usage_row(log):
    """End to end: wired caller + log => exactly one aggregatable usage row."""
    from fabulist.agents.debug import CURRENT_SESSION
    from fabulist.usage.track import usage_from_log
    from fabulist.wiring import _decision_observer

    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}),
        client=_client(
            _answers_handler(
                {
                    "answers": {
                        "genre": {"choice": "fantasy", "probabilities": {"fantasy": 0.7}}
                    },
                    "usage": {"prompt_tokens": 12, "completion_tokens": 4},
                }
            )
        ),
        observer=_decision_observer(log),
    )
    CURRENT_SESSION.set("sess-2")
    try:
        genre = run(caller.classify_genre("scene"))
    finally:
        CURRENT_SESSION.set("")

    assert genre == "fantasy"
    rows = [e for e in log.get_events("sess-2") if e.type == "system"]
    assert len(rows) == 1
    assert rows[0].actor == "decision"
    payload = rows[0].payload
    assert payload["event"] == "usage"
    assert payload["agent"] == "decision"
    assert payload["model"] == "d1-3b"
    assert payload["session_id"] == "sess-2"
    assert payload["prompt_tokens"] == 12
    assert payload["completion_tokens"] == 4
    assert payload["total_tokens"] == 16
    assert payload["latency_s"] >= 0.0
    # interrogatable detail rides the SAME row (extras pass the gate, D4)
    assert payload["ok"] is True
    assert payload["questions"] == ["genre"]
    assert payload["answers"] == {"genre": {"choice": "fantasy", "probabilities": {"fantasy": 0.7}}}
    assert payload["error"] is None
    aggregate = usage_from_log(log, "sess-2")
    assert aggregate["calls"] == 1
    assert aggregate["agents"]["decision"]["calls"] == 1
    assert aggregate["models"]["d1-3b"]["total_tokens"] == 16


def test_wired_decision_failure_is_logged_ok_false(log):
    """A failed call logs (so a silent timeout is visible), never raises."""
    from fabulist.agents.debug import CURRENT_SESSION
    from fabulist.wiring import _decision_observer

    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}),
        client=_client(lambda request: httpx.Response(500, text="boom")),
        observer=_decision_observer(log),
    )
    CURRENT_SESSION.set("sess-3")
    try:
        assert run(caller.verdict("s", "q")) is None
    finally:
        CURRENT_SESSION.set("")

    rows = [e for e in log.get_events("sess-3") if e.type == "system"]
    assert len(rows) == 1
    payload = rows[0].payload
    assert payload["ok"] is False
    assert payload["error"] and "500" in payload["error"]


def test_wired_decision_observer_skips_without_session(log):
    """No CURRENT_SESSION => no row anywhere (never write the empty session)."""
    from fabulist.agents.debug import CURRENT_SESSION
    from fabulist.usage.track import usage_from_log
    from fabulist.wiring import _decision_observer

    CURRENT_SESSION.set("")
    _decision_observer(log)({"ok": True, "model": "d1-3b", "latency_s": 0.0})

    assert usage_from_log(log, None)["calls"] == 0


def test_classify_genre_asks_the_rubric_v2_question():
    seen = {}
    payload = {
        "answers": {
            "genre": {"type": "choice", "choice": "fantasy", "probabilities": {"fantasy": 0.7}}
        }
    }
    caller = DecisionCaller(
        FakeRegistry({"decision": _connection()}),
        client=_client(_answers_handler(payload, seen)),
    )
    run(caller.classify_genre("scene"))

    question = seen["body"]["questions"]["genre"]
    assert question["type"] == "choice"
    assert set(question["criteria"]) == {"fantasy", "sci-fi", "horror", "slice-of-life", "other"}
    # the escape option is gated to a last resort (v1's failure mode was `other` absorbing uncertainty)
    assert "last resort" in question["criteria"]["other"].lower()
