"""Strip openui DSL from narrator text (the shared strip helper).

This is the canonical implementation of the DSL-strip behaviour that
``fabulist.prompts.compiler.strip_dsl`` carried (a live defect track: a
hand-listed five-name regex let flavor statements leak into narrator history as
raw text). The orchestrator swaps ``compiler.strip_dsl`` to call
:func:`strip_dsl` here — one recognizer, one name list, both from
:mod:`.spec`/:mod:`.statements`.

Handles explicit ```` ```openui ```` / ```` ```ui ```` fences and the unfenced
component statements g4-class models emit (balanced-paren statements may span
lines).
"""

from __future__ import annotations

import re

from .statements import statement_like_re

__all__ = [
    "OPEN_FENCE_RE",
    "strip_dsl",
]

#: An explicit openui fence opener line (`````openui`` or `````ui``).
OPEN_FENCE_RE = re.compile(r"```(?:openui|ui)\b[ \t]*\r?\n")


def strip_dsl(text: str) -> str:
    """Remove openui DSL from raw narrator text, keeping prose order."""
    if not text:
        return ""

    pattern = statement_like_re()
    kept: list[str] = []
    pos = 0
    for fence in OPEN_FENCE_RE.finditer(text):
        if fence.start() < pos:
            continue
        kept.append(text[pos : fence.start()])
        close = text.find("```", fence.end())
        pos = len(text) if close == -1 else close + 3
    kept.append(text[pos:])

    lines = "".join(kept).split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        if pattern.match(lines[i]):
            depth = lines[i].count("(") - lines[i].count(")")
            i += 1
            while i < len(lines) and depth > 0:
                depth += lines[i].count("(") - lines[i].count(")")
                i += 1
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out).strip()
