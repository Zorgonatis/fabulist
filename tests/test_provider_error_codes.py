"""Provider business error codes are SURFACED, not swallowed (issue #26 R6).

z.ai answers a failure with two facts, not one: the HTTP status and a business code in
the body — ``{"error": {"code": "1301", "message": …}}`` (z.ai "Errors"). They mean
different things:

* ``1301`` (HTTP 400) — the platform detected potentially unsafe/sensitive content in
  the input OR the generation. That is a CONTENT FILTER, not a fault: retrying the same
  payload unchanged cannot help, and telling the reader "400" leaves them unable to see
  the difference.
* ``1305`` (HTTP 429) — documented as "service may be temporarily overloaded", and
  reported in the field as a spurious Coding-Plan refusal. The code is what makes
  either reading visible, so it is surfaced verbatim rather than re-interpreted.

What is pinned here:

* the code is read out of the two documented body shapes (nested ``error.code`` — the
  authoritative one — and the flat ``code``), including a JSON-string body;
* :func:`fabulist.connections.client.provider_request_error` builds
  :class:`ProviderRequestError` carrying ``code``, ``status_code`` and ``filtered``,
  and the code rides the MESSAGE (an error frame is a sentence);
* a failure that named NO code — a connection error, a timeout, a plain 400 — is
  returned as ``None`` and travels UNCHANGED, so a network fault is never dressed up
  as a provider refusal;
* the ADR-023 contract is untouched: :func:`fabulist.graph.provider_errors.phase_failure`
  still classifies it provider-class and still names the PHASE and the MODEL;
* the transport seam (``wiring._open_call``, both the streaming and the non-streaming
  request) raises the typed error.

Fake endpoints only — no live inference.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import openai
import pytest

from fabulist.connections.client import (
    PROVIDER_FILTER_CODES,
    ProviderRequestError,
    provider_error_code,
    provider_request_error,
)
from fabulist.connections.registry import Registry
from fabulist.graph.provider_errors import PhaseCallError, phase_failure
import fabulist.wiring as wiring

ZAI_URL = "https://api.z.ai/api/coding/paas/v4/chat/completions"

#: The documented filter sentence, verbatim from z.ai "Errors" (code 1301).
FILTER_SENTENCE = (
    "System detected potentially unsafe or sensitive content in input or generation. "
    "Please avoid using prompts that may generate sensitive content."
)
#: The documented 1305 sentence — identical wording for a real overload and the
#: reported Coding-Plan fake, which is exactly why the CODE has to be surfaced.
OVERLOAD_SENTENCE = "The service may be temporarily overloaded, please try again later"


def _sdk_error(status: int, body, *, message: str | None = None):
    """A REAL SDK status error, built the way the transport raises one."""
    request = httpx.Request("POST", ZAI_URL)
    response = httpx.Response(
        status,
        request=request,
        json=body if isinstance(body, dict) else None,
        text=body if isinstance(body, str) else None,
    )
    rendered = message or f"Error code: {status} - {body}"
    return openai.APIStatusError(rendered, response=response, body=body)


def _zai_error(code: str, sentence: str, *, status: int = 400):
    return _sdk_error(status, {"error": {"code": code, "message": sentence}})


# --- reading the code out of the body ----------------------------------------------


def test_code_is_read_from_the_documented_nested_error_object():
    exc = _zai_error("1301", FILTER_SENTENCE)

    assert provider_error_code(exc) == "1301"


def test_code_is_read_from_a_flat_body():
    """The chat-completion reference shows ``{"code": …, "message": …}`` for a non-200."""
    exc = _sdk_error(400, {"code": 1214, "message": "Parameter `x` is invalid."})

    assert provider_error_code(exc) == "1214"


def test_code_is_read_from_a_json_string_body():
    exc = _sdk_error(429, '{"error": {"code": "1305", "message": "busy"}}')

    assert provider_error_code(exc) == "1305"


def test_code_is_read_when_the_error_object_is_a_bare_code():
    exc = _sdk_error(400, {"error": "1301"})

    assert provider_error_code(exc) == "1301"


def test_code_is_read_through_a_wrapping_exception():
    """A client may wrap the original; the body rides the innermost failure."""

    def wrap(inner: BaseException) -> BaseException:
        try:
            raise inner
        except BaseException as exc:  # noqa: BLE001 - building a chain on purpose
            try:
                raise RuntimeError("caller re-raised") from exc
            except RuntimeError as outer:
                return outer

    assert provider_error_code(wrap(_zai_error("1301", FILTER_SENTENCE))) == "1301"


@pytest.mark.parametrize(
    "body",
    [
        {"error": {"message": "no code here"}},
        {"message": "no code here"},
        {"error": {"code": ""}},
        {"error": {"code": True}},
        {"error": {"code": {"nested": "1301"}}},
        {"code": None},
        "",
    ],
)
def test_a_body_without_a_usable_code_yields_none(body):
    assert provider_error_code(_sdk_error(400, body)) is None


# --- the typed error ---------------------------------------------------------------


def test_typed_error_carries_the_code_status_and_filter_flag():
    typed = provider_request_error(_zai_error("1301", FILTER_SENTENCE))

    assert isinstance(typed, ProviderRequestError)
    assert typed.code == "1301"
    assert typed.status_code == 400
    assert typed.filtered is True
    # The message is the surface a human reads: code first, label spelled out,
    # provider's own words after.
    assert "[code 1301]" in typed.message
    assert "platform content filter" in typed.message
    assert FILTER_SENTENCE.split(".")[0] in typed.message
    assert str(typed) == typed.message


def test_1305_is_surfaced_by_code_but_not_relabelled_as_a_filter():
    """Docs say overload; the field reports a Coding-Plan fake. Surface, do not guess."""
    typed = provider_request_error(_zai_error("1305", OVERLOAD_SENTENCE, status=429))

    assert isinstance(typed, ProviderRequestError)
    assert typed.code == "1305"
    assert typed.status_code == 429
    assert typed.filtered is False
    assert "[code 1305]" in typed.message
    assert OVERLOAD_SENTENCE.split(",")[0] in typed.message


def test_filter_set_is_the_documented_content_filter():
    assert PROVIDER_FILTER_CODES == frozenset({"1301"})


def test_a_failure_without_a_status_is_never_typed():
    """A network fault has no body: it must keep reading as a network fault."""
    connection_error = openai.APIConnectionError(request=httpx.Request("POST", ZAI_URL))

    assert provider_request_error(connection_error) is None


def test_a_status_without_a_code_is_never_typed():
    exc = _sdk_error(500, {"error": {"message": "Internal Error"}})

    assert provider_request_error(exc) is None


def test_a_legacy_body_message_still_reaches_the_message_text():
    """A provider that answers a code with no prose must not print an empty frame."""
    exc = _sdk_error(400, {"error": {"code": "1301"}})

    typed = provider_request_error(exc)

    assert typed is not None
    assert typed.message.startswith("[code 1301] platform content filter")


def test_an_already_typed_error_is_not_wrapped_twice():
    typed = provider_request_error(_zai_error("1301", FILTER_SENTENCE))

    assert provider_request_error(typed) is typed


def test_a_long_provider_sentence_is_clipped_to_one_line():
    typed = provider_request_error(
        _zai_error("1301", "spam " * 200)
    )

    assert typed is not None
    assert "\n" not in typed.message
    assert len(typed.message) < 400


# --- the ADR-023 phase contract still holds ----------------------------------------


def test_the_phase_contract_names_phase_model_and_code():
    typed = provider_request_error(_zai_error("1301", FILTER_SENTENCE))

    fatal = phase_failure("Planning", typed, provider="glm-5.3")

    assert isinstance(fatal, PhaseCallError)
    assert fatal.status == 400
    assert fatal.phase == "Planning"
    message = str(fatal)
    assert message.startswith("Planning failed:")
    assert "400" in message
    assert "glm-5.3" in message
    assert "1301" in message


# --- the transport seam ------------------------------------------------------------


class _RaisingCompletions:
    def __init__(self, error: BaseException) -> None:
        self._error = error
        self.calls = 0

    async def create(self, **kwargs):
        self.calls += 1
        raise self._error


class _RaisingClient:
    def __init__(self, error: BaseException) -> None:
        self.completions = _RaisingCompletions(error)
        self.chat = SimpleNamespace(completions=self.completions)


def _registry(**connection_overrides) -> Registry:
    return Registry.from_dict(
        {
            "connections": {
                "c": {
                    "base_url": "http://localhost:4444/v1",
                    "model": "glm-5.3",
                    **connection_overrides,
                }
            },
            "agents": {"narrator": "c"},
        }
    )


def _drain(caller):
    """Drain a caller; return ``(events, raised)`` — the error, not a crash."""

    async def _run():
        events: list[dict] = []
        try:
            async for event in caller([{"role": "user", "content": "hi"}]):
                events.append(event)
        except BaseException as exc:  # noqa: BLE001 - the thing under test
            return events, exc
        return events, None

    return asyncio.run(_run())


@pytest.mark.parametrize("streaming", [None, False])
def test_the_transport_seam_raises_the_typed_error(monkeypatch, streaming):
    """Both request shapes: streaming (default) and one non-streaming call."""
    error = _zai_error("1301", FILTER_SENTENCE)
    fake = _RaisingClient(error)
    monkeypatch.setattr(wiring, "_async_client", lambda agent, registry: fake)
    connection = {} if streaming is None else {"streaming": streaming}
    caller = wiring.RegistryModelCaller("narrator", _registry(**connection))

    events, raised = _drain(caller)

    assert events == []
    assert isinstance(raised, ProviderRequestError)
    assert raised.code == "1301"
    assert raised.status_code == 400
    assert raised.filtered is True
    # The SDK's own exception stays in the chain: nothing about the failure is lost.
    assert raised.__cause__ is error


def test_a_code_less_failure_crosses_the_seam_unchanged(monkeypatch):
    error = _sdk_error(500, {"error": {"message": "Internal Error"}})
    monkeypatch.setattr(wiring, "_async_client", lambda agent, registry: _RaisingClient(error))
    caller = wiring.RegistryModelCaller("narrator", _registry())

    events, raised = _drain(caller)

    assert events == []
    assert raised is error


def test_a_network_fault_crosses_the_seam_unchanged(monkeypatch):
    error = openai.APIConnectionError(request=httpx.Request("POST", ZAI_URL))
    monkeypatch.setattr(wiring, "_async_client", lambda agent, registry: _RaisingClient(error))
    caller = wiring.RegistryModelCaller("narrator", _registry())

    events, raised = _drain(caller)

    assert raised is error
