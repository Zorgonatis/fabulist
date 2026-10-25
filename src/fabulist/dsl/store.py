"""Hot-reloadable component-library store (W9.27; the ``RegistryStore`` pattern).

The component artifacts — ``library.spec.json``, ``library.doc.txt`` and the
``set.json`` manifest that describes the set — are **data**, not code. The
``:2727`` engine is a code snapshot from process start, but these files are
re-read here, so regenerating them (or swapping the active component set) takes
effect on the NEXT turn with no code edit and no restart. That is the seam that
kills the frontend/engine vocabulary drift this epic exists for: one generated
spec feeds the renderer, the gate, the strip helper and the taught doc.

A *set* is a self-contained directory:

    <set-dir>/
      set.json             # {"id", "name", "version", "components", "spec_version"}
      library.spec.json    # the authoritative LibrarySpec
      library.doc.txt      # the generated narrator doc

The ``version`` key is the set's own identity (e.g. ``1.0.0``), distinct from
the store's ``version`` counter, which answers "which bytes did this consumer
last see?".

SELECTION IS NOT THIS STORE'S BUSINESS (issues #16/#24). A session's active set
is a LOG FACT — the latest ``set_swapped`` row in its chain — and
:func:`fabulist.dsl.sets.session_snapshot` resolves that fact to a directory and
binds it here, per session id. What this store owns is CONTENT: one cached,
versioned snapshot per set DIRECTORY, shared by every session that selects it (a
snapshot is content, not selection).

Resolution order: ``FABULIST_LIBRARY_ARTIFACT_DIR`` / ``FABULIST_DSL_SPEC``
(env wins — tests and deployments) → the current task's focused set directory
(the per-turn/per-request resolution) → the current session's bound set
directory → the default ``prompts/generated/``. The env is re-resolved on EVERY
call: ~20 tests monkeypatch it and reset, and a deployment must be able to
repoint the engine without a restart.

Failure discipline (the generator writes tmp+rename, so a torn read should never
happen — but "should never" is not a guarantee):

* a MISSING artifact is the documented fallback path (fresh checkout before the
  generator ran; the consumers then fall back to ``flavors.json`` + the four
  infrastructure components);
* a PRESENT but corrupt artifact keeps the LAST GOOD snapshot for the same
  paths — the running vocabulary never downgrades silently. :meth:`last_error`
  reports why.

The swap is applied by the SELECTION DOOR, not here: it validates the set and
appends the ``set_swapped`` FACT to the session log (D9 — the dispatcher owns
the log, the store writes no rows). The narrator builder's ``build()`` start
then resolves that fact for the session and binds the directory here, so the
taught doc, the gate table and the strip list describe the same set on the same
turn. The model-facing announcement is the log row's frozen ``notice`` (read by
:func:`fabulist.context.facts.set_swap_notice`), never store process state, so a
preview render cannot consume it.

Import graph: this module imports nothing from ``fabulist.dsl.spec`` or
``fabulist.prompts`` — :mod:`fabulist.dsl.spec` and
:mod:`fabulist.prompts.library_mini` are readers over it, not the reverse.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = [
    "ARTIFACT_DIR_ENV_VAR",
    "DOC_FILENAME",
    "FLAVORS_ENV_VAR",
    "MANIFEST_FILENAME",
    "SPEC_ENV_VAR",
    "SPEC_FILENAME",
    "ComponentLibraryStore",
    "ComponentSetError",
    "ComponentSetSnapshot",
    "ComponentSetSwap",
    "component_names_in",
    "iter_component_entries",
    "library_store",
    "resolve_artifact_dir",
    "resolve_flavors_path",
    "resolve_spec_path",
]

#: Env override for the authoritative spec file.
SPEC_ENV_VAR = "FABULIST_DSL_SPEC"
#: Env override for the component-set directory (spec + doc + ``set.json``).
ARTIFACT_DIR_ENV_VAR = "FABULIST_LIBRARY_ARTIFACT_DIR"
#: Env override for the ``flavors.json`` mirror (fallback vocabulary only).
FLAVORS_ENV_VAR = "FABULIST_FLAVORS_JSON"

SPEC_FILENAME = "library.spec.json"
DOC_FILENAME = "library.doc.txt"
MANIFEST_FILENAME = "set.json"


def _repo_root() -> Path:
    # src/fabulist/dsl/store.py -> repo root is parents[3].
    return Path(__file__).resolve().parents[3]


def resolve_artifact_dir() -> Path:
    """The active artifact directory (env override wins, re-read every call)."""
    override = os.environ.get(ARTIFACT_DIR_ENV_VAR)
    if override:
        return Path(override)
    return _repo_root() / "prompts" / "generated"


def resolve_spec_path(directory: Path | None = None) -> Path:
    """The authoritative ``library.spec.json`` path (``FABULIST_DSL_SPEC`` wins)."""
    override = os.environ.get(SPEC_ENV_VAR)
    if override:
        return Path(override)
    base = directory if directory is not None else resolve_artifact_dir()
    return base / SPEC_FILENAME


def resolve_flavors_path() -> Path:
    """The ``flavors.json`` mirror path (fallback vocabulary; env override wins)."""
    override = os.environ.get(FLAVORS_ENV_VAR)
    if override:
        return Path(override)
    return (
        _repo_root()
        / "frontend"
        / "src"
        / "lib"
        / "openui"
        / "schemas"
        / "flavors.json"
    )


def iter_component_entries(raw: Any, keys: tuple[str, ...]) -> Iterator[tuple[str, Any]]:
    """Yield ``(name, entry)`` from a mapping or list under any of ``keys``."""
    if isinstance(raw, Mapping):
        container: Any = None
        for key in keys:
            if key in raw:
                container = raw[key]
                break
        if container is None:
            # A bare {name: entry} mapping.
            container = raw
    else:
        container = raw

    if isinstance(container, Mapping):
        for name, entry in container.items():
            if isinstance(name, str):
                yield name, entry
        return
    if isinstance(container, (list, tuple)):
        for entry in container:
            if isinstance(entry, str):
                yield entry, {}
            elif isinstance(entry, Mapping):
                name = entry.get("name") or entry.get("component") or entry.get("id")
                if isinstance(name, str) and name:
                    yield name, entry


def component_names_in(raw: Any) -> tuple[str, ...]:
    """Every component name a raw spec document declares, in document order."""
    return tuple(name for name, _entry in iter_component_entries(raw, ("components",)))


@dataclass(frozen=True)
class ComponentSetSnapshot:
    """One loaded component set: the data every vocabulary consumer reads.

    ``version`` is monotonic within the store and bumps whenever THIS
    directory's artifacts are (re)loaded, so consumers key their derived caches
    on it — a regenerated or swapped artifact invalidates them without a
    restart. Every session selecting the same directory shares the snapshot
    (and therefore the version). ``spec``/``doc`` are ``None`` only for
    spec-level consumers (the doc artifact in this checkout); ``manifest`` is
    the parsed ``set.json``.
    """

    set_id: str
    name: str
    spec: dict[str, Any] | None
    doc: str | None
    version: int
    directory: Path
    manifest: dict[str, Any] | None = None


@dataclass(frozen=True)
class ComponentSetSwap:
    """The fact a selection produced — the door appends it to the event log.

    The store reports what changed; it never writes log rows (D9). A session's
    ``set_swapped`` event carries ``version`` — the vocabulary version that
    actually became active — so replay can tell which set a turn was rendered
    with. The row, not this value object, is the durable fact.
    """

    previous_set_id: str
    set_id: str
    name: str
    version: int


class ComponentSetError(ValueError):
    """A component set that cannot be used; the message is the readable reason."""


@dataclass(frozen=True)
class _SetData:
    """One set directory read; the version is assigned by the store."""

    set_id: str
    name: str
    spec: dict[str, Any] | None
    doc: str | None
    directory: Path
    manifest: dict[str, Any] | None


@dataclass(frozen=True)
class _Resolved:
    """Where one set's artifacts live right now.

    ``manifest_path`` is ``None`` when the spec is externally overridden by
    ``FABULIST_DSL_SPEC``: that file is not what the directory's ``set.json``
    describes, so the manifest (its id, name and the components subset check) is
    ignored rather than wrongly refusing the set.
    """

    directory: Path
    spec_path: Path
    doc_path: Path
    manifest_path: Path | None


def _resolved_for(directory: Path) -> _Resolved:
    spec_path = resolve_spec_path(directory)
    own_spec = spec_path == directory / SPEC_FILENAME
    return _Resolved(
        directory=directory,
        spec_path=spec_path,
        doc_path=directory / DOC_FILENAME,
        manifest_path=directory / MANIFEST_FILENAME if own_spec else None,
    )


def _read_text(path: Path) -> tuple[bool, str | None]:
    """``(exists, text)``; ``text`` is ``None`` when the file cannot be read."""
    try:
        return True, path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False, None
    except OSError:
        return True, None


def _stamp(path: Path) -> tuple[int, int] | None:
    """``(mtime_ns, size)`` — the change signal; ``None`` when absent."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


def _stamps(resolved: _Resolved) -> tuple[Any, ...]:
    return (
        resolved.directory,
        resolved.spec_path,
        _stamp(resolved.spec_path),
        _stamp(resolved.doc_path),
        _stamp(resolved.manifest_path) if resolved.manifest_path is not None else None,
    )


def _read_set(resolved: _Resolved) -> tuple[_SetData | None, str | None]:
    """Read one set directory: ``(data, None)`` or ``(None, readable_reason)``.

    A missing doc artifact raises at the consumer (library_mini). A corrupt one
    is: the caller decides whether to keep its last good snapshot.
    """
    spec: dict[str, Any] | None = None
    doc: str | None = None
    manifest: dict[str, Any] | None = None

    exists, spec_text = _read_text(resolved.spec_path)
    if exists:
        if spec_text is None:
            return None, f"{resolved.spec_path}: unreadable"
        try:
            parsed = json.loads(spec_text)
        except ValueError as exc:
            return None, f"{resolved.spec_path}: invalid JSON ({exc})"
        if not isinstance(parsed, dict):
            return None, f"{resolved.spec_path}: must be a JSON object"
        spec = parsed

    exists, doc_text = _read_text(resolved.doc_path)
    if exists:
        if doc_text is None:
            return None, f"{resolved.doc_path}: unreadable"
        doc = doc_text if doc_text.strip() else None

    manifest_path = resolved.manifest_path
    if manifest_path is not None:
        exists, manifest_text = _read_text(manifest_path)
        if exists:
            if manifest_text is None:
                return None, f"{manifest_path}: unreadable"
            try:
                parsed_manifest = json.loads(manifest_text)
            except ValueError as exc:
                return None, f"{manifest_path}: invalid JSON ({exc})"
            if not isinstance(parsed_manifest, dict):
                return None, f"{manifest_path}: must be a JSON object"
            manifest = parsed_manifest

    set_id = resolved.directory.name
    name = set_id
    if manifest is not None:
        declared_id = manifest.get("id")
        if declared_id is not None:
            if not isinstance(declared_id, str) or not declared_id.strip():
                return None, f"{manifest_path}: 'id' must be a non-empty string"
            set_id = declared_id
        declared_name = manifest.get("name")
        if declared_name is not None:
            if not isinstance(declared_name, str) or not declared_name.strip():
                return None, f"{manifest_path}: 'name' must be a non-empty string"
            name = declared_name
        declared = manifest.get("components")
        if declared is not None:
            if not isinstance(declared, (list, tuple)) or not all(
                isinstance(entry, str) for entry in declared
            ):
                return None, f"{manifest_path}: 'components' must be a list of names"
            if spec is None:
                return None, (
                    f"{manifest_path}: declares components but "
                    f"{resolved.spec_path} is missing"
                )
            unknown = sorted(set(declared) - set(component_names_in(spec)))
            if unknown:
                return None, (
                    f"{manifest_path}: set {set_id!r} declares components the spec "
                    f"does not define: {', '.join(unknown)} — a set must be a subset of its "
                    "spec's components"
                )

    return (
        _SetData(
            set_id=set_id,
            name=name,
            spec=spec,
            doc=doc,
            directory=resolved.directory,
            manifest=manifest,
        ),
        None,
    )


@dataclass
class _SetState:
    """One cached set directory: its resolution, change signal and snapshot."""

    resolved: _Resolved
    stamp: tuple[Any, ...]
    snapshot: ComponentSetSnapshot


#: The set directory the CURRENT task is resolving (a per-turn/per-request focus).
#: ``session_snapshot`` sets it at ``build()`` start, so consumers on the same
#: call path (the taught doc, the gate, the strip list) read one set even when
#: no session contextvar is installed (e.g. a prompt preview); the per-session
#: binding below covers consumer calls that run in sibling tasks.
_FOCUS_DIR: ContextVar[Path | None] = ContextVar("fabulist_component_set_dir", default=None)


def _current_session_id() -> str:
    """The turn's session id (the engine's contextvar), or ``""`` off-turn.

    The engine sets ``CURRENT_SESSION`` per turn (asyncio tasks inherit it), and
    that is the ONLY session context a low-level reader can trust: a contextvar
    mutated inside a graph-node task does not survive back to its siblings. The
    store therefore keys per-session resolution on this id.
    """
    try:
        from fabulist.agents.debug import CURRENT_SESSION
    except Exception:  # pragma: no cover - import-cycle safety only
        return ""
    return CURRENT_SESSION.get()


class ComponentLibraryStore:
    """Thread-safe hot-reloadable view over component-set DIRECTORIES.

    Duck-typed after :class:`fabulist.connections.registry.RegistryStore`:
    every read returns the CURRENT snapshot, so a caller holding the store keeps
    working when the artifacts are regenerated underneath it.

    A SNAPSHOT IS CONTENT, NOT SELECTION (issues #16/#24): this store caches one
    snapshot per set directory and every session selecting that set shares it.
    WHICH set a session uses is a log fact, resolved by
    :func:`fabulist.dsl.sets.session_snapshot` and bound here per session id —
    one session's swap can never retarget another session's vocabulary.
    """

    def __init__(self, *, default_dir: str | os.PathLike[str]) -> None:
        self._default_dir = Path(default_dir)
        self._lock = threading.RLock()
        self._sets: dict[Path, _SetState] = {}
        self._session_dirs: dict[str, Path] = {}
        self._version = 0
        self._last_error: str | None = None

    @property
    def default_dir(self) -> Path:
        """The directory used when neither env nor a session selection applies."""
        return self._default_dir

    # -- reads ---------------------------------------------------------

    def snapshot(self) -> ComponentSetSnapshot:
        """The current set, reloaded first when an artifact changed under us.

        ``current`` = env override → this task's focused directory → the
        current session's bound directory → the default directory (see the
        module docstring).
        """
        return self.snapshot_for_dir(self._current_dir())

    def snapshot_for_dir(
        self, directory: str | os.PathLike[str]
    ) -> ComponentSetSnapshot:
        """One set directory's snapshot, reloaded when its artifacts changed.

        The cache is SHARED per directory: two sessions on the same set get the
        SAME ``ComponentSetSnapshot`` object and the same vocabulary version, so
        the version-keyed consumer caches (spec table, taught doc) rebuild once
        for both.
        """
        path = Path(directory)
        with self._lock:
            return self._snapshot_locked(path, strict=False)

    def reload_if_changed(self) -> ComponentSetSnapshot:
        """Compatibility alias: :meth:`snapshot` already stats before it serves."""
        return self.snapshot()

    def reload(self) -> ComponentSetSnapshot:
        """Drop EVERY cached set and re-read the current one.

        Explicit (tests, :func:`fabulist.dsl.spec.reset_spec_cache`, deployments
        repointing env) and therefore always re-resolves
        ``FABULIST_LIBRARY_ARTIFACT_DIR`` / ``FABULIST_DSL_SPEC`` rather than
        trusting a path cached at import.

        A present-but-corrupt artifact does NOT downgrade the running
        vocabulary: the previous snapshot for the SAME paths is kept and
        :meth:`last_error` explains.
        """
        with self._lock:
            # An explicit reload re-resolves from the environment, so a stale
            # task focus must not win (tests repoint the tree between cases).
            _FOCUS_DIR.set(None)
            # Keep the last good snapshot per directory ACROSS the clear: a
            # corrupt artifact encountered by this explicit re-read must still
            # keep the running vocabulary (the same discipline as a hot read).
            previous = dict(self._sets)
            self._sets.clear()
            self._last_error = None
            directory = self._current_dir()
            return self._load_locked(
                directory,
                resolved=_resolved_for(directory),
                previous=previous.get(directory),
                strict=False,
            )

    def current_set_id(self) -> str:
        """The id of the set in effect for the current context."""
        return self.snapshot().set_id

    def last_error(self) -> str | None:
        """Why the last read kept its last good snapshot (``None`` when clean)."""
        with self._lock:
            return self._last_error

    def require_snapshot(
        self, directory: str | os.PathLike[str]
    ) -> ComponentSetSnapshot:
        """Load one set strictly: unreadable artifacts raise, never fall back.

        The selection door validates with this BEFORE appending the swap fact, so
        a refused set never becomes a log row the next turn cannot render.
        """
        path = Path(directory)
        with self._lock:
            return self._snapshot_locked(path, strict=True)

    # -- per-session binding (the selection itself is a log fact) ------

    def bind_session_set(
        self, session_id: str, directory: str | os.PathLike[str] | None
    ) -> None:
        """Bind one session to a set directory (``None`` clears the binding).

        A process-side resolution of the session's log fact, not a second
        selection truth: :func:`fabulist.dsl.sets.session_snapshot` re-reads the
        log on every turn build and rebinds, so the log always wins.
        """
        with self._lock:
            if directory is None:
                self._session_dirs.pop(session_id, None)
            else:
                self._session_dirs[session_id] = Path(directory)

    def session_set_dir(self, session_id: str) -> Path | None:
        """The directory bound to one session, or ``None``."""
        with self._lock:
            return self._session_dirs.get(session_id)

    def focus_set(self, directory: str | os.PathLike[str] | None) -> None:
        """Point the CURRENT task at one set directory (``None`` clears it).

        Called by :func:`fabulist.dsl.sets.session_snapshot` at ``build()``
        start. It is context-local, not a selection: the next build overwrites
        it, and the durable selection remains the session's log row.
        """
        _FOCUS_DIR.set(Path(directory) if directory is not None else None)

    # -- internals -----------------------------------------------------

    def _current_dir(self) -> Path:
        override = os.environ.get(ARTIFACT_DIR_ENV_VAR)
        if override:
            return Path(override)
        focused = _FOCUS_DIR.get()
        if focused is not None:
            return focused
        session_id = _current_session_id()
        if session_id:
            bound = self.session_set_dir(session_id)
            if bound is not None:
                return bound
        return self._default_dir

    def _snapshot_locked(
        self, directory: Path, *, strict: bool
    ) -> ComponentSetSnapshot:
        resolved = _resolved_for(directory)
        state = self._sets.get(directory)
        if (
            state is not None
            and state.resolved == resolved
            and state.stamp == _stamps(resolved)
            and (not strict or state.snapshot.spec is not None)
        ):
            # A strict read (the selection door) never accepts a cached
            # FALLBACK snapshot: "no readable spec" must raise, not pass.
            return state.snapshot
        return self._load_locked(
            directory, resolved=resolved, previous=state, strict=strict
        )

    def _load_locked(
        self,
        directory: Path,
        *,
        resolved: _Resolved,
        previous: _SetState | None,
        strict: bool,
    ) -> ComponentSetSnapshot:
        data, error = _read_set(resolved)
        self._last_error = error
        keep = (
            previous.snapshot
            if previous is not None and previous.resolved == resolved
            else None
        )
        unusable = data is None or (strict and data.spec is None)
        if unusable and strict:
            raise ComponentSetError(
                error or f"{resolved.directory}: no readable {SPEC_FILENAME}"
            )
        if unusable and keep is not None:
            # A present-but-corrupt artifact never downgrades the running
            # vocabulary: keep the last good snapshot for the SAME paths.
            self._sets[directory] = _SetState(resolved, _stamps(resolved), keep)
            return keep
        if data is None:
            # First load of an unusable set: the documented fallback path
            # (flavors.json + the four infrastructure components).
            data = _SetData(
                set_id=resolved.directory.name,
                name=resolved.directory.name,
                spec=None,
                doc=None,
                directory=resolved.directory,
                manifest=None,
            )
        self._version += 1
        snapshot = ComponentSetSnapshot(
            set_id=data.set_id,
            name=data.name,
            spec=data.spec,
            doc=data.doc,
            version=self._version,
            directory=data.directory,
            manifest=data.manifest,
        )
        self._sets[directory] = _SetState(resolved, _stamps(resolved), snapshot)
        return snapshot


_STORE: ComponentLibraryStore | None = None
_STORE_LOCK = threading.Lock()


def library_store() -> ComponentLibraryStore:
    """The process-wide store over the repo's ``prompts/generated`` default.

    One instance so the per-set content cache and the per-session bindings are
    shared by every consumer (spec table, strip regex, taught doc) — a
    per-consumer store would let them read different bytes for the same set. The
    STORE holds no engine-global SELECTION: selection is a per-session log fact.
    """
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = ComponentLibraryStore(default_dir=_repo_root() / "prompts" / "generated")
        return _STORE
