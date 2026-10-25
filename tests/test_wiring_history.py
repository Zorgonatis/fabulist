"""Wiring-level history tests: fork-awareness (W2.2 integration defect fix).

``history_from_log`` must walk the projection chain so a branch session sees
its inherited turns (the pre-W2.1 ``get_events`` read was session-local — a
forked narrator would have amnesia). Also covers DSL stripping and the preset
regex stage's depth gate.
"""

from __future__ import annotations

import json

import pytest

from fabulist.dsl import spec as dsl_spec
from fabulist.dsl import store as dsl_store
from fabulist.log import EventLog
from fabulist.prompts.library_mini import reset_library_artifact_cache
from fabulist.wiring import history_from_log, make_narrator_message_builder


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


def _seed(log: EventLog) -> str:
    sid = "base"
    log.append_event(sid, "system", "system", {"event": "session_created"})
    log.append_event(sid, "user", "user", {"text": "I enter the tavern."})
    log.append_event(
        sid,
        "narrative",
        "narrator",
        {
            "prose": "card = ArtifactCard(\"Damp\", \"Tavern\")\nThe fire crackles.",
            "dsl": 'card = ArtifactCard("Damp", "Tavern")',
            "segments": [
                {"kind": "dsl", "text": 'card = ArtifactCard("Damp", "Tavern")'},
                {"kind": "prose", "text": "The fire crackles."},
            ],
        },
    )
    log.append_event(sid, "user", "user", {"text": "I sit by the fire."})
    log.append_event(
        sid,
        "narrative",
        "narrator",
        {"prose": "Warmth spreads.", "dsl": "", "segments": [{"kind": "prose", "text": "Warmth spreads."}]},
    )
    return sid


def test_history_includes_inherited_turns_after_fork(tmp_path):
    log = EventLog(tmp_path / "h.db")
    base = _seed(log)
    log.fork_session(base, at_seq=3, new_session_id="branch")
    log.append_event("branch", "user", "user", {"text": "I poke the embers."})

    hist = history_from_log(log, "branch")
    # Inherited prose (seq<=3) + branch's own turn — no amnesia.
    assert [m["role"] for m in hist] == ["user", "assistant", "user"]
    assert "fire crackles" in hist[1]["content"]
    assert hist[2]["content"] == "I poke the embers."


def test_history_strips_dsl_from_prose(tmp_path):
    log = EventLog(tmp_path / "h.db")
    base = _seed(log)
    # D34 default: the newest 3 narratives keep their DSL in place (the model
    # sees its own recent panels; no reinjection seam).
    hist = history_from_log(log, base)
    assert "ArtifactCard" in hist[1]["content"]
    assert "fire crackles" in hist[1]["content"]
    # Opt-out (dsl_keep_last=0) reproduces the legacy strip-all projection.
    hist_legacy = history_from_log(log, base, surfaces={"history": {"dsl_keep_last": 0}})
    assert all("ArtifactCard" not in m["content"] for m in hist_legacy)
    assert "fire crackles" in hist_legacy[1]["content"]


def test_history_keeps_only_newest_three_narratives_dsl(tmp_path):
    log = EventLog(tmp_path / "h.db")
    base = _seed(log)
    for i in range(3):
        log.append_event(base, "user", "user", {"text": f"turn {i}"})
        log.append_event(
            base,
            "narrative",
            "narrator",
            {"prose": f"beat {i}.", "dsl": "", "segments": [{"kind": "prose", "text": f"beat {i}."}]},
        )
    hist = history_from_log(log, base)
    # The tavern DSL turn is now the 4th-newest narrative -> stripped at default.
    assert "ArtifactCard" not in hist[1]["content"]
    assert "fire crackles" in hist[1]["content"]


def test_history_regex_stage_respects_depth(tmp_path):
    log = EventLog(tmp_path / "h.db")
    base = _seed(log)
    # Add 3 newer turns so the VTK-bearing turn falls past min_depth=3.
    for i in range(3):
        log.append_event(base, "user", "user", {"text": f"turn {i}"})
        log.append_event(
            base,
            "narrative",
            "narrator",
            {"prose": f"<!-- VTK_START -->old panel {i}<!-- VTK_END --> beat {i}."},
        )
    hist = history_from_log(log, base)
    vtk_left = [m["content"] for m in hist if "VTK_START" in m["content"]]
    # Depths (from newest): panel2=0, panel1=2 -> exempt; panel0=4 -> stripped.
    assert len(vtk_left) == 2
    assert any("beat 2" in m["content"] for m in hist)  # newest prose intact
    stripped = [m["content"] for m in hist if "beat 0" in m["content"]]
    assert len(stripped) == 1 and "VTK_START" not in stripped[0]


# --- drop_current_user: the current turn's own row --------------------------
#
# ``api/app.py:_drive_turn`` appends the turn's ``user`` event BEFORE the graph
# runs, so the newest user row in the projection is the turn being generated.
# The narrate/OOC builders opt in to dropping it (the flow re-appends the same
# turn as the final state-wrapped user message); every other caller keeps the
# old behaviour. The drop is POSITIONAL — never a text match.


def test_drop_current_user_removes_only_the_newest_user_row(tmp_path):
    log = EventLog(tmp_path / "h.db")
    base = _seed(log)  # newest user row: "I sit by the fire."
    kept = history_from_log(log, base, drop_current_user=True)
    roles = [m["role"] for m in kept]
    assert roles == ["user", "assistant", "assistant"]
    assert kept[0]["content"] == "I enter the tavern."
    assert all("I sit by the fire." not in m["content"] for m in kept)


def test_drop_current_user_default_off_keeps_every_row(tmp_path):
    log = EventLog(tmp_path / "h.db")
    base = _seed(log)
    # No opt-in -> byte-identical to the pre-fix projection (callers whose
    # history is not the projection-plus-live-ask shape are untouched).
    hist = history_from_log(log, base)
    assert hist == history_from_log(log, base, drop_current_user=False)
    assert [m["content"] for m in hist if m["role"] == "user"] == [
        "I enter the tavern.",
        "I sit by the fire.",
    ]


def test_drop_current_user_is_positional_not_text_matched(tmp_path):
    # A player who sends the SAME line twice is a normal case: the prior row
    # must survive and only the newest row is dropped.
    log = EventLog(tmp_path / "h.db")
    sid = "s"
    log.append_event(sid, "user", "user", {"text": "again"})
    log.append_event(
        sid, "narrative", "narrator", {"prose": "beat one.", "segments": []}
    )
    log.append_event(sid, "user", "user", {"text": "again"})  # the current turn
    hist = history_from_log(log, sid, drop_current_user=True)
    assert [m["role"] for m in hist] == ["user", "assistant"]
    assert hist[0]["content"] == "again"


def test_drop_current_user_is_a_noop_without_a_user_row(tmp_path):
    log = EventLog(tmp_path / "h.db")
    sid = "s"
    log.append_event(
        sid, "narrative", "narrator", {"prose": "Only prose.", "segments": []}
    )
    assert history_from_log(log, sid, drop_current_user=True) == history_from_log(
        log, sid
    )


def test_ooc_builder_carries_the_live_user_ask_once(tmp_path):
    from fabulist.wiring import make_ooc_message_builder

    log = EventLog(tmp_path / "ooc.db")
    sid = "s"
    log.append_event(sid, "user", "user", {"text": "earlier question"})
    log.append_event(
        sid, "narrative", "narrator", {"prose": "A beat.", "segments": []}
    )
    # ``_drive_turn`` pre-appends the OOC turn's user row; the builder then
    # re-appends it flagged ([OOC]) as the final user turn.
    log.append_event(sid, "user", "user", {"text": "what now?", "kind": "ooc"})
    messages = make_ooc_message_builder(log)(sid, "what now?")
    contents = [str(m.get("content")) for m in messages]
    assert sum(1 for c in contents if "what now?" in c) == 1
    assert any("earlier question" in c for c in contents)  # prior row survives
    assert messages[-1]["role"] == "user"
    assert "[OOC]" in contents[-1]


# --- D-F: the validation notice rides history at its natural position --------
#
# The validate stage appends a ``validation_notice`` system row naming the
# narrative it flagged. ``history_from_log`` places it as a system message
# IMMEDIATELY AFTER that narrative's assistant message — never the prefix, never
# the tail — and attaches it after the depth cut so a trim can never leave an
# orphan notice behind.

NOTICE_FLAG = {
    "rule_id": "prose_markup",
    "check": "format",
    "severity": "warn",
    "message": "raw HTML detected in prose (left untouched)",
    "anchor": "<div>TERMINAL READY</div>",
    "fix": "remove the raw markup from prose",
}


def _seed_noticed(log: EventLog) -> str:
    """Two turns; the SECOND narrative carries a validation notice."""
    sid = "noticed"
    log.append_event(sid, "system", "system", {"event": "session_created"})
    log.append_event(sid, "user", "user", {"text": "I enter the tavern."})
    log.append_event(
        sid, "narrative", "narrator", {"prose": "The fire crackles.", "segments": []}
    )
    log.append_event(sid, "user", "user", {"text": "I sit by the fire."})
    second = log.append_event(
        sid, "narrative", "narrator", {"prose": "Warmth spreads.", "segments": []}
    )
    log.append_event(
        sid,
        "system",
        "validator",
        {"event": "validation_notice", "seq": second.seq, "flags": [NOTICE_FLAG]},
    )
    return sid


def test_history_places_the_notice_directly_after_its_narrative(tmp_path):
    log = EventLog(tmp_path / "notice.db")
    sid = _seed_noticed(log)

    hist = history_from_log(log, sid)
    roles = [message["role"] for message in hist]
    narrative_index = next(
        index
        for index, message in enumerate(hist)
        if message["content"] == "Warmth spreads."
    )
    assert roles[narrative_index] == "assistant"
    assert roles[narrative_index + 1] == "system"
    assert hist[narrative_index + 1]["content"].startswith("<validation_notice>")
    assert NOTICE_FLAG["fix"] in hist[narrative_index + 1]["content"]
    # Exactly one notice, and it belongs to the second narrative (the first
    # assistant message has no system turn after it).
    assert [index for index, role in enumerate(roles) if role == "system"] == [
        narrative_index + 1
    ]


def test_a_notice_whites_out_with_the_narrative_the_depth_cut_drops(tmp_path):
    """No orphan notices: the notice is attached only to a surviving turn."""
    log = EventLog(tmp_path / "orphan.db")
    sid = "orphan"
    log.append_event(sid, "user", "user", {"text": "one"})
    first = log.append_event(
        sid, "narrative", "narrator", {"prose": "First beat.", "segments": []}
    )
    log.append_event(sid, "user", "user", {"text": "two"})
    log.append_event(sid, "narrative", "narrator", {"prose": "Second beat.", "segments": []})
    log.append_event(
        sid,
        "system",
        "validator",
        {"event": "validation_notice", "seq": first.seq, "flags": [NOTICE_FLAG]},
    )

    # limit=2 keeps [user "two", narrative "Second beat."] — the flagged FIRST
    # turn is cut, so its notice must not survive as a floating system turn.
    hist = history_from_log(log, sid, limit=2)
    assert [message["role"] for message in hist] == ["user", "assistant"]
    assert all("validation_notice" not in str(message["content"]) for message in hist)


def test_a_notice_never_joins_the_history_of_inherited_rows_twice(tmp_path):
    """A fork inherits its ancestor's notice exactly once (chain read, not two).

    The fork cut is AFTER the notice row, so the flagged narrative and its notice
    are both inherited; a cut before the flagged narrative (a swipe) drops both —
    the turn being replaced must not inherit the notice that described it.
    """
    log = EventLog(tmp_path / "fork.db")
    sid = _seed_noticed(log)
    notice_seq = next(
        event.seq
        for event in log.get_events(sid)
        if event.payload.get("event") == "validation_notice"
    )
    log.fork_session(sid, at_seq=notice_seq, new_session_id="branch")
    log.append_event("branch", "user", "user", {"text": "I poke the embers."})

    hist = history_from_log(log, "branch")
    notices = [
        message
        for message in hist
        if str(message["content"]).startswith("<validation_notice>")
    ]
    assert len(notices) == 1


def test_a_swipe_fork_before_the_flagged_turn_drops_its_notice(tmp_path):
    """The turn being regenerated carries neither itself nor its notice."""
    log = EventLog(tmp_path / "regen.db")
    sid = "regen"
    log.append_event(sid, "user", "user", {"text": "one"})
    log.append_event(sid, "narrative", "narrator", {"prose": "First beat.", "segments": []})
    user_row = log.append_event(sid, "user", "user", {"text": "two"})
    flagged = log.append_event(
        sid, "narrative", "narrator", {"prose": "Second beat.", "segments": []}
    )
    log.append_event(
        sid,
        "system",
        "validator",
        {"event": "validation_notice", "seq": flagged.seq, "flags": [NOTICE_FLAG]},
    )
    # A swipe/regenerate fork cuts at the user row: the flagged narrative and
    # its notice are both AFTER the cut, so the branch starts clean.
    log.fork_session(sid, at_seq=user_row.seq, new_session_id="branch")

    hist = history_from_log(log, "branch")
    assert all(
        "validation_notice" not in str(message["content"]) for message in hist
    )
    assert all(message["content"] != "Second beat." for message in hist)


# --- W9.26 §2.4: the era model — covered turns leave the history -----------
#
# A ``summary`` row's ``covers_seqs`` span names the turns whose PROSE the recap
# now carries. They must not ride the history as well: rendering both pays for
# the same history twice AND gives the model two versions of it. The covered set
# is extended to whole TURN PAIRS (Lane A's ``covered_turn_seqs``), so a covered
# narrative never leaves its player line behind as an orphan.
#
# ``_seed`` writes seq 1..5: session_created, user, narrative, user, narrative.


def _seq_of(log: EventLog, sid: str, *, prose: str | None = None, text: str | None = None) -> int:
    """The seq of the row carrying ``prose`` (narrative) or ``text`` (user)."""
    for event in log.get_events(sid):
        payload = event.payload or {}
        if prose is not None and payload.get("prose") == prose:
            return event.seq
        if text is not None and payload.get("text") == text:
            return event.seq
    raise AssertionError("row not found")


def _summarize(log: EventLog, sid: str, *, covers: list[int], text: str = "Recap.") -> int:
    return log.append_event(
        sid, "summary", "summarizer", {"text": text, "covers_seqs": covers}
    ).seq


def test_a_covered_turn_leaves_the_history_and_rides_the_recap(tmp_path):
    """W9.26 §2.4: one version of the history, not two."""
    log = EventLog(tmp_path / "covered.db")
    sid = _seed(log)
    head = max(event.seq for event in log.get_events(sid))
    _summarize(log, sid, covers=[1, head], text="They met in a tavern.")

    hist = history_from_log(log, sid)
    assert hist == []
    # The text still reaches the model — through the <recalled> recap.
    from fabulist.context.facts import recalled_message

    recalled = recalled_message(log, sid, user_text="I wait.")
    assert "They met in a tavern." in str(recalled)


def test_an_uncovered_turn_stays_raw_next_to_a_compacted_one(tmp_path):
    log = EventLog(tmp_path / "partial.db")
    sid = _seed(log)
    # The FIRST turn only (its user row through its narrative).
    first_user = _seq_of(log, sid, text="I enter the tavern.")
    first_narrative = _seq_of(log, sid, prose="card = ArtifactCard(\"Damp\", \"Tavern\")\nThe fire crackles.")
    _summarize(log, sid, covers=[first_user, first_narrative])

    hist = history_from_log(log, sid)
    assert [m["content"] for m in hist if m["role"] == "user"] == ["I sit by the fire."]
    assert [m["content"] for m in hist if m["role"] == "assistant"] == ["Warmth spreads."]


def test_a_legacy_single_seq_span_covers_the_users_row_too(tmp_path):
    """The turn-PAIR extension: no orphan player line at a span boundary.

    Legacy summaries recorded ``[N, N]`` (the narrative only). Excluding just
    ``N`` would leave the player's own line (``N-1``) raw directly above the
    recap that already contains it — the defect §2.3 step 3 pins.
    """
    log = EventLog(tmp_path / "legacy.db")
    sid = _seed(log)
    narrative_seq = _seq_of(
        log, sid, prose="card = ArtifactCard(\"Damp\", \"Tavern\")\nThe fire crackles."
    )
    _summarize(log, sid, covers=[narrative_seq, narrative_seq], text="The tavern was damp.")

    hist = history_from_log(log, sid)
    contents = [m["content"] for m in hist]
    assert "The fire crackles." not in " ".join(map(str, contents))
    assert "I enter the tavern." not in contents  # its user row went with it
    assert contents == ["I sit by the fire.", "Warmth spreads."]


def test_the_covered_exclusion_runs_after_the_position_computation(tmp_path):
    """Position-first (the design's order): the covered set never moves a position.

    ``drop_current_user`` names the NEWEST user row of the UNFILTERED projection.
    Run exclusion first and a covered newest user row makes an OLDER row "the
    current turn" — dropping a settled message the model must still see.
    """
    log = EventLog(tmp_path / "positions.db")
    sid = _seed(log)
    current = log.append_event(sid, "user", "user", {"text": "what now?"})
    newest = log.append_event(
        sid, "narrative", "narrator", {"prose": "Newest beat.", "segments": []}
    )
    _summarize(log, sid, covers=[newest.seq, newest.seq])

    hist = history_from_log(log, sid, drop_current_user=True)
    assert [m["content"] for m in hist if m["role"] == "user"] == [
        "I enter the tavern.",
        "I sit by the fire.",
    ]
    # The covered newest turn (and its user row, by the turn-pair rule) is gone.
    assert "Newest beat." not in [m["content"] for m in hist]
    # The live ask IS still the newest user row of the projection: that positional
    # answer never changed, which is what keeps the drop honest.
    assert current.seq == max(
        item["seq"] for item in log.get_projection(sid)["user_messages"]
    )


def test_the_covered_exclusion_does_not_shift_the_keep_last_window(tmp_path):
    """``dsl_keep_last`` counts narratives of the UNFILTERED projection.

    Computed after exclusion, covered narratives would let an OLDER narrative
    keep its DSL (the window silently widens). Here the two newest narratives are
    covered; the tavern turn must still strip exactly as it would with no summary.
    """
    log = EventLog(tmp_path / "keep.db")
    base = _seed(log)  # narrative 1 of 2 carries DSL
    covered_seqs = []
    for i in range(2):
        log.append_event(base, "user", "user", {"text": f"turn {i}"})
        row = log.append_event(
            base, "narrative", "narrator", {"prose": f"beat {i}.", "segments": []}
        )
        covered_seqs.append(row.seq)
    _summarize(log, base, covers=covered_seqs, text="Recap of the last two beats.")

    # Four narratives exist; keep_last=3 starts at the second-newest, so the
    # tavern turn strips. Exclusion-first would leave two narratives and keep it.
    hist = history_from_log(log, base)
    tavern = next(m for m in hist if "fire crackles" in str(m["content"]))
    assert "ArtifactCard" not in tavern["content"]


def test_history_reads_the_chain_once_for_notices_and_coverage(tmp_path, monkeypatch):
    """One scan, not two (W9.26 §2.4): the notice pass and the covered set share it."""
    log = EventLog(tmp_path / "scan.db")
    sid = _seed_noticed(log)
    calls: list[str] = []
    original = log.get_chain_events

    def counting(session_id: str):
        calls.append(session_id)
        return original(session_id)

    monkeypatch.setattr(log, "get_chain_events", counting)

    hist = history_from_log(log, sid)

    assert any(str(m["content"]).startswith("<validation_notice>") for m in hist)
    assert calls == [sid]


# --- #16/#24: per-session component-set selection ----------------------------
#
# A set is selected by appending a ``set_swapped`` row (the API door does this)
# and takes effect at the NEXT TURN's narrator build — nowhere else. The store's
# ``snapshot()`` is called many times per turn in no fixed order, so the BUILD is
# the resolution boundary (R2 fold P1-2): it binds THIS session's selected
# directory, and a different session's selection can never retarget it.

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


def _select(log: EventLog, sid: str, set_id: str) -> None:
    """Append the selection fact exactly as the API door does (no promotion)."""
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


def _swap_state(sid: str) -> dict:
    return {
        "session_id": sid,
        "user_content": "I look around.",
        "kind": "in_character",
        "facts": "",
        "recall": "",
        "rolls": "",
    }


def test_a_selection_takes_effect_at_the_next_build_not_at_selection_time(
    monkeypatch, tmp_path
):
    """P1-2: the build is the turn boundary; readers before it see the old set."""
    from fabulist.dsl.spec import component_names
    from fabulist.prompts.library_mini import render_library_doc

    alpha = _write_component_set(
        tmp_path / "alpha", {"Widget": LABEL_PROPS}, doc=ALPHA_DOC, set_id="alpha"
    )
    beta = _write_component_set(
        tmp_path / "beta", {"Gadget": LABEL_PROPS}, doc=BETA_DOC, set_id="beta"
    )
    store = _install_component_store(monkeypatch, alpha)
    _write_sets_index(monkeypatch, tmp_path / "sets", {"alpha": alpha, "beta": beta}, default="alpha")

    log = EventLog(tmp_path / "swap.db")
    try:
        sid = "s"
        log.append_event(sid, "user", "user", {"text": "I look around."})
        build = make_narrator_message_builder(log)

        _select(log, sid, "beta")
        for _ in range(3):  # mid-turn reads, in no particular order
            store.snapshot()
            component_names()
            render_library_doc()
        assert store.current_set_id() == "alpha"
        assert "Gadget" not in component_names()

        build(_swap_state(sid))
        # The boundary resolved it — and the content cache is now beta's.
        assert store.current_set_id() == "beta"
        assert "Gadget" in component_names()
        assert BETA_DOC.strip() in render_library_doc()
    finally:
        log.close()


def test_the_swap_is_announced_once_and_the_prompt_teaches_the_new_doc(
    monkeypatch, tmp_path
):
    """The selected set's doc is what the build TAUGHT; the log holds one row."""
    from fabulist.log.schema_gate import SET_SWAP_EVENT

    alpha = _write_component_set(
        tmp_path / "alpha", {"Widget": LABEL_PROPS}, doc=ALPHA_DOC, set_id="alpha"
    )
    beta = _write_component_set(
        tmp_path / "beta", {"Gadget": LABEL_PROPS}, doc=BETA_DOC, set_id="beta"
    )
    store = _install_component_store(monkeypatch, alpha)
    _write_sets_index(monkeypatch, tmp_path / "sets", {"alpha": alpha, "beta": beta}, default="alpha")

    log = EventLog(tmp_path / "swap.db")
    try:
        sid = "s"
        log.append_event(sid, "user", "user", {"text": "I look around."})
        build = make_narrator_message_builder(log)
        _select(log, sid, "beta")

        out: dict = {}
        first = build(_swap_state(sid), out)
        assert BETA_DOC.strip() in str(first[0]["content"])  # taught, not alpha
        assert ALPHA_DOC.strip() not in str(first[0]["content"])
        # Issue #44: the envelope is the assistant message before the ask.
        first_announcement = str(first[-2]["content"])
        assert "Beta" in first_announcement and "(beta)" in first_announcement

        rows = [
            event
            for event in log.get_events(sid)
            if isinstance(event.payload, dict)
            and event.payload.get("event") == SET_SWAP_EVENT
        ]
        assert len(rows) == 1  # the DOOR appends one row; the build appends none
        payload = rows[0].payload
        assert (payload["previous_set_id"], payload["set_id"], payload["name"]) == (
            "alpha",
            "beta",
            "Beta",
        )
        assert payload["version"] == store.snapshot().version

        # A second build of the SAME turn (a preview, a retry) re-announces —
        # the announcement is log-derived, so nothing was consumed.
        second = build(_swap_state(sid))
        assert "Beta" in str(second[-2]["content"])
        assert len(
            [
                event
                for event in log.get_events(sid)
                if isinstance(event.payload, dict)
                and event.payload.get("event") == SET_SWAP_EVENT
            ]
        ) == 1
    finally:
        log.close()


def test_region_sizes_report_the_doc_this_build_taught(monkeypatch, tmp_path):
    """The inspector's ``library`` region is the CURRENT doc (the creation capture died)."""
    from fabulist.prompts.library_mini import render_library_doc

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
        log.append_event(sid, "user", "user", {"text": "I look around."})
        build = make_narrator_message_builder(log)  # created while alpha is active
        _select(log, sid, "beta")

        out: dict = {}
        build(_swap_state(sid), out)
        current_doc = render_library_doc()
        assert BETA_DOC.strip() in current_doc
        assert out["regions"]["library"] == len(current_doc)
    finally:
        log.close()


# --- W9.28 W4: the SPEAKER PALETTE closes the dynamic tail -------------------
#
# D7/D6b: the colours the narrator is told to write are the colours THIS turn
# parses back out, so the block is recomputed per build and rides the dynamic
# tail — AFTER the lane block, and therefore after every D-F notice and the
# ``set_swapped`` row inside ``<recalled>``. Never the prefix: the active cast
# changes as the scene does, while the cache-stable prefix must not (AGENTS #6).


def _seed_cast(log: EventLog, *names: str, sid: str = "p") -> str:
    """A session whose card cast is the whole scene (the presence seed floor)."""
    for name in names:
        log.append_event(
            sid, "system", "system", {"card": {"name": name, "description": name}}
        )
    log.append_event(sid, "user", "user", {"text": "I wait by the door."})
    return sid


def _payload(content: object) -> str:
    """The message body without the served two-bound envelope (issue #26).

    ``<preset_def>`` wraps the engine definition through the player's latest
    message, ``<preset_runtime>`` the per-turn tail after it; the guard line (on
    the first def message) sits outside both.
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


def _palette_state(sid: str, **overrides) -> dict:
    state = {
        "session_id": sid,
        "user_content": "I wait by the door.",
        "kind": "in_character",
        "facts": "",
        "recall": "",
        "rolls": "",
    }
    state.update(overrides)
    return state


def test_the_palette_block_closes_the_prompt_after_the_lane_block(tmp_path):
    """The palette is the LAST dynamic message; the lanes sit directly above it."""
    from fabulist.api.cast_api import SPEAKER_PALETTE, SPEAKER_PALETTE_NAMES

    log = EventLog(tmp_path / "palette.db")
    try:
        sid = _seed_cast(log, "Mara", "Innkeeper")
        lanes = {
            "m1": {
                "name": "Mara",
                "intent": "bar the door",
                "narrative": "she slides the bolt",
                "lines": [],
            }
        }
        messages = make_narrator_message_builder(log)(
            _palette_state(sid, npc_lanes=lanes)
        )

        # ONE per-turn engine envelope (issue #44: an assistant message
        # immediately BEFORE the player's ask): the lane block, then the
        # palette LAST.
        user_at = max(i for i, m in enumerate(messages) if m["role"] == "user")
        assert user_at == len(messages) - 1
        assert messages[-2]["role"] == "assistant"
        envelope = _payload(messages[-2]["content"])
        assert "<engine_lane>" in envelope
        assert "intentions this turn" in envelope
        assert envelope.rstrip().endswith("</engine_palette>")
        assert envelope.index("<engine_lane>") < envelope.index("<engine_palette>")
        assert "SPEAKER PALETTE" in envelope
        assert "<engine_" not in str(messages[-1]["content"])  # the bare ask

        # One line per active member, carrying BOTH spellings the parser accepts.
        rendered = [
            f"- Mara: {SPEAKER_PALETTE_NAMES[0]} {SPEAKER_PALETTE[0]}",
            f"- Innkeeper: {SPEAKER_PALETTE_NAMES[1]} {SPEAKER_PALETTE[1]}",
        ]
        assert rendered[0] in envelope and rendered[1] in envelope
        # The engine's internal id never reaches the model.
        assert "cm_" not in envelope

        # Nothing in the cache-stable prefix changed: the block is a tail row.
        prefix = str(messages[0]["content"])
        assert "\n<engine_palette>\n" not in prefix
        assert "SPEAKER PALETTE —" not in prefix  # the block's directive line
    finally:
        log.close()


def test_an_empty_palette_appends_no_message(tmp_path):
    """A cast-less turn's prompt is unchanged — no empty directive row."""
    log = EventLog(tmp_path / "no-palette.db")
    try:
        log.append_event("p", "user", "user", {"text": "I wait by the door."})
        messages = make_narrator_message_builder(log)(_palette_state("p"))

        assert not [
            message
            for message in messages
            if str(message.get("content")).startswith("SPEAKER PALETTE")
        ]
        assert messages[-1]["role"] == "user"
        assert "<engine_palette>" not in str(messages[-1]["content"])
    finally:
        log.close()


def test_the_palette_is_a_pure_suffix_and_moves_no_prefix_byte(
    monkeypatch, tmp_path
):
    """D7: with the palette and without it, every earlier message is identical."""
    import fabulist.wiring as wiring

    log = EventLog(tmp_path / "palette-suffix.db")
    try:
        sid = _seed_cast(log, "Mara")
        build = make_narrator_message_builder(log)
        real = wiring.palette_for_turn
        calls: list[str] = []

        def counting(log_arg, session):
            calls.append(session)
            return real(log_arg, session)

        monkeypatch.setattr(wiring, "palette_for_turn", counting)
        with_palette = build(_palette_state(sid))
        assert calls == [sid]  # built ONCE per build, not per message

        monkeypatch.setattr(wiring, "palette_for_turn", lambda log_arg, session: [])
        without = build(_palette_state(sid))

        # The envelope is a pure PREFIX-STABLE insert: with it dropped, the two
        # lists are identical (the palette never moves an earlier byte).
        assert with_palette[-1] == without[-1]  # the same bare ask
        assert with_palette[:-2] + with_palette[-1:] == without
        assert with_palette[0] == without[0]
        envelope = _payload(with_palette[-2]["content"])
        assert "<engine_palette>" in envelope and "SPEAKER PALETTE" in envelope
        # A palette-less turn with no other blocks emits NO envelope at all.
        from fabulist.prompts.wrapper import RUNTIME_OPEN

        assert without[-1]["role"] == "user"
        assert not any(
            f"\n{RUNTIME_OPEN}\n" in str(m["content"]) for m in without
        )
    finally:
        log.close()


def test_region_sizes_report_the_palette_as_its_own_row(tmp_path):
    """The inspector sees the palette region; it never leaks into ``npc``."""
    log = EventLog(tmp_path / "palette-regions.db")
    try:
        sid = _seed_cast(log, "Mara")
        out: dict = {}
        messages = make_narrator_message_builder(log)(_palette_state(sid), out)

        # Issue #44: the region rows report the BLOCK bodies now that they all
        # ride one envelope message (no per-block message to measure).
        assert out["regions"]["palette"] > 0
        assert out["regions"]["npc"] == 0
        assert out["regions"]["history"] == 0
    finally:
        log.close()
