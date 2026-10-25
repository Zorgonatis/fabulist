"""Auto-pin sidecar connections at engine boot (issue #36).

The out-of-the-box goal of the default compose deploy is that the three
auxiliary roles work with ZERO manual configuration: memory embeddings
(``embedder``), recall reranking (``reranker``) and the native decision model
(``decision``). The narrator/completion model is deliberately NOT this feature's
business — it is a user choice, never guessed.

The trigger is explicit, never a probe: only when ``FABULIST_SIDECAR_URL`` is
set (the compose file sets ``http://fabulist-models:8080/v1`` inside the network)
does the engine ask the sidecar which model ids it serves. A role is pinned only
when BOTH hold:

* its AGENT SLOT is empty (no authored ``embedder``/``embedding``, ``reranker``
  or ``decision`` key resolves) — a slot that already points somewhere is the
  user's deliberate state and is never touched, and a slot naming a connection
  that does not exist is left alone too (the engine does not fight the user, and
  it does not "repair" a half-edited document); and
* its model id (``embeddinggemma-2`` / ``qwen3-reranker`` / ``d1-3b``) is in the
  sidecar's ``/v1/models`` answer.

Every pin is written through the ordinary connection store — the same
:meth:`~fabulist.connections.registry.RegistryStore.add_connection` +
:meth:`~fabulist.connections.registry.RegistryStore.bind_agent` discipline a
hand-made entry goes through (validated as a whole document, atomically written,
hot-reloadable, drawer-visible). There is no raw file write here. Each created
entry also carries the context window its model's ``ctx-size`` declares in the
shipped ``deploy/sidecar/models.ini`` (:data:`SIDECAR_CONTEXT_WINDOWS`, issue
#38): a fresh deployment's drawer shows a real number, not "unknown".

Stickiness: pins only fill EMPTY slots. Deleting a pin stays deleted, and a
reboot never re-adds a role the user has re-bound. A partial sidecar pins what
it can and logs ONE notice naming what was skipped; an unreachable sidecar pins
nothing and logs ONE notice. Nothing in this module can raise into boot — a
degraded sidecar is a notice, not a startup failure.

Boot race (compose): ``docker compose up`` can start the engine and the sidecar
TOGETHER, so the boot probe can lose the race — the models host may not resolve
yet (``ConnectError: [Errno -2] Name or service not known``), the sidecar may
still be crash-looping its device guard, or a first boot's Vulkan init and image
pull may simply be slower than the engine. The default compose file now also
orders the engine behind a sidecar healthcheck (``depends_on: ...
condition: service_healthy``, probing ``/v1/models`` — an endpoint the router
answers before ANY model is loaded, so the check downloads nothing). That
ordering is not sufficient on its own: a sidecar can be recreated/restarted
behind a running engine, and the host-network variant points the engine at a
published port instead of a compose service.

So an UNREACHABLE boot probe arms a BOUNDED REPEATING re-probe
(:func:`schedule_autopin_retry`): the same probe + fill-empty-slots pin, every
:data:`RETRY_DELAY_S` seconds, at most :data:`RETRY_MAX_ATTEMPTS` times (24 ×
25 s ≈ a 10 minute window), stopping at the FIRST probe that ANSWERS. The pin is
idempotent (a rerun on an already pinned store writes nothing), so a sidecar
that came up — or a user who bound a role meanwhile — simply ends the schedule
without writing. A boot probe that ANSWERED (or an unset env) arms nothing and
behaves exactly as a single-shot boot. The schedule is fire-and-forget — boot
never waits for it — and its task is held in a module-level strong-ref set and
cancelled by loop shutdown, like every other background task in the engine.

Logging discipline (ONE convention, chosen here): ONE warning PER FAILED
ATTEMPT — the probe's own notice, unchanged from the boot path — plus exactly
ONE final warning when the attempt budget is exhausted and the schedule gives
up; a successful attempt logs the ordinary ``info`` pin line. Attempts are
never silent and never double-log.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

__all__ = [
    "CONNECTION_TIMEOUT_S",
    "PROBE_TIMEOUT_S",
    "RETRY_DELAY_S",
    "RETRY_MAX_ATTEMPTS",
    "SIDECAR_CONTEXT_WINDOWS",
    "SIDECAR_ROLES",
    "SIDECAR_URL_ENV",
    "SidecarPin",
    "autopin_sidecar",
    "fetch_served_models",
    "models_url",
    "schedule_autopin_retry",
]

logger = logging.getLogger(__name__)

#: The one env var that turns this feature on. Unset/empty = no probe, no pin.
SIDECAR_URL_ENV = "FABULIST_SIDECAR_URL"

#: How long the boot-time ``/v1/models`` probe may take. Deliberately short: a
#: sidecar that is still loading its router must not hold the engine's boot
#: open, and an unreachable one is exactly the case the notice exists for.
PROBE_TIMEOUT_S = 3.0

#: Per-connection read timeout written on a pinned entry. The sidecar's first
#: request loads a model from disk, so the generation bound has to be generous
#: (README's manual snippet uses the same 120 s).
CONNECTION_TIMEOUT_S = 120.0

#: How long the bounded retry schedule waits between deferred re-probes after
#: an unreachable boot probe. Long enough for container DNS/service discovery to
#: settle and for a sidecar restarting its device guard to come back; short
#: enough that a first turn does not have to wait long for recall. Read at
#: schedule time (a test overrides it).
RETRY_DELAY_S = 25.0

#: How many deferred re-probes the bounded schedule runs before giving up.
#: 24 × 25 s ≈ a 10 minute window from boot: enough for a first boot's Vulkan
#: init plus the HuggingFace weight pull a first request triggers on a slow
#: line. After the budget the schedule STOPS with one final warning instead of
#: probing forever. Read at schedule time (a test overrides it).
RETRY_MAX_ATTEMPTS = 24

#: Strong references to the in-flight retry schedules. ``asyncio`` keeps only
#: weak references to tasks, so an unreferenced one can be garbage-collected
#: mid-flight (silent: the notice would never log) — the same discipline as the
#: engine's other fire-and-forget tasks (``api/app._BACKGROUND_TASKS``).
_RETRY_TASKS: set[asyncio.Task[Any]] = set()

#: Role slot -> (connection name, served model id), in pin order. The slot names
#: are the ones ``connection_for`` resolves: ``embedder``, ``reranker`` and
#: ``decision`` (the same keys the manual README snippet binds).
SIDECAR_ROLES: dict[str, tuple[str, str]] = {
    "embedder": ("sidecar-embed", "embeddinggemma-2"),
    "reranker": ("sidecar-rerank", "qwen3-reranker"),
    "decision": ("sidecar-decision", "d1-3b"),
}

#: Served model id -> its context window in tokens, written onto the pinned
#: entry (issue #38). This MIRRORS the shipped router preset
#: ``deploy/sidecar/models.ini`` — one ``ctx-size`` per section, and these are
#: not guesses: they are the values the deployment actually serves (the
#: reranker at 2048 is a measured sufficiency, see that file). The pin is
#: self-documenting for a fresh deploy: without it the drawer shows "unknown"
#: for a window the sidecar's own preset already declares.
#:
#: THE TWO CHANGE TOGETHER. Editing a ``ctx-size`` in ``models.ini`` means
#: editing the row here in the same change — ``tests/test_sidecar_autopin.py``
#: parses the ini and fails if the two disagree. A model absent here pins with
#: no ``context_window`` (the entry stays "unknown" rather than fabricated).
SIDECAR_CONTEXT_WINDOWS: dict[str, int] = {
    "embeddinggemma-2": 8192,
    "qwen3-reranker": 2048,
    "d1-3b": 8192,
}

#: A URL scheme prefix (``http://``, ``https://``, …) — mirrors the registry's
#: normalizer so a bare ``host:port`` probes the same URL the entry will store.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")

#: A base URL that already names a version root (``.../v1``, ``.../v4``).
_VERSION_RE = re.compile(r"/v\d+$")


@dataclass(frozen=True)
class SidecarPin:
    """What one boot-time auto-pin attempt did (also exposed on ``app.state``).

    ``pinned`` maps a role slot to the connection name it was bound to;
    ``skipped`` maps every unpinned role to the short reason (already bound,
    model not served, write refused). ``notice`` is the ONE human line that was
    logged when the sidecar was partial/unreachable — ``None`` on a clean run
    and on the no-env no-op. ``unreachable`` distinguishes "the sidecar did not
    answer" from "it answered and some roles did not apply".
    """

    url: str | None = None
    served: tuple[str, ...] = ()
    pinned: dict[str, str] = field(default_factory=dict)
    skipped: dict[str, str] = field(default_factory=dict)
    notice: str | None = None
    unreachable: bool = False


def schedule_autopin_retry(
    registry: Any,
    boot: SidecarPin,
    *,
    transport: Any = None,
    timeout: float = PROBE_TIMEOUT_S,
    delay: float | None = None,
    attempts: int | None = None,
    on_result: Callable[[SidecarPin], None] | None = None,
) -> asyncio.Task[SidecarPin | None] | None:
    """Arm the BOUNDED retry schedule after an UNREACHABLE boot probe (#36).

    ``boot`` is what :func:`autopin_sidecar` returned at boot. Nothing is
    scheduled unless it was a real unreachable attempt (env set, probe failed),
    so a served sidecar and the no-env no-op keep today's single-shot boot
    exactly. The task sleeps ``delay`` (default :data:`RETRY_DELAY_S`) between
    re-probes and re-runs the SAME :func:`autopin_sidecar` — same
    fill-empty-slots pin, same one-notice-per-attempt discipline — until a probe
    ANSWERS or the ``attempts`` budget (default :data:`RETRY_MAX_ATTEMPTS`) is
    spent. It is idempotent: if the sidecar came up (or a user pinned meanwhile)
    the first answering pass ends the schedule and writes nothing.

    Returns the task (or ``None`` when nothing was armed). Never raises: with
    no running event loop (a test calling ``build_app`` synchronously) there is
    simply no task, which keeps boot non-blocking and side-effect free. An
    ``attempts`` budget of zero (or less) arms nothing as well. ``on_result``
    receives each attempt's :class:`SidecarPin` on the loop thread (the wiring
    hook uses it to refresh ``app.state.sidecar_pin``).
    """
    if not boot.unreachable or not boot.url:
        return None
    try:
        asyncio.get_running_loop()
    except RuntimeError:  # no running loop (sync build_app) - no task, no raise
        logger.debug("sidecar auto-pin retry not scheduled: no running event loop")
        return None
    pause = RETRY_DELAY_S if delay is None else delay
    budget = RETRY_MAX_ATTEMPTS if attempts is None else attempts
    if budget <= 0:
        logger.debug("sidecar auto-pin retry not scheduled: attempt budget is %s", budget)
        return None
    task = asyncio.create_task(
        _retry_autopin(
            registry,
            boot.url,
            transport=transport,
            timeout=timeout,
            delay=pause,
            attempts=budget,
            on_result=on_result,
        )
    )
    _RETRY_TASKS.add(task)
    return task


def _report(on_result: Callable[[SidecarPin], None] | None, result: SidecarPin) -> None:
    """Hand one attempt's result to the wiring hook; never let it abort the loop."""
    if on_result is None:
        return
    try:
        on_result(result)
    except Exception:  # noqa: BLE001 - a diagnostic refresh never matters
        logger.warning("sidecar auto-pin retry callback failed", exc_info=True)


async def _retry_autopin(
    registry: Any,
    base: str,
    *,
    transport: Any,
    timeout: float,
    delay: float,
    attempts: int,
    on_result: Callable[[SidecarPin], None] | None,
) -> SidecarPin | None:
    """Re-probe every ``delay`` until one ANSWERS or ``attempts`` are spent.

    ONE warning per failed attempt comes from :func:`autopin_sidecar` itself; a
    spent budget adds exactly ONE final warning naming what was tried. The
    strong ref is dropped on every exit path (success, exhaustion, cancel).
    """
    current = asyncio.current_task()
    last: SidecarPin | None = None
    try:
        for _attempt in range(attempts):
            await asyncio.sleep(delay)
            last = autopin_sidecar(
                registry, url=base, transport=transport, timeout=timeout
            )
            _report(on_result, last)
            if not last.unreachable:
                return last
        logger.warning(
            "sidecar auto-pin at %s: still unreachable after %d re-probe(s) over "
            "~%.0f s — giving up; the roles stay unbound until the sidecar is up "
            "and the engine restarts (or bind them in the Connections drawer)",
            base,
            attempts,
            attempts * delay,
        )
        return last
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - housekeeping must never surface
        logger.warning(
            "sidecar auto-pin retry at %s failed: %s: %s", base, type(exc).__name__, exc
        )
        return None
    finally:
        if current is not None:
            _RETRY_TASKS.discard(current)


def _as_base(url: str) -> str:
    """Normalize a sidecar URL the way the registry will store it.

    A bare ``host:port`` gains ``http://`` and — ONLY when it names no path — the
    ``/v1`` root, so ``models_url`` and the stored connection entry agree about
    the URL the engine dials. An explicit path is preserved verbatim (a sidecar
    behind a proxy is not always mounted at ``/v1``).
    """
    base = url.strip().rstrip("/")
    if not base:
        raise ValueError("empty sidecar URL")
    if not _SCHEME_RE.match(base):
        base = "http://" + base
    parts = urlsplit(base)
    if not parts.netloc:
        raise ValueError(f"not a usable URL: {url!r}")
    if parts.path in ("", "/"):
        base = base + "/v1"
    return base


def models_url(url: str) -> str:
    """The OpenAI-compatible models URL for a sidecar base URL.

    ``FABULIST_SIDECAR_URL`` conventionally names the version root
    (``.../v1``); if it does not, address the OpenAI convention explicitly.
    """
    base = _as_base(url)
    return f"{base}/models" if _VERSION_RE.search(base) else f"{base}/v1/models"


def _extract_model_ids(payload: Any) -> tuple[str, ...]:
    """Pull model ids out of the common OpenAI-compatible shapes.

    Accepts ``{"data": [...]}`` (OpenAI/llama.cpp router), ``{"models": [...]}``
    (llama.cpp legacy) and a bare list; items may be strings or objects with
    ``id``/``name``/``model``. Order and first-occurrence dedupe are preserved.
    """
    items: Any = payload
    if isinstance(payload, dict):
        for key in ("data", "models"):
            if isinstance(payload.get(key), list):
                items = payload[key]
                break
        else:
            items = []
    if not isinstance(items, list):
        return ()
    ids: list[str] = []
    for item in items:
        if isinstance(item, str):
            model_id = item
        elif isinstance(item, dict):
            model_id = item.get("id") or item.get("name") or item.get("model")
        else:
            model_id = None
        if isinstance(model_id, str) and model_id and model_id not in ids:
            ids.append(model_id)
    return tuple(ids)


def fetch_served_models(
    url: str,
    *,
    timeout: float = PROBE_TIMEOUT_S,
    transport: Any = None,
) -> tuple[str, ...]:
    """GET the sidecar's model list; raise on any transport/HTTP failure.

    ``transport`` is the test seam (an ``httpx.BaseTransport`` such as
    ``httpx.MockTransport``); production leaves it ``None``. The caller
    (:func:`autopin_sidecar`) turns every exception into one boot notice, so this
    function is free to raise loudly.
    """
    import httpx

    with httpx.Client(timeout=timeout, transport=transport) as client:
        response = client.get(
            models_url(url), headers={"Accept": "application/json"}
        )
        response.raise_for_status()
        return _extract_model_ids(response.json())


def autopin_sidecar(
    registry: Any,
    *,
    url: str | None = None,
    transport: Any = None,
    timeout: float = PROBE_TIMEOUT_S,
) -> SidecarPin:
    """Pin served sidecar roles into EMPTY agent slots at boot (issue #36).

    ``registry`` is the live connections object (a
    :class:`~fabulist.connections.registry.RegistryStore` in production). Only a
    store with the write seams is written; a read-only registry logs one notice
    and pins nothing. ``url`` defaults to ``$FABULIST_SIDECAR_URL`` — read at
    call time so a container's environment (or a test's ``monkeypatch``) applies.

    NEVER raises: every failure path returns a :class:`SidecarPin` with the one
    notice it logged, so a broken sidecar can never break the engine's boot.
    """
    raw = url if url is not None else os.environ.get(SIDECAR_URL_ENV)
    base = (raw or "").strip()
    if not base:
        return SidecarPin()

    try:
        return _autopin(registry, base, transport=transport, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - auto-pin must never break boot
        notice = (
            f"sidecar auto-pin at {base} skipped: {type(exc).__name__}: {exc} — "
            "the engine continues with the configured connections"
        )[:400]
        logger.warning(notice)
        return SidecarPin(url=base, notice=notice, unreachable=True)


def _autopin(
    registry: Any,
    base: str,
    *,
    transport: Any,
    timeout: float,
) -> SidecarPin:
    """The body of :func:`autopin_sidecar` (see there for the contract)."""
    try:
        served = fetch_served_models(base, timeout=timeout, transport=transport)
    except Exception as exc:  # noqa: BLE001 - the sidecar is optional
        notice = (
            f"sidecar auto-pin at {base} skipped: the models endpoint is "
            f"unreachable ({type(exc).__name__}: {exc}) — no connections were "
            "pinned; the engine continues with the configured connections"
        )[:400]
        logger.warning(notice)
        return SidecarPin(url=base, notice=notice, unreachable=True)

    writable = hasattr(registry, "add_connection") and hasattr(registry, "bind_agent")
    target = _as_base(base)
    pinned: dict[str, str] = {}
    skipped: dict[str, str] = {}
    problems: list[str] = []

    for slot, (name, model) in SIDECAR_ROLES.items():
        # Fill-MISSING only: any authored slot (even one naming a connection the
        # document does not carry) is the user's state — leave it alone.
        bound = _resolve_slot(registry, slot)
        if bound is not None:
            skipped[slot] = f"already bound to {bound!r}"
            continue
        if model not in served:
            skipped[slot] = f"{model} not served"
            problems.append(f"{slot} ({model} not served)")
            continue
        if not writable:
            skipped[slot] = "connections store is read-only"
            problems.append(f"{slot} (connections store is read-only)")
            continue
        try:
            existing = _entry(registry, name)
            if existing is not None and (
                existing.model != model or existing.base_url != target
            ):
                skipped[slot] = (
                    f"connection {name!r} already exists with a different target; "
                    "left untouched"
                )
                problems.append(
                    f"{slot} ({name!r} already exists with a different target)"
                )
                continue
            if existing is None:
                entry: dict[str, Any] = {
                    "base_url": base,
                    "model": model,
                    "timeout": CONNECTION_TIMEOUT_S,
                }
                # The window the sidecar's own preset declares (issue #38): the
                # owner-declared context size, taken from the deployment rather
                # than probed (ADR-008 keeps it off the wire). Omitted when the
                # model is not in the table — never a fabricated default.
                window = SIDECAR_CONTEXT_WINDOWS.get(model)
                if window is not None:
                    entry["context_window"] = window
                registry.add_connection(name, entry)
            registry.bind_agent(slot, name)
        except Exception as exc:  # noqa: BLE001 - one role never stops the rest
            skipped[slot] = f"{type(exc).__name__}: {exc}"
            problems.append(f"{slot} ({name!r} could not be pinned: {type(exc).__name__})")
            continue
        pinned[slot] = name

    notice: str | None = None
    if problems:
        notice = (
            f"sidecar auto-pin at {base}: pinned "
            f"{', '.join(sorted(pinned)) if pinned else 'nothing'}; skipped "
            + "; ".join(problems)
        )[:400]
        logger.warning(notice)
    elif pinned:
        logger.info(
            "sidecar auto-pin at %s: pinned %s",
            base,
            ", ".join(f"{slot} -> {name}" for slot, name in pinned.items()),
        )
    return SidecarPin(
        url=base, served=served, pinned=pinned, skipped=skipped, notice=notice
    )


def _resolve_slot(registry: Any, slot: str) -> str | None:
    """The connection ``slot`` already resolves to, or ``None`` when empty."""
    from fabulist.connections.taxonomy import resolve_slot

    agents = getattr(registry, "agents", None)
    return resolve_slot(dict(agents or {}), slot)


def _entry(registry: Any, name: str) -> Any:
    """The parsed connection named ``name`` in the live snapshot, if any."""
    connections = getattr(registry, "connections", None) or {}
    return connections.get(name)
