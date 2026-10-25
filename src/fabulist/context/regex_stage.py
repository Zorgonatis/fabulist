"""Regex pipeline stage (PLAN W2.3).

Port of the spec-of-record preset's ``extensions.regex_scripts``
(``~/Projects/Stabs-EDH/Stabs-GLM5.1-Directives-v4.0.0.json``). The preset uses
SillyTavern regex scripts to strip its own in-context machinery (VTK state
emission, assistant scaffolding) before text is sent to the model. This module
reproduces that pipeline:

  * :class:`StageRegex` is one ported script: ``{id, name, find, replace, flags,
    prompt_only, min_depth}`` (plus ``enabled``).
  * :func:`parse_js_regex` splits a JS literal ``/body/flags`` into body + flags.
  * :func:`js_flags_to_re` maps JS flag letters to Python ``re`` flags with an
    explicit table (``g`` is handled by ``re.sub``/``findall``, which are global
    by default). Unknown flags raise :class:`ValueError` naming the pattern.
  * :func:`apply_regex_stage` applies each enabled pattern whose ``min_depth`` is
    satisfied by the call's ``depth_index`` and whose ``prompt_only`` gate is
    compatible with the call context.

Design notes (boring on purpose):

  * ``prompt_only`` follows SillyTavern semantics: a ``prompt_only`` pattern
    applies only when building the prompt (``prompt_only=True``); a
    ``non-prompt_only`` pattern applies in every context. The shipped preset
    entries are all ``prompt_only=True``.
  * ``find`` holds the regex body with the JS delimiters already stripped;
    ``flags`` holds the JS flag letters. :meth:`StageRegex.from_preset` does that
    split so the port stays faithful to the preset JSON.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Sequence

__all__ = [
    "PRESET_REGEXES",
    "StageRegex",
    "apply_regex_stage",
    "compile_stage",
    "js_flags_to_re",
    "parse_js_regex",
    "regex_from_surface",
    "regexes_from_surfaces",
]

# Explicit JS -> Python flag mapping. ``g`` (global) has no Python ``re``
# equivalent: ``re.sub``/``re.findall`` already operate globally, so it maps to
# 0 and is accepted but inert. Unknown letters are a hard error (see
# :func:`js_flags_to_re`).
JS_FLAG_MAP: dict[str, int] = {
    "g": 0,
    "i": re.IGNORECASE,
    "s": re.DOTALL,
    "m": re.MULTILINE,
}

# JS replacement string backreference syntax (``$1``, ``$&``) differs from
# Python (``\1``, ``\g<0>``). The shipped preset replacements are empty, but the
# conversion keeps the port honest for non-empty replacements.
_JS_BACKREF_RE = re.compile(r"\$(\d{1,2}|&)")


def parse_js_regex(literal: str, *, name: str = "") -> tuple[str, str]:
    """Split a JS regex literal ``/body/flags`` into ``(body, flags)``.

    Uses the last ``/`` as the delimiter, so unescaped ``/`` inside the body
    (illegal in a JS literal anyway) is tolerated as body content up to the final
    slash. Raises :class:`ValueError` if ``literal`` is not ``/.../...``.
    """

    if len(literal) < 2 or not literal.startswith("/"):
        raise ValueError(
            f"not a JS regex literal for pattern {name!r}: {literal!r}"
        )
    end = literal.rfind("/")
    if end == 0:  # only the opening slash exists
        raise ValueError(
            f"not a JS regex literal for pattern {name!r}: {literal!r}"
        )
    return literal[1:end], literal[end + 1 :]


def js_flags_to_re(flags: str, *, name: str = "") -> int:
    """Map JS flag letters to a Python ``re`` flags bitmask.

    ``g`` is accepted but maps to ``0`` (global behavior is inherent to
    ``re.sub``/``re.findall``). Any unknown flag raises :class:`ValueError` and
    names the offending pattern.
    """

    out = 0
    for ch in flags:
        if ch not in JS_FLAG_MAP:
            raise ValueError(
                f"unknown regex flag {ch!r} in pattern {name!r}"
            )
        out |= JS_FLAG_MAP[ch]
    return out


def _convert_replacement(replace: str) -> str:
    """Translate JS ``$1``/``$&`` backreferences to Python ``re.sub`` syntax."""

    return _JS_BACKREF_RE.sub(
        lambda m: r"\g<%s>" % ("0" if m.group(1) == "&" else m.group(1)),
        replace,
    )


@dataclass(frozen=True)
class StageRegex:
    """One ported preset regex script (W2.3).

    ``find`` is the regex body (JS delimiters stripped), ``flags`` the JS flag
    letters. ``min_depth`` mirrors the preset's ``minDepth``.
    """

    id: str
    name: str
    find: str
    replace: str
    flags: str
    prompt_only: bool
    min_depth: int
    enabled: bool = True

    @classmethod
    def from_preset(cls, entry: Mapping[str, object]) -> "StageRegex":
        """Build a :class:`StageRegex` from one preset ``regex_scripts`` entry."""

        name = str(entry.get("scriptName", entry.get("name", "")))
        body, flags = parse_js_regex(str(entry["findRegex"]), name=name)
        return cls(
            id=str(entry.get("id", "")),
            name=name,
            find=body,
            replace=str(entry.get("replaceString", "")),
            flags=flags,
            prompt_only=bool(entry.get("promptOnly", False)),
            min_depth=int(entry.get("minDepth") or 0),
            enabled=not bool(entry.get("disabled", False)),
        )


def compile_stage(stage: StageRegex) -> re.Pattern[str]:
    """Compile a stage's body with its converted flags (global semantics)."""

    return re.compile(stage.find, js_flags_to_re(stage.flags, name=stage.name))


# The preset's actual ``extensions.regex_scripts`` entries (v4.0.0), ported with
# ``enabled=True`` by default. Bodies/flags came from the JS literals via
# ``StageRegex.from_preset``; see ``tests/test_regex_stage.py`` for the literal
# round-trip. "Strip VTK from Sent Context" and "Strip Assistant Block from Sent
# Context" are the two present in the spec-of-record preset.
PRESET_REGEXES: tuple[StageRegex, ...] = (
    StageRegex(
        id="118e07e4-9b2f-449f-88e7-3f21829f7326",
        name="Stabs - Strip VTK from Sent Context",
        find=r"<!-- VTK_START -->[\s\S]*?<!-- VTK_END -->",
        replace="",
        flags="gi",
        prompt_only=True,
        min_depth=3,
        enabled=True,
    ),
    StageRegex(
        id="aa908405-b96e-42c6-8fc1-1fd040643619",
        name="Stabs - Strip Assistant Block from Sent Context",
        find=r"<!-- ASST_START -->[\s\S]*?<!-- ASST_END -->",
        replace="",
        flags="gi",
        prompt_only=True,
        min_depth=3,
        enabled=True,
    ),
)


def regex_from_surface(entry: Mapping[str, object] | StageRegex) -> StageRegex:
    """Build a :class:`StageRegex` from one preset ``surfaces.regexes`` row.

    The surface row is engine-native (``find`` is the regex body with JS
    delimiters stripped, ``flags`` the JS flag letters). ``findRegex`` /
    ``replaceString`` / ``minDepth`` are also accepted for round-tripping the
    SillyTavern-shaped literal, and ``scope`` maps to ``prompt_only``
    (``"prompt"``/``"both"`` -> prompt-only; ``"display"`` -> every context).
    """
    if isinstance(entry, StageRegex):
        return entry
    if not isinstance(entry, Mapping):
        raise ValueError(f"regex surface entry must be a mapping: {entry!r}")

    name = str(entry.get("name", ""))
    raw_find = entry.get("find")
    flags = entry.get("flags")
    if raw_find is None and entry.get("findRegex") is not None:
        body, parsed_flags = parse_js_regex(str(entry["findRegex"]), name=name)
        raw_find = body
        if flags is None:
            flags = parsed_flags

    prompt_only = entry.get("prompt_only")
    if prompt_only is None:
        scope = entry.get("scope")
        prompt_only = scope != "display"

    return StageRegex(
        id=str(entry.get("id", "")),
        name=name,
        find=str(raw_find or ""),
        replace=str(entry.get("replace", entry.get("replaceString", ""))),
        flags=str(flags or ""),
        prompt_only=bool(prompt_only),
        min_depth=int(entry.get("min_depth", entry.get("minDepth", 0)) or 0),
        enabled=bool(entry.get("enabled", not entry.get("disabled", False))),
    )


def regexes_from_surfaces(
    surfaces: Mapping[str, object] | None = None,
) -> tuple[StageRegex, ...]:
    """Effective regex script list: preset surface when present, constant otherwise.

    A surface ``regexes`` list is a **full-list replace** (no partial merges):
    the preset owns the whole stage set. ``None``/absent -> :data:`PRESET_REGEXES`.
    """
    from fabulist.prompts.surfaces import normalize_surfaces

    normalized = normalize_surfaces(surfaces)
    if "regexes" not in normalized:
        return PRESET_REGEXES
    return tuple(regex_from_surface(entry) for entry in normalized["regexes"])


def _applies(stage: StageRegex, *, depth_index: int, prompt_only: bool) -> bool:
    if not stage.enabled:
        return False
    if depth_index < stage.min_depth:
        return False
    # prompt_only stages run only when assembling the prompt; ordinary stages
    # run in every context (prompt and display).
    if stage.prompt_only and not prompt_only:
        return False
    return True


def apply_regex_stage(
    texts: list[str],
    *,
    depth_index: int,
    prompt_only: bool,
    regexes: Sequence[StageRegex] | None = None,
    surfaces: Mapping[str, object] | None = None,
) -> list[str]:
    """Apply each enabled preset pattern to every text in ``texts``.

    A pattern applies when ``enabled`` and ``depth_index >= stage.min_depth`` and
    its ``prompt_only`` gate is satisfied by the call context. ``g`` semantics
    mean every occurrence in each text is replaced. Texts that match nothing are
    returned byte-identical (``re.sub`` returns the original text unchanged).

    D27: ``regexes`` names the stage set explicitly; otherwise ``surfaces``
    supplies the preset's full-replace list, falling back to
    :data:`PRESET_REGEXES`.
    """

    stage_set = regexes if regexes is not None else regexes_from_surfaces(surfaces)
    out = list(texts)
    for stage in stage_set:
        if not _applies(stage, depth_index=depth_index, prompt_only=prompt_only):
            continue
        pattern = compile_stage(stage)
        replacement = _convert_replacement(stage.replace)
        out = [pattern.sub(replacement, text) for text in out]
    return out
