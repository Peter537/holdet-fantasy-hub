"""Explicit season definitions and score-profile based standings."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
from uuid import uuid4

from .errors import PayloadError
from .hall_of_fame import HallOfFameEvent, build_hall_of_fame
from .hub_settings import HallOfFameScoreProfile
from .persistence import aware_local, replace_text_atomically


SEASON_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class SeasonDefinition:
    season_id: str
    name: str
    competition_ids: tuple[str, ...]
    archived_at: str | None = None

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


@dataclass(frozen=True, slots=True)
class SeasonStanding:
    rank: int
    manager_id: str
    manager_name: str
    points: int
    titles: int
    podiums: int
    competitions: int
    round_wins: int


def build_season_standings(
    season: SeasonDefinition,
    events: tuple[HallOfFameEvent, ...],
    score_profile: HallOfFameScoreProfile,
) -> tuple[SeasonStanding, ...]:
    """Recalculate a season without mutating its raw manager events."""

    def belongs(event: HallOfFameEvent) -> bool:
        return any(
            event.competition_id == competition
            or event.competition_id.startswith(f"{competition}:")
            for competition in season.competition_ids
        )

    season_games = {
        (event.game_locale.casefold(), event.game_slug)
        for event in events
        if event.kind != "round_win" and belongs(event)
    }
    selected = tuple(
        event
        for event in events
        if belongs(event)
        or (
            event.kind == "round_win"
            and (event.game_locale.casefold(), event.game_slug) in season_games
        )
    )
    hall = build_hall_of_fame(selected, score_profile)
    return tuple(
        SeasonStanding(
            row.rank,
            row.manager_id,
            row.manager_name,
            row.points,
            row.titles,
            row.podiums,
            row.competitions,
            sum(
                1
                for event in hall.events
                for placement in event.placements
                if event.kind == "round_win"
                and placement.rank == 1
                and placement.manager_id == row.manager_id
            ),
        )
        for row in hall.rows
    )


class SeasonStore:
    """Versioned, additive store for manually assembled seasons."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> tuple[SeasonDefinition, ...]:
        if not self.path.exists():
            return ()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PayloadError(f"Sæsoner kunne ikke læses: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise PayloadError("Sæsonlageret indeholder ugyldig JSON") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != SEASON_SCHEMA_VERSION:
            raise PayloadError("Ukendt skema for sæsoner")
        raw_seasons = payload.get("seasons", [])
        if not isinstance(raw_seasons, list):
            raise PayloadError("Sæsonlageret skal indeholde en liste")
        seasons: list[SeasonDefinition] = []
        for raw in raw_seasons:
            if not isinstance(raw, dict):
                raise PayloadError("En sæson skal være et objekt")
            competition_ids = raw.get("competition_ids")
            if not isinstance(competition_ids, list) or not all(
                isinstance(item, str) and item.strip() for item in competition_ids
            ):
                raise PayloadError("Sæsonens competition_ids skal være tekstværdier")
            season_id = str(raw.get("id", "")).strip()
            name = str(raw.get("name", "")).strip()
            archived_at = raw.get("archived_at")
            if not season_id or not name:
                raise PayloadError("En sæson skal have et ID og et navn")
            if archived_at is not None and not isinstance(archived_at, str):
                raise PayloadError("Sæsonens arkiveringstidspunkt skal være tekst")
            seasons.append(
                SeasonDefinition(
                    season_id,
                    name,
                    tuple(dict.fromkeys(item.strip() for item in competition_ids)),
                    archived_at,
                )
            )
        ids = [item.season_id for item in seasons]
        if len(ids) != len(set(ids)):
            raise PayloadError("S\u00e6son-ID'er skal v\u00e6re entydige")
        return tuple(seasons)

    def save(self, seasons: tuple[SeasonDefinition, ...]) -> None:
        ids = [item.season_id for item in seasons]
        if len(ids) != len(set(ids)):
            raise PayloadError("Sæson-ID'er skal være entydige")
        payload = {
            "schema_version": SEASON_SCHEMA_VERSION,
            "seasons": [
                {
                    "id": item.season_id,
                    "name": item.name,
                    "competition_ids": list(item.competition_ids),
                    "archived_at": item.archived_at,
                }
                for item in seasons
            ],
        }
        replace_text_atomically(
            self.path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def create(
        self,
        seasons: tuple[SeasonDefinition, ...],
        name: str,
        competition_ids: tuple[str, ...],
    ) -> tuple[tuple[SeasonDefinition, ...], SeasonDefinition]:
        normalized_name = name.strip()
        normalized_competitions = tuple(dict.fromkeys(item.strip() for item in competition_ids if item.strip()))
        if not normalized_name or not normalized_competitions:
            raise PayloadError("En sæson skal have et navn og mindst én konkurrence")
        season = SeasonDefinition(uuid4().hex[:12], normalized_name, normalized_competitions)
        updated = (*seasons, season)
        self.save(updated)
        return updated, season


    def update(
        self,
        seasons: tuple[SeasonDefinition, ...],
        season_id: str,
        *,
        name: str,
        competition_ids: tuple[str, ...],
    ) -> tuple[SeasonDefinition, ...]:
        normalized_name = name.strip()
        normalized_competitions = tuple(
            dict.fromkeys(
                item.strip()
                for item in competition_ids
                if item.strip()
            )
        )
        if not normalized_name or not normalized_competitions:
            raise PayloadError(
                "En s\u00e6son skal have et navn og mindst \u00e9n konkurrence"
            )
        found = False
        updated: list[SeasonDefinition] = []
        for season in seasons:
            if season.season_id == season_id:
                found = True
                updated.append(
                    replace(
                        season,
                        name=normalized_name,
                        competition_ids=normalized_competitions,
                    )
                )
            else:
                updated.append(season)
        if not found:
            raise PayloadError(f"Ukendt s\u00e6son: {season_id}")
        result = tuple(updated)
        self.save(result)
        return result

    def archive(
        self,
        seasons: tuple[SeasonDefinition, ...],
        season_id: str,
    ) -> tuple[SeasonDefinition, ...]:
        timestamp = aware_local().isoformat()
        found = False
        updated = []
        for season in seasons:
            if season.season_id == season_id:
                found = True
                updated.append(replace(season, archived_at=timestamp))
            else:
                updated.append(season)
        if not found:
            raise PayloadError("Ukendt sæson")
        result = tuple(updated)
        self.save(result)
        return result


def active_season_competitions(
    seasons: tuple[SeasonDefinition, ...],
) -> frozenset[str]:
    return frozenset(
        competition
        for season in seasons
        if not season.is_archived
        for competition in season.competition_ids
    )
