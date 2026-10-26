"""Bounded retry for model calls: two failure classes, ONE mechanism (issue #82).

Only the NPC lanes retried (issue #76) and a parse failure got no retry anywhere:
one malformed structured plan reply dropped the plan and the NEXT turn paid a
full planning bootstrap. This module is the one reusable bounded-retry
mechanism; every other seam rides it instead of growing its own loop.

Two failure CLASSES (`PARSE`, `PROVIDER`) live at two different layers, which is
why this is a helper rather than a single call site:

* ``PARSE`` — a pre-commit stage's model reply arrived but is unusable: empty,
  unparseable, or the wrong shape for the stage's schema
  (:class:`StructuredParseFailure`). The TRANSPORT above this layer succeeded —
  the HTTP call returned text — so only the seam that owns CALL **and** PARSE
  together can see it (``agents.loop.run_structured_call``).
* ``PROVIDER`` — the provider refused or never answered: any 4xx/5xx status
  (400 and content-filtered 1301 included, per the owner's #76 ruling), a
  connection/transport failure, an SDK timeout. Visible at the client, so the
  caller seam covers every call (``connections.client.call_with_retry``), loop
  or one-shot.

A layer retries only the classes it can SEE (``run_with_retry``'s ``classes``),
and a scope recorded while an attempt runs tells the inner layer that an OUTER
mechanism already owns a class — so one provider failure costs at most
:data:`DEFAULT_MAX_ATTEMPTS` real calls, never the product of two loops. The
scope is a ``ContextVar``, so it holds across ``asyncio.to_thread`` too.

Known unrecoverables skip the retry on the FIRST attempt: :data:`UNRECOVERABLE_CLASSES`
is the one named, extensible set (one line to extend), seeded with the classes
the code already treated as non-retryable. Extending it must never be an inline
``if`` at a call site.

On exhaustion the mechanism raises :class:`RetryExhausted` (carrying every
attempt's status and a TRUNCATED raw payload, so a deterministic failure is
diagnosable from the log) FROM the last failure — the classification chain walk
in :mod:`fabulist.graph.provider_errors` still finds the provider status through
the ``__cause__``. The per-stage EXHAUSTION POLICY is unchanged and stays with
the stage: lanes degrade and the turn commits; the planner and the narrator
abort the turn pre-commit. This module generalizes the retry, never the policy.

Layering note: this module imports the standard library only. ``connections`` is
the LOWER layer (``graph.context_monitor`` imports ``connections.client``), so
the shared classifier lives here and :mod:`fabulist.graph.provider_errors`
reads it — never the other way round.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterator

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_MAX_ATTEMPTS",
    "OTHER",
    "PARSE",
    "PAYLOAD_MAX_CHARS",
    "PROVIDER",
    "PROVIDER_CLASS_MARKERS",
    "RETRYABLE_CLASSES",
    "RETRY_BACKOFF_SECONDS",
    "AttemptRecord",
    "RetryExhausted",
    "StructuredParseFailure",
    "UNRECOVERABLE",
    "UNRECOVERABLE_CLASSES",
    "classify_failure",
    "failure_payload",
    "failure_status",
    "is_provider_failure",
    "owned_classes",
    "run_with_retry",
    "run_with_retry_sync",
]

#: Total attempts per model call (the first try plus up to two retries) — the
#: issue #76 lane precedent, now the default everywhere.
DEFAULT_MAX_ATTEMPTS = 3

#: Fixed pause between attempts (seconds). Deliberately small: the lane pool is
#: re-acquired per attempt, so a retrying call never holds a pool slot while it
#: backs off, and a healthy sibling sharing the pool is never stalled.
RETRY_BACKOFF_SECONDS = 0.2

#: Cap on the raw capture ONE failed attempt carries (a few hundred chars): the
#: excerpt is a diagnostic, never a stored payload.
PAYLOAD_MAX_CHARS = 400

#: A pre-commit stage's reply arrived and could not be parsed (class ``PARSE``).
PARSE = "parse"
#: The provider refused or never answered (class ``PROVIDER``).
PROVIDER = "provider"
#: Known unrecoverable: a retry cannot help (see :data:`UNRECOVERABLE_CLASSES`).
UNRECOVERABLE = "unrecoverable"
#: No class at all — a shape/quality failure this mechanism does not retry, or a
#: deterministic bug downstream of the call. Contained by the stage as today.
OTHER = "other"

#: The classes a seam may declare it retries. A layer only ever sees its own.
RETRYABLE_CLASSES: frozenset[str] = frozenset({PARSE, PROVIDER})

#: THE unrecoverable set (issue #82 item 3): one named, extensible place. A
#: failure whose class (or or any class in its cause chain) matches a marker here
#: is NOT retried — the stage's own containment runs on the first attempt, so a
#: deterministic failure never costs three identical calls or three log lines.
#:
#: Seeded with what the code already treats as non-retryable:
#:
#: * ``laneshapeerror`` — a parsed lane reply that failed the per-agent shape
#:   check (``graph.nodes._LaneShapeError``). Retrying it unchanged cannot help;
#:   the #76 lane loop skipped it on the first attempt and still does.
#: * ``systemmessageordererror`` — a request the wire REFUSES (a non-leading
#:   ``system`` message, issues #67/#68). The same bytes fail identically, so a
#:   retry only triples the wait before the abort.
#: * ``modelcalldeadlineexceeded`` — the connection's wall-clock ``call_timeout_s``
#:   already fired. Retrying inside the SAME window cannot help, and three
#:   900s deadlines would be 45 minutes of a turn.
#: * ``connectionconfigerror`` — a misconfigured connection (an unset
#:   ``api_key_env``, an unknown agent slot). Configuration does not heal itself
#:   between attempts.
#:
#: The owner states this list is incomplete and will grow: extending it is ONE
#: line here, and nothing else in the tree may re-decide it.
UNRECOVERABLE_CLASSES: frozenset[str] = frozenset(
    {
        "laneshapeerror",
        "systemmessageordererror",
        "modelcalldeadlineexceeded",
        "connectionconfigerror",
    }
)

#: Class-name markers of a provider/transport failure, checked over the MRO and
#: along the cause chain. Duck-typed on purpose (the ``agents.loop.
#: is_transport_timeout`` rule): ``openai`` and ``httpx`` name these classes
#: differently, and this module imports neither. Read by
#: :mod:`fabulist.graph.provider_errors` as well, so the two layers cannot drift.
PROVIDER_CLASS_MARKERS: tuple[str, ...] = (
    "apierror",
    "apistatus",
    "apiconnection",
    "apitimeout",
    "ratelimit",
    "httperror",
    "httpstatus",
    "transport",
    "connecterror",
    "readtimeout",
    "writetimeout",
    "pooltimeout",
    "remoteprotocol",
)


class StructuredParseFailure(ValueError):
    """A stage's model reply arrived but does not parse into its payload (PARSE).

    ``raw`` is the text the model actually returned (content, or the tool-call
    argument JSON when that is what arrived), so a failure record can carry a
    TRUNCATED copy: a deterministic malformed reply must be diagnosable from the
    log without a re-run. A ``ValueError`` subclass on purpose — the existing
    containers keep catching what they always caught.
    """

    def __init__(self, reason: str, *, raw: str = "") -> None:
        super().__init__(reason)
        #: The model's own text (unclipped; records clip it via failure_payload).
        self.raw = raw


@dataclass(frozen=True)
class AttemptRecord:
    """What ONE failed attempt leaves behind (issue #82 item 4).

    ``status`` is the provider's HTTP status when the failure carried one;
    ``payload`` is the TRUNCATED raw payload (the provider's own sentence, or the
    model's rejected reply for a parse failure); ``error`` is the one-line
    rendering the stage's own trace would show.
    """

    attempt: int
    failure_class: str
    status: int | None
    payload: str
    error: str

    def as_payload(self) -> dict[str, Any]:
        """The JSON-safe row a failure event carries for this attempt."""
        row: dict[str, Any] = {
            "attempt": self.attempt,
            "class": self.failure_class,
            "error": self.error,
        }
        if self.status is not None:
            row["status"] = self.status
        if self.payload:
            row["payload"] = self.payload
        return row


class RetryExhausted(RuntimeError):
    """Every attempt of one model call failed (``records`` explains each one).

    Raised FROM the last failure, so the provider status the abort/containment
    rules read is still one ``__cause__`` hop away. ``str()`` is the last
    failure's own text, which keeps every existing ``"error"`` row byte-identical.
    """

    def __init__(
        self, stage: str, records: "list[AttemptRecord]", last: BaseException
    ) -> None:
        super().__init__(str(last))
        #: The stage label the attempts were made for (``"Planning"``, ...).
        self.stage = stage
        #: One record per failed attempt, in attempt order.
        self.records = tuple(records)
        #: The final failure, already the exception's ``__cause__``.
        self.last_error = last


# -- classification ----------------------------------------------------------


def _chain(exc: BaseException) -> Iterator[BaseException]:
    """``exc`` then its cause/context chain, each visited once."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _status_of(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 100 <= value <= 599 else None


def failure_status(exc: BaseException) -> int | None:
    """The provider's HTTP status for ``exc``, or ``None`` when it carried none.

    Three spellings, all duck-typed: ``status_code`` (openai's ``APIStatusError``,
    httpx responses), ``status`` (the older clients), and ``exc.response.
    status_code`` (an ``httpx.HTTPStatusError``).
    """
    for current in _chain(exc):
        for name in ("status_code", "status"):
            status = _status_of(getattr(current, name, None))
            if status is not None:
                return status
        status = _status_of(
            getattr(getattr(current, "response", None), "status_code", None)
        )
        if status is not None:
            return status
    return None


def is_provider_failure(exc: BaseException) -> bool:
    """True when ``exc`` is a transport/provider class rather than content noise.

    The reading :func:`fabulist.graph.provider_errors.phase_failure` has always
    made, kept here so the retry mechanism and the abort policy cannot disagree:
    a builtin ``ConnectionError``/``TimeoutError``, a ``transport = True`` marker
    (the deadline, the request-shape guard, a bootstrap timeout), or a class name
    carrying one of :data:`PROVIDER_CLASS_MARKERS`.
    """
    for current in _chain(exc):
        if isinstance(current, (ConnectionError, TimeoutError)):
            return True
        if getattr(current, "transport", False) is True:
            return True
        names = [klass.__name__.lower() for klass in type(current).__mro__]
        if any(marker in name for name in names for marker in PROVIDER_CLASS_MARKERS):
            return True
    return False


def _is_unrecoverable(exc: BaseException) -> bool:
    """True when any class in ``exc``'s chain is in the unrecoverable set."""
    for current in _chain(exc):
        names = [klass.__name__.lower() for klass in type(current).__mro__]
        if any(marker in name for name in names for marker in UNRECOVERABLE_CLASSES):
            return True
    return False


def classify_failure(exc: BaseException) -> str:
    """Which class ``exc`` belongs to (``UNRECOVERABLE``, ``PARSE``, ``PROVIDER``, ``OTHER``).

    The unrecoverable set is checked FIRST: it exists to override a class that
    would otherwise be retried (a request the wire refuses carries
    ``transport = True``, so it would read as a provider failure).
    """
    if _is_unrecoverable(exc):
        return UNRECOVERABLE
    for current in _chain(exc):
        if isinstance(current, StructuredParseFailure):
            return PARSE
    if is_provider_failure(exc):
        return PROVIDER
    return OTHER


def _clip(text: str, limit: int) -> str:
    """One line, capped at ``limit`` (the payload capture, never a body dump)."""
    flat = " ".join(str(text).split())
    if len(flat) <= limit:
        return flat
    return flat[:limit].rstrip() + "…"


def _body_sentence(body: Any) -> str:
    """The human sentence out of a provider error body (dict or JSON-ish text)."""
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    for candidate in (error, body.get("message"), body.get("detail")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate
        if isinstance(candidate, dict):
            inner = candidate.get("message")
            if isinstance(inner, str) and inner.strip():
                return inner
    return ""


def failure_payload(exc: BaseException, *, limit: int = PAYLOAD_MAX_CHARS) -> str:
    """A TRUNCATED raw payload for ``exc``: the model's reply, or the provider's words.

    A parse failure carries the model's own (rejected) text — the fact a reader
    needs to see WHY it did not parse. Everything else carries the provider's
    error sentence when it had one, and the rendered exception text otherwise.
    """
    for current in _chain(exc):
        if isinstance(current, StructuredParseFailure):
            raw = getattr(current, "raw", "") or ""
            if raw.strip():
                return _clip(raw, limit)
    for current in _chain(exc):
        text = _body_sentence(getattr(current, "body", None))
        if text.strip():
            return _clip(text, limit)
        message = getattr(current, "message", None)
        if isinstance(message, str) and message.strip():
            return _clip(message, limit)
    return _clip(str(exc), limit)


def _record(attempt: int, exc: BaseException, failure_class: str) -> AttemptRecord:
    return AttemptRecord(
        attempt=attempt,
        failure_class=failure_class,
        status=failure_status(exc),
        payload=failure_payload(exc),
        error=_clip(f"{type(exc).__name__}: {exc}", PAYLOAD_MAX_CHARS),
    )


# -- the retry scope (one provider failure costs ONE loop, never two) --------


#: The retry classes an OUTER mechanism owns while its attempt runs. A seam that
#: finds its own class in here runs the attempt ONCE: an outer layer is already
#: retrying it, and two loops would multiply the attempts (3 x 3 = 9).
_OWNED_CLASSES: ContextVar[frozenset[str]] = ContextVar(
    "fabulist_retry_owned_classes", default=frozenset()
)


def owned_classes() -> frozenset[str]:
    """The classes an enclosing retry mechanism already owns (empty outside one)."""
    return _OWNED_CLASSES.get()


@contextmanager
def _scope(classes: frozenset[str]) -> Iterator[None]:
    token = _OWNED_CLASSES.set(_OWNED_CLASSES.get() | classes)
    try:
        yield
    finally:
        _OWNED_CLASSES.reset(token)


# -- the mechanism -----------------------------------------------------------


def _report(
    stage: str,
    record: AttemptRecord,
    exc: BaseException,
    max_attempts: int,
    on_attempt_failure: Callable[[AttemptRecord, BaseException], None] | None,
) -> None:
    """Record ONE failed attempt in the log (each attempt, not just the last)."""
    if on_attempt_failure is not None:
        on_attempt_failure(record, exc)
        return
    logger.warning(
        "%s attempt %d/%d failed (%s%s): %s | payload: %s",
        stage,
        record.attempt,
        max_attempts,
        record.failure_class,
        "" if record.status is None else f" {record.status}",
        record.error,
        record.payload,
    )


async def run_with_retry(
    attempt: Callable[[], Awaitable[Any]],
    *,
    stage: str,
    classes: frozenset[str] = RETRYABLE_CLASSES,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_s: float = RETRY_BACKOFF_SECONDS,
    on_attempt_failure: Callable[[AttemptRecord, BaseException], None] | None = None,
) -> Any:
    """Run ONE model call (or call+parse attempt) under the bounded retry.

    ``attempt`` performs exactly one try — call the model, and PARSE its reply
    when the seam owns both (the parse-retry class lives above the transport).
    ``classes`` is what THIS seam retries; a failure of any other class (an
    unrecoverable, a shape/quality failure, a bug) is re-raised untouched, so
    every existing containment path keeps its exception identity.

    On exhaustion :class:`RetryExhausted` is raised from the last failure with
    one :class:`AttemptRecord` per failed attempt. The STAGE still owns what
    happens next: degrade, or abort the turn.
    """
    attempts = max(1, int(max_attempts))
    if attempts == 1:
        with _scope(frozenset(classes)):
            return await attempt()

    records: list[AttemptRecord] = []
    for number in range(1, attempts + 1):
        try:
            with _scope(frozenset(classes)):
                return await attempt()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — classified below, never swallowed
            failure_class = classify_failure(exc)
            if failure_class not in classes:
                raise
            record = _record(number, exc, failure_class)
            records.append(record)
            _report(stage, record, exc, attempts, on_attempt_failure)
            if number >= attempts:
                raise RetryExhausted(stage, records, exc) from exc
            await asyncio.sleep(backoff_s)
    raise AssertionError("unreachable: the retry loop always returns or raises")


def run_with_retry_sync(
    attempt: Callable[[], Any],
    *,
    stage: str,
    classes: frozenset[str] = RETRYABLE_CLASSES,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_s: float = RETRY_BACKOFF_SECONDS,
    on_attempt_failure: Callable[[AttemptRecord, BaseException], None] | None = None,
) -> Any:
    """The blocking twin of :func:`run_with_retry` (the sync one-shot seams).

    The extractor's one-shot client is synchronous; its caller runs it on a
    worker thread, so backing off here never blocks the event loop.
    """
    attempts = max(1, int(max_attempts))
    if attempts == 1:
        with _scope(frozenset(classes)):
            return attempt()

    records: list[AttemptRecord] = []
    for number in range(1, attempts + 1):
        try:
            with _scope(frozenset(classes)):
                return attempt()
        except Exception as exc:  # noqa: BLE001 — classified below, never swallowed
            failure_class = classify_failure(exc)
            if failure_class not in classes:
                raise
            record = _record(number, exc, failure_class)
            records.append(record)
            _report(stage, record, exc, attempts, on_attempt_failure)
            if number >= attempts:
                raise RetryExhausted(stage, records, exc) from exc
            time.sleep(backoff_s)
    raise AssertionError("unreachable: the retry loop always returns or raises")
