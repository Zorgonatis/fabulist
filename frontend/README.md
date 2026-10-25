# Fabulist frontend

SvelteKit 3 / Svelte 5 chat UI for the Fabulist engine.

- **Production shape: one origin.** `npm run build` produces the client build; the engine
  serves it alongside its API, so the app and the API share an origin at <http://localhost:2727>
  and there is no CORS or `API_BASE` configuration to do. Build before you start the engine
  (`uv run python scripts/serve.py` from the repo root); it is useless without the API behind it.
- **Development:** `npm run dev` serves the app on :5173 with HMR and talks to the engine on
  :2727. That split is the only case where `VITE_API_BASE` matters — set it when the engine is
  not at the default `http://localhost:2727`, and add the dev origin to the engine's
  `FABULIST_DEV_ORIGINS` (CORS allows :5173 plus whatever that variable lists).
- **Scripted no-backend demo:** `VITE_MOCK=1 npm run dev` — every API call is answered from an
  in-memory mock; a red `MOCK` badge shows while it is active.
- **Checks:** `npm run check` (svelte-check) and `npm run build`.
- **Component vocabulary:** the openui component library under `src/lib/openui/` is the single
  source for what the narrator may emit — the engine's DSL validator reads artifacts generated
  from it (`node scripts/generate-library-spec.mjs`, `--check`-gated). Edit a component or its
  schema, then regenerate; never hand-edit `prompts/generated/*`.
