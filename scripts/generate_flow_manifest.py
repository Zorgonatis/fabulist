#!/usr/bin/env python
"""Generate the flow manifest (W9.1) from the code — never a hand-copied graph.

Writes ``prompts/generated/flow-manifest.json`` from
:func:`fabulist.flow.manifest.build_manifest` (the single topology seam shared
with ``GET /api/flow/manifest``), so the committed artifact and the endpoint
cannot drift. The output is deterministic (sorted keys, no timestamps) and
rerunning it is byte-identical — the same discipline as
``scripts/generate_agent_map.py`` and ``generate-library-spec.mjs``.

Usage::

    uv run python scripts/generate_flow_manifest.py            # write the artifact
    uv run python scripts/generate_flow_manifest.py --check    # exit 1 on drift
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:  # scripts/ is a namespace package, not installed
    sys.path.insert(0, str(REPO_ROOT))

from fabulist.flow.manifest import check, write  # noqa: E402

FLOW_MANIFEST_PATH = REPO_ROOT / "prompts" / "generated" / "flow-manifest.json"


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; ``--check`` returns non-zero on drift."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed artifact matches the code; exit 1 on drift",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.check:
        if not check(FLOW_MANIFEST_PATH):
            print(
                "DRIFT: prompts/generated/flow-manifest.json is stale — run: "
                "uv run python scripts/generate_flow_manifest.py",
                file=sys.stderr,
            )
            return 1
        print("ok prompts/generated/flow-manifest.json")
        return 0

    print(f"wrote {write(FLOW_MANIFEST_PATH)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
