"""Per-agent connection registry (PLAN R5).

Loads a JSON config that maps connection names to endpoint/model/key/sampler settings,
plus an ``agents`` map from a logical agent name (e.g. ``narrator``) to a connection name.
There is deliberately no global connection profile: every agent resolves its own entry so
that narrator/extractor/captioner can point at different endpoints, models and sampler
settings (including non-standard body params such as GLM's ``thinking`` block).

Config shape::

    {
      "connections": {
        "name": {
          "base_url": "http://host/v1",
          "model": "model-id",
          "api_key_env": "ENV_VAR_NAME",   # optional; the NAME, never the key
          "extra_body": {"thinking": {"type": "enabled"}},  # optional, non-standard params
          "sampling": {"temperature": 0.7, "max_tokens": 4096},  # optional samplers
          "timeout": 300,                   # optional, seconds
          "context_window": 65536,          # optional int >= 1 | null (UI "Context Size")
          "thinking": false,                # optional bool | null; null/true = ON
          "streaming": false,               # optional bool | null; null/true = stream
          "return_thinking": true,          # optional bool | null; null = AUTO
          "reasoning_effort": "high"        # optional str | null; null = NOT SENT
        }
      },
      "agents": {"narrator": "name", "extractor": "name"}
    }

The typed runtime fields are owner-declared runtime facts, parsed into
:class:`Connection` (unlike raw ``note``/``dims``, which ride the document only):
four come from W9.25, plus ``reasoning_effort`` (issue #26 R5).

* ``context_window`` — the owner-declared Context Size in tokens. ``null`` (or
  absent) means unknown: consumers keep today's fixed-budget behavior. NO
  dynamic detection, ever; a bool is rejected (``true`` is not ``1``).
* ``thinking`` — ``null``/absent/``true`` all mean ON (owner ruling: never
  disable thinking by default); ``false`` is the ONLY active instruction, and
  the transport translates it per provider
  (:func:`fabulist.connections.capabilities.thinking_disable_payload`).
* ``streaming`` — ``null``/absent/``true`` stream (today); ``false`` asks the
  caller for one non-streaming request.
* ``return_thinking`` — the tri-state reasoning echo: ``null`` (AUTO, the
  default) repeats a thinking model's ``reasoning_content`` back inside a tool
  loop whenever the provider sent it (DeepSeek's thinking mode 400s otherwise),
  ``true`` forces that echo, and ``false`` omits the field entirely for a
  provider that rejects it. "Sent it" includes an EMPTY ``reasoning_content`` —
  a continuation turn that reasoned nothing returns exactly that, and the value
  rides back verbatim. The rule itself lives at the loop's one decision point
  (:func:`fabulist.agents.loop.should_echo_reasoning`) — this field only
  carries the owner's setting.

``reasoning_effort``
--------------------
The DEPTH knob for a thinking model, riding the provider's own top-level body
field of the same name (``reasoning_effort``; z.ai documents it in "Chat
Completion" as `enum<string>, default max`, supported by GLM-5.2 and above,
values ``max``/``xhigh``/``high``/``medium``/``low``/``minimal``/``none`` —
GLM-5.3 accepts only ``low``/``high``/``max``). :data:`REASONING_EFFORT_VALUES`
is that documented set, and a value outside it fails the load: the field is an
enum, not free text.

``null``/absent — the default — sends NOTHING, so every other provider's request
stays byte-identical (this field must not cost a provider that never heard of
it). The transport adds it only when set (:func:`fabulist.connections.client.
build_request`) — this field only carries the owner's setting.

The value is case/whitespace-normalized (``"High"`` -> ``"high"``): the value
space is closed, so normalizing cannot change behaviour, unlike a toggle
coerced from a truthy string.

``agents`` vocabulary (W9.25 §3.1/§3.2)
--------------------------------------
The flat slot map accepts the typed spellings declared in
:mod:`fabulist.connections.taxonomy` (``completion``, ``completion.narration``,
``completion.extraction``, ``completion.state``, ``embedding``) alongside the
seven live legacy/item keys. Every dotted key MUST be a declared typed slot — a
typo (``completion.bogus``) fails loudly at load with the vocabulary; an
UNDOTTED key stays free-form (a per-item slot, exactly as today). Resolution
hierarchy (item > task > type, typed spelling beats legacy twin) lives in
``taxonomy.resolve_slot``, which :meth:`Registry.connection_for` delegates to.

Key storage (supersedes D25's literal-key refusal)
-------------------------------------------------
A key is stored in the project ``.env`` file, never in the connections document:
``POST /api/connections/{name}/key`` derives ``FABULIST_<NAME>_KEY``
(:func:`api_key_env_name`) and rewrites that ONE line (:func:`write_env_key`).
:meth:`RegistryStore.reload` re-reads that file and overlays it into
``os.environ`` (:func:`load_env_file`), so a pasted key is live on the next
lookup without a process restart, while ``api_key_env`` names set by other means
(a systemd ``EnvironmentFile``, a shell export) keep resolving unchanged.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .taxonomy import TYPED_SLOTS, declared_spellings, resolve_slot, slot_view

__all__ = [
    "CONNECTION_PATCH_FIELDS",
    "FIRST_RUN_LOG",
    "REASONING_EFFORT_VALUES",
    "Connection",
    "ConnectionConfigError",
    "Registry",
    "RegistryStore",
    "api_key_env_name",
    "clean_reasoning_effort",
    "configuration_report",
    "host_alias",
    "load_env_file",
    "load_registry",
    "parse_env_text",
    "validate_connection_patch",
    "write_env_key",
]

logger = logging.getLogger(__name__)

#: Boot log line for a checkout that has never written a connections config.
#: A missing file is a FIRST RUN, not a broken install: the engine boots with an
#: empty registry so the UI/API create door is reachable. A file that exists but
#: is malformed still fails loudly (D25 C1).
FIRST_RUN_LOG: str = (
    "no connections config found — first run; create connections via the UI or "
    "copy configs/connections.example.json"
)


# --- env file (pasted keys live here, never in the connections document) ----

#: A line this parser will accept as an env var name.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Values matching this need no quoting in the file (typical API keys do).
_ENV_RAW_VALUE = re.compile(r"^[A-Za-z0-9_@%+:,./=-]*$")


def api_key_env_name(connection_name: str) -> str:
    """The env var a pasted key for ``connection_name`` is stored under.

    ``FABULIST_<NAME>_KEY``: the connection name uppercased with every
    non-alphanumeric character replaced by ``_`` (``"your-model-id"`` ->
    ``"FABULIST_YOUR_MODEL_ID_KEY"``).
    """
    return f"FABULIST_{re.sub(r'[^A-Z0-9]', '_', connection_name.upper())}_KEY"


def _parse_env_line(raw: str) -> tuple[str, str] | None:
    """Parse one dotenv line into ``(name, value)``; ``None`` when it is not one.

    Tolerates a leading ``export ``. A value may be double-quoted (backslash
    escapes the next character) or single-quoted (taken literally); an unquoted
    value is taken verbatim after surrounding whitespace is stripped.
    """
    line = raw.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[len("export ") :].strip()
    name, sep, value = line.partition("=")
    name = name.strip()
    if not sep or not _ENV_NAME.match(name):
        return None
    return name, _env_value(value.strip())


def _env_value(raw: str) -> str:
    """Unquote a dotenv value (see :func:`_parse_env_line`)."""
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        out: list[str] = []
        escape = False
        for char in raw[1:-1]:
            if escape:
                out.append(char)
                escape = False
            elif char == "\\":
                escape = True
            else:
                out.append(char)
        if escape:
            out.append("\\")
        return "".join(out)
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    return raw


def _env_line(name: str, value: str) -> str:
    """Render one ``name=value`` line, quoting the value when it needs it."""
    if _ENV_RAW_VALUE.match(value):
        return f"{name}={value}"
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'{name}="{escaped}"'


def parse_env_text(text: str) -> dict[str, str]:
    """Parse the ``KEY=value`` lines of a dotenv-style file's text.

    Deliberately minimal: blank lines, ``#`` comments, and anything that is not
    a ``NAME=value`` line with an env-var-shaped name are skipped rather than
    guessed at. This is not a dotenv library — it is the one shape
    :func:`write_env_key` writes. A repeated name wins last, as in a shell.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        parsed = _parse_env_line(raw)
        if parsed is not None:
            name, value = parsed
            values[name] = value
    return values


def load_env_file(path: str | os.PathLike[str]) -> dict[str, str]:
    """Parse the env file at ``path`` and overlay it into ``os.environ``.

    A missing (or unreadable) file is a no-op, so a checkout without one — where
    keys come from the engine service's ``EnvironmentFile`` — behaves exactly as
    before. Names present in the file WIN over an already-set variable: the file
    is the newest write path (a pasted key), and every name it does not mention
    is left untouched. Returns the overlay that was applied.
    """
    env_path = Path(path)
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    values = parse_env_text(text)
    os.environ.update(values)
    return values


def write_env_key(path: str | os.PathLike[str], name: str, value: str) -> None:
    """Set the single ``name=value`` line of the env file; create it if absent.

    Every other line is preserved verbatim, so the file can keep unrelated
    secrets. A missing parent directory is created too (the env file is
    configurable, so its directory is not assumed to exist). The write is atomic
    (sibling tmp file + ``os.replace``) and the result is mode ``0o600`` — the
    file holds credentials.
    """
    env_path = Path(path)
    try:
        existing = env_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""

    line = _env_line(name, value)
    lines: list[str] = []
    replaced = False
    for raw in existing.splitlines():
        parsed = _parse_env_line(raw)
        if parsed is not None and parsed[0] == name:
            if not replaced:  # first mention is rewritten; later ones dropped
                lines.append(line)
                replaced = True
            continue
        lines.append(raw)
    if not replaced:
        lines.append(line)

    tmp = env_path.with_name(env_path.name + ".tmp")
    env_path.parent.mkdir(parents=True, exist_ok=True)  # fresh clone: no dir yet
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, env_path)


class ConnectionConfigError(ValueError):
    """A connections config is malformed, incomplete, or references a missing entry."""


@dataclass(frozen=True)
class Connection:
    """A single resolved endpoint + model + sampler entry."""

    name: str
    base_url: str
    model: str
    api_key_env: str | None = None
    api_key: str | None = None
    timeout: float | None = None
    #: The WALL-CLOCK bound (seconds) ONE model call on this connection may take,
    #: from send to last frame. ``None`` = :data:`fabulist.connections.client.
    #: DEFAULT_CALL_TIMEOUT_S` (900s). Distinct from ``timeout`` on purpose:
    #: ``timeout`` bounds each READ, and a stream resets it on every delta, so a
    #: trickling provider is never silent and only a total bound ends the call
    #: (issue #25).
    call_timeout_s: float | None = None
    #: True when ``FABULIST_HOST_ALIAS`` replaced a loopback host in
    #: ``base_url``. A DIAGNOSTIC fact only: ``base_url`` above is already the
    #: resolved URL the transport dials, and is what every consumer uses.
    base_url_rewritten: bool = False
    #: Owner-declared Context Size in tokens (W9.25). ``None`` = unknown -> the
    #: consumers keep today's fixed-budget behavior. Never detected dynamically.
    context_window: int | None = None
    #: Thinking toggle: ``None``/``True`` = ON (the owner ruling), ``False`` =
    #: the only active instruction (translated per provider by the transport).
    thinking: bool | None = None
    #: Streaming toggle: ``None``/``True`` = stream (today), ``False`` = one
    #: non-streaming request with the caller synthesizing the delta protocol.
    streaming: bool | None = None
    #: Reasoning-echo toggle ("Return thinking"): ``None`` = AUTO (echo the
    #: provider's ``reasoning_content`` back inside a tool loop whenever it was
    #: returned — what makes DeepSeek thinking + tools work unconfigured),
    #: ``True`` = force that echo, ``False`` = never send the field.
    return_thinking: bool | None = None
    #: Reasoning DEPTH for a thinking model, one of
    #: :data:`REASONING_EFFORT_VALUES` (z.ai's ``reasoning_effort`` body field).
    #: ``None`` = send NOTHING — the default, and what keeps every provider that
    #: does not document the field on a byte-identical request.
    reasoning_effort: str | None = None
    extra_body: dict[str, Any] = field(default_factory=dict)
    sampling: dict[str, Any] = field(default_factory=dict)
    #: Explicit provider-capability overrides (W8.4). Empty means "resolve from
    #: the provider profile" (see ``fabulist.connections.capabilities``); never
    #: a feature gate by itself — an unset/unmeasured capability is False.
    capabilities: dict[str, bool] = field(default_factory=dict)


@dataclass(frozen=True)
class Registry:
    """Connections plus the agent -> connection mapping."""

    connections: dict[str, Connection]
    agents: dict[str, str]

    def connection_for(self, agent_name: str) -> Connection:
        """Resolve a logical slot name to its connection (W9.25 §3.2 hierarchy).

        The slot resolves through :func:`taxonomy.resolve_slot`: the name's twin
        pair (typed spelling first, so ``narrator`` and ``completion.narration``
        are ONE lookup), then the item's affinity task key, then the type key.
        This is the single chokepoint every consumer rides (``build_request``,
        ``get_client``, ``DecisionCaller``, ``resolve_embedder``), so the whole
        app inherits the hierarchy from here.
        """
        connection_name = resolve_slot(self.agents, agent_name)
        if connection_name is None:
            known = ", ".join(sorted(self.agents)) or "<none>"
            declared = ", ".join(sorted(declared_spellings()))
            raise ConnectionConfigError(
                f"agents: unknown agent {agent_name!r}; known agents: {known}; "
                f"declared slots: {declared}"
            )
        return self.connections[connection_name]

    @classmethod
    def from_dict(cls, data: Any) -> "Registry":
        if not isinstance(data, dict):
            raise ConnectionConfigError("config: root must be a JSON object")

        raw_connections = data.get("connections")
        if not isinstance(raw_connections, dict) or not raw_connections:
            raise ConnectionConfigError("config: 'connections' must be a non-empty object")

        connections: dict[str, Connection] = {}
        for name, raw in raw_connections.items():
            connections[name] = _parse_connection(name, raw)

        raw_agents = data.get("agents", {})
        if not isinstance(raw_agents, dict):
            raise ConnectionConfigError("config: 'agents' must be an object")

        undeclared = sorted(
            key
            for key in raw_agents
            if isinstance(key, str) and "." in key and key not in TYPED_SLOTS
        )
        if undeclared:
            raise ConnectionConfigError(
                f"config: 'agents' has undeclared typed slot(s): {', '.join(undeclared)}; "
                f"declared typed slots: {', '.join(TYPED_SLOTS)}"
            )

        agents: dict[str, str] = {}
        for agent_name, connection_name in raw_agents.items():
            if not isinstance(connection_name, str):
                raise ConnectionConfigError(
                    f"agents[{agent_name!r}]: must be a connection name string"
                )
            if connection_name not in connections:
                raise ConnectionConfigError(
                    f"agents[{agent_name!r}]: unknown connection {connection_name!r}"
                )
            agents[agent_name] = connection_name

        return cls(connections=connections, agents=agents)

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> "Registry":
        return load_registry(path)


def configuration_report(registry: Registry | RegistryStore | None) -> dict[str, Any]:
    """The configuration half of ``GET /health`` — cheap, dependency-free.

    Reports only what tells a human whether this run is CONFIGURED: the
    narrator slot's connection NAME, the declared agent slots that resolve to
    nothing, and the config document's resolved path plus whether it exists.
    Nothing here calls a model, writes to the log, or re-reads a file: one
    ``stat`` at most, so the endpoint is safe to poll.

    A missing document (first-run checkout) is the case this exists FOR, not an
    error: the registry is then empty, the narrator is ``None``, every declared
    slot is unbound and ``config_exists`` is ``False``.

    The host-alias half (issue #33) reports the ACTIVE alias (``None`` when the
    rewrite is off) and, per connection, the RESOLVED ``base_url`` plus whether
    the alias rewrote it. That is the whole point of the endpoint here: a
    container can say exactly which URL it will dial without a model call —
    which is how "the deployment cannot reach the host" became invisible.
    Reading ``os.environ`` and the in-memory snapshot is free; the one-file-``stat``
    contract is unchanged (no reload, no probe).

    Only NAMES and URLs leave here. A connection's ``api_key`` and the
    environment value its ``api_key_env`` names are never read, so neither can be
    leaked.
    """
    agents = dict(getattr(registry, "agents", None) or {})
    path = getattr(registry, "path", None)
    connections = getattr(registry, "connections", None) or {}
    return {
        "narrator": resolve_slot(agents, "narrator"),
        "unbound_slots": [
            row["slot"] for row in slot_view(agents) if row["connection"] is None
        ],
        "config_path": None if path is None else str(path),
        "config_exists": None if path is None else Path(path).exists(),
        # issue #33: the deploy seam made visible. ``None`` = no rewrite active.
        "host_alias": host_alias(),
        "connections": {
            name: {
                "base_url": conn.base_url,
                "base_url_rewritten": bool(conn.base_url_rewritten),
            }
            for name, conn in connections.items()
        },
    }


def load_registry(path: str | os.PathLike[str]) -> Registry:
    """Read and validate a connections JSON file.

    A MISSING file is a first-run checkout: it loads as an empty registry (zero
    connections, zero agent bindings) instead of raising, so the boot path can
    serve the API that creates one. An existing-but-malformed file still raises
    :class:`ConnectionConfigError`.
    """
    return _read_valid(path)[0]


def _read_valid(path: str | os.PathLike[str]) -> tuple[Registry, dict[str, Any]]:
    """Read + validate a config file, returning ``(Registry, raw_document)``.

    The raw parsed JSON is retained alongside the typed registry so writers can
    round-trip unknown keys verbatim (D25 C1: preserve the user's format). For a
    missing file the raw document is ``{}`` — exactly what the first write should
    start from (``add_connection`` creates the ``connections`` object).
    """
    config_path = Path(path)
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.warning("%s: %s", config_path, FIRST_RUN_LOG)
        return Registry(connections={}, agents={}), {}
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConnectionConfigError(f"{config_path}: invalid JSON: {exc}") from None
    return Registry.from_dict(data), data


#: A URL scheme prefix (``http://``, ``https://``, …).
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


#: Connection-entry fields a PATCH may change. ``base_url`` is deliberately
#: excluded (D25 C1): rebinding a live caller's endpoint is out of scope, so a
#: patch naming it is rejected rather than silently ignored.
CONNECTION_PATCH_FIELDS: frozenset[str] = frozenset(
    {
        "model",
        "sampling",
        "extra_body",
        "timeout",
        "call_timeout_s",
        "api_key_env",
        "capabilities",
        "context_window",
        "thinking",
        "streaming",
        "return_thinking",
        "reasoning_effort",
    }
)


def _clean_capabilities(value: Any, where: str) -> dict[str, bool]:
    """Validate a ``capabilities`` object: a map of capability name -> bool.

    ``None`` normalises to ``{}``. Values MUST be booleans: a truthy string
    (``"yes"``/``"true"``) is rejected rather than coerced, because capability
    gating flips transport behaviour (a mis-set string must not enable it).
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConnectionConfigError(f"{where}: must be an object")
    bad = sorted(str(k) for k, v in value.items() if not isinstance(v, bool))
    if bad:
        raise ConnectionConfigError(
            f"{where}: values must be booleans (non-bool for {', '.join(bad)})"
        )
    return dict(value)


def _clean_optional_bool(value: Any, where: str) -> bool | None:
    """Validate a nullable boolean toggle: ``True``/``False``/``None`` only.

    A truthy string (``"yes"``/``"false"``) or a number is REJECTED rather than
    coerced: every optional-bool toggle flips transport behaviour, so a mis-set
    value must fail loudly (the same discipline as ``capabilities``).
    """
    if value is None or isinstance(value, bool):
        return value
    raise ConnectionConfigError(f"{where}: must be true, false, or null")


#: The documented ``reasoning_effort`` value set (z.ai "Chat Completion" body
#: reference, verified 2026-10-23: `enum<string>, default max`, supported by
#: GLM-5.2 and above). The provider narrows this per model (GLM-5.3 accepts only
#: ``low``/``high``/``max``) and maps the compatible spellings on GLM-5.2 — that
#: narrowing is the provider's business; this is the set its docs allow, so a
#: typo fails here with the vocabulary instead of as a 400 at turn time.
REASONING_EFFORT_VALUES: frozenset[str] = frozenset(
    {"max", "xhigh", "high", "medium", "low", "minimal", "none"}
)


def clean_reasoning_effort(value: Any, where: str) -> str | None:
    """Validate the reasoning-depth enum against :data:`REASONING_EFFORT_VALUES`.

    ``None`` (absent) means "send nothing". A non-string, an empty string or a
    value outside the documented set is refused with the allowed values named —
    a mistyped effort must never reach the provider as a mystery 400.

    Public so the HTTP write surface (:mod:`fabulist.api.connections_api`) can
    reject a bad value with the SAME vocabulary instead of duplicating the
    enum at the API layer.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConnectionConfigError(
            f"{where}: must be one of {', '.join(sorted(REASONING_EFFORT_VALUES))} "
            "or null"
        )
    effort = value.strip().lower()
    if effort not in REASONING_EFFORT_VALUES:
        raise ConnectionConfigError(
            f"{where}: {value!r} is not a documented reasoning effort "
            f"(allowed: {', '.join(sorted(REASONING_EFFORT_VALUES))})"
        )
    return effort


#: Backwards-compatible private alias (the promotion must not break a caller).
_clean_reasoning_effort = clean_reasoning_effort


def _clean_context_window(value: Any, where: str) -> int | None:
    """Validate the owner-declared Context Size: an int >= 1, or ``None``.

    ``bool`` is rejected explicitly (``true`` is not window ``1``) and floats are
    rejected (a token count is an integer), matching the strict discipline.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConnectionConfigError(f"{where}: must be an integer >= 1 or null")
    return value


def validate_connection_patch(name: str, patch: Any) -> dict[str, Any]:
    """Validate + normalize a connection PATCH body; raise on anything invalid.

    Only :data:`CONNECTION_PATCH_FIELDS` are accepted. Object fields are
    replaced wholesale (shallow), matching JSON-merge-patch semantics for the
    one entry. Returns a fresh dict safe to merge into the raw document.
    """
    where = f"connections[{name!r}]"
    if not isinstance(patch, dict):
        raise ConnectionConfigError(f"{where}: patch must be an object")
    unknown = sorted(set(patch) - CONNECTION_PATCH_FIELDS)
    if unknown:
        allowed = ", ".join(sorted(CONNECTION_PATCH_FIELDS))
        raise ConnectionConfigError(
            f"{where}: not patchable: {', '.join(unknown)} (allowed: {allowed})"
        )

    clean: dict[str, Any] = {}
    if "model" in patch:
        model = patch["model"]
        if not isinstance(model, str) or not model.strip():
            raise ConnectionConfigError(f"{where}.model: must be a non-empty string")
        clean["model"] = model
    if "api_key_env" in patch:
        env = patch["api_key_env"]
        if env is not None and (not isinstance(env, str) or not env.strip()):
            raise ConnectionConfigError(
                f"{where}.api_key_env: must be a non-empty env var name or null"
            )
        clean["api_key_env"] = env
    for key in ("sampling", "extra_body"):
        if key in patch:
            value = patch[key]
            if value is None:
                value = {}
            if not isinstance(value, dict):
                raise ConnectionConfigError(f"{where}.{key}: must be an object")
            clean[key] = dict(value)
    if "capabilities" in patch:
        clean["capabilities"] = _clean_capabilities(
            patch["capabilities"], f"{where}.capabilities"
        )
    if "timeout" in patch:
        timeout = patch["timeout"]
        if timeout is not None and (
            isinstance(timeout, bool) or not isinstance(timeout, (int, float))
        ):
            raise ConnectionConfigError(f"{where}.timeout: must be a number of seconds or null")
        clean["timeout"] = None if timeout is None else float(timeout)
    if "call_timeout_s" in patch:
        clean["call_timeout_s"] = _clean_call_timeout_s(
            patch["call_timeout_s"], f"{where}.call_timeout_s"
        )
    if "context_window" in patch:
        clean["context_window"] = _clean_context_window(
            patch["context_window"], f"{where}.context_window"
        )
    for key in ("thinking", "streaming", "return_thinking"):
        if key in patch:
            clean[key] = _clean_optional_bool(patch[key], f"{where}.{key}")
    if "reasoning_effort" in patch:
        clean["reasoning_effort"] = clean_reasoning_effort(
            patch["reasoning_effort"], f"{where}.reasoning_effort"
        )
    return clean


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Persist ``data`` as pretty JSON via a sibling tmp file + ``os.replace``.

    The temp file lives beside the target so the replace is atomic on one
    filesystem; a reader (or a crash) never observes a half-written config. The
    parent directory is created when absent: ``configs/`` does not exist in a
    fresh clone, and the API create door must work there.
    """
    text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


class RegistryStore:
    """Hot-reloadable view over a connections config file (D25 C1).

    Duck-types :class:`Registry` (``connection_for`` / ``connections`` /
    ``agents``) so any component already handed a registry keeps working when
    the document is patched in place: every lookup reads the *current*
    snapshot and :meth:`update_connection` swaps that snapshot after an atomic
    write. That is what makes a PATCH take effect on the NEXT request without a
    process restart — callers hold the store, not a frozen registry copy.

    The parsed JSON document is retained verbatim (unknown keys included) so a
    write rewrites only the fields it touches and never drops user keys.
    Thread-safe: lookups and writes share one re-entrant lock (the FastAPI
    threadpool may run sync handlers concurrently).

    ``env_path`` is the dotenv file pasted keys are written to. It defaults to
    the project-root ``.env`` (the engine's working directory); tests pass a
    scratch file. :meth:`reload` re-reads it, so a key pasted while the engine
    runs resolves on the next lookup without a restart.

    A missing ``path`` is a first-run checkout, not an error: the store starts
    EMPTY (see :data:`FIRST_RUN_LOG`) and the first ``add_connection`` creates
    the file, its parent directory included.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        env_path: str | os.PathLike[str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.env_path = Path(env_path) if env_path is not None else Path(
            os.environ.get("FABULIST_ENV_FILE") or (Path.cwd() / ".env")
        )
        self._lock = threading.RLock()
        self._raw: dict[str, Any] = {}
        self._registry: Registry | None = None
        self.reload()

    # -- reads ---------------------------------------------------------

    def reload(self) -> Registry:
        """Re-read the env file AND the config, installing the fresh snapshot.

        The env overlay runs first: a key pasted into ``.env`` must be in
        ``os.environ`` before the document's ``api_key_env`` names are resolved,
        otherwise the reloaded registry would carry a stale key. A config file
        that is still missing reloads as the empty (first-run) registry.
        """
        load_env_file(self.env_path)
        registry, raw = _read_valid(self.path)
        with self._lock:
            self._registry = registry
            self._raw = raw
        return registry

    @property
    def registry(self) -> Registry:
        with self._lock:
            if self._registry is None:  # pragma: no cover - set by reload()
                raise ConnectionConfigError(f"{self.path}: registry not loaded")
            return self._registry

    @property
    def connections(self) -> dict[str, Connection]:
        return self.registry.connections

    @property
    def agents(self) -> dict[str, str]:
        return self.registry.agents

    def connection_for(self, agent_name: str) -> Connection:
        return self.registry.connection_for(agent_name)

    def raw(self) -> dict[str, Any]:
        """A deep copy of the current parsed document (unknown keys included)."""
        with self._lock:
            return copy.deepcopy(self._raw)

    # -- writes --------------------------------------------------------

    def update_connection(self, name: str, patch: Any) -> Registry:
        """Merge ``patch`` into connection ``name``, persist atomically, reload.

        Raises :class:`KeyError` when ``name`` is unknown (the router maps it to
        ``404``) and :class:`ConnectionConfigError` for an invalid patch. The
        whole new document is validated BEFORE the write, so a rejected patch
        leaves the file untouched.
        """
        clean = validate_connection_patch(name, patch)
        with self._lock:
            data = copy.deepcopy(self._raw)
            connections = data.get("connections")
            if not isinstance(connections, dict) or name not in connections:
                raise KeyError(name)
            entry = connections[name]
            if not isinstance(entry, dict):
                raise ConnectionConfigError(f"connections[{name!r}]: must be an object")
            entry.update(clean)
            registry = Registry.from_dict(data)
            _atomic_write_json(self.path, data)
            self._registry = registry
            self._raw = data
        return registry

    def bind_agent(self, slot: str, connection: str) -> Registry:
        """Point ``slot`` at ``connection``, persist atomically and reload.

        The write-side twin of :meth:`add_connection` for the ``agents`` map:
        the slot is UPSERTED (authoring a declared spelling the document does
        not carry yet is the ordinary rebind path — the legacy/item key stays in
        the file, untouched and inert), the WHOLE resulting document is
        validated with :meth:`Registry.from_dict` before anything is written,
        and the live snapshot is swapped so every caller already holding the
        store resolves the new binding on its next lookup.

        Raises :class:`KeyError` for an unknown connection and
        :class:`ConnectionConfigError` when the merged document is invalid
        (including a binding to a connection that does not exist). A refused
        write leaves the file untouched.

        Issue #36 uses this for the sidecar auto-pin (the same discipline a
        hand-made connection goes through), and the connections router's
        ``PATCH /api/connections/agents/{agent}`` delegates here so the two
        doors cannot drift.
        """
        with self._lock:
            data = copy.deepcopy(self._raw)
            agents = data.get("agents")
            if not isinstance(agents, dict):
                agents = {}
                data["agents"] = agents
            if connection not in self.registry.connections:
                raise KeyError(connection)
            agents[slot] = connection
            registry = Registry.from_dict(data)  # validate BEFORE the write
            _atomic_write_json(self.path, data)
            self._registry = registry
            self._raw = data
        return registry

    def add_connection(self, name: str, entry: Any) -> Registry:
        """Create a NEW connection entry, persist atomically and reload.

        Raises :class:`KeyError` when ``name`` already exists (``409``) and
        :class:`ConnectionConfigError` when the entry is not a valid connection
        (it must carry ``base_url`` + ``model``). The connection is not bound to
        any agent here — that stays the caller's job.
        """
        parsed = _parse_connection(name, entry)  # validate the entry shape
        with self._lock:
            data = copy.deepcopy(self._raw)
            connections = data.setdefault("connections", {})
            if not isinstance(connections, dict):  # pragma: no cover - malformed doc
                raise ConnectionConfigError("config: 'connections' must be an object")
            if name in connections:
                raise KeyError(name)
            connections[name] = {
                "base_url": parsed.base_url,
                "model": parsed.model,
                **(
                    {"context_window": parsed.context_window}
                    if parsed.context_window is not None
                    else {}
                ),
                **(
                    {"thinking": parsed.thinking}
                    if parsed.thinking is not None
                    else {}
                ),
                **(
                    {"streaming": parsed.streaming}
                    if parsed.streaming is not None
                    else {}
                ),
                **(
                    {"return_thinking": parsed.return_thinking}
                    if parsed.return_thinking is not None
                    else {}
                ),
                **(
                    {"reasoning_effort": parsed.reasoning_effort}
                    if parsed.reasoning_effort is not None
                    else {}
                ),
                **(
                    {"api_key_env": parsed.api_key_env}
                    if parsed.api_key_env is not None
                    else {}
                ),
                **({"extra_body": parsed.extra_body} if parsed.extra_body else {}),
                **({"sampling": parsed.sampling} if parsed.sampling else {}),
                **(
                    {"capabilities": parsed.capabilities}
                    if parsed.capabilities
                    else {}
                ),
                **({"timeout": parsed.timeout} if parsed.timeout is not None else {}),
                **(
                    {"call_timeout_s": parsed.call_timeout_s}
                    if parsed.call_timeout_s is not None
                    else {}
                ),
            }
            registry = Registry.from_dict(data)
            _atomic_write_json(self.path, data)
            self._registry = registry
            self._raw = data
        return registry


#: Host names that mean "this machine" from the ENGINE's own process. Inside a
#: container they name the container, not the host, so a host-local model server
#: is unreachable. ``FABULIST_HOST_ALIAS`` is the deploy seam that substitutes a
#: routable name for exactly these three (issue #33).
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

#: Env var naming the host to substitute for a loopback host. Unset/empty = no
#: rewrite at all, so the bare image keeps today's raw behaviour (no magic).
HOST_ALIAS_ENV = "FABULIST_HOST_ALIAS"


def host_alias() -> str | None:
    """The active loopback host alias, or ``None`` when the rewrite is off.

    Read at call time (not import time) so a container's environment and a
    test's ``monkeypatch`` both take effect; mirroring how ``FABULIST_ENV_FILE``
    is read, an empty value is the same as unset.
    """
    value = os.environ.get(HOST_ALIAS_ENV, "").strip()
    return value or None


def _rewrite_loopback_host(url: str, parts: Any) -> tuple[str, bool]:
    """Replace ONLY the host of an http(s) loopback URL with the alias.

    Returns ``(url, rewritten)``. Surgery is textual on the netloc span so the
    scheme's case, the port, the path and the query stay byte-for-byte intact
    (``urlsplit`` lowercases the scheme, so rebuilding via ``urlunsplit`` would
    not). A non-loopback host — a remote provider, a LAN IP, a compose service
    name — is returned untouched. Idempotent: once the URL names the alias its
    host is no longer loopback, so a second pass is a no-op.
    """
    alias = host_alias()
    if not alias or parts.scheme not in ("http", "https"):
        return url, False
    host = parts.hostname
    if host is None or host not in LOOPBACK_HOSTS:
        return url, False

    authority = parts.netloc
    # ``[userinfo@]host[:port]``; userinfo is preserved verbatim.
    userinfo, at, host_port = authority.rpartition("@")
    literal = f"[{host}]" if ":" in host else host  # ::1 is bracketed in a URL
    if not host_port.lower().startswith(literal):
        return url, False  # defensive: never rewrite a netloc we do not understand
    remainder = host_port[len(literal):]  # '' or ':port'
    replacement = f"[{alias}]" if ":" in alias and not alias.startswith("[") else alias
    if replacement == host_port[: len(literal)]:
        return url, False

    scheme_end = url.index("://") + 3
    netloc_end = scheme_end + len(authority)
    rewritten = (
        url[:scheme_end]
        + f"{userinfo}{at}{replacement}{remainder}"
        + url[netloc_end:]
    )
    return rewritten, rewritten != url


def _normalize_base_url(value: str, where: str) -> str:
    """Normalize a connection base URL (owner ruling, 2026-10-23).

    A bare ``host:port`` gains ``http://`` (local servers are plain HTTP) and
    ``/v1`` — the shape the drawer hint teaches.  An EXPLICIT path is preserved
    verbatim: provider paths are not all ``/v1`` (z.ai serves ``/api/paas/v4``,
    OpenRouter ``/api/v1``), so appending blindly would break them.  Trailing
    slashes are stripped either way; the value is what the OpenAI client
    receives, so it must be exact.

    Every load/add path ends here, so the optional ``FABULIST_HOST_ALIAS``
    rewrite is applied in the same one place (issue #33): only a loopback host
    of an http/https URL is replaced, and only when the env var is non-empty.
    """
    return _resolve_base_url(value, where)[0]


def _resolve_base_url(value: str, where: str) -> tuple[str, bool]:
    """Normalize ``value`` and report whether the host alias rewrote it."""
    url = value.strip().rstrip("/")
    if not _SCHEME_RE.match(url):
        url = "http://" + url
    parts = urlsplit(url)
    if not parts.netloc:
        raise ConnectionConfigError(f"{where}.base_url: not a usable URL: {value!r}")
    url, rewritten = _rewrite_loopback_host(url, parts)
    if parts.path in ("", "/"):
        url = url + "/v1"
    return url, rewritten


def _parse_connection(name: str, raw: Any) -> Connection:
    where = f"connections[{name!r}]"
    if not isinstance(raw, dict):
        raise ConnectionConfigError(f"{where}: must be an object")

    base_url, base_url_rewritten = _resolve_base_url(
        _require_str(raw, "base_url", where), where
    )
    model = _require_str(raw, "model", where)

    api_key_env = raw.get("api_key_env")
    if api_key_env is not None and not isinstance(api_key_env, str):
        raise ConnectionConfigError(f"{where}.api_key_env: must be an env var name string")
    api_key = os.environ.get(api_key_env) if api_key_env else None

    extra_body = _optional_dict(raw, "extra_body", where)
    sampling = _optional_dict(raw, "sampling", where)
    capabilities = _clean_capabilities(raw.get("capabilities"), f"{where}.capabilities")

    timeout = raw.get("timeout")
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float))):
        raise ConnectionConfigError(f"{where}.timeout: must be a number of seconds")

    return Connection(
        name=name,
        base_url=base_url,
        base_url_rewritten=base_url_rewritten,
        model=model,
        api_key_env=api_key_env,
        api_key=api_key,
        timeout=float(timeout) if timeout is not None else None,
        call_timeout_s=_clean_call_timeout_s(
            raw.get("call_timeout_s"), f"{where}.call_timeout_s"
        ),
        context_window=_clean_context_window(
            raw.get("context_window"), f"{where}.context_window"
        ),
        thinking=_clean_optional_bool(raw.get("thinking"), f"{where}.thinking"),
        streaming=_clean_optional_bool(raw.get("streaming"), f"{where}.streaming"),
        return_thinking=_clean_optional_bool(
            raw.get("return_thinking"), f"{where}.return_thinking"
        ),
        reasoning_effort=clean_reasoning_effort(
            raw.get("reasoning_effort"), f"{where}.reasoning_effort"
        ),
        extra_body=extra_body,
        sampling=sampling,
        capabilities=capabilities,
    )


def _clean_call_timeout_s(value: Any, where: str) -> float | None:
    """Validate ``call_timeout_s``: a POSITIVE number of seconds, or null.

    ``None`` means "use the 900s default". Zero/negative is refused rather than
    silently reinterpreted: a call deadline of nothing would end every call at
    once, and that is a typo, not a setting.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConnectionConfigError(
            f"{where}: must be a positive number of seconds or null"
        )
    seconds = float(value)
    if seconds <= 0:
        raise ConnectionConfigError(f"{where}: must be greater than zero seconds")
    return seconds


def _require_str(raw: dict[str, Any], key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ConnectionConfigError(f"{where}: missing required string field {key!r}")
    return value


def _optional_dict(raw: dict[str, Any], key: str, where: str) -> dict[str, Any]:
    value = raw.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConnectionConfigError(f"{where}.{key}: must be an object")
    return dict(value)
