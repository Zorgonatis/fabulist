"""W8.5 structured-output boundary — the pure, transport-agnostic validation ladder.

One shared boundary in front of every agent that parses a model reply to branch on it
(BTS, extractor, critic). It owns *no* transport and *no* schema: it takes
the raw strings a caller already holds and answers "is this a usable JSON object?".
The per-agent parser stays the schema authority (``parse_bts`` / ``parse_delta`` /
``parse_critic_output``) — this module only guarantees those
parsers see decoded JSON instead of prose.

The spike measured why this is mandatory, not defensive (`the maintainer's private notes (unshipped)`):

* ``response_format={"type":"json_object"}`` is advisory — 1/25 replies returned
  unbalanced JSON braces.
* ``deepseek-flash`` is a reasoning model: at ``max_tokens=600`` it spent 2444 chars on
  reasoning and returned **empty content** with ``finish_reason="length"``. Empty output
  must never decode to ``{}``; length-capped output is untrustworthy (see
  :func:`reasoning_floor`). That measurement is why the ladder opens on a finish-reason
  ALLOWLIST rather than a blocklist of ``length``: a reply the transport did not end the
  ordinary way is not trustworthy even when its JSON happens to close.
* Content-mode JSON arrives fenced *or* bare (the same discipline
  ``graph/nodes.py::_split_unfenced_statements`` applies to the DSL), so both are accepted.
* Streamed tool-call arguments can arrive **truncated mid-JSON**, so
  :func:`parse_tool_args` closes a cut-off payload before giving up.

Failure is one frozen dataclass with a reason code — never a new exception hierarchy.
The calling agent surfaces ``str(failure)`` through its **existing** error class
(``BtsExtractionError`` / ``ExtractionError`` / the critic's ``parse_failed`` marker),
which keeps today's failure handling byte-identical.

No graph imports, no event-log writes, no model calls (mirrors
:mod:`fabulist.graph.quality`'s zero-side-effect contract).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from .extractor import strip_reasoning

__all__ = [
    "BTS_REASONING_MIN_TOKENS",
    "FINISH_REASONS_EXPECTED",
    "NON_REASONING_MIN_TOKENS",
    "REASONING_INTERACTIONS",
    "REASONING_MIN_TOKENS",
    "REASON_CODES",
    "REASON_EMPTY",
    "REASON_FINISH",
    "REASON_MALFORMED",
    "REASON_SHAPE",
    "REASON_TRUNCATED",
    "StructuredFailure",
    "extract_json_object",
    "is_reasoning_model",
    "parse_content_json",
    "parse_tool_args",
    "reasoning_floor",
    "structured_result",
]

# ---------------------------------------------------------------------------
# failure type + reason codes
# ---------------------------------------------------------------------------

#: Nothing to parse: the model returned an empty/whitespace-only string (the spike's
#: reasoning-model trap — 2444 chars of reasoning, empty content, ``finish_reason=length``).
REASON_EMPTY = "empty"
#: ``finish_reason == "length"``: the model hit ``max_tokens`` mid-payload. Truncated
#: structured output is untrustworthy even when it happens to close (spike §4).
REASON_TRUNCATED = "truncated"
#: ``finish_reason`` is a value this boundary does not expect for a whole reply
#: (``content_filter``, ``function_call``, a server-specific variant). The gate is an
#: ALLOWLIST — the owner ruled that an end reason we do not expect discards the reply —
#: so an unlisted value is refused even when its payload parses cleanly.
REASON_FINISH = "finish_reason"
#: No JSON object could be recovered, before or after the single repair attempt.
REASON_MALFORMED = "malformed"
#: The payload decoded but is not a JSON object (tool args can hold any JSON value).
REASON_SHAPE = "shape"

#: Every reason code a :class:`StructuredFailure` may carry.
REASON_CODES = frozenset(
    {REASON_EMPTY, REASON_TRUNCATED, REASON_FINISH, REASON_MALFORMED, REASON_SHAPE}
)


@dataclass(frozen=True)
class StructuredFailure:
    """A structured payload the boundary refused, with a machine-checkable reason.

    ``reason`` is one of :data:`REASON_CODES`; ``detail`` is human-readable and is what
    the calling agent should attach to its own error class or failure event. ``str()``
    renders both, so ``raise BtsExtractionError(str(failure))`` is the whole adaptation
    step.
    """

    reason: str
    detail: str = ""

    def __str__(self) -> str:
        return f"{self.reason}: {self.detail}" if self.detail else self.reason


# ---------------------------------------------------------------------------
# tolerant JSON extraction
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_OPENERS = {"{": "}", "[": "]"}
_CLOSERS = {"}": "{", "]": "["}
#: Bound on the truncation-recovery trim loop (one member dropped per pass).
_MAX_TRIM_ATTEMPTS = 8


def _scan(text: str) -> tuple[list[str], bool, bool]:
    """String/escape-aware scan → (open brackets, ended inside a string, escaped).

    Braces inside JSON strings never affect the depth, so a value like
    ``{"note": "a } b"}`` scans balanced.
    """
    stack: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
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
        elif ch in _OPENERS:
            stack.append(ch)
        elif ch in _CLOSERS and stack and stack[-1] == _CLOSERS[ch]:
            stack.pop()
    return stack, in_string, escaped


def _close_truncated(text: str) -> str:
    """Close whatever a streamed cut left open: the string, a dangling comma, the brackets."""
    stack, in_string, escaped = _scan(text)
    out = text
    if in_string:
        if escaped:
            # The cut landed on a backslash; it would escape the quote we add.
            out += "\\"
        out += '"'
    stripped = out.rstrip()
    if stripped.endswith(","):
        stripped = stripped[:-1]
    if not stack:
        return stripped
    return stripped + "".join(_OPENERS[ch] for ch in reversed(stack))


def _trim_last_member(text: str) -> str | None:
    """Drop everything after the last member-separating comma; ``None`` when there is none."""
    depth = 0
    in_string = False
    escaped = False
    last = -1
    for pos, ch in enumerate(text):
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
        elif ch in _OPENERS:
            depth += 1
        elif ch in _CLOSERS:
            depth -= 1
        elif ch == "," and depth >= 1:
            last = pos
    if last <= 0:
        return None
    return text[:last]


def _recover_truncated(text: str) -> Any:
    """Decode JSON cut off mid-payload by closing it; raise ``ValueError`` if hopeless.

    Closes the open string/brackets first; when the cut still lands on an incomplete
    member (``{"a": 1, "b":``) it walks back to the last member boundary and retries,
    bounded by :data:`_MAX_TRIM_ATTEMPTS`.
    """
    candidate = text
    last_error: Exception | None = None
    for _ in range(_MAX_TRIM_ATTEMPTS):
        try:
            return json.loads(_close_truncated(candidate))
        except ValueError as exc:
            last_error = exc
        trimmed = _trim_last_member(candidate)
        if trimmed is None:
            break
        candidate = trimmed
    raise ValueError(f"unbalanced JSON braces: {last_error}")


#: Sentinel for "nothing decoded" — a valid JSON ``null`` must not be confused with failure.
_NO_DECODE = object()


def _try_decode(text: str, *, close_truncated: bool, tips: list[str]) -> Any:
    """``json.loads``, then optionally one truncation-closing pass. ``_NO_DECODE`` = nothing."""
    try:
        return json.loads(text)
    except (ValueError, TypeError) as exc:
        tips.append(str(exc))
    if close_truncated:
        try:
            return _recover_truncated(text)
        except (ValueError, TypeError) as exc:
            tips.append(str(exc))
    return _NO_DECODE


def _fenced_blocks(text: str) -> Iterator[str]:
    for match in _FENCE_RE.finditer(text):
        body = match.group(1).strip()
        if body:
            yield body


def _object_spans(text: str) -> Iterator[tuple[int, int]]:
    """Yield ``(start, end)`` for every **top-level** ``{`` in ``text``, left to right.

    ``end`` is one past the matching ``}``, or ``len(text)`` when that payload never
    closes (a streamed cut). Depth is tracked with string/escape awareness, so braces
    inside JSON strings never unbalance the scan. Only an opener seen at depth 0 starts a
    candidate: an object nested inside an *unclosed* opener is not a payload —
    ``{"state": {...}, "beats": [{"a": 1}`` must be repaired as a whole, never silently
    accepted as its inner ``{"a": 1}``. A top-level array swallows its contents for the
    same reason (this boundary is object-shaped). Same discipline as
    ``graph/quality.py::_json_object_candidates``, reimplemented here because this module
    must not import from ``graph``.
    """
    depth = 0
    in_string = False
    escaped = False
    start = -1
    for pos, ch in enumerate(text):
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
        elif ch in _OPENERS:
            if depth == 0:
                start = pos if ch == "{" else -1
            depth += 1
        elif ch in _CLOSERS and depth:
            depth -= 1
            if depth == 0:
                if start != -1:
                    yield start, pos + 1
                start = -1
    if start != -1:
        yield start, len(text)


def extract_json_object(text: str) -> str | None:
    """The outermost ``{...}`` slice of ``text`` (from the first top-level ``{``), or ``None``.

    A balanced payload ends at its matching ``}``; a truncated one runs to the **end of
    the text** so the truncation-closing repair can close it (slicing to the last ``}``
    instead would silently drop every member after the last closed inner object:
    ``{"state": {...}, "beats": [{"a": 1`` → just ``{"state": {...}}``). This is the
    repair step's candidate.
    """
    if not isinstance(text, str):
        return None
    for start, end in _object_spans(text):
        return text[start:end]
    return None


# ---------------------------------------------------------------------------
# the per-source parsers
# ---------------------------------------------------------------------------


def parse_tool_args(raw: str) -> Any | StructuredFailure:
    """Parse a function-call ``arguments`` string (Tier A) into a JSON value.

    ``json.loads`` first, then one balanced-brace recovery pass for **truncated** JSON —
    streamed tool-call arguments can arrive cut off mid-object, and a cut-off payload
    that closes cleanly is far better than failing the turn. Any decoded JSON value is
    returned (the boundary in :func:`structured_result` requires an object); a string
    that will not decode even after recovery is :data:`REASON_MALFORMED`.
    """
    if not isinstance(raw, str) or not raw.strip():
        return StructuredFailure(REASON_EMPTY, "tool-call arguments are empty")
    text = strip_reasoning(raw).strip()
    if not text:
        return StructuredFailure(REASON_EMPTY, "tool-call arguments carry only reasoning")
    tips: list[str] = []
    value = _try_decode(text, close_truncated=True, tips=tips)
    if value is not _NO_DECODE:
        return value
    return StructuredFailure(
        REASON_MALFORMED, "; ".join(tips) or "tool-call arguments are not JSON"
    )


def parse_content_json(text: str) -> Any | StructuredFailure:
    """Parse a Tier B/C content reply: fenced ```json block, or a bare unfenced object.

    Everything the boundary accepts is a JSON **object**. Fenced bodies come first (the
    model was asked for ``json_object``), then a top-level balanced-brace scan catches a
    bare object inside prose — the same "statements may arrive unfenced" discipline the
    DSL segmenter applies. Truncation closing is deliberately *not* part of this direct
    parse: it belongs to the ladder's single repair attempt (:func:`structured_result`).
    """
    if not isinstance(text, str) or not text.strip():
        return StructuredFailure(REASON_EMPTY, "content is empty")
    cleaned = strip_reasoning(text)
    if not cleaned.strip():
        return StructuredFailure(REASON_EMPTY, "content carries only reasoning wrappers")
    tips: list[str] = []
    for body in _fenced_blocks(cleaned):
        value = _try_decode(body, close_truncated=False, tips=tips)
        if value is not _NO_DECODE:
            return value
    for start, end in _object_spans(cleaned):
        value = _try_decode(cleaned[start:end], close_truncated=False, tips=tips)
        if value is not _NO_DECODE:
            return value
    return StructuredFailure(
        REASON_MALFORMED, "; ".join(tips) or "no JSON object found in content"
    )


def _repair_once(raw: str) -> Any:
    """The one repair attempt: strip reasoning, take the outermost object, close a cut tail.

    Returns the decoded value, or ``None`` when there is no outermost object to repair
    (a slice derived from a ``{`` cannot itself decode to ``null``, so ``None`` is
    unambiguous here).
    """
    outer = extract_json_object(strip_reasoning(raw))
    if outer is None:
        return None
    value = _try_decode(outer, close_truncated=True, tips=[])
    return None if value is _NO_DECODE else value


# ---------------------------------------------------------------------------
# the ladder
# ---------------------------------------------------------------------------

#: Finish reasons that mean "the transport delivered the whole reply": the two clean
#: terminal reasons an OpenAI-compatible server emits (``stop`` for content, ``tool_calls``
#: for Tier A). ``None``/``""`` pass too — a stream may end without a stop frame, and a
#: missing reason is not evidence of truncation. Everything else is refused: this is the
#: allowlist the owner asked for, so a provider variant we have never seen fails closed.
FINISH_REASONS_EXPECTED = frozenset({"stop", "tool_calls"})


def structured_result(
    *,
    content: str | None = None,
    tool_args: str | None = None,
    finish_reason: str | None = None,
) -> dict | StructuredFailure:
    """Validate a structured model reply into a JSON object, or refuse it.

    Ladder (in order):

    1. The finish-reason gate — an ALLOWLIST (:data:`FINISH_REASONS_EXPECTED`), checked
       first and unconditionally. ``"length"`` → :data:`REASON_TRUNCATED`: the
       reasoning-model trap, and even a payload that happens to close is not trustworthy
       (spike §4). Any other value we do not expect (``content_filter``, ``function_call``,
       a server-specific variant) → :data:`REASON_FINISH`, whose ``detail`` names it — the
       owner ruled that an end reason we do not expect discards the reply outright.
       ``None``/``""``/``"stop"``/``"tool_calls"`` fall through.
    2. Empty ``tool_args`` **and** empty ``content`` → :data:`REASON_EMPTY`. ``""`` must
       never decode to ``{}``.
    3. Tier A: ``tool_args`` via :func:`parse_tool_args`. Non-empty tool args that will not
       decode degrade to ``content`` for that call (the plan's Tier A→B fallback — a
       reasoning model handed ``tool_choice="auto"`` may answer in prose instead).
    4. Tier B/C: ``content`` via :func:`parse_content_json`.
    5. The decoded value must be an object → else :data:`REASON_SHAPE`.
    6. Still nothing → **one** repair attempt on the primary source (strip reasoning
       wrappers, outermost ``{...}``, close a truncated tail), parity with the extractor's
       existing single repair retry — a second attempt buys latency, not fidelity. Still
       nothing → :class:`StructuredFailure` :data:`REASON_MALFORMED` whose ``detail`` names
       every attempt, ready to surface through the agent's existing error class.

    Shape validation beyond "it is an object" belongs to the per-agent parser (the schema
    authority); this boundary never invents a second schema.
    """
    if finish_reason and finish_reason not in FINISH_REASONS_EXPECTED:
        if finish_reason == "length":
            return StructuredFailure(
                REASON_TRUNCATED,
                "finish_reason=length: the model hit max_tokens before the structured "
                "payload was complete (reasoning models answer only above the token floor)",
            )
        return StructuredFailure(
            REASON_FINISH,
            f"unexpected finish_reason={finish_reason}: not an end reason this boundary "
            "expects (stop/tool_calls, or none reported), so the reply is discarded "
            "even if it parses",
        )

    sources: list[tuple[str, str, Callable[[str], Any]]] = []
    if isinstance(tool_args, str) and tool_args.strip():
        sources.append(("tool_args", tool_args, parse_tool_args))
    if isinstance(content, str) and content.strip():
        sources.append(("content", content, parse_content_json))
    if not sources:
        return StructuredFailure(REASON_EMPTY, "empty content and empty tool-call arguments")

    primary = sources[0][1]
    first_failure: StructuredFailure | None = None
    details: list[str] = []
    for name, source, parse in sources:
        value = parse(source)
        if isinstance(value, dict):
            return value
        failure = (
            value
            if isinstance(value, StructuredFailure)
            else StructuredFailure(
                REASON_SHAPE, f"decoded {type(value).__name__}, not a JSON object"
            )
        )
        if first_failure is None:
            first_failure = failure
        details.append(f"{name} {failure.reason}: {failure.detail}")

    assert first_failure is not None  # sources is non-empty, so the loop ran
    repaired = _repair_once(primary)
    if isinstance(repaired, dict):
        return repaired
    if repaired is None:
        details.append("repair: no outermost JSON object found")
    else:
        details.append(f"repair: decoded {type(repaired).__name__}, not a JSON object")
    return StructuredFailure(first_failure.reason, "; ".join(details))


# ---------------------------------------------------------------------------
# W8.3 — reasoning-budget guard
# ---------------------------------------------------------------------------

#: General floor for a structured pass on a reasoning model (PLAN W8 §4). Measured:
#: deepseek-flash at max_tokens=600 returned EMPTY content (finish_reason=length); 2000
#: answered, 8192 was comfortable. The floor is deliberately above the measured-just-OK
#: value because reasoning tokens are billed as completion and the budget must cover
#: deliberation *plus* the payload.
REASONING_MIN_TOKENS = 4096

#: BTS checkpoint turns emit the largest structured payload, so they get more room.
BTS_REASONING_MIN_TOKENS = 8192

#: No engine-imposed floor for non-reasoning models: the connection sampler governs.
#: Measured: the local llama ``your-model-id`` is not a reasoning model and needs no bump.
NON_REASONING_MIN_TOKENS = 0

#: Interaction id (``prompts/catalog.py``) whose structured pass is the large BTS
#: checkpoint payload. The extractor/critic passes get :data:`REASONING_MIN_TOKENS`.
#: (Speaker attribution is not here: it is deterministic code now.)
REASONING_INTERACTIONS = frozenset({"bts"})

#: Explicit overrides accepted as ``model_hint`` (checked before the name heuristic).
_REASONING_HINTS = frozenset({"reasoning", "think", "thinking", "true", "yes"})
_NON_REASONING_HINTS = frozenset({"non-reasoning", "noreasoning", "none", "no", "false"})

#: Substrings that mark a model id as a reasoning model. Conservative: a false positive
#: only costs tokens (the floor is a clamp-up), a false negative costs an empty reply.
_REASONING_MARKERS = (
    "deepseek",
    "reasoner",
    "reasoning",
    "thinking",
    "qwq",
    "magistral",
    "-r1",
    "r1-",
)


def is_reasoning_model(model_hint: str | None) -> bool:
    """Best-effort "does this model deliberate before answering?".

    ``model_hint`` is a provider model id (``"deepseek-flash"``, ``"your-model-id"``) or one of
    the explicit hints ``"reasoning"``/``"non-reasoning"``. W8.4's capability resolver is
    authoritative; this is the pure fallback for call sites that hold only a name.
    """
    if not isinstance(model_hint, str):
        return False
    hint = model_hint.strip().lower()
    if not hint:
        return False
    if hint in _NON_REASONING_HINTS:
        return False
    if hint in _REASONING_HINTS:
        return True
    return any(marker in hint for marker in _REASONING_MARKERS)


def reasoning_floor(
    model_hint: str | None,
    *,
    interaction: str = "delta",
    current_max_tokens: int | None = None,
) -> int:
    """The ``max_tokens`` a structured pass must request for ``model_hint``.

    Returns the configured value clamped **up** to the reasoning floor, or the floor
    alone when none is configured. :data:`NON_REASONING_MIN_TOKENS` (0) means "no
    engine-imposed floor — request the connection sampler unchanged", so a local
    non-reasoning model (``your-model-id``) keeps today's behaviour. ``interaction`` is the
    interaction id (``"bts"``, ``"extract"``, ...) and selects the floor:
    :data:`BTS_REASONING_MIN_TOKENS` for the large BTS checkpoint payload,
    :data:`REASONING_MIN_TOKENS` otherwise.

    The budget never applies to the narrator — its sampler is prose-bound and free-form.
    """
    if not is_reasoning_model(model_hint):
        return NON_REASONING_MIN_TOKENS
    floor = (
        BTS_REASONING_MIN_TOKENS
        if interaction in REASONING_INTERACTIONS
        else REASONING_MIN_TOKENS
    )
    if isinstance(current_max_tokens, bool) or not isinstance(current_max_tokens, int):
        return floor
    return max(current_max_tokens, floor)
