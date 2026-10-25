"""Provider/transport failures at a pre-commit model seam, and the abort they earn.

A model call fails in one of two ways and they must not be contained alike:

* **content** — the provider ANSWERED and the answer is unusable: empty,
  unparseable, the wrong shape, or a tool call the schema refuses. The phase's
  job can be skipped without lying about the turn: the previous plan keeps
  steering, the lane is dropped, and ``plan_refresh_failed`` records why.
* **provider** — the provider refused or never answered: ANY 4xx or 5xx status,
  a connection or a transport failure. Nothing downstream can succeed either, so
  containing it does not save the turn — it only moves the failure to the NEXT
  phase, where the reader meets it without the cause (the live bug: a planner
  rate limit surfaced only when the narrator hit the same wall, an entire phase
  later, and the plan silently went stale in between). A 400 is the provider
  rejecting OUR request: that is the silent-stale-plan class this rule exists to
  end, not a mitigation for it.

:func:`phase_failure` classifies an exception against that rule and, for the
provider class, builds the :class:`PhaseCallError` a PRE-COMMIT node RAISES.
Raising is what aborts the turn: a node exception reaches the client as the
engine's one error frame (``graph/engine.py``), the same shape a narrator
failure takes — so the reader is told which phase fell over and why.

Scope — WHICH phases may abort:

* PRE-COMMIT (planner seed/refresh, the NPC lanes, the narrator draft): a
  provider failure is FATAL. The turn has not been kept yet, so aborting costs
  the reader nothing they had, and carrying on would hide the cause.
* POST-COMMIT (critic, the tracker's world-state pass, the HUD, attribution,
  the post-turn hooks): containment stays EXACTLY as it is, and those nodes do
  not call this module. Past the commit barrier the turn is already kept, so a
  housekeeping failure must never undo it (ADR-010) — it is logged and dropped.

(Historical note, so nobody re-carves an exception: a live DeepSeek turn hit a
400 whose body read "The ``reasoning_content`` in the thinking mode must be
passed back to the API" in the planner's seed phase. That was the engine's
reasoning round-trip defect, fixed separately in 7be5782 — a bug in flight must
not define the policy, or the policy outlives the bug.)
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "FATAL_STATUSES",
    "PhaseCallError",
    "failure_status",
    "phase_failure",
]

#: The statuses the message names in full (auth, missing model/route, a server
#: timeout, a conflict, a rate limit). They are NOT the classification: every
#: other 4xx/5xx status aborts too (see :func:`failure_status` and
#: :func:`phase_failure`) — a status on a raised call means the provider did not
#: serve us, which is fatal in a pre-commit phase however it is spelled.
FATAL_STATUSES = frozenset({400, 401, 403, 404, 408, 409, 422, 425, 429})

#: Class-name markers of a provider/transport failure, checked over the MRO and
#: along the cause chain. Duck-typed on purpose (the ``agents.loop.
#: is_transport_timeout`` rule): ``openai`` and ``httpx`` name these classes
#: differently, and this module imports neither.
_PROVIDER_CLASS_MARKERS = (
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

#: The provider's own words, capped: the frame carries a sentence, not a body.
_DETAIL_LIMIT = 300


class PhaseCallError(RuntimeError):
    """A pre-commit phase's model call failed at the provider/transport seam.

    Raised by the node that owns the phase, so the turn aborts and the client
    receives the engine's error frame naming the PHASE and the provider's own
    status/text — instead of the failure surfacing later, at a phase that merely
    inherited it.
    """

    def __init__(self, phase: str, detail: str, *, status: int | None = None) -> None:
        super().__init__(f"{phase} failed: {detail}")
        #: The phase label the message opens with ("Planning", "NPC lanes", ...).
        self.phase = phase
        #: The provider's HTTP status, when the failure carried one.
        self.status = status


def _chain(exc: BaseException):
    """``exc`` then its cause/context chain, each visited once.

    A client may wrap the original failure (``raise ... from exc``), so the
    status and the class markers are looked for along the whole chain.
    """
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
        status = _status_of(getattr(getattr(current, "response", None), "status_code", None))
        if status is not None:
            return status
    return None


def _is_provider_failure(exc: BaseException) -> bool:
    """True when ``exc`` is a transport/provider class rather than content noise."""
    for current in _chain(exc):
        if isinstance(current, (ConnectionError, TimeoutError)):
            return True
        # ``PlannerBootstrapError`` sets this when its loop was ended by a
        # transport timeout: the phase failed because no answer arrived.
        if getattr(current, "transport", False) is True:
            return True
        names = [klass.__name__.lower() for klass in type(current).__mro__]
        if any(
            marker in name for name in names for marker in _PROVIDER_CLASS_MARKERS
        ):
            return True
    return False


def _provider_text(exc: BaseException) -> str:
    """The provider's own message: its error body first, then the SDK's text."""
    for current in _chain(exc):
        body = getattr(current, "body", None)
        text = _body_text(body)
        if text:
            return text
        message = getattr(current, "message", None)
        if isinstance(message, str) and message.strip():
            return _clip(message)
        rendered = str(current).strip()
        if rendered:
            return _clip(rendered)
    return ""


def _body_text(body: Any) -> str:
    """The human sentence out of a provider error body (dict or JSON-ish)."""
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


def _retry_after(exc: BaseException) -> str:
    """The provider's ``Retry-After`` header, when the failure carried one."""
    for current in _chain(exc):
        headers = getattr(getattr(current, "response", None), "headers", None)
        value = None
        if headers is not None:
            try:
                value = headers.get("retry-after")
            except Exception:  # noqa: BLE001 - a header read is never worth a raise
                value = None
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _clip(text: str) -> str:
    """One line, no provider body dump, capped at :data:`_DETAIL_LIMIT`."""
    flat = " ".join(str(text).split())
    if len(flat) <= _DETAIL_LIMIT:
        return flat
    return flat[:_DETAIL_LIMIT].rstrip() + "…"


def phase_failure(
    phase: str, exc: BaseException, *, provider: str = ""
) -> PhaseCallError | None:
    """Classify a phase's failure: the abort to raise, or ``None`` to contain.

    ``None`` means the failure is content-class: the caller keeps its existing
    containment and its ``*_failed`` trace row.

    ``provider`` is the model/connection label the message names ("glm-4.6"), as
    the wiring resolves it; empty when unknown, and the message simply omits it.
    """
    status = failure_status(exc)
    if status is not None:
        # ANY status on a raised call: the provider refused or failed to serve
        # this request. Pre-commit that is fatal — a 400 is our request being
        # rejected, and the turn must not continue on a plan that never
        # refreshed (the silent failure this rule exists to end).
        return PhaseCallError(phase, _describe(exc, status, provider), status=status)
    if not _is_provider_failure(exc):
        return None
    return PhaseCallError(phase, _describe(exc, None, provider))


def _describe(exc: BaseException, status: int | None, provider: str) -> str:
    """``"429 from glm-4.6 — Rate limit reached… (retry after 20)"``.

    The head names the status (or the exception class when the failure carried
    none, e.g. a connection error), the provider label rides it when known, and
    the provider's own words follow so a rate limit and a 500 read differently.
    """
    head = str(status) if status is not None else type(exc).__name__
    if provider:
        head = f"{head} from {provider}"
    parts = [head]
    text = _provider_text(exc)
    if text:
        parts.append(f"— {text}")
    retry_after = _retry_after(exc)
    if retry_after:
        parts.append(f"(retry after {retry_after})")
    return " ".join(parts)
