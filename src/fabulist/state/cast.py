"""Canonical cast identity (W7): one engine-owned member per person.

Root cause this module fixes: before W7 the cast was *whatever string the model
typed*. BTS keyed its state by the literal ``:: Name ::`` header, attribution
returned the literal ``speaker``, and ``GET /cast`` de-duped by exact string — so
``Clerk`` / ``clerk`` / ``the clerk`` were three unrelated entities everywhere
(colour, attribution, pane). Read-side normalisation is explicitly rejected: it
is neither durable (the next turn re-invents another spelling) nor authoritative
(two consumers can normalise differently).

The model
---------
One append-only event-log record per person, folded fork-aware exactly like
every other cast read:

* :data:`CAST_ADMIT_EVENT` — a member enters the roster. **This is the single
  admit path** used by every producer (card seed, world seed, BTS, extractor,
  attribution), which is the owner's "a new cast member must enter through the
  SAME path as a card-captured member" requirement.
* :data:`CAST_ALIAS_EVENT` — a new surface form maps to an existing member.
* :data:`CAST_PATCH_EVENT` — a rename: ``display_name`` changes, the *previous*
  display name is automatically demoted to an alias, and the ``member_id`` is
  untouched (ADDENDUM 1). A character introduced as "Woman" and later named
  "Zahra" keeps one identity, one colour and its whole attribution history.
* :data:`CAST_LINK_EVENT` — a **descriptor** link (ADDENDUM 2/3): "the woman"
  is a scene/presence-scoped handle, never an identity.
* :data:`CAST_RETIRE_EVENT` — a tombstone (a later admit resurrects, D10).
* :data:`CAST_AMBIGUOUS_EVENT` — visibility only (D9): a proposal that cannot be
  resolved uniquely is logged, never silently merged.

Identity vs descriptor (the safety property)
--------------------------------------------
Aliases resolve **backward** (old text maps to the member it referred to then).
Descriptor links resolve **forward** through a live presence-scoped link. The
reference *kind* is classified by the model in the structured contract; the
engine only enforces the consequences — plus the one spelling signal the
owner's semantic rule always honours: a surface opening with an article
("the soldier") is a descriptor whatever the model typed, unless the roster
already holds that exact surface (:func:`fabulist.state.bts.classify_reference_kind`).
Because a false merge corrupts
attribution, colour and memory irreversibly while a false split is cheap to
patch, the engine always **splits on doubt** rather than merging:

* exactly one live linked candidate -> reuse that member;
* two or more live candidates -> log :data:`CAST_AMBIGUOUS_EVENT` and split;
* no live candidate (new scene / no presence) -> a new **provisional** member;
* an explicit conflict signal -> always split.

``member_id`` is immutable for the life of the member. Seed members
(``card``/``world``) get a **deterministic** id derived from their source, so a
replay or a fork mints the identical handle without a stored counter; admitted
members store the minted id **inside** the admit event, so replay reads it
instead of re-minting.

Cache contract (AGENTS.md non-negotiable 6)
-------------------------------------------
The roster is a log fact with two projections on opposite sides of the cache
boundary. The **seed** already rides the frozen static prefix (cards -> the
``### Cast`` block and the D43 ``{{char}}`` snapshot). **Growth** — admissions
and renames — never rewrites that prefix; it is surfaced by
:func:`runtime_notice` through the existing dynamic end notice
(:func:`fabulist.prompts.cast.cast_delta_notice` ->
:func:`fabulist.context.facts.recalled_message`). A rename therefore corrects
the model at the END of the prompt while the turn-1 prefix stays byte-identical.

The player is a NODE, not a member (W9.32 / R3)
-----------------------------------------------
The session's persona is the PLAYER: its identity is the internal literal
``"player"`` and the persona name is that node's display name. A cast member
carrying the same name would split the player across two registry keys, so
the admit path REFUSES a player name (:func:`_refuse_player_admission`; exempt
``provenance="user"``, which is W9.22 User-as-Actor's participation layer) and
the card SEED loop skips it (:func:`ensure_roster` — the persona's seed card
still renders its text, it just admits nobody).
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Mapping, Sequence

from fabulist.context.roster import extract_cards

logger = logging.getLogger(__name__)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

__all__ = [
    "CAST_ADMIT_EVENT",
    "CAST_ALIAS_EVENT",
    "CAST_AMBIGUOUS_EVENT",
    "CAST_EVENT_KINDS",
    "CAST_LINK_EVENT",
    "CAST_PATCH_EVENT",
    "CAST_RETIRE_EVENT",
    "unaccepted_candidate_names",
    "CastMember",
    "REFERENCE_DESCRIPTOR",
    "REFERENCE_IDENTITY",
    "PLAYER_CAST_REFUSAL_TAIL",
    "RUNTIME_ADMITTERS",
    "TIER_ACTOR",
    "TIER_PRESENCE",
    "admit_from_proposal",
    "admit_member",
    "canonical_key",
    "descriptor_candidates",
    "ensure_roster",
    "last_descriptor_links",
    "link_descriptor",
    "member_by_id",
    "member_by_name",
    "member_index",
    "member_tier",
    "normalised_surface",
    "patch_member",
    "resolve_descriptor",
    "retire_from_proposal",
    "retire_member",
    "roster",
    "runtime_notice",
]

# -- event kinds (typed ``system`` variants, registered in schema_gate) -------

#: A member enters the roster (the single admit path).
CAST_ADMIT_EVENT = "cast_admit"
#: A new surface form maps to an existing member (union fold).
CAST_ALIAS_EVENT = "cast_alias"
#: A rename / metadata correction (latest-wins; demotes the old display name).
CAST_PATCH_EVENT = "cast_patch"
#: A descriptor ("the woman") -> member link, live only while the member is
#: present/participating. Never an alias.
CAST_LINK_EVENT = "cast_link"
#: Tombstone; a later admit resurrects (D10 append-only).
CAST_RETIRE_EVENT = "cast_retire"
#: Visibility for a proposal that could not be resolved uniquely (never merged).
CAST_AMBIGUOUS_EVENT = "cast_ambiguous"

CAST_EVENT_KINDS = frozenset(
    {
        CAST_ADMIT_EVENT,
        CAST_ALIAS_EVENT,
        CAST_PATCH_EVENT,
        CAST_LINK_EVENT,
        CAST_RETIRE_EVENT,
        CAST_AMBIGUOUS_EVENT,
    }
)

# -- reference kinds ---------------------------------------------------------

#: A specific person the reader can pick out; may become an alias on admit.
REFERENCE_IDENTITY = "identity"
#: A role phrase ("the woman", "the clerk"); scene/presence-scoped, never an
#: alias. The model classifies; the engine enforces (ADDENDUM 3).
REFERENCE_DESCRIPTOR = "descriptor"

#: ``admitted_by`` values that mark a member as *learned during play* rather
#: than part of the frozen seed — the set the dynamic end notice announces.
#: ``owner`` is the user-gated door (:func:`admit_from_proposal`): the owner's
#: accept of a cast proposal is a runtime admission like any other, and the
#: narrator has to learn the new cast member at the END of the prompt (cache
#: contract: growth never rewrites the frozen prefix).
RUNTIME_ADMITTERS = frozenset({"bts", "extractor", "attribution", "owner"})


@dataclass(frozen=True)
class CastMember:
    """One canonical cast member (the folded view of its events).

    ``member_id`` is immutable and is the only join key for the colour registry
    and attribution; ``display_name`` is rendering only and may change via
    :func:`patch_member`. ``aliases`` accumulate every surface form the member
    has ever carried (including former display names), so old text still
    resolves to the same member forever.
    """

    member_id: str
    display_name: str
    aliases: tuple[str, ...] = ()
    provenance: str = "unknown"
    source_id: str | None = None
    source_seq: int | None = None
    admitted_at_seq: int = 0
    admitted_by: str = "engine"
    status: str = "active"
    role: str = "cast"
    provisional: bool = False
    descriptor_key: str | None = None
    former_names: tuple[str, ...] = ()

    @property
    def is_active(self) -> bool:
        """True unless a :data:`CAST_RETIRE_EVENT` tombstoned the member."""
        return self.status == "active"

    def surface_forms(self) -> tuple[str, ...]:
        """Every string that resolves to this member (display + aliases)."""
        return (self.display_name, *self.aliases)


# -- name canonicalisation ---------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")
_ARTICLES = ("the ", "an ", "a ")
#: Separator characters that name the same surface as a space. The live roster
#: carried ``the_courier`` beside ``the courier`` because the key preserved the
#: underscore; a state header / notation spelling is not a second person.
_SEPARATORS_RE = re.compile(r"[_-]+")
#: A run-together surface: the model writes an article phrase as one token. Live
#: a live session wrote the character header as ``TheHeron`` every turn
#: while its own reference entry called the same person ``the heron``, so the
#: header and the reference carried two canonical keys — the identity admit minted
#: one member, the descriptor path minted another, and the descriptor churned a
#: fresh provisional every turn (``the heron (2)``, ``(3)``, ``(4)``). A
#: camelCase boundary is a word boundary, so both spellings are one surface.
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def normalised_surface(name: object) -> str:
    """NFKC -> camelCase split -> casefold -> separators/whitespace collapsed.

    The spelling-only half of :func:`canonical_key`, with the leading article
    INTACT, so a caller can inspect the determiner a surface opens with
    (:func:`fabulist.state.bts.is_determiner_phrase`). ``_`` and ``-`` count as
    spaces: ``the_courier`` / ``the-courier`` / ``the courier`` are one surface.
    A camelCase boundary counts as a space too, so ``TheHeron`` and
    ``the heron`` are one surface (and therefore one person).
    """
    text = _CAMEL_BOUNDARY_RE.sub(" ", unicodedata.normalize("NFKC", str(name or "")))
    text = text.casefold().strip()
    text = _SEPARATORS_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text)
    text = text.strip("\"'“”‘’()[]{}")
    return text.strip()


def canonical_key(name: object) -> str:
    """The write-time comparison key for a surface form (never an identity).

    NFKC -> casefold -> separators (``_``/``-``) and whitespace collapsed to one
    space -> trim -> strip one wrapping pair of quotes/brackets, and drop a
    single leading article when a token survives (so a character literally
    named "The" is preserved). This is used to build the *alias index* at admit
    time and to match a proposal to an existing member; it is never substituted
    for a name on read, which is what makes the mapping authoritative rather
    than read-side coercion.
    """
    text = normalised_surface(name)
    for article in _ARTICLES:
        if text.startswith(article) and len(text) > len(article):
            return text[len(article) :].strip()
    return text


# -- the player is a NODE, never a cast member (W9.32 / R3) -------------------
#
# The player is the session's PERSONA: its identity is the internal literal
# ``"player"`` (``state.entities.PLAYER``) and the persona name is that node's
# DISPLAY name — not a member name. The roster is what lanes, colours and
# attribution key on, so a member carrying the player's name splits the player
# across two registry keys (presence compares ``assignment[who] ==
# assignment[player]``, so the two keys drift apart independently). Two doors
# keep the roster clean: the admit path REFUSES a player name, and the card SEED
# loop SKIPS it — the persona seed card is materialised for its text alone and
# admits nobody.

#: The tail every "this name is the player" refusal carries, so the model fixes
#: its citation (the palette's ``who:"player"``) from the error itself.
PLAYER_CAST_REFUSAL_TAIL = "— the player is not a cast member (cite who:'player')"


def _persona_display_name(log: "EventLog", session_id: str) -> str | None:
    """The persona's display name, or ``None`` — best-effort, never fatal.

    :func:`fabulist.state.presence._player_name` is the ONE resolution of "who
    is the player"; this module only renames the result. The import is lazy on
    purpose: :mod:`fabulist.state.presence` imports THIS module at import time,
    so a module-level import would be a cycle. Any failure reads as "no
    persona", which switches the player guards off — an unreadable persona must
    admit as today, never block every admission.
    """
    try:
        from .presence import _player_name

        return (_player_name(log, session_id) or "").strip() or None
    except Exception:  # noqa: BLE001 - an unreadable persona admits as today
        logger.warning(
            "could not resolve the player's persona for session %r; the player "
            "guards are off for this call",
            session_id,
            exc_info=True,
        )
        return None


def _player_keys(log: "EventLog", session_id: str) -> frozenset[str]:
    """Canonical keys that name the PLAYER in this chat; EMPTY when persona-less.

    The cast layer's own reading of "who is the player": the persona's display
    name (:func:`_persona_display_name`) plus the generic
    :data:`fabulist.state.presence.PLAYER_SURFACES` words a BTS header uses for
    the same person (``User`` / ``Player`` / ``you`` / ``I`` — the live logs
    admitted those as members and then laned them).

    **Persona-conditioned by design:** a chat with no persona has no player node
    yet, so nothing in it names the player and admissions stay exactly as they
    were. That is what keeps a persona-less chat (a sandbox, or the pre-W9.32
    fixtures) admitting a literal ``User`` member — the read side still
    subtracts it from lanes.

    The being door (``state.entity_tools._player_keys``) answers the same
    question for live state and cannot be reused here: that module imports this
    one.
    """
    persona_name = _persona_display_name(log, session_id)
    if not persona_name:
        return frozenset()
    try:
        from .presence import PLAYER_SURFACES

        keys = {canonical_key(surface) for surface in PLAYER_SURFACES}
    except Exception:  # noqa: BLE001 - no surface list only narrows the guard
        logger.warning(
            "could not read the player surfaces; only the persona name is "
            "guarded for session %r",
            session_id,
            exc_info=True,
        )
        keys = set()
    keys.add(canonical_key(persona_name))
    keys.discard("")
    return frozenset(keys)


def _refuse_player_admission(
    log: "EventLog", session_id: str, cleaned: str, *, provenance: str
) -> None:
    """Raise the readable refusal when ``cleaned`` names the player.

    ``provenance="user"`` is EXEMPT and must stay exempt: W9.22's User-as-Actor
    (``state.bts._admit_user_member``) admits exactly this name on purpose — that
    member IS the player's participation layer (attribution identity and colour)
    while live state keys on ``player``, and refusing it would kill the feature.
    """
    if provenance == "user":
        return
    keys = _player_keys(log, session_id)
    if not keys:
        return
    key = canonical_key(cleaned)
    if not key or key not in keys:
        return
    persona_key = canonical_key(_persona_display_name(log, session_id) or "")
    lead = (
        f"{cleaned!r} is the player's persona name"
        if persona_key and key == persona_key
        else f"{cleaned!r} is one of the player's names"
    )
    raise ValueError(f"{lead} {PLAYER_CAST_REFUSAL_TAIL}")


# -- deterministic ids -------------------------------------------------------

_MEMBER_ID_PREFIX = "cm_"


def _digest(basis: str) -> str:
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _seed_member_id(display_name: str, source_id: str | None = None) -> str:
    """Deterministic id for a seed member (card/world).

    Derived from the source (``source_id`` when known, else the canonical name)
    so a replay or fork mints the identical handle without a stored counter.
    """
    basis = f"seed|{source_id}" if source_id else f"seed|{canonical_key(display_name)}"
    return _MEMBER_ID_PREFIX + _digest(basis)


def _mint_member_id(display_name: str, existing_ids: Iterable[str]) -> str:
    """Mint an admitted member id, stored **inside** its admit event.

    Deterministic given the same log (a counter suffix disambiguates members
    that share a canonical key, e.g. two different "the woman" references), so a
    rebuild reproduces the same handles rather than re-rolling random ones.
    """
    taken = set(existing_ids)
    base = f"admit|{canonical_key(display_name)}"
    index = 0
    while True:
        candidate = _MEMBER_ID_PREFIX + _digest(f"{base}|{index}")
        if candidate not in taken:
            return candidate
        index += 1


# -- fork-aware read ---------------------------------------------------------


def _chain_events(log: "EventLog", session_id: str) -> list[Any]:
    """Fork-resolved raw events for ``session_id``, ordered by seq.

    A branch copies no events, so ``get_events`` is session-local; walking the
    ``base_session`` chain root-first (each ancestor bounded by its child's
    ``base_seq``) replays inherited admits/aliases/patches byte-for-byte.
    Mirrors ``cast_api._session_events`` / ``bts._resolve_events``.
    """
    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = session_id
    while current is not None and current not in seen:
        seen.add(current)
        chain.append(current)
        info = log.get_session(current)
        current = info["base_session"] if info is not None else None
    chain.reverse()  # root-first

    events: list[Any] = []
    for index, sid in enumerate(chain):
        bound: int | None = None
        if index + 1 < len(chain):
            child = log.get_session(chain[index + 1])
            bound = child["base_seq"] if child is not None else None
        for event in log.get_events(sid):
            if bound is not None and event.seq > bound:
                continue
            events.append(event)
    return events


# -- fold --------------------------------------------------------------------


def _append_unique(values: list[str], candidates: Iterable[object]) -> None:
    for candidate in candidates:
        if isinstance(candidate, str):
            cleaned = candidate.strip()
            if cleaned and cleaned not in values:
                values.append(cleaned)


def _new_state(member_id: str, seq: int) -> dict[str, Any]:
    return {
        "member_id": member_id,
        "display_name": "",
        "aliases": [],
        "former_names": [],
        "provenance": "unknown",
        "source_id": None,
        "source_seq": None,
        "admitted_at_seq": seq,
        "admitted_by": "engine",
        "status": "active",
        "role": "cast",
        "provisional": False,
        "descriptor_key": None,
    }


def _to_member(state: dict[str, Any]) -> CastMember:
    return CastMember(
        member_id=state["member_id"],
        display_name=state["display_name"],
        aliases=tuple(state["aliases"]),
        provenance=state["provenance"],
        source_id=state["source_id"],
        source_seq=state["source_seq"],
        admitted_at_seq=state["admitted_at_seq"],
        admitted_by=state["admitted_by"],
        status=state["status"],
        role=state["role"],
        provisional=state["provisional"],
        descriptor_key=state["descriptor_key"],
        former_names=tuple(state["former_names"]),
    )


def unaccepted_candidate_names(events: Sequence[Any]) -> set[str]:
    """Names with a cast proposal but NO active cast member (user-gated admission).

    A BTS-detected character may live in the BTS ledger (``world_state`` events)
    while its ``kind=cast`` proposal waits for the owner — or after the owner
    rejected it. Those names are BTS-internal and must not surface in
    USER-FACING world-state reads; :mod:`fabulist.api.cast_api` and
    :mod:`fabulist.api.state_api` both filter through this helper. A name is
    visible again the moment an admit lands (accept), and hides again if a
    retire tombstones the member (undo). Names with neither a proposal nor a
    member — legacy entities, venues — are unaffected.
    """
    proposed: set[str] = set()
    for event in events:
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        if payload.get("event") != "asset_proposal" or payload.get("kind") != "cast":
            continue
        patch = payload.get("patch")
        name = patch.get("name") if isinstance(patch, dict) else None
        if isinstance(name, str) and name.strip():
            proposed.add(name.strip())
    if not proposed:
        return set()
    active: set[str] = set()
    for member in _fold_members(events):
        if member.is_active:
            active.add(member.display_name)
            active.update(member.aliases)
    return {name for name in proposed if name not in active}


def _fold_members(events: Sequence[Any]) -> list[CastMember]:
    """Fold admit/alias/patch/retire events in seq order (latest-wins per member)."""
    order: list[str] = []
    state: dict[str, dict[str, Any]] = {}
    for event in events:
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        kind = payload.get("event")
        if kind not in CAST_EVENT_KINDS:
            continue
        member_id = payload.get("member_id")
        if kind == CAST_ADMIT_EVENT:
            if not isinstance(member_id, str) or not member_id:
                continue
            if member_id not in state:
                order.append(member_id)
                state[member_id] = _new_state(member_id, event.seq)
                entry = state[member_id]
                entry["display_name"] = str(payload.get("name") or "").strip()
                entry["provenance"] = str(payload.get("provenance") or "unknown")
                entry["admitted_by"] = str(payload.get("admitted_by") or "engine")
                source_id = payload.get("source_id")
                entry["source_id"] = source_id if isinstance(source_id, str) else None
                source_seq = payload.get("source_seq")
                entry["source_seq"] = source_seq if isinstance(source_seq, int) else None
                entry["role"] = str(payload.get("role") or "cast")
                entry["provisional"] = bool(payload.get("provisional") or False)
                key = payload.get("descriptor_key")
                entry["descriptor_key"] = key if isinstance(key, str) else None
            entry = state[member_id]
            entry["status"] = "active"
            _append_unique(entry["aliases"], payload.get("aliases") or [])
            continue
        if not isinstance(member_id, str):
            continue
        entry = state.get(member_id)
        if entry is None:
            continue
        if kind == CAST_ALIAS_EVENT:
            _append_unique(entry["aliases"], [payload.get("alias")])
        elif kind == CAST_PATCH_EVENT:
            new_name = payload.get("display_name")
            if isinstance(new_name, str) and new_name.strip():
                new_name = new_name.strip()
                previous = entry["display_name"]
                if previous and previous != new_name:
                    _append_unique(entry["aliases"], [previous])
                    _append_unique(entry["former_names"], [previous])
                entry["display_name"] = new_name
                entry["provisional"] = False
            role = payload.get("role")
            if isinstance(role, str) and role:
                entry["role"] = role
            provenance = payload.get("provenance")
            if isinstance(provenance, str) and provenance:
                entry["provenance"] = provenance
            entry["status"] = "active"
        elif kind == CAST_RETIRE_EVENT:
            entry["status"] = "retired"
    return [_to_member(state[member_id]) for member_id in order]


def member_index(members: Sequence[CastMember]) -> dict[str, list[str]]:
    """Canonical key -> member ids, in roster order (display names + aliases)."""
    index: dict[str, list[str]] = {}
    for member in members:
        for surface in member.surface_forms():
            key = canonical_key(surface)
            if not key:
                continue
            ids = index.setdefault(key, [])
            if member.member_id not in ids:
                ids.append(member.member_id)
    return index


def member_by_id(
    members: Sequence[CastMember], member_id: str
) -> CastMember | None:
    for member in members:
        if member.member_id == member_id:
            return member
    return None


def member_by_name(
    members: Sequence[CastMember], name: object
) -> CastMember | None:
    """Resolve a surface form to its member (exact display, then alias index).

    Returns ``None`` when the name is unknown or when the key is ambiguous
    (two members share it) — an ambiguous key is never resolved silently.
    """
    cleaned = str(name or "").strip()
    if not cleaned:
        return None
    for member in members:
        if member.display_name == cleaned:
            return member
    ids = member_index(members).get(canonical_key(cleaned), [])
    if len(ids) == 1:
        return member_by_id(members, ids[0])
    return None


def roster(log: "EventLog", session_id: str) -> list[CastMember]:
    """The folded cast for ``session_id`` (fork-aware), in roster order."""
    return _fold_members(_chain_events(log, session_id))


def ensure_roster(log: "EventLog", session_id: str) -> list[CastMember]:
    """Return the roster, minting the deterministic SEED admits on first use.

    Idempotent: once the chain carries any ``cast_admit`` this is a pure read.
    Seeds come from the session's persisted card events (:func:`extract_cards`)
    — the same materialized cast the frozen prefix renders — so a member added
    by BTS later enters through the same event kind as a card-captured member.
    A legacy session materialises on its next read/turn (no migration, D9).

    W9.32/R3: a card whose name is a PLAYER name seeds NOBODY. Every persona'd
    chat materialises its persona as a ``system`` card event
    (``cards/v2.persona_to_seed_card``), and this loop used to admit it with
    ``provenance="card"`` through :func:`_append_admit` — below the admit
    refusal, which would then hand the player a cast member that outranks the
    ``player`` key in the being door's resolution. The card EVENT stays (its
    text still renders in the prefix); only the roster admission is skipped, so
    an all-skipped loop writes nothing and this stays idempotent.
    """
    events = _chain_events(log, session_id)
    members = _fold_members(events)
    if members:
        return members
    names: list[str] = []
    for card in extract_cards(events):
        if not isinstance(card, dict):
            continue
        name = str(card.get("name") or "").strip()
        if name and name not in names:
            names.append(name)
    player_keys = _player_keys(log, session_id)
    for name in names:
        if canonical_key(name) in player_keys:
            logger.info(
                "session %r: card %r names the player — no cast member seeded",
                session_id,
                name,
            )
            continue
        _append_admit(
            log,
            session_id,
            _seed_member_id(name),
            name,
            provenance="card",
            admitted_by="system",
            actor="system",
        )
    return _fold_members(_chain_events(log, session_id))


# -- writers -----------------------------------------------------------------


def _append_admit(
    log: "EventLog",
    session_id: str,
    member_id: str,
    name: str,
    *,
    provenance: str,
    admitted_by: str,
    source_id: str | None = None,
    source_seq: int | None = None,
    provisional: bool = False,
    descriptor_key: str | None = None,
    actor: str = "engine",
) -> None:
    payload: dict[str, Any] = {
        "event": CAST_ADMIT_EVENT,
        "member_id": member_id,
        "name": name,
        "provenance": provenance,
        "admitted_by": admitted_by,
    }
    if isinstance(source_id, str) and source_id:
        payload["source_id"] = source_id
    if isinstance(source_seq, int):
        payload["source_seq"] = source_seq
    if provisional:
        payload["provisional"] = True
    if descriptor_key:
        payload["descriptor_key"] = descriptor_key
    log.append_event(session_id, "system", actor, payload)


def _append_alias(
    log: "EventLog", session_id: str, member_id: str, alias: str
) -> None:
    log.append_event(
        session_id,
        "system",
        "engine",
        {"event": CAST_ALIAS_EVENT, "member_id": member_id, "alias": alias},
    )


def _append_ambiguous(
    log: "EventLog", session_id: str, proposed: str, candidates: Sequence[str]
) -> None:
    log.append_event(
        session_id,
        "system",
        "engine",
        {
            "event": CAST_AMBIGUOUS_EVENT,
            "proposed": str(proposed),
            "candidates": list(candidates),
        },
    )


def link_descriptor(
    log: "EventLog",
    session_id: str,
    descriptor_key: str,
    member_id: str,
    *,
    source: str = "engine",
) -> None:
    """Link a descriptor to ``member_id`` (ADDENDUM 2).

    A descriptor is a scene/presence-scoped handle, NOT an identity: it is never
    written as an alias. The link is live only while ``member_id`` is present —
    see :func:`resolve_descriptor`.
    """
    log.append_event(
        session_id,
        "system",
        str(source),
        {
            "event": CAST_LINK_EVENT,
            "descriptor_key": str(descriptor_key),
            "member_id": member_id,
            "source": str(source),
        },
    )


def _descriptor_links(events: Sequence[Any]) -> dict[str, list[str]]:
    links: dict[str, list[str]] = {}
    for event in events:
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        if payload.get("event") != CAST_LINK_EVENT:
            continue
        key = payload.get("descriptor_key")
        member_id = payload.get("member_id")
        if not isinstance(key, str) or not isinstance(member_id, str) or not member_id:
            continue
        ids = links.setdefault(canonical_key(key) or key, [])
        if member_id not in ids:
            ids.append(member_id)
    return links


def last_descriptor_links(log: "EventLog", session_id: str) -> dict[str, tuple[str, int]]:
    """``descriptor_key -> (member_id, seq)`` of the NEWEST ``cast_link`` row.

    A link is the engine's own record that a phrase pointed at a member, and the
    newest one is where that phrase currently points. The BTS apply path reads it
    as PRESENCE EVIDENCE: BTS is delta-first, so a turn that does not re-report an
    unchanged character is not evidence the character left — without this a
    drifted header split the descriptor into a fresh provisional every turn.
    Read-only; the caller decides how fresh the evidence must be.
    """
    newest: dict[str, tuple[str, int]] = {}
    for event in _chain_events(log, session_id):
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        payload = event.payload
        if payload.get("event") != CAST_LINK_EVENT:
            continue
        key = payload.get("descriptor_key")
        member_id = payload.get("member_id")
        if not isinstance(key, str) or not isinstance(member_id, str) or not member_id:
            continue
        canonical = canonical_key(key) or key
        previous = newest.get(canonical)
        if previous is None or event.seq >= previous[1]:
            newest[canonical] = (member_id, event.seq)
    return newest


def descriptor_candidates(
    log: "EventLog",
    session_id: str,
    descriptor_key: str,
    *,
    presence: Sequence[str] | None = None,
) -> list[str]:
    """The live members a descriptor is linked to, in link order (read-only).

    The read :func:`resolve_descriptor` builds its verdict from, exposed so a
    writer can ask the same question without triggering its refusal side effect
    (the BTS apply path asks "is this phrase already spoken for?").
    ``presence=None`` is presence-agnostic: any ACTIVE linked member counts.
    """
    key = canonical_key(descriptor_key) or str(descriptor_key).strip()
    events = _chain_events(log, session_id)
    members = _fold_members(events)
    live: list[str] = []
    for member_id in _descriptor_links(events).get(key, []):
        member = member_by_id(members, member_id)
        if member is None or not member.is_active:
            continue
        if presence is not None and member_id not in presence:
            continue
        if member_id not in live:
            live.append(member_id)
    return live


def resolve_descriptor(
    log: "EventLog",
    session_id: str,
    descriptor_key: str,
    *,
    presence: Sequence[str] | None = None,
    conflict: bool = False,
) -> str | None:
    """Resolve a descriptor to its live member, or ``None`` (meaning: SPLIT).

    There is no timer (ADDENDUM 3 Q2): the boundary is **presence** plus
    **unambiguity** plus explicit **conflict signals**.

    * exactly one live candidate -> that member (rule 1);
    * no live candidate -> ``None``: the descriptor is a fresh reference (rule 2);
    * two or more live candidates -> log :data:`CAST_AMBIGUOUS_EVENT` and
      ``None`` (rule 3 — refused, never merged);
    * ``conflict`` -> always ``None`` (rule 5): a typed "a different woman".
    """
    if conflict:
        return None
    live = descriptor_candidates(log, session_id, descriptor_key, presence=presence)
    if len(live) == 1:
        return live[0]
    if len(live) > 1:
        _append_ambiguous(log, session_id, descriptor_key, live)
    return None


def admit_member(
    log: "EventLog",
    session_id: str,
    proposed_name: str,
    *,
    provenance: str,
    admitted_by: str,
    source_id: str | None = None,
    source_seq: int | None = None,
    reference_kind: str = REFERENCE_IDENTITY,
    presence: Sequence[str] | None = None,
    conflict: bool = False,
) -> str:
    """Canonicalise ``proposed_name`` and return its immutable ``member_id``.

    The single admit path every producer shares. Identity resolution is: exact
    display name -> canonical/alias key (collapsing ``Clerk``/``clerk``) -> new
    member. When a canonical key matches more than one member the merge is
    REFUSED (a :data:`CAST_AMBIGUOUS_EVENT` is logged) and a new member is
    minted — the engine splits on doubt rather than risking an irreversible
    false merge. ``reference_kind=descriptor`` bypasses the alias index entirely
    and resolves through :func:`resolve_descriptor`, so a role phrase can never
    absorb a specific person (or vice versa).

    W9.32/R3: a name that names the PLAYER is refused here, on the one path
    every producer shares — the player is a NODE (identity ``player``), never a
    cast member. The refusal is the readable :class:`ValueError`
    :func:`_refuse_player_admission` raises; ``provenance="user"`` is exempt
    (User-as-Actor's member IS the player's participation layer).
    """
    cleaned = str(proposed_name or "").strip()
    if not cleaned:
        raise ValueError("cast member name must be non-empty")
    _refuse_player_admission(log, session_id, cleaned, provenance=provenance)
    members = roster(log, session_id)
    if not members:
        # Lazy deterministic bootstrap: card members get their seed ids before
        # any runtime admit, so a member learned in play enters through exactly
        # the same event kind as a card-captured member.
        members = ensure_roster(log, session_id)
    if reference_kind == REFERENCE_DESCRIPTOR:
        return _admit_descriptor(
            log,
            session_id,
            cleaned,
            members,
            provenance=provenance,
            admitted_by=admitted_by,
            presence=presence,
            conflict=conflict,
        )
    return _admit_identity(
        log,
        session_id,
        cleaned,
        members,
        provenance=provenance,
        admitted_by=admitted_by,
        source_id=source_id,
        source_seq=source_seq,
    )


def _admit_identity(
    log: "EventLog",
    session_id: str,
    cleaned: str,
    members: Sequence[CastMember],
    *,
    provenance: str,
    admitted_by: str,
    source_id: str | None,
    source_seq: int | None,
) -> str:
    for member in members:
        if member.display_name == cleaned:
            if not member.is_active:
                # W9.15: LRU eviction is a tombstone, not a ban — a later admit
                # of the same name resurrects the SAME member (id, colour and
                # attribution history preserved). The fold treats a fresh
                # admit as reactivation.
                _append_admit(
                    log,
                    session_id,
                    member.member_id,
                    member.display_name,
                    provenance=provenance,
                    admitted_by=admitted_by,
                    source_id=source_id,
                    source_seq=source_seq,
                )
            return member.member_id
    candidates = member_index(members).get(canonical_key(cleaned), [])
    if len(candidates) == 1:
        target = member_by_id(members, candidates[0])
        if target is not None and cleaned not in target.surface_forms():
            # Persist the novel surface form so every future read resolves it
            # without re-deriving the key.
            _append_alias(log, session_id, target.member_id, cleaned)
        if target is not None and not target.is_active:
            # W9.15: resurrect on admit (see the display-name branch).
            _append_admit(
                log,
                session_id,
                target.member_id,
                target.display_name,
                provenance=provenance,
                admitted_by=admitted_by,
                source_id=source_id,
                source_seq=source_seq,
            )
        return candidates[0]
    if len(candidates) > 1:
        _append_ambiguous(log, session_id, cleaned, candidates)
    member_id = _mint_member_id(cleaned, {m.member_id for m in members})
    _append_admit(
        log,
        session_id,
        member_id,
        cleaned,
        provenance=provenance,
        admitted_by=admitted_by,
        source_id=source_id,
        source_seq=source_seq,
    )
    return member_id


def _admit_descriptor(
    log: "EventLog",
    session_id: str,
    cleaned: str,
    members: Sequence[CastMember],
    *,
    provenance: str,
    admitted_by: str,
    presence: Sequence[str] | None,
    conflict: bool,
) -> str:
    key = canonical_key(cleaned)
    # W9.22: an EXACT NAME match beats an existing descriptor link. Live case:
    # the identity "TheHeron" and the reference phrase "the
    # heron" share ONE canonical key (the camelCase rule), yet turns 2-4 had
    # linked the phrase to engine-minted duplicates, and because a link existed
    # the identity bridge refused — so resolve_descriptor saw several live
    # candidates and SPLIT again every turn ("the heron (5)", ...). A phrase
    # whose canonical key IS the member's canonical display name is that member
    # (not an alias — ADDENDUM 2 A9 stays intact: this is the display name, and
    # aliases are still never matched here); re-link to it and stop the churn.
    exact = None if conflict else _identity_exact_match(members, key)
    if exact is not None:
        # Append the link only when the binding CHANGES: a repeat admission of
        # the same phrase already pointing at this identity must not append an
        # identical cast_link every turn (noise in an append-only log).
        current = last_descriptor_links(log, session_id).get(key)
        if current is None or current[0] != exact:
            link_descriptor(log, session_id, key, exact)
        return exact
    resolved = resolve_descriptor(
        log, session_id, key, presence=presence, conflict=conflict
    )
    if resolved is None:
        resolved = _reuse_disambiguated_surface(
            log, session_id, key, presence=presence, conflict=conflict
        )
    if resolved is not None:
        return resolved
    bridged = _bridge_descriptor_to_identity(
        log, session_id, key, members, conflict=conflict
    )
    if bridged is not None:
        return bridged
    display = cleaned
    taken = {member.display_name.casefold() for member in members}
    if display.casefold() in taken:
        index = 2
        while f"{display} ({index})".casefold() in taken:
            index += 1
        display = f"{display} ({index})"
    member_id = _mint_member_id(display, {m.member_id for m in members})
    _append_admit(
        log,
        session_id,
        member_id,
        display,
        provenance=provenance,
        admitted_by=admitted_by,
        provisional=True,
        descriptor_key=key,
    )
    link_descriptor(log, session_id, key, member_id)
    return member_id


#: The disambiguation suffix THIS module appends when a descriptor surface is
#: already a display name (``the heron`` -> ``the heron (2)``). Matched
#: on the canonical key, which trims a wrapping bracket — so the tail is optional.
_ENGINE_INDEX_SUFFIX_RE = re.compile(r"^(?P<base>.+?) \((?P<index>\d+)\)?$")


def _identity_exact_match(
    members: Sequence[CastMember], key: str
) -> str | None:
    """The ACTIVE member whose canonical DISPLAY name is exactly ``key``.

    W9.22: the phrase-is-the-name case (``the heron`` for ``TheHeron``).
    Display name only — aliases are deliberately NOT matched (ADDENDUM 2 A9:
    matching through aliases is the absorption the design forbids). Exactly one
    active candidate answers; zero or several fall through to the descriptor
    machinery (reuse / refuse-and-split / mint).
    """
    if not key:
        return None
    hits = [
        member.member_id
        for member in members
        # NON-PROVISIONAL only: an engine-minted descriptor duplicate carries the
        # phrase as its display name, so it would match itself here and defeat
        # the deliberate split rules (ADDENDUM 3 Q2 / A9). The exact-name rule
        # answers only for a real, admitted IDENTITY.
        if member.is_active
        and not member.provisional
        and canonical_key(member.display_name) == key
    ]
    return hits[0] if len(hits) == 1 else None


def _reuse_disambiguated_surface(
    log: "EventLog",
    session_id: str,
    key: str,
    *,
    presence: Sequence[str] | None,
    conflict: bool,
) -> str | None:
    """Reuse the member behind the base key when the surface ECHOES our own suffix.

    The ``(N)`` suffix on a descriptor display name is engine-minted, so a model
    that reads the roster (or the world-state prompt) writes it back as a surface
    of its own: a live session typed ``the heron (2)`` as a reference
    the turn after the engine minted that name, and the engine minted ``(3)`` for
    it. The suffix is not a second person, so it resolves to the ONE live member
    the base phrase points at, and the link is recorded for the echoed spelling
    too (next turn resolves directly).

    Narrow by construction: only a ``(<digits>)`` tail, only when the exact key
    resolved to nobody, only ``conflict``-free, and only when the base phrase has
    exactly one live candidate — two live candidates stay ambiguous and SPLIT.
    """
    if conflict:
        return None
    match = _ENGINE_INDEX_SUFFIX_RE.match(key)
    base = (match.group("base") or "").strip() if match else ""
    if not base:
        return None
    linked = descriptor_candidates(log, session_id, base, presence=presence)
    if len(linked) != 1:
        return None
    link_descriptor(log, session_id, key, linked[0])
    return linked[0]


def _bridge_descriptor_to_identity(
    log: "EventLog",
    session_id: str,
    key: str,
    members: Sequence[CastMember],
    *,
    conflict: bool,
) -> str | None:
    """First-sight descriptor -> the IDENTITY member it names, or ``None``.

    The typed path must not be WORSE than the canonical match it replaced: a
    reference for "the woman" while a member named "Woman" is in the roster used
    to resolve through the alias index, and the descriptor path alone would mint
    a second, provisional member beside it (the observed W8.11 failure). The
    bridge restores that resolution as the relation the design allows — a
    ``cast_link``, never an alias (ADDENDUM 2 A9: an alias resolves backward,
    a descriptor link resolves forward).

    It fires only when nothing about the phrase is already decided:

    * ``conflict`` is the typed "a different woman" — never bridged;
    * a link for ``key`` already exists — that is :func:`resolve_descriptor`'s
      case (reuse, or refuse-and-split), so the bridge leaves it alone;
    * the match is by DISPLAY-NAME canonical key only. Matching through aliases
      is exactly the absorption A9 forbids (a later, different "the woman" must
      not be merged into the member that phrase used to name).

    PRESENCE is deliberately NOT a condition here: it is the READ-side boundary
    (:func:`resolve_descriptor` re-checks it on every read, so a link to a
    member who is away changes nothing) and the model stops using the descriptor
    in the very turn it switches to the name — a write-side gate would block the
    bridge exactly when it is needed, and is stricter than the legacy match it
    replaces. Zero candidates falls through to the provisional mint; two or more
    candidates SPLIT (the caller mints), never a silent merge.
    """
    if conflict:
        return None
    if _descriptor_links(_chain_events(log, session_id)).get(key):
        return None
    candidates: list[str] = []
    for member in members:
        if not member.is_active:
            continue
        if canonical_key(member.display_name) != key:
            continue
        candidates.append(member.member_id)
    if len(candidates) != 1:
        return None
    link_descriptor(log, session_id, key, candidates[0])
    return candidates[0]


def patch_member(
    log: "EventLog",
    session_id: str,
    member_id: str,
    *,
    display_name: str | None = None,
    role: str | None = None,
    provenance: str | None = None,
    reason: str = "",
) -> str:
    """Patch a member in place (ADDENDUM 1): rename, role, provenance.

    A rename keeps the ``member_id`` — so the colour registry, attribution rows
    and past quote colouring all stay bound — and automatically demotes the
    previous display name to an alias, so old text ("the woman") still resolves
    to the same member forever.

    A patch whose new display name would make two members share a canonical key
    is REFUSED: a :data:`CAST_AMBIGUOUS_EVENT` is logged and :class:`ValueError`
    is raised (never a silent merge). Callers treat a refusal as best-effort.
    """
    members = roster(log, session_id)
    if not members:
        members = ensure_roster(log, session_id)
    target = member_by_id(members, member_id)
    if target is None:
        raise ValueError(f"unknown cast member: {member_id}")

    new_name = display_name.strip() if isinstance(display_name, str) else ""
    if new_name:
        others = [m for m in members if m.member_id != member_id]
        collision = member_index(others).get(canonical_key(new_name), [])
        if collision:
            _append_ambiguous(log, session_id, new_name, collision)
            raise ValueError(
                f"rename refused: '{new_name}' already identifies {collision}"
            )

    payload: dict[str, Any] = {
        "event": CAST_PATCH_EVENT,
        "member_id": member_id,
        "reason": str(reason or ""),
    }
    if new_name:
        payload["display_name"] = new_name
    if isinstance(role, str) and role:
        payload["role"] = role
    if isinstance(provenance, str) and provenance:
        payload["provenance"] = provenance
    log.append_event(session_id, "system", "engine", payload)
    return member_id


# -- membership tiers (W9.22 §2) ---------------------------------------------
#
# W9.22 splits cast MEMBERSHIP into two tiers without splitting membership
# itself: a presence is still a member (the walk, colours, attribution and
# ``member_categories`` all key on members — reviewer finding 2), but the member
# carries a tier, and the tier decides lanes and cap candidacy.
#
# * **actor** — ``card`` / ``world`` / promoted ``actor`` / ``user`` (User-as-
#   Actor ON). Lane-eligible, never cap-evicted.
# * **presence** — learned in play. Never lanes (bar the transitional bridge in
#   :mod:`fabulist.state.presence`), never pins, and retires by silent LRU under
#   its own budget.
#
# The tier is DERIVED from provenance on every read (never stored): provenance is
# already the log's record of where a member came from, and a promotion re-tiers
# by patching provenance, so there is no second source of truth to keep in sync.

TIER_ACTOR = "actor"
TIER_PRESENCE = "presence"


def member_tier(member: CastMember) -> str:
    """``"actor"`` or ``"presence"`` — the tier ``member.provenance`` puts it in.

    The actor provenances are :data:`fabulist.state.presence.ACTOR_PROVENANCE`
    (``card`` / ``world`` / ``actor`` / ``user``): the owner's attached cast, plus
    the player when User-as-Actor is ON. Everything else — the ``bts`` /
    ``extractor`` identities learned in play — is the presence tier.

    Derivation, not storage (W9.22 §2): a promotion is a provenance patch on the
    SAME member (:func:`patch_member`), so the tier follows automatically and no
    member can sit in the wrong tier because a tier field was not updated.
    """
    from .presence import ACTOR_PROVENANCE  # lazy: presence imports this module

    return TIER_ACTOR if member.provenance in ACTOR_PROVENANCE else TIER_PRESENCE


#: W9.15 (owner): the cast is capped at 8 ACTIVE members; W9.22 §2 splits that
#: one budget in two — the actor tier is never a candidate, and the presence tier
#: is LRU-bounded by ``cap - actors``.
DEFAULT_CAST_CAP = 8


def enforce_cast_cap(
    log: "EventLog",
    session_id: str,
    *,
    cap: int = DEFAULT_CAST_CAP,
    last_seen: Mapping[str, int] | None = None,
) -> list[str]:
    """Two budgets (W9.22 §2): retire presence-tier members, LRU first.

    ``last_seen`` maps ``member_id`` -> the last seq the member was seen present
    (from :func:`fabulist.state.presence.presence_walk` plus speaker evidence);
    a presence-tier member with no evidence ranks oldest (0) and is dropped
    first. Ties fall back to roster order (admit order, oldest first).
    Retirement is the recoverable tombstone (W8.9): ``member_id``, colour,
    aliases and attribution history survive, and a later admit resurrects the
    member.

    Two budgets, not one (the W9.21 generalisation):

    * the **actor tier** (:func:`member_tier` — ``card`` / ``world`` / ``actor``
      / ``user``) is NEVER an eviction candidate. The W9.21-D seed pin showed why:
      a live session retired "Hobbs" and "Tessa" (both ``card``) mid-scene
      while seven runtime junk members stayed, so the characters the owner
      attached vanished from the very context the narrator writes from. The pin
      was keyed on the seed provenances; W9.22 generalises it to the whole actor
      tier, because a promoted actor is exactly as much the owner's cast as a
      card member — losing it to an LRU clock would be the same defect one
      promotion later.
    * the **presence tier** (learned in play) pays the whole bill, LRU-bounded by
      ``cap - actors`` so roster growth stays bounded. A member with no evidence
      at all ranks oldest and pays first.

    Actors still COUNT against the cap (the presence budget is what is left over),
    and when the actor tier alone fills or exceeds the cap the presence budget is
    zero and NOTHING is retired: the cap is never paid for with the owner's cast.

    Returns the retired member ids, oldest first.
    """
    members = [member for member in roster(log, session_id) if member.is_active]
    if len(members) <= cap:
        # Fast path, still correct under two budgets: presence candidates can
        # never exceed the total minus the actors.
        return []
    actors = [member for member in members if member_tier(member) == TIER_ACTOR]
    candidates = [member for member in members if member_tier(member) == TIER_PRESENCE]
    budget = max(cap - len(actors), 0)
    if len(candidates) <= budget:
        return []
    seen = dict(last_seen or {})
    order = {
        member.member_id: index for index, member in enumerate(candidates)
    }
    ranked = sorted(
        candidates,
        key=lambda member: (int(seen.get(member.member_id) or 0), order[member.member_id]),
    )
    retired: list[str] = []
    for member in ranked:
        if len(candidates) - len(retired) <= budget:
            break
        try:
            retire_member(
                log,
                session_id,
                member_id=member.member_id,
                reason=f"cast cap {cap}: least recently seen presence",
            )
        except Exception:  # noqa: BLE001 - best-effort housekeeping, never fatal
            continue
        retired.append(member.member_id)
    return retired


def retire_member(
    log: "EventLog",
    session_id: str,
    *,
    member_id: str,
    reason: str = "",
) -> str:
    """Tombstone a member (ADDENDUM: a later admit resurrects, D10).

    The ``member_id`` is untouched and its colour, aliases and attribution
    history stay bound to it; only the folded ``status`` flips to ``retired``,
    which is what removes the member from runtime notice, descriptor resolution
    and the ``/cast`` pane. Retiring an already-retired member appends NOTHING
    and returns its id, so a repeated trigger can never spam the log.

    Raises :class:`ValueError` for an unknown ``member_id`` — the same refusal
    :func:`patch_member` uses, so a stale caller is told instead of appending a
    tombstone that would fold onto no member at all.
    """
    members = roster(log, session_id)
    if not members:
        members = ensure_roster(log, session_id)
    target = member_by_id(members, member_id)
    if target is None:
        raise ValueError(f"unknown cast member: {member_id}")
    if not target.is_active:
        return member_id
    log.append_event(
        session_id,
        "system",
        "engine",
        {
            "event": CAST_RETIRE_EVENT,
            "member_id": member_id,
            "reason": str(reason or ""),
        },
    )
    return member_id


# -- the owner's cast decision (user-gated admission) ------------------------
#
# Cast admission is USER-GATED (owner ruling, reversing D76's auto-promotion):
# nothing a model detects becomes a member. What BTS or the promotion lane can
# do is PROPOSE (``state.proposals``); the owner's accept is the one admission and
# its undo is a compensating retire. These two helpers are the whole cast write
# surface of that decision, exported so the proposal store calls THIS module
# instead of re-implementing cast writes beside it (D9: one home per fact).


def admit_from_proposal(
    log: "EventLog",
    session_id: str,
    name: str,
    aliases: Sequence[str] = (),
    origin: str = "",
) -> str:
    """Admit the owner-ACCEPTED cast member and teach it its reported spellings.

    The accept is an ACTOR-tier admission — ``provenance="actor"`` is the tier
    :func:`fabulist.state.presence.active_members` lanes, so this is the moment
    a proposed character becomes role-playable — and ``admitted_by="owner"``
    records that a human decided it, not the engine. It goes through
    :func:`admit_member`, the ONE admit path, so an accepted name that already
    resolves to a member (a card character that was re-proposed, a fork that
    accepted it already) reuses that member instead of minting a twin.

    ``aliases`` are the surface forms the tracker reported for the person. Each
    one rides its own :data:`CAST_ALIAS_EVENT` row — ``admit_member`` takes no
    alias parameter, and an alias resolves BACKWARD exactly like every other
    spelling in this module. Three cases are skipped because an alias row is a
    merge claim: a blank spelling, the display name itself, and a spelling
    another member already answers (the engine splits on doubt; a row that made
    two members share a key would turn a working resolution into an ambiguity).

    ``origin`` is the proposal's origin (``bts`` / ``promotion``). It is not
    persisted on the cast rows — the decision row in ``state.proposals`` already
    records which proposal this accept acted on, and the admit payload's shape is
    the log's contract — so it is logged instead, which is enough to read a log
    by hand.
    """
    cleaned = str(name or "").strip()
    if not cleaned:
        raise ValueError("cast member name must be non-empty")
    member_id = admit_member(
        log, session_id, cleaned, provenance="actor", admitted_by="owner"
    )
    # An EXISTING member keeps the provenance it entered with (``admit_member``
    # resolves identity only), and the live log proves the case matters: the
    # attribution pass admits a speaker as ``extractor`` — presence tier, no lane
    # — before the person is ever proposed. The owner's accept must therefore
    # re-tier a presence-tier member in place (the provenance PATCH, ADDENDUM 1),
    # or accepting a proposal the engine already knows would change nothing. Seed
    # provenances (``card``/``world``) and an existing ``user`` actor are left
    # alone: their tier is already correct and their provenance carries the seed
    # pin and the player's role tint.
    resolved = member_by_id(roster(log, session_id), member_id)
    if resolved is not None and member_tier(resolved) == TIER_PRESENCE:
        patch_member(
            log,
            session_id,
            member_id,
            provenance="actor",
            reason="the owner accepted a cast proposal",
        )
    _append_proposal_aliases(log, session_id, member_id, cleaned, aliases)
    logger.info(
        "cast member %s admitted from an owner-accepted proposal (%s)",
        member_id,
        str(origin or "unknown origin"),
    )
    return member_id


def _append_proposal_aliases(
    log: "EventLog",
    session_id: str,
    member_id: str,
    display_name: str,
    aliases: Sequence[str],
) -> None:
    """Bind an accepted member to the spellings the proposal carried."""
    display_key = canonical_key(display_name)
    members = roster(log, session_id)
    index = member_index(members)
    member = member_by_id(members, member_id)
    known = set(member.surface_forms()) if member is not None else {display_name}
    for alias in aliases:
        cleaned = str(alias or "").strip()
        key = canonical_key(cleaned)
        if not cleaned or not key or key == display_key or cleaned in known:
            continue
        owners = index.get(key, [])
        if owners and member_id not in owners:
            continue
        _append_alias(log, session_id, member_id, cleaned)
        known.add(cleaned)


def retire_from_proposal(
    log: "EventLog", session_id: str, name: str, *, reason: str = ""
) -> str | None:
    """Undo an accepted cast proposal: the compensating ``cast_retire``.

    The review home's Undo must work on a cast accept, and the log's undo shape is
    a small explicit pair of writes: accept = admit (:func:`admit_from_proposal`),
    undo = retire. This resolves ``name`` through the roster (display name, then a
    canonical/alias key that names exactly one member) and tombstones that member
    with :func:`retire_member`; its ``member_id``, colour and quote history stay
    bound, so nothing is destroyed and a later accept resurrects the same member.

    Returns the retired ``member_id``, or ``None`` when no single member carries
    the name (already renamed, ambiguous, or never admitted). ``None`` is not an
    error here — the record of what happened is the proposal decision row — but a
    caller that needs to tell the owner "this undo did nothing" can.
    """
    cleaned = str(name or "").strip()
    if not cleaned:
        return None
    member = member_by_name(roster(log, session_id), cleaned)
    if member is None:
        return None
    try:
        return retire_member(
            log,
            session_id,
            member_id=member.member_id,
            reason=reason or f'the owner undid the cast accept for "{cleaned}"',
        )
    except ValueError:  # pragma: no cover - the roster resolved it a moment ago
        return None


# -- dynamic end notice ------------------------------------------------------


def runtime_notice(log: "EventLog", session_id: str) -> str | None:
    """The dynamic END-of-prompt notice for admissions and renames, or ``None``.

    Reads the folded roster (never writes) and names only the members whose
    existence the *frozen* prefix cannot already know: members admitted during
    play, and members that have been renamed. The seed-and-a-wrong-name case is
    exactly the ADDENDUM 1 correction channel — the prefix stays byte-identical
    while the model is told the new name at the END of the prompt.
    """
    members = roster(log, session_id)
    if not members:
        return None
    admitted = [
        member
        for member in members
        if member.is_active and member.admitted_by in RUNTIME_ADMITTERS
    ]
    renamed = [
        member
        for member in members
        if member.is_active and member.former_names and member.display_name
    ]
    lines: list[str] = []
    if admitted:
        listed = ", ".join(f'"{member.display_name}"' for member in admitted)
        lines.append(
            f"New cast in this session: {listed}. These characters are already "
            "present; refer to each by exactly the name given and invent no "
            "alternative names for them."
        )
    for member in renamed:
        previous = member.former_names[-1]
        lines.append(
            f'The character you know as "{previous}" is {member.display_name}. '
            f'Use "{member.display_name}" from now on.'
        )
    if not lines:
        return None
    return "Cast update — " + " ".join(lines)
