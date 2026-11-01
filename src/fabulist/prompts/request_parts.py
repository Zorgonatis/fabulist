"""ONE request assembly per interaction — the engine's and the preview's (#104).

Every interaction's model-facing message list is built HERE, once. The live call
site in :mod:`fabulist.wiring` (and the graph composer for the planner) calls the
named function; ``POST /api/prompts/preview`` calls the SAME function with the
same inputs. There is no second assembly, so the preview cannot disagree with
the engine.

That is the whole point of this module. Before #104 the preview kept a private
copy of the parts assembly (``api.prompts_api._flow_parts``) that covered six of
the eleven flows; the other five disagreed with the engine — three of them
returned a bare ``KeyError: 'system'`` (a 500 in the drawer) and two returned a
list the engine never sends. A future interaction cannot reintroduce that gap
without breaking the live turn in the same edit.

Contract:

* **Nothing preview-only is ever added to a list.** Every message these
  functions return is a message the engine sends; a preview-only note (which
  preset, which lane member, which data type, which substitution) rides the
  response envelope, never a row (ADR-005, issue #104 condition 3).
* **Preset resolution stays with the caller.** A live call passes the ACTIVE
  preset (its read failure keeps its ``preset_degraded`` row where it always
  lived); the preview passes the preset it was asked to render. ``None`` means
  the code defaults — ``compile_preset_flow``'s documented normal state.
* **Imports are lazy at the call site** where a helper lives above this layer
  (``history_from_log``/``facts_line_from_log`` in :mod:`fabulist.wiring`, the
  node composers in :mod:`fabulist.graph.nodes`). This module sits in
  ``fabulist.prompts`` — upstream of the engine — so a module-level import of
  either would be a cycle; every caller here runs after boot.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "assistant_request",
    "bts_request",
    "caption_request",
    "critic_request",
    "extract_request",
    "npc_lane_context",
    "npc_request",
    "ooc_request",
    "planner_directive_messages",
    "proposal_lane_blocks",
    "proposals_request",
    "render_critic_payload",
    "summarize_request",
    "wrap_lane_tail",
]


# -- ooc ---------------------------------------------------------------------


def ooc_request(
    log: Any,
    session_id: str,
    content: str,
    preset: Any = None,
    *,
    vision: bool = False,
) -> list[dict]:
    """The OOC turn's request: flow compile + the per-turn engine envelope.

    The live call site is :func:`fabulist.wiring.make_ooc_message_builder`'s
    ``build``. The state facts are the envelope's ``<engine_state>`` block, never
    a user row (the flow's ``facts`` row is therefore inert here), and the
    ``[OOC]`` flag prefixes the player's content (issue #44/#63). The
    ``assemble_messages`` fallback is shared too: a broken flow must never break
    an OOC turn, live or previewed.
    """
    from fabulist.prompts.envelope import insert_engine_envelope, render_engine_envelope
    from fabulist.prompts.narrator import OOC_FLAG, assemble_messages, render_state_block
    from fabulist.prompts.preset_store import compile_preset_flow
    from fabulist.prompts.variables import render_display_text, resolve_variables
    from fabulist.wiring import OOC_SYSTEM, facts_line_from_log, history_from_log

    facts_line = facts_line_from_log(log, session_id) or ""
    envelope = render_engine_envelope(state=render_state_block(facts_line))
    parts = {
        "system": OOC_SYSTEM,
        "history": history_from_log(
            log,
            session_id,
            limit=8,
            drop_current_user=True,
            vision=bool(vision),
        ),
        "facts": "",
        # Issue #63: the same display step the transcript uses, so the OOC
        # turn's ``{{user}}`` resolves exactly like the bubble that sent it.
        "user_content": (
            f"{OOC_FLAG} {render_display_text(log, session_id, str(content))}"
        ),
        "variables": resolve_variables(log, session_id, content),
    }
    try:
        messages = compile_preset_flow(preset, "ooc", parts)
        # Issue #44: ONE universal carrier — the envelope is an ephemeral
        # ``assistant`` message immediately BEFORE the final user message.
        messages, _def_end, _envelope_at = insert_engine_envelope(
            messages, len(messages), envelope
        )
        return messages
    except Exception:  # noqa: BLE001 - flow path must never break a turn
        logger.warning("ooc flow compile fell back to assemble", exc_info=True)
        return assemble_messages(
            system=OOC_SYSTEM,
            history=parts["history"],
            facts=facts_line,
            user_msg=content,
            kind="ooc",
        )


# -- extract -----------------------------------------------------------------


def extract_request(user_msg: str, narrative: str, preset: Any = None) -> list[dict]:
    """The extractor's request (the ``extract`` flow).

    The session-scoped resolvers are deliberately absent: the extractor is
    post-turn and session-blind by contract, so the turn rides the variables.
    Live call site: ``wiring._extract_flow_messages``.
    """
    from fabulist.prompts.preset_store import compile_preset_flow
    from fabulist.state.extractor import EXTRACTOR_SYSTEM_PROMPT, _turn_block

    return compile_preset_flow(
        preset,
        "extract",
        {
            "system": EXTRACTOR_SYSTEM_PROMPT,
            "turn_payload": _turn_block(user_msg, narrative),
            "variables": {"message": user_msg, "last_narrative": narrative},
        },
    )


# -- summarize ---------------------------------------------------------------


def summarize_request(transcript: str, preset: Any = None) -> list[dict]:
    """The summarizer's request (the ``summarize`` flow).

    Live call site: ``wiring._summarize_flow_messages`` (the compaction
    service's ``messages_builder``).
    """
    from fabulist.graph.compaction import build_summarizer_messages
    from fabulist.prompts.preset_store import compile_preset_flow

    base = build_summarizer_messages(transcript)
    return compile_preset_flow(
        preset,
        "summarize",
        {
            "system": str(base[0].get("content", "")),
            "payload": str(base[1].get("content", "")) if len(base) > 1 else "",
            "variables": {},
        },
    )


# -- caption -----------------------------------------------------------------


def caption_request(asset: Mapping[str, Any], preset: Any = None) -> list[dict]:
    """The captioner's request (the ``caption`` flow, image part included).

    Live call sites: ``wiring._caption_flow_messages`` (``app.state.caption_flow``)
    and :func:`fabulist.media.images.caption`'s default shape.
    """
    from fabulist.media.images import build_caption_messages
    from fabulist.prompts.preset_store import compile_preset_flow

    base = build_caption_messages(dict(asset))
    return compile_preset_flow(
        preset,
        "caption",
        {
            "system": str(base[0].get("content", "")),
            "payload": base[1].get("content", "") if len(base) > 1 else "",
            "variables": {},
        },
    )


# -- bts ---------------------------------------------------------------------


def bts_request(
    user_msg: str,
    narrative: str,
    prior_state: dict | None = None,
    *,
    checkpoint: bool = False,
    preset: Any = None,
) -> list[dict]:
    """The world-state pass's request (the ``bts`` flow).

    Live call site: ``wiring._bts_flow_messages``.
    """
    from fabulist.prompts.preset_store import compile_preset_flow
    from fabulist.state.bts import BTS_SYSTEM_PROMPT, build_bts_messages

    base = build_bts_messages(
        user_msg, narrative, prior_state, checkpoint=checkpoint
    )
    return compile_preset_flow(
        preset,
        "bts",
        {
            "system": str(base[0].get("content", BTS_SYSTEM_PROMPT)),
            "payload": str(base[1].get("content", "")) if len(base) > 1 else "",
            "variables": {},
        },
    )


# -- assistant ---------------------------------------------------------------


def assistant_request(
    definition: Any, context: Any, preset: Any = None
) -> list[dict]:
    """One assistant consult's request (the ``assistant`` flow).

    Live call sites: ``wiring._assistant_flow_messages`` (the panel's per-turn
    hook AND the manual consult route — one builder, two doors).
    """
    from fabulist.assistants.manager import build_assistant_messages
    from fabulist.prompts.preset_store import compile_preset_flow

    base = build_assistant_messages(definition, context)
    return compile_preset_flow(
        preset,
        "assistant",
        {
            "system": str(base[0].get("content", "")),
            "history": [],
            "user_content": str(base[1].get("content", "")) if len(base) > 1 else "",
            "variables": {},
        },
    )


# -- critic ------------------------------------------------------------------


def render_critic_payload(evidence: Mapping[str, Any]) -> str:
    """Compose the critic's single payload block — the D41/D49 evidence bundle.

    Included, by contract: the user message; THIS turn's prose as plain text (the
    literal ``find`` source, one linebreak between lines and no numbering); the
    validator's flags; a short tail of the preceding turns' prose; the session's
    current state line.

    Deliberately EXCLUDED, each for a reason:

    * **DSL segments** — the critic judges prose only (D49: patch scope is prose;
      the DSL channel has its own validation gate). Feeding it the openui DSL
      would invite UI edits and bloat a cheap agent's context with component
      source it cannot change.
    * **Injected facts / recall / roster / library** — those are the narrator's
      INPUTS, assembled pre-turn; the critic reviews what the turn made of the
      scene, not a re-derivation of it. Including them would also hand the critic
      the fiction's ground truth and turn a prose critique into a source check.
    * **Full history** — only a short prior tail is sent: the two defects that
      matter (repetition, lack of movement) are judged against the immediately
      preceding beats, and the whole log would blow the cheap agent's budget for
      no verdict value.

    Moved here from :mod:`fabulist.wiring` by issue #104 so the preview and the
    critic call site render ONE payload (``wiring._render_critic_payload`` is a
    one-line delegate kept for its existing importers).
    """
    from fabulist.wiring import _critic_flags_block, _plain_prose

    prior = [text for text in (evidence.get("prior_prose") or []) if text]
    prior_block = "\n\n".join(f"---\n{text}" for text in prior) or "(none)"
    return (
        "### USER MESSAGE\n"
        f"{evidence.get('user_message') or ''}\n\n"
        "### THIS TURN (the turn's prose; copy `find` text verbatim from it)\n"
        f"{_plain_prose(str(evidence.get('prose') or ''))}\n\n"
        "### VALIDATOR FLAGS (hints, not commands)\n"
        f"{_critic_flags_block(list(evidence.get('flags') or []))}\n\n"
        "### PRIOR TURNS (most recent last)\n"
        f"{prior_block}\n\n"
        "### STATE\n"
        f"{evidence.get('state_line') or ''}\n"
    )


def critic_request(evidence: Mapping[str, Any], preset: Any = None) -> list[dict]:
    """The critic's request (the ``critic`` flow: system prompt + one payload).

    Live call site: ``wiring._critic_flow_messages``.
    """
    from fabulist.prompts.critic import CRITIC_SYSTEM_PROMPT
    from fabulist.prompts.preset_store import compile_preset_flow

    return compile_preset_flow(
        preset,
        "critic",
        {
            "system": CRITIC_SYSTEM_PROMPT,
            "payload": render_critic_payload(evidence),
            "variables": {},
        },
    )


# -- planner -----------------------------------------------------------------


def planner_directive_messages(preset: Any = None) -> list[dict]:
    """The planner's AUTHORED surface: its flow's ``system`` row, compiled.

    The directive is the only part the flow carries; the context block and the
    refresh/steer ask are structural and are composed by the node
    (:func:`fabulist.graph.nodes.planner_request`). Live call site:
    ``wiring._planner_flow_messages`` / ``wiring._planner_directive``.
    """
    from fabulist.graph.nodes import DEFAULT_PLANNER_DIRECTIVE
    from fabulist.prompts.preset_store import compile_preset_flow

    return compile_preset_flow(
        preset,
        "planner",
        {"system": DEFAULT_PLANNER_DIRECTIVE, "variables": {}},
    )


# -- npc ---------------------------------------------------------------------


def npc_lane_context(
    log: Any,
    session_id: str,
    member: Any,
    *,
    beat: str = "",
    last_intent: str = "",
    presence_window: int = 3,
) -> dict:
    """One lane's private context row (the shape ``render_private_block`` reads).

    Live call site: the ``NpcLaneFanout`` dispatcher's row build (one row per
    dispatched member). The preview builds its row through the SAME function, so
    a new context field cannot reach one path and not the other.
    """
    from fabulist.state.presence import member_categories, witnessed_turns

    return {
        "member_id": member.member_id,
        "name": member.display_name,
        "aliases": list(member.aliases),
        "categories": member_categories(log, session_id, member.member_id),
        "witnessed": witnessed_turns(
            log, session_id, member.member_id, window=presence_window
        ),
        "beat": beat,
        "last_intent": last_intent,
    }


def npc_request(state: Mapping, context: Mapping, preset: Any = None) -> list[dict]:
    """ONE NPC lane's TAIL — the private block + the JSON ask (``npc`` flow).

    A lane tail, never a whole request: the dispatcher appends it AFTER the
    narrator's shared prefix (:meth:`fabulist.graph.nodes.NpcLaneFanout._request`).
    The brief rides the engine carrier (``carry_lane_brief``) because it is not a
    leading system message (issue #73). A broken flow falls back to the code
    default, exactly as the live call site did.

    Live call site: ``wiring._npc_flow_messages``.
    """
    from fabulist.graph.nodes import default_npc_lane_messages, render_private_block
    from fabulist.prompts.npc import build_lane_ask, build_lane_system
    from fabulist.prompts.preset_store import compile_preset_flow

    name = str(context.get("name") or "")
    try:
        flow = compile_preset_flow(
            preset,
            "npc",
            {
                "system": build_lane_system(name, render_private_block(context)),
                "payload": build_lane_ask(name),
                "variables": {"name": name},
            },
        )
    except Exception:  # noqa: BLE001 - a broken flow never kills a lane
        logger.warning("npc lane flow fell back to the code default", exc_info=True)
        flow = []
    return flow or default_npc_lane_messages(context)


def wrap_lane_tail(tail: list[dict], anti_injection: bool = True) -> list[dict]:
    """Wrap a lane tail's content in the engine's ``<preset_runtime>`` bound.

    Issue #27/#77: the bound is applied at the ASSEMBLY seam that positions the
    lane messages, and it gates on the same persisted Anti Injection setting the
    narrator's own wrapper read for this build. Live call site:
    :meth:`fabulist.graph.nodes.NpcLaneFanout._request`; the preview calls the
    same helper so the previewed tail carries the same bytes.
    """
    if not anti_injection:
        return list(tail)
    from fabulist.prompts.wrapper import wrap_runtime

    return [
        {**message, "content": wrap_runtime(message.get("content"))}
        for message in tail
    ]


# -- proposals ---------------------------------------------------------------


def proposal_lane_blocks(
    log: Any,
    spec: Any,
    *,
    touched: Any = (),
    prose: str = "",
) -> dict[str, str]:
    """One proposal lane's two structural block bodies (system brief + payload).

    Live call site: the ``AssetProposalFanout`` router. The preview builds its
    blocks through the SAME function for the first data type, so the lane brief
    and the type digest cannot drift between the two paths.
    """
    from fabulist.prompts.proposals import build_lane_payload, build_lane_system
    from fabulist.state.proposal_lanes import (
        MAX_ITEMS,
        patch_fields,
        stored_digest,
    )

    return {
        "system": build_lane_system(
            lane_label=spec.label,
            kind=spec.kind,
            fields=patch_fields(spec.kind),
            max_items=MAX_ITEMS,
        ),
        "payload": build_lane_payload(
            lane=spec.lane,
            kind=spec.kind,
            touched=list(touched),
            digest=stored_digest(log, spec),
            prose=prose,
        ),
    }


def proposals_request(
    blocks: Mapping[str, str], session_id: str = "", preset: Any = None
) -> list[dict]:
    """ONE proposal lane's request (the ``proposals`` flow).

    A lane tail by membership (:data:`fabulist.prompts.modules.LANE_INTERACTIONS`),
    but the engine sends it STANDALONE — the router builds these messages and
    hands them to the one-call lane, with no narrator prefix
    (``AssetProposalFanout.route``). The preview renders the same list for the
    same reason: it shows the request the lane makes, not a request it never
    makes. A broken flow falls back to the code default.

    Live call site: ``wiring._proposals_flow_messages``.
    """
    from fabulist.prompts.preset_store import compile_preset_flow
    from fabulist.prompts.proposals import default_lane_messages

    default = default_lane_messages(str(blocks["system"]), str(blocks["payload"]))
    try:
        messages = compile_preset_flow(
            preset,
            "proposals",
            {
                "system": str(blocks.get("system") or ""),
                "payload": str(blocks.get("payload") or ""),
                "variables": {},
            },
        )
    except Exception:  # noqa: BLE001 - a broken flow never kills a lane
        logger.warning("proposals flow fell back to the code default", exc_info=True)
        return default
    return messages or default
