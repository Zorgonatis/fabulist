"""Canon generated-image narrative: history rendering + scene-skip.

A generated image is a CANON narrative row (``type="narrative"``, actor
``imagegen``) whose payload carries ``image_asset_id`` and the effective
positive ``prompt`` and NO prose. ``history_from_log`` must inline its bytes as
an ASSISTANT message (a text part plus an image part), and the legacy
``user`` + ``kind="image"`` row must render identically. ``_recent_scene`` (the
image generator's advisory context) must skip image rows so they never fabricate
a ``USER:/NARRATIVE:`` turn pair.
"""

from __future__ import annotations

import base64

from fabulist.api.imagegen_api import _recent_scene
from fabulist.log import EventLog
from fabulist.media.images import store_image
from fabulist.wiring import history_from_log


PNG_BYTES = b"\x89PNG\r\n\x1a\nnarrative-image"
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")
PNG_URL = f"data:image/png;base64,{PNG_B64}"


def test_history_renders_image_narrative_as_assistant_message(tmp_path):
    log = EventLog(tmp_path / "img.db")
    log.append_event("s1", "system", "system", {"event": "session_created"})
    asset = store_image(log, "s1", PNG_BYTES, "image/png", note="a red fox")
    log.append_event(
        "s1",
        "narrative",
        "imagegen",
        {
            "prose": "",
            "dsl": "",
            "kind": "image",
            "image_asset_id": asset.payload["asset_id"],
            "prompt": "a red fox",
        },
    )

    hist = history_from_log(log, "s1")
    assert hist == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Now visible: a red fox"},
                {"type": "image_url", "image_url": {"url": PNG_URL}},
            ],
        }
    ]


def test_history_renders_legacy_user_image_row_as_assistant_message(tmp_path):
    """The pre-narrative row shape (a ``user`` event with ``kind="image"``) must
    render as the same assistant image message — and NOT fall through to the
    plain user branch (which would show a bare request as a player turn)."""
    log = EventLog(tmp_path / "img.db")
    log.append_event("s1", "system", "system", {"event": "session_created"})
    asset = store_image(log, "s1", PNG_BYTES, "image/png", note="a blue fox")
    log.append_event(
        "s1",
        "user",
        "user",
        {
            "text": "a blue fox",
            "kind": "image",
            "image_asset_id": asset.payload["asset_id"],
        },
    )

    hist = history_from_log(log, "s1")
    assert hist == [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Now visible: a blue fox"},
                {"type": "image_url", "image_url": {"url": PNG_URL}},
            ],
        }
    ]


def test_history_keeps_normal_narrative_unchanged(tmp_path):
    """A plain prose narrative is untouched by the image special-case."""
    log = EventLog(tmp_path / "img.db")
    log.append_event("s1", "system", "system", {"event": "session_created"})
    log.append_event("s1", "user", "user", {"text": "I enter."})
    log.append_event("s1", "narrative", "narrator", {"prose": "The fire crackles.", "dsl": ""})

    hist = history_from_log(log, "s1")
    assert hist == [
        {"role": "user", "content": "I enter."},
        {"role": "assistant", "content": "The fire crackles."},
    ]


def test_history_image_row_interleaves_with_turns(tmp_path):
    """An image row keeps its seq position among the real turns."""
    log = EventLog(tmp_path / "img.db")
    log.append_event("s1", "system", "system", {"event": "session_created"})
    log.append_event("s1", "user", "user", {"text": "I enter."})
    log.append_event("s1", "narrative", "narrator", {"prose": "The fire crackles.", "dsl": ""})
    asset = store_image(log, "s1", PNG_BYTES, "image/png")
    log.append_event(
        "s1",
        "narrative",
        "imagegen",
        {"prose": "", "dsl": "", "kind": "image", "image_asset_id": asset.payload["asset_id"], "prompt": "the tavern"},
    )

    hist = history_from_log(log, "s1")
    assert [m["role"] for m in hist] == ["user", "assistant", "assistant"]
    assert hist[1]["content"] == "The fire crackles."
    assert hist[2]["content"][0]["text"] == "Now visible: the tavern"
    assert hist[2]["content"][1]["type"] == "image_url"


def test_recent_scene_skips_image_narratives(tmp_path):
    """The advisory scene block pairs a user text with a prose narrative; an
    image narrative carries no prose and must not fabricate a phantom turn."""
    log = EventLog(tmp_path / "img.db")
    log.append_event("s1", "system", "system", {"event": "session_created"})
    log.append_event("s1", "user", "user", {"text": "I enter."})
    log.append_event("s1", "narrative", "narrator", {"prose": "The fire crackles.", "dsl": ""})
    asset = store_image(log, "s1", PNG_BYTES, "image/png")
    log.append_event(
        "s1",
        "narrative",
        "imagegen",
        {"prose": "", "dsl": "", "kind": "image", "image_asset_id": asset.payload["asset_id"], "prompt": "the tavern"},
    )

    scene = _recent_scene(log, "s1")
    assert "USER: I enter." in scene
    assert "NARRATIVE: The fire crackles." in scene
    # The image row contributes nothing (no empty NARRATIVE block).
    assert scene.count("USER:") == 1
    assert scene.count("NARRATIVE:") == 1
