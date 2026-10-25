"""The lang-core component-spec artifacts and the consumers derived from them.

Owner mandate: ONE component spec feeds (a) the frontend render registry, (b) the
engine DSL validation table, (c) the DSL-stripping component name list, (d) the
generated narrator library doc and (e) the frontend streaming segmenter
(``frontend/src/routes/chat/segment.ts``), which derives its recognized component
names from the registry at runtime — the hand-listed array stranded six components
as raw text during streaming. These tests pin the generated artifacts
(``prompts/generated/library.spec.json`` + ``library.doc.txt``) as the
authoritative source and assert the consumers stay in lockstep:

  * the spec is a well-formed lang-core ``LibrarySpec`` (14 components, root
    ``ScenePane``) and is byte-deterministic (rerunning the generator is a no-op);
  * every component in the frontend registry (``frontend/src/lib/openui/library.ts``)
    appears in the spec and vice versa;
  * ``fabulist.prompts.compiler._COMPONENT_STMT_RE`` strips a statement for EVERY
    spec component — this is the drift fix (flavor statements used to leak into
    narrator history because the regex hardcoded only the generics);
  * the streaming segmenter derives its recognized names from the registry and
    carries no hand list (see ``test_streaming_segmenter_derives_names_from_registry``);
  * the code factory seed carries the ``{{library_doc}}`` expression as the
    default library surface content (D26/D27/D35), which resolves to the
    generated doc.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from fabulist.prompts import library_mini
from fabulist.prompts.compiler import strip_dsl
from fabulist.prompts.library_mini import (
    component_names,
    render_library_doc,
    resolve_library_doc,
)
from fabulist.prompts.surfaces import LIBRARY_DOC_EXPRESSION, factory_surfaces

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = REPO_ROOT / "prompts" / "generated" / "library.spec.json"
DOC_PATH = REPO_ROOT / "prompts" / "generated" / "library.doc.txt"
SET_PATH = REPO_ROOT / "prompts" / "generated" / "set.json"
FACTORY_ARTIFACT = REPO_ROOT / "prompts" / "factory-defaults.json"
REGISTRY_PATH = REPO_ROOT / "frontend" / "src" / "lib" / "openui" / "library.ts"
SEGMENTER_PATH = REPO_ROOT / "frontend" / "src" / "routes" / "chat" / "segment.ts"
GENERATOR = REPO_ROOT / "frontend" / "scripts" / "generate-library-spec.mjs"

#: The four infrastructure components declared inline in the frontend registry.
INFRASTRUCTURE = (
    "ScenePane",
    "MeterBar",
    "ObjectCard",
    "BeatCard",
)

#: Legacy registry aliases: ``ArtifactCard`` was renamed to ``ObjectCard`` (Wave
#: 3A) and stays registered so old statements still parse/render. It is NOT in
#: the generated spec (the model is never taught the old name).
REGISTRY_ALIASES = {"ArtifactCard"}


def _spec() -> dict:
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def _registry_names() -> set[str]:
    """Component names declared via ``defineComponent({ name: '…' })`` in library.ts."""
    source = REGISTRY_PATH.read_text(encoding="utf-8")
    return set(re.findall(r"\bname:\s*'([A-Za-z_]\w*)'", source))


# --- artifact shape + determinism ---------------------------------------------------


def test_artifacts_exist():
    assert SPEC_PATH.is_file(), "run frontend/scripts/generate-library-spec.mjs"
    assert DOC_PATH.is_file(), "run frontend/scripts/generate-library-spec.mjs"
    assert SET_PATH.is_file(), "run frontend/scripts/generate-library-spec.mjs"


def test_set_manifest_describes_the_spec():
    """``set.json`` is the client-compat marker (W9.27, P2-6).

    ``components`` states what the set renders, so the engine can refuse a set
    that is not a subset of its own spec; ``spec_version`` fingerprints the spec
    artifact the manifest describes.
    """
    manifest = json.loads(SET_PATH.read_text(encoding="utf-8"))
    # The set's REAL identity (owner ruling, #17): id, display name and a
    # major.minor.patch version — not a placeholder id kept for compatibility.
    assert manifest["id"] == "technology"
    assert manifest["name"] == "Technology"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])
    assert isinstance(manifest["name"], str) and manifest["name"].strip()
    components = manifest["components"]
    assert isinstance(components, list) and len(components) == len(set(components))
    assert set(components) == set(_spec()["components"])
    assert re.fullmatch(r"[0-9a-f]{12}", manifest["spec_version"])


def test_store_accepts_the_committed_default_set():
    """The shipped manifest and spec agree, so the engine's default set loads."""
    from fabulist.dsl.store import ComponentLibraryStore, component_names_in

    store = ComponentLibraryStore(default_dir=REPO_ROOT / "prompts" / "generated")
    snapshot = store.snapshot()
    assert store.last_error() is None
    assert snapshot.set_id == "technology"
    assert set(component_names_in(snapshot.spec)) == set(_spec()["components"])


def test_spec_is_a_libraryspec_with_root_and_20_components():
    spec = _spec()
    assert spec["root"] == "ScenePane"
    assert isinstance(spec["components"], dict)
    assert len(spec["components"]) == 20
    for name, comp in spec["components"].items():
        assert isinstance(comp.get("signature"), str) and comp["signature"]
        assert comp["signature"].startswith(f"{name}("), name
        assert isinstance(comp.get("description"), str) and comp["description"]
        # Engine-side arity metadata rides alongside the lang-core fields.
        assert isinstance(comp.get("props"), list) and comp["props"]


def test_spec_groups_partition_the_components():
    spec = _spec()
    grouped = [name for group in spec["componentGroups"] for name in group["components"]]
    assert len(grouped) == len(set(grouped)) == 20
    assert set(grouped) == set(spec["components"])


def test_spec_carries_self_contained_deep_schemas_for_flavors():
    """The engine deep gate reads these; the four infrastructure components
    (inline in ``library.ts``, not headlessly importable) are deliberately
    omitted and stay on the coarse prop-type check."""
    components = _spec()["components"]
    for name in INFRASTRUCTURE:
        assert "jsonSchema" not in components[name], name
    flavors = [name for name in components if name not in INFRASTRUCTURE]
    assert len(flavors) == 16
    for name in flavors:
        schema = components[name].get("jsonSchema")
        assert isinstance(schema, dict), name
        assert schema.get("type") == "object", name
        assert isinstance(schema.get("properties"), dict), name
        # Self-contained (`reused: 'inline'`): no `$ref`/`$defs` to resolve.
        assert "$defs" not in schema, name
        assert "$ref" not in json.dumps(schema), name
        # The deep schema's props agree with the emitted arity metadata.
        assert set(schema["properties"]) == {p["name"] for p in components[name]["props"]}


def test_postit_deep_schema_is_the_object_shape_the_frontend_uses():
    notes = _spec()["components"]["PostIt"]["jsonSchema"]["properties"]["notes"]
    assert notes["type"] == "array"
    assert notes["items"]["type"] == "object"
    assert notes["items"]["properties"]["text"]["type"] == "string"
    assert notes["items"]["required"] == ["text"]


def test_generator_rerun_is_byte_identical():
    """`node generate-library-spec.mjs --check` is the idempotency oracle."""
    if shutil.which("node") is None:
        pytest.skip("node not available")
    if not (REPO_ROOT / "frontend" / "node_modules" / "@openuidev" / "lang-core").is_dir():
        pytest.skip("@openuidev/lang-core not installed")
    result = subprocess.run(
        ["node", "scripts/generate-library-spec.mjs", "--check"],
        cwd=REPO_ROOT / "frontend",
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    # All three artifacts are covered by the same oracle, set.json included.
    for label in ("library.spec.json", "library.doc.txt", "set.json"):
        assert f"ok {label}" in result.stdout, result.stdout


def test_doc_artifact_is_not_hand_editable_stub():
    doc = DOC_PATH.read_text(encoding="utf-8")
    # lang-core's generated sections are present; the hand-authored fallback is not.
    assert "## Component Signatures" in doc
    assert "### Component reference" not in doc


# --- spec <-> frontend registry -----------------------------------------------------


def test_spec_matches_frontend_registry_names():
    spec_names = set(_spec()["components"])
    registry_names = _registry_names()
    assert registry_names, "failed to extract component names from library.ts"
    # The registry carries the 20 spec components plus the legacy alias.
    assert registry_names - REGISTRY_ALIASES == spec_names, (
        f"registry-only: {sorted(registry_names - REGISTRY_ALIASES - spec_names)}; "
        f"spec-only: {sorted(spec_names - registry_names)}"
    )
    # Sanity: the registry really declares all 20 (4 infrastructure + 10 flavors + Custom + 5 reproductions).
    assert set(INFRASTRUCTURE) <= registry_names
    assert len(spec_names) == 20
    assert len(registry_names - REGISTRY_ALIASES) == 20


def test_streaming_segmenter_derives_names_from_registry():
    """(e): the streaming segmenter derives its recognized names from the registry.

    The hand-listed array it replaced stranded six components (Monitor, Phone,
    SMS, PostIt, SocialPost, Custom) as raw prose during a live stream.
    """
    source = SEGMENTER_PATH.read_text(encoding="utf-8")
    # The derivation must be present …
    assert "from '#lib/openui/library'" in source
    assert "Object.keys(library.components)" in source
    # … and no hand list may come back: strip `//` line and `/* */` block
    # comments, then prove no registry name appears as a string literal.
    code = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    code = re.sub(r"//[^\n]*", "", code)
    for name in _registry_names():
        assert not re.search(rf"""['\"]{re.escape(name)}['\"]""", code), (
            f"{name!r} is a string literal in segment.ts — the recognized names "
            "must stay derived from the registry, never a hand list"
        )


def test_component_names_helper_matches_spec():
    assert set(component_names()) == set(_spec()["components"])


def test_engine_spec_loader_carries_the_deep_schema():
    """``fabulist.dsl.spec`` surfaces the emitted ``jsonSchema`` on the schema."""
    from fabulist.dsl import spec as dsl_spec

    dsl_spec.reset_spec_cache()
    try:
        postit = dsl_spec.component_schema("PostIt")
        assert postit is not None and isinstance(postit.json_schema, dict)
        assert postit.json_schema["properties"]["notes"]["type"] == "array"
        # The infrastructure four carry none (no emitted zod source).
        for name in INFRASTRUCTURE:
            schema = dsl_spec.component_schema(name)
            assert schema is not None and schema.json_schema is None, name
    finally:
        dsl_spec.reset_spec_cache()


# --- DSL-strip regex coverage -------------------------------------------------------


def test_compiler_regex_strips_every_spec_component():
    """The drift fix: every component name (not just the generics) is recognised."""
    for name in _spec()["components"]:
        cleaned = strip_dsl(f"first line\n{name.lower()} = {name}(1, 2)\nlast line")
        assert name not in cleaned, name
        assert cleaned == "first line\nlast line", name


def test_compiler_regex_strips_flavor_names_that_used_to_leak():
    raw = (
        "She keeps her face still.\n"
        'spotlight = DialogueSpotlight("Lyra", "Not yet.", "a flat smile")\n'
        "Her hands give her away."
    )
    cleaned = strip_dsl(raw)
    assert "DialogueSpotlight" not in cleaned
    assert cleaned == "She keeps her face still.\nHer hands give her away."


def test_compiler_regex_ignores_unknown_names():
    # A statement naming an unknown component must survive (not silently eaten).
    assert strip_dsl("x = NotAComponent(1)") == "x = NotAComponent(1)"


def test_component_name_list_is_complete():
    names = component_names()
    assert len(names) == 20
    assert len(set(names)) == 20


# --- factory seeding (D26/D27/D35) --------------------------------------------------


def test_factory_seed_carries_the_library_doc_expression():
    # D35: the factory seeds `{{library_doc}}`, not baked text; the expression
    # resolves to the generated doc through the loader.
    assert factory_surfaces()["library_doc"] == LIBRARY_DOC_EXPRESSION
    assert resolve_library_doc(factory_surfaces()) == render_library_doc()
    assert resolve_library_doc(factory_surfaces()) == DOC_PATH.read_text(encoding="utf-8")


def test_committed_factory_image_carries_the_library_doc_expression():
    """D26/D35: the committed factory image a fresh boot seeds from is in sync."""
    image = json.loads(FACTORY_ARTIFACT.read_text(encoding="utf-8"))
    assert image["surfaces"]["library_doc"] == LIBRARY_DOC_EXPRESSION
    assert resolve_library_doc(image["surfaces"]) == DOC_PATH.read_text(encoding="utf-8")


def test_generated_doc_loads_without_the_hand_constant():
    # render_library_doc returns the artifact verbatim (not the fallback).
    assert render_library_doc() == DOC_PATH.read_text(encoding="utf-8")
    assert library_mini.MINI_COMPONENTS  # fallback remains importable
