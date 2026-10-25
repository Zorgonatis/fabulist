"""Per-session component-set selection (issues #16 + #24, owner ruling).

The two issues ship as ONE lane because the selection surface is only correct
once the store stops holding an engine-global selection:

  * a swap is a PER-SESSION log fact (``set_swapped``); the selection door
    appends it and takes effect on the NEXT turn, never live;
  * the store caches CONTENT per set directory (one shared snapshot per set) and
    binds a session to a directory — one session's swap can never retarget
    another;
  * the door refuses a set the client cannot render (422 + the offending
    component names, ADR-025 ``check_set_loadable``), and a set whose artifacts
    do not read;
  * ``GET /api/component-sets`` is the picker's catalog (id/name/version/kind/
    loadable) plus the set the session currently uses.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.dsl import sets as dsl_sets
from fabulist.dsl import spec as dsl_spec
from fabulist.dsl import store as dsl_store
from fabulist.dsl.spec import CORE_COMPONENT_NAMES, component_names
from fabulist.log import EventLog
from fabulist.prompts.library_mini import render_library_doc, reset_library_artifact_cache
from fabulist.wiring import make_narrator_message_builder

LABEL = {"props": [{"name": "label", "type": "string"}]}
ALPHA_DOC = "Alpha component reference.\n- Widget(label) — the alpha panel.\n"
BETA_DOC = "Beta component reference.\n- Gadget(label) — the beta panel.\n"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch):
    """No ambient env, no warm caches: every test states its own tree."""
    for name in (
        dsl_spec.SPEC_ENV_VAR,
        dsl_store.ARTIFACT_DIR_ENV_VAR,
        dsl_spec.FLAVORS_ENV_VAR,
        dsl_sets.SETS_DIR_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    dsl_spec.reset_spec_cache()
    reset_library_artifact_cache()
    dsl_sets.reset_sets_cache()
    yield
    dsl_spec.reset_spec_cache()
    reset_library_artifact_cache()
    dsl_sets.reset_sets_cache()


@pytest.fixture
def app(tmp_path):
    """The real FastAPI app over a scripted engine (the sessions router is in)."""
    application = create_app(FakeTurnEngine(), tmp_path / "events.db")
    yield application
    application.state.log.close()


def client_for(application) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    )


async def _create(client: httpx.AsyncClient, title: str) -> str:
    resp = await client.post("/api/sessions", json={"title": title})
    assert resp.status_code == 200, resp.text
    return resp.json()["session_id"]


# --- fixtures for an isolated sets tree --------------------------------------


def _write_set(
    directory: Path,
    components: dict[str, object],
    *,
    set_id: str,
    doc: str,
) -> Path:
    """A self-contained set directory (spec + doc + ``set.json``)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / dsl_store.SPEC_FILENAME).write_text(
        json.dumps({"root": "ScenePane", "components": components}), encoding="utf-8"
    )
    (directory / dsl_store.DOC_FILENAME).write_text(doc, encoding="utf-8")
    (directory / dsl_store.MANIFEST_FILENAME).write_text(
        json.dumps(
            {"id": set_id, "name": set_id.title(), "version": "1.0.0", "components": list(components)}
        ),
        encoding="utf-8",
    )
    return directory


def _write_tree(
    monkeypatch,
    root: Path,
    sets: dict[str, tuple[Path, list[str]]],
    *,
    default: str,
    union: list[str],
) -> None:
    """A temp sets registry + union spec the engine resolves like the shipped one."""
    root.mkdir(parents=True, exist_ok=True)
    union_path = root / dsl_sets.UNION_SPEC_FILENAME
    union_path.write_text(
        json.dumps(
            {
                "root": "ScenePane",
                "components": {name: LABEL for name in union},
            }
        ),
        encoding="utf-8",
    )
    index = {
        "default_set": default,
        "union_spec": str(union_path),
        "sets": [
            {
                "id": set_id,
                "name": set_id.title(),
                "version": "1.0.0",
                "kind": "shipped",
                "dir": str(directory),
                "components": components,
            }
            for set_id, (directory, components) in sets.items()
        ],
    }
    (root / "index.json").write_text(json.dumps(index), encoding="utf-8")
    monkeypatch.setenv(dsl_sets.SETS_DIR_ENV_VAR, str(root))
    dsl_sets.reset_sets_cache()


def _install_store(monkeypatch, default_dir: Path) -> dsl_store.ComponentLibraryStore:
    store = dsl_store.ComponentLibraryStore(default_dir=default_dir)
    monkeypatch.setattr(dsl_store, "_STORE", store)
    dsl_spec.reset_spec_cache()
    reset_library_artifact_cache()
    return store


def _alpha_beta(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    alpha = _write_set(tmp_path / "alpha", {"Widget": LABEL}, set_id="alpha", doc=ALPHA_DOC)
    beta = _write_set(tmp_path / "beta", {"Gadget": LABEL}, set_id="beta", doc=BETA_DOC)
    _write_tree(
        monkeypatch,
        tmp_path / "sets",
        {
            "alpha": (alpha, ["Widget"]),
            "beta": (beta, ["Gadget"]),
        },
        default="alpha",
        union=["Widget", "Gadget", *CORE_COMPONENT_NAMES],
    )
    _install_store(monkeypatch, alpha)
    return alpha, beta


def _state(sid: str) -> dict:
    return {
        "session_id": sid,
        "user_content": "I look around.",
        "kind": "in_character",
        "facts": "",
        "recall": "",
        "rolls": "",
    }


def _swap_rows(log: EventLog, sid: str) -> list[dict]:
    return [
        event.payload
        for event in log.get_events(sid)
        if isinstance(event.payload, dict)
        and event.payload.get("event") == "set_swapped"
    ]


# --- version exposure (deliverable (d)) --------------------------------------


def test_component_set_entry_exposes_the_registry_version():
    """The shipped set's version is available to Python (the picker shows it)."""
    entry = dsl_sets.component_set("technology")
    assert entry is not None
    assert entry.version == "1.0.0"
    fixture = dsl_sets.component_set("fixture-core")
    assert fixture is not None and fixture.version == "0.0.0"


# --- the catalog + the selection door ----------------------------------------


def test_catalog_lists_the_shipped_sets_with_versions_and_loadability(app):
    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client, "Catalog")
            resp = await client.get(f"/api/component-sets?session_id={sid}")
            assert resp.status_code == 200, resp.text
            return resp.json()

    body = run(scenario())
    assert body["default_set_id"] == "technology"
    assert body["active_set_id"] == "technology"
    by_id = {entry["id"]: entry for entry in body["sets"]}
    assert {"technology", "fixture-core"} <= set(by_id)
    assert by_id["technology"]["version"] == "1.0.0"
    assert by_id["technology"]["kind"] == "shipped"
    assert by_id["technology"]["loadable"] is True
    assert by_id["fixture-core"]["kind"] == "fixture"


def test_a_selection_is_a_session_fact_that_applies_on_the_next_turn(
    monkeypatch, tmp_path, app
):
    """POST appends one row; the prompt changes at the NEXT build only."""
    _alpha_beta(monkeypatch, tmp_path)
    log = app.state.log
    build = make_narrator_message_builder(log)

    async def select(client, sid):
        return await client.post(
            f"/api/sessions/{sid}/component-set", json={"set_id": "beta"}
        )

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client, "Swap")
            return sid, await select(client, sid), (await client.get(
                f"/api/component-sets?session_id={sid}"
            )).json()

    sid, resp, catalog = run(scenario())
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["previous_set_id"], body["set_id"], body["changed"]) == (
        "alpha",
        "beta",
        True,
    )
    assert body["applies"] == "next_turn"
    assert catalog["active_set_id"] == "beta"  # what the NEXT turn will use

    # The selection row is the durable fact.
    rows = _swap_rows(log, sid)
    assert len(rows) == 1
    assert (rows[0]["previous_set_id"], rows[0]["set_id"], rows[0]["name"]) == (
        "alpha",
        "beta",
        "Beta",
    )
    assert "(beta)" in rows[0]["notice"]

    # Nothing moved yet: the builder is the turn boundary.
    assert "Gadget" not in component_names()
    assert ALPHA_DOC.strip() in render_library_doc()

    build(_state(sid))
    assert "Gadget" in component_names()
    assert BETA_DOC.strip() in render_library_doc()

    # Selecting what is already in effect appends nothing (no prefix re-prefill).
    async def reselect():
        async with client_for(app) as client:
            return await client.post(
                f"/api/sessions/{sid}/component-set", json={"set_id": "beta"}
            )

    again = run(reselect())
    assert again.status_code == 200
    assert again.json()["changed"] is False
    assert len(_swap_rows(log, sid)) == 1


def test_two_sessions_two_sets_never_retarget_each_other(monkeypatch, tmp_path, app):
    """#24: A's swap changes A's vocabulary only; B keeps its own set."""
    _, beta = _alpha_beta(monkeypatch, tmp_path)
    log = app.state.log
    build = make_narrator_message_builder(log)
    store = dsl_store.library_store()

    async def scenario():
        async with client_for(app) as client:
            a = await _create(client, "A")
            b = await _create(client, "B")
            resp = await client.post(
                f"/api/sessions/{a}/component-set", json={"set_id": "beta"}
            )
            assert resp.status_code == 200, resp.text
            return a, b

    a, b = run(scenario())

    build(_state(a))
    assert BETA_DOC.strip() in render_library_doc()
    assert "Gadget" in component_names()
    build(_state(b))
    assert ALPHA_DOC.strip() in render_library_doc()
    assert "Gadget" not in component_names()
    # …and B's build did not retarget A.
    build(_state(a))
    assert BETA_DOC.strip() in render_library_doc()
    assert dsl_sets.session_set_id(log, a) == "beta"
    assert dsl_sets.session_set_id(log, b) == "alpha"
    assert store.snapshot_for_dir(beta).set_id == "beta"


def test_the_same_set_in_two_sessions_shares_one_snapshot(monkeypatch, tmp_path, app):
    """Snapshots are CONTENT: selection is per session, the bytes are shared."""
    _, beta = _alpha_beta(monkeypatch, tmp_path)
    log = app.state.log

    async def scenario():
        async with client_for(app) as client:
            a = await _create(client, "A")
            c = await _create(client, "C")
            for sid in (a, c):
                resp = await client.post(
                    f"/api/sessions/{sid}/component-set", json={"set_id": "beta"}
                )
                assert resp.status_code == 200, resp.text
            return a, c

    a, c = run(scenario())
    snap_a = dsl_sets.session_snapshot(log, a)
    snap_c = dsl_sets.session_snapshot(log, c)
    assert snap_a is snap_c
    assert snap_a is dsl_store.library_store().snapshot_for_dir(beta)


def test_a_set_the_client_cannot_render_is_refused_with_the_names(
    monkeypatch, tmp_path, app
):
    """ADR-025: set ⊆ union — 422 names the components the client lacks."""
    alpha = _write_set(tmp_path / "alpha", {"Widget": LABEL}, set_id="alpha", doc=ALPHA_DOC)
    rogue = _write_set(
        tmp_path / "rogue",
        {"Widget": LABEL, "Unrendered": LABEL},
        set_id="rogue",
        doc="Rogue reference.\n",
    )
    _write_tree(
        monkeypatch,
        tmp_path / "sets",
        {
            "alpha": (alpha, ["Widget"]),
            "rogue": (rogue, ["Widget", "Unrendered"]),
        },
        default="alpha",
        union=["Widget", *CORE_COMPONENT_NAMES],
    )
    _install_store(monkeypatch, alpha)
    log = app.state.log

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client, "Refused")
            resp = await client.post(
                f"/api/sessions/{sid}/component-set", json={"set_id": "rogue"}
            )
            catalog = (await client.get("/api/component-sets")).json()
            return sid, resp, catalog

    sid, resp, catalog = run(scenario())
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["set_id"] == "rogue"
    assert detail["unrenderable"] == ["Unrendered"]
    assert "Unrendered" in detail["reason"]
    # A refused set never becomes a log row.
    assert _swap_rows(log, sid) == []
    # The catalog tells the picker the same thing.
    assert {entry["id"]: entry for entry in catalog["sets"]}["rogue"]["loadable"] is False


def test_an_unknown_set_is_404_and_an_unknown_session_is_404(monkeypatch, tmp_path, app):
    _alpha_beta(monkeypatch, tmp_path)

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client, "Doors")
            unknown_set = await client.post(
                f"/api/sessions/{sid}/component-set", json={"set_id": "nope"}
            )
            unknown_session = await client.post(
                "/api/sessions/absent/component-set", json={"set_id": "alpha"}
            )
            return unknown_set, unknown_session

    unknown_set, unknown_session = run(scenario())
    assert unknown_set.status_code == 404
    assert unknown_session.status_code == 404


def test_a_set_with_unreadable_artifacts_is_refused_before_it_becomes_a_fact(
    monkeypatch, tmp_path, app
):
    """A torn set never strands the next turn: the door refuses it (422)."""
    alpha = _write_set(tmp_path / "alpha", {"Widget": LABEL}, set_id="alpha", doc=ALPHA_DOC)
    broken = _write_set(tmp_path / "broken", {"Gadget": LABEL}, set_id="broken", doc=BETA_DOC)
    (broken / dsl_store.SPEC_FILENAME).write_text("{ torn", encoding="utf-8")
    _write_tree(
        monkeypatch,
        tmp_path / "sets",
        {"alpha": (alpha, ["Widget"]), "broken": (broken, ["Gadget"])},
        default="alpha",
        union=["Widget", "Gadget", *CORE_COMPONENT_NAMES],
    )
    _install_store(monkeypatch, alpha)
    log = app.state.log

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client, "Broken")
            return sid, await client.post(
                f"/api/sessions/{sid}/component-set", json={"set_id": "broken"}
            )

    sid, resp = run(scenario())
    assert resp.status_code == 422
    assert "invalid JSON" in resp.json()["detail"]["reason"]
    assert _swap_rows(log, sid) == []
