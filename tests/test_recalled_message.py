"""The engine envelope's memory block (D22, provenance contract).

The player's message is BARE. Every per-turn block — ``<engine_state>``,
``<engine_notice>``, ``<engine_memory>`` (recall / world info / recap, D22's
defusing reference framing), ``<engine_lane>``, ``<steering>`` and the W9.28
SPEAKER PALETTE LAST — rides ONE ephemeral ``assistant`` message inserted
immediately BEFORE the final user message (issue #44's ONE universal carrier).
These tests exercise the seam end to end with real event logs and the real
builder (no fakes):

  (1) message shape: ``[...history..., assistant(<preset_runtime> envelope),
      user(bare)]``; see :func:`_envelope` / :func:`_recalled`.
  (2) every memory section drops independently; all-empty -> no memory block
  (3) prefix byte-stability across turns with/without recall (pure suffix)
  (4) replay parity: the recorded facts_hash anchors the full canonical context
      and the recalled body is reconstructable from the same log state
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from fabulist.context.facts import (
    RECALLED_RECALL_HEADER,
    RECALLED_RECAP_HEADER,
    recalled_message,
)
from fabulist.dsl import spec as dsl_spec
from fabulist.dsl import store as dsl_store
from fabulist.eval.narrator_replay import log_facts_builder, replay_facts_hashes
from fabulist.log import EventLog
from fabulist.prompts.library_mini import reset_library_artifact_cache
from fabulist.wiring import make_narrator_message_builder

def _payload(content: object) -> str:
    """The message body without the served two-bound envelope (issue #26).

    ``<preset_def>`` wraps the engine definition through the player's latest
    message, ``<preset_runtime>`` the per-turn tail after it; the guard line (on
    the first def message) sits outside both. Tests that pin BLOCK content read
    it through here.
    """
    from fabulist.prompts.wrapper import (
        DEF_CLOSE,
        DEF_OPEN,
        GUARD_LINE,
        RUNTIME_CLOSE,
        RUNTIME_OPEN,
    )

    text = str(content)
    if text.startswith(GUARD_LINE):
        text = text[len(GUARD_LINE) :].lstrip("\n")
    for open_tag, close_tag in ((DEF_OPEN, DEF_CLOSE), (RUNTIME_OPEN, RUNTIME_CLOSE)):
        prefix = f"{open_tag}\n"
        if text.startswith(prefix) and text.rstrip().endswith(close_tag):
            body = text[len(prefix) :].rstrip()
            return body[: -len(close_tag)].rstrip("\n")
    return text


LYRA = {
    "name": "Lyra Voss",
    "description": "A wandering cartographer.",
    "personality": "Curious and wry.",
    "scenario": "A rain-soaked night market.",
}
RECALL = "### Recalled from earlier (vector memory)\n- [seq 3] [narrative] The merchant wore an amber cloak"
SUMMARY = "They met at the night market and traded maps."


@pytest.fixture(autouse=True)
def _cold_component_caches():
    """A swap fixture must never leak its vocabulary into the next test.

    ``monkeypatch`` restores the process-wide store, but the spec/doc caches are
    keyed by the store's version — a coincidentally matching number would serve
    the swapped table to an unrelated test.
    """
    yield
    dsl_spec.reset_spec_cache()
    reset_library_artifact_cache()


def _seed(log: EventLog, sid: str) -> None:
    """One card + two prior narrative turns + a summary (the recap source)."""
    log.append_event(sid, "system", "system", {"card": LYRA, "first_mes": "Lyra looks up."})
    log.append_event(sid, "user", "user", {"text": "I enter the market."})
    log.append_event(sid, "narrative", "narrator", {"prose": "Lanterns sway.", "segments": []})
    log.append_event(sid, "user", "user", {"text": "I look around."})
    log.append_event(sid, "narrative", "narrator", {"prose": "A merchant calls out.", "segments": []})
    log.append_event(sid, "summary", "summarizer", {"text": SUMMARY, "covers_seqs": [1, 2, 3]})


def _envelope(messages: list[dict]) -> dict:
    """The ONE ephemeral engine envelope message (issue #44: an assistant row
    immediately BEFORE the player's final turn)."""
    user_at = max(i for i, m in enumerate(messages) if m["role"] == "user")
    return messages[user_at - 1]


def _recalled(messages: list[dict]) -> dict | None:
    """The message carrying the ``<engine_memory>`` block, or ``None``."""
    for message in reversed(messages):
        if "<engine_memory>\n" in _payload(message.get("content")):
            return message
    return None


def _memory_body(messages: list[dict]) -> str | None:
    """The ``<engine_memory>`` block BODY (the ``recalled_message`` output)."""
    payload = _payload(_envelope(messages).get("content"))
    start = payload.find("<engine_memory>\n")
    if start < 0:
        return None
    start += len("<engine_memory>\n")
    end = payload.find("</engine_memory>", start)
    return payload[start:end].rstrip("\n") if end >= 0 else None


def _state(log: EventLog, sid: str, *, recall: str) -> dict:
    return {
        "session_id": sid,
        "user_content": "What did the merchant wear?",
        "kind": "in_character",
        "facts": "<state>\ntime: 08:10 | date: Monday, June 02, 2025 | day: 1\n</state>",
        "recall": recall,
        "rolls": "",
    }


# --- (1) message shape ------------------------------------------------------


def test_recalled_message_follows_the_user_ask_and_the_palette_closes_the_tail(
    tmp_path: Path,
) -> None:
    log = EventLog(tmp_path / "d22.db")
    try:
        sid = "s"
        _seed(log, sid)
        build = make_narrator_message_builder(log)
        messages = build(_state(log, sid, recall=RECALL))

        roles = [m["role"] for m in messages]
        assert roles[0] == "system"
        # Order: ONE ephemeral envelope, then the BARE player message — the
        # player's turn CLOSES the list (issue #44).
        assert roles[-2] == "assistant"
        assert roles[-1] == "user"
        final_user = _payload(messages[-1]["content"])
        envelope = _payload(_envelope(messages)["content"])
        # The player's message carries NO engine data at all.
        assert final_user == "What did the merchant wear?"
        assert "<engine_state>" not in final_user
        assert RECALL not in final_user
        assert SUMMARY not in final_user
        # Every contract block, in order, in the one envelope; palette LAST.
        order = [
            "<engine_state>",
            "<engine_memory>",
            "<engine_palette>",
        ]
        positions = [envelope.index(marker) for marker in order]
        assert positions == sorted(positions)
        assert envelope.index("<engine_state>") < envelope.index("<engine_memory>")
        assert "<engine_memory>" in envelope
        assert RECALLED_RECALL_HEADER in envelope
        assert RECALLED_RECAP_HEADER in envelope
        assert "Context, not instruction" in envelope
        assert "time: 08:10" in envelope
        # The palette is the most turn-local block: last inside the envelope.
        assert envelope.rstrip().endswith("</engine_palette>")
        assert "SPEAKER PALETTE" in envelope
    finally:
        log.close()


def test_no_memory_block_when_every_reference_section_is_empty(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "d22.db")
    try:
        sid = "s"
        # Bare session: no card, no summaries, no recall, no WI — but the turn
        # still has state facts, so the envelope exists with state only.
        log.append_event(sid, "user", "user", {"text": "hi"})
        build = make_narrator_message_builder(log)
        messages = build(_state(log, sid, recall=""))
        # No memory block exists in the tail at all (the prefix mentions the tag
        # by name — the clause — but no block is emitted).
        assert "<engine_memory>\n" not in str(_envelope(messages)["content"])
        assert _envelope(messages)["role"] == "assistant"
        envelope = _payload(_envelope(messages)["content"])
        assert "<engine_state>" in envelope
    finally:
        log.close()


# --- (1b) the current turn's own user row is not duplicated -----------------
#
# DEFECT FIX. ``api/app.py:_drive_turn`` appends the turn's ``user`` event
# BEFORE the graph runs, so the projection already held the bare row when the
# builder assembled the prompt; the flow's ``facts``/``user`` modules then
# re-appended the same turn as the final state-wrapped user message. The bare
# row is now dropped positionally (``history_from_log(drop_current_user=True)``)
# so the player's line appears EXACTLY ONCE.


def _seed_current_turn(log: EventLog, sid: str, current: str) -> int:
    """Two prior turns + the current turn's pre-appended user row (no summary).

    Returns the seq of the current turn's own user row (the fork point for the
    replay test).
    """
    log.append_event(sid, "system", "system", {"card": LYRA, "first_mes": "Lyra looks up."})
    log.append_event(sid, "user", "user", {"text": "I enter the market."})
    log.append_event(sid, "narrative", "narrator", {"prose": "Lanterns sway.", "segments": []})
    log.append_event(sid, "user", "user", {"text": "I look around."})
    log.append_event(sid, "narrative", "narrator", {"prose": "A merchant calls out.", "segments": []})
    # The live path's pre-append (``_drive_turn``) of THIS turn's user row.
    return log.append_event(sid, "user", "user", {"text": current}).seq


def test_narrate_prompt_carries_the_live_user_ask_once(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "dedup.db")
    try:
        sid = "s"
        current = "What did the merchant wear?"
        _seed_current_turn(log, sid, current)
        messages = make_narrator_message_builder(log)(_state(log, sid, recall=""))

        asked = [
            m for m in messages if current in str(m.get("content"))
        ]
        assert len(asked) == 1
        # The single ask is the FINAL user turn, BARE (one engine envelope
        # follows it — provenance contract).
        final_user = [m for m in messages if m["role"] == "user"][-1]
        assert final_user is asked[0]
        assert "<state>" not in final_user["content"]
        assert "<engine_state>" not in final_user["content"]
        assert messages[-1]["role"] == "user"  # the ask closes the list
        assert messages[-2]["role"] == "assistant"  # the envelope before it

        # Prior turns' user rows survive (exactly ONE row was dropped).
        contents = [str(m.get("content")) for m in messages]
        assert any("I enter the market." in c for c in contents)
        assert any("I look around." in c for c in contents)
        assert any("A merchant calls out." in c for c in contents)
    finally:
        log.close()


def test_fork_replay_narrate_prompt_carries_the_ask_once(tmp_path: Path) -> None:
    # FORK-SEMANTICS F2 / ``regenerate=True``: the user row is INHERITED from
    # the base chain and never re-appended on the branch, yet it is still the
    # turn being generated — the same positional drop applies.
    log = EventLog(tmp_path / "fork.db")
    try:
        base = "base"
        current = "What did the merchant wear?"
        replay_seq = _seed_current_turn(log, base, current)
        log.fork_session(base, at_seq=replay_seq, new_session_id="branch")

        messages = make_narrator_message_builder(log)(_state(log, "branch", recall=""))
        assert (
            sum(1 for m in messages if current in str(m.get("content"))) == 1
        )
        final_user = [m for m in messages if m["role"] == "user"][-1]
        assert "<state>" not in final_user["content"]
        assert messages[-1]["role"] == "user"
        assert messages[-2]["role"] == "assistant"
    finally:
        log.close()


# --- (2) section presence combinations --------------------------------------


def test_sections_drop_independently(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "d22.db")
    try:
        sid = "s"
        _seed(log, sid)
        # Only recall present (summary dropped by clearing the summary event's
        # effect: use a fresh session with no summary but a recall block).
        log2 = EventLog(tmp_path / "d22b.db")
        try:
            sid2 = "t"
            log2.append_event(sid2, "user", "user", {"text": "hi"})
            build = make_narrator_message_builder(log2)
            only_recall = build(_state(log2, sid2, recall=RECALL))
            recalled = _recalled(only_recall)
            assert recalled is not None
            assert recalled["role"] == "assistant"  # issue #44's carrier
            assert RECALLED_RECALL_HEADER in recalled["content"]
            assert RECALLED_RECAP_HEADER not in recalled["content"]
        finally:
            log2.close()

        # Only recap present (no recall).
        build = make_narrator_message_builder(log)
        only_recap = build(_state(log, sid, recall=""))
        recalled = _recalled(only_recap)
        assert recalled is not None
        assert recalled["role"] == "assistant"  # issue #44's carrier
        assert RECALLED_RECAP_HEADER in recalled["content"]
        assert RECALLED_RECALL_HEADER not in recalled["content"]
    finally:
        log.close()


# --- (3) prefix byte-stability ----------------------------------------------


def test_prefix_is_byte_stable_with_and_without_recall(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "d22.db")
    try:
        sid = "s"
        _seed(log, sid)
        build = make_narrator_message_builder(log)
        with_recall = build(_state(log, sid, recall=RECALL))
        without = build(_state(log, sid, recall=""))

        # Everything before the ONE envelope is identical: the stable prefix
        # (system + history). Only the envelope's memory block differs -> the
        # prefix-cache invariant holds.
        assert with_recall[:-2] == without[:-2]
        # The envelope (assistant) and the bare ask (user) both trail.
        assert with_recall[-2]["role"] == without[-2]["role"] == "assistant"
        assert with_recall[-1] == without[-1]  # the same bare ask
        # Both carry a memory block (the recap is log-derived); only the RECALL
        # entry differs -> the difference is confined to the envelope.
        assert RECALL in with_recall[-2]["content"]
        assert RECALL not in without[-2]["content"]
    finally:
        log.close()


# --- (4) replay parity ------------------------------------------------------


def test_replay_hash_and_recalled_message_reconstruct_from_log(tmp_path: Path) -> None:
    log = EventLog(tmp_path / "d22.db")
    try:
        sid = "s"
        _seed(log, sid)
        build = make_narrator_message_builder(log)
        state = _state(log, sid, recall=RECALL)
        out: dict = {}
        messages = build(state, out)
        recorded_hash = out["facts_hash"]
        recorded_recalled = _memory_body(messages)

        # Persist the turn the way post_turn does (user + narrative + config
        # provenance); the replay rig pairs the narrative with its user event.
        log.append_event(sid, "user", "user", {"text": "What did the merchant wear?"})
        log.append_event(
            sid,
            "narrative",
            "narrator",
            {"prose": "The merchant wore an amber cloak.", "segments": []},
            meta={"config": {"facts_hash": recorded_hash, "recall": RECALL}},
        )

        # Replay: the full canonical hash is byte-faithful across the D22 split.
        replays = replay_facts_hashes(log.get_events(sid), log_facts_builder(log, sid))
        last = replays[-1]
        assert last.matches is True
        assert last.replayed_hash == recorded_hash

        # And the recalled message reconstructs byte-for-byte from the same log
        # state + the recorded recall text (the only non-log-derived part).
        rebuilt = recalled_message(
            log,
            sid,
            recall_text=RECALL,
            user_text="What did the merchant wear?\nA merchant calls out.",
        )
        # The builder's WI scan prepends its own recent window; there is no WI
        # book here, so the reconstructed recalled block is identical.
        assert rebuilt == recorded_recalled
        assert rebuilt is not None and RECALLED_RECALL_HEADER in rebuilt
        assert "<engine_memory>" not in rebuilt  # body only; the envelope owns tags
    finally:
        log.close()


# --- (5) D-F: the validation notice rides history at its natural position ---
#
# The owner ruling: a flagged turn's findings are sent to the narration model as
# a system turn placed AT the spot the flagged turn naturally sits — immediately
# after its narrative, never in the prefix and never inside <recalled>. The
# provenance hash is over the facts blocks, not history, so the notice must not
# move it (replay stays byte-faithful).

NOTICE_FLAG = {
    "rule_id": "prose_markup",
    "check": "format",
    "severity": "warn",
    "message": "raw HTML detected in prose (left untouched)",
    "anchor": "<div>TERMINAL READY</div>",
    "fix": "remove the raw markup from prose",
}


def _append_notice(log: EventLog, sid: str) -> None:
    """Flag the newest narrative (the one the notice refers to)."""
    narrative = [
        event for event in log.get_events(sid) if event.type == "narrative"
    ][-1]
    log.append_event(
        sid,
        "system",
        "validator",
        {"event": "validation_notice", "seq": narrative.seq, "flags": [NOTICE_FLAG]},
    )


def test_validation_notice_sits_directly_after_its_narrative_and_not_in_recalled(
    tmp_path: Path,
) -> None:
    log = EventLog(tmp_path / "d-f.db")
    try:
        sid = "s"
        _seed(log, sid)
        _append_notice(log, sid)
        build = make_narrator_message_builder(log)
        messages = build(_state(log, sid, recall=RECALL))

        contents = [str(message.get("content")) for message in messages]
        narrative_index = next(
            index
            for index, content in enumerate(contents)
            if "A merchant calls out." in content
        )
        assert messages[narrative_index]["role"] == "assistant"
        # Pin (b): index relationship — the notice is the VERY NEXT message.
        notice_index = narrative_index + 1
        assert messages[notice_index]["role"] == "system"
        assert _payload(contents[notice_index]).startswith("<validation_notice>")
        assert NOTICE_FLAG["fix"] in contents[notice_index]
        # ... and the trailing envelope does NOT carry the notice: it is
        # history, not engine reference material.
        recalled = _recalled(messages)
        assert recalled is not None
        assert "validation_notice" not in str(_envelope(messages)["content"])
        assert "validation_notice" not in str(messages[-1]["content"])  # the bare ask
        assert notice_index < len(messages) - 2
    finally:
        log.close()


def test_a_validation_notice_does_not_move_the_provenance_hash(tmp_path: Path) -> None:
    """Pin (c): the notice is history, not facts — replay/parity is untouched."""
    log = EventLog(tmp_path / "d-f-hash.db")
    try:
        sid = "s"
        _seed(log, sid)
        build = make_narrator_message_builder(log)
        before: dict = {}
        with_notice_absent = build(_state(log, sid, recall=RECALL), before)

        _append_notice(log, sid)
        after: dict = {}
        with_notice_present = build(_state(log, sid, recall=RECALL), after)

        # Same provenance: prefix, final user message and <recalled> are unmoved.
        assert after["facts_hash"] == before["facts_hash"]
        assert after["facts_text"] == before["facts_text"]
        assert with_notice_present[-1] == with_notice_absent[-1]
        assert with_notice_present[-2] == with_notice_absent[-2]
        # The ONLY difference is the inserted notice message.
        assert len(with_notice_present) == len(with_notice_absent) + 1
        removed = [
            message
            for message in with_notice_present
            if message not in with_notice_absent
        ]
        assert len(removed) == 1
        assert removed[0]["role"] == "system"
        assert _payload(removed[0]["content"]).startswith("<validation_notice>")
        # Prefix byte-stability: every message before the notice is identical.
        assert with_notice_present[: with_notice_present.index(removed[0])] == (
            with_notice_absent[: with_notice_present.index(removed[0])]
        )
    finally:
        log.close()


# --- (6) W9.27 C2: a component-set swap rides the continuity tail ------------
#
# A mid-session set swap is announced to the narrator ONCE, through the dynamic
# tail — never by rewriting the cache-stable prefix (AGENTS #6). The
# announcement is a LOG read (like the cast-delta notice), not the store's
# process-state one-shot, so a preview render cannot consume it and a rebuild
# recomputes it from the same ``set_swapped`` row.

LABEL_PROPS = {"props": [{"name": "label", "type": "string"}]}
ALPHA_DOC = "Alpha component reference.\n- Widget(label) — the alpha panel.\n"
BETA_DOC = "Beta component reference.\n- Gadget(label) — the beta panel.\n"


def _write_component_set(directory, components, *, doc, set_id):
    """A self-contained component set directory (spec + doc + ``set.json``)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / dsl_store.SPEC_FILENAME).write_text(
        json.dumps({"root": "ScenePane", "components": components}), encoding="utf-8"
    )
    (directory / dsl_store.DOC_FILENAME).write_text(doc, encoding="utf-8")
    (directory / dsl_store.MANIFEST_FILENAME).write_text(
        json.dumps({"id": set_id, "name": set_id.title(), "components": list(components)}),
        encoding="utf-8",
    )
    return directory


def _install_component_store(monkeypatch, directory):
    """Point the process-wide store at ``directory`` and warm the caches afresh."""
    from fabulist.prompts.library_mini import reset_library_artifact_cache

    for name in (dsl_spec.SPEC_ENV_VAR, dsl_store.ARTIFACT_DIR_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    store = dsl_store.ComponentLibraryStore(default_dir=directory)
    monkeypatch.setattr(dsl_store, "_STORE", store)
    dsl_spec.reset_spec_cache()
    reset_library_artifact_cache()
    return store


def _write_sets_index(monkeypatch, root, dirs, *, default):
    """Declare the ad-hoc set dirs in a temp registry (resolution reads it)."""
    from fabulist.dsl import sets as dsl_sets

    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "default_set": default,
        "sets": [
            {
                "id": set_id,
                "name": set_id.title(),
                "version": "1.0.0",
                "kind": "shipped",
                "dir": str(directory),
            }
            for set_id, directory in dirs.items()
        ],
    }
    (root / "index.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv(dsl_sets.SETS_DIR_ENV_VAR, str(root))
    dsl_sets.reset_sets_cache()


def _select(log, sid: str, set_id: str) -> None:
    """Append the per-session selection fact (what the API door does)."""
    from fabulist.dsl import sets as dsl_sets

    entry = dsl_sets.component_set(set_id)
    assert entry is not None
    previous = dsl_sets.session_set_id(log, sid)
    snapshot = dsl_store.library_store().snapshot_for_dir(entry.directory)
    log.append_event(
        sid,
        "system",
        "user",
        {
            "event": "set_swapped",
            "previous_set_id": previous,
            "set_id": snapshot.set_id,
            "name": snapshot.name,
            "version": snapshot.version,
            "notice": dsl_sets.swap_notice_text(snapshot),
        },
    )


def _swap_tail(messages: list[dict]) -> str | None:
    """The envelope when its ``<engine_notice>`` carries a set announcement."""
    content = _payload(_envelope(messages).get("content"))
    if "narrator component set is now" not in content:
        return None
    assert "<engine_notice>" in content
    return content


def test_a_component_set_swap_is_announced_in_the_recalled_tail_once(
    monkeypatch, tmp_path: Path
) -> None:
    alpha = _write_component_set(
        tmp_path / "alpha", {"Widget": LABEL_PROPS}, doc=ALPHA_DOC, set_id="alpha"
    )
    beta = _write_component_set(
        tmp_path / "beta", {"Gadget": LABEL_PROPS}, doc=BETA_DOC, set_id="beta"
    )
    _install_component_store(monkeypatch, alpha)
    _write_sets_index(monkeypatch, tmp_path / "sets", {"alpha": alpha, "beta": beta}, default="alpha")

    log = EventLog(tmp_path / "swap.db")
    try:
        sid = "s"
        _seed(log, sid)
        build = make_narrator_message_builder(log)
        _select(log, sid, "beta")

        messages = build(_state(log, sid, recall=RECALL))
        tail = _swap_tail(messages)
        assert tail is not None
        assert "<engine_notice>" in tail
        # The announcement is the LAST continuity entry (freshest engine change),
        # inside the block's recognised section — not a stray paragraph.
        assert tail.index("<engine_notice>") < tail.index("<engine_memory>")
        assert tail.index("Beta") > tail.index("<engine_notice>")
        # ... and the STABLE PREFIX never carries it (the cache contract).
        assert "narrator component set is now" not in str(messages[0]["content"])

        # The turn commits: from then on the swap is history, announced once.
        log.append_event(sid, "user", "user", {"text": "I keep walking."})
        log.append_event(
            sid, "narrative", "narrator", {"prose": "The market thins.", "segments": []}
        )
        after = build(_state(log, sid, recall=RECALL))
        assert _swap_tail(after) is None
        # The row stays in the log as the record (nothing was consumed, deleted
        # or rewritten).
        assert (
            len(
                [
                    event
                    for event in log.get_events(sid)
                    if isinstance(event.payload, dict)
                    and event.payload.get("event") == "set_swapped"
                ]
            )
            == 1
        )
    finally:
        log.close()


def test_the_swap_announcement_rides_after_the_validation_notices(
    monkeypatch, tmp_path: Path
) -> None:
    """P2-7 ordering: the D-F teaching notice is history; the swap is the tail.

    Both describe engine-side corrections, and the prompt's dynamic tail is
    ordered: history (with its notices) -> the turn's own ask -> ``<recalled>``
    (swap announcement last inside it). Nothing precedes a region it describes.
    """
    alpha = _write_component_set(
        tmp_path / "alpha", {"Widget": LABEL_PROPS}, doc=ALPHA_DOC, set_id="alpha"
    )
    beta = _write_component_set(
        tmp_path / "beta", {"Gadget": LABEL_PROPS}, doc=BETA_DOC, set_id="beta"
    )
    _install_component_store(monkeypatch, alpha)
    _write_sets_index(monkeypatch, tmp_path / "sets", {"alpha": alpha, "beta": beta}, default="alpha")

    log = EventLog(tmp_path / "swap-order.db")
    try:
        sid = "s"
        _seed(log, sid)
        _append_notice(log, sid)
        build = make_narrator_message_builder(log)
        _select(log, sid, "beta")

        messages = build(_state(log, sid, recall=RECALL))
        contents = [str(message.get("content")) for message in messages]
        notice_index = next(
            index
            for index, content in enumerate(contents)
            if _payload(content).startswith("<validation_notice>")
        )
        final_user_index = next(
            index
            for index, message in enumerate(messages)
            if message["role"] == "user" and "What did the merchant wear?" in contents[index]
        )
        assert notice_index < final_user_index == len(messages) - 1
        assert messages[-1]["role"] == "user"
        assert messages[-2]["role"] == "assistant"
        assert _swap_tail(messages) is not None
    finally:
        log.close()


def test_a_swap_notice_is_advisory_and_its_absence_is_byte_stable(
    monkeypatch, tmp_path: Path
) -> None:
    """No swap row -> the prompt is exactly what the pre-W9.27 builder produced."""
    alpha = _write_component_set(
        tmp_path / "alpha", {"Widget": LABEL_PROPS}, doc=ALPHA_DOC, set_id="alpha"
    )
    _install_component_store(monkeypatch, alpha)

    log = EventLog(tmp_path / "no-swap.db")
    try:
        sid = "s"
        _seed(log, sid)
        build = make_narrator_message_builder(log)
        messages = build(_state(log, sid, recall=RECALL))
        assert _swap_tail(messages) is None
        # The envelope still carries the memory block; only the notice is absent.
        assert "<engine_memory>" in str(_envelope(messages)["content"])
        assert "narrator component set is now" not in str(
            _envelope(messages)["content"]
        )
    finally:
        log.close()
