#!/usr/bin/env python3
"""W9.25 W25.4 acceptance gate — the connections drawer (real browser, real engine).

Drives the LIVE connections drawer (``/chat`` -> the Connections surface) against
an ISOLATED engine and asserts the W9.25 surface end to end:

  1. the agents section renders TYPE/TASK groups driven by ``GET
     /api/connections``' ``slots`` payload — ``Completion`` / ``Embedding`` /
     ``Decision`` headers, the ``Narration`` / ``Extraction`` / ``State`` task
     rows, then the per-item rows — and every row carries a resolution
     provenance line (``set here`` / ``via legacy <key>`` / ``via task <key>`` /
     ``via type default`` / ``unresolved``) that AGREES with the raw ``agents``
     map;
  2. rebinding a task row through the UI writes the CANONICAL key (``GET``
     reflects ``completion.narration``, the legacy ``narrator`` alias never
     becomes a second row) and rebinding the TYPE row makes an unauthored task
     row resolve ``via type default`` — "under the type OR per item", visible;
  3. the widened connection form round-trips through save + reload: ``Context
     Size``, ``Response Length`` (the ``sampling.max_tokens`` relabel), the
     ``Thinking``/``Streaming`` toggles (checked = NO instruction, so a checked
     box writes ``null``, never ``true``), the standard sampler grid and the
     ``Stop Sequences`` lines (``sampling.stop: string[]``). No llama.cpp-specific
     knob appears anywhere in the form;
  4. the ``VITE_MOCK=1`` demo renders the seeded typed slots and the new fields;
  5. zero page errors (uncaught JS) across every page, and no console error
     beyond the deliberate save-ping probe.

Playwright only (real trusted input, actionability waits) — never synthetic
``.click()`` over raw CDP (AGENTS.md).

The pass MUTATES the engine it is pointed at (rebinds + a form save, then
restores the captured values). Point it at a THROWAWAY config copy, never the
live ``configs/connections.json``. Two consequences of the API's shape that the
pass reports honestly: a legacy-twin address normalizes to its canonical key, so
a rebind leaves the canonical key authored (restoring the resolved CONNECTION,
not the file's exact key set — there is no agent-key DELETE); and ``sampling`` is
replaced wholesale, so the form's save writes exactly the standard vocabulary it
edits.

Run (isolated engine on a config COPY + two vite servers over this tree)::

  mkdir -p /tmp/w9_25
  cp configs/connections.json /tmp/w9_25/connections.json
  FABULIST_DEV_ORIGINS=http://127.0.0.1:5198,http://localhost:5198 \\
    uv run python scripts/serve.py --config /tmp/w9_25/connections.json \\
      --log /tmp/w9_25/log.db --checkpoints /tmp/w9_25/ckpt.db --port 8100

  cd frontend
  VITE_API_BASE=http://127.0.0.1:8100 npm run dev -- --port 5198 --host 127.0.0.1 &
  VITE_MOCK=1 npm run dev -- --port 5199 --host 127.0.0.1 &

  cd ..
  uv run --with playwright python scripts/w9_25_drawer_playwright.py

  # --skip-mock  : engine pass only (no second vite server needed)
  # --no-restore : leave the mutations in place for inspection
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

DEFAULT_BASE_URL = "http://127.0.0.1:5198"
DEFAULT_API_URL = "http://127.0.0.1:8100"
DEFAULT_MOCK_URL = "http://127.0.0.1:5199"
CHROME = os.environ.get(
    "FABULIST_CHROME",
    str(Path.home() / ".cache/ms-playwright/chromium-1234/chrome-linux64/chrome"),
)

#: The drawer's trigger on the chat shell.
CONNECTIONS_BUTTON = 'button[aria-label^="Connections"]'

#: The taxonomy's declared order (W9.25 §3.2) — the payload must arrive in it.
TYPE_ORDER = ("completion", "embedding", "decision")
TASK_ORDER = ("narration", "extraction", "state")
ITEM_SLOTS = ("summarizer", "captioner", "imagegen", "embedder")

#: Parameter names that must NOT reach the connection surface (owner steering:
#: generic OpenAI-compatible runtime params only; ``extra_body`` stays the
#: escape hatch for server-specific ones).
FORBIDDEN_FORM_TOKENS = (
    "n_ctx",
    "ctx_size",
    "n_batch",
    "n_gpu_layers",
    "rope",
    "rope_freq",
    "tensor_split",
    "numa",
    "mlock",
    "cache_type",
    "draft_model",
    "n_draft",
    "slot_save_path",
)

#: Console noise that is NOT a page error: the browser's favicon probe and the
#: dev server's HMR chatter.
BENIGN_CONSOLE: tuple[str, ...] = ("favicon", "[vite]")

log_lines: list[str] = []
SHOTS: list[str] = []


def log(msg: str) -> None:
    print(msg, flush=True)
    log_lines.append(msg)


def ok(msg: str) -> None:
    print(f"  PASS  {msg}", flush=True)
    log_lines.append(f"  PASS  {msg}")


class GateError(AssertionError):
    pass


def check(condition: bool, msg: str) -> None:
    if not condition:
        raise GateError(msg)
    ok(msg)


def http_json(url: str, method: str = "GET", payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310 - local
        return json.loads(response.read().decode() or "{}")


# --- preflight: the payload the drawer renders -------------------------------------


def fetch_catalog(api_url: str) -> dict:
    return http_json(f"{api_url}/api/connections")


def check_payload_shape(catalog: dict) -> dict[str, dict]:
    """Structural assertions on ``GET /api/connections`` (before any browser)."""
    connections = catalog.get("connections") or {}
    agents = catalog.get("agents") or {}
    slots = catalog.get("slots")
    check(isinstance(slots, list) and bool(slots), "GET carries a non-empty `slots` payload")
    check(len(connections) >= 2, f"at least two connections to rebind between ({len(connections)})")

    rows = {str(row.get("slot")): row for row in slots}
    check(len(rows) == len(slots), f"every slot row is unique ({len(slots)} rows)")

    # Group order: each type row, then its task rows, then its per-item rows.
    seen_types = [str(row.get("slot")) for row in slots if row.get("type") == row.get("slot")]
    check(
        seen_types == list(TYPE_ORDER),
        f"the payload leads each group with its type row, in declared order {seen_types}",
    )
    for type_name in TYPE_ORDER:
        group = [row for row in slots if row.get("type") == type_name]
        check(bool(group), f"`{type_name}` has at least its type row")
    completion_tasks = [
        str(row.get("slot")) for row in slots if row.get("type") == "completion" and row.get("task")
    ]
    check(
        completion_tasks == [f"completion.{task}" for task in TASK_ORDER],
        f"Completion carries its task rows in order ({completion_tasks})",
    )
    for item in ITEM_SLOTS:
        check(item in rows, f"per-item row `{item}` is present")
    check(rows["completion.narration"]["label"] == "Narration", "task rows carry their label")
    check(rows["decision"]["slot"] == "decision", "the decision row is the item and the type")

    # The legacy keys are ALIASES: never their own row.
    check(
        "narrator" not in rows and "extractor" not in rows,
        "legacy twins never render as their own row",
    )
    for legacy, typed in (("narrator", "completion.narration"), ("extractor", "completion.extraction")):
        check(
            legacy in (rows[typed].get("aliases") or []),
            f"`{legacy}` is listed as an alias of `{typed}`",
        )

    # Provenance agrees with the raw agent map — the payload is not decorative.
    problems: list[str] = []
    for row in slots:
        via = row.get("via")
        slot = str(row.get("slot"))
        connection = row.get("connection")
        if via is None:
            if connection is not None:
                problems.append(f"{slot}: unresolved row carries a connection")
            continue
        if connection not in connections:
            problems.append(f"{slot}: resolved to unknown connection {connection!r}")
        if via == "authored":
            if agents.get(slot) != connection:
                problems.append(f"{slot}: via=authored but agents[{slot}]={agents.get(slot)!r}")
        elif via.startswith("legacy:"):
            key = via.split(":", 1)[1]
            if agents.get(key) != connection:
                problems.append(f"{slot}: via={via} but agents[{key}]={agents.get(key)!r}")
        elif via.startswith("type:"):
            key = via.split(":", 1)[1]
            if agents.get(key) != connection:
                problems.append(f"{slot}: via={via} but agents[{key}]={agents.get(key)!r}")
        else:
            problems.append(f"{slot}: unknown via {via!r}")
    check(not problems, f"every row's provenance matches the raw map ({'; '.join(problems) or 'ok'})")
    return rows


# --- browser helpers ---------------------------------------------------------------


def open_drawer(page, base_url: str, session_id: str | None = None) -> "object":
    """Open ``/chat`` and the Connections drawer; return the drawer locator.

    The header is SSR-rendered and visible BEFORE hydration, so a click that
    lands in that window is swallowed by an un-hydrated button (the classic
    SSR-first trap — it looks exactly like "the drawer is broken"). The click is
    therefore RETRIED until the drawer is actually on screen: setting an
    already-true ``connectionsOpen`` twice is harmless, and no input is faked.
    """
    from playwright.sync_api import TimeoutError as PWTimeout

    suffix = f"?s={session_id}" if session_id else ""
    page.goto(f"{base_url}/chat{suffix}", wait_until="domcontentloaded")
    button = page.locator(CONNECTIONS_BUTTON).first
    button.wait_for(state="visible", timeout=30_000)
    dialog = page.locator('div[role="dialog"][aria-label="Connections"]').first
    for _attempt in range(6):
        button.click()
        try:
            dialog.wait_for(state="visible", timeout=5_000)
            break
        except PWTimeout:
            page.wait_for_timeout(500)
    else:
        raise GateError("the connections drawer never opened (hydration or a render error)")
    # The catalog arrives from the engine after the drawer mounts; every phase
    # below reads the rendered slot rows, so wait for them here once.
    dialog.locator("section.agents").wait_for(state="visible", timeout=15_000)
    dialog.locator("[data-slot]").first.wait_for(state="visible", timeout=20_000)
    return dialog


def connection_card(dialog, name: str):
    """The ``li.connection`` card whose name is exactly ``name``."""
    cards = dialog.locator("li.connection")
    for index in range(cards.count()):
        card = cards.nth(index)
        if card.locator(".conn-name").inner_text().strip() == name:
            return card
    raise GateError(f"no connection card named {name!r} in the drawer")


def slot_select(dialog, slot: str):
    return dialog.locator(f'[data-slot="{slot}"] select')


def slot_via(dialog, slot: str) -> str:
    return dialog.locator(f'[data-slot="{slot}"] .slot-via').inner_text().strip()


def wait_status(dialog, needle: str, timeout: float = 15.0) -> str:
    """Wait for the drawer's status line to mention ``needle``."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        status = dialog.locator("p.status")
        if status.count():
            last = status.first.inner_text().strip()
            if needle in last:
                return last
        time.sleep(0.1)
    raise GateError(f"drawer status never mentioned {needle!r} (last: {last!r})")


def shot(page, shots_dir: Path, name: str) -> None:
    path = shots_dir / name
    page.screenshot(path=str(path))
    SHOTS.append(str(path))
    log(f"  shot  {path}")


# --- phase 1: grouped rows + provenance in the browser -----------------------------


def check_grouped_rows(page, dialog, shots_dir: Path) -> None:
    headers = [text.strip() for text in dialog.locator("h4.slot-type").all_inner_texts()]
    check(
        headers == ["Completion", "Embedding", "Decision"],
        f"the agents section renders type headers {headers}",
    )
    for slot in (
        "completion",
        "completion.narration",
        "completion.extraction",
        "completion.state",
        "summarizer",
        "captioner",
        "imagegen",
        "embedding",
        "embedder",
        "decision",
    ):
        check(
            dialog.locator(f'[data-slot="{slot}"]').count() == 1,
            f"exactly one row for `{slot}`",
        )
    dialog.locator("section.agents").scroll_into_view_if_needed()
    check(True, "every declared slot renders once (no legacy duplicate row)")
    shot(page, shots_dir, "01-agents-grouped.png")


def check_provenance(dialog, rows: dict[str, dict], catalog: dict) -> None:
    """The DOM's via line says what the payload's provenance says."""
    expected = {
        "authored": "set here",
        "legacy": "via legacy ",
        "type-task": "via task ",
        "type-default": "via type default",
        "unresolved": "unresolved",
    }
    for slot, row in rows.items():
        text = slot_via(dialog, slot)
        via = row.get("via")
        if via is None:
            want = expected["unresolved"]
            check(text == want, f"`{slot}` reads {text!r} (unresolved)")
        elif via == "authored":
            check(text == expected["authored"], f"`{slot}` reads {text!r} (its own key)")
        elif via.startswith("legacy:"):
            want = expected["legacy"] + via.split(":", 1)[1]
            check(text == want, f"`{slot}` reads {text!r} (legacy alias)")
        else:
            key = via.split(":", 1)[1]
            want = expected["type-task"] + key if "." in key else expected["type-default"]
            check(text == want, f"`{slot}` reads {text!r} (ancestor key)")


# --- phase 2: rebinds --------------------------------------------------------------


def rebind_task_row(page, dialog, api_url: str, rows: dict[str, dict], catalog: dict, shots_dir: Path):
    """Rebind ``Narration`` through the UI; the write must land on the canonical key."""
    baseline = dict(catalog["agents"])
    current = rows["completion.narration"]["connection"]
    names = sorted(catalog["connections"])
    target = next((name for name in names if name != current), None)
    if target is None:  # pragma: no cover - two connections are asserted in preflight
        raise GateError("no alternative connection to rebind to")
    log(f"rebinding completion.narration: {current!r} -> {target!r}")

    slot_select(dialog, "completion.narration").select_option(target)
    status = wait_status(dialog, "Rebound")
    ok(f"the drawer reports the rebind ({status!r})")
    shot(page, shots_dir, "02-rebind-status.png")

    after = fetch_catalog(api_url)
    check(
        after["agents"].get("completion.narration") == target,
        "GET reflects the CANONICAL key `completion.narration`",
    )
    slots = {str(row["slot"]): row for row in after["slots"]}
    check("narrator" not in slots, "the legacy `narrator` alias still has no row of its own")
    check(
        slots["completion.narration"]["connection"] == target
        and slots["completion.narration"]["via"] == "authored",
        "the typed row resolves to the new connection, authored",
    )
    check(slot_via(dialog, "completion.narration") == "set here", "the row now reads `set here`")
    if "narrator" in after["agents"]:
        inert = dialog.locator('[data-slot="completion.narration"] .slot-inert')
        check(
            inert.count() == 1 and "narrator" in inert.first.inner_text(),
            f"the inert legacy alias is called out in the row ({inert.first.inner_text()!r})",
        )
    return baseline


def rebind_type_row(page, dialog, api_url: str, rows: dict[str, dict], catalog: dict):
    """Rebind the TYPE row; an unauthored task row must then resolve `via type default`."""
    target = sorted(catalog["connections"])[0]
    log(f"rebinding the Completion type row -> {target!r}")
    slot_select(dialog, "completion").select_option(target)
    wait_status(dialog, "Rebound")

    after = fetch_catalog(api_url)
    slots = {str(row["slot"]): row for row in after["slots"]}
    state_row = slots["completion.state"]
    if state_row["via"] == "authored":
        log("  note  `completion.state` is authored here — the type fallback cannot show")
        return
    check(
        after["agents"].get("completion") == target,
        "GET reflects the type key `completion`",
    )
    check(
        state_row["via"] == "type:completion" and state_row["connection"] == target,
        f"`completion.state` now resolves through the type key ({state_row['via']})",
    )
    check(
        slot_via(dialog, "completion.state") == "via type default",
        f"the State row reads {slot_via(dialog, 'completion.state')!r}",
    )


# --- phase 3: the widened connection form ------------------------------------------


def edit_connection_fields(page, dialog, api_url: str, catalog: dict, shots_dir: Path) -> None:
    """Round-trip Context Size / Response Length / toggles / samplers / stop."""
    names = sorted(catalog["connections"])
    local = [
        name
        for name in names
        if str(catalog["connections"][name].get("base_url", "")).startswith(
            ("http://localhost:", "http://127.0.0.1:")
        )
    ]
    if not local:  # pragma: no cover - a local connection keeps the save ping quick
        raise GateError("no local connection to edit (the save ping would hang)")
    name = min(local, key=lambda n: catalog["connections"][n].get("timeout") or 3600)
    original = catalog["connections"][name]
    log(f"editing connection {name!r}")

    connection_card(dialog, name).get_by_role("button", name="Edit").click()
    form = dialog.locator("form.form").first
    form.wait_for(state="visible", timeout=15_000)

    label_text = form.inner_text()
    check("Response Length" in label_text, "the form labels the generation cap `Response Length`")
    check("Context Size" in label_text, "the form labels the window `Context Size`")
    lowered = label_text.lower()
    bad = [token for token in FORBIDDEN_FORM_TOKENS if token in lowered]
    check(not bad, f"no llama.cpp-specific knob appears in the form ({bad})")

    form.locator('input[data-field="context_window"]').fill("32768")
    form.locator('input[data-field="max_tokens"]').fill("2048")
    form.get_by_label("Temperature").fill("0.66")
    form.get_by_label("Top P").fill("0.91")
    form.get_by_label("Top K").fill("33")
    form.get_by_label("Repetition Penalty").fill("1.07")
    form.get_by_label("Frequency Penalty").fill("0.22")
    form.get_by_label("Presence Penalty").fill("0.11")
    form.get_by_label("Seed").fill("4242")
    form.locator('textarea[data-field="stop"]').fill("STOP-A\nSTOP-B")
    thinking = form.locator('input[data-field="thinking"]')
    streaming = form.locator('input[data-field="streaming"]')
    check(thinking.is_checked(), "`Thinking` starts checked (absence is on)")
    check(streaming.is_checked(), "`Streaming` starts checked (absence is on)")
    thinking.uncheck()
    streaming.uncheck()
    shot(page, shots_dir, "03-form-widened.png")

    form.get_by_role("button", name="Save & ping").click()
    form.wait_for(state="detached", timeout=60_000)
    dialog.locator("li.connection").first.wait_for(state="visible", timeout=30_000)

    saved = http_json(f"{api_url}/api/connections")["connections"][name]
    sampling = saved["sampling"]
    check(saved["context_window"] == 32768, f"Context Size round-trips ({saved['context_window']})")
    check(saved["thinking"] is False, f"an unchecked Thinking writes `false` ({saved['thinking']})")
    check(saved["streaming"] is False, f"an unchecked Streaming writes `false` ({saved['streaming']})")
    check(sampling.get("max_tokens") == 2048, f"Response Length -> max_tokens ({sampling.get('max_tokens')})")
    check(sampling.get("stop") == ["STOP-A", "STOP-B"], f"stop sequences -> string[] ({sampling.get('stop')})")
    for key, want in (
        ("temperature", 0.66),
        ("top_p", 0.91),
        ("top_k", 33),
        ("repetition_penalty", 1.07),
        ("frequency_penalty", 0.22),
        ("presence_penalty", 0.11),
        ("seed", 4242),
    ):
        check(sampling.get(key) == want, f"sampler `{key}` saved as {want} ({sampling.get(key)})")

    # Reload: close the drawer, open it again, re-open the form — the UI must show
    # what the engine stored (no optimistic local state).
    dialog.get_by_role("button", name="Close connections").click()
    dialog.wait_for(state="detached", timeout=15_000)
    dialog = open_drawer(page, page.url.split("/chat")[0])
    connection_card(dialog, name).get_by_role("button", name="Edit").click()
    form = dialog.locator("form.form").first
    form.wait_for(state="visible", timeout=15_000)
    check(
        form.locator('input[data-field="context_window"]').input_value() == "32768",
        "Context Size reloads into the form",
    )
    check(
        form.locator('input[data-field="max_tokens"]').input_value() == "2048",
        "Response Length reloads into the form",
    )
    check(not form.locator('input[data-field="thinking"]').is_checked(), "Thinking reloads unchecked")
    check(not form.locator('input[data-field="streaming"]').is_checked(), "Streaming reloads unchecked")
    check(form.get_by_label("Top K").input_value() == "33", "Top K reloads")
    check(
        form.locator('textarea[data-field="stop"]').input_value() == "STOP-A\nSTOP-B",
        "Stop Sequences reload one per line",
    )
    shot(page, shots_dir, "04-form-reloaded.png")

    # Toggles back ON: absence is the on position, so the stored value must be null.
    form.locator('input[data-field="thinking"]').check()
    form.locator('input[data-field="streaming"]').check()
    form.get_by_role("button", name="Save & ping").click()
    form.wait_for(state="detached", timeout=60_000)
    toggled = http_json(f"{api_url}/api/connections")["connections"][name]
    check(toggled["thinking"] is None, f"a checked Thinking writes `null`, never true ({toggled['thinking']})")
    check(toggled["streaming"] is None, f"a checked Streaming writes `null` ({toggled['streaming']})")
    return name, original


def restore(name: str, original: dict, agents_baseline: dict[str, str], api_url: str) -> None:
    """Put the connection's editable state and the agent map back (best effort)."""
    body = {
        "context_window": original.get("context_window"),
        "thinking": original.get("thinking"),
        "streaming": original.get("streaming"),
        "sampling": original.get("sampling") or {},
        "extra_body": original.get("extra_body") or {},
        "timeout": original.get("timeout"),
        "note": original.get("note"),
    }
    http_json(f"{api_url}/api/connections/{name}", "PATCH", body)
    for slot, connection in agents_baseline.items():
        try:
            http_json(f"{api_url}/api/connections/agents/{slot}", "PATCH", {"connection": connection})
        except urllib.error.HTTPError as err:  # a slot the config no longer resolves
            log(f"  note  could not restore agent {slot!r}: {err}")
    log(f"restored {name!r} and the baseline agent bindings")


# --- phase 4: the VITE_MOCK demo ---------------------------------------------------


def check_mock_demo(page, base_url: str, shots_dir: Path) -> None:
    dialog = open_drawer(page, base_url)
    headers = [text.strip() for text in dialog.locator("h4.slot-type").all_inner_texts()]
    check(headers == ["Completion", "Embedding", "Decision"], f"mock demo groups {headers}")
    check(
        slot_select(dialog, "completion.narration").input_value() == "your-model-id",
        "the mock seeds a typed key (`completion.narration` -> your-model-id)",
    )
    check(slot_via(dialog, "completion.narration") == "set here", "the seeded typed row reads `set here`")
    inert = dialog.locator('[data-slot="completion.narration"] .slot-inert')
    check(
        inert.count() == 1 and "narrator" in inert.first.inner_text(),
        "the mock demo shows the inert legacy alias",
    )
    check(
        slot_via(dialog, "captioner") == "via task completion.extraction",
        f"an unauthored item row shows its task fallback ({slot_via(dialog, 'captioner')!r})",
    )
    card = connection_card(dialog, "your-model-id")
    meta = " ".join(card.inner_text().split())
    check("context: 65536" in meta, f"the mock seeds Context Size on the card ({meta!r})")
    check("thinking: on" in meta, "the mock seeds Thinking on (absence = on)")
    check("streaming: on" in meta, "the mock seeds Streaming on (absence = on)")
    dialog.locator("section.agents").scroll_into_view_if_needed()
    shot(page, shots_dir, "05-mock-agents.png")

    # A mock rebind must also stay truthful (upsert + canonical normalization).
    slot_select(dialog, "completion.state").select_option("your-model-id")
    wait_status(dialog, "Rebound")
    check(
        slot_via(dialog, "completion.state") == "set here",
        "a mock rebind of an unauthored slot lands on its canonical key",
    )
    shot(page, shots_dir, "06-mock-rebind.png")


# --- main --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="frontend origin (engine-backed)")
    parser.add_argument("--api-url", default=DEFAULT_API_URL, help="isolated engine origin")
    parser.add_argument("--mock-url", default=DEFAULT_MOCK_URL, help="frontend origin (VITE_MOCK=1)")
    parser.add_argument("--shots-dir", default="/tmp/w9_25_drawer", help="where to write screenshots")
    parser.add_argument("--skip-mock", action="store_true", help="skip the VITE_MOCK demo pass")
    parser.add_argument("--no-restore", action="store_true", help="leave the mutations in place")
    parser.add_argument("--headed", action="store_true", help="show the browser")
    args = parser.parse_args()

    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    if not Path(CHROME).exists():
        log(f"ERROR: chromium not found at {CHROME} (set FABULIST_CHROME)")
        return 2
    shots_dir = Path(args.shots_dir)
    shots_dir.mkdir(parents=True, exist_ok=True)

    # ── preflight: the payload, the shape, the provenance ──────────────────────
    try:
        catalog = fetch_catalog(args.api_url)
        rows = check_payload_shape(catalog)
        local = [
            name
            for name, entry in catalog["connections"].items()
            if str(entry.get("base_url", "")).startswith(("http://localhost:", "http://127.0.0.1:"))
        ]
        check(bool(local), f"the engine has a local connection to edit fast ({local})")
        session = http_json(
            f"{args.api_url}/api/sessions", "POST", {"title": "W9.25 drawer pass"}
        )
        session_id = str(session.get("id") or session.get("session_id"))
        check(bool(session_id), f"created a throwaway session ({session_id})")
    except (urllib.error.URLError, KeyError, GateError) as err:
        log(f"ERROR: engine preflight failed: {err}")
        return 2

    log(
        f"engine payload ok: {len(rows)} slot rows, {len(catalog['connections'])} connections, "
        f"{len(catalog['agents'])} agent keys"
    )
    for slot in ("completion.narration", "completion.state", "captioner"):
        log(f"  baseline {slot}: {rows[slot]['connection']!r} via {rows[slot]['via']!r}")

    errors: list[str] = []
    page_errors: list[str] = []

    def watch(page, label: str) -> None:
        def on_console(message) -> None:
            if message.type != "error":
                return
            text = message.text
            if any(benign in text for benign in BENIGN_CONSOLE):
                return
            # The save ping deliberately probes an endpoint that may be down; the
            # drawer surfaces that in its own status line (asserted above).
            if "Failed to load resource" in text and "/models" in text:
                return
            errors.append(f"{label}: console: {text}")

        page.on("console", on_console)
        page.on("pageerror", lambda err: page_errors.append(f"{label}: {err}"))

    baseline_agents = dict(catalog["agents"])
    edited_name = ""
    original_entry: dict = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME, args=["--no-sandbox"], headless=not args.headed
        )
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 950})
            page.set_default_timeout(30_000)
            watch(page, "drawer")

            dialog = open_drawer(page, args.base_url, session_id)
            log(f"opened the drawer at {args.base_url}/chat")
            check_grouped_rows(page, dialog, shots_dir)
            check_provenance(dialog, rows, catalog)
            rebind_task_row(page, dialog, args.api_url, rows, catalog, shots_dir)
            rebind_type_row(page, dialog, args.api_url, rows, catalog)
            edited_name, original_entry = edit_connection_fields(
                page, dialog, args.api_url, catalog, shots_dir
            )

            if not args.skip_mock:
                mock_page = browser.new_page(viewport={"width": 1440, "height": 950})
                mock_page.set_default_timeout(30_000)
                watch(mock_page, "mock")
                check_mock_demo(mock_page, args.mock_url, shots_dir)
                mock_page.close()
        except (GateError, PWTimeout) as err:
            log(f"  FAIL  {err}")
            failures = 1
        else:
            failures = 0
        finally:
            browser.close()

    if not args.no_restore and edited_name:
        restore(edited_name, original_entry, baseline_agents, args.api_url)

    try:
        check(not page_errors, f"zero page errors across every page ({page_errors[:3]})")
        check(not errors, f"no unexpected console errors ({errors[:3]})")
    except GateError as err:
        log(f"  FAIL  {err}")
        failures = 1

    log("")
    log(f"screenshots: {', '.join(SHOTS) or '(none)'}")
    if failures:
        log("RESULT: FAIL")
        return 1
    log("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
