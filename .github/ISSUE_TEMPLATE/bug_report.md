---
name: Bug report
about: Something broke — a wrong turn, a crash, a blank pane
labels: bug
---

## What happened

<!-- What you expected, and what you got instead. Prose beats screenshots-with-no-text. -->

## Steps to reproduce

1.
2.
3.

<!-- Does it reproduce with the mock UI (`VITE_MOCK=1 npm run dev`, red MOCK badge)? If yes,
     the bug is client-side and the backend details below are not needed. -->

## Environment

- Fabulist version (`pyproject.toml`) / commit:
- Backend launched how (uv on the host, systemd unit, Docker):
- Browser + version:
- OS:

## Model provider and connection config

<!-- Shape only, REDACTED. Do not paste keys, base URLs with tokens, or model names you
     consider private. `api_key_env` names and the document's shape are what matter. -->

```json
{
  "connections": {
    "<name>": {
      "base_url": "<redacted>",
      "model": "<redacted>",
      "api_key_env": "<NAME>",
      "sampling": { "temperature": 0.6, "max_tokens": 1024 },
      "timeout": 120
    }
  },
  "agents": { "narrator": "<name>", "extractor": "<name>" }
}
```

- Provider kind (local llama.cpp/vLLM, OpenAI-compatible API, other):
- Does it reproduce with a different model on the same connection:

## Engine log excerpt

<!-- `journalctl --user -u fabulist-engine -n 200` or the terminal running
     `scripts/serve.py`. Trim to the failing turn and the traceback. -->

```
<paste log here>
```

## Browser console

<!-- DevTools → Console, errors and warnings only, from just before the failure. -->

```
<paste console output here>
```

## Session content

<!-- Redact session content and keys before posting. Persona names, character cards, world
     text and generated prose are yours; a short redacted excerpt is usually enough. -->
