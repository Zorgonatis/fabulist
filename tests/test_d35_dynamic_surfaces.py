"""D35: dynamic surfaces default to expressions (library doc first).

Owner mandate: every dynamic/generatable content surface seeds its editable
module with a **resolvable expression** (``{{library_doc}}``) rather than baked
text, so a regenerated artifact reaches live DBs without a manual owner PUT.
Owners top/tail/override in the editor; preview runs forward through the SAME
builder. Adoption path: the factory-version change surfaces the D26
"factory changed — review/reset" notice for existing installs.

This suite proves, end to end against the real engine builder + the real
FastAPI preview route:

* the factory default's ``library_doc`` surface IS the expression and resolves
  to the generated doc;
* a persisted default carrying the expression compiles messages whose library
  module is the generated doc — never a literal ``{{library_doc}}``;
* an owner's top/tail text survives around the resolved doc, and content with no
  token is a pure override (no doc injected);
* the version bump surfaces the factory-changed notice for a pre-D35 seed;
* ``POST /api/prompts/preview`` output == the engine builder output for the same
  preset state (parity by construction, no duplicated resolution logic).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.prompts_api import router
from fabulist.graph.types import initial_turn_state
from fabulist.log import EventLog
from fabulist.prompts import factory
from fabulist.prompts import preset_store as store
from fabulist.prompts.library_mini import render_library_doc, resolve_library_doc
from fabulist.prompts.surfaces import (
    LIBRARY_DOC_EXPRESSION,
    factory_surfaces,
    preset_surfaces,
)

LIBRARY_DOC = render_library_doc()


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolate_artifact(tmp_path: Path, monkeypatch) -> Path:
    """Keep every test off the committed repo artifact (test hermeticity).

    Same pattern as ``tests/test_default_preset.py``: redirect the artifact path
    to a per-test tmp location so ``factory_image()`` deterministically resolves
    to the code image unless a test writes an artifact deliberately.
    """
    path = tmp_path / "prompts" / "factory-defaults.json"
    monkeypatch.setattr(factory, "DEFAULT_ARTIFACT_RELPATH", path)
    return path


@pytest.fixture
def build_log(tmp_path: Path):
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


def _seed_session(log: EventLog, sid: str = "s1") -> None:
    log.append_event(sid, "system", "system", {"card": {"name": "Lyra"}})
    log.append_event(sid, "user", "user", {"text": "look around"})


def _rewrite_default_surfaces(log: EventLog, **surfaces) -> None:
    """Supersede the persisted default row's surfaces (owner edit)."""
    row = store.default_preset(log)
    assert row is not None
    payload = store.preset_to_json(row)
    payload["surfaces"] = {**preset_surfaces(row), **surfaces}
    store.append_preset(log, payload)


# -- factory seed ------------------------------------------------------------


def test_factory_library_surface_is_the_expression():
    surfaces = factory_surfaces()
    assert surfaces["library_doc"] == LIBRARY_DOC_EXPRESSION == "{{library_doc}}"
    # Resolving it yields the generated doc (the variable is not left literal).
    assert resolve_library_doc(surfaces) == LIBRARY_DOC
    assert "{{library_doc}}" not in resolve_library_doc(surfaces)


def test_engine_compiles_persisted_default_expression(build_log):
    from fabulist.wiring import make_narrator_message_builder

    log = build_log()
    sid = "s1"
    _seed_session(log, sid)
    result = store.ensure_default_preset(log)
    assert result.factory_changed is False
    assert store.default_preset(log).surfaces["library_doc"] == LIBRARY_DOC_EXPRESSION

    build = make_narrator_message_builder(log)
    messages = build(initial_turn_state(sid, "look around", "narrate"))
    joined = "\n".join(str(m.get("content", "")) for m in messages)
    # The library module carries the generated doc, never the raw token.
    assert LIBRARY_DOC in joined
    assert "{{library_doc}}" not in joined


# -- version bump notice (deliberate adoption) -------------------------------


def test_pre_d35_seed_surfaces_factory_changed_notice(tmp_path: Path):
    """A persisted default seeded at the pre-D35 version advertises the notice."""
    # Simulate the pre-D35 committed image: baked doc, factory_version 1.
    factory.save_artifact(
        {**factory.code_default_payload(), "surfaces": {"library_doc": "BAKED OLD DOC"}},
        factory_version=1,
        path=factory.artifact_path(),
    )
    log = EventLog(tmp_path / "bump.db")
    try:
        seeded = store.ensure_default_preset(log)
        assert seeded.created is True
        assert seeded.seeded_version == 1
        # Running code is at the D35 version -> the row is stale by construction.
        assert factory.current_factory_version() >= 2
        assert seeded.factory_changed is True
        status = store.default_factory_status(log)
        assert status["factory_changed"] is True
        assert status["seeded_version"] == 1
        # Boot never overwrites the baked content — adoption is owner-driven.
        assert store.default_preset(log).surfaces["library_doc"] == "BAKED OLD DOC"
        # The D35 engine upgrade commits the expression seed to the artifact;
        # the owner's reset then adopts it and the notice clears.
        factory.save_artifact(
            factory.code_default_payload(),
            factory_version=factory.current_factory_version(),
            path=factory.artifact_path(),
        )
        reset = store.reset_default_preset(log)
        assert reset.surfaces["library_doc"] == LIBRARY_DOC_EXPRESSION
        assert store.default_factory_status(log)["factory_changed"] is False
    finally:
        log.close()


# -- owner top/tail/override through the engine ------------------------------


def test_owner_top_tail_around_expression_reaches_engine(build_log):
    from fabulist.wiring import make_narrator_message_builder

    log = build_log()
    sid = "s1"
    _seed_session(log, sid)
    store.ensure_default_preset(log)
    _rewrite_default_surfaces(
        log, library_doc="EXTRA-FRAMING {{char}}\n{{library_doc}}\nTRAILING-LINE"
    )

    messages = make_narrator_message_builder(log)(
        initial_turn_state(sid, "look around", "narrate")
    )
    joined = "\n".join(str(m.get("content", "")) for m in messages)
    assert "EXTRA-FRAMING [cast: Lyra]" in joined
    assert LIBRARY_DOC in joined
    assert "TRAILING-LINE" in joined
    assert "{{library_doc}}" not in joined


def test_owner_pure_override_reaches_engine(build_log):
    from fabulist.wiring import make_narrator_message_builder

    log = build_log()
    sid = "s1"
    _seed_session(log, sid)
    store.ensure_default_preset(log)
    _rewrite_default_surfaces(log, library_doc="MY OWN COMPONENT GUIDE")

    messages = make_narrator_message_builder(log)(
        initial_turn_state(sid, "look around", "narrate")
    )
    joined = "\n".join(str(m.get("content", "")) for m in messages)
    assert "MY OWN COMPONENT GUIDE" in joined
    # Pure override: the generated doc is NOT injected.
    assert LIBRARY_DOC not in joined


def test_library_doc_variable_resolves_in_arbitrary_text_module(build_log):
    """D35: the registered variable composes like any other {{var}} (D18)."""
    from fabulist.wiring import make_narrator_message_builder

    log = build_log()
    sid = "s1"
    _seed_session(log, sid)
    store.ensure_default_preset(log)
    row = store.default_preset(log)
    modules = [dict(m) for m in row.modules]
    modules.append(
        {
            "id": "d35-ref",
            "name": "D35 ref",
            "type": "text",
            "role": "system",
            "content": "REFERENCE COPY:\n{{library_doc}}",
            "enabled": True,
        }
    )
    store.append_preset(
        log, {**store.preset_to_json(row), "modules": modules, "flows": {**row.flows, "narrate": modules}}
    )

    messages = make_narrator_message_builder(log)(
        initial_turn_state(sid, "look around", "narrate")
    )
    joined = "\n".join(str(m.get("content", "")) for m in messages)
    assert "REFERENCE COPY:" in joined
    assert LIBRARY_DOC in joined
    assert "{{library_doc}}" not in joined


# -- preview parity (run-forward) --------------------------------------------


def test_preview_parity_with_engine_builder(build_app):
    """Preview output == the engine builder output for the same preset state."""
    from fabulist.wiring import make_narrator_message_builder

    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_session(log)
    store.ensure_default_preset(log)
    # Mount the REAL production builder (what wiring.build_app does).
    builder = make_narrator_message_builder(log)
    app.state.message_builder = builder

    body = {"session_id": "s1", "user_text": "look around"}

    async def scenario():
        async with client_for(app) as client:
            return await client.post("/api/prompts/preview", json=body)

    resp = run(scenario())
    assert resp.status_code == 200
    preview = resp.json()
    assert preview["preview_mode"] == "builder"

    # The engine-compiled reference for the identical preset state, from an
    # independently constructed builder (not just re-calling the mounted one).
    reference_builder = make_narrator_message_builder(log)
    reference = reference_builder(initial_turn_state("s1", "look around", "narrate"))
    assert preview["messages"] == reference

    joined = "\n".join(str(m.get("content", "")) for m in preview["messages"])
    assert LIBRARY_DOC in joined  # the generated doc, resolved by the expression
    assert "{{library_doc}}" not in joined


def test_preview_parity_for_owner_top_tail(build_app):
    from fabulist.wiring import make_narrator_message_builder

    build, _ = build_app
    app = build()
    log = app.state.log
    _seed_session(log)
    store.ensure_default_preset(log)
    _rewrite_default_surfaces(log, library_doc="TOP-MARK {{library_doc}} BOTTOM-MARK")
    app.state.message_builder = make_narrator_message_builder(log)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/prompts/preview",
                json={"session_id": "s1", "user_text": "look around"},
            )

    preview = run(scenario()).json()
    joined = "\n".join(str(m.get("content", "")) for m in preview["messages"])
    assert "TOP-MARK" in joined and "BOTTOM-MARK" in joined
    assert LIBRARY_DOC in joined
    assert "{{library_doc}}" not in joined
