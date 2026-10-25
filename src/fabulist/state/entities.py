"""Entity registry (W9.29 §3/§5/§11.1): identity-stable BTS world-state objects.

Root cause this module fixes: the BTS tracker's world state was a **free-form,
per-turn payload** (``system``/``world_state``). A room, a plot thread or a
meter had no identity — "hostility 42" on turn 2 was a string in a fresh dict
and nothing joined it to turn 1's "hostility 47", so every downstream reader
(presence, HUD, narrator facts, planner) re-guessed what a thing *was*. W9.29
gives every tracked thing an id.

The model (plan §3)
-------------------
A **being is not a new object**: it is an existing cast ``member_id``
(:mod:`fabulist.state.cast`) plus the one reserved literal ``player``; the
scene is the reserved literal ``scene``. The registry owns eight object kinds:

* ``location`` (``loc_``, §3.1) — rooms/areas granularity, optional
  ``parent_id`` area chain, and the **reserved, always-live** off-screen bucket
  ``loc_off_screen`` (the absence destination every turn offers, §3.1/req. 2);
* ``thread`` (``th_``, §3.2) — status vocabulary ``new|active|paused|resolved``;
* ``relationship`` (``rel_``, §3.3) — the ``(from, to)`` prose edge, numbers
  live on meters, never here;
* ``meter`` (``mt_``, §3.4) — identity ``(host, subject, key)``, so turn 2's
  "hostility 42 -> 30" is the **same object** (the epic's headline example);
* ``item`` (``itm_``, §3.5) — ``holder`` is the being; transfer is an update;
* ``condition`` (``cd_``, W9.32 §3.1) — a state with a CLOCK expiry: identity
  ``(host, key)``, ``severity`` in ``minor|moderate|major``, and ``expires_at``
  an ``YYYY-MM-DD HH:MM`` instant or ``""`` for permanent (the turn-based
  ``[suspicious-2t]`` notation is deliberately not revived, O4);
* ``body`` (``bd_``, W9.32 §3.1) — ONE per being: pose, ordered clothing layers
  (outer→inner), and the visible condition surface. An object rather than three
  catalog rows because a layer set is one structured ordered edit, and "one body
  per being" then comes free from host identity instead of needing a rule;
* ``economy`` (``eco_``, W9.32 §3.1) — ONE per session: denomination + integer
  balance, seeded once by the planner (the only seeder, §5) and adjusted by the
  tracker.

The singletons and the occurrence rule (W9.32 §3.1)
---------------------------------------------------
A body is keyed by its HOST and an economy by the session, so :func:`set_body`
and :func:`adjust_economy` amend the one object instead of minting a second (a
second establish for either identity would be a different person's body or a
second currency for one purse). A condition is the one deliberate exception to
the D10 resurrection shape documented below: its identity is one OCCURRENCE of
``(host, key)``, so a condition re-established after its tombstone mints a
**fresh** ``cd_`` id — a healed wound and a new wound are two events in the log,
and the expiry sweep must never re-retire an id it already retired.

Being→location is **not** an object: ``assignment[who] = loc_id | "off_screen"``
(§3.6), latest ``entity_move`` wins per being. Presence semantics (req. 13):
a member is in-scene iff ``assignment[who] == assignment[player]`` — object
equality, no freshness window; any other location (the bucket included) is a
deterministic presence exit.

Storage + fold (plan §5, decision point §14.4)
---------------------------------------------
One writer module, typed ``system``-event variants in the ``cast_admit`` family
(never a new top-level event type, never a rewritten ``world_state`` payload —
whole-payload overwrite is exactly what this epic kills). Each validated action
is ONE append (D9: agents propose, the engine appends; the model never writes),
the fold is dumb, and there is **no merge machinery** (req. 4: duplicates are
possible by design, the palette is what prevents conflation).

* :data:`ENTITY_ESTABLISH_EVENT` — an object enters (id rides the event);
* :data:`ENTITY_SET_EVENT` — one meter/trait value, latest-wins per id;
* :data:`ENTITY_UPDATE_EVENT` — in-place field patch, id never changes;
* :data:`ENTITY_MOVE_EVENT` — a being assignment (``+ at`` sets the bucket ref);
* :data:`ENTITY_RETIRE_EVENT` — a tombstone (D10-recoverable: a later establish
  of the same identity resurrects, see the minting note);
* :data:`ENTITY_SUMMARY_EVENT` — one observability row per turn (counts only).

Id minting follows the cast pattern verbatim (:func:`fabulist.state.cast._mint_member_id`):
``prefix + sha1(kind|identity|index)[:12]``, the id **stored inside the event**
so a replay or a fork reads it instead of re-minting, and a counter suffix on
collision. ``loc_off_screen``, ``player`` and ``scene`` are
reserved literals and are never minted (§5). Because the mint excludes
tombstoned ids, re-establishing a retired thing by name re-mints the SAME id —
that is the D10 resurrection shape.

The legacy shim (plan §11.1, req. 15 — forward-only, no backfill)
-----------------------------------------------------------------
``system``/``world_state`` payloads stay readable forever. :func:`registry`
folds the entity lane first and merges the legacy world-state fold **beneath**
it: registry assignment wins per being, registry meters win per ``key``, and
legacy-only facts surface **name-only** (no ids) until the model establishes a
real object for them. The accumulation (checkpoint replaces, delta deep-merges
``state``, ``venue``/``tension`` latest-wins, ``threads``/``off_screen`` by
``name``) is the ``_fold_world_state`` semantics re-implemented here at the one
state-layer seam §11.1 names; the per-payload normalisation and the flat
``loc``/``weather``/``tone``/``inv`` derivation are IMPORTED from
:mod:`fabulist.state.bts` (:func:`~fabulist.state.bts.parse_bts_structured`,
:func:`~fabulist.state.bts.fold_flat`) rather than duplicated.

``forecast`` is deliberately absent from :func:`flat_render` (review R2): the
flat fold's ``forecast`` key is the D45 *manual* HUD pin, and a model trait must
never clobber it. The value stays reachable through :func:`scene_traits` so the
W3 env_hud node can feed it as the ladder's own rung.

The read shim (W9.32 §6)
------------------------
``loc``/``weather``/``tone``/``inv`` used to be WRITTEN into the flat
``state-delta`` beside the registry object that already held them: two copies
of one fact, free to drift. W11 retires the shadow writes and every consumer
reads :func:`rendered_state` instead — the projection fold (clock keys, the
``forecast`` pin, any legacy key) overlaid by the derived registry view, plus
the derived ``genre`` (scene trait, R8) and the derived ``conds`` line (§3.6).
:func:`flat_render` stays as the precedence rule that overlay is built from.

Hygiene: nothing here imports the API or graph layers (the cast/presence
pattern) — this is a pure projection over the fork-aware chain, like
:func:`fabulist.state.cast.roster`.
"""

from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Iterable, Sequence

from .bts import BtsParseError, fold_flat, parse_bts_structured
from .cast import canonical_key, member_by_id, roster

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

__all__ = [
    "CONDS_LINE_MAX",
    "CONDITION_SEVERITIES",
    "CONDITION_SEVERITY_DEFAULT",
    "DEFAULT_ACTOR",
    "ENTITY_ESTABLISH_EVENT",
    "ENTITY_EVENT_KINDS",
    "ENTITY_MOVE_EVENT",
    "ENTITY_RETIRE_EVENT",
    "ENTITY_SET_EVENT",
    "ENTITY_SUMMARY_EVENT",
    "ENTITY_UPDATE_EVENT",
    "ENTITY_OBJECT_KINDS",
    "Body",
    "Condition",
    "Economy",
    "EntityError",
    "INSTANT_FORMAT",
    "Item",
    "LegacyWorld",
    "Location",
    "Meter",
    "OFF_SCREEN",
    "OFF_SCREEN_LOCATION_ID",
    "PLAYER",
    "Registry",
    "Relationship",
    "RESERVED_LITERALS",
    "SCENE",
    "THREAD_STATUSES",
    "Thread",
    "active_conditions",
    "adjust_economy",
    "append_entity_summary",
    "being_render",
    "body_for",
    "conditions_for",
    "economy_of",
    "establish_economy",
    "establish_item",
    "establish_location",
    "establish_relationship",
    "establish_thread",
    "expire_conditions",
    "expired_conditions",
    "flat_render",
    "location_path",
    "mint_body_id",
    "mint_condition_id",
    "mint_economy_id",
    "mint_item_id",
    "mint_location_id",
    "mint_meter_id",
    "mint_relationship_id",
    "mint_thread_id",
    "move_being",
    "occupants",
    "registry",
    "rendered_state",
    "resolve_location",
    "retire_object",
    "same_room",
    "scene_traits",
    "set_attribute",
    "set_body",
    "set_condition",
    "update_object",
]

# -- event names (plan §5) ---------------------------------------------------

ENTITY_ESTABLISH_EVENT = "entity_establish"
ENTITY_SET_EVENT = "entity_set"
ENTITY_UPDATE_EVENT = "entity_update"
ENTITY_MOVE_EVENT = "entity_move"
ENTITY_RETIRE_EVENT = "entity_retire"
ENTITY_SUMMARY_EVENT = "entity_summary"

#: Every ``system`` variant this module folds.
ENTITY_EVENT_KINDS = frozenset(
    {
        ENTITY_ESTABLISH_EVENT,
        ENTITY_SET_EVENT,
        ENTITY_UPDATE_EVENT,
        ENTITY_MOVE_EVENT,
        ENTITY_RETIRE_EVENT,
        ENTITY_SUMMARY_EVENT,
    }
)

#: The kinds an ``entity_establish`` payload may carry (§5 table + W9.32 §3.1).
#: ``meter`` is not here on purpose: a meter is minted by its first ``entity_set``
#: (§3.4).
ENTITY_OBJECT_KINDS = (
    "location",
    "thread",
    "relationship",
    "item",
    "condition",
    "body",
    "economy",
)

#: Every kind the registry stores, in the order §3 presents them (``meter`` is
#: minted by ``entity_set``, so it is not an ``entity_establish`` kind).
_ALL_KINDS = ENTITY_OBJECT_KINDS + ("meter",)

#: ``kind`` -> the :class:`Registry`/:class:`_Fold` attribute holding its objects.
#: ``economy`` is a SINGLETON field rather than an id map (one per session, W9.32
#: R7); :func:`_objects` presents it as a one-entry map so every per-kind reader
#: stays one dispatch.
_KIND_FIELD = {
    "location": "locations",
    "thread": "threads",
    "relationship": "relationships",
    "meter": "meters",
    "item": "items",
    "condition": "conditions",
    "body": "bodies",
    "economy": "economy",
}

#: The legacy world-state lane this module folds beneath the registry (§11.1).
#: A literal like :mod:`fabulist.state.presence` keeps (the typed variant lives
#: in ``schema_gate``, which the state layer does not import).
_WORLD_STATE_EVENT = "world_state"

#: Default event actor: BTS is the writer of record (req. 12).
DEFAULT_ACTOR = "bts"

# -- reserved literals (plan §5) ---------------------------------------------

#: The player's being key (a cast member id would be the alternative).
PLAYER = "player"
#: The scene as a meter host (scene meters, scene traits — §3.4/§4).
SCENE = "scene"
#: The absence destination: ``assignment[who] = OFF_SCREEN`` (§3.6).
OFF_SCREEN = "off_screen"
#: The off-screen BUCKET's reserved location id — always live, never retired.
OFF_SCREEN_LOCATION_ID = "loc_off_screen"
#: Render name of the bucket (the palette line reads ``loc_off_screen off-screen``).
OFF_SCREEN_NAME = "off-screen"

#: Never minted (§5). These identities already exist, engine-owned.
RESERVED_LITERALS = frozenset({OFF_SCREEN_LOCATION_ID, OFF_SCREEN, PLAYER, SCENE})

#: Comparison forms of the reserved literals plus both off-screen spellings.
_OFF_SCREEN_KEYS = frozenset(
    {
        canonical_key(OFF_SCREEN),
        canonical_key(OFF_SCREEN_LOCATION_ID),
        canonical_key(OFF_SCREEN_NAME),
    }
)
_RESERVED_KEYS = frozenset(canonical_key(literal) for literal in RESERVED_LITERALS)

# -- thread statuses (plan §3.2, today's ``bts._THREAD_STATUS`` vocabulary) ---

THREAD_STATUS_NEW = "new"
THREAD_STATUS_ACTIVE = "active"
THREAD_STATUS_PAUSED = "paused"
THREAD_STATUS_RESOLVED = "resolved"
THREAD_STATUSES = frozenset(
    {THREAD_STATUS_NEW, THREAD_STATUS_ACTIVE, THREAD_STATUS_PAUSED, THREAD_STATUS_RESOLVED}
)

# -- condition vocabulary (W9.32 §3.1, O2) -----------------------------------

#: The fixed severity enum (owner ruling O2): enum-refusable at the door, one word
#: in the palette. Free text was the alternative and would not render.
CONDITION_SEVERITIES = frozenset({"minor", "moderate", "major"})
#: First-sight severity — a condition with no word about its weight is moderate.
CONDITION_SEVERITY_DEFAULT = "moderate"

#: The instant vocabulary these objects share with the clock: ``YYYY-MM-DD HH:MM``.
#: The state layer keeps its own parse because it must not import the graph layer,
#: and because comparisons here are plain string order: that format sorts
#: lexicographically in the same order it sorts chronologically, which is exactly
#: what the expiry comparison needs.
INSTANT_FORMAT = "%Y-%m-%d %H:%M"

#: Fields ``entity_update`` may patch, per object kind (§5 table + W9.32 §3.1).
#: ``name``, ``host``, ``key`` and ``denomination`` are NOT patchable anywhere: they
#: are identity (or, for a condition's key, the word the identity is built from), and
#: an identity that can be edited is not one.
_PATCH_FIELDS: dict[str, frozenset[str]] = {
    "location": frozenset({"name", "note", "parent_id"}),
    "thread": frozenset({"name", "note", "status", "detail"}),
    "relationship": frozenset({"name", "note"}),
    "item": frozenset({"name", "note", "holder"}),
    "meter": frozenset(),
    "condition": frozenset({"severity", "source", "expires_at", "note"}),
    "body": frozenset({"pose", "clothing", "surface", "note"}),
    "economy": frozenset({"balance", "note"}),
}

#: ``kind`` -> id prefix (§5 minting).
_ID_PREFIX = {
    "location": "loc_",
    "thread": "th_",
    "relationship": "rel_",
    "meter": "mt_",
    "item": "itm_",
    "condition": "cd_",
    "body": "bd_",
    "economy": "eco_",
}


class EntityError(ValueError):
    """A rejected entity action, with a readable (corrective) message.

    The write helpers raise this BEFORE anything is appended, and the tool door
    (:mod:`fabulist.state.entity_tools`, W2) hands ``str(exc)`` back to the
    model — one validation path for the tools and the sidebar's ✎ door (§8).
    """


# -- id minting (the cast pattern, plan §5 / §14.3) --------------------------


def _digest(basis: str) -> str:
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _guard_identity(kind: str, identity: str) -> str:
    """Refuse a reserved literal, then canonicalise an identity basis.

    ``loc_off_screen``/``player``/``scene`` already exist as engine-owned
    identities, so establishing a thing by one of those names is an error, not
    a second object (§5). The guard runs on the CANONICAL form, so
    ``off-screen``/``Off_Screen`` hit it too.
    """
    text = _clean(identity)
    if not text:
        raise EntityError(f"a {kind} needs a name")
    if canonical_key(text) in _RESERVED_KEYS:
        reserved = ", ".join(sorted(RESERVED_LITERALS))
        raise EntityError(
            f"{text!r} is a reserved literal ({reserved}) — it already exists; "
            "reference it by that name instead of establishing it"
        )
    return canonical_key(text)


def _mint(kind: str, identity: str, existing_ids: Iterable[str]) -> str:
    """``prefix + sha1(kind|identity|index)[:12]``, counter suffix on collision.

    Deterministic given the same log (a replay folds to the same handles),
    and ``existing_ids`` must be the LIVE ids so a retired identity
    re-mints as itself — the D10 resurrection (§5/§3).
    """
    taken = set(existing_ids)
    base = f"{kind}|{identity}"
    index = 0
    while True:
        candidate = _ID_PREFIX[kind] + _digest(f"{base}|{index}")
        if candidate not in taken:
            return candidate
        index += 1


def mint_location_id(name: object, existing_ids: Iterable[str] = ()) -> str:
    """Mint a ``loc_`` id for a room/area name (§3.1)."""
    return _mint("location", _guard_identity("location", str(name or "")), existing_ids)


def mint_thread_id(name: object, existing_ids: Iterable[str] = ()) -> str:
    """Mint a ``th_`` id for a thread name (§3.2)."""
    return _mint("thread", _guard_identity("thread", str(name or "")), existing_ids)


def mint_item_id(name: object, existing_ids: Iterable[str] = ()) -> str:
    """Mint an ``itm_`` id for an item name (§3.5)."""
    return _mint("item", _guard_identity("item", str(name or "")), existing_ids)


def mint_relationship_id(
    source: object, target: object, existing_ids: Iterable[str] = ()
) -> str:
    """Mint a ``rel_`` id for the ``(from, to)`` pair (§3.3).

    Identity is the PAIR, not the note: a second edge for the same pair is a
    legal duplicate (req. 4) and gets the counter suffix.
    """
    origin = _clean(source)
    destination = _clean(target)
    if not origin or not destination:
        raise EntityError("a relationship needs both ends (from and to)")
    identity = f"{canonical_key(origin)}->{canonical_key(destination)}"
    return _mint("relationship", identity, existing_ids)


def mint_meter_id(
    host: object, subject: object, key: object, existing_ids: Iterable[str] = ()
) -> str:
    """Mint an ``mt_`` id for a meter/trait, identity ``(host, subject, key)``.

    Stable forever after the first set (§3.4). ``subject`` is ``None`` for a
    self/ambient row and required for a relational one; the catalog (§4, W2)
    owns that distinction, so both spellings mint here.
    """
    owner = _clean(host)
    attribute = _clean(key)
    if not owner or not attribute:
        raise EntityError("a meter needs a host and a key")
    toward = _clean(subject)
    identity = "|".join(
        (canonical_key(owner), canonical_key(toward) if toward else "", canonical_key(attribute))
    )
    return _mint("meter", identity, existing_ids)


def mint_condition_id(
    host: object, key: object, existing_ids: Iterable[str] = ()
) -> str:
    """Mint a ``cd_`` id for a condition, identity ``(host, key)`` (W9.32 §3.1).

    NOT routed through :func:`_guard_identity`: a condition's host may be the
    reserved ``scene`` (a weather condition), so the reserved-literal refusal does
    not apply to this kind's identity.

    ``existing_ids`` is the whole establish HISTORY for the identity, not the live
    set — a condition is one OCCURRENCE, so re-establishing ``(host, key)`` after a
    retire gets the next counter index rather than the tombstoned id (see the
    module docstring's occurrence note).
    """
    owner = _clean(host)
    attribute = _clean(key)
    if not owner or not attribute:
        raise EntityError("a condition needs a host and a key")
    identity = f"{canonical_key(owner)}|{canonical_key(attribute)}"
    return _mint("condition", identity, existing_ids)


def mint_body_id(host: object, existing_ids: Iterable[str] = ()) -> str:
    """Mint a ``bd_`` id for a body; identity is the HOST (one body per being)."""
    owner = _clean(host)
    if not owner:
        raise EntityError("a body needs a host (the being it belongs to)")
    return _mint("body", canonical_key(owner), existing_ids)


def mint_economy_id(denomination: object, existing_ids: Iterable[str] = ()) -> str:
    """Mint an ``eco_`` id for the session's purse; identity is the denomination."""
    unit = _clean(denomination)
    if not unit:
        raise EntityError("an economy needs a denomination")
    return _mint("economy", canonical_key(unit), existing_ids)


# -- the objects (plan §3) ---------------------------------------------------


@dataclass(frozen=True)
class Location:
    """A room/area (§3.1). ``parent_id`` is the optional area chain."""

    kind: ClassVar[str] = "location"

    id: str
    name: str
    parent_id: str | None = None
    note: str = ""
    established_seq: int = 0
    #: Set ONLY on :data:`OFF_SCREEN_LOCATION_ID` — the optional loc the
    #: off-screen crowd is at while the player is elsewhere (§3.1/req. 2).
    off_screen_ref: str | None = None


@dataclass(frozen=True)
class Thread:
    """A plot thread (§3.2)."""

    kind: ClassVar[str] = "thread"

    id: str
    name: str
    status: str = THREAD_STATUS_ACTIVE
    detail: str = ""
    note: str = ""
    established_seq: int = 0


@dataclass(frozen=True)
class Relationship:
    """The prose edge between two beings (§3.3).

    ``source``/``target`` are the payload's ``from``/``to`` (``from`` is a
    Python keyword). Numbers never live here — meters address the pair (§3.4).
    """

    kind: ClassVar[str] = "relationship"

    id: str
    source: str
    target: str
    name: str = ""
    note: str = ""
    established_seq: int = 0


@dataclass(frozen=True)
class Meter:
    """One metered attribute or free-text trait with identity (§3.4).

    ``host`` is whose state it is (``member_id`` | ``player`` | ``scene``),
    ``subject`` who it is toward (``None`` for a self/ambient row), ``key`` the
    catalog row. ``value`` keeps the model's type: a number is a meter, text is
    a trait (the catalog owns the authority, §4).
    """

    kind: ClassVar[str] = "meter"

    id: str
    host: str
    key: str
    value: int | float | str
    subject: str | None = None
    established_seq: int = 0


@dataclass(frozen=True)
class Item:
    """An inventory item; ``holder`` is the being (§3.5)."""

    kind: ClassVar[str] = "item"

    id: str
    name: str
    holder: str = ""
    note: str = ""
    established_seq: int = 0


@dataclass(frozen=True)
class Condition:
    """One state with a clock expiry, identity ``(host, key)`` (W9.32 §3.1).

    ``host`` is whose state it is (a ``member_id`` | ``player`` | the reserved
    ``scene`` — a weather condition hangs on the scene); ``key`` is the word the
    tracker uses (``suspicious``, ``bleeding``). ``expires_at`` is an
    ``YYYY-MM-DD HH:MM`` instant, or ``""`` for a condition nothing retires but an
    explicit door. ``retired_at`` is filled by a reader that reconstructs a
    tombstoned condition from the log — the fold itself DROPS retired objects (§5),
    so a live record always carries ``""``.
    """

    kind: ClassVar[str] = "condition"

    id: str
    host: str
    key: str
    severity: str = CONDITION_SEVERITY_DEFAULT
    source: str = ""
    expires_at: str = ""
    note: str = ""
    established_seq: int = 0
    retired_at: str = ""


@dataclass(frozen=True)
class Body:
    """One being's body state — ONE per being, keyed by host (W9.32 §3.1, R6).

    ``pose`` is free text, ``clothing`` is the ordered layer list OUTER→INNER (a
    tuple so the frozen record stays hashable; the event payload carries a list),
    and ``surface`` is what is visibly wrong (dirt/blood/injury). Three simultaneous
    independent fields with one structured edit each is the whole reason this is an
    object and not three catalog trait rows (§3.1's R6 justification).
    """

    kind: ClassVar[str] = "body"

    id: str
    host: str
    pose: str = ""
    clothing: tuple[str, ...] = ()
    surface: str = ""
    note: str = ""
    established_seq: int = 0
    retired_at: str = ""


@dataclass(frozen=True)
class Economy:
    """The session's purse — ONE per session (W9.32 §3.1, R7).

    ``balance`` is an int so arithmetic never meets a string; the wire patch stays a
    string (§3.1) because every other patch field is one. Its identity is the
    session, so a different currency means retire-then-establish (the singleton door
    in :func:`establish_economy` refuses a second live purse).
    """

    kind: ClassVar[str] = "economy"

    id: str
    denomination: str
    balance: int = 0
    note: str = ""
    established_seq: int = 0
    retired_at: str = ""


@dataclass(frozen=True)
class LegacyWorld:
    """The folded legacy ``world_state`` lane, rendered name-only (§11.1).

    ``state`` is ``{name: {category: {key: value}}}``, ``venue`` maps name ->
    ``{venue, area}``, and ``flat`` is ``bts.fold_flat``'s
    ``loc``/``weather``/``tone``/``inv`` derivation over them (``forecast`` is
    never part of it). Not a second registry: a forward-only shim (req. 15).
    """

    state: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)
    venue: dict[str, dict[str, str]] = field(default_factory=dict)
    off_screen: list[dict[str, str]] = field(default_factory=list)
    threads: list[dict[str, str]] = field(default_factory=list)
    tension: dict[str, Any] = field(default_factory=dict)
    flat: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Registry:
    """The folded registry for one session (plan §5 projection).

    Every mapping is ``id -> object`` in establish order (seq order is the
    palette's stable order, §7 decision point §14.5). ``locations`` ALWAYS holds
    :data:`OFF_SCREEN_LOCATION_ID` (req. 2). ``conditions``/``bodies`` are id maps;
    ``economy`` is the session's one purse or ``None`` (W9.32 §3.1). ``assignments``
    maps a being key to a location id or :data:`OFF_SCREEN` (§3.6); ``legacy`` is
    the name-only shim beneath it (§11.1). Read it with :func:`location_path`/
    :func:`occupants`/:func:`same_room`/:func:`being_render`/:func:`flat_render`/
    :func:`rendered_state`/:func:`conditions_for`/:func:`body_for`/
    :func:`economy_of` — the fields are frozen but the mappings are the fold's own
    dicts, so treat them as read-only.
    """

    locations: dict[str, Location] = field(default_factory=dict)
    threads: dict[str, Thread] = field(default_factory=dict)
    relationships: dict[str, Relationship] = field(default_factory=dict)
    meters: dict[str, Meter] = field(default_factory=dict)
    items: dict[str, Item] = field(default_factory=dict)
    conditions: dict[str, Condition] = field(default_factory=dict)
    bodies: dict[str, Body] = field(default_factory=dict)
    economy: Economy | None = None
    assignments: dict[str, str] = field(default_factory=dict)
    off_screen_ref: str | None = None
    legacy: LegacyWorld = field(default_factory=LegacyWorld)


# -- small helpers -----------------------------------------------------------


def _clean(value: object) -> str:
    """Tolerant text coercion (``None`` -> ``""``, everything else stripped)."""
    return str(value).strip() if value is not None else ""


def _required_text(value: object, what: str) -> str:
    text = _clean(value)
    if not text:
        raise EntityError(f"{what} is required")
    return text


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _chain_events(log: "EventLog", session_id: str) -> list[Any]:
    """Fork-aware chain, preferring the public walk (no private re-walk)."""
    get_chain = getattr(log, "get_chain_events", None)
    if callable(get_chain):
        return list(get_chain(session_id))
    return list(log.get_events(session_id))


def _objects(holder: Any, kind: str) -> dict[str, Any]:
    """The ``id -> object`` mapping for one kind on the fold OR the registry.

    Both carry the same per-kind field names (``locations``/``threads``/...), so
    one dispatch serves the fold and every writer's live-id lookup. ``economy`` is
    a SINGLETON field, presented here as a one-entry map so every per-kind reader
    stays one call — the mapping is a TEMPORARY view: never mutate it, write through
    :func:`_put_object`/:func:`_drop_object`.
    """
    stored = getattr(holder, _KIND_FIELD[kind])
    if kind == "economy":
        return {} if stored is None else {stored.id: stored}
    return stored


def _put_object(holder: Any, kind: str, record: Any) -> None:
    """Store one folded object for its kind (the singleton economy takes the field)."""
    if kind == "economy":
        holder.economy = record
        return
    _objects(holder, kind)[record.id] = record


def _drop_object(holder: Any, kind: str, object_id: str) -> None:
    """Tombstone one object for its kind (a retired economy frees the session's purse)."""
    if kind == "economy":
        if holder.economy is not None and holder.economy.id == object_id:
            holder.economy = None
        return
    _objects(holder, kind).pop(object_id, None)


# -- the fold ----------------------------------------------------------------


@dataclass
class _Fold:
    """Mutable fold state; :func:`registry` freezes it into a :class:`Registry`."""

    locations: dict[str, Location] = field(default_factory=dict)
    threads: dict[str, Thread] = field(default_factory=dict)
    relationships: dict[str, Relationship] = field(default_factory=dict)
    meters: dict[str, Meter] = field(default_factory=dict)
    items: dict[str, Item] = field(default_factory=dict)
    conditions: dict[str, Condition] = field(default_factory=dict)
    bodies: dict[str, Body] = field(default_factory=dict)
    economy: Economy | None = None
    assignments: dict[str, str] = field(default_factory=dict)
    off_screen_ref: str | None = None
    #: id -> the seq the thing FIRST entered. Survives its tombstone so a
    #: resurrected object keeps its original ``established_seq`` (D10).
    established: dict[str, int] = field(default_factory=dict)


def _thread_status(value: object) -> str:
    status = _clean(value).lower()
    return status if status in THREAD_STATUSES else THREAD_STATUS_ACTIVE


def _condition_severity(value: object) -> str:
    """The fold's tolerant severity read: an unknown word folds as the default."""
    severity = _clean(value).lower()
    return severity if severity in CONDITION_SEVERITIES else CONDITION_SEVERITY_DEFAULT


def _stated_severity(value: object) -> str:
    """The WRITER's severity read: ``""`` means unspecified, an unknown word is refused.

    ``""`` rather than the default on purpose — the default belongs to FIRST SIGHT
    (:func:`set_condition`'s establish path). An update that never mentioned a
    severity must not silently restate one, or a ``major`` wound would be downgraded
    to ``moderate`` by a call that only amended its source.
    """
    severity = _clean(value).lower()
    if not severity:
        return ""
    if severity not in CONDITION_SEVERITIES:
        allowed = ", ".join(sorted(CONDITION_SEVERITIES))
        raise EntityError(
            f"{severity!r} is not a condition severity — use one of {allowed}"
        )
    return severity


def _instant(value: object, what: str) -> str:
    """Normalise a clock instant (``YYYY-MM-DD HH:MM``); ``""`` stays ``""`` = never.

    Validated and re-padded here so string order is chronological order (the expiry
    comparison is plain ``<=``): a ``2025-6-3 7:5`` from a model must compare as
    ``2025-06-03 07:05``, not as text. The state layer keeps its own parse instead of
    importing :mod:`fabulist.graph.clock` — this module must not import the graph
    layer (its docstring's hygiene rule).
    """
    text = _clean(value)
    if not text:
        return ""
    try:
        return datetime.strptime(text, INSTANT_FORMAT).strftime(INSTANT_FORMAT)
    except ValueError:
        raise EntityError(
            f"{what} must be an instant like \"2025-06-03 21:30\" — got {text!r}"
        ) from None


def _instant_key(value: object) -> str:
    """A comparable instant for the FOLD: normalised when it parses, else the raw text.

    The fold is dumb (§5) — it stores whatever the log holds — so an unparseable
    instant folds as written instead of refusing a whole session's replay.
    """
    try:
        return _instant(value, "an instant")
    except EntityError:
        return _clean(value)


def _clothing(value: object) -> tuple[str, ...]:
    """Clothing layers OUTER→INNER from a sequence or one comma-joined line.

    Stored as a LIST on the wire (§3.1: a layer set is one ordered structured edit)
    and folded to a tuple so the frozen :class:`Body` stays hashable. A single string
    is tolerated because the door and the ✎ field both surface one line.
    """
    if value is None:
        return ()
    if isinstance(value, str):
        raw: Iterable[object] = value.replace("\n", ",").split(",")
    elif isinstance(value, (list, tuple)):
        raw = value
    else:
        raw = (value,)
    return tuple(text for text in (_clean(layer) for layer in raw) if text)


def _balance(value: object) -> int | None:
    """A whole number from an int or its wire string; ``None`` when it is neither."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(_clean(value))
    except ValueError:
        return None


def _attribute_value(value: object) -> int | float | str | None:
    """The value as stored: number or text, model type preserved (§3.4)."""
    if isinstance(value, bool) or value is None:
        return None
    if _is_number(value):
        return value
    text = _clean(value)
    return text or None


def _new_object(kind: str, payload: dict[str, Any], seq: int) -> Any:
    object_id = _clean(payload.get("id"))
    name = _clean(payload.get("name"))
    note = _clean(payload.get("note"))
    if kind == "location":
        return Location(
            id=object_id,
            name=name,
            parent_id=_clean(payload.get("parent_id")) or None,
            note=note,
            established_seq=seq,
        )
    if kind == "thread":
        return Thread(
            id=object_id,
            name=name,
            status=_thread_status(payload.get("status")),
            detail=_clean(payload.get("detail")),
            note=note,
            established_seq=seq,
        )
    if kind == "relationship":
        source = _clean(payload.get("from"))
        target = _clean(payload.get("to"))
        return Relationship(
            id=object_id,
            source=source,
            target=target,
            name=name or f"{source}→{target}",
            note=note,
            established_seq=seq,
        )
    if kind == "condition":
        return Condition(
            id=object_id,
            host=_clean(payload.get("host")),
            # The establish's ``name`` is the condition key when the payload omits
            # ``key`` — ``name`` is required on every kind (§3.1 review P0).
            key=_clean(payload.get("key")) or name,
            severity=_condition_severity(payload.get("severity")),
            source=_clean(payload.get("source")),
            expires_at=_instant_key(payload.get("expires_at")),
            note=note,
            established_seq=seq,
        )
    if kind == "body":
        return Body(
            id=object_id,
            host=_clean(payload.get("host")),
            pose=_clean(payload.get("pose")),
            clothing=_clothing(payload.get("clothing")),
            surface=_clean(payload.get("surface")),
            note=note,
            established_seq=seq,
        )
    if kind == "economy":
        opening = _balance(payload.get("balance"))
        return Economy(
            id=object_id,
            denomination=_clean(payload.get("denomination")) or name,
            balance=opening if opening is not None else 0,
            note=note,
            established_seq=seq,
        )
    return Item(
        id=object_id,
        name=name,
        holder=_clean(payload.get("holder")),
        note=note,
        established_seq=seq,
    )


def _fold_establish(fold: _Fold, payload: dict[str, Any], seq: int) -> None:
    kind = _clean(payload.get("kind"))
    object_id = _clean(payload.get("id"))
    if kind not in ENTITY_OBJECT_KINDS or not object_id:
        return
    if not _clean(payload.get("name")):
        return
    first = fold.established.setdefault(object_id, seq)
    record = replace(_new_object(kind, payload, first), established_seq=first)
    _put_object(fold, kind, record)  # a re-establish resurrects (D10)


def _fold_set(fold: _Fold, payload: dict[str, Any], seq: int) -> None:
    object_id = _clean(payload.get("id"))
    host = _clean(payload.get("host"))
    key = _clean(payload.get("key"))
    value = _attribute_value(payload.get("value"))
    if not object_id or not host or not key or value is None:
        return
    first = fold.established.setdefault(object_id, seq)
    fold.meters[object_id] = Meter(
        id=object_id,
        host=host,
        key=key,
        value=value,
        subject=_clean(payload.get("subject")) or None,
        established_seq=first,
    )


def _fold_update(fold: _Fold, payload: dict[str, Any]) -> None:
    object_id = _clean(payload.get("id"))
    patch = payload.get("patch")
    if not object_id or not isinstance(patch, dict):
        return
    for kind in _ALL_KINDS:
        existing = _objects(fold, kind).get(object_id)
        if existing is None:
            continue
        updates = _patch_updates(kind, patch)
        if updates:
            _put_object(fold, kind, replace(existing, **updates))
        return


def _patch_updates(kind: str, patch: dict[str, Any]) -> dict[str, Any]:
    """One object's allowed fields from an ``entity_update`` patch.

    Everything is str-patched (§5) with the two exceptions that are the reason these
    kinds are objects rather than catalog rows: ``clothing`` is an ordered LIST of
    layers (a layer set is one structured edit, R6), and ``economy.balance`` rides
    the wire as a string but folds to an int so arithmetic never meets ``"85"``.
    """
    allowed = _PATCH_FIELDS[kind]
    updates: dict[str, Any] = {}
    for field_name, value in patch.items():
        if field_name not in allowed:
            continue
        if field_name == "clothing":
            updates[field_name] = _clothing(value)
            continue
        if field_name == "balance":
            amount = _balance(value)
            if amount is not None:
                updates[field_name] = amount
            continue
        if not isinstance(value, str):
            continue
        text = value.strip()
        if field_name in {"name", "holder"} and not text:
            continue
        if field_name == "status":
            updates[field_name] = _thread_status(text)
        elif field_name == "severity":
            updates[field_name] = _condition_severity(text)
        else:
            updates[field_name] = text
    return updates


def _fold_move(fold: _Fold, payload: dict[str, Any]) -> None:
    who = _clean(payload.get("who"))
    target = _clean(payload.get("to"))
    if not who or not target:
        return
    bucket = canonical_key(target) in _OFF_SCREEN_KEYS
    fold.assignments[who] = OFF_SCREEN if bucket else target
    if bucket and "at" in payload:
        fold.off_screen_ref = _clean(payload.get("at")) or None


def _fold_retire(fold: _Fold, payload: dict[str, Any]) -> None:
    object_id = _clean(payload.get("id"))
    if not object_id or object_id == OFF_SCREEN_LOCATION_ID:
        return  # the off-screen bucket is never retired (§3.1)
    for kind in _ALL_KINDS:
        _drop_object(fold, kind, object_id)


def _fold_events(events: Sequence[Any]) -> _Fold:
    """Seq-ordered latest-wins fold, one ``system`` payload family (§5)."""
    fold = _Fold()
    for event in events:
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        kind = payload.get("event")
        if kind == ENTITY_ESTABLISH_EVENT:
            _fold_establish(fold, payload, event.seq)
        elif kind == ENTITY_SET_EVENT:
            _fold_set(fold, payload, event.seq)
        elif kind == ENTITY_UPDATE_EVENT:
            _fold_update(fold, payload)
        elif kind == ENTITY_MOVE_EVENT:
            _fold_move(fold, payload)
        elif kind == ENTITY_RETIRE_EVENT:
            _fold_retire(fold, payload)
        # entity_summary is observability only (counts; joins agent_loop traces).
    return fold


# -- the legacy shim (§11.1) -------------------------------------------------


def _merge_by_name(prior: list[dict[str, str]], incoming: list[dict[str, str]]) -> list[dict[str, str]]:
    """Merge a named legacy lane (``threads``/``off_screen``) latest-wins by name."""
    merged = [dict(entry) for entry in prior if isinstance(entry, dict)]
    for entry in incoming:
        row = dict(entry)
        for index, prior_row in enumerate(merged):
            if prior_row.get("name") == row.get("name"):
                merged[index] = row
                break
        else:
            merged.append(row)
    return merged


def _legacy_world(events: Sequence[Any]) -> LegacyWorld:
    """Fold the legacy ``world_state`` lane beneath the registry (§11.1).

    Same accumulation as ``api/state_api._fold_world_state`` (D45) — checkpoint
    replaces, a delta deep-merges ``state``, ``venue``/``tension`` latest-wins,
    ``threads``/``off_screen`` by ``name`` — but built from
    :func:`fabulist.state.bts.parse_bts_structured` so the payload
    normalisation is the BTS module's, not a copy. The API layer's user-gated
    cast filter is NOT applied here: this is the tracker's own view, and hiding
    an unaccepted candidate is a user-facing read decision (D38 vocabulary),
    not a registry fact.
    """
    state: dict[str, dict[str, dict[str, str]]] = {}
    venue: dict[str, dict[str, str]] = {}
    off_screen: list[dict[str, str]] = []
    threads: list[dict[str, str]] = []
    tension: dict[str, Any] = {}
    for event in events:
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        if payload.get("event") != _WORLD_STATE_EVENT:
            continue
        try:
            parsed = parse_bts_structured(payload)
        except BtsParseError:
            continue
        if parsed.kind == "checkpoint":
            state = copy.deepcopy(parsed.state)
            venue = copy.deepcopy(parsed.venue)
            off_screen = copy.deepcopy(parsed.off_screen)
            threads = copy.deepcopy(parsed.threads)
            tension = copy.deepcopy(parsed.tension)
            continue
        for character, categories in parsed.state.items():
            bucket = state.setdefault(character, {})
            for category, attrs in categories.items():
                bucket.setdefault(category, {}).update(attrs)
        venue.update(copy.deepcopy(parsed.venue))
        tension.update(copy.deepcopy(parsed.tension))
        off_screen = _merge_by_name(off_screen, copy.deepcopy(parsed.off_screen))
        threads = _merge_by_name(threads, copy.deepcopy(parsed.threads))
    return LegacyWorld(
        state=state,
        venue=venue,
        off_screen=off_screen,
        threads=threads,
        tension=tension,
        flat=fold_flat(state, "", venue),
    )


# -- the projection ----------------------------------------------------------


def registry(log: "EventLog", session_id: str) -> Registry:
    """Fold the entity lane (fork-aware) and merge the legacy shim beneath it.

    Registry-first everywhere: an assignment the registry holds wins for that
    being, a meter wins per ``key``, and the legacy ``world_state`` fold fills
    only the gaps — name-only, no ids (§11.1). ``loc_off_screen`` is always
    present and always live (req. 2).
    """
    events = _chain_events(log, session_id)
    fold = _fold_events(events)
    locations = dict(fold.locations)
    bucket = locations.get(OFF_SCREEN_LOCATION_ID)
    if bucket is None:
        bucket = Location(id=OFF_SCREEN_LOCATION_ID, name=OFF_SCREEN_NAME)
    locations[OFF_SCREEN_LOCATION_ID] = replace(
        bucket,
        name=bucket.name or OFF_SCREEN_NAME,
        off_screen_ref=fold.off_screen_ref,
    )
    return Registry(
        locations=locations,
        threads=dict(fold.threads),
        relationships=dict(fold.relationships),
        meters=dict(fold.meters),
        items=dict(fold.items),
        conditions=dict(fold.conditions),
        bodies=dict(fold.bodies),
        economy=fold.economy,
        assignments=dict(fold.assignments),
        off_screen_ref=fold.off_screen_ref,
        legacy=_legacy_world(events),
    )


# -- read helpers (plan §5) --------------------------------------------------


def _bucket_key(value: object) -> str:
    """Normalise every off-screen spelling to :data:`OFF_SCREEN`."""
    text = _clean(value)
    return OFF_SCREEN if canonical_key(text) in _OFF_SCREEN_KEYS else text


def resolve_location(reg: Registry, ref: object, *, allow_off_screen: bool = True) -> str:
    """Resolve a location id-or-name to a stored `entity_move` target (§6).

    An ambiguous name is an ERROR listing both ids (no silent pick — req. 4);
    the off-screen bucket is spelled ``off_screen`` in an assignment (§3.6) and
    is only reachable as a DESTINATION (``allow_off_screen=False`` for a
    parent, where the absence bucket makes no sense).
    """
    text = _clean(ref)
    if not text:
        raise EntityError("a location reference is required")
    if canonical_key(text) in _OFF_SCREEN_KEYS:
        if not allow_off_screen:
            raise EntityError(
                f"{OFF_SCREEN_LOCATION_ID} is the absence bucket — it cannot be a location's parent"
            )
        return OFF_SCREEN
    if text in reg.locations:
        return text
    wanted = canonical_key(text)
    matches = [
        loc_id for loc_id, location in reg.locations.items() if canonical_key(location.name) == wanted
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        listing = ", ".join(matches)
        raise EntityError(f"{text!r} matches {len(matches)} locations ({listing}) — pass the id")
    known = ", ".join(
        f"{loc_id} ({location.name})" for loc_id, location in reg.locations.items()
    ) or "(none yet)"
    raise EntityError(
        f"no object {text!r} — registry locations: {known}; call establish_location to add one"
    )


def location_path(reg: Registry, loc_id: object) -> str:
    """The canonical path of a location: ``"upstairs hall > bathroom"`` (§8).

    Walks the ``parent_id`` chain root-first; an unknown id (or a cycle, which
    the dumb store allows) renders the chain it could walk and stops. The
    off-screen bucket has no parent, so it renders as its own name.
    """
    names: list[str] = []
    seen: set[str] = set()
    current = _clean(loc_id)
    while current and current not in seen:
        seen.add(current)
        location = reg.locations.get(current)
        if location is None:
            break
        names.append(location.name)
        current = _clean(location.parent_id)
    return " > ".join(reversed(names))


def occupants(reg: Registry, loc_id: object) -> tuple[str, ...]:
    """Beings assigned to a location, in assignment order (§5).

    ``loc_id`` is an id or an off-screen spelling; an unknown location (and a
    being with no assignment at all) is empty — presence for those falls back
    to the legacy walk (§8), which is not this fold's question.
    """
    target = _bucket_key(loc_id)
    if not target:
        return ()
    return tuple(who for who, where in reg.assignments.items() if where == target)


def same_room(reg: Registry, member_id: object, player_key: str = PLAYER) -> bool:
    """In-scene by OBJECT EQUALITY: ``assignment[who] == assignment[player]``.

    Req. 13's presence answer: any other location — the off-screen bucket
    included — is a deterministic presence exit. A being the registry has no
    assignment for is not answerable here (``False``); presence keeps the
    legacy walk for those (§8, forward-only).
    """
    where = reg.assignments.get(_clean(member_id))
    player_where = reg.assignments.get(_clean(player_key))
    if where is None or player_where is None:
        return False
    return where == player_where


def scene_traits(reg: Registry) -> dict[str, str]:
    """Scene-hosted TEXT traits by key (``weather``/``tone``/``forecast``, §4).

    Numbers are meters and never appear here, and this is the ONLY public read
    of a ``forecast`` trait: :func:`flat_render` withholds it on purpose
    (review R2 — see that function).
    """
    traits: dict[str, str] = {}
    for meter in reg.meters.values():
        if meter.host != SCENE or _is_number(meter.value):
            continue
        traits[meter.key] = str(meter.value)
    return traits


def conditions_for(reg: Registry, host: object) -> tuple[Condition, ...]:
    """Live conditions on one host (a being key or ``scene``), in establish order.

    Host matching goes through :func:`~fabulist.state.cast.canonical_key`, so the
    surface the tracker cites (``Yara``) and the roster's ``cm_`` key answer the same
    question instead of splitting one person's state in two (the D38 lesson).
    """
    owner = canonical_key(host)
    if not owner:
        return ()
    return tuple(
        condition
        for condition in reg.conditions.values()
        if canonical_key(condition.host) == owner
    )


def active_conditions(reg: Registry, now: object) -> tuple[Condition, ...]:
    """Every live condition the clock has NOT passed yet (§3.6's ``conds`` line).

    A permanent condition (``expires_at == ""``) is always active. ``now`` is the
    folded clock instant; an empty ``now`` (no clock yet) counts everything as
    active rather than hiding a condition the scene really carries.
    """
    moment = _instant_key(now)
    return tuple(
        condition
        for condition in reg.conditions.values()
        if not condition.expires_at or condition.expires_at > moment
    )


def expired_conditions(reg: Registry, now: object) -> tuple[Condition, ...]:
    """The live conditions whose expiry the clock has reached but no sweep retired.

    This is the same predicate :func:`expire_conditions` appends tombstones for, and
    the palette's "never render an expired condition" filter (§3.6) reads it — one
    definition of expired for the writer and every reader.
    """
    moment = _instant_key(now)
    if not moment:
        return ()
    return tuple(
        condition
        for condition in reg.conditions.values()
        if condition.expires_at and condition.expires_at <= moment
    )


def body_for(reg: Registry, host: object) -> Body | None:
    """The being's one body, or ``None`` before its first write (one per being)."""
    owner = canonical_key(host)
    if not owner:
        return None
    for body in reg.bodies.values():
        if canonical_key(body.host) == owner:
            return body
    return None


def economy_of(reg: Registry) -> Economy | None:
    """The session's one purse, or ``None`` until the planner seeds it (§5)."""
    return reg.economy


def _legacy_state(reg: Registry, who: str) -> dict[str, dict[str, str]]:
    """Legacy per-being rows for ``who`` (exact key, else canonical match)."""
    rows = reg.legacy.state.get(who)
    if rows is not None:
        return rows
    wanted = canonical_key(who)
    merged: dict[str, dict[str, str]] = {}
    for name, categories in reg.legacy.state.items():
        if canonical_key(name) != wanted:
            continue
        for category, attrs in categories.items():
            merged.setdefault(category, {}).update(attrs)
    return merged


def _meter_line(meter: Meter) -> str:
    if meter.subject:
        return f"{meter.id} {meter.key} {meter.host}→{meter.subject} = {meter.value}"
    return f"{meter.id} {meter.key} {meter.host} = {meter.value}"


def being_render(reg: Registry, who: object) -> str:
    """One being's meters and traits, grouped, registry first then LEGACY (§11.1).

    Registry rows win per ``key`` (the plan's precedence rule); a legacy
    ``world_state`` fact with no registry counterpart renders **name-only**
    under ``legacy:`` until the model establishes a real object for it.
    """
    key = _clean(who)
    if not key:
        return ""
    meters = [meter for meter in reg.meters.values() if meter.host == key]
    numbers = [meter for meter in meters if _is_number(meter.value)]
    traits = [meter for meter in meters if not _is_number(meter.value)]
    lines: list[str] = []
    if numbers:
        lines.append("meters: " + " · ".join(_meter_line(meter) for meter in numbers))
    if traits:
        lines.append("traits: " + " · ".join(_meter_line(meter) for meter in traits))
    covered = {canonical_key(meter.key) for meter in meters}
    legacy_rows: list[str] = []
    for categories in _legacy_state(reg, key).values():
        for legacy_key, value in categories.items():
            if canonical_key(legacy_key) in covered:
                continue
            legacy_rows.append(f"{legacy_key}: {value}")
    if legacy_rows:
        lines.append("legacy: " + " · ".join(legacy_rows))
    return "\n".join(lines)


def flat_render(reg: Registry) -> dict[str, str]:
    """The four shadow keys DERIVED from the registry (§5, W9.32 §6).

    Exactly the four keys :func:`fabulist.state.bts.fold_flat` derives —
    ``loc`` (the PLAYER's canonical path via :func:`location_path`), ``weather``
    and ``tone`` (scene traits), ``inv`` (the player's item names) — so
    downstream consumers stay byte-compatible. Registry facts win per key and
    the legacy fold fills only what the registry does not hold (§11.1).
    ``time`` is still never emitted (the rules clock owns it, D3).

    W11: nothing WRITES these keys any more (the BTS delta stopped merging this
    render, so there is no shadow storage left to drift from the registry). The
    readers go through :func:`rendered_state`, which overlays this render on the
    projection fold; this function stays because that overlay and a legacy log's
    precedence both need it.

    ``forecast`` is DELIBERATELY omitted (review R2): in the flat fold that key
    is the D45 *manual* HUD pin, and a model-written trait must never clobber
    it. The scene-trait forecast stays reachable through :func:`scene_traits`
    for the env_hud node's own ladder rung (§8).
    """
    flat: dict[str, str] = {}
    where = reg.assignments.get(PLAYER)
    loc = location_path(reg, where) if where else ""
    loc = loc or reg.legacy.flat.get("loc", "")
    if loc:
        flat["loc"] = loc
    traits = scene_traits(reg)
    weather = traits.get("weather") or reg.legacy.flat.get("weather", "")
    if weather:
        flat["weather"] = weather
    tone = traits.get("tone") or reg.legacy.flat.get("tone", "")
    if tone:
        flat["tone"] = tone
    held = [item.name for item in reg.items.values() if item.holder == PLAYER and item.name]
    inventory = " ".join(held) or reg.legacy.flat.get("inv", "")
    if inventory:
        flat["inv"] = inventory
    return flat


# -- the flat read shim (W9.32 §6) -------------------------------------------

def rendered_state(
    log: "EventLog", session_id: str, extra_facts: dict[str, str] | None = None
) -> dict[str, str]:
    """The session's flat facts: the folded clock PLUS the derived registry view.

    This is the ONE read shim W9.32 §6 lands in place of the retired flat shadow
    writes: every consumer that used to read ``loc``/``weather``/``tone``/``inv``
    out of the projection ``state`` fold reads this instead, so the values can no
    longer disagree with the registry (there is no second copy to drift).

    Composition, in this order (later wins):

    1. the projection ``state`` fold — the clock keys ``time``/``date``/``day``,
       the D45 ``forecast`` PIN, and any legacy key a pre-W9.32 log carries;
    2. ``extra_facts`` — a caller's overlay of facts the fold has not committed
       yet (the turn nodes pass pre_turn's advanced clock, whose ``state-delta``
       lands only after the plan and the prose);
    3. :func:`flat_render` — ``loc``/``weather``/``tone``/``inv``, registry-first
       with the legacy fold beneath (the precedence §11.1 pins). The shadow keys
       the fold still holds from before W11 are therefore simply never read
       again;
    4. the DERIVED ``genre`` — the scene trait (R8's new home), falling back to
       the legacy flat ``genre`` key so a pre-W9.32 log renders as it always did;

    5. the derived ``conds`` line — the scene's ACTIVE conditions only, one
       clipped line (§3.6), absent when there are none.

    ``forecast`` is NOT derived (review R2/D45): the flat key is the owner's
    manual HUD pin, and the model-written scene trait must never overwrite it.
    The trait stays reachable through :func:`scene_traits` for the env_hud
    node's own ladder rung.

    Pure: it folds and renders, it never appends and never mints. Values are
    strings (the fold's wire shape), so a caller can render them straight into
    the facts line.
    """
    reg = registry(log, session_id)
    state: dict[str, str] = {}
    for key, value in (log.get_projection(session_id).get("state") or {}).items():
        # The fold's own values pass through UNTOUCHED, empties included: an
        # explicit ``forecast: ""`` is the D45 pin being cleared (absent != empty,
        # D33), so a shim that tidied values would change what the reader sees.
        state[str(key)] = "" if value is None else str(value)
    for key, value in (extra_facts or {}).items():
        # A caller's overlay is a facts line it has not committed yet: an empty
        # value there states nothing (the pre-W11 nodes skipped it too).
        text = _clean(value)
        if text:
            state[str(key)] = text
    state.update(flat_render(reg))
    genre = scene_traits(reg).get("genre") or state.get("genre", "")
    if genre:
        state["genre"] = genre
    # The ``conds`` key rides the facts line turn to turn (``advance`` passes
    # unknown facts through), so an old line must be cleared before the derived
    # one replaces it — otherwise the last condition would never leave the prompt.
    state.pop("conds", None)
    conditions = _conds_line(log, session_id, reg, state)
    if conditions:
        state["conds"] = conditions
    return state


#: The ``conds`` line's cap — one line in the facts block, whatever the scene
#: carries. Entries are dropped whole (never mid-word); a dropped one is marked
#: with an ellipsis, like the ``EnvTracker`` chip's own clipped slots.
CONDS_LINE_MAX = 160

#: The clock's date spellings, in preference order: the rules clock's render
#: (``fabulist.graph.clock.DATE_FORMAT`` — spelled here because this module must
#: not import the graph layer, its docstring's hygiene rule) and the ISO date a
#: clock proposal may write. Anything else reads as "no instant yet".
_CLOCK_DATE_FORMATS = ("%A, %B %d, %Y", "%Y-%m-%d")


def _clock_instant(state: dict[str, str]) -> str:
    """The fold's ``(date, time)`` keys as one comparable instant (``""`` if partial).

    A condition's ``expires_at`` is stored as :data:`INSTANT_FORMAT`
    (``YYYY-MM-DD HH:MM``), so the clock's human date render has to be
    NORMALISED before the two are compared: ``"2025-06-03 21:30" > "Monday,
    June 02, 2025 14:35"`` as text would call every expiry in the future. An
    unreadable or partial clock returns ``""``, which
    :func:`active_conditions` reads as "no clock yet" — every condition active
    rather than a scene that has silently lost one.
    """
    date_text = _clean(state.get("date"))
    time_text = _clean(state.get("time"))
    if not date_text or not time_text:
        return ""
    parsed = None
    for date_format in _CLOCK_DATE_FORMATS:
        try:
            parsed = datetime.strptime(date_text, date_format).date()
            break
        except ValueError:
            continue
    if parsed is None:
        return ""
    try:
        hours, minutes = (int(part) for part in time_text.split(":", 1))
    except (TypeError, ValueError):
        return ""
    if not (0 <= hours < 24 and 0 <= minutes < 60):
        return ""
    return f"{parsed.isoformat()} {hours:02d}:{minutes:02d}"


def _clip_line(text: str, limit: int) -> str:
    """One whitespace-collapsed line, word-boundary clipped with an ellipsis."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    head = collapsed[: max(0, limit - 1)].rstrip()
    cut = head.rsplit(" ", 1)[0].rstrip(" ,;:·") if " " in head else head
    return f"{cut or head}…"


def _condition_label(host: str, members: Sequence[Any]) -> str:
    """A condition host as the name a human reads (``Yara``, ``Player``, ``scene``).

    The writers store the being KEY (a ``cm_`` member id, ``player``, or
    ``scene``), and a raw id in the narrator's facts line is noise — so a roster
    member renders as its display name. The store also accepts a bare name as a
    host (it validates a being, it does not resolve one), which passes through
    unchanged.
    """
    text = _clean(host)
    if not text:
        return ""
    if text == PLAYER:
        return "Player"  # the reserved literal's display word (never a name)
    if canonical_key(text) == canonical_key(SCENE):
        return SCENE
    member = member_by_id(members, text)
    if member is not None and member.display_name:
        return member.display_name
    return text


def _condition_entry(condition: Condition, host: str) -> str:
    """``suspicious (Yara, until 21:30)`` — the key, its host, and its expiry.

    The expiry renders as its CLOCK TIME: the facts line carries ``date`` in its
    own key, so the calendar half of an instant would repeat it on every entry.
    """
    expiry = _clean(condition.expires_at)
    clock_time = expiry.split(" ", 1)[1] if " " in expiry else expiry
    details = ", ".join(part for part in (host, f"until {clock_time}" if clock_time else "") if part)
    return f"{condition.key} ({details})" if details else _clean(condition.key)


def _conds_rank(reg: Registry, condition: Condition) -> int:
    """Order key: what the scene carries, then who is in it, then everyone else."""
    if canonical_key(condition.host) == canonical_key(SCENE):
        return 0
    return 1 if same_room(reg, condition.host) else 2


def _conds_line(log: "EventLog", session_id: str, reg: Registry, state: dict[str, str]) -> str:
    """The derived ``conds`` fact: the ACTIVE conditions, as one clipped line.

    Active only — a condition the clock has passed is the sweep's business, and
    the narrator must never be told a healed wound still bleeds (§3.6). The
    scene's own conditions and the beings in the room come first, so a clipped
    line keeps what the scene is visibly doing; off-screen beings trail it.
    """
    conditions = list(active_conditions(reg, _clock_instant(state)))
    if not conditions:
        return ""
    members = roster(log, session_id)
    ordered = sorted(conditions, key=lambda condition: _conds_rank(reg, condition))
    entries: list[str] = []
    for condition in ordered:
        entry = _condition_entry(
            condition, _condition_label(condition.host, members)
        )
        if entries and len(" · ".join([*entries, entry])) > CONDS_LINE_MAX:
            return _clip_line(" · ".join([*entries, "…"]), CONDS_LINE_MAX)
        entries.append(entry)
    return _clip_line(" · ".join(entries), CONDS_LINE_MAX)


# -- writers (plan §5 / §6) --------------------------------------------------


def _append(
    log: "EventLog",
    session_id: str,
    payload: dict[str, Any],
    *,
    seq: int | None,
    actor: str,
) -> None:
    """Append one ``system`` variant, carrying the turn's narrative seq if known.

    The seq is optional transcript anchoring (§5) — the same shape
    ``validation_notice`` uses — so it rides the payload only when the engine
    knows which narrative the action belongs to.
    """
    if isinstance(seq, int) and not isinstance(seq, bool):
        payload["seq"] = seq
    log.append_event(session_id, "system", actor, payload)


def _live_ids(reg: Registry, kind: str) -> list[str]:
    """Live ids of one kind — the mint's collision set (tombstones excluded)."""
    return list(_objects(reg, kind))


#: A root name sharing this many leading characters (canonical) with a live root
#: is treated as the same place under another word ("dorm" vs "Dormitory").
_ROOT_NEAR_MATCH_MIN = 4


def _digits(text: str) -> str:
    return "".join(char for char in text if char.isdigit())


def _no_digits(text: str) -> str:
    return "".join(char for char in text if not char.isdigit())


def _require_not_descendant(reg: Registry, object_id: str, parent_id: str) -> None:
    """Refuse re-parenting a location under one of its own descendants."""
    seen: set[str] = set()
    current = parent_id
    while current and current not in seen:
        if current == object_id:
            raise EntityError(
                f"{parent_id} sits inside {object_id} — that would make a cycle; "
                "pick an ancestor or a different area"
            )
        seen.add(current)
        location = reg.locations.get(current)
        current = str(location.parent_id) if location and location.parent_id else ""
    return None


def _live_roots(reg: Registry) -> list[Any]:
    """The live locations that have no parent, in fold order (bucket excluded)."""
    return [
        location
        for loc_id, location in reg.locations.items()
        if loc_id != OFF_SCREEN_LOCATION_ID and not location.parent_id
    ]


def _require_distinct_place(reg: Registry, name: str) -> None:
    """Refuse a place that is really an existing one under another word.

    The exact-duplicate door compares whole canonical names, so "dorm" slipped past
    "Dormitory" (three dorm-ish places in one session). A name whose canonical form
    is a PREFIX of a live place's (both at least ``_ROOT_NEAR_MATCH_MIN`` chars) is
    refused with that place's id — the same corrective-error idiom the duplicate
    door uses. Names differing only in digits stay distinct ("room 1" / "room 10"),
    and a non-prefix difference ("Guest Room" / "Guest Bathroom") is untouched.

    Scope is EVERY live place, not just roots: once unparented calls default to the
    single root (see :func:`establish_location`), a duplicate surfaces as a CHILD,
    and a duplicate child is just as wrong as a duplicate tree.
    """
    from .cast import canonical_key

    candidate = canonical_key(name)
    if len(candidate) < _ROOT_NEAR_MATCH_MIN:
        return
    for location in reg.locations.values():
        if location.id == OFF_SCREEN_LOCATION_ID:
            continue
        existing = canonical_key(location.name)
        if len(existing) < _ROOT_NEAR_MATCH_MIN:
            continue
        # Numbered siblings are DIFFERENT places ("room 1" vs "room 10"): when the
        # only difference is digits, the names are distinct by construction.
        if _digits(candidate) != _digits(existing) and (
            _no_digits(candidate) == _no_digits(existing)
        ):
            continue
        if candidate == existing or (
            len(candidate) >= _ROOT_NEAR_MATCH_MIN
            and len(existing) >= _ROOT_NEAR_MATCH_MIN
            and (existing.startswith(candidate) or candidate.startswith(existing))
        ):
            raise EntityError(
                f"'{name}' is the place the registry already holds as "
                f"'{location.name}' ({location.id}) — cite that id, or pass "
                "parent_id to nest a genuinely new room"
            )


def _require_absent_identity(
    reg: Registry, kind: str, name: str, *, skip_id: str | None = None
) -> None:
    """Refuse an establish whose identity is already live (the duplicate guard).

    The store never MERGES (req. 4 — a dumb store), but a live object with the
    same canonical name is surfaced as a corrective error naming the existing id
    instead of minting a suffixed duplicate: the live gate measured a 4B
    re-establishing "dormitory" every turn and moving the cast into the fresh
    copy, and a small model recovers from a readable id error (it already does
    for the ambiguity and range rejections). A retired object frees its name, so
    re-establishing after a tombstone still works.
    """
    from .cast import canonical_key

    wanted = canonical_key(name)
    for object_id, obj in _objects(reg, kind).items():
        if skip_id is not None and object_id == skip_id:
            continue
        if canonical_key(getattr(obj, "name", "")) == wanted:
            raise EntityError(
                f"{name!r} already exists as {object_id} — cite that id "
                "(a genuinely different place needs a different name)"
            )


def _object_kind(reg: Registry, object_id: str) -> str | None:
    for kind in _ALL_KINDS:
        if object_id in _objects(reg, kind):
            return kind
    return None


def _require_object(reg: Registry, object_id: object) -> tuple[str, str]:
    """``(id, kind)`` for a live object, or a readable "unknown object" error."""
    cleaned = _required_text(object_id, "an object id")
    kind = _object_kind(reg, cleaned)
    if kind is None:
        known: list[str] = []
        for candidate_kind in _ALL_KINDS:
            for object_id_, obj in _objects(reg, candidate_kind).items():
                label = (
                    getattr(obj, "name", "")
                    or getattr(obj, "key", "")
                    or getattr(obj, "host", "")
                    or getattr(obj, "denomination", "")
                )
                known.append(f"{object_id_} ({label})" if label else object_id_)
        listing = ", ".join(known) or "(none yet)"
        raise EntityError(f"no object {cleaned!r} — the registry holds: {listing}")
    return cleaned, kind


def establish_location(
    log: "EventLog",
    session_id: str,
    name: object,
    *,
    parent_id: object = None,
    note: object = "",
    root: bool = False,
    seq: int | None = None,
    actor: str = DEFAULT_ACTOR,
) -> str:
    """Append an ``entity_establish`` for a room/area; returns the minted id (§3.1).

    Empty locations are legal (req. 8: no seeding, no card parsing); a
    ``parent_id`` must already be a live location, and the off-screen bucket is
    a destination, never a parent.

    ROOTING RULE (owner 2026-10-21, live): a 4B told "nest, never re-root"
    still opened a second tree ("master bedroom" beside "The Crimson Lantern",
    its own note saying it was part of the building). Prompting alone does not
    hold, so the store decides:
      * no live root yet  → this call IS the root;
      * exactly ONE root  → an unparented call NESTS under it (the model's own
        note said "of house" — the default matches its intent);
      * several roots     → refuse an unparented call and name them;
      * ``root=True``     → always a new root (a genuinely separate building).
    """
    reg = registry(log, session_id)
    cleaned = _required_text(name, "a location name")
    parent = (
        resolve_location(reg, parent_id, allow_off_screen=False)
        if parent_id is not None and _clean(parent_id)
        else None
    )
    if parent is None and not root:
        roots = _live_roots(reg)
        if len(roots) == 1:
            parent = roots[0].id
        elif len(roots) > 1:
            listing = ", ".join(f"'{r.name}' ({r.id})" for r in roots)
            raise EntityError(
                f"'{cleaned}' has no parent and the tree has several roots: "
                f"{listing} — pass parent_id to nest it, or root=true for a "
                "genuinely separate building"
            )
    location_id = mint_location_id(cleaned, _live_ids(reg, "location"))
    _require_absent_identity(reg, "location", cleaned)
    # W9.31 follow-up (live): the tracker re-created places the
    # tree already held under another surface ("dorm" next to "Dormitory"). Checked
    # for every establish — nested duplicates are as wrong as parallel trees.
    _require_distinct_place(reg, cleaned)
    payload: dict[str, Any] = {
        "event": ENTITY_ESTABLISH_EVENT,
        "kind": "location",
        "id": location_id,
        "name": cleaned,
    }
    if parent:
        payload["parent_id"] = parent
    text = _clean(note)
    if text:
        payload["note"] = text
    _append(log, session_id, payload, seq=seq, actor=actor)
    return location_id


def establish_thread(
    log: "EventLog",
    session_id: str,
    name: object,
    *,
    detail: object = "",
    note: object = "",
    seq: int | None = None,
    actor: str = DEFAULT_ACTOR,
) -> str:
    """Append an ``entity_establish`` for a plot thread (§3.2).

    A thread enters ``active`` (§3.2's vocabulary); status changes are an
    ``entity_update``, not a re-establish.
    """
    reg = registry(log, session_id)
    cleaned = _required_text(name, "a thread name")
    thread_id = mint_thread_id(cleaned, _live_ids(reg, "thread"))
    _require_absent_identity(reg, "thread", cleaned)
    payload: dict[str, Any] = {
        "event": ENTITY_ESTABLISH_EVENT,
        "kind": "thread",
        "id": thread_id,
        "name": cleaned,
    }
    for key, value in (("detail", detail), ("note", note)):
        text = _clean(value)
        if text:
            payload[key] = text
    _append(log, session_id, payload, seq=seq, actor=actor)
    return thread_id


def establish_relationship(
    log: "EventLog",
    session_id: str,
    source: object,
    target: object,
    *,
    note: object = "",
    name: object = "",
    seq: int | None = None,
    actor: str = DEFAULT_ACTOR,
) -> str:
    """Append an ``entity_establish`` for the ``(from, to)`` prose edge (§3.3).

    Ends are being keys (``member_id`` | ``player``); the payload's ``name`` is
    a rendering label defaulting to ``"from→to"``. A second edge for the same
    pair is a legal duplicate (req. 4) and mints a distinct id.
    """
    reg = registry(log, session_id)
    origin = _required_text(source, "a relationship's 'from'")
    destination = _required_text(target, "a relationship's 'to'")
    relationship_id = mint_relationship_id(
        origin, destination, _live_ids(reg, "relationship")
    )
    payload: dict[str, Any] = {
        "event": ENTITY_ESTABLISH_EVENT,
        "kind": "relationship",
        "id": relationship_id,
        "name": _clean(name) or f"{origin}→{destination}",
        "from": origin,
        "to": destination,
    }
    text = _clean(note)
    if text:
        payload["note"] = text
    _append(log, session_id, payload, seq=seq, actor=actor)
    return relationship_id


def establish_item(
    log: "EventLog",
    session_id: str,
    name: object,
    *,
    holder: object,
    note: object = "",
    seq: int | None = None,
    actor: str = DEFAULT_ACTOR,
) -> str:
    """Append an ``entity_establish`` for an inventory item (§3.5)."""
    reg = registry(log, session_id)
    cleaned = _required_text(name, "an item name")
    owner = _required_text(holder, "an item holder")
    item_id = mint_item_id(cleaned, _live_ids(reg, "item"))
    _require_absent_identity(reg, "item", cleaned)
    payload: dict[str, Any] = {
        "event": ENTITY_ESTABLISH_EVENT,
        "kind": "item",
        "id": item_id,
        "name": cleaned,
        "holder": owner,
    }
    text = _clean(note)
    if text:
        payload["note"] = text
    _append(log, session_id, payload, seq=seq, actor=actor)
    return item_id


def _meter_by_identity(
    reg: Registry, host: str, subject: str | None, key: str
) -> Meter | None:
    for meter in reg.meters.values():
        if meter.host == host and meter.subject == subject and meter.key == key:
            return meter
    return None


def set_attribute(
    log: "EventLog",
    session_id: str,
    host: object,
    *,
    key: object,
    value: object,
    subject: object = None,
    seq: int | None = None,
    actor: str = DEFAULT_ACTOR,
) -> tuple[str, int | float | str | None]:
    """Append an ``entity_set``; returns ``(meter_id, prior_value)`` (§3.4).

    Identity is ``(host, subject, key)``: the first set mints the ``mt_`` id and
    every later set reuses it, so "hostility 42 -> 30" is one object across
    turns. The value keeps the model's type — the catalog (§4, W2) is what
    decides meter-vs-trait and enforces a range, not this store.
    """
    reg = registry(log, session_id)
    owner = _required_text(host, "a meter host")
    attribute = _required_text(key, "a meter key")
    stored = _attribute_value(value)
    if stored is None:
        raise EntityError(
            f"{attribute} needs a number or non-empty text, got {value!r}"
        )
    toward = _clean(subject) or None
    existing = _meter_by_identity(reg, owner, toward, attribute)
    meter_id = (
        existing.id
        if existing is not None
        else mint_meter_id(owner, toward, attribute, _live_ids(reg, "meter"))
    )
    payload: dict[str, Any] = {
        "event": ENTITY_SET_EVENT,
        "id": meter_id,
        "host": owner,
        "key": attribute,
        "value": stored,
    }
    if toward:
        payload["subject"] = toward
    _append(log, session_id, payload, seq=seq, actor=actor)
    return meter_id, (existing.value if existing is not None else None)


def update_object(
    log: "EventLog",
    session_id: str,
    object_id: object,
    *,
    name: object = None,
    note: object = None,
    status: object = None,
    detail: object = None,
    holder: object = None,
    parent_id: object = None,
    seq: int | None = None,
    actor: str = DEFAULT_ACTOR,
) -> str:
    """Append an ``entity_update`` patch; the id never changes (§5).

    ``parent_id`` RE-PARENTS a location (owner 2026-10-21): the tracker can open
    a root the tree did not need, and until now nothing could nest it afterwards.
    The move is validated — a live non-bucket location, never itself, and never
    one of its own descendants (a cycle would make the tree unwalkable).
    """
    reg = registry(log, session_id)
    cleaned, kind = _require_object(reg, object_id)
    supplied = {
        "name": name,
        "note": note,
        "status": status,
        "detail": detail,
        "holder": holder,
    }
    if parent_id is not None:
        if kind != "location":
            raise EntityError(f"{cleaned} is a {kind} — only a location has a parent")
        target = resolve_location(reg, parent_id, allow_off_screen=False)
        if target == cleaned:
            raise EntityError("a location cannot be its own parent")
        _require_not_descendant(reg, cleaned, target)
        supplied["parent_id"] = target
    patch: dict[str, str] = {}
    for field_name, value in supplied.items():
        if value is None:
            continue
        text = _clean(value)
        if field_name in {"name", "holder"} and not text:
            raise EntityError(f"{field_name} cannot be blank")
        if field_name == "status" and text.lower() not in THREAD_STATUSES:
            allowed = ", ".join(sorted(THREAD_STATUSES))
            raise EntityError(f"{text!r} is not a thread status — use one of {allowed}")
        patch[field_name] = text.lower() if field_name == "status" else text
    if not patch:
        raise EntityError("nothing to update — pass name, note, status, detail or holder")
    allowed = _PATCH_FIELDS[kind]
    unknown = sorted(set(patch) - allowed)
    if unknown:
        patchable = ", ".join(sorted(allowed)) or "(none)"
        raise EntityError(
            f"{cleaned} is a {kind} — it has no field {unknown[0]!r}; patchable: {patchable}"
        )
    _append(
        log,
        session_id,
        {"event": ENTITY_UPDATE_EVENT, "id": cleaned, "patch": patch},
        seq=seq,
        actor=actor,
    )
    return cleaned


def move_being(
    log: "EventLog",
    session_id: str,
    who: object,
    to: object,
    *,
    at: object = None,
    seq: int | None = None,
    actor: str = DEFAULT_ACTOR,
) -> tuple[str, str]:
    """Append an ``entity_move``; returns ``(who, stored_target)`` (§3.6).

    ``to`` is a location id-or-name or the off-screen bucket (stored as the
    literal ``"off_screen"``). Moving to the bucket ALWAYS states ``at`` — the
    optional location the off-screen crowd is at — so a stale reference can
    never linger: ``at=None`` is an explicit clear (§3.1/§3.7).
    """
    reg = registry(log, session_id)
    being = _required_text(who, "a being to move")
    if canonical_key(being) in _OFF_SCREEN_KEYS:
        raise EntityError(f"{being!r} is a location bucket, not a being")
    target = resolve_location(reg, to)
    payload: dict[str, Any] = {
        "event": ENTITY_MOVE_EVENT,
        "who": being,
        "to": target,
    }
    if target == OFF_SCREEN:
        payload["at"] = (
            resolve_location(reg, at, allow_off_screen=False)
            if _clean(at)
            else None
        )
    elif _clean(at):
        raise EntityError("'at' only applies when moving to off_screen")
    _append(log, session_id, payload, seq=seq, actor=actor)
    return being, target


def retire_object(
    log: "EventLog",
    session_id: str,
    object_id: object,
    *,
    reason: object = "",
    seq: int | None = None,
    actor: str = DEFAULT_ACTOR,
) -> str:
    """Append an ``entity_retire`` tombstone; a later establish resurrects (§5).

    The off-screen bucket can never be retired (§3.1) and an OCCUPIED location
    is refused with the readable, corrective error the tool door hands the model
    (§6): the tracker moves the being first, in the open.
    """
    reg = registry(log, session_id)
    cleaned, kind = _require_object(reg, object_id)
    if cleaned == OFF_SCREEN_LOCATION_ID:
        raise EntityError(f"{OFF_SCREEN_LOCATION_ID} is the absence bucket and can never be retired")
    if kind == "location":
        present = occupants(reg, cleaned)
        if present:
            raise EntityError(
                f"{cleaned} is occupied by {present[0]} — move_being them first"
            )
    _append(
        log,
        session_id,
        {
            "event": ENTITY_RETIRE_EVENT,
            "id": cleaned,
            "reason": _clean(reason),
        },
        seq=seq,
        actor=actor,
    )
    return cleaned


# -- writers for the W9.32 kinds (conditions, bodies, economy) ----------------


def _established_ids(log: "EventLog", session_id: str, kind: str) -> list[str]:
    """Every ``entity_establish`` id of ``kind`` in the chain — live AND tombstoned.

    The mint's collision set for a CONDITION, whose identity is one OCCURRENCE of
    ``(host, key)``: the folded registry cannot answer this (a tombstone is gone from
    it), and the second occurrence must not reuse the first one's id. One extra chain
    walk on a condition's FIRST sight only — every later call is a patch.
    """
    ids: list[str] = []
    for event in _chain_events(log, session_id):
        payload = event.payload if event.type == "system" else None
        if not isinstance(payload, dict):
            continue
        if payload.get("event") != ENTITY_ESTABLISH_EVENT:
            continue
        if _clean(payload.get("kind")) != kind:
            continue
        object_id = _clean(payload.get("id"))
        if object_id:
            ids.append(object_id)
    return ids


def _condition_by_identity(reg: Registry, host: str, key: str) -> Condition | None:
    """The live condition for ``(host, key)``, both sides canonicalised (§3.1)."""
    owner = canonical_key(host)
    attribute = canonical_key(key)
    for condition in reg.conditions.values():
        if canonical_key(condition.host) != owner:
            continue
        if canonical_key(condition.key) == attribute:
            return condition
    return None


def _condition_host(host: object) -> str:
    """Validate a condition host: a being key or the reserved ``scene`` (§3.1).

    ``scene`` is legal — weather and tension conditions hang on it — while the
    off-screen spellings name the absence bucket, which carries no state to hang a
    condition on.
    """
    cleaned = _required_text(host, "a condition host")
    if canonical_key(cleaned) in _OFF_SCREEN_KEYS:
        raise EntityError(
            f"{cleaned!r} is the absence bucket, not a host — cite the being or scene"
        )
    return cleaned


def _body_host(reg: Registry, host: object) -> str:
    """Validate a body host: a BEING only — the scene and rooms have no body (R6)."""
    cleaned = _required_text(host, "a body host")
    key = canonical_key(cleaned)
    if key == canonical_key(SCENE):
        raise EntityError(
            "the scene has no body — set_body is for a being (a member id or player)"
        )
    if key in _OFF_SCREEN_KEYS:
        raise EntityError(f"{cleaned!r} is the absence bucket, not a being")
    if _object_kind(reg, cleaned) == "location":
        raise EntityError(
            f"{cleaned} is a location, not a being — a body belongs to someone"
        )
    return cleaned


def _being_display(log: "EventLog", session_id: str, host: str) -> str:
    """A being's display name for an establish's REQUIRED ``name`` (else the host key).

    ``name`` is required on every establish and the fold drops a nameless row (§3.1
    review P0), so each writer synthesizes one. A body is named for its being — the
    roster's display when the host is a member, otherwise the key itself (``player``
    is not a roster member; the palette renders by host anyway).
    """
    member = member_by_id(roster(log, session_id), host)
    return member.display_name if member is not None and member.display_name else host

def set_condition(
    log: "EventLog",
    session_id: str,
    host: object,
    key: object,
    *,
    severity: object = None,
    source: object = "",
    expires_at: object = "",
    note: object = "",
    seq: int | None = None,
    actor: str = DEFAULT_ACTOR,
) -> Condition:
    """Upsert a condition by ``(host, key)``; returns the folded object (§3.1).

    First sight appends an ``entity_establish`` (``name`` = the condition key, per
    §3.1's P0 name synthesis) and every later call appends ONE ``entity_update``
    patch — the identity never changes. ``severity`` defaults to ``moderate`` on
    first sight; on a later call an OMITTED severity (``None``) keeps the stored
    word, because a call that only amends a source must not silently downgrade a
    ``major`` wound. The same rule governs ``source``/``note``/``expires_at``: an
    empty value means "leave it", so the patch carries only what was stated (and
    only when it differs — the log records edits, not echoes). ``expires_at`` is an
    ``YYYY-MM-DD HH:MM`` instant validated here; an empty one is permanent.

    Refusals (readable, corrective — the tool door hands ``str(exc)`` to the model):
    an unknown severity, a malformed instant, or a call that states nothing new.
    """
    reg = registry(log, session_id)
    owner = _condition_host(host)
    attribute = _required_text(key, "a condition key")
    existing = _condition_by_identity(reg, owner, attribute)
    stated = _stated_severity(severity)
    expiry = _instant(expires_at, "a condition expiry")
    if existing is None:
        condition_id = mint_condition_id(
            owner, attribute, _established_ids(log, session_id, "condition")
        )
        payload: dict[str, Any] = {
            "event": ENTITY_ESTABLISH_EVENT,
            "kind": "condition",
            "id": condition_id,
            "name": attribute,
            "key": attribute,
            "host": owner,
            "severity": stated or CONDITION_SEVERITY_DEFAULT,
            "source": _clean(source),
            "expires_at": expiry,
        }
        text = _clean(note)
        if text:
            payload["note"] = text
        _append(log, session_id, payload, seq=seq, actor=actor)
        return registry(log, session_id).conditions[condition_id]
    patch: dict[str, Any] = {}
    if stated and stated != existing.severity:
        patch["severity"] = stated
    for field_name, value, current in (
        ("source", source, existing.source),
        ("note", note, existing.note),
        ("expires_at", expiry, existing.expires_at),
    ):
        text = _clean(value)
        if text and text != current:
            patch[field_name] = text
    if not patch:
        raise EntityError(
            f"{existing.key} on {existing.host} is already "
            f"{existing.severity}, and nothing else changed — pass severity, source, "
            "expires_at or note to amend it"
        )
    _append(
        log,
        session_id,
        {"event": ENTITY_UPDATE_EVENT, "id": existing.id, "patch": patch},
        seq=seq,
        actor=actor,
    )
    return registry(log, session_id).conditions[existing.id]


def set_body(
    log: "EventLog",
    session_id: str,
    host: object,
    *,
    pose: object = None,
    clothing: object = None,
    surface: object = None,
    note: object = None,
    seq: int | None = None,
    actor: str = DEFAULT_ACTOR,
) -> Body:
    """Upsert the being's ONE body; returns the folded object (§3.1, R6).

    Identity is the HOST, so the duplicate door here is structural rather than an
    error path: a second write is a patch on the same ``bd_`` id, never a second
    body. Unlike :func:`set_condition` every field is ``None``-defaulted, so an
    explicit ``""``/``[]`` CLEARS that field and an omitted one leaves it alone —
    undressing someone is a real edit. ``clothing`` stays an ordered list on the wire
    (outer→inner) and folds to a tuple.
    """
    reg = registry(log, session_id)
    being = _body_host(reg, host)
    existing = body_for(reg, being)
    supplied = {"pose": pose, "clothing": clothing, "surface": surface, "note": note}
    stated = {field: value for field, value in supplied.items() if value is not None}
    if not stated:
        raise EntityError("nothing to set — pass pose, clothing, surface or note")
    if existing is None:
        body_id = mint_body_id(being, _live_ids(reg, "body"))
        payload: dict[str, Any] = {
            "event": ENTITY_ESTABLISH_EVENT,
            "kind": "body",
            "id": body_id,
            "name": _being_display(log, session_id, being),
            "host": being,
        }
        for field_name, value in stated.items():
            if field_name == "clothing":
                layers = _clothing(value)
                if layers:
                    payload["clothing"] = list(layers)
            elif _clean(value):
                payload[field_name] = _clean(value)
        _append(log, session_id, payload, seq=seq, actor=actor)
        return registry(log, session_id).bodies[body_id]
    patch: dict[str, Any] = {}
    for field_name, value in stated.items():
        if field_name == "clothing":
            patch["clothing"] = list(_clothing(value))
        else:
            patch[field_name] = _clean(value)
    _append(
        log,
        session_id,
        {"event": ENTITY_UPDATE_EVENT, "id": existing.id, "patch": patch},
        seq=seq,
        actor=actor,
    )
    return registry(log, session_id).bodies[existing.id]


def establish_economy(
    log: "EventLog",
    session_id: str,
    denomination: object,
    *,
    balance: object = 0,
    note: object = "",
    seq: int | None = None,
    actor: str = "planner",
) -> Economy:
    """Seed the session's one purse; returns the folded object (§3.1, R7).

    The PLANNER is the seeder of record (§5's ownership map), hence the actor
    default. One economy per session: a second establish is refused naming the live
    ``eco_`` id, so changing currency is an explicit retire-then-establish (O3) and
    never a silent second purse.
    """
    reg = registry(log, session_id)
    live = reg.economy
    if live is not None:
        raise EntityError(
            f"this session already has an economy in {live.denomination!r} ({live.id}) — "
            "adjust_economy moves the balance; retire it first to change the currency"
        )
    unit = _required_text(denomination, "an economy denomination")
    opening = _balance(balance)
    if opening is None:
        raise EntityError(f"an opening balance must be a whole number, got {balance!r}")
    if opening < 0:
        raise EntityError(f"an opening balance cannot be negative, got {opening}")
    economy_id = mint_economy_id(unit, _live_ids(reg, "economy"))
    payload: dict[str, Any] = {
        "event": ENTITY_ESTABLISH_EVENT,
        "kind": "economy",
        "id": economy_id,
        "name": unit,
        "denomination": unit,
        "balance": opening,
    }
    text = _clean(note)
    if text:
        payload["note"] = text
    _append(log, session_id, payload, seq=seq, actor=actor)
    seeded = registry(log, session_id).economy
    if seeded is None:  # pragma: no cover - the establish row just appended folds
        raise EntityError(f"{economy_id} did not fold — the establish row was not accepted")
    return seeded


def adjust_economy(
    log: "EventLog",
    session_id: str,
    amount: object,
    *,
    denomination: object = None,
    seq: int | None = None,
    actor: str = DEFAULT_ACTOR,
) -> Economy:
    """Move the balance by ``amount`` (negative spends); returns the folded object.

    One ``entity_update`` patch ``{"balance": "<new>"}`` — the wire stays a string
    like every other patch field (§3.1) while the fold keeps an int. Four refusals,
    all corrective: no economy yet (naming the planner's seed), a zero amount, a
    currency that is not the stored one (casefold-equal is the same currency — the
    spelling is not the point), and a result below zero.
    """
    reg = registry(log, session_id)
    live = reg.economy
    if live is None:
        raise EntityError(
            "this session has no economy yet — establish_economy(denomination, balance) "
            "seeds the opening purse before amounts can move"
        )
    delta = _balance(amount)
    if delta is None:
        raise EntityError(
            f"an amount must be a whole number (negative spends), got {amount!r}"
        )
    if delta == 0:
        raise EntityError(
            "an amount of 0 changes nothing — pass a positive gain or a negative spend"
        )
    unit = _clean(denomination)
    if unit and unit.casefold() != live.denomination.casefold():
        raise EntityError(
            f"the economy is in {live.denomination!r} — {unit!r} is a different "
            "currency; amounts never cross denominations"
        )
    balance = live.balance + delta
    if balance < 0:
        raise EntityError(
            f"{live.balance} {live.denomination} cannot absorb {delta} — that would "
            f"leave {balance}; the balance never goes negative"
        )
    _append(
        log,
        session_id,
        {
            "event": ENTITY_UPDATE_EVENT,
            "id": live.id,
            "patch": {"balance": str(balance)},
        },
        seq=seq,
        actor=actor,
    )
    return replace(live, balance=balance)


def expire_conditions(
    log: "EventLog",
    session_id: str,
    *,
    now: object,
    actor: str = "clock",
) -> list[str]:
    """Retire every condition whose instant the clock has passed (W9.32 §3.1, R5).

    The engine's sweep (W6a calls it from ``pre_turn``) appends one ``entity_retire``
    per passed expiry with the reason ``expired at <instant>`` and returns the retired
    ids. It APPENDS rather than filtering on read (D9): the log owns the removal, so a
    replay and a fork see the same scene, and the palette's read side never has to
    re-derive "was this still active last turn".

    Idempotent by tombstone: a retired condition is gone from the registry, so a
    second call with the same ``now`` retires nothing — the sweep can run on every
    turn without inventing a second tombstone for one expiry.
    """
    moment = _instant(_required_text(now, "the current instant"), "the current instant")
    reg = registry(log, session_id)
    expired = expired_conditions(reg, moment)
    for condition in expired:
        _append(
            log,
            session_id,
            {
                "event": ENTITY_RETIRE_EVENT,
                "id": condition.id,
                "reason": f"expired at {condition.expires_at}",
            },
            seq=None,
            actor=actor,
        )
    return [condition.id for condition in expired]


def _count(value: object) -> int:
    """A count from either an int or a sequence of rejected/flagged items."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    return len(value) if isinstance(value, (list, tuple, set, frozenset, dict)) else 0


def append_entity_summary(
    log: "EventLog",
    session_id: str,
    *,
    actions: object,
    rejected: object = 0,
    flags: object = 0,
    seq: int | None = None,
    actor: str = DEFAULT_ACTOR,
) -> dict[str, Any]:
    """Append the per-turn ``entity_summary`` observability row (§5).

    Counts only — it joins the ``agent_loop`` traces and feeds the palette
    footer's "N actions last turn" (§7); it never carries object state.
    ``seq`` is a REQUIRED key on this variant, so an unknown turn records 0
    rather than omitting it (the row still validates).
    """
    known = isinstance(seq, int) and not isinstance(seq, bool)
    payload: dict[str, Any] = {
        "event": ENTITY_SUMMARY_EVENT,
        "seq": int(seq) if known else 0,
        "actions": _count(actions),
        "rejected": _count(rejected),
        "flags": _count(flags),
    }
    log.append_event(session_id, "system", actor, payload)
    return payload
