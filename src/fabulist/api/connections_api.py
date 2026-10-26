"""Connections read/write API with hot re-configure (D25 C1).

The connections document (``configs/connections.json``) maps connection names
to endpoint/model/sampler settings and binds logical agents to them. Today that
file is parsed once at boot: a layperson cannot change a model or temperature
without hand-editing JSON and restarting the engine. This router closes that
door.

  GET    /api/connections                 -> redacted view (api_key_env NAME only) + ``slots``
  PATCH  /api/connections/agents/{agent}  -> rebind one agent to an existing connection
  PATCH  /api/connections/{name}          -> model/sampling/extra_body/timeout/api_key(_env)/note
                                          + typed fields (context_window/thinking/streaming/
                                          return_thinking/reasoning_effort/vision)
  POST   /api/connections/{name}          -> create a new entry (201)
  POST   /api/connections/{name}/key      -> paste a literal key into ``.env``
  DELETE /api/connections/{name}          -> remove an entry (409 while an agent binds it)
  GET    /api/connections/{name}/models   -> server-side ``{base_url}/v1/models`` proxy
  GET    /api/connections/doctor          -> probe EVERY connection (reachability, latency)

W9.25 adds the taxonomy read side and the typed connection fields:

* ``GET`` carries ``slots`` — :func:`fabulist.connections.taxonomy.slot_view`'s
  ordered rows (slot/type/task/label/canonical/aliases/connection/via plus
  ``write_slot``/``inert``) so the drawer groups type/task rows and shows
  resolution provenance. There is ONE row per resolved ROLE: a legacy twin
  (``narrator``) and an item slot that serves a task-less type (``embedder`` ->
  the ``embedding`` row) are aliases of that row, never rows of their own — a
  role bound under an alias can never render as a separate unbound row while the
  engine uses the other spelling (issue #48). ``connection`` is the effective
  binding (what the engine's call path resolves), ``write_slot`` is the declared
  spelling a rebind must write to take effect (``embedding`` -> ``embedder``,
  because the embedding call sites ask for that name and a name's own key is
  tried before its type), and ``inert`` lists the authored spellings that lost
  precedence with their own connections, so a conflicting pair is surfaced
  rather than hidden. The raw ``agents`` map stays verbatim for compatibility.
* ``PATCH /api/connections/{name}`` accepts ``context_window`` (int >= 1 | null),
  ``thinking``, ``streaming`` and ``return_thinking`` (bool | null) with strict
  validation — a truthy string or ``true`` for a window is a 422, never a
  coercion. ``reasoning_effort`` (one of the registry's documented values | null;
  null = send NOTHING) is validated through the registry's own cleaner
  (:func:`fabulist.connections.registry.clean_reasoning_effort`) so the enum
  lives in exactly one place — a typo is a ``400`` naming the vocabulary.

Issue #44 removed the per-connection envelope carrier: the per-turn engine
envelope rides ONE universal carrier (an ephemeral ``assistant`` message
immediately before the final user message), so there is no field to read, patch
or default here.
* ``PATCH /api/connections/agents/{agent}`` accepts ANY declared slot spelling and
  NORMALIZES a legacy-twin address to its canonical key (``narrator`` ->
  ``completion.narration``): the typed key is what gets written, the legacy key is
  left untouched and goes inert. Unknown names stay 404 — creating arbitrary
  per-item slots over the API is a non-goal.

The taxonomy now also declares the two per-item ROLES that are D19 interactions
(issue #55), so the drawer maps them like any other slot and this PATCH accepts
them: ``planner`` (the steering plane — unauthored it keeps resolving through
the narration task, i.e. the ``planner -> narrator`` default) and ``npc`` (the
whole NPC lane GROUP — the fan-out resolves its agent through
:func:`~fabulist.connections.taxonomy.resolve_interaction_agent` per call, so an
authored key takes effect on the next turn). Both are ordinary declared
spellings: one row, one ``write_slot``, no special PATCH address.

Every write merges into the raw document (unknown keys preserved), validates the
WHOLE result, writes atomically, and swaps the live :class:`RegistryStore`
snapshot. Callers hold the store — not a frozen registry — so the NEXT request
resolves through the patched settings with no process restart.

Model discovery
---------------
``GET /api/connections/{name}/models`` probes the endpoint from the *server*
(no CORS, and any API key stays off the browser). The target is the stored
connection's ``base_url``; ``?base_url=`` / ``?api_key_env=`` / ``?api_key=``
override it so an unsaved draft can be pinged before it is created. A pasted
literal is resolved as follows: a transient ``api_key`` (the ``X-Fabulist-Key``
header, or the ``api_key`` query parameter) WINS, then ``api_key_env``, then the
stored connection's own key. A transient literal is used for THAT ONE request
only — never written to ``.env``, never a document field, never logged, never
echoed (the error paths scrub it out of ``reason`` and ``url``). The header is
the browser's transport on purpose: uvicorn's access log records a full
path+query, so a secret must not travel in the URL. A connection with a key
sends ``Authorization: Bearer <key>``; a keyless connection sends no auth
header. The secret itself is never serialized. A non-2xx or unreachable
endpoint is a ``502`` with ``{reason, url, connection}`` so the save form can
surface "saved, but unreachable".

Redaction / key storage
-----------------------
``api_key_env`` is the *name* of the env var (never the key). ``api_key_set``
is a boolean "the named env var currently resolves to a non-empty value"; the
secret value itself is never serialized. ``extra_body`` / ``sampling`` / ``note``
are not secrets and pass through verbatim.

A literal key is persisted, but only in the project ``.env`` — never in the
connections document, a response, a log line, or an event (this supersedes D25's
literal-key refusal; the pinned contract is "keys live in ``.env``, the document
carries the name"). Three doors take the literal so the user surface can hold a
KEY instead of an env-var name, and NONE of them blocks on a prior save:
``POST /api/connections/{name}/key`` with ``{"key": "..."}``, plus an optional
``api_key`` on the create and patch bodies (a literal ``api_key`` in a patch
WINS over a supplied ``api_key_env``; on create the literal is applied right
after the entry is written). Each of them

* derives ``FABULIST_<NAME>_KEY`` (:func:`fabulist.connections.registry.api_key_env_name`:
  connection name uppercased, every non-alphanumeric -> ``_``) and rewrites ONLY
  that line of the project ``.env`` (created mode 600 when absent, every other
  line preserved),
* points the connection's ``api_key_env`` at that name (the registry's field, so
  it rides the ordinary document write + reload), and
* answers with the redacted connection (``api_key_env`` NAME + ``api_key_set``
  boolean) — never the value. ``POST .../key`` answers the same two fields
  top-level.

``api_key_env`` keeps working unchanged for env-var users: a name configured in
the document, a systemd ``EnvironmentFile`` or a shell export resolves exactly as
before, and a create/patch that names only ``api_key_env`` never touches ``.env``.

The engine keeps a :class:`RegistryStore`, whose ``reload`` re-reads ``.env`` and
overlays it into ``os.environ``; a pasted key is therefore live on the next
request with no process restart. An ``api_key_env`` configured by other means
(a systemd ``EnvironmentFile``, a shell export) keeps resolving exactly as before.

Wiring (orchestrator-owned, one line in ``app.py``)::

    from fabulist.api.connections_api import router as connections_router
    app.include_router(connections_router)

The router resolves its store from ``app.state.connections_store`` when set,
else from ``app.state.registry`` when that is a :class:`RegistryStore` (the
hot-reload path installs the store as the registry), else from
``app.state.connections_config_path``. With none of those it returns ``503``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from fabulist.connections import taxonomy
from fabulist.connections.registry import (
    Connection,
    ConnectionConfigError,
    Registry,
    RegistryStore,
    api_key_env_name,
    clean_reasoning_effort,
    host_alias,
    write_env_key,
)

router = APIRouter(tags=["connections"])

#: Timeout for a discovery probe when the connection carries no ``timeout``.
DISCOVERY_TIMEOUT_SECONDS = 30.0

#: Timeout for the connectivity doctor's per-connection probe. Deliberately
#: SHORT and independent of ``connection.timeout`` (which is a generation
#: budget in the hundreds of seconds): the doctor is an on-demand reachability
#: check, not a model call, and the request must never hang.
DOCTOR_TIMEOUT_SECONDS = 2.0

#: Header carrying a transient literal key on a discovery probe. The browser uses
#: this (never ``?api_key=``): uvicorn's access log records the full path+query,
#: so a secret must not travel in a URL. The query spelling stays supported for
#: API callers (documented + tested) and is scrubbed from every response field.
TRANSIENT_KEY_HEADER = "X-Fabulist-Key"

#: Injection seam: tests monkeypatch this to a ``httpx.MockTransport``-backed
#: factory (mirrors the embedder/STT test pattern). Production uses the default.
_http_client_factory: Any = httpx.AsyncClient


class ConnectionPatch(BaseModel):
    """Body of ``PATCH /api/connections/{name}`` (partial update).

    ``base_url`` is deliberately absent (and ``extra="forbid"`` rejects it): a
    hot endpoint swap is out of scope (D25 C1). Object fields replace wholesale.
    ``note`` is free-text user state that rides the raw document (the registry
    ignores it but preserves it).

    The W9.25 fields are STRICT: ``StrictInt``/``StrictBool`` reject a float, a
    bool-as-int, or a truthy string at the door (422) — a mis-set Context Size or
    toggle must never be coerced, because both change transport behaviour.
    """

    model_config = ConfigDict(extra="forbid")

    model: str | None = None
    sampling: dict[str, Any] | None = None
    extra_body: dict[str, Any] | None = None
    timeout: float | None = None
    api_key_env: str | None = None
    #: A literal key the owner pasted. It is NEVER a document field: the handler
    #: stores it in ``.env`` and writes the derived ``api_key_env`` NAME instead.
    #: Wins over a supplied ``api_key_env``. Kept a bare ``str`` (like
    #: :class:`KeyPaste`) so no constraint can echo the value in a 422 body.
    api_key: str | None = None
    note: str | None = None
    #: W9.25: owner-declared Context Size in tokens (null = unknown).
    context_window: StrictInt | None = Field(default=None, ge=1)
    #: W9.25: thinking toggle; null/true = ON, false is the only off instruction.
    thinking: StrictBool | None = None
    #: W9.25: streaming toggle; null/true = stream, false = one non-streaming call.
    streaming: StrictBool | None = None
    #: "Return thinking": the tri-state reasoning echo. ``null`` = AUTO (echo the
    #: provider's own ``reasoning_content`` back inside a tool loop whenever it
    #: returned some — the default, and what DeepSeek thinking models require),
    #: ``true`` = force that echo, ``false`` = never send the field.
    return_thinking: StrictBool | None = None
    #: Reasoning DEPTH for a thinking model (z.ai's top-level ``reasoning_effort``
    #: body field): one of the registry's documented values
    #: (:data:`~fabulist.connections.registry.REASONING_EFFORT_VALUES`), or null =
    #: send NOTHING (the default). Validated by the registry's own cleaner, so the
    #: vocabulary lives in ONE place — a typo is a 400 naming the allowed values.
    reasoning_effort: str | None = None
    #: Per-connection VISION toggle (issue #79): null/false = text-only (the
    #: default), true = native image content parts. Resolved live at build time.
    vision: StrictBool | None = None
    #: W8.4: explicit provider-capability overrides (bool values only). The store
    #: merges these over the provider-profile default; an unmeasured capability
    #: stays false. Object fields replace wholesale, like ``sampling``.
    capabilities: dict[str, bool] | None = None


class ConnectionCreate(BaseModel):
    """Body of ``POST /api/connections/{name}`` — a new connection entry.

    Optional at create time: the W9.25 typed fields are usually set later from
    the drawer, but a create may carry them so one request seeds a whole entry.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str
    model: str
    api_key_env: str | None = None
    #: A literal key pasted on the CREATE form — stored in ``.env`` exactly like
    #: ``POST .../key`` (see :meth:`patch_connection`), so the key never waits
    #: for a save. Wins over a supplied ``api_key_env``.
    api_key: str | None = None
    extra_body: dict[str, Any] | None = None
    sampling: dict[str, Any] | None = None
    timeout: float | None = None
    note: str | None = None
    context_window: StrictInt | None = Field(default=None, ge=1)
    thinking: StrictBool | None = None
    streaming: StrictBool | None = None
    #: "Return thinking" (null = AUTO): see :class:`ConnectionPatch`.
    return_thinking: StrictBool | None = None
    #: Reasoning depth (see :class:`ConnectionPatch`); null = send NOTHING.
    reasoning_effort: str | None = None
    #: Per-connection VISION toggle (see :class:`ConnectionPatch`).
    vision: StrictBool | None = None


class AgentsPatch(BaseModel):
    """Body of ``PATCH /api/connections/agents/{agent}`` — rebind one agent.

    The agent name lives in the path; the body carries only the target
    connection name. ``extra="forbid"`` keeps the write surface explicit.
    """

    model_config = ConfigDict(extra="forbid")

    connection: str


class KeyPaste(BaseModel):
    """Body of ``POST /api/connections/{name}/key`` — a literal key to store.

    ``key`` is a bare ``str`` with NO pydantic constraints on purpose: a 422
    body echoes the offending input, so validating it here could serialize the
    secret back to the caller. Emptiness and newlines are checked in the
    handler, whose errors carry the field name only.
    """

    model_config = ConfigDict(extra="forbid")

    key: str


def _store_for(request: Request) -> RegistryStore:
    """Resolve the hot-reloadable connections store, or 503 when unwired."""
    store = getattr(request.app.state, "connections_store", None)
    if isinstance(store, RegistryStore):
        return store
    registry = getattr(request.app.state, "registry", None)
    if isinstance(registry, RegistryStore):
        return registry
    path = getattr(request.app.state, "connections_config_path", None)
    if path:
        return RegistryStore(path)
    raise HTTPException(status_code=503, detail="connections store unavailable")


def _raw_entries(store: RegistryStore) -> dict[str, Any]:
    """The raw ``connections`` object from the document (for non-parsed keys)."""
    raw = store.raw().get("connections")
    return raw if isinstance(raw, dict) else {}


def _redact(conn: Connection, raw_entry: Any = None) -> dict[str, Any]:
    """Serialize one connection WITHOUT its secret (only the env-var name).

    ``raw_entry`` (the document's verbatim object for this connection) supplies
    keys the typed parser ignores but the user owns — today that is ``note``.
    """
    out: dict[str, Any] = {
        "name": conn.name,
        "base_url": conn.base_url,
        "model": conn.model,
        "api_key_env": conn.api_key_env,
        "api_key_set": bool(conn.api_key),
        "extra_body": dict(conn.extra_body),
        "sampling": dict(conn.sampling),
        "timeout": conn.timeout,
        # The WALL-CLOCK per-call bound (issue #25): ``null`` = the 900s default,
        # exactly as the engine reads it (never a fabricated number).
        "call_timeout_s": conn.call_timeout_s,
        # W9.25 typed fields: engine-read, so they are serialized as values (an
        # absent/null window stays ``null`` = unknown, never a fake default).
        "context_window": conn.context_window,
        "thinking": conn.thinking,
        "streaming": conn.streaming,
        # "Return thinking": null IS a value here (AUTO), serialized as such.
        "return_thinking": conn.return_thinking,
        # Reasoning depth: null IS a value here too (send NOTHING), never a fake
        # default — the drawer must be able to show "not set".
        "reasoning_effort": conn.reasoning_effort,
        # Vision toggle: null IS the value (text-only), never a fake default.
        "vision": conn.vision,
    }
    if isinstance(raw_entry, dict) and isinstance(raw_entry.get("note"), str):
        out["note"] = raw_entry["note"]
    return out


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Persist ``data`` as pretty JSON via a sibling tmp file + ``os.replace``.

    Local twin of the registry's writer so this router can remove an entry
    (a document mutation the :class:`RegistryStore` does not yet expose) without
    reaching into registry internals. See the module report's seam note.
    """
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _mutate_entry(store: RegistryStore, name: str, mutate: Any) -> None:
    """Apply ``mutate(entry)`` to connection ``name``, validate, persist, reload.

    Uses only public store seams (``raw`` / ``reload`` / ``path``) because the
    registry exposes no method for note-sets or deletes. Raises ``KeyError`` for
    an unknown connection and :class:`ConnectionConfigError` when the resulting
    document is invalid (e.g. an agent still binding the removed entry).
    """
    data = store.raw()
    connections = data.get("connections")
    if not isinstance(connections, dict) or name not in connections:
        raise KeyError(name)
    entry = connections[name]
    if not isinstance(entry, dict):
        raise ConnectionConfigError(f"connections[{name!r}]: must be an object")
    mutate(entry)
    Registry.from_dict(data)  # validate the WHOLE document before writing
    _atomic_write_json(store.path, data)
    store.reload()


def _mutate_agents(store: RegistryStore, agent: str, connection: str) -> None:
    """Bind ``agent`` to ``connection``, validate, persist, reload.

    ``agent`` is a slot KEY — the router normalizes a legacy-twin address to its
    canonical spelling before calling here — and is UPSERTED: authoring a
    declared spelling the document does not carry yet is the W9.25 rebind path
    (the legacy/it key stays in the file, untouched and inert). The write rides
    :meth:`RegistryStore.bind_agent` (the SAME seam the boot-time sidecar
    auto-pin uses, issue #36), so the two doors share one validate-then-write
    discipline and cannot drift. Raises ``KeyError`` for an unknown connection
    and :class:`ConnectionConfigError` when the resulting document is invalid.
    """
    store.bind_agent(agent, connection)


def _clean_effort_field(payload: dict[str, Any], name: str) -> None:
    """Normalize/validate ``reasoning_effort`` in a create/patch body in place.

    Reuses the registry's cleaner (and therefore its documented value set): the
    API layer never grows a second copy of the enum. A bad value raises the
    registry's :class:`ConnectionConfigError`, which the handlers map to ``400``
    exactly like any other document-validation failure. ``None`` passes through
    unchanged ("send nothing").
    """
    if "reasoning_effort" in payload:
        payload["reasoning_effort"] = clean_reasoning_effort(
            payload["reasoning_effort"], f"connections[{name!r}].reasoning_effort"
        )


def _set_note(store: RegistryStore, name: str, note: str | None) -> None:
    """Set/clear the free-text ``note`` on connection ``name`` (persists)."""

    def _apply(entry: dict[str, Any]) -> None:
        if note:
            entry["note"] = note
        else:
            entry.pop("note", None)

    _mutate_entry(store, name, _apply)


# --- literal keys (the user surface: a KEY, never an env-var name) ---------


def _literal_key(value: str, field: str = "api_key") -> str:
    """Validate a pasted literal key, raising ``422`` when it is unusable.

    A paste often carries trailing whitespace, so the value is stripped. An
    empty or multi-line value is a ``422`` whose detail names the FIELD only —
    it must never echo the offending input (that would be the secret).
    """
    key = value.strip()
    if not key:
        raise HTTPException(status_code=422, detail=f"{field}: must be a non-empty string")
    if "\n" in value or "\r" in value:
        raise HTTPException(status_code=422, detail=f"{field}: must be a single line")
    return key


def _store_literal_key(store: RegistryStore, name: str, key: str) -> str:
    """Persist a literal key for connection ``name`` in the project ``.env``.

    Writes the derived ``FABULIST_<NAME>_KEY`` line (mode 600), reloads so the
    key is live on the next lookup, and points ``api_key_env`` at that name.
    Returns the env-var NAME; the value never leaves this function. ``500`` when
    the env file cannot be written.
    """
    env_name = api_key_env_name(name)
    try:
        write_env_key(store.env_path, env_name, key)
    except OSError as exc:
        raise HTTPException(
            status_code=500, detail=f"could not write {store.env_path}: {exc.strerror}"
        ) from None
    store.reload()  # env overlay -> the connection's api_key_env resolves the key
    store.update_connection(name, {"api_key_env": env_name})
    return env_name


def _scrub(text: str, secret: str | None) -> str:
    """Remove ``secret`` from outbound text (redaction applies to errors too)."""
    if not secret or secret not in text:
        return text
    return text.replace(secret, "***")


# --- model discovery -------------------------------------------------------


def _models_url(base_url: str) -> str:
    """Return the OpenAI-compatible models URL for ``base_url``.

    ``base_url`` conventionally already names the version root (``.../v1``,
    ``.../v4``); if it does not, address the OpenAI convention explicitly.
    """
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("base_url is empty")
    if re.search(r"/v\d+$", base):
        return f"{base}/models"
    return f"{base}/v1/models"


def _extract_model_ids(payload: Any) -> list[str]:
    """Pull model ids out of the common OpenAI-compatible shapes.

    Accepts ``{"data": [...]}`` (OpenAI), ``{"models": [...]}`` (llama.cpp) and
    a bare list; items may be strings or objects with ``id``/``name``/``model``.
    Order and first-occurrence dedupe are preserved.
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
        return []
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
    return ids


def _probe_target(
    store: RegistryStore | None,
    name: str,
    base_url: str | None,
    api_key_env: str | None,
    api_key: str | None = None,
) -> tuple[str, str | None, float]:
    """Resolve ``(base_url, api_key, timeout)`` for a discovery probe.

    ``base_url`` / ``api_key_env`` override the stored connection (draft ping);
    a transient literal ``api_key`` WINS over both and is used for this one
    request only. Raises ``KeyError`` when there is neither a stored entry nor
    a ``base_url`` override.
    """
    stored: Connection | None = None
    if store is not None:
        try:
            stored = store.connections.get(name)
        except Exception:  # pragma: no cover - malformed store never expected
            stored = None

    if base_url is None and stored is None:
        raise KeyError(name)

    resolved_base = base_url if base_url is not None else stored.base_url

    if api_key is not None:
        key = api_key or None
    elif api_key_env is not None:
        key = os.environ.get(api_key_env) if api_key_env else None
    elif stored is not None:
        key = stored.api_key
    else:
        key = None

    timeout = (
        stored.timeout
        if stored is not None and stored.timeout is not None
        else DISCOVERY_TIMEOUT_SECONDS
    )
    return resolved_base, key, timeout


@router.get("/api/connections")
async def list_connections(request: Request) -> dict:
    """Return every connection (redacted) plus the agent map and the slot view."""
    store = _store_for(request)
    registry = store.registry
    raw_entries = _raw_entries(store)
    return {
        "connections": {
            name: _redact(conn, raw_entries.get(name))
            for name, conn in registry.connections.items()
        },
        "agents": dict(registry.agents),
        # W9.25 §3.7: the ordered, grouped slot rows the drawer renders. The
        # rows are derived from the SAME resolution rule ``connection_for`` uses.
        "slots": taxonomy.slot_view(registry.agents),
    }


@router.patch("/api/connections/agents/{agent}")
async def patch_agent(agent: str, body: AgentsPatch, request: Request) -> dict:
    """Rebind one agent slot to another existing connection (hot-reload PATCH).

    ``agent`` is any DECLARED slot spelling (a typed key, a legacy twin, an item
    slot, or a custom key the document authors). A legacy-twin address is
    normalized to its canonical typed key before the write (W9.25 §4): the
    response names the key that was written, so the caller can reconcile. ``404``
    when the name is not a known/declared slot (fail-closed against phantom
    agents) or when the target connection does not exist; ``400`` when the merged
    document fails registry validation. On success the live
    :class:`RegistryStore` snapshot is swapped, so a caller holding the store
    resolves the new binding on its NEXT request. Returns the fresh agents map so
    the UI can reconcile without a second GET.
    """
    store = _store_for(request)
    registry = store.registry
    if agent not in set(registry.agents) | taxonomy.declared_spellings():
        raise HTTPException(status_code=404, detail=f"unknown agent: {agent}")
    if body.connection not in registry.connections:
        raise HTTPException(
            status_code=404, detail=f"unknown connection: {body.connection}"
        )
    slot = taxonomy.canonical(agent)
    try:
        _mutate_agents(store, slot, body.connection)
    except ConnectionConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {
        "agents": dict(store.registry.agents),
        "agent": slot,
        "requested": agent,
        "reloaded": True,
    }


@router.patch("/api/connections/{name}")
async def patch_connection(
    name: str, body: ConnectionPatch, request: Request
) -> dict:
    """Patch one connection, persist atomically, hot-reload the live registry.

    ``404`` for an unknown connection; ``422`` for an invalid field (including
    ``base_url``); ``400`` when the merged document fails registry validation.
    A rejected patch leaves the file untouched.

    A literal ``api_key`` in the body is the user surface: it is stored in
    ``.env`` and the derived ``api_key_env`` NAME is written to the document, so
    a literal WINS over any ``api_key_env`` in the same patch. A key-only patch
    is valid (no other field required). The response is the redacted connection
    — ``api_key_env`` + ``api_key_set``, never the secret.
    """
    store = _store_for(request)
    patch = body.model_dump(exclude_unset=True)
    raw_key = patch.pop("api_key", None)
    key = None if raw_key is None else _literal_key(raw_key)
    if key is not None:
        patch.pop("api_key_env", None)  # a literal owns the env name
    if not patch and key is None:
        raise HTTPException(status_code=422, detail="no patchable fields supplied")
    if key is not None and name not in store.connections:
        raise HTTPException(status_code=404, detail=f"unknown connection: {name}") from None
    note_given = "note" in patch
    note = patch.pop("note", None)
    try:
        _clean_effort_field(patch, name)
        if patch:
            store.update_connection(name, patch)
        if note_given:
            _set_note(store, name, note)
        if key is not None:
            _store_literal_key(store, name, key)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown connection: {name}") from None
    except ConnectionConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    registry = store.registry
    return {
        "connection": _redact(registry.connections[name], _raw_entries(store).get(name)),
        "reloaded": True,
    }


@router.post("/api/connections/{name}/key")
async def paste_connection_key(name: str, body: KeyPaste, request: Request) -> dict:
    """Store a pasted key for ``name`` in the project ``.env`` (D25 superseded).

    Derives ``FABULIST_<NAME>_KEY``, rewrites that one line of ``store.env_path``
    (mode 600, other lines preserved), points the connection's ``api_key_env`` at
    it, and reloads — which re-reads ``.env`` into ``os.environ``, so the key is
    live on the NEXT request with no process restart. ``404`` for an unknown
    connection (nothing is written); ``422`` for an empty or multi-line key;
    ``500`` when the env file cannot be written. The response carries the env var
    NAME and whether a key now resolves — never the literal.
    """
    store = _store_for(request)
    if name not in store.connections:
        raise HTTPException(status_code=404, detail=f"unknown connection: {name}")
    # A paste often carries a trailing newline/space; the field name is all the
    # error detail may carry (never the value).
    key = _literal_key(body.key, field="key")
    env_name = _store_literal_key(store, name, key)
    return {
        "api_key_env": env_name,
        "api_key_set": bool(store.connections[name].api_key),
    }


@router.post("/api/connections/{name}", status_code=201)
async def create_connection(
    name: str, body: ConnectionCreate, request: Request
) -> dict:
    """Create a new connection entry (not bound to any agent; ``409`` if it exists).

    An optional literal ``api_key`` is applied right after the entry lands (so a
    ``409`` never leaves a stray ``.env`` line) through the same path as
    ``POST .../key`` — the key is a create-time field, never a document field,
    and never waits for a second save. A keyless create that names only
    ``api_key_env`` touches no env file.
    """
    store = _store_for(request)
    entry = body.model_dump(exclude_none=True)
    note = entry.pop("note", None)
    raw_key = entry.pop("api_key", None)
    key = None if raw_key is None else _literal_key(raw_key)
    if key is not None:
        entry.pop("api_key_env", None)  # the literal owns the env name
    try:
        _clean_effort_field(entry, name)
        store.add_connection(name, entry)
        if note:
            _set_note(store, name, note)
        if key is not None:
            _store_literal_key(store, name, key)
    except KeyError:
        raise HTTPException(status_code=409, detail=f"connection exists: {name}") from None
    except ConnectionConfigError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    registry = store.registry
    return {"connection": _redact(registry.connections[name], _raw_entries(store).get(name))}


@router.delete("/api/connections/{name}")
async def delete_connection(name: str, request: Request) -> dict:
    """Remove one connection entry (``404`` unknown, ``409`` while agent-bound).

    An entry an agent still resolves is refused rather than written: removing it
    would make the whole document invalid (``Registry.from_dict`` rejects an
    agent -> missing-connection binding). Rebind the agent first.
    """
    store = _store_for(request)
    data = store.raw()
    connections = data.get("connections")
    if not isinstance(connections, dict) or name not in connections:
        raise HTTPException(status_code=404, detail=f"unknown connection: {name}")
    del connections[name]
    try:
        Registry.from_dict(data)  # reject while an agent still binds it
    except ConnectionConfigError as exc:
        raise HTTPException(
            status_code=409, detail=f"cannot delete {name!r}: {exc}"
        ) from None
    _atomic_write_json(store.path, data)
    store.reload()
    return {"deleted": name, "reloaded": True}


# --- connectivity doctor ---------------------------------------------------


def _doctor_reason(conn: Connection, url: str, exc: Exception) -> dict[str, str]:
    """Build an ACTIONABLE error for one failed probe (class + reason).

    The reason always names the RESOLVED url (what was actually dialled) and,
    when ``FABULIST_HOST_ALIAS`` rewrote a loopback host, says so — the failure
    mode issue #33 exists for: inside a container ``127.0.0.1`` is the container,
    so a user must be told the URL was rewritten and where to look next.
    """
    cls = type(exc).__name__
    if isinstance(exc, httpx.TimeoutException):
        detail = "timed out"
        hint = "the server did not answer in time; check it is running and not overloaded"
    elif isinstance(exc, httpx.ConnectError):
        detail = "connection refused or host not resolvable"
        hint = "check the server is listening, and that this host is reachable from the engine"
    else:
        detail = str(exc) or cls
        hint = "check the URL and the transport error above"
    reason = f"cannot reach {url}: {cls}: {detail}; {hint}"
    alias = host_alias()
    if conn.base_url_rewritten and alias:
        reason += (
            f" — this URL is a loopback host rewritten by FABULIST_HOST_ALIAS={alias};"
            " inside a container loopback is the container itself, so the model server"
            " must listen on the HOST (0.0.0.0) and the host must be reachable by that name"
        )
    return {"class": cls, "reason": _scrub(reason, conn.api_key)}


async def _probe_connection(conn: Connection) -> dict[str, Any]:
    """Probe one connection's ``/models`` endpoint; never raise, never hang.

    A short client timeout bounds the request; any transport/URL error is
    reported in the row. The API key (when resolved) rides only the outbound
    ``Authorization`` header and is scrubbed from the message — it is never a
    response field.
    """
    try:
        url = _models_url(conn.base_url)
    except ValueError as exc:
        return {
            "name": conn.name,
            "base_url": conn.base_url,
            "ok": False,
            "status": None,
            "latency_ms": 0.0,
            "error": {"class": "InvalidURL", "reason": _scrub(str(exc), conn.api_key)},
        }

    headers = {"Accept": "application/json"}
    if conn.api_key:
        headers["Authorization"] = f"Bearer {conn.api_key}"

    started = time.perf_counter()
    status: int | None = None
    error: dict[str, str] | None = None
    try:
        async with _http_client_factory(timeout=DOCTOR_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=headers)
        status = response.status_code
        if status >= 400:
            error = {
                "class": "HTTPStatus",
                "reason": f"upstream returned {status} from {url}",
            }
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        error = _doctor_reason(conn, url, exc)
    latency_ms = round((time.perf_counter() - started) * 1000.0, 1)
    return {
        "name": conn.name,
        "base_url": _scrub(conn.base_url, conn.api_key),
        "ok": error is None,
        "status": status,
        "latency_ms": latency_ms,
        "error": error,
    }


@router.get("/api/connections/doctor")
async def connection_doctor(request: Request) -> dict:
    """Probe every configured connection and report reachability + latency.

    This is deliberately NOT part of ``GET /health``: it performs real network
    I/O, so it must stay on demand. Probes run CONCURRENTLY with a short
    per-connection timeout (:data:`DOCTOR_TIMEOUT_SECONDS`), so the request is
    bounded by roughly one timeout regardless of connection count. The response
    is a report, not an error: an unreachable entry is ``ok: false`` with an
    ``error`` object, and the top-level ``ok`` is the AND over all entries.
    ``503`` when the connections store is unwired (the router's existing
    convention). Never returns a key: only names, resolved URLs and booleans.
    """
    store = _store_for(request)
    connections = list(store.registry.connections.values())
    rows = await asyncio.gather(*(_probe_connection(conn) for conn in connections))
    return {
        "host_alias": host_alias(),
        "ok": all(row["ok"] for row in rows),
        "count": len(rows),
        "connections": list(rows),
    }


@router.get("/api/connections/{name}/models")
async def discover_models(
    name: str,
    request: Request,
    base_url: str | None = Query(default=None),
    api_key_env: str | None = Query(default=None),
    api_key: str | None = Query(default=None),
) -> dict:
    """Proxy ``{base_url}/v1/models`` for a connection (or an unsaved draft).

    ``base_url`` / ``api_key_env`` query overrides let the create form probe a
    not-yet-saved endpoint; a transient literal ``api_key`` (the
    ``X-Fabulist-Key`` header, or ``?api_key=``) lets it probe with a key PASTED
    into that draft — no save, no env var, no ``.env`` write. A transient literal
    is used for this request only and is scrubbed from every response field.
    The key is resolved server-side (never returned); a keyless connection sends
    no auth header.
    """
    store: RegistryStore | None
    try:
        store = _store_for(request)
    except HTTPException:
        if base_url is None:
            raise
        store = None

    # The header is the browser's transport: uvicorn's access log records the
    # full path+query, so the drawer must never put a secret in the URL.
    transient_key = request.headers.get(TRANSIENT_KEY_HEADER) or api_key

    try:
        resolved_base, key, timeout = _probe_target(
            store, name, base_url, api_key_env, transient_key
        )
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown connection: {name}") from None

    try:
        url = _models_url(resolved_base)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"reason": str(exc)}) from None

    headers = {"Accept": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    try:
        async with _http_client_factory(timeout=timeout) as client:
            response = await client.get(url, headers=headers)
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        raise HTTPException(
            status_code=502,
            detail={
                "reason": _scrub(f"connection failed: {exc}", transient_key),
                "url": _scrub(url, transient_key),
                "connection": name,
            },
        ) from None

    if response.status_code >= 400:
        raise HTTPException(
            status_code=502,
            detail={
                "reason": f"upstream returned {response.status_code}",
                "url": _scrub(url, transient_key),
                "connection": name,
                "status": response.status_code,
            },
        )
    try:
        payload = response.json()
    except ValueError:
        raise HTTPException(
            status_code=502,
            detail={
                "reason": "non-JSON response",
                "url": _scrub(url, transient_key),
                "connection": name,
            },
        ) from None

    models = _extract_model_ids(payload)
    return {
        "connection": name,
        "base_url": _scrub(resolved_base, transient_key),
        "url": _scrub(url, transient_key),
        "models": models,
        "count": len(models),
        "api_key_set": bool(key),
    }
