"""Assistant panel statement + fork-aware assistant log reads (D38 lane 2c).

An enabled assistant answers the per-turn consult with in-voice prose ending in
one OpenUI DSL statement::

    asst = AssistantCard(avatar, name, role, mood, commentary, options, footer)

Strictly positional (zod contract ``frontend/src/lib/openui/schemas/assistant.ts``).
This module owns the tolerant back-parser the engine uses to lift that statement
out of the raw reply and the serializer that re-emits it as the canonical,
definition-corrected statement the panel event carries.

Parser contract
---------------
* Fences are stripped first (``\\`\\`\\``` / ``\\`\\`\\`openui`` markers), so a fenced
  statement parses exactly like a bare one.
* Multiple ``asst = AssistantCard(`` statements -> the LAST wins.
* Scalars are JSON string literals (``"..."`` with ``\\"`` / ``\\\\`` escapes);
  options are ``{text, tag, note?, emphasis}`` objects with unquoted keys and
  ``true``/``false`` booleans.
* **Definition overrides** are applied after parsing: ``name`` always comes from
  the :class:`~fabulist.assistants.manager.AssistantDefinition`, and ``role`` is
  the fixed card subtitle :data:`ASSISTANT_CARD_ROLE`. Both discard whatever the
  model emitted, which is the anti-drift intent (the directive's ``"Sam"``/
  ``"Guide"`` placeholder copy is predicted drift). ``avatar`` keeps only an
  addressable value (``http(s)://`` / ``data:`` / path-shaped); a name — another
  predicted drift — is blanked so the frontend's initials fallback applies.
* An absent or malformed options list degrades to ``[]`` (the component renders
  no options). Anything structurally unparseable returns ``None`` so the caller
  can append the reply without a panel event — a bad statement is never fatal.
* Kwarg-form calls (``AssistantCard(name: "...")``) are structurally unsupported
  by the positional contract and return ``None`` deliberately (no coercion); a
  live probe reports whether that drift appears.

Log reads
---------
Event shapes (``system`` actor, the kind carried in the payload)::

    {"event": "assistant_panel",   "assistant": "<id>", "statement": "<stmt>"}
    {"event": "chat_assistant",    "assistant_id": "<id>" | null}

Both folds are fork-aware latest-wins (a branch inherits its parent's panel and
enablement up to the fork point), mirroring :mod:`fabulist.state.env_hud`. The
fork walk is implemented locally (not imported) so this lane never couples to a
file another lane owns.

This module also owns the two scene-digest reads the consult path shares
(:func:`scene_facts_line`, :func:`scene_recent_prose`), moved here so the
per-turn hook and the consult router read the same projection without the hook
importing the API layer.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from fabulist.assistants.manager import AssistantDefinition

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fabulist.log import EventLog

__all__ = [
    "ASSISTANT_CARD_ROLE",
    "ASSISTANT_PANEL_EVENT",
    "CHAT_ASSISTANT_EVENT",
    "AssistantCardStatement",
    "latest_assistant_panel",
    "latest_chat_assistant",
    "parse_assistant_panel",
    "render_assistant_panel_statement",
    "resolve_session_events",
    "scene_facts_line",
    "scene_recent_prose",
    "split_assistant_reply",
]

#: System-event kind carrying one panel statement (latest wins).
ASSISTANT_PANEL_EVENT = "assistant_panel"

#: System-event kind carrying a chat's enabled-assistant slot (latest wins).
CHAT_ASSISTANT_EVENT = "chat_assistant"

#: The fixed card subtitle (role arg). Chrome, not persona data: the model's
#: emitted value is discarded so the placeholder ``"Guide"`` can never render.
ASSISTANT_CARD_ROLE = "assistant"

#: The card statement ident (the frontend matches on the call name).
ASSISTANT_CARD_IDENT = "asst"

#: An addressable avatar starts with a scheme (or is a ``data:`` URI).
_AVATAR_SCHEME_RE = re.compile(r"^(?:https?://|data:)")

#: ...or is path-shaped: a ``.ext`` tail (``/a/b.png``, ``a.png``).
_AVATAR_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9]+$")

#: Locates an ``asst = AssistantCard(`` call inside a reply.
_CARD_CALL_RE = re.compile(r"\basst\s*=\s*AssistantCard\s*\(")

#: A trailing, half-emitted ``asst =`` fragment (truncated statement).
#: Only matched at the very end of the reply, so in-prose mentions survive.
_DANGLING_ASST_RE = re.compile(r"\basst\s*=\s*[A-Za-z]*\s*\(?\s*$")

#: Backtick fence markers (``\`\`\``` / ``\`\`\`openui``) — stripped before parsing.
_FENCE_RE = re.compile(r"```[A-Za-z0-9_+-]*")


@dataclass(frozen=True)
class AssistantCardStatement:
    """A parsed, definition-corrected ``asst = AssistantCard(...)`` statement."""

    avatar: str
    name: str
    role: str
    mood: str
    commentary: str
    options: list[dict] = field(default_factory=list)
    footer: str = ""

    def to_statement(self) -> str:
        """Re-emit the canonical (corrected) OpenUI statement."""
        return render_assistant_panel_statement(self)


# -- primitive literal parsing ----------------------------------------------


def _string_literal(raw: Any) -> str | None:
    """A JSON double-quoted string literal, or ``None`` when not one."""
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, str) else None


def _bool_literal(raw: Any) -> bool | None:
    """``true`` / ``false`` -> ``True`` / ``False``; anything else ``None``."""
    if raw == "true":
        return True
    if raw == "false":
        return False
    return None


# -- structural splitting (quote- and nesting-aware) ------------------------


def _close_index(text: str, open_index: int) -> int | None:
    """Index of the ``)`` matching the ``(`` at ``open_index``, or ``None``.

    Quote- and escape-aware (a paren inside a string literal never counts) and
    nesting-aware for ``()``/``[]``/``{}``. ``None`` means the call never closes.
    """
    depth = 0
    in_string = False
    escape = False
    i = open_index
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
        elif ch == '"':
            in_string = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _split_top_level(text: str) -> list[str]:
    """Split ``text`` on top-level commas (ignoring commas inside strings/groups)."""
    parts: list[str] = []
    start = 0
    depth = 0
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:i])
            start = i + 1
    parts.append(text[start:])
    return [part.strip() for part in parts]


# -- option objects ----------------------------------------------------------


def _parse_option(item: str) -> dict | None:
    """One ``{text, tag, note?, emphasis}`` object, or ``None`` when malformed."""
    item = item.strip()
    if not (item.startswith("{") and item.endswith("}")):
        return None
    fields: dict[str, str] = {}
    for pair in _split_top_level(item[1:-1]):
        key, sep, value = pair.partition(":")
        if not sep:
            return None
        fields[key.strip()] = value.strip()
    text = _string_literal(fields.get("text"))
    tag = _string_literal(fields.get("tag"))
    if text is None or tag is None:
        return None
    option: dict[str, Any] = {"text": text, "tag": tag}
    if "note" in fields:
        note = _string_literal(fields["note"])
        if note is not None:
            option["note"] = note
    if "emphasis" in fields:
        emphasis = _bool_literal(fields["emphasis"])
        if emphasis is not None:
            option["emphasis"] = emphasis
    return option


def _parse_options(raw: str) -> list[dict]:
    """Parse the options list arg; absent/garbage degrades to ``[]``."""
    raw = (raw or "").strip()
    if not (raw.startswith("[") and raw.endswith("]")):
        return []
    inner = raw[1:-1].strip()
    if not inner:
        return []
    options: list[dict] = []
    for item in _split_top_level(inner):
        option = _parse_option(item)
        if option is not None:
            options.append(option)
    return options


# -- serializer --------------------------------------------------------------


def _normalize_avatar(value: str) -> str:
    """Keep an addressable avatar URL/path; blank anything else.

    Models drift into putting their NAME in the positional avatar slot (live:
    ``AssistantCard("The Oracle", ...)``), which makes the frontend request the
    name as a URL. Only ``http(s)://`` / ``data:`` values and path-shaped values
    (a ``/`` segment or a ``.ext`` tail) survive; every other value is blanked so
    the card's initials fallback applies.
    """
    avatar = (value or "").strip()
    if not avatar:
        return ""
    if _AVATAR_SCHEME_RE.match(avatar):
        return avatar
    if "/" in avatar or _AVATAR_EXTENSION_RE.search(avatar):
        return avatar
    return ""


def _quote(value: str) -> str:
    """A double-quoted string literal the OpenUI tokenizer's JSON path accepts."""
    return json.dumps(value, ensure_ascii=False)


def _render_options(options: list[dict]) -> str:
    """Render option dicts back to the directive's ``{key: value}`` form."""
    rendered: list[str] = []
    for option in options or []:
        parts = [
            f"text: {_quote(str(option.get('text', '')))}",
            f"tag: {_quote(str(option.get('tag', '')))}",
        ]
        note = option.get("note")
        if note:
            parts.append(f"note: {_quote(str(note))}")
        emphasis = option.get("emphasis")
        if emphasis is True:
            parts.append("emphasis: true")
        elif emphasis is False:
            parts.append("emphasis: false")
        rendered.append("{" + ", ".join(parts) + "}")
    return "[" + ", ".join(rendered) + "]"


def render_assistant_panel_statement(card: AssistantCardStatement) -> str:
    """Serialize a card back to the canonical ``asst = AssistantCard(...)`` form.

    ``avatar`` is normalized on the way out (see :func:`_normalize_avatar`) so no
    emitted statement can carry a bare-name avatar.
    """
    args = [
        _quote(_normalize_avatar(card.avatar)),
        _quote(card.name),
        _quote(card.role),
        _quote(card.mood),
        _quote(card.commentary),
        _render_options(card.options),
        _quote(card.footer),
    ]
    return f"{ASSISTANT_CARD_IDENT} = AssistantCard({', '.join(args)})"


# -- the tolerant parser -----------------------------------------------------


def parse_assistant_panel(
    reply: str, definition: AssistantDefinition
) -> AssistantCardStatement | None:
    """Extract the card from an assistant reply, applying definition overrides.

    Returns ``None`` when no ``asst = AssistantCard(`` statement is present or its
    required positional scalars cannot be read; an absent/garbage options list is
    '' and never fails the whole card. The definition supplies ``name`` and the
    fixed ``role``; ``avatar`` is whatever the model emitted (empty allowed).
    """
    if not isinstance(reply, str) or not reply.strip():
        return None
    text = _FENCE_RE.sub("", reply)
    matches = list(_CARD_CALL_RE.finditer(text))
    if not matches:
        return None
    open_index = matches[-1].end() - 1
    close_index = _close_index(text, open_index)
    if close_index is None:
        return None
    args = _split_top_level(text[open_index + 1 : close_index])
    if len(args) < 5:
        return None
    scalars = [_string_literal(arg) for arg in args[:5]]
    if any(value is None for value in scalars):
        return None
    avatar, _emitted_name, _emitted_role, mood, commentary = scalars
    options = _parse_options(args[5]) if len(args) >= 6 else []
    footer = _string_literal(args[6]) if len(args) >= 7 else ""
    return AssistantCardStatement(
        avatar=_normalize_avatar(avatar),
        name=definition.name,
        role=ASSISTANT_CARD_ROLE,
        mood=mood or "",
        commentary=commentary or "",
        options=options,
        footer=footer or "",
    )


# -- prose / statement splitting --------------------------------------------


def _clean_prose(text: str) -> str:
    """Collapse the whitespace (and stray fences) a lifted statement leaves.

    Removes backtick fence markers, drops runs of blank lines down to a single
    blank line, and strips leading/trailing whitespace.
    """
    text = _FENCE_RE.sub("", text)
    cleaned: list[str] = []
    blank = False
    for line in text.split("\n"):
        line = line.rstrip()
        if line.strip():
            cleaned.append(line)
            blank = False
        elif not blank:
            cleaned.append("")
            blank = True
    return "\n".join(cleaned).strip()


def split_assistant_reply(reply: str) -> tuple[str, str | None]:
    """Split an assistant reply into (prose, statement-or-None).

    Reuses the parser's tolerant locator: fences are transparent and the LAST
    ``asst = AssistantCard(`` call wins as the returned statement. Every located
    statement span is removed from the prose (a reply that emits two statements
    must not leak the first one), and a trailing half-emitted ``asst =``
    fragment is dropped too. Whitespace/blank lines left behind are collapsed.

    A reply with no statement is returned unchanged as ``(reply, None)``.
    """
    if not isinstance(reply, str) or not reply.strip():
        return reply, None
    matches = list(_CARD_CALL_RE.finditer(reply))
    if not matches:
        dangling = _DANGLING_ASST_RE.search(reply)
        if dangling:
            return _clean_prose(reply[: dangling.start()]), None
        return reply, None

    spans: list[tuple[int, int]] = []
    statement: str | None = None
    for match in matches:
        close_index = _close_index(reply, match.end() - 1)
        end = len(reply) if close_index is None else close_index + 1
        spans.append((match.start(), end))
        # Last call wins, mirroring :func:`parse_assistant_panel`: an unclosed
        # last call yields ``None`` rather than the previous, well-formed one.
        statement = reply[match.start() : end].strip() if close_index is not None else None

    pieces: list[str] = []
    cursor = 0
    for start, end in spans:
        if start < cursor:
            continue
        pieces.append(reply[cursor:start])
        cursor = end
    pieces.append(reply[cursor:])
    return _clean_prose("".join(pieces)), statement


# -- fork-aware log reads ----------------------------------------------------


def scene_facts_line(log: "EventLog", session_id: str) -> str:
    """Folded state as the pipe-YAML facts line ('' when nothing tracked)."""
    from fabulist.graph.clock import render_facts

    state = log.get_projection(session_id).get("state") or {}
    if isinstance(state, dict) and state:
        return render_facts({str(key): str(value) for key, value in state.items()})
    return ""


def scene_recent_prose(log: "EventLog", session_id: str, limit: int = 3) -> str:
    """The last few kept narratives, DSL-stripped, joined for the digest."""
    from fabulist.prompts.compiler import strip_dsl

    narratives = log.get_projection(session_id).get("narratives", [])
    texts: list[str] = []
    for item in narratives[-limit:]:
        payload = item.get("payload") or {}
        prose = payload.get("prose")
        if not isinstance(prose, str) or not prose.strip():
            prose = strip_dsl(payload.get("text") or "")
        if prose.strip():
            texts.append(prose.strip())
    return "\n\n".join(texts)


def resolve_session_events(log: "EventLog", session_id: str) -> list[Any]:
    """Fork-resolved raw events for ``session_id`` (root-first, bounded).

    A branch copies no events, so each ancestor is replayed root-first and
    bounded at its child's ``base_seq``. Implemented locally (not imported) so
    this lane never couples to a file another lane owns.
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


def latest_assistant_panel(log: "EventLog", session_id: str) -> str | None:
    """Latest ``system``/``assistant_panel`` statement (fork-aware, latest-wins).

    ``None`` when the session has no well-formed panel statement yet.
    """
    latest: str | None = None
    for event in resolve_session_events(log, session_id):
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        if event.payload.get("event") != ASSISTANT_PANEL_EVENT:
            continue
        statement = event.payload.get("statement")
        if isinstance(statement, str) and statement.strip():
            latest = statement
    return latest


def latest_chat_assistant(log: "EventLog", session_id: str) -> str | None:
    """The session's enabled assistant id (fork-aware, latest-wins), or ``None``.

    An explicit ``assistant_id: null`` disables the slot and folds to ``None``
    exactly like an absent event (the fold reads sequentially, so the last write
    wins).
    """
    enabled: str | None = None
    for event in resolve_session_events(log, session_id):
        if event.type != "system" or not isinstance(event.payload, dict):
            continue
        if event.payload.get("event") != CHAT_ASSISTANT_EVENT:
            continue
        if "assistant_id" not in event.payload:
            continue
        value = event.payload.get("assistant_id")
        enabled = value if isinstance(value, str) and value else None
    return enabled
