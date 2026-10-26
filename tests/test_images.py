"""Tests for the media/image input pipeline + attachments router (W2.8).

Covers the three owned surfaces against the real ``EventLog`` (tmp SQLite):
``store_image`` round-trips bytes through an ``asset`` event, ``caption`` drains
an injected fake ``ModelCaller`` into an ``image_caption`` system event, the
uniform ``[inline image caption: ...]`` fallback format (issue #79), and the
router endpoints over httpx ASGI transport with a fake captioner (mirroring the
sessions/settings router tests).
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.attachments import router
from fabulist.api.fake import FakeTurnEngine
from fabulist.log import EventLog
from fabulist.media import (
    CAPTION_EVENT,
    CAPTION_MAX_WORDS,
    CAPTION_SYSTEM,
    build_caption_messages,
    build_image_turn_prefix,
    caption,
    find_asset,
    image_content_part,
    sha256_hex,
    store_image,
)


def run(coro):
    return asyncio.run(coro)


PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rimage-payload"


class FakeCaptioner:
    """Scripted ``ModelCaller`` for the captioner agent.

    Records the ``messages`` it was called with so tests can assert the image
    actually travelled as an OpenAI content part.
    """

    def __init__(self, text: str = "A weathered oak door, iron ring handle.") -> None:
        self.text = text
        self.calls: list[list[dict[str, Any]]] = []

    async def __call__(self, messages: list[dict], stream: bool = True):
        self.calls.append(messages)
        yield {"type": "reasoning", "delta": "inspecting pixels "}
        yield {"type": "content", "delta": self.text}


# ---------------------------------------------------------------------------
# images.py: asset round-trip
# ---------------------------------------------------------------------------


def test_store_image_round_trips_an_asset_event(log: EventLog):
    event = store_image(log, "s1", PNG_BYTES, "image/png", note="tavern sign")

    assert event.type == "asset"
    payload = event.payload
    assert payload["kind"] == "image"
    assert payload["mime"] == "image/png"
    assert payload["note"] == "tavern sign"
    assert payload["sha256"] == sha256_hex(PNG_BYTES)
    assert payload["asset_id"] == payload["sha256"]
    assert base64.b64decode(payload["bytes_b64"]) == PNG_BYTES

    # The log is truth: the asset is visible through the projection.
    projection = log.get_projection("s1")
    assert [item["payload"]["asset_id"] for item in projection["assets"]] == [
        payload["asset_id"]
    ]
    assert log.get_events("s1")[0].type == "asset"


def test_find_asset_matches_content_address_or_event_id(log: EventLog):
    event = store_image(log, "s1", PNG_BYTES, "image/png")
    ref = event.payload["asset_id"]

    assert find_asset(log, "s1", ref)["payload"]["sha256"] == sha256_hex(PNG_BYTES)
    assert find_asset(log, "s1", str(event.id))["payload"]["asset_id"] == ref
    assert find_asset(log, "s1", "no-such-asset") is None


def test_same_bytes_share_a_content_address(log: EventLog):
    first = store_image(log, "s1", PNG_BYTES, "image/png")
    second = store_image(log, "s1", PNG_BYTES, "image/png")
    assert first.payload["asset_id"] == second.payload["asset_id"]
    # Append-only: both rows survive.
    assert len(log.get_projection("s1")["assets"]) == 2


# ---------------------------------------------------------------------------
# images.py: caption event + prompt + prefix
# ---------------------------------------------------------------------------


def test_caption_appends_system_event_and_returns_it(log: EventLog):
    event = store_image(log, "s1", PNG_BYTES, "image/png")
    ref = event.payload["asset_id"]
    captioner = FakeCaptioner("A brass lantern, soot-streaked glass.")

    caption_event = run(caption(log, "s1", ref, captioner))

    assert caption_event.type == "system"
    assert caption_event.actor == "media"
    assert caption_event.payload == {
        "event": CAPTION_EVENT,
        "asset": ref,
        "text": "A brass lantern, soot-streaked glass.",
    }
    kinds = [e.payload.get("event") for e in log.get_events("s1") if e.type == "system"]
    assert kinds == [CAPTION_EVENT]


def test_caption_sends_the_image_as_a_content_part(log: EventLog):
    event = store_image(log, "s1", PNG_BYTES, "image/png")
    captioner = FakeCaptioner()
    run(caption(log, "s1", event.payload["asset_id"], captioner))

    assert len(captioner.calls) == 1
    messages = captioner.calls[0]
    assert messages[0]["role"] == "system"
    assert CAPTION_SYSTEM.startswith("You describe images")
    assert str(CAPTION_MAX_WORDS) in CAPTION_SYSTEM

    user = messages[1]
    text_part, image_part = user["content"]
    assert text_part["type"] == "text"
    url = image_part["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    assert base64.b64decode(url.split(",", 1)[1]) == PNG_BYTES


def test_caption_unknown_asset_raises(log: EventLog):
    with pytest.raises(KeyError):
        run(caption(log, "s1", "missing", FakeCaptioner()))


def test_build_caption_messages_uses_payload_dict():
    messages = build_caption_messages(
        {"mime": "image/jpeg", "bytes_b64": "AAAA"}
    )
    assert image_content_part({"mime": "image/jpeg", "bytes_b64": "AAAA"}) == {
        "type": "image_url",
        "image_url": {"url": "data:image/jpeg;base64,AAAA"},
    }
    assert messages[1]["content"][1]["image_url"]["url"].endswith("base64,AAAA")


def test_build_image_turn_prefix_format():
    """Issue #79: ONE uniform bracketed marker for every image kind."""
    assert build_image_turn_prefix("a red door") == "[inline image caption: a red door]"
    assert (
        build_image_turn_prefix("  spaced caption \n")
        == "[inline image caption: spaced caption]"
    )
    # An empty arrival text renders the terse placeholder, never an empty marker.
    assert build_image_turn_prefix("") == "[inline image caption: no caption available]"


# ---------------------------------------------------------------------------
# attachments.py: router endpoints (fake captioner via app.state)
# ---------------------------------------------------------------------------


@pytest.fixture
def build_app(tmp_path: Path):
    """Factory for apps with the attachments router mounted over one log."""
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


async def _create(client: httpx.AsyncClient) -> str:
    resp = await client.post("/api/sessions", json={"title": "Scene"})
    assert resp.status_code == 200
    return resp.json()["session_id"]


def test_upload_stores_asset_and_caption_then_lists(build_app):
    build, log_path = build_app
    app = build()
    app.state.captioner = FakeCaptioner("A crooked signpost.")

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client)
            uploaded = await client.post(
                f"/api/sessions/{sid}/attachments",
                files={"file": ("sign.png", PNG_BYTES, "image/png")},
                data={"note": "tavern"},
            )
            assert uploaded.status_code == 200
            listed = (await client.get(f"/api/sessions/{sid}/attachments")).json()
            return sid, uploaded.json(), listed

    sid, body, listed = run(scenario())
    asset = body["asset"]
    assert body["session_id"] == sid
    assert asset["kind"] == "image"
    assert asset["mime"] == "image/png"
    assert asset["note"] == "tavern"
    assert asset["sha256"] == sha256_hex(PNG_BYTES)
    assert asset["caption"] == "A crooked signpost."

    assert listed["session_id"] == sid
    assert len(listed["attachments"]) == 1
    assert listed["attachments"][0]["caption"] == "A crooked signpost."

    # Log truth: one asset row and one image_caption system row.
    log = EventLog(log_path)
    events = log.get_events(sid)
    log.close()
    assert [e.type for e in events] == ["system", "asset", "system"]
    captions = [
        e for e in events if e.payload.get("event") == CAPTION_EVENT
    ]
    assert len(captions) == 1
    assert captions[0].payload["asset"] == asset["asset_id"]


def test_upload_without_captioner_is_503(build_app):
    build, _ = build_app
    app = build()  # no app.state.captioner

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client)
            return await client.post(
                f"/api/sessions/{sid}/attachments",
                files={"file": ("sign.png", PNG_BYTES, "image/png")},
            )

    assert run(scenario()).status_code == 503


def test_upload_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()
    app.state.captioner = FakeCaptioner()

    async def scenario():
        async with client_for(app) as client:
            return await client.post(
                "/api/sessions/nope/attachments",
                files={"file": ("sign.png", PNG_BYTES, "image/png")},
            )

    assert run(scenario()).status_code == 404


def test_list_without_attachments_is_empty(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client)
            return sid, (await client.get(f"/api/sessions/{sid}/attachments")).json()

    sid, listed = run(scenario())
    assert listed == {"session_id": sid, "attachments": []}


def test_card_art_is_not_a_chat_attachment(build_app):
    """A card import's art belongs to the CHARACTER, not to the transcript.

    The seed event only *references* the asset (the bytes live in the definition
    store), so the listing must not hand it to the first user bubble — while a
    genuine upload on the same session keeps attaching, even when its note
    mentions the card (the filter keys on the ref, never on the note text).
    """
    build, _ = build_app
    app = build()
    app.state.captioner = FakeCaptioner("A painted portrait.")
    card_png = (Path(__file__).resolve().parent / "fixtures" / "cards" / "full_v2.png").read_bytes()

    async def scenario():
        async with client_for(app) as client:
            imported = await client.post(
                "/api/cards/import?seed=1",
                content=card_png,
                headers={"content-type": "image/png"},
            )
            sid = imported.json()["session_id"]
            before = (await client.get(f"/api/sessions/{sid}/attachments")).json()
            uploaded = await client.post(
                f"/api/sessions/{sid}/attachments",
                files={"file": ("moodboard.png", PNG_BYTES, "image/png")},
                data={"note": "card art reference"},
            )
            after = (await client.get(f"/api/sessions/{sid}/attachments")).json()
            return sid, before, uploaded.json(), after

    sid, before, uploaded, after = run(scenario())
    assert before == {"session_id": sid, "attachments": []}
    assert [item["asset_id"] for item in after["attachments"]] == [
        uploaded["asset"]["asset_id"]
    ]
    assert after["attachments"][0]["note"] == "card art reference"


def test_card_art_bytes_still_serve_from_the_session_raw_route(build_app):
    """The card details view reads the art through the session raw route."""
    build, _ = build_app
    app = build()
    card_png = (Path(__file__).resolve().parent / "fixtures" / "cards" / "full_v2.png").read_bytes()

    async def scenario():
        async with client_for(app) as client:
            imported = await client.post(
                "/api/cards/import?seed=1",
                content=card_png,
                headers={"content-type": "image/png"},
            )
            sid = imported.json()["session_id"]
            opening = (await client.get(f"/api/sessions/{sid}/opening")).json()
            raw = await client.get(
                f"/api/sessions/{sid}/attachments/{opening['card_asset']}/raw"
            )
            return opening, raw

    opening, raw = run(scenario())
    assert isinstance(opening["card_asset"], str) and opening["card_asset"]
    assert raw.status_code == 200
    assert raw.content == card_png
    assert raw.headers["content-type"].startswith("image/png")


def test_list_unknown_session_is_404(build_app):
    build, _ = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await client.get("/api/sessions/nope/attachments")

    assert run(scenario()).status_code == 404


def test_forked_session_with_no_own_rows_is_known(build_app):
    """A fork exists in the sessions table before it owns any event rows."""
    build, log_path = build_app
    app = build()

    async def scenario():
        async with client_for(app) as client:
            return await _create(client)

    sid = run(scenario())
    log = EventLog(log_path)
    fork = log.fork_session(sid, 0, "branch-fork")
    log.close()

    async def read():
        async with client_for(app) as client:
            return await client.get(f"/api/sessions/{fork}/attachments")

    resp = run(read())
    assert resp.status_code == 200
    assert resp.json() == {"session_id": fork, "attachments": []}


def test_latest_caption_wins_for_an_asset(build_app):
    build, log_path = build_app
    app = build()
    app.state.captioner = FakeCaptioner("first caption")

    async def scenario():
        async with client_for(app) as client:
            sid = await _create(client)
            first = (
                await client.post(
                    f"/api/sessions/{sid}/attachments",
                    files={"file": ("sign.png", PNG_BYTES, "image/png")},
                )
            ).json()
            return sid, first["asset"]["asset_id"]

    sid, ref = run(scenario())

    # A second caption for the same asset supersedes in the folded view (D10).
    log = EventLog(log_path)
    run(caption(log, sid, ref, FakeCaptioner("second caption")))
    log.close()

    async def read():
        async with client_for(build()) as client:
            return (await client.get(f"/api/sessions/{sid}/attachments")).json()

    listed = run(read())
    assert listed["attachments"][0]["caption"] == "second caption"
