#!/usr/bin/env python
"""Fold retired ``prompt_overrides`` values into each flow's ``system`` row.

W6.3 retired the ``ooc_system`` / ``extractor_system`` override shim:
:func:`fabulist.prompts.preset_store.compile_preset_flow` no longer consults
``preset.prompt_overrides`` — the flow's own ``system`` row body is the one
authoring seam. A preset that still carries an override string would silently
lose that text, so this one-time migration moves it to where the engine now
reads it.

Per (preset, override key):

  * FOLD when the target flow is all-default (never edited — its rows match
    :data:`fabulist.prompts.modules.DEFAULT_FLOWS`). The override string is
    copied into the flow's ``system`` row ``content`` (the flow is materialized
    from ``DEFAULT_FLOWS`` first when the preset defines no ``ooc``/``extract``
    rows), then the key is dropped.
  * DROP when the flow is already customized: the authored flow wins anyway, so
    the override was already inert — the key is cleared with no content change.

HTTP-first, API-only
--------------------
Like ``scripts/migrate_directive_surfaces.py`` this never touches ``data/`` or
the SQLite log directly (gitignored user state owned by the running engine):

  1. ``GET /api/prompts/presets`` — the preset wire still carries
     ``prompt_overrides`` while non-empty (read-tolerant, W6.3);
  2. build the ``PUT`` body: the flows with the override folded in, the override
     key omitted (so the store clears it);
  3. ``PUT /api/prompts/presets/{id}`` with the FULL preset, every other field
     preserved verbatim.

Idempotent: once an override is cleared the wire no longer carries it, so a
rerun is a no-op. ``--dry-run`` prints the plan and writes nothing.

Usage::

    uv run python scripts/migrate_prompt_overrides.py --dry-run  # plan only
    uv run python scripts/migrate_prompt_overrides.py            # apply

Base URL defaults to ``$FABULIST_ENGINE_URL`` or ``http://localhost:2727``.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

#: Default engine base URL (overridable with --base-url or the env var).
DEFAULT_BASE_URL = os.environ.get("FABULIST_ENGINE_URL", "http://localhost:2727")

#: Retired override key -> the interaction whose ``system`` row it folds into.
#: Mirrors the removed ``preset_store.FLOW_OVERRIDE_KEYS``.
OVERRIDE_KEYS: dict[str, str] = {
    "ooc_system": "ooc",
    "extractor_system": "extract",
}


# -- tolerant readers --------------------------------------------------------


def override_map(preset: Mapping[str, Any]) -> dict[str, str]:
    """The preset's non-empty ``prompt_overrides`` map (empty when absent)."""
    raw = preset.get("prompt_overrides")
    if not isinstance(raw, Mapping):
        return {}
    return {
        str(key): value
        for key, value in raw.items()
        if isinstance(value, str) and value.strip()
    }


def default_flow(interaction: str) -> list[dict[str, Any]]:
    """The code default flow rows for ``interaction`` (type/content/order)."""
    from fabulist.prompts.modules import DEFAULT_FLOWS

    return [
        {"type": module.type, "content": module.content}
        for module in DEFAULT_FLOWS.get(interaction, [])
    ]


def flow_is_default(interaction: str, rows: Any) -> bool:
    """True when ``rows`` match ``DEFAULT_FLOWS[interaction]`` (type + content).

    A missing / empty list is the all-default signal too: the engine falls back
    to ``DEFAULT_FLOWS`` for an interaction the preset does not define
    (``preset.flows.get(interaction) or DEFAULT_FLOWS[interaction]``).
    """
    if not rows:
        return True
    if not isinstance(rows, (list, tuple)):
        return False
    normalized = [
        {"type": str(row.get("type")), "content": str(row.get("content") or "")}
        for row in rows
        if isinstance(row, Mapping)
    ]
    return normalized == default_flow(interaction)


# -- plan --------------------------------------------------------------------


@dataclass
class PlanEntry:
    """One (preset, override key) migration decision."""

    preset_id: str
    key: str
    interaction: str
    action: str  # fold | drop
    reason: str


def _materialized_system_rows(interaction: str, content: str) -> list[dict[str, Any]]:
    """A fresh default flow for ``interaction`` with the system row authored."""
    from fabulist.prompts.modules import DEFAULT_FLOWS

    rows: list[dict[str, Any]] = []
    for index, module in enumerate(DEFAULT_FLOWS.get(interaction, ())):
        row: dict[str, Any] = {
            "id": f"{interaction}_{index}",
            "name": module.name,
            "type": module.type,
            "role": module.role,
            "content": content if module.type == "system" else module.content,
            "enabled": module.enabled,
        }
        rows.append(row)
    if not any(row["type"] == "system" for row in rows):
        rows.append(
            {
                "id": f"{interaction}_system",
                "name": "System prompt",
                "type": "system",
                "role": "system",
                "content": content,
                "enabled": True,
            }
        )
    return rows


def build_plan(presets: list[Mapping[str, Any]]) -> list[PlanEntry]:
    """Classify every preset's every override key (fold vs drop)."""
    entries: list[PlanEntry] = []
    for preset in presets:
        preset_id = str(preset.get("preset_id") or "")
        flows = preset.get("flows") if isinstance(preset.get("flows"), Mapping) else {}
        for key, interaction in OVERRIDE_KEYS.items():
            if key not in override_map(preset):
                continue
            rows = flows.get(interaction) if isinstance(flows, Mapping) else None
            if flow_is_default(interaction, rows):
                entries.append(
                    PlanEntry(
                        preset_id,
                        key,
                        interaction,
                        "fold",
                        f"{interaction} flow is all-default -> copy into the system row",
                    )
                )
            else:
                entries.append(
                    PlanEntry(
                        preset_id,
                        key,
                        interaction,
                        "drop",
                        f"{interaction} flow is customized -> it already wins",
                    )
                )
    return entries


def put_body(preset: Mapping[str, Any], entries: list[PlanEntry]) -> dict[str, Any]:
    """The full-replace PUT body: flows updated, ``prompt_overrides`` omitted.

    Every preset field rides through verbatim; the flow named by each plan entry
    has its ``system`` row authored (fold) or is left untouched (drop). Omitting
    ``prompt_overrides`` makes the store clear it (W6.3 write shape).
    """
    flows = {
        str(interaction): [dict(row) for row in rows]
        for interaction, rows in (preset.get("flows") or {}).items()
        if isinstance(rows, (list, tuple))
    }
    overrides = override_map(preset)
    for entry in entries:
        if entry.action != "fold":
            continue
        content = overrides.get(entry.key, "")
        existing = flows.get(entry.interaction)
        if existing:
            for row in existing:
                if isinstance(row, dict) and row.get("type") == "system":
                    row["content"] = content
            if not any(row.get("type") == "system" for row in existing):
                existing.append(_materialized_system_rows(entry.interaction, content)[-1])
        else:
            flows[entry.interaction] = _materialized_system_rows(
                entry.interaction, content
            )
    body: dict[str, Any] = {
        "name": str(preset.get("name") or "default"),
        "modules": [dict(row) for row in (preset.get("modules") or [])],
        "flows": flows,
        "agent_bindings": preset.get("agent_bindings") or {},
        "surfaces": preset.get("surfaces") or {},
    }
    if preset.get("directives"):
        body["directives"] = [dict(row) for row in preset["directives"]]
    return body


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


# -- presentation ------------------------------------------------------------


def _print_plan(entries: list[PlanEntry]) -> None:
    """Print the per-(preset, key) migration plan as a small aligned table."""
    preset_width = max(len(entry.preset_id) for entry in entries)
    key_width = max(len(entry.key) for entry in entries)
    reason_width = max(len(entry.reason) for entry in entries)
    print(
        f"| {'preset'.ljust(preset_width)} | {'override'.ljust(key_width)} | "
        f"action | {'basis'.ljust(reason_width)} |"
    )
    print(
        f"|{'-' * (preset_width + 2)}|{'-' * (key_width + 2)}|--------|"
        f"{'-' * (reason_width + 2)}|"
    )
    for entry in entries:
        print(
            f"| {entry.preset_id.ljust(preset_width)} | "
            f"{entry.key.ljust(key_width)} | {entry.action.ljust(6)} | "
            f"{entry.reason.ljust(reason_width)} |"
        )


# -- main --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run the migration; return a process exit code."""
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
    args = parser.parse_args(argv)
    base_url = args.base_url.rstrip("/")

    mode = "DRY RUN (no writes)" if args.dry_run else "APPLY"
    print(f"migrate prompt overrides — {mode}")
    print(f"engine: {base_url}")
    print()

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        try:
            presets = _expect(client.get("/api/prompts/presets"))["presets"]
        except httpx.HTTPError as exc:
            raise SystemExit(
                f"engine unreachable at {base_url} ({exc}); start it "
                "(systemctl --user start fabulist-engine) or pass --base-url"
            ) from None
        entries = build_plan(presets)
        if not entries:
            print("no presets carry prompt_overrides — no-op.")
            return 0

        _print_plan(entries)
        print()
        by_preset: dict[str, list[PlanEntry]] = {}
        for entry in entries:
            by_preset.setdefault(entry.preset_id, []).append(entry)
        folded = sum(1 for entry in entries if entry.action == "fold")
        dropped = sum(1 for entry in entries if entry.action == "drop")
        print(
            f"plan: fold {folded} override(s), drop {dropped} override(s) "
            f"across {len(by_preset)} preset(s)"
        )

        if args.dry_run:
            print("would clear the keys above; nothing written.")
            return 0

        index = {str(preset.get("preset_id")): preset for preset in presets}
        for preset_id, group in by_preset.items():
            body = put_body(index[preset_id], group)
            updated = _expect(
                client.put(f"/api/prompts/presets/{preset_id}", json=body)
            )["preset"]
            if override_map(updated):
                print(
                    f"VERIFY FAIL: {preset_id} still carries prompt_overrides "
                    f"{sorted(override_map(updated))}",
                    file=sys.stderr,
                )
                return 1
            print(f"cleared {preset_id}: {', '.join(entry.key for entry in group)}")

    print(
        f"done: cleared {len(entries)} override key(s) "
        f"({folded} folded, {dropped} dropped)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
