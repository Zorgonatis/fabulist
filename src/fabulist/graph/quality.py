"""W8.1 post-turn critic-fixer — the pure, model-free core (D41).

D41 ratifies ONE post-turn agent that inspects the finished turn and returns a
tri-state verdict: ``ok`` (keep), ``diffs`` (a patch applied **on top**, committed
as a revision event — never an in-place rewrite), or ``fail`` (keep the turn and
record the verdict: since W9.24 the re-run it used to ask for is RETIRED — the
narrator's in-node repair loop runs earlier and cheaper). This module owns the
deterministic half of that agent and nothing else:

* :class:`CriticVerdict` — the frozen result type (verdict / reason / normalised
  edits / parse-failure marker).
* :func:`parse_critic_output` — a tolerant reader for the model's fenced-JSON
  reply. It extracts the first balanced JSON object from the text and *never
  raises*: a bad shape (missing/unknown verdict, non-list ``edits``, non-string
  ``find``/``replace``) is rejected into a ``parse_failed`` marker the caller can
  fall back on.
* :func:`apply_patch` — the load-bearing deterministic patcher. It rewrites the
  narrative's ordered ``segments`` (``[{"kind": "prose"|"dsl", "text": ...}]``,
  the list ``graph/nodes.py`` builds) in prose only, and **atomically**: one
  absent or mis-counted ``find`` rejects the WHOLE patch, so a turn is never left
  half-edited. Every caller applies its edits ONE AT A TIME through it (the
  narrator loop's ``patch_prose`` tool and the critic's ``diffs`` staging), so a
  rejected edit costs only itself — the patcher is whole-patch, the call is
  per-edit.

The model output contract (authored by the prompt lane, treated as fixed)::

    {"verdict": "ok"|"diffs"|"fail", "reason": "<one sentence>",
     "edits": [{"find": "...", "replace": "...", "count": 1}]}

``regen_feedback`` is RETIRED as an authored field (W9.24 §3.3.7): the critic no
longer asks for a narrator re-run, and nothing consumes the value. Parsing it is
tolerated forever — an old log or a drifted prompt that still sends the key must
never break a parse — it simply lands on :attr:`CriticVerdict.regen_feedback`
and is ignored. ``apply_patch`` is ALSO the narrator repair loop's patch
vocabulary (W9.24 §3.3.3): the loop's ``patch_prose`` tool applies exactly these
edits over the working-memory segments before the turn commits.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass

__all__ = [
    "CriticVerdict",
    "PatchResult",
    "QualityEdit",
    "VERDICT_DIFFS",
    "VERDICT_FAIL",
    "VERDICT_OK",
    "VERDICTS",
    "apply_patch",
    "parse_critic_output",
]

#: The three ratified verdicts (lower-cased on parse).
VERDICT_OK = "ok"
VERDICT_DIFFS = "diffs"
VERDICT_FAIL = "fail"
VERDICTS = frozenset({VERDICT_OK, VERDICT_DIFFS, VERDICT_FAIL})

#: The only editable segment kind; every other kind is copied through untouched.
_PROSE = "prose"


# ---------------------------------------------------------------------------
# result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualityEdit:
    """One normalised ``find``/``replace`` pair (``count`` defaults to 1).

    ``find`` must be a literal substring of one prose segment appearing exactly
    ``count`` times across the whole prose; anything else rejects the patch.
    """

    find: str
    replace: str
    count: int = 1


@dataclass(frozen=True)
class CriticVerdict:
    """A parsed critic reply.

    ``verdict`` is one of ``"ok"``, ``"diffs"`` or ``"fail"`` — and ``""`` *only*
    when :attr:`parse_failed` is true (the parser never returns a trusted verdict
    for a malformed reply). ``edits`` are the normalised patch edits in model
    order. ``regen_feedback`` is the RETIRED fail-retry steer: still parsed (an
    old prompt or log must never break a parse) and never consumed — the critic
    keeps a ``fail`` turn instead of re-running the narrator (W9.24 §3.3.7). On
    a parse failure ``reason`` carries the parse error instead of the model's
    sentence.
    """

    verdict: str = ""
    reason: str = ""
    edits: tuple[QualityEdit, ...] = ()
    regen_feedback: str = ""
    parse_failed: bool = False


@dataclass(frozen=True)
class PatchResult:
    """Outcome of one :func:`apply_patch` call.

    On success ``segments`` is the rebuilt ordered payload and ``prose`` its
    joined prose channel (``"\\n".join`` of every ``kind == "prose"`` segment
    text — the same join ``graph/nodes.py`` uses), so the transcript and the
    prose body can never disagree. On failure ``segments``/``prose`` are the
    *untouched* inputs, ``failed_index`` indexes the input ``edits`` sequence and
    ``error`` names the offending edit.

    The remaining failure fields are the DIAGNOSIS the caller teaches with, so a
    rejection can never be re-read as "the prose does not contain this":
    ``prose_head`` is the head of the prose channel that was actually searched
    (prose only — never DSL text), ``elsewhere`` is ``(index, kind)`` of a
    non-prose segment the ``find`` matches verbatim, and ``boundary`` /
    ``boundary_anchors`` name the prose pair a ``find`` straddles plus the
    copyable edge of each side.
    """

    ok: bool
    segments: list[dict]
    prose: str
    applied: int = 0
    failed_index: int | None = None
    error: str = ""
    prose_head: str = ""
    elsewhere: tuple[int, str] | None = None
    boundary: tuple[int, int] | None = None
    boundary_anchors: tuple[str, str] | None = None


# ---------------------------------------------------------------------------
# tolerant JSON extraction
# ---------------------------------------------------------------------------


def _json_object_candidates(text: str) -> Iterator[str]:
    """Yield every balanced ``{...}`` substring, left to right.

    Brace depth is tracked with string/escape awareness so braces *inside* JSON
    strings do not unbalance the scan. An opener that never closes yields nothing
    and the scan advances to the next ``{`` (stray prose braces are skipped).
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escaped = False
        for pos in range(start, len(text)):
            ch = text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : pos + 1]
                    break
        start = text.find("{", start + 1)


def _coerce_edit(raw: Mapping) -> QualityEdit | None:
    """Normalise one raw edit mapping, or ``None`` when its shape is unusable."""
    find = raw.get("find")
    replace = raw.get("replace")
    if not isinstance(find, str) or not isinstance(replace, str):
        return None
    count = raw.get("count", 1)
    if count is None:
        count = 1
    # bool is an int subclass in Python — reject it explicitly.
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        return None
    return QualityEdit(find=find, replace=replace, count=count)


def _failure(reason: str) -> CriticVerdict:
    """A rejected parse — no trusted verdict, marker set, human reason."""
    return CriticVerdict(reason=reason, parse_failed=True)


def parse_critic_output(text: str | None) -> CriticVerdict:
    """Parse a critic reply tolerantly; reject (never raise) on any bad shape.

    Takes the first balanced JSON object that decodes to an object. A reply with
    no usable object, a missing/missing-typed/unknown ``verdict``, a non-list
    ``edits``, or an edit without string ``find``/``replace`` returns a
    :class:`CriticVerdict` with ``parse_failed`` true and ``verdict == ""``.
    """
    if not isinstance(text, str) or not text.strip():
        return _failure("empty critic output")

    payload: dict | None = None
    for candidate in _json_object_candidates(text):
        try:
            decoded = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(decoded, dict):
            payload = decoded
            break
    if payload is None:
        return _failure("no balanced JSON object found")

    raw_verdict = payload.get("verdict")
    if not isinstance(raw_verdict, str):
        return _failure("missing or non-string `verdict`")
    verdict = raw_verdict.strip().lower()
    if verdict not in VERDICTS:
        return _failure(f"unknown verdict {raw_verdict!r}")

    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        reason = ""
    regen_feedback = payload.get("regen_feedback", "")
    if not isinstance(regen_feedback, str):
        regen_feedback = ""

    raw_edits = payload.get("edits", [])
    if raw_edits is None:
        raw_edits = []
    if not isinstance(raw_edits, list):
        return _failure("`edits` must be a list")

    edits: list[QualityEdit] = []
    for item in raw_edits:
        if not isinstance(item, Mapping):
            return _failure("each `edits` entry must be an object")
        edit = _coerce_edit(item)
        if edit is None:
            return _failure("each edit needs string `find`/`replace` and a positive int `count`")
        edits.append(edit)

    return CriticVerdict(
        verdict=verdict,
        reason=reason,
        edits=tuple(edits),
        regen_feedback=regen_feedback,
    )


# ---------------------------------------------------------------------------
# segment helpers
# ---------------------------------------------------------------------------


def _copy_segment(segment) -> dict:
    """A fresh copy of one segment dict (non-mappings pass through by value)."""
    if isinstance(segment, Mapping):
        return dict(segment)
    return segment


def _is_prose(segment) -> bool:
    """True for an editable ``{"kind": "prose", "text": str}`` segment."""
    return (
        isinstance(segment, Mapping)
        and segment.get("kind") == _PROSE
        and isinstance(segment.get("text"), str)
    )


def _join_prose(segments: Sequence) -> str:
    """The joined prose channel (``"\\n".join`` of prose segment texts)."""
    return "\n".join(seg["text"] for seg in segments if _is_prose(seg))


#: Characters of the prose channel's head a rejection quotes back.
_PROSE_HEAD_CHARS = 120

#: Characters of each prose segment's edge quoted when a ``find`` straddles a
#: boundary — either edge is a copyable ``find`` on its own.
_BOUNDARY_ANCHOR_CHARS = 80


def _clip_head(text: str, limit: int = _PROSE_HEAD_CHARS) -> str:
    """``text`` clipped to ``limit`` characters, ``…`` marking the cut tail."""
    return text if len(text) <= limit else text[:limit] + "…"


def _clip_tail(text: str, limit: int = _BOUNDARY_ANCHOR_CHARS) -> str:
    """``text`` clipped to ``limit`` characters, ``…`` marking the cut head."""
    return text if len(text) <= limit else "…" + text[-limit:]


def _prose_head(segments: Sequence) -> str:
    """Head of the prose channel a patch actually searched — never DSL text.

    A rejection quotes THIS as the searched text. Quoting only the attempted
    ``find`` let a DSL ``find`` read back as though the prose had been searched
    and come up empty (live 98e092bf: 14 identical retries of a DSL edit).
    """
    for segment in segments:
        if _is_prose(segment) and segment["text"].strip():
            return _clip_head(segment["text"].strip())
    return ""


def _find_elsewhere(segments: Sequence, find: str) -> tuple[int, str] | None:
    """``(index, kind)`` of the first NON-prose segment holding ``find``.

    A ``find`` that is verbatim DSL source is a channel mistake, not a typo: the
    patcher rewrites prose only. Reporting it as "not present in prose" told the
    model its DSL text was simply mis-copied, so it re-copied it for 158s.
    """
    for index, segment in enumerate(segments):
        if not isinstance(segment, Mapping) or _is_prose(segment):
            continue
        text = segment.get("text")
        if isinstance(text, str) and find in text:
            return index, str(segment.get("kind"))
    return None


def _crossing(segments: Sequence, find: str) -> tuple[int, int, str, str] | None:
    """The prose pair a ``find`` straddles, with a copyable anchor per side.

    Prose segments are joined with ``"\\n"`` (:func:`_join_prose`), so a ``find``
    copied from the rendered prose can include a boundary that no single segment
    contains. Returns ``(left_index, right_index, left_anchor, right_anchor)``:
    the part of ``find`` that DOES live in each neighbour, clipped at its outer
    edge, so the next attempt has an exactly-matchable ``find``.
    """
    prose = [
        (index, segment["text"])
        for index, segment in enumerate(segments)
        if _is_prose(segment)
    ]
    for (left, left_text), (right, right_text) in zip(prose, prose[1:]):
        position = (left_text + "\n" + right_text).find(find)
        if position == -1:
            continue
        if position >= len(left_text):
            # Starts inside the right segment — impossible when the find matched
            # neither segment (the caller's premise), so keep the indices honest.
            continue
        inside_left = find[: len(left_text) - position]
        inside_right = find[len(inside_left) + 1 :]
        return (
            left,
            right,
            _clip_tail(inside_left or left_text),
            _clip_head(inside_right or right_text, _BOUNDARY_ANCHOR_CHARS),
        )
    return None


def _locate(segments: Sequence, find: str) -> tuple[int, int, int]:
    """Count ``find`` over prose segments.

    Returns ``(first_segment_index, total_occurrences, segments_containing)``.
    ``find`` must land wholly inside ONE prose segment for a patch to be valid.
    """
    first = -1
    total = 0
    containing = 0
    for index, segment in enumerate(segments):
        if not _is_prose(segment):
            continue
        count = segment["text"].count(find)
        if count:
            if first == -1:
                first = index
            containing += 1
            total += count
    return first, total, containing


def _rejected(
    original: list[dict],
    prose: str,
    failed_index: int | None,
    error: str,
    *,
    prose_head: str = "",
    elsewhere: tuple[int, str] | None = None,
    boundary: tuple[int, int] | None = None,
    boundary_anchors: tuple[str, str] | None = None,
) -> PatchResult:
    """A rejected patch — the untouched segments/prose, never a partial apply."""
    return PatchResult(
        ok=False,
        segments=original,
        prose=prose,
        failed_index=failed_index,
        error=error,
        prose_head=prose_head,
        elsewhere=elsewhere,
        boundary=boundary,
        boundary_anchors=boundary_anchors,
    )


# ---------------------------------------------------------------------------
# the patch applier
# ---------------------------------------------------------------------------


def apply_patch(
    segments: Sequence[Mapping] | None,
    edits: Sequence[QualityEdit | Mapping] = (),
) -> PatchResult:
    """Apply critic edits to prose segments atomically; never a partial patch.

    Only ``kind == "prose"`` segments are editable (DSL and unknown kinds are
    copied through byte-identical). Each edit's ``find`` must be a literal
    substring present in exactly one prose segment, exactly ``count`` times
    across the whole prose; otherwise the WHOLE patch is rejected and the
    pristine inputs are returned. On success ``segments`` is the rebuilt payload
    and ``prose`` its joined channel.

    Applying an identical patch twice is safe: the first application consumes its
    ``find`` text, so the second simply fails to match and is rejected (never a
    double substitution).

    A miss is DIAGNOSED, not merely reported (:class:`PatchResult`): a ``find``
    that is verbatim text of a non-prose segment reports ``elsewhere``, one that
    only matches across the ``"\\n"`` join reports ``boundary`` with a copyable
    anchor per side, and every rejection carries ``prose_head`` — the head of the
    prose actually searched, never the text of a channel this cannot edit.
    """
    original = [_copy_segment(segment) for segment in (segments or [])]
    original_prose = _join_prose(original)

    normalised: list[QualityEdit] = []
    for index, raw in enumerate(edits or ()):
        if isinstance(raw, QualityEdit):
            edit: QualityEdit | None = raw
        elif isinstance(raw, Mapping):
            edit = _coerce_edit(raw)
        else:
            edit = None
        if edit is None or not edit.find:
            return _rejected(original, original_prose, index, "edit is not a valid find/replace")
        normalised.append(edit)

    # A second, independent copy: edits mutate this scratch, failures return the
    # pristine `original` above (so the caller's input is never touched).
    working = [_copy_segment(segment) for segment in (segments or [])]

    for index, edit in enumerate(normalised):
        target, total, containing = _locate(working, edit.find)
        if total == 0:
            # Three different misses, three different answers — a model that is
            # told the wrong one retries the wrong edit (live 98e092bf).
            elsewhere = _find_elsewhere(working, edit.find)
            if elsewhere is not None:
                where, kind = elsewhere
                return _rejected(
                    original,
                    original_prose,
                    index,
                    f"`find` matches the {kind} segment at index {where}, not prose — "
                    f"only prose segments are editable; a {kind} statement cannot be "
                    "patched with this tool",
                    prose_head=_prose_head(working),
                    elsewhere=elsewhere,
                )
            crossing = _crossing(working, edit.find)
            if crossing is not None:
                left, right, left_anchor, right_anchor = crossing
                return _rejected(
                    original,
                    original_prose,
                    index,
                    f"`find` straddles prose segments {left} and {right} — a patch "
                    "applies inside ONE segment, never across a boundary; aim at "
                    f"one side: {left_anchor!r} (segment {left}) or "
                    f"{right_anchor!r} (segment {right})",
                    prose_head=_prose_head(working),
                    boundary=(left, right),
                    boundary_anchors=(left_anchor, right_anchor),
                )
            return _rejected(
                original,
                original_prose,
                index,
                f"`find` not present in prose: {edit.find!r}",
                prose_head=_prose_head(working),
            )
        if total != edit.count:
            return _rejected(
                original,
                original_prose,
                index,
                f"`find` occurs {total} time(s), expected {edit.count}: {edit.find!r}",
                prose_head=_prose_head(working),
            )
        if containing != 1:
            return _rejected(
                original,
                original_prose,
                index,
                f"`find` spans {containing} prose segments: {edit.find!r}",
                prose_head=_prose_head(working),
            )
        working[target] = dict(working[target])
        working[target]["text"] = working[target]["text"].replace(
            edit.find, edit.replace, edit.count
        )

    return PatchResult(
        ok=True,
        segments=working,
        prose=_join_prose(working),
        applied=len(normalised),
    )
