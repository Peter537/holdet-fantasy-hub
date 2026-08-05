"""Pure serializers for schema-versioned team snapshots."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any, cast

from .errors import PayloadError
from .models import (
    GameUrl,
    RosterEntry,
    RoundStatus,
    RoundSummary,
    ScrapedTeam,
    TeamOverview,
    TeamReference,
)
from .output import team_to_dict


SCHEMA_VERSION = 2


def team_to_json(team: ScrapedTeam, *, generated_at: datetime) -> str:
    """Serialize a team snapshot as indented UTF-8-safe JSON text."""

    return json.dumps(
        team_to_dict(team, generated_at=generated_at),
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def _object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PayloadError(f"Snapshottets {label} skal være et objekt")
    return value


def _list(value: object, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise PayloadError(f"Snapshottets {label} skal være en liste")
    return value


def _integer(value: object, label: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise PayloadError(f"Snapshottets {label} skal være et heltal")
    return value


def _text(value: object, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PayloadError(f"Snapshottets {label} skal være udfyldt tekst")
    return value


def _round_status(value: object, label: str) -> RoundStatus:
    if value not in {"complete", "in_progress", "unknown"}:
        raise PayloadError(f"Snapshottets {label} har en ugyldig rundestatus")
    return cast(RoundStatus, value)


def _datetime(
    value: object, label: str, *, optional: bool = False
) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PayloadError(f"Snapshottets {label} skal være et ISO-tidspunkt")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PayloadError(f"Snapshottets {label} skal være et ISO-tidspunkt") from exc


def _round_from_dict(raw: object, *, schema_version: int) -> RoundSummary:
    item = _object(raw, "history item")
    return RoundSummary(
        round_number=_integer(item.get("round"), "history.round"),
        round_status=(
            _round_status(item.get("round_status"), "history.round_status")
            if schema_version >= 2
            else "unknown"
        ),
        round_end_at=(
            _datetime(
                item.get("round_end_at"),
                "history.round_end_at",
                optional=True,
            )
            if schema_version >= 2
            else None
        ),
        total=_integer(item.get("total"), "history.total"),
        change=_integer(item.get("change"), "history.change"),
        bank=_integer(item.get("bank"), "history.bank", optional=True),
        player_value=_integer(
            item.get("player_value"), "history.player_value", optional=True
        ),
        bank_change=_integer(
            item.get("bank_change"), "history.bank_change", optional=True
        ),
        interest=_integer(item.get("interest"), "history.interest", optional=True),
        player_change=_integer(item.get("player_change"), "history.player_change"),
        transfer=_integer(item.get("transfer"), "history.transfer", optional=True),
        captain_bonus=_integer(item.get("captain_bonus"), "history.captain_bonus"),
        special_bonus=_integer(item.get("special_bonus"), "history.special_bonus"),
        substitutions_used=_integer(
            item.get("substitutions_used"),
            "history.substitutions_used",
            optional=True,
        ),
        round_rank=_integer(item.get("round_rank"), "history.round_rank", optional=True),
        overall_rank=_integer(
            item.get("overall_rank"), "history.overall_rank", optional=True
        ),
        round_rank_change=_integer(
            item.get("round_rank_change"),
            "history.round_rank_change",
            optional=True,
        ),
        overall_rank_change=_integer(
            item.get("overall_rank_change"),
            "history.overall_rank_change",
            optional=True,
        ),
    )


def _roster_from_dict(raw: object) -> RosterEntry:
    item = _object(raw, "roster item")
    statuses = item.get("statuses", [])
    if not isinstance(statuses, list) or not all(isinstance(value, str) for value in statuses):
        raise PayloadError("Snapshottets roster.statuses skal være en liste med tekstværdier")
    status_set = set(statuses)
    return RosterEntry(
        source_index=_integer(item.get("source_index"), "roster.source_index"),
        player_id=_integer(item.get("player_id"), "roster.player_id"),
        name=_text(item.get("name"), "roster.name"),
        team=_text(item.get("team"), "roster.team"),
        position=_text(item.get("position"), "roster.position"),
        value=_integer(item.get("value"), "roster.value"),
        round_change=_integer(item.get("round_change"), "roster.round_change"),
        since_purchase_change=_integer(
            item.get("since_purchase_change"), "roster.since_purchase_change"
        ),
        purchase_round=_integer(
            item.get("purchase_round"), "roster.purchase_round", optional=True
        ),
        role=_text(item.get("role"), "roster.role"),
        is_active="inactive" not in status_set,
        is_disabled="disabled" in status_set,
        is_injured="injured" in status_set,
        has_suspension="suspended" in status_set,
    )


def team_from_dict(payload: object) -> ScrapedTeam:
    """Deserialize and validate a compatible team snapshot."""

    root = _object(payload, "root")
    schema_version = root.get("schema_version")
    if schema_version not in {1, SCHEMA_VERSION}:
        raise PayloadError(
            f"Ikke-understøttet skema for holdsnapshot: {schema_version!r}"
        )
    source = _object(root.get("source"), "source")
    game_data = _object(root.get("game"), "game")
    account = _object(root.get("account"), "account")
    team_data = _object(root.get("team"), "team")
    overview_data = _object(root.get("overview"), "overview")
    substitutions = _object(overview_data.get("substitutions"), "substitutions")
    game = GameUrl(
        original=_text(source.get("game_url"), "source.game_url"),
        locale=_text(game_data.get("locale"), "game.locale"),
        slug=_text(game_data.get("slug"), "game.slug"),
    )
    reference = TeamReference(
        game=game,
        team_id=_integer(team_data.get("id"), "team.id"),
        team_name=_text(team_data.get("name"), "team.name"),
        source_url=_text(source.get("team_url"), "source.team_url"),
        account_key=_text(account.get("key"), "account.key"),
        account_label=_text(account.get("label"), "account.label"),
        account_user_id=_integer(
            account.get("configured_user_id"),
            "account.configured_user_id",
            optional=True,
        ),
        profile_url=_text(source.get("profile_url"), "source.profile_url", optional=True),
    )
    overview = TeamOverview(
        current_round=_integer(game_data.get("current_round"), "game.current_round"),
        unit=_text(game_data.get("unit"), "game.unit"),
        player_value=_integer(
            overview_data.get("player_value"), "overview.player_value", optional=True
        ),
        bank=_integer(overview_data.get("bank"), "overview.bank", optional=True),
        total=_integer(overview_data.get("total"), "overview.total", optional=True),
        current_change=_integer(
            overview_data.get("current_change"),
            "overview.current_change",
            optional=True,
        ),
        rank=_integer(overview_data.get("rank"), "overview.rank", optional=True),
        rank_change=_integer(
            overview_data.get("rank_change"), "overview.rank_change", optional=True
        ),
        top_percent=_integer(
            overview_data.get("top_percent"), "overview.top_percent", optional=True
        ),
        substitutions_remaining=_integer(
            substitutions.get("remaining"), "substitutions.remaining", optional=True
        ),
        substitutions_limit=_integer(
            substitutions.get("limit"), "substitutions.limit", optional=True
        ),
        substitutions_used=_integer(
            substitutions.get("used"), "substitutions.used", optional=True
        ),
    )
    roster = tuple(_roster_from_dict(item) for item in _list(root.get("roster"), "roster"))
    assert isinstance(schema_version, int)
    history = tuple(
        _round_from_dict(item, schema_version=schema_version)
        for item in _list(root.get("history"), "history")
    )
    return ScrapedTeam(
        reference=reference,
        variant=_text(game_data.get("variant"), "game.variant"),
        game_id=_integer(game_data.get("game_id"), "game.game_id"),
        team_name=reference.team_name,
        owner_name=_text(account.get("owner_name"), "account.owner_name"),
        owner_user_id=_integer(
            account.get("owner_user_id"), "account.owner_user_id", optional=True
        ),
        overview=overview,
        roster=roster,
        history=history,
    )
