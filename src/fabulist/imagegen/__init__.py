"""Image-gen definitions (ComfyUI workflow + prompt template) as log-owned assets.

PLAN.md COMFY-PLAN Wave 4, lane V2 — an **imagegen definition** is a nameable,
reusable pair of a ComfyUI API-format ``workflow`` and the user-supplied
``prompt_template`` that renders a request into positive/negative prompt text,
plus a parsed ``param_map`` (seed / positive / negative / resolution). Like
books (D31), definitions are session-free assets (non-negotiable 7) folded from
a reserved anchor session (``__imagegen__``); see
:mod:`fabulist.imagegen.registry`.
"""
