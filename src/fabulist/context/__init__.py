"""Context hygiene stages (PLAN Wave 2).

W2.3 ships the regex pipeline stage: the port of the preset's ``regex_scripts``
that scrubs preset-internal machinery out of the text before it reaches the
narrator or the display surface.
"""

from .regex_stage import (
    PRESET_REGEXES,
    StageRegex,
    apply_regex_stage,
    compile_stage,
    js_flags_to_re,
    parse_js_regex,
)

__all__ = [
    "PRESET_REGEXES",
    "StageRegex",
    "apply_regex_stage",
    "compile_stage",
    "js_flags_to_re",
    "parse_js_regex",
]
