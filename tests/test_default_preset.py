"""Tests for the persisted default preset (D26 / W7-A).

Covers the factory image (``prompts/factory-defaults.json`` load/save), boot
auto-seed only when missing, never-overwrite on a factory version bump,
reset-to-factory, commit-to-factory + loader round-trip, undeletable default,
no-active resolution to the persisted row, and the API surface
(``GET /api/prompts/default`` + reset/commit + default PUT/PATCH/DELETE).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.prompts_api import router
from fabulist.log import EventLog
from fabulist.prompts import factory
from fabulist.prompts import preset_store as store
from fabulist.prompts.modules import DEFAULT_FLOWS, INTERACTIONS
from fabulist.prompts.parts import default_directives, directive_to_row


def run(coro):
    return asyncio.run(coro)


#: The real repo-relative artifact constant, captured before the autouse fixture
#: monkeypatches it (the gitignore test must inspect the real value).
REAL_ARTIFACT_RELPATH = factory.DEFAULT_ARTIFACT_RELPATH


@pytest.fixture(autouse=True)
def _isolate_artifact(tmp_path: Path, monkeypatch) -> Path:
    """Keep every test off a *committed* repo artifact (test hermeticity).

    ``artifact_path(None)`` reads ``factory.DEFAULT_ARTIFACT_RELPATH`` on every
    call, so redirecting it to a per-test tmp path (absent unless a test writes
    it) makes ``factory_image()`` deterministically fall back to the code image.
    Without this, a live-committed ``prompts/factory-defaults.json`` would become
    test input for the whole module.
    """
    path = tmp_path / "prompts" / "factory-defaults.json"
    monkeypatch.setattr(factory, "DEFAULT_ARTIFACT_RELPATH", path)
    return path


@pytest.fixture
def tmp_artifact(_isolate_artifact: Path) -> Path:
    """The isolated factory artifact path for tests that write/read one."""
    return _isolate_artifact


@pytest.fixture
def build_app(tmp_path: Path):
    log_path = tmp_path / "events.db"
    built = []

    def _build(engine=None, **kwargs):
        app = create_app(engine or FakeTurnEngine(), log_path, **kwargs)
        app.include_router(router)
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


# -- boot auto-seed ----------------------------------------------------------


def test_ensure_default_seeds_only_when_missing(tmp_path: Path):
    log = EventLog(tmp_path / "seed.db")
    try:
        assert store.default_preset(log) is None
        first = store.ensure_default_preset(log)
        assert first.created is True
        assert first.factory_changed is False
        assert first.seeded_version == factory.current_factory_version()
        assert first.factory_version == factory.current_factory_version()

        row = store.default_preset(log)
        assert row is not None
        assert row.preset_id == store.DEFAULT_PRESET_ID
        assert row.is_default is True
        assert [m.type for m in row.modules] == [m.type for m in DEFAULT_FLOWS["narrate"]]
        assert set(row.flows) == set(INTERACTIONS)

        second = store.ensure_default_preset(log)
        assert second.created is False
        assert len([p for p in store.list_presets(log) if p.preset_id == "default"]) == 1
    finally:
        log.close()


def test_default_preset_carries_the_critic_flow(tmp_path: Path):
    """D41/D49: the critic interaction ships in the default preset.

    The default flow is system + payload (mirroring bts). The system
    body is empty in the CODE factory image; the seeded, editable copy rides
    ``prompts/factory-defaults.json`` via ``scripts/seed_prompt_defaults.py``.
    """
    log = EventLog(tmp_path / "critic.db")
    try:
        store.ensure_default_preset(log)
        row = store.default_preset(log)
        assert "critic" in row.flows
        assert [m["type"] for m in row.flows["critic"]] == [
            m.type for m in DEFAULT_FLOWS["critic"]
        ]
    finally:
        log.close()


def test_no_active_resolution_returns_persisted_default(tmp_path: Path):
    log = EventLog(tmp_path / "resolve.db")
    try:
        # Pre-boot (no row): None -> callers compile the code default flow.
        assert store.active_preset(log) is None
        store.ensure_default_preset(log)
        # Post-seed: no preset_active pointer, yet the default row resolves.
        assert store.active_preset(log).preset_id == "default"
    finally:
        log.close()


# -- factory version bump: never overwrite -----------------------------------


def test_version_bump_never_overwrites_and_surfaces_notice(
    tmp_path: Path, monkeypatch
):
    log = EventLog(tmp_path / "bump.db")
    try:
        store.ensure_default_preset(log)
        # Owner edits the default (a text tweak on the narrate flow).
        row = store.default_preset(log)
        modules = [dict(m) for m in row.modules]
        modules[0]["content"] = "OWNER EDIT"
        store.append_preset(
            log,
            {
                **store.preset_to_json(row),
                "modules": modules,
                "flows": {**row.flows, "narrate": modules},
            },
        )

        # Engine upgrade advances the factory version.
        seeded = factory.current_factory_version()
        monkeypatch.setattr(factory, "FACTORY_VERSION", seeded + 1)
        result = store.ensure_default_preset(log)
        assert result.created is False
        assert result.factory_changed is True
        # Content is untouched by boot.
        after = store.default_preset(log)
        assert after.modules[0]["content"] == "OWNER EDIT"
        assert after.factory_version == seeded  # still the seeded version

        status = store.default_factory_status(log)
        assert status["present"] is True
        assert status["factory_changed"] is True
        assert status["seeded_version"] == seeded
        assert status["factory_version"] == seeded + 1
    finally:
        log.close()


# -- reset -------------------------------------------------------------------


def test_reset_reseeds_from_factory(tmp_path: Path):
    log = EventLog(tmp_path / "reset.db")
    try:
        store.ensure_default_preset(log)
        row = store.default_preset(log)
        modules = [dict(m) for m in row.modules]
        modules[0]["content"] = "TUNED"
        store.append_preset(log, {**store.preset_to_json(row), "preset_id": "default", "modules": modules})
        assert store.default_preset(log).modules[0]["content"] == "TUNED"

        reset = store.reset_default_preset(log)
        assert reset.modules[0]["content"] == ""
        assert reset.factory_version == factory.current_factory_version()
        assert store.default_factory_status(log)["factory_changed"] is False
    finally:
        log.close()


# -- artifact load/save + commit round-trip ----------------------------------


def test_artifact_round_trips_through_loader(tmp_path: Path, tmp_artifact: Path):
    log = EventLog(tmp_path / "commit.db")
    try:
        store.ensure_default_preset(log)
        row = store.default_preset(log)
        modules = [dict(m) for m in row.modules]
        modules[0]["content"] = "COMMITTED TWEAK"
        store.append_preset(log, {**store.preset_to_json(row), "preset_id": "default", "modules": modules})

        result = store.commit_default_to_factory(log)
        assert Path(result.artifact_path) == tmp_artifact
        assert tmp_artifact.exists()

        document = json.loads(tmp_artifact.read_text(encoding="utf-8"))
        assert "_comment" in document and "factory image" in document["_comment"]
        assert document["factory_version"] == factory.current_factory_version()
        assert document["preset_id"] == "default"
        assert document["is_default"] is True
        assert document["modules"][0]["content"] == "COMMITTED TWEAK"

        # Loader round-trip: the image reads back the committed content.
        image = factory.factory_image()
        assert image.source == "artifact"
        assert image.payload["modules"][0]["content"] == "COMMITTED TWEAK"

        # Commit re-stamped the row to the current factory version.
        assert store.default_preset(log).factory_version == factory.current_factory_version()
        assert store.default_factory_status(log)["factory_changed"] is False
    finally:
        log.close()


def test_commit_without_default_is_lookup_error(tmp_path: Path):
    log = EventLog(tmp_path / "nocommit.db")
    try:
        with pytest.raises(LookupError):
            store.commit_default_to_factory(log)
    finally:
        log.close()


def test_missing_artifact_falls_back_to_code(tmp_path: Path, tmp_artifact: Path):
    assert not tmp_artifact.exists()
    image = factory.factory_image()
    assert image.source == "code"
    assert image.factory_version == factory.current_factory_version()
    assert image.payload["preset_id"] == "default"


def test_seed_uses_committed_artifact_when_present(tmp_path: Path, tmp_artifact: Path):
    # Commit a deliberately tuned image, then seed a fresh log from it.
    # Version-agnostic: the artifact is written AT the code factory's version,
    # so "seed at the image version -> no factory-changed notice" holds at any
    # bump (the 7/7 literal rotted at v8).
    artifact_version = factory.current_factory_version()
    factory.save_artifact(
        {
            "preset_id": "default",
            "name": "default",
            "modules": [
                {
                    "id": "narrator_system",
                    "name": "Narrator system prompt",
                    "type": "narrator_system",
                    "role": "system",
                    "content": "",
                    "enabled": True,
                }
            ],
            "flows": {"narrate": []},
        },
        factory_version=artifact_version,
        path=tmp_artifact,
    )
    log = EventLog(tmp_path / "from_art.db")
    try:
        result = store.ensure_default_preset(log)
        assert result.seeded_version == artifact_version
        # Artifact at the code factory version -> no notice (seed is at the image version).
        assert result.factory_changed is False
        assert store.default_preset(log).factory_version == artifact_version
    finally:
        log.close()


def test_prompts_artifact_path_is_not_gitignored():
    root = factory.repo_root()
    ignore = (root / ".gitignore").read_text(encoding="utf-8")
    for raw in ignore.splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            assert not line.rstrip("/") == "prompts", f"prompts/ is gitignored: {raw!r}"
            assert "factory-defaults" not in line
    # The artifact lives under prompts/ (in-repo, versioned).
    assert REAL_ARTIFACT_RELPATH.as_posix() == "prompts/factory-defaults.json"


# -- undeletable default -----------------------------------------------------


def test_default_preset_is_undeletable(tmp_path: Path):
    log = EventLog(tmp_path / "undelete.db")
    try:
        store.ensure_default_preset(log)
        with pytest.raises(ValueError):
            store.delete_preset(log, store.DEFAULT_PRESET_ID)
        assert store.default_preset(log) is not None
    finally:
        log.close()


def test_delete_normal_preset_tombstones(tmp_path: Path):
    log = EventLog(tmp_path / "delete.db")
    try:
        pid = store.append_preset(log, {"name": "Doomed"})
        assert store.get_preset(log, pid) is not None
        store.delete_preset(log, pid)
        assert store.get_preset(log, pid) is None
        assert store.list_presets(log) == []
        with pytest.raises(ValueError):
            store.delete_preset(log, pid)
    finally:
        log.close()


def test_serialization_round_trips_default_fields(tmp_path: Path):
    log = EventLog(tmp_path / "ser.db")
    try:
        store.ensure_default_preset(log)
        row = store.default_preset(log)
        again = store.preset_from_json(store.preset_to_json(row))
        assert again.is_default is True
        assert again.factory_version == row.factory_version
    finally:
        log.close()


# -- engine integration (the persisted default is what the narrator compiles) --


def _turn_state(sid: str) -> dict:
    return {
        "session_id": sid,
        "user_content": "look around",
        "kind": "in_character",
        "facts": "<state>\ntime: 09:00\n</state>",
        "recall": "",
        "rolls": "",
    }


def test_engine_compiles_persisted_default_and_tweak_reflects(tmp_path: Path):
    from fabulist.wiring import make_narrator_message_builder

    log = EventLog(tmp_path / "engine.db")
    try:
        sid = "w7a"
        log.append_event(sid, "user", "user", {"text": "hi"})
        store.ensure_default_preset(log)
        # No preset_active pointer; the engine still uses the persisted default.
        assert store.active_preset(log).preset_id == "default"
        build = make_narrator_message_builder(log)
        before = build(_turn_state(sid))
        assert all(m["role"] in ("system", "user", "assistant") for m in before)

        # Edit the default's narrate flow (a text row) -> next compile reflects it.
        row = store.default_preset(log)
        modules = [dict(m) for m in row.modules]
        modules.append(
            {
                "id": "tweak-row",
                "name": "Tweak",
                "type": "text",
                "role": "system",
                "content": "W7A-MARKER",
                "enabled": True,
            }
        )
        store.append_preset(
            log,
            {**store.preset_to_json(row), "modules": modules, "flows": {**row.flows, "narrate": modules}},
        )
        after = build(_turn_state(sid))
        assert any("W7A-MARKER" in str(m.get("content", "")) for m in after)
    finally:
        log.close()


def test_reboot_preserves_owner_edit_and_engine_keeps_using_it(
    tmp_path: Path, monkeypatch
):
    from fabulist.wiring import make_narrator_message_builder

    db = tmp_path / "reboot.db"
    log = EventLog(db)
    store.ensure_default_preset(log)
    row = store.default_preset(log)
    modules = [dict(m) for m in row.modules]
    modules.append(
        {
            "id": "owner",
            "name": "Owner",
            "type": "text",
            "role": "system",
            "content": "OWNER-KEPT",
            "enabled": True,
        }
    )
    store.append_preset(
        log,
        {**store.preset_to_json(row), "modules": modules, "flows": {**row.flows, "narrate": modules}},
    )
    log.close()

    # "Restart": reopen the same DB and re-run boot with a bumped factory.
    monkeypatch.setattr(factory, "FACTORY_VERSION", factory.current_factory_version() + 1)
    log2 = EventLog(db)
    try:
        result = store.ensure_default_preset(log2)
        assert result.created is False
        assert result.factory_changed is True
        row2 = store.default_preset(log2)
        assert any(m.get("content") == "OWNER-KEPT" for m in row2.modules)
        log2.append_event("s", "user", "user", {"text": "hi"})
        build = make_narrator_message_builder(log2)
        msgs = build(_turn_state("s"))
        assert any("OWNER-KEPT" in str(m.get("content", "")) for m in msgs)
    finally:
        log2.close()


# -- API surface -------------------------------------------------------------


def test_get_default_returns_persisted_row_after_seed(build_app):
    build, _ = build_app
    app = build()
    store.ensure_default_preset(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/prompts/default")

    body = run(scenario()).json()
    preset = body["preset"]
    assert preset["preset_id"] == "default"
    assert preset["is_default"] is True
    assert body["present"] is True
    assert body["factory_changed"] is False
    # The default row is reachable directly through the normal preset path.
    async def direct():
        async with client_for(app) as client:
            return await client.get("/api/prompts/presets/default")

    assert run(direct()).json()["preset"]["preset_id"] == "default"


def test_get_default_pre_boot_returns_factory_shape(build_app, tmp_artifact):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/prompts/default")

    body = run(scenario()).json()
    assert body["present"] is False
    assert body["preset"]["preset_id"] == "default"
    assert set(body["preset"]["flows"]) == set(INTERACTIONS)


def test_put_default_edits_and_preserves_metadata(build_app):
    build, _ = build_app
    app = build()
    store.ensure_default_preset(app.state.log)
    original = store.default_preset(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.put(
                "/api/prompts/presets/default",
                json={
                    "name": "default",
                    "modules": [
                        {
                            "id": "narrator_system",
                            "name": "Narrator system prompt",
                            "type": "narrator_system",
                            "role": "system",
                            "content": "",
                            "enabled": True,
                        },
                        {
                            "id": "tweak",
                            "name": "Tweak",
                            "type": "text",
                            "role": "system",
                            "content": "NARRATE TWEAK",
                            "enabled": True,
                        },
                    ],
                },
            )

    body = run(scenario()).json()["preset"]
    assert body["is_default"] is True
    assert body["factory_version"] == original.factory_version
    assert body["modules"][1]["content"] == "NARRATE TWEAK"
    # Persisted in the log (log-owned, survives restart by construction).
    assert store.default_preset(app.state.log).modules[1]["content"] == "NARRATE TWEAK"


def test_patch_default_edits_single_module(build_app):
    build, _ = build_app
    app = build()
    store.ensure_default_preset(app.state.log)
    row = store.default_preset(app.state.log)
    modules = [dict(m) for m in row.modules]
    modules[0]["content"] = "PATCHED"

    async def scenario():
        async with client_for(app) as client:
            return await client.patch(
                "/api/prompts/presets/default", json={"modules": modules}
            )

    body = run(scenario()).json()["preset"]
    assert body["is_default"] is True
    assert body["modules"][0]["content"] == "PATCHED"


def test_default_delete_is_403(build_app):
    build, _ = build_app
    app = build()
    store.ensure_default_preset(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.delete("/api/prompts/presets/default")

    assert run(scenario()).status_code == 403


def test_delete_unknown_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.delete("/api/prompts/presets/ghost")

    assert run(scenario()).status_code == 404


def test_reset_route_reseeds(build_app):
    build, _ = build_app
    app = build()
    store.ensure_default_preset(app.state.log)
    row = store.default_preset(app.state.log)
    modules = [dict(m) for m in row.modules]
    modules[0]["content"] = "TUNED"
    store.append_preset(
        app.state.log,
        {**store.preset_to_json(row), "preset_id": "default", "modules": modules},
    )

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/prompts/default/reset")

    body = run(scenario()).json()
    assert body["preset"]["modules"][0]["content"] == ""
    assert body["factory_changed"] is False


def test_commit_route_writes_artifact(build_app, tmp_artifact):
    build, _ = build_app
    app = build()
    store.ensure_default_preset(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/prompts/default/commit")

    body = run(scenario()).json()
    assert body["artifact_path"] == str(tmp_artifact)
    assert tmp_artifact.exists()
    assert json.loads(tmp_artifact.read_text())["preset_id"] == "default"


def test_commit_route_without_default_is_409(build_app, tmp_artifact):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/prompts/default/commit")

    assert run(scenario()).status_code == 409


# -- directives (W2): nothing baked at seed, committed lists survive ----------


def test_seeded_default_carries_no_directive_list(tmp_path: Path, tmp_artifact: Path):
    """The seed bakes NO directive list; an edited, committed list survives.

    Phase 1 (§1.2): the factory image never bakes a directive list — empty
    means INHERIT the code catalog — so the seeded default starts with an
    empty list and its export omits the key (artifact stays diff-free).
    Phase 2 (§3.2): once the owner materializes/edits the list on the default
    row, ``commit_default_to_factory`` must carry it into the artifact, and a
    re-seed from that artifact restores it (reset never drops directive data).
    """
    log = EventLog(tmp_path / "noseed.db")
    try:
        store.ensure_default_preset(log)
        row = store.default_preset(log)
        assert row.directives == []
        assert "directives" not in store.preset_to_json(row)
        assert store.preset_directives(row) is None  # inherit signal

        # Owner materializes + edits the list directly on the default row.
        rows = [directive_to_row(d) for d in default_directives()]
        rows[0] = {**rows[0], "content": "OWNER TIGHTENED"}
        store.append_preset(
            log,
            {
                **store.preset_to_json(row),
                "preset_id": "default",
                "directives": rows,
            },
        )
        assert store.default_preset(log).directives[0]["content"] == "OWNER TIGHTENED"

        result = store.commit_default_to_factory(log)
        assert Path(result.artifact_path) == tmp_artifact
        document = json.loads(tmp_artifact.read_text(encoding="utf-8"))
        assert document["directives"][0]["content"] == "OWNER TIGHTENED"
        assert len(document["directives"]) == 19

        # Loader round-trip: the image reads the committed list back.
        image = factory.factory_image()
        assert image.source == "artifact"
        assert image.payload["directives"][0]["content"] == "OWNER TIGHTENED"

        # A reset from that artifact re-seeds the list (not dropped).
        reset = store.reset_default_preset(log)
        assert reset.directives[0]["content"] == "OWNER TIGHTENED"
    finally:
        log.close()
