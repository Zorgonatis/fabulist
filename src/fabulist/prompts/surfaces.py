"""D27 token-surface schema: preset-carried data with constant fallback.

Five token surfaces contribute to agent contexts but historically lived as code
constants. D27 promotes them to an additive, preset-carried ``surfaces`` block
so every token is viewable + editable with ``{{expression}}`` support; the code
constants demote to a factory seed only. This module owns the schema, the
tolerant normalizer, the factory seed, and the preset accessor. It is a **leaf**
(no package imports at module scope) so every consumer -- ``compiler``,
``context.facts``, ``context.regex_stage``, ``modules`` -- can import it without
an import cycle.

Schema (every key optional; absent/empty means "fall back to the constant")::

    surfaces = {
        "library_doc": str,                       # OpenUI component reference
        "recalled": {                             # <recalled> framing + headers
            "framing": str, "open": str, "close": str,
            "headers": {"recall": str, "world_info": str,
                        "recap": str, "bts": str},
        },
        "regexes": [                              # full-list replace (no merges)
            {"id": str, "name": str, "find": str, "replace": str,
             "flags": str, "prompt_only": bool, "min_depth": int,
             "enabled": bool},                    # or "scope": "prompt"|"display"
        ],
        "history": {"depth": int, "strip_dsl": bool, "dsl_keep_last": int},
        "directives": {directive_id: content},    # directive default text
    }

An older preset that never carried ``surfaces`` unfolds to ``{}`` and every
loader reads the code constant, so nothing breaks mid-migration.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

__all__ = [
    "DEFAULT_HISTORY_DEPTH",
    "DEFAULT_HISTORY_KEEP_LAST_DSL",
    "DEFAULT_HISTORY_STRIP_DSL",
    "LIBRARY_DOC_EXPRESSION",
    "SURFACES_KEY",
    "factory_surfaces",
    "normalize_surfaces",
    "preset_surfaces",
    "render_surface_text",
    "serialize_regex",
]

#: Top-level preset payload key carrying the D27 surfaces block.
SURFACES_KEY = "surfaces"

#: D35: the factory seed for the ``library_doc`` surface is a *resolvable
#: expression*, not baked text — so a regenerated library doc reaches every live
#: DB without a manual owner PUT. ``{{library_doc}}`` is a registered prompt
#: variable (see :mod:`fabulist.prompts.variables`) resolving through
#: :func:`fabulist.prompts.library_mini.resolve_library_doc` (generated
#: artifact; a missing one raises). An owner tops/tails it in the editor
#: (``"Extra\n{{library_doc}}\nMore"``) or replaces the surface entirely with
#: their own text (no token = pure override).
LIBRARY_DOC_EXPRESSION = "{{library_doc}}"

#: Default history projection depth (mirrors ``wiring.history_from_log``'s
#: ``limit=20`` -- the pre-D27 constant).
DEFAULT_HISTORY_DEPTH = 20

#: Default DSL-strip behavior: history is projected WITHOUT its stale panels
#: (mirrors ``compiler.stripped_history``'s pre-D27 unconditional strip).
DEFAULT_HISTORY_STRIP_DSL = True

#: Default keep-last window (counted in narrative turns) whose DSL segments
#: survive the strip; older turns strip as before. ``0`` reproduces the pre-window
#: strip-all projection. Only bites callers that pass raw narrative segments --
#: ``wiring.history_from_log`` strips DSL upstream of the compiler.
DEFAULT_HISTORY_KEEP_LAST_DSL = 3

_RECALLED_TEXT_KEYS = ("framing", "open", "close")
_RECALLED_HEADER_KEYS = ("recall", "world_info", "recap", "bts")

#: Canonical serialization field order for one regex row.
_REGEX_KEYS = (
    "id",
    "name",
    "find",
    "replace",
    "flags",
    "prompt_only",
    "min_depth",
    "enabled",
)


def serialize_regex(stage: Any) -> dict[str, Any]:
    """Serialize a :class:`~fabulist.context.regex_stage.StageRegex` (or mapping).

    Accepts either the engine dataclass or an already-shaped mapping and returns
    the canonical ``{id,name,find,replace,flags,prompt_only,min_depth,enabled}``
    dict, so the factory seed and a UI-authored row share one shape.
    """
    if isinstance(stage, Mapping):
        src: Any = stage

        def _get(key: str, default: Any) -> Any:
            return src.get(key, default)

    else:

        def _get(key: str, default: Any) -> Any:
            return getattr(stage, key, default)

    return {
        "id": str(_get("id", "") or ""),
        "name": str(_get("name", "") or ""),
        "find": str(_get("find", "") or ""),
        "replace": str(_get("replace", "") or ""),
        "flags": str(_get("flags", "") or ""),
        "prompt_only": bool(_get("prompt_only", True)),
        "min_depth": int(_get("min_depth", 0) or 0),
        "enabled": bool(_get("enabled", True)),
    }


def normalize_surfaces(raw: Any) -> dict[str, Any]:
    """Tolerantly normalize a ``surfaces`` block; unknown shapes drop to fallback.

    Idempotent: normalizing an already-normalized block returns an equal block.
    The result only ever carries well-typed values, so loaders can trust it.
    """
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, Any] = {}

    if "library_doc" in raw and isinstance(raw["library_doc"], str):
        out["library_doc"] = raw["library_doc"]

    recalled = raw.get("recalled")
    if isinstance(recalled, Mapping):
        block: dict[str, Any] = {}
        for key in _RECALLED_TEXT_KEYS:
            if key in recalled and isinstance(recalled[key], str):
                block[key] = recalled[key]
        headers = recalled.get("headers")
        if isinstance(headers, Mapping):
            kept = {
                str(key): value
                for key, value in headers.items()
                if key in _RECALLED_HEADER_KEYS and isinstance(value, str)
            }
            if kept:
                block["headers"] = kept
        if block:
            out["recalled"] = block

    regexes = raw.get("regexes")
    if isinstance(regexes, (list, tuple)):
        # Keep mapping rows as-is so an ST-shaped entry (``findRegex`` /
        # ``replaceString`` / ``scope``) reaches ``regex_from_surface``, which
        # parses it. Dataclass rows are serialized to the canonical shape.
        rows = [
            dict(entry) if isinstance(entry, Mapping) else serialize_regex(entry)
            for entry in regexes
            if isinstance(entry, Mapping) or hasattr(entry, "find")
        ]
        out["regexes"] = rows

    history = raw.get("history")
    if isinstance(history, Mapping):
        knobs: dict[str, Any] = {}
        depth = history.get("depth")
        if isinstance(depth, int) and not isinstance(depth, bool) and depth >= 0:
            knobs["depth"] = depth
        strip = history.get("strip_dsl")
        if isinstance(strip, bool):
            knobs["strip_dsl"] = strip
        keep_last = history.get("dsl_keep_last")
        if (
            isinstance(keep_last, int)
            and not isinstance(keep_last, bool)
            and keep_last >= 0
        ):
            knobs["dsl_keep_last"] = keep_last
        if knobs:
            out["history"] = knobs

    directives = raw.get("directives")
    if isinstance(directives, Mapping):
        kept_directives = {
            str(key): value
            for key, value in directives.items()
            if isinstance(value, str)
        }
        if kept_directives:
            out["directives"] = kept_directives

    return out


def preset_surfaces(preset: Any) -> dict[str, Any]:
    """The normalized ``surfaces`` block of a preset (or ``{}``).

    Tolerant of the pre-patch :class:`~fabulist.prompts.preset_store.Preset`
    dataclass (no ``surfaces`` field -> attribute lookup yields ``None`` ->
    ``{}``), a mapping payload, or ``None``. This is the single seam the wiring
    patch uses: ``surfaces = preset_surfaces(preset)``.
    """
    if preset is None:
        return {}
    raw: Any = None
    if isinstance(preset, Mapping):
        raw = preset.get(SURFACES_KEY)
    if raw is None:
        raw = getattr(preset, SURFACES_KEY, None)
    return normalize_surfaces(raw)


def render_surface_text(text: str, variables: Mapping[str, str] | None = None) -> str:
    """Render ``{{expression}}`` tokens in a surface text field.

    Delegates to :func:`fabulist.prompts.variables.render_template` (plain
    string substitution, no evaluation). Unknown tokens stay literal, and an
    absent/empty ``variables`` map leaves every token untouched -- so a surface
    that carries no expression is byte-identical to its constant.
    """
    from fabulist.prompts.variables import render_template

    return render_template(text, dict(variables or {}))


def factory_surfaces() -> dict[str, Any]:
    """The code-constant seed of every surface (W7-A's factory artifact source).

    Captures today's constants exactly as preset-carried data. Loading this block
    back through the loaders reproduces the pre-D27 assembly byte-for-byte; it is
    what a fresh default preset is seeded from and what "commit to default"
    exports. Imports are function-local so this leaf module stays import-cycle
    free.

    D35: the ``library_doc`` surface seeds the *expression*
    :data:`LIBRARY_DOC_EXPRESSION` (``{{library_doc}}``), not baked text, so a
    regenerated library doc reaches live DBs without a manual owner PUT. The
    ``directives`` surface follows the same principle: it seeds ``{}`` — the
    code catalog (:data:`fabulist.prompts.parts.DEFAULT_DIRECTIVES`) stays
    the source of truth, and only a USER edit persists a literal override.
    Every other surface stays a byte-identical constant.
    """
    from fabulist.context.facts import (
        RECALLED_BTS_HEADER,
        RECALLED_CLOSE,
        RECALLED_FRAMING,
        RECALLED_OPEN,
        RECALLED_RECALL_HEADER,
        RECALLED_RECAP_HEADER,
        RECALLED_WORLD_INFO_HEADER,
    )
    from fabulist.context.regex_stage import PRESET_REGEXES

    return normalize_surfaces(
        {
            "library_doc": LIBRARY_DOC_EXPRESSION,
            "recalled": {
                "framing": RECALLED_FRAMING,
                "open": RECALLED_OPEN,
                "close": RECALLED_CLOSE,
                "headers": {
                    "recall": RECALLED_RECALL_HEADER,
                    "world_info": RECALLED_WORLD_INFO_HEADER,
                    "recap": RECALLED_RECAP_HEADER,
                    "bts": RECALLED_BTS_HEADER,
                },
            },
            "regexes": [serialize_regex(stage) for stage in PRESET_REGEXES],
            "history": {
                "depth": DEFAULT_HISTORY_DEPTH,
                "strip_dsl": DEFAULT_HISTORY_STRIP_DSL,
            },
            # Directives INHERIT from the code catalog (`parts.DEFAULT_DIRECTIVES`):
            # absent/empty means "fall back to the constant" (schema docstring),
            # so a `parts.py` edit reaches every live DB with no artifact churn and
            # no owner PUT. Baking copies here is what let stale directive text
            # shadow the code silently (same class D35 fixed for `library_doc`).
            # Only a USER EDITED directive should ever appear as a literal override.
            # (The normalizer drops the empty mapping, so the seed carries no key.)
            "directives": {},
        }
    )
