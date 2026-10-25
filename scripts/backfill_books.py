#!/usr/bin/env python
"""Backfill D31 book entities from existing chats (migration CLI).

Walks every visible non-anchor session that carries session-scoped lore — a
card's ``character_book`` (``payload["card"]["character_book"]``) or a legacy
``world_info`` system event (the exact sources
:func:`fabulist.context.facts.session_world_info_block` renders). Each source
book is find-or-created as a reusable Book entity on the reserved ``__books__``
anchor session (deduped by :func:`fabulist.context.books.book_signature`) and
attached to the chat's backfilled persona (``Persona.book_ids``); a sandbox chat
with no persona keeps its lore session-scoped.

The log owns everything (D9): this script only appends book / persona events via
:func:`fabulist.context.books.backfill_books` — no side table is written and no
existing row is rewritten. The run is idempotent: a second pass finds the books
by signature and skips the persona update, appending nothing.

Usage::

    uv run python scripts/backfill_books.py [--log data/fabulist.db] [--apply]

**Dry-run is the default** — it performs the same detection and prints what
*would* happen without writing. Pass ``--apply`` to append the events.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from fabulist.context.books import backfill_books
from fabulist.log import EventLog


def main(argv: list[str] | None = None) -> int:
    """Run the book backfill; return a process exit code.

    ``--log`` selects the SQLite event-log file (default ``data/fabulist.db``).
    Dry-run is ON unless ``--apply`` is given. Prints a small metrics table of
    the counts returned by :func:`fabulist.context.books.backfill_books`.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log",
        default="data/fabulist.db",
        help="path to the SQLite event log (default: data/fabulist.db)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="report what would change without writing (the default)",
    )
    mode.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="append the book / persona events (disables the default dry-run)",
    )
    args = parser.parse_args(argv)

    # A fresh log path creates the DB (sqlite); make sure its directory exists.
    Path(args.log).parent.mkdir(parents=True, exist_ok=True)

    log = EventLog(args.log)
    try:
        result = backfill_books(log, dry_run=args.dry_run)
    finally:
        log.close()

    mode_label = "DRY RUN (no writes)" if args.dry_run else "APPLIED"
    print(f"book backfill — {mode_label}")
    print(f"log: {args.log}")
    print()
    _print_table(
        [
            ("sessions", result["sessions"]),
            ("books_created", result["books_created"]),
            ("attached", result["attached"]),
            ("left_session_scoped", result["left_session_scoped"]),
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
