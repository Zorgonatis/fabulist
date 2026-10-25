#!/usr/bin/env python
"""Clear the persisted default preset's baked ``surfaces.directives`` map.

The ``directives`` surface now INHERITS from the code catalog
(``fabulist.prompts.parts.DEFAULT_DIRECTIVES``): per the surfaces schema,
absent/empty means "fall back to the constant", so a ``parts.py`` edit reaches
every live DB with no artifact churn and no owner PUT — the same principle D35
established for ``library_doc`` (the seed carries the expression, not text).
The factory seed and the committed artifact no longer bake directive copies;
only a USER-EDITED directive should ever appear as a literal override.

This one-time migration moves a LIVE default preset row to that contract by
clearing its baked ``surfaces.directives`` map over HTTP.

Why a baseline gate (not "clear everything")
--------------------------------------------
A stale factory copy and a genuine owner edit are indistinguishable *from the
current code catalog* (both differ). They ARE distinguishable *from the
pre-migration factory image*: the stale copies byte-match it, an owner edit
does not. So the default run clears only ids that are provably factory copies:

  * CLEAR an id whose content byte-matches the pre-migration artifact's
    ``surfaces.directives[id]`` (a factory copy, possibly a stale one), or
    whose content equals the CURRENT code-catalog entry (a no-op clear);
  * KEEP anything else and report it as a possible owner edit.

``--force`` clears every id (still printing the kept/cleared plan).

Baseline resolution
-------------------
The pre-migration image is ``git show HEAD:prompts/factory-defaults.json``
(run at the repo root); ``--baseline <preset-json-path>`` names a preset-shaped
JSON explicitly instead. If neither is available the script HALTS — it never
silently falls through to a clear-all.

HTTP-first, API-only
--------------------
Like ``scripts/seed_prompt_defaults.py`` this never touches ``data/`` or the
SQLite log directly (gitignored user state owned by the running engine). It:

  1. ``GET /api/prompts/presets/default`` (404 = no persisted row -> nothing
     to migrate; a fresh boot seeds the factory image, which already inherits);
  2. classifies every id in ``surfaces.directives`` against the baseline;
  3. ``PUT /api/prompts/presets/default`` with the FULL preset, every field
     preserved verbatim except the cleared directive ids (the store is a
     full replace; the default row's ``is_default``/``factory_version`` are
     preserved server-side).

An absent/empty ``surfaces.directives`` is a no-op ("already inherits") —
even with ``--force``. ``--dry-run`` prints the per-id plan and writes
nothing, so a rerun after apply is a true no-op.

Usage::

    uv run python scripts/migrate_directive_surfaces.py --dry-run  # plan only
    uv run python scripts/migrate_directive_surfaces.py            # apply
    uv run python scripts/migrate_directive_surfaces.py --force    # clear all

Base URL defaults to ``$FABULIST_ENGINE_URL`` or ``http://localhost:2727``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

#: Default engine base URL (overridable with --base-url or the env var).
DEFAULT_BASE_URL = os.environ.get("FABULIST_ENGINE_URL", "http://localhost:2727")

#: Repo-root-relative path of the committed factory image (the baseline source).
ARTIFACT_RELPATH = "prompts/factory-defaults.json"


def repo_root() -> Path:
    """Repository root, derived from this file's location (``scripts/``)."""
    return Path(__file__).resolve().parents[1]


# -- tolerant readers --------------------------------------------------------


def directive_map(payload: Any) -> dict[str, str]:
    """The ``surfaces.directives`` map of a preset-shaped payload (empty if absent)."""
    if not isinstance(payload, Mapping):
        return {}
    surfaces = payload.get("surfaces")
    if not isinstance(surfaces, Mapping):
        return {}
    directives = surfaces.get("directives")
    if not isinstance(directives, Mapping):
        return {}
    return {
        str(key): value for key, value in directives.items() if isinstance(value, str)
    }


def code_catalog() -> dict[str, str]:
    """The CURRENT code catalog (``parts.DEFAULT_DIRECTIVES``) as ``{id: content}``."""
    # Lazy like seed_prompt_defaults: keeps the script importable without the
    # package (e.g. --help) and mirrors the engine's own import site.
    from fabulist.prompts.parts import DEFAULT_DIRECTIVES

    return {directive.id: directive.content for directive in DEFAULT_DIRECTIVES}


def load_baseline(explicit: Path | None) -> dict[str, str]:
    """The pre-migration ``surfaces.directives`` map; halt when unobtainable.

    Resolution order: an explicit ``--baseline`` preset-shaped JSON file, else
    ``git show HEAD:<ARTIFACT_RELPATH>`` at the repo root. Neither silently
    falls through to clear-all: an unreadable baseline is a hard stop with the
    manual escape hatch spelled out.
    """
    hint = (
        "extract one manually (git show HEAD:"
        f"{ARTIFACT_RELPATH} > /tmp/factory-baseline.json) and pass "
        "--baseline /tmp/factory-baseline.json"
    )
    if explicit is not None:
        try:
            raw = json.loads(explicit.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"cannot read --baseline {explicit}: {exc}") from None
        if not isinstance(raw, Mapping):
            raise SystemExit(f"--baseline {explicit} is not a preset-shaped JSON object")
        return directive_map(raw)

    proc = subprocess.run(
        ["git", "show", f"HEAD:{ARTIFACT_RELPATH}"],
        capture_output=True,
        text=True,
        cwd=repo_root(),
    )
    if proc.returncode != 0:
        raise SystemExit(
            "cannot read the pre-migration factory image "
            f"(git show HEAD:{ARTIFACT_RELPATH}: {proc.stderr.strip() or 'failed'}); "
            f"{hint}"
        )
    try:
        raw = json.loads(proc.stdout)
    except ValueError as exc:
        raise SystemExit(
            f"HEAD:{ARTIFACT_RELPATH} is not valid JSON ({exc}); {hint}"
        ) from None
    if not isinstance(raw, Mapping):
        raise SystemExit(f"HEAD:{ARTIFACT_RELPATH} is not a JSON object; {hint}")
    return directive_map(raw)


# -- plan --------------------------------------------------------------------


@dataclass
class PlanEntry:
    """One directive id's migration decision (rendered in the plan table)."""

    ident: str
    action: str  # clear | keep
    reason: str


def classify(
    ident: str, content: str, baseline: dict[str, str], catalog: dict[str, str], *, force: bool
) -> PlanEntry:
    """Decide one directive id: factory copy -> clear, possible edit -> keep."""
    if force:
        return PlanEntry(ident, "clear", "--force")
    if ident in baseline and content == baseline[ident]:
        return PlanEntry(ident, "clear", "factory copy (matches pre-migration image)")
    if ident in catalog and content == catalog[ident]:
        return PlanEntry(ident, "clear", "matches current code catalog (no-op)")
    return PlanEntry(ident, "keep", "differs from the factory baseline (possible owner edit)")


def build_plan(
    live: dict[str, str], baseline: dict[str, str], catalog: dict[str, str], *, force: bool
) -> list[PlanEntry]:
    """Classify every id in the live ``surfaces.directives`` map, in its own order."""
    return [
        classify(ident, content, baseline, catalog, force=force)
        for ident, content in live.items()
    ]


def put_body(preset: dict, live: dict[str, str], entries: list[PlanEntry]) -> dict:
    """Build the ``PUT /api/prompts/presets/default`` full-replace payload.

    Every preset field rides through verbatim except the cleared directive ids;
    the remaining ``surfaces`` keys (library_doc / recalled / regexes / history)
    and any kept directives are untouched. When every id clears, the
    ``directives`` key is dropped entirely (absent == inherit, per the schema).
    """
    cleared = {entry.ident for entry in entries if entry.action == "clear"}
    directives = {
        ident: content for ident, content in live.items() if ident not in cleared
    }
    surfaces = dict(preset.get("surfaces") or {})
    if directives:
        surfaces["directives"] = directives
    else:
        surfaces.pop("directives", None)
    return {
        "name": str(preset.get("name") or "default"),
        "modules": preset.get("modules") or [],
        "flows": preset.get("flows") or {},
        "toggles": preset.get("toggles") or {},
        "prompt_overrides": preset.get("prompt_overrides") or {},
        "agent_bindings": preset.get("agent_bindings") or {},
        "surfaces": surfaces,
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


def _get_default_preset(client: httpx.Client, base_url: str) -> dict:
    """Fetch the persisted default preset row (``{"preset": ...}`` unwrapped)."""
    try:
        response = client.get("/api/prompts/presets/default")
    except httpx.HTTPError as exc:
        raise SystemExit(
            f"engine unreachable at {base_url} ({exc}); start it "
            "(systemctl --user start fabulist-engine) or pass --base-url"
        ) from None
    if response.status_code == 404:
        raise SystemExit(
            "no persisted default preset row at "
            f"{base_url} — nothing to migrate (a fresh boot seeds the factory "
            "image, which already inherits)"
        )
    return _expect(response)["preset"]


# -- presentation ------------------------------------------------------------


def _print_plan(entries: list[PlanEntry], baseline: dict[str, str]) -> None:
    """Print the per-id migration plan as a small aligned table."""
    id_width = max(len(entry.ident) for entry in entries)
    reason_width = max(len(entry.reason) for entry in entries)
    print(f"| {'directive'.ljust(id_width)} | action | {'basis'.ljust(reason_width)} |")
    print(f"|{'-' * (id_width + 2)}|--------|{'-' * (reason_width + 2)}|")
    for entry in entries:
        print(
            f"| {entry.ident.ljust(id_width)} | {entry.action.ljust(6)} | "
            f"{entry.reason.ljust(reason_width)} |"
        )
    if not baseline:
        print(
            "note: the factory baseline carries NO directives block — only "
            "current-catalog matches can clear without --force."
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
        help="print the per-id plan and write nothing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="clear every directive id, including possible owner edits",
    )
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        metavar="PRESET_JSON",
        help=(
            "preset-shaped JSON to use as the pre-migration factory baseline "
            f"(default: git show HEAD:{ARTIFACT_RELPATH})"
        ),
    )
    args = parser.parse_args(argv)
    base_url = args.base_url.rstrip("/")

    mode = "DRY RUN (no writes)" if args.dry_run else "APPLY"
    print(f"migrate directive surfaces — {mode}")
    print(f"engine: {base_url}")
    print()

    baseline = load_baseline(args.baseline)
    catalog = code_catalog()

    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        preset = _get_default_preset(client, base_url)
        live = directive_map(preset)
        if not live:
            print("already inherits: surfaces.directives is absent/empty — no-op.")
            return 0

        entries = build_plan(live, baseline, catalog, force=args.force)
        _print_plan(entries, baseline)
        print()
        to_clear = [entry for entry in entries if entry.action == "clear"]
        kept = [entry for entry in entries if entry.action == "keep"]
        # --force never keeps anything, so a kept entry always implies "no force".
        for entry in kept:
            print(f"kept {entry.ident} — {entry.reason}; rerun with --force to clear")

        if not to_clear:
            print("nothing to clear — rerun is a no-op.")
            return 0
        if args.dry_run:
            print(
                f"would clear {len(to_clear)} of {len(entries)} directive id(s) "
                f"({len(kept)} kept); nothing written."
            )
            return 0

        updated = _expect(
            client.put(
                "/api/prompts/presets/default", json=put_body(preset, live, entries)
            )
        )["preset"]
        print(f"cleared {len(to_clear)} directive id(s): {', '.join(e.ident for e in to_clear)}")

        remaining = directive_map(updated)
        if set(remaining) != {entry.ident for entry in kept}:
            print(
                "VERIFY FAIL: remaining directives "
                f"{sorted(remaining)} != kept ids {sorted(e.ident for e in kept)}",
                file=sys.stderr,
            )
            return 1
        print(
            "verified: the default preset now inherits directive text from the "
            "code catalog"
            if not kept
            else f"verified: only the kept id(s) remain as literal overrides: {', '.join(sorted(remaining))}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
