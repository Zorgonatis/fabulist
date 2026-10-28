# Configuration

Reference for `configs/connections.json` and for the agent slots `GET /api/connections` returns.
The README has the shape and the JSON. This page has the rules behind each field.

## Two sections

Endpoints, and which agent uses which endpoint.

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

- `api_key_env` names an **environment variable**, never the key itself. Omit it for keyless local
  servers.
- Everything user-specific stays out of git: `configs/connections.json`, `data/`, `.env`.

## Where a base_url may point

A `base_url` can be a cloud endpoint (`https://…`), a host-local server (`http://127.0.0.1:4445`,
`localhost`, `[::1]`), or a compose service name (`http://fabulist-models:8080`).

In the Docker image the compose file sets `extra_hosts: host.docker.internal:host-gateway` and
`FABULIST_HOST_ALIAS=host.docker.internal`. That rewrites a loopback host to the host machine,
because inside a container `127.0.0.1` is the container itself. Remote hosts and LAN IPs are never
rewritten. An **empty** `FABULIST_HOST_ALIAS` turns the rewrite off, and the URL is dialled exactly
as written.

`GET /api/connections/doctor` probes every configured connection and reports the resolved URL, the
reachability and the latency.

## Keys

Keys can also be pasted in the Connections drawer. The engine writes `FABULIST_<NAME>_KEY` into the
project `.env`, which it creates with mode `0600` and which is gitignored. It rewrites only that
line, and the key applies on the next call with no restart. The API never echoes the key back, and
the key is never serialised into a response, a log line or the config document.

## Context size

Declare each connection's context size in the drawer (`context_window`). It is never probed. The
budget warnings, the prompt headroom and the compaction threshold are all computed from it, so the
declared number is the one the engine believes.

DeepSeek's `GET /v1/models` returns a `context_window` you can copy. z.ai returns none, and `200000`
for `glm-5.3` is a community guess rather than a published figure.

## Other fields

- `vision` is a per-connection toggle, resolved live at every assembly seam.
- `FABULIST_DEV_ORIGINS=http://localhost:5173,…` adds CORS origins when you serve the UI from a
  different origin than the engine's defaults.

## Agent slots

`GET /api/connections` returns one row per agent slot. That is the `slots` array the Connections
drawer renders: eleven in the default sidecar setup, each bound to one connection.

| slot | what it feeds |
|---|---|
| `completion` | the type default: any completion task or item with no more specific binding |
| `completion.narration` | the narrator pass — prose and the generative-UI DSL |
| `completion.extraction` | structured pulls: critic, proposals, summaries, captions |
| `completion.state` | the state tracker's entity-registry writes |
| `summarizer` | compaction and era summaries (falls back to `completion.extraction`) |
| `captioner` | attachment and image captions (falls back to `completion.extraction`) |
| `imagegen` | image-generation requests |
| `embedding` | the type default for embedding slots (an authored `embedder` wins) |
| `embedder` | the live memory embedder |
| `decision` | the native decision model (`POST /v1/systemone`) |
| `reranker` | recall reranking (per-item key; the sidecar auto-pin writes it) |

`narrator` is a legacy alias. It normalises to `completion.narration`, so either spelling binds the
same slot, and the typed spelling wins if both are authored. `extractor` behaves the same way and
normalises to `completion.extraction`.

An empty slot falls through item → task → type. It only fails at call time, if nothing resolves.

## Known issues

- **#45** — `glm-5.3` can echo the engine's own `<preset_def>` wrapper into committed prose. Bind
  narration to another connection until that is fixed.
- **#46** — a client that disconnects mid-stream can leave a committed turn with an empty narrative
  payload: empty prose, empty DSL. The turn is kept.
