"""The bounded agent-loop runner: agent + tools -> final response (W9.24 §3.2).

One component, deliberately boring and transport-agnostic: it drives the
existing streaming caller seam (``fabulist.wiring.RegistryModelCaller``'s
``{"type": ..., "delta": ...}`` protocol, whose types are ``content``,
``tool_call``, ``reasoning`` and ``error`` — plus the terminal ``finish``
side-band, which carries ``finish_reason`` and no delta) around a
caller-supplied tool set. It contains ZERO domain logic — every tool is
a thin wrapper over an existing deterministic module (``graph/validator.py``,
``graph/quality.apply_patch``, ``state/cast.roster``, ...), supplied by the
caller. The first customer is the narrator repair loop (W9.24 §3.3); the second
is the BTS read-tools loop (§3.4).

Protocol
--------
One completion. If it carried ``tool_call`` fragments, EVERY call they group into
runs ENGINE-SIDE, one ``role: "tool"`` result per call, and the loop runs another
completion. A completion with no tool call is the final answer. The transport
groups the call fragments by ``meta["index"]``; a transport that stamps no index
is one call (the shape every single-tool customer produces).

``on_completion(text)`` is an engine hook run after EVERY completion (the
narrator's deterministic validate step). It may return :class:`LoopInjection` to
engage the loop when the model did not call a tool itself — the draft needed a
deterministic nudge. The injected messages are a SYNTHETIC PAIR (R2 review fold,
§3.2): an assistant message carrying
``tool_calls: [{"id": "engine_flags", ..., "arguments": "{}"}]`` immediately
followed by the ``role: "tool"`` result that references ``engine_flags``. A BARE
tool message referencing nothing is forbidden — it is dropped or 400s on strict
transports. When the injection follows a text completion, that text rides its own
assistant message ahead of the pair (the pinned §3.3.2 shape: "append the draft
as an assistant message and the synthetic flags pair"), so the model still sees
the draft it is being asked to repair.

Owner rulings this module implements (do not reintroduce the retired patterns)
-----------------------------------------------------------------------------
* **NO iteration caps.** Models close out properly; a bounded loop would cut
  legitimate runs short.
* **Hang protection is PER API CONNECTION** (owner ruling 2026-10-20). Every
  model call rides the caller's own transport timeout (``Connection.timeout``,
  enforced by the transport — your-model-id 900s). A timed-out call raises through, the
  loop records the failed iteration as a ``timeout`` row and returns the last
  complete text with ``timed_out=True`` on its ``final`` row. **NO loop-total
  ``asyncio.wait_for`` exists anywhere here** — the run-total bound is FORBIDDEN
  (it was measured killing legitimate multi-lane runs the day it shipped).
  ``timeout_s`` is accepted only as PROVENANCE: the bound the calls rode, kept on
  the trace so an inspector reads "this call rode 900s and timed out" without
  opening the connection config. It is never enforced here.
* Deterministic tool ``fn``s are our own code, not provider calls, so they get a
  plain engine-side execution guard (:data:`TOOL_EXECUTION_GUARD_SECONDS`).
* **D9 (the event log owns everything).** The RUNNER is engine code and appends
  one schema-gated ``agent_loop`` row per measured step; the model never writes
  the log. Rows already appended stay (append-only). Narrative corrections are
  NOT made here — they remain ``append_revision`` (D10) business.
* **Window headroom is PASSIVE observability (W9.25 §3.5.3, owner ruling on F1).**
  When the caller declares a Context Size the rows carry ``headroom`` (available
  prompt tokens − estimate) and the loop PROCEEDS regardless — no stop condition,
  no overflow catch, no iteration cap. The compaction-aware answer is W9.26;
  until it lands the transport's own behavior is the only bound.
* **Prompt-cache contract (AGENTS 6).** Iteration N+1's message list is iteration
  N's list + the assistant turn and tool messages appended at the TAIL. The
  stable prefix (system, cast block, snapshot) is byte-identical across iterations
  by construction: this module only ever appends, never rewrites.
* **The capabilities ladder is the caller's choice at the seam.** The runtime
  never guesses: it builds the request profile from the caller-supplied
  ``capabilities`` (``resolve_capabilities``) through :func:`loop_request_profile`,
  and when function calling is unavailable it performs ONE completion and returns
  with ``looped=False`` so the caller applies its own degrade (narrator: today's
  one-shot text repair; BTS: the Tier D notation floor, D69). ``tool_choice`` is
  always ``"auto"`` — a forced choice would trap the model in the tool instead of
  letting it close out.
* **Reasoning is echoed back inside the loop, by default (owner report,
  DeepSeek thinking mode).** A thinking model is stateless between requests like
  every other model here, and DeepSeek's thinking mode REQUIRES the previous
  assistant turn's ``reasoning_content`` to travel back with its ``tool_calls``
  — omit it and the next request is a 400 ("The `reasoning_content` in the
  thinking mode must be passed back to the API"). So the loop carries each
  completion's ``reasoning`` deltas onto the assistant ``tool_calls`` message it
  appends for the next iteration. The rule is :func:`should_echo_reasoning` and
  it is deliberately tri-state: AUTO (``return_thinking is None``, the default)
  echoes whatever the provider actually returned, ``True`` forces that same
  echo, ``False`` never sends the field. **PRESENCE and TEXT are different
  facts** and the loop keeps them apart (``reasoning: str | None``, ``None`` =
  the provider never sent the field): the runtime NEVER invents reasoning and
  never pads it, but it resends the provider's own value verbatim — INCLUDING
  an empty string when an empty string is what the provider sent — and a
  non-reasoning provider's message list is byte-identical to before this
  existed (no field arrived, so no field goes back).

  Measured provider fact (live probe, DeepSeek ``deepseek-flash``, thinking
  mode): the rule is enforced on EVERY assistant message of a request that
  carries ``tools`` — a ``tool_calls`` turn, an ENGINE-authored one (the
  synthetic ``engine_flags`` pair the narrator repair seeds), and a content-only
  DRAFT turn that performed no tool call (issue #42 live 400, DeepSeek-V4.1-Flash:
  the draft was the only assistant message without the field). **A continuation
  turn frequently reasons NOTHING and still returns the field**: the raw
  stream's opening frame is ``{"delta": {"role": "assistant", "content": null,
  "reasoning_content": ""}}``, later frames omit the key, and the closing frame
  carries ``"reasoning_content": null``. That empty string must travel back
  verbatim — the vendor rejects an ABSENT field (400) but accepts ``""`` (200)
  — which is why the echo is decided by presence and not by truthiness: reading
  ``if reasoning:`` here is what made the planner's seed phase 400 on turn 1.

  **Presence is ALL-OR-NOTHING per REQUEST, not per message.** Measured on
  ``deepseek-flash`` with ``tools`` on the request: no assistant message carrying
  the field is 200, EVERY assistant message carrying it (``""`` or text) is 200,
  and a MIXED request (some yes, some no) is a 400 with the same "must be passed
  back to the API" body; without ``tools`` a mixed request is 200. What the vendor
  validates is the CONSISTENCY of field presence across the request's assistant
  messages, so a conversation that mixes turns with stored reasoning and turns
  without (a session started on a non-thinking connection then switched, an older
  session, a provider that sometimes omits the field) used to 400 on its next
  narrator turn. That gap is closed ONCE PER REQUEST by
  :func:`normalise_reasoning_presence`, which :func:`run_agent_loop` calls
  immediately before the conversation is handed to the model caller — the only
  place that sees the whole request, the loop's own appends included. It only
  FILLS blanks (``""``) when at least one assistant message already carries the
  field; a request with no field anywhere is left byte-identical (non-thinking
  providers, older sessions), and a present field's value is never rewritten.
  Nothing outside ``role == "assistant"`` is touched, so the prompt cache's
  stable prefix and append-only tail (AGENTS 6) are undisturbed.

  **The CURRENT round is carried VERBATIM, in full (owner ruling).** Every
  iteration of the running round keeps its own ``reasoning_content`` on its
  assistant message, exactly as the provider sent it, one block per iteration —
  nothing is blanked as the next iteration is stamped, and every one of those
  blocks is PAID FOR as prompt tokens. That is deliberate: a repair round must
  see its own deliberation, so the model asked to fix its draft sees the thinking
  that produced it. The trim that saves the unbounded cost is at the ROUND
  boundary, not the iteration boundary: prior graph executions (past turns) are
  re-attached by history assembly with the field PRESENT but EMPTY (``""``) — a
  past turn's reasoning is stored for diagnosis and is never re-sent as text
  (``wiring.history_from_log``). The vendor's presence rule is enforced on the
  REQUEST (all-or-nothing, above): the FIELD travels on every assistant message
  of a request that carries it anywhere, and a conversation that never carried
  it keeps a byte-identical message list.

Deviations (grounded in the code as read, per the "be faithful" instruction)
---------------------------------------------------------------------------
* **Tool-call attribution.** ``RegistryModelCaller`` surfaces function-call
  ARGUMENT fragments — no ``id`` — so the loop resolves the called tool from
  ``event["meta"]["name"]`` when a transport provides it, and otherwise from the
  single declared tool (unambiguous by construction). With several tools and no
  surfaced name the call is unattributable: its row records that and the call is
  refused by its own ``tool_result`` while the rest of the completion's calls run
  (a lone such call is the fail-open case — the completion's text is the answer).
* **Parallel tool calls are grouped, never concatenated.** The transport stamps
  the provider's grouping index on every fragment (``meta["index"]``), because a
  provider streams several calls INTERLEAVED and their fragments are otherwise
  indistinguishable: concatenating them glued three calls into one unparseable
  argument string (a live-gate defect). The loop therefore executes every call of
  a completion in index order, appending ONE assistant message carrying all of
  them and ONE ``role: "tool"`` message per result — a malformed or unnameable call
  is refused individually and never blocks its siblings.
* **The hook is ``on_completion(text)``**, not the plan sketch's
  ``on_completion(text, segments_like)``: the loop drains ``content`` deltas into
  the joined text (exactly what ``_drain_lane`` hands the narrator node today),
  and the customer re-segments it with ``segment_narrative`` — its canonical
  path. Forwarding segments through the runtime would duplicate segmentation the
  runtime does not own.
* ``LoopTool``/``LoopInjection``/``LoopResult``/``LoopIteration`` are frozen
  dataclasses rather than the plan's mapping sketch: same field names and values,
  typed access, and the values stay JSON-safe where they reach the log.
* The injected pair is built here, from ``LoopInjection.content`` (the flags
  payload the model reads back) — the runtime owns the message shape so the
  customer cannot drift from the pinned pair.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fabulist.log.schema_gate import AGENT_LOOP_EVENT, validate_agent_loop_payload

logger = logging.getLogger(__name__)

__all__ = [
    "ENGINE_FLAGS_CALL_ID",
    "ENGINE_FLAGS_TOOL",
    "LOOP_EVENT_TEXT_MAX_CHARS",
    "LOOP_ACTOR",
    "TOOL_EXECUTION_GUARD_SECONDS",
    "LoopInjection",
    "LoopIteration",
    "LoopResult",
    "LoopTool",
    "is_transport_timeout",
    "loop_request_profile",
    "normalise_reasoning_presence",
    "run_agent_loop",
    "should_echo_reasoning",
]

#: Every text field carried on an ``agent_loop`` row is clipped here (W9.24
#: §3.2) — a defect turn's draft must stay diagnosable, not a log-bloat vector.
LOOP_EVENT_TEXT_MAX_CHARS = 2000

#: Execution guard for ONE deterministic tool call. Tools are engine code (local
#: patches, state reads), not provider calls, so their bound is ours to set; it is
#: not a loop or iteration bound and never touches model latency.
TOOL_EXECUTION_GUARD_SECONDS = 30.0

#: The synthetic call id the engine's injected flags pair references. Fixed so a
#: log reader tells an engine nudge from a model-initiated call at a glance.
ENGINE_FLAGS_CALL_ID = "engine_flags"

#: The tool name the injected flags pair declares. It names the deterministic
#: validate step the model is being handed; the model never calls it itself.
ENGINE_FLAGS_TOOL = "report_narrative_flags"

#: Actor on every ``agent_loop`` row: the runtime is engine code (D9).
LOOP_ACTOR = "engine"

#: The refusal one UNNAMEABLE call is recorded and answered with: the transport
#: announced no name and the tool set cannot resolve one, so nothing is
#: executable. Same vocabulary the row and the tool result carry (one spelling).
_UNATTRIBUTABLE_TOOL_CALL = "unattributable tool call"


@dataclass(frozen=True)
class LoopTool:
    """One tool the model may call: a thin wrapper over an existing module.

    ``fn`` is ``async`` in the plan's sketch and may be a plain ``def`` in
    practice (``quality.apply_patch`` and the validator are sync, pure
    functions); a sync ``fn`` runs in a worker thread so
    :data:`TOOL_EXECUTION_GUARD_SECONDS` can actually bound it. It receives the
    parsed argument object and returns a dict — whatever the model should read
    back about the execution.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[[dict[str, Any]], Any]

    def declaration(self) -> dict[str, Any]:
        """The OpenAI-compatible tool declaration this tool rides the wire as."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class LoopInjection:
    """An engine-authored nudge: the ``on_completion`` hook's non-final answer.

    ``content`` is the payload handed back as the synthetic tool's result (the
    narrator's flags report). The assistant side of the pair — the call id, the
    declared tool name and its empty ``arguments`` — is pinned by the runtime, not
    by the hook.
    """

    content: str


@dataclass(frozen=True)
class LoopIteration:
    """What one completion did, summarized for the caller's trace.

    ``reasoning`` is the completion's ``reasoning_content`` exactly as the
    provider sent it — ``""`` when it sent an empty one, ``None`` when it never
    sent the field at all. It rides the iteration for inspection only: the
    ``agent_loop`` rows keep their schema, and the wire echo is decided by
    :func:`should_echo_reasoning` from the connection's ``return_thinking``.
    """

    iteration: int
    text: str
    tool: str | None
    tool_args: str | None
    injected: bool
    latency_s: float
    reasoning: str | None = None


@dataclass(frozen=True)
class LoopResult:
    """The loop's answer plus the trace behind it.

    ``text`` is the last complete completion's text (a repair iteration that
    emitted no text never erases the draft it repaired). ``tool_args`` is the raw
    argument JSON of the last tool call executed, or ``None`` — a caller that
    treats the call itself as the answer (BTS's ``report_world_state``) reads it
    there. ``looped`` records whether the capabilities ladder engaged function
    calling at all; ``timed_out`` records that a transport timeout ended the loop.

    ``finish_reason`` is the terminal reason the LAST completion reported — the
    provider's own value off the terminal ``finish`` side-band frame (or the
    ``length`` an ``error`` frame already carried), ``None`` when the transport
    reported none at all (issue #28). It is the loop's record of the same fact its
    rows carry, and it is the ONLY copy when the caller passed ``log=None``; an
    absent reason is not evidence of truncation (ADR-021), so a reader decides the
    allowlist question, the loop does not.
    """

    text: str
    tool_args: str | None
    looped: bool
    iterations: tuple[LoopIteration, ...]
    timed_out: bool
    finish_reason: str | None = None


def is_transport_timeout(exc: BaseException) -> bool:
    """True when ``exc`` is a connection-timeout failure (duck-typed, no imports).

    The transports this seam rides (``openai``/``httpx``) each name their timeout
    class differently — ``APITimeoutError``, ``ReadTimeout``, ``TimeoutException``
    — and none of them is the builtin, so the class name is the portable signal.
    The cause/context chain is walked because a client may wrap the original.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, TimeoutError):  # builtin (== asyncio.TimeoutError)
            return True
        if any("timeout" in klass.__name__.lower() for klass in type(current).__mro__):
            return True
        current = current.__cause__ or current.__context__
    return False


def should_echo_reasoning(return_thinking: bool | None, reasoning: str | None) -> bool:
    """Does this iteration's assistant tool-call message carry ``reasoning_content``?

    The one decision point for the reasoning echo (the connection's tri-state
    "Return thinking" setting):

    * ``False`` — never: for a provider that rejects the field outright.
    * ``None`` (AUTO, the default) and ``True`` — echo it, but ONLY when the
      provider actually sent the field. AUTO is what makes a thinking model with
      tools work with nothing configured: DeepSeek's thinking mode 400s when the
      ``reasoning_content`` behind a ``tool_calls`` turn is dropped ("must be
      passed back to the API").

    ``reasoning`` is the provider's own value, and its TYPE carries the two
    distinct facts: ``None`` means the field never arrived (no field goes back —
    the runtime never invents reasoning), while ``""`` means the provider sent an
    empty one and EMPTY must be resent verbatim (a continuation turn that
    reasoned nothing returns exactly that, and omitting it is the 400). Emptiness
    is therefore never a reason to drop the field; absence is the only one.

    The answer is per STAMPED message, and EVERY message the loop stamps inside
    the current round carries the provider's value verbatim (owner ruling: a
    repair round must see its own deliberation). The text stops at the ROUND
    boundary, not between iterations: history assembly re-attaches a prior
    execution's field PRESENT but EMPTY, so older turns contribute no reasoning
    text to the prompt. Stamping alone is not enough, because the vendor's rule
    is per REQUEST and all-or-nothing: a turn that stamps nothing into a
    conversation that already carries the field elsewhere would make the request
    MIXED (a 400 with ``tools``). :func:`normalise_reasoning_presence` closes that
    gap once per request, immediately before the call.
    """
    if reasoning is None:
        return False
    return return_thinking is not False


def normalise_reasoning_presence(
    conversation: list[dict[str, Any]], return_thinking: bool | None = None
) -> bool:
    """Make ``reasoning_content`` presence uniform across ONE request's turns.

    The vendor's rule is per REQUEST, not per message (measured on
    ``deepseek-flash`` with ``tools``: none present = 200, all present = 200,
    MIXED = 400 "The `reasoning_content` in the thinking mode must be passed back
    to the API"). The loop stamps turns as they complete, and history assembly
    stamps past turns that stored reasoning — so a conversation can easily mix
    the two (a session begun on a non-thinking connection, an older session, a
    provider that omits the field on some turns). This pass makes presence
    uniform: if AT LEAST ONE assistant message carries the key, every assistant
    message that lacks it gets the vendor's own "nothing reasoned" value
    (``""``).

    It fills blanks and nothing else: a present field's value is never rewritten
    (``None`` never suppresses one; the echo rule already decided whether the
    field travels), and when NO assistant message carries the field the list is
    left byte-identical — non-thinking providers and older sessions stay exactly
    as they were before the field existed. Only ``role == "assistant"`` messages
    are inspected or touched, and the pass is idempotent, so the append-only
    prompt-cache contract holds (the stable prefix is never rewritten and within
    one round the fill only ever happens before the first call that needs it).

    ``return_thinking=False`` is the owner's "never send this field" switch: the
    loop stamps nothing in that case, so there is nothing for it to normalise and
    the caller's history travels untouched (if that caller-supplied history mixes
    presence, the contradiction is the caller's, not the loop's — the loop does
    not invent a field on a connection that forbids it).

    Returns whether the request carries the field at all after the pass (``False``
    = the list was left byte-identical).
    """
    if return_thinking is False:
        return False
    assistant = [
        message for message in conversation if message.get("role") == "assistant"
    ]
    if not any("reasoning_content" in message for message in assistant):
        return False
    for message in assistant:
        if "reasoning_content" not in message:
            message["reasoning_content"] = ""
    return True


def _echoed_reasoning(return_thinking: bool | None, reasoning: str | None) -> str | None:
    """The value to stamp on a ``tool_calls`` message, or ``None`` for no field.

    :func:`should_echo_reasoning` answers the yes/no; this keeps the two facts
    its answer collapses apart for the wire: ``None`` (no field at all) and
    ``""`` (the provider's own empty string, which must be sent).
    """
    return reasoning if should_echo_reasoning(return_thinking, reasoning) else None


def loop_request_profile(
    capabilities: Mapping[str, bool] | None, tools: Sequence[LoopTool]
) -> dict[str, Any] | None:
    """The transport profile for one loop, or ``None`` (= no function calling).

    ``None`` is the caller's cue to apply its own degrade: the runtime performs ONE
    completion and returns ``looped=False``. Where the connection does resolve
    ``function_calling`` the tools ride with ``tool_choice: "auto"`` — never a
    forced choice (W9.14 measured this build enforcing ``"required"`` as a
    grammar, which would trap the model inside the tool instead of letting it
    close the turn out). An unmeasured capability is False, as
    ``resolve_capabilities`` says.
    """
    if not tools or not (capabilities or {}).get("function_calling"):
        return None
    return {"tools": [tool.declaration() for tool in tools], "tool_choice": "auto"}


async def run_agent_loop(
    *,
    caller: Callable[..., Any],
    messages: list[dict[str, Any]],
    tools: Sequence[LoopTool] = (),
    surface: str,
    log: Any | None,
    session_id: str,
    on_completion: Callable[[str], Any] | None = None,
    timeout_s: float | None = None,
    capabilities: Mapping[str, bool] | None = None,
    window_fn: Callable[[], int | None] | None = None,
    return_thinking: bool | None = None,
) -> LoopResult:
    """Drive ``caller`` around ``tools`` until it answers without a tool call.

    ``caller`` is any streaming caller with the shared W1.1 event protocol
    (``RegistryModelCaller`` in production); it owns the transport, its
    ``Connection.timeout`` and its usage rows. ``messages`` is the caller's prefix
    — never mutated; the loop works on its own copy and only ever appends.
    ``surface`` labels the trace (``"narrate"``, ``"bts"``, ...); ``log`` +
    ``session_id`` are where the ``agent_loop`` rows land (``log=None`` or an
    empty session silences the trace, like ``record_usage``).

    ``window_fn`` (W9.25 §3.5.3): a zero-arg callable returning the AVAILABLE
    prompt tokens for this surface (declared Context Size − Response Length −
    engine reserve), or ``None`` when no window is declared. When it reports a
    number, every row of an iteration carries ``headroom`` (available − estimate),
    which decreases as the append-only tail grows. That is PASSIVE observability
    ONLY: by owner ruling the loop PROCEEDS past zero headroom — no stop
    condition, no heuristic gate, no overflow catch ships here. The proper answer
    (compaction integrated with the loop case) is W9.26; until then the transport's
    own behavior is the only bound.

    ``return_thinking`` is the connection's reasoning-echo setting (``None`` =
    AUTO, the default). Its one reader is :func:`should_echo_reasoning`: the
    assistant ``tool_calls`` message this loop appends for the NEXT request
    repeats the provider's own ``reasoning_content`` whenever the setting allows
    it and the provider sent the field — which is what a thinking model in a tool
    loop requires (DeepSeek 400s without it), and what a non-reasoning provider
    never notices (no field arrived = no field sent). "Sent the field" includes
    an empty one: a continuation turn that reasoned nothing returns
    ``reasoning_content: ""``, and that empty string travels back verbatim.

    EVERY iteration of the current round keeps its own reasoning text verbatim
    (owner ruling: a repair round must see its own deliberation), so the
    conversation carries one FIELD and one paid BLOCK per stamped assistant turn
    of the running round. The round boundary is where the text stops: history
    assembly re-attaches a PRIOR execution's field present-but-empty, never its
    text. Because the vendor validates presence across the WHOLE request rather
    than per message (a mixed tools request is a 400), every request is passed
    through :func:`normalise_reasoning_presence` right before the model call: the
    field is filled in (``""``) on the assistant turns that lack it whenever ANY
    assistant turn carries it, so a caller-supplied history and this round's
    appends never disagree. A request with no field anywhere is not touched.
    ``return_thinking is False`` means the loop stamps nothing, so there is
    nothing to normalise and a caller-supplied field stays the caller's business.

    Only a transport TIMEOUT is recovered (the owner's per-connection doctrine);
    every other failure raises through to the caller's own error handling.
    """
    tool_list = tuple(tools)
    by_name = {tool.name: tool for tool in tool_list}
    profile = loop_request_profile(capabilities, tool_list)
    conversation: list[dict[str, Any]] = [dict(message) for message in messages]

    iterations: list[LoopIteration] = []
    answer = ""
    last_tool_args: str | None = None
    timed_out = False
    iteration = 0
    headroom: int | None = None
    #: The terminal reason the last drained completion reported (issue #28): the
    #: provider's ``finish_reason`` off the terminal ``finish`` side-band frame, or
    #: the ``length`` an ``error`` frame carries. ``None`` = nothing was reported.
    finish_reason: str | None = None

    while True:
        iteration += 1
        # Presence of ``reasoning_content`` is ALL-OR-NOTHING per request at the
        # vendor (a MIXED tools request 400s), so normalise the WHOLE conversation
        # here — immediately before it is handed to the model caller, the one
        # point that sees both the caller's history and this round's appends.
        # Idempotent and fill-only: no field anywhere = list untouched.
        normalise_reasoning_presence(conversation, return_thinking)
        # W9.25 §3.5.3: the headroom the NEXT prompt has, measured before the call.
        # It only ever shrinks (the tail is append-only) and it NEVER stops the loop.
        headroom = _headroom_tokens(window_fn, conversation)
        started = time.monotonic()
        try:
            completion = await _drain_completion(caller, conversation, profile)
        except Exception as exc:  # noqa: BLE001 — narrowed to transport timeouts
            if not is_transport_timeout(exc):
                raise
            _append_row(
                log,
                session_id,
                _row(
                    surface,
                    iteration,
                    "timeout",
                    headroom=headroom,
                    latency_s=float(time.monotonic() - started),
                    error=_clip_text(f"{type(exc).__name__}: {exc}"),
                    **({} if timeout_s is None else {"timeout_s": float(timeout_s)}),
                ),
            )
            timed_out = True
            break
        latency_s = float(time.monotonic() - started)
        # The reason the LAST completion reported, for the result's own record (the
        # rows carry each iteration's; ``log=None`` leaves this as the only copy).
        finish_reason = completion.finish_reason

        if completion.text.strip():
            answer = completion.text
        _append_row(
            log,
            session_id,
            _row(
                surface,
                iteration,
                "completion",
                headroom=headroom,
                latency_s=latency_s,
                text=_clip_text(completion.text),
                **(
                    {}
                    if completion.finish_reason is None
                    else {"finish_reason": completion.finish_reason}
                ),
            ),
        )

        # Every call the completion carried, in index order. The transport groups
        # the streamed fragments by index, so one group is ONE call's argument
        # JSON and N groups are N executions in this iteration — never one glued
        # payload (which is how three parallel calls corrupted the arguments).
        calls = sorted(completion.calls, key=lambda call: call.index)
        names = [_resolve_call_name(call, tool_list) for call in calls]
        injection = await _run_hook(on_completion, completion.text)

        # ONE resolvable call is the historic shape; SEVERAL always execute (each
        # gets its own tool message), even when one of them cannot be named.
        if len(calls) > 1 or (calls and names[0] and calls[0].args):
            # ONE assistant turn carries every call (the OpenAI-compatible shape),
            # then ONE ``role: "tool"`` message per call referencing its own id. A
            # call the engine refuses still gets its result, so the conversation
            # stays valid and the other calls run untouched.
            call_ids = _call_ids(iteration, len(calls))
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": completion.text,
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": call_name, "arguments": call.args},
                    }
                    for call_id, call, call_name in zip(call_ids, calls, names)
                ],
            }
            # The reasoning a thinking model returned rides back on THIS message
            # (its tool_calls turn): DeepSeek's thinking mode 400s when the field
            # is dropped, and every other provider simply never returns any. Every
            # iteration of the round keeps its own block verbatim (owner ruling: a
            # repair round must see its own deliberation).
            echoed = _echoed_reasoning(return_thinking, completion.reasoning)
            if echoed is not None:
                assistant_message["reasoning_content"] = echoed
            conversation.append(assistant_message)
            last_tool_args = calls[-1].args
            for call_id, call, call_name in zip(call_ids, calls, names):
                parsed_args, args_error = _parse_tool_arguments(call.args)
                row = _row(
                    surface,
                    iteration,
                    "tool_call",
                    headroom=headroom,
                    tool=call_name,
                    args=(
                        _clip_mapping(parsed_args)
                        if parsed_args is not None
                        else _clip_mapping({"raw": call.args})
                    ),
                    injected=False,
                )
                if call_name:
                    result, tool_latency_s = await _execute_tool(
                        by_name.get(call_name), call_name, parsed_args, args_error
                    )
                else:
                    # Unnamed with several tools: nothing is executable, so this
                    # one call is refused under the existing "unattributable"
                    # vocabulary and its siblings still run.
                    row["error"] = _UNATTRIBUTABLE_TOOL_CALL
                    result, tool_latency_s = {
                        "ok": False,
                        "error": _UNATTRIBUTABLE_TOOL_CALL,
                    }, 0.0
                _append_row(log, session_id, row)
                _append_row(
                    log,
                    session_id,
                    _row(
                        surface,
                        iteration,
                        "tool_result",
                        headroom=headroom,
                        tool=call_name,
                        result=_clip_mapping(result),
                        latency_s=tool_latency_s,
                        injected=False,
                    ),
                )
                conversation.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": _result_content(result),
                    }
                )
            iterations.append(
                LoopIteration(
                    iteration=iteration,
                    text=completion.text,
                    # One completion is one iteration: the trace describes its
                    # FIRST call (every call has its own row pair above).
                    tool=names[0],
                    tool_args=calls[0].args,
                    injected=False,
                    latency_s=latency_s,
                    reasoning=completion.reasoning,
                )
            )
            continue

        if calls and calls[0].args:
            # The branch above took every multi-call completion, so this is the LONE
            # call that neither the transport nor the tool set can name: nothing is
            # executable, so the text is the answer (fail-open, never a hang).
            _append_row(
                log,
                session_id,
                _row(
                    surface,
                    iteration,
                    "tool_call",
                    headroom=headroom,
                    tool="",
                    args=_clip_mapping({"raw": calls[0].args}),
                    injected=False,
                    error=_UNATTRIBUTABLE_TOOL_CALL,
                ),
            )
            iterations.append(
                LoopIteration(
                    iteration=iteration,
                    text=completion.text,
                    tool="",
                    tool_args=calls[0].args,
                    injected=False,
                    latency_s=latency_s,
                    reasoning=completion.reasoning,
                )
            )
            break

        if injection is not None:
            # The synthetic pair closes a text completion; when that completion's
            # provider also returned reasoning it rides its ``tool_calls`` message
            # by the same rule as a model-issued call (see _injection_messages),
            # verbatim, like every other iteration of the round. The pair is always
            # [draft?] + tool_calls + tool result.
            pair = _injection_messages(
                completion.text,
                injection,
                reasoning=_echoed_reasoning(return_thinking, completion.reasoning),
            )
            conversation.extend(pair)
            _append_row(
                log,
                session_id,
                _row(
                    surface,
                    iteration,
                    "tool_call",
                    headroom=headroom,
                    tool=ENGINE_FLAGS_TOOL,
                    args={},
                    injected=True,
                ),
            )
            _append_row(
                log,
                session_id,
                _row(
                    surface,
                    iteration,
                    "tool_result",
                    headroom=headroom,
                    tool=ENGINE_FLAGS_TOOL,
                    result=_clip_mapping({"content": injection.content}),
                    latency_s=0.0,
                    injected=True,
                ),
            )
            iterations.append(
                LoopIteration(
                    iteration=iteration,
                    text=completion.text,
                    tool=ENGINE_FLAGS_TOOL,
                    tool_args=None,
                    injected=True,
                    latency_s=latency_s,
                    reasoning=completion.reasoning,
                )
            )
            continue

        # No tool call and no engine nudge: this completion is the answer.
        iterations.append(
            LoopIteration(
                iteration=iteration,
                text=completion.text,
                tool=None,
                tool_args=None,
                injected=False,
                latency_s=latency_s,
                reasoning=completion.reasoning,
            )
        )
        break

    _append_row(
        log,
        session_id,
        _row(
            surface,
            iteration,
            "final",
            headroom=headroom,
            text=_clip_text(answer),
            timed_out=timed_out,
        ),
    )
    return LoopResult(
        text=answer,
        tool_args=last_tool_args,
        looped=profile is not None,
        iterations=tuple(iterations),
        timed_out=timed_out,
        finish_reason=finish_reason,
    )


# -- internals ---------------------------------------------------------------


@dataclass(frozen=True)
class _ToolCall:
    """One tool call a completion carried: stream index, name, raw arguments.

    ``index`` is the transport's grouping key (``meta["index"]`` on every
    ``tool_call`` event): fragments sharing an index are ONE call's argument JSON,
    in arrival order, however interleaved the stream was. ``name`` is ``None``
    when the transport never announced one.
    """

    index: int
    name: str | None
    args: str


@dataclass(frozen=True)
class _Completion:
    """One drained completion: text, every tool call it carried, stop reason.

    ``calls`` holds the calls the completion made, in FIRST-SEEN order. A
    completion with no tool-call fragment has no calls; a single-call completion
    has exactly one. ``tool_name``/``tool_args`` mirror the FIRST call — the
    single-call customers (and every tracer reading a lone name/args pair) read
    what they read before parallel calls were grouped at all. ``reasoning`` is the
    provider's ``reasoning_content`` for this completion: the joined deltas, ``""``
    when it sent an empty one, ``None`` when the field never arrived — the value
    :func:`should_echo_reasoning` decides to send back. ``finish_reason`` is how
    the completion ENDED: the terminal ``finish`` side-band's reason (or the
    ``length`` an ``error`` frame carries), ``None`` when nothing was reported.
    """

    text: str
    tool_args: str
    finish_reason: str | None
    tool_name: str | None
    calls: tuple[_ToolCall, ...] = ()
    reasoning: str | None = None


async def _drain_completion(
    caller: Callable[..., Any],
    messages: list[dict[str, Any]],
    request_profile: dict[str, Any] | None,
) -> _Completion:
    """Drain one caller stream into a completion (transport, not parsing).

    Tier A argument fragments win over content (the ``collect_structured`` rule) —
    a function-calling answer carries its payload on ``tool_calls`` and no prose.

    The terminal ``finish_reason`` is read from BOTH frames the shared contract
    allows (issue #28): the terminal ``finish`` side-band frame (top level, or its
    ``meta``) and the ``error`` frame's ``meta`` (where a ``length`` truncation has
    been surfaced since W9.26). The side-band is the last word — it is emitted
    after the error frame — so it wins when both speak, and a ``finish`` frame with
    no reason never erases one an error frame carried. Reading is all the loop
    does with it: recording the reason is what lets a caller apply ADR-021's
    allowlist instead of treating a truncation, a provider content-filter and a
    clean stop as the same thing.

    ``reasoning`` deltas are accumulated verbatim: a thinking model's own
    ``reasoning_content`` is the ONLY thing a provider may be shown back (it is
    never synthesized), so the joined text is what
    :func:`should_echo_reasoning` offers the next request. A ``reasoning`` event
    means the provider SENT the field — the transport emits one even when the
    text is empty — so the joined value is ``""`` for an empty send and ``None``
    when no event ever arrived. Those are different facts at the vendor.
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    reasoning_present = False
    #: Argument fragments per call index, and the index order they were first seen
    #: in. Several parallel calls stream INTERLEAVED, so the fragments are grouped
    #: by ``meta["index"]`` — concatenating them all into one string glued three
    #: calls into one unparseable payload (the live gate's corruption).
    fragments: dict[int, list[str]] = {}
    names: dict[int, str | None] = {}
    finish_reason: str | None = None
    kwargs = {} if request_profile is None else {"request_profile": request_profile}
    async for event in caller(messages, True, **kwargs):
        kind = event.get("type")
        if kind == "content":
            content_parts.append(event.get("delta") or "")
        elif kind == "reasoning":
            reasoning_parts.append(event.get("delta") or "")
            reasoning_present = True
        elif kind == "tool_call":
            meta = event.get("meta") or {}
            index = _call_index(meta)
            fragments.setdefault(index, []).append(event.get("delta") or "")
            names.setdefault(index, None)
            if meta.get("name"):
                names[index] = str(meta["name"])
        elif kind == "finish":
            # Issue #28: the terminal finish side-band. Top level first (the
            # documented carrier), then ``meta`` (the reader also accepts it
            # there, per ``graph.types.StreamEvent``). No delta rides it.
            reason = event.get("finish_reason")
            if reason is None:
                meta = event.get("meta") or {}
                reason = meta.get("finish_reason")
            if reason is not None:
                finish_reason = str(reason)
        elif kind == "error":
            meta = event.get("meta") or {}
            finish_reason = meta.get("finish_reason") or finish_reason
    calls = tuple(
        _ToolCall(index=index, name=names[index], args="".join(parts))
        for index, parts in fragments.items()
    )
    # The first call's name/args stay on the completion itself: the single-tool
    # customers and the trace rows read that pair exactly as they always have.
    return _Completion(
        text="".join(content_parts),
        tool_args=calls[0].args if calls else "",
        finish_reason=finish_reason,
        tool_name=calls[0].name if calls else None,
        calls=calls,
        reasoning="".join(reasoning_parts) if reasoning_present else None,
    )


def _call_index(meta: Mapping[str, Any]) -> int:
    """The grouping index one ``tool_call`` fragment belongs to.

    An absent index (a transport that predates the stamp) or a non-integer one is
    index 0 — one call, the shape every single-tool customer produces.
    """
    index = meta.get("index")
    return index if isinstance(index, int) else 0


def _resolve_call_name(call: _ToolCall, tools: Sequence[LoopTool]) -> str:
    """Which tool ONE call names: the surfaced name, the lone tool, or ``""``.

    ``""`` is the honest third state — a call arrived but nothing can be executed,
    so it is refused by name (and the other calls of the same completion still
    run). A single-declared-tool caller is unambiguous by construction, so an
    unnamed call there falls back to that one tool.
    """
    if call.name:
        return call.name
    if len(tools) == 1:
        return tools[0].name
    return ""


def _call_ids(iteration: int, count: int) -> list[str]:
    """The ``tool_calls`` ids for one completion's calls, in execution order.

    ONE call keeps the historic ``call_{iteration}`` id (the message shape every
    single-tool customer already produces); several calls are numbered so each
    ``role: "tool"`` result references its own call.
    """
    if count == 1:
        return [f"call_{iteration}"]
    return [f"call_{iteration}_{position}" for position in range(count)]


async def _run_hook(
    on_completion: Callable[[str], Any] | None, text: str
) -> LoopInjection | None:
    """Run the engine hook after one completion (sync or async, both accepted)."""
    if on_completion is None:
        return None
    outcome = on_completion(text)
    if inspect.isawaitable(outcome):
        outcome = await outcome
    if outcome is not None and not isinstance(outcome, LoopInjection):
        raise TypeError(
            "on_completion must return LoopInjection | None, got "
            f"{type(outcome).__name__}"
        )
    return outcome


def _injection_messages(
    text: str, injection: LoopInjection, reasoning: str | None = None
) -> list[dict[str, Any]]:
    """The synthetic flags pair (plus the draft it flags), as tail messages.

    The assistant ``tool_calls`` message and its ``role: "tool"`` result are the
    OpenAI-compatible shape ``:4444`` accepts; the result references
    :data:`ENGINE_FLAGS_CALL_ID` so no BARE tool message ever reaches the wire.

    ``reasoning`` is the ``reasoning_content`` of the completion behind this pair
    (``None`` = the provider never sent the field, or the connection's
    ``return_thinking: false`` suppresses it). It is stamped on BOTH assistant
    messages for a tools request — the synthetic ``tool_calls`` turn carries the
    completion's TEXT verbatim (the engine is continuing that assistant turn),
    while the flagged DRAFT turn carries the vendor's empty value, so the same
    block is never billed twice inside one iteration: measured live on
    DeepSeek-V4.1-Flash (2026-10-24), the
    API validates the FIELD on every assistant message of a request that carries
    ``tools`` — including a turn that performed no tool call — and 400s with
    "The `reasoning_content` in the thinking mode must be passed back to the API"
    when the draft is sent content-only. The field is never invented: an empty
    string is the value the provider itself returns, and it is omitted entirely
    when no reasoning arrived (or the owner's switch is OFF).
    """
    messages: list[dict[str, Any]] = []
    if text.strip():
        draft_message: dict[str, Any] = {"role": "assistant", "content": text}
        if reasoning is not None:
            draft_message["reasoning_content"] = ""
        messages.append(draft_message)
    tool_calls_message: dict[str, Any] = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": ENGINE_FLAGS_CALL_ID,
                "type": "function",
                "function": {"name": ENGINE_FLAGS_TOOL, "arguments": "{}"},
            }
        ],
    }
    if reasoning is not None:
        tool_calls_message["reasoning_content"] = reasoning
    messages.append(tool_calls_message)
    messages.append(
        {
            "role": "tool",
            "tool_call_id": ENGINE_FLAGS_CALL_ID,
            "content": injection.content,
        }
    )
    return messages


def _parse_tool_arguments(raw_args: str) -> tuple[dict[str, Any] | None, str | None]:
    """Parse the model's argument JSON into ``(arguments, error)``.

    A malformed payload is not a dead turn: the error becomes the tool result the
    model reads back, so the repair is another iteration (§3.4 folds BTS's parse
    failure the same way).
    """
    try:
        parsed = json.loads(raw_args) if raw_args.strip() else {}
    except json.JSONDecodeError as exc:
        return None, f"malformed tool arguments: {exc}"
    if not isinstance(parsed, dict):
        return None, "tool arguments must be a JSON object"
    return parsed, None


async def _execute_tool(
    tool: LoopTool | None,
    name: str,
    parsed_args: dict[str, Any] | None,
    args_error: str | None,
) -> tuple[dict[str, Any], float]:
    """Run one deterministic tool under the engine-side guard; never raises.

    Every failure becomes a ``{"ok": False, "error": ...}`` result the model reads
    back and can act on.
    """
    started = time.monotonic()

    def _elapsed() -> float:
        return float(time.monotonic() - started)

    if args_error is not None or parsed_args is None:
        return {"ok": False, "error": args_error or "no tool arguments"}, _elapsed()
    if tool is None:
        return {"ok": False, "error": f"no tool named {name!r}"}, _elapsed()

    try:
        if inspect.iscoroutinefunction(tool.fn):
            outcome = await asyncio.wait_for(tool.fn(parsed_args), TOOL_EXECUTION_GUARD_SECONDS)
        else:
            outcome = await asyncio.wait_for(
                asyncio.to_thread(tool.fn, parsed_args), TOOL_EXECUTION_GUARD_SECONDS
            )
    except TimeoutError:
        return {
            "ok": False,
            "error": (
                f"tool {name!r} exceeded its {TOOL_EXECUTION_GUARD_SECONDS:g}s "
                "execution guard"
            ),
        }, _elapsed()
    except Exception as exc:  # noqa: BLE001 — the model reads the failure back
        return {"ok": False, "error": f"tool {name!r} raised {type(exc).__name__}: {exc}"}, _elapsed()
    if not isinstance(outcome, dict):
        return {
            "ok": False,
            "error": f"tool {name!r} returned {type(outcome).__name__}, expected a dict",
        }, _elapsed()
    return outcome, _elapsed()


def _result_content(result: Mapping[str, Any]) -> str:
    """Serialize a tool result into the string a ``role: "tool"`` message carries."""
    try:
        return json.dumps(dict(result), ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return json.dumps({"ok": False, "error": "tool result was not serializable"})


def _clip_text(text: str) -> str:
    """Clip one text field to :data:`LOOP_EVENT_TEXT_MAX_CHARS` (head kept)."""
    if len(text) <= LOOP_EVENT_TEXT_MAX_CHARS:
        return text
    return text[:LOOP_EVENT_TEXT_MAX_CHARS]


def _clip_mapping(value: Any) -> dict[str, Any]:
    """Clip one args/result payload to the same constant, as a dict the gate accepts."""
    try:
        serialized = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {"unserializable": type(value).__name__}
    if len(serialized) <= LOOP_EVENT_TEXT_MAX_CHARS and isinstance(value, dict):
        return value
    return {"clipped": serialized[:LOOP_EVENT_TEXT_MAX_CHARS]}


def _headroom_tokens(
    window_fn: Callable[[], int | None] | None, messages: Sequence[dict[str, Any]]
) -> int | None:
    """Available prompt tokens minus the ESTIMATE of ``messages``, or ``None``.

    ``None`` (no ``window_fn``, no declared Context Size, or a failing lookup)
    means the row carries no ``headroom`` key at all — a call with no window
    declared keeps today's row shape byte-for-byte. Negative is legal: it says
    the prompt has already outgrown the declared window, and by owner ruling the
    loop proceeds anyway (W9.25 §3.5.3).
    """
    if window_fn is None:
        return None
    # Imported lazily: ``fabulist.graph``'s package init imports this module
    # (graph/nodes owns the repair-loop customer), so a module-level import of
    # the graph package from here would be a cycle.
    from fabulist.graph.compaction import estimate_prompt_tokens

    try:
        available = window_fn()
    except Exception:  # noqa: BLE001 - observability never breaks a run
        logger.warning("window_fn failed; headroom not recorded", exc_info=True)
        return None
    if available is None:
        return None
    return int(available) - estimate_prompt_tokens(messages)


def _row(
    surface: str, iteration: int, kind: str, *, headroom: int | None = None, **measured: Any
) -> dict[str, Any]:
    """One ``agent_loop`` payload: the shared keys plus what this kind measured.

    ``headroom`` rides only when the surface declared a window (W9.25 §3.5.3);
    absent means "not measured", never "zero".
    """
    row = {
        "event": AGENT_LOOP_EVENT,
        "surface": surface,
        "iteration": iteration,
        "kind": kind,
        **measured,
    }
    if headroom is not None:
        row["headroom"] = headroom
    return row


def _append_row(log: Any | None, session_id: str, payload: dict[str, Any]) -> None:
    """Append one ``agent_loop`` row; a log hiccup costs the row, not the turn.

    The payload is validated against the gate's variant FIRST so a malformed row
    is a warning here rather than an exception that would abort a live turn
    (``append_event`` gates too — this is the same check, made non-fatal). Best
    effort matches the historian's own trace rule.
    """
    if log is None or not session_id:
        return
    problems = validate_agent_loop_payload(payload)
    if problems:
        logger.warning("agent_loop row rejected: %s", "; ".join(problems))
        return
    try:
        log.append_event(session_id, "system", LOOP_ACTOR, payload)
    except Exception:  # noqa: BLE001 — the trace is best-effort
        logger.warning("could not record agent_loop row for %s", session_id, exc_info=True)
