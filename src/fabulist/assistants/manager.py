"""W2.5 assistants — Extra Assistant personas as injectable subagents.

An *assistant* is a persona with its own context and, via the connection
registry, its own model tier (``agent`` — e.g. ``SEGFAULT`` -> a small model).
This module owns the definition type, the trigger scanner, and the consult
path; it deliberately owns **no state** and writes **no log events** (D9:
callers append). The narrator graph does not call this module in W2.5 — the
orchestrator wires :class:`RegistryModelCaller` per assistant agent later.

Spec of record: PLAN.md W2.5 and the maintainer's private notes (unshipped) row 11 (Extra Assistants:
``SEGFAULT`` et al.). Persona text for ``SEGFAULT`` lives in the preset's
Tier 4 / Extra Assistant entries; this module only carries it.

Model callers are injected. ``model_caller_factory(agent)`` returns a
:class:`~fabulist.graph.types.ModelCaller` for that assistant's agent name, so
tests inject fakes and production resolves one caller per model tier.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from fabulist.graph.types import ModelCaller, collect_text

__all__ = [
    "AssistantContext",
    "AssistantDefinition",
    "AssistantManager",
    "AssistantReply",
    "build_assistant_messages",
    "keyword_matches",
]

#: Allowed trigger modes. ``manual`` assistants are only ever invoked by id;
#: ``keyword`` assistants may additionally be discovered by :meth:`scan_triggers`.
TRIGGER_MODES = ("manual", "keyword")


@dataclass(frozen=True)
class AssistantDefinition:
    """One Extra Assistant persona.

    ``agent`` is a model-tier name resolved by the connection registry (default
    ``"narrator"`` so an unwired assistant still has a home). ``trigger`` selects
    how the assistant is discovered: ``"manual"`` requires an explicit
    :meth:`AssistantManager.consult`; ``"keyword"`` adds :meth:`scan_triggers`
    matching over ``keywords``.

    ``actor`` (D47) is an optional reference — a persona id or name — to the
    actor this assistant runs as in its assistant context. Absent stays absent
    (``""``): a definition that names no actor keeps loading, and no default
    actor is invented.
    """

    id: str
    name: str
    persona: str
    style_notes: str = ""
    agent: str = "narrator"
    trigger: str = "manual"
    keywords: list[str] = field(default_factory=list)
    actor: str = ""

    def __post_init__(self) -> None:
        if self.trigger not in TRIGGER_MODES:
            raise ValueError(
                f"assistant {self.id!r}: trigger must be one of {TRIGGER_MODES}, "
                f"got {self.trigger!r}"
            )


@dataclass(frozen=True)
class AssistantContext:
    """The compact scene digest a caller supplies for one consult.

    Kept deliberately small and read-only: facts (the current state block),
    recent prose, and the triggering user message. The manager never reads the
    event log itself, so callers decide what the assistant is allowed to see.
    """

    facts: str = ""
    recent_prose: str = ""
    user_msg: str = ""


@dataclass(frozen=True)
class AssistantReply:
    """An assistant's drained response. Never written to the log by this module."""

    text: str
    assistant_id: str
    agent: str


def keyword_matches(keyword: str, text: str) -> bool:
    """Word-boundary, case-insensitive match of one keyword against ``text``.

    Uses negative word-char lookarounds rather than ``\\b`` so keywords edged
    with punctuation (e.g. ``c++``) still anchor sensibly, while ``cat`` never
    matches inside ``concatenate``. Blank keywords never match.
    """

    stripped = keyword.strip()
    if not stripped:
        return False
    pattern = rf"(?<!\w){re.escape(stripped)}(?!\w)"
    return re.search(pattern, text, re.IGNORECASE) is not None


#: Model-facing output contract appended to every assistant system prompt (D38).
#: The persona block above it is the sole source of voice and name; this contract
#: names each element the assistant is responsible for and how to populate it
#: (D47), not just the call format. The statement shape matches
#: ``frontend/src/lib/openui/schemas/assistant.ts`` (strictly positional: avatar,
#: name, role, mood, commentary, options, footer). Positive-only wording
#: (D38(d)) — the example teaches the form, never a wrong one.
_CARD_OUTPUT_DIRECTIVE = """## Card output
You drive the AssistantCard surface: the panel beside the transcript that shows your commentary and the replies the user may send. Every reply is your in-voice commentary in prose, ending with one statement on its own line that renders that card:

asst = AssistantCard("", "Sam", "Guide", "🙂", "One line on the turn.", [{text: "I head for the door", tag: "leave", note: "momentum", emphasis: true}, {text: "I ask what they meant", tag: "ask", note: "clarity", emphasis: false}, {text: "I wait and see", tag: "wait", note: "low risk", emphasis: false}], "Later.")

The identifier is always asst; the arguments are positional in this exact order and each fills one element of the card:
1. avatar — your portrait URL; leave it empty ("") and the panel renders initials from your name.
2. name — your name; the panel draws the correct one.
3. role — the subtitle under your name.
4. mood — one or two emoji reacting to the current beat.
5. commentary — your persona speaking to the user, one fresh line distilled from your prose.
6. options — three to six suggested replies phrased as the user would send them; choose how many between three and six best fit the turn, each with a one-word tag, with exactly one carrying emphasis: true — the one that best fits your style — and note optional, in your voice.
7. footer — an optional closing flavour line.
The statement ends every reply, every turn."""


def render_assistant_system(definition: AssistantDefinition) -> str:
    """Persona-first system prompt for an assistant (no narrator directives).

    Ends with the verbatim :data:`_CARD_OUTPUT_DIRECTIVE` output contract so the
    assistant's reply carries the ``asst = AssistantCard(...)`` statement the
    panel lane parses (D38).
    """

    lines = [
        f"You are {definition.name}, an assistant embedded in an ongoing "
        "role-play.",
        "Stay fully in persona. Offer your own in-voice commentary only: never "
        "advance the shared narrative state and never write for other characters.",
        "",
        "<persona>",
        definition.persona.strip(),
        "</persona>",
    ]
    if definition.style_notes.strip():
        lines += ["", "<style>", definition.style_notes.strip(), "</style>"]
    return "\n".join(lines) + "\n\n" + _CARD_OUTPUT_DIRECTIVE


def render_scene_digest(context: AssistantContext) -> str:
    """Compact, XML-tagged digest of the scene handed to the assistant."""

    parts = ["<scene_digest>"]
    if context.facts.strip():
        parts += ["<facts>", context.facts.strip(), "</facts>"]
    if context.recent_prose.strip():
        parts += ["<recent_prose>", context.recent_prose.strip(), "</recent_prose>"]
    parts += ["<user_message>", context.user_msg.strip(), "</user_message>"]
    parts.append("</scene_digest>")
    return "\n".join(parts)


def build_assistant_messages(
    definition: AssistantDefinition, context: AssistantContext
) -> list[dict]:
    """Build the assistant's own chat context: persona system, then digest user.

    The system message is always first (persona-first); the digest rides in the
    final user message, matching the repo convention (facts inside the final
    user message — provider templates reject mid-conversation system messages).
    """

    return [
        {"role": "system", "content": render_assistant_system(definition)},
        {"role": "user", "content": render_scene_digest(context)},
    ]


class AssistantManager:
    """Read-only registry + consult path over a set of assistant definitions.

    The manager is stateless with respect to the event log: :meth:`consult`
    returns a :class:`AssistantReply` for the caller to append (D9). It never
    advances the clock, mutates facts, or touches SQLite.
    """

    def __init__(
        self,
        definitions: Iterable[AssistantDefinition],
        model_caller_factory: Callable[[str], ModelCaller],
    ) -> None:
        self._definitions = list(definitions)
        self._factory = model_caller_factory
        self._by_id: dict[str, AssistantDefinition] = {}
        for definition in self._definitions:
            if definition.id in self._by_id:
                raise ValueError(f"duplicate assistant id: {definition.id!r}")
            self._by_id[definition.id] = definition

    def available(self) -> list[AssistantDefinition]:
        """All definitions, in declaration order (a copy — do not mutate)."""

        return list(self._definitions)

    def get(self, assistant_id: str) -> AssistantDefinition:
        """Resolve one definition by id, naming the id on failure."""

        try:
            return self._by_id[assistant_id]
        except KeyError:
            known = ", ".join(sorted(self._by_id)) or "<none>"
            raise KeyError(
                f"unknown assistant {assistant_id!r}; known assistants: {known}"
            ) from None

    def scan_triggers(self, user_msg: str) -> list[str]:
        """Ids of keyword-triggered assistants whose keywords appear in ``user_msg``.

        Manual-only definitions are never considered. Matches are
        word-boundary and case-insensitive; results preserve declaration order.
        """

        matched: list[str] = []
        for definition in self._definitions:
            if definition.trigger != "keyword":
                continue
            if any(keyword_matches(kw, user_msg) for kw in definition.keywords):
                matched.append(definition.id)
        return matched

    async def consult(
        self, assistant_id: str, context: AssistantContext
    ) -> AssistantReply:
        """Ask one assistant for an in-voice reply.

        Builds the assistant's own context, resolves its model caller by agent
        tier, drains the stream into text, and returns
        ``{text, assistant_id, agent}``. State and manifest are untouched.
        """

        definition = self.get(assistant_id)
        caller = self._factory(definition.agent)
        # D18 completion: the assistant interaction's module flow, when the
        # router supplies one (preset-customized or default); None keeps the
        # fixed persona-first shape.
        builder = getattr(self, "_messages_builder", None)
        messages = (
            builder(definition, context)
            if builder is not None
            else build_assistant_messages(definition, context)
        )
        text = await collect_text(caller, messages)
        return AssistantReply(
            text=text, assistant_id=definition.id, agent=definition.agent
        )
