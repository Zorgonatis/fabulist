#!/usr/bin/env python
"""Seed the persisted default preset's non-narrate system prompts (D26 migration).

Owner directive: every model-facing prompt string must become preset-carried
data — *"nothing prompt-like in a python file"*. The Python constants stay as
the factory / code fallbacks; this one-time migration copies them into the
**persisted default preset** (the D26 row in the event log, which is what the
flow compiler reads) and then commits the versioned factory artifact
(``prompts/factory-defaults.json``).

Scope (this lane)
-----------------
Flows covered, and the constant each one's ``system`` module is seeded from —
imported from the same modules the engine imports them from::

    ooc          fabulist.wiring.OOC_SYSTEM
    extract      fabulist.state.extractor.EXTRACTOR_SYSTEM_PROMPT
    summarize    fabulist.graph.compaction.SUMMARIZER_SYSTEM_PROMPT
    caption      fabulist.media.images.CAPTION_SYSTEM
    bts          fabulist.state.bts.BTS_SYSTEM_PROMPT
    critic       fabulist.prompts.critic.CRITIC_SYSTEM_PROMPT

``narrate`` is intentionally untouched (its ``narrator_system`` row is assembled
by the compiler, not a single constant). ``attribution`` is no longer a flow at
all: speaker attribution is deterministic code (owner decision), so there is no
model-facing prompt left to seed. ``assistant`` is **skipped on purpose**:
its system prompt is per-definition
(``fabulist.assistants.manager.render_assistant_system``), not one constant, so
there is nothing to seed into the flow.

HTTP-first, API-only
--------------------
The script never touches ``data/`` or the SQLite log directly (that is
gitignored user state owned by the running engine). It:

  1. ensures the engine is up (``systemctl --user`` restart when down),
  2. ``GET /api/prompts/default``,
  3. ``PUT /api/prompts/presets/default`` with each covered flow's ``system``
     module content set (a full-preset replace — the log-owned write path),
  4. ``POST /api/prompts/default/commit`` to regenerate the versioned artifact.

Idempotent: a flow whose ``system`` row already carries non-empty content is
skipped unless ``--force`` is given. When nothing needs writing, no
state-mutating request is sent at all, so a rerun is a true no-op. ``--dry-run``
prints the plan and writes nothing.

Usage::

    uv run python scripts/seed_prompt_defaults.py            # apply
    uv run python scripts/seed_prompt_defaults.py --dry-run  # plan only
    uv run python scripts/seed_prompt_defaults.py --force    # overwrite non-empty

Base URL defaults to ``$FABULIST_ENGINE_URL`` or ``http://localhost:2727``.
"""

from __future__ import annotations

import argparse
import copy
import os
import subprocess
import sys
import time
from dataclasses import dataclass

import httpx

#: Default engine base URL (overridable with --base-url or the env var).
DEFAULT_BASE_URL = os.environ.get("FABULIST_ENGINE_URL", "http://localhost:2727")

#: The system-event anchor session the preset store folds (informational only).
DEFAULT_PRESET_ID = "default"

#: flow -> the canonical id of its ``system`` structural row in the default
#: flow (see ``fabulist.prompts.modules.DEFAULT_FLOWS``). The row is matched by
#: id first, then by ``type == "system"`` as a fallback.
FLOW_SYSTEM_MODULE_ID: dict[str, str] = {
    "ooc": "ooc_system",
    "extract": "extract_system",
    "summarize": "summarize_system",
    "caption": "caption_system",
    "bts": "bts_system",
    "critic": "critic_system",
}

#: How long to wait for the engine to answer after a restart, in seconds.
ENGINE_BOOT_TIMEOUT = 60.0


@dataclass
class PlanEntry:
    """One flow's seeding decision (rendered in the plan table)."""

    flow: str
    module_id: str
    action: str  # set | already | skip-nonempty | missing | no-system-row
    current_chars: int
    new_chars: int


def resolve_system_prompts() -> dict[str, str]:
    """Import each flow's system constant from its engine call site.

    Lazy per-module imports mirror the engine's own import sites (see
    ``fabulist.wiring`` / ``state`` / ``graph`` / ``media``), so the seeded
    value can never silently drift from the module the engine reads.
    """
    # Lazy imports: wiring imports fabulist.api, which imports this script's
    # consumers; importing at call time keeps the module import graph flat.
    from fabulist.graph.compaction import SUMMARIZER_SYSTEM_PROMPT
    from fabulist.media.images import CAPTION_SYSTEM
    from fabulist.prompts.critic import CRITIC_SYSTEM_PROMPT
    from fabulist.state.bts import BTS_SYSTEM_PROMPT
    from fabulist.state.extractor import EXTRACTOR_SYSTEM_PROMPT
    from fabulist.wiring import OOC_SYSTEM

    return {
        "ooc": OOC_SYSTEM,
        "extract": EXTRACTOR_SYSTEM_PROMPT,
        "summarize": SUMMARIZER_SYSTEM_PROMPT,
        "caption": CAPTION_SYSTEM,
        "bts": BTS_SYSTEM_PROMPT,
        "critic": CRITIC_SYSTEM_PROMPT,
    }


# -- HTTP helpers ------------------------------------------------------------


def _expect(response: httpx.Response) -> dict:
    """Return a JSON body for a 2xx response, else exit with the API detail."""
    if response.status_code >= 400:
        detail = response.text
        try:
            detail = response.json().get("detail", detail)
        except ValueError:
            pass
        raise SystemExit(
            f"engine returned HTTP {response.status_code} for "
            f"{response.request.method} {response.request.url}: {detail}"
        )
    return response.json()


def _engine_alive(client: httpx.Client) -> bool:
    """True when ``GET /api/prompts/default`` answers 200."""
    try:
        return client.get("/api/prompts/default", timeout=3.0).status_code == 200
    except httpx.HTTPError:
        return False


def ensure_engine(client: httpx.Client, base_url: str) -> None:
    """Make sure the engine answers; restart the user unit when it is down.

    Probes first (a healthy API need not be restarted); on failure checks
    ``systemctl --user is-active fabulist-engine`` and restarts the unit when it
    is not active, then polls until the API answers.
    """
    if _engine_alive(client):
        return
    active = subprocess.run(
        ["systemctl", "--user", "is-active", "fabulist-engine"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    if active != "active":
        print(
            f"engine not active at {base_url} (systemctl is-active: "
            f"{active or 'unknown'}) — restarting fabulist-engine…"
        )
        subprocess.run(
            ["systemctl", "--user", "restart", "fabulist-engine"], check=False
        )
    deadline = time.monotonic() + ENGINE_BOOT_TIMEOUT
    while time.monotonic() < deadline:
        if _engine_alive(client):
            return
        time.sleep(1.0)
    raise SystemExit(
        f"engine at {base_url} did not become reachable within "
        f"{ENGINE_BOOT_TIMEOUT:.0f}s"
    )


# -- plan --------------------------------------------------------------------


def _find_system_row(rows: list[dict], module_id: str) -> dict | None:
    """The flow's system row: matched by id first, then by structural type."""
    for row in rows:
        if str(row.get("id") or "") == module_id:
            return row
    for row in rows:
        if str(row.get("type") or "") == "system":
            return row
    return None


def build_plan(
    preset: dict, constants: dict[str, str], *, force: bool
) -> list[PlanEntry]:
    """Decide, per covered flow, whether its system module needs seeding.

    Skips a flow whose row already holds non-empty content (owner edit or a
    prior seed) unless ``force``; a row already equal to the constant is always
    a no-op.
    """
    flows = preset.get("flows") or {}
    entries: list[PlanEntry] = []
    for flow, module_id in FLOW_SYSTEM_MODULE_ID.items():
        new = constants[flow]
        rows = flows.get(flow)
        if not rows:
            entries.append(PlanEntry(flow, module_id, "missing", 0, len(new)))
            continue
        row = _find_system_row(rows, module_id)
        if row is None:
            entries.append(PlanEntry(flow, module_id, "no-system-row", 0, len(new)))
            continue
        current = str(row.get("content") or "")
        if current == new:
            action = "already"
        elif current.strip() and not force:
            action = "skip-nonempty"
        else:
            action = "set"
        entries.append(PlanEntry(flow, module_id, action, len(current), len(new)))
    return entries


def apply_plan(
    flows: dict, entries: list[PlanEntry], constants: dict[str, str]
) -> dict:
    """Return a deep-copied ``flows`` map with the planned system rows seeded."""
    updated = copy.deepcopy(flows)
    for entry in entries:
        if entry.action != "set":
            continue
        row = _find_system_row(updated.get(entry.flow) or [], entry.module_id)
        assert row is not None  # a "set" entry always located its row
        row["content"] = constants[entry.flow]
    return updated


def put_body(preset: dict, flows: dict) -> dict:
    """Build the ``PUT /api/prompts/presets/default`` full-replace payload.

    ``modules`` is the canonical narrate flow (the store mirrors
    ``flows["narrate"]`` from it). ``surfaces`` rides through untouched so the
    D27 token surfaces are preserved across the edit.
    """
    body: dict = {
        "name": str(preset.get("name") or DEFAULT_PRESET_ID),
        "modules": flows.get("narrate") or preset.get("modules") or [],
        "flows": flows,
        "toggles": preset.get("toggles") or {},
        "prompt_overrides": preset.get("prompt_overrides") or {},
        "agent_bindings": preset.get("agent_bindings") or {},
    }
    if preset.get("surfaces"):
        # D35 guard: the library_doc surface must ride through as the EXPRESSION
        # (``{{library_doc}}``), never as the live preset's RESOLVED text — a
        # stored-resolved surface in the live DB would otherwise bake the doc
        # into the committed artifact and break the expression contract (found
        # live 2026-10-12: the artifact carried expanded text after a --force).
        surfaces = dict(preset["surfaces"])
        if surfaces.get("library_doc"):
            surfaces["library_doc"] = "{{library_doc}}"
        body["surfaces"] = surfaces
    return body


def verify(preset: dict, constants: dict[str, str]) -> list[str]:
    """Return a list of human-readable mismatches (empty when all rows seeded)."""
    flows = preset.get("flows") or {}
    problems: list[str] = []
    for flow, module_id in FLOW_SYSTEM_MODULE_ID.items():
        row = _find_system_row(flows.get(flow) or [], module_id)
        if row is None:
            problems.append(f"{flow}: no system row")
            continue
        actual = str(row.get("content") or "")
        if actual != constants[flow]:
            problems.append(
                f"{flow}: content mismatch (got {len(actual)} chars, "
                f"expected {len(constants[flow])})"
            )
    return problems


# -- presentation ------------------------------------------------------------


def _print_plan(entries: list[PlanEntry]) -> None:
    """Print the per-flow seeding plan as a small aligned table."""
    labels = {
        "set": "SEED",
        "already": "already seeded",
        "skip-nonempty": "SKIP (non-empty)",
        "missing": "MISSING FLOW",
        "no-system-row": "NO SYSTEM ROW",
    }
    key_width = max(len(label) for label in ("flow", *FLOW_SYSTEM_MODULE_ID))
    action_width = max(len(labels[e.action]) for e in entries)
    print(f"| {'flow'.ljust(key_width)} | {'action'.ljust(action_width)} | chars |")
    print(f"|{'-' * (key_width + 2)}|{'-' * (action_width + 2)}|-------|")
    for entry in entries:
        chars = (
            f"{entry.current_chars} -> {entry.new_chars}"
            if entry.action in ("set", "already", "skip-nonempty")
            else "-"
        )
        print(
            f"| {entry.flow.ljust(key_width)} | "
            f"{labels[entry.action].ljust(action_width)} | {chars} |"
        )
    print()
    print(
        "note: flow 'assistant' is intentionally skipped — its system prompt is "
        "per-definition (assistants.manager.render_assistant_system), not a single "
        "constant."
    )


# -- main --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run the seed migration; return a process exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"engine base URL (default: {DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the plan and write nothing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-seed even rows whose system content is already non-empty",
    )
    args = parser.parse_args(argv)
    base_url = args.base_url.rstrip("/")

    constants = resolve_system_prompts()
    missing = [flow for flow, value in constants.items() if not str(value).strip()]
    if missing:
        raise SystemExit(f"Python constants are empty for flow(s): {', '.join(missing)}")

    mode = "DRY RUN (no writes)" if args.dry_run else "APPLY"
    print(f"seed prompt defaults — {mode}")
    print(f"engine: {base_url}")
    print()

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        ensure_engine(client, base_url)
        status = _expect(client.get("/api/prompts/default"))
        preset = status["preset"]

        needs_reset = not status.get("present", False)
        if needs_reset:
            print(
                "no persisted default row yet (pre-boot) — a real run will "
                "POST /api/prompts/default/reset to seed it from the factory image."
            )

        entries = build_plan(preset, constants, force=args.force)
        _print_plan(entries)
        to_write = [e for e in entries if e.action == "set"]

        if not to_write:
            print("nothing to write — rerun is a no-op.")
            return 0

        if args.dry_run:
            print(f"would seed {len(to_write)} flow(s) and commit the artifact.")
            return 0

        if needs_reset:
            preset = _expect(client.post("/api/prompts/default/reset"))["preset"]
            entries = build_plan(preset, constants, force=args.force)
            to_write = [e for e in entries if e.action == "set"]
            if not to_write:
                print("nothing to write after reset — rerun is a no-op.")
                return 0

        flows = preset.get("flows") or {}
        seeded = apply_plan(flows, to_write, constants)
        updated = _expect(
            client.put("/api/prompts/presets/default", json=put_body(preset, seeded))
        )["preset"]
        print(f"seeded {len(to_write)} flow(s): {', '.join(e.flow for e in to_write)}")

        committed = _expect(client.post("/api/prompts/default/commit"))
        print(f"committed artifact: {committed['artifact_path']}")
        print(
            f"factory_version: {committed.get('factory_version')} "
            f"(factory_changed: {committed.get('factory_changed')})"
        )

        problems = verify(updated, constants)
        if problems:
            for problem in problems:
                print(f"VERIFY FAIL: {problem}", file=sys.stderr)
            return 1
        print("verified: every covered flow's system row carries its constant.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
