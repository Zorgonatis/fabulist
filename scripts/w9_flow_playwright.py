#!/usr/bin/env python3
"""W9 flow-visualizer acceptance gate (wave 1, lane C) + the G8 deep-link pass
(W9.24 W6) — real browser, real model.

Drives the LIVE chat page (``:5173``, vite dev over the working tree) against the
LIVE engine (``:2727``) and asserts the in-chat flow surface end to end:

  1. the flow surface is reachable: the header strip carrying the Flow control
     appears with a live turn and the drop-down opens (a fresh chat has no door
     to the panel before its first turn — the strip only renders with something
     to show);
  2. a REAL turn lights the activation trail (the running node is on screen,
     then the trail reads done);
  3. the turn graph renders every manifest node and every labeled edge, and the
     off-turn graphs render as their own boards;
  4. selecting the narrator node shows its captured inputs/outputs;
  5. G8 (W9.24 §3.6) — the deep-link: a node's ``edit step`` chip opens the
     PromptDrawer ON THAT NODE'S step for every model-pass node the manifest
     links (``narrate``, ``critic``, ``npc``, ``planner``) and for an OFF-TURN
     node (the historian's ``proposal_lanes`` -> the ``proposals`` step); a
     deterministic node (``validate``, door ``fixed``) shows the engine's reason
     and NO chip; the drawer's step list carries every catalog step, and every
     step door in the manifest names a step that list offers;
  6. zero page errors across the whole run.

Everything the gate expects is read from the live engine (manifest node set,
edge labels, fixed-door reasons, the step catalog), so a manifest change cannot
leave a stale expectation behind here — the old hardcoded node count and branch
labels were exactly that and are gone (W9.24 W6).

A full turn is ~80-130 s, so the turn budget defaults to 300 s. Playwright only
(real trusted input, actionability waits) — never synthetic ``.click()`` over
raw CDP (AGENTS.md).

Run:
  uv run --with playwright python scripts/w9_flow_playwright.py
  uv run --with playwright python scripts/w9_flow_playwright.py --session <id>
  # re-run only the G8 deep-link pass against a session that already has a turn:
  uv run --with playwright python scripts/w9_flow_playwright.py --session <id> --skip-turn
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "http://localhost:5173"
DEFAULT_API_URL = "http://localhost:2727"
DEFAULT_SESSION = "94001b5ec92b4d06bc432fee32c499ad"
DEFAULT_MESSAGE = "I look up at the sound of the door and stay very still."
#: G8 deep-link cases (W9.24 §3.6): ``(node id, graph id or None for the turn
#: graph, expected step id or None for a ``fixed`` door)``. The model-pass pair
#: (``narrate`` and ``critic`` — the second also pins a RE-OPEN after a close),
#: a deterministic gate (``validate``), the two doors this wave revived
#: (``npc``/``planner`` — they were missing from the drawer's flow list), and an
#: OFF-TURN flow's model-pass node — the last one proves the manifest's off-turn
#: rows deep-link as well as the turn's.
DEEP_LINK_CASES: tuple[tuple[str, str | None, str | None], ...] = (
    ("narrate", None, "narrate"),
    ("critic", None, "critic"),
    ("npc_lanes", None, "npc"),
    ("planner", None, "planner"),
    ("validate", None, None),
    ("proposal_lanes", "historian", "proposals"),
)
CHROME = os.environ.get(
    "FABULIST_CHROME",
    str(Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"),
)

#: Console noise that is NOT a page error: the browser's own favicon probe and
#: the dev server's HMR websocket chatter. Anything else fails the pass.
BENIGN_CONSOLE: tuple[str, ...] = ("favicon", "[vite]")

CONSOLE_ERRORS: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)


def ok(msg: str) -> None:
    print(f"  PASS  {msg}", flush=True)


class GateError(AssertionError):
    pass


def check(condition: bool, msg: str) -> None:
    if not condition:
        raise GateError(msg)
    ok(msg)


def http_json(url: str, method: str = "GET", payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as res:  # noqa: S310 - local
        return json.loads(res.read().decode() or "{}")


def poll(fn, timeout: float, interval: float = 0.25, what: str = "condition"):
    """Call ``fn`` until it returns a truthy value; raise on timeout."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise GateError(f"timed out after {timeout:.0f}s waiting for {what}")


def _flow_expectations(api_url: str) -> tuple[dict, dict[str, dict], dict[str, str]]:
    """Read everything the gate asserts ABOUT the graph from the live engine.

    Returns ``(manifest, nodes_by_id, catalog_titles)``. Deriving the node set,
    the edge labels and the fixed-door reasons from the manifest (instead of
    hardcoding them) is what keeps this gate honest across manifest waves: the
    gate proves the BROWSER draws what the manifest says, and the manifest's own
    build-time validation proves the manifest is well formed.
    """
    manifest = http_json(f"{api_url}/api/flow/manifest")
    nodes = {node["id"]: node for graph in manifest["graphs"] for node in graph["nodes"]}
    catalog = http_json(f"{api_url}/api/prompts/steps").get("steps", [])
    titles = {str(step["id"]): str(step["title"]) for step in catalog}
    return manifest, nodes, titles


def _check_step_doors(nodes: dict[str, dict], catalog_titles: dict[str, str]) -> None:
    """Every manifest step door must name a REAL catalog step (G8 drift check).

    The manifest builder validates this at build time; this reads the live API
    pair (manifest + ``/api/prompts/steps``) because a drifted row is exactly
    the failure the deep-link pass exists to catch — a manifest listing a step
    the drawer cannot open would render a chip that goes nowhere.
    """
    dangling = [
        f"{node_id} -> {node['prompt_surface']['step_id']!r}"
        for node_id, node in nodes.items()
        if node["prompt_surface"]["door"] == "step_editor"
        and node["prompt_surface"]["step_id"] not in catalog_titles
    ]
    check(not dangling, f"every manifest step door names a live catalog step ({len(nodes)} nodes)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="frontend origin")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="engine origin")
    parser.add_argument("--session", default=DEFAULT_SESSION, help="session id to drive")
    parser.add_argument("--message", default=DEFAULT_MESSAGE, help="user turn to send")
    parser.add_argument("--turn-timeout", type=float, default=300.0, help="turn budget (s)")
    parser.add_argument(
        "--skip-turn",
        action="store_true",
        help="skip the live turn (the trail + the capture inspector) and run only "
        "the graph + G8 deep-link pass — for a session that already carries a turn",
    )
    parser.add_argument("--shots-dir", default="/tmp", help="where to write screenshots")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    args = parser.parse_args()

    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    if not Path(CHROME).exists():
        log(f"ERROR: chromium not found at {CHROME} (set FABULIST_CHROME)")
        return 2

    # ── preflight: the engine must serve the manifest, every step door must
    #    name a live catalog step (G8), and the session's debug toggle must be
    #    ON (captures only exist for turns that ran with it on — the inspector
    #    assertion depends on it).
    try:
        manifest, nodes, catalog_titles = _flow_expectations(args.api_url)
        turn_graph = next((g for g in manifest["graphs"] if g["kind"] == "turn"), None)
        if turn_graph is None:
            raise GateError("the manifest carries no turn graph")
        turn_nodes = turn_graph["nodes"]
        graph_titles = {str(graph["id"]): str(graph["title"]) for graph in manifest["graphs"]}
        # W9.17: the fan-out template node is a Send target, never a station —
        # the read view strips it (FlowGraph.withLaneArms), so it must NOT
        # render. Every other turn node must render exactly once.
        stripped = {
            str(node_id)
            for node in turn_nodes
            if node.get("fanout")
            for node_id in (node["fanout"].get("nodes") or [])
        }
        edge_labels = [
            str(edge["label"])
            for edge in turn_graph["edges"]
            # `fanout` edges are redrawn as one ARM/edge per live lane member, so
            # their generic label never appears on the board (FlowGraph.withLaneArms).
            if edge.get("label") and str(edge.get("kind")) not in ("fanout", "attach")
        ]
        log(
            f"engine manifest ok: {len(turn_nodes)} turn nodes "
            f"({len(stripped)} fan-out template(s) stripped), "
            f"{len(manifest['graphs'])} graphs, {len(edge_labels)} labeled edges"
        )
        _check_step_doors(nodes, catalog_titles)
    except (urllib.error.URLError, KeyError, GateError) as err:
        log(f"ERROR: engine preflight failed: {err}")
        return 2

    url = f"{args.api_url}/api/sessions/{args.session}/agent-debug"
    try:
        state = http_json(url)
        if state.get("enabled") is not True:
            state = http_json(url, "PATCH", {"enabled": True})
        check(state.get("enabled") is True, "session agent-debug is on (captures will exist)")
    except (urllib.error.URLError, GateError) as err:
        log(f"ERROR: could not enable session debug: {err}")
        return 2

    failures: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME, args=["--no-sandbox"], headless=not args.headed
        )
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 950})
            page.set_default_timeout(30_000)
            page.on("console", lambda msg: CONSOLE_ERRORS.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda err: CONSOLE_ERRORS.append(str(err)))

            page.goto(f"{args.base_url}/chat?s={args.session}", wait_until="domcontentloaded")
            composer = page.locator("form.composer textarea")
            composer.wait_for(state="visible", timeout=30_000)
            log(f"opened {args.base_url}/chat?s={args.session}")

            # ── 1. reach the flow surface --------------------------------------
            #
            # The strip that hosts the Flow control only renders when it has
            # something to say (a scene title, a step trace, or a turn in
            # flight — `DslHeader.visible`). A brand-new chat therefore has NO
            # door to the flow panel until its first turn; the pass starts that
            # turn first and opens the panel as soon as the strip appears. With
            # `--skip-turn` the session must already carry a turn.
            flow_btn = page.locator('button[aria-label^="Flow"]').first
            turn_board = page.locator(f'section[aria-label="{turn_graph["title"]}"]').first

            def submit_turn() -> None:
                """Fill the box and press Enter; re-submit while the turn lock holds."""
                composer.fill(args.message)
                last_error = "no attempt"
                for attempt in range(4):
                    composer.press("Enter")
                    try:
                        page.wait_for_selector("form.composer button.stop", timeout=6_000)
                        log(f"turn started (attempt {attempt + 1})")
                        return
                    except PWTimeout:
                        last_error = f"attempt {attempt + 1}: composer never entered streaming"
                raise GateError(f"could not start a turn — {last_error}")

            if args.skip_turn:
                log("  SKIP  the live turn (--skip-turn): driving the graph + G8 pass only")
            else:
                submit_turn()

            try:
                flow_btn.wait_for(state="visible", timeout=30_000)
            except PWTimeout:
                raise GateError(
                    "no Flow control in the header — the chat has no turn trace, so the "
                    "flow panel is unreachable from this session"
                ) from None
            check(
                (flow_btn.get_attribute("aria-label") or "").startswith("Flow"),
                "header exposes a Flow control (aria-label starts with 'Flow')",
            )
            flow_btn.click()
            panel = page.locator("[data-flow-panel]")
            panel.wait_for(state="visible", timeout=20_000)
            check(panel.is_visible(), "flow panel opens in the drop-down")
            turn_board.wait_for(state="visible", timeout=20_000)

            # Node lookup helpers (used by the trail, the shape checks and the
            # deep-link pass alike): a node is located by its VISIBLE title, not
            # by id — the read view carries no node id in the DOM (only the
            # Studio's edit board labels cells with ids), and the manifest's
            # titles are unique within a graph, which the shape check proves.
            def section_for(graph_title: str):
                section = page.locator(f'section[aria-label="{graph_title}"]').first
                section.wait_for(state="visible", timeout=20_000)
                return section

            def node_label_count(graph_title: str, node_id: str) -> int:
                """How many node labels the board draws for ``node_id`` (want 1/0)."""
                return section_for(graph_title).get_by_text(
                    str(nodes[node_id]["title"]), exact=True
                ).count()

            def node_cell(graph_title: str, node_id: str):
                """The `.cell` around one node — its chip is a sibling of its main button.

                Resolved from the node's own label upward: Playwright's
                ``filter(has=...)`` anchors the inner locator to the page, not to
                each candidate, so it cannot express this step.
                """
                label = section_for(graph_title).get_by_text(
                    str(nodes[node_id]["title"]), exact=True
                )
                return label.locator("xpath=ancestor::div[contains(@class,'cell')][1]")

            # ── 2. the live turn lights the trail -------------------------------
            if args.skip_turn:
                check(
                    turn_board.locator(".node.running").count() == 0,
                    "no node is running (the session's last turn is finished)",
                )
            else:
                # The running node must be on screen inside the drop-down viewport
                # (the board is ~1392 px tall and the drop-down is capped).
                def running_visible() -> bool:
                    node = turn_board.locator(".node.running").first
                    if node.count() == 0:
                        return False
                    dropdown = page.locator("[data-flow-dropdown]").first
                    node_box = node.bounding_box()
                    drop_box = dropdown.bounding_box()
                    if not node_box or not drop_box:
                        return False
                    return (
                        node_box["y"] >= drop_box["y"] - 1
                        and node_box["y"] + node_box["height"] <= drop_box["y"] + drop_box["height"] + 1
                    )

                try:
                    poll(running_visible, timeout=90, what="the running node inside the panel viewport")
                    ok("the running node lights and stays in the visible panel area")
                except GateError as err:
                    # The trail itself is the requirement; auto-scroll is a plus.
                    running = turn_board.locator(".node.running").count()
                    check(running > 0, "the running node lights during the turn")
                    log(f"  WARN  auto-scroll did not land the running node in view: {err}")

                # ── 3. the turn completes; the trail reads done ----------------
                page.wait_for_selector("form.composer button.send", timeout=args.turn_timeout * 1000)
                page.wait_for_selector("form.composer button.stop", state="detached", timeout=15_000)
                check(
                    turn_board.locator(".node.running").count() == 0,
                    "no node is left running after the turn",
                )
                # Which stages a turn emits is the turn's business (the planner is
                # cadence-gated, the critic conditional) — a fixed count would be
                # a stale expectation. What is NOT negotiable: the narrate node is
                # lit, and the trail lights more than just it.
                lit = poll(
                    lambda: turn_board.locator(".node.done").count(),
                    timeout=20,
                    what="the completed activation trail",
                )
                check(lit >= 2, f"the activation trail is done ({lit} nodes lit)")
                narrate_classes = (
                    node_cell(turn_graph["title"], "narrate").locator(".node").get_attribute("class")
                    or ""
                )
                check(
                    "done" in narrate_classes.split(),
                    "the narrate node is lit done by the turn's own stage frame",
                )

            # ── 4. graph shape --------------------------------------------------
            missing_nodes = [
                node["id"]
                for node in turn_nodes
                if node["id"] not in stripped
                and node_label_count(turn_graph["title"], node["id"]) != 1
            ]
            check(
                not missing_nodes,
                f"the turn graph renders all {len(turn_nodes) - len(stripped)} nodes; "
                f"missing={missing_nodes}",
            )
            # The fan-out TEMPLATE node is a Send target, never a station: the read
            # view strips it and draws one arm per live lane member instead.
            leaked = [
                node_id
                for node_id in stripped
                if node_label_count(turn_graph["title"], node_id)
            ]
            check(not leaked, f"the stripped fan-out template renders no stub node (leaked={leaked})")
            labels = turn_board.locator(".edge-label").all_inner_texts()
            missing_labels = [label for label in edge_labels if not any(label in t for t in labels)]
            check(
                not missing_labels,
                f"every labeled manifest edge renders its label ({len(edge_labels)} of them); "
                f"missing={missing_labels}",
            )
            off_turn_graphs = [g for g in manifest["graphs"] if g["kind"] != "turn"]
            missing_graphs = [
                g["title"]
                for g in off_turn_graphs
                if page.locator(f'section[aria-label="{g["title"]}"]').count() == 0
            ]
            check(
                not missing_graphs,
                f"all {len(off_turn_graphs)} off-turn graphs render as their own boards; "
                f"missing={missing_graphs}",
            )

            # ── 5. inspector: the narrator node's captured I/O -------------------
            if not args.skip_turn:
                node_cell(turn_graph["title"], "narrate").locator("button.node-main").click()
                inspector = page.locator("[data-flow-panel] .inspector")
                inspector.wait_for(state="visible", timeout=10_000)

                def captures_rendered() -> bool:
                    text = inspector.inner_text()
                    return "messages (" in text and "response" in text

                poll(captures_rendered, timeout=30, what="narrate captures in the inspector")
                check(True, "the narrate node's captured inputs/outputs render in the inspector")

            # ── 6. G8 (W9.24 §3.6): the graph-view deep-link -------------------
            #
            # A node's "edit step" chip carries the manifest's step id up the
            # EXISTING chain (FlowNode.onopenstep -> ChatFlowPanel -> the page's
            # openPromptStep -> PromptDrawer.initialStepId) and the drawer must
            # open ON THAT STEP. Proven here for a model-pass node (twice, so a
            # re-open after a close is covered too), for the deterministic gate
            # (door `fixed`: the engine's reason, never a chip) and for an
            # OFF-TURN flow's node (the historian) — the off-turn row deep-links
            # exactly like the turn's.
            shots = Path(args.shots_dir)
            shots.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(shots / "w9_g8_flow_panel.png"))

            drawer = page.locator('div.prompt-drawer[role="dialog"]')
            step_head_titles = drawer.locator("header.step-head h3")

            def close_drawer() -> None:
                drawer.locator('button[aria-label="Close prompt manager"]').click()
                drawer.wait_for(state="detached", timeout=15_000)

            for node_id, graph_id, expected_step in DEEP_LINK_CASES:
                graph_title = turn_graph["title"] if graph_id is None else graph_titles[graph_id]
                check(
                    node_label_count(graph_title, node_id) == 1,
                    f"{node_id}: renders exactly one node label in the {graph_title!r} board",
                )
                cell = node_cell(graph_title, node_id)
                cell.wait_for(state="visible", timeout=10_000)
                surface = nodes[node_id]["prompt_surface"]
                if expected_step is None:
                    # The deterministic gate: door `fixed` — a reason, never a chip.
                    check(
                        surface["door"] == "fixed",
                        f"{node_id}: the manifest gives it a non-editable door ({surface['door']})",
                    )
                    check(
                        cell.locator("button.door-open").count() == 0,
                        f"{node_id}: renders NO edit-step chip (the door is fixed)",
                    )
                    fixed_text = cell.locator(".door-fixed").inner_text()
                    check(
                        str(surface["reason"]) in fixed_text,
                        f"{node_id}: shows the engine's fixed reason ({fixed_text.strip()!r})",
                    )
                    continue
                check(
                    surface["step_id"] == expected_step,
                    f"{node_id}: the manifest's step door names {expected_step!r}",
                )
                chip = cell.locator("button.door-open")
                check(
                    chip.inner_text().strip() == "edit step",
                    f"{node_id}: renders the 'edit step' chip",
                )
                title = catalog_titles[expected_step]
                chip.click()  # real trusted input — never a synthetic .click()
                drawer.wait_for(state="visible", timeout=20_000)
                poll(
                    lambda want=title: step_head_titles.all_inner_texts() == [want],
                    timeout=25,
                    what=f"the drawer to open the {expected_step!r} step editor",
                )
                check(
                    True,
                    f"{node_id} ({graph_title}) chip opens the drawer ON step "
                    f"{expected_step!r} — step editor head reads {title!r}",
                )
                page.screenshot(path=str(shots / f"w9_g8_{expected_step}_drawer.png"))
                close_drawer()

            check(drawer.count() == 0, "the drawer closes cleanly after every deep-link")

            # ── 7. the drawer's step list carries EVERY catalog step -------------
            # A door is only real if the editor can open what it names: the three
            # flows this wave revived (npc / planner / proposals) were silently
            # absent from the drawer's list while the engine catalog and the flow
            # manifest both carried them.
            flow_btn.click()  # close the drop-down so the header is clickable
            panel.wait_for(state="detached", timeout=15_000)
            page.locator('button[aria-label^="Prompts"]').first.click()
            drawer.wait_for(state="visible", timeout=20_000)
            # The drawer re-opens on the last deep-linked step, and only after its
            # preset load lands: wait for either view, then go back to the list.
            poll(
                lambda: drawer.locator("li.step").count()
                or drawer.locator("header.step-head h3").count(),
                timeout=25,
                what="the drawer to finish its preset load (list or step editor)",
            )
            back = drawer.locator("button.back")
            if back.count():
                back.first.click()  # the drawer re-opens on the last deep-linked step
            rows = drawer.locator("li.step .title")
            poll(
                lambda: rows.count() == len(catalog_titles),
                timeout=25,
                what=f"the drawer's step list to render all {len(catalog_titles)} steps",
            )
            row_titles = rows.all_inner_texts()
            check(
                True,
                f"the drawer's step list renders all {len(catalog_titles)} catalog steps "
                f"({', '.join(row_titles[:3])}, …)",
            )
            unreachable = [
                f"{node_id} -> {surface['step_id']!r}"
                for node_id, node in nodes.items()
                if (surface := node["prompt_surface"])["door"] == "step_editor"
                and catalog_titles[str(surface["step_id"])] not in row_titles
            ]
            check(
                not unreachable,
                f"every manifest step door names a step the drawer LISTS; unreachable={unreachable}",
            )
            page.screenshot(path=str(shots / "w9_g8_step_list.png"))
            close_drawer()

            # ── 8. zero page errors ---------------------------------------------
            fatal = [e for e in CONSOLE_ERRORS if "each_key_duplicate" in e or "Internal Error" in e]
            check(not fatal, f"no render-boundary errors in the console (fatal={fatal})")
            noisy = [e for e in CONSOLE_ERRORS if not any(b in e for b in BENIGN_CONSOLE)]
            check(not noisy, f"zero page/console errors across the whole pass (got {noisy})")
        except (GateError, PWTimeout) as err:
            failures.append(str(err))
            log(f"  FAIL  {err}")
            try:
                shot = Path(args.shots_dir) / "w9_flow_failure.png"
                shot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shot), full_page=False)
                log(f"failure screenshot: {shot}")
            except Exception:  # noqa: BLE001 - diagnostics only
                pass
        finally:
            browser.close()

    if failures:
        log(f"GATE FAILED: {len(failures)} assertion(s)")
        return 1
    log("GATE PASSED: all W9 flow assertions hold against the live tree")
    return 0


if __name__ == "__main__":
    sys.exit(main())
