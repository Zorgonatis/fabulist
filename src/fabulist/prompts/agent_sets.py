"""Agent sets as preset data (D19).

The engine never hard-codes which *agent tier* (connections-registry agent name)
serves which interaction. Instead every interaction — ``narrate``, ``ooc``,
``extract``, ``summarize``, ``caption``, ``assistant`` — resolves its agent
through a small binding record carried by the ACTIVE preset, falling back to
:data:`DEFAULT_BINDINGS` which mirrors today's wiring.

Where the defaults come from (cite every seam):

* ``narrate`` -> ``narrator``: ``wiring.build_engine`` builds
  ``RegistryModelCaller(narrator_agent, registry)`` with ``narrator_agent``
  defaulting to ``"narrator"`` (wiring.py ``build_engine``).
* ``ooc`` -> ``narrator``: the OOC turn uses the engine's single ``model``
  caller, i.e. the same narrator ``RegistryModelCaller``
  (wiring.py ``build_engine`` -> ``GraphTurnEngine(model=caller, ...)``).
* ``extract`` -> ``extractor``: ``LLMExtractor._complete`` calls
  ``build_request("extractor", ...)`` (state/extractor.py).
* ``summarize`` -> ``summarizer``: ``build_app`` constructs
  ``RegistryModelCaller("summarizer", registry)`` (wiring.py ``build_app``).
* ``caption`` -> ``captioner``: ``build_app`` sets
  ``app.state.captioner = RegistryModelCaller("captioner", registry)``
  (wiring.py ``build_app``; consumed by api/attachments.py).
* ``assistant`` -> per-definition agent (default ``narrator``):
  ``AssistantDefinition.agent`` defaults to ``"narrator"`` and the API coerces a
  missing field to ``"narrator"`` (assistants/manager.py;
  api/assistants_api.py ``_coerce_definition``).

The binding dict shape, fixed by the D19 contract::

    {
      "narrate": {"agent": "narrator", "overrides": {}},
      "extract": {"agent": "extractor", "overrides": {"temperature": 0.1}},
      ...
    }

``overrides`` is an optional free-form dict of sampler/parameter overrides; the
resolver returns ``{}`` when it is absent so callers can splat it unconditionally.

Typed slots at this layer (W9.25 §3.4)
--------------------------------------
A binding ``agent`` is a connections-registry SLOT name, and since W9.25 that
vocabulary is wider than the item keys: the typed spellings declared in
:mod:`fabulist.connections.taxonomy` (``completion``, ``completion.narration``,
``completion.extraction``, ``completion.state``, ``embedding``) are legal values
and resolve through the item > task > type hierarchy
(:func:`~fabulist.connections.taxonomy.resolve_interaction_agent`). The
shipped defaults keep their legacy spelling for display stability — ``npc`` is
the ONE exception: its lanes are narrative-voice work, so it names the narration
task (see ``DEFAULT_BINDINGS``).

Preset source
-------------
:func:`bindings_for` reads the active preset's ``agent_bindings`` from
``fabulist.prompts.preset_store.active_preset`` — a parallel lane's module. The
import is defended at call time: if it is missing/unusable, or the active preset
declares no bindings, the defaults apply. Tests and early callers can inject a
provider with :func:`set_bindings_provider`, a module-level seam that bypasses
the preset store entirely.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from typing import Any

__all__ = [
    "INTERACTIONS",
    "DEFAULT_BINDINGS",
    "set_bindings_provider",
    "validate_bindings",
    "resolve_agent",
    "resolve_overrides",
    "bindings_for",
]

#: The interactions that resolve an agent through a binding record.
INTERACTIONS: tuple[str, ...] = (
    "narrate",
    "ooc",
    "extract",
    "summarize",
    "caption",
    "assistant",
    # Wave 6: the BTS world-state tracker is preset-bindable too. Speaker
    # attribution used to live here as well; it is deterministic code now
    # (owner decision — no model call, so no agent binding).
    "bts",
    # Wave 8 (D41/D49): the post-turn critic quality gate.
    "critic",
    # Wave 9.13: the per-NPC fan-out lane (one decision pass per active member,
    # run pre-turn just before the narrator).
    "npc",
    # W9.20: the planner (steering plane) — the prompt surface is its control
    # plane, exactly like the critic's or the lanes'.
    "planner",
    # W9.22 §3: the asset-proposal lanes (one per data type: world / actors /
    # books). AUTHORING is extractor-class generation (decision doctrine 4: the
    # decision model judges, it never generates), so it owns a flow and a
    # binding like the lanes do.
    "proposals",
)

#: Today's wiring, expressed as data (see module docstring for the call sites).
DEFAULT_BINDINGS: dict[str, dict[str, Any]] = {
    "narrate": {"agent": "narrator", "overrides": {}},
    "ooc": {"agent": "narrator", "overrides": {}},
    "extract": {"agent": "extractor", "overrides": {}},
    "summarize": {"agent": "summarizer", "overrides": {}},
    "caption": {"agent": "captioner", "overrides": {}},
    "assistant": {"agent": "narrator", "overrides": {}},
    # Wave 6: the post-turn BTS pass runs cheap and deliberately defaults to the
    # ``extractor`` agent (the connection's dedicated cheap local entry); the
    # preset may rebind it. The binding resolver falls back to the caller's own
    # fallback agent when it is absent.
    "bts": {"agent": "extractor", "overrides": {}},
    # Wave 8 (D49): the critic runs in-graph at the tail of the turn and holds
    # the turn lock until it returns, so it defaults to the same cheap
    # extractor-class agent; the preset may rebind it (D19) with no code change.
    "critic": {"agent": "extractor", "overrides": {}},
    # W9.25 §3.4 (superseding W9.13's extractor-class default): the lanes are
    # narrative-voice work, so they ride the NARRATION task — ``narrator``'s
    # connection on any legacy config. The W9.13 cheap binding was a cost
    # classing that made the lanes fail their own shape validation on a thinking
    # extractor model; lane cost is now the dispatch policy's knob (W9.24-W5:
    # strategy + max_dispatch), NEVER the model class. A preset may still bind
    # ``npc`` per item (e.g. ``extractor``) and that wins.
    "npc": {"agent": "completion.narration", "overrides": {}},
    # W9.20 ([OWNER] #11): the planner binds to the NARRATOR's connection for
    # now — one call every ``cadence`` turns, and the story-steering quality
    # wants the writer's model. It is its own D19 interaction so pointing it at
    # a faster model later is a binding edit, never a code change.
    "planner": {"agent": "narrator", "overrides": {}},
    # W9.22 §3 (doctrine 4): proposal AUTHORING is extractor-class generation —
    # the decision model judges each item and cannot author content — so the
    # lane defaults to the same cheap extractor agent as ``bts``/``critic``/``npc``
    # and the preset may rebind it (D19). One call per DATA TYPE, and only on a
    # turn the double gate admits: this binding is the stage's cost knob.
    "proposals": {"agent": "extractor", "overrides": {}},
}


#: Optional injected provider: ``fn(log) -> Mapping | None``. When set it wholly
#: replaces the preset-store lookup (used by tests / non-log callers).
_bindings_provider: Callable[[Any], Mapping[str, Any] | None] | None = None


def set_bindings_provider(
    fn: Callable[[Any], Mapping[str, Any] | None] | None,
) -> None:
    """Install (or clear, with ``None``) the preset bindings provider seam.

    The provider receives the ``EventLog`` and returns a bindings mapping or
    ``None``/empty for "no preset bindings -> defaults". Production leaves this
    unset so :func:`bindings_for` reads the live preset store.
    """

    global _bindings_provider
    _bindings_provider = fn


def _preset_bindings_provider(log: Any) -> Mapping[str, Any] | None:
    """Default provider: the active preset's ``agent_bindings`` (or ``None``).

    Defensive by design — ``preset_store`` is a parallel lane's module and may
    not exist yet, and an active preset without bindings must degrade to the
    defaults rather than raise.
    """

    try:
        from fabulist.prompts import preset_store
    except ImportError:
        return None
    try:
        preset = preset_store.active_preset(log)
    except Exception:  # noqa: BLE001 - a bad preset must not break a turn
        return None
    if not isinstance(preset, Mapping):
        return None
    bindings = preset.get("agent_bindings")
    if isinstance(bindings, Mapping) and bindings:
        return bindings
    return None


def validate_bindings(
    data: Mapping[str, Any] | None,
    known_agents: Collection[str] | None = None,
) -> None:
    """Validate a bindings mapping, raising ``ValueError`` on anything unknown.

    ``data`` maps interaction -> ``{"agent": str, "overrides": {...}}``.
    ``known_agents`` is the caller-supplied registry agent set (the registry's
    ``agents`` keys); when provided, any binding naming an agent outside it is
    rejected. Unknown interactions and unknown agents are reported together in a
    single error message so a preset author sees every problem at once.

    W9.25 §3.4: the effective known set is ``known_agents`` UNION the taxonomy's
    declared spellings, so a preset may bind a typed slot (``completion.narration``
    / ``completion.extraction`` ...) even when the connections document authors no
    such key — resolution then falls through the hierarchy at call time
    (:func:`~fabulist.connections.taxonomy.resolve_slot`). A dotted spelling
    outside the declared vocabulary stays rejected (typo protection).
    """

    if not isinstance(data, Mapping):
        raise ValueError("agent_bindings: must be an object mapping interactions")

    # Lazy import: taxonomy imports this module's INTERACTIONS/DEFAULT_BINDINGS.
    from fabulist.connections.taxonomy import declared_spellings

    known: set[str] | None = None
    if known_agents is not None:
        known = set(known_agents) | declared_spellings()

    problems: list[str] = []
    unknown_interactions: list[str] = []
    unknown_agents: list[str] = []

    for interaction, binding in data.items():
        if interaction not in INTERACTIONS:
            unknown_interactions.append(str(interaction))
            continue
        if not isinstance(binding, Mapping):
            problems.append(f"{interaction}: binding must be an object")
            continue
        agent = binding.get("agent")
        if not isinstance(agent, str) or not agent:
            problems.append(f"{interaction}: missing required string 'agent'")
            continue
        if known is not None and agent not in known:
            unknown_agents.append(f"{interaction}->{agent}")

    if unknown_interactions:
        known = ", ".join(INTERACTIONS)
        problems.append(
            f"unknown interaction(s): {', '.join(sorted(unknown_interactions))}; "
            f"known interactions: {known}"
        )
    if unknown_agents:
        vocabulary = ", ".join(sorted(known)) if known else "<none>"
        problems.append(
            f"unknown agent(s): {', '.join(sorted(unknown_agents))}; "
            f"known agents: {vocabulary}"
        )

    if problems:
        raise ValueError("agent_bindings invalid: " + "; ".join(problems))


def resolve_agent(
    bindings: Mapping[str, Any] | None,
    interaction: str,
    *,
    fallback: str,
) -> str:
    """Resolve the agent for ``interaction``; ``bindings`` win, else ``fallback``.

    A missing interaction, a non-mapping binding, or a missing/blank ``agent``
    all degrade to ``fallback`` — resolution is never allowed to raise mid-turn.
    """

    if isinstance(bindings, Mapping):
        binding = bindings.get(interaction)
        if isinstance(binding, Mapping):
            agent = binding.get("agent")
            if isinstance(agent, str) and agent:
                return agent
    return fallback


def resolve_overrides(bindings: Mapping[str, Any] | None, interaction: str) -> dict:
    """Return the interaction's override dict, or ``{}`` when absent."""

    if isinstance(bindings, Mapping):
        binding = bindings.get(interaction)
        if isinstance(binding, Mapping):
            overrides = binding.get("overrides")
            if isinstance(overrides, Mapping):
                return dict(overrides)
    return {}


def bindings_for(log: Any) -> dict:
    """Bindings for the ACTIVE preset, or :data:`DEFAULT_BINDINGS` when absent.

    Reads ``fabulist.prompts.preset_store.active_preset(log)["agent_bindings"]``
    (overridable via :func:`set_bindings_provider`). A missing/empty/None result
    falls back to today's wiring (D19 default).
    """

    provider = _bindings_provider or _preset_bindings_provider
    try:
        found = provider(log)
    except Exception:  # noqa: BLE001 - never let preset lookup break a turn
        found = None
    if isinstance(found, Mapping) and found:
        return dict(found)
    return dict(DEFAULT_BINDINGS)
