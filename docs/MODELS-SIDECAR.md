# Models sidecar

Operator reference for `fabulist-models`, the container the default compose file starts next to
the engine. You do not need this page to take a first turn — the README has the summary and the
download size. This is the detail behind it.

## The three models

One llama.cpp server, Vulkan build, in router mode.

| model id | route | role |
|---|---|---|
| `embeddinggemma-2` | `POST /v1/embeddings` | memory index and recall embeddings |
| `qwen3-reranker` | `POST /v1/rerank` | recall reranking (on by default) |
| `d1-3b` | `POST /v1/systemone` | the native decision model behind the yes/no checks |

Nothing ships in the image. The compose file names each model as a HuggingFace reference, and your
first request for one downloads it. The router starts with all three unloaded. The child it spawns
for a model fetches that model's files, then serves it. All three repos are public and ungated, so
nothing asks for a token.

## Sizes

| model id | weights | size |
|---|---|---|
| `d1-3b` | [`LiquidAI/d1-3B-GGUF`](https://huggingface.co/LiquidAI/d1-3B-GGUF) → `d1-3B-Q4_K_M.gguf` | 1.56 GiB |
| `embeddinggemma-2` | [`unsloth/embeddinggemma-2-GGUF`](https://huggingface.co/unsloth/embeddinggemma-2-GGUF) → `embeddinggemma-2-Q8_0.gguf` + `mmproj-Q8_0.gguf` | 295 MiB + 529 MiB |
| `qwen3-reranker` | [`Voodisss/Qwen3-Reranker-0.6B-GGUF-llama_cpp`](https://huggingface.co/Voodisss/Qwen3-Reranker-0.6B-GGUF-llama_cpp) → `Qwen3-Reranker-0.6B-Q4_K_M.gguf` | 378 MiB |

That is **~2.7 GiB, once**. On a fresh cache I measured 2.73 GiB: 1.56 GiB for d1, 0.30 GiB for the
embedder, 0.53 GiB for its projector and 0.38 GiB for the reranker.

## The cache

The pull lands in llama.cpp's HuggingFace cache. `LLAMA_CACHE=/cache` points there, and a compose
named volume backs it: `fabulist-model-cache`, or `<project>_fabulist-model-cache` per project.

The volume survives `docker compose down`, `up`, image pulls and recreates, so you pay for the
download once. The first request after a fresh volume is slow — a few minutes on a home connection.
Every request after that is a normal cold model load. To force a fresh pull, run
`docker compose down -v`, or `docker volume rm <project>_fabulist-model-cache`. Bind a host
directory at `/cache` instead if you would rather see the blobs.

llama.cpp picks the embedder's projector from the same repo and matches its quantisation to the
model file (`mmproj-Q8_0.gguf`). The d1 section turns that auto-pick **off**, because its repo ships
projectors the decision route never uses. Left on, it would drag in another 543 MiB.

## Why this reranker file

The reranker's repo was chosen for its **conversion**, not its popularity. A reranker GGUF has to
carry the sequence-classification head (`qwen3.classifier.output_labels`, `cls.output.weight`).
Several widely mirrored "Qwen3-Reranker" conversions drop it silently. The server then scores a
generative model's logits, ranks the wrong document first, and still looks healthy.

The pinned file is byte-identical to the artifact the maintainer's own deployment verified.
`deploy/sidecar/models.ini` names the trap and the measurement.

## The manifest

`deploy/sidecar/models.ini` is the manifest: one section per model id, an `hf-repo` plus an exact
`hf-file`, then the owner's serving flags.

At boot the entrypoint checks the **form** of every reference, and that the cache directory is
writable. It does not go looking for files that do not exist yet. A fetch that fails — network,
disk, a repo that turned gated — surfaces as llama.cpp's own error in
`docker compose logs fabulist-models`, naming the repo and the file.

## Offline, or your own weights

`./models` is mounted read-only at `/models`, which is the local-weights override. Put GGUFs there
and bind a preset that names them: replace a section's `hf-repo`/`hf-file` pair with
`model = /models/your-model.gguf` (plus `mmproj = ...`). You do not have to edit the repo files to
point at it:

```sh
# .env beside docker-compose.yml
FABULIST_MODELS_DIR=/data/models                          # host directory mounted at /models
FABULIST_SIDECAR_PRESET=./deploy/sidecar/models.local.ini  # host path of your preset
```

For those sections the boot check changes shape. A local `model` or `mmproj` path must exist. A
missing one is a single boot error naming the file, instead of a router that fails on the first
turn.

`HF_TOKEN` is passed through to the sidecar; llama.cpp reads it natively as its `--hf-token`
default. The three shipped refs do not need it, and an empty value sends no header.

## How the engine finds it

Inside the compose network the sidecar is `http://fabulist-models:8080/v1`. That is a service name,
so `FABULIST_HOST_ALIAS` never rewrites it. The compose file sets `FABULIST_SIDECAR_URL` to that
URL, and the engine **pins the roles at boot**: it asks the sidecar which model ids it serves,
creates `sidecar-embed`, `sidecar-rerank` and `sidecar-decision` through the normal connection
store, and binds each one. It binds only when that agent slot is **empty** and the model is served.

A role that already points somewhere is never touched, and the narrator is never touched at all. A
deleted pin stays deleted across reboots. A partial or unreachable sidecar pins what it can and logs
one notice; it does not fail the boot.

Two things keep the boot race from costing you the pins.

1. Compose starts the engine only after the sidecar reports **healthy** (`depends_on:
   fabulist-models: condition: service_healthy`). That healthcheck requests `/v1/models`, which the
   router answers as soon as it listens, with every model still `unloaded`. So it is a real "the
   router parsed its preset" check: it never loads a model, never downloads a weight, and never
   waits for a Vulkan model load.
2. If the probe is unreachable anyway — a sidecar restarted behind a running engine, the
   host-network variant, a first boot slower than the healthcheck's start period — the engine keeps
   re-probing on a bounded schedule. Every ~25 s, at most 24 attempts (about ten minutes), stopping
   at the first answer. Each failed attempt logs one notice. A spent budget logs one giving-up line.
   Boot never waits for any of it.

For another host, another port, or no compose, write the three rows yourself:

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

Or add them in the Connections drawer. The narrator stays a separate connection either way: the
sidecar serves the auxiliary roles, not the prose model.

## Device selection

Vulkan enumerates devices in its own order, so index 0 is not necessarily your first card. When more
than one GPU is visible the entrypoint refuses to guess. Set one of these in `.env`:

```sh
FABULIST_SIDECAR_DEVICE=Vulkan0              # or a bare 0
FABULIST_SIDECAR_DEVICE_MATCH="RTX 5090"     # or a name substring
```

A clone ships no `.env`. Copy `.env.example` and uncomment the one you need. One visible device needs
no setting. An id that does not exist is a boot failure that lists the devices. Zero Vulkan devices
falls back to CPU — the Vulkan build's own behaviour — and the log says so.

The image tag is pinned (`server-vulkan-b11804`). The d1 decision model is a new architecture, and
older images answer 501 on `/v1/systemone`.

## Verify it

```sh
uv run python scripts/sidecar_smoke.py
```

It checks the three model ids, one rerank call, one embedding call and one `d1-3b` decision call. On
the rerank it checks sorted scores and `truncated = 0` on every sequence; the reranker's 2048-token
context is only sufficient while the engine clips each candidate to 600 chars.

It reads the service log with `docker compose logs`, and you can watch a download with
`docker compose logs -f fabulist-models`. Use `--log-file` or `--log-cmd` for another topology. On a
fresh cache the first run also pulls ~2.7 GiB: the per-request timeout is 300 s, and `--timeout`
raises it on a slow line. A second run does no network work — same three assertions, no pull.

## Run without it

```sh
docker compose -f docker-compose.app-only.yml up -d
```

That is the engine alone, against your own host model server or a hosted API.

## Troubleshooting

If a model's first request fails with a download error, look at the weight cache first.
`docker compose exec fabulist-models du -sh /cache` shows what is already there. A half-finished blob
is re-fetched automatically, because llama.cpp stores partials by content hash.

If the container fails at create with `failed to fulfil mount request: ... libnvidia-egl-wayland.so.1.1.21: no such file`,
the NVIDIA CDI spec is stale after a driver update. Regenerate it:

```sh
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
docker compose up -d
```

On an AMD or Intel host, delete the `deploy:` block in `docker-compose.yml` — it requests an NVIDIA
device — and keep `devices: [/dev/dri]`.
