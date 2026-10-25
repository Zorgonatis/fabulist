"""Tests for the imagegen definition registry + definitions router (lane V2).

Two layers, mirroring :mod:`tests.test_books`:

* the pure registry (:mod:`fabulist.imagegen.registry`) against a tmp SQLite
  log — append/fold/tombstone, wire conversions, workflow parsing/validation,
  and the parameter map (seed / positive / negative / resolution);
* the real FastAPI app (httpx ASGI transport) with the definitions routes
  mounted from :mod:`fabulist.api.imagegen_defs_api`. The log owns everything
  (D9): definitions fold the ``__imagegen__`` anchor and CRUD is session-free.

No pytest-asyncio in this repo, so async scenarios run via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.imagegen_defs_api import router as imagegen_router
from fabulist.imagegen.registry import (
    IMAGEGEN_ANCHOR,
    append_imagegen,
    delete_imagegen,
    detect_parameters,
    imagegen_by_id,
    imagegen_to_wire,
    list_imagegen,
    parse_workflow,
    validate_workflow,
    wire_to_imagegen,
)
from fabulist.log import EventLog

#: A minimal ComfyUI API-format graph with every adjustable present.
WORKFLOW = {
    "3": {
        "class_type": "KSampler",
        "inputs": {
            "seed": 42,
            "steps": 20,
            "cfg": 8,
            "positive": ["6", 0],
            "negative": ["7", 0],
            "latent_image": ["5", 0],
        },
    },
    "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "m.safetensors"}},
    "5": {"class_type": "EmptyLatentImage", "inputs": {"width": 512, "height": 768}},
    "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "a lighthouse", "clip": ["4", 1]}},
    "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "blurry", "clip": ["4", 1]}},
}

FULL_PARAM_MAP = {
    "seed": {"node_id": "3", "input": "seed"},
    "positive": {"node_id": "6", "input": "text"},
    "negative": {"node_id": "7", "input": "text"},
    "resolution": {"node_id": "5", "width_input": "width", "height_input": "height"},
}

#: The REAL user workflow shape (RES4LYF / kroma): a custom
#: ``ClownsharKSampler_Beta`` (seed input is ``seed``) fed by a ``UNETLoader``,
#: two ``CLIPTextEncode`` conditioners and an ``EmptyLatentImage``.
CLOWNSHARK_WORKFLOW = {
    "8": {
        "class_type": "UNETLoader",
        "inputs": {
            "unet_name": "kroma-v0.3.1-turbo-opd.safetensors",
            "weight_dtype": "default",
        },
    },
    "11": {
        "class_type": "ClownsharKSampler_Beta",
        "inputs": {
            "seed": 798893994874649,
            "steps": 8,
            "cfg": 1,
            "sampler_name": "linear/euler",
            "scheduler": "simple",
            "model": ["8", 0],
            "positive": ["13", 0],
            "negative": ["14", 0],
            "latent_image": ["5", 0],
            "eta": 0.6,
            "steps_to_run": -1,
            "denoise": 1,
            "sampler_mode": "standard",
            "bongmath": True,
        },
    },
    "13": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "a lighthouse", "clip": ["7", 0]},
    },
    "14": {
        "class_type": "CLIPTextEncode",
        "inputs": {"text": "blurry", "clip": ["7", 0]},
    },
    "5": {
        "class_type": "EmptyLatentImage",
        "inputs": {"width": 768, "height": 1024, "batch_size": 1},
    },
}

CLOWNSHARK_PARAM_MAP = {
    "seed": {"node_id": "11", "input": "seed"},
    "positive": {"node_id": "13", "input": "text"},
    "negative": {"node_id": "14", "input": "text"},
    "resolution": {"node_id": "5", "width_input": "width", "height_input": "height"},
}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_log(tmp_path: Path):
    """Factory for an :class:`EventLog` on a tmp path; closes logs after."""
    built: list[EventLog] = []

    def _build(name: str = "events.db") -> EventLog:
        log = EventLog(tmp_path / name)
        built.append(log)
        return log

    yield _build
    for log in built:
        log.close()


@pytest.fixture
def build_app(tmp_path: Path):
    """App over a tmp log with the imagegen definitions router mounted."""
    log_path = tmp_path / "events.db"
    built = []

    def _build(**kwargs):
        app = create_app(FakeTurnEngine(), log_path, **kwargs)
        app.include_router(imagegen_router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


# -- registry: parse / validate ----------------------------------------------


def test_parse_workflow_accepts_object_and_text():
    assert parse_workflow(WORKFLOW) is WORKFLOW
    assert parse_workflow(json.dumps(WORKFLOW)) == WORKFLOW


def test_parse_workflow_rejects_blank_bad_and_non_object():
    with pytest.raises(ValueError, match="empty"):
        parse_workflow("   ")
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_workflow("{not json")
    with pytest.raises(ValueError, match="must be a JSON object"):
        parse_workflow("[1, 2, 3]")


def test_validate_workflow_requires_a_sampler():
    validate_workflow(WORKFLOW)  # has KSampler -> ok
    with pytest.raises(ValueError, match="no sampler node"):
        validate_workflow({"5": {"class_type": "EmptyLatentImage", "inputs": {}}})


def test_validate_workflow_accepts_clownshark_custom_sampler():
    validate_workflow(CLOWNSHARK_WORKFLOW)  # ClownsharKSampler_Beta -> ok
    # A workflow with no sampler still fails, and the message names the
    # accepted classes (no longer only KSampler / KSamplerAdvanced).
    with pytest.raises(ValueError) as excinfo:
        validate_workflow({"5": {"class_type": "EmptyLatentImage", "inputs": {}}})
    assert "ClownsharKSampler_Beta" in str(excinfo.value)


# -- registry: parameter map -------------------------------------------------


def test_detect_parameters_full_graph():
    assert detect_parameters(WORKFLOW) == FULL_PARAM_MAP


def test_detect_parameters_ksampler_advanced_noise_seed_and_unmappable():
    workflow = {
        "10": {
            "class_type": "KSamplerAdvanced",
            "inputs": {"noise_seed": 7, "positive": ["11", 0]},
        },
        "11": {"class_type": "CLIPTextEncode", "inputs": {"text": "storm"}},
    }
    assert detect_parameters(workflow) == {
        "seed": {"node_id": "10", "input": "noise_seed"},
        "positive": {"node_id": "11", "input": "text"},
    }


def test_detect_parameters_degrades_to_empty_on_non_graph():
    assert detect_parameters({}) == {}
    assert detect_parameters({"weird": {"class_type": "KSampler"}}) == {}


def test_detect_parameters_clownshark_workflow():
    assert detect_parameters(CLOWNSHARK_WORKFLOW) == CLOWNSHARK_PARAM_MAP


# -- registry: append / fold / tombstone -------------------------------------


def test_append_and_fold_definition(build_log):
    log = build_log()
    imagegen_id = append_imagegen(
        log, {"name": "Portrait", "workflow": WORKFLOW, "prompt_template": "render {request}"}
    )
    definitions = list_imagegen(log)
    assert [d.imagegen_id for d in definitions] == [imagegen_id]
    definition = definitions[0]
    assert definition.name == "Portrait"
    assert definition.workflow == WORKFLOW
    assert definition.prompt_template == "render {request}"
    assert definition.param_map == FULL_PARAM_MAP
    assert definition.provenance["created_at"]


def test_append_requires_non_empty_name(build_log):
    log = build_log()
    with pytest.raises(ValueError, match="non-empty 'name'"):
        append_imagegen(log, {"name": "  ", "workflow": WORKFLOW})


def test_update_is_latest_wins_by_id(build_log):
    log = build_log()
    imagegen_id = append_imagegen(log, {"name": "First", "workflow": WORKFLOW})
    append_imagegen(
        log, {"imagegen_id": imagegen_id, "name": "Second", "workflow": WORKFLOW}
    )
    definitions = list_imagegen(log)
    assert len(definitions) == 1
    assert definitions[0].name == "Second"


def test_delete_tombstones(build_log):
    log = build_log()
    imagegen_id = append_imagegen(log, {"name": "Gone", "workflow": WORKFLOW})
    delete_imagegen(log, imagegen_id)
    assert list_imagegen(log) == []
    assert imagegen_by_id(log, imagegen_id) is None


def test_wire_round_trip_recomputes_param_map():
    wire = imagegen_to_wire(
        wire_to_imagegen({"name": "Lighthouse", "workflow": WORKFLOW, "prompt_template": "p"})
    )
    assert wire["name"] == "Lighthouse"
    assert wire["prompt_template"] == "p"
    assert wire["workflow"] == WORKFLOW
    assert wire["param_map"] == FULL_PARAM_MAP


def test_wire_to_imagegen_rejects_bad_shapes():
    with pytest.raises(ValueError, match="non-empty 'name'"):
        wire_to_imagegen({"name": "", "workflow": WORKFLOW})
    with pytest.raises(ValueError, match="'workflow' must be an object"):
        wire_to_imagegen({"name": "x", "workflow": "not decoded"})


def test_anchor_is_reserved():
    assert IMAGEGEN_ANCHOR == "__imagegen__"


# -- API: definitions CRUD ---------------------------------------------------


def test_api_list_empty(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.get("/api/imagegen/definitions")
            assert resp.status_code == 200
            assert resp.json() == {"definitions": []}

    run(scenario())


def test_api_create_with_object_and_list(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/imagegen/definitions",
                json={"name": "Portrait", "workflow": WORKFLOW, "prompt_template": "render"},
            )
            assert resp.status_code == 200, resp.text
            definition = resp.json()["definition"]
            assert definition["name"] == "Portrait"
            assert definition["param_map"] == FULL_PARAM_MAP
            assert definition["provenance"]["created_at"]

            listed = await client.get("/api/imagegen/definitions")
            assert [d["imagegen_id"] for d in listed.json()["definitions"]] == [
                definition["imagegen_id"]
            ]

    run(scenario())


def test_api_create_accepts_raw_json_text(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/imagegen/definitions",
                json={"name": "Text workflow", "workflow": json.dumps(WORKFLOW)},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["definition"]["workflow"] == WORKFLOW

    run(scenario())


def test_api_create_update_in_place(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            first = (
                await client.post(
                    "/api/imagegen/definitions", json={"name": "A", "workflow": WORKFLOW}
                )
            ).json()["definition"]
            second = await client.post(
                "/api/imagegen/definitions",
                json={
                    "imagegen_id": first["imagegen_id"],
                    "name": "B",
                    "workflow": WORKFLOW,
                },
            )
            assert second.status_code == 200, second.text
            listed = (await client.get("/api/imagegen/definitions")).json()["definitions"]
            assert len(listed) == 1
            assert listed[0]["name"] == "B"

    run(scenario())


def test_api_create_422_empty_name(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/imagegen/definitions", json={"name": "   ", "workflow": WORKFLOW}
            )
            assert resp.status_code == 422
            assert "non-empty" in resp.json()["detail"]

    run(scenario())


def test_api_create_422_bad_json(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/imagegen/definitions", json={"name": "Bad", "workflow": "{not json"}
            )
            assert resp.status_code == 422
            assert "not valid JSON" in resp.json()["detail"]

    run(scenario())


def test_api_create_422_no_sampler(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/imagegen/definitions",
                json={
                    "name": "No sampler",
                    "workflow": {"5": {"class_type": "EmptyLatentImage", "inputs": {}}},
                },
            )
            assert resp.status_code == 422
            assert "no sampler node" in resp.json()["detail"]

    run(scenario())


def test_api_create_accepts_clownshark_workflow(build_app):
    """The reported 422: a real RES4LYF ``ClownsharKSampler_Beta`` workflow."""
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post(
                "/api/imagegen/definitions",
                json={"name": "Kroma", "workflow": CLOWNSHARK_WORKFLOW},
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["definition"]["param_map"] == CLOWNSHARK_PARAM_MAP

    run(scenario())


def test_api_delete_and_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            created = (
                await client.post(
                    "/api/imagegen/definitions", json={"name": "Gone", "workflow": WORKFLOW}
                )
            ).json()["definition"]
            deleted = await client.delete(
                f"/api/imagegen/definitions/{created['imagegen_id']}"
            )
            assert deleted.status_code == 200
            assert deleted.json() == {"imagegen_id": created["imagegen_id"], "deleted": True}
            assert (await client.get("/api/imagegen/definitions")).json()["definitions"] == []
            again = await client.delete(f"/api/imagegen/definitions/{created['imagegen_id']}")
            assert again.status_code == 404

    run(scenario())
