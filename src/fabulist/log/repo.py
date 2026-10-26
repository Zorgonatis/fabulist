"""EventLog: the SQLite-backed append-only repository over the event log.

Fork mechanism (O(1) branch pointer, PLAN.md W2.2)
--------------------------------------------------
A fork copies no *history*. ``sessions`` rows carry a ``(base_session,
base_seq)`` pointer; a branch's event stream is the base session's rows up to
``base_seq`` (inclusive) followed by the branch's own rows. The branch's own
sequence numbers start at ``base_seq + 1`` so the combined stream stays
strictly ordered by ``seq`` and fully resolves revision references that point
across the fork boundary. Pruning an abandoned branch therefore never touches
the base session's rows.

Session-constant *config*, however, is a live setting rather than history: a
swipe must not silently drop your persona/world/assistant/memory just because
they were assigned **after** the turn being rerolled. So ``fork_session``
additionally carries the source's whitelisted config rows with ``seq >`` the
cut onto the branch (rows at/before the cut already arrive through the chain).
Narrative/user prose is still never copied (FORK-SEMANTICS F2).

Session *state* is carried as one persisted SNAPSHOT (issue #62, owner ruling:
the whole state is as important as the preceding history). ``fork_session``
writes ONE ``system``/``session_state_snapshot`` row into the new session whose
payload is the state of the session at the CUT, folded through the existing
projection reads (never a second fold, never a hand-rolled chain walk): the
projection's ``state``, the entity registry (:func:`fabulist.state.entities.registry`)
and the latest plan (:func:`fabulist.graph.plans.load_last_plan`), each read
through :class:`_CutView` so nothing above the cut can leak in. That snapshot is
a BASE, written once at fork time and never rewritten: every state read folds
the branch's own rows ABOVE it (its own ``state-delta`` rows, registry writes,
plans) to the tip, exactly as :meth:`get_projection` does -- the snapshot is
where the branch starts, not where it stops (issue #78). An older branch
without a snapshot row reads the same fold off the parent chain unchanged.

Projections are always rebuilt from raw events (the log is truth, D9/D11);
superseded content is resolved latest-wins and the originals stay queryable.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .events import EVENT_TYPES, SCHEMA_VERSION, Event
from .schema_gate import (
    COMPACTION_MARKER_EVENT,
    CONTEXT_LOW_EVENT,
    LogSchemaError,
    VALIDATION_NOTICE_EVENT,
    validate_event,
)
from .schema import DDL

# The legacy card-import seed marker predicate lives with the prefix. Importing
# the leaf module keeps this free of a cycle (``fabulist.cards.v2`` is stdlib
# only) and makes the projection the single derived view that refuses a seed as
# a player turn — the transcript, prompt history, variables and auto-assistant
# all read ``user_messages`` from here.
from fabulist.cards.v2 import is_import_seed_text

logger = logging.getLogger(__name__)

#: Boot log line for an event log that does not exist yet. A missing log file is
#: a FIRST RUN, not a broken install: the constructor creates an empty log so the
#: engine boots. A moved or lost log is NOT recovered automatically -- the new
#: file is empty, which is why this is a WARNING (the log owns everything,
#: ADR-001) and not a silent DEBUG like the derived stores below.
NEW_LOG_WARNING: str = (
    "event log not found - creating a NEW EMPTY log; a moved or lost log is NOT "
    "recovered automatically"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(value: Any) -> str:
    return json.dumps({} if value is None else value, ensure_ascii=False)


#: The ``system`` variant that carries a fork's whole-session state snapshot
#: (issue #62). Naming/verification helper: :func:`is_state_snapshot_event`.
STATE_SNAPSHOT_EVENT = "session_state_snapshot"

#: Actor recorded on the snapshot row a fork writes (D9: the dispatcher/proposer
#: writes; a fork is repo bookkeeping, not a turn step).
STATE_SNAPSHOT_ACTOR = "system"


class PruneError(RuntimeError):
    """Raised when a branch cannot be hard-deleted because others fork from it.

    Deleting a session that a live child's base pointer names would corrupt
    that child's projection, so ``prune_branch`` refuses instead.
    """


#: Session-constant config ``system`` payload kinds a fork must CARRY onto the
#: branch. These are live settings the user can change at any time -- a swipe
#: must not silently drop them just because they were assigned after the turn
#: being rerolled. History rows (narrative/user) are still never copied (F2).
#:
#: Deliberately excluded as content-adjacent, not live config:
#:   * ``greeting_selected`` -- selects which opening prose shows; the opening
#:     belongs to the inherited prefix, not to configuration.
#:   * ``session_module`` / ``session_module_deleted`` -- prompt text seeded at
#:     creation; content, whose tombstone fold is per ``module_id`` (a blanket
#:     carry would need item-level rules).
#:   * ``cast_snapshot`` -- derived content memoised at first consumption.
#:   * ``chat_agent_debug`` -- operational capture toggle, not narrative config;
#:     it can be flipped on the branch directly.
FORK_CONFIG_EVENTS = frozenset(
    {
        "chat_persona",
        # D53: a swipe/reroll must carry the chat's OWNER (the ``chat_actor``
        # record) onto the branch, exactly as chat_persona/world_ref are carried.
        "chat_actor",
        "chat_assistant",
        "world_ref",
        "chat_memory",
        "chat_memory_config",
        # W9.22: User-as-Actor is a live session setting (chat_config_api) — a
        # fork cut before the toggle was written must keep the owner's choice,
        # like every other session-constant config above.
        "chat_user_as_actor",
        # W9.24 §3.5: the NPC dispatch policy is the same kind of live session
        # setting (a branch must lane by the owner's chosen strategy/bound, not
        # silently revert to the defaults at a fork).
        "chat_npc_dispatch",
        # Issue #86: a chat's book attachment is a SELECTION over the catalogue,
        # the same class as ``world_ref`` -- not content. A book attached after
        # the swiped turn (regenerate is the common path) must survive onto the
        # branch, or the user's attach silently does nothing.
        "session_books",
        # Issue #86: the legacy session-scoped world-info import is the same
        # selection class (raw lorebook JSON attached to one chat).
        "world_info",
        # Issue #86: the component-set swap is a live session setting
        # (ADR-025: a selection over the generated catalogue, read back from the
        # latest ``set_swapped`` row in the chain next turn).
        "set_swapped",
    }
)


def is_state_snapshot_event(event: Any) -> bool:
    """Whether ``event`` is a fork's persisted state snapshot row (issue #62).

    The one predicate every reader/tests share, so "is this row the snapshot?"
    has a single definition instead of a payload string scattered around.
    Accepts an :class:`Event` or a projection dict alike.
    """
    if isinstance(event, dict):
        payload = event.get("payload")
        event_type = event.get("type")
        ephemeral = event.get("ephemeral", False)
    else:
        payload = getattr(event, "payload", None)
        event_type = getattr(event, "type", None)
        ephemeral = getattr(event, "ephemeral", False)
    if event_type != "system" or ephemeral:
        return False
    return isinstance(payload, dict) and payload.get("event") == STATE_SNAPSHOT_EVENT


class _CutView:
    """A session's fork-resolved reads, TRUNCATED at a seq (issue #62).

    The state a fork must carry is the state as of the CUT, which is not the
    source's current state: the cut can sit below the source's tip (delete-tail,
    a fork from an older message, a fork of a fork). This adapter exposes the
    two duck-typed log reads the existing folds consume
    (``get_chain_events``/``get_events``) over the inherited slice
    (``seq <= at_seq``) only, so
    :func:`fabulist.state.entities.registry` and
    :func:`fabulist.graph.plans.load_last_plan` fold the cut without a single
    new fold and without a hand-rolled chain walk.

    Read-only by construction: it never appends, never mints, and never touches
    the log's connection.
    """

    __slots__ = ("_log", "_session_id", "at_seq")

    def __init__(self, log: "EventLog", session_id: str, at_seq: int) -> None:
        self._log = log
        self._session_id = session_id
        self.at_seq = at_seq

    def _bound(self, session_id: str | None) -> str:
        return self._session_id if session_id is None else session_id

    def get_chain_events(
        self, session_id: str | None = None, **_kwargs: Any
    ) -> list[Event]:
        """The chain slice at or below the cut (fork-aware, revisions kept raw)."""
        return [
            event
            for event in self._log.get_chain_events(self._bound(session_id))
            if event.seq <= self.at_seq
        ]

    def get_events(
        self, session_id: str | None = None, **_kwargs: Any
    ) -> list[Event]:
        """The session-local slice at or below the cut (audit shape)."""
        return [
            event
            for event in self._log.get_events(self._bound(session_id))
            if event.seq <= self.at_seq
        ]


class EventLog:
    """Repository over one SQLite event-log file."""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        # A missing file is a first run: sqlite3.connect below creates it. Warn
        # ONCE per process -- the first construction creates the file, so the
        # later constructions in the same boot see it exist and stay silent.
        if self.path != ":memory:" and not Path(self.path).exists():
            logger.warning("%s: %s", self.path, NEW_LOG_WARNING)
        # isolation_level=None: transactions are explicit (BEGIN IMMEDIATE) so
        # read-then-write seq assignment is atomic across connections.
        self._conn = sqlite3.connect(self.path, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.executescript(DDL)

    # -- lifecycle ---------------------------------------------------------
    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "EventLog":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- writes ------------------------------------------------------------
    def append_event(
        self,
        session_id: str,
        type: str,
        actor: str,
        payload: dict | None = None,
        *,
        ts: str | None = None,
        ephemeral: bool = False,
        meta: dict | None = None,
        supersedes: int | None = None,
        schema_version: int = SCHEMA_VERSION,
    ) -> Event:
        """Append one event, assigning the next per-session ``seq`` atomically.

        The schema gate (PLAN §4: one validation chokepoint at the append
        door) runs here — malformed payloads raise ``LogSchemaError`` BEFORE
        anything touches the log. Extras pass (D4 forward-compat); ``ooc``
        and ``system`` stay permissive by design.
        """
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {type!r}")
        problems = validate_event(type, payload or {})
        if problems:
            raise LogSchemaError(
                f"schema gate rejected {type!r} event for {session_id!r}: "
                + "; ".join(problems)
            )
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            event = self._insert_event(
                session_id=session_id,
                type=type,
                actor=actor,
                payload=payload,
                ts=ts,
                ephemeral=ephemeral,
                meta=meta,
                supersedes=supersedes,
                schema_version=schema_version,
            )
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        return event

    def append_revision(
        self,
        session_id: str,
        supersedes_id: int,
        actor: str,
        payload: dict,
        *,
        ts: str | None = None,
        ephemeral: bool = False,
        meta: dict | None = None,
    ) -> Event:
        """Append a revision and point the target's ``superseded_by`` at it.

        ``payload`` follows the revision convention
        ``{"revised": <full replacement payload>, "reason": <str>}``. The insert
        and the ``superseded_by`` update happen in one transaction.
        """
        if "revised" not in payload:
            raise ValueError("revision payload must contain a 'revised' key")
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            target = self._conn.execute(
                "SELECT id FROM events WHERE id = ?", (supersedes_id,)
            ).fetchone()
            if target is None:
                raise ValueError(f"cannot revise unknown event id {supersedes_id}")
            event = self._insert_event(
                session_id=session_id,
                type="revision",
                actor=actor,
                payload=payload,
                ts=ts,
                ephemeral=ephemeral,
                meta=meta,
                supersedes=supersedes_id,
                schema_version=SCHEMA_VERSION,
            )
            self._conn.execute(
                "UPDATE events SET superseded_by = ? WHERE id = ?",
                (event.id, supersedes_id),
            )
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        return event

    def _insert_event(
        self,
        *,
        session_id: str,
        type: str,
        actor: str,
        payload: dict | None,
        ts: str | None,
        ephemeral: bool,
        meta: dict | None,
        supersedes: int | None,
        schema_version: int,
    ) -> Event:
        """Insert one row inside an already-open transaction."""
        payload = payload if payload is not None else {}
        meta = meta if meta is not None else {}
        ts = ts or _now()

        self._ensure_session(session_id)
        base_row = self._conn.execute(
            "SELECT base_seq FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        base_seq = (base_row["base_seq"] or 0) if base_row is not None else 0
        max_row = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS m FROM events WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        seq = max(max_row["m"], base_seq) + 1

        cursor = self._conn.execute(
            "INSERT INTO events "
            "(ts, schema_version, session_id, seq, type, actor, payload, "
            " supersedes, ephemeral, meta) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ts,
                schema_version,
                session_id,
                seq,
                type,
                actor,
                _dump(payload),
                supersedes,
                int(ephemeral),
                _dump(meta),
            ),
        )
        return Event(
            id=cursor.lastrowid,
            ts=ts,
            schema_version=schema_version,
            session_id=session_id,
            seq=seq,
            type=type,
            actor=actor,
            payload=payload,
            supersedes=supersedes,
            superseded_by=None,
            ephemeral=ephemeral,
            meta=meta,
        )

    def _ensure_session(self, session_id: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO sessions (session_id, base_session, base_seq, created_ts) "
            "VALUES (?, NULL, NULL, ?)",
            (session_id, _now()),
        )

    # -- reads -------------------------------------------------------------
    def get_event(self, event_id: int) -> Event | None:
        row = self._conn.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        return None if row is None else self._row_to_event(row)

    def get_events(
        self, session_id: str, *, max_schema_version: int | None = None
    ) -> list[Event]:
        """Raw rows owned by one session, ordered by seq.

        Unlike :meth:`get_projection` this does not walk forks, does not apply
        revisions, and does not hide ephemeral rows — it is the audit view.
        Fork-aware readers use :meth:`get_chain_events` instead.
        """
        rows = self._conn.execute(
            "SELECT * FROM events WHERE session_id = ? ORDER BY seq", (session_id,)
        ).fetchall()
        events = [self._row_to_event(row) for row in rows]
        if max_schema_version is not None:
            events = [e for e in events if e.schema_version <= max_schema_version]
        return events

    def get_chain_events(
        self, session_id: str, *, max_schema_version: int | None = None
    ) -> list[Event]:
        """Fork-aware sibling of :meth:`get_events`: the inherited chain.

        Walks the base-session chain (:meth:`_stream_events`) so a branch reads
        its ancestors' rows up to the fork point — plus revisions carried across
        the boundary (F7) — exactly as :meth:`get_projection` does. Raw like
        ``get_events``: revisions are not applied and ephemeral rows are not
        hidden; ``max_schema_version`` mirrors ``get_events``. Ordered by seq.

        A root session with no forks returns the same rows as ``get_events``.
        Session-local reads that must NOT walk forks (audit views, replay cut
        points, anchor catalogs) keep using :meth:`get_events`.
        """
        events = sorted(self._stream_events(session_id), key=lambda e: e.seq)
        if max_schema_version is not None:
            events = [e for e in events if e.schema_version <= max_schema_version]
        return events

    def get_projection(
        self,
        session_id: str,
        *,
        max_schema_version: int = SCHEMA_VERSION,
        at_seq: int | None = None,
    ) -> dict:
        """Rebuild the derived view of a session (fork-aware).

        Returns ``narratives`` (seq order, revisions applied), ``state`` (shallow
        fold of state-delta payloads, last write wins), ``user_messages``,
        ``rolls``, ``assets``, ``notices``, ``context_lows`` and ``compactions``.
        Ephemeral events and events whose ``schema_version`` exceeds
        ``max_schema_version`` are skipped.

        ``user_messages`` rows carry BOTH ``payload["text"]`` (the RAW stored
        text — what the player typed and what a revision re-opens) and
        ``payload["text_rendered"]`` (the same text with the known macros
        ``{{user}}`` / ``{{char}}`` / ``{{world}}`` resolved for display, issue
        #63). The event itself is never rewritten (ADR-001): the rendered string
        is computed on this read and exists only in the returned view.

        ``notices`` (D-F) are the validate stage's teaching notices, each
        ``{seq, flags, score?}`` where ``seq`` is the FLAGGED NARRATIVE's seq —
        the anchor a transcript needs to render the notice directly after the
        turn it refers to. They are collected from the same fork-resolved stream
        this projection already walks (the read
        :func:`fabulist.context.facts.validation_notices` renders for the prompt),
        so a branch inherits its ancestors' notices exactly as it inherits the
        turns they describe. Latest wins per anchor, ordered by ``seq``.

        ``context_lows`` and ``compactions`` are the W9.26 transport keys: both
        are CHAIN-WALKED (a branch inherits its ancestors' rows, with the
        ancestors' own seqs) and seq-ordered, each item ``{seq, session_id,
        payload}``. ``context_lows`` is every gauge reading the chain ever filed
        (the client renders an inherited one above the break, settled);
        ``compactions`` is the era MARKER list — one break per item, and the
        LAST item is the current era's. Both are additive keys: no existing key
        changes shape for them.

        ``at_seq`` truncates the view at a seq: the projection a fork cut there
        inherits (no row above the cut is folded). It is what
        :meth:`build_state_snapshot` reads, and the default ``None`` keeps every
        existing caller on the whole stream.

        Issue #62 (owner ruling: a fork persists the WHOLE session state): a
        session's own ``session_state_snapshot`` row is folded into ``state`` in
        seq order, i.e. AFTER the inherited ``state-delta`` rows it was built
        from and BEFORE anything the branch writes itself. That is the read
        precedence — the branch's persisted snapshot first, the parent chain as
        the fallback for older branches (which carry no snapshot row) — and it
        is why a late revision of an inherited state row can no longer move a
        branch's committed state.
        """
        active = [
            e
            for e in self._stream_events(session_id)
            if not e.ephemeral and e.schema_version <= max_schema_version
        ]
        if at_seq is not None:
            # Fork cut (issue #62): the inherited slice only. Seq is monotonic
            # along a chain (a branch's own seqs start at ``base_seq + 1``), so
            # one seq filter truncates the ancestor prefix, the branch's own
            # rows, and the ancestor revisions carried across the boundary (F7)
            # in one move.
            active = [e for e in active if e.seq <= at_seq]
        resolved = self._resolve_payloads(active)

        narratives: list[dict] = []
        user_messages: list[dict] = []
        rolls: list[dict] = []
        assets: list[dict] = []
        state: dict = {}
        # W3B/D42: one persisted per-turn step trace (``turn_stages`` system
        # event) per narrative seq, latest-wins. Folded here and re-attached to
        # the narrative item it describes (below) so the client rehydrates the
        # header on reload and on a swipe/regenerate branch switch — the trace
        # otherwise lives only in the client store and is wiped on the switch.
        stages_by_seq: dict[int, list] = {}
        # D-F: one teaching notice per flagged narrative seq, latest wins — the
        # same fold shape ``turn_stages`` uses for the trace it attaches.
        notices_by_seq: dict[int, dict] = {}
        # W9.26 §2.2: chain-walked transport rows. Both are collected from the
        # fork-resolved stream (``active``), so an inherited ancestor row rides
        # along with its OWN seq — the transcript draws the break where the
        # ancestor's history actually sits.
        context_lows: list[dict] = []
        compactions: list[dict] = []

        for event in active:
            payload = resolved[event.id]
            if event.type == "narrative":
                narratives.append(self._item(event, payload))
            elif event.type == "user":
                # Legacy card-import seeds fabricated a ``user`` row
                # ("[Imported card: ...]"). History is append-only so the row
                # stays in the log, but it was never the player speaking: it
                # must not surface as a transcript bubble, a history turn, a
                # ``{{last_user}}`` or a ``history_count`` slot. New imports
                # write no such row (``cards.to_session_seed``).
                text = payload.get("text") if isinstance(payload, dict) else ""
                if is_import_seed_text(text):
                    continue
                user_messages.append(self._item(event, payload))
            elif event.type == "roll":
                rolls.append(self._item(event, payload))
            elif event.type == "asset":
                assets.append(self._item(event, payload))
            elif event.type == "state-delta":
                state.update(payload)
            elif (
                event.type == "system"
                and isinstance(payload, dict)
                and payload.get("event") == "turn_stages"
            ):
                seq = payload.get("seq")
                turn_steps = payload.get("steps")
                if (
                    isinstance(seq, int)
                    and not isinstance(seq, bool)
                    and isinstance(turn_steps, list)
                ):
                    # ``active`` is seq-ordered, so a later row supersedes.
                    stages_by_seq[seq] = turn_steps
            elif (
                event.type == "system"
                and isinstance(payload, dict)
                and payload.get("event") == VALIDATION_NOTICE_EVENT
            ):
                notice = self._validation_notice(payload)
                if notice is not None:
                    notices_by_seq[notice["seq"]] = notice
            elif event.type == "system" and isinstance(payload, dict):
                variant = payload.get("event")
                if variant == CONTEXT_LOW_EVENT:
                    context_lows.append(
                        {
                            "seq": event.seq,
                            "session_id": event.session_id,
                            "payload": payload,
                        }
                    )
                elif variant == COMPACTION_MARKER_EVENT:
                    compactions.append(
                        {
                            "seq": event.seq,
                            "session_id": event.session_id,
                            "payload": payload,
                        }
                    )
                elif variant == STATE_SNAPSHOT_EVENT:
                    # Issue #62: the state a fork persisted at its cut. Folded
                    # here (not seeded up front) so ``active``'s seq order gives
                    # the precedence for free: the snapshot lands above the
                    # inherited deltas it was built from and below anything the
                    # branch writes after the fork.
                    snapshot_state = payload.get("state")
                    if isinstance(snapshot_state, dict):
                        state.update(snapshot_state)

        # Additive: a turn with a persisted trace carries it on its payload as
        # ``steps`` (the same ordered record the live ``stage`` frames fold to).
        for narrative in narratives:
            turn_steps = stages_by_seq.get(narrative.get("seq"))
            if turn_steps is not None:
                narrative["payload"] = {
                    **(narrative.get("payload") or {}),
                    "steps": turn_steps,
                }

        context_lows.sort(key=lambda item: item["seq"])
        compactions.sort(key=lambda item: item["seq"])

        # Issue #63: user rows carry the DISPLAY text (known macros resolved)
        # beside the raw one; the stored event is untouched (ADR-001).
        self._render_user_texts(session_id, user_messages)

        return {
            "narratives": narratives,
            "state": state,
            "user_messages": user_messages,
            "rolls": rolls,
            "assets": assets,
            "notices": [notices_by_seq[seq] for seq in sorted(notices_by_seq)],
            "context_lows": context_lows,
            "compactions": compactions,
        }

    # -- forks -------------------------------------------------------------
    def fork_session(
        self, source_session: str, at_seq: int, new_session_id: str
    ) -> str:
        """Create a branch whose projection is ``source`` up to ``at_seq``.

        History is not copied -- only a session row pointing back at the source
        is written (F2). Two things ARE written onto the branch:

        * the source's session-constant config rows *after* the cut
          (:meth:`_carry_config`), so a swipe keeps a persona/world/assistant/
          memory assigned after the swiped turn;
        * ONE complete state snapshot of the session at the cut
          (:meth:`_carry_state`, issue #62), so the new chat holds the whole
          state -- entity registry, plan, tracks, every object -- beside the
          inherited history.

        Building the snapshot is the only work done before the transaction: it
        is a pure read, and doing it first means a fork that cannot assemble its
        state writes nothing at all.
        Returns ``new_session_id``.
        """
        if at_seq < 0:
            raise ValueError("at_seq must be >= 0")
        if new_session_id == source_session:
            raise ValueError("fork target must differ from source session")
        exists = self._conn.execute(
            "SELECT 1 FROM sessions WHERE session_id = ?", (new_session_id,)
        ).fetchone()
        if exists is not None:
            raise ValueError(f"session already exists: {new_session_id!r}")

        # Read-only, outside the write lock: the state this fork is cut from.
        snapshot = self.build_state_snapshot(source_session, at_seq)

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._ensure_session(source_session)
            self._conn.execute(
                "INSERT INTO sessions (session_id, base_session, base_seq, created_ts) "
                "VALUES (?, ?, ?, ?)",
                (new_session_id, source_session, at_seq, _now()),
            )
            self._carry_config(source_session, new_session_id, at_seq)
            self._carry_state(new_session_id, snapshot)
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        return new_session_id

    def _carry_state(self, new_session_id: str, snapshot: dict) -> None:
        """Re-append the cut's state snapshot onto the branch (issue #62).

        Called inside :meth:`fork_session`'s transaction, after
        :meth:`_carry_config` (so the branch's first own row stays the carried
        config, which the era-marker/branch-kind readers depend on). ONE
        ``system`` row: append-only like everything else (ADR-001), revisable
        through the ordinary revision door (ADR-002, the superseding payload
        wins latest-wins), and read back by :meth:`get_state_snapshot`.
        """
        self._insert_event(
            session_id=new_session_id,
            type="system",
            actor=STATE_SNAPSHOT_ACTOR,
            payload=snapshot,
            ts=None,
            ephemeral=False,
            meta={},
            supersedes=None,
            schema_version=SCHEMA_VERSION,
        )

    def build_state_snapshot(self, session_id: str, at_seq: int) -> dict:
        """The complete state of ``session_id`` AT ``at_seq`` (issue #62).

        Assembled from the existing projection reads only -- there is no second
        fold here and no chain walk of its own:

        * ``state`` -- :meth:`get_projection` truncated at the cut (the
          fork- and revision-aware fold of the ``state-delta`` rows);
        * ``registry`` -- :func:`fabulist.state.entities.registry` over a
          :class:`_CutView`, i.e. every established object: locations, threads
          (the plot tracks), relationships, meters, items, conditions, bodies,
          the economy purse, being→location assignments and the legacy shim;
        * ``plan`` -- :func:`fabulist.graph.plans.load_last_plan` over the same
          cut view, so a fork inherits the plan only when it sits AT or BELOW
          the cut (a plan written above the cut is not the state the fork was
          cut from) and therefore does not re-plan inside its inherited cadence
          window.

        ``None`` values are normalized to empty shapes (``state``/``registry``
        dicts, ``plan`` null) so a reader never has to guess. Nothing above the
        cut is read: every fold above runs over ``seq <= at_seq``.
        """
        if at_seq < 0:
            raise ValueError("at_seq must be >= 0")
        # Lazy imports: ``fabulist.state.entities`` pulls the state layer and
        # ``fabulist.graph.plans`` the graph layer, both of which import THIS
        # package (the ``_render_user_texts`` pattern for a cycle-free leaf).
        from fabulist.graph.plans import load_last_plan
        from fabulist.state.entities import registry

        view = _CutView(self, session_id, at_seq)
        projection = self.get_projection(session_id, at_seq=at_seq)
        plan = load_last_plan(view, session_id)
        return {
            "event": STATE_SNAPSHOT_EVENT,
            "at_seq": at_seq,
            "state": dict(projection.get("state") or {}),
            "registry": asdict(registry(view, session_id)),
            "plan": plan.to_dict() if plan is not None else None,
        }

    def get_state_snapshot(self, session_id: str) -> dict | None:
        """The session's OWN persisted state snapshot as the fork's BASE, or ``None``.

        The snapshot is written ONCE at fork time (issue #62) and is the state
        the branch was cut from -- a base, not a live view: it is frozen at its
        ``at_seq`` by construction. Every read folds the branch's own rows above
        it to the tip, so this payload is deliberately the state *at the cut*.

        Session-local by construction (``get_events``): this read never touches
        the parent chain, so a fork's base survives its parent's later writes
        and costs no ancestor walk. Revisions are applied latest-wins (ADR-002),
        so a corrected snapshot reads as corrected. A pre-#62 branch owns no
        snapshot row and gets ``None`` -- :meth:`get_session_state` then folds
        the chain alone, which is the same answer it always gave.
        """
        own_rows = self.get_events(session_id)
        own = [event for event in own_rows if is_state_snapshot_event(event)]
        if not own:
            return None
        # Resolve over the WHOLE own-row set, not just the snapshot rows: a
        # revision that corrects a snapshot is its own row of type
        # ``revision`` (ADR-002), so dropping it would read the stale payload.
        resolved = self._resolve_payloads(own_rows)
        latest = own[-1]
        payload = resolved.get(latest.id) or {}
        snapshot = dict(payload)
        snapshot.setdefault("event", STATE_SNAPSHOT_EVENT)
        snapshot["seq"] = latest.seq
        return snapshot

    def get_session_state(self, session_id: str) -> dict:
        """A session's state at its TIP, with the snapshot as the BASE (issue #78).

        Lifecycle, precisely: the snapshot is written ONCE at fork time and is a
        base; every read folds the branch's own rows ABOVE it to the tip -- the
        same fold :meth:`get_projection` performs, where the snapshot lands at
        its own ``seq`` (fork cut + 1), above the inherited rows it was built
        from and below anything the branch wrote after the fork. So the branch's
        own ``state-delta`` rows win over the cut's, and ``at_seq`` reports the
        resolved position (the tip), not the cut the snapshot froze.

        This is a thin read over the existing machinery, not a second fold:
        :meth:`build_state_snapshot` at the tip already assembles ``state``
        (the projection fold, snapshot included), ``registry`` and ``plan`` over
        one cut view. A legacy (pre-#62) branch owns no snapshot row and gets
        the identical fold off the parent chain -- ``persisted`` names which of
        the two row sets answered, so a caller (or a test) can tell them apart
        without guessing.
        """
        persisted = self.get_state_snapshot(session_id) is not None
        chain = self.get_chain_events(session_id)
        tip = max((event.seq for event in chain), default=0)
        return {**self.build_state_snapshot(session_id, tip), "persisted": persisted}

    def _carry_config(
        self, source_session: str, new_session_id: str, at_seq: int
    ) -> None:
        """Re-append the source's post-cut session-constant config to a branch.

        Called inside :meth:`fork_session`'s transaction. A fork cuts *history*
        at ``at_seq`` (F2), but session-constant config is a live setting: a
        persona/world/assistant/memory choice assigned to the source after the
        swiped turn would otherwise fall outside the inherited chain window
        (``seq <= at_seq``) and vanish from the branch.

        Only the source's own ``system`` rows of a whitelisted kind
        (:data:`FORK_CONFIG_EVENTS`) with ``seq > at_seq`` are re-appended --
        rows at or before the cut already arrive through the chain, so copying
        them would duplicate them. The readers' latest-wins folds make the
        newest value per key win, whichever session owns it. The new rows get
        the branch's own ``seq`` space (continuing from ``at_seq + 1``) via
        :meth:`_insert_event`, so ordering and pruning stay correct.
        """
        carried: list[Event] = []
        for event in self.get_events(source_session):
            if event.seq <= at_seq or event.ephemeral or event.type != "system":
                continue
            payload = event.payload
            if not isinstance(payload, dict):
                continue
            if payload.get("event") in FORK_CONFIG_EVENTS:
                carried.append(event)
        for source in carried:
            self._insert_event(
                session_id=new_session_id,
                type=source.type,
                actor=source.actor,
                payload=dict(source.payload),
                ts=None,
                ephemeral=False,
                meta={},
                supersedes=None,
                schema_version=source.schema_version,
            )

    def list_branches(self, base_session: str) -> list[str]:
        """Session ids whose base pointer is ``base_session``, sorted."""
        rows = self._conn.execute(
            "SELECT session_id FROM sessions WHERE base_session = ? ORDER BY session_id",
            (base_session,),
        ).fetchall()
        return [row["session_id"] for row in rows]

    def get_session(self, session_id: str) -> dict | None:
        """Fork bookkeeping for one session, or ``None`` when unknown.

        Returns ``{session_id, base_session, base_seq, created_ts}``. A root
        session has ``base_session``/``base_seq`` of ``None``.
        """
        row = self._conn.execute(
            "SELECT session_id, base_session, base_seq, created_ts "
            "FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "session_id": row["session_id"],
            "base_session": row["base_session"],
            "base_seq": row["base_seq"],
            "created_ts": row["created_ts"],
        }

    def last_swipe_target(self, session_id: str) -> list[str]:
        """Ordered sibling sessions for the swipe UI, pivot-first (issue #59).

        The group is every reply AT THE SAME POSITION: one pivot ``(anchor,
        cut)`` in the shared chain, listed as the original ``anchor`` line
        (position 0 -- it owns the prefix, so it comes first) plus every session
        forked there, whichever chain it forked from (FORK-SEMANTICS §3). The
        forks follow ordered by creation (``created_ts``, id tiebreak).

        Symmetric by construction: the group a session reads is a function of
        its pivot, so a fork always lists the line it forked from and that line
        always lists the fork back. That is what used to be broken -- the group
        was read from the asking session's own pointer only, so the ORIGINAL
        line listed nothing after a regenerate (the reported bug: swiping back
        to the original left no arrow to the freshly regenerated reply).

        A session with no pivot at all (no fork of its own and no fork inside
        its segment) is its own one-line group; an unknown session yields
        ``[]``.
        """
        if self.get_session(session_id) is None:
            return []
        sessions = self._session_map()
        pivot = self._swipe_pivot(sessions, session_id)
        if pivot is None:
            return [session_id]
        anchor = pivot[0]
        ordered = [
            sid
            for sid in sorted(
                sessions,
                key=lambda s: (sessions[s]["created_ts"], s),
            )
            if sid != anchor and self._fork_pivot(sessions, sid) == pivot
        ]
        return [anchor, *ordered]

    def _session_map(self) -> dict[str, dict]:
        """Every ``sessions`` row keyed by id (one read for a group query)."""
        rows = self._conn.execute(
            "SELECT session_id, base_session, base_seq, created_ts FROM sessions"
        ).fetchall()
        return {
            row["session_id"]: {
                "base_session": row["base_session"],
                "base_seq": row["base_seq"],
                "created_ts": row["created_ts"],
            }
            for row in rows
        }

    @staticmethod
    def _fork_pivot(
        sessions: dict[str, dict], session_id: str
    ) -> tuple[str, int] | None:
        """Canonical ``(anchor, cut)`` position the session was forked at.

        ``fork_session`` records ``(base_session, base_seq)``, but that pointer
        can name a parent that is ITSELF cut at the same seq or below: a fork of
        a fork at one cut is a fork of one POSITION (regenerate twice -- ``B``
        forked from ``A`` at ``K`` while ``A`` was forked from ``R`` at ``K``).
        Climbing while the cut is at or below the parent's own cut canonicalises
        it onto the line that owns the prefix, so sessions forked at one
        position compare equal whichever chain each came from.

        Returns ``None`` for a root session (it has no fork pivot).
        """
        info = sessions.get(session_id)
        if info is None or info["base_session"] is None:
            return None
        anchor = info["base_session"]
        cut = info["base_seq"]
        seen = {session_id}
        while anchor not in seen:
            seen.add(anchor)
            row = sessions.get(anchor)
            if row is None or row["base_session"] is None:
                break
            own = row["base_seq"]
            if own is None or cut > own:
                break
            anchor = row["base_session"]
        return (anchor, cut)

    @classmethod
    def _swipe_pivot(
        cls, sessions: dict[str, dict], session_id: str
    ) -> tuple[str, int] | None:
        """The pivot the session's swipe group is read at, or ``None``.

        The reader stands at the END of the session's chain, so the pivot it
        offers is the LATEST one on its own line: the deepest cut among its
        direct children that forks INSIDE this session's own segment (an earlier
        pivot is a position further up the transcript, not the reply on screen).
        A session with no such child -- a root with none, or one whose children
        all fork at or below its own cut (the chained-regenerate case, where a
        child canonicalises onto the same pivot) -- reads at the pivot it was
        itself forked at.
        """
        if session_id not in sessions:
            return None
        cuts = []
        for sid, info in sessions.items():
            if info["base_session"] != session_id:
                continue
            pivot = cls._fork_pivot(sessions, sid)
            if pivot is not None and pivot[0] == session_id:
                cuts.append(pivot[1])
        if cuts:
            return (session_id, max(cuts))
        return cls._fork_pivot(sessions, session_id)

    # -- kept turns --------------------------------------------------------
    def keep_turn(self, session_id: str, at_seq: int) -> Event:
        """Append an explicit ``turn_kept`` marker for the turn at ``at_seq``.

        Kept-ness is an append-only fact (D10 / FORK-SEMANTICS F5): post-turn
        agents may gate on this ``system`` event instead of inferring kept from
        "a later event exists in the branch".
        """
        if at_seq < 0:
            raise ValueError("at_seq must be >= 0")
        return self.append_event(
            session_id, "system", "user", {"event": "turn_kept", "seq": at_seq}
        )

    # -- ephemeral branches ------------------------------------------------
    def mark_ephemeral(self, session_id: str) -> int:
        """Flag every event in a session ephemeral; returns rows flagged."""
        cursor = self._conn.execute(
            "UPDATE events SET ephemeral = 1 WHERE session_id = ? AND ephemeral = 0",
            (session_id,),
        )
        return cursor.rowcount

    def prune_branch(self, session_id: str) -> int:
        """Hard-delete a branch's *own* events and its ``sessions`` row.

        Fixes the ghost-row defect flagged in FORK-SEMANTICS §4/§10: pruning a
        branch must also drop the bookkeeping row so ``list_branches`` stops
        advertising it. The base session's inherited rows are never touched.

        Refuses (``PruneError``) when other sessions fork *from* this one: a
        live child's base pointer would become dangling. Returns the number of
        events removed.
        """
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM sessions WHERE base_session = ?",
            (session_id,),
        ).fetchone()
        if row is not None and row["n"] > 0:
            raise PruneError(
                f"session {session_id!r} has children; prune them first"
            )

        self._conn.execute("BEGIN IMMEDIATE")
        try:
            ids = [
                row["id"]
                for row in self._conn.execute(
                    "SELECT id FROM events WHERE session_id = ?", (session_id,)
                ).fetchall()
            ]
            if ids:
                placeholders = ", ".join("?" for _ in ids)
                self._conn.execute(
                    f"UPDATE events SET superseded_by = NULL "
                    f"WHERE superseded_by IN ({placeholders})",
                    ids,
                )
                self._conn.execute(
                    f"UPDATE events SET supersedes = NULL "
                    f"WHERE supersedes IN ({placeholders})",
                    ids,
                )
                self._conn.execute(
                    "DELETE FROM events WHERE session_id = ?", (session_id,)
                )
            self._conn.execute(
                "DELETE FROM sessions WHERE session_id = ?", (session_id,)
            )
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        return len(ids)

    def prune_ephemeral(self, session_id: str) -> int:
        """Hard-delete the session's ephemeral events; returns rows removed.

        Dangling ``superseded_by`` / ``supersedes`` references to removed rows
        are reset so the remaining log stays consistent.
        """
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            ids = [
                row["id"]
                for row in self._conn.execute(
                    "SELECT id FROM events WHERE session_id = ? AND ephemeral = 1",
                    (session_id,),
                ).fetchall()
            ]
            if ids:
                placeholders = ", ".join("?" for _ in ids)
                self._conn.execute(
                    f"UPDATE events SET superseded_by = NULL "
                    f"WHERE superseded_by IN ({placeholders})",
                    ids,
                )
                self._conn.execute(
                    f"UPDATE events SET supersedes = NULL "
                    f"WHERE supersedes IN ({placeholders})",
                    ids,
                )
                self._conn.execute(
                    f"DELETE FROM events WHERE id IN ({placeholders})", ids
                )
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        self._conn.execute("COMMIT")
        return len(ids)

    # -- assets ------------------------------------------------------------
    def add_asset(
        self,
        asset_id: str,
        kind: str,
        path: str | Path,
        meta: dict | None = None,
        *,
        created_ts: str | None = None,
    ) -> dict:
        """Insert or update asset metadata (file bytes live outside the log)."""
        meta = meta if meta is not None else {}
        created_ts = created_ts or _now()
        self._conn.execute(
            "INSERT INTO assets (id, kind, path, meta, created_ts) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET "
            "kind = excluded.kind, path = excluded.path, meta = excluded.meta",
            (asset_id, kind, str(path), _dump(meta), created_ts),
        )
        return {
            "id": asset_id,
            "kind": kind,
            "path": str(path),
            "meta": meta,
            "created_ts": created_ts,
        }

    def get_asset(self, asset_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM assets WHERE id = ?", (asset_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "kind": row["kind"],
            "path": row["path"],
            "meta": json.loads(row["meta"]),
            "created_ts": row["created_ts"],
        }

    # -- internals ---------------------------------------------------------
    def _stream_events(self, session_id: str) -> list[Event]:
        """Fork-resolved stream for a session, ordered by seq.

        Revisions are carried across the fork boundary (F7): a revision appended
        to an *ancestor* after the branch was cut is still included when its
        target event lies inside the inherited slice. Without this, "edit an
        early turn, then swipe a later one" would silently lose the edit.
        """
        events: list[Event] = []
        inherited_ids: set[int] = set()
        carried_revisions: list[Event] = []
        for sid, bound in self._session_chain(session_id):
            if bound is None:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE session_id = ? ORDER BY seq", (sid,)
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM events WHERE session_id = ? AND seq <= ? "
                    "ORDER BY seq",
                    (sid, bound),
                ).fetchall()
            for row in rows:
                event = self._row_to_event(row)
                events.append(event)
                inherited_ids.add(event.id)
            if bound is not None:
                # Ancestor prefix: revisions appended after the cut (seq > bound)
                # whose target is inherited must still apply (F7).
                late = self._conn.execute(
                    "SELECT * FROM events WHERE session_id = ? AND seq > ? "
                    "AND type = 'revision' ORDER BY seq",
                    (sid, bound),
                ).fetchall()
                for row in late:
                    event = self._row_to_event(row)
                    if event.supersedes in inherited_ids:
                        carried_revisions.append(event)
        events.extend(carried_revisions)
        return events

    def _session_chain(self, session_id: str) -> list[tuple[str, int | None]]:
        """Root-first ``[(session_id, seq_upper_bound_or_None), ...]`` chain.

        A non-tip node's upper bound is the tightest (minimum) of the
        ``base_seq`` recorded by its child and every later fork's effective
        bound. The child's ``base_seq`` alone is not enough: a fork cut BELOW
        the child's own ``base_seq`` (delete-tail below a branch's pivot) makes
        the child's slice tighter than the cut it recorded, and the ancestor
        must not leak the rows above it. ``min`` is the right merge because an
        ancestor's visible prefix is the tightest cut any descendant makes.
        """
        nodes: list[tuple[str, str | None, int | None]] = []
        seen: set[str] = set()
        current: str | None = session_id
        while current is not None and current not in seen:
            seen.add(current)
            row = self._conn.execute(
                "SELECT base_session, base_seq FROM sessions WHERE session_id = ?",
                (current,),
            ).fetchone()
            if row is None:
                nodes.append((current, None, None))
                break
            nodes.append((current, row["base_session"], row["base_seq"]))
            current = row["base_session"]
        nodes.reverse()

        # Walk tip -> root so each ancestor sees the effective bound of every
        # later fork: ``own`` is the cut this node's child recorded, and
        # ``effective`` is the tightest cut below that child.
        chain: list[tuple[str, int | None]] = []
        effective: int | None = None
        for index in range(len(nodes) - 1, -1, -1):
            sid = nodes[index][0]
            own = nodes[index + 1][2] if index + 1 < len(nodes) else None
            if own is None:
                bound = effective
            elif effective is None:
                bound = own
            else:
                bound = min(own, effective)
            chain.append((sid, bound))
            effective = bound
        chain.reverse()
        return chain

    @staticmethod
    def _resolve_payloads(events: list[Event]) -> dict[int, dict]:
        """Map each event id to its latest superseding payload (latest wins)."""
        by_id = {event.id: event for event in events}
        resolved: dict[int, dict] = {}
        for event in events:
            payload = event.payload
            seen: set[int] = set()
            current = event
            while current.superseded_by is not None and current.id not in seen:
                seen.add(current.id)
                revision = by_id.get(current.superseded_by)
                if revision is None or revision.type != "revision":
                    break
                payload = revision.payload.get("revised", payload)
                current = revision
            resolved[event.id] = payload
        return resolved

    @staticmethod
    def _item(event: Event, payload: dict) -> dict:
        item = event.to_dict()
        item["payload"] = payload
        return item

    def _render_user_texts(
        self, session_id: str, user_messages: list[dict]
    ) -> None:
        """Attach ``payload["text_rendered"]`` to every user row (issue #63).

        ADR-001: the log keeps the RAW text — ``text`` is what the player typed
        and what a revision edit re-opens; ``text_rendered`` is the DISPLAY
        string with the known macro vocabulary resolved
        (:func:`fabulist.prompts.variables.render_display_text`, the one display
        step). Keeping both means the transcript never shows ``{{user}}`` while
        every text-matching reader (``{{last_user_turn}}``, the regenerate/
        fork content, revision edits) still compares the bytes that were stored.

        A message carrying no macro is copied verbatim, so the field is always
        present on a user row and a client may read it unconditionally.
        In-memory only: nothing is written back to the event.
        """
        if not user_messages:
            return
        # Lazy import: fabulist.prompts.variables imports fabulist.context.facts,
        # which imports THIS module — a top-level import would cycle.
        from fabulist.prompts.variables import render_display_text

        variables: dict[str, str] | None = None
        for item in user_messages:
            payload = item.get("payload")
            if not isinstance(payload, dict):
                continue
            text = payload.get("text")
            if not isinstance(text, str):
                continue
            if variables is None and "{{" in text:
                from fabulist.prompts.variables import display_variables

                variables = display_variables(self, session_id)
            item["payload"] = {
                **payload,
                "text_rendered": render_display_text(
                    self, session_id, text, variables=variables
                ),
            }

    @staticmethod
    def _validation_notice(payload: dict) -> dict | None:
        """One projection notice item from a ``validation_notice`` payload (D-F).

        Returns ``{seq, flags, score?}`` — ``seq`` is the flagged narrative's seq
        and the render anchor; ``flags`` is the row's teaching findings. Returns
        ``None`` for a row that cannot be placed (no positive narrative seq) or
        that carries no finding: such a row would render at no position, so the
        projection drops it rather than trusting the row's shape.
        """
        seq = payload.get("seq")
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 1:
            return None
        flags = payload.get("flags")
        if not isinstance(flags, list):
            return None
        findings = [dict(flag) for flag in flags if isinstance(flag, dict)]
        if not findings:
            return None
        notice: dict = {"seq": seq, "flags": findings}
        score = payload.get("score")
        if isinstance(score, int) and not isinstance(score, bool):
            notice["score"] = score
        return notice

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> Event:
        return Event(
            id=row["id"],
            ts=row["ts"],
            schema_version=row["schema_version"],
            session_id=row["session_id"],
            seq=row["seq"],
            type=row["type"],
            actor=row["actor"],
            payload=json.loads(row["payload"]),
            supersedes=row["supersedes"],
            superseded_by=row["superseded_by"],
            ephemeral=bool(row["ephemeral"]),
            meta=json.loads(row["meta"]),
        )
