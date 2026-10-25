#!/usr/bin/env python
"""Fold retired ``settings_changed`` directive toggles into the default preset.

W6.3 made the preset's directive ROWS the single directive ``enabled`` source:
:func:`fabulist.wiring.make_narrator_message_builder` no longer folds
``current_toggles(log)``. A user whose directive choices still live in the
old ``settings_changed`` map would silently lose them, so this one-time
migration writes them onto the persisted default preset's directive rows
(materializing the code catalog first).

NOTHING is protected (D67): every known directive is patchable, so a legacy
``false`` is honoured exactly like any other value. No id is refused.

The legacy map is read back READ-ONLY from ``GET /api/settings`` under
``legacy_toggles`` (the raw latest-wins ``settings_changed`` fold, W6.3); the
current effective state is the ``toggles`` map, folded from the preset rows.

Classify each legacy id:

  * APPLY — the id is a known directive and the preset row does not already
    carry the value. The row is set (materialized if needed).
  * KEEP — unknown id, or a value the preset rows already match. The effective
    state is already correct, so nothing changes for that id.

HTTP-first, API-only
--------------------
Like ``scripts/migrate_directive_surfaces.py`` this never touches ``data/`` or
the SQLite log directly (gitignored user state owned by the running engine):

  1. ``GET /api/settings`` — ``legacy_toggles`` (input) + ``toggles`` (current);
  2. ``GET /api/prompts/presets/default`` — the persisted default row; when it
     is missing (pre-boot) it is seeded first via
     ``POST /api/prompts/default/reset`` (there is nothing to overwrite);
  3. ``PUT /api/prompts/presets/default`` with the FULL preset, its directive
     rows updated.

Idempotent: after applying, every legacy value matches the preset rows, so a
rerun classifies everything as KEEP and writes nothing. ``--dry-run`` prints the
plan and writes nothing.

Usage::

    uv run python scripts/migrate_directive_toggles.py --dry-run  # plan only
    uv run python scripts/migrate_directive_toggles.py            # apply

Base URL defaults to ``$FABULIST_ENGINE_URL`` or ``http://localhost:2727``.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import Any

import httpx

#: Default engine base URL (overridable with --base-url or the env var).
DEFAULT_BASE_URL = os.environ.get("FABULIST_ENGINE_URL", "http://localhost:2727")


# -- catalog -----------------------------------------------------------------


def catalog() -> dict[str, Any]:
    """The code directive catalog keyed by id."""
    from fabulist.prompts.parts import default_directives

    return {
        directive.id: {
            "row": {
                "id": directive.id,
                "name": directive.name,
                "content": directive.content,
                "priority": directive.priority,
                "tier": directive.tier,
                "core": directive.core,
                "protected": directive.protected,
                "enabled": directive.enabled,
                "description": directive.description,
            },
        }
        for directive in default_directives()
    }


# -- plan --------------------------------------------------------------------


@dataclass
class PlanEntry:
    """One legacy directive id's migration decision."""

    ident: str
    value: bool
    action: str  # apply | keep
    reason: str


def build_plan(
    legacy: dict[str, bool],
    current: dict[str, bool],
    known: dict[str, Any],
) -> list[PlanEntry]:
    """Classify every legacy toggle id: fold onto the preset rows, or keep."""
    entries: list[PlanEntry] = []
    for ident, value in legacy.items():
        if ident not in known:
            entries.append(
                PlanEntry(ident, value, "keep", "unknown directive id")
            )
            continue
        if current.get(ident) == value:
            entries.append(
                PlanEntry(ident, value, "keep", "preset rows already match")
            )
            continue
        entries.append(
            PlanEntry(ident, value, "apply", "fold the legacy settings value")
        )
    return entries


def apply_rows(preset: dict, entries: list[PlanEntry], known: dict[str, Any]) -> dict:
    """The full-replace PUT body with the directive rows updated.

    Every preset field rides through verbatim; the directive rows are the stored
    rows (materialized from the code catalog when empty) with each APPLY id set.
    """
    rows = [dict(row) for row in (preset.get("directives") or [])]
    if not rows:
        rows = [dict(known[directive_id]["row"]) for directive_id in known]
    by_id = {row.get("id"): row for row in rows}
    for entry in entries:
        if entry.action != "apply":
            continue
        row = by_id.get(entry.ident)
        if row is None:
            row = dict(known[entry.ident]["row"])
            rows.append(row)
            by_id[entry.ident] = row
        row["enabled"] = entry.value
    body: dict[str, Any] = {
        "name": str(preset.get("name") or "default"),
        "modules": [dict(row) for row in (preset.get("modules") or [])],
        "flows": preset.get("flows") or {},
        "agent_bindings": preset.get("agent_bindings") or {},
        "surfaces": preset.get("surfaces") or {},
        "directives": rows,
    }
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


def _default_preset(client: httpx.Client, base_url: str) -> dict:
    """The persisted default preset row, seeding it from the factory if absent."""
    response = client.get("/api/prompts/presets/default")
    if response.status_code == 404:
        print("no persisted default row — seeding it from the factory image.")
        return _expect(client.post("/api/prompts/default/reset"))["preset"]
    return _expect(response)["preset"]


# -- presentation ------------------------------------------------------------


def _print_plan(entries: list[PlanEntry]) -> None:
    """Print the per-id migration plan as a small aligned table."""
    id_width = max(len(entry.ident) for entry in entries)
    reason_width = max(len(entry.reason) for entry in entries)
    print(
        f"| {'directive'.ljust(id_width)} | value | action | "
        f"{'basis'.ljust(reason_width)} |"
    )
    print(f"|{'-' * (id_width + 2)}|-------|--------|{'-' * (reason_width + 2)}|")
    for entry in entries:
        print(
            f"| {entry.ident.ljust(id_width)} | {str(entry.value).ljust(5)} | "
            f"{entry.action.ljust(6)} | {entry.reason.ljust(reason_width)} |"
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
    print(f"migrate directive toggles — {mode}")
    print(f"engine: {base_url}")
    print()

    known = catalog()
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        try:
            settings = _expect(client.get("/api/settings"))
        except httpx.HTTPError as exc:
            raise SystemExit(
                f"engine unreachable at {base_url} ({exc}); start it "
                "(systemctl --user start fabulist-engine) or pass --base-url"
            ) from None
        legacy = {
            str(key): value
            for key, value in (settings.get("legacy_toggles") or {}).items()
            if isinstance(value, bool)
        }
        current = {
            str(key): bool(definition.get("enabled"))
            for key, definition in (settings.get("toggles") or {}).items()
            if isinstance(definition, dict)
        }
        if not legacy:
            print("no legacy settings toggles — nothing to migrate (no-op).")
            return 0

        entries = build_plan(legacy, current, known)
        _print_plan(entries)
        print()
        to_apply = [entry for entry in entries if entry.action == "apply"]
        kept = [entry for entry in entries if entry.action == "keep"]
        for entry in kept:
            print(f"kept {entry.ident} — {entry.reason}")
        if not to_apply:
            print("nothing to apply — rerun is a no-op.")
            return 0
        if args.dry_run:
            print(
                f"would apply {len(to_apply)} of {len(entries)} legacy toggle(s) "
                f"({len(kept)} kept); nothing written."
            )
            return 0

        preset = _default_preset(client, base_url)
        updated = _expect(
            client.put(
                "/api/prompts/presets/default",
                json=apply_rows(preset, entries, known),
            )
        )["preset"]
        written = {
            row.get("id"): bool(row.get("enabled"))
            for row in (updated.get("directives") or [])
        }
        for entry in to_apply:
            if written.get(entry.ident) != entry.value:
                print(
                    f"VERIFY FAIL: {entry.ident} is {written.get(entry.ident)}, "
                    f"expected {entry.value}",
                    file=sys.stderr,
                )
                return 1
        print(
            f"applied {len(to_apply)} toggle(s): "
            f"{', '.join(entry.ident for entry in to_apply)}"
        )

    print(
        f"done: applied {len(to_apply)}, kept {len(kept)} "
        f"(of {len(entries)} legacy toggle(s))."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
