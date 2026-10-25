"""Tests for the per-agent connection registry and client factory (W0.3)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx2
import pytest

from fabulist.connections import (
    DEFAULT_CAPABILITIES,
    DEFAULT_SAMPLING,
    Connection,
    ConnectionConfigError,
    Registry,
    build_request,
    configure,
    get_client,
    load_registry,
    provider_family,
    resolve_capabilities,
)
from fabulist.connections.capabilities import (
    THINKING_DISABLE_PAYLOADS,
    thinking_disable_payload,
)
from fabulist.connections.registry import (
    CONNECTION_PATCH_FIELDS,
    RegistryStore,
    api_key_env_name,
    parse_env_text,
    validate_connection_patch,
    write_env_key,
)

EXAMPLE_CONFIG = Path(__file__).resolve().parents[1] / "configs" / "connections.example.json"


def _write_config(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "connections.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _minimal_config(**connection_overrides) -> dict:
    connection = {"base_url": "http://host/v1", "model": "m"}
    connection.update(connection_overrides)
    return {"connections": {"c": connection}, "agents": {"agent": "c"}}


# --- registry loading / validation -------------------------------------------------


def test_registry_loads_example_config():
    # The committed example is a NEUTRAL template (W3c): one local
    # OpenAI-compatible server + one cloud placeholder whose api_key_env names
    # the environment variable holding the key (never the key itself).
    registry = load_registry(EXAMPLE_CONFIG)

    assert set(registry.connections) == {"local", "cloud"}
    assert registry.connections["local"].base_url == "http://127.0.0.1:8080/v1"
    assert registry.connections["cloud"].api_key_env == "YOUR_PROVIDER_API_KEY"


def test_agent_resolves_through_mapping():
    registry = load_registry(EXAMPLE_CONFIG)

    assert registry.agents == {
        "narrator": "local",
        "extractor": "local",
        "captioner": "local",
    }
    assert registry.connection_for("narrator").name == "local"
    assert registry.connection_for("extractor").name == "local"


@pytest.mark.parametrize("missing", ["base_url", "model"])
def test_missing_required_field_names_entry_and_field(tmp_path, missing):
    config = _minimal_config()
    del config["connections"]["c"][missing]

    with pytest.raises(ConnectionConfigError) as excinfo:
        load_registry(_write_config(tmp_path, config))

    message = str(excinfo.value)
    assert "connections['c']" in message
    assert repr(missing) in message


def test_api_key_env_is_expanded_from_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MY_TEST_KEY", "secret-value")
    config = _minimal_config(api_key_env="MY_TEST_KEY")

    registry = load_registry(_write_config(tmp_path, config))

    assert registry.connections["c"].api_key == "secret-value"
    assert registry.connections["c"].api_key_env == "MY_TEST_KEY"


def test_non_object_extra_body_is_rejected(tmp_path):
    config = _minimal_config(extra_body=["not", "a", "dict"])

    with pytest.raises(ConnectionConfigError) as excinfo:
        load_registry(_write_config(tmp_path, config))

    assert "extra_body" in str(excinfo.value)
    assert "connections['c']" in str(excinfo.value)


def test_unknown_agent_names_the_agent():
    registry = Registry.from_dict(_minimal_config())

    with pytest.raises(ConnectionConfigError) as excinfo:
        registry.connection_for("ghost")

    assert "ghost" in str(excinfo.value)


def test_agent_mapping_to_unknown_connection_is_rejected():
    config = {"connections": {"c": {"base_url": "http://host/v1", "model": "m"}}, "agents": {"a": "nope"}}

    with pytest.raises(ConnectionConfigError) as excinfo:
        Registry.from_dict(config)

    assert "agents['a']" in str(excinfo.value)
    assert "'nope'" in str(excinfo.value)


# --- request building / merge order ------------------------------------------------


def test_build_request_merge_order():
    registry = Registry.from_dict(
        {
            "connections": {
                "c": {"base_url": "http://host/v1", "model": "m", "sampling": {"temperature": 0.5}}
            },
            "agents": {"agent": "c"},
        }
    )
    messages = [{"role": "user", "content": "hi"}]

    entry_only = build_request("agent", messages, registry=registry)
    assert entry_only["temperature"] == 0.5  # entry overrides default
    assert entry_only["top_p"] == DEFAULT_SAMPLING["top_p"]  # default applied when entry silent
    assert entry_only["model"] == "m"
    assert entry_only["messages"] is messages

    overridden = build_request("agent", messages, registry=registry, temperature=0.1, max_tokens=7)
    assert overridden["temperature"] == 0.1  # call-site overrides entry
    assert overridden["max_tokens"] == 7
    assert overridden["top_p"] == DEFAULT_SAMPLING["top_p"]  # untouched default survives


def test_build_request_merges_extra_body_with_overrides():
    registry = Registry.from_dict(
        {
            "connections": {
                "c": {
                    "base_url": "http://host/v1",
                    "model": "m",
                    "extra_body": {
                        "thinking": {"type": "enabled", "clear_thinking": True},
                        "reasoning_effort": "high",
                    },
                }
            },
            "agents": {"agent": "c"},
        }
    )

    kwargs = build_request(
        "agent",
        [{"role": "user", "content": "hi"}],
        registry=registry,
        extra_body={"reasoning_effort": "low"},
    )

    assert kwargs["extra_body"]["thinking"] == {"type": "enabled", "clear_thinking": True}
    assert kwargs["extra_body"]["reasoning_effort"] == "low"  # override wins


def test_build_request_omits_extra_body_when_empty(tmp_path):
    registry = Registry.from_dict(_minimal_config())

    kwargs = build_request("agent", [{"role": "user", "content": "hi"}], registry=registry)

    assert "extra_body" not in kwargs


# --- client factory ----------------------------------------------------------------


def test_get_client_uses_connection_settings():
    registry = load_registry(EXAMPLE_CONFIG)

    client = get_client("extractor", registry=registry)

    assert str(client.base_url).rstrip("/") == "http://127.0.0.1:8080/v1"
    assert client.api_key == "not-needed"  # keyless local endpoint


def test_get_client_requires_declared_key_env(tmp_path, monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    registry = Registry.from_dict(_minimal_config(api_key_env="MISSING_KEY"))

    with pytest.raises(ConnectionConfigError) as excinfo:
        get_client("agent", registry=registry)

    message = str(excinfo.value)
    assert "MISSING_KEY" in message
    assert "connections['c']" in message


def test_configure_installs_the_active_registry():
    registry = configure(EXAMPLE_CONFIG)

    assert registry.connection_for("extractor").name == "local"
    # no registry/agent passed: resolves through the installed active registry
    kwargs = build_request("extractor", [{"role": "user", "content": "hi"}])
    assert kwargs["model"] == "your-model-id"
    assert str(get_client("extractor").base_url).rstrip("/") == "http://127.0.0.1:8080/v1"


# --- non-standard passthrough through the real SDK ---------------------------------

#: A connection carrying NON-STANDARD body params (GLM-style ``thinking`` block +
#: siblings). W3c: the committed example is a neutral template now, so this
#: coverage carries its own document instead of borrowing the example's entry.
NON_STANDARD_CONFIG: dict = {
    "connections": {
        "vendor": {
            "base_url": "https://api.vendor.example/v1",
            "model": "vendor-large",
            "extra_body": {
                "thinking": {"type": "enabled", "clear_thinking": True},
                "reasoning_effort": "high",
                "do_sample": True,
            },
            "sampling": {"temperature": 0.7, "top_p": 0.95, "max_tokens": 4096},
        }
    },
    "agents": {"narrator": "vendor"},
}


def _capturing_transport(captured: dict):
    def handler(request: httpx2.Request) -> httpx2.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content.decode())
        return httpx2.Response(
            200,
            json={
                "id": "cmpl-1",
                "object": "chat.completion",
                "created": 0,
                "model": "vendor-large",
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


def test_non_standard_fields_pass_through_sdk_serialization():
    registry = Registry.from_dict(NON_STANDARD_CONFIG)
    connection = registry.connection_for("narrator")

    captured: dict = {}
    http_client = httpx2.Client(
        transport=_capturing_transport(captured), base_url=connection.base_url
    )
    client = get_client("narrator", registry=registry, http_client=http_client)

    request = build_request(
        "narrator", [{"role": "user", "content": "Say OK."}], registry=registry
    )
    response = client.chat.completions.create(**request)

    assert response.choices[0].message.content == "OK"
    assert captured["url"].endswith("/chat/completions")

    body = captured["body"]
    assert body["model"] == "vendor-large"
    assert body["thinking"] == {"type": "enabled", "clear_thinking": True}
    assert body["reasoning_effort"] == "high"
    assert body["do_sample"] is True
    assert body["temperature"] == 0.7
    assert body["max_tokens"] == 4096


def test_non_standard_body_defaults_are_overridable_per_request():
    registry = Registry.from_dict(NON_STANDARD_CONFIG)
    connection = registry.connection_for("narrator")

    captured: dict = {}
    http_client = httpx2.Client(
        transport=_capturing_transport(captured), base_url=connection.base_url
    )
    client = get_client("narrator", registry=registry, http_client=http_client)

    request = build_request(
        "narrator",
        [{"role": "user", "content": "hi"}],
        registry=registry,
        extra_body={"thinking": {"type": "disabled"}},
        temperature=0.2,
    )
    client.chat.completions.create(**request)

    body = captured["body"]
    assert body["thinking"] == {"type": "disabled"}  # override replaces nested value
    assert body["reasoning_effort"] == "high"  # sibling entry param survives
    assert body["temperature"] == 0.2


# --- provider capabilities (W8.4) ---------------------------------------------------


def test_capability_llamacpp_profile_is_measured():
    conn = Connection(name="g", base_url="http://localhost:4444/v1", model="your-model-id")

    caps = resolve_capabilities(conn)

    assert provider_family(conn) == "llamacpp"
    # Measured truth: forced tool_choice + json_schema work on llama.cpp.
    assert caps["function_calling"] is True
    assert caps["json_object"] is True
    assert caps["json_schema"] is True
    assert caps["tool_choice_forced"] is True
    assert caps["reasoning_model"] is False


def test_capability_deepseek_profile_is_measured():
    conn = Connection(
        name="ds", base_url="https://api.deepseek.com/v1", model="deepseek-reasoner"
    )

    caps = resolve_capabilities(conn)

    assert provider_family(conn) == "deepseek"
    # Measured truth: thinking mode rejects forced tool_choice and json_schema.
    assert caps["function_calling"] is True
    assert caps["json_object"] is True
    assert caps["json_schema"] is False
    assert caps["tool_choice_forced"] is False
    assert caps["reasoning_model"] is True
    assert caps["thinking_disable"] is True


def test_capability_unknown_provider_defaults_all_false():
    conn = Connection(name="x", base_url="http://example.invalid/v1", model="mystery-9b")

    assert provider_family(conn) is None
    assert resolve_capabilities(conn) == DEFAULT_CAPABILITIES
    assert set(resolve_capabilities(conn)) == set(DEFAULT_CAPABILITIES)


def test_capability_explicit_keys_override_profile():
    conn = Connection(
        name="g",
        base_url="http://localhost:4444/v1",
        model="your-model-id",
        capabilities={"json_schema": False, "tool_choice_forced": False},
    )

    caps = resolve_capabilities(conn)

    assert caps["json_schema"] is False  # explicit override wins over the profile
    assert caps["tool_choice_forced"] is False
    assert caps["function_calling"] is True  # unset keys keep the profile value


def test_capabilities_field_parses_from_config():
    registry = Registry.from_dict(
        _minimal_config(capabilities={"json_schema": True})
    )

    assert registry.connections["c"].capabilities == {"json_schema": True}


def test_non_object_capabilities_is_rejected():
    with pytest.raises(ConnectionConfigError) as excinfo:
        Registry.from_dict(_minimal_config(capabilities=["not", "a", "dict"]))

    assert "capabilities" in str(excinfo.value)


def test_non_bool_capability_value_is_rejected():
    with pytest.raises(ConnectionConfigError) as excinfo:
        Registry.from_dict(_minimal_config(capabilities={"json_schema": "yes"}))

    assert "boolean" in str(excinfo.value)


def test_capabilities_is_patchable_and_persisted(tmp_path):
    path = _write_config(tmp_path, _minimal_config())
    store = RegistryStore(path)

    store.update_connection("c", {"capabilities": {"json_schema": True}})

    assert store.connection_for("agent").capabilities == {"json_schema": True}
    # reload from disk proves the write persisted the field.
    assert RegistryStore(path).connection_for("agent").capabilities == {"json_schema": True}


def test_validate_connection_patch_rejects_non_bool_capabilities():
    with pytest.raises(ConnectionConfigError) as excinfo:
        validate_connection_patch("c", {"capabilities": {"json_schema": 1}})

    assert "boolean" in str(excinfo.value)


# --- pasted-key storage: env file + derived var name --------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("your-model-id", "FABULIST_YOUR_MODEL_ID_KEY"),
        ("local-27b", "FABULIST_LOCAL_27B_KEY"),
        ("GLM API", "FABULIST_GLM_API_KEY"),
        ("z.ai/v4", "FABULIST_Z_AI_V4_KEY"),
    ],
)
def test_api_key_env_name_derives_from_connection_name(name, expected):
    assert api_key_env_name(name) == expected


def test_parse_env_text_skips_noise_and_unquotes_values():
    text = (
        "# a comment\n"
        "\n"
        "PLAIN=value\n"
        "export EXPORTED=v2\n"
        'QUOTED="sk-abc"\n'
        r'ESCAPED="a\\b\"c"' "\n"
        "SINGLE='raw $value'\n"
        "not a pair\n"
        "=nameless\n"
        "  SPACED = padded  \n"
    )

    assert parse_env_text(text) == {
        "PLAIN": "value",
        "EXPORTED": "v2",
        "QUOTED": "sk-abc",
        "ESCAPED": 'a\\b"c',
        "SINGLE": "raw $value",
        "SPACED": "padded",
    }


def test_write_env_key_creates_private_file_and_preserves_other_lines(tmp_path):
    env_file = tmp_path / ".env"

    write_env_key(env_file, "FABULIST_C_KEY", "sk-1")
    write_env_key(env_file, "OTHER_KEY", "keep")

    assert env_file.read_text(encoding="utf-8") == "FABULIST_C_KEY=sk-1\nOTHER_KEY=keep\n"
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_write_env_key_replaces_the_matching_line_only(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# header\nFABULIST_C_KEY=old\nOTHER_KEY=keep\nFABULIST_C_KEY=dupe\n",
        encoding="utf-8",
    )

    write_env_key(env_file, "FABULIST_C_KEY", "new")

    assert env_file.read_text(encoding="utf-8") == (
        "# header\nFABULIST_C_KEY=new\nOTHER_KEY=keep\n"
    )


@pytest.mark.parametrize(
    "value",
    ["sk-plain-123", 'sk "quoted" value', "has space", "back\\slash", "hash#here"],
)
def test_write_env_key_round_trips_through_parse_env_text(tmp_path, value):
    env_file = tmp_path / ".env"

    write_env_key(env_file, "FABULIST_C_KEY", value)

    assert parse_env_text(env_file.read_text(encoding="utf-8")) == {
        "FABULIST_C_KEY": value
    }


def test_registry_store_reload_reads_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("FABULIST_C_KEY", raising=False)
    config = _write_config(tmp_path, _minimal_config(api_key_env="FABULIST_C_KEY"))
    env_file = tmp_path / ".env"
    store = RegistryStore(config, env_path=env_file)
    assert store.connection_for("agent").api_key is None

    write_env_key(env_file, "FABULIST_C_KEY", "sk-live")
    store.reload()  # no new process, no new store

    assert store.connection_for("agent").api_key == "sk-live"
    # The FILE wins over an already-set variable: it is the newest write path (a
    # pasted key), which is what makes a re-paste take effect.
    monkeypatch.setenv("FABULIST_C_KEY", "sk-manual")
    store.reload()
    assert store.connection_for("agent").api_key == "sk-live"


def test_registry_store_reload_without_env_file_keeps_process_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FABULIST_C_KEY", "sk-from-systemd")
    config = _write_config(tmp_path, _minimal_config(api_key_env="FABULIST_C_KEY"))

    store = RegistryStore(config, env_path=tmp_path / "absent.env")

    assert store.connection_for("agent").api_key == "sk-from-systemd"


# --- request profile passthrough (W8.4) ---------------------------------------------


def test_build_request_without_profile_is_byte_identical():
    registry = Registry.from_dict(_minimal_config())
    messages = [{"role": "user", "content": "hi"}]

    baseline = build_request("agent", messages, registry=registry)
    explicit_none = build_request("agent", messages, registry=registry, request_profile=None)

    assert baseline == explicit_none
    # The narrator/OOC request must never carry transport keys by accident.
    assert set(baseline) == {"model", "messages", "temperature", "top_p", "max_tokens"}
    for key in ("tools", "tool_choice", "response_format"):
        assert key not in baseline


def test_build_request_profile_does_not_leak_into_the_next_call():
    registry = Registry.from_dict(_minimal_config())
    messages = [{"role": "user", "content": "hi"}]
    baseline = build_request("agent", messages, registry=registry)

    build_request(
        "agent",
        messages,
        registry=registry,
        request_profile={"tools": [{"type": "function"}], "tool_choice": "required"},
    )

    assert build_request("agent", messages, registry=registry) == baseline


def test_build_request_carries_request_profile_transport_keys():
    registry = Registry.from_dict(_minimal_config())
    tool = {"type": "function", "function": {"name": "emit", "parameters": {}}}

    kwargs = build_request(
        "agent",
        [{"role": "user", "content": "hi"}],
        registry=registry,
        request_profile={
            "response_format": {"type": "json_object"},
            "tools": [tool],
            "tool_choice": "required",
        },
    )

    assert kwargs["response_format"] == {"type": "json_object"}
    assert kwargs["tools"] == [tool]
    assert kwargs["tool_choice"] == "required"


def test_build_request_profile_extra_body_merges_over_connection():
    registry = Registry.from_dict(
        _minimal_config(extra_body={"thinking": {"type": "enabled"}})
    )

    kwargs = build_request(
        "agent",
        [{"role": "user", "content": "hi"}],
        registry=registry,
        request_profile={"extra_body": {"thinking": {"type": "disabled"}}},
    )

    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}


# --- W9.25 typed connection fields: context_window / thinking / streaming -----------

DEEPSEEK = {"base_url": "https://api.deepseek.com/v1", "model": "deepseek-chat"}
LOCAL_QWEN = {"base_url": "http://localhost:4444/v1", "model": "qwen35-4b"}
LOCAL_OTHER = {"base_url": "http://localhost:4444/v1", "model": "your-model-id"}
UNMEASURED = {"base_url": "http://example.invalid/v1", "model": "mystery-9b"}


def _registry_for(connection: dict, **connection_overrides) -> Registry:
    entry = {**connection, **connection_overrides}
    return Registry.from_dict({"connections": {"c": entry}, "agents": {"agent": "c"}})


def test_typed_fields_parse_from_config(tmp_path):
    config = _minimal_config(context_window=65536, thinking=False, streaming=False)

    registry = load_registry(_write_config(tmp_path, config))
    connection = registry.connections["c"]

    assert connection.context_window == 65536
    assert connection.thinking is False
    assert connection.streaming is False


def test_typed_fields_default_to_none(tmp_path):
    registry = load_registry(_write_config(tmp_path, _minimal_config()))

    connection = registry.connections["c"]
    assert connection.context_window is None
    assert connection.thinking is None
    assert connection.streaming is None
    assert connection.return_thinking is None


def test_return_thinking_is_tri_state(tmp_path):
    """AUTO (absent/null), ON and OFF all parse — the reasoning echo's setting."""
    for value in (None, True, False):
        config = _minimal_config(**({} if value is None else {"return_thinking": value}))
        registry = load_registry(_write_config(tmp_path, config))
        assert registry.connections["c"].return_thinking is value


@pytest.mark.parametrize(
    "value",
    [0, -1, True, False, "65536", 65536.0, "lots"],
)
def test_context_window_rejects_anything_but_a_positive_int(tmp_path, value):
    config = _minimal_config(context_window=value)

    with pytest.raises(ConnectionConfigError) as excinfo:
        load_registry(_write_config(tmp_path, config))

    assert "connections['c'].context_window" in str(excinfo.value)


@pytest.mark.parametrize("field", ["thinking", "streaming", "return_thinking"])
@pytest.mark.parametrize("value", ["yes", "false", 1, 0, "true"])
def test_toggles_reject_truthy_strings_and_numbers(tmp_path, field, value):
    config = _minimal_config(**{field: value})

    with pytest.raises(ConnectionConfigError) as excinfo:
        load_registry(_write_config(tmp_path, config))

    assert f"connections['c'].{field}" in str(excinfo.value)


def test_typed_fields_are_patchable_and_null_clears(tmp_path):
    assert {"context_window", "thinking", "streaming", "return_thinking"} <= set(
        CONNECTION_PATCH_FIELDS
    )

    clean = validate_connection_patch(
        "c",
        {
            "context_window": 8192,
            "thinking": False,
            "streaming": None,
            "return_thinking": False,
        },
    )
    assert clean == {
        "context_window": 8192,
        "thinking": False,
        "streaming": None,
        "return_thinking": False,
    }


@pytest.mark.parametrize(
    "patch",
    [
        {"context_window": 0},
        {"context_window": True},
        {"context_window": "8192"},
        {"thinking": "yes"},
        {"streaming": 1},
        {"return_thinking": "auto"},
        {"return_thinking": 0},
    ],
)
def test_validate_connection_patch_rejects_loose_typed_values(patch):
    with pytest.raises(ConnectionConfigError):
        validate_connection_patch("c", patch)


def test_registry_store_persists_typed_fields_and_reloads(tmp_path):
    path = _write_config(tmp_path, _minimal_config())
    store = RegistryStore(path)

    store.update_connection(
        "c",
        {
            "context_window": 32768,
            "thinking": False,
            "streaming": False,
            "return_thinking": False,
        },
    )

    live = store.connection_for("agent")
    assert (live.context_window, live.thinking, live.streaming) == (32768, False, False)
    assert live.return_thinking is False
    # survives a fresh read of the document
    reloaded = RegistryStore(path).connection_for("agent")
    assert (reloaded.context_window, reloaded.thinking, reloaded.streaming) == (
        32768,
        False,
        False,
    )
    assert reloaded.return_thinking is False
    assert json.loads(path.read_text())["connections"]["c"]["context_window"] == 32768
    # null is AUTO, and AUTO is an explicit written value, not an omission
    store.update_connection("c", {"return_thinking": None})
    assert store.connection_for("agent").return_thinking is None
    assert json.loads(path.read_text())["connections"]["c"]["return_thinking"] is None


def test_undeclared_dotted_agent_key_fails_loudly_with_the_vocabulary():
    config = {"connections": {"c": {"base_url": "http://host/v1", "model": "m"}}}
    config["agents"] = {"completion.bogus": "c"}

    with pytest.raises(ConnectionConfigError) as excinfo:
        Registry.from_dict(config)

    message = str(excinfo.value)
    assert "completion.bogus" in message
    assert "declared typed slots" in message
    assert "completion.narration" in message
    assert "completion.state" in message


def test_declared_typed_slots_and_custom_undotted_keys_load():
    config = {
        "connections": {"c": {"base_url": "http://host/v1", "model": "m"}},
        "agents": {
            "completion.narration": "c",
            "completion.extraction": "c",
            "completion.state": "c",
            "embedding": "c",
            "my-own-slot": "c",
        },
    }

    registry = Registry.from_dict(config)

    assert registry.connection_for("completion.narration").name == "c"
    assert registry.connection_for("narrator").name == "c"  # legacy twin of the typed key
    assert registry.connection_for("my-own-slot").name == "c"


def test_connection_for_falls_back_to_the_type_key():
    config = {
        "connections": {"c": {"base_url": "http://host/v1", "model": "m"}},
        "agents": {"completion": "c"},
    }

    registry = Registry.from_dict(config)

    assert registry.connection_for("completion").name == "c"
    assert registry.connection_for("extractor").name == "c"  # extraction -> completion
    assert registry.connection_for("imagegen").name == "c"  # type affinity -> completion
    with pytest.raises(ConnectionConfigError):
        registry.connection_for("embedder")  # embedding type not authored


# --- W9.25 thinking translation (engine-side, per provider) ------------------------


def test_thinking_disable_payload_table_is_measured():
    assert set(THINKING_DISABLE_PAYLOADS) == {"deepseek", "llamacpp"}
    assert THINKING_DISABLE_PAYLOADS["deepseek"].payload == {
        "thinking": {"type": "disabled"}
    }
    assert THINKING_DISABLE_PAYLOADS["llamacpp"].payload == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_thinking_disable_payload_is_family_and_model_gated():
    def conn(base_url: str, model: str) -> Connection:
        return Connection(name="c", base_url=base_url, model=model)

    assert thinking_disable_payload(conn(**DEEPSEEK)) == {"thinking": {"type": "disabled"}}
    assert thinking_disable_payload(conn(**LOCAL_QWEN)) == {
        "chat_template_kwargs": {"enable_thinking": False}
    }
    # llama.cpp's switch is a chat-template variable: unmeasured model -> no-op
    assert thinking_disable_payload(conn(**LOCAL_OTHER)) is None
    assert thinking_disable_payload(conn(**UNMEASURED)) is None


def test_thinking_disable_payload_returns_a_fresh_copy():
    connection = Connection(name="c", **DEEPSEEK)

    payload = thinking_disable_payload(connection)
    payload["thinking"]["type"] = "mutated"

    assert THINKING_DISABLE_PAYLOADS["deepseek"].payload == {
        "thinking": {"type": "disabled"}
    }


def test_thinking_off_overrides_the_authored_thinking_extra_body():
    registry = _registry_for(
        DEEPSEEK, extra_body={"thinking": {"type": "enabled"}, "top_k": 20}, thinking=False
    )

    kwargs = build_request("agent", [{"role": "user", "content": "hi"}], registry=registry)

    assert kwargs["extra_body"]["thinking"] == {"type": "disabled"}
    assert kwargs["extra_body"]["top_k"] == 20  # other authored params survive


def test_thinking_off_adds_the_llamacpp_qwen_chat_template_kwarg():
    registry = _registry_for(LOCAL_QWEN, thinking=False)

    kwargs = build_request("agent", [{"role": "user", "content": "hi"}], registry=registry)

    assert kwargs["extra_body"] == {
        "chat_template_kwargs": {"enable_thinking": False}
    }


def test_thinking_off_is_a_noop_for_an_unmeasured_family():
    off = build_request(
        "agent",
        [{"role": "user", "content": "hi"}],
        registry=_registry_for(UNMEASURED, thinking=False),
    )
    default = build_request(
        "agent", [{"role": "user", "content": "hi"}], registry=_registry_for(UNMEASURED)
    )

    assert off == default
    assert "extra_body" not in off


@pytest.mark.parametrize("thinking", [None, True])
def test_thinking_on_is_byte_identical_to_the_field_being_absent(thinking):
    """Owner ruling: thinking ON is the default — the request must not change."""
    authored = {"thinking": {"type": "enabled"}}
    off_request = build_request(
        "agent",
        [{"role": "user", "content": "hi"}],
        registry=_registry_for(DEEPSEEK, extra_body=dict(authored), thinking=thinking),
    )
    bare_request = build_request(
        "agent",
        [{"role": "user", "content": "hi"}],
        registry=_registry_for(DEEPSEEK, extra_body=dict(authored)),
    )

    assert off_request == bare_request
    assert off_request["extra_body"] == authored


def test_call_site_and_profile_extras_win_over_the_thinking_payload():
    registry = _registry_for(DEEPSEEK, thinking=False)

    kwargs = build_request(
        "agent",
        [{"role": "user", "content": "hi"}],
        registry=registry,
        request_profile={"extra_body": {"thinking": {"type": "enabled"}}},
        extra_body={"thinking": {"type": "enabled"}},
    )

    assert kwargs["extra_body"]["thinking"] == {"type": "enabled"}


@pytest.mark.parametrize(
    "entry, expected",
    [
        (
            {**DEEPSEEK, "api_key_env": "DS_KEY", "thinking": False},
            {"thinking": {"type": "disabled"}},
        ),
        (
            {**LOCAL_QWEN, "thinking": False},
            {"chat_template_kwargs": {"enable_thinking": False}},
        ),
    ],
)
def test_thinking_off_payload_reaches_the_wire(monkeypatch, entry, expected):
    """Not just a merged dict: the real SDK serializes the payload into the body."""
    monkeypatch.setenv("DS_KEY", "test-key")
    registry = Registry.from_dict(
        {"connections": {"c": entry}, "agents": {"agent": "c"}}
    )

    captured: dict = {}
    http_client = httpx2.Client(
        transport=_capturing_transport(captured), base_url=entry["base_url"]
    )
    client = get_client("agent", registry=registry, http_client=http_client)
    client.chat.completions.create(
        **build_request("agent", [{"role": "user", "content": "hi"}], registry=registry)
    )

    body = captured["body"]
    for key, value in expected.items():
        assert body[key] == value


def test_thinking_payload_does_not_leak_between_calls():
    registry = _registry_for(DEEPSEEK, thinking=False)
    on_registry = _registry_for(DEEPSEEK)

    build_request("agent", [{"role": "user", "content": "hi"}], registry=registry)
    kwargs = build_request(
        "agent", [{"role": "user", "content": "hi"}], registry=on_registry
    )

    assert "extra_body" not in kwargs


def test_base_url_normalization_bare_host_and_explicit_paths():
    """Owner ruling 2026-10-23: a bare host:port becomes http://…/v1, an
    explicit path is preserved (z.ai /api/paas/v4, OpenRouter /api/v1)."""
    from fabulist.connections.registry import Registry

    def norm(url: str) -> str:
        reg = Registry.from_dict(
            {"connections": {"c": {"base_url": url, "model": "m"}}, "agents": {}}
        )
        return reg.connections["c"].base_url

    # bare host:port (the drawer hint shape) → scheme + /v1
    assert norm("0.0.0.0:8080") == "http://0.0.0.0:8080/v1"
    assert norm("http://127.0.0.1:8080") == "http://127.0.0.1:8080/v1"
    # explicit path preserved, trailing slash stripped
    assert norm("http://localhost:4444/v1/") == "http://localhost:4444/v1"
    assert norm("https://api.z.ai/api/paas/v4") == "https://api.z.ai/api/paas/v4"
    assert norm("https://openrouter.ai/api/v1") == "https://openrouter.ai/api/v1"


def test_env_file_path_can_be_pinned_by_environment(monkeypatch, tmp_path):
    """FABULIST_ENV_FILE moves the dotenv pasted keys land in.

    Why it exists: in the container the working directory is the image, not a
    volume, so a key written to /app/.env would vanish on recreate. The image
    pins this to the mounted configs volume instead.
    """
    from fabulist.connections.registry import RegistryStore

    target = tmp_path / "configs" / ".env"
    monkeypatch.setenv("FABULIST_ENV_FILE", str(target))
    store = RegistryStore(tmp_path / "connections.json")
    assert store.env_path == target

    monkeypatch.delenv("FABULIST_ENV_FILE")
    fallback = RegistryStore(tmp_path / "connections.json")
    assert fallback.env_path == __import__("pathlib").Path.cwd() / ".env"
