#!/usr/bin/env python
"""Backfill D16 personas onto existing chats (migration CLI).

Walks every visible non-anchor session that carries a Character Card event
(``payload["card"]`` — the shape ``cards_api`` / :func:`fabulist.cards.to_session_seed`
writes), finds or creates a persona by the card's name on the reserved
``__personas__`` anchor session, and assigns it to the chat with a
``chat_persona`` system event. The run is idempotent: chats already assigned a
persona are skipped, so re-running appends nothing.

The log owns everything (D9): this script only appends persona / assignment
events via :func:`fabulist.personas.registry.backfill_from_cards` — no side
table is written and no existing row is rewritten.

Usage::

    uv run python scripts/backfill_personas.py [--log data/fabulist.db] [--dry-run]

``--dry-run`` performs the same detection and prints what *would* happen
without appending anything.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from fabulist.log import EventLog
from fabulist.personas.registry import backfill_from_cards


def main(argv: list[str] | None = None) -> int:
    """Run the persona backfill; return a process exit code.

    ``--log`` selects the SQLite event-log file (default ``data/fabulist.db``);
    ``--dry-run`` reports the plan without writing. Prints a small metrics table
    of the counts returned by :func:`backfill_from_cards`.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log",
        default="data/fabulist.db",
        help="path to the SQLite event log (default: data/fabulist.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without appending any events",
    )
    args = parser.parse_args(argv)

    # A fresh log path creates the DB (sqlite); make sure its directory exists.
    Path(args.log).parent.mkdir(parents=True, exist_ok=True)

    log = EventLog(args.log)
    try:
        result = backfill_from_cards(log, dry_run=args.dry_run)
    finally:
        log.close()

    mode = "DRY RUN (no writes)" if args.dry_run else "APPLIED"
    print(f"persona backfill — {mode}")
    print(f"log: {args.log}")
    print()
    _print_table(
        [
            ("sessions", result["sessions"]),
            ("assigned", result["assigned"]),
            ("personas_created", result["personas_created"]),
        ]
    )
    return 0


def _print_table(rows: list[tuple[str, int]]) -> None:
    """Print a two-column ``metric | value`` table sized to its content."""
    key_width = max(len(key) for key, _ in rows)
    value_width = max(len(str(value)) for _, value in rows)
    header = ("metric", "value")
    key_width = max(key_width, len(header[0]))
    value_width = max(value_width, len(header[1]))
    rule = f"+{'-' * (key_width + 2)}+{'-' * (value_width + 2)}+"
    print(rule)
    print(f"| {header[0].ljust(key_width)} | {header[1].ljust(value_width)} |")
    print(rule)
    for key, value in rows:
        print(f"| {key.ljust(key_width)} | {str(value).ljust(value_width)} |")
    print(rule)


if __name__ == "__main__":
    raise SystemExit(main())
