"""Stage-failure events (S4c): failures must reach the log, not just the journal.

Audit 2026-10-10 (silent-failure class): several post-turn paths swallowed a
failure into the server journal so the log (D9: source of truth) missed it.
This file covers the background speech-to-text failure path
(:func:`fabulist.api.attachments.schedule_transcription`) and the
:class:`~fabulist.media.stt.MediaSttError` ``stage`` label it records.

The sibling variants are exercised elsewhere: ``index_failure`` /
``compaction_failed`` / ``stt_failure`` schema shape in
``tests/test_schema_gate.py``, and the ``compaction_failed`` append in
``tests/test_compaction.py``. The ``index_failure`` append from the orchestrator-
owned ``api/app.py`` is a *prepared* patch (``docs/W5B-app-schedule-post-turn.patch``)
and is therefore not exercised at runtime here.
"""

from __future__ import annotations

import asyncio

import pytest

import fabulist.media.stt as stt_module
from fabulist.api import attachments
from fabulist.media.stt import MediaSttError, SttResult


def run(coro):
    return asyncio.run(coro)


def _failures(log, session):
    return [
        e
        for e in log.get_events(session)
        if e.type == "system" and e.payload.get("event") == "stt_failure"
    ]


def test_media_stt_error_carries_stage_label():
    assert MediaSttError("boom").stage == "stt"  # default preserved
    assert MediaSttError("boom", stage="decode").stage == "decode"
    assert MediaSttError("boom", stderr="tail").stderr == "tail"


def test_garbage_audio_decode_failure_is_stage_decode():
    # ffmpeg is available in this environment; garbage cannot be demuxed.
    with pytest.raises(MediaSttError) as excinfo:
        stt_module.decode_audio(b"not audio at all \x00\x01\x02", "audio/wav")
    assert excinfo.value.stage == "decode"


def test_schedule_transcription_failure_appends_stt_failure(log, monkeypatch):
    def boom(*args, **kwargs):
        raise MediaSttError("transcription failed: model exploded", stage="transcribe")

    monkeypatch.setattr(stt_module, "transcribe", boom)

    async def scenario():
        attachments.schedule_transcription(log, "s1", "asset-1", b"bytes", "audio/wav")
        await asyncio.gather(*attachments._STT_TASKS)

    run(scenario())

    failures = _failures(log, "s1")
    assert len(failures) == 1
    payload = failures[0].payload
    assert payload["stage"] == "transcribe"
    assert payload["asset_id"] == "asset-1"
    assert "model exploded" in payload["error"]


def test_schedule_transcription_success_appends_stt_not_failure(log, monkeypatch):
    monkeypatch.setattr(
        stt_module,
        "transcribe",
        lambda *a, **k: SttResult(text="hello there", language="en", duration_s=1.5),
    )

    async def scenario():
        attachments.schedule_transcription(log, "s1", "asset-2", b"bytes", "audio/wav")
        await asyncio.gather(*attachments._STT_TASKS)

    run(scenario())

    assert _failures(log, "s1") == []
    success = [
        e
        for e in log.get_events("s1")
        if e.type == "system" and e.payload.get("event") == "stt"
    ]
    assert len(success) == 1
    assert success[0].payload["text"] == "hello there"
