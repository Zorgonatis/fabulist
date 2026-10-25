"""ComfyUI image-generation engine (V1 image-gen lane).

Two seams, deliberately separable so the mapping is unit-testable without a
network:

* :mod:`fabulist.comfy.workflow` — pure parse + parameter mapping over a
  ComfyUI **API-format** workflow JSON (the ``{node_id: {class_type, inputs}}``
  shape the ``/prompt`` route consumes, NOT the UI graph format). It maps the
  adjustable keys ``seed`` / ``positive`` / ``negative`` / ``width`` / ``height``
  onto workflow node inputs and never mutates its input.
* :mod:`fabulist.comfy.client` — the ``httpx`` transport: submit to ``/prompt``,
  poll ``/history/{id}``, fetch bytes from ``/view``, and probe ``/system_stats``
  for the feature's enabled check.

The router lives in :mod:`fabulist.api.imagegen_api`; the orchestrator wires it
(app router include + the settings read).
"""

from __future__ import annotations

from .client import (
    ComfyClient,
    ComfyError,
    ComfyResponseError,
    ComfySubmitError,
    ComfyTimeout,
    first_image,
)
from .workflow import (
    ADJUSTABLE_KEYS,
    OverrideResult,
    ParamMap,
    apply_overrides,
    parse_workflow,
)

__all__ = [
    "ADJUSTABLE_KEYS",
    "ComfyClient",
    "ComfyError",
    "ComfyResponseError",
    "ComfySubmitError",
    "ComfyTimeout",
    "OverrideResult",
    "ParamMap",
    "apply_overrides",
    "first_image",
    "parse_workflow",
]
