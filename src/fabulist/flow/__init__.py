"""Flow manifest (W9.1) — code-authored topology snapshot + generator seam.

The manifest is a descriptive, read-only projection of the engine's turn flow
(R5 node set) and its off-turn graphs, plus the prompt-assembly composition for
the narrator pass. It is never truth (C1): the event log owns everything and
nothing here appends events. The topology snapshot is built through one seam
(:func:`fabulist.flow.manifest.build_manifest`) so the committed artifact and
the read endpoint cannot drift, and so wave W9.6 can re-point the same schema at
the compiled LangGraph source.
"""

from __future__ import annotations

from fabulist.flow.manifest import (
    build_manifest,
    check,
    code_topology,
    render,
    write,
)

__all__ = ["build_manifest", "check", "code_topology", "render", "write"]
