"""Assistants — Extra Assistant personas as subagents (PLAN W2.5).

Ports the preset's Extra Assistant surface (``SEGFAULT`` et al.) into a small,
injectable manager: each assistant has its own persona context and its own model
tier, and callers — never the manager — append replies to the event log (D9).

Wiring into the graph/API is deferred; this package is module + tests only.
"""

from .manager import (
    TRIGGER_MODES,
    AssistantContext,
    AssistantDefinition,
    AssistantManager,
    AssistantReply,
    build_assistant_messages,
    keyword_matches,
)

__all__ = [
    "TRIGGER_MODES",
    "AssistantContext",
    "AssistantDefinition",
    "AssistantManager",
    "AssistantReply",
    "build_assistant_messages",
    "keyword_matches",
]
