"""Personal team policies, separate from official game allowances."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path

from .errors import PayloadError
from .models import GameUrl
from .persistence import exclusive_configuration_update, replace_text_atomically


@dataclass(frozen=True, slots=True)
class TeamStrategy:
    transfer_policy: str = "unspecified"
    captain_policy: str = "unspecified"
    team_type: str | None = None
    max_transfers: int | None = None
    reserve_contracts: int = 0
    start_round: int | None = None
    end_round: int | None = None
    identity: str = ""
    required_player_ids: tuple[int, ...] = ()
    excluded_player_ids: tuple[int, ...] = ()
    fixed_captain_id: int | None = None
    budget: int | None = None

    def __post_init__(self):
        if self.reserve_contracts is None:
            raise ValueError("reserve_contracts must be an integer")
        if not isinstance(self.transfer_policy, str) or self.transfer_policy not in {"unspecified", "active", "limited", "emergency_only", "fixed"}:
            raise ValueError("Invalid transfer_policy")
        if not isinstance(self.captain_policy, str) or self.captain_policy not in {"unspecified", "fixed", "rotate"}:
            raise ValueError("Invalid captain_policy")
        if self.team_type is not None and (not isinstance(self.team_type, str) or self.team_type not in {"basis", "gold"}):
            raise ValueError("team_type must be basis or gold")
        for name in ("max_transfers", "reserve_contracts", "start_round",
                     "end_round", "fixed_captain_id", "budget"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                or name in {"start_round", "end_round", "fixed_captain_id"} and value == 0
            ):
                raise ValueError(f"{name} must be a non-negative integer (rounds and IDs positive)")
        if self.start_round is not None and self.end_round is not None and self.end_round < self.start_round:
            raise ValueError("end_round precedes start_round")
        if not isinstance(self.identity, str) or len(self.identity) > 2000:
            raise ValueError("identity must be text of at most 2000 characters")
        for ids in (self.required_player_ids, self.excluded_player_ids):
            if (not isinstance(ids, tuple)
                    or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in ids)
                    or len(ids) != len(set(ids))):
                raise ValueError("Player IDs must be distinct positive integers")
        if set(self.required_player_ids) & set(self.excluded_player_ids):
            raise ValueError("A player cannot be both required and excluded")
        if self.fixed_captain_id in self.excluded_player_ids:
            raise ValueError("The fixed captain cannot be excluded")


def strategy_from_dict(raw: object) -> TeamStrategy:
    if not isinstance(raw, dict):
        raise ValueError("Strategy must be an object")
    names = {item.name for item in fields(TeamStrategy)}
    if unknown := raw.keys() - names:
        raise ValueError(f"Unknown strategy fields: {', '.join(sorted(unknown))}")
    values = dict(raw)
    for name in ("required_player_ids", "excluded_player_ids"):
        if name in values:
            if not isinstance(values[name], list):
                raise ValueError(f"{name} must be a list")
            values[name] = tuple(values[name])
    return TeamStrategy(**values)


def strategy_to_dict(strategy: TeamStrategy) -> dict:
    value = asdict(strategy)
    value["required_player_ids"] = list(strategy.required_player_ids)
    value["excluded_player_ids"] = list(strategy.excluded_player_ids)
    return value


class StrategyStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> dict[tuple[str, str, int], TeamStrategy]:
        if not self.path.exists():
            return {}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != 1 or not isinstance(raw.get("teams"), list):
            raise PayloadError("Unsupported team-strategy file")
        result = {}
        for item in raw["teams"]:
            if not isinstance(item, dict):
                raise PayloadError("Invalid team strategy")
            game = item.get("game")
            team_id = item.get("team_id")
            if (not isinstance(game, dict) or not isinstance(game.get("locale"), str)
                    or not game["locale"] or not isinstance(game.get("slug"), str)
                    or not game["slug"] or isinstance(team_id, bool)
                    or not isinstance(team_id, int) or team_id <= 0):
                raise PayloadError("Strategy needs an exact game and positive team ID")
            identity = game["locale"].casefold(), game["slug"], team_id
            if identity in result:
                raise PayloadError("Duplicate team strategy")
            result[identity] = strategy_from_dict(item.get("strategy"))
        return result

    def get(self, game: GameUrl, team_id: int) -> TeamStrategy | None:
        return self.load().get((game.locale.casefold(), game.slug, team_id))

    def set(self, game: GameUrl, team_id: int, strategy: TeamStrategy) -> None:
        if isinstance(team_id, bool) or not isinstance(team_id, int) or team_id <= 0:
            raise ValueError("team_id must be a positive integer")
        with exclusive_configuration_update(self.path.parent):
            values = self.load()
            values[game.locale.casefold(), game.slug, team_id] = strategy
            replace_text_atomically(self.path, json.dumps({
                "schema_version": 1,
                "teams": [
                    {"game": {"locale": locale, "slug": slug}, "team_id": identifier,
                     "strategy": strategy_to_dict(value)}
                    for (locale, slug, identifier), value in sorted(values.items())
                ],
            }, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
