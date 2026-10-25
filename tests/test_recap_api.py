"""W9.26 §2.3 step 7 / §5 — ``/recap`` is chain-aware and carries summary v2.

The router is exercised through a REAL FastAPI app over a REAL
:class:`~fabulist.log.EventLog`: a fork child inherits its parent's summaries
(``get_chain_events``), which is exactly the read the RecapPane needs when a
compaction era child takes over the conversation.

Backward compatibility is pinned too: every row keeps ``{seq, text,
covers_seqs}``, legacy single-seq ``[N, N]`` spans survive unchanged, and the
v2 facts (``trigger``/``stats``/``absorbs``/``reason``) appear only where the
summary actually carries them (§2.9's omit-when-absent discipline).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from fabulist.api.recap_api import build_recap, router as recap_router
from fabulist.log import EventLog

SID = "s-root"
CHILD = "s-root-era2"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def log(tmp_path: Path):
    repo = EventLog(tmp_path / "events.db")
    yield repo
    repo.close()


def add_turn(log, session, prose="Rain."):
    log.append_event(session, "user", "user", {"text": "I wait."})
    return log.append_event(
        session, "narrative", "narrator", {"prose": prose, "dsl": "", "segments": []}
    )


def add_summary(
    log,
    session,
    covers,
    *,
    text="SUMMARY",
    trigger=None,
    stats=None,
    absorbs=None,
    reason=None,
    ephemeral=False,
):
    payload = {"text": text, "covers_seqs": list(covers)}
    if trigger:
        payload["trigger"] = trigger
    if stats:
        payload["stats"] = dict(stats)
    if absorbs:
        payload["absorbs"] = list(absorbs)
    if reason:
        payload["reason"] = reason
    return log.append_event(session, "summary", "summarizer", payload, ephemeral=ephemeral)


def client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def build_app(log) -> FastAPI:
    app = FastAPI()
    app.include_router(recap_router)
    app.state.log = log
    return app


def get_recap(log, session):
    async def scenario():
        async with client_for(build_app(log)) as client:
            return await client.get(f"/api/sessions/{session}/recap")

    return run(scenario())


# --- chain awareness -------------------------------------------------------


def test_era_child_lists_inherited_summaries(log):
    """An era child reads the WHOLE chain, not just its own rows (§5)."""
    add_turn(log, SID)
    first = add_summary(log, SID, [1, 2], text="ERA ONE", trigger="manual")
    log.fork_session(SID, first.seq, CHILD)
    add_turn(log, CHILD, "The morning after.")
    second = add_summary(
        log,
        CHILD,
        [first.seq, first.seq + 1],
        text="ERA TWO",
        trigger="forced_90",
        stats={"calls": 2, "prompt_tokens": 4000, "model": "your-model-id"},
        absorbs=[SID, first.seq],
    )

    response = get_recap(log, CHILD)
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == CHILD
    rows = body["recap"]
    assert [row["seq"] for row in rows] == [first.seq, second.seq]
    assert [row["text"] for row in rows] == ["ERA ONE", "ERA TWO"]


def test_root_session_is_unchanged(log):
    add_turn(log, SID)
    summary = add_summary(log, SID, [1, 2], text="ONLY")
    rows = get_recap(log, SID).json()["recap"]
    assert len(rows) == 1
    assert rows[0]["seq"] == summary.seq
    assert rows[0]["text"] == "ONLY"
    assert rows[0]["covers_seqs"] == [1, 2]
    # Byte-compatible with the pre-W9.26 row (no extra keys on a legacy row).
    assert set(rows[0]) == {"seq", "text", "covers_seqs"}


# --- summary v2 fields -----------------------------------------------------


def test_v2_fields_ride_the_row(log):
    add_turn(log, SID)
    add_summary(
        log,
        SID,
        [1, 2],
        trigger="forced_90",
        stats={"calls": 3, "prompt_tokens": 12345, "model": "your-model-id"},
        absorbs=["s-prior", 7],
        reason="threshold exceeded",
    )
    row = get_recap(log, SID).json()["recap"][0]
    assert row["trigger"] == "forced_90"
    assert row["stats"] == {"calls": 3, "prompt_tokens": 12345, "model": "your-model-id"}
    assert row["absorbs"] == ["s-prior", 7]
    assert row["reason"] == "threshold exceeded"


def test_legacy_summary_omits_the_v2_keys(log):
    """A legacy row keeps exactly its old shape — omit, never null (§2.9)."""
    add_turn(log, SID)
    add_summary(log, SID, [5, 5], text="LEGACY", reason="threshold exceeded")
    row = get_recap(log, SID).json()["recap"][0]
    assert set(row) == {"seq", "text", "covers_seqs", "reason"}
    assert row["covers_seqs"] == [5, 5]  # the legacy single-seq span survives


def test_content_spelling_still_reads_as_text(log):
    """The schema gate allows ``content`` instead of ``text`` (legacy rows)."""
    add_turn(log, SID)
    log.append_event(SID, "summary", "summarizer", {"content": "FROM CONTENT", "covers_seqs": [1, 2]})
    row = get_recap(log, SID).json()["recap"][0]
    assert row["text"] == "FROM CONTENT"


def _stub_summary(payload, *, seq=7, session=SID):
    """A row-shaped stub: the projection must survive a hand-edited log (D4).

    The append gate is typed (Lane A), so a malformed payload can only reach the
    reader through a log written by an older build or by hand — exactly the case
    ``build_recap`` is required not to crash on.
    """
    return SimpleNamespace(
        type="summary", seq=seq, payload=payload, ephemeral=False, session_id=session
    )


def test_malformed_span_and_absent_text_never_crash():
    rows = build_recap([_stub_summary({"covers_seqs": "nope"})])
    assert rows[0]["covers_seqs"] == [None, None]
    assert rows[0]["text"] == ""


def test_absorbs_that_is_not_a_pair_is_dropped():
    rows = build_recap(
        [_stub_summary({"text": "T", "covers_seqs": [1, 2], "absorbs": "s-prior"})]
    )
    assert "absorbs" not in rows[0]


# --- read discipline -------------------------------------------------------


def test_ephemeral_summaries_are_skipped(log):
    add_turn(log, SID)
    add_summary(log, SID, [1, 2], text="KEPT")
    add_summary(log, SID, [1, 2], text="PRUNED", ephemeral=True)
    texts = [row["text"] for row in get_recap(log, SID).json()["recap"]]
    assert texts == ["KEPT"]


def test_unknown_session_404s(log):
    assert get_recap(log, "nope").status_code == 404


def test_build_recap_is_pure_over_an_event_list(log):
    """The projection helper stays a pure function of its event list."""
    add_turn(log, SID)
    add_summary(log, SID, [1, 2], text="A")
    add_summary(log, SID, [3, 4], text="B")
    rows = build_recap(log.get_chain_events(SID))
    assert [row["text"] for row in rows] == ["A", "B"]
