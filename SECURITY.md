# Security Policy

Fabulist is a **personal, localhost-only** narrative engine. It has no accounts, no
multi-tenancy and no network hardening to speak of — that is a design choice, not an
oversight, and it shapes everything below. Read this before pointing it at anything you
would not hand your shell to.

## Reporting a vulnerability

Report privately through **GitHub's private vulnerability reporting**: on this repository,
go to **Security → Advisories → Report a vulnerability**. Please do not open a public issue
for a vulnerability.

Include what you did, what happened, and the version (`pyproject.toml`) plus the commit you
run. Please **redact** session content and anything that looks like a key. There is no bug
bounty and no response SLA; this is a solo-maintained project.

## Threat model

### 1. Prompt injection is a first-class threat

Everything a model reads is an instruction channel, and most of what the model reads is
**not authored by the engine**:

- imported **character cards** (including the PNG `chara` payload),
- imported **world-info books**,
- imported **prompt presets** (`POST /api/prompts/presets/import`),
- imported **persona/profile exports**,
- model output from a previous turn, MCP tool results, and attachments transcribed by STT,
- your own chat text.

The engine's mitigation is a serving-time envelope, not a content filter: assembled
narrator messages carry two bounds — `<preset_def>` (the engine definition together with the
conversation through the player's latest message, the player's own message being scene input
rather than instruction) and `<preset_runtime>` (the per-turn tail after it: recalled
context, lanes, palette) — with an owner-authored guard line, outside both, stating that
anything outside them carries no instruction authority
(`src/fabulist/prompts/wrapper.py`, `src/fabulist/prompts/parts.py`). This raises the cost of
a hijack; it does not defeat a determined one. Treat it as a seatbelt.

Practical consequences:

- **Do not import presets, cards, worlds or profiles from untrusted sources.** An imported
  preset is executable influence over a model that can call tools, write to the event log,
  and describe its own state.
- A successful injection can corrupt a session's narrative and the state recorded from it. A
  corrupted event log is hard to unwind by hand — fork or discard the affected session.
- If you run a model you do not control (a remote API), the injection surface includes
  whatever that provider injects, and your prompt content is theirs to see.

### 2. There is no authentication — by design

The API has **no authentication and no authorization**. Every endpoint is open to anything
that can reach the port, and it mutates everything: sessions, event log, personas, cards,
worlds, prompt presets, assistant definitions, connection routing, `.env` keys.

Therefore:

- **Never expose `:2727` to a network** — not with `--host 0.0.0.0`, not behind a reverse
  proxy, not through a tunnel, not "just for a minute". The default is `127.0.0.1`.
- CORS accepts the local dev frontend origins (plus whatever you add explicitly through
  `FABULIST_DEV_ORIGINS`). That restricts browsers, not attackers with `curl`.
- Anything else running as your user on that machine can drive the engine, including writing
  state and storing keys. Host trust is the only trust boundary.

If you need remote access, put a VPN in front of it and accept that you are the access
control layer.

### 3. API keys and secrets

- Model API keys live in the project-root **`.env`**, written with mode **0600** and
  gitignored (`src/fabulist/connections/registry.py`). The connections document only names
  an environment variable (`api_key_env`).
- `configs/connections.json` (connection routing: base URLs, models, sampling) is
  gitignored user state. `configs/connections.example.json` is the shape-only template.
- **`data/`** (event log — which also holds attachment bytes — checkpoints and the memory
  index) is gitignored user state and contains the full text of everything you have
  generated.
- Never commit `.env`, `configs/connections.json`, or `data/`. Never paste a key into an
  issue, a bug report or a log excerpt. The key-storage path refuses to write a literal key
  into the connections document, but the engine cannot protect a key you commit yourself.

### 4. MCP servers are a capability surface

Fabulist is an MCP **client only**: it connects to external MCP servers *you* configure and
calls the tools they advertise (`src/fabulist/mcp/client.py`). Two consequences:

- The **stdio transport spawns a subprocess** from the command you configured. Configuring a
  server is granting code execution as the engine's user; do that only with servers you
  trust and have read.
- Tool results are **untrusted input** to the model and inherit the injection risk in §1,
  and the tools themselves act on your machine or a remote service.

### 5. This is an adult-content engine, and outputs are model-generated

The preset, the components and the engine are built for unfiltered roleplay. The project does
not filter, classify or moderate model output, and takes no responsibility for it: what the
model writes is generated by the model you configured. Provider terms, local law and the
consequences of what you publish are yours. Nothing in `data/` leaves your machine unless you
send it to a remote endpoint or publish it yourself.

### 6. Model output is rendered in your browser

Prose, HTML and DSL component output from the model reaches the page. Markdown and the
`Custom(html=,css=)` escape hatch are sanitized with DOMPurify in the browser
(`frontend/src/lib/markdown.ts`, `frontend/src/lib/openui/customSanitize.ts`), and DSL
statements are validated against the generated component spec before they render. Sanitizers
and validators have bypasses; running a hostile model is a browser-side risk you are
accepting.

## Supported versions

Only the default branch is supported. There are no backports; upgrade to receive fixes.
