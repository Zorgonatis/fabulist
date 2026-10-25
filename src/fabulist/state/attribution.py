"""Speaker attribution (W9.28) — the model-emitted colour-markup contract.

Attribution is its own post-turn step: read the narrator's prose and report, for
each spoken line, who said it. It is deliberately NOT an extractor task (owner
steer 2026-10-10): the extractor's contract stays pure state-deltas.

The contract (W9.28 supersedes the D75 deterministic detector)
--------------------------------------------------------------
The narrator writes each spoken line inside the speaking character's palette
colour and the engine PARSES that markup:

    <span style="color:#e06c75">"You're late."</span>

:func:`parse_attribution` reports a row only for a quote that sits inside a
colour span resolving to a palette entry — the colour IS the attribution.
**Strict omission** (owner ruling Q2 = B): an untagged spoken line is
unattributed, never inferred from a cue, a speech verb, a pronoun or the
paragraph before it. The miss is surfaced instead: :func:`attribution_missing_flags`
hands the narrate repair loop an ``attribution_missing`` flag anchored on the
quoted line, so the model fixes its own omission in place. There is no
heuristic fallback anywhere in this module; deleting the D75 tiers is what makes
that true rather than a promise.

The palette (:func:`palette_for_turn`) is the SPEAKING cast's colour rows — the
engine's own registry (:mod:`fabulist.api.cast_api`), one entry per member who
can speak, rendered into the narrator's dynamic tail by the palette block
(``fabulist.prompts.palette``, W2). It describes the CAST, not the instant
presence answer, because the post-turn attribution pass runs before the turn's
own presence evidence means anything (see :func:`palette_for_turn`). Colours are
still assigned by the engine registry at first sighting (D25); the model never
picks one, it only reuses the one it was shown.

Parsing tolerance (D5)
----------------------
A colour VALUE resolves against the palette when it is the entry's hex
(``#e06c75`` / ``#E06C75`` / ``#rgb``), a ``rgb()``/``rgba()`` triple (alpha
dropped), the palette LABEL (``red`` — case-insensitive equality with
:attr:`PaletteEntry.name`, never a CSS-spec lookup), or within
:data:`RGB_TOLERANCE` Manhattan distance of the entry's RGB. One candidate
attributes the line; zero means missing. A value within tolerance of two
DIFFERENT members is ambiguous and resolves to nobody — the cast module's
"never merge" rule, applied at the value level.

The colour context is BOUNDED to the paragraph (D5): a blank line closes every
open span, and a segment never inherits an open span from the segment before it.
A dangling open therefore makes the following quotes MISSING instead of
silently attributing the rest of the turn to the last speaker.

Masking (R2 P0-1)
-----------------
The quote scan matches the quotes ``style="color:#e06c75"`` is written with, so
colour tags are blanked to SAME-LENGTH runs before any scan
(:func:`_mask_tags`). Offsets are unchanged by the mask, so a span still indexes
the narrative the caller handed in, and the wrapped line is the only quote the
scan sees.

The other half of strict omission: colour with no quote (W9.28 D3)
---------------------------------------------------------------
The parser can only report a quote it finds. A span wrapping speech the model
LEFT UNQUOTED — ``<span style="color:#e06c75">Get out.</span>`` — therefore
produced no row AND no missing flag, so the repair loop never saw it and the
attribution presence rung starved. The same shape appears when the model
mis-colours ordinary narration. Both are deterministic, so both are reported:
:func:`attribution_unquoted_flags` flags every complete colour span (palette
colour, non-blank content) that carries no quoted line, anchored on the prose
inside the span and naming the hue the span used.

Not every quoted span is speech (the W9.22 live false positive)
--------------------------------------------------------------
Prose quotes names, brands and labels inline — ``a cloying, artificial scent
Hobbs calls "Spring Rain"``, ``a faded neon "OPEN" sign``. Such a span is a
CITATION inside the narrator's sentence, not an utterance, so it is neither
attributed nor flagged (:func:`_is_inline_citation`). The test applies to
UNWRAPPED quotes only, to decide whether the engine demands a colour; a span the
model did wrap is attributed as written.

The mechanical half of the repair (issue 31)
--------------------------------------------
Most missing spans are not judgements: the prose NAMES the speaker in a speech
tag beside the quote (``"You're late," Aria said.``), so wrapping it is
deterministic text editing rather than a model round trip.
:func:`wrap_attributed_quotes` writes those spans itself — palette display name,
tag adjacent to the quote, one name only — and hands the narrate repair loop
only what no tag names (a pronoun tag, alternating dialogue, a lone quote). It
never guesses: a pronoun or a second name resolves nobody, exactly as strict
omission requires.

Contract of the persisted row
-----------------------------
Rows are ``{"speaker", "quote", "speaker_id"}`` in prose order — the W7 row shape
every downstream consumer already reads (``graph/nodes._run_attribution``,
``state.presence.active_members``' attribution rung, ``GET /attributions``, the
transcript colouring). ``quote`` is the exact spoken text **without** the
surrounding quotation marks, verbatim including internal punctuation and line
breaks, from the ONE quote scan the frontend mirrors (``quotedSpans``: straight
``"…"`` and curly ``“…”``).

"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from fabulist.api.cast_api import (
    SPEAKER_PALETTE,
    assign_member_color,
    color_name,
    list_member_colors,
    normalize_hex_color,
)
from fabulist.graph.validator import anchor_for_span

__all__ = [
    "AttributionFn",
    "AttributionWrap",
    "MarkupAttributionRunner",
    "PaletteEntry",
    "QuoteSpan",
    "RGB_TOLERANCE",
    "attribution_missing_flags",
    "attribution_unquoted_flags",
    "extract_quotes",
    "mechanical_attribution_wraps",
    "palette_for_turn",
    "parse_attribution",
    "scan_quotes",
    "wrap_attributed_quotes",
]

#: Manhattan RGB distance still read as the same palette slot (D5). Small on
#: purpose: it absorbs a model that rounds a channel, not a different hue.
RGB_TOLERANCE = 16


# -- the speech-tag catalogue ------------------------------------------------
#
# The catalogues outlived the D75 detector for ONE job: deciding whether a
# continuation after an unwrapped quote is a speech tag (a spoken line) or the
# prose carrying on around a citation. Finite forms only, because finite forms
# are what prose writes.

#: Speech verbs: a visual surface (or pronoun) next to one of these tags a line.
SPEECH_VERBS: frozenset[str] = frozenset(
    {
        # say / tell
        "say", "says", "said", "saying",
        "tell", "tells", "told", "telling",
        # reply / answer / respond / add / continue / begin / start
        "reply", "replies", "replied", "replying",
        "answer", "answers", "answered", "answering",
        "respond", "responds", "responded", "responding",
        "add", "adds", "added", "adding",
        "continue", "continues", "continued", "continuing",
        "begin", "begins", "began", "beginning",
        "start", "starts", "started", "starting",
        # ask / demand / beg
        "ask", "asks", "asked", "asking",
        "inquire", "inquires", "inquired", "inquiring",
        "enquire", "enquires", "enquired", "enquiring",
        "demand", "demands", "demanded", "demanding",
        "beg", "begs", "begged", "begging",
        "plead", "pleads", "pleaded", "pleading",
        # quiet
        "whisper", "whispers", "whispered", "whispering",
        "mutter", "mutters", "muttered", "muttering",
        "murmur", "murmurs", "murmured", "murmuring",
        "mumble", "mumbles", "mumbled", "mumbling",
        "breathe", "breathes", "breathed", "breathing",
        # sharp
        "snap", "snaps", "snapped", "snapping",
        "growl", "growls", "growled", "growling",
        "hiss", "hisses", "hissed", "hissing",
        "grunt", "grunts", "grunted", "grunting",
        "bark", "barks", "barked", "barking",
        "spit", "spits", "spat", "spitting",
        "scoff", "scoffs", "scoffed", "scoffing",
        "sneer", "sneers", "sneered", "sneering",
        # loud
        "shout", "shouts", "shouted", "shouting",
        "yell", "yells", "yelled", "yelling",
        "call", "calls", "called", "calling",
        "cry", "cries", "cried", "crying",
        "roar", "roars", "roared", "roaring",
        "bellow", "bellows", "bellowed", "bellowing",
        # tone-carrying
        "drawl", "drawls", "drawled", "drawling",
        "sigh", "sighs", "sighed", "sighing",
        "laugh", "laughs", "laughed", "laughing",
        "chuckle", "chuckles", "chuckled", "chuckling",
        "snort", "snorts", "snorted", "snorting",
        "purr", "purrs", "purred", "purring",
        "rumble", "rumbles", "rumbled", "rumbling",
        "echo", "echoes", "echoed", "echoing",
        "chant", "chants", "chanted", "chanting",
        "intone", "intones", "intoned", "intoning",
        "recite", "recites", "recited", "reciting",
        # structural / framing
        "offer", "offers", "offered", "offering",
        "insist", "insists", "insisted", "insisting",
        "agree", "agrees", "agreed", "agreeing",
        "promise", "promises", "promised", "promising",
        "warn", "warns", "warned", "warning",
        "admit", "admits", "admitted", "admitting",
        "correct", "corrects", "corrected", "correcting",
        "repeat", "repeats", "repeated", "repeating",
        "announce", "announces", "announced", "announcing",
        "declare", "declares", "declared", "declaring",
        "explain", "explains", "explained", "explaining",
        "remark", "remarks", "remarked", "remarking",
        "observe", "observes", "observed", "observing",
        "note", "notes", "noted", "noting",
        "state", "states", "stated", "stating",
        "suggest", "suggests", "suggested", "suggesting",
        "muse", "muses", "mused", "musing",
        "concede", "concedes", "conceded", "conceding",
        "protest", "protests", "protested", "protesting",
        "counter", "counters", "countered", "countering",
        "interrupt", "interrupts", "interrupted", "interrupting",
        "interject", "interjects", "interjected", "interjecting",
        "stammer", "stammers", "stammered", "stammering",
        "stutter", "stutters", "stuttered", "stuttering",
        "order", "orders", "ordered", "ordering",
        "command", "commands", "commanded", "commanding",
        "wonder", "wonders", "wondered", "wondering",
        "conclude", "concludes", "concluded", "concluding",
        # physical / rough voice ("Runs... this place?" he wheezes.)
        "wheeze", "wheezes", "wheezed", "wheezing",
        "rasp", "rasps", "rasped", "rasping",
        "croak", "croaks", "croaked", "croaking",
        "grate", "grates", "grated", "grating",
        "squawk", "squawks", "squawked", "squawking",
        "bleat", "bleats", "bleated", "bleating",
        "gasp", "gasps", "gasped", "gasping",
        "pant", "pants", "panted", "panting",
        "huff", "huffs", "huffed", "huffing",
        "grumble", "grumbles", "grumbled", "grumbling",
        "chime", "chimes", "chimed", "chiming",
        "return", "returns", "returned", "returning",
        "retort", "retorts", "retorted", "retorting",
        "quip", "quips", "quipped", "quipping",
    }
)

#: Words allowed between a subject and its verb without breaking the tag
#: ("Aria quietly replied"). Deliberately tiny.
FILLER_WORDS: frozenset[str] = frozenset(
    {
        "then", "also", "still", "just", "only", "even", "now", "again",
        "finally", "suddenly", "instead", "quickly", "slowly", "softly",
        "quietly", "loudly", "sharply", "flatly", "dryly", "coldly", "warmly",
        "wearily", "carefully", "absently", "mildly", "evenly", "hoarsely",
        "already", "almost", "barely", "simply", "merely", "immediately",
        "at", "last", "once", "more", "half",
    }
)


def _alternation(words: Iterable[str]) -> str:
    """A regex alternation of ``words``, longest first (no prefix shadowing)."""
    unique = sorted({word for word in words if word}, key=len, reverse=True)
    return "|".join(re.escape(word) for word in unique)


_SPEECH_ALT = _alternation(SPEECH_VERBS)
_FILLER_ALT = _alternation(FILLER_WORDS)

#: ``0..3`` filler words on the SAME line between a subject and its verb.
_FILLER_RUN = rf"(?:[ \t]+(?:{_FILLER_ALT})){{0,3}}"
#: Word boundaries: ``said`` must not match inside ``saidso``, and ``he`` must
#: not match inside ``the``.
_WORD_START = r"(?<![\w])"
_WORD_END = r"(?![\w])"
#: Markdown emphasis may wrap a subject or close right after it (``**Aria**``).
_EMPHASIS = r"\**[ \t]*"

#: Pronouns that can carry a speech tag without naming anyone.
_PRONOUN = r"(?:he|she|they)"


def _frame_re(palette: Sequence["PaletteEntry"]) -> re.Pattern[str] | None:
    """A speech tag at the START of a string, either word order (D4).

    ``he said`` / ``Aria quietly replied`` (subject first) and ``said Aria`` /
    ``replied the innkeeper`` (verb first) — the shapes that follow a spoken
    line on the same line. The surfaces are the palette's display names: the
    people the narrator was told to colour. ``None`` when the palette names
    nobody (nothing could be a tag).
    """
    surfaces = sorted(
        {entry.display_name for entry in palette if entry.display_name},
        key=len,
        reverse=True,
    )
    if not surfaces:
        return None
    subject = rf"(?:{'|'.join(re.escape(surface) for surface in surfaces)}|{_PRONOUN})"
    verbs = rf"(?:{_SPEECH_ALT})"
    return re.compile(
        rf"^(?:{_WORD_START}{subject}{_EMPHASIS}{_FILLER_RUN}"
        rf"[ \t]+{verbs}{_WORD_END}"
        rf"|{_WORD_START}{verbs}{_WORD_END})",
        re.IGNORECASE,
    )


# -- colour markup -----------------------------------------------------------

#: A colour-markup tag: an opening or closing ``<span>``/``<font>`` — the ONLY
#: tags this module reads or masks. Quotes inside any such tag are attribute
#: syntax, never speech.
_TAG_RE = re.compile(
    r"<\s*(?P<close>/?)\s*(?P<tag>span|font)\b(?P<attrs>[^<>]*)>", re.IGNORECASE
)

#: One attribute of a tag: name plus a double-quoted, single-quoted or bare value.
_ATTR_RE = re.compile(
    r"(?P<name>[A-Za-z_:][-A-Za-z0-9_:.]*)\s*=\s*"
    r"(?:\"(?P<dq>[^\"]*)\"|'(?P<sq>[^']*)'|(?P<bare>[^\s\"'>]+))"
)

#: The ``color`` declaration inside a ``style`` attribute (``color:#e06c75`` or
#: ``font-weight:bold; color : red``).
_STYLE_COLOR_RE = re.compile(r"(?:^|;)\s*color\s*:\s*([^;]+)", re.IGNORECASE)

#: ``rgb(r,g,b)`` / ``rgb(r g b)`` / ``rgba(r,g,b,alpha)`` — the alpha is dropped.
_RGB_VALUE_RE = re.compile(
    r"^rgba?\(\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*[, ]\s*(\d{1,3})\s*"
    r"(?:[,/]\s*[\d.]+%?)?\s*\)$",
    re.IGNORECASE,
)

#: A blank line — the end of a colour context (D5: bounded per paragraph).
_PARAGRAPH_BREAK_RE = re.compile(r"\n[ \t\r]*\n")

#: The quote-span scan — the ONE scan :func:`extract_quotes` (frontend parity:
#: ``TranscriptList.quotedSpans``) and the resolver both read. ``[^"]*`` crosses
#: newlines, so a quote over several lines is ONE span.
_QUOTE_RE = re.compile(r'"([^"]*)"|“([^”]*)”')

#: Punctuation that marks a quoted stretch as CLAUSE structure rather than a
#: bare noun phrase. A citation (``a scent Hobbs calls "Spring Rain"``) is a
#: noun phrase and carries none of these; an utterance either closes its
#: sentence (sentence punctuation) or is joined to its tag (``,"`` / ``:"``).
_CLAUSE_MARKS = ("…", ".", "!", "?", ",", ";", ":")

#: What may precede a speech tag after a quote's closing mark: spacing and the
#: punctuation a tag can be joined by (``," she said`` / ``—" said Aria``).
_TAIL_TRIM = " \t,;:—–-*_"

#: Evidence clip for a missing-attribution flag (the model reads this line).
_EVIDENCE_CHARS = 200


@dataclass(frozen=True)
class PaletteEntry:
    """One active speaker's colour row: the join key, the name, and the hue.

    ``member_id`` is the immutable cast id (the colour registry's key), so a
    rename keeps its hue; ``display_name`` is what a row reports; ``hex`` is the
    colour the narrator was shown; ``name`` is the palette LABEL (``red``) when
    the hue is one of the engine's ten (:func:`fabulist.api.cast_api.color_name`)
    and ``None`` for a user-picked hex with no label.
    """

    member_id: str
    display_name: str
    hex: str
    name: str | None = None


def _mask_tags(narrative: str) -> str:
    """Blank every colour tag to a SAME-LENGTH run of spaces (R2 P0-1).

    ``style="color:#B03A48"`` carries two quote marks; without this mask the
    quote scan reports ``color:#B03A48`` as a phantom span per wrapped line.
    Offsets are preserved (one blank per character) so a span found in the mask
    still indexes ``narrative``. Newlines are kept, so the paragraph boundaries
    the colour context is bounded by survive the mask.
    """
    if "<" not in narrative:
        return narrative
    chars = list(narrative)
    for match in _TAG_RE.finditer(narrative):
        for index in range(match.start(), match.end()):
            if chars[index] != "\n":
                chars[index] = " "
    return "".join(chars)


def _tag_color(attrs: str) -> str | None:
    """The colour a tag's attributes declare, or ``None`` (no colour on the tag).

    ``style`` wins over a plain ``color`` attribute; a ``style`` without a
    ``color`` declaration leaves the tag colourless (``<span class="calm">``).
    """
    style: str | None = None
    color_attr: str | None = None
    for match in _ATTR_RE.finditer(attrs):
        value = next(
            (
                group
                for group in (
                    match.group("dq"),
                    match.group("sq"),
                    match.group("bare"),
                )
                if group is not None
            ),
            "",
        )
        name = match.group("name").lower()
        if name == "style":
            style = value
        elif name == "color":
            color_attr = value
    if style is not None:
        found = _STYLE_COLOR_RE.search(style)
        if found is not None:
            return found.group(1).strip()
    return color_attr.strip() if color_attr else None


def _rgb_triplet(value: str) -> tuple[int, int, int] | None:
    """``value`` as an RGB triple (hex or ``rgb()``/``rgba()``), else ``None``."""
    cleaned = value.strip()
    hexed = normalize_hex_color(cleaned)
    if hexed is not None:
        return (
            int(hexed[1:3], 16),
            int(hexed[3:5], 16),
            int(hexed[5:7], 16),
        )
    match = _RGB_VALUE_RE.match(cleaned)
    if match is None:
        return None
    red, green, blue = match.groups()
    return (min(255, int(red)), min(255, int(green)), min(255, int(blue)))


def _match_entry(
    value: str, palette: Sequence[PaletteEntry]
) -> PaletteEntry | None:
    """Resolve one colour VALUE against the palette (D5), or ``None``.

    A value matches an entry by RGB within :data:`RGB_TOLERANCE` or by LABEL
    equality (case-insensitive — never a CSS-spec lookup, so ``crimson`` resolves
    nothing). Nothing matched, or two DIFFERENT members matched, resolves to
    nobody: the engine splits on doubt, never merges.
    """
    cleaned = value.strip()
    if not cleaned:
        return None
    wanted = _rgb_triplet(cleaned)
    matches: list[PaletteEntry] = []
    for entry in palette:
        if wanted is not None:
            actual = _rgb_triplet(entry.hex)
            if actual is not None:
                distance = sum(abs(a - b) for a, b in zip(wanted, actual))
                if distance <= RGB_TOLERANCE:
                    matches.append(entry)
                    continue
        if entry.name and cleaned.casefold() == entry.name.casefold():
            matches.append(entry)
    if len({entry.member_id for entry in matches}) != 1:
        return None
    return matches[0]


# -- the quote scan ----------------------------------------------------------


@dataclass(frozen=True)
class QuoteSpan:
    """One quoted span: the char offsets of the whole run plus its inner text.

    ``start`` is the opening mark's index and ``end`` one past the closing mark,
    so ``narrative[start:end]`` is the quoted run and ``text`` is what the
    transcript tints. Offsets index the ORIGINAL narrative even though the scan
    runs over the tag-masked copy.
    """

    start: int
    end: int
    text: str


def scan_quotes(narrative: str) -> list[QuoteSpan]:
    """Every quoted span in ``narrative``, in order (straight + curly marks).

    Colour markup is masked to same-length blanks first, so the quotes a
    ``style="color:#…"`` attribute is written with are not spans (R2 P0-1).
    """
    source = narrative or ""
    spans: list[QuoteSpan] = []
    for match in _QUOTE_RE.finditer(_mask_tags(source)):
        inner = match.group(1) if match.group(1) is not None else match.group(2)
        spans.append(QuoteSpan(match.start(), match.end(), inner or ""))
    return spans


def extract_quotes(narrative: str) -> list[str]:
    """Every quoted line in ``narrative``, in order, WITHOUT the quote marks.

    Matches the frontend's quote-span scan (``TranscriptList.quotedSpans``):
    straight ``"..."`` and curly ``“...”`` spans only, inner text verbatim.
    Empty/blank spans carry no speech and are skipped.
    """
    return [span.text for span in scan_quotes(narrative) if span.text.strip()]


# -- citations (not every quoted span is speech) -----------------------------


def _line_tail(narrative: str, end: int) -> str:
    """What is left of the quote's own line after its closing mark (trimmed)."""
    stop = narrative.find("\n", end)
    tail = narrative[end:] if stop < 0 else narrative[end:stop]
    return tail.lstrip(_TAIL_TRIM)


def _is_inline_citation(
    narrative: str, span: QuoteSpan, palette: Sequence[PaletteEntry]
) -> bool:
    """True when ``span`` is a quoted NAME/LABEL inside a sentence, not speech.

    The live case (seq 34): ``a cloying, artificial scent
    Hobbs calls "Spring Rain" that smells more like industrial floor cleaner``
    — the quoted brand is what he calls the scent, not a line he speaks. Same
    class: ``a faded neon "OPEN" sign``, ``the "gold" seems to hold the light``.

    Structural test, three conditions, all required:

    1. the span closes as a NOUN PHRASE — no sentence punctuation and no clause
       punctuation anywhere inside (:data:`_CLAUSE_MARKS`). An utterance closes
       its own sentence (``"…."``) or is joined to its tag (``"…," he said``);
       a citation does neither;
    2. the sentence CARRIES ON after the closing mark on the same line, in
       lower case — the citation is grammatically embedded;
    3. that continuation is not a speech tag (``…" she said``).

    ``narrative`` is expected to be the MASKED text (:func:`_mask_tags`), so a
    closing ``</span>`` cannot pass for a sentence continuation.

    The test decides whether the engine DEMANDS a colour (the missing-flag
    path); it never overrules a span the model already wrapped (D4).
    """
    text = span.text.strip()
    if not text:
        return False
    if any(mark in text for mark in _CLAUSE_MARKS):
        return False
    tail = _line_tail(narrative, span.end)
    if not tail or not tail[:1].islower():
        return False
    frame = _frame_re(palette)
    return frame is None or frame.match(tail) is None


# -- the resolver ------------------------------------------------------------


@dataclass(frozen=True)
class ResolvedQuote:
    """One scanned quote plus the colour context it was written inside.

    ``member`` is the innermost open span's palette entry, or ``None`` when the
    quote is unwrapped or its colour resolves to nobody; ``wrapped`` records
    which of those two it was (both are ``member is None``).
    """

    span: QuoteSpan
    member: PaletteEntry | None
    wrapped: bool


def _resolve_quotes(
    narrative: str, palette: Sequence[PaletteEntry]
) -> list[ResolvedQuote]:
    """Walk ``narrative`` once, pairing every quote with its colour context.

    Three event kinds, in prose order: a colour tag open (push), a colour tag
    close (pop) and a quote (read the innermost open context). The stack is
    BOUNDED: a blank line clears it, and it never outlives the call, so a
    dangling open cannot attribute the rest of the turn to the last speaker.
    """
    source = narrative or ""
    masked = _mask_tags(source)
    breaks = [match.start() for match in _PARAGRAPH_BREAK_RE.finditer(masked)]
    events: list[tuple[int, int, re.Match[str]]] = [
        (match.start(), 0, match) for match in _TAG_RE.finditer(source)
    ]
    events += [(match.start(), 1, match) for match in _QUOTE_RE.finditer(masked)]
    events.sort(key=lambda event: (event[0], event[1]))

    stack: list[PaletteEntry | None] = []
    resolved: list[ResolvedQuote] = []
    next_break = 0
    for position, kind, match in events:
        while next_break < len(breaks) and breaks[next_break] < position:
            stack.clear()
            next_break += 1
        if kind == 0:
            if match.group("close"):
                if stack:
                    stack.pop()
                continue
            member = _match_entry(_tag_color(match.group("attrs")) or "", palette)
            stack.append(member)
            continue
        inner = match.group(1) if match.group(1) is not None else match.group(2)
        resolved.append(
            ResolvedQuote(
                span=QuoteSpan(match.start(), match.end(), inner or ""),
                member=stack[-1] if stack else None,
                wrapped=bool(stack),
            )
        )
    return resolved


# -- colour spans (the unquoted half of strict omission) ---------------------


@dataclass(frozen=True)
class ColourSpan:
    """One COMPLETE colour span: the entry it resolves to and its content bounds.

    ``start`` is one past the opening tag and ``end`` the closing tag's first
    character, so ``narrative[start:end]`` is the prose the span wraps — the tags
    themselves excluded, because the prose is what a repair patch edits.
    """

    entry: PaletteEntry
    start: int
    end: int


def _resolve_colour_spans(
    narrative: str, palette: Sequence[PaletteEntry]
) -> list[ColourSpan]:
    """Every span that both OPENS and CLOSES in ``narrative``, with its colour.

    STRUCTURAL tag pairing, not the attribution resolver: the D5 paragraph bound
    decides whether a quote inside a span is ATTRIBUTED, never whether the span
    exists — prose the model coloured is prose the model coloured, and the
    teaching flag has to reach it either way.

    A span whose colour resolves to nobody is not reported: no colour at all
    (``<span class="calm">``) makes no attribution claim, and a colour outside
    the palette is :func:`attribution_missing_flags`' business (the quote inside
    it is reported as missing, not the span). Results are in prose order.
    """
    source = narrative or ""
    stack: list[tuple[PaletteEntry | None, int]] = []
    spans: list[ColourSpan] = []
    for match in _TAG_RE.finditer(source):
        if not match.group("close"):
            entry = _match_entry(_tag_color(match.group("attrs")) or "", palette)
            stack.append((entry, match.end()))
            continue
        if not stack:
            continue
        entry, start = stack.pop()
        if entry is not None:
            spans.append(ColourSpan(entry, start, match.start()))
    spans.sort(key=lambda span: span.start)
    return spans


# -- the palette -------------------------------------------------------------


def palette_for_turn(log: Any, session_id: str) -> list[PaletteEntry]:
    """The SPEAKING cast's colour palette for one turn (D1/D2).

    The palette answers *who can speak* — the CAST — not who the presence ladder
    says was in the scene this instant. Sourcing it from
    ``presence.active_members`` made it depend on evidence the turn itself
    produces: post-turn the turn's own ``world_state`` exists but normally names
    only the player, so the ladder falls to its attribution rung — the very rows
    this pass is computing — and answers "none". The palette came back empty and
    :func:`parse_attribution` could not resolve the colours the narrator had just
    been taught (live W9.28 defect: perfect spans, zero attributions, zero
    flags). So the palette is built from the ROSTER:

    * every ACTIVE actor-tier member (``card`` / ``world`` / ``actor``) is always
      in the palette, in roster order — the owner's attached cast plus the
      members she accepted from a proposal;
    * the presence ladder's members are UNIONED in, so a member the walk reports
      is never starved of the hue it was shown;
    * a ``provenance == "user"`` member is the PLAYER (User-as-Actor): the
      narrator quotes the cast, never the user's own turn, so it is filtered out
      here even though the presence ladder keeps it (D76(6));
    * retired members are not speakers, and the list is capped at
      ``len(SPEAKER_PALETTE)`` entries so the narrator's tail block stays bounded
      (:mod:`fabulist.prompts.palette`).

    The ladder itself is untouched — dispatch reads it for its own purposes and
    must keep its own semantics.

    ``ensure_roster`` runs FIRST: the roster is empty on a fresh chat until the
    deterministic seed admits are minted (the same turn-1 race ``graph/nodes``
    mints for before dispatch) — so the palette would answer "nobody" on the
    very turn the model needs a hue.

    Colours are ensured before the prompt: one read of the registry supplies the
    hues that exist, and a member without a hue yet is minted through
    ``cast_api.assign_member_color`` — idempotent (it returns the existing hue
    when there is one, assigns the first free slot otherwise, best-effort, never
    breaks the turn). Never a fold read per member (R2 P2).

    Entries come back in roster order, so the tail block and the palette are
    deterministic turn to turn.
    """
    from fabulist.state.cast import ensure_roster
    from fabulist.state.presence import ACTOR_PROVENANCE, active_members

    cast = ensure_roster(log, session_id)
    if not cast:
        return []
    speaking_provenance = ACTOR_PROVENANCE - {"user"}
    member_ids, _source = active_members(log, session_id)
    present = set(member_ids)
    speakers = [
        member
        for member in cast
        if getattr(member, "provenance", "") != "user"
        and getattr(member, "is_active", True)
        and (
            member.member_id in present
            or (
                member.provenance in speaking_provenance
                and not getattr(member, "provisional", False)
            )
        )
    ][: len(SPEAKER_PALETTE)]
    colors = list_member_colors(log, session_id)
    entries: list[PaletteEntry] = []
    for member in speakers:
        color = colors.get(member.member_id)
        if not color:
            try:
                color = assign_member_color(
                    log, session_id, member.member_id, member.display_name
                )
            except Exception:  # noqa: BLE001 - colouring is best-effort (D2)
                continue
        entries.append(
            PaletteEntry(
                member_id=member.member_id,
                display_name=member.display_name,
                hex=color,
                name=color_name(color),
            )
        )
    return entries


# -- the two outputs ---------------------------------------------------------


def parse_attribution(
    narrative: str, palette: Sequence[PaletteEntry]
) -> list[dict[str, str]]:
    """Attribute every colour-wrapped spoken line in ``narrative``.

    Rows are ``{"speaker", "quote", "speaker_id"}`` in prose order — a row exists
    only for a quote inside a colour span resolving to a palette entry (strict
    omission, D3). No palette means nobody can speak, so nothing is attributed.
    """
    if not palette:
        return []
    rows: list[dict[str, str]] = []
    for quote in _resolve_quotes(narrative, palette):
        entry = quote.member
        if entry is None or not quote.span.text.strip():
            continue
        rows.append(
            {
                "speaker": entry.display_name,
                "quote": quote.span.text,
                "speaker_id": entry.member_id,
            }
        )
    return rows


def _example_span(entry: PaletteEntry) -> str:
    """The palette span a ``fix`` line teaches (a real hue, never an invented one)."""
    label = (
        f"{entry.display_name}: {entry.name} {entry.hex}"
        if entry.name
        else f"{entry.display_name}: {entry.hex}"
    )
    return f'<span style="color:{entry.hex}">"line"</span> ({label})'


def _unattributed_quotes(text: str, palette: Sequence[PaletteEntry]) -> list[ResolvedQuote]:
    """The spoken quotes in ``text`` that carry no usable colour (D3).

    ONE spelling of the predicate the missing fold and the mechanical repair
    (:func:`mechanical_attribution_wraps`) both steer on: a quoted line with no
    non-blank content is not speech, a quote inside a span resolving to a palette
    entry is already attributed, and an UNWRAPPED quote that reads as an inline
    citation is not an utterance to wrap. A quote the model wrapped in a colour
    OUTSIDE the palette is reported missing — never silently re-wrapped.
    """
    masked = _mask_tags(text)
    missing: list[ResolvedQuote] = []
    for quote in _resolve_quotes(text, palette):
        span = quote.span
        if not span.text.strip() or quote.member is not None:
            continue
        if not quote.wrapped and _is_inline_citation(masked, span, palette):
            continue
        missing.append(quote)
    return missing


def attribution_missing_flags(
    segments: Sequence[Mapping] | None, palette: Sequence[PaletteEntry]
) -> list[dict]:
    """One ``attribution_missing`` flag per unattributed spoken quote (D3).

    This IS the fallback the owner chose over heuristics: a quoted line the model
    did not wrap in a palette colour is reported, anchored on the exact prose to
    patch, with the fix naming a real palette span — so the narrate repair loop
    can hand it back through ``patch_prose``. Citations are exempt (they are not
    speech), and a quote the model DID wrap but in a colour outside the palette
    is reported too: the line would otherwise drop silently, which is the defect
    W9.28 exists to end.

    An empty palette flags nothing: with no active cast there is no hue to teach
    and no speaker a wrapped line could resolve to.
    """
    if not palette:
        return []
    example = _example_span(palette[0])
    flags: list[dict] = []
    for segment_index, segment in enumerate(segments or ()):
        if not isinstance(segment, Mapping) or segment.get("kind") != "prose":
            continue
        text = segment.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        for quote in _unattributed_quotes(text, palette):
            span = quote.span
            flags.append(
                {
                    "rule_id": "attribution_missing",
                    "check": "attribution",
                    "severity": "warn",
                    "evidence": span.text.strip()[:_EVIDENCE_CHARS],
                    "source": (
                        "engine (attribution contract: wrap dialogue in the "
                        "speaker's palette colour)"
                    ),
                    "segment_index": segment_index,
                    "message": (
                        "quoted line is not wrapped in a palette colour span; it "
                        "will be left unattributed"
                    ),
                    "anchor": anchor_for_span(text, span.start, span.end),
                    "fix": (
                        "wrap the quoted line in the speaking character's palette "
                        f"colour, e.g. {example}"
                    ),
                }
            )
    return flags


def attribution_unquoted_flags(
    segments: Sequence[Mapping] | None, palette: Sequence[PaletteEntry]
) -> list[dict]:
    """One ``attribution_unquoted`` flag per colour span wrapping no quote (D3).

    A colour span is the model asserting *this is speech, by this hue*, so a
    span carrying no quoted line is wrong either way: it is speech the model
    forgot to quote (the parser can anchor nothing, so
    :func:`attribution_missing_flags` cannot see it and the presence rung
    starves), or it is narration the model mis-coloured. Both are reported the
    same way — anchored on the prose INSIDE the span, naming the hue the span
    used — so the narrate repair loop can quote the line or drop the span.

    Silent by construction on: an empty or whitespace-only span (nothing was
    claimed), a span that already carries a quoted line (attributed as written,
    or missing if its colour was outside the palette), a colourless span, and a
    colour outside the palette (that is :func:`attribution_missing_flags`'
    business). An empty palette flags nothing, exactly like the missing fold.

    The span must both OPEN and CLOSE in the segment: a dangling open is the D5
    case, and the quotes it wraps are already reported missing.
    """
    if not palette:
        return []
    flags: list[dict] = []
    for segment_index, segment in enumerate(segments or ()):
        if not isinstance(segment, Mapping) or segment.get("kind") != "prose":
            continue
        text = segment.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        quoted = [
            (span.start, span.end)
            for span in scan_quotes(text)
            if span.text.strip()
        ]
        for colour_span in _resolve_colour_spans(text, palette):
            content = text[colour_span.start : colour_span.end]
            if not content.strip():
                continue
            if any(
                start >= colour_span.start and end <= colour_span.end
                for start, end in quoted
            ):
                continue
            entry = colour_span.entry
            flags.append(
                {
                    "rule_id": "attribution_unquoted",
                    "check": "attribution",
                    "severity": "warn",
                    "evidence": content.strip()[:_EVIDENCE_CHARS],
                    "source": (
                        "engine (attribution contract: a colour span wraps a "
                        "quoted line)"
                    ),
                    "segment_index": segment_index,
                    "message": (
                        "a colour span must wrap a quoted line; either quote the "
                        "spoken line inside the span, or remove the span if it is "
                        "narration"
                    ),
                    "anchor": anchor_for_span(
                        text, colour_span.start, colour_span.end
                    ),
                    "fix": (
                        "quote the spoken line and keep it in "
                        f"{entry.display_name}'s palette colour (e.g. "
                        f"{_example_span(entry)}), or remove the span if this is "
                        "narration"
                    ),
                }
            )
    return flags


# -- the mechanical repair (issue 31) ----------------------------------------
#
# The palette block teaches the model to wrap each spoken line; the check then
# demands a span around EVERY quoted line. Most misses are not judgements: the
# prose itself NAMES the speaker in a speech tag ADJACENT to the quote
# (``"You're late," Aria said.`` / ``Aria set the cup down and said, "You're
# late."``). Wrapping a quote the prose already attributes is deterministic text
# editing, so the engine does it itself — zero model calls — and the repair loop
# spends its model round only on what this cannot resolve (live: 10+ flags and
# 40-100 s per round trip, nearly all of them this shape).
#
# The rule is strictly NARROWER than the retired D75 tiers (ADR-012: unattributed
# is better than plausibly wrong):
#
# * the speaker is a PALETTE display name — never a cue, a neighbour's pronoun or
#   an alternation pattern. A pronoun tag (``she said``) names nobody here, so it
#   resolves nobody;
# * the tag must be ADJACENT to the quote: the quote's own line after it, or the
#   lead-in sentence's CLOSING speech verb (``Aria set the cup down and said,``);
# * a lead-in naming TWO palette speakers is ambiguous and resolves to nobody;
# * an unwrapped inline citation is not speech and is never wrapped;
# * a quote the model DID wrap, in a colour outside the palette, is left alone:
#   recolouring a span is a colour choice, not a span to add.

#: Sentence punctuation that ends the lead-in window a tag may sit in.
_SENTENCE_ENDS = (".", "!", "?", "…")


def _name_surfaces(
    palette: Sequence[PaletteEntry],
) -> list[tuple[str, PaletteEntry]]:
    """The palette's display names (casefolded), longest first.

    Longest first so ``Old Mara`` is never shadowed by a shorter ``Mara``; one
    entry per name, so a duplicate surface cannot make a name ambiguous.
    """
    surfaces: dict[str, PaletteEntry] = {}
    for entry in palette:
        name = (entry.display_name or "").strip()
        if name:
            surfaces.setdefault(name.casefold(), entry)
    return sorted(surfaces.items(), key=lambda item: len(item[0]), reverse=True)


def _entry_for_surface(
    surface: str, surfaces: Sequence[tuple[str, PaletteEntry]]
) -> PaletteEntry | None:
    """The palette entry a matched display-name surface names, or ``None``."""
    wanted = surface.strip().casefold()
    for name, entry in surfaces:
        if name == wanted:
            return entry
    return None


def _entry_named_in_tail(
    masked: str, end: int, surfaces: Sequence[tuple[str, PaletteEntry]]
) -> PaletteEntry | None:
    """The speaker the quote's OWN LINE names after it, or ``None``.

    Two finite shapes, both with the tag immediately after the closing mark::

        "You're late," Aria said.
        "You're late," said Aria.

    A tag on a later line is not adjacent — prose that leaves a line between the
    quote and its tag may be attributing something else, so it stays for the
    model (the loop's judgement half).
    """
    tail = _line_tail(masked, end)
    if not tail:
        return None
    names = "|".join(re.escape(name) for name, _ in surfaces)
    verbs = f"(?:{_SPEECH_ALT})"
    subject_first = re.compile(
        rf"^(?P<name>{names})(?!['’\w]){_EMPHASIS}{_FILLER_RUN}[ \t]+{verbs}{_WORD_END}",
        re.IGNORECASE,
    )
    match = subject_first.match(tail)
    if match is not None:
        return _entry_for_surface(match.group("name"), surfaces)
    verb_first = re.compile(
        rf"^{verbs}{_WORD_END}[ \t]+(?:(?:the|a|an)[ \t]+)?"
        rf"(?P<name>{names})(?!['’\w])",
        re.IGNORECASE,
    )
    match = verb_first.match(tail)
    if match is not None:
        return _entry_for_surface(match.group("name"), surfaces)
    return None


def _lead_in(masked: str, start: int) -> str:
    """The prose that runs up to ``start`` inside its own paragraph and sentence.

    Bounded twice, and both bounds matter: a blank line is a new paragraph (D5's
    colour context), and the sentence's own terminator ends the window — so a
    speaker named in a PREVIOUS sentence is never read as this quote's tag.
    """
    floor = 0
    for match in _PARAGRAPH_BREAK_RE.finditer(masked, 0, start):
        floor = match.end()
    lead = masked[floor:start]
    cut = max((lead.rfind(mark) for mark in _SENTENCE_ENDS), default=-1)
    if cut >= 0:
        lead = lead[cut + 1 :]
    return lead.rstrip(_TAIL_TRIM + "\n\r")


def _entry_named_in_lead_in(
    masked: str, start: int, surfaces: Sequence[tuple[str, PaletteEntry]]
) -> PaletteEntry | None:
    """The speaker a LEAD-IN sentence names, or ``None``.

    ``Aria set the cup down and said, "You're late."`` — the sentence closes on
    a speech verb and names exactly one palette speaker: the quote is hers. The
    two conditions are both required, and a sentence naming two speakers is
    ambiguous (resolves to nobody) — this must never guess.
    """
    lead = _lead_in(masked, start)
    if not lead:
        return None
    closing_verb = re.search(
        rf"(?<![\w])(?:{_SPEECH_ALT}){_WORD_END}[ \t]*$", lead, re.IGNORECASE
    )
    if closing_verb is None:
        return None
    named: dict[str, PaletteEntry] = {}
    for name, entry in surfaces:
        if re.search(rf"(?<![\w]){re.escape(name)}{_WORD_END}", lead, re.IGNORECASE):
            named.setdefault(entry.member_id, entry)
    if len(named) != 1:
        return None
    return next(iter(named.values()))


@dataclass(frozen=True)
class AttributionWrap:
    """One mechanical wrap: which quote wears which speaker's palette colour.

    ``start``/``end`` are the quote run's offsets in its segment's ORIGINAL text
    (mark to mark, the shape :func:`scan_quotes` reports), so a caller can apply
    the span without re-scanning.
    """

    segment_index: int
    entry: PaletteEntry
    start: int
    end: int
    quote: str


def mechanical_attribution_wraps(
    segments: Sequence[Mapping] | None, palette: Sequence[PaletteEntry]
) -> list[AttributionWrap]:
    """Every unattributed quote whose speaker the PROSE itself names (issue 31).

    One pass over the same quotes :func:`attribution_missing_flags` reports, in
    prose order. A quote is returned only when a palette display name sits in a
    speech tag adjacent to it (:func:`_entry_named_in_tail` /
    :func:`_entry_named_in_lead_in`); everything a tag does not name — a pronoun
    tag, alternating dialogue, a quote alone in its paragraph — is left for the
    model, because inferring it would be the plausible-wrong attribution ADR-012
    exists to forbid.
    """
    if not palette:
        return []
    surfaces = _name_surfaces(palette)
    if not surfaces:
        return []
    wraps: list[AttributionWrap] = []
    for segment_index, segment in enumerate(segments or ()):
        if not isinstance(segment, Mapping) or segment.get("kind") != "prose":
            continue
        text = segment.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        masked = _mask_tags(text)
        for quote in _unattributed_quotes(text, palette):
            if quote.wrapped:
                continue
            span = quote.span
            entry = _entry_named_in_tail(masked, span.end, surfaces)
            if entry is None:
                entry = _entry_named_in_lead_in(masked, span.start, surfaces)
            if entry is None:
                continue
            wraps.append(
                AttributionWrap(
                    segment_index=segment_index,
                    entry=entry,
                    start=span.start,
                    end=span.end,
                    quote=span.text,
                )
            )
    return wraps


def wrap_attributed_quotes(
    segments: Sequence[Mapping] | None, palette: Sequence[PaletteEntry]
) -> tuple[list, list[AttributionWrap]]:
    """Write the mechanical spans; returns ``(segments, wraps)``.

    No wraps means the caller's own list comes back untouched (a clean channel
    is never rebuilt). Otherwise every touched segment is a fresh dict, and the
    spans are written RIGHT TO LEFT so each quote keeps the offsets the scan
    reported. Nothing else in the segment is edited: the wrap is the span around
    the quoted run, never a rewrite of the sentence carrying it.
    """
    original = list(segments or ())
    wraps = mechanical_attribution_wraps(original, palette)
    if not wraps:
        return original, []
    repaired = [
        dict(segment) if isinstance(segment, Mapping) else segment
        for segment in original
    ]
    for segment_index in sorted({wrap.segment_index for wrap in wraps}):
        segment = repaired[segment_index]
        text = segment.get("text") if isinstance(segment, Mapping) else None
        if not isinstance(text, str):
            continue
        for wrap in sorted(
            (item for item in wraps if item.segment_index == segment_index),
            key=lambda item: item.start,
            reverse=True,
        ):
            quoted = text[wrap.start : wrap.end]
            text = (
                f"{text[: wrap.start]}"
                f'<span style="color:{wrap.entry.hex}">{quoted}</span>'
                f"{text[wrap.end :]}"
            )
        repaired[segment_index] = {**segment, "text": text}
    return repaired, wraps


# -- the post-turn seam ------------------------------------------------------


class AttributionFn(Protocol):
    """The post-turn attribution seam: ``(user_msg, narrative) -> list[dict]``."""

    async def __call__(self, user_msg: str, narrative: str) -> list[dict]: ...


class MarkupAttributionRunner:
    """The attribution seam, backed by the palette and the markup parser (W9.28).

    ``palette_fn`` is the PALETTE seam the wiring supplies (W4): it closes over
    the event log + the CURRENT session and returns that session's
    :func:`palette_for_turn`, re-read per call so a member admitted earlier in
    the turn is attributable immediately. No ``palette_fn`` — or a session whose
    ladder answers nobody — yields no rows: this pass never invents a speaker.

    ``user_msg`` is accepted and ignored: the narrator's prose is the only input
    (B5). It stays in the signature because the post-turn seam is shared with the
    model passes that do read it.

    A palette read that raises is NOT swallowed: the node turns any exception into
    an ``attribution_failed`` row and keeps the turn (D9 visibility), which is
    exactly the contract the retired model flow had.
    """

    def __init__(
        self, palette_fn: Callable[[], Sequence[PaletteEntry]] | None = None
    ) -> None:
        self._palette_fn = palette_fn

    async def __call__(self, user_msg: str, narrative: str) -> list[dict[str, str]]:
        palette = (
            tuple(self._palette_fn() or ()) if self._palette_fn is not None else ()
        )
        if not palette:
            return []
        return parse_attribution(narrative, palette)
