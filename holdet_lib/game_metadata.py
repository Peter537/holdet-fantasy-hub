"""Cached, versioned game metadata written only after explicit fetches."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any

from .errors import PayloadError
from .models import GameUrl, ScheduleRound
from .output import sanitize_path_component
from .persistence import aware_local, replace_text_atomically
from .teams import GameContext


GAME_METADATA_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class GameMetadata:
    game: GameUrl
    variant: str
    format: str
    game_id: int
    salary_cap: int
    final_round: int | None
    display_name: str | None
    rounds: tuple[ScheduleRound, ...]
    fetched_at: datetime

    @property
    def identity(self) -> tuple[str, str]:
        return self.game.locale.casefold(), self.game.slug

    @property
    def unit(self) -> str:
        return "money" if self.salary_cap > 0 else "points"

    @property
    def active_round(self) -> int | None:
        now = datetime.now().astimezone()
        active = [item.round_number for item in self.rounds if item.start <= now <= item.end]
        return max(active, default=None)

    @property
    def next_deadline(self) -> datetime | None:
        now = datetime.now().astimezone()
        future = [item.close for item in self.rounds if item.close > now]
        return min(future, default=None)


def game_metadata_from_context(
    context: GameContext, *, fetched_at: datetime | None = None
) -> GameMetadata:
    return GameMetadata(
        context.game,
        context.variant,
        context.format,
        context.game_id,
        context.salary_cap,
        context.final_round,
        context.display_name,
        context.rounds,
        aware_local(fetched_at),
    )


def _metadata_to_dict(metadata: GameMetadata) -> dict[str, object]:
    return {
        "schema_version": GAME_METADATA_SCHEMA_VERSION,
        "fetched_at": metadata.fetched_at.isoformat(),
        "game": {
            "url": metadata.game.original,
            "locale": metadata.game.locale,
            "slug": metadata.game.slug,
            "variant": metadata.variant,
            "format": metadata.format,
            "game_id": metadata.game_id,
            "salary_cap": metadata.salary_cap,
            "final_round": metadata.final_round,
            "display_name": metadata.display_name,
        },
        "rounds": [
            {
                "round": item.round_number,
                "start": item.start.isoformat(),
                "close": item.close.isoformat(),
                "end": item.end.isoformat(),
            }
            for item in metadata.rounds
        ],
    }


def _text(value: object, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PayloadError(f"Metadatafeltet {label} skal være tekst")
    return value.strip()


def _integer(value: object, label: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise PayloadError(f"Metadatafeltet {label} skal være et heltal")
    return value


def _timestamp(value: object, label: str) -> datetime:
    text = _text(value, label)
    assert text is not None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PayloadError(f"Metadatafeltet {label} skal være et ISO-tidspunkt") from exc
    return parsed.astimezone() if parsed.tzinfo is None else parsed


def _metadata_from_dict(payload: object) -> GameMetadata:
    if not isinstance(payload, dict):
        raise PayloadError("Metadataroden skal være et objekt")
    if payload.get("schema_version") != GAME_METADATA_SCHEMA_VERSION:
        raise PayloadError("Ukendt skema for spilmetadata")
    game = payload.get("game")
    rounds = payload.get("rounds")
    if not isinstance(game, dict) or not isinstance(rounds, list):
        raise PayloadError("Spilmetadata mangler game eller rounds")
    parsed_rounds: list[ScheduleRound] = []
    for index, raw in enumerate(rounds):
        if not isinstance(raw, dict):
            raise PayloadError(f"Metadatafeltet rounds[{index}] skal være et objekt")
        round_number = _integer(raw.get("round"), "round")
        assert round_number is not None
        parsed_rounds.append(
            ScheduleRound(
                round_number,
                _timestamp(raw.get("start"), "start"),
                _timestamp(raw.get("close"), "close"),
                _timestamp(raw.get("end"), "end"),
            )
        )
    url = _text(game.get("url"), "game.url")
    locale = _text(game.get("locale"), "game.locale")
    slug = _text(game.get("slug"), "game.slug")
    variant = _text(game.get("variant"), "game.variant")
    game_format = _text(game.get("format"), "game.format")
    game_id = _integer(game.get("game_id"), "game.game_id")
    salary_cap = _integer(game.get("salary_cap"), "game.salary_cap")
    assert all(value is not None for value in (url, locale, slug, variant, game_format))
    assert game_id is not None and salary_cap is not None
    return GameMetadata(
        GameUrl(url, locale, slug),
        variant,
        game_format,
        game_id,
        salary_cap,
        _integer(game.get("final_round"), "game.final_round", optional=True),
        _text(game.get("display_name"), "game.display_name", optional=True),
        tuple(sorted(parsed_rounds, key=lambda item: item.round_number)),
        _timestamp(payload.get("fetched_at"), "fetched_at"),
    )


class GameMetadataStore:
    """Maintain one current metadata document per game, without implicit writes."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def _path(self, game: GameUrl) -> Path:
        locale = sanitize_path_component(game.locale.casefold(), fallback="da")
        slug = sanitize_path_component(game.slug, fallback="game")
        return self.directory / f"{locale}--{slug}.json"

    def save(
        self, value: GameMetadata | GameContext, *, fetched_at: datetime | None = None
    ) -> GameMetadata:
        metadata = (
            value
            if isinstance(value, GameMetadata)
            else game_metadata_from_context(value, fetched_at=fetched_at)
        )
        replace_text_atomically(
            self._path(metadata.game),
            json.dumps(_metadata_to_dict(metadata), ensure_ascii=False, indent=2) + "\n",
        )
        return metadata

    def load(self, game: GameUrl) -> GameMetadata | None:
        path = self._path(game)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PayloadError(f"Spilmetadata kunne ikke læses: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise PayloadError("Spilmetadata indeholder ugyldig JSON") from exc
        metadata = _metadata_from_dict(payload)
        if metadata.identity != (game.locale.casefold(), game.slug):
            raise PayloadError("Spilmetadata tilhører et andet spil")
        return metadata

    def scan(self) -> tuple[tuple[GameMetadata, ...], tuple[str, ...]]:
        if not self.directory.exists():
            return (), ()
        values: list[GameMetadata] = []
        warnings: list[str] = []
        for path in self.directory.glob("*.json"):
            try:
                values.append(_metadata_from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, PayloadError, ValueError) as exc:
                warnings.append(f"{path}: {exc}")
        values.sort(key=lambda item: (item.fetched_at, item.game.slug), reverse=True)
        return tuple(values), tuple(warnings)

