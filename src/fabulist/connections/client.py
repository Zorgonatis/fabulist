"""Client factory + request builder + provider-error typing (PLAN R5, W0.3, #26).

``get_client`` returns an ``openai.OpenAI`` bound to an agent's resolved connection.
``build_request`` returns the complete kwargs for ``client.chat.completions.create`` with
samplers and non-standard ``extra_body`` params merged in a fixed precedence:

    defaults  <  connection entry  <  thinking-off payload  <  call-site overrides

The merge logic is pure dict handling, so precedence and passthrough are unit-testable
without a network round trip.

``provider_request_error`` (issue #26 R6) turns an SDK failure whose response body
names the provider's OWN error code (z.ai's ``{"error": {"code": "1301", …}}``) into
:class:`ProviderRequestError`, so a platform CONTENT FILTER reads differently from a
network fault — the code rides the message and the attribute, and the ADR-023 phase
contract (phase + model) keeps working through the existing duck-typed chain walk.
"""

from __future__ import annotations

import json
import os
from typing import Any

from openai import OpenAI

from .capabilities import thinking_disable_payload
from .registry import (
    Connection,
    ConnectionConfigError,
    Registry,
    load_registry,
)

__all__ = [
    "DEFAULT_CALL_TIMEOUT_S",
    "DEFAULT_SAMPLING",
    "KEYLESS_API_KEY",
    "ModelCallDeadlineExceeded",
    "PROVIDER_FILTER_CODES",
    "ProviderRequestError",
    "build_request",
    "call_deadline_s",
    "configure",
    "deadline_message",
    "get_client",
    "merge_extra_body",
    "merge_sampling",
    "provider_error_code",
    "provider_request_error",
    "set_registry",
]

# Baseline samplers applied to every request before the connection entry and call-site
# overrides. Entries only need to specify what they change.
DEFAULT_SAMPLING: dict[str, Any] = {
    "temperature": 0.8,
    "top_p": 0.95,
    "max_tokens": 1024,
}

# No baseline extra_body params: non-standard fields are opt-in per connection.
DEFAULT_EXTRA_BODY: dict[str, Any] = {}

# Sent as the bearer token for keyless OpenAI-compatible servers (e.g. local llama.cpp);
# the SDK requires a non-empty api_key string.
KEYLESS_API_KEY = "not-needed"

#: The default WALL-CLOCK bound ONE model call rides (``call_timeout_s`` unset).
#:
#: Measured, not guessed: a real GLM turn took 453.2s end to end and its longest
#: single call (narration + DSL gate) ~233s, so 900s is ~3.9x headroom — it does
#: not cut a normal slow turn, and it does end a call that cannot finish. This is
#: NOT ``Connection.timeout``: that one bounds each READ, and on a stream every
#: delta resets it, so a provider dripping one token every few hundred ms is
#: never silent and the turn hangs forever (issue #25).
DEFAULT_CALL_TIMEOUT_S = 900.0

_active_registry: Registry | None = None


def set_registry(registry: Registry) -> None:
    """Install the registry used by ``get_client``/``build_request`` when none is passed."""
    global _active_registry
    _active_registry = registry


def configure(path: str | os.PathLike[str]) -> Registry:
    """Load a config file, install it as the active registry, and return it."""
    registry = load_registry(path)
    set_registry(registry)
    return registry


class ModelCallDeadlineExceeded(RuntimeError):
    """A model call exceeded its connection's ``call_timeout_s`` WALL-CLOCK bound.

    Raised by the streaming caller (``wiring.RegistryModelCaller``) when a call
    has been open longer than the connection's deadline, whatever the stream was
    doing — a trickling stream included. It is deliberately NOT the transport's
    per-read timeout (``Connection.timeout``): that one is reset by every delta,
    which is why a live-but-slow stream could hang a turn forever (issue #25).

    ``transport = True`` classifies it as a transport-class failure for
    :func:`fabulist.graph.provider_errors.phase_failure` (the same flag
    ``PlannerBootstrapError`` uses for its timeout): in a PRE-COMMIT phase the
    turn ABORTS on the engine's error frame naming the phase and the model,
    instead of the failure being contained as if it were bad content.
    """

    #: True: no answer arrived in time — a transport failure, never content.
    transport = True

    def __init__(self, slot: str, deadline_s: float, *, delivered: bool) -> None:
        self.slot = slot
        self.deadline_s = float(deadline_s)
        #: True when deltas had already arrived: the stream was ALIVE, which is
        #: exactly why the per-read timeout never fired.
        self.delivered = bool(delivered)
        super().__init__(deadline_message(self.slot, self.deadline_s, self.delivered))


def deadline_message(slot: str, deadline_s: float, delivered: bool) -> str:
    """``"model call exceeded the 900s call deadline (call_timeout_s for …)"``.

    ``slot`` is the connection slot the call resolved through
    (``completion.narration`` &c.), so the reader knows WHICH limit fired, and
    ``delivered`` says whether the stream was still live when it did — the fact
    that separates this bound from the per-read timeout.
    """
    why = (
        "the stream was still delivering, so the per-read timeout never fired"
        if delivered
        else "no chunk arrived before the deadline"
    )
    return (
        f"model call exceeded the {_seconds(deadline_s)} call deadline "
        f"(call_timeout_s for {slot}; {why})"
    )


def _seconds(value: float) -> str:
    """``900.0`` -> ``"900s"``; ``1.5`` -> ``"1.5s"`` (the message's units)."""
    return f"{int(value)}s" if float(value).is_integer() else f"{value:g}s"


def call_deadline_s(connection: Connection) -> float:
    """The WALL-CLOCK bound one call on ``connection`` rides — never ``None``.

    An undeclared (or zero) ``call_timeout_s`` means :data:`DEFAULT_CALL_TIMEOUT_S`:
    every call has a bound, so a connection can only ever make it TIGHTER.
    """
    if connection.call_timeout_s:
        return float(connection.call_timeout_s)
    return DEFAULT_CALL_TIMEOUT_S


#: z.ai business codes that mean the PLATFORM FILTERED the content — the request
#: or the generation tripped a safety check, which is NOT a network fault and must
#: not read like one. ``1301`` is the documented one ("System detected potentially
#: unsafe or sensitive content in input or generation", HTTP 400; z.ai "Errors").
#:
#: ``1305`` is deliberately NOT in this set: z.ai documents it as "The service may
#: be temporarily overloaded" (HTTP 429). It is *reported* in the field as a
#: spurious Coding-Plan refusal, so what this module owes the reader is the CODE
#: surfaced verbatim (:attr:`ProviderRequestError.code`) — never a re-classification
#: guessed from a code that two mechanisms share.
PROVIDER_FILTER_CODES: frozenset[str] = frozenset({"1301"})

#: What the message does NOT dump: the provider's own sentence is capped, and it
#: is one line — a frame carries a sentence, not a body.
_PROVIDER_TEXT_LIMIT = 300


class ProviderRequestError(RuntimeError):
    """A provider HTTP failure that named its OWN business error code in the body.

    z.ai answers errors in the shape ``{"error": {"code": "1301", "message": …}}``
    (z.ai "Errors"), where the HTTP status and the business code are two different
    facts: a 400/``1301`` is the platform FILTERING the content, a 429/``1305`` is
    the service, a ``0029``-shaped timeout is the network. Collapsing all of them
    into "the call failed" is what made a filter indistinguishable from a fault
    (issue #26 R6), so the code is carried both in the message and as an attribute.

    It is raised FROM the SDK's own exception (``raise … from exc``), so the
    classification the ADR-023 contract already does keeps working unchanged:
    :func:`fabulist.graph.provider_errors.phase_failure` reads ``status_code``,
    the ``transport`` flag and the message across the cause chain, and the
    :class:`~fabulist.graph.provider_errors.PhaseCallError` it builds still names
    the PHASE and the MODEL — the code now rides the provider's own words.
    """

    #: True: the provider ANSWERED and refused — a provider-class failure, never
    #: content the phase can contain (the same flag ``ModelCallDeadlineExceeded``
    #: uses for its timeout).
    transport = True

    def __init__(self, *, code: str, status: int | None, text: str = "") -> None:
        #: The provider's own code, as it wrote it (z.ai: ``"1301"``).
        self.code = code
        #: The HTTP status the code arrived with, when the failure carried one —
        #: named ``status_code`` so the ADR-023 chain walk finds it here first.
        self.status_code = status
        #: True when the code is a documented PLATFORM CONTENT FILTER (see
        #: :data:`PROVIDER_FILTER_CODES`): the call was refused for its content,
        #: so retrying it unchanged cannot help.
        self.filtered = code in PROVIDER_FILTER_CODES
        #: The provider's own sentence (empty when it sent none).
        self.message = _compose_message(self.code, self.filtered, text)
        super().__init__(self.message)


def _compose_message(code: str, filtered: bool, text: str) -> str:
    """``"[code 1301] platform content filter — System detected …"``.

    The code leads because it is the fact the provider's prose does not reliably
    carry (a Coding-Plan ``1305`` and a genuine overload share their wording), and
    the filter label is spelled out so the reader does not have to know the table.
    """
    head = f"[code {code}]"
    if filtered:
        head += " platform content filter"
    return f"{head} — {text}" if text else head


def _chain(exc: BaseException):
    """``exc`` then its cause/context chain, each visited once (no imports needed)."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _http_status(exc: BaseException) -> int | None:
    """The HTTP status of an SDK/httpx failure, or ``None`` when it carried none.

    Duck-typed on the three spellings that exist in the wild (``status_code`` from
    openai's ``APIStatusError``, an older ``status``, and ``exc.response.
    status_code``) — the same reading ``graph.provider_errors.failure_status``
    does, kept local because ``connections`` is the LOWER layer: importing that
    module from here would close a cycle (``graph.context_monitor`` already
    imports this one).
    """
    for current in _chain(exc):
        for name in ("status_code", "status"):
            value = getattr(current, name, None)
            if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
                return value
        value = getattr(getattr(current, "response", None), "status_code", None)
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            return value
    return None


def _error_bodies(exc: BaseException):
    """The response bodies an error carries, most-parsed first (dicts or text)."""
    body = getattr(exc, "body", None)
    if body is not None:
        yield body
    response = getattr(exc, "response", None)
    if response is None:
        return
    try:  # an httpx.Response keeps the parsed JSON one call away
        yield response.json()
    except Exception:  # noqa: BLE001 - a body read is never worth hiding the failure
        text = getattr(response, "text", None)
        if isinstance(text, str) and text.strip():
            yield text


def _normalize_code(value: Any) -> str | None:
    """A business code as a string (``1301``/``"1301"``), or ``None``.

    Booleans, containers and blanks are refused: the field is an identifier, not
    a free-form value, and a wrong one would print a lie into the turn's error.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        code = value.strip()
        if code and len(code) <= 40:
            return code
    return None


def _code_of(body: Any) -> str | None:
    """The business code out of one error body.

    Two documented shapes are read: z.ai's nested ``{"error": {"code": …}}``
    ("Errors") and the flat ``{"code": …, "message": …}`` its chat-completion
    reference shows for a non-200. A JSON STRING body is parsed first; a bare
    string code under ``error`` is taken as-is.
    """
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except (TypeError, ValueError):
            return None
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if isinstance(error, dict):
        code = _normalize_code(error.get("code"))
        if code is not None:
            return code
    else:
        code = _normalize_code(error)
        if code is not None:
            return code
    return _normalize_code(body.get("code"))


def provider_error_code(exc: BaseException) -> str | None:
    """The provider's own business error code behind ``exc``, or ``None``.

    Walks the cause chain, because a client may wrap the original failure
    (``raise … from exc``) and the body rides the innermost exception.
    """
    for current in _chain(exc):
        for body in _error_bodies(current):
            code = _code_of(body)
            if code is not None:
                return code
    return None


def _error_text(exc: BaseException) -> str:
    """The provider's own sentence: its error body first, then the SDK's text."""
    for current in _chain(exc):
        for body in _error_bodies(current):
            text = _body_sentence(body)
            if text:
                return text
        message = getattr(current, "message", None)
        if isinstance(message, str) and message.strip():
            return _clip(message)
        rendered = str(current).strip()
        if rendered:
            return _clip(rendered)
    return ""


def _body_sentence(body: Any) -> str:
    """The human sentence out of a provider error body (dict or JSON-ish text)."""
    if isinstance(body, str):
        return _clip(body)
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    for candidate in (error, body.get("message"), body.get("detail")):
        if isinstance(candidate, str) and candidate.strip():
            return _clip(candidate)
        if isinstance(candidate, dict):
            inner = candidate.get("message")
            if isinstance(inner, str) and inner.strip():
                return _clip(inner)
    return ""


def _clip(text: str) -> str:
    """One line, capped at :data:`_PROVIDER_TEXT_LIMIT`."""
    flat = " ".join(str(text).split())
    if len(flat) <= _PROVIDER_TEXT_LIMIT:
        return flat
    return flat[:_PROVIDER_TEXT_LIMIT].rstrip() + "…"


def provider_request_error(exc: BaseException) -> ProviderRequestError | None:
    """The typed, code-carrying error for ``exc`` — ``None`` when it named no code.

    A failure with NO HTTP status (a connection error, a timeout) has no body and
    earns ``None``: it travels unchanged, so a network fault is never dressed up
    as a provider refusal — that distinction is the whole point of this function.
    An already-typed error is returned as-is, so wrapping twice cannot nest.
    """
    if isinstance(exc, ProviderRequestError):
        return exc
    status = _http_status(exc)
    if status is None or status < 400:
        return None
    code = provider_error_code(exc)
    if code is None:
        return None
    return ProviderRequestError(code=code, status=status, text=_error_text(exc))


def merge_sampling(entry: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """defaults < entry < overrides; shallow merge (one spelling: later wins)."""
    return {**DEFAULT_SAMPLING, **entry, **overrides}


def merge_extra_body(entry: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """defaults < entry < overrides; shallow merge so whole nested params can be replaced."""
    return {**DEFAULT_EXTRA_BODY, **entry, **overrides}


def _resolve(agent_name: str, registry: Registry | None) -> Connection:
    active = registry if registry is not None else _active_registry
    if active is None:
        raise RuntimeError(
            "no active registry; call configure(path) or set_registry(registry) first"
        )
    return active.connection_for(agent_name)


def _with_reasoning_history(
    messages: list[dict[str, Any]], connection: Connection
) -> list[dict[str, Any]]:
    """Gate prior-turn ``reasoning_content`` on the connection's thinking settings.

    Issue #42: ``history_from_log`` stamps ``reasoning_content`` on every
    assistant history message whose turn PRODUCED reasoning (the narrative row
    carries it) — PRESENT but EMPTY (owner ruling on #29: a prior execution's
    text is stored for diagnosis, never re-sent). Whether that field may ride the
    wire is a property of the CONNECTION, so the decision lives here, at the one
    request-building seam:

    * ``thinking is False`` — thinking mode is off, where the field is noise;
    * ``return_thinking is False`` — the owner's "Return thinking" switch says
      never send it;
    * otherwise AUTO/ON — the field travels as history assembled it, including
      the empty string the provider itself returns for a no-reasoning turn
      (presence is the fact; DeepSeek 400s on a MISSING field).

    Turns with no stored reasoning have no key and stay content-only, so a
    non-thinking provider keeps a byte-identical message list (the original
    list object is returned untouched when no message carries the field).

    The per-message answer reuses :func:`fabulist.agents.loop.should_echo_reasoning`
    — the SAME tri-state rule the tool loop applies — rather than a fork of it.
    """
    if not any(
        isinstance(message, dict) and "reasoning_content" in message
        for message in messages
    ):
        return messages

    from fabulist.agents.loop import should_echo_reasoning

    out: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or "reasoning_content" not in message:
            out.append(message)
            continue
        reasoning = message.get("reasoning_content")
        echo = (
            message.get("role") == "assistant"
            and connection.thinking is not False
            and should_echo_reasoning(
                connection.return_thinking,
                reasoning if isinstance(reasoning, str) else None,
            )
        )
        if echo:
            out.append(message)
        else:
            out.append(
                {key: value for key, value in message.items() if key != "reasoning_content"}
            )
    return out


def build_request(
    agent_name: str,
    messages: list[dict[str, Any]],
    *,
    registry: Registry | None = None,
    request_profile: dict[str, Any] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """Return complete ``chat.completions.create`` kwargs for ``agent_name``.

    ``overrides`` are call-site values: any key other than ``extra_body`` is treated as a
    top-level sampler (temperature, max_tokens, stop, ...). ``extra_body={...}`` overrides
    the connection's non-standard body params.

    ``request_profile`` (W8.4) is an optional per-interaction transport profile — e.g.
    ``response_format`` / ``tools`` / ``tool_choice`` for structured output, plus an
    ``extra_body`` for a per-request thinking toggle. Non-``extra_body`` keys ride as
    top-level kwargs after the samplers; a profile ``extra_body`` merges over the
    connection's (the call-site ``extra_body`` still wins). When ``request_profile`` is
    omitted the returned kwargs are byte-identical to a build with no profile — the
    narrator/OOC request must never gain transport params by accident.

    The connection's ``thinking`` toggle (W9.25) is translated here: ``None``/``True``
    (ON, the default) adds NOTHING — the request is byte-identical to a build without the
    field — while ``False`` merges the provider family's measured disable payload
    (:func:`fabulist.connections.capabilities.thinking_disable_payload`) INTO
    ``extra_body``, replacing the connection's own authored keys for the same names (that
    override IS the switch) and staying under a call-site/profile ``extra_body``. An
    unmeasured family gets no payload: the toggle is a no-op, never a guess.

    ``reasoning_effort`` (issue #26 R5) rides as a TOP-LEVEL kwarg, which is where the
    provider documents it (z.ai "Chat Completion": ``reasoning_effort``, sibling of
    ``temperature``) and what the SDK serializes into the request body at that level.
    It is added ONLY when the connection sets one: unset means no key at all, so every
    provider that has never heard of the field keeps a byte-identical request.

    Issue #42: assistant history messages may arrive carrying ``reasoning_content``
    (a prior turn stored the field — history assembly emits it PRESENT but EMPTY
    per the owner's #29 ruling). :func:`_with_reasoning_history` keeps that field
    only for a thinking-active connection whose ``return_thinking`` allows it, and
    strips it everywhere else — so a non-thinking provider's body is unchanged.
    """
    connection = _resolve(agent_name, registry)

    extra_override = overrides.pop("extra_body", None) or {}
    if not isinstance(extra_override, dict):
        raise ConnectionConfigError(
            f"build_request({agent_name!r}): extra_body override must be an object"
        )

    profile = dict(request_profile) if request_profile else {}
    profile_extra = profile.pop("extra_body", None) or {}
    if not isinstance(profile_extra, dict):
        raise ConnectionConfigError(
            f"build_request({agent_name!r}): request_profile extra_body must be an object"
        )

    kwargs: dict[str, Any] = {
        "model": connection.model,
        "messages": _with_reasoning_history(messages, connection),
    }
    # R5 (issue #26): the reasoning DEPTH is a TOP-LEVEL body field of the same
    # name, so it is set here — before the samplers and the profile — which keeps
    # it on the connection's side of the precedence: a call-site/profile value
    # still wins, and a connection that declares none sends NOTHING (the key is
    # absent, so every other provider's request stays byte-identical).
    if connection.reasoning_effort is not None:
        kwargs["reasoning_effort"] = connection.reasoning_effort
    kwargs.update(merge_sampling(connection.sampling, overrides))
    kwargs.update(profile)

    disable = (
        thinking_disable_payload(connection) if connection.thinking is False else None
    )
    extra_body = merge_extra_body(
        connection.extra_body,
        {**(disable or {}), **profile_extra, **extra_override},
    )
    if extra_body:
        kwargs["extra_body"] = extra_body
    return kwargs


def get_client(
    agent_name: str,
    *,
    registry: Registry | None = None,
    http_client: Any | None = None,
) -> OpenAI:
    """Return an ``OpenAI`` client configured from the agent's resolved connection.

    ``http_client`` is an injection seam for tests (an ``httpx2.Client``); production
    callers omit it and the SDK builds its own.
    """
    connection = _resolve(agent_name, registry)

    if connection.api_key is None and connection.api_key_env is not None:
        raise ConnectionConfigError(
            f"connections[{connection.name!r}].api_key_env: "
            f"env var {connection.api_key_env!r} is not set"
        )

    init: dict[str, Any] = {
        "base_url": connection.base_url,
        "api_key": connection.api_key or KEYLESS_API_KEY,
    }
    if connection.timeout is not None:
        init["timeout"] = connection.timeout
    if http_client is not None:
        init["http_client"] = http_client

    return OpenAI(**init)
