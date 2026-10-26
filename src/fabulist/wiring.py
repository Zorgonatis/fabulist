"""Orchestrator-owned integration seam (W1 wiring).

Connects the landed Wave 1 parts:
  connections registry -> streaming AsyncOpenAI caller (reasoning + DSL-fenced content)
  -> distilled narrator prompt (W1.2) -> turn graph (W1.1) -> extractor (W1.3)
  -> API app (W1.4).

Nothing here invents policy; it only connects landed components. The DSL fence
segmentation is the W1 v0 heuristic — W2.4 owns the real convention.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, AsyncIterator, Callable

from openai import AsyncOpenAI

from fabulist.api import create_app
from fabulist.connections.client import (
    ModelCallDeadlineExceeded,
    build_request,
    call_deadline_s,
    call_with_retry,
    merge_sampling,
    provider_request_error,
)
from fabulist.connections.registry import Connection, ConnectionConfigError, Registry
from fabulist.context.regex_stage import apply_regex_stage
from fabulist.decision import DecisionCaller
from fabulist.log.degradation import (
    LANE_DEGRADED_EVENT,
    RECALL_DEGRADED_EVENT,
    STAGES,
    append_degradation,
    append_preset_degraded,
    degradation_reason,
)

from fabulist.graph import GraphTurnEngine, build_graph
from fabulist.graph.clock import render_facts
from fabulist.graph.compaction import (
    CompactService,
    covered_turn_seqs,
    prompt_budget_tokens,
    warn_prompt_budget,
)
from fabulist.graph.context_monitor import (
    CompactionClaims,
    ContextJobs,
    PollingTurnSlot,
    make_context_monitor_hook,
)
from fabulist.context import facts as _facts
from fabulist.context.facts import (
    assemble_turn_context,
    facts_hash,
    join_facts_suffix,
    session_persona_block,
    session_stt_block,
    session_world_info_block,
    static_prefix_parts,
    validation_notices,
)
from fabulist.context.roster import extract_cards, roster_line
from fabulist.media.images import (
    CAPTION_EVENT,
    build_image_turn_prefix,
    connection_has_vision,
    find_asset,
    is_image_narrative,
    render_image_history_message,
    sha256_hex,
)
from fabulist.context.world_info import (
    WorldInfoError,
    activate,
    parse_book,
    render_block,
)
from fabulist.memory.recall import RecallResult
from fabulist.graph.nodes import (
    DEFAULT_PLAN_CADENCE,
    DEFAULT_PLANNER_DIRECTIVE,
    make_npc_lanes_node,
    make_planner_node,
)
from fabulist.graph.plans import load_last_plan, render_plan_for_prompt
from fabulist.graph.types import TurnState
from fabulist.log import EventLog
from fabulist.prompts import assemble_messages
from fabulist.prompts.compiler import DEFAULT_BUDGET, DEFAULT_HISTORY_STRIP_DSL
from fabulist.prompts.envelope import (
    ENVELOPE_ROLE,
    assert_envelope_not_in_system_scope,
    assert_envelope_not_in_user_scope,
    insert_engine_envelope,
    render_engine_envelope,
)
from fabulist.prompts.narrator import render_state_block
from fabulist.prompts.compiler import compile as compiler_compile
from fabulist.prompts.compiler import render_system_prompt, strip_dsl
from fabulist.prompts.library_mini import render_library_doc, resolve_library_doc
from fabulist.prompts.palette import render_palette_block
from fabulist.prompts.surfaces import DEFAULT_HISTORY_KEEP_LAST_DSL, preset_surfaces
from fabulist.prompts.wrapper import fold_system_prefix, wrap_preset_messages
from fabulist.state import AsyncExtractor, FallbackExtractor, LLMExtractor, RuleBasedExtractor
from fabulist.state.attribution import (
    MarkupAttributionRunner,
    PaletteEntry,
    palette_for_turn,
)
from fabulist.state.bts import BtsRunner, build_bts_messages
from fabulist.state.historian import make_historian_runner
from fabulist.state.settings import ANTI_INJECTION_DEFAULT, anti_injection_enabled

DSL_FENCE = "```openui"
FENCE_CLOSE = "```"

_FENCE_RE = re.compile(r"```openui\n?(.*?)```", re.DOTALL)

#: How many preceding turns' prose the critic sees (a "short tail", D49; mirrors
#: the narrator builder's ``[-2:]`` recent slice).
CRITIC_PRIOR_TURNS = 2


def _plain_prose(prose: str) -> str:
    """``prose`` as plain lines: one linebreak between them, no annotation.

    The critic copies its ``find`` text from this block VERBATIM, so the block
    must carry nothing the prose does not: a live turn copied a seeded 1-based
    line number into its ``find``, striking prose that matched character for
    character, and the patch was rejected.
    """
    return "\n".join(prose.splitlines())


def _critic_flags_block(flags: list[dict]) -> str:
    """Render the deterministic validator flags as the critic's hint list."""
    if not flags:
        return "(none)"
    lines = []
    for flag in flags:
        check = str(flag.get("check") or "flag")
        severity = str(flag.get("severity") or "info")
        evidence = str(flag.get("evidence") or flag.get("message") or "").strip()
        lines.append(f"- [{check}] {severity}: {evidence}")
    return "\n".join(lines)


def _render_critic_payload(evidence: Mapping[str, Any]) -> str:
    """Compose the critic's single payload block — the D41/D49 evidence bundle.

    Included, by contract: the user message; THIS turn's prose as plain text (the
    literal ``find`` source, one linebreak between lines and no numbering); the
    validator's flags; a short tail of the preceding turns' prose; the session's
    current state line.

    Deliberately EXCLUDED, each for a reason:

    * **DSL segments** — the critic judges prose only (D49: patch scope is prose;
      the DSL channel has its own validation gate). Feeding it the openui DSL
      would invite UI edits and bloat a cheap agent's context with component
      source it cannot change.
    * **Injected facts / recall / roster / library** — those are the narrator's
      INPUTS, assembled pre-turn; the critic reviews what the turn made of the
      scene, not a re-derivation of it. Including them would also hand the critic
      the fiction's ground truth and turn a prose critique into a source check.
    * **Full history** — only a short prior tail is sent: the two defects that
      matter (repetition, lack of movement) are judged against the immediately
      preceding beats, and the whole log would blow the cheap agent's budget for
      no verdict value.
    """
    prior = [text for text in (evidence.get("prior_prose") or []) if text]
    prior_block = "\n\n".join(f"---\n{text}" for text in prior) or "(none)"
    return (
        "### USER MESSAGE\n"
        f"{evidence.get('user_message') or ''}\n\n"
        "### THIS TURN (the turn's prose; copy `find` text verbatim from it)\n"
        f"{_plain_prose(str(evidence.get('prose') or ''))}\n\n"
        "### VALIDATOR FLAGS (hints, not commands)\n"
        f"{_critic_flags_block(list(evidence.get('flags') or []))}\n\n"
        "### PRIOR TURNS (most recent last)\n"
        f"{prior_block}\n\n"
        "### STATE\n"
        f"{evidence.get('state_line') or ''}\n"
    )


class _DslSegmenter:
    """Route a content token stream into prose vs ```openui DSL channels.

    Holds back a partial fence marker at the buffer tail so a fence split
    across provider chunks is never leaked into the prose channel.
    """

    def __init__(self) -> None:
        self.in_dsl = False
        self._buf = ""

    def feed(self, delta: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self._buf += delta
        while self._buf:
            marker = DSL_FENCE if not self.in_dsl else FENCE_CLOSE
            idx = self._buf.find(marker)
            if idx == -1:
                hold = len(marker) - 1
                tail = self._buf[-hold:] if hold else ""
                could_split = bool(tail) and any(
                    marker.startswith(tail[:i]) for i in range(1, len(tail) + 1)
                )
                if could_split:
                    release = self._buf[: len(self._buf) - len(tail)]
                    if release:
                        out.append((self._chan(), release))
                    self._buf = tail
                else:
                    out.append((self._chan(), self._buf))
                    self._buf = ""
                break
            pre = self._buf[:idx]
            if pre:
                out.append((self._chan(), pre))
            self._buf = self._buf[idx + len(marker):]
            # swallow one newline directly after a fence marker
            if self._buf.startswith("\n"):
                self._buf = self._buf[1:]
            self.in_dsl = not self.in_dsl
            if self.in_dsl is False and self._buf:
                # after closing fence, continue loop on remainder as prose
                continue
        return [(c, t) for c, t in out if t]

    def _chan(self) -> str:
        return "dsl" if self.in_dsl else "prose"

    def flush(self) -> list[tuple[str, str]]:
        if not self._buf:
            return []
        out = [(self._chan(), self._buf)]
        self._buf = ""
        return out


async def _open_call(call: Any, *, deadline: float, slot: str, deadline_s: float) -> Any:
    """Await a request's OPEN, bounded by the call's WALL-CLOCK deadline.

    The open is where a provider that never answers headers hangs. It shares the
    ONE clock the stream itself is bounded by (:class:`_DeadlineStream`), so a
    call has a single deadline rather than two different ones.

    It is also the ONE seam where a provider's HTTP failure becomes a typed,
    code-carrying error (issue #26 R6): z.ai returns its business code in the
    error body of the RESPONSE (``{"error": {"code": "1301", …}}``), so it
    arrives here, at the open — a mid-stream abort is documented to carry a
    ``finish_reason`` instead of a code. A failure with no code travels unchanged: a
    network fault must never read as a provider refusal.
    """
    left = deadline - time.monotonic()
    if left > 0:
        try:
            return await asyncio.wait_for(call, timeout=left)
        except (TimeoutError, asyncio.TimeoutError):
            pass
        except Exception as exc:  # noqa: BLE001 - re-raised, typed when it named a code
            typed = provider_request_error(exc)
            if typed is None:
                raise
            raise typed from exc
    # Never leave an un-awaited coroutine behind on the expired path.
    closer = getattr(call, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001 - cleanup never hides the abort
            logger.debug("request cleanup failed", exc_info=True)
    raise ModelCallDeadlineExceeded(slot, deadline_s, delivered=False)


def _async_client(agent: str, registry: Registry) -> AsyncOpenAI:
    conn = registry.connection_for(agent)
    key = os.environ.get(conn.api_key_env, "EMPTY") if conn.api_key_env else "EMPTY"
    return AsyncOpenAI(
        base_url=conn.base_url, api_key=key, timeout=conn.timeout or 300.0
    )


def _slot_label(agent: str) -> str:
    """The canonical SLOT spelling for ``agent`` (``completion.narration``).

    The call-deadline message names the slot the limit belongs to
    (``call_timeout_s for completion.narration``) — the row the owner edits in the
    Connections drawer, not the connection's own name and not the internal agent
    alias. Unknown names pass through unchanged.
    """
    try:
        from fabulist.connections.taxonomy import canonical

        return canonical(agent)
    except Exception:  # noqa: BLE001 - a label read never breaks the call it labels
        return agent


class _DeadlineStream:
    """An async chunk iterator bounded by a WALL-CLOCK deadline (issue #25).

    ``async for chunk in _DeadlineStream(response, deadline=…, slot=…):`` — the
    ONLY reason this exists is that the transport's own bound is per-READ: httpx
    (and so the OpenAI SDK) resets its read timer on every byte, so a provider
    that keeps dripping a delta every few hundred ms is never silent and the call
    never ends (the observed hang: 63 deltas, no terminal frame, no timeout).

    Here the clock runs from the SEND: each ``__anext__`` may take at most the
    time LEFT, and a stream that stays open past the deadline raises
    :class:`~fabulist.connections.client.ModelCallDeadlineExceeded` — live stream
    and all — after closing the response so the socket is not left dripping.

    Per-read protection is unchanged and still useful: a provider that goes
    SILENT is cut by the transport's own read timeout first, and this wrapper
    reports that as the transport error it is (it never rewrites it). A
    consumer's own time between deltas is NOT counted: the clock is checked at
    the call's boundaries only, so a slow node never fails a healthy call.
    """

    def __init__(
        self,
        source: Any,
        *,
        deadline: float,
        slot: str,
        deadline_s: float,
    ) -> None:
        self._source = source
        self._iterator = source.__aiter__()
        self._deadline = deadline
        self.slot = slot
        self.deadline_s = deadline_s
        #: Chunks delivered so far — proof the stream was ALIVE when the clock
        #: fired (the fact that tells this apart from a per-read timeout).
        self.delivered = 0

    def __aiter__(self) -> "_DeadlineStream":
        return self

    async def __anext__(self) -> Any:
        remaining = self._deadline - time.monotonic()
        expired = remaining <= 0
        if not expired:
            try:
                more, chunk = await asyncio.wait_for(self._step(), timeout=remaining)
            except (TimeoutError, asyncio.TimeoutError):  # asyncio's == builtin on 3.11+
                expired = True
            else:
                if not more:
                    raise StopAsyncIteration
                self.delivered += 1
                return chunk
        await self.aclose()
        # Raised OUTSIDE the ``except``: a chained ``TimeoutError`` would let
        # ``agents.loop.is_transport_timeout`` read this as the loop's own
        # recoverable read timeout, and the phase's abort would lose its name.
        raise self.expired()

    async def _step(self) -> tuple[bool, Any]:
        """``(True, chunk)``, or ``(False, None)`` at the end of the stream."""
        try:
            return True, await self._iterator.__anext__()
        except StopAsyncIteration:
            return False, None

    def expired(self) -> ModelCallDeadlineExceeded:
        """The abort this call earns, naming the slot and how it was hanging."""
        return ModelCallDeadlineExceeded(
            self.slot, self.deadline_s, delivered=self.delivered > 0
        )

    async def aclose(self) -> None:
        """Best-effort close of the underlying response/stream (never raises)."""
        closer = getattr(self._source, "aclose", None) or getattr(
            self._source, "close", None
        )
        if not callable(closer):
            return
        try:
            result = closer()
            if result is not None and hasattr(result, "__await__"):
                await result
        except Exception:  # noqa: BLE001 - a cleanup never hides the real abort
            logger.debug("stream close failed", exc_info=True)


class RegistryModelCaller:
    """Streaming ModelCaller over the connections registry (async client).

    Provider reasoning deltas map to ``reasoning`` events; content deltas are
    routed by the DSL segmenter into ``meta.channel`` = prose|dsl. Function-call
    argument fragments map to ``tool_call`` events (W8.4 Tier A) — the
    JSON-string ``arguments`` stream, concatenated by :func:`collect_structured`.

    ``request_profile`` (W8.4): an optional per-call transport profile carrying
    ``response_format`` / ``tools`` / ``tool_choice`` / a thinking toggle. When
    omitted (the narrator/OOC path always omits it) the request is byte-identical
    to a call with no profile.

    ``log`` (I1, D23): when supplied, each call's usage is persisted as a
    ``system``/``usage`` event on the turn's session (read from CURRENT_SESSION,
    set by the engine) so /api/usage reads the log, not process RAM.

    ``connection.streaming is False`` (W9.25): ONE non-streaming request, with the
    event protocol SYNTHESIZED here — reasoning, DSL-segmented content, tool-call
    arguments as a single fragment, the usage row and the ``finish_reason=length``
    error frame all arrive in the same shapes a streamed call produces, so no
    consumer (the agent loop, ``collect_structured``, the narrator) can tell the
    transport apart. ``None``/``True`` keeps today's streaming path byte-identical.

    ``connection.context_window`` (W9.25 §3.5): when the owner declared one, every
    over-budget call appends ONE ``system``/``prompt_budget`` row (and a warning);
    an undeclared window means no math and no row — today's behavior.

    ``emit_finish`` (W9.26 §2.3 step 5): append ONE TERMINAL
    ``{"type": "finish", "finish_reason": <str|null>}`` frame so the D86
    allowlist can see how the generation ended. Scoped to the SUMMARIZER caller
    by construction (``make_agent_caller(..., emit_finish=True)``): the narrator
    turn wire stays byte-stable, and ``"finish"`` is not ``"done"`` — nothing
    that consumes a turn stream treats it as an end-of-turn. The existing
    ``finish_reason == "length"`` error frame is unchanged (and is emitted
    BEFORE the finish frame, so the truncation signal keeps its position).

    ``connection.call_timeout_s`` (issue #25): EVERY call is bounded by a
    WALL-CLOCK deadline — ``call_timeout_s``, or the 900s default when the
    connection declares none — measured from the send. This is the bound
    ``Connection.timeout`` cannot be: that one is PER READ, and a streaming
    response resets it on every delta, so a provider dripping one token every
    few hundred ms is never silent, never times out, and hangs the turn forever.
    On expiry the call raises
    :class:`~fabulist.connections.client.ModelCallDeadlineExceeded` (a
    transport-class failure: a pre-commit phase aborts on the engine's error
    frame, naming the phase and the model), and the response is closed so the
    socket is not left dripping.
    """

    def __init__(
        self,
        agent: str,
        registry: Registry,
        log: Any | None = None,
        *,
        emit_finish: bool = False,
    ) -> None:
        self.agent = agent
        self.registry = registry
        self.log = log
        self.emit_finish = emit_finish

    async def __call__(
        self,
        messages: list[dict],
        stream: bool = True,
        request_profile: dict | None = None,
    ) -> AsyncIterator[dict]:
        from fabulist.agents.debug import CURRENT_SESSION
        from fabulist.usage.track import record_usage

        started = time.monotonic()
        connection = self.registry.connection_for(self.agent)
        client = _async_client(self.agent, self.registry)
        # Issue #25: the WALL-CLOCK bound for THIS call. ``deadline`` is the
        # monotonic instant it fires at; every await below is capped by the time
        # left, so the backstop holds however many deltas the provider drips.
        deadline_s = call_deadline_s(connection)
        deadline = started + deadline_s
        slot = _slot_label(self.agent)
        req = build_request(
            self.agent, messages, registry=self.registry, request_profile=request_profile
        )
        # W9.25 §3.5: the prompt-budget warn rides the SEND, so the row names what
        # actually went over — at most one row per call, no math without a window.
        warn_prompt_budget(
            self.log,
            session_id=CURRENT_SESSION.get(""),
            agent=self.agent,
            context_window=connection.context_window,
            response_length=req.get("max_tokens"),
            messages=messages,
        )
        seg = _DslSegmenter()
        usage = None
        finish_reason: str | None = None
        if connection.streaming is False:
            # Streaming OFF (W9.25): one request, then the SAME frame protocol.
            req["stream"] = False
            # Issue #82: the provider-class retry lives at THIS seam, so every
            # caller of this one transport rides it — loop or one-shot. The
            # deadline is fixed at the call's start, so all attempts share one
            # wall-clock budget (a retry never doubles a 900s bound).
            response = await call_with_retry(
                lambda: _open_call(
                    client.chat.completions.create(**req),
                    deadline=deadline,
                    slot=slot,
                    deadline_s=deadline_s,
                ),
                stage=f"model call ({slot})",
            )
            usage = getattr(response, "usage", None)
            choice = response.choices[0] if getattr(response, "choices", None) else None
            if choice is not None:
                finish_reason = getattr(choice, "finish_reason", None)
                message = getattr(choice, "message", None)
                reasoning = getattr(message, "reasoning_content", None)
                # PRESENCE, not truthiness: a thinking model that reasoned
                # nothing this turn still returns the field with an empty
                # string, and the tool loop must know it was there (echoing
                # nothing back is a 400 on DeepSeek's thinking mode). A
                # provider that never sets the field yields no event at all.
                if isinstance(reasoning, str):
                    yield {"type": "reasoning", "delta": reasoning}
                content = getattr(message, "content", None)
                if content:
                    for channel, text in seg.feed(content):
                        yield {
                            "type": "content",
                            "delta": text,
                            "meta": {"channel": channel},
                        }
                # W8.4 Tier A parity: the whole argument JSON is ONE fragment.
                # The enumeration is the call's grouping index, exactly what the
                # streaming path stamps — several parallel calls in one message
                # stay distinguishable to the consumer (see the streaming note).
                for index, call in enumerate(getattr(message, "tool_calls", None) or ()):
                    fn = getattr(call, "function", None)
                    name = getattr(fn, "name", None) if fn is not None else None
                    args = getattr(fn, "arguments", None) if fn is not None else None
                    if args:
                        meta: dict[str, object] = {"index": index}
                        if name:
                            meta["name"] = str(name)
                        yield {"type": "tool_call", "delta": args, "meta": meta}
        else:
            req["stream"] = True
            # llama.cpp/OpenAI-compatible: the final usage chunk arrives with an
            # EMPTY choices list — it was being skipped, so tokens read 0.
            req["stream_options"] = {"include_usage": True}
            #: The function name each streamed tool-call index announced. A provider
            #: names the call on its FIRST fragment only (often with empty arguments),
            #: so the name is remembered per index and stamped on every argument
            #: fragment: a caller running several tools attributes the call by name
            #: instead of guessing from position (W9.24 §3.4's read-tools loop; a
            #: single-tool customer reads the same name it used to infer).
            tool_names: dict[object, str] = {}
            # Issue #82: same provider-class retry as the non-streaming branch —
            # it covers the OPEN only; a stream that fails mid-flight is already
            # half-consumed and cannot be replayed.
            response = await call_with_retry(
                lambda: _open_call(
                    client.chat.completions.create(**req),
                    deadline=deadline,
                    slot=slot,
                    deadline_s=deadline_s,
                ),
                stage=f"model call ({slot})",
            )
            # The stream itself is bounded from here on: see ``_DeadlineStream``.
            async for chunk in _DeadlineStream(
                response, deadline=deadline, slot=slot, deadline_s=deadline_s
            ):
                u = getattr(chunk, "usage", None)
                if u and int(getattr(u, "prompt_tokens", 0) or 0):
                    usage = u
                if not getattr(chunk, "choices", None):
                    continue
                choice = chunk.choices[0]
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
                delta = choice.delta
                reasoning = getattr(delta, "reasoning_content", None)
                # Same presence rule as the non-streaming branch above: the
                # opening frame of a no-reasoning turn carries
                # ``"reasoning_content": ""`` and later frames omit the key (a
                # trailing ``null`` is not a value), so a string — empty or not
                # — is the provider saying "this turn's reasoning is this".
                if isinstance(reasoning, str):
                    yield {"type": "reasoning", "delta": reasoning}
                content = getattr(delta, "content", None)
                if content:
                    for channel, text in seg.feed(content):
                        yield {"type": "content", "delta": text, "meta": {"channel": channel}}
                # W8.4 Tier A: a forced function call returns its JSON payload on
                # ``tool_calls`` (content is empty). Stream the argument fragments as
                # their own event; the reasoning/content/error protocol is unchanged.
                tool_calls = getattr(delta, "tool_calls", None)
                if tool_calls:
                    for call in tool_calls:
                        fn = getattr(call, "function", None)
                        index = getattr(call, "index", 0)
                        name = getattr(fn, "name", None) if fn is not None else None
                        if name:
                            tool_names[index] = str(name)
                        args = getattr(fn, "arguments", None) if fn is not None else None
                        if args:
                            #: ``index`` is the provider's grouping key: parallel
                            #: calls stream INTERLEAVED, so a consumer that only
                            #: concatenates ``delta`` glues several calls into one
                            #: unparseable argument string (the live gate). The
                            #: name is remembered per index above.
                            meta: dict[str, object] = {"index": index}
                            seen = tool_names.get(index)
                            if seen:
                                meta["name"] = seen
                            yield {"type": "tool_call", "delta": args, "meta": meta}
        for channel, text in seg.flush():
            yield {"type": "content", "delta": text, "meta": {"channel": channel}}
        record_usage(
            self.log,
            session_id=CURRENT_SESSION.get(""),
            agent=self.agent,
            model=self.registry.connection_for(self.agent).model,
            prompt_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            completion_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
            latency_s=time.monotonic() - started,
        )
        if finish_reason == "length":
            # Never let a truncated turn pass silently: reasoning models can
            # burn the entire budget deliberating (incl. full scene drafts in
            # reasoning_content) before any content token is emitted.
            yield {
                "type": "error",
                "delta": "generation truncated: max_tokens reached before natural stop",
                "meta": {"finish_reason": "length"},
            }
        if self.emit_finish:
            # W9.26 §2.3 step 5 (D86): the TERMINAL side-band frame the
            # summarizer's finish-reason allowlist reads. Emitted last so it is
            # the stream's closing frame, and only by a caller built with
            # ``emit_finish=True`` — never on the narrator/OOC wire.
            yield {"type": "finish", "finish_reason": finish_reason}


async def collect_structured(
    caller: Any,
    messages: list[dict],
    *,
    request_profile: dict | None = None,
) -> str:
    """Drain a caller into structured text, preferring tool-call arguments (W8.4).

    Tier A (function-calling) streams the JSON arguments as ``tool_call``
    deltas — when any arrive they win, and the concatenation is the payload a
    structured parser consumes. A model that answered in ``content`` instead
    (e.g. an ``auto`` tool_choice that degraded to prose, or a lower tier)
    falls back to the concatenated content, exactly what :func:`collect_text`
    would return. ``reasoning``/``error`` deltas are ignored.

    ``request_profile`` rides through to the caller so the tool definition and
    ``tool_choice`` reach ``build_request``; the caller must accept the keyword
    (``RegistryModelCaller`` / ``PresetAgentCaller`` / ``DebugCapture`` do).
    """
    tool_parts: list[str] = []
    content_parts: list[str] = []
    profile_kw = {} if request_profile is None else {"request_profile": request_profile}
    async for event in caller(messages, True, **profile_kw):
        kind = event.get("type")
        if kind == "tool_call":
            tool_parts.append(event.get("delta", ""))
        elif kind == "content":
            content_parts.append(event.get("delta", ""))
    return "".join(tool_parts) if tool_parts else "".join(content_parts)


def make_agent_caller(
    agent: str,
    registry: Registry,
    log: EventLog,
    *,
    agent_debug_default: bool = False,
    emit_finish: bool = False,
) -> Any:
    """Build the one shared ``RegistryModelCaller`` for ``agent`` (W5-S2 chokepoint).

    Every capturable agent surface — the narrator/OOC preset caller, the
    summarizer, the captioner and the per-definition assistant callers — is
    constructed through here, so :class:`~fabulist.agents.debug.DebugCapture`
    applies uniformly instead of only to the narrator.

    The capture is gated PER CALL by the session's runtime toggle
    (:func:`fabulist.api.chat_config_api.resolve_agent_debug`, folded from the
    session's ``chat_agent_debug`` events); ``agent_debug_default`` only supplies
    the default for a session that never wrote one, so today's ``--agent-debug``
    boot flag keeps defaulting sessions ON. When the toggle is off the wrapper
    forwards events without accumulating anything — no capture rows, no
    truncation work.

    ``emit_finish`` (W9.26 §2.3 step 5) appends the terminal ``finish`` frame; it
    is set for the SUMMARIZER caller ONLY, so the narrator turn wire carries no
    new frame name at all.

    The extractor (a sync client) and the embedder (direct httpx) do not ride a
    ``RegistryModelCaller`` and are deliberately NOT captured here.
    """
    from fabulist.agents.debug import (
        CURRENT_SESSION,
        CURRENT_INTERACTION,
        DebugCapture,
        append_call_event,
    )
    from fabulist.api.chat_config_api import resolve_agent_debug

    caller = RegistryModelCaller(agent, registry, log=log, emit_finish=emit_finish)

    def _enabled() -> bool:
        return resolve_agent_debug(log, CURRENT_SESSION.get(""), agent_debug_default)

    def _sink(wire: dict) -> None:
        sid = CURRENT_SESSION.get("")
        if sid:
            # OOC rides the same wrapped caller as the narrator (one engine
            # ``self._model``): label the capture with the CALL PATH's
            # interaction so the inspector filter shows the truth.
            wire = {**wire, "agent": CURRENT_INTERACTION.get("") or wire.get("agent")}
            append_call_event(log, sid, wire)

    return DebugCapture(caller, _sink, enabled=_enabled)


def _notice_lane_degradation(log: EventLog, session_id: str, record: dict) -> None:
    """Emit ``lane_degraded`` when a FAILED decision call was the lane dispatch.

    Issue #34: the lane strategy is ``select_dispatch_members``' ``decision``
    ask, and it FAILS OPEN (:mod:`fabulist.state.presence`: a dead or ill-shaped
    answer returns the full ladder set). The turn therefore still produces prose
    and lanes — the issue's exact symptom: "lanes strategy ... silently
    degraded". :class:`fabulist.decision.DecisionCaller` swallows the transport
    error and reports it on the observer record (``ok=False`` + ``error``), so
    this observer is the one place every decision call crosses; a notice is
    emitted ONLY for the dispatch question, so the gates, the planner and the
    proposal lanes keep their own failure vocabulary.

    ``log`` is the observer's own handle; ``session_id`` comes from the engine's
    ``CURRENT_SESSION`` contextvar (already resolved by the caller).
    """
    if record.get("ok"):
        return
    try:
        from fabulist.decision_queries import (
            DISPATCH_QUESTION_ID,
            DISPATCH_SECTION,
            questions_for_section,
        )

        dispatch_ids = {DISPATCH_QUESTION_ID}
        dispatch_ids |= {
            str(row.get("id"))
            for row in questions_for_section(log, DISPATCH_SECTION)
            if row.get("id")
        }
    except Exception:  # noqa: BLE001 - a missing bank cannot break the observer
        return
    questions = {str(question) for question in record.get("questions") or []}
    if not questions & dispatch_ids:
        return
    error = record.get("error") or "no usable answer"
    append_degradation(
        log,
        session_id,
        LANE_DEGRADED_EVENT,
        stage=STAGES[LANE_DEGRADED_EVENT],
        reason=degradation_reason(
            error,
            "the lane set fell back to the full cast — check the decision "
            "connection",
        ),
    )


def _decision_observer(log: EventLog) -> Callable[[dict], None]:
    """Observer that makes each d1-3b decision call visible in the event log.

    ``fabulist.decision`` is stdlib+httpx pure and stays so; this closure is
    the wiring-side seam that connects it to the log. For every ``ask`` attempt
    it resolves the current session from the engine-set ``CURRENT_SESSION``
    contextvar (silently skipping when unset) and appends ONE ``system``/``usage``
    event so the existing inspector aggregation (``/api/usage``, per-turn calls
    panel) shows the decision model with zero frontend change.

    The interrogatable detail (``ok``/``questions``/``answers``/``error``) rides
    the SAME payload: the schema gate permits extras (D4), so one row is both
    measurable and queryable. A failed call still logs (``ok=false`` + error),
    so a silent timeout becomes visible — the point of this seam.
    """

    def observe(record: dict) -> None:
        try:
            from fabulist.agents.debug import CURRENT_SESSION
            from fabulist.usage.track import usage_payload

            session_id = CURRENT_SESSION.get("")
            if not session_id:
                return
            payload = usage_payload(
                agent="decision",
                model=str(record.get("model") or "decision"),
                session_id=session_id,
                prompt_tokens=int(record.get("input_tokens") or 0),
                completion_tokens=int(record.get("output_tokens") or 0),
                latency_s=float(record.get("latency_s") or 0.0),
            )
            # Interrogatable detail on the SAME row; extras pass the gate (D4).
            payload.update(
                {
                    "ok": bool(record.get("ok")),
                    "questions": list(record.get("questions") or []),
                    "answers": record.get("answers"),
                    "error": record.get("error"),
                }
            )
            log.append_event(session_id, "system", "decision", payload)
            _notice_lane_degradation(log, session_id, record)
        except Exception:  # noqa: BLE001 - visibility must never break a turn
            logger.warning("decision usage event append failed", exc_info=True)

    return observe


def history_from_log(
    log: EventLog,
    session_id: str,
    limit: int = 20,
    strip: bool = True,
    surfaces: dict | None = None,
    drop_current_user: bool = False,
    vision: bool = False,
) -> list[dict]:
    """Fork-aware interleaved user/narrative history, DSL-stripped.

    ``get_events`` is session-local (a branch would see none of its inherited
    turns); the projection resolves the base chain, so branches inherit. The
    preset regex stage (W2.3) is applied depth-gated — ST semantics: the newest
    ``min_depth`` messages are exempt, prompt-only context.

    D27: ``limit`` (``history.depth``), ``strip`` (``history.strip_dsl``) and the
    ``surfaces``-carried regex list are the preset-overridable projection knobs.
    ``dsl_keep_last`` (default 3) unstrips the newest narratives — the model
    sees its own recent panels in place (D34; no reinjection seam).

    ``drop_current_user`` (OFF by default) drops the turn's OWN user row from
    the projection — the newest user row, POSITIONALLY, never by matching the
    text (a player who sends the same line twice in a row is a normal case).
    The live narrate/OOC paths append the turn's ``user`` event BEFORE the
    builder runs (``api/app.py:_drive_turn``), so the newest user row IS the
    turn being generated; the flow's own ``user``/``facts`` modules then append
    that same turn as the final (state-wrapped) user message. The history copy
    is pure duplication — the player's line rendered twice invites the model to
    ECHO it — so the narrate/OOC builders opt in. A ``regenerate=True`` fork
    replay never re-appends the user row, but the inherited row is still the
    turn being generated, so the same positional rule holds. Callers with their
    own payload shapes (extract / summarize / caption / bts / attribution /
    assistant / preview) leave this off: their history is not the same
    projection-plus-live-ask shape.

    ``vision`` (issue #79) is THIS consumer's live connection capability. ``True``
    renders an image row as the native multipart ASSISTANT message; ``False``
    (the default, and every unset connection) renders the uniform
    ``[inline image caption: <text>]`` marker from the row's caption/prompt/text.
    The decision is per call, never baked into the row: the same log renders
    differently for a vision narrator and a text-only lane, and a rebind takes
    effect on the next build. The returned image message carries transient
    render metadata (:data:`fabulist.media.images.MEDIA_META_KEY`) so a second
    consumer (an NPC lane) can re-render the SAME shared history for ITS
    connection; ``build_request`` strips the key at the wire.

    D-F: a flagged turn's ``validation_notice`` rides history IMMEDIATELY AFTER the
    narrative it refers to (the builder's demand: the natural position, never the
    prefix and never ``<recalled>``). The notice is attached AFTER the depth cut,
    so trimming can never leave an orphan notice behind without its narrative.
    Issue #68: the notice is an ``assistant`` row, not a ``system`` one — a stock
    Jinja chat template (the Qwen family) refuses any system message after index
    0, and folding a PER-TURN notice into the stable prefix would break the cache
    contract (ADR-005). Same position, same tag, legal role.

    W9.26 §2.4 (the era model): a turn already folded into a ``summary`` is DROPPED
    — its text reaches the model through the ``<recalled>`` recap instead, so the
    post-fork history is the post-fork turns only (plus whatever the summary
    covers, once, as prose). The covered set is
    :func:`fabulist.graph.compaction.covered_turn_seqs`, which extends each
    summary's ``covers_seqs`` span to whole TURN PAIRS so a covered narrative
    never leaves its player line behind as an orphan. Order matters and is
    pinned: ``current_user_seq`` and the ``dsl_keep_last`` window are computed
    from the UNFILTERED projection (they describe the whole visible history),
    and only then do covered rows leave. The chain scan is shared with the D-F
    notice pass below — one read, not two.
    """
    projection = log.get_projection(session_id)
    items = sorted(
        [*projection.get("user_messages", []), *projection.get("narratives", [])],
        key=lambda item: item["seq"],
    )
    # Position of the turn's own user row: the newest user row in the
    # projection. Dropped before the depth limit so the current turn never
    # consumes a ``history.depth`` slot (it was never a prior turn).
    current_user_seq: int | None = None
    if drop_current_user:
        user_seqs = [item["seq"] for item in items if item["type"] == "user"]
        current_user_seq = user_seqs[-1] if user_seqs else None
    # D34 keep-last window: the newest ``dsl_keep_last`` NARRATIVE turns keep
    # their DSL; older narratives strip exactly as before. User rows never
    # count toward the window (mirrors compiler.stripped_history semantics).
    knobs = (surfaces or {}).get("history") or {}
    try:
        keep_last = max(0, int(knobs.get("dsl_keep_last", DEFAULT_HISTORY_KEEP_LAST_DSL)))
    except (TypeError, ValueError):
        keep_last = DEFAULT_HISTORY_KEEP_LAST_DSL
    narrative_seqs = [item["seq"] for item in items if item["type"] != "user"]
    keep_from_seq = (
        narrative_seqs[max(0, len(narrative_seqs) - keep_last)]
        if strip and keep_last > 0 and narrative_seqs
        else None
    )
    # W9.26 §2.4: ONE chain read pays for both the covered-range exclusion and
    # the D-F notice pass (``validation_notices`` takes the events it would
    # otherwise read itself). Exclusion runs AFTER the positions above: a
    # covered row must not steal a ``history.depth`` slot from a live turn, but
    # it must also not move the turn's own user row or the keep-last window
    # (both are positional facts about the UNFILTERED projection).
    chain_events = log.get_chain_events(session_id)
    covered = covered_turn_seqs(chain_events)
    if covered:
        items = [item for item in items if item["seq"] not in covered]
    # Issue #79: arrival captions fold from the SAME chain read. Later rows win
    # (append-only log); no captioner call ever runs here — an uncaptioned
    # legacy asset falls back to its row text or the terse placeholder.
    captions: dict[str, str] = {}
    for event in chain_events:
        if event.type != "system" or event.payload.get("event") != CAPTION_EVENT:
            continue
        ref = event.payload.get("asset")
        if isinstance(ref, str):
            captions[ref] = str(event.payload.get("text") or "")
    history: list[dict] = []
    # The source row's seq for each history message, index-aligned with
    # ``history``: a validation notice is placed after the narrative message its
    # row names, so the interleave needs the mapping, not just the text.
    message_seqs: list[int] = []
    for item in items:
        payload = item["payload"]
        # Image rows render as an ASSISTANT message whose content is a multipart
        # list: the effective prompt as text plus the inlined image. Both the
        # canon generated-image narrative and the legacy ``user`` row that
        # carried a generated image (kind=="image") take this path. The
        # special-case runs FIRST: such rows carry no prose (the prose branch
        # below would drop them), and a legacy image ``user`` row must NOT also
        # fall through to the plain user branch.
        image_asset_id = payload.get("image_asset_id")
        if is_image_narrative(item) or (
            item["type"] == "user"
            and payload.get("kind") == "image"
            and bool(image_asset_id)
        ):
            asset = find_asset(log, session_id, image_asset_id)
            if asset is not None:
                asset_payload = asset.get("payload") or {}
                mime = asset_payload.get("mime") or "application/octet-stream"
                b64 = asset_payload.get("bytes_b64") or ""
                url = f"data:{mime};base64,{b64}"
                prompt = payload.get("prompt") or payload.get("text", "")
                kind = payload.get("kind") or asset_payload.get("kind") or "image"
                # The arrival text ALREADY exists (upload caption or imagegen
                # prompt); a legacy asset without one falls back to whatever
                # text it carries, then the terse placeholder. Never a captioner
                # call inside a turn build.
                fallback = (
                    captions.get(str(image_asset_id))
                    or prompt
                    or asset_payload.get("note")
                    or ""
                )
                history.append(
                    render_image_history_message(
                        {
                            "asset_id": str(image_asset_id),
                            "url": url,
                            "kind": kind,
                            "fallback": fallback,
                            "visible": prompt or fallback,
                        },
                        vision,
                    )
                )
                message_seqs.append(item["seq"])
            continue
        if item["type"] == "user":
            if item["seq"] == current_user_seq:
                continue
            # Issue #63: the model reads the SAME text the transcript shows.
            # The projection resolves the known macros per row
            # (``text_rendered``); the stored event keeps the raw bytes
            # (ADR-001), so nothing is rewritten and ``{{last_user_turn}}``
            # still matches what was typed.
            rendered = payload.get("text_rendered")
            history.append(
                {
                    "role": "user",
                    "content": (
                        rendered
                        if isinstance(rendered, str)
                        else payload.get("text", "")
                    ),
                }
            )
            message_seqs.append(item["seq"])
        else:
            do_strip = strip and (keep_from_seq is None or item["seq"] < keep_from_seq)
            prose = (
                strip_dsl(payload.get("prose", ""))
                if do_strip
                else payload.get("prose", "")
            )
            if prose:
                message = {"role": "assistant", "content": prose}
                # Issue #42: a turn that PRODUCED ``reasoning_content`` must bring
                # the field back on the next request — a request carrying
                # ``tools`` 400s when an assistant message omits it (measured live
                # on DeepSeek-V4.1-Flash). Issue #29's owner ruling puts the trim
                # at the ROUND boundary: a PRIOR execution's reasoning is stored
                # for diagnosis, never re-sent as text. So the field is emitted
                # PRESENT but EMPTY (``""``, the vendor's own "nothing reasoned"
                # value): the presence rule is satisfied without re-billing every
                # past block. Presence, not truthiness — turns with no stored
                # reasoning (older sessions, non-thinking models) emit no field at
                # all and stay byte-identical.
                reasoning = payload.get("reasoning")
                if isinstance(reasoning, str):
                    message["reasoning_content"] = ""
                history.append(message)
                message_seqs.append(item["seq"])
    history = history[-limit:]
    message_seqs = message_seqs[-limit:]
    # Regex stage, ST depth convention: depth = distance from the NEWEST
    # message (0 = newest, exempt while < min_depth). Older context gets
    # the preset's strip patterns applied.
    n = len(history)
    treated = []
    for i, msg in enumerate(history):
        depth = n - 1 - i
        if isinstance(msg["content"], list):
            # Multipart content (an inlined image/video message): the regex
            # stage operates on plain strings only — leave the parts untouched.
            treated.append(msg)
            continue
        text = apply_regex_stage(
            [msg["content"]], depth_index=depth, prompt_only=True, surfaces=surfaces
        )[0]
        treated.append({**msg, "content": text})
    # D-F: place each validation notice directly after the narrative message it
    # refers to. Done after the depth cut and the regex stage (which only rewrites
    # the narrative/user strings) so an orphan notice can never survive a trim.
    # The chain was read once above (W9.26 §2.4) — this pass reuses those events.
    #
    # Issue #68: the row is an ASSISTANT message (``prompts.envelope.
    # ENVELOPE_ROLE``), never a second ``system`` one — the template rule (the
    # Qwen family raises "System message must be at the beginning") makes any
    # non-leading system row a hard 500. The POSITION is unchanged (D-F: directly
    # after the narrative it flags, which is also cache-stable — the notice never
    # edits bytes above it); only the role moves, exactly the carrier the
    # narrator's own engine envelope already rides.
    notices = validation_notices(log, session_id, events=chain_events)
    if notices:
        interleaved: list[dict] = []
        for message, seq in zip(treated, message_seqs):
            interleaved.append(message)
            notice = notices.get(seq)
            if notice:
                interleaved.append({"role": ENVELOPE_ROLE, "content": notice})
        treated = interleaved
    return treated


def session_persona_block(
    log: EventLog, session_id: str, variables: dict | None = None
) -> str | None:
    """Delegate to :func:`fabulist.context.facts.session_persona_block` (single source)."""
    return _facts.session_persona_block(log, session_id, variables=variables)



def session_stt_block(log: EventLog, session_id: str) -> str | None:
    """Delegate to :func:`fabulist.context.facts.session_stt_block` (single source)."""
    return _facts.session_stt_block(log, session_id)



def session_world_info_block(
    log: EventLog,
    session_id: str,
    scan_text: str,
    variables: dict | None = None,
) -> str | None:
    """Delegate to :func:`fabulist.context.facts.session_world_info_block` (single source)."""
    return _facts.session_world_info_block(log, session_id, scan_text, variables=variables)



def _content_chars(message: dict) -> int:
    """Character count of one message's text (str or multipart text parts)."""
    content = message.get("content")
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(
            len(part["text"])
            for part in content
            if isinstance(part, dict) and isinstance(part.get("text"), str)
        )
    return 0


def _response_length(connection: Any) -> int:
    """The connection's Response Length in tokens: ``sampling.max_tokens``.

    Merged over :data:`fabulist.connections.client.DEFAULT_SAMPLING` so an
    un-authored sampler still yields the real number the request will carry
    (W9.25 §3.5: Response Length IS the generation cap, the human label for it).
    """
    sampling = merge_sampling(getattr(connection, "sampling", None) or {}, {})
    return int(sampling.get("max_tokens") or 0)


def _format_session_for_row() -> str:
    """The CURRENT turn's session id for a degradation row, or ``""``.

    Binding/profile seams are invoked through the model-caller interface, which
    carries no session id; the pump installs ``CURRENT_SESSION`` for the whole
    turn (the same seam the usage rows and the BTS pass read it from), so this
    is where those sites recover it. Anchor sessions are refused by
    :func:`append_preset_degraded` itself.
    """
    try:
        from fabulist.agents.debug import CURRENT_SESSION

        return CURRENT_SESSION.get("") or ""
    except Exception:  # noqa: BLE001 - a row is best-effort, never fatal
        return ""


def _preset_degraded(
    log: EventLog,
    session_id: str,
    interaction: str,
    exc: BaseException,
    advice: str = "",
) -> None:
    """Append ONE ``preset_degraded`` row for a swallowed preset/context read.

    ``interaction`` is both the stage the Inspector shows and the label the
    caller's warning names; the caller has already warned (issue #70 pairs one
    warning per handler with one row per block). A ``""`` session id (an
    off-turn build) is a no-op — the warning is the whole record there.
    """
    append_preset_degraded(
        log, session_id, stage=interaction, exc=exc, advice=advice
    )


def _interaction_slot(
    registry: Registry, log: EventLog, interaction: str, fallback: str
) -> str:
    """The slot name serving ``interaction`` right now (``fallback`` on failure).

    W9.25 §3.4's ONE resolution rule, shared by every W9.26 seam that needs a
    slot: the Summary/ledger/budget consumers below all come through here, so a
    preset re-bind or a hot-reloaded connection applies on the next call and no
    seam can re-derive the hierarchy on its own.

    Issue #70: a failed preset read here silently answers with the WRONG MODEL,
    so it warns and (when the turn's session is known) leaves a
    ``preset_degraded`` row.
    """
    from fabulist.connections.taxonomy import resolve_interaction_agent
    from fabulist.prompts.agent_sets import bindings_for

    try:
        return resolve_interaction_agent(
            bindings_for(log), registry.agents, interaction, fallback
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable preset store = the default
        logger.warning(
            "%s binding resolution fell back to %r", interaction, fallback,
            exc_info=True,
        )
        _preset_degraded(
            log,
            _format_session_for_row(),
            f"{interaction}_binding",
            exc,
            "the interaction fell back to its default agent — check the preset "
            "bindings",
        )
        return fallback


def _interaction_connection(
    registry: Registry, log: EventLog, interaction: str, fallback: str
) -> Any | None:
    """The connection an INTERACTION resolves to right now, or ``None``.

    Resolved live through the binding layer, exactly like the loop's capability
    and timeout seams: a re-bound preset agent or a hot-reloaded connection is
    honoured per call, and an unresolvable slot degrades to ``None`` (no window
    math) instead of breaking the turn.
    """
    try:
        return registry.connection_for(
            _interaction_slot(registry, log, interaction, fallback)
        )
    except Exception as exc:  # noqa: BLE001 - an unknown connection means no window math
        # Issue #70: a swallowed settings read is the same class — never silent.
        logger.warning(
            "%s connection read failed; no window math this read", interaction,
            exc_info=True,
        )
        _preset_degraded(
            log,
            _format_session_for_row(),
            f"{interaction}_connection",
            exc,
            "the interaction resolved no connection — check the connections "
            "document and the preset bindings",
        )
        return None


def _narrate_connection(registry: Registry, log: EventLog) -> Any | None:
    """The connection the NARRATE slot resolves to right now, or ``None``."""
    return _interaction_connection(registry, log, "narrate", "narrator")


#: Window floor for the summarizer when NO connection declares a Context Size
#: (W9.26 §2.3 step 2). A flat floor would go negative against the live
#: summarizer binding (``max_tokens=16384``), so the fallback is
#: ``max(FLOOR, response_length + HEADROOM)`` — the fallback must SCALE.
SUMMARIZER_WINDOW_FLOOR = 8192
SUMMARIZER_WINDOW_HEADROOM = 4096


def summarizer_budget_tokens(registry: Registry, log: EventLog) -> int:
    """The summarizer's usable prompt budget, by the §2.3 step-2 fallback chain.

    ``window = summarizer conn's declared Context Size ▸ the narrate conn's ▸
    max(8192, response_length + 4096)``, then
    ``budget = window − response_length − RESERVE_TOKENS``
    (:func:`fabulist.graph.compaction.prompt_budget_tokens`). Chunk math always
    has a number; a DECLARATION improves it. A non-positive result is a real
    answer, not a bug: ``CompactService`` reports it as a ``stage:"config"``
    failure pointing at ⚑C (the owner's undeclared Context Size), because
    compaction cannot free a window nobody declared.
    """
    connection = _interaction_connection(registry, log, "summarize", "summarizer")
    response = _response_length(connection) if connection is not None else 0
    window = getattr(connection, "context_window", None) if connection is not None else None
    if window is None:
        narrate = _narrate_connection(registry, log)
        window = getattr(narrate, "context_window", None) if narrate is not None else None
    if window is None:
        window = max(SUMMARIZER_WINDOW_FLOOR, response + SUMMARIZER_WINDOW_HEADROOM)
    return int(prompt_budget_tokens(window, response) or 0)


def _summarizer_model(registry: Registry, log: EventLog) -> str:
    """The model label the summary's ``stats.model`` records (the serving conn).

    Resolved live like every other slot read; an unresolvable slot yields ``""``
    (``CompactService`` then records an empty model rather than inventing one),
    and the call itself fails loudly at the caller with the real config error.
    """
    connection = _interaction_connection(registry, log, "summarize", "summarizer")
    return str(getattr(connection, "model", "") or "") if connection is not None else ""


def narrate_window_budget(
    registry: Registry, log: EventLog
) -> Callable[[], int | None]:
    """Zero-arg available prompt-token budget for the NARRATE slot (W9.25 §3.5.1).

    The value is ``context_window − response_length − RESERVE_TOKENS``
    (:func:`fabulist.graph.compaction.prompt_budget_tokens`), or ``None`` when the
    owner declared no Context Size — which keeps the assembler's fixed budget
    behavior byte-identical to the pre-W9.25 path.
    """

    def resolve() -> int | None:
        connection = _narrate_connection(registry, log)
        if connection is None:
            return None
        return prompt_budget_tokens(
            connection.context_window, _response_length(connection)
        )

    return resolve


def narrate_vision(
    registry: Registry, log: EventLog
) -> Callable[[], bool]:
    """Zero-arg live reader of the NARRATE connection's vision toggle (#79)."""

    def resolve() -> bool:
        return connection_has_vision(_narrate_connection(registry, log))

    return resolve


def interaction_vision(
    registry: Registry, log: EventLog, interaction: str, fallback: str
) -> Callable[[], bool]:
    """Whether an INTERACTION's live connection accepts image parts (#79).

    Per-connection and fully dynamic: the narrator, each NPC lane, OOC and the
    previews each resolve THEIR OWN connection's ``vision`` toggle at build
    time, so a rebind changes rendering on the next turn with no migration.
    An unresolvable slot degrades to text-only (the safe default).
    """

    def resolve() -> bool:
        return connection_has_vision(
            _interaction_connection(registry, log, interaction, fallback)
        )

    return resolve


def bts_window_budget(registry: Registry, log: EventLog) -> Callable[[], int | None]:
    """Zero-arg available prompt-token budget for the BTS slot (W9.25 §3.5.3).

    The same formula as :func:`narrate_window_budget` — ``context_window −
    response_length − RESERVE_TOKENS`` — over the ``bts`` interaction's OWN
    connection, resolved live through the taxonomy (fallback ``extractor``,
    the same fallback every other BTS seam uses) so a re-bound preset agent or
    a hot-reloaded connection is honoured per call. ``None`` (no declared
    Context Size, or an unresolvable slot) leaves the BTS loop's rows exactly as
    they were: no ``headroom`` key, and the loop proceeds regardless.
    """

    def resolve() -> int | None:
        from fabulist.connections.taxonomy import resolve_interaction_agent
        from fabulist.prompts.agent_sets import bindings_for

        try:
            agent = resolve_interaction_agent(
                bindings_for(log), registry.agents, "bts", "extractor"
            )
            connection = registry.connection_for(agent)
        except Exception:  # noqa: BLE001 - an unknown connection means no window math
            return None
        return prompt_budget_tokens(
            connection.context_window, _response_length(connection)
        )

    return resolve


def _authored_directive_budget(directive_config: Any) -> int:
    """The owner-authored system/directive budget (the compiler's own precedence)."""
    if isinstance(directive_config, Mapping) and "budget" in directive_config:
        return int(directive_config["budget"])
    return DEFAULT_BUDGET


def _directive_budget(
    directive_config: Any, window_budget_fn: Callable[[], int | None] | None
) -> int | None:
    """``min(authored directive budget, available prompt tokens)`` — W9.25 §3.5.1.

    ``None`` when no window is declared: the compiler then applies the authored
    budget exactly as before. The cap applies to the STABLE system/directive block
    only. History is NEVER auto-trimmed here — the cache contract (AGENTS 6) plus
    D9 make compaction and the surfaces knobs the pressure valves, and the
    ``prompt_budget`` warn is the owner's signal to act.
    """
    if window_budget_fn is None:
        return None
    try:
        available = window_budget_fn()
    except Exception:  # noqa: BLE001 - a budget lookup never breaks a turn
        logger.warning(
            "window budget lookup failed; keeping the authored budget", exc_info=True
        )
        return None
    if available is None:
        return None
    return min(_authored_directive_budget(directive_config), int(available))


def _region_sizes(
    messages: list[dict],
    facts: str,
    library_doc: str | None,
    state: dict,
    plan_text: str | None,
    recalled: str | None = "",
    npc_block: str | None = "",
    palette_block: str | None = "",
    envelope_at: int | None = None,
) -> dict[str, int]:
    """Per-region character sizes for the inspector prompt breakdown (I2).

    ``library`` is a SUBSET of ``system`` (the library doc is interpolated into
    the system directives); it is reported separately for legibility as the doc
    THIS build taught (W9.27: the active component set's doc, resolved per build
    — never a capture taken when the builder was created). History is every
    message between the system prefix and the per-turn suffix message.

    Issue #44: the per-turn engine envelope is ONE ephemeral ``assistant``
    message inserted immediately before the final user message, so ``history``
    is every message between the prefix and that envelope (excluding it and the
    player's message). ``envelope_at`` is the out-of-band index the assembly
    seam already tracks; ``None`` means a lean turn with no envelope.
    """
    system = _content_chars(messages[0]) if messages else 0
    if envelope_at is None:
        # No envelope this turn: everything between the prefix and the player's
        # final message is history.
        end = max(1, len(messages) - 1)
    else:
        end = max(0, min(envelope_at, len(messages)))
    history = sum(_content_chars(message) for message in messages[1:end])
    return {
        "system": system,
        "library": len(library_doc or ""),
        "history": history,
        "facts": len(facts or ""),
        "recalled": len(recalled or ""),
        "npc": len(npc_block or ""),
        "palette": len(palette_block or ""),
        "user": len(str(state.get("user_content", ""))),
        "plan": len(plan_text or ""),
    }


def _splice_compiled_messages_with_counts(
    messages: list[dict], rows: list[dict]
) -> tuple[list[dict], int]:
    """Insert enabled session modules into compiler-path messages (D29).

    The preset path splices via :func:`splice_session_modules` on the flow;
    this is the byte-faithful compiler path's equivalent. ``top`` rows insert
    after the leading system prefix (ahead of history); ``post_history`` rows
    insert immediately before the final user message — the turn's actual ask.
    Returns a NEW list plus ``(top_count, post_count)`` — both land BEFORE the
    final user message, so both shift the ``<preset_def>`` close (§5.1, issue
    #26; ``top`` rows shift the history-splice boundary as well). The input is
    never mutated.

    Roles (issue #68): a ``top`` row rides the leading ``system`` run (it is
    inside the frozen prefix, which ``fold_system_prefix`` folds into ONE), while
    a ``post_history`` row — a card's ``post_history_instructions`` — rides the
    ``assistant`` carrier. It sits AFTER history, so a system role there is a
    system message after index 0: a hard 500 on a stock Jinja template (the
    Qwen family). Its POSITION is deliberately unchanged (the card author put it
    immediately before the ask), so the served content and its recency survive;
    only the role moves.
    """

    tops: list[dict] = []
    posts: list[dict] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("enabled", True):
            continue
        post = row.get("anchor") == "post_history"
        stored = str(row.get("role") or "system")
        if post and stored == "system":
            stored = ENVELOPE_ROLE
        msg = {"role": stored, "content": str(row.get("content") or "")}
        (posts if post else tops).append(msg)
    if not tops and not posts:
        return messages, 0, 0
    out = list(messages)
    if tops:
        cut = 0
        for m in out:
            if m.get("role") == "system":
                cut += 1
            else:
                break
        out[cut:cut] = tops
    if posts:
        last_user = max(
            (i for i, m in enumerate(out) if m.get("role") == "user"), default=None
        )
        at = last_user if last_user is not None else len(out)
        out[at:at] = posts
    return out, len(tops), len(posts)


def _splice_compiled_messages(messages: list[dict], rows: list[dict]) -> list[dict]:
    """Back-compat alias: :func:`_splice_compiled_messages_with_counts` without the counts."""
    return _splice_compiled_messages_with_counts(messages, rows)[0]


def resolve_component_set(log: EventLog, session_id: str) -> None:
    """The per-session component-set resolution boundary, at ``build()`` start.

    Selection is a LOG fact (issues #16/#24): the selection door appends a
    ``set_swapped`` row and this boundary resolves it on the NEXT turn — reading
    the fact from the session's own chain, binding that set directory in the
    shared component store, and nothing else. ``snapshot()`` is called many
    times per turn in no fixed order; resolving HERE (once, first) means the
    taught doc, the gate table and the strip list describe one set for the
    whole turn (P1-2), and a different session's swap can never retarget this
    one.

    The model-facing announcement is the row's frozen ``notice``, read by
    :func:`fabulist.context.facts.set_swap_notice` — never store process state,
    so a preview render cannot consume it.

    A swap never breaks a turn: a resolution failure is logged and the turn
    proceeds on the store's default set.
    """
    from fabulist.dsl import sets as dsl_sets

    try:
        dsl_sets.session_snapshot(log, session_id)
    except Exception:  # noqa: BLE001 - resolution is advisory, never fatal
        logger.warning("component-set resolution failed", exc_info=True)


def make_narrator_message_builder(
    log: EventLog,
    *,
    directive_config: Any = None,
    toggles: Mapping[str, bool] | None = None,
    library_doc: str | None = None,
    window_budget_fn: Callable[[], int | None] | None = None,
    vision_fn: Callable[[], bool] | None = None,
) -> Callable[[TurnState], list[dict]]:
    """Graph-facing message builder on the W2.1 directive compiler.

    Cache-stable system prefix (byte-identical across turns for a fixed
    directive/toggle set), fork-aware DSL-stripped regex-treated history, and a
    per-turn suffix carrying facts/plan + the final user message. Compaction
    summaries join the facts block; the W2.4 ``active_panels`` seam is RETIRED
    (keep-last-3 DSL history replaces it) and the session-static persona block
    splices in ahead of history (D34).

    ``window_budget_fn`` (W9.25 §3.5.1): a zero-arg callable returning the
    available prompt tokens for the turn (``None`` = no declared Context Size),
    resolved per build so a rebind or hot-reload applies on the next turn. The
    effective directive budget becomes ``min(authored, available)``; omitted, the
    compiled prompt is byte-identical to the pre-W9.25 path.

    ``vision_fn`` (issue #79): a zero-arg callable returning whether the
    NARRATE connection accepts image parts, resolved ONCE per build. ``None``
    means text-only — history renders the ``[inline image caption: ...]``
    marker and the current-turn attachment rides as that same marker inside the
    player's final message. Dynamic by construction: a rebind applies on the
    next build, and no capability is baked into the log.

    W9.27/#16: the per-session component-set RESOLUTION is the FIRST thing
    ``build`` does (:func:`resolve_component_set`), and the library doc is
    resolved per build — the builder holds no vocabulary of its own, so the set
    the session selected is taught and gated on the same turn it is announced
    (a selection made between turns takes effect on the next one).

    W9.28: the SPEAKER PALETTE block (the active cast's colours) closes the
    dynamic tail, rebuilt per build — after the lane block, never the prefix.
    """

    def build(state: TurnState, out: dict | None = None) -> list[dict]:
        # Single source of truth (context.facts): the suffix join lives in one
        # importable place so the replay rig recomputes byte-identical suffixes
        # (W3.5) — wiring no longer owns the ordering/blank-drop conventions.
        sid = state["session_id"]
        # W9.27/#16: resolve THIS session's selected set at the turn boundary.
        # First, so everything below — the taught doc, the gate's vocabulary,
        # the announcement in the tail — describes the set this turn runs with.
        resolve_component_set(log, sid)
        recall = state.get("recall") or ""
        scan = state.get("user_content", "")
        # W9.25 §3.5.1: one resolution per build; None keeps the authored budget.
        budget = _directive_budget(directive_config, window_budget_fn)
        recent = " ".join(
            str(n.get("payload", {}).get("prose", ""))
            for n in log.get_projection(sid).get("narratives", [])[-2:]
        )
        # D27: resolve the active preset's token surfaces (with constant
        # fallback) BEFORE assembling context, so the <recalled> copy and the
        # history projection honor them too.
        from fabulist.cards.homes import session_modules_from_log
        from fabulist.prompts.modules import (
            compile_preset_messages_with_boundary,
            splice_session_modules,
        )
        from fabulist.prompts.preset_store import active_preset, preset_directives
        from fabulist.prompts.variables import render_display_text, resolve_variables

        # Issue #63: the player's text reaches the model through the SAME display
        # step the transcript uses, so ``{{user}}`` / ``{{char}}`` / ``{{world}}``
        # name the same things in both places. Unknown tokens stay literal; the
        # stored row keeps the raw bytes (ADR-001) and history rows read the
        # projection's already-rendered copy.
        rendered_user = render_display_text(
            log, sid, str(state.get("user_content") or "")
        )

        preset = None
        surfaces: dict = {}
        variables: dict = {}
        # Preset-carried directive catalog (list[Directive] | None).
        # None = inherit the code catalog (every unedited preset); a non-empty
        # list replaces it in stored order (Item 2 W1).
        dir_base = None
        try:
            preset = active_preset(log)
            surfaces = preset_surfaces(preset)
            dir_base = preset_directives(preset)
            variables = resolve_variables(log, sid, state["user_content"])
        except Exception:  # noqa: BLE001 - surface resolution must never break a turn
            logger.warning("surface resolution fell back to constants", exc_info=True)

        # Issue #39: the Anti Injection toggle, read PER BUILD (the log is the
        # state — flipping it in the settings surface applies to the next turn,
        # no restart). Default OFF: the ``## Engine context`` clause is dropped
        # from the assembled prompt on BOTH paths below (code catalog and
        # preset/artifact rows), while the per-turn envelope — the clause's
        # own ``<preset_runtime>`` payload — keeps riding its ONE universal
        # carrier (issue #44): the envelope INJECTION is not gated by this
        # toggle, only the clause's rendering is. A fold failure keeps the
        # shipped default (OFF), never resurrects the clause.
        anti_injection = ANTI_INJECTION_DEFAULT
        try:
            anti_injection = anti_injection_enabled(log)
        except Exception as exc:  # noqa: BLE001 - the setting must never break a turn
            logger.warning("anti-injection setting read failed", exc_info=True)
            _preset_degraded(
                log,
                sid,
                "anti_injection",
                exc,
                "the Anti Injection setting fell back to its shipped default — "
                "check the settings log",
            )

        lib_effective = (
            resolve_library_doc(surfaces, variables)
            if library_doc is None
            else library_doc
        )
        # D22: one log read yields BOTH blocks. The final user message keeps
        # persona/roster/stt/rolls/clock (prescriptive ground truth); recall,
        # world-info, summaries (and future BTS) ride the post-user `<recalled>`
        # system message. `context_parts` is the FULL canonical context (pre-D22
        # order) — used only for the provenance hash + replay parity, so the hash
        # anchors every dynamic entry regardless of which message carries it.
        blocks = assemble_turn_context(
            log,
            sid,
            recall_text=recall,
            user_text=f"{scan}\n{recent}",
            rolls_text=state.get("rolls") or "",
            facts=state.get("facts") or "",
            bts_text=state.get("bts") or "",
            surfaces=surfaces,
            variables=variables,
        )
        state_facts = join_facts_suffix(blocks.state_parts)
        facts = join_facts_suffix(blocks.context_parts)
        recalled = blocks.recalled
        if out is not None:
            # Provenance (R10/W3.6): hash the FULL dynamic context this turn
            # actually saw — persona, roster, summaries, recall, WI, clock.
            out["facts_hash"] = facts_hash(facts)
            # Replay fidelity (W3.5): the recall block is the ONLY facts part
            # not derivable from the log (vector query against the live memory
            # index at generation time) — record it unconditionally ("" = no
            # recall this turn) so replay recomputes the exact same suffix
            # byte-for-byte instead of approximating, and can distinguish
            # "no recall" from "recall not recorded" (legacy turns).
            out["recall"] = recall
            # D25 C4: recall_queries/recall_config are NOT builder outputs —
            # the engine unpacks RecallResult into STATE (the builder only ever
            # sees the block string); post_turn stamps them from state. Never
            # emit them here: an outbox [] would clobber the engine's values.
            # Inspector I2: the injected facts TEXT (not just the hash) rides
            # the narrative config so the pane shows what the model actually saw.
            out["facts_text"] = facts
        prior_plan = load_last_plan(log, sid)
        plan_text = render_plan_for_prompt(prior_plan) if prior_plan else None
        # ---- the ONE trailing engine envelope (provenance contract) --------
        # Every per-turn block is composed HERE, before either compile path, so
        # the state/notice/memory/lane/steering/palette order is a contract and
        # not a by-product of flow row order. Contract order (§3.3): state ->
        # notice -> memory -> lane -> steering -> palette (palette LAST: it
        # describes this turn's own output, so nothing it describes sits below
        # it). Empty means absent; no blocks = no envelope message at all.
        from fabulist.graph.nodes import render_lane_block

        lane_block = render_lane_block(state.get("npc_lanes") or {})
        # W9.28 D7: the palette is recomputed per build and rides the tail.
        palette_block = ""
        try:
            palette_block = render_palette_block(palette_for_turn(log, sid))
        except Exception:  # noqa: BLE001 - the palette is advisory, never fatal
            logger.warning("speaker palette block could not be built", exc_info=True)
        engine_tail = render_engine_envelope(
            state=render_state_block(state_facts),
            notice=blocks.notice,
            memory=blocks.recalled,
            lane=lane_block,
            steering=plan_text or state.get("plan") or None,
            palette=palette_block,
        )
        # D18: the pipeline is data. When an active preset carries custom
        # modules, the message list compiles from THEM (same structural parts
        # this builder resolves — parity by construction); no preset = the
        # byte-faithful compiler path below, untouched.
        history_knobs = surfaces.get("history") or {}
        # Issue #79: resolve THIS consumer's live vision capability once per
        # build. The narrator's history renders inline for a vision connection
        # and as the bracketed caption marker otherwise — per call, never baked
        # into a row.
        vision = bool(vision_fn()) if vision_fn is not None else False
        history_block = history_from_log(
            log,
            sid,
            limit=int(history_knobs.get("depth", 20)),
            strip=bool(history_knobs.get("strip_dsl", DEFAULT_HISTORY_STRIP_DSL)),
            surfaces=surfaces,
            vision=vision,
            # The turn's own user row is already in the projection (the API
            # appends it before the graph runs) and the flow's `user`/`facts`
            # modules re-append the SAME turn as the final state-wrapped user
            # message — the bare history copy is pure duplication.
            drop_current_user=True,
        )
        messages: list[dict] | None = None
        # §5.1 (issue #26) boundary bookkeeping: the compile step hands the
        # ``<preset_def>`` close (``def_end`` — one past the final user
        # message) out-of-band; wiring translates it to FINAL-list coordinates
        # per path (in-front-of-the-user splice/static-insert shift terms) for
        # the wrapper. ``None`` = no path produced one (a compile double that
        # ignores ``boundary_out``): the whole list is then definition.
        boundary_out: dict = {}
        def_end: int | None = None
        # Issue #44: the index of the ONE ephemeral engine envelope message the
        # compile seam inserted immediately before the final user message
        # (``None`` = a lean turn with no envelope, or a double that ignores
        # ``boundary_out``).
        envelope_at: int | None = None
        try:
            if preset is not None and preset.modules:
                # Structural parts map onto the SAME compiler regions the
                # default path emits: the stable system prefix (directives +
                # framing), and the per-turn suffix (facts/plan/panels + user)
                # assembled as one block so plan/panel injections are never
                # dropped by a seeded preset. `history` and `library` come
                # from the live pipeline below.
                system_only = compiler_compile(
                    directive_config,
                    dict(toggles or {}),
                    facts=None,
                    history=[],
                    user_content="",
                    plan=None,
                    library_doc="",
                    # W9.25 §3.5.1: the stable system prefix is the ONE block the
                    # assembled window may cap; the suffix compile below only
                    # supplies the per-turn user text.
                    budget=budget,
                    surfaces=surfaces,
                    variables=variables,
                    directives=dir_base,
                    anti_injection=anti_injection,
                )
                # The player's message compiles BARE: the state facts and the
                # steering pin ride the engine envelope appended below, never
                # the user turn (provenance contract).
                suffix = compiler_compile(
                    directive_config,
                    {},
                    facts=None,
                    history=[],
                    user_content=rendered_user,
                    plan=None,
                    library_doc=None,
                    surfaces=surfaces,
                    variables=variables,
                    directives=dir_base,
                    anti_injection=anti_injection,
                )
                # D18 variables: text modules resolve {{name}} against the
                # live session (last turns, char, persona, state, wi, ...).
                # D29: session-carried modules (the card system_prompt /
                # post_history_instructions / mes_example homes) splice into
                # the active narrate flow at BUILD time. Preview renders
                # through this same builder, so parity is by construction.
                # The boundary rides out-of-band (``boundary_out``): the
                # compile records ``def_end`` where the final user message is
                # emitted, never re-derived here from roles.
                messages, _preset_history_start = compile_preset_messages_with_boundary(
                    splice_session_modules(
                        preset.modules,
                        session_modules_from_log(log, sid),
                    ),
                    {
                        "narrator_system": "".join(
                            str(m.get("content", "")) for m in system_only
                        ),
                        "library": lib_effective,
                        "history": history_block,
                        # The state facts are NOT a user message any more: the
                        # whole per-turn tail is the one engine envelope.
                        "facts": "",
                        "user_content": str(
                            suffix[-1].get("content", "") if suffix else rendered_user
                        ),
                        "engine_tail": engine_tail or "",
                        "variables": variables,
                    },
                    boundary_out,
                )
                def_end = int(boundary_out.get("def_end", len(messages)))
                envelope_at = boundary_out.get("envelope_at")
                # An authored flow that dropped its `recalled` row still gets
                # the engine envelope: the contract is positional (immediately
                # before the player's final message, issue #44), not a row an
                # author can silently lose.
                if engine_tail and envelope_at is None:
                    messages, def_end, envelope_at = insert_engine_envelope(
                        messages, def_end, engine_tail
                    )
                    boundary_out["def_end"] = def_end
                    boundary_out["envelope_at"] = envelope_at
        except Exception:  # noqa: BLE001 - preset path must never break a turn
            logger.warning("preset compile fell back to compiler path", exc_info=True)
            messages = None
        if messages is None:
            # §5.1: both fallback boundaries travel out-of-band via
            # ``boundary_out`` (a compile double that ignores it changes
            # nothing — the layout constant ``len(messages)`` is the safe
            # default: the whole compiled list is definition).
            messages = compiler_compile(
                directive_config,
                dict(toggles or {}),
                facts=state_facts or None,
                history=history_block,
                user_content=rendered_user,
                plan=plan_text or state.get("plan") or None,
                library_doc=lib_effective,
                recalled=recalled,
                notice=blocks.notice,
                lane=lane_block,
                palette=palette_block,
                budget=budget,
                surfaces=surfaces,
                variables=variables,
                directives=dir_base,
                anti_injection=anti_injection,
                boundary_out=boundary_out,
            )
            def_end = int(boundary_out.get("def_end", len(messages)))
            envelope_at = boundary_out.get("envelope_at")
            # D29 (compiler path): session-carried modules ride this path too —
            # a bare engine (feel-test harness, scratch logs) has no persisted
            # default preset, and card homes must never silently drop. Guarded
            # like every builder extra: a splice failure never breaks a turn.
            # Top rows splice ahead of history and post_history rows directly
            # before the final user message: both land BEFORE ``def_end``, so
            # both extend the definition bound (issue #26).
            try:
                _session_rows = session_modules_from_log(log, sid)
                if _session_rows:
                    messages, top_rows, post_rows = (
                        _splice_compiled_messages_with_counts(messages, _session_rows)
                    )
                    def_end += top_rows + post_rows
            except Exception:  # noqa: BLE001
                logger.warning(
                    "session-module splice failed on compiler path", exc_info=True
                )
        # D34: session-static context (persona/world card block) renders as its
        # own system message directly after the directive — identity is stable
        # across turns and sits ahead of history; <state> stays dynamic-only.
        static_block = join_facts_suffix(
            static_prefix_parts(log, sid, variables=variables)
        )
        static_inserted = bool(static_block) and bool(messages)
        if static_inserted:
            messages.insert(1, {"role": "system", "content": static_block})
            # The insert lands between the directive and history — inside the
            # definition bound whenever a message already followed it. The
            # envelope (issue #44) sits after history, so it shifts too.
            if def_end is not None and def_end > 1:
                def_end += 1
            if envelope_at is not None and envelope_at >= 1:
                envelope_at += 1
        image_url = state.get("image_url") or ""
        if image_url and messages:
            # Attach to the final USER message — with D22 the last message is
            # the recalled system turn, which must never carry image parts.
            last = next(
                (m for m in reversed(messages) if m.get("role") == "user"),
                messages[-1],
            )
            # Issue #79: the gate is the NARRATE connection's live vision
            # toggle. A vision connection gets the native multipart message
            # (llama.cpp mtmd: image_url for images, input_video for videos at
            # the server's video-fps); a text-only one gets the already-computed
            # arrival text as the uniform bracketed marker inside the message.
            if vision:
                kind = state.get("image_kind") or "image"
                if kind == "video":
                    part = {"type": "input_video", "input_video": {"url": image_url}}
                else:
                    part = {"type": "image_url", "image_url": {"url": image_url}}
                last["content"] = [
                    {"type": "text", "text": str(last.get("content", ""))},
                    part,
                ]
            else:
                marker = build_image_turn_prefix(str(state.get("image_text") or ""))
                last["content"] = f"{marker}\n{last.get('content', '')}"
        # DYNAMIC TAIL (never the prefix — AGENTS #6). Everything that changes
        # per turn is ONE ephemeral ``assistant`` message — the engine envelope
        # composed above from, in contract order:
        #   <engine_state> -> <engine_notice> -> <engine_memory> ->
        #   <engine_lane> -> <steering> -> <engine_palette>.
        # Issue #44 (owner ruling 2026-10-24): the envelope is inserted
        # IMMEDIATELY BEFORE the final user message by the compile seam
        # (``insert_engine_envelope``) — never a system message (stock Jinja
        # templates ``raise_exception`` on any non-first system message: the
        # hard 500 this issue fixes) and never inside the player's message
        # (engine data there acquires player provenance). It is EPHEMERAL: it
        # is rebuilt every turn and never persisted, so it can never appear in
        # a later turn's history.
        #
        # §5.1 (issue #26) the TWO-BOUND wrapper — the FINAL message-list
        # transformation, after both construction paths, the session-module
        # splices, the static persona-block insert, lane block, palette and
        # image multipart, so the positional rule sees the finished layout:
        #   [0, def_end)   -> <preset_def>  (engine definition + the
        #                     conversation through the player's latest message)
        #   envelope_at    -> <preset_runtime> (the ONE per-turn engine
        #                     envelope; positional, INSIDE the def region but
        #                     marked engine-runtime by provenance, not position)
        #   [def_end, end) -> <preset_runtime> (any authored rows a flow serves
        #                     after the player's final message)
        # Both boundaries travelled out-of-band from the emission sites; this
        # seam never role-scans. Renaming the tags from <preset> changes the
        # stable-prefix bytes and so invalidates the prompt cache ONCE
        # (ADR-005 / AGENTS #6) — accepted.
        if def_end is None:
            def_end = len(messages)
        # Issue #44 (ONE system message in the prefix): fold the leading system
        # run — the compiled directive block, the session-static persona/cast
        # block and the library spec — into a single system message. Stock Jinja
        # chat templates (Qwen family) raise_exception on ANY system message
        # that is not the first, so the split prefix was a hard 500 on a stock
        # local GGUF. Content is untouched and in served order (behind the
        # renderer's own section divider); the stable prefix stays byte-identical
        # turn-to-turn (ADR-005).
        if envelope_at is None:
            messages, (def_end,) = fold_system_prefix(messages, (def_end,))
        else:
            messages, (def_end, envelope_at) = fold_system_prefix(
                messages, (def_end, envelope_at)
            )
        # Issue #77: the guard line + both bounds are INSTRUCTION framing, so
        # they gate on the same folded Anti Injection setting as the clause and
        # the framing paragraph. OFF = the same messages in the same order, with
        # no guard and no tags (the player's message bare and untagged).
        messages = wrap_preset_messages(
            messages,
            def_end,
            envelope_at=envelope_at,
            anti_injection=anti_injection,
        )
        # Belt + braces: the envelope is never in player scope (provenance) and
        # never a non-first system message (a hard 500 on a stock template).
        assert_envelope_not_in_user_scope(messages, engine_tail)
        assert_envelope_not_in_system_scope(messages, engine_tail)
        if out is not None:
            out["regions"] = _region_sizes(
                messages,
                facts,
                lib_effective,
                state,
                plan_text,
                recalled or "",
                lane_block,
                palette_block,
                envelope_at=envelope_at,
            )
        return messages

    return build


def narrator_system_prompt(*, anti_injection: bool = False) -> str:
    """The greeting/opening system contract (consolidation §5.3).

    ONE content source with the live narrate builder: the parts.py catalog
    through the compiler's system render (``templates/system.j2``). The
    narrator.py hand-synced mirror is retired; the compiled surface serves
    both paths.

    ``anti_injection`` (issue #39) is the persisted Anti Injection setting: the
    opening is a built prompt like any turn, so it obeys the same default (OFF =
    no ``## Engine context`` clause, and no two-bound framing paragraph — issue
    #77; the framing's first paragraph always renders). The one caller that owns a log
    (:func:`fabulist.api.planner_api._opening_messages`) passes the folded
    value; the default here is the shipped default, never the ON state.
    """
    return render_system_prompt(
        library_doc=render_library_doc(), anti_injection=anti_injection
    )


OOC_SYSTEM = (
    "You are the out-of-character (OOC) assistant for an ongoing roleplay. "
    "Reply directly, concisely, and helpfully. You are given the recent scene "
    "history and the current state facts — use them when answering. Do not "
    "narrate, do not write prose, and do not advance the scene, time, or state."
)


def facts_line_from_log(log: EventLog, session_id: str) -> str | None:
    """Folded state as the pipe-YAML facts line (None when nothing tracked)."""
    state = log.get_projection(session_id).get("state") or {}
    if isinstance(state, dict) and state:
        return render_facts({str(k): str(v) for k, v in state.items()})
    return None


def make_ooc_message_builder(
    log: EventLog,
    *,
    vision_fn: Callable[[], bool] | None = None,
) -> Callable[[str, str], list[dict]]:
    """OOC turns see recent history + state facts; they just never advance.

    D18 completion: the OOC interaction compiles through its MODULE FLOW
    (preset-customized or default) — the same data-driven pipeline as narrate.

    BUG FIXED HERE (flows-lane finding): ``assemble_messages(kind="ooc")``
    computed the facts block and DISCARDED it — the OOC system prompt promises
    "you are given the current state facts" but they never reached the model.
    The state facts now ride the same per-turn engine envelope as the narrate
    path (provenance contract: engine data never sits in the player's message),
    and the ``[OOC]`` flag the OOC clause keys on stays prefixing the user
    content. Issue #44: the envelope rides the ONE universal carrier — an
    ephemeral ``assistant`` message inserted immediately before the final user
    message, never a trailing system one (a stock Jinja template refuses any
    non-first system message).
    """

    def build(session_id: str, content: str) -> list[dict]:
        from fabulist.prompts.narrator import OOC_FLAG, render_state_block
        from fabulist.prompts.preset_store import active_preset, compile_preset_flow
        from fabulist.prompts.variables import render_display_text, resolve_variables

        try:
            preset = active_preset(log)
        except Exception as exc:  # noqa: BLE001 - absent store = defaults
            logger.warning("ooc preset read failed; using code defaults", exc_info=True)
            _preset_degraded(
                log,
                session_id,
                "ooc",
                exc,
                "the OOC turn used the code default prompt — check the preset store",
            )
            preset = None
        facts_line = facts_line_from_log(log, session_id) or ""
        # The state facts are the envelope's <engine_state> block, never a
        # user-role message (the flow's `facts` row is therefore inert here).
        envelope = render_engine_envelope(state=render_state_block(facts_line))
        parts = {
            "system": OOC_SYSTEM,
            # Same duplication as narrate: the API pre-appended the OOC turn's
            # user row, and `user_content` below re-appends it flagged.
            # Issue #79: OOC resolves ITS OWN connection's vision (the ``ooc``
            # interaction, falling back to the narrator) per build.
            "history": history_from_log(
                log,
                session_id,
                limit=8,
                drop_current_user=True,
                vision=bool(vision_fn()) if vision_fn is not None else False,
            ),
            "facts": "",
            # Issue #63: the same display step the transcript uses, so the OOC
            # turn's ``{{user}}`` resolves exactly like the bubble that sent it.
            "user_content": (
                f"{OOC_FLAG} {render_display_text(log, session_id, str(content))}"
            ),
            "variables": resolve_variables(log, session_id, content),
        }
        try:
            messages = compile_preset_flow(preset, "ooc", parts)
            # Issue #44: ONE universal carrier — the envelope is an ephemeral
            # ``assistant`` message immediately BEFORE the final user message.
            messages, _def_end, _envelope_at = insert_engine_envelope(
                messages, len(messages), envelope
            )
            return messages
        except Exception:  # noqa: BLE001 - flow path must never break a turn
            logger.warning("ooc flow compile fell back to assemble", exc_info=True)
            return assemble_messages(
                system=OOC_SYSTEM,
                history=parts["history"],
                facts=facts_line,
                user_msg=content,
                kind="ooc",
            )

    return build


def _provider_label(
    registry: Registry, log: EventLog, interaction: str, fallback: str
) -> Callable[[], str]:
    """A LIVE reader of the model an interaction's calls resolve to (``""``).

    A provider failure aborts the turn with ``"<phase> failed: 429 from <model>
    …"``, so the message has to name the model the phase ACTUALLY called. This
    resolves the interaction with the CALLER's own rule
    (:func:`_interaction_slot`: preset binding > per-item default > task slot >
    type slot > ``fallback``) and reads that connection's model EVERY TIME it is
    read — the wiring hands it over as a closure, and every phase reads it at the
    failure, not at build time and not once per run.

    Reading the caller object's last-resolved agent instead was the bug: it is a
    post-hoc attribute (the caller's FALLBACK slot before the first call, e.g.
    ``extractor`` -> the extraction model), so the abort could name a model the
    phase never called — and a rebind had to wait for the caller to resolve
    before the label caught up. Swallowed to ``""``: a label read is cosmetic and
    must never raise into the phase it describes.
    """

    def label() -> str:
        connection = _interaction_connection(registry, log, interaction, fallback)
        return "" if connection is None else str(connection.model or "")

    return label


def build_engine(
    *,
    registry: Registry,
    log: EventLog,
    checkpointer: Any = None,
    narrator_agent: str = "narrator",
    recall_fn: Any | None = None,
    agent_debug: bool = False,
    message_builder: Any | None = None,
    bts_enabled: bool = True,
    attribution_enabled: bool = True,
    off_turn: dict[str, Any] | None = None,
) -> GraphTurnEngine:
    """Assemble the full turn engine from landed parts.

    ``recall_fn``: optional ``async (session_id, user_text) -> str | None`` —
    the pre-turn vector-memory recall seam (W3.2). The engine embeds the
    user's message and injects the recalled block into ``state["recall"]``;
    the message builder renders it in the facts suffix.

    ``off_turn`` is an optional OUT mapping: the seams the app builds OUTSIDE the
    turn need from this function, filled here because this is where their model
    bindings live. Today that is ``"proposal_lanes"`` — the W9.23 §3.6 historian's
    lane system, which must see the SAME ``proposals`` generator caller and the
    ONE shared decision caller the turn's other phases use (a second
    construction in the API layer would drift from the turn's).
    """
    # D19 agent sets: the narrator interaction resolves its agent PER TURN
    # from the active preset's agent_bindings (fallback: connections.json
    # mapping for "narrate" — today the narrator agent). Constructing
    # RegistryModelCallers per agent is cheap (thin config lookup).
    class PresetAgentCaller:
        """Resolves the interaction's agent at call time from the preset.

        Resolution rides ONE function —
        :func:`fabulist.connections.taxonomy.resolve_interaction_agent` — so the
        typed taxonomy slots (``completion.narration`` &c.) mean the same thing
        here as in the connections document: preset binding > per-item default >
        task slot > type slot > this caller's fallback.
        """

        agent = narrator_agent  # resolved per call (logging + usage rows)

        def __init__(self, interaction: str, fallback_agent: str) -> None:
            self._interaction = interaction
            self._fallback = fallback_agent
            self._cache: dict[str, Any] = {}

        def _caller(self) -> Any:
            agent = self._fallback
            try:
                from fabulist.connections.taxonomy import resolve_interaction_agent
                from fabulist.prompts.agent_sets import bindings_for

                agent = resolve_interaction_agent(
                    bindings_for(log),
                    registry.agents,
                    self._interaction,
                    self._fallback,
                )
            except Exception as exc:  # noqa: BLE001 - preset lookup must never break calls
                logger.warning(
                    "%s caller binding fell back to %r",
                    self._interaction,
                    self._fallback,
                    exc_info=True,
                )
                _preset_degraded(
                    log,
                    _format_session_for_row(),
                    f"{self._interaction}_binding",
                    exc,
                    "the call fell back to its default agent — check the preset "
                    "bindings",
                )
            self.agent = agent
            if agent not in self._cache:
                # W5-S2 chokepoint: every resolved narrator/OOC caller is
                # capture-wrapped here, so OOC rides the same capture as the
                # narrator pass (the per-session toggle decides per call).
                self._cache[agent] = make_agent_caller(
                    agent, registry, log, agent_debug_default=agent_debug,
                    emit_finish=True,
                )
            return self._cache[agent]

        def call_timeout_s(self) -> float | None:
            """The resolved connection's WALL-CLOCK per-call deadline (seconds).

            A live read (a Connections edit applies to the next phase that asks),
            forwarded to seams that must RECORD the bound they run under — the
            planner seed's tool loop is the one that does (it has no transport of
            its own: every call inside it rides this caller). ``None`` when the
            slot resolves to nothing, exactly like ``Connection.timeout``.
            """
            try:
                self._caller()  # resolves (and caches) the agent this call would use
                return registry.connection_for(self.agent).call_timeout_s
            except Exception as exc:  # noqa: BLE001 - a settings read never breaks a phase
                # Issue #70: a swallowed settings read is the same class.
                logger.warning(
                    "%s call-deadline read failed; the phase records no bound",
                    self._interaction,
                    exc_info=True,
                )
                _preset_degraded(
                    log,
                    _format_session_for_row(),
                    f"{self._interaction}_connection",
                    exc,
                    "the phase recorded no call deadline — check the "
                    "connection settings",
                )
                return None

        async def __call__(
            self,
            messages: list[dict],
            stream: bool = True,
            request_profile: dict | None = None,
        ) -> Any:
            async for event in self._caller()(
                messages, stream, request_profile=request_profile
            ):
                yield event

    caller: Any = PresetAgentCaller("narrate", narrator_agent)
    # W3c first run: the connections document may not exist yet, so no narrator is
    # bound. BOOT must not crash on that (the create door is what fixes it); the
    # snapshot is per-turn metadata, and a TURN with nothing bound fails at the
    # caller with the registry's readable "unknown agent" error, never a NoneType
    # 500 — hence the fallbacks here rather than a fabricated connection.
    try:
        narrator_conn: Connection | None = registry.connection_for(narrator_agent)
    except ConnectionConfigError:
        narrator_conn = None
    config_snapshot = {
        "model": narrator_conn.model if narrator_conn is not None else "",
        "sampler": dict(narrator_conn.sampling or {}) if narrator_conn is not None else {},
        "prompt_version": 2,  # W2.1 compiler + interweave + WI/roster facts
        "facts_hash": None,   # per-turn (narrator node fills it)
    }
    # D18 completion: the extract interaction compiles through its MODULE
    # FLOW (preset-customized or default; the prompt_overrides shim wins only
    # while the flow stays all-default — precedence documented in
    # preset_store.compile_preset_flow). Variables resolve per call so
    # {{message}}/{{last_narrative}} carry the turn under extraction.
    def _extract_flow_messages(
        user_msg: str, narrative: str, session_id: str = ""
    ) -> list[dict]:
        # Lazy imports at call scope (cycle hygiene). The turn under
        # extraction rides the variables; the session-scoped resolvers use
        # "" (the extractor is post-turn and session-blind by contract).
        # The session id is threaded explicitly (issue #70) so a failed preset
        # read can leave its ``preset_degraded`` row on the TURN's session.
        from fabulist.prompts.preset_store import active_preset, compile_preset_flow
        from fabulist.state.extractor import EXTRACTOR_SYSTEM_PROMPT, _turn_block

        try:
            preset = active_preset(log)
        except Exception as exc:  # noqa: BLE001
            logger.warning("extract preset read failed; using code defaults", exc_info=True)
            _preset_degraded(
                log,
                session_id or "",
                "extract",
                exc,
                "the extractor used the code default prompt — check the preset store",
            )
            preset = None
        return compile_preset_flow(
            preset,
            "extract",
            {
                "system": EXTRACTOR_SYSTEM_PROMPT,
                "turn_payload": _turn_block(user_msg, narrative),
                "variables": {"message": user_msg, "last_narrative": narrative},
            },
        )

    # D21/B4: the ``bts`` interaction owns world-state; the extractor's delta
    # emission is retired (it stays importable for the eval rig). Speaker
    # attribution is NOT a model interaction any more (owner decision:
    # deterministic, see :mod:`fabulist.state.attribution`) — the ``bts`` pass
    # still resolves its agent per call from the active preset's agent_bindings
    # (fallback ``extractor``) and compiles through its D18 module flow.
    def _bts_flow_messages(
        user_msg, narrative, prior_state, checkpoint, session_id: str = ""
    ):
        # D25 capture correctness: post-turn hooks inherit the pump's
        # CURRENT_INTERACTION ("narrate") — relabel so bts captures/usage
        # attribute truthfully.
        from fabulist.agents.debug import CURRENT_INTERACTION

        CURRENT_INTERACTION.set("bts")
        from fabulist.prompts.preset_store import active_preset, compile_preset_flow
        from fabulist.state.bts import BTS_SYSTEM_PROMPT

        try:
            preset = active_preset(log)
        except Exception as exc:  # noqa: BLE001
            logger.warning("bts preset read failed; using code defaults", exc_info=True)
            _preset_degraded(
                log,
                session_id,
                "bts",
                exc,
                "the world-state pass used the code default prompt — check the "
                "preset store",
            )
            preset = None
        base = build_bts_messages(user_msg, narrative, prior_state, checkpoint=checkpoint)
        return compile_preset_flow(
            preset,
            "bts",
            {
                "system": str(base[0].get("content", BTS_SYSTEM_PROMPT)),
                "payload": str(base[1].get("content", "")) if len(base) > 1 else "",
                "variables": {},
            },
        )

    bts_caller = PresetAgentCaller("bts", "extractor")

    def _bts_request_profile() -> dict | None:
        """W8.6: the BTS pass's Tier A transport profile, or ``None`` (notation).

        Resolved per call from the interaction's bound agent's connection
        capabilities, so a re-bound preset agent or a hot-reloaded connection is
        honoured. ``None`` — a connection that cannot do function-calling — keeps
        today's notation behaviour (the permanent Tier D floor, D69).
        """
        from fabulist.connections.capabilities import resolve_capabilities
        from fabulist.connections.taxonomy import resolve_interaction_agent
        from fabulist.state.bts import build_bts_request_profile

        agent = "extractor"
        try:
            from fabulist.prompts.agent_sets import bindings_for

            agent = resolve_interaction_agent(
                bindings_for(log), registry.agents, "bts", agent
            )
        except Exception as exc:  # noqa: BLE001 - preset lookup must never break calls
            logger.warning("bts binding resolution fell back to %r", agent, exc_info=True)
            _preset_degraded(
                log,
                _format_session_for_row(),
                "bts_binding",
                exc,
                "the BTS request profile fell back to its default agent — check "
                "the preset bindings",
            )
        try:
            return build_bts_request_profile(
                resolve_capabilities(registry.connection_for(agent))
            )
        except Exception as exc:  # noqa: BLE001 - unknown connection degrades to notation
            logger.warning(
                "bts request profile read failed; using notation", exc_info=True
            )
            _preset_degraded(
                log,
                _format_session_for_row(),
                "bts_connection",
                exc,
                "the BTS pass fell back to the notation path — check the "
                "connection settings",
            )
            return None

    def _loop_connection(interaction: str, fallback: str) -> Connection | None:
        """The connection a tool-loop interaction resolves to (``None`` if unknown).

        ONE resolution for the loop seams that read an interaction's connection
        (the reasoning-echo readers below): the preset binding wins, then the
        taxonomy slots, then ``fallback`` — the same rule every caller rides, so
        a re-bound preset agent or a hot-reloaded connection applies on the next
        call. An unknown connection answers ``None`` rather than raising: a
        settings read must never be the reason a pass does not run.
        """
        from fabulist.connections.taxonomy import resolve_interaction_agent

        agent = fallback
        try:
            from fabulist.prompts.agent_sets import bindings_for

            agent = resolve_interaction_agent(
                bindings_for(log), registry.agents, interaction, agent
            )
        except Exception as exc:  # noqa: BLE001 - preset lookup must never break calls
            logger.warning(
                "%s binding resolution fell back to %r", interaction, agent,
                exc_info=True,
            )
            _preset_degraded(
                log,
                _format_session_for_row(),
                f"{interaction}_binding",
                exc,
                "the loop's connection fell back to its default agent — check "
                "the preset bindings",
            )
        try:
            return registry.connection_for(agent)
        except Exception:  # noqa: BLE001 - unknown connection keeps the default
            return None

    def _loop_return_thinking(interaction: str, fallback: str) -> bool | None:
        """The interaction connection's "Return thinking" setting (``None`` = AUTO).

        Handed to the agent loop unchanged: AUTO repeats a thinking model's
        ``reasoning_content`` back inside a tool loop whenever the provider
        returned some — required by DeepSeek's thinking mode after a tool call,
        invisible to a provider that returns none. ``false`` suppresses the echo
        for a provider that rejects the field, ``true`` pins it on.
        """
        connection = _loop_connection(interaction, fallback)
        return None if connection is None else connection.return_thinking

    def _bts_loop_timeout() -> float | None:
        """The transport bound each BTS loop call rides (PROVENANCE only, W9.24).

        Hang protection is PER API CONNECTION (owner ruling): the bound is the
        interaction's connection ``timeout``, enforced by the transport. The
        runner records it on its ``agent_loop`` rows so an inspector reads "this
        call rode 900s" without opening the connection config; nothing here
        bounds a run total.
        """
        from fabulist.connections.taxonomy import resolve_interaction_agent

        agent = "extractor"
        try:
            from fabulist.prompts.agent_sets import bindings_for

            agent = resolve_interaction_agent(
                bindings_for(log), registry.agents, "bts", agent
            )
        except Exception as exc:  # noqa: BLE001 - preset lookup must never break calls
            logger.warning("bts timeout binding fell back to %r", agent, exc_info=True)
            _preset_degraded(
                log,
                _format_session_for_row(),
                "bts_binding",
                exc,
                "the BTS loop timeout fell back to its default agent — check the "
                "preset bindings",
            )
        try:
            return registry.connection_for(agent).timeout
        except Exception:  # noqa: BLE001 - unknown connection keeps the row honest
            return None

    def _bts_loop_return_thinking() -> bool | None:
        """The BTS connection's reasoning echo (``None`` = AUTO).

        Without it a DeepSeek thinking model's SECOND action request is a 400:
        the assistant ``tool_calls`` message must carry the reasoning the first
        completion returned. AUTO echoes whatever was received, never invents it.
        """
        return _loop_return_thinking("bts", "extractor")

    world_state = (
        BtsRunner(
            bts_caller,
            renderer=_bts_flow_messages,
            log=log,
            profile_fn=_bts_request_profile,
            timeout_fn=_bts_loop_timeout,
            # W9.25 §3.5.3: the BTS slot's own budget reader, so the read-tools
            # loop's rows carry passive headroom (None = undeclared = today).
            window_fn=bts_window_budget(registry, log),
            return_thinking_fn=_bts_loop_return_thinking,
        )
        if bts_enabled
        else None
    )
    decision = DecisionCaller(registry, observer=_decision_observer(log))

    # No cast-promotion gate is installed: cast admission is USER-GATED (owner
    # ruling), so BTS detection files a ``cast`` proposal and the owner's accept
    # is the only admission (``state.cast.admit_from_proposal``). The decision
    # caller above stays — the attribution path and the proposal lanes use it.

    def _attribution_palette() -> list[PaletteEntry]:
        """The CURRENT session's speaking-cast palette, read once per turn.

        Read once per call by :class:`MarkupAttributionRunner`. The session comes
        from the turn's ``CURRENT_SESSION`` (set pump-side, ``api/app.py``) rather
        than from this closure: ONE engine serves every session, so the session
        scope has to be per call, exactly like the other post-turn passes'
        capture labels. No session — or an empty roster — means no palette, and
        the pass then attributes nothing (strict omission).
        """
        from fabulist.agents.debug import CURRENT_SESSION

        session_id = CURRENT_SESSION.get("")
        if not session_id:
            return []
        return palette_for_turn(log, session_id)

    attribution = (
        MarkupAttributionRunner(_attribution_palette) if attribution_enabled else None
    )

    # W9.20 ([OWNER] #11): the planner's model seam. It rides the ``planner``
    # interaction's binding, whose D19 default IS the narrator's agent — the
    # planner writes the story's steering on the writer's model today, and a
    # faster planner model is later a binding edit. Relabelled on capture like
    # bts/critic: the planner node owns its messages (graph/nodes), so the
    # caller wrapper is the only place its calls can be labelled truthfully.
    planner_agent_caller = PresetAgentCaller("planner", narrator_agent)

    async def planner_caller(
        messages: list[dict],
        stream: bool = True,
        request_profile: dict | None = None,
    ) -> Any:
        from fabulist.agents.debug import CURRENT_INTERACTION

        CURRENT_INTERACTION.set("planner")
        async for event in planner_agent_caller(
            messages, stream, request_profile=request_profile
        ):
            yield event

    # Issue #25: the seed phase runs its tool loop THROUGH this caller, so this
    # is where the wall-clock bound it rides is readable. `run_planner_tool_phase`
    # picks the reader up off the caller (it owns no registry) and puts the value
    # on the loop's `agent_loop` rows — the bound is the transport's
    # `call_timeout_s` deadline, and the seed's trace should say so rather than
    # claim the phase is unbounded. Forwarded live: the reader resolves per call.
    planner_caller.call_timeout_s = planner_agent_caller.call_timeout_s  # type: ignore[attr-defined]

    def _planner_config() -> dict:
        """The ACTIVE preset's planner config (defaults when absent/unreadable).

        A CALLABLE is what the node and the junction take, so a Studio cadence /
        fields / steer edit applies on the next due evaluation with no rebuild
        (the planner config is deliberately outside the topology digest).

        Issue #70: a failed read silently drops the owner's steer/fields, which
        reads like a model that ignored instructions — so it warns and leaves a
        ``preset_degraded`` row on the turn's session.
        """
        from fabulist.prompts.preset_store import active_preset, planner_config

        try:
            preset = active_preset(log)
        except Exception as exc:  # noqa: BLE001 - a bad preset must not break a turn
            logger.warning("planner config read failed; using defaults", exc_info=True)
            _preset_degraded(
                log,
                _format_session_for_row(),
                "planner_config",
                exc,
                "the planner used its default cadence/steer/fields — check the "
                "preset store",
            )
            preset = None
        return planner_config(preset)

    def _planner_plan_cadence() -> int:
        """Committed turns between planner runs (the junction's due rule)."""
        cadence = _planner_config().get("cadence")
        return cadence if isinstance(cadence, int) and cadence >= 1 else DEFAULT_PLAN_CADENCE

    def _planner_flow_messages(session_id: str = "") -> list[dict]:
        """Render the planner's prompt surface through the preset's flow.

        W9.20 §6: the planner is edited like the critic or the lanes — its
        ``planner`` flow's ``system`` row is the directive, and this is the one
        place that row is compiled. The context block and the refresh/steer ask
        are structural (the node composes them from the log + the planner
        config), so the flow carries a single authored row.
        """
        from fabulist.prompts.preset_store import active_preset, compile_preset_flow

        try:
            preset = active_preset(log)
        except Exception as exc:  # noqa: BLE001 - absent store = the code default
            logger.warning(
                "planner directive read failed; using the code default", exc_info=True
            )
            _preset_degraded(
                log,
                session_id,
                "planner_directive",
                exc,
                "the planner used the code default directive — check the preset "
                "store",
            )
            preset = None
        return compile_preset_flow(
            preset,
            "planner",
            {"system": DEFAULT_PLANNER_DIRECTIVE, "variables": {}},
        )

    def _planner_directive(session_id: str = "") -> str:
        """The planner's directive text (the flow's system row, compiled)."""
        messages = _planner_flow_messages(session_id)
        content = messages[0].get("content") if messages else None
        return str(content or DEFAULT_PLANNER_DIRECTIVE)

    def _planner_request_profile() -> dict | None:
        """The planner's Tier A transport profile, or ``None`` (content mode).

        Same capability ladder as bts/extractor: resolved per call from the
        bound agent's connection, and the preset's extra field list rides the
        tool's schema so a JSON-schema-capable model returns the plan directly.
        """
        from fabulist.connections.capabilities import resolve_capabilities
        from fabulist.connections.taxonomy import resolve_interaction_agent
        from fabulist.graph.planner_schema import build_planner_request_profile
        from fabulist.prompts.agent_sets import bindings_for

        agent = narrator_agent
        try:
            agent = resolve_interaction_agent(
                bindings_for(log), registry.agents, "planner", narrator_agent
            )
        except Exception as exc:  # noqa: BLE001 - preset lookup must never break calls
            logger.warning("planner binding resolution fell back to %r", agent, exc_info=True)
            _preset_degraded(
                log,
                _format_session_for_row(),
                "planner_binding",
                exc,
                "the planner request profile fell back to its default agent — "
                "check the preset bindings",
            )
        try:
            return build_planner_request_profile(
                resolve_capabilities(registry.connection_for(agent)),
                _planner_config().get("fields") or [],
            )
        except Exception as exc:  # noqa: BLE001 - unknown connection degrades to notation
            logger.warning(
                "planner request profile read failed; using content mode",
                exc_info=True,
            )
            _preset_degraded(
                log,
                _format_session_for_row(),
                "planner_connection",
                exc,
                "the planner fell back to content mode — check the planner "
                "connection",
            )
            return None

    def _planner_loop_return_thinking() -> bool | None:
        """The planner connection's reasoning echo (``None`` = AUTO).

        The seed phase's tool loop repeats a thinking model's own
        ``reasoning_content`` back by this setting; the planner resolves its
        connection under its own interaction slot, so it gets its own reader.
        """
        return _loop_return_thinking("planner", narrator_agent)

    # D41/D49: the post-turn critic runs in-graph at the tail of the turn. Like
    # bts/attribution it compiles through its own D18 module flow (``critic``) and
    # resolves its agent per call from the active preset's agent_bindings
    # (fallback ``extractor``, D49's cheap extractor-class default).
    def _critic_flow_messages(evidence, session_id: str = ""):
        # D25 capture correctness: post-turn hooks inherit the pump's
        # CURRENT_INTERACTION ("narrate") — relabel so critic captures/usage
        # attribute truthfully.
        from fabulist.agents.debug import CURRENT_INTERACTION

        CURRENT_INTERACTION.set("critic")
        from fabulist.prompts.critic import CRITIC_SYSTEM_PROMPT
        from fabulist.prompts.preset_store import active_preset, compile_preset_flow

        try:
            preset = active_preset(log)
        except Exception as exc:  # noqa: BLE001
            logger.warning("critic preset read failed; using code defaults", exc_info=True)
            _preset_degraded(
                log,
                session_id,
                "critic",
                exc,
                "the critic used the code default prompt — check the preset store",
            )
            preset = None
        return compile_preset_flow(
            preset,
            "critic",
            {
                "system": CRITIC_SYSTEM_PROMPT,
                "payload": _render_critic_payload(evidence),
                "variables": {},
            },
        )

    critic_caller = PresetAgentCaller("critic", "extractor")

    # W9.22 §3: the asset-proposal lanes compile through their own D18 module flow
    # (``proposals``), ONE call per data type, and that flow is the stage's prompt
    # surface — its step editor row, its agent binding and the generated agent
    # map all resolve through it. The builder is handed to the fan-out below, so
    # an owner edit to the ``proposals`` flow applies to the next turn with no
    # rebuild.
    def _proposals_flow_messages(
        payload: Mapping[str, str], session_id: str = ""
    ) -> list[dict]:
        """Compile the ``proposals`` flow for ONE data type's lane (W9.22 §3).

        ``payload`` carries the two structural block bodies the flow splices
        (``system`` = the lane brief for that data type, ``payload`` = the
        turn-touched entities + type digest + the turn's prose), exactly the
        ``{system, payload, variables}`` contract ``compile_preset_flow`` takes
        for bts/critic.
        """
        # D25 capture correctness: a post-turn hook inherits the pump's
        # CURRENT_INTERACTION ("narrate") — relabel so a proposal capture/usage
        # row attributes truthfully.
        from fabulist.agents.debug import CURRENT_INTERACTION

        CURRENT_INTERACTION.set("proposals")
        from fabulist.prompts.preset_store import active_preset, compile_preset_flow

        try:
            preset = active_preset(log)
        except Exception as exc:  # noqa: BLE001
            logger.warning("proposals preset read failed; using code defaults", exc_info=True)
            _preset_degraded(
                log,
                session_id,
                "proposals",
                exc,
                "the proposal lanes used the code default prompt — check the "
                "preset store",
            )
            preset = None
        return compile_preset_flow(
            preset,
            "proposals",
            {
                "system": str(payload.get("system") or ""),
                "payload": str(payload.get("payload") or ""),
                "variables": {},
            },
        )

    # W9.13/W9.25: the NPC fan-out's lane binding. ``npc`` is its own interaction
    # (D19) so the owner can point lanes at a different connection than the
    # narrator with no code change. Since W9.25 its default is the NARRATION task:
    # lanes are narrative-voice work, so they ride the narrator's connection on
    # any legacy config. Lane cost is the dispatch policy's knob (W9.24-W5:
    # strategy + max_dispatch), NEVER the model class. ``extractor`` survives only
    # as the last-resort fallback when no slot resolves.
    npc_caller = PresetAgentCaller("npc", "extractor")

    # W9.22 §3: the proposal stage's GENERATOR binding (doctrine 4 — authoring is
    # extractor-class generation, the JUDGMENTS are the decision model's). It is
    # its own interaction for the same D19 reason as the NPC lanes: the owner can
    # point the three lanes at a cheaper connection with no code change. At most
    # one call per data type per turn, and only behind the double gate.
    proposals_caller = PresetAgentCaller("proposals", "extractor")

    def _proposal_lanes_fanout():
        """Build the proposal lane system: three decision-gated lanes (§3.6.1).

        The SAME object the turn used to run in-graph (W9.22 §3). Since W9.23
        it is driven OFF-TURN by :mod:`fabulist.state.historian`, so it no
        longer enters ``build_graph`` — but its three seams are still owned
        here: the ``proposals`` generator binding above, the ONE shared decision
        caller (every judgment — doctrine 4 — so the historian inherits
        ``_decision_observer``'s usage rows and never builds a second client),
        and the preset ``proposals`` flow (the owner's prompt door). Everything
        else — the touched-entity evidence, the per-type digests, the gate
        questions and the row shape — is log-native and lives in
        :mod:`fabulist.state.proposal_lanes`, so there is nothing else for the
        wiring to hold.
        """
        from fabulist.graph.nodes import make_asset_proposals_node

        return make_asset_proposals_node(
            log=log,
            caller=proposals_caller,
            decision=decision,
            lane_messages=_proposals_flow_messages,
        )

    def _npc_flow_messages(state, context):
        """Render ONE lane's private tail through the preset's ``npc`` flow.

        The shared prefix is NOT rendered here — the node calls the narrator's
        own builder once and appends this tail to it (D2/D3).
        """
        from fabulist.agents.debug import CURRENT_INTERACTION
        from fabulist.graph.nodes import (
            default_npc_lane_messages,
            render_private_block,
        )
        from fabulist.prompts.npc import build_lane_ask, build_lane_system
        from fabulist.prompts.preset_store import active_preset, compile_preset_flow

        # D25 capture correctness: each lane runs as its own task, so setting the
        # contextvar here labels that lane's capture/usage truthfully.
        CURRENT_INTERACTION.set("npc")
        try:
            preset = active_preset(log)
        except Exception as exc:  # noqa: BLE001 - absent store = defaults
            logger.warning("npc preset read failed; using code defaults", exc_info=True)
            _preset_degraded(
                log,
                str(state.get("session_id") or ""),
                "npc",
                exc,
                "the NPC lane used the code default prompt — check the preset store",
            )
            preset = None
        name = str(context.get("name") or "")
        try:
            flow = compile_preset_flow(
                preset,
                "npc",
                {
                    "system": build_lane_system(name, render_private_block(context)),
                    "payload": build_lane_ask(name),
                    "variables": {"name": name},
                },
            )
        except Exception:  # noqa: BLE001 - a broken flow never kills a lane
            logger.warning("npc lane flow fell back to the code default", exc_info=True)
            flow = []
        return flow or default_npc_lane_messages(context)

    def _template_behaviours() -> dict[str, str]:
        """``{template id: implements}`` for the node-template library (W9.10).

        The graph layer resolves a topology node carrying ``"template"``
        through this map; both the compile and the engine's validated read take
        it, so the editor's template graphs build instead of falling back to
        canonical. The store import is lazy (``prompts`` is upstream of the
        engine wiring and the store ensures its seeds on the read).
        """
        from fabulist.graph.node_templates import template_behaviours

        return template_behaviours(log)

    def _npc_lanes_node(topology: dict | None):
        """Build the ``npc_lanes`` node against the turn's own lane data.

        ``max_lanes`` bounds CONCURRENCY only. W9.10: a from-scratch graph may
        place the fan-out under ANY node id through a template, so the row is
        found by BEHAVIOUR (the node whose id is ``npc_lanes`` or whose template
        implements it), and the template's ``config`` overrides the row's
        ``data`` — a template says "this behaviour, configured like this".
        No request profile is passed: forced ``tool_choice`` is an open defect
        (R29), and the lane's JSON contract is content-mode (the Tier D floor) —
        ``structured_result`` handles fenced, bare and prose-wrapped JSON alike.
        """
        from fabulist.graph.node_templates import lane_config, list_node_templates

        library = {tpl.template_id: tpl for tpl in list_node_templates(log)}
        data = lane_config(topology, library)
        return make_npc_lanes_node(
            log=log,
            build_messages=build_messages,
            caller=npc_caller,
            config=data,
            lane_messages=_npc_flow_messages,
            decision=decision,
            provider_label_fn=_provider_label(registry, log, "npc", "extractor"),
            # Issue #79: each lane resolves its OWN connection's vision and
            # re-renders the shared history for it, so a text-only lane never
            # inherits the narrator's inline image parts.
            lane_vision_fn=interaction_vision(registry, log, "npc", "extractor"),
        )

    async def critic(evidence, session_id: str = ""):
        """Call the ``critic`` flow, returning its raw reply text (D23 caller)."""
        from fabulist.graph.types import collect_text

        return await collect_text(
            critic_caller, _critic_flow_messages(evidence, session_id)
        )

    # The SAME builder the narrator node uses for the turn (one instance). The
    # W9.24 repair loop rides that same message list: its iterations only ever
    # APPEND (assistant draft + tool messages), so the stable prefix is untouched
    # and the prompt cache keeps working across iterations (AGENTS 6).
    build_messages = message_builder or make_narrator_message_builder(
        log,
        window_budget_fn=narrate_window_budget(registry, log),
        vision_fn=narrate_vision(registry, log),
    )

    def _narrate_loop_capabilities() -> dict[str, bool] | None:
        """The narrate connection's capability ladder (W9.24 §3.3.2).

        Resolved per call from the interaction's bound agent, exactly like the
        BTS/extractor profiles: a re-bound preset agent or a hot-reloaded
        connection is honoured, and ``None`` (unknown connection) means the
        repair loop never engages — the streamed draft is kept as today.
        """
        from fabulist.connections.capabilities import resolve_capabilities
        from fabulist.connections.taxonomy import resolve_interaction_agent
        from fabulist.prompts.agent_sets import bindings_for

        agent = narrator_agent
        try:
            agent = resolve_interaction_agent(
                bindings_for(log), registry.agents, "narrate", narrator_agent
            )
        except Exception as exc:  # noqa: BLE001 - preset lookup must never break calls
            logger.warning("narrate binding resolution fell back to %r", agent, exc_info=True)
            _preset_degraded(
                log,
                _format_session_for_row(),
                "narrate_binding",
                exc,
                "the repair loop fell back to its default agent — check the "
                "preset bindings",
            )
        try:
            return resolve_capabilities(registry.connection_for(agent))
        except Exception as exc:  # noqa: BLE001 - unknown connection degrades to keep + flags
            logger.warning(
                "narrate capability read failed; the repair loop stays off",
                exc_info=True,
            )
            _preset_degraded(
                log,
                _format_session_for_row(),
                "narrate_connection",
                exc,
                "the repair loop did not engage — check the narrate connection",
            )
            return None

    def _narrate_loop_timeout() -> float | None:
        """The transport bound each repair iteration rides (provenance only).

        Hang protection is PER API CONNECTION (owner ruling): the connection's own
        ``timeout`` is what the transport enforces. The loop records it on its
        ``agent_loop`` rows so an inspector reads "this call rode 900s" without
        opening the connection config; it never enforces a run total.
        """
        from fabulist.connections.taxonomy import resolve_interaction_agent
        from fabulist.prompts.agent_sets import bindings_for

        agent = narrator_agent
        try:
            agent = resolve_interaction_agent(
                bindings_for(log), registry.agents, "narrate", narrator_agent
            )
            return registry.connection_for(agent).timeout
        except Exception as exc:  # noqa: BLE001 - unknown connection keeps the row honest
            logger.warning(
                "narrate timeout binding read failed; no bound recorded", exc_info=True
            )
            _preset_degraded(
                log,
                _format_session_for_row(),
                "narrate_binding",
                exc,
                "the repair loop recorded no transport bound — check the preset "
                "bindings and connections",
            )
            return None

    def _narrate_loop_return_thinking() -> bool | None:
        """The narrate connection's "Return thinking" setting (``None`` = AUTO).

        The repair loop's assistant ``tool_calls`` messages repeat the thinking
        model's own ``reasoning_content`` by this setting: AUTO (the default)
        echoes it whenever DeepSeek (or any provider) returned some, which is
        what its thinking mode requires after a tool call; ``false`` never sends
        the field. Resolved per call, so a hot-reloaded connection applies.
        """
        return _loop_return_thinking("narrate", narrator_agent)

    def _extractor_request_profile() -> dict | None:
        """W8.x: the extractor's Tier A transport profile, or ``None`` (notation).

        Same discipline as :func:`_bts_request_profile`: resolved per call from
        the bound ``extractor`` agent's connection capabilities, so a re-bound
        preset agent or a hot-reloaded connection is honoured. ``None`` keeps
        today's notation request byte-for-byte (Tier D floor, D69).
        """
        from fabulist.connections.capabilities import resolve_capabilities
        from fabulist.state.extractor import build_extractor_request_profile

        try:
            return build_extractor_request_profile(
                resolve_capabilities(registry.connection_for("extractor"))
            )
        except Exception as exc:  # noqa: BLE001 - unknown connection degrades to notation
            logger.warning(
                "extractor request profile read failed; using notation", exc_info=True
            )
            _preset_degraded(
                log,
                _format_session_for_row(),
                "extractor_connection",
                exc,
                "the extractor fell back to the notation path — check the "
                "extractor connection",
            )
            return None

    # W9.5 (R10): compile from the PRESET's topology (validated; falls back to
    # canonical when the stored one is broken). ``graph_source`` lets a topology
    # Save take effect on the next turn without a restart.
    def _build(topology: dict | None = None):
        return build_graph(
            model=caller,
        extractor=AsyncExtractor(
            FallbackExtractor(
                LLMExtractor(
                    registry=registry,
                    messages_renderer=_extract_flow_messages,
                    log=log,
                    profile_fn=_extractor_request_profile,
                ),
                RuleBasedExtractor(),
                log=log,
            )
        ),
        log=log,
        build_messages=build_messages,
        checkpointer=checkpointer,
        config_snapshot=config_snapshot,
        world_state=world_state,
        attribution=attribution,
        critic=critic,
        # W9.24 §3.3: the narrator's repair loop rides the narrate interaction's
        # own connection — its capability ladder (function calling engages the
        # loop; anything less keeps the draft) and the timeout each iteration's
        # call rides (provenance; the transport enforces it).
        narrate_loop_capabilities=_narrate_loop_capabilities,
        narrate_loop_timeout=_narrate_loop_timeout,
        # W9.25 §3.5.3: the NARRATE slot's available prompt-token budget (the
        # same reader the message builder caps the directive with), so the repair
        # loop's ``agent_loop`` rows carry passive headroom for the connection
        # that actually runs them.
        narrate_loop_window=narrate_window_budget(registry, log),
        # The narrate connection's reasoning echo (None = AUTO): the repair loop
        # must hand a thinking model its own reasoning back with its tool_calls.
        narrate_loop_return_thinking=_narrate_loop_return_thinking,
        # The model a PRE-COMMIT provider abort must name, resolved live for the
        # narrate interaction (a rebind applies on the same turn it applies to
        # the call — ADR-023).
        narrate_provider_label=_provider_label(registry, log, "narrate", narrator_agent),
        # d1-3b decision model (tiered router :4445). One shared caller serves
        # the genre fact and the batched turn gate (slop/movement, advisory).
        # Feature-off when the ``decision`` binding is absent — a missing
        # binding must never break a turn. Speaker attribution no longer uses
        # it: that pass is deterministic code now (state/attribution).
        decision=decision,
        # W9.20: the steering pass. The narrator-class caller ([OWNER] #11),
        # the planner step's directive, the preset's cadence/fields/steer
        # read LIVE (so a Studio edit applies on the next due turn), and the
        # capability-ranked request profile.
        planner=make_planner_node(
            log=log,
            caller=planner_caller,
            # Issue #70: a session-aware reader, not a build-time snapshot, so
            # a failed preset read leaves a ``preset_degraded`` row on the turn.
            directive=_planner_directive,
            planner_config=_planner_config,
            request_profile_fn=_planner_request_profile,
            provider_label_fn=_provider_label(
                registry, log, "planner", narrator_agent
            ),
            return_thinking_fn=_planner_loop_return_thinking,
        ),
        plan_cadence=_planner_plan_cadence,
        topology=topology,
        npc_lanes=_npc_lanes_node(topology),
        # W9.10: a topology node may be a TEMPLATE reference; the builder
        # resolves it to the behaviour the template implements. Seeds first
        # (the read ensures), so a fresh log still resolves its built-ins.
        templates=_template_behaviours(),
    )

    def _compile(topology: dict | None = None):
        """Compile ``topology``, falling back to CANONICAL if it is refused.

        W9.20 §10/N3: the plan junction's compile-upgrade REFUSES a planner
        wired anywhere but the canonical-old or canonical-new shape (a
        per-turn post-commit planner is the opposite of the owner's design). A
        stored topology the upgrade refuses therefore compiles as the canonical
        turn — the same behaviour a broken stored topology already has — instead
        of booting the wrong graph or crashing the engine.
        """
        try:
            return _build(topology)
        except ValueError:
            if topology is None:
                raise
            logger.warning(
                "stored topology refused (see the traceback); compiling the "
                "canonical turn topology instead",
                exc_info=True,
            )
            return _build(None)

    def _graph_source() -> tuple[str, Any]:
        import hashlib
        import json

        from fabulist.prompts.preset_store import active_preset, preset_topology

        topology = preset_topology(active_preset(log), _template_behaviours())
        # W9.10: the LIBRARY is part of the graph's identity. A template edit
        # (implements / config / prompt door) must rebuild the compiled graph at
        # the next turn start, exactly like a topology edit — digesting the
        # topology alone would keep running the old node silently.
        from fabulist.graph.node_templates import library_rows, list_node_templates

        library = library_rows({tpl.template_id: tpl for tpl in list_node_templates(log)})
        # The digest is the STORED topology + the template library — deliberately
        # NOT the compile-time upgrades (they are a pure function of the stored
        # data) and NOT the planner config (cadence/fields/steer only matter when
        # the planner RUNS: a Studio edit applies on the next due turn with no
        # rebuild, W9.20 §10). Do not "fix" this by hashing the planner config.
        digest = hashlib.sha1(
            json.dumps(
                {"topology": topology, "templates": library},
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()
        return digest, _compile(topology)

    # W9.23 §3.6: the proposal lane system's ONE construction site. It no longer
    # enters the graph (the historian drives it between turns), so the seam the
    # app's post-turn hook needs is handed back through ``off_turn``.
    if off_turn is not None:
        off_turn["proposal_lanes"] = _proposal_lanes_fanout()
        # W9.29 §12: the same runner the turn path drives, handed to the app so
        # the manual "check state now" door (POST .../reconcile) runs the pass
        # off-turn instead of rebuilding a second BTS wiring beside it.
        off_turn["world_state"] = world_state

    graph = _compile()
    return GraphTurnEngine(
        graph=graph,
        log=log,
        model=caller,
        ooc_messages=make_ooc_message_builder(
            log, vision_fn=interaction_vision(registry, log, "ooc", "narrator")
        ),
        recall_fn=recall_fn,
        graph_source=_graph_source,
    )


def _scope_read_layers(scope: Any) -> tuple[str, ...]:
    """Label each of ``scope.read_collections`` with its D53 layer (for the RAG panel).

    The scope's read set is the chat's OWN layer followed by the nested actor
    layers, so its ``layer`` labels the first collection and every nested
    collection is an ``actor`` layer. Returned alongside the collections on the
    :class:`~fabulist.memory.recall.RecallResult` so the inspector can show
    WHICH layers were read, not just the ids.
    """
    labels = [scope.layer]
    labels.extend(["actor"] * (len(scope.read_collections) - 1))
    return tuple(labels)


def make_memorizer(registry: Registry, log_path: Path) -> "Memorizer | None":
    """Vector-memory wiring (W3.1/W3.2) — None when the registry has no
    ``embedder`` entry (memory off, turns proceed without recall).

    D25 LIVE resolution: the embedder/reranker endpoint+model+key are resolved
    from the (possibly hot-reloadable) registry on EVERY call — never snapshotted
    at boot — so a Connections rebind applies to the next call with no restart.
    The ``api_key`` is read from ``os.environ`` at call time (a configured
    ``api_key_env`` used to be silently ignored). The ``MemoryStore`` is built
    once (it IS the index); a wipe re-initializes it at the new embedder's dims
    on the next write.
    """
    from fabulist.memory.reembed import resolve_embedder, resolve_reranker

    try:
        registry.connection_for("embedder")  # feature gate only; not retained
    except Exception:  # noqa: BLE001 - missing entry = feature disabled
        return None
    from fabulist.memory.recall import (
        gather_candidates,
        recall_block,
        scene_query_from_log,
        visible_turn_seqs,
    )
    from fabulist.memory.scope import resolve_scope
    from fabulist.memory.store import MemoryStore

    log = EventLog(log_path)
    # W1f: the embed transport is a direct httpx caller outside the registry
    # chokepoint, so its usage rows need this log handle explicitly. Bound here
    # (the memory subsystem's owner) and never fatal: with no embedder binding
    # this function already returned above.
    from fabulist.memory.embedder import bind_usage_log

    bind_usage_log(log)
    store = MemoryStore(log_path.parent / "memory.db")

    class Memorizer:
        def __init__(self) -> None:
            # The store IS the index; the memory API (reset/reembed) reaches it
            # through here. The registry is held (not a snapshot) so every call
            # resolves the LIVE binding.
            self.store = store
            self.registry = registry

        async def recall_fn(
            self, session_id: str, user_text: str
        ) -> "RecallResult | str | None":
            """Pre-turn recall (D25): RecallResult carries block + the exact
            queries + the resolved config for the inspector RAG panel.

            D53 layers: recall queries the chat's RESOLVED read set
            (``scope.read_collections``) — its own layer plus the layers nested
            inside it (a world chat reads ``w_<world>`` plus each actor's
            ``a_<actor>``; an actor/chat chat reads only its own). The read set
            is labelled with its layers on the result so the inspector can show
            WHICH layers were read. ``exclude_session_id`` keeps any seq
            exclusion scoped to this chat when a shared layer carries many
            chats. Opted-out chats (``chat_memory`` off) recall NOTHING.

            Issue #84: the exclusion SET is this chat's visible-history seqs —
            the trailing narrative turns the narrator already carries in its
            history prompt (:func:`fabulist.memory.recall.visible_turn_seqs`,
            the ``[-2:]`` narrative window). Recall stops returning prose the
            model can already see, freeing the block's slots for real memory;
            older turns stay recallable (it is the tail, not the history).

            Never raises: recall is an enhancement, not a dependency.
            """
            try:
                from fabulist.api.chat_config_api import resolve_recall_config

                scope = resolve_scope(log, session_id)
                if not scope.enabled:
                    return None
                embedder = resolve_embedder(self.registry)
                reranker = resolve_reranker(
                    self.registry, fallback_endpoint=embedder.endpoint
                )
                cfg = resolve_recall_config(log, session_id)
                scene_query = (
                    scene_query_from_log(log, session_id)
                    if cfg.query_mode == "message+scene"
                    else None
                )
                # Issue #84: wire the per-chat trim the store already supports.
                # The seqs are THIS chat's recent-turn tail (the prose already
                # visible in the narrator's history); the session id scopes them
                # so a shared layer's other chats keep their colliding seqs.
                visible_seqs = visible_turn_seqs(log.get_projection(session_id))
                return await recall_block(
                    store,
                    embedder.endpoint,
                    embedder.model,
                    scope.read_collections,
                    user_text,
                    config=cfg,
                    scene_query=scene_query,
                    rerank_endpoint=reranker.endpoint,
                    rerank_model=reranker.model,
                    exclude_seqs=visible_seqs,
                    exclude_session_id=session_id,
                    layers=_scope_read_layers(scope),
                    # The log, explicitly: the recalled fragments are tagged by
                    # the layer that holds them (D53), which needs world/persona
                    # display names. Passing it is the difference between a
                    # resolved name and a filename guess inside the recall path.
                    log=log,
                    api_key=embedder.api_key,
                    rerank_api_key=reranker.api_key,
                )
            except Exception as exc:  # noqa: BLE001
                # Issue #34: recall is best-effort, but its failure is NOT silent.
                # The turn continues without memories (the swallow stays), and one
                # ``recall_degraded`` row makes that visible in the Inspector
                # instead of a stderr line the reader never sees.
                logger.warning("recall failed for %s", session_id, exc_info=True)
                append_degradation(
                    log,
                    session_id,
                    RECALL_DEGRADED_EVENT,
                    stage=STAGES[RECALL_DEGRADED_EVENT],
                    reason=degradation_reason(
                        exc,
                        "the turn ran without memories — check the embedding "
                        "connection",
                    ),
                )
                return None

        async def search(self, session_id: str, query: str, k: int = 8) -> list[dict]:
            """User-facing semantic search over the chat's RESOLVED read set.

            Searches ``scope.read_collections`` (own layer + nested layers), so
            a world chat finds scenes from any chat in the world (and its
            actors') while an actor/chat chat searches only its own layer.
            ``exclude_session_id`` scopes any seq exclusion to this chat when a
            shared layer carries many chats. Opted-out chats return nothing.
            Never raises: an embedder failure/down returns ``[]`` (empty
            results) rather than bubbling an unhandled 500 through the router.
            """
            scope = resolve_scope(log, session_id)
            if not scope.enabled:
                return []
            try:
                embedder = resolve_embedder(self.registry)
                return await gather_candidates(
                    store,
                    embedder.endpoint,
                    embedder.model,
                    scope.read_collections,
                    [query],
                    fetch_k=k,
                    exclude_session_id=session_id,
                    api_key=embedder.api_key,
                )
            except Exception:  # noqa: BLE001 - search degrades to no results
                logger.warning("search failed for %s", session_id, exc_info=True)
                return []

        async def index_tail(self, session_id: str) -> None:
            """Index the newest turn (last user + narrative + fresh assets).

            Upserts are seq-keyed, so re-indexing a turn is idempotent. The
            embedder is resolved LIVE (a rebind mid-session applies here too).
            """
            from fabulist.memory.indexer import index_media, index_turn

            scope = resolve_scope(log, session_id)
            if not scope.enabled:
                return  # D53 opt-out: this chat never embeds
            # D53 write rule: the chat's OWN layer collection, exactly ONE write.
            collection = scope.write_collection
            embedder = resolve_embedder(self.registry)
            events = log.get_events(session_id)
            narratives = [e for e in events if e.type == "narrative"]
            if not narratives:
                return
            last = narratives[-1]
            prev_seq = narratives[-2].seq if len(narratives) >= 2 else 0
            for event in events:
                if event.seq <= prev_seq or event.seq > last.seq:
                    continue
                if event.type == "user" and event.payload.get("text"):
                    await index_turn(
                        store, embedder.endpoint, embedder.model, collection,
                        event.seq, "user", str(event.payload["text"]),
                        session_id=session_id, api_key=embedder.api_key,
                        # D55: the writer stamp is resolved from the log, so the
                        # LIVE path must pass it too — otherwise every row this
                        # chat writes carries no writer until a rebuild.
                        log=log,
                    )
                elif event.type == "narrative" and event.payload.get("prose"):
                    await index_turn(
                        store, embedder.endpoint, embedder.model, collection,
                        event.seq, "narrative", str(event.payload["prose"]),
                        session_id=session_id, api_key=embedder.api_key,
                    )
                elif event.type == "asset":
                    payload = event.payload
                    if payload.get("bytes_b64"):
                        await index_media(
                            store, embedder.endpoint, embedder.model, collection,
                            event.seq, str(payload.get("kind", "image")),
                            str(payload.get("mime", "")),
                            str(payload["bytes_b64"]),
                            session_id=session_id, api_key=embedder.api_key,
                        )

    return Memorizer()


def _turn_busy(sessions: Any, session_id: str) -> bool:
    """Whether a turn is in flight for ``session_id`` (the turn-slot predicate).

    ``api/app.py`` owns the per-session slot (``_Session.turn`` / ``_Turn.done``);
    W9.26 §2.3 step 1's forced path must wait for it to CLEAR before claiming the
    session, and this adapter is the read it polls. A missing/foreign session is
    "clear" — there is no turn to wait for.
    """
    session = sessions.get(session_id) if isinstance(sessions, Mapping) else None
    turn = getattr(session, "turn", None)
    return turn is not None and not turn.done.is_set()


def build_app(
    *,
    registry: Registry,
    log_path: Path,
    checkpointer: Any | None = None,
    agent_debug: bool = False,
) -> Any:
    """Build the FastAPI app the ONE way (serve.py and tests share this path).

    ``checkpointer``: an ALREADY-OPENED sqlite checkpointer (serve owns the
    async context); None builds the engine without one.
    The W2.9 ``enable_compaction`` flag is RETIRED (W9.26 §2.8): the threshold
    auto-hook is gone — compaction is the owner-driven ``POST /compact`` plus
    the measured 90% force, both governed by the context monitor.
    ``agent_debug``: default False (D17/W5-S2). It is the DEFAULT for sessions
    that never wrote the per-session toggle (``chat_agent_debug``, flipped in
    the UI); with the flag on, sessions start capturing and may still turn it
    off, and vice versa. When capture is on, every capturable agent call
    (narrator/OOC, summarizer, captioner, assistant) records its full context
    snapshot + response (+ reasoning) as a system event on the session — not
    always on (cost/size guard: MAX_CAPTURE_CHARS truncation in agents.debug);
    the API surface (api/agent_debug_api) reads whatever captures exist either
    way.
    """
    log = EventLog(log_path)
    # Issue #36: auto-pin the auxiliary sidecar roles (embedder/reranker/decision)
    # when FABULIST_SIDECAR_URL is set. It runs BEFORE the memorizer is built so
    # a fresh compose deploy has memory on its very first boot, fills EMPTY slots
    # only, writes through the ordinary connection store (validated like a
    # hand-made entry) and can never raise into boot — a partial or unreachable
    # sidecar is one notice and the engine carries on. The result is exposed on
    # ``app.state.sidecar_pin`` below for diagnostics.
    from fabulist.sidecar import autopin_sidecar, schedule_autopin_retry

    sidecar_pin = autopin_sidecar(registry)
    # D26 (W7-A): seed the persisted default preset ONLY when missing. Never
    # overwrites owner edits; a factory-version bump surfaces a notice instead.
    from fabulist.prompts.preset_store import ensure_default_preset

    ensure_default_preset(log)
    memorizer = make_memorizer(registry, log_path)
    # ONE builder instance: the engine compiles through it AND the preview
    # endpoint reads it off app.state — preview parity by construction (D18).
    # Issue #79: the builder resolves the NARRATE connection's live vision
    # capability per build, so preview and the live turn render identically.
    message_builder = make_narrator_message_builder(
        log,
        window_budget_fn=narrate_window_budget(registry, log),
        vision_fn=narrate_vision(registry, log),
    )
    # The off-turn seams the hooks below need (the historian's lane system); the
    # engine builds them because it owns their model bindings.
    off_turn: dict[str, Any] = {}
    engine = build_engine(
        registry=registry,
        log=log,
        checkpointer=checkpointer,
        recall_fn=memorizer.recall_fn if memorizer else None,
        agent_debug=agent_debug,
        message_builder=message_builder,
        off_turn=off_turn,
    )
    # --- W9.26 §2.3: the compact service and its one summarizer caller -------
    # The summarize slot is resolved LIVE per call (the same "preset bindings win"
    # doctrine the narrator's caller uses, D18), because the Inspector's per-task
    # ledger resolves it the same way (`api/context_api.LEDGER_TASKS`) — the row
    # the owner reads and the call that actually runs must name one connection.
    # The caller is built through `make_agent_caller` with `emit_finish=True`: the
    # SUMMARIZER ONLY carries the terminal `finish` frame (§2.3 step 5 / D86), so
    # the narrator/OOC turn wire is byte-stable.
    _summarizer_callers: dict[str, Any] = {}

    class _SummarizeCaller:
        """Async-iterator ModelCaller seam over the live `summarize` slot."""

        def _caller(self) -> Any:
            agent = _interaction_slot(registry, log, "summarize", "summarizer")
            self.agent = agent
            caller = _summarizer_callers.get(agent)
            if caller is None:
                caller = make_agent_caller(
                    agent,
                    registry,
                    log,
                    agent_debug_default=agent_debug,
                    emit_finish=True,
                )
                _summarizer_callers[agent] = caller
            return caller

        async def __call__(
            self,
            messages: list[dict],
            stream: bool = True,
            request_profile: dict | None = None,
        ) -> AsyncIterator[dict]:
            kwargs = {} if request_profile is None else {"request_profile": request_profile}
            async for event in self._caller()(messages, stream, **kwargs):
                yield event

    summarizer = _SummarizeCaller()

    def _summarize_flow_messages(transcript: str, session_id: str = "") -> list[dict]:
        """The summarize interaction's module flow (preset-customized or default).

        D18 completion: Studio preview and the real call render through the same
        `compile_preset_flow`, so what the owner previews is what compaction
        sends (accepted drift: a preset edit applies to the next run).
        """
        from fabulist.graph.compaction import build_summarizer_messages
        from fabulist.prompts.preset_store import active_preset, compile_preset_flow

        try:
            preset = active_preset(log)
        except Exception as exc:  # noqa: BLE001
            logger.warning("summarize preset read failed; using code defaults", exc_info=True)
            # The compaction service is shared across sessions and does not
            # hand its session to the builder; it keeps CURRENT_SESSION installed
            # for the run, so resolve the row's target from there.
            _preset_degraded(
                log,
                session_id or _format_session_for_row(),
                "summarize",
                exc,
                "compaction used the code default prompt — check the preset store",
            )
            preset = None
        base = build_summarizer_messages(transcript)
        return compile_preset_flow(
            preset,
            "summarize",
            {
                "system": str(base[0].get("content", "")),
                "payload": str(base[1].get("content", "")) if len(base) > 1 else "",
                "variables": {},
            },
        )

    # ONE service instance: the forced path (the monitor hook) and the button
    # path (`api/context_api`) compact through the same object, onto the same
    # `app.state` seams below.
    compact_service = CompactService(
        summarizer=summarizer,
        budget_tokens=summarizer_budget_tokens(registry, log),
        model=_summarizer_model(registry, log),
        messages_builder=_summarize_flow_messages,
    )

    async def memory_hook(session_id: str) -> None:
        """Post-turn vector indexing of the newest turn (fire-and-forget).

        Owner ruling (2026-10-14): memory is NOT gated by the decision model —
        every turn is persisted. The d1-3b worthiness question was removed.
        """
        if memorizer:
            await memorizer.index_tail(session_id)

    # D38: the Assistant panel's per-turn consult (fire-and-forget, never
    # fatal; disabled enablement returns silently inside the hook). Built from
    # the same flow + factory the consult route uses, so panel and manual
    # consult are byte-identical paths.
    def _assistant_flow_messages(
        definition: Any, context: Any, session_id: str = ""
    ) -> list[dict]:
        from fabulist.prompts.preset_store import active_preset, compile_preset_flow
        from fabulist.assistants.manager import build_assistant_messages

        try:
            preset = active_preset(log)
        except Exception as exc:  # noqa: BLE001
            logger.warning("assistant preset read failed; using code defaults", exc_info=True)
            _preset_degraded(
                log,
                session_id,
                "assistant",
                exc,
                "the assistant used the code default prompt — check the preset store",
            )
            preset = None
        base = build_assistant_messages(definition, context)
        return compile_preset_flow(
            preset,
            "assistant",
            {
                "system": str(base[0].get("content", "")),
                "history": [],
                "user_content": str(base[1].get("content", "")) if len(base) > 1 else "",
                "variables": {},
            },
        )

    def _assistant_model_caller_factory(agent: str):
        # W5-S2 chokepoint parity: every assistant caller is built through
        # make_agent_caller (captured like the narrator).
        return make_agent_caller(agent, registry, log, agent_debug_default=agent_debug)

    from fabulist.assistants.auto import make_assistant_turn_hook

    assistant_hook = make_assistant_turn_hook(
        log, _assistant_model_caller_factory, flow=_assistant_flow_messages
    )

    # W9.23 §3.6: the historian. It drives the proposal lane system off-turn on
    # the preset's cadence (read LIVE, so a Studio edit applies on the next
    # evaluation with no rebuild) or on the owner's run-now door. The runner is
    # built ONCE and shared: the post-turn hook below and the API's status/run
    # routes must agree about what is in flight.
    def _historian_cadence(session_id: str = "") -> int:
        """Committed turns between automatic historian runs (the preset's)."""
        from fabulist.prompts.preset_store import active_preset, historian_config

        try:
            preset = active_preset(log)
        except Exception as exc:  # noqa: BLE001 - an unreadable store = the default
            logger.warning("historian cadence read failed; using the default", exc_info=True)
            _preset_degraded(
                log,
                session_id,
                "historian_cadence",
                exc,
                "the historian used its default cadence — check the preset store",
            )
            preset = None
        return historian_config(preset)["cadence"]

    historian = make_historian_runner(
        log=log, fanout=off_turn["proposal_lanes"], cadence=_historian_cadence
    )

    async def historian_hook(session_id: str) -> None:
        """Post-turn: run the historian when the cadence says it is due.

        Two cheap chain reads when it is not (the zero-cost contract for an
        ordinary turn). The in-flight check is the double-spawn guard: a manual
        run already working on this session wins. A run that MISSED its window
        advanced no clock, so this hook is what retries it — every turn until a
        run completes (historian §3.6.3).
        """
        if historian.inflight(session_id):
            return
        if historian.is_due(session_id):
            await historian.run(session_id, trigger="cadence")

    # --- W9.26 §2.3 step 1: the claim registry, the job registry and the turn
    # slot. All THREE are built here (the design's binding contract) and consumed
    # by the /turns guards (this file via `api/app.py`), the monitor hook and
    # Lane D's router — never constructed by a consumer.
    context_jobs = ContextJobs()
    compaction_claims = CompactionClaims()
    # The turn-slot view reads `app.state.sessions` (owned by `create_app`), so the
    # predicate is late-bound through a holder filled the moment the app exists.
    # The slot is only consulted once a turn is running, i.e. long after.
    sessions_holder: dict[str, Any] = {}
    turn_slot = PollingTurnSlot(
        lambda session_id: _turn_busy(sessions_holder.get("sessions"), session_id)
    )
    context_monitor_hook = make_context_monitor_hook(
        log=log,
        registry=registry,
        service=compact_service,
        jobs=context_jobs,
        claims=compaction_claims,
        slot=turn_slot,
    )

    app = create_app(
        engine,
        log_path,
        context_monitor_hook=context_monitor_hook,
        memory_hook=memory_hook,
        assistant_hook=assistant_hook,
        historian_hook=historian_hook,
    )
    sessions_holder["sessions"] = app.state.sessions
    # W9.26: the four `app.state` seams the monitor/router/turn-guards share.
    app.state.compact_service = compact_service
    app.state.context_jobs = context_jobs
    app.state.compaction_claims = compaction_claims
    app.state.turn_slot = turn_slot
    # The historian's control plane (W9.23 §3.6.7) reads the runner off the app:
    # the status route asks it what is in flight, the run door runs it.
    app.state.historian = historian
    # W9.29 §12: the BTS runner, for the manual reconciliation door — the SAME
    # instance the turn path uses (None when BTS is disabled: the door then
    # answers its soft failure instead of inventing a pass).
    app.state.world_state = off_turn.get("world_state")
    # Agent seams for the routers (attachments/assistants/search read these):
    app.state.registry = registry
    # D18 preview parity: the SAME builder the engine compiles through —
    # /api/prompts/preview renders through it so preview == reality.
    app.state.message_builder = message_builder

    # D18 completion: caption + assistant flow renderers (the routers read
    # them off app.state; None keeps the fixed default shapes).
    def _caption_flow_messages(asset: dict, session_id: str = "") -> list[dict]:
        from fabulist.media.images import build_caption_messages
        from fabulist.prompts.preset_store import active_preset, compile_preset_flow

        try:
            preset = active_preset(log)
        except Exception as exc:  # noqa: BLE001
            logger.warning("caption preset read failed; using code defaults", exc_info=True)
            _preset_degraded(
                log,
                session_id,
                "caption",
                exc,
                "the captioner used the code default prompt — check the preset store",
            )
            preset = None
        base = build_caption_messages(asset)
        return compile_preset_flow(
            preset,
            "caption",
            {
                "system": str(base[0].get("content", "")),
                "payload": base[1].get("content", "") if len(base) > 1 else "",
                "variables": {},
            },
        )

    app.state.caption_flow = _caption_flow_messages
    app.state.assistants_flow = _assistant_flow_messages
    # W5-S2 chokepoint: the captioner and every per-definition assistant caller
    # are built through make_agent_caller, so they are captured like the
    # narrator. assistants_api prefers an injected factory over its registry
    # default, so no router edit is needed for the plumbing. The factory is
    # shared with the D38 per-turn assistant hook (same path both ways).
    app.state.captioner = make_agent_caller(
        "captioner", registry, log, agent_debug_default=agent_debug
    )
    # COMFY-PLAN step 2: the image generator agent. Same chokepoint as the
    # captioner (RegistryModelCaller + DebugCapture), so the render call records
    # its usage row and is visible to the inspector. It is constructed
    # unconditionally like the other callers — the ``imagegen`` binding lives in
    # the user's connections.json (resolved only when called), so a missing
    # binding degrades to the render path's row-level error, never a boot failure.
    app.state.imagegen_caller = make_agent_caller(
        "imagegen", registry, log, agent_debug_default=agent_debug
    )
    app.state.assistant_model_caller_factory = _assistant_model_caller_factory
    # The default a session inherits when it never wrote a chat_agent_debug row
    # (GET /agent-debug resolves it); the capture sink reads it too.
    app.state.agent_debug_default = agent_debug
    app.state.memorizer = memorizer
    # Issue #36: what the boot-time sidecar auto-pin did (pinned/skipped/notice).
    # A read-only diagnostic; the connections/store snapshot above is the truth.
    app.state.sidecar_pin = sidecar_pin
    # Issue #36 (compose boot race): when the boot probe was UNREACHABLE (the
    # sidecar's DNS/service name was not ready, or it was still crash-looping its
    # device guard), arm the BOUNDED repeating re-probe. It re-runs the same
    # fill-empty-slots pin every ~25 s, at most 24 times (~10 minutes), stopping
    # at the first probe that ANSWERS — non-blocking (boot never waits), and
    # idempotent, so a sidecar that came up (or a user who pinned meanwhile)
    # simply ends the schedule without writing. A boot probe that ANSWERED, or an
    # unset FABULIST_SIDECAR_URL, arms nothing: single-shot boot is unchanged.
    # The compose file also orders the engine behind the sidecar's healthcheck;
    # this schedule is the second line of defence for a sidecar that restarts
    # later. The task refreshes this diagnostic on every attempt and is dropped
    # by loop shutdown, so it never outlives the app (see fabulist.sidecar for
    # the task discipline).
    def _refresh_sidecar_pin(result: Any) -> None:
        app.state.sidecar_pin = result

    app.state.sidecar_retry = schedule_autopin_retry(
        registry, sidecar_pin, on_result=_refresh_sidecar_pin
    )
    return app
