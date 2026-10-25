"""Rule-based state extractor: keyword/pattern heuristics, no model (PLAN W1.3).

This is the deterministic fallback for when no extractor model is configured, and
the test oracle the LLM extractor is compared against. It stays deliberately
conservative: it only emits a delta for an explicit, unmistakable pattern, so it
never invents state. Where it is uncertain it emits no delta, not a guess.

Action classes mirror the rules clock's intent (PLAN D3): ``travel`` / ``rest`` /
``dialogue`` / ``inspection`` / ``task``, with ``other`` when nothing matches.
"""

from __future__ import annotations

import re

from .extractor import ExtractorResult

__all__ = ["RuleBasedExtractor"]

# Ordered longest-signal-first so "go to sleep" reads as rest, not travel.
_ACTION_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "rest",
        ("sleep", "rest", "camp", "relax", "wait", "eat", "drink", "nap"),
    ),
    (
        "travel",
        (
            "go",
            "walk",
            "run",
            "travel",
            "enter",
            "leave",
            "head",
            "arrive",
            "ride",
            "sail",
            "climb",
        ),
    ),
    (
        "dialogue",
        (
            "say",
            "ask",
            "tell",
            "speak",
            "shout",
            "whisper",
            "reply",
            "greet",
            "call",
            "answer",
        ),
    ),
    (
        "inspection",
        (
            "look",
            "inspect",
            "examine",
            "search",
            "check",
            "study",
            "read",
            "watch",
            "observe",
        ),
    ),
    (
        "task",
        (
            "take",
            "pick",
            "grab",
            "give",
            "drop",
            "use",
            "open",
            "close",
            "push",
            "pull",
            "craft",
            "build",
            "repair",
        ),
    ),
)

# Explicit delta patterns. Each captures a short noun phrase (up to two words).
# Dative "gives X to Y" captures X, not Y — acceptable for a conservative oracle.
_TAKE_RE = re.compile(
    r"\b(?:takes?|took|picks? up|picked up|grabs?|grabbed)\s+"
    r"(?:the|a|an|my|his|her|their)?\s*"
    r"([a-z][a-z0-9'_-]*(?:\s+[a-z][a-z0-9'_-]*)?)",
    re.IGNORECASE,
)
_DROP_RE = re.compile(
    r"\b(?:drops?|dropped|gives?|gave|loses?|lost)\s+"
    r"(?:the|a|an|my|his|her|their)?\s*"
    r"([a-z][a-z0-9'_-]*(?:\s+[a-z][a-z0-9'_-]*)?)",
    re.IGNORECASE,
)
_GO_RE = re.compile(
    r"\b(?:enters?|entered|arrives? at|arrived at|go(?:es)? to|went to|"
    r"heads? to|headed to)\s+"
    r"(?:the|a|an|my|his|her|their)?\s*"
    r"([a-z][a-z0-9'_-]*(?:\s+[a-z][a-z0-9'_-]*)?)",
    re.IGNORECASE,
)

# Trailing function words are not part of a captured item/location phrase.
_STOPWORDS = frozenset(
    {"and", "then", "to", "the", "a", "an", "with", "from", "into",
     "at", "in", "on", "of", "for", "but", "so", "as", "by"}
)


def _phrase(value: str) -> str:
    """Trim trailing function words from a captured noun phrase."""
    words = value.split()
    while words and words[-1].lower() in _STOPWORDS:
        words.pop()
    return " ".join(words)


def _keyword_match(text: str, keywords: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(word)}\b", text) for word in keywords)


class RuleBasedExtractor:
    """Deterministic keyword/pattern extractor (fallback + test oracle).

    ``confidence`` is ``1.0`` when an explicit delta pattern matched and ``0.5``
    when only the action class was inferred — the signal that a model-backed
    extractor should be preferred when one is available.
    """

    def __call__(self, user_msg: str, narrative: str) -> ExtractorResult:
        text = f"{user_msg}\n{narrative}"

        action_class = self._action_class(text)
        deltas = self._deltas(text)
        confidence = 1.0 if deltas else 0.5
        return ExtractorResult(
            deltas=deltas, action_class=action_class, confidence=confidence
        )

    @staticmethod
    def _action_class(text: str) -> str:
        for action_class, keywords in _ACTION_KEYWORDS:
            if _keyword_match(text, keywords):
                return action_class
        return "other"

    @staticmethod
    def _deltas(text: str) -> dict[str, str]:
        deltas: dict[str, str] = {}

        take = _TAKE_RE.search(text)
        if take:
            deltas["inv"] = f"+{_phrase(take.group(1))}"

        drop = _DROP_RE.search(text)
        if drop:
            item = _phrase(drop.group(1))
            # A take and a drop in one turn read as a swap: keep both signs.
            deltas["inv"] = (
                f"{deltas['inv']} -{item}" if "inv" in deltas else f"-{item}"
            )

        go = _GO_RE.search(text)
        if go:
            deltas["loc"] = _phrase(go.group(1))

        return deltas
