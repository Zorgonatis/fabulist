# Fabulist

This is an AI slop project, it will break.

## What it is

A personal roleplay narrative engine — a MUD with an LLM narrator. It takes the parts
SillyTavern-shaped clients emulate in prompt space (agent passes, out-of-band state tracking,
generative UI, swipes and branches) and gives them real runtimes: an append-only event log, a
LangGraph agent loop, and a component library the narrator composes panels from. Nothing is
hosted here: Fabulist is self-hosted and local-first — your sessions, your log, your keys, your
machine.

- **The event log owns everything.** One SQLite file is the source of truth for a session;
  transcript, state panels, memory index and every agent's context are projections of it.
  Corrections are revision events, replay is a swipe, a fork is a branch.
- **Generative UI panels.** The narrator emits prose plus statements from a taught component
  library (chat transcripts, phones, monitors, social posts, holograms, …), validated by the
  engine before they render. One escape hatch component accepts authored HTML behind a policy
  gate.
- **Out-of-band state tracking.** A dedicated tracker agent maintains an entity registry —
  places as a tree, items, threads, relationships, meters, conditions, money — with stable ids,
  plus a clock that the model may propose to and the engine validates. The narrator never emits
  state; it receives it as facts.
- **Forks, swipes, revisions.** Message edits are appended revisions, branches are sibling
  sessions, and a rejected variant costs nothing downstream.
- **Local RAG memory.** Per-scope vector collections (world / actor / chat) over any
  OpenAI-compatible embedding endpoint, with optional reranking and a re-embed job; the index is
  derived and rebuildable.
- **Voice input and attachments.** Optional speech-to-text input (faster-whisper, local) and
  image/video/audio attachments normalised with ffmpeg; a vision-capable narrator sees images
  directly.
- **Per-agent model routing.** Connections are typed (`completion` / `embedding` / `decision`)
  and resolved per call, so the narrator, the tracker, the critic and the embedder can each run
  on a different endpoint — a local llama.cpp server, vLLM, LM Studio, OpenRouter, anything
  OpenAI-compatible.
- **Editable prompt surface.** Prompt modules, directive switches and the turn topology are
  log-owned data edited in the UI, not code constants; a compiled preview shows exactly what
  the model will see.
- **Context management.** Per-turn window accounting against a declared context size, a
  low-context notice, and compaction that summarises an era of turns and continues on a fork,
  leaving the raw log intact.
- **Inspection.** A turn inspector, a per-task context ledger, per-agent usage rows and a
  graph view of the turn itself.

## 18+ · adult content notice

Fabulist is not a content filter: the engine's output is whatever the models you configure
produce. Some functions it ships with can encourage the generation of adult / 18+ content — the
NSFW Content directive (off by default), user presets and imported character cards among them.
Enabling and using them is the user's responsibility, as are the terms of any model provider
the engine is pointed at.

## Requirements

- Python **3.12** and [uv](https://docs.astral.sh/uv/)
- Node **>= 22.6** and npm (to build the web UI)
- At least one **OpenAI-compatible endpoint**: llama.cpp's `llama-server`, vLLM, LM Studio,
  Ollama's OpenAI shim, OpenRouter, or any hosted API. A narrator wants a large, capable model;
  extraction and judging are happy on something small.
- **ffmpeg** (+ `ffprobe`) if you want to attach audio or video
- Linux, macOS or WSL; no GPU is required by the engine itself — the model server owns the
  hardware question

## Quickstart

### Docker (primary)

Each release ships a `Dockerfile` and a `docker-compose.yml` next to the source, and the
image carries the engine **and** the built UI — no Node, Python or uv needed on the host. Unpack
the release and create the host-side mount directories **as your own user** before the first
boot. The image runs as uid 1000, and Docker creates a missing bind-mount directory root-owned,
which makes the engine die with `sqlite3.OperationalError: unable to open database file`:

```sh
mkdir -p data configs
docker compose up -d
```

That is the whole first run: with an empty `./configs` the engine boots and tells you to create a
connection in the Connections drawer (or over the API) — a `connections.json` is not required
up front, but a **completion** connection is: the sidecar serves only embedding, reranking and
the decision model, so nothing writes prose until you point an agent at an OpenAI-compatible
endpoint. The engine and the built UI are served together at <http://localhost:2727>. `./data`
holds the event log, `./configs` holds your connections — both are active bind mounts in the
compose file (`./data:/app/data`, `./configs:/app/configs`), both must exist first, and both
survive a rebuild. A `.env` beside the compose file is passed in as container environment —
optional, and where cloud API keys belong; `.env.example` is the commented template. Compose
only builds when the `fabulist:latest` tag is absent, so run `docker compose build fabulist`
explicitly if an image from an earlier attempt is already there. Voice input is not installed in
the image by default; the compose file shows the one-line build arg that adds it.

### Models sidecar

The default compose file also starts **`fabulist-models`**: one llama.cpp server (Vulkan build,
router mode) serving the three models the engine's memory and decision paths use:

| model id | route | role |
|---|---|---|
| `embeddinggemma-2` | `POST /v1/embeddings` | memory index + recall embeddings |
| `qwen3-reranker` | `POST /v1/rerank` | recall reranking (on by default) |
| `d1-3b` | `POST /v1/systemone` | native decision model |

The weights are **not in the image** and there is no download step: the compose file names
them as HuggingFace references, and **your first request for a model downloads it**. The router
starts with all three unloaded; the child it spawns for a model fetches that model's files, then
serves it. All three repos are public and ungated, so nothing asks for a token.

| model id | weights (HuggingFace) | size |
|---|---|---|
| `d1-3b` | [`LiquidAI/d1-3B-GGUF`](https://huggingface.co/LiquidAI/d1-3B-GGUF) → `d1-3B-Q4_K_M.gguf` | 1.56 GiB |
| `embeddinggemma-2` | [`unsloth/embeddinggemma-2-GGUF`](https://huggingface.co/unsloth/embeddinggemma-2-GGUF) → `embeddinggemma-2-Q8_0.gguf` + `mmproj-Q8_0.gguf` | 295 MiB + 529 MiB |
| `qwen3-reranker` | [`Voodisss/Qwen3-Reranker-0.6B-GGUF-llama_cpp`](https://huggingface.co/Voodisss/Qwen3-Reranker-0.6B-GGUF-llama_cpp) → `Qwen3-Reranker-0.6B-Q4_K_M.gguf` | 378 MiB |

**~2.7 GiB, once** — 2.73 GiB measured on a fresh cache (1.56 GiB d1 + 0.30 GiB embedder +
0.53 GiB projector + 0.38 GiB reranker). The pull lands in llama.cpp's HuggingFace cache —
`LLAMA_CACHE=/cache`, backed by the compose named volume `fabulist-model-cache` (per-project
name: `<project>_fabulist-model-cache`). It persists across `docker compose down`, `up`, image
pulls and recreates, so the download is paid exactly once; the first request after a fresh
volume is slow (a few minutes on a home connection), every later one is a normal cold model
load. `docker compose down -v` (or `docker volume rm <project>_fabulist-model-cache`) is how you
force a fresh pull. Bind a host directory at `/cache` instead if you would rather see the
blobs. The embedder's projector is picked by llama.cpp from the same repo, quant-matched to the
model file (`mmproj-Q8_0.gguf`); the d1 section turns that auto-pick **off** because its repo
ships projectors the decision route never uses (543 MiB it would otherwise drag in).

The reranker's repo is chosen for its **conversion**, not its popularity: a reranker GGUF must
carry the sequence-classification head (`qwen3.classifier.output_labels`, `cls.output.weight`),
and several widely mirrored "Qwen3-Reranker" conversions silently drop it — the server then
scores a generative model's logits and ranks the wrong document first while still looking
healthy. The pinned file is byte-identical to the artifact the maintainer's own deployment had
verified; `deploy/sidecar/models.ini` names the trap and the measurement.

`deploy/sidecar/models.ini` is the manifest: one section per model id, `hf-repo` + an exact
`hf-file`, then the owner's serving flags. The entrypoint validates the **form** of every
reference at boot (and that the cache directory is writable) rather than looking for files that
do not exist yet; a fetch that fails — network, disk, a repo that turned gated — surfaces as
llama.cpp's own error in `docker compose logs fabulist-models`, naming repo and file.

**Offline / your own weights.** `./models` is still mounted read-only at `/models` as the
local-weights override: put GGUFs there and bind a preset that names them, i.e. replace a
section's `hf-repo`/`hf-file` pair with `model = /models/your-model.gguf` (+ `mmproj = ...`).
Point at it without editing the repo files:

```sh
# .env beside docker-compose.yml
FABULIST_MODELS_DIR=/data/models          # host directory mounted at /models
FABULIST_SIDECAR_PRESET=./deploy/sidecar/models.local.ini   # host path of your preset
```

The entrypoint check changes shape for those sections: a local `model`/`mmproj` path must
exist, and a missing one is one boot error naming the file instead of a router that fails on the
first turn. `HF_TOKEN` is passed through to the sidecar (llama.cpp reads it natively, as the
`--hf-token` default) but is not needed for the three shipped refs; it sends no header when
empty.

Point the engine at it. Inside the compose network the sidecar is
`http://fabulist-models:8080/v1` — a service name, so `FABULIST_HOST_ALIAS` never rewrites it.
The compose file sets `FABULIST_SIDECAR_URL` to that URL, so **the engine pins the roles for
you at boot**: it asks the sidecar which model ids it serves, creates
`sidecar-embed`/`sidecar-rerank`/`sidecar-decision` through the normal connection store, and
binds each one — but only when the agent slot is **empty** and the model is served. A role that
already points somewhere is never touched (and the narrator is never touched at all), deleting a
pin stays deleted across reboots, and a partial or unreachable sidecar pins what it can and logs
one notice instead of failing the boot.

Two separate things keep the boot race from costing you the pins. First, the compose file
starts the engine only after the sidecar reports **healthy** (`depends_on: fabulist-models:
condition: service_healthy`). That healthcheck requests `/v1/models`, which the router answers
as soon as it listens — with every model `unloaded` — so it is a real "the router parsed its
preset" check that never loads a model, never downloads a weight, and never waits for a Vulkan
model load. Second, if the probe is unreachable anyway (a sidecar restarted behind a running
engine, the host-network variant, or a first boot slower than the healthcheck's start period),
the engine keeps re-probing on a **bounded repeating schedule**: every ~25 s, at most 24
attempts (about ten minutes), stopping at the first probe that answers. Each failed attempt
logs one notice, a spent budget logs one final giving-up line, and boot never waits for any of
it. The snippet below is the manual fallback for a custom setup (another host, a different
port, no compose).

Three connections, one per role:

```json
{
  "connections": {
    "sidecar-embed":    { "base_url": "http://fabulist-models:8080/v1", "model": "embeddinggemma-2", "timeout": 120 },
    "sidecar-rerank":   { "base_url": "http://fabulist-models:8080/v1", "model": "qwen3-reranker",   "timeout": 120 },
    "sidecar-decision": { "base_url": "http://fabulist-models:8080/v1", "model": "d1-3b",            "timeout": 120 }
  },
  "agents": {
    "embedder": "sidecar-embed",
    "reranker": "sidecar-rerank",
    "decision": "sidecar-decision",
    "narrator": "your-completion-connection"
  }
}
```

Or add them in the Connections drawer. The narrator is still a separate connection: the
sidecar serves the auxiliary roles, not the prose model.

**Device selection.** Vulkan enumerates devices in its own order (index 0 is not necessarily
your first card), so the entrypoint refuses to guess when more than one GPU is visible. Set one
of these in `.env`:

```sh
FABULIST_SIDECAR_DEVICE=Vulkan0              # or a bare 0
FABULIST_SIDECAR_DEVICE_MATCH="RTX 5090"     # or a name substring
```

A clone ships no `.env`; copy `.env.example` and uncomment the one you need. One visible device
needs no setting. An id that does not exist is a boot failure that lists
the devices; zero Vulkan devices falls back to CPU — the Vulkan build's own behaviour, and the
log says so. The image tag is pinned (`server-vulkan-b11804`): the d1 decision model is a new
architecture, and older images answer 501 on `/v1/systemone`.

**Verify it** after `docker compose up -d`:

```sh
uv run python scripts/sidecar_smoke.py
```

It checks the three model ids, one rerank call (sorted scores **and** `truncated = 0` on every
sequence — the reranker's 2048-token context is sufficient only while the engine clips each
candidate to 600 chars), one embedding call, and one `d1-3b` decision call. It reads the
service log with `docker compose logs`; use `--log-file` or `--log-cmd` for another topology.
On a fresh cache the first run also pulls ~2.7 GiB: the smoke's per-request timeout is 300 s
(raise it with `--timeout` on a slow line), and `docker compose logs -f fabulist-models` shows
the download. A second run does no network work — same three assertions, no pull.

**No sidecar.** To run the engine alone (your own host model server, a hosted API):

```sh
docker compose -f docker-compose.app-only.yml up -d
```

**Troubleshooting.** If a model's first request fails with a download error, the weight cache is
the place to look: `docker compose exec fabulist-models du -sh /cache` shows what is already
there, and a half-finished blob is re-fetched automatically (llama.cpp stores partials by
content hash). If the sidecar container fails at create with
`failed to fulfil mount request: ... libnvidia-egl-wayland.so.1.1.21: no such file`, the NVIDIA
CDI spec is stale after a driver update — regenerate it with
`sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml` and `docker compose up -d` again.
On an AMD/Intel host, delete the `deploy:` block in `docker-compose.yml` (it requests an NVIDIA
device) and keep `devices: [/dev/dri]`.

### From source

```sh
cp configs/connections.example.json configs/connections.json
$EDITOR configs/connections.json          # your endpoints, model ids, and the agents map
uv sync
(cd frontend && npm install && npm run build)
uv run python scripts/serve.py            # engine on :2727, log in data/
```

The engine serves the built UI at <http://localhost:2727>: one origin for the app and the API,
no CORS setup needed. To work on the UI instead, run `cd frontend && npm run dev` and open
<http://localhost:5173> — see [frontend/README.md](frontend/README.md).

### No model at all

```sh
cd frontend && VITE_MOCK=1 npm run dev
```

Every API call is answered from an in-memory mock, so the whole UI is explorable with no engine
and no model. A red `MOCK` badge shows while it is active.

## Configuration

`configs/connections.json` (gitignored, user-owned) has two sections — endpoints, and which
agent uses which endpoint:

```json
{
  "connections": {
    "local": {
      "base_url": "http://localhost:8080/v1",
      "model": "your-model-id",
      "sampling": { "temperature": 0.9, "top_p": 0.95, "max_tokens": 2048 },
      "timeout": 300
    },
    "hosted": {
      "base_url": "https://openrouter.ai/api/v1",
      "model": "vendor/model-name",
      "api_key_env": "OPENROUTER_KEY",
      "timeout": 300
    }
  },
  "agents": { "narrator": "hosted", "extractor": "local" }
}
```

- `api_key_env` names an **environment variable**, never the key itself. Omit it for keyless
  local servers.
- A `base_url` may name a **cloud** endpoint (`https://…`), a **host-local** server
  (`http://127.0.0.1:4445`, `localhost`, `[::1]`), or a **compose service** name
  (`http://fabulist-models:8080`, the sidecar the default compose file ships). In the Docker image the
  compose file sets `extra_hosts: host.docker.internal:host-gateway` and
  `FABULIST_HOST_ALIAS=host.docker.internal`, so a loopback host is rewritten to
  the host machine — inside a container `127.0.0.1` is the container itself.
  Remote hosts and LAN IPs are never rewritten; an **empty** `FABULIST_HOST_ALIAS`
  disables the rewrite (the URL is dialled exactly as written). `GET
  /api/connections/doctor` probes every configured connection and reports the
  resolved URL, reachability and latency.
- Keys can also be pasted in the Connections drawer: the engine writes `FABULIST_<NAME>_KEY`
  into the project `.env` (created `0600`, gitignored) by rewriting only that line, and applies
  it on the next call with no restart. The key is never echoed back by the API.
- Declare each connection's **context size** in the drawer (`context_window`). It is never
  probed; budget warnings, prompt headroom and the compaction threshold are computed from it,
  so the declared number is the one the engine believes. DeepSeek's `GET /v1/models` returns a
  `context_window` you can copy; z.ai returns none, and `200000` for `glm-5.3` is a community
  guess rather than a published figure.
- `FABULIST_DEV_ORIGINS=http://localhost:5173,…` adds extra CORS origins when you serve the UI
  from a different origin than the engine's defaults.
- Everything user-specific stays out of git: `configs/connections.json`, `data/`, `.env`.

### Agent slots

`GET /api/connections` returns one row per agent slot (the `slots` array the Connections drawer
renders) — eleven in the default sidecar setup, each bound to one connection:

| slot | what it feeds |
|---|---|
| `completion` | type default: any completion task or item with no more specific binding |
| `completion.narration` | the narrator pass — prose + generative-UI DSL |
| `completion.extraction` | structured pulls: critic, proposals, summaries, captions |
| `completion.state` | the state tracker's entity-registry writes |
| `summarizer` | compaction and era summaries (falls back to `completion.extraction`) |
| `captioner` | attachment and image captions (falls back to `completion.extraction`) |
| `imagegen` | image-generation requests |
| `embedding` | type default for embedding slots (an authored `embedder` wins) |
| `embedder` | the live memory embedder |
| `decision` | the native decision model (`POST /v1/systemone`) |
| `reranker` | recall reranking (per-item key; the sidecar auto-pin writes it) |

`narrator` is a legacy alias: it normalises to `completion.narration`, so either spelling binds
the same slot and the typed spelling wins if both are authored (`extractor` likewise normalises
to `completion.extraction`). An empty slot falls through item → task → type and only fails at
call time if nothing resolves.

Known issue #45: `glm-5.3` can echo the engine's own `<preset_def>` wrapper into committed prose
— bind narration to another connection until that is fixed.

## API

The API is served on the same origin as the UI; the OpenAPI document is at `/docs`.

### Turns are an SSE stream

`POST /api/sessions/{id}/turns` answers `text/event-stream` — there is no JSON mode and no
`Accept` negotiation, so a plain client gets the stream too. Frames are `id:` / `event:` /
`data:` lines, `data` carries the JSON delta, and `: ping` comment lines keep an idle stream
warm. A failure *inside* the turn is not an HTTP status: the response is already 200, so it
arrives as an `error` event in the stream (`"generation truncated: max_tokens reached before
natural stop"` is one) and the stream closes when the turn's work is done.

**`max_tokens` × thinking models.** A thinking model spends the same `max_tokens` budget on
reasoning before it writes anything, so a small budget can be consumed entirely by
`reasoning_content` — the stream ends with only that transient `error` event and no prose. This
is the most common silent-looking failure on a new connection (see the #45/#48-era field
reports). The built-in default is `max_tokens: 1024`: set it explicitly for a thinking narrator
(`8192` or more), or lower the model's reasoning depth (`reasoning_effort: "low"`, the z.ai/GLM
knob). A non-thinking small model is fine at 2048.

Known issue #46: a client that disconnects mid-stream can leave a committed turn with an empty
narrative payload — empty prose, empty DSL — and the turn is kept.

### Exposure

There is **no authentication** on any route. The image starts the engine with `--host 0.0.0.0`
and the default compose file publishes `2727:2727`, so the app listens on every host interface.
On a machine with a public interface, either publish loopback only (`"127.0.0.1:2727:2727"`) or
put a reverse proxy with its own authentication in front of it. `scripts/serve.py` run from
source defaults to `127.0.0.1:2727`.

## Architecture

A turn is a compiled graph. Pre-turn nodes assemble context (persona and cast, world
definition, recalled memory, plan, runtime notices) and inject state as facts at the end of the
prompt; the **narrator pass emits prose and generative-UI DSL and nothing else**. At `commit`
the turn is kept and the user is released; the remaining nodes — validation and repair, a
quality gate and critic, the planner on its cadence, the state tracker, the environment HUD,
attribution — run behind them and stream their landings back into the transcript.

Everything that lands is an appended event: `user`, `narrative`, `revision`, `state-delta`,
`roll`, `asset`, `summary`, `ooc`, `system`. Only the dispatcher writes, and every read — the
transcript, the state sidebar, the memory index, the next prompt — is a projection over those
rows. Derived stores (vector index, LangGraph checkpoints) are cache and may be dropped or
rebuilt at any time; on divergence the log wins. `docs/decisions/README.md` records the rulings
that keep that shape honest.

## Docs

- [docs/decisions/README.md](docs/decisions/README.md) — the design rulings, one entry each
- [AGENTS.md](AGENTS.md) — how to work in this repo (conventions, verification rules)
- [.env.example](.env.example) — the sidecar and API-key environment variables
- [docs/PERSONA-SPEC.md](docs/PERSONA-SPEC.md) — persona/actor cards and structured canon
- [docs/PROMPT-SURFACES.md](docs/PROMPT-SURFACES.md) — prompt modules, presets and the compiler
- [docs/UI-LANGUAGE.md](docs/UI-LANGUAGE.md) — component flavours and panel language
- [docs/COMPONENT-PATTERNS.md](docs/COMPONENT-PATTERNS.md) — how to build a component
- [docs/COMPONENT-SETS.md](docs/COMPONENT-SETS.md) — the frozen `technology` set (v1.0.0) and the reusable component core

## License

Apache-2.0.
