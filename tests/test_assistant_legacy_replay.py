"""Legacy-replay gate for the sidekick -> assistant rename (zero migration).

The owner ratified a FULL rename (identifiers, files, event names, API routes)
with ONE hard constraint: the append-only event log owns history, so existing
sessions must replay/render byte-identically with **zero** migration of
``data/``. This module is the gate for that promise. It seeds raw *legacy*
spellings — a persisted preset carrying ``flows.sidekick``, an assistant roster
on the ``__sidekicks__`` anchor, a ``sidekick_reply`` event, and a profile
envelope carrying the legacy ``sidekicks`` key — and asserts the read-side shims
render exactly what the pre-rename code rendered.

No pytest-asyncio in this repo, so async API scenarios run via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.assistants_api import (
    ASSISTANTS_SESSION_ID,
    LEGACY_SIDEKICKS_SESSION_ID,
    router,
)
from fabulist.api.fake import FakeTurnEngine
from fabulist.log import EventLog
from fabulist.profiles import export_profile, import_profile
from fabulist.profiles.schema import (
    ASSISTANTS_ANCHOR,
    LEGACY_SIDEKICKS_ANCHOR,
    assistant_definitions,
)
from fabulist.prompts.modules import DEFAULT_FLOWS, compile_flow
from fabulist.prompts.preset_store import (
    PRESETS_ANCHOR,
    compile_preset_flow,
    get_preset,
)

STAMP = "2026-01-01T00:00:00+00:00"

#: The legacy event/definition payloads exactly as the pre-rename code wrote them.
LEGACY_DEFINITIONS = [
    {
        "id": "segfault",
        "name": "SEGFAULT",
        "persona": "A hacked-in assistant that monitors for CRITICAL ERRORS.",
        "style_notes": "emoji at the wrong moment",
        "agent": "narrator",
        "trigger": "keyword",
        "keywords": ["segfault", "error"],
    }
]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_log(tmp_path: Path):
    """Factory for a fresh file-backed :class:`EventLog`; closes all after."""
    built: list[EventLog] = []

    def _build(name: str = "events.db") -> EventLog:
        log = EventLog(tmp_path / name)
        built.append(log)
        return log

    yield _build
    for log in built:
        log.close()


def _seed_legacy_definitions(log: EventLog) -> None:
    """The pre-rename writer: definitions on ``__sidekicks__``, legacy kind."""
    log.append_event(
        LEGACY_SIDEKICKS_ANCHOR,
        "system",
        "user",
        {"event": "sidekicks_defined", "definitions": LEGACY_DEFINITIONS},
    )


def _legacy_preset_flow() -> list[dict]:
    """The pre-rename assistant flow rows: ``sidekick_*`` ids."""
    return [
        {
            "id": "sidekick_system",
            "name": "Sidekick system prompt",
            "type": "system",
            "role": "system",
            "content": "",
            "enabled": True,
        },
        {
            "id": "sidekick_history",
            "name": "Chat history",
            "type": "history",
            "role": "system",
            "content": "",
            "enabled": True,
        },
        {
            "id": "sidekick_user",
            "name": "Scene digest",
            "type": "user",
            "role": "user",
            "content": "",
            "enabled": True,
        },
    ]


def _seed_legacy_preset(log: EventLog, preset_id: str = "legacy") -> None:
    """A persisted preset written before the rename: ``flows.sidekick``."""
    log.append_event(
        PRESETS_ANCHOR,
        "system",
        "user",
        {
            "event": "preset",
            "preset_id": preset_id,
            "name": "legacy",
            "modules": [],
            "flows": {"sidekick": _legacy_preset_flow()},
            "agent_bindings": {"sidekick": {"agent": "narrator", "overrides": {}}},
        },
    )


# -- persisted preset: flows.sidekick folds onto assistant, renders identically


def test_legacy_preset_flow_renders_byte_identically(build_log):
    log = build_log()
    _seed_legacy_preset(log)
    preset = get_preset(log, "legacy")
    assert preset is not None

    # The legacy interaction key folded onto its canonical spelling...
    assert "sidekick" not in preset.flows
    assert "assistant" in preset.flows
    # ...and the legacy module ids were rewritten to the canonical prefix.
    assert [m["id"] for m in preset.flows["assistant"]] == [
        "assistant_system",
        "assistant_history",
        "assistant_user",
    ]
    # The legacy agent binding key folded too.
    assert "sidekick" not in preset.agent_bindings
    assert preset.agent_bindings["assistant"]["agent"] == "narrator"

    parts = {"system": "PERSONA", "history": [], "user_content": "DIGEST", "variables": {}}
    # Pre-rename, the persisted flow compiled to exactly this message list.
    assert compile_preset_flow(preset, "assistant", dict(parts)) == [
        {"role": "system", "content": "PERSONA"},
        {"role": "user", "content": "DIGEST"},
    ]
    # ...which is the default assistant flow's rendering (all-default, unedited).
    assert compile_preset_flow(preset, "assistant", dict(parts)) == compile_flow(
        "assistant", DEFAULT_FLOWS["assistant"], dict(parts)
    )


def test_legacy_module_ids_rewrite_without_legacy_key(build_log):
    log = build_log()
    log.append_event(
        PRESETS_ANCHOR,
        "system",
        "user",
        {
            "event": "preset",
            "preset_id": "half",
            "name": "half",
            "modules": [],
            # New key, but rows still carry the legacy ids (independent rewrite).
            "flows": {"assistant": _legacy_preset_flow()},
        },
    )
    preset = get_preset(log, "half")
    assert [m["id"] for m in preset.flows["assistant"]] == [
        "assistant_system",
        "assistant_history",
        "assistant_user",
    ]


# -- definitions fold: new anchor first, legacy __sidekicks__ fallback ---------


def test_definitions_fold_falls_back_to_legacy_anchor(build_log):
    log = build_log()
    _seed_legacy_definitions(log)
    app = create_app(FakeTurnEngine(), log.path)
    app.include_router(router)
    try:

        async def scenario():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return (await client.get("/api/assistants")).json()

        body = run(scenario())
    finally:
        app.state.log.close()

    assert body["meta"]["source"] == "log"
    assert body["meta"]["demo"] is False
    assert [a["id"] for a in body["assistants"]] == ["segfault"]
    assert body["assistants"][0]["name"] == "SEGFAULT"
    assert body["assistants"][0]["trigger"] == "keyword"


def test_definitions_fold_prefers_new_anchor_over_legacy(build_log):
    log = build_log()
    _seed_legacy_definitions(log)
    log.append_event(
        ASSISTANTS_ANCHOR,
        "system",
        "user",
        {
            "event": "assistants_defined",
            "definitions": [{"id": "muse", "name": "Muse", "persona": "A quiet muse."}],
        },
    )
    app = create_app(FakeTurnEngine(), log.path)
    app.include_router(router)
    try:

        async def scenario():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return (await client.get("/api/assistants")).json()

        body = run(scenario())
    finally:
        app.state.log.close()

    assert [a["id"] for a in body["assistants"]] == ["muse"]


# -- replies hydration: legacy sidekick_reply event reads back unchanged --------


def test_legacy_replies_hydrate_unchanged(build_log):
    log = build_log()
    session_id = "chat-legacy"
    log.append_event(
        session_id, "system", "system", {"event": "session_created", "title": "t"}
    )
    log.append_event(
        session_id,
        "system",
        "sidekick",
        {"event": "sidekick_reply", "sidekick": "segfault", "text": "SEGFAULT online."},
        meta={"agent": "narrator", "user_msg": "status?"},
    )
    app = create_app(FakeTurnEngine(), log.path)
    app.include_router(router)
    try:

        async def scenario():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                return (
                    await client.get(f"/api/sessions/{session_id}/assistants/replies")
                ).json()

        body = run(scenario())
    finally:
        app.state.log.close()

    assert body["session_id"] == session_id
    (reply,) = body["replies"]
    # The legacy payload key ``sidekick`` resolves to the canonical ``assistant``.
    assert reply["assistant"] == "segfault"
    assert reply["text"] == "SEGFAULT online."
    assert reply["agent"] == "narrator"
    assert reply["user_msg"] == "status?"


# -- profile envelope: legacy "sidekicks" key is the identical roster ----------


def _seed_exportable(log: EventLog) -> str:
    """Seed a legacy roster + a preset to export; return the preset id."""
    _seed_legacy_definitions(log)
    preset_id = "legacy-preset"
    log.append_event(
        PRESETS_ANCHOR,
        "system",
        "user",
        {
            "event": "preset",
            "preset_id": preset_id,
            "name": "Legacy Preset",
            "modules": [],
            "flows": {"sidekick": _legacy_preset_flow()},
        },
    )
    return preset_id


def test_export_reads_legacy_roster_new_key(build_log):
    log = build_log()
    preset_id = _seed_exportable(log)
    envelope = export_profile(
        log, preset_id=preset_id, include_assistants=True, exported_at=STAMP
    )
    # The new key is written; it equals the legacy-anchor fold.
    assert "sidekicks" not in envelope
    assert envelope["assistants"] == assistant_definitions(log)
    assert envelope["assistants"][0]["id"] == "segfault"


def test_import_accepts_legacy_sidekicks_key(build_log):
    source = build_log("source.db")
    preset_id = _seed_exportable(source)
    modern = export_profile(
        source, preset_id=preset_id, include_assistants=True, exported_at=STAMP
    )
    # Rewrite the roster key to the pre-rename spelling.
    legacy = dict(modern)
    legacy["sidekicks"] = legacy.pop("assistants")

    target = build_log("target.db")
    result = import_profile(target, legacy, known_agents={"narrator"})
    assert assistant_definitions(target) == modern["assistants"]
    assert any(row["kind"] == "assistants" for row in result["imported"])
    # A re-export of the identical roster writes the new key.
    reexported = export_profile(
        target, preset_id=preset_id, include_assistants=True, exported_at=STAMP
    )
    assert reexported["assistants"] == modern["assistants"]
    assert "sidekicks" not in reexported


def test_legacy_sidekicks_key_absent_leaves_roster_untouched(build_log):
    source = build_log("source.db")
    preset_id = _seed_exportable(source)
    legacy = export_profile(
        source, preset_id=preset_id, include_assistants=True, exported_at=STAMP
    )
    legacy["sidekicks"] = legacy.pop("assistants")
    legacy.pop("sidekicks")  # absent != empty: neither key present

    target = build_log("target.db")
    _seed_legacy_definitions(target)
    before = assistant_definitions(target)
    import_profile(target, legacy, known_agents={"narrator"})
    assert assistant_definitions(target) == before
    assert len(target.get_events(LEGACY_SIDEKICKS_ANCHOR)) == 1
    assert not target.get_events(ASSISTANTS_ANCHOR)


def test_legacy_explicit_empty_sidekicks_clears_roster(build_log):
    source = build_log("source.db")
    preset_id = _seed_exportable(source)
    legacy = export_profile(
        source, preset_id=preset_id, include_assistants=True, exported_at=STAMP
    )
    legacy["sidekicks"] = []  # explicit empty carries upsert semantics
    legacy.pop("assistants", None)

    target = build_log("target.db")
    _seed_legacy_definitions(target)
    assert assistant_definitions(target)  # precondition: non-empty roster

    import_profile(target, legacy, known_agents={"narrator"})
    # An explicit [] supersedes the legacy roster with an empty list.
    assert assistant_definitions(target) == []
    assert len(target.get_events(ASSISTANTS_ANCHOR)) == 1
