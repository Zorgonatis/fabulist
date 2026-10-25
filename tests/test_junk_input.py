"""Junk-input hardening: API-surface fuzz over the real app router set.

Every test drives the **real** FastAPI app built by
:func:`fabulist.api.create_app` (which mounts the full production router set:
sessions, settings, branches, attachments, assistants, cards, worldinfo,
revisions, search, export, state, inspector, stt — see ``api/app.py``) over a
tmp SQLite event log, using ``httpx``'s ASGI transport and the scripted
:class:`~fabulist.api.fake.FakeTurnEngine`. The fixture shape mirrors
``tests/test_state_api.py`` / ``tests/test_settings_api.py``.

The contract under test is deliberately weak: **junk input must never produce a
500**. Where the current behaviour is deterministic we additionally pin the
exact status code and, where meaningful, that the event log is untouched or the
junk round-trips faithfully. A 500 here means an unhandled exception escaped a
router — a bug report, not a test failure to paper over.

Ground truth for the append door
--------------------------------
``EventLog.append_event`` (``log/repo.py``) calls
:func:`fabulist.log.schema_gate.validate_event` and raises
:class:`~fabulist.log.schema_gate.LogSchemaError` **before** any transaction
opens; it does not catch or wrap the error. ``schema_gate`` treats a ``bool`` as
not-an-``int`` (``_matches``) and lets unknown keys through (D4). Those two
facts are asserted directly at the bottom of the file.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.log import EventLog
from fabulist.log.schema_gate import LogSchemaError, validate_event

#: Unicode torture string exercised end-to-end: accented Latin, emoji, RTL
#: (Hebrew), a zero-width space, a zero-width joiner, and a BOM.
UNICODE_CONTENT = "h\u00e9llo \U0001f600 \u05d0\u05d1\u05d2 \u200b\u200d\ufeff fin"

#: NUL + C0 control + ANSI escape junk.
CONTROL_CONTENT = "a\x00b\x07c\x1b[31mred"


def run(coro):
    """Drive a coroutine to completion (the suite has no pytest-asyncio)."""
    return asyncio.run(coro)


class FakeCaptioner:
    """Minimal ``ModelCaller`` stand-in so image uploads reach the store path.

    Mirrors ``tests/test_images.py``: the attachments router calls
    ``request.app.state.captioner`` (an injected :class:`ModelCaller`) after
    storing an image; without one it returns 503 (``attachments.py``
    ``_captioner_for``). Junk-upload tests that care about the storage path set
    this fake.
    """

    def __init__(self, text: str = "a test caption") -> None:
        self.text = text

    async def __call__(self, messages: list[dict], stream: bool = True):
        yield {"type": "content", "delta": self.text}


def _no_500(resp: httpx.Response) -> None:
    """Assert a response is anything but an unhandled server error."""
    assert resp.status_code != 500, (
        f"junk input escaped as a 500: {resp.status_code} {resp.text[:240]}"
    )


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for the real app over one tmp event log; closes logs after.

    Mirrors ``tests/test_state_api.py::build_app``; ``captioner=True`` wires the
    :class:`FakeCaptioner` seam so image uploads exercise the store path.
    """
    log_path = tmp_path / "events.db"
    built: list[Any] = []

    def _build(*, captioner: bool = False, **kwargs: Any):
        app = create_app(FakeTurnEngine(), log_path, **kwargs)
        if captioner:
            app.state.captioner = FakeCaptioner()
        built.append(app)
        return app

    yield _build, log_path
    for app in built:
        app.state.log.close()


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def _create_session(client: httpx.AsyncClient, title: str = "junk") -> str:
    resp = await client.post("/api/sessions", json={"title": title})
    assert resp.status_code == 200
    return resp.json()["session_id"]


async def _wait_user_event(log: EventLog, session_id: str, timeout: float = 5.0):
    """Return the last appended ``user`` event, waiting out the producer task.

    ``POST /turns`` schedules ``_drive_turn`` as a task; the user row is
    appended at task start. The SSE generator only completes when the turn is
    done, so the row normally exists when the response returns — this loop is
    belt-and-braces against scheduling order.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        users = [e for e in log.get_events(session_id) if e.type == "user"]
        if users:
            return users[-1]
        await asyncio.sleep(0.01)
    raise AssertionError("user event was never appended")


def _seed_revision_targets(log: EventLog) -> tuple[Any, Any]:
    """Append one editable ``user`` and one editable ``narrative`` row."""
    user = log.append_event(
        "s1", "user", "user", {"text": "orig", "kind": "in_character"}
    )
    narrative = log.append_event(
        "s1", "narrative", "narrator", {"prose": "old prose", "dsl": "x"}
    )
    return user, narrative


# ---------------------------------------------------------------------------
# 1. POST /api/sessions/{sid}/turns — junk content
# ---------------------------------------------------------------------------


def test_turn_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/ghost/turns", json={"content": "hi"}
            )

    resp = run(scenario())
    _no_500(resp)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown session"


@pytest.mark.parametrize(
    "content",
    [
        "",  # empty string: TurnRequest.content is a plain str, no min_length
        "   ",  # whitespace only
        "A" * 200_000,  # 200k chars, no crash
        CONTROL_CONTENT,  # NUL + C0 control + ANSI escape
        UNICODE_CONTENT,  # emoji + RTL + zero-width round-trip
    ],
    ids=["empty", "whitespace", "200k", "control", "unicode"],
)
def test_turn_junk_content_is_never_500(build_app, content):
    """No content validation exists in ``app.TurnRequest``: every body is a
    well-formed request. The turn starts (200 SSE) and the user row is appended
    verbatim — empty/whitespace content is NOT rejected with a 422."""
    build, _ = build_app
    app = build()
    log = app.state.log
    log.append_event(
        "s1", "system", "system", {"event": "session_created", "title": "T"}
    )

    async def scenario():
        async with client_for(app) as client:
            resp = await client.post("/api/sessions/s1/turns", json={"content": content})
            user = await _wait_user_event(log, "s1")
            return resp, user

    resp, user = run(scenario())
    _no_500(resp)
    assert resp.status_code == 200
    assert user.payload["text"] == content
    assert user.payload["kind"] == "in_character"
    # 200k survives the JSON→SQLite→JSON round-trip intact.
    assert len(user.payload["text"]) == len(content)


def test_turn_unicode_content_round_trips_json_in_log(build_app):
    """Emoji/RTL/zero-width bytes are stored byte-for-byte (ensure_ascii=False)."""
    build, _ = build_app
    app = build()
    log = app.state.log
    log.append_event(
        "s1", "system", "system", {"event": "session_created", "title": "T"}
    )

    async def scenario():
        async with client_for(app) as client:
            await client.post(
                "/api/sessions/s1/turns", json={"content": UNICODE_CONTENT}
            )
            return await _wait_user_event(log, "s1")

    user = run(scenario())
    assert user.payload["text"] == UNICODE_CONTENT
    # Re-serialize/re-parse to prove the DB text is not mangled.
    assert json.loads(json.dumps(user.payload))["text"] == UNICODE_CONTENT


# ---------------------------------------------------------------------------
# 2. POST /api/sessions/{sid}/revisions — junk edit bodies
# ---------------------------------------------------------------------------


def test_revision_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/ghost/revisions",
                json={"target_id": 1, "new_text": "x"},
            )

    resp = run(scenario())
    _no_500(resp)
    assert resp.status_code == 404


def test_revision_unknown_target_is_404(build_app):
    build, _ = build_app
    app = build()
    _seed_revision_targets(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": 99_999, "new_text": "x"},
            )

    resp = run(scenario())
    _no_500(resp)
    assert resp.status_code == 404
    assert "99999" in resp.json()["detail"]


def test_revision_empty_text_is_accepted_200(build_app):
    """``new_text`` is a plain str; empty text supersedes with ``""``."""
    build, _ = build_app
    app = build()
    user, _ = _seed_revision_targets(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": user.id, "new_text": ""},
            )

    resp = run(scenario())
    _no_500(resp)
    assert resp.status_code == 200
    # The projection now renders the empty replacement (latest wins).
    projection = app.state.log.get_projection("s1")
    assert [m["payload"]["text"] for m in projection["user_messages"]] == [""]


def test_revision_huge_text_is_accepted_200(build_app):
    build, _ = build_app
    app = build()
    user, _ = _seed_revision_targets(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": user.id, "new_text": "B" * 100_000},
            )

    resp = run(scenario())
    _no_500(resp)
    assert resp.status_code == 200
    projection = app.state.log.get_projection("s1")
    assert len(projection["user_messages"][0]["payload"]["text"]) == 100_000


def test_revision_of_a_revision_event_is_422(build_app):
    """``EDITABLE_TEXT_KEYS`` covers only user/narrative; a revision row is a
    non-editable type and is refused with a machine-readable reason."""
    build, _ = build_app
    app = build()
    user, _ = _seed_revision_targets(app.state.log)

    async def scenario():
        async with client_for(app) as client:
            first = await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": user.id, "new_text": "v2"},
            )
            rev_id = first.json()["revision_id"]
            second = await client.post(
                "/api/sessions/s1/revisions",
                json={"target_id": rev_id, "new_text": "v3"},
            )
            return first, second

    first, second = run(scenario())
    assert first.status_code == 200
    _no_500(second)
    assert second.status_code == 422
    detail = second.json()["detail"]
    assert detail["type"] == "revision"
    assert "not editable" in detail["reason"]


# ---------------------------------------------------------------------------
# 3. POST /api/sessions/{sid}/state — junk facts
# ---------------------------------------------------------------------------


def test_state_whitespace_key_is_422(build_app):
    """``edit_state`` strips the key and 422s on empty — existing guard."""
    build, _ = build_app
    app = build()
    app.state.log.append_event(
        "s1", "system", "system", {"event": "session_created", "title": "T"}
    )

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/state", json={"key": "   ", "value": "v"}
            )

    resp = run(scenario())
    _no_500(resp)
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason"] == "key must be non-empty"


def test_state_huge_value_is_accepted_and_round_trips(build_app):
    build, _ = build_app
    app = build()
    log = app.state.log
    log.append_event(
        "s1", "system", "system", {"event": "session_created", "title": "T"}
    )
    big = "V" * 100_000

    async def scenario():
        async with client_for(app) as client:
            posted = await client.post(
                "/api/sessions/s1/state", json={"key": "note", "value": big}
            )
            fetched = await client.get("/api/sessions/s1/state")
            return posted, fetched

    posted, fetched = run(scenario())
    _no_500(posted)
    assert posted.status_code == 200
    assert fetched.status_code == 200
    assert fetched.json()["state"]["note"] == big


def test_state_nul_in_key_is_accepted_and_folds_back(build_app):
    """A NUL is a valid bound SQLite parameter (only NUL in the *statement*
    raises), so a NUL key appends and folds; it is not a 500."""
    build, _ = build_app
    app = build()
    app.state.log.append_event(
        "s1", "system", "system", {"event": "session_created", "title": "T"}
    )
    key = "a\x00b"

    async def scenario():
        async with client_for(app) as client:
            posted = await client.post(
                "/api/sessions/s1/state", json={"key": key, "value": "v"}
            )
            fetched = await client.get("/api/sessions/s1/state")
            return posted, fetched

    posted, fetched = run(scenario())
    _no_500(posted)
    assert posted.status_code == 200
    assert posted.json()["key"] == key
    assert fetched.json()["state"][key] == "v"


# ---------------------------------------------------------------------------
# 4. POST /api/sessions/{sid}/attachments — garbage bytes / traversal names
# ---------------------------------------------------------------------------


def test_attachment_garbage_bytes_as_image_is_stored_content_addressed(build_app):
    """Garbage bytes declared ``image/*`` are sniffed as an image by the mime
    fallback (``av.sniff_kind``: magic unknown → declared prefix), then stored
    as an ``asset`` event whose id is the SHA-256 of the bytes (``store_image``).
    The captioner runs (fake), so this is a 200, never a 500."""
    build, log_path = build_app
    app = build(captioner=True)
    garbage = b"\x00\x01\x02 not really a png \xff\xfe"

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            resp = await client.post(
                f"/api/sessions/{sid}/attachments",
                files={"file": ("garbage.png", garbage, "image/png")},
            )
            return sid, resp

    sid, resp = run(scenario())
    _no_500(resp)
    assert resp.status_code == 200
    asset = resp.json()["asset"]
    assert asset["kind"] == "image"
    assert asset["sha256"] == hashlib.sha256(garbage).hexdigest()
    assert asset["asset_id"] == asset["sha256"]

    log = EventLog(log_path)
    payloads = [e.payload for e in log.get_events(sid) if e.type == "asset"]
    log.close()
    assert payloads and payloads[0]["sha256"] == hashlib.sha256(garbage).hexdigest()


def test_attachment_empty_file_is_handled_without_500(build_app):
    """An empty body declared ``image/png`` smells as image and stores zero
    bytes; the response is 200 (asset sha256 of empty), never a 500."""
    build, _ = build_app
    app = build(captioner=True)

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            return await client.post(
                f"/api/sessions/{sid}/attachments",
                files={"file": ("empty.png", b"", "image/png")},
            )

    resp = run(scenario())
    _no_500(resp)
    assert resp.status_code == 200
    assert resp.json()["asset"]["sha256"] == hashlib.sha256(b"").hexdigest()


def test_attachment_path_traversal_filename_writes_nothing_to_disk(build_app, tmp_path):
    """The router never reads ``file.filename`` — it uses ``await file.read()``
    and ``file.content_type`` only — and storage is a content-addressed sqlite
    ``asset`` row (``media/images.py::store_image`` / ``media/av.py::store_media``),
    never a filesystem path. So a ``../../etc/passwd.png`` filename cannot escape:
    the only files in the app's directory are the event-log DB files."""
    build, _ = build_app
    app = build(captioner=True)
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 24

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            resp = await client.post(
                f"/api/sessions/{sid}/attachments",
                files={"file": ("../../etc/passwd.png", png, "image/png")},
            )
            return sid, resp

    sid, resp = run(scenario())
    _no_500(resp)
    assert resp.status_code == 200
    asset = resp.json()["asset"]
    assert asset["sha256"] == hashlib.sha256(png).hexdigest()
    # No traversal component leaked into the stored metadata.
    assert "passwd" not in json.dumps(asset)

    # Storage is sqlite-backed: no media file (or any other file) appeared.
    names = {p.name for p in tmp_path.iterdir()}
    assert names <= {"events.db", "events.db-wal", "events.db-shm"}, names
    assert not any(p.name == "passwd.png" for p in tmp_path.rglob("*"))


def test_attachment_garbage_octet_stream_is_415(build_app):
    """Unknown magic + non-media mime → ``sniff_kind`` returns "unknown" and the
    router 415s (``attachments.py``); still never a 500."""
    build, _ = build_app
    app = build(captioner=True)

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            return await client.post(
                f"/api/sessions/{sid}/attachments",
                files={"file": ("g.bin", b"plain junk", "application/octet-stream")},
            )

    resp = run(scenario())
    _no_500(resp)
    assert resp.status_code == 415


def test_attachment_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build(captioner=True)

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/ghost/attachments",
                files={"file": ("x.png", b"\x89PNG\r\n\x1a\n", "image/png")},
            )

    resp = run(scenario())
    _no_500(resp)
    assert resp.status_code == 404


def test_attachment_without_captioner_is_503(build_app):
    """The documented guard: image upload with no wired captioner is 503."""
    build, _ = build_app
    app = build()  # no app.state.captioner

    async def scenario():
        async with client_for(app) as client:
            sid = await _create_session(client)
            return await client.post(
                f"/api/sessions/{sid}/attachments",
                files={"file": ("x.png", b"\x89PNG\r\n\x1a\n", "image/png")},
            )

    resp = run(scenario())
    _no_500(resp)
    assert resp.status_code == 503


# ---------------------------------------------------------------------------
# 5. URL-level junk — encoded NUL, oversized sid, non-UTF-8 body
# ---------------------------------------------------------------------------


def test_url_encoded_nul_in_session_id_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/%00/state")

    resp = run(scenario())
    _no_500(resp)
    assert resp.status_code == 404


def test_url_oversized_session_id_is_404(build_app):
    build, _ = build_app
    app = build()
    huge_sid = "x" * 10_000

    async def scenario():
        async with client_for(app) as client:
            return await client.get(f"/api/sessions/{huge_sid}/state")

    resp = run(scenario())
    _no_500(resp)
    assert resp.status_code == 404


@pytest.mark.parametrize(
    "path",
    ["/api/sessions/%00/state", "/api/sessions/%00/revisions"],
)
def test_url_encoded_nul_never_500_across_routers(build_app, path):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get(path)

    resp = run(scenario())
    _no_500(resp)
    assert resp.status_code in {404, 405}


def test_non_utf8_json_body_is_400_not_500(build_app):
    """Latin-1 mojibake bytes with an ``application/json`` content type are
    rejected at the body-parse boundary (FastAPI → 400); the router body never
    runs, so no 500."""
    build, log_path = build_app
    app = build()
    app.state.log.append_event(
        "s1", "system", "system", {"event": "session_created", "title": "T"}
    )
    raw = b'{"key":"k","value":"\xe9\xff"}'  # invalid UTF-8 bytes

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/s1/state",
                content=raw,
                headers={"Content-Type": "application/json"},
            )

    resp = run(scenario())
    _no_500(resp)
    assert resp.status_code == 400

    # Nothing was appended for the malformed request.
    log = EventLog(log_path)
    deltas = [e for e in log.get_events("s1") if e.type == "state-delta"]
    log.close()
    assert deltas == []


# ---------------------------------------------------------------------------
# 6. Schema gate (the append chokepoint) — direct
# ---------------------------------------------------------------------------


def test_gate_bool_does_not_satisfy_int_field():
    """``roll.result`` is the codebase's only int-typed payload field; a bool
    must not satisfy it (``schema_gate._matches`` excludes bool from int)."""
    assert validate_event("roll", {"dice": "1d20", "result": True}) == [
        "key 'result' expects int, got bool"
    ]


def test_gate_narrative_has_no_int_field_bool_is_rejected_where_typed():
    """Deviation from the task sketch, grounded in ``schema_gate.EVENT_SCHEMAS``:
    ``narrative`` has NO int-typed field (prose/dsl/reasoning: str, segments:
    list, config: dict), so "bool where int expected" cannot be expressed for
    it. The nearest faithful checks are bool-vs-str and bool-vs-list, plus the
    real int field on ``roll`` above."""
    assert validate_event("narrative", {"prose": True}) == [
        "key 'prose' expects str, got bool"
    ]
    assert validate_event("narrative", {"segments": True}) == [
        "key 'segments' expects list, got bool"
    ]


def test_gate_unknown_top_level_keys_are_allowed():
    """D4 forward-compat: keys absent from the schema pass unchanged."""
    assert validate_event(
        "narrative",
        {"prose": "p", "future_field": {"nested": [1, 2, 3]}, "unknown": None},
    ) == []
    assert validate_event("user", {"text": "hi", "brand_new": "anything"}) == []


def test_gate_huge_payload_is_not_size_limited():
    """The gate type-checks keys; it never enforces a size ceiling."""
    huge = {f"k{i:05d}": "v" * 100 for i in range(1_000)}
    assert validate_event("state-delta", huge) == []
    assert validate_event("narrative", {"prose": "P" * 1_000_000}) == []


def test_append_event_gate_raises_logschemaerror_and_writes_nothing(tmp_path):
    """``repo.append_event`` calls the gate and raises ``LogSchemaError``
    *before* opening its transaction — it does not catch or wrap the error, and
    a rejected payload leaves no row behind (``log/repo.py``)."""
    log = EventLog(tmp_path / "events.db")
    try:
        with pytest.raises(LogSchemaError):
            log.append_event("s1", "roll", "user", {"dice": "1d20", "result": True})
        assert log.get_events("s1") == []
    finally:
        log.close()


def test_append_event_unknown_type_raises_before_gate(tmp_path):
    """An out-of-set event type is a ``ValueError`` at the door, also no row."""
    log = EventLog(tmp_path / "events.db")
    try:
        with pytest.raises(ValueError):
            log.append_event("s1", "bogus", "user", {})
        assert log.get_events("s1") == []
    finally:
        log.close()
