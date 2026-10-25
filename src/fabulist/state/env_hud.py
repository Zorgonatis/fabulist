"""Environment HUD statement (Lane 1f — Diagram Ratification Wave).

The env HUD is the persistent in-fiction station: in-fiction time, date,
location, weather, and the short weather outlook. Owner fork decision (FINAL):
the HUD travels as an **OpenUI DSL statement**, engine-assembled — not typed
JSON. The engine owns the values (clock + BTS) and the frontend owns the render,
so the statement is the one seam between them.

Fixed statement contract (do not vary)::

    hud = EnvTracker("14:35", "Monday, June 02, 2025", "night market", "light rain", "clearing")

Five positional string args in order ``(time, date, loc, weather, forecast)``,
fixed ident ``hud``. Empty/unknown values are empty strings (``""``) and are
**never omitted** — the frontend component degrades gracefully, so a missing
value must still occupy its slot. The OpenUI parser is positional-only; named
args are invalid by design, which is why this is a straight five-arg call.

String rule (matched against ``@openuidev/lang-core``'s tokenizer): a
double-quoted literal is ``JSON.parse``-ed, falling back to a quote-strip when
that fails. :func:`json.dumps` therefore produces literals the parser accepts
byte-for-byte (``"`` -> ``\\"``, ``\\`` -> ``\\\\``, newline -> ``\\n``); the
back-parser reads them back with :func:`json.loads`.

What this module owns
---------------------
* :func:`env_hud_statement` — assemble the five values, with the forecast
  fallback ladder (manual override -> the tracker's forecast -> prior statement's
  forecast -> steady = current weather). The manual override is the flat fold's
  ``forecast`` key (a human ``state-delta`` row, D45); the function stays pure
  and reads it from the ``clock_facts`` mapping it is already handed. Rung 2 is
  the tracker's own forecast, handed in as the ``bts_result`` slot: since W9.29
  §8 that is the registry's scene-trait ``forecast`` (the env_hud node passes it
  as the retired ``bts_result`` field's successor), which is exactly why
  :func:`fabulist.state.entities.flat_render` omits the key — the pin and the
  model's trait must never share a slot (review R2).
* :func:`parse_env_hud_statement` — tolerant back-parser for a prior statement
  (``None`` on anything unrecognisable).
* :func:`latest_env_hud` — fork-aware, latest-wins read of ``system``/``env_hud``
  events.
* :func:`env_hud_statement_from_fold` — legacy degradation for sessions whose log
  predates the HUD event: a bare state fold (``time``/``loc``/``weather``) still
  renders a reduced station (``date``/``forecast`` empty).

Event emission is orchestrator wiring (the per-turn append in the graph's
``post_turn``); this module only exposes the functions. The event shape is
``system`` actor, payload ``{"event": "env_hud", "statement": "<stmt>"}``.
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = [
    "ENV_HUD_EVENT",
    "ENV_HUD_IDENT",
    "HUD_PHRASE_MAX",
    "env_hud_statement",
    "env_hud_statement_from_fold",
    "latest_env_hud",
    "parse_env_hud_statement",
]

#: The statement ident (the frontend parser matches on the call name).
ENV_HUD_IDENT = "hud"

#: System-event kind carrying a HUD write (latest wins).
ENV_HUD_EVENT = "env_hud"

#: The five positional args, in wire order.
_ENV_HUD_KEYS: tuple[str, ...] = ("time", "date", "loc", "weather", "forecast")

#: Locates the ``EnvTracker(`` call inside a statement (ident is not matched —
#: we wrote the format, and a re-idented statement still back-parses).
_ENV_HUD_CALL_RE = re.compile(r"\bEnvTracker\s*\(")

#: Boring defensive cap on the two PROSE-ish HUD slots (weather / forecast). The
#: tracker is asked for a few words, but a live turn answered with a whole
#: sentence (~120 characters of temperature, drizzle and haze), and a value that
#: long breaks the station's single-line layout. The cap is the HUD's own
#: guarantee: whatever the model writes, the chip cannot grow past this. Word
#: boundary truncation, and an ellipsis so a clipped value never reads as
#: complete.
HUD_PHRASE_MAX = 48


def _phrase(value: Any, limit: int = HUD_PHRASE_MAX) -> str:
    """A HUD-sized phrase: whitespace-collapsed, word-boundary truncated."""
    text = " ".join(_text(value).split())
    if len(text) <= limit:
        return text
    head = text[:limit].rstrip()
    cut = head.rsplit(" ", 1)[0].rstrip(",;:") if " " in head else head
    return f"{cut or head}…"


def _quote(value: str) -> str:
    """A double-quoted string literal the OpenUI parser's JSON path accepts."""
    return json.dumps(value, ensure_ascii=False)


def _text(value: Any) -> str:
    """Coerce a fact value to a stripped string (``""`` for ``None``)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _format_statement(time: str, date: str, loc: str, weather: str, forecast: str) -> str:
    """Render the fixed ``hud = EnvTracker(...)`` statement (five args)."""
    args = ", ".join(_quote(value) for value in (time, date, loc, weather, forecast))
    return f"{ENV_HUD_IDENT} = EnvTracker({args})"


def _bts_forecast(bts_result_or_none: Any) -> str:
    """The tracker's forecast field, tolerant of a dataclass or a plain mapping.

    ``getattr`` first (a :class:`~fabulist.state.bts.BtsResult` carries
    ``forecast: str``); a dict form (the env_hud node's registry-derived
    ``{"forecast": ...}``, or ``dataclasses.asdict``) is read with ``.get``.
    Missing/unknown -> ``""`` so the ladder falls through.
    """
    if bts_result_or_none is None:
        return ""
    value = getattr(bts_result_or_none, "forecast", None)
    if value is None and isinstance(bts_result_or_none, dict):
        value = bts_result_or_none.get("forecast")
    return _text(value)


def env_hud_statement(
    clock_facts: dict,
    bts_result_or_none: Any = None,
    prior_statement: str | None = None,
) -> str:
    """Assemble the env HUD statement from this turn's clock + BTS results.

    ``clock_facts`` is the folded facts line's dict (``time``/``date``/``loc``/
    ``weather``/``tone``/``inv``...). The forecast fallback ladder is:

    1. the manual override — the fold's ``forecast`` key, written by a human
       ``state-delta`` edit (D45); an absent or empty value is no override;
    2. the tracker's forecast this turn (``getattr``/dict tolerant) — the
       registry's scene-trait ``forecast`` since W9.29 §8;
    3. the prior statement's forecast (tolerant back-parse);
    4. steady — the current weather.

    Missing values become ``""`` (never omitted). The returned string is the
    fixed five-arg ``EnvTracker`` statement.
    """
    facts = clock_facts if isinstance(clock_facts, dict) else {}
    time = _text(facts.get("time"))
    date = _text(facts.get("date"))
    loc = _text(facts.get("loc"))
    weather = _phrase(facts.get("weather"))

    # 1. Manual override (D45): a user-pinned forecast lives in the flat fold,
    #    so it rides ``clock_facts`` beside the other facts. Absent or empty is
    #    no override (D33), which falls through to the engine's own ladder.
    forecast = _phrase(facts.get("forecast"))
    if not forecast:
        # 2. The tracker's forecast this turn (the registry scene trait, §8).
        forecast = _phrase(_bts_forecast(bts_result_or_none))
    if not forecast:
        prior = parse_env_hud_statement(prior_statement)
        if prior is not None:
            forecast = _phrase(prior["forecast"])
    if not forecast:
        forecast = weather  # steady: no forecast is known, hold the weather

    return _format_statement(time, date, loc, weather, forecast)


def env_hud_statement_from_fold(fold: dict) -> str:
    """Degrade a bare state fold into a reduced HUD statement (legacy sessions).

    Old sessions predate the ``env_hud`` event but still have a folded ``time``/
    ``loc``/``weather`` in the projection. The ``date`` and ``forecast`` slots do
    not exist there, so they render empty (``""``) rather than being omitted —
    the frontend shows a reduced station.
    """
    facts = fold if isinstance(fold, dict) else {}
    return _format_statement(
        time=_text(facts.get("time")),
        date="",
        loc=_text(facts.get("loc")),
        weather=_phrase(facts.get("weather")),
        forecast="",
    )


def parse_env_hud_statement(statement: str | None) -> dict[str, str] | None:
    """Back-parse a prior ``EnvTracker`` statement into its five values.

    Tolerant by contract: anything that is not a well-formed five-string
    ``EnvTracker(...)`` call returns ``None`` (no exceptions), so a corrupt or
    foreign statement simply skips the ladder's prior-forecast rung.
    """
    if not isinstance(statement, str) or not statement.strip():
        return None
    match = _ENV_HUD_CALL_RE.search(statement)
    if match is None:
        return None
    args = _split_call_args(statement, match.end() - 1)
    if args is None or len(args) < len(_ENV_HUD_KEYS):
        return None
    values: list[str] = []
    for raw in args[: len(_ENV_HUD_KEYS)]:
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(value, str):
            return None
        values.append(value)
    return dict(zip(_ENV_HUD_KEYS, values))


def _split_call_args(text: str, open_index: int) -> list[str] | None:
    """Split the args of a call whose ``(`` is at ``open_index``.

    Quote- and escape-aware (a comma inside a string literal never splits), and
    nesting-aware for ``()``/``[]``/``{}``. Returns ``None`` when the call never
    closes, so :func:`parse_env_hud_statement` can treat it as garbage.
    """
    args: list[str] = []
    start = open_index + 1
    depth = 0
    in_string = False
    escape = False
    i = start
    n = len(text)
    while i < n:
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            i += 1
            continue
        if ch == '"':
            in_string = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                args.append(text[start:i])
                return [arg.strip() for arg in args]
            depth -= 1
        elif ch == "," and depth == 0:
            args.append(text[start:i])
            start = i + 1
        i += 1
    return None


# -- fork-aware log reads ----------------------------------------------------


def _resolve_events(log: Any, session_id: str) -> list[Any]:
    """Fork-resolved raw events for ``session_id`` (root-first, bounded).

    Mirrors the walk in :mod:`fabulist.state.bts` / ``cast_api`` using only
    public reads: a branch copies no events, so each ancestor is replayed
    root-first and bounded at its child's ``base_seq``. Implemented locally (not
    imported) so this lane never couples to a file another lane owns.
    """
    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = session_id
    while current is not None and current not in seen:
        seen.add(current)
        chain.append(current)
        info = log.get_session(current)
        current = info["base_session"] if info else None
    chain.reverse()

    events: list[Any] = []
    for index, sid in enumerate(chain):
        bound: int | None = None
        if index + 1 < len(chain):
            child = log.get_session(chain[index + 1])
            bound = child["base_seq"] if child else None
        for event in log.get_events(sid):
            if bound is not None and event.seq > bound:
                continue
            events.append(event)
    return events


def latest_env_hud(log: Any, session_id: str) -> str | None:
    """Latest ``system``/``env_hud`` statement for the session (fork-aware).

    Latest-wins across the fork-resolved event chain. Returns ``None`` when no
    well-formed statement exists yet (the API route then degrades the fold).
    """
    latest: str | None = None
    for event in _resolve_events(log, session_id):
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        if event.payload.get("event") != ENV_HUD_EVENT:
            continue
        statement = event.payload.get("statement")
        if isinstance(statement, str) and statement.strip():
            latest = statement
    return latest
