#!/usr/bin/env python
"""Generate a draw.io (mxGraph) mock-up file from the flow manifest (W9).

Purpose: give the owner a REAL markup surface for the graph re-architecture
(R9 edge-policy decisions, W9.4 Studio design, flow re-wiring sketches).

Reads ``prompts/generated/flow-manifest.json`` and writes
``docs/diagrams/flow-graph.drawio`` with two pages:

  1. "Turn flow"      — the 11-node turn graph + off-turn flows attached at
                        their anchors (faithful to the in-app board).
  2. "Narrate prompt" — the narrate composition sub-graph (R13): modules in
                        their stable-prefix / neutral / dynamic-tail regions,
                        fanning into the assembled prompt.

DELIBERATELY NOT --check-pinned: the file is the owner's to edit in draw.io;
rerun this script to regenerate a fresh copy after the manifest changes
(your manual edits are then overwritten — move them aside first).

Usage: uv run python scripts/generate_flow_drawio.py [--out PATH]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "prompts/generated/flow-manifest.json"
DEFAULT_OUT = REPO / "docs/diagrams/flow-graph.drawio"

NODE_W, NODE_H = 170, 54
GAP_X, GAP_Y = 60, 46

KIND_STYLE = {
    "model_pass": "rounded=1;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;",
    "gate": "rounded=1;whiteSpace=wrap;html=1;fillColor=#fff2cc;strokeColor=#d6b656;",
    "fixed": "rounded=1;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#999999;",
    "engine": "rounded=1;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#999999;",
    "hook": "rounded=1;whiteSpace=wrap;html=1;fillColor=#e1d5e7;strokeColor=#9673a6;",
    "embedder": "rounded=1;whiteSpace=wrap;html=1;fillColor=#d5e8d4;strokeColor=#82b366;",
    "stage": "rounded=1;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;",
    "branch": "rhombus;whiteSpace=wrap;html=1;fillColor=#ffe6cc;strokeColor=#d79b00;",
    "trigger": "rounded=1;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#999999;",
    "terminal": "rounded=1;whiteSpace=wrap;html=1;fillColor=#d5e8d4;strokeColor=#82b366;",
}

BRANCH_COLOR = {"ok": "#82b366", "diffs": "#d6b656", "fail": "#d08770"}


def nl(label: str) -> str:
    """draw.io label with a newline."""
    return escape(label).replace("\n", "&#10;")


def node_label(node: dict) -> str:
    lines = [node["id"]]
    kind = (node.get("runs") or {}).get("kind") or node.get("kind", "")
    agent = (node.get("agent") or {})
    bound = agent.get("bound_agent") or agent.get("default_agent")
    sub = kind if not bound else f"{kind} · {bound}"
    if sub:
        lines.append(sub)
    comp = node.get("composition")
    if comp:
        regions = [r["id"] for r in comp.get("regions", [])]
        lines.append("[" + ", ".join(regions) + "]")
    return nl("\n".join(lines))


def layered_positions(nodes: list[dict], edges: list[dict]) -> dict[str, tuple[int, int]]:
    """Longest-path layers over forward edges (loop_back/attach excluded).

    Horizontal (lane B): the LAYER maps to x, so the flow reads left->right and an
    edge leaves its source's right face; members of one layer stack on y. This is
    the same layout the in-app board draws (``FlowGraph.svelte`` ``layoutGraph``)
    when its host pane is at least as wide as it is tall.
    """
    ids = [n["id"] for n in nodes]
    forward: dict[str, list[str]] = {i: [] for i in ids}
    indeg: dict[str, int] = {i: 0 for i in ids}
    for e in edges:
        if e.get("kind") in ("loop_back", "attach"):
            continue
        if e["from"] in forward and e["to"] in forward:
            forward[e["from"]].append(e["to"])
            indeg[e["to"]] += 1
    layer = {i: 0 for i in ids}
    queue = [i for i in ids if indeg[i] == 0]
    seen: set[str] = set()
    while queue:
        cur = queue.pop(0)
        seen.add(cur)
        for nxt in forward[cur]:
            layer[nxt] = max(layer[nxt], layer[cur] + 1)
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)
    # Cycle leftovers: the critic --fail--> narrate branch closes a loop, so the
    # nodes behind it never reach in-degree 0. Stack them one layer past the
    # deepest, in declaration order — the SAME fallback the in-app layout uses
    # (``FlowGraph.svelte`` ``layoutGraph``); without it the whole back half of the
    # turn would pile into layer 0 and the page would read sideways.
    deepest = max((layer[i] for i in seen), default=0)
    for i in ids:
        if i not in seen:
            deepest += 1
            layer[i] = deepest
    by_layer: dict[int, list[str]] = {}
    for i in ids:
        by_layer.setdefault(layer[i], []).append(i)
    pos: dict[str, tuple[int, int]] = {}
    for li, members in by_layer.items():
        for ci, mid in enumerate(members):
            pos[mid] = (li * (NODE_W + GAP_X), ci * (NODE_H + GAP_Y))
    return pos


def cells_for_graph(graph: dict, *, kind_of_node) -> list[str]:
    nodes, edges = graph["nodes"], graph["edges"]
    pos = layered_positions(nodes, edges)
    out: list[str] = []
    cell_id = 1
    mx_id: dict[str, str] = {}
    for n in nodes:
        mx_id[n["id"]] = f"n{cell_id}"
        cell_id += 1
    for n in nodes:
        x, y = pos[n["id"]]
        style = KIND_STYLE.get(kind_of_node(n), KIND_STYLE["fixed"])
        out.append(
            f'<mxCell id="{mx_id[n["id"]]}" value={quoteattr(node_label(n))} '
            f'style="{style}" vertex="1" parent="1">'
            f'<mxGeometry x="{x}" y="{y}" width="{NODE_W}" height="{NODE_H}" as="geometry"/>'
            f"</mxCell>"
        )
    for e in edges:
        style = "edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;"
        label = e.get("label") or e.get("condition") or ""
        if e["kind"] == "branch":
            color = BRANCH_COLOR.get(label, "#6c8ebf")
            style += f"strokeColor={color};fontColor={color};"
        if e["kind"] == "loop_back":
            style += "dashed=1;strokeColor=#9673a6;"
        if e["kind"] == "attach":
            style += "dashed=1;strokeColor=#999999;"
        cap = f" (cap {e['cap']})" if e.get("cap") else ""
        out.append(
            f'<mxCell id="e{cell_id}" value={quoteattr(nl(label + cap))} style="{style}" '
            f'edge="1" parent="1" source="{mx_id.get(e["from"])}" target="{mx_id.get(e["to"])}">'
            f'<mxGeometry relative="1" as="geometry"/></mxCell>'
        )
        cell_id += 1
    return out


def turn_page(manifest: dict) -> str:
    turn = manifest["graphs"][0]
    off_turn = manifest["graphs"][1:]

    # Position the turn flow first; then off-turn graphs in a band BELOW it,
    # stacked horizontally, with attach edges up to their anchors. The band is
    # below (not beside) because the turn flow now runs left->right: a right-hand
    # band would sit past the terminal node and widen the page for no gain.
    cells = cells_for_graph(turn, kind_of_node=lambda n: (n.get("runs") or {}).get("kind") or n["kind"])
    pos = layered_positions(turn["nodes"], turn["edges"])

    # Re-emit with off-turn: easiest is to merge graphs virtually.
    merged_nodes = list(turn["nodes"])
    merged_edges = list(turn["edges"])
    deepest = max((y // (NODE_H + GAP_Y) for _, y in pos.values()), default=0)
    band_y = (deepest + 2) * (NODE_H + GAP_Y)
    for g in off_turn:
        anchor = (g.get("attached_to") or {}).get("node") if isinstance(g.get("attached_to"), dict) else None
        for i, n in enumerate(g["nodes"]):
            copy = dict(n)
            copy["id"] = f"{g['id']}:{n['id']}"
            merged_nodes.append(copy)
        for e in g["edges"]:
            copy = dict(e)
            copy["from"] = f"{g['id']}:{e['from']}"
            copy["to"] = f"{g['id']}:{e['to']}"
            merged_edges.append(copy)
        if anchor:
            entry = g.get("entry")
            if entry:
                merged_edges.append(
                    {"id": f"att-{g['id']}", "kind": "attach", "from": anchor, "to": f"{g['id']}:{entry}", "label": g["id"], "cap": None, "condition": None}
                )

    merged = {"nodes": merged_nodes, "edges": merged_edges}
    # Layout: turn flow layered; off-turn nodes in the right band.
    pos = layered_positions(
        turn["nodes"], [e for e in merged_edges if e["kind"] != "attach"]
    )
    # give off-turn nodes their own mini-layers in the band
    row = 0
    for g in off_turn:
        for i, n in enumerate(g["nodes"]):
            pos[f"{g['id']}:{n['id']}"] = (i * (NODE_W + GAP_X), band_y + row * (NODE_H + GAP_Y))
        row += 2

    out: list[str] = []
    cell_id = 1
    mx_id: dict[str, str] = {}
    for n in merged_nodes:
        mx_id[n["id"]] = f"n{cell_id}"
        cell_id += 1
    for n in merged_nodes:
        x, y = pos[n["id"]]
        kind = (n.get("runs") or {}).get("kind") or n["kind"]
        style = KIND_STYLE.get(kind, KIND_STYLE["fixed"])
        label = node_label(n)
        if ":" in n["id"]:
            label = nl(f"{n['id']}")
            style += "fontSize=10;"
        out.append(
            f'<mxCell id="{mx_id[n["id"]]}" value={quoteattr(label)} style="{style}" vertex="1" parent="1">'
            f'<mxGeometry x="{x}" y="{y}" width="{NODE_W}" height="{NODE_H}" as="geometry"/>'
            f"</mxCell>"
        )
    for e in merged_edges:
        style = "edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;"
        label = e.get("label") or e.get("condition") or ""
        if e["kind"] == "branch":
            color = BRANCH_COLOR.get(label, "#6c8ebf")
            style += f"strokeColor={color};fontColor={color};"
        if e["kind"] == "loop_back":
            style += "dashed=1;strokeColor=#9673a6;"
        if e["kind"] == "attach":
            style += "dashed=1;strokeColor=#999999;fontSize=10;"
        cap = f" (cap {e['cap']})" if e.get("cap") else ""
        out.append(
            f'<mxCell id="e{cell_id}" value={quoteattr(nl(label + cap))} style="{style}" '
            f'edge="1" parent="1" source="{mx_id.get(e["from"])}" target="{mx_id.get(e["to"])}">'
            f'<mxGeometry relative="1" as="geometry"/></mxCell>'
        )
        cell_id += 1
    # legend
    out.append(
        f'<mxCell id="legend" value={quoteattr(nl("blue = model pass · yellow = gate · grey = fixed/engine · green = embedder/terminal · orange rhombus = branch\ndashed purple = regenerate loop-back · dashed grey = off-turn attach"))} '
        f'style="text;html=1;align=left;verticalAlign=top;fontSize=11;" vertex="1" parent="1">'
        f'<mxGeometry x="0" y="-60" width="620" height="50" as="geometry"/></mxCell>'
    )
    return "".join(out)


def composition_page(manifest: dict) -> str:
    turn = manifest["graphs"][0]
    comp = next(n["composition"] for n in turn["nodes"] if n.get("composition"))
    out: list[str] = []
    cell_id = 1
    mx_id: dict[str, str] = {}
    for n in comp["nodes"]:
        mx_id[n["id"]] = f"c{cell_id}"
        cell_id += 1

    # Region containers as vertical bands; modules inside their region band.
    # Membership rides on the NODE (meta.description = "Region: <id>").
    region_of = {}
    for n in comp["nodes"]:
        desc = str((n.get("meta") or {}).get("description") or "")
        if desc.lower().startswith("region:"):
            region_of[n["id"]] = desc.split(":", 1)[1].strip()
    region_order = {r["id"]: i for i, r in enumerate(comp.get("regions", []))}

    REGION_STYLE = {
        "stable_prefix": "swimlane;horizontal=0;rounded=1;fillColor=#dae8fc;strokeColor=#6c8ebf;opacity=40;startSize=24;",
        "neutral": "swimlane;horizontal=0;rounded=1;fillColor=#f5f5f5;strokeColor=#999999;opacity=40;startSize=24;",
        "dynamic_tail": "swimlane;horizontal=0;rounded=1;fillColor=#d5e8d4;strokeColor=#82b366;opacity=40;startSize=24;",
    }

    # place modules: x by region band, y by order within band
    bands: dict[str, list[str]] = {}
    for n in comp["nodes"]:
        rid = region_of.get(n["id"], "neutral")
        bands.setdefault(rid, []).append(n["id"])

    pos: dict[str, tuple[int, int]] = {}
    band_x = 40
    sink_x = 40 + max(len(b) for b in bands.values()) * 0  # computed below
    for rid in sorted(bands, key=lambda r: region_order.get(r, 99)):
        members = bands[rid]
        for i, mid in enumerate(members):
            pos[mid] = (band_x, 60 + i * (NODE_H + GAP_Y))
        out.append(
            f'<mxCell id="region-{rid}" value={quoteattr(nl(rid))} style="{REGION_STYLE.get(rid, REGION_STYLE["neutral"])}" vertex="1" parent="1">'
            f'<mxGeometry x="{band_x - 20}" y="30" width="{NODE_W + 40}" height="{60 + len(members) * (NODE_H + GAP_Y)}" as="geometry"/></mxCell>'
        )
        band_x += NODE_W + 140
    sink_id = comp.get("sink")
    if sink_id:
        max_y = max(y for _, y in pos.values()) if pos else 60
        pos[sink_id] = (band_x + 60, max_y // 2)

    for n in comp["nodes"]:
        x, y = pos[n["id"]]
        kind = (n.get("runs") or {}).get("kind") or n.get("kind", "trigger")
        style = KIND_STYLE.get(kind, KIND_STYLE["trigger"])
        if n["id"] == sink_id:
            style = KIND_STYLE["terminal"]
        out.append(
            f'<mxCell id="{mx_id[n["id"]]}" value={quoteattr(nl(n["id"]))} style="{style}" vertex="1" parent="1">'
            f'<mxGeometry x="{x}" y="{y}" width="{NODE_W}" height="{NODE_H}" as="geometry"/>'
            f"</mxCell>"
        )
    for e in comp["edges"]:
        style = "edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;"
        out.append(
            f'<mxCell id="x{cell_id}" value="" style="{style}" edge="1" parent="1" '
            f'source="{mx_id.get(e["from"])}" target="{mx_id.get(e["to"])}">'
            f'<mxGeometry relative="1" as="geometry"/></mxCell>'
        )
        cell_id += 1
    return "".join(out)


def proposal_page() -> str:
    """W9.9 PROPOSAL: the narrator agent loop, granular (R15/R16 + open O1).

    Hand-laid on purpose — this is a mock-up for the owner to mark up, not a
    layout derived from data. Deterministic-first (R16a): the check is CODE;
    the corrections agent is AI invoked only on failure.
    """
    out: list[str] = []
    cid = 1

    def cell(value: str, x: int, y: int, w: int, h: int, style: str) -> str:
        nonlocal cid
        s = (
            f'<mxCell id="p{cid}" value={quoteattr(nl(value))} style="{style}" vertex="1" parent="1">'
            f'<mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry"/></mxCell>'
        )
        cid += 1
        return s

    def edge(src: str, dst: str, label: str, style: str) -> str:
        nonlocal cid
        s = (
            f'<mxCell id="p{cid}" value={quoteattr(nl(label))} style="{style}" edge="1" parent="1" source="{src}" target="{dst}">'
            f'<mxGeometry relative="1" as="geometry"/></mxCell>'
        )
        cid += 1
        return s

    ids: dict[str, str] = {}

    def node(key: str, value: str, x: int, y: int, w=170, h=54, style=None) -> str:
        s = cell(value, x, y, w, h, style or KIND_STYLE["fixed"])
        i = s.split('"')[1]
        ids[key] = i
        return s

    # title
    out.append(cell("PROPOSED (W9.9, R15/R16) — the narrator as an agent loop. Mark this page up; nothing here is built.", 40, 0, 900, 30, "text;html=1;fontSize=14;fontStyle=1"))
    out.append(cell("Principle (R16): deterministic CODE first, AI second. The check is code; the corrections agent is invoked only on failure. Premise: a stronger result, not 100% avoidance.", 40, 34, 900, 30, "text;html=1;fontSize=11;fontColor=#666666"))

    # inputs (context UNCHANGED — R16d) + the new instructional layer
    out.append(node("prefix", "CONTEXT (unchanged)\nstable prefix: system · cast · library", 40, 100, style=KIND_STYLE["fixed"]))
    out.append(node("history", "CONTEXT (unchanged)\nhistory [neutral]", 40, 180, style=KIND_STYLE["fixed"]))
    out.append(node("tail", "CONTEXT (unchanged)\nfacts · recalled [dynamic tail]", 40, 260, style=KIND_STYLE["fixed"]))
    out.append(node("instr", "INSTRUCTIONAL layer (NEW)\nassemble step-by-step, resolve issues,\npresent ONLY the final corrected draft", 40, 360, style=KIND_STYLE["model_pass"]))

    # workspace container + slots
    out.append(cell("WORKSPACE (turn state slots — draft space; nothing here commits to the log)", 300, 90, 240, 330, "swimlane;rounded=1;fillColor=#f5f5f5;strokeColor=#666666;opacity=60;startSize=28;fontSize=11;"))
    out.append(node("draft", "drafts d1..dN\n(revise/append in place)", 320, 150, style=KIND_STYLE["trigger"]))
    out.append(node("traces", "tool_traces\n(what was called, what returned)", 320, 220, style=KIND_STYLE["trigger"]))
    out.append(node("att", "attempts · verification\n(counters + check results)", 320, 290, style=KIND_STYLE["trigger"]))

    # the loop
    out.append(node("compose", "compose / revise draft\n(model · tools: workspace,\ndsl_validate [R17], plan+story ref,\nsubmit)", 620, 110, style=KIND_STYLE["model_pass"]))
    out.append(node("check", "DETERMINISTIC CHECK (code)\nDSL schema + segment gate\n(today's validate_dsl, merged)", 620, 250, style=KIND_STYLE["gate"]))
    out.append(node("correct", "corrections agent\n(AI — only on failure;\nfix THESE errors)", 860, 250, style=KIND_STYLE["model_pass"]))
    out.append(node("submit", "submit final draft", 620, 390, style=KIND_STYLE["terminal"]))
    out.append(node("commit", "commits to the LOG (D9)\nstate = draft, log = truth", 620, 470, style=KIND_STYLE["fixed"]))

    out.append(edge(ids["prefix"], ids["compose"], "", "edgeStyle=orthogonalEdgeStyle;html=1;"))
    out.append(edge(ids["history"], ids["compose"], "", "edgeStyle=orthogonalEdgeStyle;html=1;"))
    out.append(edge(ids["tail"], ids["compose"], "", "edgeStyle=orthogonalEdgeStyle;html=1;"))
    out.append(edge(ids["instr"], ids["compose"], "", "edgeStyle=orthogonalEdgeStyle;html=1;strokeColor=#6c8ebf;fontColor=#6c8ebf;"))
    out.append(edge(ids["draft"], ids["compose"], "reads/writes", "edgeStyle=orthogonalEdgeStyle;html=1;dashed=1;strokeColor=#999999;fontSize=10;"))
    out.append(edge(ids["compose"], ids["check"], "draft", "edgeStyle=orthogonalEdgeStyle;html=1;"))
    out.append(edge(ids["check"], ids["submit"], "pass", "edgeStyle=orthogonalEdgeStyle;html=1;strokeColor=#82b366;fontColor=#82b366;"))
    out.append(edge(ids["check"], ids["correct"], "fail (attempts < cap)", "edgeStyle=orthogonalEdgeStyle;html=1;strokeColor=#d08770;fontColor=#d08770;"))
    out.append(edge(ids["correct"], ids["compose"], "revised instructions", "edgeStyle=orthogonalEdgeStyle;html=1;dashed=1;strokeColor=#9673a6;"))
    out.append(edge(ids["check"], ids["submit"], "cap reached → best effort + validator notes (D64 keep-and-tag)", "edgeStyle=orthogonalEdgeStyle;html=1;strokeColor=#d6b656;fontColor=#d6b656;dashed=1;"))
    out.append(edge(ids["submit"], ids["commit"], "final only", "edgeStyle=orthogonalEdgeStyle;html=1;strokeColor=#82b366;"))

    # R18: the critic follows on (external) — fed by the d1-3b decision call
    out.append(node("critic", "CRITIC (R18, external)\nfull model — catches stalling,\nmovement, story-level defects", 860, 390, style=KIND_STYLE["model_pass"]))
    out.append(node("decision", "DECISION (d1-3b)\ncall class: decision · no-token\nslop map + movement flags", 860, 290, style="rounded=1;whiteSpace=wrap;html=1;fillColor=#ffe6cc;strokeColor=#d79b00;dashed=1;"))
    out.append(edge(ids["submit"], ids["critic"], "submitted draft", "edgeStyle=orthogonalEdgeStyle;html=1;strokeColor=#6c8ebf;"))
    out.append(edge(ids["decision"], ids["critic"], "advisory flags", "edgeStyle=orthogonalEdgeStyle;html=1;dashed=1;strokeColor=#d79b00;fontColor=#d79b00;"))
    out.append(edge(ids["critic"], ids["commit"], "ok / diffs", "edgeStyle=orthogonalEdgeStyle;html=1;strokeColor=#82b366;fontColor=#82b366;"))
    out.append(edge(ids["critic"], ids["correct"], "fail → regenerate (cap 2, D49)", "edgeStyle=orthogonalEdgeStyle;html=1;strokeColor=#d08770;fontColor=#d08770;dashed=1;"))
    out.append(cell("R19: new nodes definable FROM the graph view (W9.5 scope) — a created node is a configured run + prompt surface + edges, executed by the graph processor.", 40, 560, 900, 24, "text;html=1;fontSize=11;fontColor=#666666;"))
    return "".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    manifest = json.loads(MANIFEST.read_text())

    pages = [
        ("Turn flow", turn_page(manifest)),
        ("Narrate prompt assembly", composition_page(manifest)),
        ("PROPOSED narrator agent loop", proposal_page()),
    ]
    diagrams = "".join(
        f'<diagram name={quoteattr(name)} id="page{i}">'
        f'<mxGraphModel dx="800" dy="600" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="1600" pageHeight="1200" math="0" shadow="0">'
        f'<root><mxCell id="0"/><mxCell id="1" parent="0"/>{cells}</root></mxGraphModel></diagram>'
        for i, (name, cells) in enumerate(pages)
    )
    xml = (
        '<mxfile host="app.diagrams.net" modified="2026-10-16T00:00:00.000Z" agent="fabulist flow manifest generator" version="24.7.5">'
        + diagrams
        + "</mxfile>"
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(xml)
    print(f"wrote {out} ({len(xml)} bytes, {len(pages)} pages)")


if __name__ == "__main__":
    main()
