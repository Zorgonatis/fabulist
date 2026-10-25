"""Scenario model + strict YAML loader for the RP spike harness.

The scenario file is authored by a separate lane and is a hard contract. This
module parses it with **fail-fast strictness**: an unknown key at any level is a
loud :class:`ScenarioError`, never a silently ignored field. Relative card paths
resolve against the scenario file's own directory, so scenario bundles move as a
unit.

Contract (see the spike brief for the authoritative shape)::

    name: dnd-guild
    title: "Spike — Guild RPG"
    flavor: dnd-dungeonmaster
    card: cards/GuildRPG.png          # relative to this file (PNG or JSON)
    turns: 15
    player_model: glm-api
    player:
      name: Ryn
      persona: |
        ...
      directives:
        - {at: 4, note: "Check the quest board — read one posting aloud."}
    beats:
      - {kind: in_character, content: "..."}
    ooc_probes:
      - {at: 6, content: "OOC: quick recap — where are we and what's pending?"}
    worldinfo: []
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

__all__ = [
    "Beat",
    "Directive",
    "OocProbe",
    "Player",
    "Scenario",
    "ScenarioError",
    "WorldInfoEntry",
    "load_scenario",
]


class ScenarioError(ValueError):
    """A scenario file is malformed, incomplete, or carries unknown keys."""


#: Beat kinds accepted in ``beats`` (mirrors the API's ``TurnRequest.kind``).
BEAT_KINDS = ("in_character", "ooc")

_TOP_REQUIRED = frozenset({"name", "card", "turns", "player_model", "player", "beats"})
_TOP_OPTIONAL = frozenset({"title", "flavor", "ooc_probes", "worldinfo"})

_PLAYER_REQUIRED = frozenset({"name", "persona"})
_PLAYER_OPTIONAL = frozenset({"directives"})

_BEAT_KEYS = frozenset({"kind", "content"})
_PROBE_KEYS = frozenset({"at", "content"})
_DIRECTIVE_KEYS = frozenset({"at", "note"})
_WORLDINFO_KEYS = frozenset({"keys", "content"})


@dataclass(frozen=True)
class Beat:
    """One scripted opening turn (runs before any model-driven turn)."""

    kind: str
    content: str


@dataclass(frozen=True)
class OocProbe:
    """An out-of-character probe that REPLACES the sim turn at absolute ``at``."""

    at: int
    content: str


@dataclass(frozen=True)
class Directive:
    """A director note injected into the player sim's context at turn ``at``."""

    at: int
    note: str


@dataclass(frozen=True)
class WorldInfoEntry:
    """One world-info entry POSTed to the session before the run starts."""

    keys: tuple[str, ...]
    content: str

    def as_book_entry(self) -> dict[str, Any]:
        """The ST-shaped entry the legacy ``/worldinfo`` endpoint accepts."""
        return {"keys": list(self.keys), "content": self.content}


@dataclass(frozen=True)
class Player:
    """The simulated human player's character + director notes."""

    name: str
    persona: str
    directives: tuple[Directive, ...] = ()


@dataclass(frozen=True)
class Scenario:
    """A fully validated scenario (card path already resolved + existence-checked)."""

    name: str
    title: str
    flavor: str
    card: Path
    turns: int
    player_model: str
    player: Player
    beats: tuple[Beat, ...] = ()
    ooc_probes: tuple[OocProbe, ...] = ()
    worldinfo: tuple[WorldInfoEntry, ...] = ()
    source_path: Path = field(default=Path("scenario.yaml"))

    # -- turn planning -----------------------------------------------------

    def beat_for(self, turn: int) -> Beat | None:
        """The scripted beat for ``turn`` (1-based), or ``None``.

        Beats occupy turns ``1..len(beats)`` in order.
        """
        if 1 <= turn <= len(self.beats):
            return self.beats[turn - 1]
        return None

    def probe_for(self, turn: int) -> OocProbe | None:
        """The OOC probe scheduled at absolute ``turn``, or ``None``."""
        for probe in self.ooc_probes:
            if probe.at == turn:
                return probe
        return None

    def directives_at(self, turn: int) -> tuple[Directive, ...]:
        """Every director note scheduled at ``turn`` (in file order)."""
        return tuple(d for d in self.player.directives if d.at == turn)

    def directive_for(self, turn: int) -> str | None:
        """The director note text for ``turn`` (notes joined by newline), or ``None``.

        Only ever consulted for model-driven (sim) turns; beats and OOC probes
        do not receive directives.
        """
        notes = [d.note for d in self.directives_at(turn)]
        return "\n".join(notes) if notes else None

    def worldinfo_book(self) -> list[dict[str, Any]]:
        """The world-info entries as an ST-shaped book for the POST body."""
        return [entry.as_book_entry() for entry in self.worldinfo]


# ---------------------------------------------------------------------------
# strict helpers
# ---------------------------------------------------------------------------


def _require_mapping(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise ScenarioError(
            f"{where}: expected a mapping, got {type(value).__name__}"
        )
    return value


def _check_keys(
    data: dict, where: str, *, required: frozenset[str], optional: frozenset[str]
) -> None:
    unknown = sorted(set(data) - required - optional)
    if unknown:
        raise ScenarioError(
            f"{where}: unknown key(s) {unknown}; allowed keys: "
            f"{sorted(required | optional)}"
        )
    missing = sorted(required - set(data))
    if missing:
        raise ScenarioError(f"{where}: missing required key(s) {missing}")


def _require_str(value: Any, where: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ScenarioError(f"{where}: expected a string, got {type(value).__name__}")
    if not allow_empty and not value.strip():
        raise ScenarioError(f"{where}: must be a non-empty string")
    return value


def _require_int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioError(f"{where}: expected an integer, got {type(value).__name__}")
    return value


def _require_list(value: Any, where: str) -> list:
    if not isinstance(value, list):
        raise ScenarioError(f"{where}: expected a list, got {type(value).__name__}")
    return value


def _parse_beat(raw: Any, where: str) -> Beat:
    data = _require_mapping(raw, where)
    _check_keys(data, where, required=_BEAT_KEYS, optional=frozenset())
    kind = _require_str(data["kind"], f"{where}.kind")
    if kind not in BEAT_KINDS:
        raise ScenarioError(
            f"{where}.kind: must be one of {list(BEAT_KINDS)}, got {kind!r}"
        )
    content = _require_str(data["content"], f"{where}.content")
    return Beat(kind=kind, content=content)


def _parse_probe(raw: Any, where: str) -> OocProbe:
    data = _require_mapping(raw, where)
    _check_keys(data, where, required=_PROBE_KEYS, optional=frozenset())
    at = _require_int(data["at"], f"{where}.at")
    if at < 1:
        raise ScenarioError(f"{where}.at: must be >= 1, got {at}")
    content = _require_str(data["content"], f"{where}.content")
    return OocProbe(at=at, content=content)


def _parse_directive(raw: Any, where: str) -> Directive:
    data = _require_mapping(raw, where)
    _check_keys(data, where, required=_DIRECTIVE_KEYS, optional=frozenset())
    at = _require_int(data["at"], f"{where}.at")
    if at < 1:
        raise ScenarioError(f"{where}.at: must be >= 1, got {at}")
    note = _require_str(data["note"], f"{where}.note")
    return Directive(at=at, note=note)


def _parse_worldinfo(raw: Any, where: str) -> WorldInfoEntry:
    data = _require_mapping(raw, where)
    _check_keys(data, where, required=_WORLDINFO_KEYS, optional=frozenset())
    keys_raw = _require_list(data["keys"], f"{where}.keys")
    keys: list[str] = []
    for i, key in enumerate(keys_raw):
        keys.append(_require_str(key, f"{where}.keys[{i}]"))
    if not keys:
        raise ScenarioError(f"{where}.keys: must contain at least one key")
    content = _require_str(data["content"], f"{where}.content")
    return WorldInfoEntry(keys=tuple(keys), content=content)


def _parse_player(raw: Any, where: str) -> Player:
    data = _require_mapping(raw, where)
    _check_keys(data, where, required=_PLAYER_REQUIRED, optional=_PLAYER_OPTIONAL)
    name = _require_str(data["name"], f"{where}.name")
    persona = _require_str(data["persona"], f"{where}.persona")
    directives_raw = data.get("directives", [])
    directives = tuple(
        _parse_directive(item, f"{where}.directives[{i}]")
        for i, item in enumerate(_require_list(directives_raw, f"{where}.directives"))
    )
    return Player(name=name, persona=persona, directives=directives)


def load_scenario(path: str | Path) -> Scenario:
    """Read + strictly validate a scenario YAML file.

    Raises :class:`ScenarioError` for a missing/unreadable file, invalid YAML,
    unknown keys at any level, missing required fields, wrong types, or
    out-of-range turn references. The ``card`` path is resolved against the
    scenario file's directory and must exist.
    """
    scenario_path = Path(path).expanduser()
    if not scenario_path.is_absolute():
        scenario_path = (Path.cwd() / scenario_path).resolve()
    if not scenario_path.is_file():
        raise ScenarioError(f"scenario file not found: {scenario_path}")

    try:
        raw = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{scenario_path}: invalid YAML: {exc}") from exc

    data = _require_mapping(raw, str(scenario_path))
    _check_keys(
        data,
        str(scenario_path),
        required=_TOP_REQUIRED,
        optional=_TOP_OPTIONAL,
    )

    name = _require_str(data["name"], "name")
    title = _require_str(data.get("title", name), "title", allow_empty=True)
    flavor = _require_str(data.get("flavor", ""), "flavor", allow_empty=True)
    turns = _require_int(data["turns"], "turns")
    if turns < 1:
        raise ScenarioError(f"turns: must be >= 1, got {turns}")
    player_model = _require_str(data["player_model"], "player_model")
    player = _parse_player(data["player"], "player")

    card_raw = _require_str(data["card"], "card")
    card_path = Path(card_raw)
    if not card_path.is_absolute():
        card_path = (scenario_path.parent / card_path).resolve()
    if not card_path.is_file():
        raise ScenarioError(f"card: file not found: {card_path}")

    beats = tuple(
        _parse_beat(item, f"beats[{i}]")
        for i, item in enumerate(_require_list(data["beats"], "beats"))
    )
    ooc_probes = tuple(
        _parse_probe(item, f"ooc_probes[{i}]")
        for i, item in enumerate(
            _require_list(data.get("ooc_probes", []), "ooc_probes")
        )
    )
    worldinfo = tuple(
        _parse_worldinfo(item, f"worldinfo[{i}]")
        for i, item in enumerate(_require_list(data.get("worldinfo", []), "worldinfo"))
    )

    _validate_ranges(turns, beats, ooc_probes, player.directives)

    return Scenario(
        name=name,
        title=title or name,
        flavor=flavor,
        card=card_path,
        turns=turns,
        player_model=player_model,
        player=player,
        beats=beats,
        ooc_probes=ooc_probes,
        worldinfo=worldinfo,
        source_path=scenario_path,
    )


def _validate_ranges(
    turns: int,
    beats: tuple[Beat, ...],
    ooc_probes: tuple[OocProbe, ...],
    directives: tuple[Directive, ...],
) -> None:
    """Fail fast on schedule references that can never fire."""
    if len(beats) > turns:
        raise ScenarioError(
            f"beats: {len(beats)} beats declared but turns={turns}; "
            "every beat must fit inside the run"
        )
    seen_probe_turns: set[int] = set()
    for i, probe in enumerate(ooc_probes):
        if probe.at > turns:
            raise ScenarioError(
                f"ooc_probes[{i}].at: {probe.at} is past turns={turns}"
            )
        if probe.at in seen_probe_turns:
            raise ScenarioError(
                f"ooc_probes[{i}].at: duplicate probe at turn {probe.at}"
            )
        seen_probe_turns.add(probe.at)
    for i, directive in enumerate(directives):
        if directive.at > turns:
            raise ScenarioError(
                f"player.directives[{i}].at: {directive.at} is past turns={turns}"
            )
