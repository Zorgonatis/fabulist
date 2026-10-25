"""Template variable registry + resolver for the prompt-module engine (D18).

The prompt-module engine (:mod:`fabulist.prompts.modules`) turns the narrator
pipeline into data: ordered :class:`~fabulist.prompts.modules.PromptModule`
rows whose structural slots are filled by the engine at build time. Text
modules carry a template body that may reference SillyTavern-familiar
``{{name}}`` variables; this module owns the *fixed* variable vocabulary and
resolves it from the event log (D9 — the log owns everything).

Two seams:

* :data:`VARIABLE_SPECS` — the frozen catalog of variable names, with a human
  description and an example for the editor UI. A prompt engineer authors
  against exactly these names.
* :func:`resolve_variables` — folds the log/session into a ``{name: str}`` map
  for one turn. Every spec resolves to a string (``""`` when absent) so
  ``render_template`` never drops a known variable. NOT pure: the first resolve
  of ``char`` memoises a one-time ``cast_snapshot`` append (D43) — see below.

Seams this builds on (cited):

* ``char`` (D43) — the session's cast rendered ``[cast: a, b, c]`` (all actors
  in declaration order), via
  :func:`fabulist.context.roster.extract_cards` over the session's raw
  ``system`` card events (same read as
  :func:`fabulist.context.facts.session_persona_block`). The VALUE is **frozen
  at first consumption**: :func:`fabulist.prompts.cast.ensure_cast_snapshot`
  evaluates it once, appends a ``cast_snapshot`` system event, and every later
  turn reads that stored string verbatim. The freeze is deliberate prompt-cache
  protection — ``{{char}}`` feeds the static prefix. Studio edits that change a
  session's cast surface through :func:`fabulist.prompts.cast.cast_delta_notice`
  in the dynamic end region, never by thawing this value.
* ``user`` / ``message`` (D43) — ST semantics: ``user`` is the selected user
  persona NAME (the same ``chat_persona`` -> ``__personas__`` fold as
  ``persona``, live per turn — a deliberate reassignment takes effect); the
  current message text moves to ``message``.
* ``world`` (D43) — the session's latest ``world_ref`` snapshot name (D32
  snapshot-at-creation), ``""`` for a plain chat.
* ``persona`` — read **directly** from ``system`` events (the personas registry
  is intentionally not imported): the chat's latest-wins ``chat_persona`` row
  names a ``persona_id``, which is then resolved against the
  ``__personas__`` anchor's latest-wins ``persona`` definitions. Same fold
  shape as :func:`fabulist.personas.registry.persona_for_chat` /
  :func:`~fabulist.personas.registry.list_personas`, reimplemented here so
  this module does not import :mod:`fabulist.personas`. When the chat has no
  chat-local assignment has **never been recorded**, the pinned global default
  persona supplies the id (read via ``state.settings.default_persona_id``, a
  lazy import) — the same read-time fallback the registry applies. An explicit
  clear (``{"cleared": true}`` on a ``chat_persona`` row) suppresses the
  fallback: a cleared chat stays persona-less. When no persona is in effect,
  ``{{user}}`` resolves to the literal ``user`` (PART 2) — never the raw token.
* ``last_user_turn`` / ``last_narrative`` / ``state`` scalars / ``history_count``
  / ``turns`` — :meth:`fabulist.log.EventLog.get_projection`.
* ``facts`` / ``wi`` — :func:`fabulist.context.facts.facts_suffix_parts` +
  :func:`~fabulist.context.facts.join_facts_suffix` and
  :func:`~fabulist.context.facts.session_world_info_block`, as in
  :func:`fabulist.wiring.make_narrator_message_builder.build`.
* ``state`` line — :func:`fabulist.graph.clock.render_facts`.
* ``library`` — :func:`fabulist.prompts.library_mini.render_library_doc`.
* ``library_doc`` (D35) — the same generated doc, resolved through
  :func:`fabulist.prompts.library_mini.resolve_library_doc` (artifact with
  a missing one raises). This is the target of the factory default's
  ``library_doc`` surface expression ``{{library_doc}}``: the surface seeds the
  *expression* rather than a baked copy so generator updates reach live DBs
  without a manual owner PUT. Unlike D18's unknown-token rule, this name ALWAYS
  resolves (never left literal) — it is a registered variable.
* ``recall`` — always ``""`` here: a live vector-memory query is out of scope
  for a pure log resolver (documented; callers may override via ``extra``).

``render_template`` is plain string substitution — no code execution, ever.
Unknown ``{{...}}`` tokens are left **literal** so a prompt engineer sees an
unresolved variable rather than a silent drop.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from fabulist.context.facts import (
    facts_suffix_parts,
    join_facts_suffix,
    persona_identity,
    session_world_info_block,
)
from fabulist.graph.clock import render_facts
from fabulist.media.images import is_image_narrative
from fabulist.prompts.cast import ensure_cast_snapshot
from fabulist.prompts.library_mini import render_library_doc, resolve_library_doc

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

__all__ = [
    "PERSONAS_ANCHOR",
    "VARIABLE_SPECS",
    "VariableSpec",
    "render_template",
    "resolve_variables",
]

#: Reserved anchor session carrying persona definitions (D16). Duplicated as a
#: literal so this module never imports :mod:`fabulist.personas`; the fold shape
#: mirrors :data:`fabulist.personas.registry.PERSONAS_ANCHOR` exactly.
PERSONAS_ANCHOR = "__personas__"

#: ``{{ name }}`` / ``{{name}}`` — whitespace-insensitive inside the braces.
#: Braces are excluded from the captured name so nested/garbage tokens stay put.
_VAR_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")


@dataclass(frozen=True)
class VariableSpec:
    """One resolvable ``{{name}}`` variable: stable name, description, example."""

    name: str
    description: str
    example: str


#: The fixed variable vocabulary (editor UI reads this). Every name resolves in
#: :func:`resolve_variables` — absent data yields ``""``.
VARIABLE_SPECS: tuple[VariableSpec, ...] = (
    VariableSpec(
        "char",
        "Session cast, snapshotted at first consumption (D43)",
        "[cast: Lyra Voss, Corin Hale]",
    ),
    VariableSpec(
        "world",
        "World name from the session's world snapshot (empty for a plain chat)",
        "The Rain-Soaked Market",
    ),
    VariableSpec("persona", "Parent persona name, or empty for a sandbox chat", "Alice"),
    VariableSpec(
        "user",
        "User persona name, or the literal 'user' when none is in effect (D43)",
        "Alice",
    ),
    VariableSpec("message", "Current user message text (D43)", "I open the door."),
    VariableSpec(
        "last_user_turn", "Previous turn's user message text", "I knock twice."
    ),
    VariableSpec(
        "last_narrative", "Previous turn's narrative prose", "The door creaks open."
    ),
    VariableSpec("state", "Merged state as one pipe-YAML facts line", "time: 14:35 | loc: market"),
    VariableSpec("facts", "Dynamic per-turn state suffix (roster/audio/rolls/clock)", "time: 14:35 | loc: night market"),
    VariableSpec("recall", "Vector-recall block (empty: live query out of scope)", ""),
    VariableSpec("wi", "Active world-info block", "### World info\n- The market closes at dusk."),
    VariableSpec("library", "Rendered OpenUI component reference doc", "### Component reference"),
    VariableSpec(
        "library_doc",
        "Generated OpenUI component reference doc (D35 expression target)",
        "### Component reference",
    ),
    VariableSpec("time", "In-world time from state", "14:35"),
    VariableSpec("loc", "Location from state", "night market"),
    VariableSpec("weather", "Weather from state", "light rain"),
    VariableSpec("tone", "Scene tone from state", "warm"),
    VariableSpec("inv", "Inventory scalars from state", "brass compass"),
    VariableSpec("history_count", "Number of history messages entering the prompt", "6"),
    VariableSpec("now", "Wall-clock timestamp (UTC ISO 8601)", "2024-01-01T12:00:00+00:00"),
    VariableSpec("turns", "Number of completed narrator turns", "3"),
)


def _last_user_before(projection: dict, user_text: str) -> str:
    """The previous user message text, excluding the *current* ``user_text``.

    When the current message is already in the log (its text matches the last
    projection row) the one before it wins; otherwise the last row is the
    previous turn. No prior user turn -> ``""``.
    """
    texts = [
        str(item.get("payload", {}).get("text", ""))
        for item in projection.get("user_messages", [])
    ]
    if not texts:
        return ""
    if texts[-1] == user_text and len(texts) >= 2:
        return texts[-2]
    if texts[-1] == user_text:
        return ""
    return texts[-1]


def _persona_name(log: "EventLog", session_id: str) -> str:
    """Effective persona name for the chat, or ``""``.

    Thin delegate to :func:`fabulist.context.facts.persona_identity`, which owns
    the registry fold (the chat's ``chat_persona`` assignment, fork-aware, else
    the pinned global default; an explicit clear suppresses the pin) and the
    ``__personas__`` catalog lookup. Keeping ONE fold means the ``{{persona}}`` /
    ``{{user}}`` macro and the D54 narrator identity block can never name
    different personas.
    """
    return persona_identity(log, session_id)[0]


def _world_name(log: "EventLog", session_id: str) -> str:
    """Latest ``world_ref`` snapshot's world name, or ``""`` (D32/D43).

    A world-materialized chat records a snapshot-at-creation
    ``system``/``world_ref`` event; latest wins. A plain session carries none
    and resolves to the empty string. Read fork-aware
    (:meth:`EventLog.get_chain_events`) so a branch inherits its parent's world.
    """
    name = ""
    for event in log.get_chain_events(session_id):
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        if event.payload.get("event") != "world_ref":
            continue
        name = str(event.payload.get("name") or "")
    return name


def resolve_variables(
    log: "EventLog",
    session_id: str,
    user_text: str,
    extra: dict | None = None,
) -> dict[str, str]:
    """Resolve every :data:`VARIABLE_SPECS` name for one turn from the log.

    ``user_text`` is the current (not-yet-narrated) user message; it seeds
    ``message`` and the world-info/facts scan strings. ``extra`` is folded last
    so a caller can inject values this resolver cannot derive (e.g. a live
    ``recall`` block). All values are strings — an absent source yields ``""``.

    This resolver is NOT pure: ``char`` (D43) is memoised by
    :func:`fabulist.prompts.cast.ensure_cast_snapshot`, which appends one
    ``cast_snapshot`` event on first consumption. The write is idempotent, so
    repeated calls in a turn (and every later turn) append nothing further.

    Cites: ``char`` via :func:`fabulist.prompts.cast.ensure_cast_snapshot`
    (over :func:`fabulist.context.roster.extract_cards`); ``user`` / ``persona``
    via a direct ``chat_persona`` / ``__personas__`` event fold (``user`` falls
    back to the literal ``user`` when no persona is in effect); ``world`` via
    the latest ``world_ref`` snapshot; ``last_user_turn`` / ``last_narrative`` /
    state scalars / counts via :meth:`fabulist.log.EventLog.get_projection`;
    ``facts`` / ``wi`` via :mod:`fabulist.context.facts`; ``library`` via
    :func:`fabulist.prompts.library_mini.render_library_doc` and ``library_doc``
    via :func:`fabulist.prompts.library_mini.resolve_library_doc` (D35).
    """
    projection = log.get_projection(session_id)
    narratives = projection.get("narratives", [])
    state = projection.get("state") or {}

    # D43: the cast value is frozen at first consumption (memoised write).
    char = ensure_cast_snapshot(log, session_id)
    persona = _persona_name(log, session_id)

    # A generated image is appended as a ``narrative`` row too (imagegen), but it
    # is a canon image carrying no prose, not a turn. Every prose/turn reader
    # below works on REAL narratives only, so an image never shifts
    # ``{{turns}}`` / ``{{last_narrative}}`` or seeds the world-info/facts scan.
    real_narratives = [item for item in narratives if not is_image_narrative(item)]

    last_narrative = ""
    if real_narratives:
        last_narrative = str(
            real_narratives[-1].get("payload", {}).get("prose", "")
        )

    # World-info + facts scans use the wiring shape: current user text plus the
    # last two narratives' prose joined by a space (facts.py docstring).
    recent = " ".join(
        str(item.get("payload", {}).get("prose", ""))
        for item in real_narratives[-2:]
    )
    scan = f"{user_text}\n{recent}"

    state_line = (
        render_facts({str(k): str(v) for k, v in state.items()}) if state else ""
    )
    suffix = join_facts_suffix(
        facts_suffix_parts(log, session_id, user_text=scan, facts=state_line)
    )
    wi = session_world_info_block(log, session_id, scan) or ""

    resolved: dict[str, str] = {
        "char": char,
        "world": _world_name(log, session_id),
        "persona": persona,
        # PART 2: no persona in effect -> the literal word ``user`` (never an
        # empty substitution or the raw ``{{user}}`` token).
        "user": persona or "user",
        "message": str(user_text),
        "last_user_turn": _last_user_before(projection, user_text),
        "last_narrative": last_narrative,
        "state": state_line,
        "facts": suffix,
        "recall": "",
        "wi": wi,
        "library": render_library_doc(),
        # D35: the factory library-doc surface seeds `{{library_doc}}`; this is
        # the value that resolves it (generated artifact; missing raises).
        "library_doc": resolve_library_doc(),
        "time": str(state.get("time", "")),
        "loc": str(state.get("loc", "")),
        "weather": str(state.get("weather", "")),
        "tone": str(state.get("tone", "")),
        "inv": str(state.get("inv", "")),
        "history_count": str(
            len(projection.get("user_messages", [])) + len(real_narratives)
        ),
        "now": datetime.now(timezone.utc).isoformat(),
        "turns": str(len(real_narratives)),
    }
    if extra:
        resolved.update({str(k): str(v) for k, v in extra.items()})
    return resolved


def render_template(content: str, variables: dict[str, str]) -> str:
    """Substitute ``{{name}}`` tokens in ``content`` from ``variables``.

    Whitespace inside the braces is ignored (``{{ name }}`` == ``{{name}}``).
    Only names present in ``variables`` are replaced; every other ``{{...}}``
    token is left **literal** (verbatim, spacing intact) so an unresolved
    variable is visible to the prompt engineer rather than silently dropped.
    This is plain string substitution — no evaluation, no code execution.
    """

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1).strip()
        if name in variables:
            return variables[name]
        return match.group(0)

    return _VAR_RE.sub(_sub, content)
