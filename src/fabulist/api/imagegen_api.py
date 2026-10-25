"""Image-generation router (V1 image-gen lane).

Three endpoints over :mod:`fabulist.comfy` + the existing media path:

  GET  /api/imagegen/config                     -> {enabled, base_url, reachable}
  POST /api/imagegen/render                     -> {positive, negative, parse: json|labeled|fallback}
  POST /api/sessions/{sid}/imagegen/render      -> same, WITH session cast + recent scene injected
  POST /api/sessions/{sid}/imagegen             -> {asset_id, seq, unmapped, ...}

The generator agent (the ``imagegen`` binding, rebindable per agent like any
other) is the control plane between the user's request and the diffusion
prompts. ``POST /api/imagegen/render`` runs it: the definition's
``prompt_template`` is the agent's SYSTEM prompt and it synthesizes
positive/negative text the frontend shows and lets the user edit BEFORE submit;
the run endpoint uses the SAME shared render helper, and an explicit override
always wins.

The feature flag rides the EXISTING settings fold: ``comfy.enabled`` as a
``settings_changed`` feature flag on the reserved ``__settings__`` anchor
(default false), read through :func:`fabulist.api.settings.current_features`.
The base URL
is a plain string the boolean feature fold cannot carry, so it is read from an
injectable seam (``request.app.state.imagegen_base_url``) with the documented
default; the orchestrator may override it. Reachability (``/system_stats`` 2 s,
cached 60 s) gates ``enabled`` — the Composer shows the button only when the flag
is on AND ComfyUI answers.

Generation (POST):
  1. the generator agent (the ``imagegen`` binding, default ``your-model-id``,
     rebindable per agent like any other) renders ``request`` through the
     definition's ``prompt_template`` (its SYSTEM prompt) into positive and
     negative text — the SAME shared path the render route uses (one code path);
  2. apply the request's ``overrides`` (:func:`fabulist.comfy.apply_overrides`,
     which reports unmappable keys — never a broken run); an explicit
     ``positive`` override ALWAYS wins and skips the agent (the
     editable-before-submit contract);
  3. submit + poll ComfyUI, fetch the image bytes;
  4. store the image as a session ``asset`` (the existing media path) and append
     a CANON narrative row (``type="narrative"``, actor ``imagegen``) whose
     payload is ``{"prose": "", "dsl": "", "kind": "image",
     "image_asset_id": ..., "prompt": <effective positive>}``. The row carries
     no prose: ``history_from_log`` inlines its image into the narrator prompt
     as an ASSISTANT message, and the UI reads it as a scene image, not a turn.

Failures — disabled, unreachable, a render caller that is missing/unbound or
raises (the render route returns an ``{"error": {"stage": "render", ...}}``
payload; the run endpoint falls back to the documented static path), a 422 with
node_errors, a timeout, an empty output, a store error — are recorded as an
``imagegen_failed`` ``system`` row (mirroring the STT-failure convention) and
returned as an ``{"error": {...}}`` payload with HTTP 200. The turn pipeline is
never touched.

Wiring (orchestrator-owned)::

    from fabulist.api.imagegen_api import router
    app.include_router(router)

Reads the live event log from ``request.app.state.log`` (the app factory stores
it there), like :mod:`fabulist.api.attachments`.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import re
import sqlite3
import time
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from fabulist.comfy import (
    ComfyClient,
    ComfyError,
    ComfySubmitError,
    ComfyTimeout,
    apply_overrides,
    first_image,
)
from fabulist.media import store_image
from fabulist.media.images import is_image_narrative

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.comfy.workflow import OverrideResult
    from fabulist.log import Event, EventLog


router = APIRouter(tags=["imagegen"])

logger = logging.getLogger(__name__)

#: Default ComfyUI endpoint (the tuned systemd unit).
DEFAULT_BASE_URL = "http://127.0.0.1:8188"

#: The settings toggle id that turns the feature on (default false).
FLAG_KEY = "comfy.enabled"

#: ``system`` event kind carrying a generation failure (mirrors ``stt_failure``).
IMAGEGEN_FAILURE_EVENT = "imagegen_failed"

#: Cap on the error text carried by a failure row (mirrors the stt/media caps).
_FAILURE_ERROR_LIMIT = 800

#: How many prior turns (user text + narrative prose pairs) the session-scoped
#: render injects as RECENT SCENE context for the generator agent.
IMAGEGEN_RECENT_TURNS = 5

#: Reachability cache TTL (the plan's 60 s).
_REACH_TTL_S = 60.0

#: base_url -> (monotonic_ts, reachable). Engine-local, process RAM, per app.
_reach_cache: dict[str, tuple[float, bool]] = {}

#: Injectable clock (tests expire the cache without sleeping).
_clock = time.monotonic


class ImageGenBody(BaseModel):
    """Body of ``POST /api/sessions/{sid}/imagegen``.

    ``workflow`` is the ComfyUI API-format JSON inline; ``prompt_template`` is the
    user-supplied generator prompt (``{request}`` placeholder); ``overrides``
    prefills/edits seed / positive / negative / width / height before submit.
    """

    request: str
    # One of these two: an inline workflow (the original contract) OR the id of
    # a saved imagegen definition (the V2 registry) — the merge ruling: the
    # inline form always wins when both are present, and a saved definition
    # supplies the workflow + prompt_template it stores.
    workflow: dict | None = None
    imagegen_id: str | None = None
    prompt_template: str | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)


class RenderBody(BaseModel):
    """Body of ``POST /api/imagegen/render`` (the generator control plane).

    The frontend renders a request into positive/negative text BEFORE submit and
    lets the user edit the result; the edited values are then sent as explicit
    overrides to the run endpoint. ``prompt_template`` (explicit) wins over the
    saved definition's ``imagegen_id``.
    """

    request: str
    imagegen_id: str | None = None
    prompt_template: str | None = None


# -- settings seams ---------------------------------------------------------


def feature_enabled(log: "EventLog") -> bool:
    """``comfy.enabled`` from the settings feature fold; default false.

    Feature flags ride the settings anchor's ``settings_changed`` event under
    the ``features`` key (fabulist.api.settings.FEATURE_FLAGS) — never the
    directive catalogue, which the narrator renders.
    """
    from fabulist.api.settings import current_features

    return current_features(log).get("comfy.enabled", False)


def _base_url(request: Request) -> str:
    """The ComfyUI base URL (injectable via ``app.state.imagegen_base_url``)."""
    value = getattr(request.app.state, "imagegen_base_url", None)
    return str(value) if value else DEFAULT_BASE_URL


def _client_for(request: Request, base_url: str) -> ComfyClient:
    """Build the client (injectable via ``app.state.imagegen_client_factory``)."""
    factory = getattr(request.app.state, "imagegen_client_factory", None)
    if callable(factory):
        return factory(base_url)
    return ComfyClient(base_url)


async def _reachable(request: Request, base_url: str) -> bool:
    """``/system_stats`` reachability, cached for :data:`_REACH_TTL_S` seconds."""
    now = _clock()
    cached = _reach_cache.get(base_url)
    if cached is not None and now - cached[0] < _REACH_TTL_S:
        return cached[1]
    ok = await _client_for(request, base_url).reachable()
    _reach_cache[base_url] = (now, ok)
    return ok


# -- log helpers ------------------------------------------------------------


def _log_for(request: Request) -> "EventLog":
    """Return the app's event log, or 500 when the app was built without one."""
    log = getattr(request.app.state, "log", None)
    if log is None:
        raise HTTPException(status_code=500, detail="event log unavailable")
    return log


def _session_exists(log: "EventLog", session_id: str) -> bool:
    """True when the log knows ``session_id`` (mirrors ``api.attachments``)."""
    if log.get_events(session_id):
        return True
    conn = sqlite3.connect(log.path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    finally:
        conn.close()
    return row is not None


def _failure(
    log: "EventLog",
    session_id: str,
    stage: str,
    message: str,
    *,
    node_errors: Any = None,
) -> dict:
    """Append an ``imagegen_failed`` system row and build the error payload.

    Best-effort: recording the failure must never itself surface (the row is an
    audit aid; the payload is what the caller gets). ``seq`` is the failure row's
    seq when it landed, else ``None``.
    """
    payload: dict[str, Any] = {
        "event": IMAGEGEN_FAILURE_EVENT,
        "stage": stage,
        "error": message[:_FAILURE_ERROR_LIMIT],
    }
    if node_errors is not None:
        payload["node_errors"] = node_errors
    seq: int | None = None
    try:
        event = log.append_event(session_id, "system", "imagegen", payload)
        seq = event.seq
    except Exception:  # noqa: BLE001 - failure reporting is best-effort
        logger.exception("could not record imagegen failure for %s", session_id)
    error: dict[str, Any] = {"stage": stage, "message": message}
    if node_errors is not None:
        error["node_errors"] = node_errors
    return {"error": error, "seq": seq}


def _render_positive(template: str | None, request_text: str) -> str:
    """The pre-agent static substitution (the DEGRADED run-endpoint fallback).

    Used ONLY when the generator agent is unavailable. A plain substitution (not
    ``str.format``) so a template with other literal braces — common in prompt
    text — cannot raise. No template -> the request verbatim; a template without
    ``{request}`` -> the template verbatim.
    """
    if template is None:
        return request_text
    return template.replace("{request}", request_text)


# -- generator-agent rendering (the shared path for both routes) -------------

#: Fenced code block (```json ... ``` or plain ``` ... ```), anywhere in the text.
_FENCE_RE = re.compile(r"```[a-zA-Z0-9]*\s*\n?(.*?)\n?\s*```", re.DOTALL)

#: The ``POSITIVE:`` / ``NEGATIVE:`` labels (case-insensitive).
_LABEL_RE = re.compile(r"(?i)(positive|negative)\s*:")


class RenderUnavailable(RuntimeError):
    """The generator agent caller is missing/unbound (render cannot run)."""


def _strip_fence(text: str) -> str:
    """Unwrap the first fenced code block, or return the text unchanged."""
    match = _FENCE_RE.search(text)
    return match.group(1).strip() if match else text


def _try_json(text: str) -> tuple[str, str] | None:
    """Parse ``{"positive": ..., "negative": ...}`` (fenced blocks accepted)."""
    candidate = _strip_fence(text).strip()
    if not candidate.startswith("{"):
        return None
    try:
        obj = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict) or not isinstance(obj.get("positive"), str):
        return None
    negative = obj.get("negative")
    if not isinstance(negative, str):
        negative = "" if negative is None else str(negative)
    return obj["positive"], negative


def _try_labeled(text: str) -> tuple[str, str] | None:
    """Parse two ``POSITIVE:`` / ``NEGATIVE:`` blocks (negative optional)."""
    matches = list(_LABEL_RE.finditer(text))
    if not matches:
        return None
    blocks: dict[str, str] = {}
    for index, match in enumerate(matches):
        label = match.group(1).lower()
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        blocks[label] = text[start:end].strip()
    if "positive" not in blocks:
        return None
    return blocks["positive"], blocks.get("negative", "")


def parse_render(text: str) -> tuple[str, str, str]:
    """Parse a generator reply into ``(positive, negative, parse_path)``.

    Robust and boring, in order: a JSON object with string ``positive`` /
    ``negative`` fields (fenced ```json blocks accepted) -> ``"json"``; two
    labeled ``POSITIVE:`` / ``NEGATIVE:`` blocks, case-insensitive, negative
    optional -> ``"labeled"``; otherwise the whole trimmed text is the positive
    and the negative is empty -> ``"fallback"``. Never raises.
    """
    stripped = (text or "").strip()
    parsed = _try_json(stripped)
    if parsed is not None:
        return parsed[0], parsed[1], "json"
    parsed = _try_labeled(stripped)
    if parsed is not None:
        return parsed[0], parsed[1], "labeled"
    return stripped, "", "fallback"


def _caller_for(request: Request) -> Any:
    """The generator agent caller (injectable via ``imagegen_caller_factory``).

    Mirrors :func:`_client_for`: an injected
    ``app.state.imagegen_caller_factory`` wins (tests pass a fake), else the
    wiring-built ``app.state.imagegen_caller`` (``wiring.build_app``), else
    ``None`` (unbound — the caller reports it).
    """
    factory = getattr(request.app.state, "imagegen_caller_factory", None)
    if callable(factory):
        return factory("imagegen")
    return getattr(request.app.state, "imagegen_caller", None)


def _resolve_template(log: "EventLog", body: "RenderBody") -> str | None:
    """Resolve the generator SYSTEM template: explicit wins, else the definition.

    An explicit ``prompt_template`` (even empty) always wins. Otherwise the saved
    definition named by ``imagegen_id`` supplies its template; an unknown id is a
    precise ``404``. With neither, ``None`` (the caller gets an empty system).
    """
    if body.prompt_template is not None:
        return body.prompt_template
    if body.imagegen_id:
        from fabulist.imagegen.registry import imagegen_by_id

        definition = imagegen_by_id(log, body.imagegen_id)
        if definition is None:
            raise HTTPException(status_code=404, detail="unknown imagegen definition")
        return definition.prompt_template
    return None


def _recent_scene(
    log: "EventLog", session_id: str, limit: int = IMAGEGEN_RECENT_TURNS
) -> str:
    """The last ``limit`` user-text/narrative-prose turn pairs as plain text.

    Read through ONE fork- AND revision-aware ``get_projection`` (never a
    session-local ``get_events``), mirroring how nodes.py builds
    ``prior_narratives``/the critic's ``prior_prose``: a branched chat inherits
    its ancestors' recent turns. Prose is passed through the shared DSL-strip
    helper (:func:`fabulist.dsl.strip_dsl`) so no UI statement leaks into the
    image prompt. Returns ``""`` for a fresh chat (no turns).
    """
    from fabulist.dsl import strip_dsl

    projection = log.get_projection(session_id)
    items = sorted(
        [
            *projection.get("user_messages", []),
            *projection.get("narratives", []),
        ],
        key=lambda item: item["seq"],
    )
    turns: list[tuple[str, str]] = []
    pending_user = ""
    for item in items:
        payload = item.get("payload") or {}
        if item.get("type") == "user":
            text = payload.get("text")
            pending_user = text.strip() if isinstance(text, str) else ""
            continue
        # A generated-image narrative is a canon image, not a turn: it carries
        # no prose, and pairing it with ``pending_user`` would fabricate a
        # phantom USER/NARRATIVE pair in the advisory scene block.
        if is_image_narrative(item):
            continue
        raw = payload.get("prose")
        if not isinstance(raw, str) or not raw.strip():
            fallback = payload.get("text")
            raw = fallback if isinstance(fallback, str) else ""
        turns.append((pending_user, strip_dsl(raw).strip()))
        pending_user = ""

    blocks: list[str] = []
    for user_text, prose in turns[-limit:]:
        parts: list[str] = []
        if user_text:
            parts.append("USER: " + user_text)
        if prose:
            parts.append("NARRATIVE: " + prose)
        if parts:
            blocks.append("\n".join(parts))
    return "\n\n".join(blocks)


def assemble_imagegen_context(log: "EventLog", session_id: str) -> str:
    """The generator agent's advisory context block (cast + recent scene).

    ``CAST CONTEXT`` follows the established nodes.py pattern EXACTLY —
    :func:`~fabulist.log.EventLog.get_chain_events` (fork-aware; NEVER
    ``get_events``, which starves branch turns) -> ``extract_cards`` ->
    ``persona_block(cards, [])`` — so a branched chat inherits the base cast and
    every CardV2 field rides losslessly (the cast block is deliberately
    uncapped). ``RECENT SCENE`` is the last :data:`IMAGEGEN_RECENT_TURNS` turn
    pairs from the fork/revision-aware projection. A section is omitted
    entirely when empty; both empty -> ``""`` (the caller injects no context
    message). Assembly is advisory only — each section is wrapped so it can
    never raise.
    """
    sections: list[str] = []
    try:
        from fabulist.context.roster import extract_cards, persona_block

        events = log.get_chain_events(session_id)
        # ``first_mes=[]`` suppresses the opening-scene line: the scene lives in
        # the prose; the generator needs the actor definitions (nodes.py idiom).
        cast_block = persona_block(extract_cards(events), []) or ""
    except Exception:  # noqa: BLE001 - context is advisory, never fatal
        cast_block = ""
    if cast_block:
        sections.append("CAST CONTEXT\n" + cast_block)
    try:
        recent = _recent_scene(log, session_id)
    except Exception:  # noqa: BLE001 - context is advisory, never fatal
        recent = ""
    if recent:
        sections.append(
            f"RECENT SCENE (last {IMAGEGEN_RECENT_TURNS} turns)\n" + recent
        )
    return "\n\n".join(sections)


async def _render_prompts(
    *, caller: Any, template: str | None, request_text: str, context: str = ""
) -> dict:
    """Render positive/negative via the generator agent (ONE shared code path).

    Calls the caller with the template as the SYSTEM prompt, the request as the
    USER message, drains the stream, and parses the reply (:func:`parse_render`).
    When ``context`` is non-empty it rides as a SECOND SYSTEM message between
    them, so the request template itself is never rewritten (it is a
    user-editable asset). The caller — the wiring-built ``imagegen``
    :class:`~fabulist.wiring.RegistryModelCaller`, which is what records the
    usage row — must be present; otherwise :class:`RenderUnavailable` is raised
    so the run endpoint can take its degraded path and the render route can
    report a row-level error.
    """
    from fabulist.graph.types import collect_text

    if not callable(caller):
        raise RenderUnavailable("imagegen caller unavailable")
    messages = [{"role": "system", "content": template or ""}]
    if context:
        messages.append({"role": "system", "content": context})
    messages.append({"role": "user", "content": request_text})
    positive, negative, path = parse_render(await collect_text(caller, messages))
    return {"positive": positive, "negative": negative, "parse": path}


async def _render_prompts_with_context(
    *,
    request: Request,
    log: "EventLog",
    session_id: str,
    template: str | None,
    request_text: str,
) -> dict:
    """The shared context-enriched render path (both agent-side routes use it).

    Assembles the advisory cast/recent-scene context, then renders. Context
    assembly is best-effort: on ANY error the render still proceeds with the
    request alone (never fatal) — the directive's advisory contract.
    """
    try:
        context = assemble_imagegen_context(log, session_id)
    except Exception:  # noqa: BLE001 - assembly is advisory, never fatal
        logger.warning("imagegen context assembly failed", exc_info=True)
        context = ""
    return await _render_prompts(
        caller=_caller_for(request),
        template=template,
        request_text=request_text,
        context=context,
    )


# -- routes -----------------------------------------------------------------


@router.get("/api/imagegen/config")
async def imagegen_config(request: Request) -> dict:
    """Report whether image generation is usable right now.

    ``enabled`` is the settings flag AND ``/system_stats`` answering; the probe is
    skipped (and ``reachable`` false) while the flag is off, so a disabled feature
    costs no network call. Reachability is cached for 60 s.
    """
    log = _log_for(request)
    base_url = _base_url(request)
    enabled_flag = feature_enabled(log)
    reachable = False
    if enabled_flag:
        reachable = await _reachable(request, base_url)
    return {
        "enabled": bool(enabled_flag and reachable),
        "base_url": base_url,
        "reachable": reachable,
    }


@router.post("/api/imagegen/render")
async def render_imagegen(body: RenderBody, request: Request) -> dict:
    """Render the request into positive/negative via the generator agent.

    The control plane: the frontend calls this with the definition (or an
    explicit template) BEFORE submit, lets the user edit the result, and sends
    the edited values as explicit overrides to the run endpoint. Returns
    ``{"positive", "negative", "parse"}`` on success; a missing/unbound caller
    or a raising call is an ``{"error": {"stage": "render", ...}}`` payload
    with HTTP 200 (never a 500).
    """
    log = _log_for(request)
    template = _resolve_template(log, body)
    try:
        return await _render_prompts(
            caller=_caller_for(request),
            template=template,
            request_text=body.request,
        )
    except Exception as exc:  # noqa: BLE001 - a render failure is a payload, not a 500
        logger.warning("imagegen render failed: %s", exc, exc_info=True)
        return {"error": {"stage": "render", "message": f"{type(exc).__name__}: {exc}"}}


@router.post("/api/sessions/{session_id}/imagegen/render")
async def render_imagegen_for_session(
    session_id: str, body: RenderBody, request: Request
) -> dict:
    """Render the request into positive/negative WITH session context injected.

    The chat-side control plane. Mirrors the run route: ``404`` for an unknown
    session, and ``CURRENT_SESSION`` installed before the caller runs so the
    usage row + agent-debug capture land on THIS session. The session's cast
    (through the fork chain) and its last :data:`IMAGEGEN_RECENT_TURNS` turn
    pairs ride as a second SYSTEM message, so "a picture of Tessa" carries
    Tessa's definition into the prompt. Response and error shape match the
    session-less render route (never a 500).
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")
    template = _resolve_template(log, body)

    # The chat-side render path is not driven through the engine, so the turn's
    # CURRENT_SESSION contextvar is unset; set it before the call so the shared
    # caller attributes its usage row (+ agent-debug capture) to this session —
    # exactly like the run endpoint and the assistant-consult paths.
    from fabulist.agents.debug import CURRENT_SESSION

    CURRENT_SESSION.set(session_id)
    try:
        return await _render_prompts_with_context(
            request=request,
            log=log,
            session_id=session_id,
            template=template,
            request_text=body.request,
        )
    except Exception as exc:  # noqa: BLE001 - a render failure is a payload, not a 500
        logger.warning("imagegen render failed: %s", exc, exc_info=True)
        return {"error": {"stage": "render", "message": f"{type(exc).__name__}: {exc}"}}


@router.post("/api/sessions/{session_id}/imagegen")
async def generate_image(
    session_id: str, body: ImageGenBody, request: Request
) -> dict:
    """Generate an image from ``body.workflow`` and append it to the chat.

    Returns ``{"asset_id", "seq", "unmapped", "prompt_id"}`` on success, or an
    ``{"error": {...}}`` payload (HTTP 200) on any failure — never a 500 that
    breaks the session.
    """
    log = _log_for(request)
    if not _session_exists(log, session_id):
        raise HTTPException(status_code=404, detail="unknown session")

    if not feature_enabled(log):
        return _failure(log, session_id, "disabled", "image generation is disabled")

    # Merge ruling (two lanes, one contract): an inline workflow wins; a saved
    # imagegen definition supplies workflow + prompt_template when only an id is
    # given. A body with neither is a precise 422, never a silent empty run.
    workflow = body.workflow
    prompt_template = body.prompt_template
    if workflow is None and body.imagegen_id:
        from fabulist.imagegen.registry import imagegen_by_id

        definition = imagegen_by_id(log, body.imagegen_id)
        if definition is None:
            raise HTTPException(status_code=404, detail="unknown imagegen definition")
        workflow = definition.workflow
        prompt_template = prompt_template or definition.prompt_template
    if not isinstance(workflow, dict):
        raise HTTPException(
            status_code=422,
            detail="provide a 'workflow' object or an 'imagegen_id' of a saved definition",
        )

    base_url = _base_url(request)
    client = _client_for(request, base_url)

    overrides = dict(body.overrides or {})
    if "positive" in overrides:
        # Explicit positive wins (editable-before-submit); the agent is skipped
        # entirely — the user has already reviewed/edited the prompt text.
        overrides.setdefault("negative", "")
    else:
        # No explicit positive: render through the generator agent (the SAME
        # shared helper the render route uses — one code path).
        if not (body.request or "").strip():
            raise HTTPException(
                status_code=422,
                detail=(
                    "provide a 'request' to render or an explicit 'positive' "
                    "override"
                ),
            )
        # This path is not driven through the engine, so the turn's
        # CURRENT_SESSION contextvar is unset; set it before the call so the
        # shared caller records the usage row (+ agent-debug capture) on THIS
        # session — exactly like the captioner/assistant paths.
        from fabulist.agents.debug import CURRENT_SESSION

        CURRENT_SESSION.set(session_id)
        render_error = ""
        try:
            rendered: dict | None = await _render_prompts_with_context(
                request=request,
                log=log,
                session_id=session_id,
                template=prompt_template,
                request_text=body.request,
            )
        except Exception as exc:  # noqa: BLE001 - fall back to the degraded path
            rendered = None
            render_error = f"{type(exc).__name__}: {exc}"
        if rendered is not None:
            overrides.setdefault("positive", rendered["positive"])
            overrides.setdefault("negative", rendered["negative"])
        elif prompt_template is None:
            # DEGRADED path (no caller, no template): today's static behavior.
            overrides.setdefault("positive", body.request)
            overrides.setdefault("negative", "")
        elif "{request}" in prompt_template:
            # DEGRADED path (documented): static substitution, ONLY when the
            # template actually carries the {request} placeholder.
            overrides.setdefault(
                "positive", _render_positive(prompt_template, body.request)
            )
            overrides.setdefault("negative", "")
        else:
            # The template cannot be rendered statically and the agent is
            # unavailable: surface a row-level error (never submit a broken run).
            return _failure(
                log,
                session_id,
                "render",
                render_error
                or "imagegen caller unavailable and prompt_template has no {request}",
            )
    result: "OverrideResult" = apply_overrides(workflow, overrides)

    try:
        prompt_id = await client.submit(result.workflow)
    except ComfySubmitError as exc:
        return _failure(
            log,
            session_id,
            "submit",
            str(exc),
            node_errors=getattr(exc, "node_errors", None),
        )
    except ComfyError as exc:
        return _failure(log, session_id, "submit", str(exc))
    except Exception as exc:  # noqa: BLE001 - a transport failure is never a 500
        return _failure(log, session_id, "submit", f"{type(exc).__name__}: {exc}")

    try:
        outputs = await client.poll(prompt_id)
    except ComfyTimeout as exc:
        return _failure(log, session_id, "poll", str(exc))
    except ComfyError as exc:
        return _failure(log, session_id, "poll", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _failure(log, session_id, "poll", f"{type(exc).__name__}: {exc}")

    image = first_image(outputs)
    if image is None:
        return _failure(log, session_id, "no_image", "ComfyUI produced no image output")

    try:
        data = await client.fetch_image(
            image["filename"], image.get("subfolder", ""), image.get("type", "output")
        )
    except ComfyError as exc:
        return _failure(log, session_id, "fetch", str(exc))
    except Exception as exc:  # noqa: BLE001
        return _failure(log, session_id, "fetch", f"{type(exc).__name__}: {exc}")

    mime = mimetypes.guess_type(image["filename"])[0] or "image/png"
    try:
        asset_event = store_image(log, session_id, data, mime, note=body.request)
        # A generated image is appended as a CANON narrative row (its image is
        # inlined into the narrator prompt as an assistant message by
        # history_from_log). ``prompt`` records the EFFECTIVE positive prompt
        # actually applied to the workflow — the explicit override if given,
        # else the agent render, else the degraded static substitution.
        row: "Event" = log.append_event(
            session_id,
            "narrative",
            "imagegen",
            {
                "prose": "",
                "dsl": "",
                "kind": "image",
                "image_asset_id": asset_event.payload.get("asset_id"),
                "prompt": overrides.get("positive", ""),
            },
        )
    except Exception as exc:  # noqa: BLE001 - a store failure is a row, not a 500
        return _failure(log, session_id, "store", f"{type(exc).__name__}: {exc}")

    return {
        "asset_id": row.payload.get("image_asset_id"),
        "seq": row.seq,
        "prompt_id": prompt_id,
        "unmapped": list(result.unmapped),
    }
