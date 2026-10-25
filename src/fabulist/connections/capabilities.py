"""Provider-capability resolution for the structured-output transport tier (W8.4).

The transport ladder (Tier A function-calling → B ``json_object`` → C
``json_schema`` → D free-form; see ``the maintainer's private notes (unshipped)``
§2) needs to know what a *connection's* backend actually supports. This module
answers exactly that, and nothing else — it never calls a provider, so it is pure
dict/string handling and trivially testable.

Resolution rule (one spelling per concept)
------------------------------------------
``effective = provider-profile default  <  the connection's explicit
``capabilities`` keys``.

* The provider profile is chosen by **base_url / model family** (a substring
  match on the lowercased ``"<base_url> <model>"``), because that is the only
  identity a :class:`~fabulist.connections.registry.Connection` carries.
* An explicit ``capabilities`` key on the connection wins outright — that is how
  a user pins a capability the profiles guess wrong.
* A provider with no measured identity gets :data:`DEFAULT_CAPABILITIES`, the
  modern-OpenAI-compatible default: tools and ``json_object`` ("json mode") are
  on, while every capability that changes the request in a way a provider might
  REJECT (``json_schema``, a forced ``tool_choice``) and every claim about
  reasoning/thinking stays False — the ladder degrades, it never assumes.
  Streaming is not a capability: it is the connection's own ``streaming``
  toggle (:class:`fabulist.connections.registry.Connection`), ON unless the
  owner says otherwise.
* An explicit ``capabilities`` key beyond the profiles is preserved verbatim — a
  user can gate a capability ahead of a profile update.

Measured truth encoded here (orchestrator probe + W8 spike; see the plan §2 and
the ORCHESTRATOR CORRECTIONS appendix):

* llama.cpp (the local ``:4444`` deployment, ``your-model-id``): function-calling with a
  FORCED ``tool_choice`` works, and ``response_format: json_schema`` is accepted.
* DeepSeek ``thinking``: rejects a forced ``tool_choice`` (both ``"required"`` and
  ``{"type": "function"}``) but accepts ``"auto"``; ``response_format:
  json_schema`` returns 400. ``thinking`` can be disabled per request.

The thinking-disable TRANSLATION table (W9.25 §3.3)
---------------------------------------------------
:data:`THINKING_DISABLE_PAYLOADS` is the per-provider body payload the W9.25
one-tap thinking toggle translates into: the connection's ``thinking: false``
becomes the measured ``extra_body`` fragment for its provider family, and
:func:`thinking_disable_payload` is the only reader. It is deliberately
SEPARATE from the ``thinking_disable`` capability key above: that key gates the
structured-output transport tier (forced ``tool_choice``), while this table
answers "what body fragment turns thinking off", which llama.cpp CAN answer per
request via its chat template even though the ladder profile leaves the key
False. A family with no measured entry gets no payload — degrade, never assume.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from .registry import Connection

__all__ = [
    "CAPABILITY_KEYS",
    "DEFAULT_CAPABILITIES",
    "PROVIDER_PROFILES",
    "THINKING_DISABLE_PAYLOADS",
    "ThinkingDisable",
    "provider_family",
    "resolve_capabilities",
    "thinking_disable_payload",
]

#: The capability names the transport tier consults. A false / unset value means
#: "not available" — features degrade to a lower tier, they never assume.
CAPABILITY_KEYS: frozenset[str] = frozenset(
    {
        "function_calling",  # tools=[...] accepted; tool_calls surfaced back
        "json_object",  # response_format={"type": "json_object"} accepted
        "json_schema",  # response_format={"type": "json_schema", ...} accepted
        "tool_choice_forced",  # tool_choice="required"/{type:function} accepted
        "reasoning_model",  # deliberates; empty-content / token-budget trap
        "thinking_disable",  # thinking can be turned OFF per request
    }
)

#: The all-off base every profile is stated on top of, so the difference from
#: "nothing measured" is auditable key by key.
_DISABLED: dict[str, bool] = {key: False for key in CAPABILITY_KEYS}

#: Default profile for a provider with no measured identity (W3c): the modern
#: OpenAI-compatible surface — ``tools=[...]`` and
#: ``response_format={"type": "json_object"}`` are accepted by essentially every
#: current server (LM Studio, vLLM, Ollama's ``/v1``, OpenAI, Anthropic-
#: compatible gateways). The two keys still off are the ones a server may
#: REJECT rather than degrade (strict ``json_schema``, a forced ``tool_choice``),
#: plus the two claims about the MODEL (``reasoning_model``/
#: ``thinking_disable``) which must be measured, not assumed. An owner pins any
#: of them per connection via ``capabilities``.
DEFAULT_CAPABILITIES: dict[str, bool] = {
    **_DISABLED,
    "function_calling": True,
    "json_object": True,
}


def _profile(**overrides: bool) -> dict[str, bool]:
    return {**_DISABLED, **overrides}


#: Provider family -> capability profile. Every measured capability is stated
#: explicitly so the difference from the safe default is auditable.
PROVIDER_PROFILES: dict[str, dict[str, bool]] = {
    # Local llama.cpp (the :4444 deployment). Measured: forced tool_choice + JSON
    # schema both work; not a reasoning model.
    "llamacpp": _profile(
        function_calling=True,
        json_object=True,
        json_schema=True,
        tool_choice_forced=True,
        reasoning_model=False,
        thinking_disable=False,
    ),
    # DeepSeek thinking mode. Measured: forced tool_choice is rejected ("Thinking
    # mode does not support this tool_choice"), json_schema is 400; "auto" is
    # accepted; thinking can be disabled per request.
    "deepseek": _profile(
        function_calling=True,
        json_object=True,
        json_schema=False,
        tool_choice_forced=False,
        reasoning_model=True,
        thinking_disable=True,
    ),
}

#: Ordered family detection: the FIRST family whose markers appear in
#: ``"<base_url> <model>"`` wins. DeepSeek first (its host is unambiguous);
#: llama.cpp next by explicit markers. A bare ``localhost``/``127.0.0.1`` host is
#: treated as the local llama.cpp router this project serves its models from.
_PROVIDER_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("deepseek", ("deepseek",)),
    ("llamacpp", ("llama.cpp", "llamacpp", "gguf")),
    ("llamacpp", ("localhost:", "127.0.0.1:")),
)


@dataclass(frozen=True)
class ThinkingDisable:
    """The measured body payload that turns thinking OFF for one provider family."""

    #: Merged into the request's ``extra_body`` as-is (top-level keys replace the
    #: connection's authored ones — the toggle is the point of the switch).
    payload: dict[str, Any]
    #: Model-name substrings the payload is measured for; empty = every model of
    #: the family. llama.cpp's switch is a chat-template VARIABLE
    #: (``enable_thinking``), so it only means something on template families
    #: that read it — measured for Qwen; anything else gets the no-op.
    model_markers: tuple[str, ...] = ()


#: Provider family -> the measured thinking-disable payload (W9.25 §3.3).
#: Unmeasured family = no entry = the toggle is a documented no-op.
THINKING_DISABLE_PAYLOADS: dict[str, ThinkingDisable] = {
    # DeepSeek: the W8.4 ladder's measured non-thinking body (thinking can be
    # disabled per request; the accepted alternative spelling is
    # ``{"reasoning_effort": "none"}``).
    "deepseek": ThinkingDisable({"thinking": {"type": "disabled"}}),
    # llama.cpp: the owner's own measured note — the GGUF chat template takes
    # ``enable_thinking`` as a template kwarg, so the disable rides there and
    # NOT as a first-class body field (llama.cpp-specific params stay out of the
    # connection schema; this is the engine's translation of the generic toggle).
    "llamacpp": ThinkingDisable(
        {"chat_template_kwargs": {"enable_thinking": False}}, ("qwen",)
    ),
}


def provider_family(connection: Connection) -> str | None:
    """Detect the provider family (a :data:`PROVIDER_PROFILES` key) or ``None``."""
    haystack = f"{connection.base_url} {connection.model}".lower()
    for family, markers in _PROVIDER_MARKERS:
        if any(marker in haystack for marker in markers):
            return family
    return None


def resolve_capabilities(connection: Connection) -> dict[str, bool]:
    """Effective capability set for ``connection``: explicit keys over profile.

    Returns the full :data:`CAPABILITY_KEYS` set (every known key present, bool),
    so callers read ``caps["json_schema"]`` without a membership default. An
    explicit key the profiles do not know is preserved verbatim — a user can
    gate a capability ahead of a profile update.
    """
    family = provider_family(connection)
    effective = dict(PROVIDER_PROFILES.get(family, DEFAULT_CAPABILITIES))
    for key, value in (connection.capabilities or {}).items():
        effective[key] = bool(value)
    return effective


def thinking_disable_payload(connection: Connection) -> dict[str, Any] | None:
    """The ``extra_body`` fragment that turns thinking OFF for ``connection``.

    ``None`` when the provider family (or, for a family measured on a template
    subset, the model) has no measured entry — the W9.25 toggle is then a no-op,
    never a guess. The returned dict is a fresh copy: callers merge it into a
    request body and must not be able to mutate the measured table.
    """
    entry = THINKING_DISABLE_PAYLOADS.get(provider_family(connection) or "")
    if entry is None:
        return None
    if entry.model_markers:
        model = connection.model.lower()
        if not any(marker in model for marker in entry.model_markers):
            return None
    return copy.deepcopy(entry.payload)
