"""Session-carried module homes (Wave B / D29).

Cards parse into rich V2 fields — ``system_prompt``,
``post_history_instructions``, ``mes_example`` — that used to be parsed but
never injected: the narrator never saw them. D29 makes them observable WITHOUT
letting a card mutate shared preset state (D9: the log owns everything). On
import each present field is *seeded* as a ``session_module`` **system event on
the chat session**; the narrator builder later splices those events into the
active preset's narrate flow at a named anchor
(:func:`fabulist.prompts.modules.splice_session_modules`).

Two anchors, mirroring SillyTavern's prompt slots:

* ``top`` — spliced before the first module of the narrate flow. The card
  ``system_prompt`` and ``mes_example`` live here.
* ``post_history`` — spliced immediately after the history inserter (falling
  back to before the first ``user``/``facts`` row for presets that predate the
  ``recalled`` row). The card ``post_history_instructions`` lives here.

Edits are events too: a content edit re-appends the same ``module_id``
(latest-wins), a disable re-appends it with ``enabled: false``, and a delete
appends a ``session_module_deleted`` tombstone (latest-wins per ``module_id``).
Because every change is an append, an edited session replays exactly.

Design (boring on purpose): this module only *shapes* the seed payloads and
*folds* the log into the current module list. The splice is pure and lives in
:mod:`fabulist.prompts.modules`; that module never imports the log, this one
does. ``wiring.py`` stays orchestrator-owned — it calls
:func:`session_modules_from_log` + the splicer just before compiling the flow.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ANCHOR_POST_HISTORY",
    "ANCHOR_TOP",
    "SESSION_MODULE_DELETED_EVENT",
    "SESSION_MODULE_EVENT",
    "seed_homes",
    "session_modules_from_log",
]

#: Anchor names valid for a seeded/edited session module. ``top`` inserts before
#: the first flow module; ``post_history`` inserts after the history inserter.
ANCHOR_TOP = "top"
ANCHOR_POST_HISTORY = "post_history"

#: System-event kind carrying one session module (its payload is the module).
SESSION_MODULE_EVENT = "session_module"

#: System-event kind tombstoning a session module by ``module_id``.
SESSION_MODULE_DELETED_EVENT = "session_module_deleted"

#: ``(card field, module name, anchor, role)`` in seed order. Only the fields a
#: V2 card reliably carries as free text; a sparse card seeds a sparse list.
_HOMES: tuple[tuple[str, str, str, str], ...] = (
    ("system_prompt", "Card system prompt", ANCHOR_TOP, "system"),
    (
        "post_history_instructions",
        "Card post-history instructions",
        ANCHOR_POST_HISTORY,
        "system",
    ),
    ("mes_example", "Card example messages", ANCHOR_TOP, "system"),
)


def _name(card: Any) -> str:
    """Card name, or the roster module's ``Unknown`` fallback.

    Accepts a :class:`~fabulist.cards.CardV2` or a plain dict (the seed event
    carries the dict form, but a caller may seed from the object).
    """
    if isinstance(card, dict):
        value = card.get("name")
    else:
        value = getattr(card, "name", None)
    return str(value or "Unknown")


def _field(card: Any, field_name: str) -> str:
    """Whitespace-stripped text of one card field, or ``""`` when absent/empty."""
    if isinstance(card, dict):
        value = card.get(field_name)
    else:
        value = getattr(card, field_name, None)
    if value is None:
        return ""
    return value.strip() if isinstance(value, str) else str(value)


def seed_homes(card: Any) -> list[dict]:
    """Session-event payloads seeding a card's present module homes (D29).

    Returns one ``system``-event payload per present field, in :data:`_HOMES`
    order: ``system_prompt`` (anchor ``top``), ``post_history_instructions``
    (anchor ``post_history``), ``mes_example`` (anchor ``top``). A card that
    omits a field — the common case — contributes nothing (sparse); the caller
    appends each payload to the freshly created session.

    ``module_id`` — ``card:<name>:<field>`` — is deterministic so a re-import
    or a later edit addresses the same row. ``origin`` — ``card:<name>`` —
    labels the module in the Session-overrides UI.
    """
    name = _name(card)
    origin = f"card:{name}"
    payloads: list[dict] = []
    for field_name, module_name, anchor, role in _HOMES:
        content = _field(card, field_name)
        if not content:
            continue
        payloads.append(
            {
                "event": SESSION_MODULE_EVENT,
                "module_id": f"card:{name}:{field_name}",
                "name": module_name,
                "anchor": anchor,
                "role": role,
                "content": content,
                "enabled": True,
                "origin": origin,
            }
        )
    return payloads


def session_modules_from_log(log: Any, session_id: str) -> list[dict]:
    """Fold a session's module events into its current, ordered module list.

    Walks the session's ``system`` events in seq order and applies the
    latest-wins rules: a ``session_module`` event sets/updates its
    ``module_id`` (a re-append replaces the content/enabled state), a
    ``session_module_deleted`` tombstone removes it. Surviving modules keep the
    order in which their first event appeared. Disabled modules stay in the
    result (the Session-overrides UI must show them to re-enable); the splice
    step drops them from the prompt. Fork-aware: reads the inherited base chain
    so a branch keeps its parent's card modules.
    """

    events = log.get_chain_events(session_id)
    order: list[str] = []
    by_id: dict[str, dict] = {}
    for event in events:
        if getattr(event, "type", None) != "system":
            continue
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            continue
        kind = payload.get("event")
        module_id = payload.get("module_id")
        if kind == SESSION_MODULE_DELETED_EVENT:
            if isinstance(module_id, str) and module_id in by_id:
                by_id.pop(module_id)
                order.remove(module_id)
            continue
        if kind != SESSION_MODULE_EVENT:
            continue
        if not isinstance(module_id, str) or not module_id:
            continue
        if module_id not in by_id:
            order.append(module_id)
        by_id[module_id] = dict(payload)
    return [by_id[module_id] for module_id in order]
