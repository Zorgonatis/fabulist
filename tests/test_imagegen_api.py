"""Tests for the image-generation router (V1 image-gen).

Mount pattern mirrors the other API tests: build the app over a tmp event-log
path via :func:`create_app`, include this lane's router, and inject a ComfyUI
client built over an ``httpx.MockTransport`` (``app.state.imagegen_client_factory``)
so no live ComfyUI is touched. The feature flag rides the real settings fold — a
``settings_changed`` toggle on the ``__settings__`` anchor.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api import imagegen_api
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.imagegen_api import IMAGEGEN_FAILURE_EVENT, router
from fabulist.api.settings import SETTINGS_SESSION_ID, SETTINGS_EVENT
from fabulist.comfy import ComfyClient
from fabulist.imagegen.registry import append_imagegen

#: Deterministic bytes a fake ``/view`` returns.
PNG_BYTES = b"\x89PNG\r\n\x1a\nfabulist-fake-image"

WORKFLOW = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 111,
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    },
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 512}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "old positive"}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "old negative"}},
}

BASE_URL = "http://comfy.test:8188"


def run(coro):
    return asyncio.run(coro)


def _comfy_handler(calls: dict, posted: dict, *, stats_status: int = 200, submit=None):
    """A routing MockTransport handler for the four ComfyUI routes."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls[path] = calls.get(path, 0) + 1
        if path == "/system_stats":
            if stats_status != 200:
                return httpx.Response(stats_status, text="nope")
            return httpx.Response(200, json={"system": {"comfyui_version": "0.0"}})
        if path == "/prompt":
            posted["prompt"] = json.loads(request.content.decode("utf-8"))
            if submit is not None:
                return submit()
            return httpx.Response(200, json={"prompt_id": "p1"})
        if path == "/history/p1":
            return httpx.Response(
                200,
                json={
                    "p1": {
                        "outputs": {
                            "9": {
                                "images": [
                                    {
                                        "filename": "gen.png",
                                        "subfolder": "",
                                        "type": "output",
                                    }
                                ]
                            }
                        }
                    }
                },
            )
        if path == "/view":
            return httpx.Response(200, content=PNG_BYTES)
        return httpx.Response(404)

    return handler


@pytest.fixture
def build_app(tmp_path, monkeypatch):
    log_path = tmp_path / "events.db"
    built = []
    # The reachability cache is module-level; isolate it per test.
    monkeypatch.setattr(imagegen_api, "_reach_cache", {})

    def _build(handler=None, *, caller=None, caller_factory=None):
        app = create_app(FakeTurnEngine(), log_path)
        app.include_router(router)
        app.state.imagegen_base_url = BASE_URL
        if caller is not None:
            app.state.imagegen_caller = caller
        if caller_factory is not None:
            app.state.imagegen_caller_factory = caller_factory
        if handler is not None:
            transport = httpx.MockTransport(handler)
            app.state.imagegen_client_factory = lambda base: ComfyClient(
                base, client=httpx.AsyncClient(transport=transport), poll_interval_s=0.0
            )
        built.append(app)
        return app

    yield _build
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


def _seed_session(app, session_id: str = "s1") -> None:
    app.state.log.append_event(
        session_id, "system", "system", {"event": "session_created"}
    )


def _enable_flag(app) -> None:
    app.state.log.append_event(
        SETTINGS_SESSION_ID,
        "system",
        "user",
        {"event": SETTINGS_EVENT, "features": {"comfy.enabled": True}},
    )


# --- config ----------------------------------------------------------------


def test_config_disabled_by_default_does_not_probe(build_app):
    calls: dict = {}
    app = build_app(_comfy_handler(calls, {}))

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/imagegen/config")

    resp = run(scenario())
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"enabled": False, "base_url": BASE_URL, "reachable": False}
    # The flag is off: no /system_stats probe.
    assert calls == {}


def test_config_enabled_and_reachable_is_cached(build_app):
    calls: dict = {}
    app = build_app(_comfy_handler(calls, {}))
    _seed_session(app)
    _enable_flag(app)

    async def scenario():
        async with client_for(app) as client:
            first = await client.get("/api/imagegen/config")
            second = await client.get("/api/imagegen/config")
            return first, second

    first, second = run(scenario())
    assert first.json() == {"enabled": True, "base_url": BASE_URL, "reachable": True}
    assert second.json() == first.json()
    # Reachability is cached for 60 s: one probe for two reads.
    assert calls["/system_stats"] == 1


def test_config_enabled_but_unreachable(build_app):
    calls: dict = {}
    app = build_app(_comfy_handler(calls, {}, stats_status=503))
    _seed_session(app)
    _enable_flag(app)

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/imagegen/config")

    body = run(scenario()).json()
    assert body["enabled"] is False
    assert body["reachable"] is False


# --- generation ------------------------------------------------------------


def test_generate_stores_asset_and_appends_image_row(build_app):
    calls: dict = {}
    posted: dict = {}
    app = build_app(_comfy_handler(calls, posted))
    _seed_session(app)
    _enable_flag(app)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/imagegen",
                json={"request": "a red fox", "workflow": WORKFLOW},
            )

    resp = run(scenario())
    assert resp.status_code == 200
    data = resp.json()
    assert data["unmapped"] == []
    assert data["prompt_id"] == "p1"
    assert isinstance(data["seq"], int)

    # the built prompts were applied onto the submitted workflow
    submitted = posted["prompt"]["prompt"]
    assert submitted["6"]["inputs"]["text"] == "a red fox"
    assert submitted["7"]["inputs"]["text"] == ""

    projection = app.state.log.get_projection("s1")
    assets = [
        a for a in projection["assets"] if a["payload"]["asset_id"] == data["asset_id"]
    ]
    assert len(assets) == 1
    assert assets[0]["payload"]["mime"] == "image/png"

    # The generated image is a CANON narrative row, NOT a user row (a user row
    # would fabricate a "USER:" turn in the narrator history).
    assert projection["user_messages"] == []
    image_rows = [
        n for n in projection["narratives"] if n["payload"].get("kind") == "image"
    ]
    assert len(image_rows) == 1
    assert image_rows[0]["type"] == "narrative"
    assert image_rows[0]["actor"] == "imagegen"
    assert image_rows[0]["payload"]["image_asset_id"] == data["asset_id"]
    assert image_rows[0]["payload"]["prompt"] == "a red fox"
    assert image_rows[0]["seq"] == data["seq"]


def test_generate_applies_overrides(build_app):
    posted: dict = {}
    app = build_app(_comfy_handler({}, posted))
    _seed_session(app)
    _enable_flag(app)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/imagegen",
                json={
                    "request": "ignored",
                    "workflow": WORKFLOW,
                    "overrides": {"seed": 42, "positive": "explicit", "width": 768},
                },
            )

    run(scenario())
    submitted = posted["prompt"]["prompt"]
    assert submitted["3"]["inputs"]["seed"] == 42
    assert submitted["6"]["inputs"]["text"] == "explicit"
    assert submitted["5"]["inputs"]["width"] == 768


def test_generate_row_records_effective_positive(build_app):
    """The canon image-narrative row records the EFFECTIVE positive prompt: the
    explicit override when given, else the agent render (never the raw request)."""
    posted: dict = {}
    fake = FakeGeneratorCaller('{"positive": "rendered pos", "negative": ""}')
    app = build_app(_comfy_handler({}, posted), caller_factory=lambda agent: fake)
    _seed_session(app)
    _enable_flag(app)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/imagegen",
                json={
                    "request": "explicit ignored",
                    "workflow": WORKFLOW,
                    "overrides": {"positive": "explicit pos"},
                },
            )

    run(scenario())
    rows = [n for n in app.state.log.get_projection("s1")["narratives"]]
    assert rows[-1]["payload"]["prompt"] == "explicit pos"
    # The agent is skipped when the positive is explicit; the row still records it.
    assert posted["prompt"]["prompt"]["6"]["inputs"]["text"] == "explicit pos"

    async def rendered():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/imagegen",
                json={"request": "a fox", "workflow": WORKFLOW, "prompt_template": "SYS"},
            )

    run(rendered())
    rows = [n for n in app.state.log.get_projection("s1")["narratives"]]
    assert rows[-1]["payload"]["prompt"] == "rendered pos"
    assert posted["prompt"]["prompt"]["6"]["inputs"]["text"] == "rendered pos"


def test_generate_renders_prompt_template(build_app):
    posted: dict = {}
    app = build_app(_comfy_handler({}, posted))
    _seed_session(app)
    _enable_flag(app)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/imagegen",
                json={
                    "request": "a red fox",
                    "workflow": WORKFLOW,
                    "prompt_template": "photo of {request}, dramatic light",
                },
            )

    run(scenario())
    assert posted["prompt"]["prompt"]["6"]["inputs"]["text"] == (
        "photo of a red fox, dramatic light"
    )


def test_generate_reports_unmapped_and_still_succeeds(build_app):
    workflow = {
        "3": {
            "class_type": "KSampler",
            "inputs": {"seed": 1, "positive": ["6", 0], "negative": ["7", 0]},
        },
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "p"}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "n"}},
    }
    app = build_app(_comfy_handler({}, {}))
    _seed_session(app)
    _enable_flag(app)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/imagegen",
                json={
                    "request": "x",
                    "workflow": workflow,
                    "overrides": {"width": 256},
                },
            )

    data = run(scenario()).json()
    assert data["unmapped"] == ["width"]
    assert data["asset_id"]


def test_generate_submit_422_surfaces_node_errors(build_app):
    node_errors = {"6": {"errors": [{"message": "required input missing"}]}}

    def _reject():
        return httpx.Response(422, json={"node_errors": node_errors})

    app = build_app(_comfy_handler({}, {}, submit=_reject))
    _seed_session(app)
    _enable_flag(app)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/imagegen",
                json={"request": "x", "workflow": WORKFLOW},
            )

    resp = run(scenario())
    # Never a 500: a row-level error payload.
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"]["stage"] == "submit"
    assert data["error"]["node_errors"] == node_errors

    events = app.state.log.get_events("s1")
    failures = [
        e for e in events
        if e.type == "system" and e.payload.get("event") == IMAGEGEN_FAILURE_EVENT
    ]
    assert len(failures) == 1
    assert failures[0].payload["stage"] == "submit"
    # No asset row was written on a failed submit.
    assert app.state.log.get_projection("s1")["assets"] == []


def test_generate_disabled_is_a_row_level_error(build_app):
    app = build_app(_comfy_handler({}, {}))
    _seed_session(app)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/imagegen",
                json={"request": "x", "workflow": WORKFLOW},
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json()["error"]["stage"] == "disabled"


def test_generate_unknown_session_is_404(build_app):
    app = build_app(_comfy_handler({}, {}))
    _seed_session(app)
    _enable_flag(app)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/nope/imagegen",
                json={"request": "x", "workflow": WORKFLOW},
            )

    resp = run(scenario())
    assert resp.status_code == 404


def test_generate_submit_400_names_the_offending_node(build_app):
    """Live-gate regression: Comfy answers an invalid prompt with 400 AND a
    ``node_errors`` body. The message the user sees must name the node/why —
    the bare "returned 400" made a missing-checkpoint failure undiagnosable."""
    node_errors = {"3": {"errors": [{"message": "Value not in list: 'nope.safetensors'"}]}}

    def _bad_request():
        return httpx.Response(400, json={"error": {"type": "prompt_outputs_failed_validation"}, "node_errors": node_errors})

    app = build_app(_comfy_handler({}, {}, submit=_bad_request))
    _seed_session(app)
    _enable_flag(app)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/imagegen",
                json={"request": "x", "workflow": WORKFLOW},
            )

    data = run(scenario()).json()
    assert data["error"]["stage"] == "submit"
    message = data["error"]["message"]
    assert "400" in message
    assert "node 3" in message and "nope.safetensors" in message


# --- generator agent: render route -----------------------------------------


class FakeGeneratorCaller:
    """A ``ModelCaller`` yielding a scripted generator reply.

    Emulates the wiring-built ``RegistryModelCaller`` in the one way the routes
    depend on for usage: it records a ``system``/``usage`` row attributed to the
    ambient ``CURRENT_SESSION`` (the real shared caller does this internally), so
    the run endpoint's usage contract is exercised with no network call.
    """

    agent = "imagegen"

    def __init__(self, reply: str, *, log=None, model: str = "your-model-id") -> None:
        self.reply = reply
        self.log = log
        self.model = model
        self.calls: list[list[dict]] = []
        self.sessions: list[str] = []

    async def __call__(self, messages, stream=True):
        from fabulist.agents.debug import CURRENT_SESSION

        self.calls.append(messages)
        self.sessions.append(CURRENT_SESSION.get())
        yield {"type": "content", "delta": self.reply}
        if self.log is not None:
            from fabulist.usage.track import record_usage

            record_usage(
                self.log,
                session_id=CURRENT_SESSION.get(),
                agent="imagegen",
                model=self.model,
                prompt_tokens=12,
                completion_tokens=34,
                latency_s=0.0,
            )


class BoomCaller:
    """A caller whose stream raises after starting (exercises error payloads)."""

    async def __call__(self, messages, stream=True):
        if False:  # pragma: no cover - makes this an async generator
            yield {}
        raise RuntimeError("boom")


def _render(app, payload):
    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/imagegen/render", json=payload)

    return run(scenario())


def test_render_route_parses_json_reply(build_app):
    fake = FakeGeneratorCaller(
        '{"positive": "a red fox at dawn", "negative": "blurry"}'
    )
    app = build_app(None, caller_factory=lambda agent: fake)
    body = _render(app, {"request": "a fox", "prompt_template": "SYS"}).json()
    assert body == {
        "positive": "a red fox at dawn",
        "negative": "blurry",
        "parse": "json",
    }
    # template -> SYSTEM, request -> USER (the generator's control plane).
    assert fake.calls == [
        [{"role": "system", "content": "SYS"}, {"role": "user", "content": "a fox"}]
    ]


def test_render_route_parses_fenced_json(build_app):
    reply = '```json\n{"positive": "p", "negative": "n"}\n```'
    app = build_app(None, caller_factory=lambda agent: FakeGeneratorCaller(reply))
    body = _render(app, {"request": "a fox"}).json()
    assert body == {"positive": "p", "negative": "n", "parse": "json"}


def test_render_route_parses_labeled_blocks(build_app):
    reply = "POSITIVE: a red fox\nNEGATIVE: blurry"
    app = build_app(None, caller_factory=lambda agent: FakeGeneratorCaller(reply))
    body = _render(app, {"request": "a fox"}).json()
    assert body == {"positive": "a red fox", "negative": "blurry", "parse": "labeled"}


def test_render_route_labeled_negative_is_optional(build_app):
    app = build_app(
        None, caller_factory=lambda agent: FakeGeneratorCaller("Positive: a red fox")
    )
    body = _render(app, {"request": "a fox"}).json()
    assert body == {"positive": "a red fox", "negative": "", "parse": "labeled"}


def test_render_route_falls_back_on_garbage(build_app):
    app = build_app(
        None,
        caller_factory=lambda agent: FakeGeneratorCaller("  just some prose  "),
    )
    body = _render(app, {"request": "a fox"}).json()
    assert body == {"positive": "just some prose", "negative": "", "parse": "fallback"}


def test_render_route_uses_definition_template(build_app):
    fake = FakeGeneratorCaller('{"positive": "p", "negative": ""}')
    app = build_app(None, caller_factory=lambda agent: fake)
    imagegen_id = append_imagegen(
        app.state.log,
        {"name": "Portrait", "workflow": WORKFLOW, "prompt_template": "DEF SYS"},
    )
    _render(app, {"request": "a fox", "imagegen_id": imagegen_id})
    assert fake.calls[0][0] == {"role": "system", "content": "DEF SYS"}


def test_render_route_unknown_definition_is_404(build_app):
    app = build_app(None, caller_factory=lambda agent: FakeGeneratorCaller("{}"))
    resp = _render(app, {"request": "a fox", "imagegen_id": "nope"})
    assert resp.status_code == 404


def test_render_route_explicit_template_wins(build_app):
    fake = FakeGeneratorCaller('{"positive": "p", "negative": ""}')
    app = build_app(None, caller_factory=lambda agent: fake)
    imagegen_id = append_imagegen(
        app.state.log,
        {"name": "Portrait", "workflow": WORKFLOW, "prompt_template": "DEF SYS"},
    )
    _render(
        app,
        {"request": "a fox", "imagegen_id": imagegen_id, "prompt_template": "EXPLICIT"},
    )
    assert fake.calls[0][0] == {"role": "system", "content": "EXPLICIT"}


def test_render_route_missing_caller_is_row_level_error(build_app):
    app = build_app(None)  # no caller
    resp = _render(app, {"request": "a fox", "prompt_template": "SYS"})
    assert resp.status_code == 200
    assert resp.json()["error"]["stage"] == "render"


def test_render_route_caller_raising_is_row_level_error(build_app):
    app = build_app(None, caller_factory=lambda agent: BoomCaller())
    resp = _render(app, {"request": "a fox"})
    assert resp.status_code == 200
    assert resp.json()["error"]["stage"] == "render"


# --- generator agent: run endpoint -----------------------------------------


def test_run_renders_then_applies_overrides(build_app):
    posted: dict = {}
    fake = FakeGeneratorCaller('{"positive": "rendered pos", "negative": "rendered neg"}')
    app = build_app(_comfy_handler({}, posted), caller_factory=lambda agent: fake)
    _seed_session(app)
    _enable_flag(app)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/imagegen",
                json={
                    "request": "a fox",
                    "workflow": WORKFLOW,
                    "prompt_template": "SYS",
                    "overrides": {"seed": 7},
                },
            )

    run(scenario())
    submitted = posted["prompt"]["prompt"]
    assert submitted["6"]["inputs"]["text"] == "rendered pos"
    assert submitted["7"]["inputs"]["text"] == "rendered neg"
    assert submitted["3"]["inputs"]["seed"] == 7
    assert fake.calls[0] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "a fox"},
    ]


def test_run_explicit_positive_skips_agent_call(build_app):
    posted: dict = {}
    fake = FakeGeneratorCaller('{"positive": "SHOULD NOT BE USED", "negative": ""}')
    app = build_app(_comfy_handler({}, posted), caller_factory=lambda agent: fake)
    _seed_session(app)
    _enable_flag(app)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/imagegen",
                json={
                    "request": "a fox",
                    "workflow": WORKFLOW,
                    "overrides": {"positive": "explicit pos", "negative": "explicit neg"},
                },
            )

    run(scenario())
    submitted = posted["prompt"]["prompt"]
    assert submitted["6"]["inputs"]["text"] == "explicit pos"
    assert submitted["7"]["inputs"]["text"] == "explicit neg"
    # The editable-before-submit contract: an explicit positive skips the agent.
    assert fake.calls == []


def test_run_records_imagegen_usage_event(build_app):
    posted: dict = {}
    fake = FakeGeneratorCaller('{"positive": "p", "negative": ""}')
    app = build_app(_comfy_handler({}, posted), caller_factory=lambda agent: fake)
    fake.log = app.state.log
    _seed_session(app)
    _enable_flag(app)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/imagegen",
                json={"request": "a fox", "workflow": WORKFLOW, "prompt_template": "SYS"},
            )

    run(scenario())
    # The route installed the session context the shared caller records against.
    assert fake.sessions == ["s1"]
    rows = [
        e
        for e in app.state.log.get_events("s1")
        if e.type == "system"
        and e.payload.get("event") == "usage"
        and e.payload.get("agent") == "imagegen"
    ]
    assert len(rows) == 1
    assert rows[0].payload["model"] == "your-model-id"
    assert rows[0].payload["session_id"] == "s1"


def test_run_degraded_static_path_when_caller_absent(build_app):
    posted: dict = {}
    app = build_app(_comfy_handler({}, posted))  # no caller
    _seed_session(app)
    _enable_flag(app)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/imagegen",
                json={
                    "request": "a red fox",
                    "workflow": WORKFLOW,
                    "prompt_template": "photo of {request}, dramatic light",
                },
            )

    run(scenario())
    assert posted["prompt"]["prompt"]["6"]["inputs"]["text"] == (
        "photo of a red fox, dramatic light"
    )


def test_run_unrenderable_template_without_caller_is_row_level_error(build_app):
    posted: dict = {}
    app = build_app(_comfy_handler({}, posted))  # no caller
    _seed_session(app)
    _enable_flag(app)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/imagegen",
                json={
                    "request": "a fox",
                    "workflow": WORKFLOW,
                    "prompt_template": "no placeholder at all",
                },
            )

    resp = run(scenario())
    assert resp.status_code == 200
    assert resp.json()["error"]["stage"] == "render"
    # Nothing was submitted and no image row was written.
    assert posted == {}
    assert app.state.log.get_projection("s1")["assets"] == []


# --- session-scoped render: context injection ------------------------------


def _seed_card(app, session_id: str, name: str = "Tessa", description: str = "A cartographer."):
    return app.state.log.append_event(
        session_id,
        "system",
        "system",
        {"card": {"name": name, "description": description}, "first_mes": ""},
    )


def _seed_turn(app, session_id: str, index: int) -> None:
    app.state.log.append_event(
        session_id, "user", "user", {"text": f"turn {index} user"}
    )
    app.state.log.append_event(
        session_id,
        "narrative",
        "narrator",
        {"prose": f"turn {index} prose", "dsl": "", "segments": []},
    )


def _render_session(app, session_id: str, payload):
    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                f"/api/sessions/{session_id}/imagegen/render", json=payload
            )

    return run(scenario())


def _context_message(call: list[dict]) -> str | None:
    """The injected CAST/RECENT system message from a captured call, or None."""
    systems = [m for m in call if m["role"] == "system"]
    return systems[1]["content"] if len(systems) > 1 else None


def test_session_render_unknown_session_is_404(build_app):
    app = build_app(None, caller_factory=lambda agent: FakeGeneratorCaller("{}"))
    _seed_session(app)
    resp = _render_session(app, "nope", {"request": "x"})
    assert resp.status_code == 404


def test_session_render_inherits_cast_through_fork_chain(build_app):
    """The owner's 'top active layer and its children': a branch reads the base
    cast via the fork-aware chain (get_chain_events), never a session-local read."""
    fake = FakeGeneratorCaller('{"positive": "p", "negative": ""}')
    app = build_app(None, caller_factory=lambda agent: fake)
    _seed_session(app, "base1")
    card = _seed_card(app, "base1", "Tessa")
    # The branch copies NO events: the card is reachable only through the chain.
    app.state.log.fork_session("base1", at_seq=card.seq, new_session_id="branch1")

    resp = _render_session(
        app, "branch1", {"request": "A picture of Tessa", "prompt_template": "SYS"}
    )
    assert resp.status_code == 200
    ctx = _context_message(fake.calls[0])
    assert ctx is not None
    assert "CAST CONTEXT" in ctx and "Tessa" in ctx
    # The template stays verbatim; the request still arrives as the final USER.
    assert fake.calls[0][0] == {"role": "system", "content": "SYS"}
    assert fake.calls[0][-1] == {"role": "user", "content": "A picture of Tessa"}


def test_session_render_recent_scene_window_is_exactly_last_5(build_app):
    fake = FakeGeneratorCaller('{"positive": "p", "negative": ""}')
    app = build_app(None, caller_factory=lambda agent: fake)
    _seed_session(app)
    for index in range(1, 8):  # 7 turns seeded; only the last 5 may appear
        _seed_turn(app, "s1", index)

    _render_session(app, "s1", {"request": "x", "prompt_template": "SYS"})
    ctx = _context_message(fake.calls[0])
    assert ctx is not None
    assert "RECENT SCENE (last 5 turns)" in ctx
    assert "turn 3 user" in ctx and "turn 7 prose" in ctx
    assert "turn 1 user" not in ctx and "turn 2 user" not in ctx


def test_session_render_strips_dsl_fences_from_recent_prose(build_app):
    fake = FakeGeneratorCaller('{"positive": "p", "negative": ""}')
    app = build_app(None, caller_factory=lambda agent: fake)
    _seed_session(app)
    app.state.log.append_event("s1", "user", "user", {"text": "look"})
    app.state.log.append_event(
        "s1",
        "narrative",
        "narrator",
        {
            "prose": 'A quiet room.\n```openui\nCharacterPanel(name="X")\n```\nThe door opens.',
            "dsl": "",
            "segments": [],
        },
    )

    _render_session(app, "s1", {"request": "x", "prompt_template": "SYS"})
    ctx = _context_message(fake.calls[0])
    assert ctx is not None
    assert "A quiet room." in ctx and "The door opens." in ctx
    assert "CharacterPanel" not in ctx


def test_session_render_records_usage_row_on_the_session(build_app):
    fake = FakeGeneratorCaller('{"positive": "p", "negative": ""}')
    app = build_app(None, caller_factory=lambda agent: fake)
    fake.log = app.state.log
    _seed_session(app)

    _render_session(app, "s1", {"request": "a fox", "prompt_template": "SYS"})
    # The route installed the session context the shared caller records against.
    assert fake.sessions == ["s1"]
    rows = [
        e
        for e in app.state.log.get_events("s1")
        if e.type == "system"
        and e.payload.get("event") == "usage"
        and e.payload.get("agent") == "imagegen"
    ]
    assert len(rows) == 1
    assert rows[0].payload["session_id"] == "s1"


def test_session_render_fresh_chat_has_no_context_message(build_app):
    fake = FakeGeneratorCaller('{"positive": "p", "negative": ""}')
    app = build_app(None, caller_factory=lambda agent: fake)
    _seed_session(app)  # session_created only: no cards, no turns

    _render_session(app, "s1", {"request": "a fox", "prompt_template": "SYS"})
    # Both sections empty -> the context message is omitted entirely.
    assert fake.calls[0] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "a fox"},
    ]


def test_session_render_context_failure_is_advisory(build_app, monkeypatch):
    fake = FakeGeneratorCaller('{"positive": "p", "negative": ""}')
    app = build_app(None, caller_factory=lambda agent: fake)
    _seed_session(app)

    def boom(*args, **kwargs):
        raise RuntimeError("context boom")

    monkeypatch.setattr(imagegen_api, "assemble_imagegen_context", boom)
    resp = _render_session(app, "s1", {"request": "a fox", "prompt_template": "SYS"})
    # Assembly raising is never fatal: the render proceeds with the request alone.
    assert resp.status_code == 200
    assert resp.json()["positive"] == "p"
    assert fake.calls[0] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "a fox"},
    ]


def test_sessionless_render_stays_context_free_even_with_cards(build_app):
    """The session-less route is the Studio preview path: it never injects a
    chat's cast/scene even when a session with cards exists in the log."""
    fake = FakeGeneratorCaller('{"positive": "p", "negative": ""}')
    app = build_app(None, caller_factory=lambda agent: fake)
    _seed_session(app)
    _seed_card(app, "s1", "Tessa")

    _render(app, {"request": "a fox", "prompt_template": "SYS"})
    assert fake.calls[0] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "a fox"},
    ]


def test_run_endpoint_injects_session_context(build_app):
    """The run endpoint renders agent-side through the SAME context-enriched
    path: the cast is visible in the captured messages."""
    posted: dict = {}
    fake = FakeGeneratorCaller('{"positive": "rendered pos", "negative": ""}')
    app = build_app(_comfy_handler({}, posted), caller_factory=lambda agent: fake)
    _seed_session(app)
    _enable_flag(app)
    _seed_card(app, "s1", "Tessa")

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/imagegen",
                json={
                    "request": "A picture of Tessa",
                    "workflow": WORKFLOW,
                    "prompt_template": "SYS",
                },
            )

    run(scenario())
    ctx = _context_message(fake.calls[0])
    assert ctx is not None
    assert "CAST CONTEXT" in ctx and "Tessa" in ctx
    assert posted["prompt"]["prompt"]["6"]["inputs"]["text"] == "rendered pos"
