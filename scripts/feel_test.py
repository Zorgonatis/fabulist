#!/usr/bin/env python
"""M1.5 feel-test harness: one real-model session, streamed to the terminal.

Runs three scripted turns against the LIVE local narrator (registry agent
"narrator", default localhost:4444) through the REAL wiring — clock, distilled
prompt, streaming with reasoning, DSL fences, extractor, event log — and prints
the resulting state after each turn. This is the gate the plan calls M1.5:
does out-of-band state + DSL-restricted narration feel right?

Usage: uv run python scripts/feel_test.py [--config configs/connections.json]
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
import uuid

from fabulist.connections.client import configure
from fabulist.log import EventLog
from fabulist.wiring import build_engine

TURNS: list[tuple[str, str]] = [
    ("in_character", "I push through the saloon doors of the Rusty Anchor tavern, "
     "soaked from the rain, and look for the harbormaster."),
    ("in_character", "I ignore him and head out into the storm to cross the market "
     "square to the chandler's shop. On the way I pick up a dropped brass lantern."),
    ("ooc", "OOC: quick recap for me - where am I and what's the weather doing?"),
]

DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
BOLD = "\033[1m"
RESET = "\033[0m"


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/connections.json")
    parser.add_argument("--log", default="data/feel_test.db")
    args = parser.parse_args()

    registry = configure(args.config)
    log_path = Path(args.log)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = EventLog(log_path)
    engine = build_engine(registry=registry, log=log)

    session_id = f"feel-{uuid.uuid4().hex[:8]}"
    log.append_event(
        session_id, "system", "human",
        {"title": "M1.5 feel test", "event": "session_created"},
    )
    print(f"{BOLD}session {session_id}{RESET}  (model: "
          f"{registry.connection_for('narrator').model})\n")

    for i, (kind, content) in enumerate(TURNS, 1):
        print(f"{BOLD}── turn {i} [{kind}] ──{RESET}")
        print(f"{GREEN}you: {content}{RESET}\n")
        async for event in engine.run_turn(session_id, content, kind):
            etype = event.get("type")
            if etype == "reasoning":
                sys.stdout.write(f"{DIM}{event.get('delta', '')}{RESET}")
                sys.stdout.flush()
            elif etype == "content":
                meta = event.get("meta") or {}
                if meta.get("channel") == "dsl":
                    sys.stdout.write(f"{YELLOW}{event.get('delta', '')}{RESET}")
                else:
                    sys.stdout.write(event.get("delta", ""))
                sys.stdout.flush()
            elif etype == "error":
                print(f"\033[31mERROR EVENT: {event}\033[0m")
            elif etype == "done":
                print(f"{DIM}[done: {event.get('meta')}]\n{RESET}")
        print("\n")
        print(f"{DIM}--- log tail ---{RESET}")
        for ev in log.get_events(session_id)[-6:]:
            print(f"{DIM}  #{ev.seq} {ev.type}/{ev.actor} payload={str(ev.payload)[:160]} ephem={ev.ephemeral}{RESET}")
        print()
        proj = log.get_projection(session_id)
        state = proj.get("state") or {}
        if state:
            facts = " | ".join(f"{k}: {v}" for k, v in state.items())
            print(f"{CYAN}state: {facts}{RESET}\n")
        for ev in log.get_events(session_id):
            if ev.type == "state-delta" and ev.seq > 0 and ev.payload:
                print(f"{CYAN}  delta logged: Δ[{ev.payload}]{RESET}")

    log.close()


if __name__ == "__main__":
    asyncio.run(main())
