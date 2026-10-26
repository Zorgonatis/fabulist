"""Post-turn state extraction: prose + user message -> pipe-YAML deltas (PLAN D2, W1.3).

The narrator never emits state (R2). After a kept turn, the extractor reads the user
message and the turn prose and returns a :class:`ExtractorResult`: the changed state keys
as a dict plus the resolved action class.

Two implementations share the :class:`ExtractorFn` shape:

* :class:`LLMExtractor` — calls the ``extractor`` agent through the connection registry
  with a compact embedded system prompt, parses one ``Δ[...]`` line, and repairs once on
  malformed output. :meth:`LLMExtractor.acall` is the async twin the async post_turn
  hook awaits (W5A-2/S5).
* :class:`fabulist.state.rules.RuleBasedExtractor` — no model, keyword/pattern based;
  run through :class:`FallbackExtractor` when the LLM fails (W5A-3/S4d) and used as the
  test oracle.

Delta line format (the log-boundary schema, D2)::

    Δ[time: 14:35 | loc: night market | weather: light rain | tone: warm | inv: +lantern]

Only keys that changed this turn appear; ``Δ[]`` means nothing changed. Values are
atomic *current-value snapshots* (the state after the turn), never transitions:
the old prompt taught ``tone: tense→warm`` arrow syntax, but the projection folds
deltas latest-wins per key without understanding arrows, so arrows accumulate in
every folded value (``tense→warm→playful``) and ``style_tokens()`` — which
case-insensitively exact-matches ``tone`` against :data:`CURATED_PALETTE` — can
never theme an arrowed tone. :func:`parse_delta` still *accepts* legacy arrows
(old logs may carry them) and must not crash; see `the maintainer's private notes (unshipped)`
"Conventions v2".

B4 retirement (Wave 6 / D21)
---------------------------
BTS is the single world-state owner: the post-turn node routes its environment
slice (``loc``/``weather``/``tone``/``inv``) through the ``bts`` interaction and
the extractor's delta emission is **retired in production** (no parallel state
systems). ``action_class`` comes from the deterministic rules classifier in
``pre_turn`` (D3), not from this extractor. Every symbol here stays importable
and tested: the deterministic :class:`~fabulist.state.rules.RuleBasedExtractor`
and :func:`parse_delta` remain the eval-rig oracle (``scripts/eval_extractor.py``,
``tests/fixtures/bts_labeled``), and :class:`LLMExtractor`/:class:`FallbackExtractor`
remain the fallback path when the ``bts`` seam is absent (unit graphs, tests).

Vocabulary gate (W5A-1/S4b): :func:`parse_delta` rejects any key outside
:data:`DELTA_KEYS` and any value structurally bad for its key; the failure is a
:class:`DeltaParseError`, so it routes through the existing repair-retry path and
never escapes the :class:`LLMExtractor` API as anything but
:class:`ExtractionError` (dead-end #6: a hallucinated key used to persist into the
fold forever).
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from ..connections import build_request, get_client
from ..graph.clock import DATE_FORMAT
from ..theme.tokens import CURATED_PALETTE

__all__ = [
    "ACTION_CLASSES",
    "DELTA_KEYS",
    "EXTRACTOR_STRUCTURED_REPAIR_INSTRUCTION",
    "EXTRACTOR_SYSTEM_PROMPT",
    "EXTRACTOR_TOOL_NAME",
    "AsyncExtractor",
    "DeltaParseError",
    "ExtractionError",
    "ExtractorFn",
    "ExtractorResult",
    "FallbackExtractor",
    "InvalidActionClass",
    "LLMExtractor",
    "build_extractor_request_profile",
    "extractor_tool",
    "parse_action_class",
    "parse_delta",
    "parse_extractor_structured",
    "strip_reasoning",
]

#: Action classes the extractor may resolve (shared contract; PLAN W1.3).
ACTION_CLASSES = frozenset({"dialogue", "inspection", "travel", "task", "rest", "other"})

#: The state-delta vocabulary — derived from the pipeline's one fold, not invented.
#:
#: ``state-delta`` payloads fold into one ``state`` mapping
#: (:meth:`fabulist.log.EventLog.get_projection` — ``state.update(payload)``,
#: latest write wins per key) and every consumer reads that same flat key set.
#: ``time`` / ``loc`` / ``weather`` / ``tone`` / ``inv`` are the literal keys
#: ``fabulist.prompts.variables.resolve_variables`` maps out of ``state``
#: (``{"loc": str(state.get("loc"))}`` etc.), the keys
#: ``fabulist.graph.clock.parse_facts``/``render_facts`` round-trip, and the
#: register ``fabulist.theme.tokens.style_tokens`` themes against ``tone``.
#:
#: Order is canonical (prompt order == fold order); membership is what
#: :func:`parse_delta` enforces. An extractor that invents a key
#: (``location``/``whereabouts``) used to persist it into the fold forever
#: (agent I/O audit dead-end #6) because the fold is deliberately open (human
#: state edits via :mod:`fabulist.api.state_api` also append ``state-delta``
#: rows). The gate lives at the extractor boundary, not in the fold.
DELTA_KEYS: tuple[str, ...] = (
    "time",
    "loc",
    "weather",
    "tone",
    "inv",
    "date",
    "day",
    "genre",
)

#: ``date``/``day`` mirror ``time``'s treatment: the vocabulary ACCEPTS them
#: (the clock folds them into the injected facts, so extractor echoes must
#: validate instead of flagging), while the clock stays the authoritative
#: stamper (D21) — the extractor may echo, never invent.
#:
#: ``genre`` (D44) is the fiction's register, captured from the turn like
#: ``tone``: BTS owns no genre category, so the extractor is its only source,
#: and the sidebar's ``facts['genre']`` row reads the fold this key lands in.

#: Membership set for the fold vocabulary (:data:`DELTA_KEYS`).
_DELTA_KEY_SET: frozenset[str] = frozenset(DELTA_KEYS)

#: Tone register taught to the model. Derived from the theme palette's keys so the
#: extractor vocabulary and :func:`fabulist.theme.tokens.style_tokens` can never
#: drift apart: ``style_tokens`` exact-matches ``tone`` against
#: :data:`CURATED_PALETTE`, and any tone the prompt teaches must therefore be a
#: palette key (or a single other word, which deliberately themes neutral).
_TONE_REGISTER: str = " | ".join(sorted(CURATED_PALETTE))

#: System prompt embedded in every extractor request. Compact on purpose: the
#: extractor runs on a fast local model and its only job is one delta line + one
#: action line. State semantics live in the schema, not in prose here.
#:
#: Conventions v2: values are atomic current-value snapshots, not transitions. The
#: old arrow rule ("a transition is an arrow: tone: tense→warm") is deleted —
#: arrows accumulated in folded state and defeated the theme pipeline — and
#: replaced by an explicit prohibition. Everything else (Δ[] form, key
#: vocabulary, sign syntax, two-line contract) is unchanged.
EXTRACTOR_SYSTEM_PROMPT = (
    "You extract state changes from one roleplay turn. You are given the user's "
    "message and the narrator's prose. Output exactly two lines and nothing else.\n"
    "\n"
    "Line 1 — a single state delta: Δ[key: value | key: value]\n"
    f"  - keys MUST come from this vocabulary: {', '.join(DELTA_KEYS)}\n"
    "    (never invent alternate keys like location/whereabouts — use loc)\n"
    "  - include ONLY keys whose value changed this turn\n"
    "  - values are ATOMIC CURRENT-VALUE SNAPSHOTS: the state's value AFTER this "
    "turn, never a transition\n"
    f"  - tone is ONE word from this register when it fits: {_TONE_REGISTER} "
    "(other single words allowed; never a phrase, never arrows)\n"
    "  - never put arrows/transition syntax (→, ->) inside values — write the new "
    "value only\n"
    "  - time is a 24-hour clock reading: time: 14:35\n"
    "  - genre is one short descriptor of the fiction's register: "
    "genre: urban fantasy\n"
    "  - separate entries with ' | '\n"
    "  - output Δ[] when nothing changed\n"
    "  - never invent a change the turn does not show\n"
    "  - inventory changes use a sign: inv: +lantern or inv: -key\n"
    "\n"
    "Line 2 — the user's action class: action: <class>\n"
    "  - <class> is one of: dialogue | inspection | travel | task | rest | other\n"
    "\n"
    "Do not narrate, explain, or add commentary."
)

#: Corrective turn appended after a malformed first reply; one repair attempt only.
REPAIR_INSTRUCTION = (
    "Your previous reply could not be parsed. Reply again with ONLY the two "
    "required lines: a single Δ[...] delta line (use Δ[] if nothing changed), "
    "then a line 'action: <class>' where <class> is one of "
    "dialogue | inspection | travel | task | rest | other."
)

#: The function name the structured extractor pass declares (Tier A function-calling).
EXTRACTOR_TOOL_NAME = "report_state_delta"

#: Corrective turn for the structured (Tier A) transport, BTS parity.
EXTRACTOR_STRUCTURED_REPAIR_INSTRUCTION = (
    "Your previous reply was not a usable structured delta. Call the provided "
    "function again with one valid JSON argument object whose `deltas` holds only "
    "the changed keys from the vocabulary (empty object when nothing changed) and "
    "whose `action_class` is one of dialogue | inspection | travel | task | rest | other."
)


class DeltaParseError(ValueError):
    """No parseable ``Δ[...]`` delta line was found in the extractor output."""


class InvalidActionClass(ValueError):
    """The extractor output carried no action line, or an action class outside the set."""


class ExtractionError(RuntimeError):
    """Extraction failed after the repair retry; carries the raw model output."""

    def __init__(self, message: str, *, raw: str) -> None:
        super().__init__(message)
        self.raw = raw


@dataclass(frozen=True)
class ExtractorResult:
    """One turn's extracted state: changed keys, action class, and confidence."""

    deltas: dict[str, str]
    action_class: str
    confidence: float = 1.0


class ExtractorFn(Protocol):
    """The post-turn extractor seam: ``(user_msg, narrative) -> ExtractorResult``.

    Issue #70: the production renderer is session-aware (the flow builder takes
    an explicit ``session_id``), but the seam keeps the two-argument shape so
    deterministic oracles and test stubs stay valid — the node only passes the
    keyword when the callable declares it.
    """

    def __call__(
        self, user_msg: str, narrative: str, session_id: str | None = None
    ) -> ExtractorResult: ...


# --- tolerant parsing -------------------------------------------------------

# A line that is only a code-fence marker (``` or ```yaml); stripped so a fenced
# delta is parsed like a bare one.
_FENCE_LINE = re.compile(r"^\s*```[A-Za-z0-9_-]*\s*$", re.MULTILINE)
_DELTA_RE = re.compile(r"Δ\[(.*?)\]", re.DOTALL)
_ACTION_RE = re.compile(r"\baction\s*:\s*([A-Za-z_]+)", re.IGNORECASE)
_ARROW_RE = re.compile(r"\s*→\s*")

# --- per-key value structure (S4b) ------------------------------------------
#
# The fold stores values as freeform strings, so the only structurally-bad values
# are those a key's own downstream contract cannot carry:
#
# * every key: an empty value is meaningless (the empty form is ``Δ[]``).
# * ``time``: the clock consumes it as a 24h ``HH:MM`` reading
#   (:func:`fabulist.graph.clock._parse_time`; a non-clock string silently snaps
#   the fiction to ``08:00`` and corrupts the advance table).
# * ``inv``: the documented syntax is signed tokens (``+lantern -torch``), which
#   :class:`fabulist.state.rules.RuleBasedExtractor` also emits.
#
# ``loc`` / ``weather`` / ``tone`` are deliberately freeform: multi-word values
# (``night market``, ``light rain``) are the canonical form and an unmatched
# ``tone`` legally themes neutral (:func:`fabulist.theme.tokens.style_tokens`),
# so they are NOT gated. Tone arrows stay accepted as legacy (see the module
# docstring / ``the maintainer's private notes (unshipped)`` Conventions v2).
_TIME_VALUE_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_INV_TOKEN_RE = re.compile(r"^[+-]\S+$")


def _validate_value(key: str, value: str) -> None:
    """Raise :class:`DeltaParseError` when ``value`` is structurally bad for ``key``."""
    if not value:
        raise DeltaParseError(f"empty value for delta key {key!r}")
    if key == "time":
        match = _TIME_VALUE_RE.match(value)
        if match is None or int(match.group(1)) > 23 or int(match.group(2)) > 59:
            raise DeltaParseError(
                f"time value {value!r} is not a 24h HH:MM clock reading"
            )
    elif key == "date":
        try:
            datetime.strptime(value, DATE_FORMAT)
        except ValueError:
            raise DeltaParseError(
                f"date value {value!r} is not a '{DATE_FORMAT}' date "
                "(e.g. 'Monday, June 02, 2025')"
            ) from None
    elif key == "day":
        if not value.isdigit() or int(value) < 1:
            raise DeltaParseError(
                f"day value {value!r} is not a positive 1-based day number"
            )
    elif key == "inv":
        for token in value.split():
            if not _INV_TOKEN_RE.match(token):
                raise DeltaParseError(
                    f"inv value {value!r} must be signed tokens like '+lantern -torch'"
                )


# Reasoning wrappers are built from chr(60) so the literal control-token sequences
# never appear in this source file.
_LT = chr(60)
_GT = chr(62)
_THINK_TAG = _LT + "think"
_THINK_CLOSE = _LT + "/think"
_REASONING_TAG = _LT + "reasoning"
_REASONING_CLOSE = _LT + "/reasoning"

_THINK_FENCE_RE = re.compile(r"```think\b.*?```", re.DOTALL | re.IGNORECASE)
_THINK_BLOCK_RE = re.compile(
    re.escape(_THINK_TAG) + r"\b[^" + _GT + r"]*" + _GT + r".*?" + re.escape(_THINK_CLOSE) + _GT,
    re.DOTALL | re.IGNORECASE,
)
_REASONING_BLOCK_RE = re.compile(
    re.escape(_REASONING_TAG) + r"\b[^" + _GT + r"]*" + _GT + r".*?" + re.escape(_REASONING_CLOSE) + _GT,
    re.DOTALL | re.IGNORECASE,
)


def strip_reasoning(text: str) -> str:
    """Remove reasoning wrappers from model output: think tags, ```` ```think ````

    fences, and reasoning blocks. Whatever remains is what parsing sees.
    """
    text = _THINK_FENCE_RE.sub("", text)
    text = _THINK_BLOCK_RE.sub("", text)
    text = _REASONING_BLOCK_RE.sub("", text)
    return text


def parse_delta(text: str) -> dict[str, str]:
    """Parse the first ``Δ[...]`` line from ``text`` into an ordered dict.

    Tolerant by design: the delta may sit anywhere in the output (prose or a code
    fence around it). Entries are split on ``|`` and each on its first ``:``;
    whitespace around ``→`` is normalized; ``Δ[]`` yields ``{}``.

    Gates the delta at the extractor boundary (W5A-1 / S4b): a key outside
    :data:`DELTA_KEYS`, or a value structurally bad for its key (see
    :func:`_validate_value`), raises :class:`DeltaParseError`. The API never
    raises anything else, so the caller's repair-retry path handles it exactly
    like the pre-existing malformed-output case.

    Raises :class:`DeltaParseError` when no ``Δ[...]`` line is present, or when
    the line names an unknown key / carries a structurally-bad value.
    """
    cleaned = _FENCE_LINE.sub("", strip_reasoning(text))
    match = _DELTA_RE.search(cleaned)
    if match is None:
        raise DeltaParseError(f"no Δ[...] delta line found in output: {text!r}")

    parsed: dict[str, str] = {}
    for chunk in match.group(1).split("|"):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        key, value = chunk.split(":", 1)
        key = key.strip()
        value = _ARROW_RE.sub("→", value.strip())
        if not key:
            continue
        if key not in _DELTA_KEY_SET:
            raise DeltaParseError(
                f"unknown delta key {key!r}; allowed keys: {', '.join(DELTA_KEYS)}"
            )
        _validate_value(key, value)
        if key in parsed:
            # Repeated key (live-observed: ``Δ[inv: +lantern | inv: -torch]``)
            # MERGES — a dict overwrite silently dropped the first change
            # (eval failure mode #2). Space-joined preserves both.
            parsed[key] = f"{parsed[key]} {value}".strip()
        else:
            parsed[key] = value
    return parsed


def parse_action_class(text: str) -> str:
    """Extract and validate the ``action: <class>`` line from extractor output."""
    match = _ACTION_RE.search(strip_reasoning(text))
    if match is None:
        raise InvalidActionClass(f"no action line found in output: {text!r}")
    action_class = match.group(1).lower()
    if action_class not in ACTION_CLASSES:
        raise InvalidActionClass(
            f"action class {action_class!r} is not one of {sorted(ACTION_CLASSES)}"
        )
    return action_class


# --- W8.x: the structured (Tier A) contract ---------------------------------
#
# The extractor's structured transport mirrors the BTS lane (state/bts.py): a tool
# declaration carrying the state-delta fields the extractor emits today, a
# capability-resolved request profile, and a JSON-schema-authority parser. The
# boundary in fabulist.state.structured guarantees only "a usable JSON object";
# parse_extractor_structured is the schema authority and reuses the SAME gates
# parse_delta applies (DELTA_KEYS membership + _validate_value), so the persisted
# state-delta payload stays byte-compatible with the notation path.


def extractor_tool() -> dict[str, Any]:
    """The Tier A tool declaration for the extractor pass (``report_state_delta``).

    ``deltas`` mirrors the notation delta's key/value shape exactly (the same
    :data:`DELTA_KEYS` vocabulary and atomic-snapshot string values) and
    ``action_class`` mirrors the ``action: <class>`` line, so the payload
    :func:`parse_extractor_structured` maps is the one the fold already expects.
    """
    return {
        "type": "function",
        "function": {
            "name": EXTRACTOR_TOOL_NAME,
            "description": (
                "Report this turn's state delta and the user's action class."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "deltas": {
                        "type": "object",
                        "description": (
                            "Only keys whose value changed this turn, each to its "
                            "atomic current-value snapshot string. Vocabulary: "
                            f"{', '.join(DELTA_KEYS)}. Empty object when nothing changed."
                        ),
                        "additionalProperties": {"type": "string"},
                    },
                    "action_class": {
                        "type": "string",
                        "enum": sorted(ACTION_CLASSES),
                        "description": "The user's action class for this turn.",
                    },
                },
                "required": ["deltas", "action_class"],
            },
        },
    }


def build_extractor_request_profile(
    capabilities: dict[str, bool]
) -> dict[str, Any] | None:
    """Tier A request profile for the extractor pass, or ``None`` (= notation path).

    ``None`` is returned when the connection cannot do function-calling: the
    extractor then keeps today's notation behaviour, the permanent Tier D floor
    (D69). Mirror of :func:`fabulist.state.bts.build_bts_request_profile`.
    """
    if not capabilities.get("function_calling"):
        return None
    profile: dict[str, Any] = {"tools": [extractor_tool()]}
    # W9.14 (T1b measured): this llama.cpp build ENFORCES "required" as a
    # grammar, but treats {"type":"function","function":{"name":...}} as a
    # hint — the model complied only ~1/3 of the time (9 live turns, identical
    # prompt: 6 notation / 3 tool calls). The profile declares exactly ONE
    # tool, so "required" is unambiguous and costs nothing.
    if capabilities.get("tool_choice_forced"):
        profile["tool_choice"] = "required"
    else:
        profile["tool_choice"] = "auto"
    return profile


def _structured_value(value: object) -> str:
    """A delta value as the notation parser would have stored it (a stripped string)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def parse_extractor_structured(
    payload: dict[str, Any], *, confidence: float = 1.0
) -> ExtractorResult:
    """Map a validated structured payload into :class:`ExtractorResult`.

    This is the JSON schema authority for the extractor (the boundary in
    :mod:`fabulist.state.structured` only guarantees "a usable JSON object").
    It reuses the notation path's own gates — :data:`DELTA_KEYS` membership and
    :func:`_validate_value` — so a payload that would parse as a ``Δ[...]`` line
    produces the identical ``ExtractorResult`` (byte-compatible state-delta).
    Key/action failures raise the SAME errors ``parse_delta``/``parse_action_class``
    raise (:class:`DeltaParseError` / :class:`InvalidActionClass`), so they ride the
    caller's existing repair ladder and never a new error class.

    A missing ``deltas`` field is an empty delta (``Δ[]``); an unknown key or a
    structurally bad value is a :class:`DeltaParseError`; a missing/unknown action
    class is an :class:`InvalidActionClass`.
    """
    if not isinstance(payload, dict):
        raise DeltaParseError(f"structured payload is not an object: {payload!r}")
    raw_deltas = payload.get("deltas")
    deltas: dict[str, str] = {}
    if raw_deltas is not None:
        if not isinstance(raw_deltas, dict):
            raise DeltaParseError(
                f"structured deltas must be an object, got {type(raw_deltas).__name__}"
            )
        for raw_key, raw_value in raw_deltas.items():
            key = str(raw_key).strip()
            if not key:
                continue
            if key not in _DELTA_KEY_SET:
                raise DeltaParseError(
                    f"unknown delta key {key!r}; allowed keys: {', '.join(DELTA_KEYS)}"
                )
            value = _structured_value(raw_value)
            _validate_value(key, value)
            if key in deltas:
                # Repeated key MERGES, exactly like parse_delta's live-observed case.
                deltas[key] = f"{deltas[key]} {value}".strip()
            else:
                deltas[key] = value
    raw_action = payload.get("action_class")
    action_class = raw_action.strip().lower() if isinstance(raw_action, str) else ""
    if action_class not in ACTION_CLASSES:
        raise InvalidActionClass(
            f"action class {raw_action!r} is not one of {sorted(ACTION_CLASSES)}"
        )
    return ExtractorResult(deltas=deltas, action_class=action_class, confidence=confidence)


def _turn_block(user_msg: str, narrative: str) -> str:
    return f"USER MESSAGE:\n{user_msg}\n\nNARRATIVE:\n{narrative}"


# --- LLM-backed extractor ---------------------------------------------------


class LLMExtractor:
    """Call the ``extractor`` connection and parse one delta + action turn.

    The client is resolved lazily through :func:`fabulist.connections.get_client`
    and the request through :func:`fabulist.connections.build_request`, so both
    the registry merge order and the request shape are the real production ones.
    ``client``/``http_client`` are injection seams for tests.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        registry: Any | None = None,
        http_client: Any | None = None,
        system_prompt: str | None = None,
        messages_renderer: Any | None = None,
        log: Any | None = None,
        profile_fn: Any | None = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._http_client = http_client
        # I1/D23: when supplied, the extractor's per-call usage is persisted as
        # a system/usage event on the turn's session (CURRENT_SESSION).
        self._log = log
        # D18 prompt surface: an active preset's prompt_overrides["extractor_system"]
        # replaces the compiled default; None keeps conventions v2.
        self._system_prompt = system_prompt
        # D18 completion: full module-flow renderer (user_msg, narrative) ->
        # messages; None keeps the fixed two-message shape.
        self._messages_renderer = messages_renderer
        # W8.x: the per-call structured transport profile (BTS parity). ``None``
        # keeps the notation path byte-for-byte; a Tier A profile switches this
        # call to function-calling. It is a callable so a re-bound preset agent
        # or a hot-reloaded connection is honoured per turn.
        self._profile_fn = profile_fn

    def _resolve_client(self) -> Any:
        if self._client is None:
            self._client = get_client(
                "extractor", registry=self._registry, http_client=self._http_client
            )
        return self._client

    def _complete(self, messages: list[dict[str, str]]) -> str:
        import time

        client = self._resolve_client()
        request = build_request("extractor", messages, registry=self._registry)
        # Extraction must be low-variance (D2: the Δ line is a commitment, not
        # prose). The shared your-model-id connection samples at 0.85 for narration;
        # override per-call regardless of the connection default.
        request = {**request, "temperature": 0.1}
        started = time.monotonic()
        response = client.chat.completions.create(**request)
        self._record_usage(response, request, started, messages)
        return response.choices[0].message.content or ""

    async def _complete_async(self, messages: list[dict[str, str]]) -> str:
        """Async twin of :meth:`_complete`: block only the network round trip.

        The OpenAI SDK call is synchronous, so :func:`asyncio.to_thread` moves
        just the HTTP call off the event loop (W5A-2 / S5); usage recording stays
        on the loop thread because the shared :class:`fabulist.log.EventLog`
        sqlite connection is created on — and must be used from — that thread.
        """
        import time

        client = self._resolve_client()
        request = build_request("extractor", messages, registry=self._registry)
        request = {**request, "temperature": 0.1}
        started = time.monotonic()
        response = await asyncio.to_thread(
            lambda: client.chat.completions.create(**request)
        )
        self._record_usage(response, request, started, messages)
        return response.choices[0].message.content or ""

    def _record_usage(
        self,
        response: Any,
        request: dict[str, Any],
        started: float,
        messages: list[dict[str, str]],
    ) -> None:
        """Persist this call's usage; measurement only, never fatal.

        W9.25 §3.5.1 (P2-6 fold): this seam calls ``build_request`` +
        ``get_client`` directly and bypasses :class:`RegistryModelCaller`, so the
        prompt-budget estimate + warn lives here, BESIDE the usage row, against
        the same connection's declared Context Size. One over-budget call = one
        ``prompt_budget`` row; no declared window = no math (today's behavior).
        """
        try:
            import time

            from fabulist.agents.debug import CURRENT_SESSION
            from fabulist.graph.compaction import warn_prompt_budget
            from fabulist.usage.track import record_usage

            session_id = CURRENT_SESSION.get("")
            connection = self._connection()
            if connection is not None:
                warn_prompt_budget(
                    self._log,
                    session_id=session_id,
                    agent="extractor",
                    context_window=connection.context_window,
                    response_length=request.get("max_tokens"),
                    messages=messages,
                )
            usage = getattr(response, "usage", None)
            record_usage(
                self._log,
                session_id=session_id,
                agent="extractor",
                model=str(request.get("model") or getattr(response, "model", "") or "extractor"),
                prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
                completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
                latency_s=time.monotonic() - started,
            )
        except Exception:  # noqa: BLE001 - usage is measurement, never fatal
            pass

    def _connection(self) -> Any | None:
        """The extractor connection (for its declared window), or ``None``.

        ``registry`` is the production handle (wiring always passes one); a bare
        extractor with no registry keeps today's behavior — no window knowledge,
        no row.
        """
        if self._registry is None:
            return None
        try:
            return self._registry.connection_for("extractor")
        except Exception:  # noqa: BLE001 - unknown slot = no window math
            return None

    def _messages(
        self, user_msg: str, narrative: str, session_id: str | None = None
    ) -> list[dict[str, str]]:
        if self._messages_renderer is not None:
            # D18 completion: the extract interaction's module flow compiles
            # the messages (preset-customized or default); the repair path
            # below still appends its assistant+REPAIR messages after.
            # Issue #70: the renderer takes the turn's session so a swallowed
            # preset read leaves its ``preset_degraded`` row on the right log.
            if session_id is None:
                return list(self._messages_renderer(user_msg, narrative))
            return list(
                self._messages_renderer(user_msg, narrative, session_id)
            )
        return [
            {"role": "system", "content": self._system_prompt or EXTRACTOR_SYSTEM_PROMPT},
            {"role": "user", "content": _turn_block(user_msg, narrative)},
        ]

    def _profile(self) -> dict[str, Any] | None:
        """The Tier A transport profile for this call, or ``None`` (notation path)."""
        if self._profile_fn is None:
            return None
        return self._profile_fn()

    def __call__(
        self, user_msg: str, narrative: str, session_id: str | None = None
    ) -> ExtractorResult:
        messages = self._messages(user_msg, narrative, session_id)
        profile = self._profile()
        if profile is not None:
            return self._structured_call(messages, profile)
        raw = self._complete(messages)
        try:
            return self._parse(raw, confidence=1.0)
        except (DeltaParseError, InvalidActionClass) as first_error:
            self._append_repair(messages, raw)
            repaired = self._complete(messages)
            try:
                return self._parse(repaired, confidence=0.5)
            except (DeltaParseError, InvalidActionClass) as repair_error:
                raise ExtractionError(
                    f"extractor output unparseable after repair: {repair_error}",
                    raw=repaired,
                ) from first_error

    async def acall(
        self, user_msg: str, narrative: str, session_id: str | None = None
    ) -> ExtractorResult:
        """Async :meth:`__call__` for the async post_turn hook (W5A-2 / S5)."""
        messages = self._messages(user_msg, narrative, session_id)
        profile = self._profile()
        if profile is not None:
            return await self._structured_call_async(messages, profile)
        raw = await self._complete_async(messages)
        try:
            return self._parse(raw, confidence=1.0)
        except (DeltaParseError, InvalidActionClass) as first_error:
            self._append_repair(messages, raw)
            repaired = await self._complete_async(messages)
            try:
                return self._parse(repaired, confidence=0.5)
            except (DeltaParseError, InvalidActionClass) as repair_error:
                raise ExtractionError(
                    f"extractor output unparseable after repair: {repair_error}",
                    raw=repaired,
                ) from first_error

    # -- W8.x: structured (Tier A) transport ---------------------------------

    def _structured_call(
        self, messages: list[dict[str, str]], profile: dict[str, Any]
    ) -> ExtractorResult:
        """Tier A sync: drain the function-call args, validate, one repair retry.

        The boundary (:func:`fabulist.state.structured.structured_result`) owns
        the ladder — empty content, ``finish_reason=length``, a non-object payload
        and the single repair attempt — and this method owns only the transport. A
        payload the boundary accepts is mapped by :func:`parse_extractor_structured`,
        whose :class:`DeltaParseError`/:class:`InvalidActionClass` rides the same
        repair ladder. Exhausted -> :class:`ExtractionError`, exactly the error the
        notation path raises, so :class:`FallbackExtractor` degrades to the rules
        pass and the turn is never failed.
        """
        result, failure, raw = self._attempt_structured(messages, profile, 1.0)
        if result is not None:
            return result
        self._append_repair(messages, raw, EXTRACTOR_STRUCTURED_REPAIR_INSTRUCTION)
        result, repaired_failure, repaired_raw = self._attempt_structured(
            messages, profile, 0.5
        )
        if result is not None:
            return result
        detail = repaired_failure or failure
        raise ExtractionError(
            f"extractor structured output unusable after repair: {detail}",
            raw=repaired_raw,
        )

    async def _structured_call_async(
        self, messages: list[dict[str, str]], profile: dict[str, Any]
    ) -> ExtractorResult:
        """Async twin of :meth:`_structured_call` (W5A-2 thread seam)."""
        result, failure, raw = await self._attempt_structured_async(messages, profile, 1.0)
        if result is not None:
            return result
        self._append_repair(messages, raw, EXTRACTOR_STRUCTURED_REPAIR_INSTRUCTION)
        result, repaired_failure, repaired_raw = await self._attempt_structured_async(
            messages, profile, 0.5
        )
        if result is not None:
            return result
        detail = repaired_failure or failure
        raise ExtractionError(
            f"extractor structured output unusable after repair: {detail}",
            raw=repaired_raw,
        )

    def _attempt_structured(
        self, messages: list[dict[str, str]], profile: dict[str, Any], confidence: float
    ) -> tuple[ExtractorResult | None, Any, str]:
        tool_args, content, finish_reason = self._complete_structured(messages, profile)
        return self._resolve_structured(tool_args, content, finish_reason, confidence)

    async def _attempt_structured_async(
        self, messages: list[dict[str, str]], profile: dict[str, Any], confidence: float
    ) -> tuple[ExtractorResult | None, Any, str]:
        tool_args, content, finish_reason = await self._complete_structured_async(
            messages, profile
        )
        return self._resolve_structured(tool_args, content, finish_reason, confidence)

    def _resolve_structured(
        self,
        tool_args: str,
        content: str,
        finish_reason: str | None,
        confidence: float,
    ) -> tuple[ExtractorResult | None, Any, str]:
        # Lazy import: structured.py imports strip_reasoning from this module.
        from .structured import StructuredFailure, structured_result

        payload = structured_result(
            content=content, tool_args=tool_args, finish_reason=finish_reason
        )
        raw = tool_args or content or ""
        if isinstance(payload, StructuredFailure):
            # Tier A -> D degrade: a model that ignored the tool and answered in
            # the notation still parses with today's parser, so a provider whose
            # function-calling misbehaves degrades to today's behaviour instead of
            # failing the turn (D69: Tier D is the permanent floor).
            try:
                return self._parse(raw, confidence=confidence), None, raw
            except (DeltaParseError, InvalidActionClass):
                return None, payload, raw
        try:
            return (
                parse_extractor_structured(payload, confidence=confidence),
                None,
                raw,
            )
        except (DeltaParseError, InvalidActionClass) as exc:
            return None, StructuredFailure("shape", str(exc)), raw

    def _complete_structured(
        self, messages: list[dict[str, str]], profile: dict[str, Any]
    ) -> tuple[str, str, str | None]:
        import time

        client = self._resolve_client()
        request = build_request(
            "extractor", messages, registry=self._registry, request_profile=profile
        )
        request = {**request, "temperature": 0.1}
        started = time.monotonic()
        response = client.chat.completions.create(**request)
        self._record_usage(response, request, started, messages)
        return self._structured_completion(response)

    async def _complete_structured_async(
        self, messages: list[dict[str, str]], profile: dict[str, Any]
    ) -> tuple[str, str, str | None]:
        import time

        client = self._resolve_client()
        request = build_request(
            "extractor", messages, registry=self._registry, request_profile=profile
        )
        request = {**request, "temperature": 0.1}
        started = time.monotonic()
        response = await asyncio.to_thread(
            lambda: client.chat.completions.create(**request)
        )
        self._record_usage(response, request, started, messages)
        return self._structured_completion(response)

    @staticmethod
    def _structured_completion(response: Any) -> tuple[str, str, str | None]:
        """The raw field set the boundary consumes: ``(tool_args, content, finish_reason)``."""
        choice = response.choices[0]
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None) or ""
        tool_args = ""
        for call in getattr(message, "tool_calls", None) or ():
            function = getattr(call, "function", None)
            arguments = getattr(function, "arguments", None) if function else None
            if isinstance(arguments, str) and arguments.strip():
                tool_args = arguments
                break
        return tool_args, content, getattr(choice, "finish_reason", None)

    @staticmethod
    def _append_repair(
        messages: list[dict[str, str]],
        raw: str,
        instruction: str = REPAIR_INSTRUCTION,
    ) -> None:
        messages.extend(
            [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": instruction},
            ]
        )

    @staticmethod
    def _parse(raw: str, *, confidence: float) -> ExtractorResult:
        deltas = parse_delta(raw)
        action_class = parse_action_class(raw)
        return ExtractorResult(
            deltas=deltas, action_class=action_class, confidence=confidence
        )


# --- async + deterministic-fallback wrappers --------------------------------


class AsyncExtractor:
    """Adapt an :class:`ExtractorFn` to an awaitable for the async post_turn hook.

    ``post_turn`` is async and already awaits an awaitable extractor result
    (``fabulist.graph.nodes.make_post_turn_node``:
    ``if inspect.isawaitable(outcome): outcome = await outcome``). The extractor
    seam is synchronous today, so this adapter exposes the same call as a
    coroutine. It prefers the inner's own ``acall`` (W5A-2):
    :meth:`LLMExtractor.acall` moves only the blocking HTTP call off the event
    loop with :func:`asyncio.to_thread` while usage/log writes stay on the loop
    thread. A plain sync inner with no ``acall`` (rule-based oracle, test stubs)
    is run whole in a worker thread — it touches no sqlite-backed log. Either way
    two concurrent turns no longer serialize on the event loop.

    :func:`fabulist.connections.get_client` returns the sync ``OpenAI`` client
    (the factory is not async and this lane does not own it), so the thread seam
    is used rather than duplicating the connection wiring. The wrapped extractor
    keeps its synchronous ``__call__`` for the eval scripts and the rule-based
    oracle.
    """

    def __init__(self, inner: ExtractorFn) -> None:
        self._inner = inner

    async def __call__(
        self, user_msg: str, narrative: str, session_id: str | None = None
    ) -> ExtractorResult:
        acall = getattr(self._inner, "acall", None)
        if acall is not None:
            if session_id is None:
                return await acall(user_msg, narrative)
            return await acall(user_msg, narrative, session_id)
        if session_id is None:
            return await asyncio.to_thread(self._inner, user_msg, narrative)
        return await asyncio.to_thread(self._inner, user_msg, narrative, session_id)


class FallbackExtractor:
    """Primary extractor + deterministic rule-based fallback (W5A-3 / S4d).

    READ verdict: :class:`fabulist.state.rules.RuleBasedExtractor` is
    deterministic, cheap, and emits only fold keys (``inv``/``loc``), so it is
    WIRED here rather than deleted (the audit's dead-end #5 was its zero
    production callers). When the primary raises — :class:`LLMExtractor` raises
    :class:`ExtractionError` after its one repair retry, which ``nodes.py`` used
    to record as ``extraction_failed`` and then drop to clock-only state — the
    rule-based pass runs instead and its deltas/action class are used. A
    ``system``/``extractor`` ``extraction_failed`` event carries the fallback
    source and the rule-derived deltas, so the failure stays visible in the log
    (D9) while the turn keeps its derivable state.

    Broad ``except Exception`` mirrors ``nodes.py``'s own "extractor failure is
    logged, not fatal" contract: any primary failure degrades to the rules pass
    rather than dropping the turn's derivable state.
    """

    def __init__(
        self,
        primary: ExtractorFn,
        fallback: ExtractorFn | None = None,
        *,
        log: Any | None = None,
    ) -> None:
        self._primary = primary
        if fallback is None:
            # Lazy import: rules.py imports ExtractorResult from this module.
            from .rules import RuleBasedExtractor

            fallback = RuleBasedExtractor()
        self._fallback = fallback
        self._log = log

    def __call__(
        self, user_msg: str, narrative: str, session_id: str | None = None
    ) -> ExtractorResult:
        try:
            if session_id is None:
                return self._primary(user_msg, narrative)
            return self._primary(user_msg, narrative, session_id)
        except Exception as exc:  # noqa: BLE001 - deterministic fallback, never fatal
            result = self._fallback(user_msg, narrative)
            self._note_fallback(exc, result)
            return result

    async def acall(
        self, user_msg: str, narrative: str, session_id: str | None = None
    ) -> ExtractorResult:
        """Async fallback path for the async post_turn hook (W5A-2 / W5A-3).

        Prefers the primary's own async method so its network I/O runs off the
        loop while log writes stay on the loop thread; the rule-based fallback
        and the event append both run on the loop thread.
        """
        try:
            primary = getattr(self._primary, "acall", None)
            if session_id is None:
                if primary is not None:
                    return await primary(user_msg, narrative)
                return await asyncio.to_thread(self._primary, user_msg, narrative)
            if primary is not None:
                return await primary(user_msg, narrative, session_id)
            return await asyncio.to_thread(
                self._primary, user_msg, narrative, session_id
            )
        except Exception as exc:  # noqa: BLE001 - deterministic fallback, never fatal
            result = self._fallback(user_msg, narrative)
            self._note_fallback(exc, result)
            return result

    def _note_fallback(self, exc: Exception, result: ExtractorResult) -> None:
        """Append a ``system`` event naming the fallback source, best-effort.

        Reuses the existing ``extraction_failed`` event name
        (``nodes.py`` emitted it when the extractor raised) and adds a
        ``fallback`` field plus the rule-derived deltas, so the LLM failure stays
        on the log (D9) even though the turn no longer loses its derivable state.
        """
        if self._log is None:
            return
        try:
            from fabulist.agents.debug import CURRENT_SESSION

            session_id = CURRENT_SESSION.get("")
            if not session_id:
                return
            self._log.append_event(
                session_id,
                "system",
                "extractor",
                {
                    "event": "extraction_failed",
                    "fallback": "rules",
                    "error": str(exc)[:500],
                    "deltas": dict(result.deltas),
                },
            )
        except Exception:  # noqa: BLE001 - the fallback result is the deliverable
            pass
