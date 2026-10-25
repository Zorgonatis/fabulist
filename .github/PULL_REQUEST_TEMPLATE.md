## What and why

<!-- What changed, and the problem it solves. The "why" matters more than the diff summary. -->

## Scope

<!-- One or two lines. Separate mechanical sweeps from behavior changes; call out anything
     another reviewer would trip over (regenerated artifacts, renamed surfaces, migrations). -->

## Checklist

- [ ] `uv run pytest -q` — green
- [ ] `cd frontend && npm run check` — svelte-check 0 errors, 0 warnings
- [ ] `cd frontend && npm run build` — green
- [ ] `cd frontend && node scripts/generate-library-spec.mjs --check` — green (run it
      explicitly: the pytest oracle skips when `node_modules` is absent)
- [ ] `uv lock --check` — green, if dependencies were touched (dependency changes are
      maintainer-owned; ask first)
- [ ] No changes under `data/` or to `configs/connections.json`; no `.env`, keys, session
      exports or absolute local paths in the diff
- [ ] Backend tests added/updated for behavior changes; no test depends on a live model or the
      network

## Notes for the reviewer

<!-- Anything unverified, anything deliberately left out, anything that needs a live gate. -->
