"""Issue #79: the per-connection VISION capability gate.

The owner ruling: a per-connection VISION toggle decides, at message-build
time, whether image-bearing content reaches a model as native content parts or
as text. The gate is per connection and fully dynamic — the SAME log rows render
inline on one binding and captioned on another, with no migration and nothing
baked into a row. The text fallback is ONE uniform shape for every image kind:
``[inline image caption: <text>]``.

What these tests pin, in order: the capability matrix from one row set, replay
across a rebind, arrival captioning, lane divergence (the lane's own binding, not
the narrator's), OOC + preview parity, the exact fallback shape, and the no
in-turn captioner rule.
"""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from fabulist.api import create_app
from fabulist.api.fake import FakeTurnEngine
from fabulist.api.prompts_api import router as prompts_router
from fabulist.connections.client import build_request
from fabulist.connections.registry import (
    Connection,
    ConnectionConfigError,
    Registry,
    validate_connection_patch,
)
from fabulist.graph.nodes import make_npc_lanes_node
from fabulist.graph.types import initial_turn_state
from fabulist.log import EventLog
from fabulist.media.images import (
    IMAGE_CAPTION_PLACEHOLDER,
    MEDIA_META_KEY,
    build_image_turn_prefix,
    store_image,
)
from fabulist.wiring import (
    history_from_log,
    make_narrator_message_builder,
    make_ooc_message_builder,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\ncapability-image"
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")
PNG_URL = f"data:image/png;base64,{PNG_B64}"


def run(coro):
    return asyncio.run(coro)


def _seed_image_turn(log: EventLog, session_id: str = "s1") -> tuple[str, str]:
    """One session: a user line, an imagegen row (prompt), a caption event."""
    log.append_event(session_id, "system", "system", {"event": "session_created"})
    log.append_event(session_id, "user", "user", {"text": "I look at the painting."})
    asset = store_image(log, session_id, PNG_BYTES, "image/png", note="a crow on a wire")
    log.append_event(
        session_id, "system", "media",
        {"event": "image_caption", "asset": asset.payload["asset_id"], "text": "a crow on a wire"},
    )
    log.append_event(
        session_id,
        "narrative",
        "imagegen",
        {
            "prose": "",
            "dsl": "",
            "kind": "image",
            "image_asset_id": asset.payload["asset_id"],
            "prompt": "a crow on a wire",
        },
    )
    return session_id, asset.payload["asset_id"]


# ---------------------------------------------------------------------------
# (0) the signal: a first-class per-connection field, off by default
# ---------------------------------------------------------------------------


def test_vision_is_a_first_class_connection_field(tmp_path):
    config = {
        "connections": {
            "plain": {"base_url": "http://host/v1", "model": "m"},
            "sees": {"base_url": "http://host/v1", "model": "m", "vision": True},
        },
        "agents": {"narrator": "plain"},
    }
    registry = Registry.from_dict(config)
    # Unset means TEXT-ONLY (off), never an assumed capability.
    assert registry.connections["plain"].vision is None
    assert registry.connections["sees"].vision is True

    assert validate_connection_patch("c", {"vision": True}) == {"vision": True}
    assert validate_connection_patch("c", {"vision": None}) == {"vision": None}
    with pytest.raises(ConnectionConfigError):
        validate_connection_patch("c", {"vision": "yes"})


# ---------------------------------------------------------------------------
# (1) capability matrix: SAME rows, two connections, two shapes
# ---------------------------------------------------------------------------


def test_vision_and_text_only_render_same_rows_differently(tmp_path):
    log = EventLog(tmp_path / "matrix.db")
    sid, _ = _seed_image_turn(log)

    vision = history_from_log(log, sid, vision=True)
    text_only = history_from_log(log, sid, vision=False)

    # The narration copy is identical for both; only the image row differs.
    assert vision[0] == text_only[0]
    assert vision[1]["role"] == text_only[1]["role"] == "assistant"

    assert isinstance(vision[1]["content"], list)
    assert vision[1]["content"][0] == {"type": "text", "text": "Now visible: a crow on a wire"}
    assert vision[1]["content"][1]["type"] == "image_url"
    assert vision[1]["content"][1]["image_url"]["url"] == PNG_URL

    assert text_only[1]["content"] == "[inline image caption: a crow on a wire]"


# ---------------------------------------------------------------------------
# (2) history replay across a rebind: no migration, no new rows
# ---------------------------------------------------------------------------


def test_rebinding_changes_rendering_on_the_next_build_without_migration(tmp_path):
    log = EventLog(tmp_path / "replay.db")
    sid, _ = _seed_image_turn(log)
    rows_before = len(log.get_events(sid))

    first = history_from_log(log, sid, vision=False)
    second = history_from_log(log, sid, vision=True)  # the "rebind"

    assert first[1]["content"] == "[inline image caption: a crow on a wire]"
    assert isinstance(second[1]["content"], list)
    # Nothing was appended: the rendering is derived, never baked into the log.
    assert len(log.get_events(sid)) == rows_before
    assert log.get_events(sid)[-1].payload.get("image_asset_id")


# ---------------------------------------------------------------------------
# (3) arrival captioning: the upload stores a caption event (arrival, not turn)
# ---------------------------------------------------------------------------


def test_upload_path_stores_the_arrival_caption(tmp_path):
    from fabulist.api.attachments import router as attachments_router

    log_path = tmp_path / "upload.db"
    app = create_app(FakeTurnEngine(), log_path)
    app.include_router(attachments_router)

    class FakeCaptioner:
        async def __call__(self, messages, stream=True):
            yield {"type": "content", "delta": "A lantern on a wet sill."}

    app.state.captioner = FakeCaptioner()

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            created = await client.post("/api/sessions", json={"title": "Scene"})
            sid = created.json()["session_id"]
            uploaded = await client.post(
                f"/api/sessions/{sid}/attachments",
                files={"file": ("a.png", PNG_BYTES, "image/png")},
            )
            assert uploaded.status_code == 200
            return app.state.log.get_events(sid)

    try:
        events = run(scenario())
    finally:
        app.state.log.close()

    captions = [
        e for e in events
        if e.type == "system" and e.payload.get("event") == "image_caption"
    ]
    assert len(captions) == 1
    assert captions[0].payload["text"] == "A lantern on a wet sill."


# ---------------------------------------------------------------------------
# (4) lane divergence: a lane bound differently receives ITS rendering
# ---------------------------------------------------------------------------


class RecordingLaneCaller:
    def __init__(self):
        self.calls: list[list[dict]] = []

    async def __call__(self, messages, stream=True, **kwargs):
        self.calls.append([dict(m) for m in messages])
        reply = {
            "intent": "The crow watches.",
            "narrative": "It tilts its head.",
            "lines": [{"text": "Caw."}],
        }
        yield {"type": "content", "delta": json.dumps(reply)}


def _lane_run(log, shared, *, lane_vision):
    fanout = make_npc_lanes_node(
        log=log,
        build_messages=lambda state: [],
        caller=RecordingLaneCaller(),
        lane_vision_fn=lambda: lane_vision,
    )
    payload = {
        "session_id": "s1",
        "member": {
            "member_id": "m1",
            "name": "Marcus",
            "aliases": [],
            "categories": {},
            "witnessed": [],
            "beat": "the crow lands",
            "last_intent": "",
        },
        "shared": shared,
        "max_lanes": 2,
    }
    update = run(fanout.lane(payload))
    assert update["lane_meta"]["ran"] == 1
    return fanout.caller.calls[0]


def test_a_lane_bound_differently_rerenders_the_shared_history(tmp_path):
    log = EventLog(tmp_path / "lane.db")
    _seed_image_turn(log)
    # The narrator (text-only) built the shared prefix: the image row is a marker.
    shared = history_from_log(log, "s1", vision=False)
    assert shared[1]["content"] == "[inline image caption: a crow on a wire]"

    vision_lane = _lane_run(log, [dict(m) for m in shared], lane_vision=True)
    # The lane's own binding is vision: ITS copy carries the native part.
    assert isinstance(vision_lane[1]["content"], list)
    assert vision_lane[1]["content"][1]["type"] == "image_url"

    text_lane = _lane_run(log, [dict(m) for m in shared], lane_vision=False)
    assert text_lane[1]["content"] == "[inline image caption: a crow on a wire]"


def test_a_vision_narrator_history_is_captioned_for_a_text_only_lane(tmp_path):
    log = EventLog(tmp_path / "lane2.db")
    _seed_image_turn(log)
    shared = history_from_log(log, "s1", vision=True)
    assert isinstance(shared[1]["content"], list)

    text_lane = _lane_run(log, [dict(m) for m in shared], lane_vision=False)
    assert text_lane[1]["content"] == "[inline image caption: a crow on a wire]"


def test_wire_messages_never_carry_the_transient_media_metadata(tmp_path):
    log = EventLog(tmp_path / "wire.db")
    _seed_image_turn(log)
    history = history_from_log(log, "s1", vision=True)
    assert MEDIA_META_KEY in history[1]

    registry = Registry(
        connections={
            "c": Connection(name="c", base_url="http://host/v1", model="m", vision=True)
        },
        agents={"narrator": "c"},
    )
    request = build_request("narrator", history, registry=registry)
    assert all(MEDIA_META_KEY not in message for message in request["messages"])
    assert request["messages"][1]["content"][1]["type"] == "image_url"


# ---------------------------------------------------------------------------
# (5) OOC + preview parity
# ---------------------------------------------------------------------------


def test_ooc_builder_gates_on_its_own_connection(tmp_path):
    log = EventLog(tmp_path / "ooc.db")
    _seed_image_turn(log)

    text_only = make_ooc_message_builder(log, vision_fn=lambda: False)("s1", "what is it?")
    assert any(
        m["content"] == "[inline image caption: a crow on a wire]" for m in text_only
    )

    vision = make_ooc_message_builder(log, vision_fn=lambda: True)("s1", "what is it?")
    assert any(
        isinstance(m["content"], list) and m["content"][1]["type"] == "image_url"
        for m in vision
    )


def test_preview_parity_for_the_narrator_builder(tmp_path):
    """The preview rides the SAME builder, so the vision gate applies there too."""
    log_path = tmp_path / "preview.db"
    app = create_app(FakeTurnEngine(), log_path)
    app.include_router(prompts_router)

    log = app.state.log
    _seed_image_turn(log)

    async def preview(vision: bool):
        app.state.message_builder = make_narrator_message_builder(
            log, vision_fn=lambda: vision
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/prompts/preview",
                json={"session_id": "s1", "user_text": "what is it?", "interaction": "narrate"},
            )
        assert resp.status_code == 200
        return resp.json()["messages"]

    try:
        text_only = run(preview(False))
        vision = run(preview(True))
    finally:
        app.state.log.close()

    assert any(
        m.get("content") == "[inline image caption: a crow on a wire]" for m in text_only
    )
    assert any(
        isinstance(m.get("content"), list)
        and any(part.get("type") == "image_url" for part in m["content"])
        for m in vision
    )
    # The transient render metadata never leaks into the preview payload.
    assert all(MEDIA_META_KEY not in m for m in text_only + vision)


# ---------------------------------------------------------------------------
# (6) the fallback shape pin — ONE bracketed marker for every image kind
# ---------------------------------------------------------------------------


def test_the_fallback_marker_is_one_uniform_shape():
    assert build_image_turn_prefix("a crow on a wire") == (
        "[inline image caption: a crow on a wire]"
    )
    assert build_image_turn_prefix("  spaced text \n") == (
        "[inline image caption: spaced text]"
    )
    assert build_image_turn_prefix("") == (
        f"[inline image caption: {IMAGE_CAPTION_PLACEHOLDER}]"
    )


# ---------------------------------------------------------------------------
# (7) no in-turn captioner call: legacy uncaptioned asset -> placeholder
# ---------------------------------------------------------------------------


def test_uncaptioned_legacy_asset_uses_the_placeholder_and_never_captions(
    tmp_path, monkeypatch
):
    log = EventLog(tmp_path / "legacy.db")
    sid = "s1"
    log.append_event(sid, "system", "system", {"event": "session_created"})
    # A legacy row: no caption event, no prompt/text, no note.
    asset = store_image(log, sid, PNG_BYTES, "image/png")
    log.append_event(
        sid, "narrative", "imagegen",
        {"prose": "", "dsl": "", "kind": "image",
         "image_asset_id": asset.payload["asset_id"], "prompt": ""},
    )

    # Captioning is ARRIVAL-ONLY: any captioner reachable from a turn build
    # would raise here, so the pin is real rather than structural.
    from fabulist.media import images as media_images

    async def _explode(*args, **kwargs):
        raise AssertionError("a captioner must never run inside a turn build")
        yield  # pragma: no cover

    monkeypatch.setattr(media_images, "caption", _explode)
    rows_before = len(log.get_events(sid))

    hist = history_from_log(log, sid, vision=False)
    assert hist[0]["content"] == f"[inline image caption: {IMAGE_CAPTION_PLACEHOLDER}]"
    assert len(log.get_events(sid)) == rows_before  # no caption event written


# ---------------------------------------------------------------------------
# (A) current-turn attachment (path A) gates on the narrator connection
# ---------------------------------------------------------------------------


def test_current_turn_attachment_gates_on_the_narrator_connection(tmp_path):
    log = EventLog(tmp_path / "turn.db")
    sid = "s1"
    log.append_event(sid, "system", "system", {"event": "session_created"})
    asset = store_image(log, sid, PNG_BYTES, "image/png")

    def build(vision):
        builder = make_narrator_message_builder(log, vision_fn=lambda: vision)
        state = initial_turn_state(sid, "what is this?", "narrate")
        state["image_url"] = PNG_URL
        state["image_kind"] = "image"
        state["image_text"] = "A lantern on a wet sill."
        return builder(state)

    text_only = build(False)
    final_user = next(m for m in reversed(text_only) if m["role"] == "user")
    assert str(final_user["content"]).startswith(
        "[inline image caption: A lantern on a wet sill.]"
    )

    vision = build(True)
    final_user = next(m for m in reversed(vision) if m["role"] == "user")
    assert isinstance(final_user["content"], list)
    assert any(part.get("type") == "image_url" for part in final_user["content"])
