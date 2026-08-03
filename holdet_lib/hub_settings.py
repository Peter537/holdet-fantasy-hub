"""Versioned Hub settings: watchlists, manager aliases and score rules."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any

from .errors import PayloadError
from .models import GameUrl, PlayerEntry
from .persistence import replace_text_atomically


HUB_SETTINGS_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class HallOfFameScoreProfile:
    """Editable scoring weights applied to frozen raw results."""

    group_points: tuple[int, int, int, int] = (10, 6, 3, 1)
    tournament_winner: int = 10
    tournament_finalist: int = 6
    tournament_semifinalist: int = 3
    global_round_win: int = 1

    def __post_init__(self) -> None:
        values = (
            *self.group_points,
            self.tournament_winner,
            self.tournament_finalist,
            self.tournament_semifinalist,
            self.global_round_win,
        )
        if any(isinstance(value, bool) or value < 0 for value in values):
            raise ValueError("Hall of Fame-point skal være ikke-negative heltal")


@dataclass(frozen=True, slots=True)
class WatchlistEntry:
    game_locale: str
    game_slug: str
    player_key: str
    entry_id: int | None
    person_id: int | None
    name: str
    team: str
    position: str

    @property
    def game_identity(self) -> tuple[str, str]:
        return self.game_locale.casefold(), self.game_slug


@dataclass(frozen=True, slots=True)
class ManagerAlias:
    canonical_id: str
    display_name: str
    identity_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HubSettings:
    watchlist: tuple[WatchlistEntry, ...] = ()
    manager_aliases: tuple[ManagerAlias, ...] = ()
    hall_of_fame_score: HallOfFameScoreProfile = HallOfFameScoreProfile()


def player_identity(game: GameUrl, entry: PlayerEntry) -> str:
    """Return the stable game-scoped player identity with a legacy fallback."""

    game_key = f"{game.locale.casefold()}:{game.slug}"
    if entry.entry_id is not None:
        return f"{game_key}:entry:{entry.entry_id}"
    legacy = "|".join(
        value.strip().casefold() for value in (entry.name, entry.team, entry.position)
    )
    return f"{game_key}:legacy:{legacy}"


def watchlist_entry(game: GameUrl, entry: PlayerEntry) -> WatchlistEntry:
    return WatchlistEntry(
        game.locale.casefold(),
        game.slug,
        player_identity(game, entry),
        entry.entry_id,
        entry.person_id,
        entry.name,
        entry.team,
        entry.position,
    )


def manager_identity_keys(
    *,
    owner_user_id: int | None,
    account_user_id: int | None,
    account_key: str,
    owner_name: str,
) -> tuple[str, ...]:
    """Return manager keys in authoritative-to-legacy order."""

    keys: list[str] = []
    if owner_user_id is not None:
        keys.append(f"owner:{owner_user_id}")
    if account_user_id is not None:
        keys.append(f"account-user:{account_user_id}")
    if account_key.strip():
        keys.append(f"account-key:{account_key.strip().casefold()}")
    if owner_name.strip():
        keys.append(f"legacy-name:{owner_name.strip().casefold()}")
    return tuple(dict.fromkeys(keys))


def resolve_manager_identity(
    settings: HubSettings,
    *,
    owner_user_id: int | None,
    account_user_id: int | None,
    account_key: str,
    owner_name: str,
) -> tuple[str, str]:
    keys = manager_identity_keys(
        owner_user_id=owner_user_id,
        account_user_id=account_user_id,
        account_key=account_key,
        owner_name=owner_name,
    )
    aliases = {
        key: alias
        for alias in settings.manager_aliases
        for key in alias.identity_keys
    }
    for key in keys:
        if alias := aliases.get(key):
            return alias.canonical_id, alias.display_name
    canonical = keys[0] if keys else f"legacy-name:{owner_name.strip().casefold() or 'ukendt'}"
    return canonical, owner_name.strip() or "Ukendt manager"


def _require_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PayloadError(f"{label} skal være et objekt")
    return value


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PayloadError(f"{label} skal være tekst")
    return value.strip()


def _optional_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise PayloadError(f"{label} skal være et heltal")
    return value


def _score_from_dict(raw: object) -> HallOfFameScoreProfile:
    item = _require_object(raw, "hall_of_fame_score")
    group_raw = item.get("group_points")
    if (
        not isinstance(group_raw, list)
        or len(group_raw) != 4
        or any(not isinstance(value, int) or isinstance(value, bool) for value in group_raw)
    ):
        raise PayloadError("group_points skal indeholde fire heltal")
    try:
        return HallOfFameScoreProfile(
            tuple(group_raw),
            int(item.get("tournament_winner")),
            int(item.get("tournament_finalist")),
            int(item.get("tournament_semifinalist")),
            int(item.get("global_round_win")),
        )
    except (TypeError, ValueError) as exc:
        raise PayloadError(f"ugyldig Hall of Fame-pointprofil: {exc}") from exc


def _settings_from_dict(payload: object) -> HubSettings:
    root = _require_object(payload, "Hub-indstillinger")
    if root.get("schema_version") != HUB_SETTINGS_SCHEMA_VERSION:
        raise PayloadError("ukendt schema for Hub-indstillinger")
    raw_watchlist = root.get("watchlist", [])
    raw_aliases = root.get("manager_aliases", [])
    if not isinstance(raw_watchlist, list) or not isinstance(raw_aliases, list):
        raise PayloadError("watchlist og manager_aliases skal være lister")
    watched: list[WatchlistEntry] = []
    for index, raw in enumerate(raw_watchlist):
        item = _require_object(raw, f"watchlist[{index}]")
        watched.append(
            WatchlistEntry(
                _require_text(item.get("game_locale"), "game_locale").casefold(),
                _require_text(item.get("game_slug"), "game_slug"),
                _require_text(item.get("player_key"), "player_key"),
                _optional_integer(item.get("entry_id"), "entry_id"),
                _optional_integer(item.get("person_id"), "person_id"),
                _require_text(item.get("name"), "name"),
                _require_text(item.get("team"), "team"),
                _require_text(item.get("position"), "position"),
            )
        )
    aliases: list[ManagerAlias] = []
    for index, raw in enumerate(raw_aliases):
        item = _require_object(raw, f"manager_aliases[{index}]")
        keys = item.get("identity_keys")
        if not isinstance(keys, list) or not keys or not all(
            isinstance(value, str) and value.strip() for value in keys
        ):
            raise PayloadError("manageralias skal have identity_keys")
        aliases.append(
            ManagerAlias(
                _require_text(item.get("canonical_id"), "canonical_id"),
                _require_text(item.get("display_name"), "display_name"),
                tuple(dict.fromkeys(value.strip() for value in keys)),
            )
        )
    return HubSettings(
        tuple(watched),
        tuple(aliases),
        _score_from_dict(root.get("hall_of_fame_score", {})),
    )


def _settings_to_dict(settings: HubSettings) -> dict[str, object]:
    score = settings.hall_of_fame_score
    return {
        "schema_version": HUB_SETTINGS_SCHEMA_VERSION,
        "watchlist": [
            {
                "game_locale": item.game_locale,
                "game_slug": item.game_slug,
                "player_key": item.player_key,
                "entry_id": item.entry_id,
                "person_id": item.person_id,
                "name": item.name,
                "team": item.team,
                "position": item.position,
            }
            for item in settings.watchlist
        ],
        "manager_aliases": [
            {
                "canonical_id": item.canonical_id,
                "display_name": item.display_name,
                "identity_keys": list(item.identity_keys),
            }
            for item in settings.manager_aliases
        ],
        "hall_of_fame_score": {
            "group_points": list(score.group_points),
            "tournament_winner": score.tournament_winner,
            "tournament_finalist": score.tournament_finalist,
            "tournament_semifinalist": score.tournament_semifinalist,
            "global_round_win": score.global_round_win,
        },
    }


class HubSettingsStore:
    """Atomically load and save additive Hub settings."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> HubSettings:
        if not self.path.exists():
            return HubSettings()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PayloadError(f"Hub-indstillinger kunne ikke læses: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise PayloadError("Hub-indstillinger indeholder ugyldig JSON") from exc
        return _settings_from_dict(payload)

    def save(self, settings: HubSettings) -> None:
        payload = _settings_to_dict(settings)
        replace_text_atomically(
            self.path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def set_watchlist(
        self, settings: HubSettings, watchlist: tuple[WatchlistEntry, ...]
    ) -> HubSettings:
        unique = {item.player_key: item for item in watchlist}
        updated = replace(
            settings,
            watchlist=tuple(sorted(unique.values(), key=lambda item: item.player_key)),
        )
        self.save(updated)
        return updated

