"""Pure serializers for schema-versioned player-statistics snapshots."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from .errors import PayloadError
from .models import GameUrl, PlayerEntry, ScrapedGame
from .policies import legacy_policy


PLAYER_STATISTICS_SCHEMA_VERSION = 2


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
        raise PayloadError(f"player snapshot {label} must be an object")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PayloadError(f"player snapshot {label} must be non-empty text")
    return value


def _integer(value: object, label: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise PayloadError(f"player snapshot {label} must be an integer")
    return value


def player_statistics_from_dict(payload: object) -> ScrapedGame:
    """Deserialize and validate a player-statistics schema-version 1 snapshot."""

    root = _object(payload, "root")
    schema_version = root.get("schema_version")
    if schema_version not in {1, PLAYER_STATISTICS_SCHEMA_VERSION}:
        raise PayloadError(
            "unsupported player snapshot schema: "
            f"{schema_version!r}"
        )
    source = _object(root.get("source"), "source")
    game_data = _object(root.get("game"), "game")
    raw_entries = root.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise PayloadError("player snapshot entries must be a non-empty list")
    round_number = _integer(game_data.get("round"), "game.round")
    assert round_number is not None
    if round_number < 0:
        raise PayloadError("player snapshot round must be non-negative")
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
                f"player snapshot entries[{index}].statuses must be a string list"
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
            )
        )
    variant = _text(game_data.get("variant"), "game.variant")
    if schema_version == 1:
        if variant in {"cycling", "cycling_world_tour"}:
            raise PayloadError(
                "legacy cycling player snapshot has an unknown value unit; "
                "fetch this round again"
            )
        policy = legacy_policy(variant)
        game_format = policy.format
        unit = policy.unit
    else:
        game_format = _text(game_data.get("format"), "game.format")
        unit = _text(game_data.get("unit"), "game.unit")
    return ScrapedGame(
        game=game,
        variant=variant,
        round_number=round_number,
        entries=tuple(entries),
        format=game_format,
        unit=unit,
    )

