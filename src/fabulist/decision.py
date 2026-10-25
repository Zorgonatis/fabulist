"""d1-3b decision-model pre-gate client (optional critic skip).

Owner directive: "replace appropriate agents with a decision model … much cheaper
and faster". LiquidAI's d1-3b answers a TYPED question (``noul`` / ``choice`` /
``score``) in one forward pass on llama.cpp's ``POST /v1/systemone`` route — it
generates no tokens; the response carries the answer plus its probabilities
(~10-17ms in the spike, ``scripts/spike/d1_systemone.py``). This module is the
thin client for that route. Wired uses today: the post-turn genre
classification (``graph/nodes.py`` ``_enrich_genre``) and the batched turn gate
(the artifact-derived slop-phrase checks + narrative movement, advisory flags —
``turn_gate_questions``). It does NOT gate the
critic — the corrected spike run proved no safe skip cutoff exists (P(ok)=0.77
on a defective turn vs 0.73-0.76 on clean ones), so that pre-gate was built and
removed the same day. Memory is never gated (owner ruling: every turn is
embedded). An optional ``observer`` callback makes every ask()
attempt visible in the event log (agent ``decision`` usage rows).

Deliberate, boring constraints:

* **Missing binding = feature off.** The endpoint and model are resolved once at
  construction from a registry-like object (``connection_for``) via the
  ``decision`` agent binding. If that binding — or a usable ``base_url`` /
  ``model`` on the resolved connection — is absent, the caller is DISABLED and
  every method is a no-op returning ``None``. A stale/partial user config
  therefore degrades to today's behaviour, never an error.
* **Never fatal.** Every call is wrapped: any HTTP error, timeout, bad JSON or
  unexpected shape returns ``None``; callers read ``None`` as "no answer — do
  the expensive thing".
* **Pure stdlib + httpx.** No graph, log or OpenAI-SDK imports, so it is
  importable by tests with an injected ``httpx.MockTransport`` client.
"""

from __future__ import annotations

import inspect
import time
from collections.abc import Callable
from typing import Any

import httpx

from fabulist.decision_queries import (
    MOVEMENT_QUESTION,
    PER_TURN_SECTION,
    SLOP_PHRASES,
    question_payloads,
    questions_for_section,
    slop_phrase_map,
    slop_questions,
    slugify,
)

__all__ = [
    "DEFAULT_TIMEOUT",
    "DecisionCaller",
    "GENRE_QUESTION",
    "MOVEMENT_QUESTION",
    "SLOP_PHRASES",
    "slop_phrase_map",
    "slop_questions",
    "slugify",
    "turn_gate_questions",
    "turn_gate_state",
]

#: The agent binding the caller resolves through ``connection_for``.
_AGENT = "decision"

#: The genre classification question. Rubric v2, live-validated in the spike
#: (2026-10-13): a secondary-world scene read ``fantasy`` at 0.72, repeatable
#: across runs. Criteria carry concrete sign-lists; ``other`` is gated to a last
#: resort so it cannot absorb uncertainty (the v1 failure mode).
GENRE_QUESTION = {
    "type": "choice",
    "instructions": (
        "Name the dominant genre. One clear sign is enough. Answer 'other' "
        "only when no sign below appears."
    ),
    "criteria": {
        "fantasy": (
            "Secondary world, magic, ritual orders, guilds, pre-industrial "
            "life, artefacts that answer."
        ),
        "sci-fi": "Futuristic technology, space, machines, laboratories, ships, AI.",
        "horror": "Dread, fear, gore, monsters, the uncanny as threat.",
        "slice-of-life": "Ordinary everyday life, no magic or technology premise.",
        "other": "No sign above appears. Use only as a last resort.",
    },
}

#: Minimum probability before a genre answer is written as a fact.
GENRE_MIN_CONFIDENCE = 0.5


# ---------------------------------------------------------------------------
# per-turn decision gate (batched: slop phrases + movement)
# ---------------------------------------------------------------------------
#
# The BANK is an owner-editable asset (R25): :mod:`fabulist.decision_queries`
# owns the question rows, the log-owned storage and the validated seeded bank
# (``DEFAULT_QUESTIONS`` — the artifact-derived slop rows plus movement as the
# per-turn ``section``, plus the two W9.22 proposal rows this gate never asks). The
# question WORDING is validated — ``scripts/spike/d1_respike.py`` measured these
# exact formulations on d1-3b and found bit-identical probabilities across runs
# (the model emits no tokens, so identical input -> identical P). Rewording them
# invalidates the measurements, which is why the seeded default's payload is
# byte-identical to the pre-asset map.


def turn_gate_questions(log: Any | None = None) -> dict[str, dict]:
    """The per-turn gate payload: this gate's enabled rows as a question map.

    ``{id: {"type", "instructions"[, "criteria"]}}`` — the ``/v1/systemone``
    shape. Read from the log-owned asset, sliced to :data:`PER_TURN_SECTION`
    (:func:`questions_for_section`), so the two W9.22 proposal rows the same bank
    now carries are NOT included — another gate consumes them. An absent/empty
    (or unreadable) asset yields the seeded bank
    (:data:`fabulist.decision_queries.DEFAULT_QUESTIONS`), whose slop rows derive
    from the canonical artifact list (issue #50); a session that stored a bank
    keeps asking its own rows. Disabled rows are omitted. ``log=None`` asks the
    seeded bank.
    """
    return question_payloads(questions_for_section(log, PER_TURN_SECTION))


def turn_gate_state(user_msg: str, prose: str, scene_line: str = "") -> str:
    """The decision model's view for the per-turn gate.

    The validated re-spike posted the RAW fixture prose as the state for both
    the slop map and the movement question — no scene line, no user message.
    Match that exactly: adding a scene line (as the genre question does) would
    change the measured input and invalidate the numbers. ``user_msg`` and
    ``scene_line`` are accepted for call-site symmetry only.
    """
    return prose


#: Per-request timeout (seconds). A WARM decision route answers in one forward
#: pass (~10-17ms in the spike), and 30s is NOT about that: the sidecar's compose
#: healthcheck proves the ROUTER listens, not that a model is LOADED, so the
#: first call after a sidecar (re)start pays the on-disk model load. Measured on
#: a freshly booted deploy: the first call raised ``ReadTimeout`` at 5.0039s under
#: the old 5s bound and the immediately following call succeeded in 1.55s. 30s
#: absorbs a cold load while still bounding a genuinely hung call (an accepted
#: trade: a hung decision call may block a turn for up to 30s, after which it
#: returns ``None`` like every other failure).
DEFAULT_TIMEOUT = 30.0

#: The route path (relative to the connection's ``/v1`` base).
_ROUTE = "/systemone"


def _error_text(exc: Exception) -> str:
    """Human-readable failure text for one observer record.

    ``str(exc)`` is empty for a bare ``HTTPStatusError`` subclass in some httpx
    versions, so fall back to the class name rather than logging a blank error.
    """
    text = str(exc).strip()
    return text or type(exc).__name__


def _as_token(value: Any) -> int | None:
    """Coerce one ``usage`` token count to ``int``, or ``None`` when unusable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _usage_tokens(usage: Any) -> tuple[int | None, int | None]:
    """Pull ``(input, output)`` token counts from a response ``usage`` block.

    Accepts both the OpenAI-compatible ``prompt_tokens``/``completion_tokens``
    names and the ``input_tokens``/``output_tokens`` aliases; a missing or
    malformed block yields ``(None, None)`` (the route emits no usage today).
    """
    if not isinstance(usage, dict):
        return None, None
    return (
        _as_token(usage.get("prompt_tokens", usage.get("input_tokens"))),
        _as_token(usage.get("completion_tokens", usage.get("output_tokens"))),
    )


def _systemone_url(base_url: str) -> str:
    """``base_url`` (with or without a trailing ``/v1``) -> the systemone URL.

    The registry convention ends ``base_url`` with ``/v1`` (e.g.
    ``http://127.0.0.1:4445/v1``), but a bare host is accepted too.
    """
    base = base_url.rstrip("/")
    if base.endswith("/v1"):
        return base + _ROUTE
    return base + "/v1" + _ROUTE


class DecisionCaller:
    """Client for llama.cpp's ``/v1/systemone`` decision route.

    Constructed with any object exposing ``connection_for(name) -> connection``
    where the connection carries ``base_url`` and ``model`` (the project's
    :class:`fabulist.connections.registry.Registry` qualifies). Construction
    never raises: a missing binding or an unusable connection leaves the caller
    disabled and every method returns ``None``.

    ``client`` is a test seam — inject an ``httpx.AsyncClient`` (e.g. backed by
    ``httpx.MockTransport``). When omitted, a short-lived client is created per
    call with :data:`DEFAULT_TIMEOUT`. An injected client is never closed here.

    ``observer`` is an optional plain callable invoked once per :meth:`ask`
    attempt (success OR failure) with one plain ``dict`` describing the call —
    the seam that makes an otherwise-invisible decision call observable. It is
    never awaited/accepted as fatal: a raising observer is swallowed. A disabled
    caller (no binding) makes no call and therefore notifies no observer.
    """

    def __init__(
        self,
        registry: Any,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        client: httpx.AsyncClient | None = None,
        observer: Callable[[dict[str, Any]], Any] | None = None,
    ):
        self._timeout = timeout
        self._client = client
        self._observer = observer
        self._url: str | None = None
        self._model: str | None = None
        try:
            connection = registry.connection_for(_AGENT)
        except Exception:  # noqa: BLE001 - any resolution failure disables the caller
            return
        base_url = getattr(connection, "base_url", None)
        model = getattr(connection, "model", None)
        if not base_url or not model:
            return
        self._url = _systemone_url(str(base_url))
        self._model = str(model)

    @property
    def enabled(self) -> bool:
        """True when a usable ``decision`` binding resolved at construction."""
        return self._url is not None and self._model is not None

    async def ask(
        self, state: str, questions: dict[str, Any]
    ) -> dict[str, Any] | None:
        """POST one decision request; return the ``answers`` mapping or ``None``.

        ``questions`` is the ``/v1/systemone`` question map
        (``{id: {"type": "noul"|"choice"|"score", "instructions": …,
        "criteria": …}}``). Returns ``None`` on a disabled caller or on ANY
        error/timeout/ill-shaped response.
        """
        if not self.enabled:
            return None
        body = {"model": self._model, "state": state, "questions": questions}
        started = time.monotonic()
        answers: dict[str, Any] | None = None
        error: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        try:
            if self._client is not None:
                response = await self._client.post(self._url, json=body)
            else:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.post(self._url, json=body)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                input_tokens, output_tokens = _usage_tokens(data.get("usage"))
                candidate = data.get("answers")
                if isinstance(candidate, dict):
                    answers = candidate
                else:
                    error = "response carried no 'answers' mapping"
            else:
                error = "response was not a JSON object"
        except Exception as exc:  # noqa: BLE001 - any error/timeout means "no answer"
            error = _error_text(exc)
        await self._notify(
            {
                "ok": answers is not None,
                "model": self._model,
                "questions": list(questions) if isinstance(questions, dict) else [],
                "answers": answers,
                "error": error,
                "latency_s": time.monotonic() - started,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        )
        return answers

    async def _notify(self, record: dict[str, Any]) -> None:
        """Offer one call record to the observer, swallowing any observer fault.

        The observer may be sync or async; an awaitable return value is awaited.
        A raise (sync or async) is ignored — measurement must never break a
        turn, matching this module's "never fatal" contract.
        """
        observer = self._observer
        if observer is None:
            return
        try:
            result = observer(record)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - an observer must never break a call
            pass

    async def verdict(self, state: str, instructions: str) -> float | None:
        """P(true) for one ``noul`` question; ``None`` on any error/timeout.

        The question id is fixed (``"verdict"``) — one question, one probability.

        Legacy: the removed critic SKIP gate used this. Kept as the minimal
        single-question seam; the wired gate now uses :meth:`ask` with
        :func:`turn_gate_questions`.
        """
        answers = await self.ask(
            state, {"verdict": {"type": "noul", "instructions": instructions}}
        )
        if not answers:
            return None
        answer = answers.get("verdict")
        if not isinstance(answer, dict):
            return None
        probability = answer.get("noul")
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            return None
        return float(probability)

    async def classify_genre(self, state: str) -> str | None:
        """The scene's dominant genre, or ``None``.

        Returns ``None`` on a disabled caller, any error/timeout, the ``other``
        class, or a winner below :data:`GENRE_MIN_CONFIDENCE` — the caller writes
        a fact only from a confident, usable answer.
        """
        answers = await self.ask(state, {"genre": GENRE_QUESTION})
        if not answers:
            return None
        answer = answers.get("genre")
        if not isinstance(answer, dict):
            return None
        choice = answer.get("choice")
        if not isinstance(choice, str) or choice == "other":
            return None
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, dict):
            return None
        probability = probabilities.get(choice)
        if isinstance(probability, bool) or not isinstance(probability, (int, float)):
            return None
        return choice if float(probability) >= GENRE_MIN_CONFIDENCE else None
