"""Player simulation for the RP spike harness (the "autopilot co-player").

:class:`PlayerSim` impersonates the human player: it takes the recent transcript
(**prose only** — the engine's own :func:`fabulist.prompts.compiler.strip_dsl`
removes openui DSL), the player persona, and the director note scheduled for the
current turn, and asks an OpenAI-compatible chat endpoint for the player
character's next in-character message.

Design notes
------------
* The player model is a **connection** in ``configs/connections.json`` (read
  only). :func:`load_player_connection` resolves it through the engine's registry
  (``api_key_env`` -> ``os.environ``), so the harness never duplicates config
  semantics.
* One request, one retry (the brief's "one retry on failure, then abort"). A
  second failure raises :class:`PlayerSimError`, which the run loop surfaces as a
  resumable abort.
* The transcript projection is fed as ordinary ``user``/``assistant`` messages;
  the persona + format instruction ride the system prompt, and the director note
  for the turn rides the final user nudge.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import httpx

from fabulist.connections.registry import ConnectionConfigError, load_registry
from fabulist.prompts.compiler import strip_dsl

__all__ = [
    "PLAYER_FORMAT_INSTRUCTION",
    "PlayerConnection",
    "PlayerSim",
    "PlayerSimError",
    "build_history",
    "load_player_connection",
    "prose_history",
]

#: The exact format contract appended to the persona system prompt.
PLAYER_FORMAT_INSTRUCTION = (
    "Reply with ONLY your next in-character message, 1-4 sentences."
)

#: Default sampling for the player sim. Owner directive: minimum 16k — the
#: connection's own ceiling, so a thinking model's reasoning_content never
#: starves the actual reply (a 400 cap finished `length` with empty content).
PLAYER_TEMPERATURE = 0.9
PLAYER_MAX_TOKENS = 16384

#: Default transcript depth fed back to the player model.
DEFAULT_HISTORY_DEPTH = 20


class PlayerSimError(RuntimeError):
    """The player model could not be resolved or produced a usable reply."""


@dataclass(frozen=True)
class PlayerConnection:
    """A resolved ``player_model`` connection (short-lived, read-only)."""

    name: str
    base_url: str
    model: str
    api_key: str | None = None
    timeout: float | None = None


def load_player_connection(
    connections_path: str | Path, name: str
) -> PlayerConnection:
    """Resolve connection ``name`` from ``connections.json`` (read-only).

    Uses the engine registry so ``api_key_env`` -> ``os.environ`` lookup and
    validation stay identical to the engine's own resolution. Raises
    :class:`PlayerSimError` with a precise message when the file or name is bad.
    """
    path = Path(connections_path)
    if not path.is_file():
        raise PlayerSimError(f"connections file not found: {path}")
    try:
        registry = load_registry(path)
    except (ConnectionConfigError, OSError, ValueError) as exc:
        raise PlayerSimError(f"{path}: {exc}") from exc
    connection = registry.connections.get(name)
    if connection is None:
        known = ", ".join(sorted(registry.connections)) or "<none>"
        raise PlayerSimError(
            f"player_model {name!r} not found in {path}; known connections: {known}"
        )
    return PlayerConnection(
        name=connection.name,
        base_url=connection.base_url,
        model=connection.model,
        api_key=connection.api_key,
        timeout=connection.timeout,
    )


def _payload_of(event: Any) -> dict:
    payload = event.get("payload") if isinstance(event, dict) else None
    return payload if isinstance(payload, dict) else {}


def build_history(
    user_messages: Sequence[dict] | None,
    narratives: Sequence[dict] | None,
    *,
    limit: int = DEFAULT_HISTORY_DEPTH,
) -> list[dict[str, str]]:
    """Merge a session projection's users + narratives into chat messages.

    Narratives contribute **prose only**: their ``dsl`` is dropped through the
    engine's :func:`strip_dsl`, and any DSL that leaked into ``prose`` is stripped
    too (unfenced component statements included). Rendered texts that become
    empty after stripping are skipped. ``limit`` keeps the newest N messages.
    """
    merged: list[dict] = [
        event for event in [*(user_messages or []), *(narratives or [])] if isinstance(event, dict)
    ]
    merged.sort(key=lambda event: event.get("seq", 0))

    history: list[dict[str, str]] = []
    for event in merged:
        payload = _payload_of(event)
        if event.get("type") == "user" or (event.get("type") is None and "text" in payload):
            text = payload.get("text") or ""
            text = text.strip()
            if text:
                history.append({"role": "user", "content": text})
        else:
            prose = payload.get("prose") or payload.get("text") or ""
            prose = strip_dsl(prose).strip()
            if prose:
                history.append({"role": "assistant", "content": prose})
    if limit > 0:
        history = history[-limit:]
    return history


#: Backwards-friendly alias: the projection-fed history is prose-only.
prose_history = build_history


class PlayerSim:
    """Model-driven co-player: persona + transcript + turn note -> next message."""

    def __init__(
        self,
        connection: PlayerConnection,
        *,
        name: str,
        persona: str,
        client: httpx.Client | None = None,
        temperature: float = PLAYER_TEMPERATURE,
        max_tokens: int = PLAYER_MAX_TOKENS,
        history_depth: int = DEFAULT_HISTORY_DEPTH,
    ) -> None:
        self.connection = connection
        self.name = name
        self.persona = persona
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.history_depth = history_depth
        self._owns_client = client is None
        self._client = client if client is not None else httpx.Client()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> "PlayerSim":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- prompt assembly ---------------------------------------------------

    def system_prompt(self) -> str:
        """Persona + the exact reply-format instruction."""
        persona = self.persona.strip()
        return f"{persona}\n\nYou are {self.name}. {PLAYER_FORMAT_INSTRUCTION}"

    def turn_instruction(self, directive: str | None = None) -> str:
        """The final user nudge, carrying the director note when one fires."""
        nudge = f"Continue the scene as {self.name}."
        if directive and directive.strip():
            return (
                f"[Director note for this reply] {directive.strip()}\n\n{nudge}"
            )
        return nudge

    def build_messages(
        self, history: Sequence[dict[str, str]], directive: str | None = None
    ) -> list[dict[str, str]]:
        """Assemble the chat-completions message list for one player turn."""
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self.system_prompt()}
        ]
        for message in history:
            role = message.get("role")
            content = message.get("content")
            if role in ("user", "assistant") and isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": self.turn_instruction(directive)})
        return messages

    def generate(
        self,
        history: Sequence[dict[str, str]],
        directive: str | None = None,
    ) -> str:
        """Ask the player model for the next message; one retry, then abort.

        Raises :class:`PlayerSimError` after two consecutive failures so the run
        can abort resumably with a clear error.
        """
        messages = self.build_messages(history, directive)
        body: dict[str, Any] = {
            "model": self.connection.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": False,
        }
        url = f"{self.connection.base_url.rstrip('/')}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.connection.api_key:
            headers["Authorization"] = f"Bearer {self.connection.api_key}"

        last_error = "unknown error"
        for _attempt in range(2):
            try:
                resp = self._client.post(
                    url,
                    json=body,
                    headers=headers,
                    timeout=self.connection.timeout or 120.0,
                )
                if resp.status_code >= 400:
                    last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                    continue
                data = resp.json()
                text = self._extract_text(data)
                if text is None:
                    last_error = f"unusable completion: {str(data)[:300]}"
                    continue
                return text
            except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
        raise PlayerSimError(
            f"player model {self.connection.name!r} failed after one retry: "
            f"{last_error}"
        )

    @staticmethod
    def _extract_text(data: Any) -> str | None:
        """Pull the assistant text out of a non-streaming completion body."""
        try:
            choices = data["choices"]
            message = choices[0]["message"]
            content = message["content"]
        except (KeyError, IndexError, TypeError):
            return None
        if not isinstance(content, str):
            return None
        text = content.strip()
        return text or None
