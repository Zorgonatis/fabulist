"""Post-turn state extraction (PLAN R2 / D2 / W1.3).

Turns "user message + narrator prose" into a pipe-YAML state delta plus an action
class. The narrator never emits state; this package is the out-of-band deriver
that captures it after a kept turn.
"""

from .extractor import (
    ACTION_CLASSES,
    DELTA_KEYS,
    EXTRACTOR_STRUCTURED_REPAIR_INSTRUCTION,
    EXTRACTOR_SYSTEM_PROMPT,
    EXTRACTOR_TOOL_NAME,
    AsyncExtractor,
    DeltaParseError,
    ExtractionError,
    ExtractorFn,
    ExtractorResult,
    FallbackExtractor,
    InvalidActionClass,
    LLMExtractor,
    build_extractor_request_profile,
    extractor_tool,
    parse_action_class,
    parse_delta,
    parse_extractor_structured,
    strip_reasoning,
)
from .rules import RuleBasedExtractor

__all__ = [
    "ACTION_CLASSES",
    "DELTA_KEYS",
    "EXTRACTOR_STRUCTURED_REPAIR_INSTRUCTION",
    "EXTRACTOR_SYSTEM_PROMPT",
    "EXTRACTOR_TOOL_NAME",
    "AsyncExtractor",
    "DeltaParseError",
    "ExtractionError",
    "ExtractorFn",
    "ExtractorResult",
    "FallbackExtractor",
    "InvalidActionClass",
    "LLMExtractor",
    "RuleBasedExtractor",
    "build_extractor_request_profile",
    "extractor_tool",
    "parse_action_class",
    "parse_delta",
    "parse_extractor_structured",
    "strip_reasoning",
]
