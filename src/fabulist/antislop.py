"""The ONE canonical anti-slop phrase list (issue #50).

The owner-approved consolidated design makes the factory artifact the list's
home: ``prompts/factory-defaults.json`` carries an ``anti_slop`` section that
the studio/factory commit path can write (D26). Three consumers DERIVE from it,
so the list is authored once and every layer agrees:

* **Consumer A — the validator's free catch-and-repair.** Entries become
  literal regexes by default (the phrase is escaped, so a user entry is data,
  never a pattern). An entry may carry an optional ``pattern`` escape hatch when
  it needs real regex syntax — the 34 rules the engine used to hard-code are the
  only entries that do.
* **Consumer B — prevention in the narrator prompt.** The factory's
  ``anti_slop`` directive carries the ``{{slop_phrases}}`` token; the compiler
  renders it from this same list (the full owner list, in the stable prefix).
* **Consumer C — the d1 semantic net.** :mod:`fabulist.decision_queries`
  derives its questions from the entries with ``skip_d1`` false, so phrases the
  regex already catches need not be asked of the model.

Artifact shape (owner-editable, unknown keys preserved)::

    "anti_slop": {
      "version": 1,
      "source": "Stabs-GLM5.1-Directives-v4.0.0.json prompts[15] ...",
      "sections": [
        {"name": "Emotion", "entries": [
            {"phrase": "Shivers down spine", "id": "shivers_down",
             "pattern": "\\\\bshivers? down\\\\b", "skip_d1": true},
            "arousal pooling"
        ]},
        ...
      ]
    }

An entry is a plain string (a literal phrase) or an object with ``phrase`` plus
any of ``id`` / ``pattern`` / ``severity`` / ``skip_d1`` / ``enabled``. A string
is interpreted literally and escaped; only an explicit ``pattern`` is regex
syntax. Absent/older artifacts (no ``anti_slop`` key) read as an empty list,
which is a safe degradation: no code list is retained as a second source.

Reads never raise: a missing/unreadable/invalid artifact yields the empty list.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "ANTI_SLOP_SECTION",
    "ANTI_SLOP_TOKEN",
    "AntiSlopList",
    "SlopEntry",
    "DEFAULT_ARTIFACT_RELPATH",
    "SEVERITIES",
    "artifact_path",
    "load_anti_slop",
    "normalize_anti_slop",
    "parse_anti_slop",
    "render_prompt_list",
    "reset_cache",
    "slugify",
]

#: Top-level factory-artifact key carrying the canonical list.
ANTI_SLOP_SECTION = "anti_slop"

#: The directive-prompt token Consumer B renders from this list.
ANTI_SLOP_TOKEN = "{{slop_phrases}}"

#: Repo-relative artifact location (mirrors ``prompts.factory``; duplicated here
#: only as a fallback when the prompt factory cannot be imported).
DEFAULT_ARTIFACT_RELPATH: Path = Path("prompts") / "factory-defaults.json"

#: Severities a phrase may carry (parity with the validator's weights).
SEVERITIES = ("warn", "info")

#: Bumped when the artifact section's SHAPE changes (not when phrases change).
LIST_VERSION = 1


def slugify(phrase: str) -> str:
    """A stable id for a phrase: lowercase, non-alphanumerics -> ``_``."""
    return re.sub(r"[^a-z0-9]+", "_", phrase.lower()).strip("_")


def _literal_pattern(phrase: str) -> str:
    """A literal phrase as a case-insensitive, whole-word-ish regex.

    ``re.escape`` makes user text data. ``(?<!\\w)`` / ``(?!\\w)`` reproduce the
    word-boundary intent of the retired hard-coded rules without failing on a
    phrase that starts/ends with punctuation.
    """
    return rf"(?<!\w){re.escape(phrase.strip())}(?!\w)"


@dataclass(frozen=True)
class SlopEntry:
    """One canonical banned phrase, derived for the consumers."""

    id: str
    phrase: str
    section: str
    pattern: str
    severity: str = "warn"
    skip_d1: bool = False
    enabled: bool = True
    literal: bool = True

    @property
    def compiled(self) -> re.Pattern[str]:
        return re.compile(self.pattern, re.IGNORECASE)


@dataclass(frozen=True)
class AntiSlopList:
    """The normalized list: metadata plus a flat, ordered entry tuple."""

    version: int
    source: str
    sections: tuple[tuple[str, tuple[SlopEntry, ...]], ...]

    @property
    def entries(self) -> tuple[SlopEntry, ...]:
        return tuple(entry for _name, rows in self.sections for entry in rows)

    def enabled_entries(self) -> tuple[SlopEntry, ...]:
        return tuple(entry for entry in self.entries if entry.enabled)

    def d1_entries(self) -> tuple[SlopEntry, ...]:
        """Entries whose semantics the model layer should ask (skip honoured)."""
        return tuple(
            entry for entry in self.entries if entry.enabled and not entry.skip_d1
        )


_EMPTY = AntiSlopList(version=0, source="", sections=())


def artifact_path(path: Path | str | None = None) -> Path:
    """Resolve the factory artifact path (delegates to the prompt factory)."""
    if path is not None:
        return Path(path)
    try:
        from fabulist.prompts import factory

        return factory.artifact_path()
    except Exception:  # pragma: no cover - import-cycle/defensive fallback
        return Path(__file__).resolve().parents[2] / DEFAULT_ARTIFACT_RELPATH


def _split_sections(raw: Any) -> list[tuple[str, list[Any]]]:
    """Accept every tolerant input shape as ``[(section_name, entries)]``."""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        return [("General", list(raw))]
    if not isinstance(raw, Mapping):
        return []
    sections = raw.get("sections")
    if isinstance(sections, (list, tuple)):
        out: list[tuple[str, list[Any]]] = []
        for section in sections:
            if isinstance(section, Mapping):
                name = str(section.get("name") or "").strip()
                entries = section.get("entries")
                if isinstance(entries, (list, tuple)):
                    out.append((name, list(entries)))
            elif isinstance(section, (list, tuple)):
                out.append(("", list(section)))
        return out
    entries = raw.get("entries")
    if isinstance(entries, (list, tuple)):
        return [(str(raw.get("name") or "General"), list(entries))]
    return []


def _entry_record(raw: Any) -> dict[str, Any] | None:
    """Normalize one string/object entry; ``None`` when it carries no phrase."""
    if isinstance(raw, str):
        phrase = raw.strip()
        if not phrase:
            return None
        return {
            "phrase": phrase,
            "id": slugify(phrase),
            "literal": True,
        }
    if not isinstance(raw, Mapping):
        return None
    phrase = raw.get("phrase")
    if not isinstance(phrase, str) or not phrase.strip():
        return None
    record: dict[str, Any] = {"phrase": phrase.strip()}
    ident = raw.get("id")
    record["id"] = str(ident).strip() if ident else slugify(phrase)
    pattern = raw.get("pattern")
    if isinstance(pattern, str) and pattern.strip():
        record["pattern"] = pattern
        record["literal"] = False
    else:
        record["literal"] = True
    severity = raw.get("severity")
    if isinstance(severity, str) and severity.strip() in SEVERITIES:
        record["severity"] = severity.strip()
    skip = raw.get("skip_d1")
    if isinstance(skip, bool):
        record["skip_d1"] = skip
    enabled = raw.get("enabled")
    if isinstance(enabled, bool):
        record["enabled"] = enabled
    return record


def parse_anti_slop(raw: Any) -> AntiSlopList:
    """Parse an artifact/preset ``anti_slop`` block into the normalized list.

    Tolerant by design: strings are literal phrases, objects may override the
    derived id/pattern/severity/skip flag, invalid rows are dropped, and a
    duplicate id is suffixed deterministically (``_2``, ``_3`` …) so the
    validator's rule ids stay unique without losing an owner entry.
    """
    version = LIST_VERSION
    source = ""
    if isinstance(raw, Mapping):
        raw_version = raw.get("version")
        if isinstance(raw_version, int) and not isinstance(raw_version, bool):
            version = raw_version
        raw_source = raw.get("source")
        if isinstance(raw_source, str):
            source = raw_source
    sections: list[tuple[str, tuple[SlopEntry, ...]]] = []
    seen: set[str] = set()
    for name, rows in _split_sections(raw):
        parsed: list[SlopEntry] = []
        for item in rows:
            record = _entry_record(item)
            if record is None:
                continue
            phrase = record["phrase"]
            ident = record["id"] or slugify(phrase)
            if ident in seen:
                base = ident
                suffix = 2
                while ident in seen:
                    ident = f"{base}_{suffix}"
                    suffix += 1
            seen.add(ident)
            pattern = record.get("pattern") or _literal_pattern(phrase)
            try:
                re.compile(pattern, re.IGNORECASE)
            except re.error:
                # A typo in an owner pattern must not break the turn: fall back
                # to the safe literal form (reads tolerate; D9 data, not code).
                pattern = _literal_pattern(phrase)
                record["literal"] = True
            parsed.append(
                SlopEntry(
                    id=ident,
                    phrase=phrase,
                    section=name,
                    pattern=pattern,
                    severity=str(record.get("severity", "warn")),
                    skip_d1=bool(record.get("skip_d1", False)),
                    enabled=bool(record.get("enabled", True)),
                    literal=bool(record.get("literal", True)),
                )
            )
        if parsed:
            sections.append((name, tuple(parsed)))
    return AntiSlopList(version=version, source=source, sections=tuple(sections))


def normalize_anti_slop(raw: Any) -> dict[str, Any]:
    """The canonical storable shape (for preset/artifact round-trips).

    Strings stay strings and objects keep only their declared optional keys, so
    an uncontroversial list keeps a clean, hand-editable diff. An absent/empty
    value returns ``{}`` — which the preset/artifact writers omit entirely
    (the same "absent means inherit/none" discipline as ``directives``).
    """
    if raw is None:
        return {}
    sections: list[dict[str, Any]] = []
    for name, rows in _split_sections(raw):
        entries: list[Any] = []
        for item in rows:
            record = _entry_record(item)
            if record is None:
                continue
            if record["literal"] and not any(
                key in record
                for key in ("pattern", "severity", "skip_d1", "enabled")
            ) and isinstance(item, str):
                entries.append(record["phrase"])
                continue
            out: dict[str, Any] = {"phrase": record["phrase"]}
            if record.get("id") and record["id"] != slugify(record["phrase"]):
                out["id"] = record["id"]
            elif not isinstance(item, str) and item.get("id"):
                out["id"] = str(item.get("id"))
            if not record["literal"]:
                out["pattern"] = record["pattern"]
            if record.get("severity"):
                out["severity"] = record["severity"]
            if "skip_d1" in record:
                out["skip_d1"] = record["skip_d1"]
            if "enabled" in record:
                out["enabled"] = record["enabled"]
            entries.append(out)
        if entries:
            section: dict[str, Any] = {}
            if name:
                section["name"] = name
            section["entries"] = entries
            sections.append(section)
    if not sections:
        return {}
    out: dict[str, Any] = {}
    if isinstance(raw, Mapping):
        raw_version = raw.get("version")
        if isinstance(raw_version, int) and not isinstance(raw_version, bool):
            out["version"] = raw_version
        raw_source = raw.get("source")
        if isinstance(raw_source, str) and raw_source.strip():
            out["source"] = raw_source
    out.setdefault("version", LIST_VERSION)
    out["sections"] = sections
    return out


# -- loading (cached by file identity) ---------------------------------------

_CACHE: dict[tuple[str, int, int], AntiSlopList] = {}


def reset_cache() -> None:
    """Drop the parsed-artifact cache (tests that rewrite the artifact)."""
    _CACHE.clear()


def load_anti_slop(path: Path | str | None = None) -> AntiSlopList:
    """Load the canonical list from the factory artifact.

    Missing, unreadable, invalid-JSON, or section-less artifacts return the
    empty list (consumers then do nothing — there is no code list to resurrect).
    A successful parse is cached by ``(path, mtime_ns, size)`` so an owner edit
    reaches the next turn without a restart.
    """
    resolved = artifact_path(path)
    try:
        stat = resolved.stat()
    except OSError:
        return _EMPTY
    key = (str(resolved), stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _EMPTY
    if not isinstance(raw, Mapping):
        return _EMPTY
    listing = parse_anti_slop(raw.get(ANTI_SLOP_SECTION))
    _CACHE[key] = listing
    # Bound the cache: a long edit session must not keep every stat.
    if len(_CACHE) > 16:
        for old_key in list(_CACHE)[:-8]:
            _CACHE.pop(old_key, None)
    return listing


def render_prompt_list(listing: AntiSlopList | None = None) -> str:
    """Render the owner list for the narrator directive (Consumer B).

    One line per section, phrases kept verbatim and separated by ``; ``. Disabled
    entries are omitted; an empty list renders the empty string (the directive's
    own prose still stands).
    """
    current = listing if listing is not None else load_anti_slop()
    lines: list[str] = []
    for name, rows in current.sections:
        phrases = [entry.phrase for entry in rows if entry.enabled]
        if not phrases:
            continue
        label = f"{name}: " if name else ""
        lines.append(f"- {label}" + "; ".join(phrases))
    return "\n".join(lines)
