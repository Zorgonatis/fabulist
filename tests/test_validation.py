"""Tests for the W2.4b narrator-side DSL validation gate.

Covers the spec's acceptance matrix: the four shipped components parse; unknown
components, arity mismatches, unbalanced parens and HTML/script injections are
rejected; prose passes through untouched; a mixed payload keeps its valid
segments; and the retry seam reports both a successful and a still-bad
re-synthesis. Flavor loading (lazy, cached, JSON-driven fallback) is covered too.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json

import pytest

from fabulist.dsl import spec as dsl_spec
from fabulist.dsl import store as dsl_store
from fabulist.graph import validation
from fabulist.graph.validation import (
    DslError,
    DslValidationReport,
    component_specs,
    dsl_error_flags,
    known_component_names,
    reset_component_specs_cache,
    revalidate_with_retry,
    validate_dsl,
)

#: The four canonical infrastructure components plus the legacy ``ArtifactCard``
#: alias (renamed to ``ObjectCard`` in Wave 3A; the alias still validates/renders).
CORE_NAMES = {"ScenePane", "MeterBar", "ObjectCard", "ArtifactCard", "BeatCard"}


@pytest.fixture(autouse=True)
def _fresh_spec_cache():
    """Reset the spec cache around every test.

    Lazy-load tests pointing the path at a missing file cache the fallback
    four; without a reset, later flavor tests in the same process inherit the
    poisoned table and report known components as unknown.
    """
    reset_component_specs_cache()
    yield
    reset_component_specs_cache()


@pytest.fixture(autouse=True)
def _clean_spec_cache(monkeypatch, tmp_path):
    """Each test starts with a cold cache pointed at nonexistent spec files.

    Hermetic by default: tests that want flavors set their own env path. This
    keeps the suite independent of whether the repo's generated
    ``library.spec.json`` / ``flavors.json`` exist (other W2.4 lanes own them),
    and the tests below exercise the core-four fallback and the injected-mirror
    merge in isolation.
    """
    monkeypatch.setenv(validation.FLAVORS_ENV_VAR, str(tmp_path / "absent-flavors.json"))
    monkeypatch.setenv(dsl_spec.SPEC_ENV_VAR, str(tmp_path / "absent-spec.json"))
    reset_component_specs_cache()
    yield
    reset_component_specs_cache()


def run(coro):
    return asyncio.run(coro)


def dsl(text: str) -> dict:
    return {"kind": "dsl", "text": text}


def prose(text: str) -> dict:
    return {"kind": "prose", "text": text}


VALID_PAYLOAD = [
    prose("The night market hums."),
    dsl('root = ScenePane("Rusty Anchor Tavern — Evening", [tension, compass, hook])'),
    dsl('tension = MeterBar("Tension", 68, 100)'),
    dsl('compass = ArtifactCard("Cursed Compass", "Quest item", "Points at the last liar.")'),
    dsl('hook = BeatCard("The innkeeper avoids your eyes.")'),
]


# ---------------------------------------------------------------------------
# 1. valid segments
# ---------------------------------------------------------------------------


def test_valid_component_payload_passes():
    report = validate_dsl(VALID_PAYLOAD)

    assert report.ok is True
    assert report.errors == []
    # Every segment survives untouched and in order.
    assert report.segments == VALID_PAYLOAD
    assert report.retry_applied is False


def test_prose_segments_pass_through_untouched():
    segments = [prose("A line."), prose("Another line.")]
    report = validate_dsl(segments)
    assert report.ok is True
    assert report.segments == segments


def test_empty_payload_is_ok():
    report = validate_dsl([])
    assert report.ok is True
    assert report.errors == []
    assert report.segments == []


def test_commas_and_parens_inside_strings_do_not_split_args():
    segment = dsl(
        'card = ArtifactCard("Blade, of Dawn", "Weapon (rare)", "Deals 1d6, +2 at dusk.")'
    )
    report = validate_dsl([segment])
    assert report.ok is True, report.errors


def test_multiline_statement_is_one_segment():
    segment = dsl('sum = MeterBar(\n  "Tension",\n  68,\n  100\n)')
    assert validate_dsl([segment]).ok is True


# ---------------------------------------------------------------------------
# 2. unknown component
# ---------------------------------------------------------------------------


def test_unknown_component_is_rejected():
    report = validate_dsl([dsl('widget = FancyWidget("x")')])

    assert report.ok is False
    assert [e.code for e in report.errors] == ["unknown_component"]
    assert report.errors[0].segment_index == 0
    # Keep + tag: the failed statement is KEPT verbatim (nothing is dropped).
    assert report.segments == [dsl('widget = FancyWidget("x")')]


# ---------------------------------------------------------------------------
# 3. arity mismatch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        'card = ArtifactCard("Anxious", "Tavern")',  # 2 of 3
        'card = ArtifactCard("a", "b", "c", "d", "e")',  # 5 of 4 (options widened max)
        'hook = BeatCard()',  # 0 of 1
        'root = ScenePane("Only a title")',  # 1 of 2
    ],
)
def test_arity_mismatch_is_rejected(text):
    report = validate_dsl([dsl(text)])

    assert report.ok is False
    assert [e.code for e in report.errors] == ["arity_mismatch"]
    assert "expects" in report.errors[0].message


# ---------------------------------------------------------------------------
# 4. unbalanced parens / quotes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ('card = ArtifactCard("a", "b"', "unbalanced_parens"),
        ('card = ArtifactCard("a", "b", "c"', "unbalanced_parens"),
        ('hook = BeatCard("unterminated)', "unbalanced_quotes"),
        ('hook = BeatCard("never closes", "x)', "unbalanced_quotes"),
    ],
)
def test_unbalanced_delimiters_are_rejected(text, code):
    report = validate_dsl([dsl(text)])

    assert report.ok is False
    assert [e.code for e in report.errors] == [code]


# ---------------------------------------------------------------------------
# 5. HTML / script injection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        'root = ScenePane("<script>alert(1)</script>", [])',
        'hook = BeatCard("<img src=x onerror=alert(1)>")',
        'hook = BeatCard("hello <b>bold</b>")',
        'hook = BeatCard("<div class=\'x\'>y</div>")',
        'hook = BeatCard("click javascript:alert(1)")',
        'card = ArtifactCard("a", "b", "<!-- comment -->")',
        'card = ArtifactCard("a", "b", "<iframe src=//evil>")',
    ],
)
def test_html_or_script_content_is_rejected(text):
    report = validate_dsl([dsl(text)])

    assert report.ok is False
    assert [e.code for e in report.errors] == ["html_content"]
    assert "HTML" in report.errors[0].message


def test_plain_less_than_is_not_html():
    # ``<`` without a tag shape must not trip the HTML gate.
    report = validate_dsl([dsl('hook = BeatCard("health < half and mana > 0")')])
    assert report.ok is True, report.errors


# ---------------------------------------------------------------------------
# 6. mixed valid + invalid keeps the valid segments
# ---------------------------------------------------------------------------


def test_mixed_payload_keeps_valid_and_failed_segments():
    segments = [
        prose("Before."),
        dsl('card = ArtifactCard("Anxious", "Tavern", "Evening")'),  # valid
        dsl('bad = FancyWidget("x")'),  # unknown
        prose("Between."),
        dsl('hook = BeatCard("kept")'),  # valid
        dsl('short = MeterBar("T", 1)'),  # arity
    ]
    report = validate_dsl(segments)

    assert report.ok is False
    assert [e.code for e in report.errors] == ["unknown_component", "arity_mismatch"]
    # Every segment is kept in document order — the two failing statements are
    # kept verbatim as dsl segments (keep + tag), never replaced with a marker.
    assert report.segments == [
        prose("Before."),
        segments[1],
        segments[2],
        prose("Between."),
        segments[4],
        segments[5],
    ]


def test_malformed_segment_text_is_rejected():
    report = validate_dsl([dsl("this is not a component call")])
    assert [e.code for e in report.errors] == ["malformed_statement"]


def test_empty_dsl_segment_is_rejected():
    report = validate_dsl([dsl("   ")])
    assert [e.code for e in report.errors] == ["empty_statement"]


# ---------------------------------------------------------------------------
# 7. retry seam
# ---------------------------------------------------------------------------


def test_retry_not_applied_when_already_valid():
    calls: list[list[DslError]] = []

    async def resynthesize(errors):
        calls.append(errors)
        return []

    report = run(revalidate_with_retry(VALID_PAYLOAD, resynthesize))

    assert report.ok is True
    assert report.retry_applied is False
    assert calls == []


def test_retry_success_replaces_payload_and_reports_ok():
    bad = [prose("Intro."), dsl('bad = FancyWidget("x")')]
    fixed = [prose("Intro."), dsl('hook = BeatCard("recovered")')]
    seen: list[list[DslError]] = []

    async def resynthesize(errors):
        seen.append(list(errors))
        return fixed

    report = run(revalidate_with_retry(bad, resynthesize))

    assert report.ok is True
    assert report.retry_applied is True
    assert report.errors == []
    assert report.segments == fixed
    # The seam receives the original errors.
    assert [e.code for e in seen[0]] == ["unknown_component"]


def test_retry_still_bad_reports_failure_and_keeps_the_statement():
    bad = [dsl('bad = FancyWidget("x")')]

    async def resynthesize(errors):  # noqa: ARG001 - fake ignores the errors
        return [dsl('worse = FancyWidget("still bad")')]

    report = run(revalidate_with_retry(bad, resynthesize))

    assert report.ok is False
    assert report.retry_applied is True
    assert [e.code for e in report.errors] == ["unknown_component"]
    assert report.segments == [dsl('worse = FancyWidget("still bad")')]


def test_retry_can_fix_only_some_segments():
    bad = [dsl('a = FancyWidget("x")'), dsl('b = AlsoBad("y")')]

    async def resynthesize(errors):
        return [dsl('hook = BeatCard("ok")'), dsl('still = AlsoBad("y")')]

    report = run(revalidate_with_retry(bad, resynthesize))

    assert report.ok is False
    assert report.retry_applied is True
    assert report.segments == [dsl('hook = BeatCard("ok")'), dsl('still = AlsoBad("y")')]


def test_retry_discards_a_replacement_that_moves_the_dsl_slots():
    """A seam that re-lays-out the payload is refused; the draft stands.

    The replacement here repairs the statement but moves the panel to the top of
    the payload — the live 54d73764 shape (panels re-ordered / stacked). The DSL
    ordinals must match the draft's, so the replacement is discarded rather than
    committed with a different layout.
    """
    bad = [prose("Intro."), dsl('bad = FancyWidget("x")')]
    moved = [dsl('hook = BeatCard("recovered")'), prose("Intro.")]

    async def resynthesize(errors):  # noqa: ARG001 - fake ignores the errors
        return moved

    report = run(revalidate_with_retry(bad, resynthesize))

    assert report.ok is False
    assert report.retry_applied is True
    assert report.resynth_discarded is True
    assert report.segments == bad
    assert [e.code for e in report.errors] == ["unknown_component"]


def test_retry_discards_a_replacement_that_changes_nothing():
    """An unchanged replacement repaired nothing — reported as discarded."""
    bad = [dsl('bad = FancyWidget("x")')]

    async def resynthesize(errors):  # noqa: ARG001 - fake echoes the draft
        return [dsl('bad = FancyWidget("x")')]

    report = run(revalidate_with_retry(bad, resynthesize))

    assert report.retry_applied is True
    assert report.resynth_discarded is True
    assert report.segments == bad


# ---------------------------------------------------------------------------
# flavor schema: lazy, cached, JSON-driven
# ---------------------------------------------------------------------------


def test_resolves_core_four_when_no_flavor_file():
    reset_component_specs_cache()
    assert known_component_names() == CORE_NAMES


def test_the_gate_reads_the_live_spec_table_without_a_cache_of_its_own():
    """W9.27 C3: no second cache — the gate IS ``spec.spec_table()``.

    A gate-local cache shadowed the component store's hot reload (Wave A's
    smoke test needed ``reset_component_specs_cache()`` to see a swap).
    """
    reset_component_specs_cache()
    assert component_specs() is dsl_spec.spec_table()


def _write_component_set(directory, components, *, set_id):
    """A self-contained component set directory (spec + doc + ``set.json``)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / dsl_store.SPEC_FILENAME).write_text(
        json.dumps({"root": "ScenePane", "components": components}), encoding="utf-8"
    )
    (directory / dsl_store.DOC_FILENAME).write_text(
        f"{set_id} component reference.\n", encoding="utf-8"
    )
    (directory / dsl_store.MANIFEST_FILENAME).write_text(
        json.dumps({"id": set_id, "name": set_id.title(), "components": list(components)}),
        encoding="utf-8",
    )
    return directory


def test_a_selected_component_set_reaches_the_gate_without_a_restart(monkeypatch, tmp_path):
    """#16/#24: the session's selected set reaches the gate with no reset.

    The selection door appends the ``set_swapped`` fact; the turn boundary
    resolves it (:func:`fabulist.dsl.sets.session_snapshot`). From then on the
    SAME process validates against the new vocabulary — no
    ``reset_component_specs_cache()``, no engine restart.
    """
    from fabulist.dsl import sets as dsl_sets
    from fabulist.log import EventLog
    from fabulist.log.schema_gate import SET_SWAP_EVENT

    monkeypatch.delenv(dsl_spec.SPEC_ENV_VAR, raising=False)
    monkeypatch.delenv(dsl_store.ARTIFACT_DIR_ENV_VAR, raising=False)
    label = {"props": [{"name": "label", "type": "string"}]}
    alpha = _write_component_set(tmp_path / "alpha", {"Widget": label}, set_id="alpha")
    beta = _write_component_set(tmp_path / "beta", {"Gadget": label}, set_id="beta")

    store = dsl_store.ComponentLibraryStore(default_dir=alpha)
    monkeypatch.setattr(dsl_store, "_STORE", store)  # the process-wide seam
    reset_component_specs_cache()

    sets_root = tmp_path / "sets"
    sets_root.mkdir()
    (sets_root / "index.json").write_text(
        json.dumps(
            {
                "default_set": "alpha",
                "sets": [
                    {"id": "alpha", "name": "Alpha", "version": "1.0.0", "dir": str(alpha)},
                    {"id": "beta", "name": "Beta", "version": "1.0.0", "dir": str(beta)},
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(dsl_sets.SETS_DIR_ENV_VAR, str(sets_root))
    dsl_sets.reset_sets_cache()

    gadget = [_seg('panel = Gadget("x")')]
    widget = [_seg('panel = Widget("x")')]
    assert validate_dsl(gadget).ok is False  # alpha does not know Gadget

    log = EventLog(tmp_path / "gate.db")
    try:
        sid = "s"
        log.append_event(sid, "user", "user", {"text": "hi"})
        # The selection is a per-session LOG fact; the gate changes nothing yet.
        snapshot = store.snapshot_for_dir(beta)
        log.append_event(
            sid,
            "system",
            "user",
            {
                "event": SET_SWAP_EVENT,
                "previous_set_id": "alpha",
                "set_id": snapshot.set_id,
                "name": snapshot.name,
                "version": snapshot.version,
                "notice": dsl_sets.swap_notice_text(snapshot),
            },
        )
        assert validate_dsl(gadget).ok is False

        # The turn boundary resolves the session's set; no reset, no restart.
        dsl_sets.session_snapshot(log, sid)
        assert validate_dsl(gadget).ok is True
        assert validate_dsl(widget).ok is False
    finally:
        log.close()


def test_spec_table_is_cached_across_calls():
    first = component_specs()
    second = component_specs()
    assert first is second


def test_flavors_json_adds_known_components(monkeypatch, tmp_path):
    flavors = tmp_path / "flavors.json"
    flavors.write_text(
        json.dumps(
            {
                "components": [
                    {"name": "Mindscape", "props": ["text", "mood"]},
                    {"name": "Interface", "props": ["title"]},
                    {"name": "Entrance"},  # name only => arity unchecked
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(validation.FLAVORS_ENV_VAR, str(flavors))
    reset_component_specs_cache()

    names = known_component_names()
    assert CORE_NAMES <= names
    assert {"Mindscape", "Interface", "Entrance"} <= names

    assert validate_dsl([dsl('mind = Mindscape("shadow", "dread")')]).ok is True
    assert [e.code for e in validate_dsl([dsl('mind = Mindscape("only")')]).errors] == [
        "arity_mismatch"
    ]
    assert [e.code for e in validate_dsl([dsl('mind = Nope("x")')]).errors] == [
        "unknown_component"
    ]
    # Name-only flavor: arity is not policed.
    assert validate_dsl([dsl('e = Entrance()')]).ok is True


def test_core_component_wins_flavor_name_collision(monkeypatch, tmp_path):
    flavors = tmp_path / "flavors.json"
    flavors.write_text(
        json.dumps({"components": {"BeatCard": {"props": ["a", "b", "c"]}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv(validation.FLAVORS_ENV_VAR, str(flavors))
    reset_component_specs_cache()

    # Core BeatCard keeps its 1-prop contract.
    assert validate_dsl([dsl('hook = BeatCard("single")')]).ok is True
    assert [e.code for e in validate_dsl([dsl('hook = BeatCard("a", "b", "c")')]).errors] == [
        "arity_mismatch"
    ]


def test_malformed_flavor_file_falls_back_to_core(monkeypatch, tmp_path):
    flavors = tmp_path / "flavors.json"
    flavors.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setenv(validation.FLAVORS_ENV_VAR, str(flavors))
    reset_component_specs_cache()

    assert known_component_names() == CORE_NAMES


def test_flavor_arity_from_arity_field(monkeypatch, tmp_path):
    flavors = tmp_path / "flavors.json"
    flavors.write_text(
        json.dumps({"components": [{"name": "Clash", "arity": [2, 4]}]}),
        encoding="utf-8",
    )
    monkeypatch.setenv(validation.FLAVORS_ENV_VAR, str(flavors))
    reset_component_specs_cache()

    assert validate_dsl([dsl('c = Clash("a", "b")')]).ok is True
    assert validate_dsl([dsl('c = Clash("a", "b", "c", "d")')]).ok is True
    assert [e.code for e in validate_dsl([dsl('c = Clash("a")')]).errors] == ["arity_mismatch"]
    assert [e.code for e in validate_dsl([dsl('c = Clash("a","b","c","d","e")')]).errors] == [
        "arity_mismatch"
    ]


def test_flavor_optional_props_widen_arity_range(monkeypatch, tmp_path):
    # Mirrors the generated mirror's shape: a `flavors` list whose entries carry
    # `component` + `props` with `required` flags. Optional props widen the range.
    flavors = tmp_path / "flavors.json"
    flavors.write_text(
        json.dumps(
            {
                "flavors": [
                    {
                        "component": "Document",
                        "props": [
                            {"name": "docType", "required": True},
                            {"name": "title", "required": True},
                            {"name": "body", "required": True},
                            {"name": "signature", "required": False},
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(validation.FLAVORS_ENV_VAR, str(flavors))
    reset_component_specs_cache()

    assert validate_dsl([dsl('d = Document("letter", "Subject", "Body text")')]).ok is True
    assert (
        validate_dsl([dsl('d = Document("letter", "Subject", "Body", "— signed")')]).ok
        is True
    )
    assert [
        e.code for e in validate_dsl([dsl('d = Document("letter", "Subject")')]).errors
    ] == ["arity_mismatch"]


#: The ten VTK flavors that survive the Wave 2a cull.
ARTIFACT_NAMES = {"Custom", "Phone", "SMS", "Monitor", "PostIt", "SocialPost"}
FLAVOR_NAMES = {
    "Mindscape",
    "Interface",
    "Document",
    "Artifact",
    "DialogueSpotlight",
    "ReactionSpotlight",
    "FailureAchievement",
    "Memory",
    "Entrance",
    "Clash",
}


def test_real_flavors_json_mirror_is_parsed(monkeypatch):
    """When the generated mirror exists, all ten flavors are known."""
    # Ignore the hermetic default path so the real repo file is consulted.
    monkeypatch.delenv(validation.FLAVORS_ENV_VAR, raising=False)
    path = validation.flavors_path()
    if not path.is_file():
        pytest.skip("generated flavors.json not present in this checkout")

    reset_component_specs_cache()
    names = known_component_names()
    assert CORE_NAMES <= names
    assert FLAVOR_NAMES <= names
    # Exactly the four core + ten flavors, nothing more.
    assert names == CORE_NAMES | FLAVOR_NAMES | ARTIFACT_NAMES


def test_culled_component_names_fail_gracefully_not_crash(monkeypatch):
    """Wave 2a cull: statements for removed components (old event logs) are
    unknown to the gate, so they are flagged and kept verbatim (never a crash),
    while the root's reference to them is pruned as dangling."""
    monkeypatch.delenv(validation.FLAVORS_ENV_VAR, raising=False)
    if not validation.flavors_path().is_file():
        pytest.skip("generated flavors.json not present in this checkout")
    reset_component_specs_cache()

    report = validate_dsl(
        [
            _seg('sub = Subtext("just a plugin", "scared", "ask")'),
            _seg('beat = BeatCard("kept")'),
            _seg('root = ScenePane("Old log turn", [sub, beat])'),
        ]
    )
    assert [e.code for e in report.errors] == ["unknown_component", "dangling_ref"]
    # The unknown statement is kept verbatim; root's ref to it is still pruned.
    assert report.segments[0] == _seg('sub = Subtext("just a plugin", "scared", "ask")')
    assert report.segments[2]["text"] == 'root = ScenePane("Old log turn", [beat])'


def test_flavor_arity_checks_cover_the_mirror(monkeypatch):
    """The gate enforces arity for the flavors from the mirror."""
    monkeypatch.delenv(validation.FLAVORS_ENV_VAR, raising=False)
    if not validation.flavors_path().is_file():
        pytest.skip("generated flavors.json not present in this checkout")
    reset_component_specs_cache()

    # 3-arity exact.
    assert validate_dsl([dsl('e = Entrance("Voss", "enforcer", "a coat of knife-scars")')]).ok
    assert [e.code for e in validate_dsl([dsl('e = Entrance("Voss")')]).errors] == [
        "arity_mismatch"
    ]
    # 2-arity exact.
    assert validate_dsl([dsl('r = ReactionSpotlight("😬", "too loud")')]).ok
    assert [e.code for e in validate_dsl([dsl('r = ReactionSpotlight("😬")')]).errors] == [
        "arity_mismatch"
    ]
    # Optional widens the range: Document(docType, title, body, signature?) = 3..4.
    assert validate_dsl([dsl('d = Document("letter", "Subject", "Body text")')]).ok
    assert validate_dsl(
        [dsl('d = Document("letter", "Subject", "Body text", "— signed")')]
    ).ok
    assert [e.code for e in validate_dsl([dsl('d = Document("letter", "Subject")')]).errors] == [
        "arity_mismatch"
    ]


def test_validation_and_library_doc_flavor_sets_are_in_parity(monkeypatch):
    """Every consumer resolves the SAME ten flavors (three-consumer parity)."""
    monkeypatch.delenv(validation.FLAVORS_ENV_VAR, raising=False)
    if not validation.flavors_path().is_file():
        pytest.skip("generated flavors.json not present in this checkout")
    reset_component_specs_cache()

    from fabulist.prompts.library_mini import _load_flavors

    mirror_names = {f["component"] for f in _load_flavors()}
    assert mirror_names == FLAVOR_NAMES | ARTIFACT_NAMES
    # `flavor_names()` is the full resolved table (core + flavors, sorted).
    assert set(validation.flavor_names()) == CORE_NAMES | FLAVOR_NAMES | ARTIFACT_NAMES
    assert validation.known_component_names() == CORE_NAMES | mirror_names


def test_report_dataclass_shape():
    report = DslValidationReport(ok=True)
    assert report.errors == []
    assert report.segments == []
    assert report.retry_applied is False


# --- dangling-ref pruning (live defect: phantom children break the render) -------

def _seg(text: str) -> dict:
    return {"kind": "dsl", "text": text}


def test_dangling_refs_pruned_from_scenepane():
    segments = [
        _seg('card = ArtifactCard("Tense", "Study", "08:30")'),
        _seg('root = ScenePane("Morning", [card, cat_mind, circle_note])'),
    ]
    report = validate_dsl(segments)
    # The refs are dropped deterministically (repaired), so the composition is
    # valid — but the drop is EXPLICIT (an advisory flag), never silent.
    assert report.ok is True
    assert report.needs_retry is False
    codes = [e.code for e in report.errors]
    assert codes == ["dangling_ref"]
    assert report.errors[0].repaired is True
    assert "cat_mind, circle_note" in report.errors[0].message
    kept = report.segments[1]["text"]
    assert "card" in kept and "cat_mind" not in kept and "circle_note" not in kept


def test_fully_declared_children_untouched():
    segments = [
        _seg('card = ArtifactCard("Tense", "Study", "08:30")'),
        _seg('beat = BeatCard("the cat watches.")'),
        _seg('root = ScenePane("Morning", [card, beat])'),
    ]
    report = validate_dsl(segments)
    assert report.ok
    assert report.segments[2]["text"].endswith("[card, beat])")


def test_flavor_string_lists_never_rewritten(tmp_path, monkeypatch):
    # Mindscape thoughts are strings, not identifiers: no pruning may touch
    # them. Hermetic: declare the flavor via a temp flavors file (the suite's
    # autouse fixture points the env at an absent file).
    flavors = tmp_path / "flavors.json"
    flavors.write_text(
        json.dumps(
            {
                "components": [
                    {
                        "name": "Mindscape",
                        "props": [
                            {"name": "title", "type": "string"},
                            {"name": "thoughts", "type": "string[]"},
                            {"name": "mood", "type": "string"},
                        ],
                    }
                ]
            }
        )
    )
    monkeypatch.setenv(validation.FLAVORS_ENV_VAR, str(flavors))
    validation.reset_component_specs_cache()
    segments = [
        _seg('mind = Mindscape("Judgment", ["disdain", "hunger"], "smug")'),
        _seg('root = ScenePane("Morning", [mind])'),
    ]
    report = validate_dsl(segments)
    assert report.ok
    assert '"disdain", "hunger"' in report.segments[0]["text"]


def test_invalid_statement_does_not_declare_ident():
    segments = [
        _seg('bad = NotAComponent("x")'),  # unknown component -> failed
        _seg('root = ScenePane("Morning", [bad])'),
    ]
    report = validate_dsl(segments)
    # bad was never validly declared, so root's ONLY child is a dangling ref:
    # the prune is REFUSED (issue #30) and the root is a hard failure, not a
    # committed hollow pane.
    assert [e.code for e in report.errors] == ["unknown_component", "empty_after_prune"]
    assert report.ok is False
    assert report.segments[1]["text"] == 'root = ScenePane("Morning", [bad])'


# ===========================================================================
# Defect regressions + deterministic repair (owner directive: "All valid
# defects. fix"; specs from the live sol-romance / dnd-guild RP runs).
# ===========================================================================
@pytest.fixture
def real_spec(monkeypatch):
    """Point the gate at the repo's real generated spec (skip if absent)."""
    monkeypatch.delenv(dsl_spec.SPEC_ENV_VAR, raising=False)
    monkeypatch.delenv(dsl_spec.FLAVORS_ENV_VAR, raising=False)
    validation.reset_component_specs_cache()
    if not dsl_spec.spec_path().is_file():
        pytest.skip("generated library.spec.json not present in this checkout")
    yield
    validation.reset_component_specs_cache()


# --- defect 4: blended card shape (type gate) -------------------------------


def test_artifact_card_shape_on_artifact_name_is_type_mismatched(real_spec):
    """Live defect (Wave 3A epoch): an ArtifactCard-shaped call on the flavor
    name ``Artifact`` degraded silently — the trailing options object counted as
    a positional and the raw args were never shape-checked. The gate now flags
    the array-vs-string and object-vs-string[] contradictions."""
    text = (
        'epoch = Artifact("The Epoch 27", "Vessel", '
        '"A decommissioned exploration ship now acting as a permanent, half-buried landmark", '
        '["Material: Non-reflective alloy", "State: Inert/Leaking", "Vibe: Wrong"], '
        '{glyph: "🛸"})'
    )
    report = validate_dsl([_seg(text)])

    assert report.ok is False
    assert report.needs_retry is True
    assert [e.code for e in report.errors] == ["type_mismatch"]
    assert report.errors[0].repaired is False
    assert report.segments == [_seg(text)]


def test_type_mismatch_blend_options_object_on_non_options_prop():
    """The options object parked before the trailing slot is the blend signature
    even when the earlier shapes happen to line up."""
    # ArtifactCard wants (name, kind, description, options?); the object lands on
    # `description` (3rd positional), not the trailing options slot.
    text = 'card = ArtifactCard("n", "k", {glyph: "🃏"})'
    report = validate_dsl([_seg(text)])

    assert report.ok is False
    assert [e.code for e in report.errors] == ["type_mismatch"]
    assert "options object" in report.errors[0].message


def test_type_gate_accepts_valid_core_and_flavor_shapes():
    assert validate_dsl([dsl('t = MeterBar("Tension", 68, 100)')]).ok is True
    assert (
        validate_dsl(
            [
                dsl('card = ArtifactCard("n", "k", "d", {glyph: "🗝️"})'),
                dsl('root = ScenePane("Rusty Anchor", [card], {glyph: "⚓"})'),
            ]
        ).ok
        is True
    )
    assert validate_dsl([dsl('card = ArtifactCard("n", "k", "d", {glyph: "🗝️"})')]).ok is True
    # Bare child refs and expressions are never shape-policed (declared here, so
    # the root is a COMPLETE composition — an undeclared-only child list is a
    # hard failure now, see the empty-after-prune tests).
    assert (
        validate_dsl(
            [
                dsl('a = BeatCard("one")'),
                dsl('b = BeatCard("two")'),
                dsl('root = ScenePane("T", [a, b])'),
            ]
        ).ok
        is True
    )


# --- deep gate: the engine sees what the frontend zod parser sees -----------
#
# The coarse gate collapsed EVERY ``X[]`` prop to the shape "array", so PostIt's
# ``notes`` (``{text}`` objects) and Interface's ``lines`` (strings) were
# indistinguishable: an array of strings parked on ``notes`` passed the engine
# clean while the frontend parser rejected it. The gate now validates each
# positional literal against the component's emitted JSON Schema.

#: The EXACT committed statement that used to slip through (strings where the
#: schema wants ``{text}`` objects).
POSTIT_STRING_NOTES = (
    'note = PostIt(["Avoid the service lift. They\'re scrubbing the logs."], '
    '{"glyph": "📝"})'
)

#: A Phone whose messages omit the required ``time``/``status`` keys.
PHONE_MISSING_TIME = 'ph = Phone("Voss", [{from: "Voss", text: "oi"}])'


def test_postit_string_notes_are_flagged_and_kept(real_spec):
    """The PostIt regression: deep shape mismatch, kept verbatim, tagged."""
    report = validate_dsl([_seg(POSTIT_STRING_NOTES)])

    assert report.ok is False
    assert report.needs_retry is True
    assert [e.code for e in report.errors] == ["type_mismatch"]
    # Diagnostic names component, argument, expected and received shape.
    assert report.errors[0].message == (
        "PostIt arg 1 (notes) expects object[] of {text}, got a string element"
    )
    # Keep + tag: the offending statement is committed verbatim (nothing dropped).
    assert report.segments == [_seg(POSTIT_STRING_NOTES)]
    # NO silent path: the failure yields exactly one warn flag anchored to the
    # segment (so the chat message carries the "panel failed validation" tag).
    flags = dsl_error_flags(report.errors)
    assert len(flags) == 1
    assert flags[0]["severity"] == "warn"
    assert flags[0]["segment_index"] == 0
    assert "PostIt" in flags[0]["message"]


def test_postit_object_notes_pass_the_deep_gate(real_spec):
    text = 'note = PostIt([{text: "Avoid the service lift."}], {glyph: "📝"})'
    assert validate_dsl([_seg(text)]).ok is True


def test_phone_message_missing_required_keys_is_flagged(real_spec):
    report = validate_dsl([_seg(PHONE_MISSING_TIME)])

    assert report.ok is False
    message = report.errors[0].message
    assert message.startswith("Phone arg 2 (messages) expects object[] of {")
    assert "missing required key 'time'" in message
    # Kept verbatim + an anchored warn flag.
    assert report.segments == [_seg(PHONE_MISSING_TIME)]
    flags = dsl_error_flags(report.errors)
    assert flags[0]["severity"] == "warn"
    assert flags[0]["segment_index"] == 0


def test_every_gate_failure_yields_a_flag(real_spec):
    """NO silent path: every unrepaired finding maps to a warn flag carrying the
    segment index (criterion: no gate failure may disappear)."""
    segments = [
        _seg(POSTIT_STRING_NOTES),
        _seg(PHONE_MISSING_TIME),
        _seg('bad = Nope("x")'),
    ]
    report = validate_dsl(segments)
    assert report.ok is False
    flags = dsl_error_flags(report.errors)
    failures = [e for e in report.errors if not e.repaired]
    assert failures  # sanity
    for error in failures:
        assert any(
            f["rule_id"] == error.code
            and f["segment_index"] == error.segment_index
            and f["severity"] == "warn"
            for f in flags
        ), error
    assert {f["segment_index"] for f in flags} == {0, 1, 2}


# --- defect 3: kwargs blindness ---------------------------------------------


def test_kwargs_core_statement_repaired_to_positional():
    """A kwargs core statement (ArtifactCard) must render, not fail."""
    text = 'card = ArtifactCard(name="Exhausted", kind="Streaming Room", description="08:45")'
    report = validate_dsl([_seg(text)])

    assert report.ok is True
    assert report.needs_retry is False
    assert [e.code for e in report.errors] == ["named_args"]
    assert report.errors[0].repaired is True
    assert report.segments[0]["text"] == (
        'card = ArtifactCard("Exhausted", "Streaming Room", "08:45")'
    )


def test_kwargs_dialoguespotlight_repaired(real_spec):
    text = 'line = DialogueSpotlight(speaker="Tessa", line="I got the dip.", tell="flat")'
    report = validate_dsl([_seg(text)])

    assert report.ok is True
    assert report.segments[0]["text"] == (
        'line = DialogueSpotlight("Tessa", "I got the dip.", "flat")'
    )


def test_kwargs_with_unknown_prop_fails_and_flags():
    report = validate_dsl([_seg('card = ArtifactCard(name="x", typo="y", kind="z")')])

    assert report.ok is False
    assert report.needs_retry is True
    assert [e.code for e in report.errors] == ["named_args"]
    assert report.errors[0].repaired is False
    assert report.segments == [_seg('card = ArtifactCard(name="x", typo="y", kind="z")')]


# --- defect 2: silent prune-to-empty ----------------------------------------


def test_sol_t2_failed_interface_declaration_keeps_its_root_child(real_spec):
    """sol T2: root references a kwargs `status`, a failed `interface` (HTML),
    and a kwargs `subtext`. The kwargs children are repaired positionally and the
    failed `interface` statement is kept verbatim — because its text still
    DECLARES `interface`, the root's ref to it is not dangling and is kept."""
    segments = [
        _seg('card = ArtifactCard(name="Exhausted", kind="Room", description="08:45")'),
        _seg('line = DialogueSpotlight(speaker="just a plugin", line="scared", tell="ask")'),
        _seg('interface = Interface(mode="Chat Log", title="x", lines=["<User123> hi"])'),
        _seg('root = ScenePane("The Neon Bunker", [card, interface, line])'),
    ]
    report = validate_dsl(segments)

    codes = [e.code for e in report.errors]
    assert codes == ["named_args", "named_args", "html_content"]
    root = report.segments[3]["text"]
    assert root == 'root = ScenePane("The Neon Bunker", [card, interface, line])'
    # The kwargs children were repaired positionally before being kept.
    assert report.segments[0]["text"] == (
        'card = ArtifactCard("Exhausted", "Room", "08:45")'
    )
    assert (
        'DialogueSpotlight("just a plugin", "scared", "ask")'
        in report.segments[1]["text"]
    )
    # The failed statement is kept verbatim, and its ref survives the prune.
    assert report.segments[2] == segments[2]


def test_sol_t4_mixed_child_list_bare_refs_dropped(real_spec):
    """sol T4: `[ArtifactCard(...), Interface, DialogueSpotlight]` — the old regex
    never matched mixed lists, so bare phantom refs reached the renderer."""
    segments = [
        _seg(
            'root = ScenePane("Kick Chat", '
            '[ArtifactCard("Frozen", "Kitchen", "08:45"), Interface, DialogueSpotlight])'
        ),
    ]
    report = validate_dsl(segments)
    root = report.segments[0]["text"]
    assert 'ArtifactCard("Frozen", "Kitchen", "08:45")' in root
    assert "Interface" not in root
    assert "DialogueSpotlight" not in root
    assert [e.code for e in report.errors] == ["dangling_ref"]


# --- issue #30: the prune may never empty the root into a hollow pane -------

#: The EXACT composition one live narration produced: the root's only child is an
#: identifier no segment declares, so the prune emptied the pane and the turn
#: committed `root = ScenePane("…", [])` as a successful panel.
OBSERVED_HOLLOW_ROOT = 'root = ScenePane("Kitchen — house business, whispered", [chline])'


def test_all_dangling_refs_is_a_hard_failure_not_a_hollow_pane():
    """Issue #30: 'every child was undeclared' is a WRONG COMPOSITION (repair).

    It is not a tidy-up: pruning the last child leaves `ScenePane(title, [])`,
    an empty panel that shipped as a successful turn. The gate now refuses the
    prune, keeps the statement as authored (nothing is dropped) and files an
    UNREPAIRED finding, so `ok` is false and the turn takes a repair round.
    """
    report = validate_dsl([prose("The kitchen narrows."), _seg(OBSERVED_HOLLOW_ROOT)])

    assert report.ok is False
    assert report.needs_retry is True
    assert [e.code for e in report.errors] == ["empty_after_prune"]
    finding = report.errors[0]
    assert finding.repaired is False
    assert finding.segment_index == 1
    # The undeclared ref is named, and the message says what to do instead.
    assert "chline" in finding.message
    assert "refusing to prune" in finding.message
    # THE PIN: the payload carries no hollow pane — and no silent drop either.
    assert report.segments[1]["text"] == OBSERVED_HOLLOW_ROOT
    assert "[]" not in report.segments[1]["text"]


def test_hard_empty_finding_survives_revalidation_of_the_committed_channel():
    """The finding must re-derive from the text, not only from one report.

    The repair loop and the post-commit validate node re-read the CHANNEL
    (`validate_dsl(segments)`), so a finding that stopped re-deriving would
    disappear from the committed record. Keeping the authored statement is what
    keeps it observable: the undeclared ref is still there to be found.
    """
    first = validate_dsl([_seg(OBSERVED_HOLLOW_ROOT)])
    again = validate_dsl(first.segments)

    assert [e.code for e in again.errors] == ["empty_after_prune"]
    assert again.errors[0].repaired is False
    assert again.ok is False


def test_hard_empty_finding_maps_to_a_warn_flag():
    """Budget spent: the turn keeps the finding as a WARN flag (never silent)."""
    report = validate_dsl([_seg(OBSERVED_HOLLOW_ROOT)])
    flags = dsl_error_flags(report.errors)

    assert [f["severity"] for f in flags] == ["warn"]
    assert flags[0]["rule_id"] == "empty_after_prune"
    assert flags[0]["check"] == "dsl_validation"
    assert flags[0]["segment_index"] == 0
    assert "chline" in flags[0]["evidence"]


def test_observed_case_bounces_into_a_repair_round():
    """The observed case calls the re-synthesis seam instead of committing.

    One repair round, position-preserving: the fence declares a child and the
    root composes from it, so the retried turn is clean and the hollow pane is
    gone.
    """
    segments = [prose("The kitchen narrows."), _seg(OBSERVED_HOLLOW_ROOT)]
    seen: list[list[DslError]] = []

    async def resynthesize(errors):
        seen.append(list(errors))
        return [
            prose("The kitchen narrows."),
            _seg(
                'chline = BeatCard("House business, whispered.")\n'
                'root = ScenePane("Kitchen — house business, whispered", [chline])'
            ),
        ]

    report = run(revalidate_with_retry(segments, resynthesize))

    assert len(seen) == 1  # exactly one bounce, never a silent commit
    assert [e.code for e in seen[0]] == ["empty_after_prune"]
    assert report.retry_applied is True
    assert report.ok is True
    assert report.errors == []
    assert "chline = BeatCard(" in report.segments[1]["text"]
    assert "[]" not in report.segments[1]["text"]


def test_single_dangling_ref_among_healthy_children_still_repairs_at_info():
    """THE OTHER PIN: a partial prune is unchanged — an `info` repair, no bounce."""
    segments = [
        _seg('card = ArtifactCard("Tense", "Study", "08:30")'),
        _seg('root = ScenePane("Kitchen", [card, chline])'),
    ]
    report = validate_dsl(segments)

    assert report.ok is True
    assert report.needs_retry is False
    assert [e.code for e in report.errors] == ["dangling_ref"]
    assert report.errors[0].repaired is True
    assert "chline" in report.errors[0].message
    assert report.segments[1]["text"] == 'root = ScenePane("Kitchen", [card])'
    assert [f["severity"] for f in dsl_error_flags(report.errors)] == ["info"]


def test_partial_and_emptying_prunes_in_one_reply_stay_separate():
    """Only the pane that would empty is a failure; the healthy prune still lands."""
    segments = [
        _seg('card = ArtifactCard("Tense", "Study", "08:30")'),
        _seg('root = ScenePane("Kitchen", [card, ghost])'),
        _seg('aside = ScenePane("Aside", [phantom])'),
    ]
    report = validate_dsl(segments)

    assert report.ok is False
    assert [(e.segment_index, e.code, e.repaired) for e in report.errors] == [
        (1, "dangling_ref", True),
        (2, "empty_after_prune", False),
    ]
    assert report.segments[1]["text"] == 'root = ScenePane("Kitchen", [card])'
    assert report.segments[2]["text"] == 'aside = ScenePane("Aside", [phantom])'


def test_declared_children_are_never_pruned_at_all():
    """Control: a complete composition takes no finding and no rewrite."""
    segments = [
        _seg('card = ArtifactCard("Tense", "Study", "08:30")'),
        _seg('root = ScenePane("Kitchen", [card])'),
    ]
    report = validate_dsl(segments)
    assert report.ok is True
    assert report.errors == []
    assert report.segments == segments


def test_failed_declaration_still_declares_its_ident():
    """A failed-but-DECLARED child ref is NOT dangling (live).

    dnd T15 / SPIKE 4.3 shipped the opposite rule and it corrupted the committed
    composition: `art` failed arity, root's ref to it was pruned, and the prune
    outlived the narrator's in-place repair — `root = ScenePane("Loot", [])` was
    committed with nothing to restore the child. A ref is dangling only when NO
    dsl segment declares the identifier, so a failed statement's own ident keeps
    its child in place; the arity error is still reported.
    """
    segments = [
        _seg('art = ArtifactCard("only", "two")'),  # arity failure
        _seg('root = ScenePane("Loot", [art])'),
    ]
    report = validate_dsl(segments)
    assert [e.code for e in report.errors] == ["arity_mismatch"]
    # Keep-verbatim on the failed segment; the root keeps its child.
    assert report.segments[0] == segments[0]
    assert report.segments[1]["text"] == 'root = ScenePane("Loot", [art])'


def test_unparsable_failed_segment_declares_nothing():
    """The unchanged fallback: a failed segment whose text yields NO parsable
    statement declares no identifier, so a ref to it is still dangling — and,
    because it was the root's ONLY child, the prune is refused (hard failure)."""
    segments = [
        _seg('art = ArtifactCard("only", "two"'),  # unbalanced → no statement
        _seg('root = ScenePane("Loot", [art])'),
    ]
    report = validate_dsl(segments)
    assert [e.code for e in report.errors] == [
        "unbalanced_parens",
        "empty_after_prune",
    ]
    assert report.ok is False
    assert report.segments[1]["text"] == 'root = ScenePane("Loot", [art])'


def test_failed_entrance_declaration_keeps_its_scenepane_child(real_spec):
    """Live (seq 247/248): a bad-arity `Entrance` still declares
    `entrance_1`, so the later root's child ref must survive Phase B."""
    segments = [
        prose("The Glade opens onto a rust-streaked hull."),
        _seg(
            'entrance_1 = Entrance("Epoch 27", "The Vessel", "A rust-streaked ship.",'
            ' "extra", {glyph: "🚀"})'
        ),
        prose("It settles into the treeline."),
        _seg('root = ScenePane("T", [entrance_1])'),
    ]
    report = validate_dsl(segments)

    assert [e.code for e in report.errors] == ["arity_mismatch"]
    assert report.errors[0].segment_index == 1
    assert report.ok is False
    # The failed statement is kept verbatim and the root is left unpruned.
    assert report.segments[1] == segments[1]
    assert report.segments[3]["text"] == 'root = ScenePane("T", [entrance_1])'


# --- defect 1: dead repair / zero model calls for known classes --------------


def test_deterministic_repair_never_calls_model():
    calls: list[list[DslError]] = []

    async def resynthesize(errors):
        calls.append(list(errors))
        return []

    segments = [
        _seg('card = ArtifactCard(name="Tense", kind="Study", description="08:30")'),
        _seg('root = ScenePane("Morning", [card, ghost])'),
    ]
    report = run(revalidate_with_retry(segments, resynthesize))

    assert report.ok is True
    assert report.retry_applied is False
    assert calls == []
    assert report.segments[0]["text"] == (
        'card = ArtifactCard("Tense", "Study", "08:30")'
    )
    assert report.segments[1]["text"] == 'root = ScenePane("Morning", [card])'


def test_retry_still_runs_for_unrepairable():
    seen: list[list[DslError]] = []

    async def resynthesize(errors):
        seen.append(list(errors))
        return [dsl('hook = BeatCard("recovered")')]

    report = run(revalidate_with_retry([dsl('bad = Nope("x")')], resynthesize))

    assert report.retry_applied is True
    assert report.ok is True
    assert [e.code for e in seen[0]] == ["unknown_component"]


# --- defect 4: dsl findings expose inspector-visible flags -------------------


def test_dsl_error_flags_shape_matches_validator_flag():
    report = validate_dsl(
        [
            _seg('card = ArtifactCard(name="x", kind="y", description="z")'),
            _seg('bad = Nope("x")'),
        ]
    )
    flags = dsl_error_flags([dataclasses.asdict(e) for e in report.errors])

    assert [f["rule_id"] for f in flags] == ["named_args", "unknown_component"]
    assert all(f["check"] == "dsl_validation" for f in flags)
    assert flags[0]["severity"] == "info"  # repaired
    assert flags[1]["severity"] == "warn"  # unrecoverable
    assert flags[0]["evidence"] == 'card = ArtifactCard(name="x", kind="y", description="z")'
    assert {"rule_id", "check", "severity", "evidence", "source", "segment_index", "message"} == set(
        flags[0]
    )


def test_dsl_error_flags_accepts_dataclass_objects():
    flags = dsl_error_flags([DslError(2, "dangling_ref", "dropped: ghost", "root = ...", True)])
    assert flags == [
        {
            "rule_id": "dangling_ref",
            "check": "dsl_validation",
            "severity": "info",
            "evidence": "root = ...",
            "source": "dsl_gate",
            "segment_index": 2,
            "message": "dropped: ghost",
        }
    ]
