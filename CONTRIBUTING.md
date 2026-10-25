# Contributing to Fabulist

Thanks for looking. Fabulist is a personal project published in the open: it is
solo-maintained, so **the roadmap is maintainer-driven** — direction, waves and the decision
log (`docs/decisions/`, pointed at by `docs/DECISIONS.md`) are set by the maintainer. That is
compatible with contributions: bug reports, fixes, docs corrections and focused improvements
are welcome, and a good patch that fits the current direction lands quickly.

Read `AGENTS.md` before writing code. It is the operating manual for this repo (commands,
non-negotiables, and the gotchas that have already bitten us once). `README.md` covers layout
and quickstart.

## Development setup

Requirements: **Python 3.12** with [uv](https://docs.astral.sh/uv/), **Node >= 22.6** (the
schema generator type-strips TypeScript natively; older Node fails), and `ffmpeg` only if you
care about the optional STT input channel.

```sh
uv sync                                  # backend deps from uv.lock
cd frontend && npm ci                    # frontend deps from package-lock.json
```

Run the engine and UI as described in `README.md`. Model endpoints are user state: copy
`configs/connections.example.json` to `configs/connections.json` and point it at your own
OpenAI-compatible endpoints. Never commit that file, `.env`, or anything under `data/`.

## Test doctrine

**The test suite never calls a live model.** Every seam is exercised through
`MockTransport`; the suite is hermetic and fast, and that is a hard property — a PR that makes
a test depend on a running model endpoint (or on the network) will be rejected.

Live verification — a real model behind `configs/connections.json`, a real browser — is
**manual and reserved to the maintainer's own gates.** It is not part of what a PR is expected
to provide, and "it works against my local model" is not a substitute for a hermetic test.
Write the hermetic test; the maintainer runs the live gate before merging.

Generated artifacts are never hand-edited: `prompts/generated/*` comes from
`frontend/scripts/generate-library-spec.mjs`, so change the component/schema and regenerate.

## Pull request checklist

Run these from a clean tree and paste the results (or say "green") in the PR description:

- [ ] `uv run pytest -q` — green (backend suite)
- [ ] `cd frontend && npm run check` — svelte-check reports **0 errors, 0 warnings**
- [ ] `cd frontend && npm run build` — green
- [ ] `cd frontend && node scripts/generate-library-spec.mjs --check` — green (generated
      artifacts in sync; the pytest oracle silently skips this when `node_modules` is absent,
      so run it explicitly)
- [ ] `uv lock --check` — green, if you touched dependencies (dependency changes are
      maintainer-owned; ask first)
- [ ] No changes under `data/` or to `configs/connections.json`; no `.env`, keys, session
      exports or absolute local paths in the diff

Keep the PR small enough to review in one sitting, describe the *why*, and separate mechanical
sweeps from behavior changes. New surfaces have to earn themselves — see the non-goals in
`PLAN.md` §2 before adding one.

## Conduct

Be civil and argue about the code, not the person; anything else is not worth maintainer time.
