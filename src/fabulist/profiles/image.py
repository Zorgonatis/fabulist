"""The profile's assigned image (owner ruling #7).

A profile PNG's body is "the assigned image" of the profile (owner ruling #7).
A profile is a *stack of definition layers*, and images live on definitions
(T4a: ``Persona.image_asset`` / ``World.image_asset``), so the assigned image is
the **top layer's** image, resolved strictly in layer order:

1. the world layer — ``envelope["world"]["world"]["image_asset"]``;
2. the cast layer — each ``envelope["world"]["cast"][i]["image_asset"]``, in
   ``world.cast`` order (the established roster order).

The first ``image_asset`` that **resolves** in the sessionless definition-asset
store wins: a layer carrying no image, or a dangling ref whose asset row is gone
(:func:`fabulist.media.images.find_definition_asset` returns ``None``), falls
through to the next layer rather than failing the export. Nothing resolvable
means the profile has no assigned image at all — the export route answers
``404`` (it does not fabricate a carrier PNG).

Read-only: this module never appends an event (D9).
"""

from __future__ import annotations

import base64
import binascii
from typing import TYPE_CHECKING, Any

from fabulist.media.images import find_definition_asset

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

__all__ = ["assigned_image", "assigned_image_refs"]


def assigned_image_refs(envelope: dict[str, Any]) -> list[str]:
    """The envelope's ``image_asset`` refs in layer order (world, then cast).

    Pure shape walk — no log read. A malformed/absent world bundle yields ``[]``.
    """
    bundle = envelope.get("world")
    if not isinstance(bundle, dict):
        return []

    refs: list[str] = []
    world = bundle.get("world")
    if isinstance(world, dict):
        refs.append(_ref(world))
    cast = bundle.get("cast")
    if isinstance(cast, list):
        for member in cast:
            if isinstance(member, dict):
                refs.append(_ref(member))
    return [ref for ref in refs if ref]


def _ref(definition: dict[str, Any]) -> str:
    """One definition's ``image_asset`` as a string, or ``""``."""
    value = definition.get("image_asset")
    return value if isinstance(value, str) else ""


def assigned_image(
    log: "EventLog", envelope: dict[str, Any]
) -> tuple[bytes, str] | None:
    """``(bytes, mime)`` of the profile's assigned image, or ``None``.

    Walks :func:`assigned_image_refs` in order and returns the first ref the
    anchor asset store resolves; ``None`` when no layer has a resolvable image.
    The mime is the stored one (definition images keep their uploaded type), so
    callers can decide what they can do with the bytes.
    """
    for ref in assigned_image_refs(envelope):
        item = find_definition_asset(log, ref)
        if item is None:
            continue
        payload = item.get("payload", {})
        try:
            data = base64.b64decode(payload.get("bytes_b64") or "", validate=True)
        except (binascii.Error, ValueError):
            continue
        if not data:
            continue
        return data, str(payload.get("mime") or "application/octet-stream")
    return None
