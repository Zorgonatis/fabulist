#!/usr/bin/env python
"""Generate the agent map (D46) from the code — never a hand-copied list.

PLAN D23 requires an explicit per-agent contract: trigger, input sources,
output schema, disposition (consumed or deleted), debug-capture coverage and
usage per call. No generated artifact implemented it, and the human-authored
``docs/PROMPT-SURFACES.md`` is stale (still says *sidekick* post-D38). This
script emits ``prompts/generated/agent-map.json`` derived from the code so the
map cannot silently drift:

* **which agents exist** — the registry interactions come from
  :data:`fabulist.prompts.agent_sets.INTERACTIONS`; their default agents come
  from :data:`~fabulist.prompts.agent_sets.DEFAULT_BINDINGS`; their taxonomy
  type/task slot from
  :data:`~fabulist.connections.taxonomy.INTERACTION_TASKS`; their flows from
  :data:`fabulist.prompts.modules.DEFAULT_FLOWS` /
  :data:`~fabulist.prompts.modules.FLOW_STRUCTURAL_TYPES`;
* **their message builders** — discovered by parsing ``src/fabulist/wiring.py``
  (the ``_*_flow_messages`` functions, plus the narrate/ooc engine builders),
  never re-typed;
* **non-registry model callers** — the DSL re-synthesizer, the reranker, the
  local whisper STT and the embedder are listed with ``registry: false`` after
  their source symbols are verified present (a rename fails the run loudly).

Usage::

    uv run python scripts/generate_agent_map.py            # write the artifact
    uv run python scripts/generate_agent_map.py --check    # exit 1 on drift

Mirrors ``frontend/scripts/generate-library-spec.mjs``: output is deterministic
(sorted keys, no timestamps) and rerunning it is byte-identical.

The semantic fields (output schema / disposition) are authored here; the
display title/blurb/trigger are read from
:data:`fabulist.prompts.catalog.INTERACTION_CATALOG` (the single authoring
source shared with ``GET /api/prompts/steps``). The *enumeration* and every
cited seam is read from the code, and a new interaction or a renamed builder
fails generation instead of silently missing.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import inspect
import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_MAP_PATH = REPO_ROOT / "prompts" / "generated" / "agent-map.json"
WIRING_PATH = REPO_ROOT / "src" / "fabulist" / "wiring.py"

#: The narrator/OOC builders do not follow the ``_*_flow_messages`` naming
#: convention (narrate compiles through the W2.1 directive compiler, OOC through
#: ``assemble_messages``), so they are named here and verified present by AST —
#: a rename still fails loudly.
_ENGINE_BUILDERS: dict[str, str] = {
    "narrate": "make_narrator_message_builder",
    "ooc": "make_ooc_message_builder",
}

#: Authored per-interaction contract fields the code does not spell out
#: (``output_schema`` / ``disposition``). The human title/blurb/trigger live in
#: :data:`fabulist.prompts.catalog.INTERACTION_CATALOG` — the single authoring
#: source shared with the ``GET /api/prompts/steps`` endpoint — so the two
#: surfaces cannot drift. Every key of ``INTERACTIONS`` MUST have an entry in
#: both places or :func:`build_agent_map` raises; the enumeration itself is
#: never authored here.
_INTERACTION_AUDIT: dict[str, dict[str, str]] = {
    "narrate": {
        "output_schema": (
            "prose + openui DSL statements (components from "
            "prompts/generated/library.spec.json); never state/env/clock (D9/D38)"
        ),
        "disposition": (
            "graph.nodes narrator parser -> narrative event (log, D10) + DSL "
            "segments -> transcript + OpenUI render"
        ),
    },
    "ooc": {
        "output_schema": "plain prose reply (no narrative/state commit)",
        "disposition": (
            "streamed to the client; engine appends a `user` + `ooc` event "
            "(graph/engine.py _run_ooc)"
        ),
    },
    "extract": {
        "output_schema": (
            "one Δ[...] pipe-YAML state-delta line over extractor.DELTA_KEYS "
            "+ action_class (one repair retry on malformed output)"
        ),
        "disposition": (
            "state-delta event -> the log's one state fold (D9) -> state dashboard"
        ),
    },
    "summarize": {
        "output_schema": "one compaction summary text (covers_seqs span)",
        "disposition": (
            "summary event (log) -> narrator facts block; raw narratives retained "
            "(D9/D10)"
        ),
    },
    "caption": {
        "output_schema": "one image caption string",
        "disposition": (
            "image_caption system event -> folded asset caption; VLM content-part "
            "fallback (D14)"
        ),
    },
    "assistant": {
        "output_schema": (
            "assistant reply prose + optional openui DSL "
            "(`asst = AssistantCard(...)` statement)"
        ),
        "disposition": (
            "assistant_reply + assistant_panel system events -> panel/transcript "
            "surfaces (D25)"
        ),
    },
    "bts": {
        "output_schema": (
            "one BTS block over the nine v3.0.1 categories; delta by default, "
            "checkpoint on MODE: checkpoint"
        ),
        "disposition": (
            "system/world_state event (latest-wins) + flat state-delta fold keys "
            "(loc/weather/tone/inv); injected pre-turn as continuity notes (D21)"
        ),
    },
    "attribution": {
        "output_schema": "per-quote attributions [{speaker, quote}]",
        "disposition": (
            "attribution system event; first-sighting speaker colours (engine "
            "registry); DialogueSpotlight (D25)"
        ),
    },
    "npc": {
        "output_schema": (
            "one JSON lane result {intent, narrative, lines:[{text, tone}]} "
            "(no forced tool_choice, R29; shape failure skips the lane)"
        ),
        "disposition": (
            "system/npc_lane event per member + one npc_lanes summary row; the "
            "lane results are rendered into ONE system message at the END of the "
            "narrator prompt (W9.13/D5) and stitched into the turn's prose"
        ),
    },
    "planner": {
        "output_schema": (
            "ONE plan payload {strings: [4-6], datetime?, weather?, forecast?, "
            "established?, <preset-defined extras>} (no forced tool_choice on a "
            "connection without the capability, M6/§4)"
        ),
        "disposition": (
            "plan system event (created_from=graph, latest wins, D9) + a "
            "state-delta seeding the planned weather/forecast (and the opening "
            "time on turn 1); the narrator prompt and the lanes' beat read its "
            "render. A failed or refused refresh keeps the previous plan and "
            "never fails the turn"
        ),
    },
    "critic": {
        "output_schema": (
            "one fenced JSON verdict {verdict: ok|diffs|fail, reason, edits} "
            "(a legacy regen_feedback key is tolerated and ignored)"
        ),
        "disposition": (
            "ok keeps the turn; diffs -> revision event applied on top (D10), "
            "edit by edit (a rejected edit is logged with its diagnosis and the "
            "landed subset still commits); "
            "fail -> the turn is KEPT and the verdict logged (the regeneration "
            "is retired, W9.24 §3.3.7); a rejected edit or an unparseable "
            "verdict is non-fatal (keep the delivered turn)"
        ),
    },
    "proposals": {
        "output_schema": (
            "ONE JSON batch of {kind, action, target, patch, evidence} rows for "
            "a single data type (world | actors | books); no prose, no "
            "statements, and a malformed batch skips the lane (W9.22 §3)"
        ),
        "disposition": (
            "a system/asset_proposal event per item, judged per item by the "
            "decision model and queued for the owner's review home; NOTHING is "
            "written to an asset — agents propose, the owner disposes (W9.22 §1 "
            "doctrine 3), and apply/undo are later log appends (Wave 4)"
        ),
    },
}

#: Interactions whose agent caller is NOT wrapped by ``wiring.make_agent_caller``
#: (no debug capture). Extractor: ``state/extractor.py`` uses the sync client and
#: records usage itself. Every other registry interaction rides PresetAgentCaller
#: / make_agent_caller and IS captured (session toggle permitting).
_NO_DEBUG_CAPTURE: frozenset[str] = frozenset({"extract"})

#: Non-registry model callers (D46: they MUST be listed or they stay invisible).
#: ``call_site`` symbols are verified present in their source by AST before the
#: map is emitted, so a rename fails generation loudly.
_NON_REGISTRY: tuple[dict[str, Any], ...] = (
    {
        "name": "dsl_resynthesizer",
        "default_agent": "narrator",
        "trigger": (
            "in-turn, only when the deterministic DSL gate cannot repair the "
            "narrator reply (validate_dsl not ok)"
        ),
        "input_builder": "fabulist.graph.nodes._repair_messages",
        "call_site": "fabulist.graph.nodes._model_resynthesizer",
        "output_schema": (
            "a corrected narrator reply (prose + openui DSL), re-segmented"
        ),
        "disposition": (
            "replaces the turn's DSL segments in graph.nodes.narrator "
            "(one attempt, capped by revalidate_with_retry)"
        ),
        "debug_capture": True,
        "usage": True,
        "note": (
            "second narrator model call; loads the narrator caller, so its usage "
            "and debug capture ride that caller (labelled 'narrate') and it is "
            "indistinguishable as its own pass"
        ),
    },
    {
        "name": "reranker",
        "default_agent": None,
        "trigger": (
            "memory recall, only when rerank is enabled (default off, D25)"
        ),
        "input_builder": "fabulist.memory.rerank.build_rerank_payload",
        "call_site": "fabulist.memory.rerank.rerank_documents",
        "output_schema": "scored, best-first [{index, score, ...}] entries",
        "disposition": (
            "reorders the pre-turn recall block; the score is not persisted"
        ),
        "debug_capture": False,
        "usage": False,
        "note": (
            "direct httpx call to the embeddings/rerank service; no registry "
            "interaction, no usage row, no debug capture"
        ),
    },
    {
        "name": "stt",
        "default_agent": None,
        "trigger": "audio attachment ingestion (local whisper transcription)",
        "input_builder": "fabulist.media.stt.transcribe",
        "call_site": "fabulist.media.stt.transcribe",
        "output_schema": "SttResult {text, language, duration_s}",
        "disposition": "transcript text -> asset/turn text event",
        "debug_capture": False,
        "usage": False,
        "note": (
            "in-process faster-whisper model (no HTTP service); no registry "
            "interaction, no usage row, no debug capture"
        ),
    },
    {
        "name": "embedder",
        "default_agent": None,
        "trigger": (
            "pre-turn recall query embed + post-turn memory indexing "
            "(D13 EmbeddingGemma service)"
        ),
        "input_builder": "fabulist.memory.embedder._build_request",
        "call_site": "fabulist.memory.embedder.embed_texts",
        "output_schema": "one 768-dim vector per input text",
        "disposition": "derived memory index / recall query (D5, never truth)",
        "debug_capture": False,
        "usage": False,
        "note": (
            "direct httpx call to POST {endpoint}/v1/embeddings; no registry "
            "interaction, no usage row, no debug capture"
        ),
    },
)

#: The D46 headline gap: the run-time resynthesis call has no accounting.
RESYNTHESIZER_NOTE = (
    "The DSL re-synthesis pass is a second, uncounted narrator model call: it is "
    "absent from INTERACTIONS (not preset-bindable by name), gets no dedicated "
    "usage row or debug-capture row, and is not a distinct step on the turn "
    "stream. Its cost/attribution rides the narrator caller (labelled 'narrate'), "
    "so today it is invisible as its own pass (RESUME 2026-10-12 gotcha)."
)


def _literal_str(node: ast.AST) -> str | None:
    """Return a constant ``str`` node's value, else ``None`` (ast.Constant)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _compiled_interaction(fn: ast.FunctionDef) -> str | None:
    """The interaction a flow builder compiles, from its ``compile_preset_flow`` call."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "compile_preset_flow" or len(node.args) < 2:
            continue
        interaction = _literal_str(node.args[1])
        if interaction:
            return interaction
    return None


def _discover_builders() -> dict[str, str]:
    """interaction -> dotted ``wiring`` builder, read from ``wiring.py`` source."""
    tree = ast.parse(WIRING_PATH.read_text(encoding="utf-8"))
    builders: dict[str, str] = {}
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        names.add(node.name)
        if node.name.endswith("_flow_messages"):
            interaction = _compiled_interaction(node)
            if interaction:
                builders[interaction] = f"fabulist.wiring.{node.name}"
    for interaction, fn_name in _ENGINE_BUILDERS.items():
        if fn_name not in names:
            raise RuntimeError(
                f"wiring.{fn_name} not found (builder renamed?); the {interaction} "
                "flow builder could not be resolved"
            )
        builders[interaction] = f"fabulist.wiring.{fn_name}"
    return builders


def _assert_call_site(dotted: str) -> None:
    """Fail loudly when a cited non-registry symbol no longer exists."""
    module_path, _, symbol = dotted.rpartition(".")
    module = importlib.import_module(module_path)
    source = inspect.getsourcefile(module)
    if source is None:  # pragma: no cover - every cited module has a file
        raise RuntimeError(f"no source file for {module_path}")
    tree = ast.parse(Path(source).read_text(encoding="utf-8"))
    top_level = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if symbol not in top_level:
        raise RuntimeError(
            f"non-registry caller {dotted} is gone (renamed/removed); update "
            "_NON_REGISTRY in scripts/generate_agent_map.py"
        )


def build_agent_map() -> dict[str, Any]:
    """Assemble the deterministic agent map from the code. Raises on drift."""
    from fabulist.connections.taxonomy import INTERACTION_TASKS
    from fabulist.prompts.agent_sets import DEFAULT_BINDINGS, INTERACTIONS
    from fabulist.prompts.catalog import INTERACTION_CATALOG
    from fabulist.prompts.modules import DEFAULT_FLOWS, FLOW_STRUCTURAL_TYPES

    builders = _discover_builders()

    agents: list[dict[str, Any]] = []
    for interaction in INTERACTIONS:
        if interaction not in _INTERACTION_AUDIT:
            raise RuntimeError(
                f"interaction {interaction!r} has no _INTERACTION_AUDIT entry; "
                "author output_schema/disposition in scripts/generate_agent_map.py"
            )
        catalog_meta = INTERACTION_CATALOG.get(interaction)
        if catalog_meta is None:
            raise RuntimeError(
                f"interaction {interaction!r} has no fabulist.prompts.catalog "
                "entry; author title/blurb/trigger in src/fabulist/prompts/catalog.py"
            )
        builder = builders.get(interaction)
        if builder is None:
            raise RuntimeError(
                f"no message builder found for interaction {interaction!r} in "
                "src/fabulist/wiring.py"
            )
        binding = DEFAULT_BINDINGS.get(interaction) or {}
        meta = _INTERACTION_AUDIT[interaction]
        flow = DEFAULT_FLOWS.get(interaction) or []
        # W9.25 §3.2: the slot the interaction belongs to (type + task). The
        # preset binding layer above it is the caller's; this is the layer's own
        # default, so the map shows WHICH model class a step actually rides.
        type_name, task_name = INTERACTION_TASKS[interaction]
        agents.append(
            {
                "name": interaction,
                "registry": True,
                "type": type_name,
                "task": task_name,
                "default_agent": binding.get("agent"),
                "trigger": catalog_meta.trigger,
                "input_builder": builder,
                "flow": [module.type for module in flow],
                "structural_types": sorted(FLOW_STRUCTURAL_TYPES.get(interaction, ())),
                "output_schema": meta["output_schema"],
                "disposition": meta["disposition"],
                "debug_capture": interaction not in _NO_DEBUG_CAPTURE,
                "usage": True,
            }
        )

    for entry in _NON_REGISTRY:
        _assert_call_site(entry["call_site"])
        # Not registry interactions: no binding layer, so no taxonomy slot.
        agents.append({"registry": False, "type": None, "task": None, **entry})

    agents.sort(key=lambda agent: agent["name"])

    return {
        "schema_version": 1,
        "generated_from": {
            "interactions": "fabulist.prompts.agent_sets.INTERACTIONS",
            "bindings": "fabulist.prompts.agent_sets.DEFAULT_BINDINGS",
            "interaction_tasks": "fabulist.connections.taxonomy.INTERACTION_TASKS",
            "interaction_catalog": "fabulist.prompts.catalog.INTERACTION_CATALOG",
            "flows": "fabulist.prompts.modules.DEFAULT_FLOWS",
            "flow_structural_types": "fabulist.prompts.modules.FLOW_STRUCTURAL_TYPES",
            "message_builders": "src/fabulist/wiring.py",
        },
        "resynthesizer_note": RESYNTHESIZER_NOTE,
        "agents": agents,
    }


def render(data: dict[str, Any]) -> str:
    """Render the map as deterministic JSON text (sorted keys, trailing newline)."""
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def check(path: Path | None = None) -> bool:
    """Return ``True`` when the file at ``path`` is up to date with the code."""
    target = path or AGENT_MAP_PATH
    try:
        existing = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return False
    return existing == render(build_agent_map())


def write(path: Path | None = None) -> Path:
    """Write the generated map to ``path`` (default the committed artifact)."""
    target = path or AGENT_MAP_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render(build_agent_map()), encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; ``--check`` returns non-zero on drift."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed artifact matches the code; exit 1 on drift",
    )
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    if args.check:
        if not check():
            print(
                "DRIFT: prompts/generated/agent-map.json is stale — run: "
                "uv run python scripts/generate_agent_map.py",
                file=sys.stderr,
            )
            return 1
        print("ok prompts/generated/agent-map.json")
        return 0

    print(f"wrote {write()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
