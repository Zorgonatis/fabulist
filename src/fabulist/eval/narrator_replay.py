"""Narrator prompt replay rig (W3.5, second half): replay vs the recorded config.

Every ``narrative`` event records a provenance snapshot in ``meta["config"]``::

    {"model": str, "sampler": dict, "prompt_version": int, "facts_hash": str}

``facts_hash`` is the provenance anchor stamped by the narrator message builder
(``fabulist/wiring.py`` :func:`make_narrator_message_builder`): it hashes the
*merged facts suffix* the narrator actually saw that turn -- the ``parts`` join of
``session_persona_block`` / ``persona_block``, roster, compaction summaries,
recall, world-info, STT, rolls and the clock facts -- as
``sha256(facts.encode("utf-8")).hexdigest()[:16]`` (see
``out["facts_hash"] = sha256_hex(facts.encode("utf-8"))[:16]`` beside the
``parts`` merge in wiring).

This rig replays the *prompt pipeline* for every recorded ``(user, narrative)``
turn: it re-renders the facts block with the **current** builder (injected as a
callable, exactly as the graph injects ``build_messages``) and compares the
resulting hash against the hash recorded at generation time.

    stable hash  -> the prompt pipeline is unchanged for that turn
    drifted hash -> the compiler/prompt changed (R10 regression signal)
    unknown      -> the narrative carries no recorded ``facts_hash``

Design notes
------------
* Only the builder is injected; this module never imports ``fabulist.wiring``
  (import-cycle risk: wiring imports the graph and the API). The rig takes a
  ``build_facts(events_up_to_turn, narrative_seq) -> str`` callable, mirroring
  how the graph takes ``build_messages`` as an injected seam.
* ``build_facts`` receives every event with ``seq <= narrative_seq`` -- the
  turn's own post-narrative state deltas come *after* the narrative event, so
  they are naturally excluded -- plus the narrative's seq, so a builder can
  reproduce clock advance and any per-turn logic.
* Unknown turns do **not** fail the run: they are recorded as ``matches=None``
  and counted as ``unknowns``. Only drift (``mismatch``) is a regression signal.

CLI
---
``python -m fabulist.eval.narrator_replay --log PATH --session ID [--json OUT]``

The CLI injects a deliberately *minimal* builder (see :func:`log_facts_builder`):
it folds the state-delta events up to the turn and renders the clock facts line.
It does **not** reproduce the persona block, summaries, recall, world-info, STT
or rolls that the real builder merges in, so its replayed hash will generally
differ from the recorded one whenever those parts are present. It is a
stability/drift probe for the clock-facts portion of the pipeline, not a full
verification of it -- documented honestly rather than papered over.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fabulist.api.attachments import _session_exists
from fabulist.graph.clock import advance, classify_action, render_facts
from fabulist.log import EventLog

__all__ = [
    "APPROXIMATION_NOTE",
    "BuildFactsFn",
    "ReplaySummary",
    "TurnReplay",
    "facts_hash",
    "log_facts_builder",
    "main",
    "render_summary",
    "replay_facts_hashes",
    "replay_report",
    "replays_to_dict",
]

#: The injected builder seam: ``(events_up_to_turn, narrative_seq) -> facts block``.
BuildFactsFn = Callable[[list[Any], int], str]

#: Honest caveat printed to stderr by the CLI (its builder is an approximation).
APPROXIMATION_NOTE = (
    "note: matching turns are byte-faithful replays of the FULL facts "
    "suffix (persona, roster, summaries, recall-from-recorded-text, "
    "world-info, STT, rolls, clock-advanced facts) via context.facts — the "
    "same single source wiring joins. Legacy turns (recorded before per-turn "
    "recall recording) are classified unknown (unverifiable), never drift."
)


def facts_hash(facts: str) -> str:
    """Hash a facts block the way wiring does: ``sha256(utf-8)[:16]``.

    Mirrors ``fabulist.wiring.make_narrator_message_builder``'s
    ``sha256_hex(facts.encode("utf-8"))[:16]`` convention exactly, so a hash
    computed here is byte-comparable to a recorded ``config["facts_hash"]``.
    """
    return hashlib.sha256(facts.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class TurnReplay:
    """One replayed ``(user, narrative)`` turn.

    ``recorded_hash`` is the ``config["facts_hash"]`` captured at generation
    time (``None`` when the narrative carries no provenance). ``replayed_hash``
    is the hash of the facts block the *current* builder produces for the same
    turn. ``matches`` is ``True``/``False`` when a recorded hash exists and
    ``None`` when it is unknown (no config / no ``facts_hash``) -- an unknown
    turn cannot be judged stable or drifted.
    """

    user_seq: int
    narrative_seq: int
    recorded_hash: str | None
    replayed_hash: str
    matches: bool | None


@dataclass(frozen=True)
class ReplaySummary:
    """Aggregate of a replay run: one bucket per :class:`TurnReplay` verdict."""

    turns: int
    matches: int
    mismatches: int
    unknowns: int

    def passes(self) -> bool:
        """True when no turn drifted (unknown turns are tolerated).

        A run with only matches and unknowns passes: the recorded config gave
        us nothing to compare for the unknowns, so they are not regressions.
        """
        return self.mismatches == 0


# -- event access (Event objects or projection dicts) ------------------------


def _attr(event: Any, name: str) -> Any:
    """Read ``name`` from an ``Event`` object or a projection-item ``dict``."""
    if isinstance(event, dict):
        return event.get(name)
    return getattr(event, name, None)


def _recorded_hash(event: Any) -> str | None:
    """The narrative's recorded ``meta["config"]["facts_hash"]``, or ``None``.

    ``None`` (unknown) whenever the meta is absent, the ``config`` block is not
    a dict, or ``facts_hash`` is missing/blank/non-string -- a snapshot with no
    hash cannot anchor a replay.
    """
    meta = _attr(event, "meta")
    if not isinstance(meta, dict):
        return None
    config = meta.get("config")
    if not isinstance(config, dict):
        return None
    value = config.get("facts_hash")
    if isinstance(value, str) and value:
        return value
    return None


def _pair_turns(events: list[Any]) -> list[tuple[int, int, Any]]:
    """Pair each narrative with its triggering user (nearest preceding user).

    ``post_turn`` appends the turn's ``user`` event immediately before its
    ``narrative`` (swipe narratives reuse the same user), so the nearest
    preceding user is the turn's trigger. Narratives before any user (a seeded
    opening) and users without a following narrative yield no pair. Returns
    ``(user_seq, narrative_seq, narrative_event)`` in seq order.
    """
    pairs: list[tuple[int, int, Any]] = []
    latest_user: int | None = None
    for event in events:
        etype = _attr(event, "type")
        if etype == "user":
            latest_user = int(_attr(event, "seq"))
        elif etype == "narrative" and latest_user is not None:
            pairs.append((latest_user, int(_attr(event, "seq")), event))
    return pairs


# -- replay ------------------------------------------------------------------


def replay_facts_hashes(events: list[Any], build_facts: BuildFactsFn) -> list[TurnReplay]:
    """Replay every recorded ``(user, narrative)`` turn against its config hash.

    ``events`` may be ``Event`` objects (``log.get_events``) or projection-item
    dicts; it is sorted by ``seq`` first. For each pair the rig calls
    ``build_facts(events_up_to_turn, narrative_seq)`` -- where the window is
    every event with ``seq <= narrative_seq`` -- hashes the returned facts block
    with :func:`facts_hash`, and compares it to the recorded
    ``config["facts_hash"]``. Missing provenance yields ``matches=None``.

    ``build_facts`` must return a ``str`` and must not raise; any exception
    propagates to the caller.
    """
    ordered = sorted(events, key=lambda event: int(_attr(event, "seq")))
    replays: list[TurnReplay] = []
    for user_seq, narrative_seq, narrative in _pair_turns(ordered):
        window = [
            event for event in ordered if int(_attr(event, "seq")) <= narrative_seq
        ]
        replayed = facts_hash(build_facts(window, narrative_seq))
        recorded = _recorded_hash(narrative)
        if recorded is None:
            matches = None
        elif _is_legacy_turn(narrative):
            # recorded hash but no recorded recall (pre-recording turn): the
            # recall block was a live vector query — unrecoverable from the
            # log alone — so byte-faithful replay is impossible. Classify as
            # LEGACY-unknown, never as drift.
            matches = None
        else:
            matches = recorded == replayed
        replays.append(
            TurnReplay(
                user_seq=user_seq,
                narrative_seq=narrative_seq,
                recorded_hash=recorded,
                replayed_hash=replayed,
                matches=matches,
            )
        )
    return replays


def _is_legacy_turn(narrative: Any) -> bool:
    """True when a narrative recorded a facts_hash but predates recall recording.

    Post-recording turns always carry ``meta["config"]["recall"]`` (possibly
    ``""`` — recorded unconditionally). A missing key means the turn predates
    the convention: its live recall block cannot be reconstructed from the
    log, so replay classifies it legacy-unknown rather than drifted.
    """
    conf = _recorded_config(narrative) or {}
    return "recall" not in conf


def _recorded_config(narrative: Any) -> dict[str, Any] | None:
    """The narrative's recorded provenance config dict, or ``None``."""
    meta = _attr(narrative, "meta")
    conf = (meta or {}).get("config") if isinstance(meta, dict) else None
    return conf if isinstance(conf, dict) else None


def replay_report(replays: list[TurnReplay]) -> ReplaySummary:
    """Tally replays into a :class:`ReplaySummary` (match / mismatch / unknown)."""
    matches = sum(1 for replay in replays if replay.matches is True)
    mismatches = sum(1 for replay in replays if replay.matches is False)
    unknowns = sum(1 for replay in replays if replay.matches is None)
    return ReplaySummary(
        turns=len(replays),
        matches=matches,
        mismatches=mismatches,
        unknowns=unknowns,
    )


# -- rendering ---------------------------------------------------------------


def render_summary(summary: ReplaySummary) -> str:
    """Render a summary as two stable, diffable lines (no timestamps)."""
    return (
        f"Fabulist narrator prompt replay — {summary.turns} turns: "
        f"{summary.matches} match, {summary.mismatches} mismatch, "
        f"{summary.unknowns} unknown\n"
        f"RESULT: {'PASS' if summary.passes() else 'FAIL'}"
    )


def replays_to_dict(replays: list[TurnReplay], summary: ReplaySummary) -> dict[str, Any]:
    """Serialise a replay run to a JSON-friendly dict (for ``--json OUT``)."""
    return {
        "turns": summary.turns,
        "matches": summary.matches,
        "mismatches": summary.mismatches,
        "unknowns": summary.unknowns,
        "passes": summary.passes(),
        "results": [
            {
                "user_seq": replay.user_seq,
                "narrative_seq": replay.narrative_seq,
                "recorded_hash": replay.recorded_hash,
                "replayed_hash": replay.replayed_hash,
                "status": (
                    "match"
                    if replay.matches is True
                    else "mismatch"
                    if replay.matches is False
                    else "unknown"
                ),
            }
            for replay in replays
        ],
    }


# -- CLI builder + entry point -----------------------------------------------


def log_facts_builder(log: "EventLog | None" = None, session_id: str = "") -> BuildFactsFn:
    """Full-fidelity facts-suffix replay via :mod:`fabulist.context.facts`.

    The suffix join lives in ``context.facts`` (one importable source, adopted
    by wiring too), so replay no longer approximates: it rebuilds persona,
    roster, summaries, world-info, STT and rolls exactly as the narrator
    merged them. Recall is replayed from the narrative's recorded
    ``meta["config"]["recall"]`` when present (the recall block is a live
    vector query at generation time; wiring records it per turn for exactly
    this purpose) and falls back to blank for legacy turns that predate the
    record -- flagged honestly below.

    Legacy turns (no recorded recall but a recall-shaped session) are reported
    by :func:`_legacy_recall_missing` so the summary can carry a
    ``legacy_recall`` count instead of silently calling them drifted.
    """

    def build_facts(events_up_to_turn: list[Any], narrative_seq: int) -> str:
        from fabulist.context import facts as facts_mod

        if log is None:
            return ""
        sid = session_id or None
        recall_text = ""
        for event in events_up_to_turn:
            if _attr(event, "type") == "narrative":
                meta = _attr(event, "meta")
                conf = (meta or {}).get("config", {}) if isinstance(meta, dict) else {}
                if conf.get("recall"):
                    recall_text = str(conf["recall"])
            if sid is None:
                payload = _attr(event, "payload")
                if isinstance(payload, dict) and payload.get("event") == "turn":
                    sid = str(payload.get("session_id") or "")
        # This turn's rolls only: the engine commits roll events after the
        # user event and before the narrative (pre-stream); walking backwards
        # from the narrative, rolls collected until the user event are this
        # turn's -- prior turns' rolls must not join this turn's block.
        roll_lines: list[str] = []
        user_text = ""
        for event in reversed(events_up_to_turn):
            etype = _attr(event, "type")
            p = _attr(event, "payload") if isinstance(_attr(event, "payload"), dict) else {}
            if etype == "user":
                user_text = str(p.get("text") or p.get("content") or "")
                break  # stop at this turn's user event
            if etype == "roll":
                roll_lines.insert(0, f"{p.get('dice')} -> {p.get('result')} ({p.get('detail')})")
        rolls_text = "### Rolls this turn\n" + "\n".join(roll_lines) if roll_lines else ""
        # recent = last-2 narratives BEFORE this turn's narrative existed at
        # build time (the live builder folds this window pre-generation; a
        # projection read after the fact must exclude this narrative).
        recent = " ".join(
            str(n.get("payload", {}).get("prose", ""))
            for n in [
                n
                for n in log.get_projection(sid).get("narratives", [])
                if int(n.get("seq") or 0) < narrative_seq
            ][-2:]
        )
        # Live `facts` part = the PRE-TURN CLOCK ADVANCE, not the raw fold:
        # pre_turn runs advance(action_class, prior_facts) and the advanced
        # string rides the builder's facts param. Replay the same
        # deterministic rules engine (classify + advance) to reproduce it.
        state: dict[str, Any] = {}
        for event in events_up_to_turn:
            if _attr(event, "type") == "state-delta":
                payload = _attr(event, "payload")
                if isinstance(payload, dict):
                    state.update(payload)
        prior_facts = render_facts(state) if state else ""
        action_class = classify_action(user_text) if user_text else "other"
        # pre_turn ALWAYS advances from `prior_facts`, falling back to the clock
        # default (08:00) when no state-delta exists yet — so replay must advance
        # unconditionally too, or a session's first turn (no prior state) drifts.
        facts_line = advance(action_class, prior_facts).facts
        # Macro rendering parity (D43): the live builder resolves the fixed
        # vocabulary and renders it into card/cast/WI text BEFORE hashing the
        # canonical context, so replay must resolve the same variables or a
        # macro-bearing card drifts (recorded hash = rendered text). Same
        # call shape as the live builder (wiring.py narrator branch).
        from fabulist.prompts.variables import resolve_variables

        try:
            variables = resolve_variables(log, sid, user_text or "")
        except Exception:  # noqa: BLE001 - replay must never crash on variables
            variables = {}
        # D22: the recorded facts_hash anchors the FULL canonical dynamic context
        # (state block + recalled block), so replay rebuilds that same canonical
        # list — byte-identical to the pre-D22 merged suffix, independent of the
        # message split. `facts_suffix_parts` is now the user-message half only.
        parts = facts_mod.context_suffix_parts(
            log,
            sid,
            recall_text=recall_text,
            user_text=f"{user_text}\n{recent}" if user_text else "",
            rolls_text=rolls_text,
            facts=facts_line,
            variables=variables,
        )
        return facts_mod.join_facts_suffix(parts)

    return build_facts


def main(argv: list[str] | None = None) -> int:
    """Replay a session's recorded turns and report drift.

    Loads the SQLite log at ``--log``, replays ``--session`` through
    :func:`log_facts_builder`, prints the summary, and (with ``--json``) writes
    the JSON report. Exits 0 when no turn drifted, 1 on any mismatch, and 2 on
    a usage/load error (unknown session, missing/unreadable log).
    """
    parser = argparse.ArgumentParser(
        prog="fabulist.eval.narrator_replay",
        description="Replay recorded narrator turns against their config facts_hash.",
    )
    parser.add_argument("--log", required=True, help="path to the SQLite event log")
    parser.add_argument("--session", required=True, help="session id to replay")
    parser.add_argument(
        "--json", dest="json_path", default=None, help="write the JSON report here"
    )
    args = parser.parse_args(argv)

    log_path = Path(args.log)
    if not log_path.exists():
        print(f"error: log does not exist: {log_path}", file=sys.stderr)
        return 2

    try:
        log = EventLog(log_path)
    except Exception as exc:  # noqa: BLE001 - any unreadable path is a usage error
        print(f"error: cannot open log: {exc}", file=sys.stderr)
        return 2
    try:
        if not _session_exists(log, args.session):
            print(f"error: unknown session: {args.session}", file=sys.stderr)
            return 2
        events = log.get_events(args.session)
        # full-fidelity builder needs the LIVE log (projection/persona reads);
        # keep it open through the replay, then close.
        replays = replay_facts_hashes(events, log_facts_builder(log, args.session))
        summary = replay_report(replays)
    except Exception as exc:  # noqa: BLE001 - replay failures are reported, not raised
        print(f"error: replay failed: {exc}", file=sys.stderr)
        return 2
    finally:
        log.close()

    print(APPROXIMATION_NOTE, file=sys.stderr)
    print(render_summary(summary))

    if args.json_path:
        out_path = Path(args.json_path)
        out_path.write_text(
            json.dumps(replays_to_dict(replays, summary), indent=2, ensure_ascii=False)
            + "\n",
            encoding="utf-8",
        )

    return 0 if summary.passes() else 1


if __name__ == "__main__":
    raise SystemExit(main())
