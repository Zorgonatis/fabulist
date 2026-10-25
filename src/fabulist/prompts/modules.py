"""Prompt-module engine (D18): the model pipeline as editable data.

Today's narrator pipeline is hard-coded in
:func:`fabulist.wiring.make_narrator_message_builder.build` — a compiled
narrator system message (``prompts.compiler.compile`` + the library doc),
fork-aware regex-treated history, a merged facts suffix
(:func:`fabulist.context.facts.facts_suffix_parts` +
:func:`~fabulist.context.facts.join_facts_suffix`), and a final user message
(vision multipart appended after). D18 promotes that fixed order to a list of
:class:`PromptModule` rows a user can reorder, disable, and extend from the UI;
the store lane persists the list, the engine lane compiles it here.

Per-interaction flows (D18 completion)
--------------------------------------
Every interaction that talks to a model carries its own ordered module list,
not just the narrator. The user's design statement: *"this entire setup will be
generalized to have agent sets for specific types of interactions"* — v1 gave
extractor/OOC side override strings, an inconsistency: every interaction that
talks to a model carries a module flow now. :data:`INTERACTIONS` and
:data:`DEFAULT_FLOWS` are the fixed contract; each default flow mirrors the
current call site byte-for-byte (citations on :data:`DEFAULT_FLOWS`).

Module model
------------
A module is either a **structural insertion point** whose content the engine
resolves at build time or a **text** module carrying a ``{{variable}}`` template
body (rendered by :func:`fabulist.prompts.variables.render_template`). The
narrator's structural types map onto the exact seams the wiring builder uses:

* ``narrator_system`` -> the compiled narrator system message
  (``compiler.compile``'s system block, which already embeds the library doc).
* ``library`` -> the rendered OpenUI component reference
  (:func:`fabulist.prompts.library_mini.render_library_doc`).
* ``history`` -> the interleaved, DSL-stripped history message list.
* ``facts`` -> accepted for call-site compatibility; the narrator's state facts
  now ride the per-turn engine envelope (``engine_tail``), so the live builder
  passes an empty body and this row emits nothing. (The OOC interaction's own
  flow still uses it for its state line.)
* ``user`` -> the final user message, BARE (vision multipart is applied by the
  caller after :func:`compile_preset_messages`). Nothing engine-authored rides
  inside it.
* ``recalled`` -> the ONE per-turn engine envelope (``<preset_runtime>`` body):
  every per-turn engine block in contract order. ``parts["engine_tail"]`` wins;
  ``parts["recalled"]`` is the pre-contract key (a memory body). Blank/absent
  emits nothing, so a lean turn has no envelope at all. The ROW only decides
  that the envelope ships: since issue #44 its position is a contract — ONE
  ephemeral ``assistant`` message inserted immediately BEFORE the final user
  message (never a system message, never inside the player's message).

Other interactions add their own structural types (see
:data:`FLOW_STRUCTURAL_TYPES`): ``system`` resolves to **that interaction's**
compiled default prompt (the parts dict supplies it), ``turn_payload`` /
``payload`` splice the interaction's user-side request body.

:data:`DEFAULT_MODULES` is the byte-faithful narrator fallback describing
the order ``[narrator_system, library, history, facts, user, recalled]`` — the
``recalled`` row (D22) carries the per-turn engine envelope (issue #44: it is
inserted immediately before the final user message, whatever the row order);
:func:`from_default` returns a fresh, uniquely-identified copy so an edited
preset never aliases it.

D27 token surfaces
------------------
The five formerly code-only surfaces (library doc content, ``<recalled>``
framing + headers, regex scripts, history projection knobs, and directive
default text) ride an additive preset ``surfaces`` block. The schema, tolerant
normalizer and constant-seed factory live in
:mod:`fabulist.prompts.surfaces` and are re-exported here as the preset-module
surface API: :data:`SURFACES_KEY`, :func:`normalize_surfaces`,
:func:`preset_surfaces`, :func:`factory_surfaces`. Loaders in ``compiler`` /
``context.facts`` / ``context.regex_stage`` / ``library_mini`` each read the
surface with a constant fallback, so an older preset that never carried
``surfaces`` compiles byte-identically to the pre-D27 pipeline.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any, Literal

from .surfaces import (
    SURFACES_KEY,
    factory_surfaces,
    normalize_surfaces,
    preset_surfaces,
)
from .variables import render_template
from .envelope import insert_engine_envelope
from .wrapper import fold_system_prefix

__all__ = [
    "DEFAULT_FLOWS",
    "DEFAULT_MODULES",
    "FLOW_STRUCTURAL_TYPES",
    "INTERACTIONS",
    "STRUCTURAL_TYPES",
    "SURFACES_KEY",
    "PromptModule",
    "compile_flow",
    "compile_flow_with_boundary",
    "compile_preset_messages",
    "compile_preset_messages_with_boundary",
    "factory_surfaces",
    "flow_is_default",
    "from_default",
    "from_default_flow",
    "normalize_surfaces",
    "preset_surfaces",
    "splice_session_modules",
]

#: Module types whose content the engine resolves at build time (pipeline
#: insertion points) rather than reading from :attr:`PromptModule.content`.
STRUCTURAL_TYPES: frozenset[str] = frozenset(
    {"narrator_system", "library", "history", "facts", "user", "recalled"}
)

#: The interactions that carry a module flow. Mirrors
#: :data:`fabulist.prompts.agent_sets.INTERACTIONS` (the D19 agent-binding
#: key set) — kept as a local literal so this engine module has no cross-lane
#: import; ``tests/test_prompt_flows.py`` asserts the two never drift.
INTERACTIONS: tuple[str, ...] = (
    "narrate",
    "ooc",
    "extract",
    "summarize",
    "caption",
    "assistant",
    # Wave 6 (D21/D25): the BTS world-state tracker is a first-class
    # preset-bindable interaction with its own flow. Speaker attribution sat
    # beside it in the same wave and is deterministic code now (owner decision:
    # no model call), so it has no flow and no binding.
    "bts",
    # Wave 8 (D41/D49): the post-turn critic quality gate.
    "critic",
    # Wave 9.13: the per-NPC fan-out lane (private tail + JSON ask).
    "npc",
    # W9.20: the planner (steering plane) — its system row is the directive the
    # plan is written under.
    "planner",
    # W9.22 §3: the asset-proposal lanes (one per data type).
    "proposals",
)

#: Structural insertion points valid per interaction (plus ``text``, always
#: valid). A module of any other type for that flow raises in
#: :func:`compile_flow`, listing these as the valid set.
FLOW_STRUCTURAL_TYPES: dict[str, frozenset[str]] = {
    "narrate": frozenset(
        {"narrator_system", "library", "history", "facts", "user", "recalled"}
    ),
    "ooc": frozenset({"system", "history", "facts", "user"}),
    "extract": frozenset({"system", "turn_payload"}),
    "summarize": frozenset({"system", "payload"}),
    "caption": frozenset({"system", "payload"}),
    "assistant": frozenset({"system", "history", "user"}),
    # The BTS tracker is a post-turn single-shot interaction: a system prompt and
    # one user payload (the turn block).
    "bts": frozenset({"system", "payload"}),
    # Wave 8 (D41/D49): mirrors bts — the critic's system prompt plus one
    # payload (the turn's prose, validator flags and state line).
    "critic": frozenset({"system", "payload"}),
    # W9.13: a lane is a system brief + its private block, then the JSON ask.
    "npc": frozenset({"system", "payload"}),
    # W9.20: the planner's authored surface is its directive; the context block
    # and the ask are structural (the node composes them from the log/state).
    "planner": frozenset({"system"}),
    # W9.22 §3: a proposal lane is a brief + its type-scoped payload (the
    # turn-touched entities for that data type plus the type digest), then the
    # JSON ask. Mirrors ``npc`` — one lane, one call.
    "proposals": frozenset({"system", "payload"}),
}

ModuleType = Literal[
    "text",
    "narrator_system",
    "library",
    "history",
    "facts",
    "user",
    "recalled",
    "system",
    "turn_payload",
    "payload",
]
ModuleRole = Literal["system", "user", "assistant"]


@dataclass
class PromptModule:
    """One ordered row of an interaction's model pipeline (D18).

    ``id`` is stable across edits so the store/UI can address a row; ``type``
    is either ``"text"`` or a structural **pipeline insertion point** (a type
    in :data:`FLOW_STRUCTURAL_TYPES` for the interaction). A structural
    module's content normally comes from the engine at build time; the one
    exception is the plain ``"system"`` inserter, whose NON-EMPTY ``content``
    overrides the engine-supplied system prompt (see :func:`compile_flow`). A
    ``text`` module carries a template body that may reference ``{{variable}}``
    names; ``role`` selects which chat role its rendered message uses
    (structural types emit their fixed role). ``enabled=False`` skips the
    module entirely.
    """

    id: str
    name: str
    type: ModuleType
    role: ModuleRole = "system"
    content: str = ""
    enabled: bool = True


#: Today's narrator pipeline exactly: narrator system, library doc, treated
#: history, the merged facts suffix, and the final user message.
#: ``from_default`` copies it with fresh ids so a user seeds an editable preset
#: from a known-good order.
DEFAULT_MODULES: list[PromptModule] = [
    PromptModule("narrator_system", "Narrator system prompt", "narrator_system"),
    PromptModule("library", "OpenUI component library", "library"),
    PromptModule("history", "Chat history", "history"),
    PromptModule("facts", "World facts", "facts"),
    PromptModule("user", "User message", "user"),
    # D22: the post-user `<recalled>` reference message (recall / world info /
    # summaries / future BTS). Last by default; presets may reorder or drop it.
    PromptModule("recalled", "Recalled context (RAG reference)", "recalled"),
]


#: Each interaction's default flow, mirroring its current call site exactly
#: (cite per flow):
#:
#: * ``narrate`` -> :data:`DEFAULT_MODULES`:
#:   :func:`fabulist.wiring.make_narrator_message_builder.build` ->
#:   :func:`fabulist.prompts.compiler.compile` (system + library) +
#:   ``history_from_log`` + the facts suffix + the final user message.
#: * ``ooc`` -> :func:`fabulist.wiring.make_ooc_message_builder.build`, which
#:   calls :func:`fabulist.prompts.narrator.assemble_messages` with
#:   ``system=OOC_SYSTEM``, ``history=history_from_log(limit=8)``, the folded
#:   ``facts_line_from_log``, and the user content.
#: * ``extract`` -> :meth:`fabulist.state.extractor.LLMExtractor.__call__`:
#:   ``[system: EXTRACTOR_SYSTEM_PROMPT, user: _turn_block(user_msg, narrative)]``;
#:   a repair retry appends ``assistant`` + ``REPAIR_INSTRUCTION`` after (the
#:   caller appends those — the base flow is the two-message request).
#: * ``summarize`` -> :func:`fabulist.graph.compaction.build_summarizer_messages`:
#:   ``[system: SUMMARIZER_SYSTEM_PROMPT, user: instruction + transcript]``.
#: * ``caption`` -> :func:`fabulist.media.images.build_caption_messages`:
#:   ``[system: CAPTION_SYSTEM, user: [text CAPTION_INSTRUCTION, image part]]``.
#: * ``assistant`` -> :func:`fabulist.assistants.manager.build_assistant_messages`:
#:   ``[system: per-definition persona, user: render_scene_digest(context)]``
#:   (no history in the base call; the flow's ``history`` row splices the
#:   caller-supplied list, empty by default).
DEFAULT_FLOWS: dict[str, list[PromptModule]] = {
    "narrate": DEFAULT_MODULES,
    "ooc": [
        PromptModule("ooc_system", "OOC system prompt", "system"),
        PromptModule("ooc_history", "Chat history", "history"),
        PromptModule("ooc_facts", "World facts", "facts"),
        PromptModule("ooc_user", "User message", "user"),
    ],
    "extract": [
        PromptModule("extract_system", "Extractor system prompt", "system"),
        PromptModule("extract_turn", "Turn payload", "turn_payload"),
    ],
    "summarize": [
        PromptModule("summarize_system", "Summarizer system prompt", "system"),
        PromptModule("summarize_payload", "Transcript payload", "payload"),
    ],
    "caption": [
        PromptModule("caption_system", "Captioner system prompt", "system"),
        PromptModule("caption_payload", "Image payload", "payload"),
    ],
    "assistant": [
        PromptModule("assistant_system", "Assistant system prompt", "system"),
        PromptModule("assistant_history", "Chat history", "history"),
        PromptModule("assistant_user", "Scene digest", "user"),
    ],
    # Wave 6 D21: the BTS world-state tracker (system prompt distilled from the
    # BTS v3.0.1 directive + the turn/prior-state payload).
    "bts": [
        PromptModule("bts_system", "BTS system prompt", "system"),
        PromptModule("bts_payload", "Turn + prior-state payload", "payload"),
    ],
    # Wave 8 D41/D49: the post-turn critic quality gate (its own system prompt +
    # one payload block: the turn's prose, the validator flags, the state line).
    "critic": [
        PromptModule("critic_system", "Critic system prompt", "system"),
        PromptModule("critic_payload", "Turn + flags payload", "payload"),
    ],
    # W9.13: one lane = the NPC decision pass. ``system`` carries the lane brief
    # plus that member's PRIVATE block (identity anchor, own state, witnessed
    # turns, beat, own last intent); ``payload`` is the JSON ask. The shared
    # prefix is not part of this flow — the node appends this tail to the
    # narrator's own prefix (D2).
    "npc": [
        PromptModule("npc_system", "Lane brief + private context", "system"),
        PromptModule("npc_payload", "Lane ask (JSON contract)", "payload"),
    ],
    # W9.20: the planner (steering plane). ONE authored row — the directive the
    # plan is written under (setup / refresh steering, follow a stated start,
    # vary, never restate what the context already says). The context block and
    # the refresh/steer ask are structural: the node composes them from the
    # folded log + the preset's planner config, so the row is the prompt
    # surface and nothing else is hand-copied.
    "planner": [
        PromptModule("planner_system", "Planner directive", "system"),
    ],
    # W9.22 §3: one proposal lane = the brief for ONE data type (what an
    # amendment of this kind looks like, and that nothing is written without the
    # owner) + the payload block (the turn-touched entities of that type, with
    # the turn's prose and the type digest). The gate that decides WHETHER the
    # lane runs is not part of this flow: it is the dispatcher's, above.
    "proposals": [
        PromptModule("proposals_system", "Proposal lane brief", "system"),
        PromptModule("proposals_payload", "Turn + type payload", "payload"),
    ],
}


def from_default() -> list[PromptModule]:
    """Fresh copy of :data:`DEFAULT_MODULES` with unique per-row ids.

    Every row gets a new ``uuid4().hex`` id so an edited preset never aliases
    the module singleton (mutating a copy cannot touch the default).
    """
    return [replace(module, id=uuid.uuid4().hex) for module in DEFAULT_MODULES]


def from_default_flow(interaction: str) -> list[PromptModule]:
    """Fresh copy of ``DEFAULT_FLOWS[interaction]`` with unique per-row ids.

    Unknown interactions raise :class:`ValueError` naming the valid set. Used by
    :func:`fabulist.prompts.preset_store.seed_from_default` to seed ALL flows.
    """
    default = DEFAULT_FLOWS.get(interaction)
    if default is None:
        raise ValueError(
            f"unknown interaction: {interaction!r}; valid interactions: "
            f"{', '.join(INTERACTIONS)}"
        )
    return [replace(module, id=uuid.uuid4().hex) for module in default]


def _module_signature(modules: list) -> list[tuple[str, str, str, bool]]:
    """Structural identity of a flow: (type, role, content, enabled) per row.

    Deliberately ignores ``id`` (seeded copies carry fresh uuids) and ``name``
    (cosmetic), so a seeded-but-unedited flow compares equal to its default.
    """
    return [
        (
            str(getattr(module, "type", "")),
            str(getattr(module, "role", "system")),
            str(getattr(module, "content", "")),
            bool(getattr(module, "enabled", True)),
        )
        for module in modules
    ]


def flow_is_default(interaction: str, modules: list) -> bool:
    """Whether ``modules`` are structurally identical to the interaction default.

    "All-default" means never edited: same types in the same order, same roles,
    empty/identical text bodies, same enabled flags. A reorder, an added text
    row, an edit, or a disable flips this to ``False`` (the flow is customized).
    Unknown interactions are never default. This is the gate the
    ``prompt_overrides`` deprecation shim uses (see
    :func:`fabulist.prompts.preset_store.compile_preset_flow`).
    """
    default = DEFAULT_FLOWS.get(interaction)
    if default is None:
        return False
    return _module_signature(modules) == _module_signature(default)


def _row_type(module: Any) -> str:
    """Structural type of a flow row (``PromptModule`` or a wire dict)."""
    if isinstance(module, dict):
        return str(module.get("type") or "")
    return str(getattr(module, "type", "") or "")


def _copy_row(module: Any) -> Any:
    """A copy of one flow row so the splicer never shares mutable state.

    ``flow_modules`` are :class:`PromptModule` dataclasses in every real call
    site; a plain dict row (a wire shape a caller might pass) is shallow-copied
    too. The splicer never edits rows, so this only guards a caller that mutates
    the returned list in place.
    """
    try:
        return replace(module)
    except TypeError:
        if isinstance(module, dict):
            # Preserve the row's class. Preset-store rows are PromptModuleDict
            # — a dict that ALSO answers attribute access (the documented
            # engine seam bridge); flattening to a plain dict would break
            # compile_flow's ``module.enabled`` attribute reads. Found at the
            # wiring splice integration (test_default_preset engine path).
            cls = module.__class__
            try:
                return cls(module)
            except Exception:  # noqa: BLE001 - exotic constructors degrade flat
                return dict(module)
        return module


def _active_session_modules(session_modules: list) -> list[dict]:
    """Fold session-module events to their current rows (latest-wins).

    Mirrors :func:`fabulist.cards.homes.session_modules_from_log`'s fold but
    is pure: it walks the supplied list, drops ``session_module_deleted``
    tombstones, replaces a ``module_id`` on re-append, and keeps first-seen
    order. Non-dict entries are ignored.
    """
    order: list[str] = []
    by_id: dict[str, dict] = {}
    for entry in session_modules:
        if not isinstance(entry, dict):
            continue
        kind = entry.get("event", "session_module")
        module_id = entry.get("module_id")
        if kind == "session_module_deleted":
            if isinstance(module_id, str) and module_id in by_id:
                by_id.pop(module_id)
                order.remove(module_id)
            continue
        if not isinstance(module_id, str) or not module_id:
            continue
        if module_id not in by_id:
            order.append(module_id)
        by_id[module_id] = entry
    return [by_id[module_id] for module_id in order]


def _session_module_row(entry: dict) -> PromptModule | None:
    """Convert one active session-module payload into a text ``PromptModule``.

    Returns ``None`` for a disabled row or a blank body (an empty text module
    is prompt noise). ``type`` is always ``"text"`` — the session module owns
    its rendered content; ``role`` selects the chat role.
    """
    if not entry.get("enabled", True):
        return None
    content = entry.get("content")
    content = "" if content is None else str(content)
    if not content.strip():
        return None
    role = entry.get("role") or "system"
    if role not in ("system", "user", "assistant"):
        role = "system"
    return PromptModule(
        id=str(entry.get("module_id") or uuid.uuid4().hex),
        name=str(entry.get("name") or "Session module"),
        type="text",
        role=role,
        content=content,
        enabled=True,
    )


def splice_session_modules(flow_modules: list, session_modules: list) -> list:
    """Splice session-carried modules into a flow at their anchors (D29).

    The narrator builder calls this immediately before
    :func:`compile_flow` with the active preset's narrate rows and the session's
    current module list (``session_modules_from_log``). Returns a NEW list;
    neither input list nor its rows are mutated.

    Anchors:

    * ``top`` — inserted before the first module of the flow.
    * ``post_history`` — inserted immediately after the ``history`` structural
      inserter. When the flow has no ``history`` row (a pre-``recalled`` saved
      preset) it falls back to just before the first ``user``/``facts`` row,
      else appends last.

    Modules within an anchor keep the order their events first appeared.
    Disabled rows and tombstoned ids are dropped (they never reach the prompt).
    Unknown anchors are ignored. Non-session-module entries are ignored.
    """
    rows = _active_session_modules(session_modules)
    top: list[Any] = []
    post_history: list[Any] = []
    for entry in rows:
        row = _session_module_row(entry)
        if row is None:
            continue
        anchor = entry.get("anchor")
        if anchor == "top":
            top.append(row)
        elif anchor == "post_history":
            post_history.append(row)

    base = [_copy_row(module) for module in flow_modules]

    post_at: int | None = None
    for index, module in enumerate(base):
        if _row_type(module) == "history":
            post_at = index + 1
            break
    if post_at is None:
        for index, module in enumerate(base):
            if _row_type(module) in ("user", "facts"):
                post_at = index
                break
    if post_at is None:
        post_at = len(base)

    combined = top + base
    insert_at = len(top) + post_at
    return combined[:insert_at] + post_history + combined[insert_at:]


def _part_message(messages: list[dict], role: str, content: object) -> None:
    """Append one structural message, skipping blank-string content.

    A facts/user/payload part whose resolved content is an empty (or
    whitespace-only) string emits NOTHING — empty user messages are prompt
    noise. Multipart content lists append unconditionally.
    """
    if content is None:
        return
    if isinstance(content, str):
        if not content.strip():
            return
    messages.append({"role": role, "content": content})


def compile_flow_with_boundary(
    interaction: str, modules: list, parts: dict, boundary_out: dict | None = None
) -> tuple[list[dict], int]:
    """:func:`compile_flow` plus the assembly boundaries, out-of-band.

    Returns ``(messages, prefix_end)`` where ``prefix_end`` is the MESSAGE
    index of the history splice point — the position immediately before the
    ``history`` rows are appended — well-defined even when the history block
    is empty. A flow with NO enabled ``history`` row reuses the builder's
    flow-row splice anchor (the ``post_at`` logic: after a ``history`` row if
    one exists, else at the first ``user``/``facts`` row, else the flow end),
    translated to message coordinates by the same emission rules — never
    re-derived downstream, and never inferred from roles or content shapes
    (authored presets may place user/assistant-role ``text`` rows before the
    boundary; §5.1 prohibits heuristic detectors).

    ``boundary_out`` (optional dict) additionally receives
    ``boundary_out["def_end"]`` — one past the FINAL user message emitted by
    the flow (issue #26: the ``<preset_def>`` bound closes there; every row
    served after it is the per-turn runtime bound) — and
    ``boundary_out["envelope_at"]``, the index of the per-turn engine envelope
    message inserted immediately before that message (issue #44; ``None``
    when the flow carries no ``recalled`` row or no envelope is composed). It
    is captured at the emission point of the
    ``user``/``facts``/``turn_payload``/``payload`` rows, never re-derived from
    roles or content downstream. A flow that emits no user turn has no
    post-user region: ``def_end`` is then the whole compiled list.

    Message assembly is byte-identical to :func:`compile_flow`; the boundaries
    are tracked alongside, not computed after the fact.
    """
    if interaction not in FLOW_STRUCTURAL_TYPES:
        raise ValueError(
            f"unknown interaction: {interaction!r}; valid interactions: "
            f"{', '.join(INTERACTIONS)}"
        )
    valid = FLOW_STRUCTURAL_TYPES[interaction]
    variables = parts.get("variables") or {}
    messages: list[dict] = []
    # §5.1 boundary tracking: message count at each row's start (disabled rows
    # emit nothing, so the count carries across them), plus the splice point
    # itself once an enabled ``history`` row extends its messages.
    msg_at_row: dict[int, int] = {}
    boundary: int | None = None
    # §5.1 (issue #26): one past the final user message — the <preset_def>
    # close. Tracked at the emission point of each user-role structural row;
    # 0 means "no user turn emitted" (handled after the loop).
    def_end = 0
    # §44: the per-turn engine envelope body, stashed by the ``recalled`` row.
    envelope_body: str | None = None
    for index, module in enumerate(modules):
        msg_at_row[index] = len(messages)
        if not module.enabled:
            continue
        mtype = module.type
        if mtype == "text":
            messages.append(
                {
                    "role": module.role,
                    "content": render_template(module.content, variables),
                }
            )
        elif mtype in valid:
            if mtype == "narrator_system":
                messages.append({"role": "system", "content": parts["narrator_system"]})
            elif mtype == "library":
                messages.append({"role": "system", "content": parts["library"]})
            elif mtype == "system":
                # Module-content override seam: a non-empty stored body wins
                # over the engine part, rendered like a text module so preset
                # authors get {{variable}} resolution. Empty/whitespace-only
                # keeps today's byte-identical behavior (parts["system"]).
                stored = str(getattr(module, "content", "") or "")
                if stored.strip():
                    messages.append(
                        {
                            "role": "system",
                            "content": render_template(stored, variables),
                        }
                    )
                else:
                    messages.append(
                        {"role": "system", "content": parts["system"]}
                    )
            elif mtype == "history":
                boundary = len(messages)
                messages.extend(parts["history"])
            elif mtype == "facts":
                _part_message(messages, "user", parts.get("facts"))
                def_end = len(messages)
            elif mtype == "user":
                _part_message(messages, "user", parts.get("user_content"))
                def_end = len(messages)
            elif mtype == "recalled":
                # The per-turn engine envelope. Issue #44 (owner ruling
                # 2026-10-24): its POSITION is a contract, not a flow-row
                # choice — it rides ONE ephemeral ``assistant`` message
                # inserted immediately BEFORE the final user message, at
                # assembly time. The row therefore only DECIDES whether the
                # envelope ships with this flow; the body is stashed here and
                # inserted after the loop, once ``def_end`` is known.
                # ``engine_tail`` is the composed envelope; ``recalled`` is the
                # pre-contract key (memory body). Blank/absent -> nothing
                # (never an empty envelope).
                body = parts.get("engine_tail") or parts.get("recalled")
                if isinstance(body, str) and body.strip():
                    envelope_body = body
            elif mtype == "turn_payload":
                _part_message(messages, "user", parts.get("turn_payload"))
                def_end = len(messages)
            elif mtype == "payload":
                # payload may be a string OR a multipart content list
                # (vision); lists are never "empty text"
                _part_message(messages, "user", parts.get("payload"))
                def_end = len(messages)
            else:  # pragma: no cover - every FLOW_STRUCTURAL_TYPES entry is handled
                raise ValueError(
                    f"unhandled structural type {mtype!r} for interaction "
                    f"{interaction!r}"
                )
        else:
            valid_types = ", ".join(sorted(valid | {"text"}))
            raise ValueError(
                f"unknown module type {mtype!r} for interaction "
                f"{interaction!r}; valid types: {valid_types}"
            )
    if boundary is None:
        # No ENABLED history row spliced messages: fall back to the builder's
        # flow-row anchor (the ``post_at`` logic — a FLOW-ROW position), then
        # translate to message coordinates by the same emission rules.
        anchor: int | None = None
        for index, module in enumerate(modules):
            if _row_type(module) == "history":
                anchor = index
                break
        if anchor is None:
            for index, module in enumerate(modules):
                if _row_type(module) in ("user", "facts"):
                    anchor = index
                    break
        boundary = msg_at_row.get(anchor, len(messages)) if anchor is not None else len(messages)
    # Issue #44: the prefix is ONE system message. The engine's narrator-system
    # row and the library row are separate system messages by construction (the
    # flow order is data), and a stock Jinja chat template refuses any non-first
    # system message — so fold the leading system run into the narrator-system
    # message, behind the existing section divider. Content is untouched and in
    # served order; both boundaries shift with the fold.
    final_def_end = def_end or len(messages)
    messages, (final_def_end, boundary) = fold_system_prefix(
        messages, (final_def_end, boundary)
    )
    # Issue #44: the per-turn engine envelope rides ONE ephemeral ``assistant``
    # message inserted immediately BEFORE the final user message — never a
    # system message (a stock Jinja template refuses any non-first system
    # message) and never inside the player's message. Positional, from the
    # out-of-band ``def_end``; the flow row only decided that it ships.
    messages, final_def_end, envelope_at = insert_engine_envelope(
        messages, final_def_end, envelope_body
    )
    if boundary_out is not None:
        # No user-role row emitted a message -> no post-user region: the whole
        # compiled list is the definition, and the caller's appended tail is
        # still runtime by position.
        boundary_out["def_end"] = final_def_end
        boundary_out["envelope_at"] = envelope_at
    return messages, boundary


def compile_flow(
    interaction: str, modules: list, parts: dict
) -> list[dict]:
    """Render an interaction's ordered OpenAI ``messages`` list from modules.

    Same rendering rules as :func:`compile_preset_messages` (the narrator's
    alias for ``compile_flow("narrate", ...)``): walk modules in order, skip
    disabled rows, render ``text`` bodies through
    :func:`fabulist.prompts.variables.render_template` against
    ``parts["variables"]``, and expand each structural type by its fixed role.

    Structural expansion by type (the parts dict supplies the content):

    * ``narrator_system`` / ``library`` -> one system message (the compiled
      narrator system / library doc — always engine-supplied, never a row
      body).
    * ``system`` -> one system message. A row with NON-EMPTY stored
      ``content`` wins: it renders as the flow's system message, resolving
      ``{{variable}}`` tokens exactly like a ``text`` module. An EMPTY (or
      whitespace-only) row keeps the byte-identical default — the engine part
      ``parts["system"]`` (the interaction's compiled default prompt) is
      emitted. This is the module-content override seam: preset-authored
      system text rides the flow as data instead of a code constant.
    * ``history`` -> splices the supplied ``parts["history"]`` verbatim.
    * ``facts`` / ``user`` -> one user message (``parts["facts"]`` /
      ``parts["user_content"]``).
    * ``turn_payload`` / ``payload`` -> one user message (``parts[<type>]`` may
      be a string or a multipart content list).

    ``interaction`` must be one of :data:`INTERACTIONS`; a structural type not
    valid for that flow raises :class:`ValueError` listing the valid types.

    The emission loop lives in :func:`compile_flow_with_boundary` (single
    source); this entry point returns the messages alone for every existing
    caller.
    """
    return compile_flow_with_boundary(interaction, modules, parts)[0]


def compile_preset_messages(modules: list, parts: dict) -> list[dict]:
    """Render the narrator pipeline (D18 back-compat alias).

    Kept as the narrator-facing entry point: identical to
    ``compile_flow("narrate", modules, parts)``. ``parts`` carries the
    engine-resolved structural content the wiring builder produces:
    ``narrator_system`` (str), ``library`` (str), ``history`` (list of
    ``{"role","content"}`` messages), ``facts`` (str), and ``user_content``
    (str). An optional ``variables`` mapping feeds ``text`` module templates
    (defaults to ``{}``). Disabled modules are skipped; any type outside the
    narrator structural set raises :class:`ValueError`.
    """
    return compile_flow("narrate", modules, parts)


def compile_preset_messages_with_boundary(
    modules: list, parts: dict, boundary_out: dict | None = None
) -> tuple[list[dict], int]:
    """:func:`compile_preset_messages` plus the assembly boundaries (§5.1).

    Returns ``(messages, prefix_end)`` — the history-splice MESSAGE index — and
    fills ``boundary_out["def_end"]`` (one past the final user message) when a
    dict is supplied: the wiring seam translates both per-path into final-list
    coordinates for the ``<preset_def>`` / ``<preset_runtime>`` wrapper (issue
    #26). Message assembly is byte-identical to
    :func:`compile_preset_messages`; see :func:`compile_flow_with_boundary`
    for the boundary rules (including the no-history-row ``post_at`` anchor
    fallback).
    """
    return compile_flow_with_boundary("narrate", modules, parts, boundary_out)
