"""Recap read router (Wave 4b H1 backend half; W9.26 §2.3 step 7 chain-aware).

Compaction folds old turns into ``summary`` events (``graph/compaction.py``);
the prompt side already consumes them (``summaries_for_prompt``) but the player
had no reader. This router exposes them, in session ``seq`` order, with the
inclusive ``[first_seq, last_seq]`` narrative span they cover so the Recap pane
can render jump anchors into the transcript.

  GET /api/sessions/{id}/recap
      -> ``{"session_id", "recap": [{"seq", "text", "covers_seqs",
                                     "reason"?, "trigger"?, "stats"?,
                                     "absorbs"?}]}``

W9.26 §5 ("RecapPane lists all summaries across eras"): the read walks the
CHAIN (``get_chain_events``), so an era child lists the summaries it inherited
instead of appearing to have lost the story. The row keeps its original
``{seq, text, covers_seqs}`` fields (the pane and the schema stay compatible)
and gains the summary-v2 facts the compaction notice needs on expand:

* ``trigger`` — ``"manual"`` / ``"forced_90"`` / a legacy reason;
* ``stats`` — ``{calls, prompt_tokens, model}`` from the summarizer's own run;
* ``absorbs`` — ``[session_id, seq]`` of the prior summary this one folded in
  (the ``absorbs`` key is OMITTED when there is none: the typed gate rejects a
  present ``null``, and a legacy summary has no such fact).

Absent optional facts are omitted from the row for the same reason (§2.9's
omit-when-absent discipline): a legacy summary's row is byte-identical to what
the pane served before this wave (``{seq, text, covers_seqs}``), which is what
"keep the existing response fields compatible" means.

Read-only and additive: raw ``narrative`` events stay in the log (D9/D10); the
summary is just another event.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request

from fabulist.api.attachments import _session_exists

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log.events import Event

__all__ = ["build_recap", "router"]

router = APIRouter(tags=["recap"])


def _opt(payload: Mapping[str, Any], key: str) -> Any:
    """One optional summary field, or ``None`` when absent/unusable."""
    value = payload.get(key)
    if value is None:
        return None
    if key == "absorbs":
        # A pair by contract ([session_id, seq]); anything else is not a fact.
        if isinstance(value, (list, tuple)) and len(value) >= 2:
            return [value[0], value[1]]
        return None
    if key == "stats":
        return dict(value) if isinstance(value, Mapping) and value else None
    if isinstance(value, str):
        return value if value.strip() else None
    return value


def build_recap(events: list["Event"]) -> list[dict]:
    """Project ``summary`` events to recap rows, seq-ordered.

    ``covers_seqs`` is the inclusive ``[low, high]`` narrative span recorded by
    the compactor. The ``content`` spelling is accepted as a fallback for
    ``text`` (the schema gate allows both); ephemeral summaries are skipped
    (abandoned swipe branches, PLAN W2.2).

    Summary-v2 facts (``trigger``/``stats``/``absorbs``/``reason``) ride along
    when the row carries them and are OMITTED otherwise — a legacy summary keeps
    exactly its old shape.
    """
    recap: list[dict] = []
    for event in events:
        if event.type != "summary" or event.ephemeral:
            continue
        payload = event.payload if isinstance(event.payload, dict) else {}
        text = payload.get("text")
        if not isinstance(text, str):
            content = payload.get("content")
            text = content if isinstance(content, str) else ""
        span = payload.get("covers_seqs")
        if isinstance(span, list) and len(span) >= 2:
            low, high = span[0], span[1]
        else:
            low = high = None
        row: dict = {
            "seq": event.seq,
            "text": text,
            "covers_seqs": [low, high],
        }
        for key in ("reason", "trigger", "stats", "absorbs"):
            value = _opt(payload, key)
            if value is not None:
                row[key] = value
        recap.append(row)
    recap.sort(key=lambda item: item["seq"])
    return recap


@router.get("/api/sessions/{session_id}/recap")
async def get_recap(session_id: str, request: Request) -> dict:
    """Return the session's summaries (whole CHAIN) in seq order (404 when unknown).

    Chain-aware by ruling: an era child inherits its ancestors' summaries, so
    ``get_chain_events`` — not ``get_events`` — is the read. A single-seq legacy
    summary (``[N,N]``) renders as-is; the turn-pair span is the writer's fix
    (§2.3 step 3), not the reader's.
    """
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    return {"session_id": session_id, "recap": build_recap(log.get_chain_events(session_id))}
