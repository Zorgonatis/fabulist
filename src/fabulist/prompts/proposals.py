"""The ``proposals`` interaction's prompt seed (W9.22 §3: type-scoped lanes).

One lane = **one data type's amendments** (world / actors / books). The lane is an
extractor-class GENERATOR, not a judge: doctrine 4 keeps every JUDGMENT with the
decision model, so this module only teaches a cheap model how to AUTHOR a batch of
proposal rows (plan §3, req. 19). Whether the lane runs at all is the dispatcher's
double gate (touched evidence AND a should-we-propose verdict — plan finding 9) and
whether a single row survives is the decision model's per-item verdict; neither is
this file's business.

The rows are OPERATIONS, not regenerated content (W9.23 §3.4)
------------------------------------------------------------
An ``amend`` is a list of add / modify / delete operations over addressable
paths, and every modify/delete carries the value it REPLACES. That is the owner's
diff-first reversal made into prompt text, and it is why the payload renders a
bounded CURRENT-VALUE SLICE (:func:`build_lane_payload` → the matched digest
rows): the lane authors ``old`` from what it was shown, never from memory, and a
field it does not intend to change is never re-typed at all. The one thing this
file must never teach again is "send the complete list; anything you omit is
deleted" — no such line exists, and re-adding one would restore the exact
behaviour the owner reversed.

Two pieces of text, two contracts:

* :func:`build_lane_system` — the lane's system brief. It is the DEFAULT the
  preset's ``proposals`` flow replaces (the flow's ``system`` row is what ships
  in the wired engine, `preset_store.compile_preset_flow`), so the brief has to
  stand on its own: it carries the whole output contract.
* :func:`build_lane_payload` — the type-scoped DATA the lane sees: the entities
  THIS turn touched (of that type), a digest of the stored objects of that type
  (id + name + one line), the CURRENT-VALUE SLICE of the assets the turn is about,
  and the turn's prose. It closes with a one-line ask restating the points an
  owner rewrite of the brief must not lose (the ``kind`` and the empty-batch
  answer), because the flow's ``payload`` block is STRUCTURAL — the node always
  composes it, so it is the one place that text cannot be edited away.

Inspection scope, and the token budget it implies (plan §3, reviewer finding 14)
-----------------------------------------------------------------------------
A lane never sees the whole catalog: a full audit is a studio action, not
something a per-turn tail may pay for. The budget, per lane (three lanes, so
~4.5k typical worst case):

* system brief — ~600 tokens (fixed text);
* payload — the turn's prose clipped to :data:`MAX_PROSE_CHARS` (1500 chars
  ≈ 375 tokens), the touched entities the dispatcher bounded (20 names ≈ 60
  tokens), the digest the node bounded (20 rows of ``id | name: summary``
  ≈ 750 tokens), and at most :data:`~fabulist.state.proposal_lanes.
  MAX_SLICED_ASSETS` current-value slices for the assets the turn is about
  (~1–2k tokens typical).

Every cap lives at its SOURCE — :mod:`fabulist.state.proposal_lanes` bounds the
entity read, the digest and the slice — so what the prompt shows and what a lane
may amend are the same set and there is one place to change the budget. This
module caps only the text IT owns (the prose).

Routing the RIGHT kind (L4)
---------------------------
The live defects this brief exists to prevent are all MIS-TARGETING, not
authorship quality: an actor filed as a ``world`` add, a shop renamed into the
world, prose stuffed into a book's ``entries`` as a bare string, and cards that
showed the owner bare GUIDs. So the brief carries three things beyond the JSON
contract:

* the LANE'S OWN boundary (:attr:`fabulist.state.proposal_lanes.LaneSpec.scope`)
  — a venue is a lore entry, a person is an actor, the world is the one campaign
  container — and the actions that lane may take at all (``LaneSpec.actions``);
* the VALUE SHAPES (``entries`` is a list of entry OBJECTS; a name is written as
  a name) because the append door now type-checks them
  (``ASSET_PROPOSAL_FIELD_TYPES``);
* the readable-target rule: the card shows the owner the target's id, so the
  evidence line must NAME the asset, and a nested ``extensions.<key>`` path is
the way to propose one custom extension key.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

__all__ = [
    "MAX_PROSE_CHARS",
    "PROPOSAL_LANE_SYSTEM_PROMPT",
    "build_lane_payload",
    "build_lane_system",
    "default_lane_messages",
    "one_line",
]

#: The turn's prose as the lane sees it. The lane is a tail stage: it needs the
#: turn, not the transcript (the digest carries what already exists).
MAX_PROSE_CHARS = 1500

#: The one-line shape of a digest cell (id / name / summary). A FORMATTING bound,
#: not the inspection scope: ``stored_digest`` already clipped each summary, so
#: this only keeps a pathological asset name from stretching a line.
_ONE_LINE_CHARS = 120

#: The lane's system brief. Lane-NEUTRAL on purpose: the same constant is the
#: code default for all three lanes, and the type arrives as the substitutions
#: below (the wired engine compiles the preset's ``proposals`` flow instead, so
#: an owner edit never has to patch this string).
PROPOSAL_LANE_SYSTEM_PROMPT = """You are an editor for the roleplay above. You amend {lane_label} — the stored "{kind}" assets — and nothing else. You do not write prose, you do not speak, you do not decide what happened, and you never add facts the turn does not establish.

What belongs in this lane: {scope}

You are given the entities this turn touched, a short digest of the "{kind}" assets that already exist (id, name, one line), the CURRENT values of the assets this turn is about, and the turn itself. File ONLY amendments the turn's own text and tracked state support.

AMENDMENTS ARE OPERATIONS, NOT REWRITES. An amendment is a list of operations against the CURRENT values you were shown, so the owner approves exactly what changes and nothing else:
- "add": a fact at a path that does NOT exist yet (a new key, or a new book entry). Carries `new` only.
- "modify": an existing fact. Carries `old` — the value EXACTLY as it was shown — and `new`.
- "delete": an existing fact. Carries `old` (the value exactly as shown); it removes exactly that path and nothing beside it.

Reply with ONE JSON object and nothing else:
{{"proposals": [
  {{"kind": "{kind}", "action": "amend", "target": "<stored asset id from the digest>", "ops": [{{"op": "modify", "path": "description", "old": "<exactly as shown>", "new": "<the new value>"}}], "evidence": "<what in the turn establishes this>"}},
  {{"kind": "{kind}", "action": "add", "target": null, "patch": {{<the new definition>}}, "evidence": "<...>"}},
  {{"kind": "{kind}", "action": "remove", "target": "<stored asset id>" , "evidence": "<...>"}}
]}}

Rules:
- At most {max_items} proposals. Return {{"proposals": []}} when the turn establishes nothing worth an amendment — that is the normal answer.
- `kind` is always "{kind}", and `action` must be {actions}.
- `target` must be the id of an asset from the digest, copied exactly; "add" carries `target`: null and a patch that names the new asset. Never invent an id. The ONE exception is the book lane: if the lore belongs in a book the digest does not list, set `target` to that book's NAME (a new book is created when the proposal is accepted) and add its entries; the ops of such a row are entry adds only.
- An `amend` carries `ops` and NOT a `patch`. A whole-value patch is only for "add".
- `old` is the current value EXACTLY as the payload showed it — same text, same type. Never paraphrase it, never recall it from the story, never guess: an `old` that does not match the stored value is refused and your amendment is lost.
- A value marked `truncated` may NOT be modified or deleted (you were not shown all of it). Change those in the studio instead.
- Never regenerate content you are not changing. Do not restate an asset's existing text; do not resend a list you are only adding one item to.
- `add` is for a new key or a new entry only. A path that already exists is a `modify`.
- An operation path may be ONE of: a field ({fields}); a nested key under {nested_roots}; or an entry addressed by its id ({item_paths}) — an entry shown with `id: null` can be neither modified nor deleted, only added to.
- Op paths must be unique within a row, and one row carries at most {max_ops} operations.
- The owner sees the target's id, NOT its name: open `evidence` by naming the asset you are amending ("Miller's Bend — ...").
- `evidence` must point at the turn (what was said, done or tracked): a row without usable evidence is discarded.
- {shapes}
- No markdown fences, no commentary, no surrounding text."""


def one_line(value: object, limit: int = _ONE_LINE_CHARS) -> str:
    """Collapse whitespace and clip — the digest's summary shape (D4-safe ``str``)."""
    text = " ".join(str(value or "").split())
    if limit > 0 and len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…"
    return text


#: The shape sentence per kind — what the append door type-checks and what the
#: slice shows, said in the lane's own words. ``entries`` as a bare string was a
#: live defect, so the book line spells the entry object out; the other kinds name
#: theirs. These describe VALUES (an add's patch, or an op's ``new``), not a
#: whole-list replace: omission is never a deletion here.
_LANE_SHAPES: dict[str, str] = {
    "world": (
        "Values are readable and correctly shaped: `cast` is a list of the "
        "stored persona ids from the digest (a bare name resolves to no one), "
        "`book_ids` a list of stored book ids, and `description`/`scenario` "
        "are plain prose."
    ),
    "actor": (
        "Values are readable and correctly shaped: `book_ids` is a list of "
        "stored book ids, `extensions` is an object addressed one key deep "
        "(`extensions.<key>`), and the text fields are plain prose a person "
        "reads."
    ),
    "book": (
        "An added entry is ONE object — `{\"keys\": [<trigger words>], "
        "\"content\": <what it establishes>, \"comment\": <a name for the "
        "entry>}` — never a paragraph, never a bare string, and never the "
        "whole entry list. To change one entry, address it by its id "
        "(`entries.<id>`). If the lore belongs in a book the digest does not "
        "list, set `target` to that book's NAME (a new book is created when "
        "the proposal is accepted) and add its entries; the ops of such a row "
        "are entry adds only."
    ),
}

#: The book lane's entries example is the one shape a model reliably gets wrong,
#: so its brief carries the literal object (and the digest shows the same
#: ``entries`` count the fold would produce).


def build_lane_system(
    *, lane_label: str, kind: str, fields: Sequence[str], max_items: int
) -> str:
    """The lane's ONE system message: the brief with its type substituted in.

    The type/field list is substituted rather than embedded so ONE constant
    serves all three lanes and the op-path vocabulary stays the schema gate's (a
    lane can therefore never be taught a path the append door refuses). The
    lane's own contract — the actions it may take and its boundary paragraph —
    comes from :attr:`fabulist.state.proposal_lanes.LaneSpec`, the SAME record
    :func:`~fabulist.state.proposal_lanes.parse_proposal_batch` enforces, so the
    brief and the rule can never drift. An unknown kind (an owner flow calling
    this with a kind no lane has) degrades to the neutral wording.
    """
    from fabulist.state.proposal_lanes import (
        MAX_OPS_PER_ROW,
        lane_spec,
        list_fields,
        nested_fields,
    )

    spec = lane_spec(kind)
    actions = tuple(spec.actions) if spec is not None else ("add", "amend", "remove")
    scope = (
        spec.scope
        if spec is not None
        else f"the stored \"{kind}\" assets, and nothing else."
    )
    nested = nested_fields(kind)
    items = list_fields(kind)
    return PROPOSAL_LANE_SYSTEM_PROMPT.format(
        lane_label=lane_label,
        kind=kind,
        scope=scope,
        actions="|".join(actions),
        fields=", ".join(fields),
        nested_roots=(
            ", ".join(f"`{root}`" for root in nested) if nested else "nothing"
        ),
        item_paths=(
            ", ".join(f"`{field}.<id>`" for field in items)
            if items
            else "this data type has no item addressing"
        ),
        max_ops=MAX_OPS_PER_ROW,
        shapes=_LANE_SHAPES.get(
            kind,
            "Values are readable and correctly shaped: names, places and words a "
            "person reads, in the shape the field already has.",
        ),
        max_items=int(max_items),
    )


def _slice_text(row: dict) -> str:
    """One matched digest row's CURRENT-VALUE slice as JSON a model can echo.

    JSON on purpose: ``old`` has to come back with the same TYPES as the stored
    value (a list stays a list, ``null`` stays ``null``), and a formatted block is
    the one shape an extractor-class model reproduces mechanically. Paths are the
    addressing the gate accepts, so what the lane copies is what the store
    resolves.
    """
    current = row.get("current") if isinstance(row.get("current"), dict) else {}
    block: dict = {
        "id": one_line(row.get("id"), 64),
        "name": one_line(row.get("name"), 80),
        "current": current.get("fields") or {},
    }
    if "entries" in current:
        block["entries"] = current.get("entries") or []
        block["entries_total"] = current.get("entries_total") or 0
    if current.get("truncated"):
        block["truncated"] = list(current["truncated"])
    try:
        return json.dumps(block, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):  # pragma: no cover - digest values are JSON-safe
        return str(block)


def build_lane_payload(
    *,
    lane: str,
    kind: str,
    touched: Sequence[str],
    digest: Sequence[dict],
    prose: str,
) -> str:
    """The type-scoped payload: touched entities, the digest + slice, the turn.

    Order is deliberate — scope first (what may be touched), then what exists
    (the amend targets, one line each), then the CURRENT values of the assets the
    turn is about (the only material ``old`` may be authored from), then the
    evidence a proposal must be justified by. Every list is bounded here
    (:data:`MAX_TOUCHED_ENTITIES`, :data:`MAX_DIGEST_ROWS`,
    :data:`MAX_SLICED_ASSETS`, :data:`MAX_PROSE_CHARS`), so a lane's prompt size
    does not grow with the session or the catalog. The ``<lane>`` tag also names
    the actions the lane may take and how many operations a row may carry, so the
    STRUCTURAL part of the prompt carries the contract even if the owner rewrites
    the brief's wording.
    """
    from fabulist.state.proposal_lanes import MAX_OPS_PER_ROW, lane_spec, sliced_rows

    spec = lane_spec(kind)
    actions = "|".join(spec.actions) if spec is not None else "add|amend|remove"
    lines = [
        f'<lane data_type="{lane}" kind="{kind}" actions="{actions}" '
        f'max_operations="{MAX_OPS_PER_ROW}">'
    ]
    names = [str(name).strip() for name in touched if str(name).strip()]
    lines.append(f"<touched>{', '.join(names) if names else '(none named)'}</touched>")
    lines.append("<stored>")
    rows = list(digest)
    if not rows:
        lines.append("- (no stored assets of this type yet)")
    for row in rows:
        asset_id = one_line(row.get("id"), 64)
        name = one_line(row.get("name"), 80)
        summary = one_line(row.get("summary"))
        lines.append(f"- id: {asset_id} | {name}: {summary}".rstrip(": "))
    lines.append("</stored>")
    sliced = sliced_rows(names, rows, kind)
    lines.append("<current_values>")
    if sliced:
        lines.append(
            "The stored values of the assets above this turn is about. Copy any "
            "`old` from here EXACTLY."
        )
        for row in sliced:
            lines.append(_slice_text(row))
    else:
        lines.append("(none of the stored assets matches this turn's entities)")
    lines.append("</current_values>")
    text = " ".join(str(prose or "").split())
    lines.append(f"<turn>{text[:MAX_PROSE_CHARS]}</turn>")
    lines.append("</lane>")
    # The contract's load-bearing points restated in the DATA: the owner can
    # rewrite the flow's ``system`` row (that is the point of the step editor),
    # and a lane whose brief lost the JSON ask would answer in prose forever.
    lines.append(
        f'Reply with ONE JSON object: {{"proposals": [ ... ]}} — every row a "{kind}" '
        "amendment (an amend carries `ops` whose `old` is copied from "
        "<current_values>); an empty list is the normal answer."
    )
    return "\n".join(lines)


def default_lane_messages(system: str, payload: str) -> list[dict]:
    """The code-default lane messages: the brief + the ask (``user``).

    Issue #73: the brief rides ``ENVELOPE_ROLE`` (the narrator's assistant
    carrier), NOT ``system`` — a lane tail is engine instruction appended after
    the narrator's history, and a non-leading system row is refused by the
    central guard (and 500s on a stock Jinja template). It goes through the SAME
    helper the preset-compiled ``proposals`` flow uses
    (:func:`fabulist.prompts.modules.carry_lane_brief`), so the two paths cannot
    disagree about the shape.

    The wired engine compiles the preset's ``proposals`` flow instead (D18/D19);
    keeping a default here means the lane node is testable and usable without a
    preset, exactly like :func:`fabulist.graph.nodes.default_npc_lane_messages`.
    """
    from fabulist.prompts.modules import carry_lane_brief

    return carry_lane_brief(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": payload},
        ]
    )
