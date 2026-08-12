"""Pure serializers for schema-versioned player-statistics snapshots."""

from __future__ import annotations

from datetime import datetime
import json
from math import isfinite
from typing import Any, cast

from .errors import PayloadError
from .models import (
    GameUrl,
    PlayerEntry,
    PlayerPerformanceStat,
    RoundStatus,
    ScrapedGame,
)
from .policies import legacy_policy


PLAYER_STATISTICS_SCHEMA_VERSION = 4


def _statuses(entry: PlayerEntry) -> list[str]:
    values: list[str] = []
    if not entry.is_active:
        values.append("inactive")
    if entry.is_disabled:
        values.append("disabled")
    if entry.is_injured:
        values.append("injured")
    if entry.has_suspension:
        values.append("suspended")
    return values


def player_statistics_to_dict(
    game: ScrapedGame, *, generated_at: datetime
) -> dict[str, object]:
    """Return the stable public JSON representation of one statistics round."""

    return {
        "schema_version": PLAYER_STATISTICS_SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "source": {"game_url": game.game.original},
        "game": {
            "locale": game.game.locale,
            "slug": game.game.slug,
            "variant": game.variant,
            "format": game.format,
            "unit": game.unit,
            "round": game.round_number,
            "round_status": game.round_status,
            "round_end_at": (
                game.round_end_at.isoformat()
                if game.round_end_at is not None
                else None
            ),
        },
        "entries": [
            {
                "source_index": entry.source_index,
                "entry_id": entry.entry_id,
                "person_id": entry.person_id,
                "name": entry.name,
                "team": entry.team,
                "position": entry.position,
                "value": entry.value,
                "total_growth": entry.total_growth,
                "round_growth": entry.round_growth,
                "popularity": entry.popularity,
                "popularity_change": entry.popularity_change,
                "trend": entry.trend,
                "index": entry.index,
                "stats": [
                    {"name": item.name, "value": item.value}
                    for item in entry.stats
                ],
                "total_stats": [
                    {"name": item.name, "value": item.value}
                    for item in entry.total_stats
                ],
                "statuses": _statuses(entry),
            }
            for entry in game.entries
        ],
    }


def player_statistics_to_json(
    game: ScrapedGame, *, generated_at: datetime
) -> str:
    return json.dumps(
        player_statistics_to_dict(game, generated_at=generated_at),
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PayloadError(f"Spillersnapshottets {label} skal være et objekt")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PayloadError(f"Spillersnapshottets {label} skal være udfyldt tekst")
    return value


def _integer(value: object, label: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise PayloadError(f"Spillersnapshottets {label} skal være et heltal")
    return value


def _number(value: object, label: str, *, optional: bool = False) -> float | None:
    if value is None and optional:
        return None
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise PayloadError(f"Spillersnapshottets {label} skal være et tal")
    result = float(value)
    if not isfinite(result):
        raise PayloadError(f"Spillersnapshottets {label} skal være et endeligt tal")
    return result


def _performance_stats(value: object, label: str) -> tuple[PlayerPerformanceStat, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PayloadError(f"Spillersnapshottets {label} skal være en liste")
    result: list[PlayerPerformanceStat] = []
    seen: set[str] = set()
    for index, raw in enumerate(value):
        item = _object(raw, f"{label}[{index}]")
        name = _text(item.get("name"), f"{label}[{index}].name").strip()
        if len(name) > 80 or any(ord(character) < 32 for character in name):
            raise PayloadError(
                f"Spillersnapshottets {label}[{index}].name er ugyldigt"
            )
        normalized = name.casefold()
        if normalized in seen:
            raise PayloadError(f"Spillersnapshottets {label} har dublerede statnavne")
        seen.add(normalized)
        number = _number(item.get("value"), f"{label}[{index}].value")
        assert number is not None
        result.append(PlayerPerformanceStat(name, number))
    return tuple(result)


def _round_status(value: object) -> RoundStatus:
    if value not in {"complete", "in_progress", "unknown"}:
        raise PayloadError("Spillersnapshottets game.round_status er ugyldig")
    return cast(RoundStatus, value)


def _datetime(value: object, label: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PayloadError(f"Spillersnapshottets {label} skal være et ISO-tidspunkt")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PayloadError(
            f"Spillersnapshottets {label} skal være et ISO-tidspunkt"
        ) from exc


def player_statistics_from_dict(payload: object) -> ScrapedGame:
    """Deserialize and validate a compatible player-statistics snapshot."""

    root = _object(payload, "root")
    schema_version = root.get("schema_version")
    if schema_version not in {1, 2, 3, PLAYER_STATISTICS_SCHEMA_VERSION}:
        raise PayloadError(
            "Ikke-understøttet skema for spillersnapshot: "
            f"{schema_version!r}"
        )
    source = _object(root.get("source"), "source")
    game_data = _object(root.get("game"), "game")
    raw_entries = root.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise PayloadError("Spillersnapshottets entries skal være en udfyldt liste")
    round_number = _integer(game_data.get("round"), "game.round")
    assert round_number is not None
    if round_number < 0:
        raise PayloadError("Spillersnapshottets runde må ikke være negativ")
    game = GameUrl(
        original=_text(source.get("game_url"), "source.game_url"),
        locale=_text(game_data.get("locale"), "game.locale"),
        slug=_text(game_data.get("slug"), "game.slug"),
    )
    entries: list[PlayerEntry] = []
    for index, raw in enumerate(raw_entries):
        item = _object(raw, f"entries[{index}]")
        statuses = item.get("statuses", [])
        if not isinstance(statuses, list) or not all(
            isinstance(value, str) for value in statuses
        ):
            raise PayloadError(
                f"Spillersnapshottets entries[{index}].statuses skal være en liste med tekstværdier"
            )
        status_set = set(statuses)
        entries.append(
            PlayerEntry(
                source_index=_integer(
                    item.get("source_index"), f"entries[{index}].source_index"
                ),
                name=_text(item.get("name"), f"entries[{index}].name"),
                team=_text(item.get("team"), f"entries[{index}].team"),
                position=_text(item.get("position"), f"entries[{index}].position"),
                value=_integer(item.get("value"), f"entries[{index}].value"),
                is_active="inactive" not in status_set,
                is_disabled="disabled" in status_set,
                is_injured="injured" in status_set,
                has_suspension="suspended" in status_set,
                entry_id=_integer(
                    item.get("entry_id"), f"entries[{index}].entry_id", optional=True
                ),
                person_id=_integer(
                    item.get("person_id"), f"entries[{index}].person_id", optional=True
                ),
                total_growth=_integer(
                    item.get("total_growth"),
                    f"entries[{index}].total_growth",
                    optional=True,
                ),
                round_growth=_integer(
                    item.get("round_growth"),
                    f"entries[{index}].round_growth",
                    optional=True,
                ),
                popularity=(
                    _number(
                        item.get("popularity"),
                        f"entries[{index}].popularity",
                        optional=True,
                    )
                    if schema_version >= 4
                    else None
                ),
                popularity_change=(
                    _number(
                        item.get("popularity_change"),
                        f"entries[{index}].popularity_change",
                        optional=True,
                    )
                    if schema_version >= 4
                    else None
                ),
                trend=(
                    _number(
                        item.get("trend"),
                        f"entries[{index}].trend",
                        optional=True,
                    )
                    if schema_version >= 4
                    else None
                ),
                index=(
                    _number(
                        item.get("index"),
                        f"entries[{index}].index",
                        optional=True,
                    )
                    if schema_version >= 4
                    else None
                ),
                stats=(
                    _performance_stats(
                        item.get("stats", []), f"entries[{index}].stats"
                    )
                    if schema_version >= 4
                    else ()
                ),
                total_stats=(
                    _performance_stats(
                        item.get("total_stats", []),
                        f"entries[{index}].total_stats",
                    )
                    if schema_version >= 4
                    else ()
                ),
            )
        )
    variant = _text(game_data.get("variant"), "game.variant")
    if schema_version == 1:
        if variant in {"cycling", "cycling_world_tour"}:
            raise PayloadError(
                "Det ældre cykelspillersnapshot har en ukendt værdienhed; "
                "hent denne runde igen"
            )
        policy = legacy_policy(variant)
        game_format = policy.format
        unit = policy.unit
    else:
        game_format = _text(game_data.get("format"), "game.format")
        unit = _text(game_data.get("unit"), "game.unit")
    round_status = (
        _round_status(game_data.get("round_status"))
        if schema_version >= 3
        else "unknown"
    )
    round_end_at = (
        _datetime(game_data.get("round_end_at"), "game.round_end_at")
        if schema_version >= 3
        else None
    )
    return ScrapedGame(
        game=game,
        variant=variant,
        round_number=round_number,
        entries=tuple(entries),
        format=game_format,
        unit=unit,
        round_status=round_status,
        round_end_at=round_end_at,
    )

