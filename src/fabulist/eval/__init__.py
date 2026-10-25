"""Evaluation rigs for the Fabulist extractor (W3.5).

The extractor seam (``fabulist.state``) turns one turn into a pipe-YAML state
delta. This package holds offline rigs that replay recorded/labeled turns through
an injected extract callable and score the result, so a seam can be measured
without a live model. ``fabulist.eval.replay`` is the key-level replay rig.

Public names are re-exported lazily (PEP 562) so ``python -m
fabulist.eval.replay`` does not double-import the submodule.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CaseResult",
    "ReplayError",
    "ReplayReport",
    "discover_cases",
    "load_case",
    "main",
    "render_report",
    "replay_extract",
    "report_to_dict",
    "validate_case",
]

_REPLAY_EXPORTS = frozenset(__all__)


def __getattr__(name: str) -> Any:
    if name in _REPLAY_EXPORTS:
        from . import replay

        return getattr(replay, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
