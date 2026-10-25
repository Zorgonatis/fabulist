"""W2.6 Dress Rehearsal validator — flags, never rewrites.

The engine-side counterpart to the preset's Phase V *Dress Rehearsal*: after the
narrator returns a turn, this pass inspects the **prose segments** against the
preset's anti-slop / anti-parroting / no-user-control directives and *flags*
problems. It never rewrites, mutates, or edits the narrative — the event log is
the single source of truth (D9), so a flag is advisory data the dispatcher may
later promote into a revision event (D10).

Four checks, each independently toggleable through :class:`ValidatorConfig`:

* ``parrot``     — 4-word shingle overlap between the user message and prose
                   (>``shingle_min`` distinct shingles).
* ``slop``       — word-boundary matches against the canonical banned-phrase list
                   (``prompts/factory-defaults.json`` ``anti_slop``; issue #50).
* ``control``    — prose that cedes narrative control back to the user in
                   out-of-character meta language.
* ``echo_start`` — this turn's prose opens with the same first
                   ``first_words_count`` words as the previous turn
                   (``prior_first_words``), case-insensitively.

Only segments whose ``kind`` is ``"prose"`` are inspected; the openui ``dsl``
channel is ignored by every prose check. Everything here is synchronous, pure,
and model-free.

Every flag also carries an ``anchor`` — the EXACT prose substring that
triggered it (a window around the match, clipped with ``…`` only at the
boundaries) — and a ``fix`` line naming the corrective action. The narrator's
in-node repair loop renders both: a flag that cannot say *where* in the prose
the problem is and *what to do* leaves the model guessing (live session
98e092bf: 14 identical rejected patches, zero progress).

Scoring: a clean turn scores ``100``. Each flag deducts a fixed weight by
severity (:data:`SEVERITY_WEIGHT`), clamped to ``0``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

__all__ = [
    "ANCHOR_CONTEXT_CHARS",
    "CHECK_CONTROL",
    "CHECK_ECHO_START",
    "CHECK_FIX",
    "CHECK_PARROT",
    "CHECK_SLOP",
    "DEFAULT_SLOP_PATTERNS",
    "PRESET_ANTISLOP_LINE",
    "PRESET_SOURCE",
    "SEVERITY_WEIGHT",
    "SlopPattern",
    "ValidatorConfig",
    "ValidatorFlag",
    "ValidatorReport",
    "anchor_for_span",
    "current_slop_patterns",
    "derive_slop_patterns",
    "first_words",
    "prose_segments",
    "shingles",
    "validate_narrative",
    "words",
]

# ---------------------------------------------------------------------------
# preset provenance
# ---------------------------------------------------------------------------

#: Spec-of-record preset the anti-slop list is distilled from.
PRESET_SOURCE = "Stabs-GLM5.1-Directives-v4.0.0.json"

#: Line holding the ``Writing Guidelines (Anti-Slop)`` content in the preset.
PRESET_ANTISLOP_LINE = 207

#: Check identifiers (also the keys accepted by :attr:`ValidatorConfig.enabled`).
CHECK_PARROT = "parrot"
CHECK_SLOP = "slop"
CHECK_CONTROL = "control"
CHECK_ECHO_START = "echo_start"

#: Score deduction per flag severity.
SEVERITY_WEIGHT: dict[str, int] = {"warn": 15, "info": 5}

#: Characters of prose kept on each side of a flag's anchor before it is clipped.
ANCHOR_CONTEXT_CHARS = 80

#: The corrective action each check teaches, keyed by check id. A flag names the
#: ACTION and the shape that clears it; the repair loop never asks the model to
#: infer the fix from a diagnostic alone.
CHECK_FIX: dict[str, str] = {
    CHECK_PARROT: "rephrase the echoed span in your own words — do not reuse the wording",
    CHECK_SLOP: "replace the phrase with fresh wording — do not repeat it",
    CHECK_CONTROL: "remove the out-of-character meta line and end the turn on a beat of the scene",
    CHECK_ECHO_START: "open the turn with different wording than the previous turn",
}

#: Case-insensitive, punctuation-stripped word matcher.
_WORD_RE = re.compile(r"[a-z0-9']+")

#: Default shingle width for the parrot n-gram overlap check.
DEFAULT_SHINGLE_SIZE = 4

#: Default word count for the echo-start check.
DEFAULT_FIRST_WORDS = 6


# ---------------------------------------------------------------------------
# data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlopPattern:
    """One banned-phrase rule, citing the preset line it was distilled from."""

    rule_id: str
    regex: str
    source: str = "config"
    severity: str = "warn"
    _compiled: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_compiled", re.compile(self.regex, re.IGNORECASE))

    @property
    def compiled(self) -> re.Pattern[str]:
        return self._compiled


def derive_slop_patterns(
    anti_slop: object | None = None,
) -> tuple[SlopPattern, ...]:
    """Consumer A (issue #50): derive the validator's rules from ONE artifact list.

    The canonical list lives in ``prompts/factory-defaults.json`` (its ``anti_slop``
    section, :mod:`fabulist.antislop`); this function turns every enabled entry into
    a :class:`SlopPattern`. An entry is a literal phrase unless it carries the
    optional ``pattern`` escape hatch (the 34 rules the engine used to hard-code
    are the only entries in the shipped list that do), so owner text is data.

    ``anti_slop`` is injectable for tests; ``None`` loads the artifact through the
    mtime-cached reader, so an owner edit reaches the next validation without a
    restart. A missing/unreadable artifact yields no patterns (there is no code
    list left to resurrect).
    """
    from fabulist.antislop import load_anti_slop

    listing = anti_slop if anti_slop is not None else load_anti_slop()
    entries = getattr(listing, "enabled_entries", None)
    rows = entries() if callable(entries) else getattr(listing, "entries", ())
    return tuple(
        SlopPattern(
            rule_id=entry.id,
            regex=entry.pattern,
            source=f"{PRESET_SOURCE}:{PRESET_ANTISLOP_LINE} (Anti-Slop / {entry.section})",
            severity=entry.severity,
        )
        for entry in rows
    )


#: The derived default list, snapshotted at import for callers/tests that read a
#: constant. Runtime validation goes through :func:`current_slop_patterns` (which
#: re-reads the mtime-cached artifact), so this snapshot never becomes a second
#: source of truth.
DEFAULT_SLOP_PATTERNS: tuple[SlopPattern, ...] = derive_slop_patterns()


def current_slop_patterns() -> tuple[SlopPattern, ...]:
    """The artifact's current rules (owner edits apply on the next validation)."""
    return derive_slop_patterns()



# ---------------------------------------------------------------------------
# control-ceding markers (preset: No User Control ⛓️ + Phase V.Dress Rehearsal)
# ---------------------------------------------------------------------------

_CONTROL_CEDING = r"\b(?:" + "|".join(
    (
        r"as you wish,? the story obeys",
        r"the narrative bends? to your will",
        r"the story (?:bends?|yields?|obeys) to your (?:will|command|whim)",
        r"i (?:will|shall|must) (?:follow|obey) your (?:lead|command|will|direction|cues)",
        r"i take my (?:cues|orders) from you",
        r"your will be done",
        r"command me",
        r"what (?:is|are) your (?:command|commands|orders|wishes)",
        r"i (?:exist|am here) only to (?:serve|follow|obey) you",
        r"the scene is yours to (?:command|control|direct)",
        r"the story awaits your (?:command|word|lead)",
    )
) + r")\b"

_CONTROL_META = r"\b(?:" + "|".join(
    (
        r"you (?:are|remain) in control(?: of (?:the|this) (?:scene|story|narrative))?",
        r"you (?:control|command|direct) (?:the|this) (?:scene|story|narrative)",
        r"it'?s up to you(?: to (?:decide|choose|control))?",
        r"(?:tell|show) me what you (?:want|wish|desire)(?: to happen| to do| next)?",
        r"whatever you (?:command|wish|desire|want)",
        r"you (?:decide|choose|dictate) (?:what|how|the|which)",
        r"your (?:turn|move|call),? (?:now|then)?",
        r"how (?:do|would) you (?:wish|like) (?:to proceed|the scene to)",
        r"the choice is yours",
        r"you (?:may|can|must|should) (?:dictate|decide|control|direct|choose)",
    )
) + r")\b"

CONTROL_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("control.ceding", re.compile(_CONTROL_CEDING, re.IGNORECASE)),
    ("control.meta_instruction", re.compile(_CONTROL_META, re.IGNORECASE)),
)


# ---------------------------------------------------------------------------
# config + report types
# ---------------------------------------------------------------------------


@dataclass
class ValidatorConfig:
    """Thresholds and toggles for the Dress Rehearsal pass.

    ``shingle_min`` is the overlap count above which PARROT fires (default ``1``
    means *two or more* distinct shared 4-shingles). ``slop_list`` replaces the
    distilled default list when provided (entries may be :class:`SlopPattern`,
    regex strings, or mappings with ``rule_id``/``regex``/``source``/
    ``severity``). ``enabled`` maps a check id to ``bool``; omitted ids default
    to enabled. ``prior_first_words`` feeds the ECHO-START comparison.
    """

    shingle_min: int = 1
    shingle_size: int = DEFAULT_SHINGLE_SIZE
    slop_list: Sequence[SlopPattern | str | Mapping] | None = None
    enabled: Mapping[str, bool] | None = None
    prior_first_words: str | None = None
    first_words_count: int = DEFAULT_FIRST_WORDS

    def check_enabled(self, check: str) -> bool:
        if not self.enabled:
            return True
        return bool(self.enabled.get(check, True))


@dataclass(frozen=True)
class ValidatorFlag:
    """One advisory finding. Flags never mutate the narrative.

    ``anchor`` is the EXACT prose substring that triggered the finding — the
    match in a small window of context, byte-exact in the middle and clipped
    with ``…`` only where the window was cut (:func:`anchor_for_span`). It is the
    text a repair should patch, so the model never has to guess the wording.
    ``fix`` names the corrective action for this check (:data:`CHECK_FIX`).
    """

    rule_id: str
    check: str
    severity: str
    evidence: str
    source: str = ""
    segment_index: int | None = None
    message: str = ""
    anchor: str = ""
    fix: str = ""

    def as_dict(self) -> dict:
        return {
            "rule_id": self.rule_id,
            "check": self.check,
            "severity": self.severity,
            "evidence": self.evidence,
            "source": self.source,
            "segment_index": self.segment_index,
            "message": self.message,
            "anchor": self.anchor,
            "fix": self.fix,
        }


@dataclass
class ValidatorReport:
    """Outcome of one Dress Rehearsal pass: ``100`` is clean."""

    flags: list[ValidatorFlag] = field(default_factory=list)
    score: int = 100

    @property
    def clean(self) -> bool:
        return not self.flags

    def by_check(self) -> dict[str, list[ValidatorFlag]]:
        out: dict[str, list[ValidatorFlag]] = {}
        for flag in self.flags:
            out.setdefault(flag.check, []).append(flag)
        return out

    def as_dict(self) -> dict:
        return {"score": self.score, "flags": [f.as_dict() for f in self.flags]}


# ---------------------------------------------------------------------------
# text helpers
# ---------------------------------------------------------------------------


def words(text: str) -> list[str]:
    """Case-normalized, punctuation-stripped word tokens."""
    return _WORD_RE.findall((text or "").lower())


def _word_spans(text: str) -> list[tuple[str, int, int]]:
    """``(normalized token, start, end)`` for every word in ``text``.

    The same tokenization :func:`words` applies, carrying the character span so a
    match can be turned back into the exact prose it came from.
    """
    return [
        (match.group(0).lower(), match.start(), match.end())
        for match in _WORD_RE.finditer(text or "")
    ]


def anchor_for_span(
    text: str, start: int, end: int, *, context: int = ANCHOR_CONTEXT_CHARS
) -> str:
    """The prose around ``text[start:end]``, clipped with boundary ``…`` markers.

    The middle is byte-exact prose (an unclipped anchor is a literal substring of
    ``text``). Where the window was cut, ``…`` marks the cut and the boundaries
    are nudged to whitespace, so an anchor never starts or ends mid-word — the
    model can copy it straight back as a ``find``. ``start``/``end`` always land
    inside the returned text.
    """
    lo = max(0, start - context)
    if lo > 0:
        # Start the anchor at a word boundary: advance to just after whitespace.
        while lo < start and not text[lo - 1].isspace():
            lo += 1
    hi = min(len(text), end + context)
    if hi < len(text):
        # End it at a word boundary: retreat to just before whitespace.
        while hi > end and not text[hi].isspace():
            hi -= 1
    return ("…" if lo > 0 else "") + text[lo:hi] + ("…" if hi < len(text) else "")


def shingles(tokens: Sequence[str], size: int = DEFAULT_SHINGLE_SIZE) -> set[str]:
    """Set of overlapping ``size``-word shingles (``size`` >= 1)."""
    size = max(1, int(size))
    if len(tokens) < size:
        return set()
    return {" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)}


def first_words(text: str, count: int = DEFAULT_FIRST_WORDS) -> list[str]:
    """The leading ``count`` normalized words of ``text``."""
    return words(text)[: max(0, int(count))]


def prose_segments(
    segments: Sequence[Mapping] | None,
) -> list[tuple[int, str]]:
    """``(index, text)`` for every non-empty ``kind == "prose"`` segment.

    DSL (and unknown-kind) segments are excluded from all prose checks.
    """
    out: list[tuple[int, str]] = []
    for idx, seg in enumerate(segments or []):
        if not isinstance(seg, Mapping) or seg.get("kind") != "prose":
            continue
        text = seg.get("text")
        if isinstance(text, str) and text.strip():
            out.append((idx, text))
    return out


# ---------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------


def _shingle_span(text: str, shared: set[str], size: int) -> tuple[int, int] | None:
    """Char span of the FIRST contiguous run of shared shingles in ``text``.

    The parrot check works on normalized words; this maps its finding back onto
    the exact prose so the flag can hand the model a patchable anchor.
    """
    spans = _word_spans(text)
    first: int | None = None
    last: int | None = None
    for pos in range(len(spans) - size + 1):
        phrase = " ".join(token for token, _, _ in spans[pos : pos + size])
        if phrase in shared:
            if first is None:
                first = spans[pos][1]
            last = spans[pos + size - 1][2]
        elif first is not None:
            break  # the anchor is the first run, not the union of every run
    if first is None or last is None:
        return None
    return first, last


def _check_parrot(
    user_msg: str, prose: list[tuple[int, str]], cfg: ValidatorConfig
) -> list[ValidatorFlag]:
    user_shingles = shingles(words(user_msg), cfg.shingle_size)
    if not user_shingles:
        return []
    shared: set[str] = set()
    first_index: int | None = None
    anchor = ""
    for idx, text in prose:
        overlap = shingles(words(text), cfg.shingle_size) & user_shingles
        if not overlap:
            continue
        if first_index is None:
            first_index = idx
            span = _shingle_span(text, overlap, cfg.shingle_size)
            if span is not None:
                anchor = anchor_for_span(text, *span)
        shared |= overlap
    if len(shared) <= cfg.shingle_min:
        return []
    quoted = ", ".join(f'"{s}"' for s in sorted(shared))
    return [
        ValidatorFlag(
            rule_id="parrot",
            check=CHECK_PARROT,
            severity="warn",
            evidence=quoted,
            source=PRESET_SOURCE,
            segment_index=first_index,
            message=f"{len(shared)} distinct {cfg.shingle_size}-word shingle(s) reused from the user message",
            anchor=anchor,
            fix=CHECK_FIX[CHECK_PARROT],
        )
    ]


def _resolve_slop(
    slop_list: Sequence[SlopPattern | str | Mapping] | None,
) -> tuple[SlopPattern, ...]:
    if slop_list is None:
        return current_slop_patterns()
    resolved: list[SlopPattern] = []
    for item in slop_list:
        if isinstance(item, SlopPattern):
            resolved.append(item)
        elif isinstance(item, str):
            resolved.append(SlopPattern(rule_id=f"custom:{item}", regex=item))
        elif isinstance(item, Mapping):
            resolved.append(
                SlopPattern(
                    rule_id=str(item.get("rule_id") or item.get("id") or "custom"),
                    regex=str(item["regex"]),
                    source=str(item.get("source", "config")),
                    severity=str(item.get("severity", "warn")),
                )
            )
        else:  # pragma: no cover - defensive
            raise TypeError(f"unsupported slop_list entry: {item!r}")
    return tuple(resolved)


def _check_slop(
    prose: list[tuple[int, str]], cfg: ValidatorConfig
) -> list[ValidatorFlag]:
    flags: list[ValidatorFlag] = []
    for pattern in _resolve_slop(cfg.slop_list):
        hits: list[str] = []
        first_index: int | None = None
        anchor = ""
        for idx, text in prose:
            for match in pattern.compiled.finditer(text):
                hits.append(match.group(0))
                if first_index is None:
                    first_index = idx
                    anchor = anchor_for_span(text, *match.span())
        if not hits:
            continue
        unique = list(dict.fromkeys(hits))
        evidence = "; ".join(f'"{h}"' for h in unique[:5])
        flags.append(
            ValidatorFlag(
                rule_id=pattern.rule_id,
                check=CHECK_SLOP,
                severity=pattern.severity,
                evidence=evidence,
                source=pattern.source,
                segment_index=first_index,
                message=f"banned phrase ({len(unique)} match(es))",
                anchor=anchor,
                fix=CHECK_FIX[CHECK_SLOP],
            )
        )
    return flags


def _check_control(prose: list[tuple[int, str]]) -> list[ValidatorFlag]:
    flags: list[ValidatorFlag] = []
    for rule_id, pattern in CONTROL_MARKERS:
        hits: list[str] = []
        first_index: int | None = None
        anchor = ""
        for idx, text in prose:
            for match in pattern.finditer(text):
                hits.append(match.group(0))
                if first_index is None:
                    first_index = idx
                    anchor = anchor_for_span(text, *match.span())
        if not hits:
            continue
        unique = list(dict.fromkeys(hits))
        flags.append(
            ValidatorFlag(
                rule_id=rule_id,
                check=CHECK_CONTROL,
                severity="warn",
                evidence="; ".join(f'"{h}"' for h in unique[:5]),
                source=f"{PRESET_SOURCE} (No User Control)",
                segment_index=first_index,
                message="prose cedes narrative control to the user (out-of-character)",
                anchor=anchor,
                fix=CHECK_FIX[CHECK_CONTROL],
            )
        )
    return flags


def _check_echo_start(
    prose: list[tuple[int, str]], cfg: ValidatorConfig
) -> list[ValidatorFlag]:
    prior = cfg.prior_first_words
    if not prior:
        return []
    count = max(1, int(cfg.first_words_count))
    current = first_words("\n".join(text for _, text in prose), count)
    previous = first_words(prior, count)
    if len(current) < count or len(previous) < count:
        return []
    if current != previous:
        return []
    prefix = " ".join(current)
    # The echoed words are the OPENING of the turn, so the anchor is the span of
    # the first `count` words in the first prose segment's own text.
    first_index, first_text = prose[0]
    spans = _word_spans(first_text)
    anchor = (
        anchor_for_span(first_text, spans[0][1], spans[count - 1][2])
        if len(spans) >= count
        else ""
    )
    return [
        ValidatorFlag(
            rule_id="echo_start",
            check=CHECK_ECHO_START,
            severity="info",
            evidence=f'"{prefix}"',
            source="validator:turns",
            message=f"turn opens with the previous turn's first {count} words",
            segment_index=first_index,
            anchor=anchor,
            fix=CHECK_FIX[CHECK_ECHO_START],
        )
    ]


def _score(flags: Sequence[ValidatorFlag]) -> int:
    deduction = sum(SEVERITY_WEIGHT.get(f.severity, 0) for f in flags)
    return max(0, 100 - deduction)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


def validate_narrative(
    user_msg: str,
    segments: list[dict],
    config: ValidatorConfig | None = None,
) -> ValidatorReport:
    """Run the Dress Rehearsal pass over one turn's segments.

    Pure and synchronous: inspects prose segments against the parrot, slop,
    control, and echo-start checks, returns a :class:`ValidatorReport` of
    advisory flags (never edits the input). ``config`` carries thresholds and
    per-check toggles; a clean turn scores ``100`` with no flags.
    """
    cfg = config or ValidatorConfig()
    prose = prose_segments(segments)

    flags: list[ValidatorFlag] = []
    if cfg.check_enabled(CHECK_PARROT):
        flags.extend(_check_parrot(user_msg, prose, cfg))
    if cfg.check_enabled(CHECK_SLOP):
        flags.extend(_check_slop(prose, cfg))
    if cfg.check_enabled(CHECK_CONTROL):
        flags.extend(_check_control(prose))
    if cfg.check_enabled(CHECK_ECHO_START):
        flags.extend(_check_echo_start(prose, cfg))

    return ValidatorReport(flags=flags, score=_score(flags))
