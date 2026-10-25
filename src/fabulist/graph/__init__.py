"""LangGraph turn engine: clock -> narrator -> extractor (W1.1).

Public surface:

* :func:`build_graph` — assemble the turn topology (W9.6): ``pre_turn ->
  narrate -> validate -> decision_gate -> critic -> (planner -> bts -> env_hud
  -> attribution -> done | narrate on a critic fail, cap 2)``.
* :func:`open_sqlite_checkpointer` — the SQLite checkpoint cache (D11).
* :class:`GraphTurnEngine` — `TurnEngine` implementation (stream events, OOC,
  cancel) over a compiled graph.
* :mod:`fabulist.graph.clock` — the deterministic rules clock.
* :mod:`fabulist.graph.types` — shared seams (`ModelCaller`, `ExtractorFn`,
  `LogPort`, `StreamEvent`, `TurnState`).
"""

from .clock import advance, classify_action
from .contracts import NODE_CONTRACTS, VIOLATION_CODES, validate_topology
from .engine import GraphTurnEngine, OOC_PROMPT_MARKER, TurnEngine
from .graph import (
    CANONICAL_TOPOLOGY,
    NODE_NAMES,
    build_graph,
    open_sqlite_checkpointer,
)
from .types import (
    ClockInputs,
    ClockOutputs,
    ExtractorFn,
    LogPort,
    ModelCaller,
    StreamEvent,
    TurnState,
    collect_text,
    initial_turn_state,
)

__all__ = [
    "CANONICAL_TOPOLOGY",
    "ClockInputs",
    "ClockOutputs",
    "ExtractorFn",
    "GraphTurnEngine",
    "LogPort",
    "ModelCaller",
    "NODE_CONTRACTS",
    "NODE_NAMES",
    "OOC_PROMPT_MARKER",
    "StreamEvent",
    "TurnEngine",
    "TurnState",
    "VIOLATION_CODES",
    "advance",
    "build_graph",
    "classify_action",
    "collect_text",
    "initial_turn_state",
    "open_sqlite_checkpointer",
    "validate_topology",
]
