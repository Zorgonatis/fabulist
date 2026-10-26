"""D16 memory re-index: re-embed every visible chat's events into its RESOLVED scope.

Backfill assigns personas to existing chats; new turns embed into the persona
collection (``p_<persona_id>``) via ``memory.scope.resolve_scope`` — but the
rows embedded BEFORE the assignment sit in the chat-local collections
(``c_<session_id>``). This script walks every VISIBLE session (soft-hidden chats
are skipped), resolves the CURRENT scope, and re-embeds each user/narrative/asset
event at its original seq into the resolved collection — so cross-chat persona
memory covers the whole back catalog, not just turns after the fence moved.

D5 semantics: the index is derived and rebuildable. Upserts are seq-keyed, so
re-running is idempotent. Opted-out chats (``chat_memory`` off) are skipped —
their rows are deleted-ish (upsert never fires); leaving them is harmless
(nothing reads them).

The enumeration + batch now live in :mod:`fabulist.memory.reembed` so the API's
background re-embed job (``POST /api/memory/reembed``) and this CLI script share
ONE implementation. This script only adds progress printing + a dry run.

Issue #85 adds ``--prune-orphans``: a one-time cleanup that deletes index rows
whose session is hidden or absent from the log (the rows the per-row delete
did not catch before it existed). It needs no embedder and never rebuilds.

CLI::

    uv run python scripts/reindex_memory.py --log data/fabulist.db [--dry-run]
    uv run python scripts/reindex_memory.py --log data/fabulist.db --prune-orphans [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from fabulist.log import EventLog
from fabulist.memory.reembed import (
    planned_sessions,
    prune_orphan_sessions,
    reembed_all,
    resolve_embedder,
    visible_session_ids,
)
from fabulist.connections.registry import Registry


async def reindex_all(log_path: Path, registry: Registry, *, dry_run: bool = False) -> dict:
    """Re-embed every visible session's events into the resolved scope."""
    from fabulist.memory.store import MemoryStore

    log = EventLog(log_path)
    try:
        target = resolve_embedder(registry)
    except Exception:  # noqa: BLE001 - no embedder = nothing to reindex
        print("no embedder connection — nothing to reindex", file=sys.stderr)
        return {"sessions": 0, "embedded": 0, "media": 0, "errors": [], "skipped": 0}
    store = MemoryStore(log_path.parent / "memory.db")

    visible = visible_session_ids(log)
    planned = planned_sessions(log)
    planned_ids = {session_id for session_id, _ in planned}
    for session_id in visible:
        if session_id not in planned_ids:
            print(f"  {session_id[:12]}… OPT-OUT (chat_memory off) — skipped")
    for session_id, scope in planned:
        label = "persona" if scope.persona_id else "sandbox"
        print(
            f"  {session_id[:12]}… -> {scope.collection[:24]} ({label})"
            f"{' [DRY]' if dry_run else ''}"
        )

    stats = await reembed_all(log, store, target, dry_run=dry_run)
    stats["skipped"] = len(visible) - len(planned)
    return stats


def prune_orphans(log_path: Path, *, dry_run: bool = False) -> dict:
    """Delete index rows whose session is hidden or absent from the log."""
    from fabulist.memory.store import MemoryStore

    log = EventLog(log_path)
    try:
        store = MemoryStore(log_path.parent / "memory.db")
        try:
            stats = prune_orphan_sessions(log, store, dry_run=dry_run)
        finally:
            store.close()
    finally:
        log.close()
    for session_id in stats["pruned"]:
        print(f"  {session_id[:12]}… orphan{' [DRY]' if dry_run else ' — deleted'}")
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default="data/fabulist.db")
    parser.add_argument("--config", default="configs/connections.json")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--prune-orphans",
        action="store_true",
        help="delete rows whose session is hidden or absent from the log, then exit",
    )
    args = parser.parse_args(argv)

    if args.prune_orphans:
        stats = prune_orphans(Path(args.log), dry_run=args.dry_run)
        print(
            f"\nprune: {stats['sessions']} orphan session(s), {stats['rows']} row(s)"
            + (" [DRY RUN]" if args.dry_run else "")
        )
        return 0

    registry = Registry.load(Path(args.config))
    stats = asyncio.run(reindex_all(Path(args.log), registry, dry_run=args.dry_run))
    print(
        f"\nreindex: {stats['sessions']} sessions, {stats['embedded']} rows embedded, "
        f"{stats['skipped']} opted-out"
        + (" [DRY RUN]" if args.dry_run else "")
    )
    if stats.get("errors"):
        print(f"{len(stats['errors'])} row(s) failed:", file=sys.stderr)
        for error in stats["errors"]:
            print(f"  {error}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
