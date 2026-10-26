"""Factory image for the default preset (D26).

D26 demotes the code constants: the out-of-the-box pipeline is a **persisted
preset** in the event log and the code becomes *loader + factory image*. This
module owns the factory-image half of that contract:

* :data:`FACTORY_VERSION` is the code factory's version constant. Bump it when
  an engine upgrade changes the default pipeline. It is deliberately separate
  from the per-turn ``prompt_version`` recorded in ``meta["config"]`` (the
  compiler/provenance seam in :mod:`fabulist.wiring`).
* The committed artifact is ``prompts/factory-defaults.json`` at the repo root
  (in-repo and versioned — ``configs/`` is gitignored user state, so it cannot
  hold this; see the ``_comment`` header in the written file). It carries the
  default preset's export shape plus ``factory_version``.
* :func:`factory_image` LOADS that artifact when present and falls back to
  :func:`code_default_payload` only when the file is missing/invalid — so a
  committed default survives a fresh DB, and the JSON is the authority.
* :func:`save_artifact` writes atomically (temp file + :func:`os.replace`) so a
  crash mid-commit can never leave a truncated factory image.

The module intentionally has **no top-level import of**
:mod:`fabulist.prompts.preset_store` (that module imports this one); the few
functions that want the store's normalizer import it lazily at call time.
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .modules import DEFAULT_FLOWS, INTERACTIONS, PromptModule
from .surfaces import factory_surfaces, normalize_surfaces

__all__ = [
    "ARTIFACT_COMMENT",
    "DEFAULT_ARTIFACT_RELPATH",
    "FACTORY_VERSION",
    "FactoryImage",
    "artifact_path",
    "code_default_payload",
    "current_factory_version",
    "factory_image",
    "load_artifact",
    "save_artifact",
]

#: The code factory's version constant. Bump when an engine upgrade changes the
#: default pipeline. A persisted default seeded from an older version surfaces
#: a "factory changed" state (never a silent overwrite — D26).
#:
#: Version history:
#:   1 — pre-D35: the ``library_doc`` surface carried a baked text copy.
#:   2 — D35: the ``library_doc`` surface seeds the expression ``{{library_doc}}``
#:       (dynamic surfaces default to expressions; generator updates reach live
#:       DBs without a manual owner PUT). Existing installs see the review/reset
#:       notice and adopt deliberately.
#:   3 — D38 diagram wave: narrator prose-state sweep (neutral facts example,
#:       positive-only HUD-owns-display directives), BTS ``forecast:`` notation
#:       line, and the ``sidekick``→``assistant`` interaction rename (persisted
#:       presets normalize on read, but the factory image carries the new keys).
#:   4 — D41/D49: the ``critic`` post-turn quality-gate interaction (its flow +
#:       the seeded system prompt) joins the default preset. Existing installs
#:       see the review/reset notice and adopt deliberately.
#:   5 — W6.3 prompt-manager backend model: the directive ``toggles`` map and
#:       the ``prompt_overrides`` shim are retired from the preset payload (the
#:       preset directive rows are the single ``enabled`` source). The artifact
#:       drops both keys. Existing installs see the review/reset notice.
#:   8 — Content Engine consolidation (docs/plans/content-engine-consolidation-
#:       plan.md): parts.py directive catalog rewritten from the owner's preset
#:       (15 → 18 directives; engine_is_not core), the ``<preset>`` wrapper seam
#:       (prompts/wrapper.py; since issue #26 the two bounds ``<preset_def>`` /
#:       ``<preset_runtime>``) added at the narrate + greeting assemblies, and the
#:       narrator.py hand-synced mirror retired (narrator_system_prompt renders
#:       from the compiled directive surface). Existing installs see the
#:       review/reset notice and adopt deliberately; serving is catalog-driven
#:       (preset_directives None), so the new content reaches live after restart
#:       without adoption.
#:   9 — provenance contract (issue #26 follow-up): the trailing engine envelope
#:       (``<engine_state>`` / ``<engine_notice>`` / ``<engine_memory>`` /
#:       ``<engine_lane>`` / ``<steering>`` / ``<engine_palette>``) replaces the
#:       inline ``<state>`` + ``<plan>`` suffix and the loose ``<recalled>``
#:       tail; the frozen prefix gains the ``## Engine context`` clause. The
#:       recalled surface's delimiter seed follows the family spelling
#:       (``<engine_memory>``); the tags themselves are now contract-fixed in
#:       ``prompts/envelope.py``. Existing installs see the review/reset notice
#:       and adopt deliberately; serving is catalog-driven, so the clause
#:       reaches live after restart without adoption.
#:  10 — issue #50 anti-slop consolidation: the canonical banned-phrase list
#:       moves out of code into the artifact's ``anti_slop`` section; the
#:       validator, the narrator's ``anti_slop`` directive (via
#:       ``{{slop_phrases}}``) and the d1 question bank all derive from it.
#:       Existing installs see the review/reset notice and adopt deliberately.
#:  11 — issue #75: the ``## Engine context`` clause is rewritten in full —
#:       the six blocks are named as engine-authored wherever they arrive, the
#:       ``<validation_notice>`` case is spelled out, and the clause gains the
#:       input-language statement (read the tags, never write them: render what
#:       a block carries as plain prose). No tag, anchor or position changes.
#:       Existing installs see the review/reset notice and adopt deliberately.
FACTORY_VERSION: int = 11

#: Repo-relative location of the committed factory image. ``configs/`` is
#: gitignored user state, so the artifact lives under ``prompts/`` instead
#: (also not gitignored — verified in W7-A).
DEFAULT_ARTIFACT_RELPATH: Path = Path("prompts") / "factory-defaults.json"

#: Human-readable header written into the artifact (JSON has no comments). Names
#: it as the factory image and explains how it is produced/consumed.
ARTIFACT_COMMENT: str = (
    "Fabulist factory default preset image (D26). This file is the versioned "
    "factory image the engine LOADS when seeding a fresh default: edit it only "
    "deliberately and commit it. Produced by POST /api/prompts/default/commit; "
    "reset (POST /api/prompts/default/reset) re-seeds the persisted default from "
    "it. Do not hand-edit module ids unless you mean to."
)

def repo_root() -> Path:
    """Repository root, derived from this file's location (``src/fabulist/``)."""
    return Path(__file__).resolve().parents[3]


def artifact_path(path: Path | str | None = None) -> Path:
    """Resolve the factory artifact path (absolute by default)."""
    if path is not None:
        return Path(path)
    return repo_root() / DEFAULT_ARTIFACT_RELPATH


def current_factory_version() -> int:
    """The code factory's version at call time (patchable in tests)."""
    return FACTORY_VERSION


# -- payload construction ----------------------------------------------------


def _module_payload(raw: Any) -> dict[str, Any] | None:
    """Normalize one module row into the store's ``list[dict]`` shape.

    Unknown extra keys are preserved (the log is forward-compatible, D4);
    missing optional fields get the module defaults. Non-mapping rows are
    skipped by returning ``None``.
    """
    if isinstance(raw, PromptModule):
        data: dict[str, Any] = {
            "id": raw.id,
            "name": raw.name,
            "type": raw.type,
            "role": raw.role,
            "content": raw.content,
            "enabled": raw.enabled,
        }
    elif isinstance(raw, Mapping):
        data = {str(key): value for key, value in raw.items()}
    else:
        return None
    normalized: dict[str, Any] = {
        "id": str(data.get("id") or uuid.uuid4().hex),
        "name": str(data.get("name") or ""),
        "type": str(data.get("type") or "text"),
        "role": str(data.get("role") or "system"),
        "content": str(data.get("content") or ""),
        "enabled": bool(data.get("enabled", True)),
    }
    for key, value in data.items():
        if key not in normalized:
            normalized[key] = value
    return normalized


def _flow_payload(rows: Any) -> list[dict[str, Any]]:
    """Normalize one interaction's module rows (non-list -> empty)."""
    if not isinstance(rows, (list, tuple)):
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        payload = _module_payload(row)
        if payload is not None:
            out.append(payload)
    return out


def _flows_payload(raw: Any) -> dict[str, list[dict[str, Any]]]:
    """Normalize a ``flows`` mapping into ``{interaction: [module dicts]}``."""
    flows: dict[str, list[dict[str, Any]]] = {}
    if not isinstance(raw, Mapping):
        return flows
    for interaction, rows in raw.items():
        flows[str(interaction)] = _flow_payload(rows)
    return flows


def _directives_payload(raw: Any) -> list[dict[str, Any]]:
    """Normalize a preset's ``directives`` list (non-list -> empty).

    Tolerant row normalization lives in
    :func:`fabulist.prompts.parts.normalize_directive_rows` (the store and the
    factory share one contract). The key is only ever EMITTED when non-empty —
    an empty list means "inherit the code catalog".
    """
    from .parts import normalize_directive_rows

    return normalize_directive_rows(raw)


def code_default_payload() -> dict[str, Any]:
    """The code-defined default preset as an export-shaped payload.

    Built from :data:`fabulist.prompts.modules.DEFAULT_FLOWS` directly so the
    module ids are the stable canonical ids (``narrator_system`` …) — a
    committed artifact then diffs cleanly instead of churning fresh uuids on
    every commit. The shape is exactly the preset event payload the store folds
    (``modules`` mirrors ``flows["narrate"]``), plus ``is_default`` /
    ``factory_version``.
    """
    flows = {
        interaction: _flow_payload(DEFAULT_FLOWS[interaction])
        for interaction in INTERACTIONS
    }
    return {
        "preset_id": "default",
        "name": "default",
        "modules": flows["narrate"],
        "flows": flows,
        "agent_bindings": {},
        "version": 1,
        "is_default": True,
        "factory_version": current_factory_version(),
        "surfaces": factory_surfaces(),
    }


def normalize_preset_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize any preset-shaped mapping into the canonical export payload.

    ``modules``/``flows`` are normalized (``flows["narrate"]`` mirrored from
    ``modules`` when absent, and vice-versa), scalar fields coerced, unknown
    keys dropped. ``preset_id``/``name`` default to the default preset identity.

    A flow the image does not carry falls back to the CODE default for that
    interaction — the same rule :func:`compile_preset_flow` applies at render
    time. That keeps a committed artifact from an older release readable after a
    new interaction lands (W9.13 ``npc``): every interaction renders its default
    flow instead of a missing one.
    """
    data = {str(key): value for key, value in dict(raw).items()}
    flows = _flows_payload(data.get("flows"))
    modules = _flow_payload(data.get("modules"))
    if modules:
        flows["narrate"] = modules
    elif flows.get("narrate"):
        modules = flows["narrate"]
    else:
        flows["narrate"] = modules
    for interaction in INTERACTIONS:
        if not flows.get(interaction):
            flows[interaction] = _flow_payload(DEFAULT_FLOWS[interaction])

    # W6.3: the retired ``toggles`` / ``prompt_overrides`` keys are read-tolerant
    # (no-op) and never emitted — the preset directive rows are the single
    # ``enabled`` source and the override shim is gone.
    bindings_raw = data.get("agent_bindings")
    bindings = dict(bindings_raw) if isinstance(bindings_raw, Mapping) else {}
    # Preset-carried directive catalog: carried through so commit/reset never
    # drop an owner's directive edits. ``[]`` (absent/empty) is NOT emitted —
    # it means "inherit the code catalog".
    directives = _directives_payload(data.get("directives"))
    # Issue #50: the canonical anti-slop list is an artifact section. Carried
    # through normalization so a factory commit preserves the owner's list;
    # absent/empty is omitted (there is no code list to inherit).
    from fabulist.antislop import normalize_anti_slop

    anti_slop = normalize_anti_slop(data.get("anti_slop"))

    version_raw = data.get("version")
    version = (
        version_raw
        if isinstance(version_raw, int) and not isinstance(version_raw, bool)
        else 1
    )
    factory_version_raw = data.get("factory_version")
    factory_version = (
        factory_version_raw
        if isinstance(factory_version_raw, int)
        and not isinstance(factory_version_raw, bool)
        else current_factory_version()
    )
    payload: dict[str, Any] = {
        "preset_id": str(data.get("preset_id") or "default"),
        "name": str(data.get("name") or "default"),
        "modules": modules,
        "flows": flows,
        "agent_bindings": bindings,
        "version": version,
        "is_default": bool(data.get("is_default", False)),
        "factory_version": factory_version,
        "surfaces": normalize_surfaces(data.get("surfaces")),
    }
    if anti_slop:
        payload["anti_slop"] = anti_slop
    if directives:
        payload["directives"] = directives
    # W9.20: the planner config (cadence / steer / extra fields) is preset DATA
    # and rides the export shape, so a default commit keeps the owner's block
    # instead of dropping it. Emitted only when it differs from the default
    # shape — absent already reads as the defaults (preset_store.planner_config).
    from .preset_store import default_planner_config, normalize_planner_config

    planner = normalize_planner_config(data.get("planner"))[0]
    if planner != default_planner_config():
        payload["planner"] = planner
    return payload


# -- artifact load/save ------------------------------------------------------


def load_artifact(path: Path | str | None = None) -> dict[str, Any] | None:
    """Load and normalize the committed factory artifact.

    Returns ``None`` (never raises) when the file is missing, unreadable, not a
    JSON object, or carries no usable module surface — the caller then falls
    back to :func:`code_default_payload`. The file is the authority only when it
    is well-formed.
    """
    resolved = artifact_path(path)
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, Mapping):
        return None
    if "flows" not in raw and "modules" not in raw:
        return None
    return normalize_preset_payload(raw)


def save_artifact(
    payload: Mapping[str, Any],
    *,
    factory_version: int | None = None,
    path: Path | str | None = None,
) -> Path:
    """Write the factory image atomically; return the written path.

    The payload is normalized first, then stamped with ``factory_version``
    (defaulting to :func:`current_factory_version`) and the :data:`ARTIFACT_COMMENT`
    header. Writes to a sibling temp file and :func:`os.replace`s it into place,
    so readers never observe a partial file.
    """
    resolved = artifact_path(path)
    # Issue #50: the ``anti_slop`` list is ARTIFACT-OWNED. A commit from a
    # persisted default row that predates it (or one the owner never touched)
    # must not silently delete the canonical list — absence preserves the
    # section already on disk; an explicit block (even ``{}``) replaces it.
    preserve_anti_slop = isinstance(payload, Mapping) and "anti_slop" not in payload
    existing_anti_slop: dict[str, Any] | None = None
    if preserve_anti_slop:
        existing = load_artifact(resolved)
        if isinstance(existing, Mapping):
            candidate = existing.get("anti_slop")
            if isinstance(candidate, Mapping) and candidate:
                existing_anti_slop = dict(candidate)
    normalized = normalize_preset_payload(payload)
    if existing_anti_slop and not normalized.get("anti_slop"):
        normalized["anti_slop"] = existing_anti_slop
    normalized["factory_version"] = (
        current_factory_version()
        if factory_version is None
        else int(factory_version)
    )
    # Header first for a readable committed diff.
    document = {
        "_comment": ARTIFACT_COMMENT,
        "factory_version": normalized.pop("factory_version"),
        **normalized,
    }
    resolved.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2, ensure_ascii=False) + "\n"
    tmp = resolved.with_name(f".{resolved.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, resolved)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:  # pragma: no cover - best-effort cleanup
                pass
    return resolved


# -- the factory image -------------------------------------------------------


@dataclass(frozen=True)
class FactoryImage:
    """The default pipeline the code should seed from, plus its provenance.

    ``source`` is ``"artifact"`` when a committed ``prompts/factory-defaults.json``
    was loaded (its ``factory_version`` is preserved) and ``"code"`` when the
    constants were used (version = :func:`current_factory_version`).
    """

    payload: dict[str, Any]
    factory_version: int
    source: str

    @property
    def from_code(self) -> bool:
        return self.source == "code"


def factory_image(path: Path | str | None = None) -> FactoryImage:
    """Resolve the factory image: committed artifact when present, else code.

    A present artifact wins and its recorded ``factory_version`` rides through
    (so if the code constant has since advanced, the seeded row is stamped with
    the *older* committed version and the "factory changed" notice fires — the
    deliberate-adoption flow, D26).
    """
    loaded = load_artifact(path)
    if loaded is not None:
        return FactoryImage(
            payload=loaded,
            factory_version=int(loaded.get("factory_version", current_factory_version())),
            source="artifact",
        )
    return FactoryImage(
        payload=code_default_payload(),
        factory_version=current_factory_version(),
        source="code",
    )
