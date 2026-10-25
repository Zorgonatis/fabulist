#!/usr/bin/env python
"""Fabulist API server entry point.

Usage:
  uv run python scripts/serve.py [--config <repo>/configs/connections.json]
                                  [--log <repo>/data/fabulist.db]
                                  [--checkpoints <repo>/data/checkpoints.db]
                                  [--host 127.0.0.1] [--port 2727]

Every DEFAULT is resolved against this checkout's root (from ``__file__``), not
against the caller's working directory, and ``FABULIST_ENV_FILE`` is pinned to
``<repo>/.env`` when the caller set none — so a bare ``python scripts/serve.py``
run from anywhere behaves like a repo-root run. Explicit flags still win and
keep their usual CWD-relative meaning.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import NamedTuple

import uvicorn

from fabulist.connections.registry import RegistryStore
from fabulist.graph.graph import open_sqlite_checkpointer
from fabulist.wiring import build_app

#: The checkout this script lives in — the anchor for every default below.
REPO_ROOT = Path(__file__).resolve().parents[1]

#: Printed once when the engine boots with no configured connection (first run).
#: The create door is the UI/API; the committed example is the hand-edit path.
#: JSON carries no comments, so the shape is explained HERE instead of inline.
FIRST_RUN_HINT = """\
fabulist: no connections configured yet (first run).
  Create one in the Connections drawer, or:
    POST /api/connections/local {"base_url": "http://127.0.0.1:8080/v1", "model": "your-model-id"}
  or copy the committed example and edit it:
    cp configs/connections.example.json configs/connections.json
  The example holds a local OpenAI-compatible server (http://127.0.0.1:8080/v1,
  LM Studio's default) and a cloud placeholder: a cloud entry's "api_key_env"
  names the ENVIRONMENT VARIABLE that holds the key, never the key itself.
  Then bind a slot: PATCH /api/connections/agents/narrator {"connection": "local"}"""


class RuntimePaths(NamedTuple):
    """The three files this entry point opens, already resolved."""

    config: Path
    log: Path
    checkpoints: Path


def resolve_paths(
    *,
    config: str | os.PathLike[str] | None = None,
    log: str | os.PathLike[str] | None = None,
    checkpoints: str | os.PathLike[str] | None = None,
) -> RuntimePaths:
    """Resolve the config/log/checkpoints paths: an explicit flag, else the repo.

    Only the DEFAULTS are repo-anchored (issue #5: running the server from
    another directory used to look for ``configs/`` and ``data/`` beside the
    caller, silently ignoring the repo's config and dotenv). An explicit flag is
    taken verbatim, so a relative ``--log data/x.db`` keeps its usual meaning:
    relative to the caller's working directory.
    """
    return RuntimePaths(
        config=Path(config)
        if config is not None
        else REPO_ROOT / "configs" / "connections.json",
        log=Path(log) if log is not None else REPO_ROOT / "data" / "fabulist.db",
        checkpoints=Path(checkpoints)
        if checkpoints is not None
        else REPO_ROOT / "data" / "checkpoints.db",
    )


def prepare_runtime(
    *,
    config: str | os.PathLike[str] | None = None,
    log: str | os.PathLike[str] | None = None,
    checkpoints: str | os.PathLike[str] | None = None,
) -> RuntimePaths:
    """The entry point's one path/env step: resolve, create directories, pin .env.

    Factored out of :func:`main` so the CWD-independence is testable without
    booting a server.
    """
    paths = resolve_paths(config=config, log=log, checkpoints=checkpoints)
    # First run (W3c): the create door writes configs/connections.json and the
    # log/checkpoints live under data/ — a fresh clone has neither directory.
    # These are the PARENTS OF THE RESOLVED PATHS, so a run from elsewhere
    # creates them where they belong and never litters the caller's CWD.
    for path in (paths.config, paths.log, paths.checkpoints):
        path.parent.mkdir(parents=True, exist_ok=True)
    # The dotenv pasted keys land in is the REPO's, whatever the CWD: the
    # registry store would otherwise default it to <cwd>/.env and silently
    # ignore the repo's keys — the failure this fixes. A caller that exported
    # FABULIST_ENV_FILE (a container pinning it to a mounted volume) still wins.
    if not os.environ.get("FABULIST_ENV_FILE"):
        os.environ["FABULIST_ENV_FILE"] = str(REPO_ROOT / ".env")
    return paths


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=None,
        help="connections document (default: <repo>/configs/connections.json)",
    )
    parser.add_argument(
        "--log", default=None, help="event log (default: <repo>/data/fabulist.db)"
    )
    parser.add_argument(
        "--checkpoints",
        default=None,
        help="checkpoint db (default: <repo>/data/checkpoints.db)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2727)
    parser.add_argument(
        "--agent-debug",
        action="store_true",
        help="D17/W5-S2: DEFAULT agent-debug capture ON for sessions that never "
        "set the per-session toggle; each session can flip it at runtime in the "
        "UI. Off by default (cost/size).",
    )
    args = parser.parse_args()

    paths = prepare_runtime(
        config=args.config, log=args.log, checkpoints=args.checkpoints
    )
    # D25 C1: hot-reloadable store (duck-types Registry). Callers keep this
    # object, so /api/connections PATCHes apply to the NEXT request without a
    # process restart; unknown keys in the config file survive rewrites.
    registry = RegistryStore(paths.config)
    if not registry.connections:
        print(FIRST_RUN_HINT, file=sys.stderr, flush=True)

    async with open_sqlite_checkpointer(paths.checkpoints) as checkpointer:
        # ONE build path: build_app owns engine + agent seams + hooks. serve
        # only supplies the opened checkpointer. Compaction ACTIVATED
        # 2026-10-09: vector recall is live and E2E-proven (D12 gate met) —
        # summaries narrow the prompt, the index + log keep every raw turn.
        app = build_app(
            registry=registry,
            log_path=paths.log,
            checkpointer=checkpointer,
            agent_debug=args.agent_debug,
        )
        app.state.connections_store = registry  # D25 C1: /api/connections seam
        config = uvicorn.Config(app, host=args.host, port=args.port, log_level="info")
        server = uvicorn.Server(config)
        await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
