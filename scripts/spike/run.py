"""CLI entry point for the RP spike harness.

Runs a scenario's turns against the LIVE engine API, streams each turn to the
terminal (feel-test styling), and exports analysis-ready artifacts after
**every** turn so a crash leaves a resumable run::

    uv run python -m scripts.spike.run scenarios/dnd-guild.yaml
    uv run python -m scripts.spike.run scenarios/dnd-guild.yaml --turns 8
    uv run python -m scripts.spike.run scenarios/dnd-guild.yaml --resume data/spikes/dnd-guild-20261011-120000

Turn ordering (the exact contract):

1. scripted ``beats`` occupy turns ``1..len(beats)`` in order;
2. an ``ooc_probes`` entry **replaces** the sim turn at its absolute turn;
3. otherwise the player sim generates the turn;
4. ``player.directives`` only ever reach sim turns (never beats/probes).

Artifacts (written after every turn, atomically):

* ``manifest.json`` — ``{scenario, session_id, turns_done, started_at, updated_at}``
  plus resume extras (``turn_seconds``, ``states``).
* ``transcript.md`` — per turn: header, user message, prose, fenced DSL, state
  deltas (diffed from the projection's folded state), the BTS agent output,
  validator flags, rolls, usage.
* ``dsl_inventory.json`` — ``[{turn, seq, raw}]`` for every DSL statement emitted.
* ``telemetry.csv`` — per turn: wall seconds + the per-agent usage rows
  ``/inspect/turns`` exposes.

Readback sources are the API's ``GET /events``, ``GET /inspect/turns`` and
``GET /agent-calls`` (debug capture is toggled ON at seed time).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import httpx

from .client import DEFAULT_BASE_URL, SpikeClient, SpikeClientError, SpikeTurnError
from .player import PlayerSim, PlayerSimError, build_history, load_player_connection
from .scenario import Scenario, ScenarioError, load_scenario

__all__ = [
    "TurnPlan",
    "build_dsl_inventory",
    "build_turns_view",
    "main",
    "plan_turn",
    "render_telemetry",
    "render_transcript",
    "run",
]

#: ANSI palette shared with ``scripts/feel_test.py``.
DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RED = "\033[31m"
BOLD = "\033[1m"
RESET = "\033[0m"

#: Default artifact root (relative to the repo root / cwd).
DEFAULT_ARTIFACTS_ROOT = "data/spikes"

#: Default connections registry path (read-only for the player sim).
DEFAULT_CONNECTIONS = "configs/connections.json"

#: Per-agent response clip in the transcript (keeps a runaway capture sane).
_AGENT_TEXT_CLIP = 8000

#: The card-import seed marker user event (``cards.v2.to_session_seed``).
_IMPORT_MARKER = "[Imported card:"


@dataclass(frozen=True)
class TurnPlan:
    """What turn ``turn`` should do, decided before any model call."""

    turn: int
    source: str  # "beat" | "ooc_probe" | "sim"
    kind: str
    content: str | None  # set for beat/ooc_probe; None for sim
    directive: str | None  # only ever non-None for sim turns


def plan_turn(scenario: Scenario, turn: int) -> TurnPlan:
    """Decide the source/kind/content/directive for one 1-based turn."""
    beat = scenario.beat_for(turn)
    if beat is not None:
        return TurnPlan(turn, "beat", beat.kind, beat.content, None)
    probe = scenario.probe_for(turn)
    if probe is not None:
        return TurnPlan(turn, "ooc_probe", "ooc", probe.content, None)
    return TurnPlan(turn, "sim", "in_character", None, scenario.directive_for(turn))


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload(event: Any) -> dict:
    if isinstance(event, dict):
        value = event.get("payload")
        if isinstance(value, dict):
            return value
    return {}


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file + replace (crash-safe)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _atomic_write_json(path: Path, obj: Any) -> None:
    _atomic_write(path, json.dumps(obj, indent=2, ensure_ascii=False))


def _read_manifest(artifact_dir: Path) -> dict:
    path = artifact_dir / "manifest.json"
    if not path.is_file():
        raise SpikeClientError(f"no manifest.json in {artifact_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def _new_artifact_dir(root: Path, name: str, timestamp: str | None = None) -> Path:
    stamp = timestamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = root / f"{name}-{stamp}"
    suffix = 2
    while candidate.exists():
        candidate = root / f"{name}-{stamp}-{suffix}"
        suffix += 1
    return candidate


def _printer(stream: TextIO):
    """Return an ``on_event`` callable mirroring ``feel_test.py``'s colors."""

    def on_event(event: dict) -> None:
        etype = event.get("type")
        if etype == "reasoning":
            stream.write(f"{DIM}{event.get('delta', '')}{RESET}")
        elif etype == "content":
            channel = (event.get("meta") or {}).get("channel", "prose")
            if channel == "dsl":
                stream.write(f"{YELLOW}{event.get('delta', '')}{RESET}")
            else:
                stream.write(event.get("delta", ""))
        elif etype == "error":
            stream.write(f"\n{RED}ERROR EVENT: {event}{RESET}\n")
        elif etype == "done":
            stream.write(f"\n{DIM}[done: {event.get('meta')}]{RESET}\n")
        stream.flush()

    return on_event


# ---------------------------------------------------------------------------
# artifact rendering (pure)
# ---------------------------------------------------------------------------


def _collect_users(events: dict) -> list[dict]:
    users = [
        e
        for e in events.get("user_messages", [])
        if isinstance(e, dict) and isinstance(e.get("seq"), int)
    ]
    users.sort(key=lambda e: e["seq"])
    if users:
        first_text = str(_payload(users[0]).get("text") or "")
        if first_text.startswith(_IMPORT_MARKER):
            users = users[1:]
    return users


def _collect_narratives(events: dict) -> list[dict]:
    narratives = [
        e
        for e in events.get("narratives", [])
        if isinstance(e, dict) and isinstance(e.get("seq"), int)
    ]
    narratives.sort(key=lambda e: e["seq"])
    return narratives


def _dsl_statements(narrative: dict) -> list[str]:
    """Every DSL statement in one narrative (segments first, else split ``dsl``)."""
    payload = _payload(narrative)
    segments = payload.get("segments")
    raw_statements: list[str] = []
    if isinstance(segments, list):
        for segment in segments:
            if isinstance(segment, dict) and segment.get("kind") == "dsl":
                text = segment.get("text")
                if isinstance(text, str) and text.strip():
                    raw_statements.append(text.strip())
    if raw_statements:
        return raw_statements
    dsl = payload.get("dsl")
    if isinstance(dsl, str) and dsl.strip():
        return [line.strip() for line in dsl.splitlines() if line.strip()]
    return []


def _state_delta(previous: dict, current: dict) -> dict[str, str]:
    """Keys whose folded value changed between two per-turn state snapshots."""
    delta: dict[str, str] = {}
    for key in sorted(set(previous) | set(current)):
        before = previous.get(key)
        after = current.get(key)
        if before != after:
            delta[key] = f"{'' if before is None else before}->{'' if after is None else after}"
    return delta


def build_turns_view(
    scenario: Scenario,
    events: dict,
    inspect: dict,
    agent_calls: list[dict],
    states: dict[int, dict] | None = None,
    turn_seconds: dict[int, float] | None = None,
) -> list[dict]:
    """Join the API readbacks into one descriptor per played turn.

    Turn numbers are assigned by ordering the *played* user events (the card
    import seed marker is excluded). Narratives and OOC replies are attached to
    their preceding user event; agent calls are bucketed by the
    ``[user_seq, next_user_seq)`` window the inspector uses.
    """
    states = states or {}
    turn_seconds = turn_seconds or {}
    users = _collect_users(events)
    narratives = _collect_narratives(events)

    all_seqs = [
        e.get("seq", 0)
        for e in [*users, *narratives]
        if isinstance(e.get("seq"), int)
    ]
    for call in agent_calls:
        if isinstance(call.get("seq"), int):
            all_seqs.append(call["seq"])
    max_seq = max(all_seqs, default=0)

    windows: list[tuple[int, int]] = []
    for index, user in enumerate(users):
        low = user["seq"]
        high = users[index + 1]["seq"] if index + 1 < len(users) else max_seq + 1
        windows.append((low, high))

    # narrative -> the index of its immediately preceding played user.
    narrative_by_user: dict[int, dict] = {}
    for narrative in narratives:
        owner = None
        for index, user in enumerate(users):
            if user["seq"] < narrative["seq"]:
                owner = index
            else:
                break
        if owner is not None:
            narrative_by_user[owner] = narrative

    inspect_by_narrative: dict[int, dict] = {}
    for turn in inspect.get("turns", []):
        if isinstance(turn, dict) and isinstance(turn.get("narrative_seq"), int):
            inspect_by_narrative[turn["narrative_seq"]] = turn

    view: list[dict] = []
    for index, user in enumerate(users):
        turn_no = index + 1
        payload = _payload(user)
        kind = payload.get("kind", "in_character")
        low, high = windows[index]
        window_calls = [
            call
            for call in agent_calls
            if isinstance(call, dict) and low <= call.get("seq", -1) < high
        ]
        narrative = narrative_by_user.get(index)
        matched = (
            inspect_by_narrative.get(narrative["seq"]) if narrative is not None else None
        )
        prose = ""
        statements: list[str] = []
        if narrative is not None:
            prose = str(_payload(narrative).get("prose") or "")
            statements = [
                {"turn": turn_no, "seq": narrative["seq"], "raw": raw}
                for raw in _dsl_statements(narrative)
            ]

        ooc_reply = ""
        if kind == "ooc":
            ooc_calls = [c for c in window_calls if c.get("agent") == "ooc"]
            if ooc_calls:
                ooc_reply = str(ooc_calls[-1].get("response_text") or "")

        agents = {
            str(call.get("agent") or "?"): call for call in window_calls
        }
        previous_state = states.get(turn_no - 1, {})
        current_state = states.get(turn_no)
        delta = (
            _state_delta(previous_state, current_state)
            if current_state is not None
            else {}
        )
        beat = scenario.beat_for(turn_no)
        if beat is not None:
            source = "beat"
        elif scenario.probe_for(turn_no) is not None:
            source = "ooc_probe"
        else:
            source = "sim"

        view.append(
            {
                "turn": turn_no,
                "source": source,
                "kind": kind,
                "user_seq": user["seq"],
                "user_text": str(payload.get("text") or ""),
                "narrative_seq": narrative["seq"] if narrative else None,
                "prose": prose,
                "dsl_statements": statements,
                "ooc_reply": ooc_reply,
                "agent_calls": agents,
                "agents_all": window_calls,
                "validation_details": (matched or {}).get("validation_details", []),
                "rolls": (matched or {}).get("rolls", []),
                "tokens": (matched or {}).get("tokens", {}),
                "calls": (matched or {}).get("calls", []),
                "latency_s": (matched or {}).get("latency_s"),
                "delta": delta,
                "delta_count": (matched or {}).get("delta_count"),
                "wall_seconds": turn_seconds.get(turn_no),
                "complete": bool(narrative or (kind == "ooc" and ooc_reply)),
            }
        )
    return view


def build_dsl_inventory(turns_view: list[dict]) -> list[dict]:
    """``[{turn, seq, raw}]`` for every DSL statement across all turns."""
    inventory: list[dict] = []
    for turn in turns_view:
        inventory.extend(turn.get("dsl_statements", []))
    return inventory


def _clip(text: str, limit: int = _AGENT_TEXT_CLIP) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


def render_transcript(
    scenario: Scenario,
    session_id: str,
    turns_view: list[dict],
    *,
    generated_at: str | None = None,
) -> str:
    """Render ``transcript.md`` from the joined turn descriptors."""
    lines: list[str] = []
    lines.append(f"# Spike transcript — {scenario.title}")
    lines.append("")
    lines.append(f"- scenario: `{scenario.name}`")
    if scenario.flavor:
        lines.append(f"- flavor: `{scenario.flavor}`")
    lines.append(f"- session: `{session_id}`")
    lines.append(f"- player: {scenario.player.name}")
    lines.append(f"- generated: {generated_at or _now_iso()}")
    lines.append("")

    for turn in turns_view:
        status = "" if turn.get("complete") else "  _(incomplete)_"
        lines.append(
            f"## Turn {turn['turn']} — {turn['kind']} ({turn['source']}){status}"
        )
        lines.append("")
        lines.append(f"**User:** {turn['user_text']}")
        lines.append("")
        if turn.get("prose"):
            lines.append("**Narrator prose:**")
            lines.append("")
            lines.append(turn["prose"].strip())
            lines.append("")
        if turn.get("ooc_reply"):
            lines.append("**OOC reply:**")
            lines.append("")
            lines.append(_clip(turn["ooc_reply"]).strip())
            lines.append("")
        if turn.get("dsl_statements"):
            lines.append("**DSL:**")
            lines.append("")
            lines.append("```openui")
            for statement in turn["dsl_statements"]:
                lines.append(statement["raw"])
            lines.append("```")
            lines.append("")

        delta = turn.get("delta") or {}
        if delta:
            rendered = " | ".join(f"{k}: {v}" for k, v in delta.items())
            lines.append(f"**State deltas:** Δ[{rendered}]")
            lines.append("")
        elif turn.get("delta_count") is not None:
            lines.append(f"**State deltas:** ({turn['delta_count']} delta event(s))")
            lines.append("")

        agents = turn.get("agent_calls") or {}
        call = agents.get("bts")
        if call and str(call.get("response_text") or "").strip():
            lines.append("**Behind the scenes:**")
            lines.append("")
            lines.append(_clip(str(call["response_text"])).strip())
            lines.append("")

        validations = turn.get("validation_details") or []
        if validations:
            lines.append(f"**Validator flags:** {json.dumps(validations, ensure_ascii=False)}")
        else:
            lines.append("**Validator flags:** none")
        lines.append("")

        rolls = turn.get("rolls") or []
        if rolls:
            lines.append(f"**Rolls:** {json.dumps(rolls, ensure_ascii=False)}")
            lines.append("")

        tokens = turn.get("tokens") or {}
        calls = turn.get("calls") or []
        if calls:
            per_agent = ", ".join(
                f"{c.get('agent')}: {c.get('total_tokens', 0)}tok/{c.get('latency_s', 0):.1f}s"
                for c in calls
            )
            lines.append(
                f"**Usage:** prompt={tokens.get('prompt_tokens', 0)} "
                f"completion={tokens.get('completion_tokens', 0)} "
                f"total={tokens.get('total_tokens', 0)} | {per_agent}"
            )
        elif turn.get("latency_s") is not None:
            lines.append(f"**Usage:** latency={turn['latency_s']:.1f}s")
        else:
            lines.append("**Usage:** (none)")
        if turn.get("wall_seconds") is not None:
            lines.append(f"**Wall:** {turn['wall_seconds']:.1f}s")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_telemetry(turns_view: list[dict]) -> str:
    """Render ``telemetry.csv`` (long format: one row per usage call)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "turn",
            "wall_seconds",
            "agent",
            "model",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "latency_s",
        ]
    )
    for turn in turns_view:
        wall = turn.get("wall_seconds")
        wall_text = "" if wall is None else f"{wall:.3f}"
        calls = turn.get("calls") or []
        if not calls:
            writer.writerow([turn["turn"], wall_text, "", "", "", "", "", ""])
            continue
        for call in calls:
            writer.writerow(
                [
                    turn["turn"],
                    wall_text,
                    call.get("agent", ""),
                    call.get("model", ""),
                    call.get("prompt_tokens", ""),
                    call.get("completion_tokens", ""),
                    call.get("total_tokens", ""),
                    call.get("latency_s", ""),
                ]
            )
    return buffer.getvalue()


def _export_artifacts(
    artifact_dir: Path,
    scenario: Scenario,
    session_id: str,
    client: SpikeClient,
    states: dict[int, dict],
    turn_seconds: dict[int, float],
) -> None:
    """Fetch readbacks and (re)write every artifact atomically."""
    events = client.get_events(session_id)
    inspect = client.get_inspect_turns(session_id)
    calls = client.get_agent_calls(session_id)
    turns_view = build_turns_view(
        scenario, events, inspect, calls, states, turn_seconds
    )
    _atomic_write(
        artifact_dir / "transcript.md",
        render_transcript(scenario, session_id, turns_view),
    )
    _atomic_write_json(
        artifact_dir / "dsl_inventory.json", build_dsl_inventory(turns_view)
    )
    _atomic_write(artifact_dir / "telemetry.csv", render_telemetry(turns_view))


# ---------------------------------------------------------------------------
# run loop
# ---------------------------------------------------------------------------


def _write_manifest(
    artifact_dir: Path,
    *,
    scenario: Scenario,
    session_id: str,
    turns_done: int,
    started_at: str,
    total_turns: int,
    turn_seconds: dict[int, float],
    states: dict[int, dict],
) -> None:
    manifest = {
        "scenario": scenario.name,
        "session_id": session_id,
        "turns_done": turns_done,
        "started_at": started_at,
        "updated_at": _now_iso(),
        # Resume + analysis extras (additive; the contract keys above are fixed).
        "scenario_path": str(scenario.source_path),
        "name": scenario.name,
        "title": scenario.title,
        "flavor": scenario.flavor,
        "card": str(scenario.card),
        "turns": total_turns,
        "player_model": scenario.player_model,
        "turn_seconds": {str(k): v for k, v in turn_seconds.items()},
        "states": {str(k): v for k, v in states.items()},
    }
    _atomic_write_json(artifact_dir / "manifest.json", manifest)


def run(
    scenario_path: str | Path,
    *,
    turns: int | None = None,
    resume: str | Path | None = None,
    player_model: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    connections_path: str | Path = DEFAULT_CONNECTIONS,
    artifacts_root: str | Path = DEFAULT_ARTIFACTS_ROOT,
    http_client: httpx.Client | None = None,
    out: TextIO | None = None,
    timestamp: str | None = None,
) -> Path:
    """Execute a scenario; return the artifact directory.

    Raises :class:`ScenarioError` for a bad scenario file and the client/player
    errors for live failures — in every case the manifest on disk reflects the
    last fully completed turn, so the run can be resumed.
    """
    stream = out if out is not None else sys.stdout
    scenario = load_scenario(scenario_path)
    total_turns = int(turns) if turns is not None else scenario.turns
    if total_turns < 1:
        raise ScenarioError(f"--turns must be >= 1, got {total_turns}")

    client = SpikeClient(base_url, card_path=scenario.card, client=http_client)
    player: PlayerSim | None = None
    turn_seconds: dict[int, float] = {}
    states: dict[int, dict] = {}
    try:
        if resume is not None:
            artifact_dir = Path(resume)
            manifest = _read_manifest(artifact_dir)
            session_id = str(manifest["session_id"])
            turns_done = int(manifest.get("turns_done", 0))
            # No --turns given: inherit the total the original run used (the
            # manifest records it even when it came from a --turns override).
            if turns is None and isinstance(manifest.get("turns"), int):
                total_turns = int(manifest["turns"])
            started_at = str(manifest.get("started_at") or _now_iso())
            turn_seconds = {
                int(k): float(v) for k, v in (manifest.get("turn_seconds") or {}).items()
            }
            states = {
                int(k): v for k, v in (manifest.get("states") or {}).items()
            }
            # Confirm the session still resolves through the live API.
            client.get_events(session_id)
            stream.write(
                f"{BOLD}resuming {scenario.name} — session {session_id} "
                f"({turns_done}/{total_turns} turns done){RESET}\n"
            )
        else:
            artifact_dir = _new_artifact_dir(Path(artifacts_root), scenario.name, timestamp)
            artifact_dir.mkdir(parents=True, exist_ok=True)
            seed = client.seed_session()
            session_id = seed.session_id
            if scenario.title:
                client.rename_session(session_id, scenario.title)
            client.set_agent_debug(session_id, True)
            if scenario.worldinfo:
                client.post_worldinfo(session_id, scenario.worldinfo_book())
            turns_done = 0
            started_at = _now_iso()
            stream.write(
                f"{BOLD}session {session_id}{RESET}  card={scenario.card.name}  "
                f"artifacts={artifact_dir}\n"
            )
            _write_manifest(
                artifact_dir,
                scenario=scenario,
                session_id=session_id,
                turns_done=0,
                started_at=started_at,
                total_turns=total_turns,
                turn_seconds=turn_seconds,
                states=states,
            )

        model_name = player_model or scenario.player_model
        on_event = _printer(stream)

        for turn in range(turns_done + 1, total_turns + 1):
            plan = plan_turn(scenario, turn)
            stream.write(f"\n{BOLD}── turn {turn} [{plan.source}] ──{RESET}\n")

            if plan.source == "sim":
                if player is None:
                    connection = load_player_connection(connections_path, model_name)
                    player = PlayerSim(
                        connection,
                        name=scenario.player.name,
                        persona=scenario.player.persona,
                        client=http_client,
                    )
                events_before = client.get_events(session_id)
                history = build_history(
                    events_before.get("user_messages"),
                    events_before.get("narratives"),
                )
                stream.write(f"{DIM}[sim: {model_name} via {player.connection.base_url}]{RESET}\n")
                content = player.generate(history, plan.directive)
                if plan.directive:
                    stream.write(f"{CYAN}[director note: {plan.directive}]{RESET}\n")
            else:
                content = plan.content or ""

            stream.write(f"{GREEN}you: {content}{RESET}\n\n")
            started = time.monotonic()
            client.stream_turn(session_id, content, plan.kind, on_event=on_event)
            wall = time.monotonic() - started
            turn_seconds[turn] = wall

            events_after = client.get_events(session_id)
            states[turn] = events_after.get("state") or {}
            turns_done = turn

            _write_manifest(
                artifact_dir,
                scenario=scenario,
                session_id=session_id,
                turns_done=turns_done,
                started_at=started_at,
                total_turns=total_turns,
                turn_seconds=turn_seconds,
                states=states,
            )
            _export_artifacts(
                artifact_dir,
                scenario,
                session_id,
                client,
                states,
                turn_seconds,
            )
            stream.write(
                f"{DIM}  ({wall:.1f}s; {turns_done}/{total_turns} turns; "
                f"artifacts updated){RESET}\n"
            )

        stream.write(
            f"\n{BOLD}done — {turns_done}/{total_turns} turns{RESET}  "
            f"artifacts: {artifact_dir}\n"
        )
        return artifact_dir
    finally:
        if player is not None:
            player.close()
        client.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scripts.spike.run",
        description="Scenario-driven RP spike harness against the live engine API.",
    )
    parser.add_argument("scenario", help="path to the scenario YAML file")
    parser.add_argument("--turns", type=int, default=None, help="override scenario turns")
    parser.add_argument(
        "--resume",
        default=None,
        help="artifact directory of a prior run to resume (reopens its session)",
    )
    parser.add_argument(
        "--player-model",
        default=None,
        help="override scenario player_model (a connections.json name)",
    )
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="engine API base URL")
    parser.add_argument(
        "--connections", default=DEFAULT_CONNECTIONS, help="connections.json path"
    )
    parser.add_argument(
        "--artifacts-dir", default=DEFAULT_ARTIFACTS_ROOT, help="artifact root directory"
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    http_client: httpx.Client | None = None,
    out: TextIO | None = None,
) -> int:
    """CLI entry. Returns 0 on success, 1 on a (resumable) failure, 2 on usage error."""
    parser = build_parser()
    args = parser.parse_args(argv)
    stream = out if out is not None else sys.stdout
    try:
        artifact_dir = run(
            args.scenario,
            turns=args.turns,
            resume=args.resume,
            player_model=args.player_model,
            base_url=args.base_url,
            connections_path=args.connections,
            artifacts_root=args.artifacts_dir,
            http_client=http_client,
            out=stream,
        )
    except (ScenarioError, SpikeClientError, SpikeTurnError, PlayerSimError) as exc:
        stream.write(f"{RED}spike aborted: {exc}{RESET}\n")
        if args.resume:
            stream.write(
                f"{DIM}resume with: uv run python -m scripts.spike.run "
                f"{args.scenario} --resume {args.resume}{RESET}\n"
            )
        else:
            stream.write(
                f"{DIM}the artifact dir was printed above ('artifacts=...'); "
                f"resume with: uv run python -m scripts.spike.run {args.scenario} "
                f"--resume <that dir>{RESET}\n"
            )
        return 1
    stream.write(f"{DIM}artifacts: {artifact_dir}{RESET}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
