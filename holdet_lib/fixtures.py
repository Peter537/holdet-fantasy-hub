"""Fail-closed contracts for public, explicitly fetched fixture data."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
import json
from pathlib import Path
from typing import Literal

from .errors import PayloadError
from .models import GameUrl
from .output import sanitize_path_component
from .persistence import aware_local, replace_text_atomically


FIXTURE_CACHE_SCHEMA_VERSION = 1
HomeAway = Literal["home", "away", "neutral"]


@dataclass(frozen=True, slots=True)
class FixtureSourceProfile:
    source_url: str
    accessed_on: date
    public_access_verified: bool
    parser_fixture_verified: bool
    official_difficulty_field: str | None = None
    difficulty_documentation_url: str | None = None

    def __post_init__(self) -> None:
        if not self.source_url.startswith(("https://", "http://")):
            raise ValueError("Fixturekilden kræver en offentlig kilde-URL")
        if self.difficulty_documentation_url is not None and not (
            self.difficulty_documentation_url.startswith(("https://", "http://"))
        ):
            raise ValueError("Difficulty-dokumentation skal være en URL")

    @property
    def verified(self) -> bool:
        return self.public_access_verified and self.parser_fixture_verified

    @property
    def difficulty_verified(self) -> bool:
        return bool(
            self.verified
            and self.official_difficulty_field
            and self.difficulty_documentation_url
        )


@dataclass(frozen=True, slots=True)
class FixtureRecord:
    round_number: int
    team: str
    opponent: str
    home_away: HomeAway
    start_at: datetime
    official_difficulty: float | None = None


@dataclass(frozen=True, slots=True)
class FixtureSnapshot:
    game: GameUrl
    records: tuple[FixtureRecord, ...]
    source: FixtureSourceProfile
    fetched_at: datetime


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise PayloadError("Fixturetidspunktet skal være tekst")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PayloadError("Fixturetidspunktet skal være ISO-formateret") from exc
    return result.astimezone() if result.tzinfo is None else result


def parse_fixture_records(
    payload: object,
    source: FixtureSourceProfile,
) -> tuple[FixtureRecord, ...]:
    """Validate a parser-tested public payload without inventing difficulty."""

    if not source.verified:
        raise PayloadError(
            "Fixturekilden er ikke verificeret som offentlig og parsertestet"
        )
    if not isinstance(payload, list):
        raise PayloadError("Fixturepayloaden skal være en liste")
    records: list[FixtureRecord] = []
    seen: set[tuple[int, str, str, datetime]] = set()
    for index, item in enumerate(payload):
        if not isinstance(item, dict):
            raise PayloadError(f"Fixture {index} skal være et objekt")
        round_number = item.get("round")
        team = item.get("team")
        opponent = item.get("opponent")
        home_away = item.get("home_away")
        if (
            not isinstance(round_number, int)
            or isinstance(round_number, bool)
            or round_number < 1
            or not isinstance(team, str)
            or not team.strip()
            or not isinstance(opponent, str)
            or not opponent.strip()
            or home_away not in {"home", "away", "neutral"}
        ):
            raise PayloadError(f"Fixture {index} mangler validerede kernefelter")
        start_at = _timestamp(item.get("start_at"))
        difficulty = None
        if source.difficulty_verified:
            raw_difficulty = item.get(source.official_difficulty_field)
            if raw_difficulty is not None and (
                not isinstance(raw_difficulty, (int, float))
                or isinstance(raw_difficulty, bool)
            ):
                raise PayloadError(f"Fixture {index} har ugyldig officiel difficulty")
            difficulty = (
                None if raw_difficulty is None else float(raw_difficulty)
            )
        key = (round_number, team.strip().casefold(), opponent.strip().casefold(), start_at)
        if key in seen:
            continue
        seen.add(key)
        records.append(
            FixtureRecord(
                round_number,
                team.strip(),
                opponent.strip(),
                home_away,
                start_at,
                difficulty,
            )
        )
    return tuple(
        sorted(
            records,
            key=lambda item: (
                item.round_number,
                item.start_at,
                item.team.casefold(),
                item.opponent.casefold(),
            ),
        )
    )


class FixtureStore:
    """Atomically cache fixtures written only by an explicit caller action."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def _path(self, game: GameUrl) -> Path:
        locale = sanitize_path_component(game.locale.casefold(), fallback="da")
        slug = sanitize_path_component(game.slug, fallback="game")
        return self.directory / f"{locale}--{slug}.json"

    def save(self, snapshot: FixtureSnapshot) -> Path:
        payload = {
            "schema_version": FIXTURE_CACHE_SCHEMA_VERSION,
            "fetched_at": snapshot.fetched_at.isoformat(),
            "game": {
                "url": snapshot.game.original,
                "locale": snapshot.game.locale,
                "slug": snapshot.game.slug,
            },
            "source": {
                "url": snapshot.source.source_url,
                "accessed_on": snapshot.source.accessed_on.isoformat(),
                "public_access_verified": snapshot.source.public_access_verified,
                "parser_fixture_verified": snapshot.source.parser_fixture_verified,
                "official_difficulty_field": snapshot.source.official_difficulty_field,
                "difficulty_documentation_url": (
                    snapshot.source.difficulty_documentation_url
                ),
            },
            "fixtures": [
                {
                    "round": item.round_number,
                    "team": item.team,
                    "opponent": item.opponent,
                    "home_away": item.home_away,
                    "start_at": item.start_at.isoformat(),
                    "official_difficulty": item.official_difficulty,
                }
                for item in snapshot.records
            ],
        }
        path = self._path(snapshot.game)
        replace_text_atomically(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        return path

    def load(self, game: GameUrl) -> FixtureSnapshot | None:
        path = self._path(game)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PayloadError(f"Fixturecachen kunne ikke læses: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise PayloadError("Ukendt fixtureschema")
        raw_game = payload.get("game")
        raw_source = payload.get("source")
        raw_records = payload.get("fixtures")
        if not isinstance(raw_game, dict) or not isinstance(raw_source, dict):
            raise PayloadError("Fixturecachen mangler spil eller kilde")
        cached_game = GameUrl(
            str(raw_game.get("url", "")),
            str(raw_game.get("locale", "")),
            str(raw_game.get("slug", "")),
        )
        if (cached_game.locale.casefold(), cached_game.slug) != (
            game.locale.casefold(), game.slug
        ):
            raise PayloadError("Fixturecachen tilhører et andet spil")
        try:
            source = FixtureSourceProfile(
                str(raw_source.get("url", "")),
                date.fromisoformat(str(raw_source.get("accessed_on", ""))),
                bool(raw_source.get("public_access_verified", False)),
                bool(raw_source.get("parser_fixture_verified", False)),
                raw_source.get("official_difficulty_field")
                if isinstance(raw_source.get("official_difficulty_field"), str)
                else None,
                raw_source.get("difficulty_documentation_url")
                if isinstance(raw_source.get("difficulty_documentation_url"), str)
                else None,
            )
        except ValueError as exc:
            raise PayloadError("Fixturecachen har ugyldig kildeprovenance") from exc
        if not isinstance(raw_records, list):
            raise PayloadError("Fixturecachen mangler fixturelisten")
        parser_payload = [
            {
                "round": item.get("round"),
                "team": item.get("team"),
                "opponent": item.get("opponent"),
                "home_away": item.get("home_away"),
                "start_at": item.get("start_at"),
                **(
                    {source.official_difficulty_field: item.get("official_difficulty")}
                    if source.official_difficulty_field
                    else {}
                ),
            }
            for item in raw_records
            if isinstance(item, dict)
        ]
        if len(parser_payload) != len(raw_records):
            raise PayloadError("Fixturecachen har en ugyldig fixturepost")
        return FixtureSnapshot(
            cached_game,
            parse_fixture_records(parser_payload, source),
            source,
            aware_local(_timestamp(payload.get("fetched_at"))),
        )
