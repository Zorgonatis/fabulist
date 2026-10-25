"""Synchronous STT endpoint (W3.8 voice-input channel).

The mic channel records a short clip in the browser and posts it here for
transcription — no session attachment needed. Attachment audio keeps its
own path (asset + background transcription + 'Heard (audio)' facts block);
this endpoint is the composer-side door.

v1 honesty: faster-whisper constructs the model per call (seconds of setup
on CPU, model cached on disk). Acceptable for short clips; a process-cached
singleton is the documented optimization follow-up.

Optionality (W3b): the transcription stack is an optional install
(``fabulist[stt]``). The route is always mounted; a missing stack answers
``503`` with :data:`~fabulist.media.stt.STT_EXTRA_HINT` (the readable install
fix) instead of a 500 from an import error.
"""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile

from fabulist.media.av import sniff_kind
from fabulist.media.stt import (
    STT_EXTRA_HINT,
    MediaSttError,
    SttUnavailableError,
    transcribe,
)

router = APIRouter(tags=["stt"])

#: Cap for mic clips (short utterances, not media libraries).
STT_MAX_BYTES = 20 * 1024 * 1024

_DEFAULT_MIME = "application/octet-stream"


@router.post("/api/stt")
async def transcribe_upload(file: UploadFile = File(...)) -> dict:
    """Transcribe one uploaded audio clip; returns text + language + duration."""
    data = await file.read()
    if len(data) > STT_MAX_BYTES:
        raise HTTPException(413, detail=f"audio too large (max {STT_MAX_BYTES} bytes)")
    mime = file.content_type or _DEFAULT_MIME
    if sniff_kind(data, mime) != "audio":
        raise HTTPException(415, detail=f"not audio (mime {mime!r})")
    try:
        result = await __import__("asyncio").to_thread(transcribe, data, mime)
    except SttUnavailableError as exc:
        # The optional stack is absent: a deployment fix, not a bad upload.
        raise HTTPException(503, detail=STT_EXTRA_HINT) from exc
    except MediaSttError as exc:
        raise HTTPException(422, detail=f"transcription failed: {exc}") from exc
    return {
        "text": result.text,
        "language": result.language,
        "duration_s": result.duration_s,
    }
