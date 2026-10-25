"""Tests for the D33 experience-profile envelope (schema + export + import + API).

Acceptance coverage (D33):

1. round-trip portability: log A -> export -> import into empty log B -> export
   from B; both envelopes are canonically equal;
2. idempotence: importing the same envelope twice appends zero rows on every
   anchor session;
3. one changed book entry appends exactly one book row and nothing else;
4. an unknown agent binding falls back to ``DEFAULT_BINDINGS`` (via the real
   resolver) with a warning;
5. a preset id of ``"default"`` is imported under a suffixed new id (warning),
   the persisted default id is untouched;
6. the exporter reads only the five anchor sessions (a chat session is never
   read), and its output equals the fold of those anchors.

No pytest-asyncio in this repo, so async API scenarios run via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.assistants_api import assistant_definitions as api_assistant_definitions
from fabulist.api.profiles_api import profiles_router
from fabulist.context.books import BOOKS_ANCHOR, ENTRY_ID_KEY, append_book, book_by_id
from fabulist.log import EventLog
from fabulist.personas.registry import PERSONAS_ANCHOR, append_persona
from fabulist.profiles import (
    canonical_json,
    export_profile,
    import_profile,
)
from fabulist.profiles.schema import (
    PROVENANCE_META_KEY,
    ASSISTANTS_ANCHOR,
    ASSISTANTS_EVENT,
    ASSISTANT_DEFINED_EVENT,
    ASSISTANT_DELETED_EVENT,
    LEGACY_SIDEKICKS_ANCHOR,
    LEGACY_SIDEKICKS_EVENT,
    entity_signature,
    assistant_definitions,
)
from fabulist.prompts.agent_sets import DEFAULT_BINDINGS, resolve_agent
from fabulist.prompts.preset_store import (
    DEFAULT_PRESET_ID,
    PRESETS_ANCHOR,
    append_preset,
    get_preset,
    list_presets,
    preset_to_json,
)
from fabulist.worlds.registry import (
    WORLDS_ANCHOR,
    append_world,
    world_by_id,
    world_to_wire,
)

#: A fixed export timestamp so two exports compare byte-for-byte.
STAMP = "2026-01-01T00:00:00+00:00"

#: The local connection-registry agent names the import validates against.
KNOWN_AGENTS = {"narrator", "extractor", "summarizer", "captioner"}

#: Every anchor session the exporter/importer may touch (D33).
ANCHORS = [
    WORLDS_ANCHOR,
    PERSONAS_ANCHOR,
    BOOKS_ANCHOR,
    PRESETS_ANCHOR,
    ASSISTANTS_ANCHOR,
]


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


# -- builders ----------------------------------------------------------------


def _seed_experience(log: EventLog) -> tuple[str, str]:
    """Seed log A with a 2-cast world + 2 books + a custom preset + 1 assistant.

    Returns ``(world_id, preset_id)``. Everything lands on the anchor sessions
    (D31/D32) — the definition-only inputs a profile bundles.
    """
    b_world = append_book(
        log,
        {
            "name": "Emberfall lore",
            "entries": [
                {
                    "keys": ["ember"],
                    "content": "Embers fall each dusk.",
                    "insertion_order": 1,
                }
            ],
        },
    )
    b_alice = append_book(
        log,
        {
            "name": "Alice lore",
            "entries": [
                {
                    "keys": ["alice"],
                    "content": "Alice is a wry detective.",
                    "insertion_order": 1,
                }
            ],
        },
    )
    alice = append_persona(
        log,
        {
            "name": "Alice",
            "description": "A wry detective.",
            "first_mes": "You're late.",
            "book_ids": [b_alice],
            "alternate_greetings": ["Alt hello."],
            "extensions": {"depth_prompt": "x"},
            "provenance": {"creator": "me", "origin": "card:Alice", "imported_at": STAMP},
        },
    )
    bob = append_persona(log, {"name": "Bob", "description": "A quiet smith."})
    world_id = append_world(
        log,
        {
            "name": "Emberfall",
            "scenario": "The city burns each night.",
            "cast": [alice, bob],
            "book_ids": [b_world],
        },
    )
    preset_id = "custom-preset"
    append_preset(
        log,
        {
            "preset_id": preset_id,
            "name": "My Custom Preset",
            "modules": [
                {
                    "id": "m1",
                    "name": "System",
                    "type": "text",
                    "role": "system",
                    "content": "Narrate.",
                    "enabled": True,
                }
            ],
            "agent_bindings": {"narrate": {"agent": "narrator", "overrides": {}}},
        },
    )
    log.append_event(
        ASSISTANTS_ANCHOR,
        "system",
        "user",
        {
            "event": "assistants_defined",
            "definitions": [
                {
                    "id": "segfault",
                    "name": "SEGFAULT",
                    "persona": "A gleeful gremlin.",
                    "style_notes": "chaotic",
                    "agent": "narrator",
                    "trigger": "manual",
                    "keywords": [],
                }
            ],
        },
    )
    return world_id, preset_id


def _seed_chat_history(log: EventLog, session_id: str = "chat-1") -> None:
    """Seed a chat session with history the exporter must never read."""
    log.append_event(
        session_id, "system", "system", {"title": "A chat", "event": "session_created"}
    )
    log.append_event(
        session_id,
        "system",
        "system",
        {"card": {"name": "Alice", "description": "A wry detective."}, "first_mes": "Hi"},
    )
    log.append_event(session_id, "user", "user", {"text": "Hello", "kind": "in_character"})
    log.append_event(
        session_id, "narrative", "narrator", {"prose": "The night market stirs."}
    )


def _anchor_counts(log: EventLog) -> dict[str, int]:
    """Row count per anchor session (idempotence / no-extra-appends assertions)."""
    return {anchor: len(log.get_events(anchor)) for anchor in ANCHORS}


def _define_assistant(log: EventLog, **fields) -> None:
    """Append one CRUD ``assistant_defined`` event (per-id upsert, D47)."""
    log.append_event(
        ASSISTANTS_ANCHOR, "system", "user", {"event": ASSISTANT_DEFINED_EVENT, **fields}
    )


def _delete_assistant(log: EventLog, assistant_id: str) -> None:
    """Append one CRUD ``assistant_deleted`` tombstone (D47)."""
    log.append_event(
        ASSISTANTS_ANCHOR,
        "system",
        "user",
        {"event": ASSISTANT_DELETED_EVENT, "id": assistant_id},
    )


def _profile(log: EventLog, world_id: str, preset_id: str, name: str = "Once") -> dict:
    """Export the seeded experience into an envelope with a fixed timestamp."""
    return export_profile(
        log,
        name=name,
        world_id=world_id,
        preset_id=preset_id,
        include_assistants=True,
        exported_at=STAMP,
    )


# -- (1) round-trip portability ----------------------------------------------


def test_round_trip_portability(build_log):
    a = build_log("a.db")
    world_id, preset_id = _seed_experience(a)
    env_a = _profile(a, world_id, preset_id, name="Test Profile")

    b = build_log("b.db")
    result = import_profile(b, env_a, known_agents=KNOWN_AGENTS)
    assert result["warnings"] == []
    # 2 books + 2 cast personas + 1 world + 1 preset + 1 assistant list.
    assert [row["kind"] for row in result["imported"]].count("book") == 2
    assert [row["kind"] for row in result["imported"]].count("persona") == 2
    assert len(result["imported"]) == 7

    env_b = _profile(b, world_id, preset_id, name="Test Profile")
    assert canonical_json(env_a) == canonical_json(env_b)


# -- (2) idempotence ---------------------------------------------------------


def test_reimport_is_a_no_op(build_log):
    a = build_log("a.db")
    world_id, preset_id = _seed_experience(a)
    env = _profile(a, world_id, preset_id)

    b = build_log("b.db")
    first = import_profile(b, env, known_agents=KNOWN_AGENTS)
    assert first["imported"] and first["skipped"] == []

    before = _anchor_counts(b)
    second = import_profile(b, env, known_agents=KNOWN_AGENTS)
    after = _anchor_counts(b)

    assert second["imported"] == []
    assert second["skipped"]
    assert before == after


# -- (2b) entry keys: minted on import, invisible to the dedupe digest ------


def test_import_keys_book_entries_and_stays_idempotent(build_log):
    """W9.23 §3.4.3: an import keys every entry; a re-import is still a no-op.

    Profiles authored before the stable key carry unkeyed entries, so the import
    IS the migration: the filed proposal path is ``entries.<entry_id>``, and a
    fresh ``uuid4`` per import must NOT read as "the definition changed" (D33).
    """
    a = build_log("a.db")
    world_id, preset_id = _seed_experience(a)
    env = _profile(a, world_id, preset_id)
    for book in env["world"]["books"]:
        for entry in book["entries"]:
            entry.pop(ENTRY_ID_KEY, None)

    b = build_log("b.db")
    first = import_profile(b, env, known_agents=KNOWN_AGENTS)
    assert [row["kind"] for row in first["imported"]].count("book") == 2

    keys: dict[str, list[str]] = {}
    for book in env["world"]["books"]:
        folded = book_by_id(b, book["book_id"])
        assert folded is not None
        minted = [entry.get(ENTRY_ID_KEY) for entry in folded.entries]
        assert all(isinstance(key, str) and key for key in minted)
        assert len(set(minted)) == len(minted)
        keys[book["book_id"]] = minted

    before = _anchor_counts(b)
    second = import_profile(b, env, known_agents=KNOWN_AGENTS)
    assert second["imported"] == []
    assert before == _anchor_counts(b)
    for book in env["world"]["books"]:
        folded = book_by_id(b, book["book_id"])
        assert folded is not None
        assert [entry[ENTRY_ID_KEY] for entry in folded.entries] == keys[
            book["book_id"]
        ]


# -- (3) one changed book -> one book append ---------------------------------


def test_changed_book_appends_one_book_row_only(build_log):
    a = build_log("a.db")
    world_id, preset_id = _seed_experience(a)
    env = _profile(a, world_id, preset_id)

    b = build_log("b.db")
    import_profile(b, env, known_agents=KNOWN_AGENTS)

    changed = copy.deepcopy(env)
    changed["world"]["books"][0]["entries"][0]["content"] = "Embers no longer fall."

    before = _anchor_counts(b)
    result = import_profile(b, changed, known_agents=KNOWN_AGENTS)
    after = _anchor_counts(b)

    assert after[BOOKS_ANCHOR] == before[BOOKS_ANCHOR] + 1
    for anchor in ANCHORS:
        if anchor != BOOKS_ANCHOR:
            assert after[anchor] == before[anchor], anchor
    assert [row["kind"] for row in result["imported"]] == ["book"]


# -- (4) unknown agent binding -> DEFAULT_BINDINGS fallback ------------------


def test_unknown_agent_binding_falls_back_to_default(build_log):
    a = build_log("a.db")
    world_id, preset_id = _seed_experience(a)
    env = _profile(a, world_id, preset_id)
    env["preset"]["agent_bindings"] = {"narrate": {"agent": "mega-narrator", "overrides": {}}}

    b = build_log("b.db")
    result = import_profile(b, env, known_agents=KNOWN_AGENTS)

    warning = next(w for w in result["warnings"] if w.get("wanted") == "mega-narrator")
    assert warning["interaction"] == "narrate"
    assert warning["fell_back_to"] == DEFAULT_BINDINGS["narrate"]["agent"]

    folded = get_preset(b, preset_id)
    assert folded is not None
    assert folded.agent_bindings["narrate"]["agent"] == DEFAULT_BINDINGS["narrate"]["agent"]
    # The real resolver reaches the local default (D19).
    assert (
        resolve_agent(folded.agent_bindings, "narrate", fallback="narrator")
        == DEFAULT_BINDINGS["narrate"]["agent"]
    )


def test_typed_slot_bindings_import_verbatim_without_a_warning(build_log):
    """W25.5 (W25.2 residual): a declared slot spelling is a KNOWN agent.

    ``completion.extraction`` names no key in the connections document the test
    injects, but it IS a taxonomy-declared slot — the same union
    ``validate_bindings`` applies — so the import preserves the binding byte for
    byte instead of rewriting it to the local default.
    """
    a = build_log("a.db")
    world_id, preset_id = _seed_experience(a)
    env = _profile(a, world_id, preset_id)
    env["preset"]["agent_bindings"] = {
        "npc": {"agent": "completion.extraction", "overrides": {"temperature": 0.2}},
        "narrate": {"agent": "completion.narration", "overrides": {}},
    }

    b = build_log("b.db")
    result = import_profile(b, env, known_agents=KNOWN_AGENTS)

    assert [w for w in result["warnings"] if w.get("type") == "agent_binding"] == []
    folded = get_preset(b, preset_id)
    assert folded is not None
    assert folded.agent_bindings["npc"] == {
        "agent": "completion.extraction",
        "overrides": {"temperature": 0.2},
    }
    assert folded.agent_bindings["narrate"]["agent"] == "completion.narration"


def test_an_undeclared_dotted_agent_is_still_rejected(build_log):
    """The union is the declared vocabulary only — a typo stays a typo."""
    a = build_log("a.db")
    world_id, preset_id = _seed_experience(a)
    env = _profile(a, world_id, preset_id)
    env["preset"]["agent_bindings"] = {
        "npc": {"agent": "completion.bogus", "overrides": {}}
    }

    b = build_log("b.db")
    result = import_profile(b, env, known_agents=KNOWN_AGENTS)

    warning = next(w for w in result["warnings"] if w.get("wanted") == "completion.bogus")
    assert warning["interaction"] == "npc"
    assert warning["fell_back_to"] == DEFAULT_BINDINGS["npc"]["agent"]
    folded = get_preset(b, preset_id)
    assert folded is not None
    assert folded.agent_bindings["npc"]["agent"] == DEFAULT_BINDINGS["npc"]["agent"]


# -- (5) default preset id remap ---------------------------------------------


def test_default_preset_id_is_remapped(build_log):
    a = build_log("a.db")
    world_id, preset_id = _seed_experience(a)
    env = _profile(a, world_id, preset_id, name="Once")
    env["preset"]["preset_id"] = DEFAULT_PRESET_ID

    b = build_log("b.db")
    result = import_profile(b, env, known_agents=KNOWN_AGENTS)

    warning = next(w for w in result["warnings"] if w.get("wanted") == DEFAULT_PRESET_ID)
    assert warning["assigned"] == f"{DEFAULT_PRESET_ID}-once"

    # The persisted default id is never written to.
    assert get_preset(b, DEFAULT_PRESET_ID) is None
    ids = [preset.preset_id for preset in list_presets(b)]
    assert warning["assigned"] in ids
    assert DEFAULT_PRESET_ID not in ids


# -- (6) structural: exporter reads only anchors -----------------------------


def test_exporter_reads_only_anchor_sessions(build_log, monkeypatch):
    a = build_log("a.db")
    world_id, preset_id = _seed_experience(a)
    _seed_chat_history(a, "chat-1")

    reads: list[str] = []
    original = EventLog.get_events

    def spy(self, session_id, **kwargs):
        reads.append(session_id)
        return original(self, session_id, **kwargs)

    monkeypatch.setattr(EventLog, "get_events", spy)

    env = _profile(a, world_id, preset_id)

    assert "chat-1" not in reads
    assert set(reads) <= set(ANCHORS)

    # The envelope equals the fold of those anchors.
    world = world_by_id(a, world_id)
    assert env["world"]["world"] == world_to_wire(world)
    assert env["preset"] == preset_to_json(get_preset(a, preset_id))
    assert env["assistants"] == assistant_definitions(a)
    assert {b["name"] for b in env["world"]["books"]} == {"Emberfall lore", "Alice lore"}


# -- import provenance stamp -------------------------------------------------


def test_import_stamps_provenance_in_event_meta(build_log):
    """D33: the supersede append is stamped with origin / imported_at.

    The stamp rides event meta, so the definition payload (and its signature) is
    untouched — the reason a re-import is a no-op.
    """
    a = build_log("a.db")
    world_id, preset_id = _seed_experience(a)
    env = _profile(a, world_id, preset_id, name="Stamped")

    b = build_log("b.db")
    import_profile(b, env, known_agents=KNOWN_AGENTS, now=STAMP)

    event = b.get_events(WORLDS_ANCHOR)[0]
    assert event.meta[PROVENANCE_META_KEY] == {
        "origin": "profile:Stamped",
        "imported_at": STAMP,
    }
    assert event.payload["provenance"] == {}  # payload unperturbed


# -- unknown top-level keys (forward-compat) ---------------------------------


def test_unknown_top_level_keys_survive_import_export(build_log):
    a = build_log("a.db")
    world_id, preset_id = _seed_experience(a)
    env = _profile(a, world_id, preset_id)
    env["future_key"] = {"nested": [1, 2, 3]}

    b = build_log("b.db")
    import_profile(b, env, known_agents=KNOWN_AGENTS)

    reexported = _profile(b, world_id, preset_id)
    assert reexported["future_key"] == {"nested": [1, 2, 3]}


def test_worldless_profile_round_trips_preset_and_assistants(build_log):
    a = build_log("a.db")
    _, preset_id = _seed_experience(a)
    env = export_profile(
        a, name="Preset Only", preset_id=preset_id, include_assistants=True, exported_at=STAMP
    )
    assert env["world"] is None

    b = build_log("b.db")
    import_profile(b, env, known_agents=KNOWN_AGENTS)

    reexported = export_profile(
        b, name="Preset Only", preset_id=preset_id, include_assistants=True, exported_at=STAMP
    )
    assert canonical_json(env) == canonical_json(reexported)


# -- schema unit checks ------------------------------------------------------


def test_entity_signature_is_canonical_and_kind_namespaced():
    payload = {"b": 1, "a": 2}
    assert entity_signature("book", payload) == entity_signature("book", {"a": 2, "b": 1})
    assert entity_signature("book", payload) != entity_signature("world", payload)


def test_import_rejects_a_non_profile_envelope(build_log):
    log = build_log()
    with pytest.raises(ValueError, match="envelope"):
        import_profile(log, {"format": "something-else", "format_version": 1, "name": "x"})
    with pytest.raises(ValueError, match="format_version"):
        import_profile(log, {"format": "fabulist-profile", "format_version": 99, "name": "x"})
    with pytest.raises(ValueError, match="name"):
        import_profile(
            log, {"format": "fabulist-profile", "format_version": 1, "preset": {}}
        )


def test_export_requires_a_target_and_404s_unknown_ids(build_log):
    log = build_log()
    _seed_experience(log)
    with pytest.raises(ValueError):
        export_profile(log)
    with pytest.raises(LookupError, match="unknown world"):
        export_profile(log, world_id="ghost", preset_id="custom-preset")
    with pytest.raises(LookupError, match="unknown preset"):
        export_profile(log, preset_id="ghost")


# -- API routes --------------------------------------------------------------


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def build_app(tmp_path: Path):
    """App factory: each call gets its OWN log file (source vs target)."""
    built = []
    counter = {"n": 0}

    def _build():
        counter["n"] += 1
        app = create_app(FakeTurnEngine(), tmp_path / f"events-{counter['n']}.db")
        app.include_router(profiles_router)
        built.append(app)
        return app

    yield _build
    for app in built:
        app.state.log.close()


def test_export_route_downloads_json_and_404s(build_app):
    build = build_app
    app = build()
    world_id, preset_id = _seed_experience(app.state.log)

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            ok = await client.get(
                "/api/profiles/export",
                params={"world": world_id, "preset": preset_id, "assistants": 1, "name": "API"},
            )
            unknown_preset = await client.get(
                "/api/profiles/export", params={"preset": "ghost"}
            )
            unknown_world = await client.get(
                "/api/profiles/export", params={"world": "ghost", "preset": preset_id}
            )
            missing_preset = await client.get("/api/profiles/export")
            return ok, unknown_preset, unknown_world, missing_preset

    ok, unknown_preset, unknown_world, missing_preset = run(scenario())
    assert ok.status_code == 200
    assert "attachment" in ok.headers["content-disposition"]
    body = ok.json()
    assert body["format"] == "fabulist-profile"
    assert body["world"]["world"]["world_id"] == world_id
    assert unknown_preset.status_code == 404
    assert "unknown preset" in unknown_preset.json()["detail"]
    assert unknown_world.status_code == 404
    assert "unknown world" in unknown_world.json()["detail"]
    assert missing_preset.status_code == 422  # preset is required


def test_import_route_returns_result_and_422s_bad_envelope(build_app):
    build = build_app
    source = build()
    world_id, preset_id = _seed_experience(source.state.log)
    env = _profile(source.state.log, world_id, preset_id)

    target = build()

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=target), base_url="http://test"
        ) as client:
            imported = await client.post("/api/profiles/import", json=env)
            bad = await client.post(
                "/api/profiles/import", json={"format": "nope", "format_version": 1}
            )
            return imported, bad

    imported, bad = run(scenario())
    assert imported.status_code == 200
    body = imported.json()
    assert body["imported"]
    assert {row["kind"] for row in body["imported"]} == {
        "book",
        "persona",
        "world",
        "preset",
        "assistants",
    }
    assert bad.status_code == 422


# -- absent ≠ empty (owner 2026-10-11) ---------------------------------------


def test_absent_assistants_key_leaves_roster_untouched(build_log):
    """A plain export (no assistants flag) must never clear a local roster."""
    a = build_log("a.db")
    world_id, preset_id = _seed_experience(a)
    plain = export_profile(
        a, name="Plain", world_id=world_id, preset_id=preset_id, exported_at=STAMP
    )
    assert "assistants" not in plain  # omitted, not []

    b = build_log("b.db")
    _seed_experience(b)
    roster_before = assistant_definitions(b)
    counts_before = _anchor_counts(b)
    result = import_profile(b, plain, known_agents=KNOWN_AGENTS)

    assert assistant_definitions(b) == roster_before
    # a's entities carry a's ids, so they append as NEW rows in b — correct.
    # The promise is narrower and stricter: the assistants anchor is untouched.
    assert len(b.get_events(ASSISTANTS_ANCHOR)) == counts_before[ASSISTANTS_ANCHOR]
    assert "assistants" not in {row["kind"] for row in result["imported"]}


def test_explicit_empty_assistants_clears_roster(build_log):
    """An explicit [] is the one intentional clear path (last-import-wins)."""
    a = build_log("a.db")
    world_id, preset_id = _seed_experience(a)
    env = export_profile(
        a,
        name="Empty",
        world_id=world_id,
        preset_id=preset_id,
        include_assistants=True,
        exported_at=STAMP,
    )
    env["assistants"] = []

    b = build_log("b.db")
    _seed_experience(b)
    assert assistant_definitions(b)  # precondition: non-empty roster

    result = import_profile(b, env, known_agents=KNOWN_AGENTS)
    assert assistant_definitions(b) == []
    assert any(row["kind"] == "assistants" for row in result["imported"])


# -- CRUD roster: per-id events fold like the assistants_api (D47) -----------


def test_assistant_fold_mirrors_crud_event_shapes(build_log):
    """The local fold matches ``assistants_api._fold_anchor``: upsert + tombstone."""
    log = build_log()
    _define_assistant(log, id="seg", name="Old SEG", persona="p")
    _define_assistant(log, id="seg", name="New SEG", persona="p")  # latest wins
    _define_assistant(log, id="greeter", name="Greeter", persona="p", actor="alice")
    _delete_assistant(log, "seg")  # removes one id, leaves the rest

    roster = assistant_definitions(log)
    assert [row["id"] for row in roster] == ["greeter"]
    assert roster[0]["actor"] == "alice"

    # A whole-list payload resets the fold (snapshot, not merge).
    log.append_event(
        ASSISTANTS_ANCHOR,
        "system",
        "user",
        {
            "event": ASSISTANTS_EVENT,
            "definitions": [{"id": "solo", "name": "Solo", "persona": "p"}],
        },
    )
    assert [row["id"] for row in assistant_definitions(log)] == ["solo"]


def test_assistant_fold_agrees_with_assistants_api(build_log):
    """Pin the profile fold to assistants_api._fold_anchor so the two cannot drift."""
    log = build_log()
    log.append_event(
        ASSISTANTS_ANCHOR,
        "system",
        "user",
        {
            "event": ASSISTANTS_EVENT,
            "definitions": [
                {"id": "a", "name": "A", "persona": "p"},
                {"id": "b", "name": "B", "persona": "p", "actor": "alice"},
            ],
        },
    )
    _define_assistant(log, id="c", name="C", persona="p")
    _define_assistant(log, id="a", name="A2", persona="p")  # upsert, latest wins
    _delete_assistant(log, "b")

    local = assistant_definitions(log)
    api = api_assistant_definitions(log)
    assert [row["id"] for row in local] == [definition.id for definition in api]
    assert {row["id"]: row["name"] for row in local}["a"] == "A2"


def test_assistant_fold_reads_legacy_sidekicks_anchor(build_log):
    """Pre-rename anchors still fold; a new-anchor event (even a tombstone) wins."""
    log = build_log()
    log.append_event(
        LEGACY_SIDEKICKS_ANCHOR,
        "system",
        "user",
        {
            "event": LEGACY_SIDEKICKS_EVENT,
            "definitions": [{"id": "old", "name": "Old", "persona": "p"}],
        },
    )
    assert [row["id"] for row in assistant_definitions(log)] == ["old"]

    # Any event on the new anchor defines the roster, so the legacy one is ignored.
    _delete_assistant(log, "ghost")
    assert assistant_definitions(log) == []


def test_crud_defined_assistant_is_exported(build_log):
    """A per-id ``assistant_defined`` roster reaches the envelope's assistants key."""
    log = build_log()
    append_preset(log, {"preset_id": "p1", "name": "P1"})
    _define_assistant(log, id="seg", name="SEGFAULT", persona="A gremlin.", actor="gremlin")

    env = export_profile(log, preset_id="p1", include_assistants=True, exported_at=STAMP)
    assert [row["id"] for row in env["assistants"]] == ["seg"]
    assert env["assistants"][0]["actor"] == "gremlin"


def test_tombstoned_assistant_is_not_exported(build_log):
    """A per-id tombstone removes that id from the exported roster."""
    log = build_log()
    append_preset(log, {"preset_id": "p1", "name": "P1"})
    _define_assistant(log, id="keep", name="Keep", persona="p")
    _define_assistant(log, id="gone", name="Gone", persona="p")
    _delete_assistant(log, "gone")

    env = export_profile(log, preset_id="p1", include_assistants=True, exported_at=STAMP)
    assert [row["id"] for row in env["assistants"]] == ["keep"]


def test_crud_roster_round_trip_preserves_actor(build_log):
    """Export -> import into a fresh log reproduces the roster and the actor binding."""
    a = build_log("a.db")
    append_preset(a, {"preset_id": "p1", "name": "P1"})
    _define_assistant(a, id="seg", name="SEGFAULT", persona="A gremlin.", actor="alice")
    _define_assistant(a, id="plain", name="Plain", persona="No actor.")
    env = export_profile(
        a, name="CRUD", preset_id="p1", include_assistants=True, exported_at=STAMP
    )
    # Absent stays absent: no default actor is injected.
    assert {row["id"]: row["actor"] for row in env["assistants"]} == {
        "seg": "alice",
        "plain": "",
    }

    b = build_log("b.db")
    import_profile(b, env, known_agents=KNOWN_AGENTS)
    assert assistant_definitions(b) == env["assistants"]

    reexported = export_profile(
        b, name="CRUD", preset_id="p1", include_assistants=True, exported_at=STAMP
    )
    assert canonical_json(reexported) == canonical_json(env)


def test_absent_key_leaves_crud_roster_untouched(build_log):
    """An envelope without the assistants key never touches a CRUD-built roster."""
    a = build_log("a.db")
    append_preset(a, {"preset_id": "p1", "name": "P1"})
    plain = export_profile(a, name="Plain", preset_id="p1", exported_at=STAMP)
    assert "assistants" not in plain

    b = build_log("b.db")
    _define_assistant(b, id="seg", name="SEGFAULT", persona="p")
    before = assistant_definitions(b)
    rows_before = len(b.get_events(ASSISTANTS_ANCHOR))

    result = import_profile(b, plain, known_agents=KNOWN_AGENTS)

    assert assistant_definitions(b) == before
    assert len(b.get_events(ASSISTANTS_ANCHOR)) == rows_before
    assert "assistants" not in {row["kind"] for row in result["imported"]}


def test_explicit_empty_clears_crud_roster(build_log):
    """An explicit ``[]`` resets a CRUD-built roster (the one clear path)."""
    a = build_log("a.db")
    append_preset(a, {"preset_id": "p1", "name": "P1"})
    env = export_profile(
        a, name="Empty", preset_id="p1", include_assistants=True, exported_at=STAMP
    )
    assert env["assistants"] == []

    b = build_log("b.db")
    _define_assistant(b, id="seg", name="SEGFAULT", persona="p")
    assert assistant_definitions(b)  # precondition: non-empty CRUD roster

    result = import_profile(b, env, known_agents=KNOWN_AGENTS)
    assert assistant_definitions(b) == []
    assert any(row["kind"] == "assistants" for row in result["imported"])
