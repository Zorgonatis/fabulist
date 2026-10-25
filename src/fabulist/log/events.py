"""Event model for the Fabulist append-only log.

The log owns all data (PLAN.md D9). Every row is an immutable fact; corrections
arrive as `revision` events that supersede a prior event rather than rewriting
it (D10). Events carry a `schema_version` so future readers can skip shapes they
do not understand.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Current event payload/row schema. Bump when the shape of an event changes in
#: a way older readers cannot safely interpret.
SCHEMA_VERSION = 1

#: The closed set of event types. Enforced by the dataclass, the repository,
#: and a CHECK constraint in `schema.py`.
EVENT_TYPES = frozenset(
    {
        "user",
        "narrative",
        "revision",
        "state-delta",
        "roll",
        "asset",
        "summary",
        "ooc",
        "system",
    }
)


@dataclass
class Event:
    """One immutable entry in the event log."""

    id: int
    ts: str
    schema_version: int
    session_id: str
    seq: int
    type: str
    actor: str
    payload: dict
    supersedes: int | None = None
    superseded_by: int | None = None
    ephemeral: bool = False
    meta: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.type not in EVENT_TYPES:
            raise ValueError(f"unknown event type: {self.type!r}")

    def to_dict(self) -> dict:
        """Serializable view of the event (used by projections and the API)."""
        return {
            "id": self.id,
            "ts": self.ts,
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "seq": self.seq,
            "type": self.type,
            "actor": self.actor,
            "payload": self.payload,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "ephemeral": self.ephemeral,
            "meta": self.meta,
        }
