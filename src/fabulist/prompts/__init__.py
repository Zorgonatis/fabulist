"""Narrator prompt assembly: distilled directives + mini OpenUI library (PLAN W1.2)."""

from .library_mini import (
    MINI_COMPONENTS,
    ROOT_COMPONENT,
    MiniComponent,
    Prop,
    render_library_doc,
)
from .envelope import render_engine_envelope
from .narrator import (
    DEFAULT_FACTS_EXAMPLE,
    ENGINE_STATE_CLOSE,
    ENGINE_STATE_OPEN,
    FACTS_CLOSE,
    FACTS_OPEN,
    OOC_FLAG,
    assemble_messages,
    render_facts_block,
    render_state_block,
)

__all__ = [
    "DEFAULT_FACTS_EXAMPLE",
    "ENGINE_STATE_CLOSE",
    "ENGINE_STATE_OPEN",
    "FACTS_CLOSE",
    "FACTS_OPEN",
    "MINI_COMPONENTS",
    "MiniComponent",
    "OOC_FLAG",
    "Prop",
    "ROOT_COMPONENT",
    "assemble_messages",
    "render_engine_envelope",
    "render_facts_block",
    "render_library_doc",
    "render_state_block",
]
