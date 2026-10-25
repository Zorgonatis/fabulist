#!/usr/bin/env python
"""Live proof: a thinking model's reasoning must ride back inside a tool loop.

The owner-reported failure: DeepSeek's thinking mode answers
``400 — The `reasoning_content` in the thinking mode must be passed back to the
API`` on the SECOND request of a tool loop, because the assistant ``tool_calls``
message the engine appends dropped the reasoning the provider had just returned.
Unit tests with fake transports cannot prove the vendor's rule; this script runs
the ENGINE'S OWN loop (:func:`fabulist.agents.loop.run_agent_loop`) over the
engine's own streaming caller (:class:`fabulist.wiring.RegistryModelCaller`)
against the real endpoint, twice:

* ``before`` — ``return_thinking=False``, which is byte-for-byte the pre-fix
  wire shape (no ``reasoning_content`` on the assistant ``tool_calls`` message).
  This must FAIL with the vendor's 400.
* ``after`` — the default (AUTO), which echoes the provider's own reasoning.
  This must COMPLETE the loop (tool call → tool result → final answer).

PRESENCE is the whole rule (measured, ``deepseek-flash``): a thinking-mode
response ALWAYS carries ``reasoning_content``, and a continuation turn that
reasoned nothing carries it with an EMPTY string (the opening frame says
``"reasoning_content": ""``; later frames omit the key; the closing frame says
``null``). The vendor 400s on the field's ABSENCE and accepts the empty string,
so the engine resends the provider's own value verbatim — never a synthesized
one, and never nothing when the provider said something. The ``after`` probe
prints ``present, empty`` when it catches exactly that turn.

The API key is read from an env FILE into ``os.environ`` and never appears in
argv, in a printed line, or in an error string. Nothing is written to the event
log (``log=None``), so no capture, usage row or fixture ever holds the key.

Usage::

    uv run python scripts/probe_return_thinking.py --env-file /path/to/.env
    uv run python scripts/probe_return_thinking.py --model deepseek-v4-pro

Exit codes: 0 = the expected before/after pair was observed, 1 = the endpoint
errored in an unexpected way, 2 = the probe was inconclusive (the model never
called the tool, so the second request never happened).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import Any

from fabulist.agents.loop import (
    LoopInjection,
    LoopTool,
    _echoed_reasoning,
    _injection_messages,
    run_agent_loop,
)
from fabulist.connections.registry import Registry, load_env_file
from fabulist.wiring import RegistryModelCaller

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-v4-pro"
DEFAULT_API_KEY_ENV = "FABULIST_DEEPSEEK_KEY"
PROBE_AGENT = "return-thinking-probe"

ASK = (
    "Use the record_note tool to record the single word LANTERN. "
    "Then answer with one short sentence confirming it."
)
DRAFT_ASK = "Describe a brass lantern on a dock in one sentence."


class RecordingCaller:
    """Forward every event, remembering each REQUEST's message list.

    The proof is about what the SECOND request carries, so the request bodies are
    the evidence — captured here rather than reconstructed from the loop's
    internals.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.requests: list[list[dict[str, Any]]] = []

    async def __call__(self, messages, stream=True, request_profile=None):
        self.requests.append([dict(message) for message in messages])
        async for event in self._inner(
            messages, stream, request_profile=request_profile
        ):
            yield event

    def assistant_tool_calls_message(self) -> dict[str, Any] | None:
        """The second request's assistant turn that carries ``tool_calls``."""
        if len(self.requests) < 2:
            return None
        for message in self.requests[1]:
            if message.get("role") == "assistant" and message.get("tool_calls"):
                return message
        return None


def note_tool() -> LoopTool:
    """A trivial engine-side tool: the loop needs one the model can actually call."""

    async def fn(args: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "recorded": args.get("note")}

    return LoopTool(
        name="record_note",
        description="Record one short note in the engine's scratchpad.",
        parameters={
            "type": "object",
            "properties": {"note": {"type": "string", "description": "The note."}},
            "required": ["note"],
        },
        fn=fn,
    )


def build_registry(base_url: str, model: str, api_key_env: str) -> Registry:
    """One connection + one binding, built through the real registry parser."""
    return Registry.from_dict(
        {
            "connections": {
                "deepseek": {
                    "base_url": base_url,
                    "model": model,
                    "api_key_env": api_key_env,
                    "timeout": 300,
                    # Room for a thinking pass plus the answer; the provider's
                    # default budget is small enough to truncate the reasoning.
                    "sampling": {"max_tokens": 2048, "temperature": 0.2},
                }
            },
            "agents": {PROBE_AGENT: "deepseek"},
        }
    )


async def probe(registry: Registry, *, return_thinking: bool | None) -> tuple[str, Any]:
    """Run ONE tool loop; return ``("completed"|"failed", detail)``."""
    caller = RecordingCaller(RegistryModelCaller(PROBE_AGENT, registry, log=None))
    try:
        result = await run_agent_loop(
            caller=caller,
            messages=[
                {
                    "role": "system",
                    "content": "You are a terse assistant that obeys tool instructions.",
                },
                {"role": "user", "content": ASK},
            ],
            tools=[note_tool()],
            surface="probe_return_thinking",
            log=None,
            session_id="",
            capabilities={"function_calling": True},
            return_thinking=return_thinking,
        )
    except Exception as exc:  # noqa: BLE001 - the failure IS the evidence
        return "failed", (exc, caller)
    return "completed", (result, caller)


def describe_second_request(caller: RecordingCaller) -> str:
    """What the second request's ``tool_calls`` message carried (keys only)."""
    message = caller.assistant_tool_calls_message()
    if message is None:
        return f"second request: none (the loop made {len(caller.requests)} request(s))"
    reasoning = message.get("reasoning_content")
    keys = sorted(message)
    if reasoning is None:
        return f"second request assistant tool_calls keys: {keys} (no reasoning_content)"
    if reasoning == "":
        return (
            f"second request assistant tool_calls keys: {keys} "
            "(reasoning_content: present, empty — the provider reasoned nothing "
            "this turn and the empty value rides back verbatim)"
        )
    return (
        f"second request assistant tool_calls keys: {keys} "
        f"(reasoning_content: {len(reasoning)} chars)"
    )


async def probe_seeded_pair(
    registry: Registry, *, return_thinking: bool | None
) -> tuple[str, str, dict[str, Any]]:
    """The NARRATOR repair shape: draft completion, then the engine's flags pair.

    The draft is streamed first (exactly what ``make_narrator_node`` does), so its
    reasoning is the provider's own. The seeded pair is then sent as the repair
    loop's first iteration sends it — ``_injection_messages`` with the draft's
    reasoning passed through the same ``should_echo_reasoning`` gate.
    """
    caller = RegistryModelCaller(PROBE_AGENT, registry, log=None)
    draft_messages = [
        {"role": "system", "content": "You are a terse assistant."},
        {"role": "user", "content": DRAFT_ASK},
    ]
    content: list[str] = []
    reasoning: list[str] = []
    reasoning_present = False
    async for event in caller(draft_messages, True):
        if event.get("type") == "content":
            content.append(event.get("delta") or "")
        elif event.get("type") == "reasoning":
            reasoning.append(event.get("delta") or "")
            reasoning_present = True
    draft = "".join(content)
    # ``None`` (the field never arrived) and ``""`` (it arrived empty) are
    # different facts: the vendor wants the field passed back either way.
    draft_reasoning: str | None = "".join(reasoning) if reasoning_present else None
    if draft_reasoning is None:
        return "inconclusive", "the draft carried no reasoning_content field", {}

    seeded = [
        *draft_messages,
        *_injection_messages(
            draft or "(draft)",
            LoopInjection(content='{"flags": []}'),
            reasoning=_echoed_reasoning(return_thinking, draft_reasoning),
        ),
    ]
    pair = seeded[-2]
    evidence = {
        "pair keys": sorted(pair),
        "draft reasoning": (
            f"{len(draft_reasoning)} chars" if draft_reasoning else "present, empty"
        ),
    }
    try:
        async for _event in caller(seeded, True):
            pass
    except Exception as exc:  # noqa: BLE001 - the failure IS the evidence
        return "failed", f"{type(exc).__name__}: {exc}", evidence
    return "completed", "", evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--env-file",
        default=".env",
        help="dotenv file holding the key (read into the environment; never printed)",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help="env var NAME the key is read from (the value never leaves the process)",
    )
    args = parser.parse_args(argv)

    load_env_file(args.env_file)  # overlays os.environ; nothing is echoed
    if not os.environ.get(args.api_key_env):
        print(
            f"error: {args.api_key_env} is not set by {args.env_file}; "
            "nothing was sent",
            file=sys.stderr,
        )
        return 1

    registry = build_registry(args.base_url, args.model, args.api_key_env)
    print(f"endpoint: {args.base_url} · model: {args.model} · key env: {args.api_key_env}")
    # ONE event loop for every probe: each call opens its own transport (the
    # engine's caller owns it), and a loop that outlives them lets each close.
    return asyncio.run(_report(registry))


async def _report(registry: Registry) -> int:
    print("\n--- BEFORE (return_thinking=false: the pre-fix wire shape) ---")
    before_status, (before_detail, before_caller) = await probe(
        registry, return_thinking=False
    )
    print(describe_second_request(before_caller))
    if before_status == "failed":
        print(f"outcome: FAILED — {type(before_detail).__name__}: {before_detail}")
    else:
        print(
            "outcome: completed WITHOUT reasoning_content — the model answered in "
            "one turn or the provider did not enforce the rule; see the AFTER run"
        )

    print("\n--- AFTER (default: Return thinking AUTO) ---")
    after_status, (after_detail, after_caller) = await probe(
        registry, return_thinking=None
    )
    print(describe_second_request(after_caller))
    if after_status == "failed":
        print(f"outcome: FAILED — {type(after_detail).__name__}: {after_detail}")
    elif after_status == "completed":
        result = after_detail
        tools = [iteration.tool for iteration in result.iterations]
        reasoned = [
            None if iteration.reasoning is None else len(iteration.reasoning)
            for iteration in result.iterations
        ]
        print(
            f"outcome: completed in {len(result.iterations)} iteration(s) "
            f"tools={tools} reasoning chars per iteration={reasoned} "
            f"timed_out={result.timed_out}"
        )
        print(f"final text: {result.text.strip()[:200]}")

    print("\n--- BEFORE (narrator repair: engine-authored flags pair, no reasoning) ---")
    seed_before, seed_before_detail, seed_before_evidence = await probe_seeded_pair(
        registry, return_thinking=False
    )
    print(f"seeded pair keys: {seed_before_evidence.get('pair keys')}")
    if seed_before == "failed":
        print(f"outcome: FAILED — {seed_before_detail}")
    else:
        print(f"outcome: {seed_before.upper()} — {seed_before_detail or 'no error'}")

    print("\n--- AFTER (narrator repair: the draft's own reasoning on the pair) ---")
    seed_after, seed_after_detail, seed_after_evidence = await probe_seeded_pair(
        registry, return_thinking=None
    )
    print(
        f"seeded pair keys: {seed_after_evidence.get('pair keys')} "
        f"(draft reasoning: {seed_after_evidence.get('draft reasoning')})"
    )
    if seed_after == "failed":
        print(f"outcome: FAILED — {seed_after_detail}")
    else:
        print(f"outcome: {seed_after.upper()} — {seed_after_detail or 'no error'}")

    if after_status != "completed":
        return 1
    if before_status != "failed":
        return 2
    if seed_after != "completed":
        return 1
    if seed_before != "failed":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
