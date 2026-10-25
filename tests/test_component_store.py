"""The hot-reloadable component-set store (W9.27 Wave A — the load-bearing pins).

One vocabulary: the generated component artifacts (``library.spec.json``,
``library.doc.txt``, ``set.json``) are DATA the running engine re-reads, so a
regenerated or swapped set takes effect on the NEXT turn with no Python edit and
no restart. These tests pin the seams that make that true:

  * ``reload_if_changed`` detects an artifact edit by ``(mtime, size)`` and bumps
    the version — the version every consumer cache is keyed on;
  * a present-but-corrupt artifact keeps the LAST GOOD snapshot (the generator
    writes tmp+rename, and the store refuses to downgrade silently);
  * ``reload()`` re-resolves ``FABULIST_LIBRARY_ARTIFACT_DIR`` /
    ``FABULIST_DSL_SPEC`` on EVERY call (the ~20 tests that monkeypatch env then
    reset depend on it);
  * a name added to the artifacts is recognized by ``strip_dsl`` +
    ``component_names()`` + the narrate segmenter (``component_stmt_re()``) and
    TAUGHT in the doc on the next turn — the R2 P2-3 acceptance, restated on the
    layer Wave A moves;
  * the store caches CONTENT per set directory (one snapshot shared by every
    session that selects the set) and holds NO global selection: a session's set
    is a log fact, bound here per session id (issues #16/#24);
  * ``require_snapshot`` is the strict load the selection door uses — a set the
    client cannot render, or whose artifacts do not read, is REFUSED with a
    readable reason instead of silently falling back;
  * ``set.json`` is the client-compat marker: a set whose ``components`` are not
    a subset of its own spec's names is refused with a readable reason.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from fabulist.dsl import spec as dsl_spec
from fabulist.dsl import store as dsl_store
from fabulist.dsl.spec import component_names
from fabulist.dsl.statements import (
    component_stmt_re,
    statement_like_re,
    statement_re,
)
from fabulist.dsl.strip import strip_dsl
from fabulist.dsl.store import (
    ARTIFACT_DIR_ENV_VAR,
    FLAVORS_ENV_VAR,
    SPEC_ENV_VAR,
    ComponentLibraryStore,
    ComponentSetError,
    component_names_in,
)
from fabulist.prompts import library_mini
from fabulist.prompts.library_mini import reset_library_artifact_cache


@pytest.fixture(autouse=True)
def _clean_caches(monkeypatch):
    """No ambient env, no warm caches: every test states its own set."""
    for name in (SPEC_ENV_VAR, ARTIFACT_DIR_ENV_VAR, FLAVORS_ENV_VAR):
        monkeypatch.delenv(name, raising=False)
    dsl_spec.reset_spec_cache()
    reset_library_artifact_cache()
    yield
    dsl_spec.reset_spec_cache()
    reset_library_artifact_cache()


def _write_set(
    directory: Path,
    components: dict[str, Any],
    *,
    doc: str = "Test-set component reference.\n",
    manifest: dict[str, Any] | None = None,
) -> Path:
    """A self-contained set directory (spec + doc + optional ``set.json``)."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / dsl_store.SPEC_FILENAME).write_text(
        json.dumps({"root": "ScenePane", "components": components}), encoding="utf-8"
    )
    (directory / dsl_store.DOC_FILENAME).write_text(doc, encoding="utf-8")
    if manifest is not None:
        (directory / dsl_store.MANIFEST_FILENAME).write_text(
            json.dumps(manifest), encoding="utf-8"
        )
    return directory


def _props(*names: str) -> dict[str, list[dict[str, Any]]]:
    return {"props": [{"name": name, "type": "string"} for name in names]}


# --- reload: change detection + version bump ---------------------------------------


def test_unchanged_artifacts_return_the_same_snapshot(tmp_path):
    directory = _write_set(tmp_path, {"Widget": _props("label")})
    store = ComponentLibraryStore(default_dir=directory)
    first = store.snapshot()
    assert store.snapshot() is first


def test_artifact_change_bumps_the_version_and_the_vocabulary(tmp_path):
    directory = _write_set(tmp_path, {"Widget": _props("label")})
    store = ComponentLibraryStore(default_dir=directory)
    first = store.snapshot()

    _write_set(tmp_path, {"Widget": _props("label"), "Gadget": _props("dial")})
    second = store.snapshot()

    assert second.version == first.version + 1
    assert "Gadget" in component_names_in(second.spec)
    assert "Widget" in component_names_in(first.spec)
    assert "Gadget" not in component_names_in(first.spec)


def test_doc_only_change_bumps_the_version(tmp_path):
    directory = _write_set(tmp_path, {"Widget": _props("label")}, doc="doc v1\n")
    store = ComponentLibraryStore(default_dir=directory)
    first = store.snapshot()

    _write_set(tmp_path, {"Widget": _props("label")}, doc="doc v2, longer\n")
    second = store.snapshot()

    assert second.version == first.version + 1
    assert second.doc == "doc v2, longer\n"


# --- reload: last-good snapshot on a corrupt artifact ------------------------------


def test_corrupt_spec_keeps_the_last_good_snapshot(tmp_path):
    directory = _write_set(tmp_path, {"Widget": _props("label")})
    store = ComponentLibraryStore(default_dir=directory)
    good = store.snapshot()

    (directory / dsl_store.SPEC_FILENAME).write_text("{ not json", encoding="utf-8")
    again = store.reload()

    # The running vocabulary never downgrades silently to flavors+core.
    assert again is good
    assert store.last_error() is not None
    assert "invalid JSON" in store.last_error()


def test_corrupt_manifest_keeps_the_last_good_snapshot(tmp_path):
    directory = _write_set(tmp_path, {"Widget": _props("label")})
    store = ComponentLibraryStore(default_dir=directory)
    good = store.snapshot()

    manifest_dir = directory / dsl_store.MANIFEST_FILENAME
    manifest_dir.write_text("[]", encoding="utf-8")  # not an object
    assert store.reload() is good
    assert "must be a JSON object" in (store.last_error() or "")


def test_missing_artifacts_are_the_documented_fallback_path(tmp_path):
    store = ComponentLibraryStore(default_dir=tmp_path / "absent")
    snapshot = store.snapshot()
    assert snapshot.spec is None and snapshot.doc is None
    # A missing artifact is not an error — it is a fresh checkout.
    assert store.last_error() is None
    assert snapshot.set_id == "absent"


# --- reload: env re-resolution per call (P2-5) ------------------------------------


def test_reload_reresolves_the_artifact_dir_every_call(monkeypatch, tmp_path):
    default = tmp_path / "default"
    _write_set(default, {"Core": _props("label")})
    store = ComponentLibraryStore(default_dir=default)
    assert store.reload().set_id == "default"

    alpha = _write_set(tmp_path / "alpha", {"Widget": _props("label")})
    monkeypatch.setenv(ARTIFACT_DIR_ENV_VAR, str(alpha))
    assert store.reload().set_id == "alpha"

    beta = _write_set(tmp_path / "beta", {"Gadget": _props("dial")})
    monkeypatch.setenv(ARTIFACT_DIR_ENV_VAR, str(beta))
    assert store.reload().set_id == "beta"

    monkeypatch.delenv(ARTIFACT_DIR_ENV_VAR)
    assert store.reload().set_id == "default"


def test_reload_reresolves_the_spec_file_every_call(monkeypatch, tmp_path):
    store = ComponentLibraryStore(default_dir=tmp_path)
    alpha = _write_set(tmp_path / "alpha", {"Widget": _props("label")})
    beta = _write_set(tmp_path / "beta", {"Gadget": _props("dial")})

    monkeypatch.setenv(SPEC_ENV_VAR, str(alpha / dsl_store.SPEC_FILENAME))
    assert "Widget" in component_names_in(store.reload().spec)

    monkeypatch.setenv(SPEC_ENV_VAR, str(beta / dsl_store.SPEC_FILENAME))
    assert "Gadget" in component_names_in(store.reload().spec)
    assert "Widget" not in component_names_in(store.reload().spec)


def test_an_overridden_spec_ignores_the_directories_manifest(tmp_path, monkeypatch):
    """``FABULIST_DSL_SPEC`` points at a single file, not at a set: the set's
    ``set.json`` describes a DIFFERENT spec and must not refuse it."""
    directory = _write_set(
        tmp_path, {"Phone": _props("messages")}, manifest={"id": "phone-set"}
    )
    spec_file = tmp_path / "custom-spec.json"
    spec_file.write_text(json.dumps({"components": {"Widget": _props("label")}}))
    monkeypatch.setenv(SPEC_ENV_VAR, str(spec_file))

    store = ComponentLibraryStore(default_dir=directory)
    snapshot = store.snapshot()
    assert snapshot.set_id == directory.name
    assert "Widget" in component_names_in(snapshot.spec)


# --- the acceptance pin: a name added to the artifacts is live, no restart ---------


def test_a_name_added_to_the_artifacts_is_live_without_a_restart(monkeypatch, tmp_path):
    directory = _write_set(tmp_path, {"Widget": _props("label")})
    monkeypatch.setenv(ARTIFACT_DIR_ENV_VAR, str(directory))
    dsl_spec.reset_spec_cache()
    reset_library_artifact_cache()

    assert "FreshName" not in component_names()
    assert statement_like_re().match('mood = FreshName("x")') is None
    assert strip_dsl('mood = FreshName("x")') == 'mood = FreshName("x")'

    # Regenerate the artifacts: a new component in the spec, its line in the doc.
    _write_set(
        tmp_path,
        {"Widget": _props("label"), "FreshName": _props("mood")},
        doc="Component reference.\n- FreshName(mood) — a panel added live.\n",
    )

    # No reset, no restart: the store's stat check carries the change.
    assert "FreshName" in component_names()
    assert statement_re().match('mood = FreshName("x")')
    assert strip_dsl('one\nmood = FreshName("x")\ntwo') == "one\ntwo"
    assert "FreshName(" in "\n".join(dsl_spec.component_signatures())
    assert "FreshName(mood)" in library_mini.render_library_doc()


def test_the_segmenter_classes_a_newly_added_component_as_dsl(monkeypatch, tmp_path):
    """P1-1: the narrate segmenter must read the CURRENT vocabulary at its
    use-site — a swap that is taught and gated but classed as prose by our own
    segmenter is the ``bad71c7af`` chain."""
    from fabulist.graph.nodes import segment_narrative

    directory = _write_set(tmp_path, {"Widget": _props("label")})
    monkeypatch.setenv(ARTIFACT_DIR_ENV_VAR, str(directory))
    dsl_spec.reset_spec_cache()

    raw = 'He waits.\nmood = FreshName("still")\nThe rain starts.'
    assert [segment["kind"] for segment in segment_narrative(raw)] == ["prose"]

    _write_set(tmp_path, {"Widget": _props("label"), "FreshName": _props("mood")})
    assert any("FreshName" in segment["text"] for segment in segment_narrative(raw))
    assert [segment["kind"] for segment in segment_narrative(raw)] == [
        "prose",
        "dsl",
        "prose",
    ]
    assert component_stmt_re().match('mood = FreshName("still")')


# --- set.json: the client-compat marker + subset refusal (P2-6) -------------------


def test_set_manifest_parses_into_the_snapshot(tmp_path):
    directory = _write_set(
        tmp_path,
        {"Widget": _props("label")},
        manifest={
            "id": "widget-set",
            "name": "Widget Set",
            "components": ["Widget"],
            "spec_version": "abc123",
        },
    )
    snapshot = ComponentLibraryStore(default_dir=directory).snapshot()
    assert (snapshot.set_id, snapshot.name) == ("widget-set", "Widget Set")
    assert snapshot.manifest is not None
    assert snapshot.manifest["spec_version"] == "abc123"


# --- per-directory snapshot cache + per-session binding (issues #16/#24) ----------


def test_snapshots_are_shared_per_directory(tmp_path):
    """A snapshot is CONTENT: two sessions on one set share one object/version."""
    alpha = _write_set(tmp_path / "alpha", {"Widget": _props("label")}, manifest={"id": "alpha"})
    beta = _write_set(tmp_path / "beta", {"Gadget": _props("dial")}, manifest={"id": "beta"})
    store = ComponentLibraryStore(default_dir=alpha)

    first = store.snapshot_for_dir(alpha)
    assert store.snapshot_for_dir(alpha) is first
    other = store.snapshot_for_dir(beta)
    assert other is not first
    assert (first.set_id, other.set_id) == ("alpha", "beta")
    # A shared cache still gets one distinct vocabulary version.
    assert first.version != other.version


def test_a_session_binds_its_set_and_never_retargets_another(tmp_path):
    """#24: one session's set can never change what another session reads."""
    from fabulist.agents.debug import CURRENT_SESSION

    alpha = _write_set(tmp_path / "alpha", {"Widget": _props("label")}, manifest={"id": "alpha"})
    beta = _write_set(tmp_path / "beta", {"Gadget": _props("dial")}, manifest={"id": "beta"})
    store = ComponentLibraryStore(default_dir=alpha)
    store.bind_session_set("session-a", beta)
    store.bind_session_set("session-b", alpha)

    token = CURRENT_SESSION.set("session-a")
    try:
        assert store.snapshot().set_id == "beta"
    finally:
        CURRENT_SESSION.reset(token)

    token = CURRENT_SESSION.set("session-b")
    try:
        assert store.snapshot().set_id == "alpha"
    finally:
        CURRENT_SESSION.reset(token)

    # And a=beta is still beta after b read alpha: no retargeting either way.
    token = CURRENT_SESSION.set("session-a")
    try:
        assert store.snapshot().set_id == "beta"
    finally:
        CURRENT_SESSION.reset(token)


def test_binding_none_clears_a_session_back_to_the_default(tmp_path):
    from fabulist.agents.debug import CURRENT_SESSION

    alpha = _write_set(tmp_path / "alpha", {"Widget": _props("label")}, manifest={"id": "alpha"})
    beta = _write_set(tmp_path / "beta", {"Gadget": _props("dial")}, manifest={"id": "beta"})
    store = ComponentLibraryStore(default_dir=alpha)
    store.bind_session_set("s", beta)

    token = CURRENT_SESSION.set("s")
    try:
        assert store.snapshot().set_id == "beta"
        store.bind_session_set("s", None)
        assert store.snapshot().set_id == "alpha"
    finally:
        CURRENT_SESSION.reset(token)


def test_require_snapshot_is_strict_where_snapshot_falls_back(tmp_path):
    """The selection door must refuse, not fall back (the store's read path)."""
    empty = tmp_path / "empty"
    empty.mkdir()
    store = ComponentLibraryStore(default_dir=empty)
    assert store.snapshot().spec is None  # documented fallback on a hot read
    with pytest.raises(ComponentSetError) as excinfo:
        store.require_snapshot(empty)
    assert dsl_store.SPEC_FILENAME in str(excinfo.value)


def test_require_snapshot_refuses_a_non_subset_manifest(tmp_path):
    directory = _write_set(
        tmp_path,
        {"Widget": _props("label")},
        manifest={"id": "broken", "components": ["Widget", "Phone"]},
    )
    store = ComponentLibraryStore(default_dir=tmp_path)
    with pytest.raises(ComponentSetError) as excinfo:
        store.require_snapshot(directory)
    message = str(excinfo.value)
    assert "Phone" in message and "subset" in message
    assert dsl_store.MANIFEST_FILENAME in message
