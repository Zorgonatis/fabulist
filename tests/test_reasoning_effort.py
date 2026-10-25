"""Per-connection ``reasoning_effort`` (issue #26 R5).

The knob exists because of a DOCUMENTED GLM-5.3 failure mode: the model attributes
instructions to a system prompt that does not exist, at ``reasoning_effort=max`` —
which is z.ai's default — and stops doing it at ``high``/``low``. The cheapest
experiment is therefore a per-connection field, not a prompt change.

Wire contract pinned here:

* the field rides as the provider's own TOP-LEVEL body parameter, ``reasoning_effort``
  (z.ai "Chat Completion": ``enum<string>, default max``, sibling of ``temperature``),
  on both the streaming and the non-streaming request;
* UNSET sends NOTHING — no key in the body at all — so every provider that has never
  heard of the field keeps a byte-identical request;
* a value outside the documented set fails the LOAD with the vocabulary, because a
  mistyped effort must never become a mystery 400 at turn time;
* a call-site/profile value still wins (the connection's setting is the default).

Fake endpoints only — no live inference.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest

from fabulist.connections import (
    Connection,
    ConnectionConfigError,
    Registry,
    build_request,
    get_client,
    load_registry,
)
from fabulist.connections.registry import (
    CONNECTION_PATCH_FIELDS,
    REASONING_EFFORT_VALUES,
    RegistryStore,
    validate_connection_patch,
)

EXAMPLE_CONFIG = (
    Path(__file__).resolve().parents[1] / "configs" / "connections.example.json"
)

#: The documented z.ai value set, spelled out — the allowlist this repo pins. A doc
#: change upstream should fail THIS test, not silently widen what we accept.
DOCUMENTED_VALUES = {"max", "xhigh", "high", "medium", "low", "minimal", "none"}


def _write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "connections.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _minimal_config(**connection_overrides) -> dict:
    connection = {"base_url": "http://host/v1", "model": "m"}
    connection.update(connection_overrides)
    return {"connections": {"c": connection}, "agents": {"agent": "c"}}


def _registry(**connection_overrides) -> Registry:
    return Registry.from_dict(_minimal_config(**connection_overrides))


# --- schema: parse, normalize, allowlist -------------------------------------------


def test_allowlist_is_the_documented_value_set():
    assert set(REASONING_EFFORT_VALUES) == DOCUMENTED_VALUES


def test_reasoning_effort_parses_from_config(tmp_path):
    registry = load_registry(_write_config(tmp_path, _minimal_config(reasoning_effort="high")))

    assert registry.connections["c"].reasoning_effort == "high"


def test_reasoning_effort_defaults_to_unset(tmp_path):
    """Absent = None = "send nothing". This is the byte-compat default."""
    registry = load_registry(_write_config(tmp_path, _minimal_config()))
    assert registry.connections["c"].reasoning_effort is None

    explicit_null = load_registry(
        _write_config(tmp_path, _minimal_config(reasoning_effort=None))
    )
    assert explicit_null.connections["c"].reasoning_effort is None


def test_reasoning_effort_normalizes_case_and_padding(tmp_path):
    """The value space is CLOSED, so normalizing cannot change behaviour."""
    registry = load_registry(_write_config(tmp_path, _minimal_config(reasoning_effort=" High ")))

    assert registry.connections["c"].reasoning_effort == "high"


@pytest.mark.parametrize("value", ["", "  ", "rare", "maximum", "x-high", "HIGH!"])
def test_reasoning_effort_rejects_undocumented_strings(tmp_path, value):
    with pytest.raises(ConnectionConfigError) as excinfo:
        load_registry(_write_config(tmp_path, _minimal_config(reasoning_effort=value)))

    message = str(excinfo.value)
    assert "connections['c'].reasoning_effort" in message
    assert "high" in message  # the vocabulary is named, not just refused


@pytest.mark.parametrize("value", [1, 1.5, True, False, ["high"], {"effort": "high"}])
def test_reasoning_effort_rejects_non_strings(tmp_path, value):
    with pytest.raises(ConnectionConfigError) as excinfo:
        load_registry(_write_config(tmp_path, _minimal_config(reasoning_effort=value)))

    assert "connections['c'].reasoning_effort" in str(excinfo.value)


def test_reasoning_effort_is_patchable_and_null_clears():
    assert "reasoning_effort" in CONNECTION_PATCH_FIELDS

    assert validate_connection_patch("c", {"reasoning_effort": "low"}) == {
        "reasoning_effort": "low"
    }
    assert validate_connection_patch("c", {"reasoning_effort": None}) == {
        "reasoning_effort": None
    }
    with pytest.raises(ConnectionConfigError):
        validate_connection_patch("c", {"reasoning_effort": "medium-rare"})


def test_registry_store_persists_reasoning_effort_and_reloads(tmp_path):
    path = _write_config(tmp_path, _minimal_config())
    store = RegistryStore(path)

    store.update_connection("c", {"reasoning_effort": "high"})

    assert store.connection_for("agent").reasoning_effort == "high"
    # survives a fresh read of the document (the owner's edit path)
    assert RegistryStore(path).connection_for("agent").reasoning_effort == "high"


def test_add_connection_writes_reasoning_effort_only_when_set(tmp_path):
    path = tmp_path / "connections.json"
    store = RegistryStore(path)

    store.add_connection(
        "glm",
        {
            "base_url": "https://api.z.ai/api/coding/paas/v4",
            "model": "glm-5.3",
            "reasoning_effort": "high",
        },
    )
    store.add_connection("plain", {"base_url": "http://127.0.0.1:4444", "model": "m"})

    document = store.raw()["connections"]
    assert document["glm"]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in document["plain"]


def test_example_config_documents_the_field():
    """The tracked template is where a reader learns the field exists."""
    registry = load_registry(EXAMPLE_CONFIG)

    assert registry.connections["cloud"].reasoning_effort == "high"
    assert registry.connections["local"].reasoning_effort is None


# --- transport: the key is sent when set, ABSENT when unset ------------------------


def test_build_request_sends_reasoning_effort_when_set():
    kwargs = build_request(
        "agent",
        [{"role": "user", "content": "hi"}],
        registry=_registry(reasoning_effort="high"),
    )

    assert kwargs["reasoning_effort"] == "high"


def test_build_request_is_byte_identical_when_reasoning_effort_is_unset():
    """No key at all — this is what keeps every other provider unaffected."""
    kwargs = build_request(
        "agent", [{"role": "user", "content": "hi"}], registry=_registry()
    )

    assert "reasoning_effort" not in kwargs
    assert set(kwargs) == {"model", "messages", "temperature", "top_p", "max_tokens"}


def test_call_site_and_profile_still_win_over_the_connection():
    registry = _registry(reasoning_effort="high")

    call_site = build_request(
        "agent",
        [{"role": "user", "content": "hi"}],
        registry=registry,
        reasoning_effort="low",
    )
    assert call_site["reasoning_effort"] == "low"

    profiled = build_request(
        "agent",
        [{"role": "user", "content": "hi"}],
        registry=registry,
        request_profile={"reasoning_effort": "max"},
    )
    assert profiled["reasoning_effort"] == "max"


def _capturing_transport(captured: dict, *, stream: bool):
    """An httpx2 transport that records the request body and answers plausibly."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        if stream:
            events = [
                {
                    "id": "cmpl-1",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "glm-5.3",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "OK"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            ]
            body = "".join(f"data: {json.dumps(event)}\n\n" for event in events)
            body += "data: [DONE]\n\n"
            return httpx2.Response(
                200, text=body, headers={"content-type": "text/event-stream"}
            )
        return httpx2.Response(
            200,
            json={
                "id": "cmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": "glm-5.3",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )

    return httpx2.MockTransport(handler)


def _client_for(registry: Registry, captured: dict, *, stream: bool):
    connection = registry.connection_for("agent")
    http_client = httpx2.Client(
        transport=_capturing_transport(captured, stream=stream),
        base_url=connection.base_url,
    )
    return get_client("agent", registry=registry, http_client=http_client)


def test_the_wire_carries_reasoning_effort_as_a_top_level_body_field():
    """Serialized by the REAL SDK: the code path, not just the kwargs dict."""
    registry = _registry(reasoning_effort="high")
    captured: dict = {}
    client = _client_for(registry, captured, stream=False)

    request = build_request("agent", [{"role": "user", "content": "Say OK."}], registry=registry)
    client.chat.completions.create(**request)

    body = captured["body"]
    assert body["reasoning_effort"] == "high"
    assert body["model"] == "m"
    assert body["messages"] == [{"role": "user", "content": "Say OK."}]
    assert captured["url"].endswith("/chat/completions")


def test_the_streaming_wire_carries_reasoning_effort_too():
    registry = _registry(reasoning_effort="low")
    captured: dict = {}
    client = _client_for(registry, captured, stream=True)

    request = build_request("agent", [{"role": "user", "content": "hi"}], registry=registry)
    stream = client.chat.completions.create(**request, stream=True)
    for _chunk in stream:  # drain so the response is released
        pass
    stream.close()

    assert captured["body"]["reasoning_effort"] == "low"
    assert captured["body"]["stream"] is True


def test_the_wire_carries_no_reasoning_effort_key_when_unset():
    registry = _registry()
    captured: dict = {}
    client = _client_for(registry, captured, stream=False)

    request = build_request("agent", [{"role": "user", "content": "hi"}], registry=registry)
    client.chat.completions.create(**request)

    assert "reasoning_effort" not in captured["body"]


def test_a_declared_effort_never_touches_extra_body():
    """The field is a FIRST-CLASS body param; ``extra_body`` stays the owner's."""
    kwargs = build_request(
        "agent",
        [{"role": "user", "content": "hi"}],
        registry=_registry(reasoning_effort="high", extra_body={"do_sample": True}),
    )

    assert kwargs["extra_body"] == {"do_sample": True}
    assert kwargs["reasoning_effort"] == "high"


def test_connection_dataclass_default_is_unset():
    """A hand-built Connection (no config document) sends nothing either."""
    connection = Connection(name="c", base_url="http://host/v1", model="m")

    assert connection.reasoning_effort is None
