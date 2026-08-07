"""Explicit on-demand refresh orchestration for games and groups."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .client import HoldetClient
from .groups import GroupDefinition, GroupTeam, ManagerGame
from .models import GameUrl, ScrapedTeam
from .storage import (
    ManifestStore,
    PlayerStatisticsStore,
    SnapshotIndex,
    SnapshotStore,
)
from .hub_settings import HubSettings
from .analysis_inbox import (
    AnalysisInboxStore,
    build_watchlist_alerts,
    select_alert_baseline,
)
from .tournament import build_tournament_state, latest_tournament_round


@dataclass(frozen=True, slots=True)
class TeamRefresh:
    team_id: int
    team_name: str
    status: str
    snapshot_path: Path | None
    team: ScrapedTeam | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PlayerRefresh:
    status: str
    round_number: int | None
    snapshot_path: Path | None
    alert_count: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class GameRefreshResult:
    manager_game: ManagerGame
    round_number: int
    generated_at: datetime
    teams: tuple[TeamRefresh, ...]
    attempted_team_ids: tuple[int, ...]
    skipped_team_ids: tuple[int, ...]
    manifest_path: Path
    player: PlayerRefresh | None = None

    @property
    def failures(self) -> tuple[TeamRefresh, ...]:
        return tuple(item for item in self.teams if item.status != "success")


@dataclass(frozen=True, slots=True)
class GroupRefreshResult:
    group: GroupDefinition
    round_number: int
    generated_at: datetime
    teams: tuple[TeamRefresh, ...]
    manifest_path: Path

    @property
    def failures(self) -> tuple[TeamRefresh, ...]:
        return tuple(item for item in self.teams if item.status != "success")


def _game_identity(game: GameUrl) -> tuple[str, str]:
    return game.locale.casefold(), game.slug

def _group_refresh_state(
    group: GroupDefinition, snapshots: SnapshotIndex
) -> dict[str, object]:
    member_ids = {member.team_id for member in group.teams}
    if group.kind != "tournament":
        return {
            "id": group.group_id,
            "name": group.name,
            "type": group.kind,
            "revision": None,
            "phase": None,
            "latest_round": max(
                (
                    snapshot.team.overview.current_round
                    for team_id in member_ids
                    if (snapshot := snapshots.newest(group.game, team_id))
                    is not None
                ),
                default=0,
            ),
            "active_team_ids": sorted(member_ids),
            "eliminated_team_ids": [],
            "champion_id": None,
            "champion_name": None,
        }
    latest_round = latest_tournament_round(group, snapshots)
    state = build_tournament_state(group, snapshots, latest_round)
    champion = next(
        (row.team_name for row in state.standings if row.team_id == state.champion_id),
        None,
    )
    return {
        "id": group.group_id,
        "name": group.name,
        "type": group.kind,
        "revision": group.active_revision,
        "phase": state.phase,
        "latest_round": latest_round,
        "active_team_ids": sorted(state.active_team_ids),
        "eliminated_team_ids": sorted(state.eliminated_team_ids),
        "champion_id": state.champion_id,
        "champion_name": champion,
    }


def refresh_game(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    client: HoldetClient,
    snapshot_store: SnapshotStore,
    manifest_store: ManifestStore,
    *,
    now: datetime | None = None,
) -> GameRefreshResult:
    """Refresh every fixed team in a manager game at most once."""

    identity = manager_game.identity
    selected_groups = tuple(
        group for group in groups if _game_identity(group.game) == identity
    )
    generated_at = now or datetime.now().astimezone()
    if generated_at.tzinfo is None:
        generated_at = generated_at.astimezone()
    before = snapshot_store.scan()
    members: dict[int, GroupTeam] = {}
    for group in selected_groups:
        for member in group.teams:
            existing = members.get(member.team_id)
            if existing is None or (
                existing.account_key == "direct" and member.account_key != "direct"
            ):
                members[member.team_id] = member

    attempted = tuple(sorted(members))
    results: list[TeamRefresh] = []
    for team_id in attempted:
        member = members[team_id]
        try:
            team = client.fetch_team(member.reference(manager_game.game))
            path = snapshot_store.save_team_json(team, now=generated_at)
            results.append(TeamRefresh(team_id, team.team_name, "success", path, team))
        except Exception as exc:
            cached = before.newest(manager_game.game, team_id)
            if cached is not None:
                results.append(
                    TeamRefresh(
                        team_id, cached.team.team_name, "cached_fallback",
                        cached.path, cached.team, str(exc),
                    )
                )
            else:
                results.append(
                    TeamRefresh(team_id, member.name, "failed", None, None, str(exc))
                )

    after = snapshot_store.scan()
    round_number = max(
        (
            snapshot.team.overview.current_round
            for team_id in attempted
            if (snapshot := after.newest(manager_game.game, team_id)) is not None
        ),
        default=0,
    )
    group_states = [_group_refresh_state(group, after) for group in selected_groups]
    manifest = {
        "schema_version": 1,
        "scope": "game",
        "generated_at": generated_at.isoformat(),
        "game": {
            "name": manager_game.name,
            "url": manager_game.game.original,
            "locale": manager_game.game.locale,
            "slug": manager_game.game.slug,
        },
        "round": round_number,
        "groups": group_states,
        "attempted_team_ids": list(attempted),
        "skipped_team_ids": [],
        "teams": [
            {
                "team_id": item.team_id,
                "team_name": item.team_name,
                "status": item.status,
                "snapshot_path": str(item.snapshot_path) if item.snapshot_path else None,
                "error": item.error,
            }
            for item in results
        ],
    }
    manifest_path = manifest_store.save_game_manifest(
        manager_game.game.slug, round_number, manifest, now=generated_at
    )
    return GameRefreshResult(
        manager_game, round_number, generated_at, tuple(results),
        attempted, (), manifest_path,
    )


def refresh_manager_game(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    client: HoldetClient,
    snapshot_store: SnapshotStore,
    player_store: PlayerStatisticsStore,
    manifest_store: ManifestStore,
    *,
    settings: HubSettings | None = None,
    inbox_store: AnalysisInboxStore | None = None,
    now: datetime | None = None,
) -> GameRefreshResult:
    """Refresh latest players once plus every fixed team, with partial results."""

    generated_at = now or datetime.now().astimezone()
    if generated_at.tzinfo is None:
        generated_at = generated_at.astimezone()
    before_players = player_store.scan(manager_game.game)
    try:
        statistics = client.fetch_players(manager_game.game)
        previous = select_alert_baseline(
            before_players,
            manager_game.game,
            statistics.round_number,
        )
        player_path = player_store.save(statistics, now=generated_at)
        current = player_store.scan(manager_game.game).newest(
            manager_game.game, statistics.round_number
        )
        alert_count = 0
        if (
            previous is not None
            and current is not None
            and settings is not None
            and inbox_store is not None
        ):
            alerts = build_watchlist_alerts(
                previous,
                current,
                settings.watchlist,
                now=generated_at,
            )
            inbox_store.merge(alerts)
            alert_count = len(alerts)
        player_refresh = PlayerRefresh(
            "success",
            statistics.round_number,
            player_path,
            alert_count,
        )
    except Exception as exc:
        cached_rounds = before_players.rounds_for(manager_game.game)
        previous = (
            before_players.newest(manager_game.game, max(cached_rounds))
            if cached_rounds
            else None
        )
        player_refresh = PlayerRefresh(
            "cached_fallback" if previous is not None else "failed",
            None if previous is None else previous.statistics.round_number,
            None if previous is None else previous.path,
            error=str(exc),
        )
    result = refresh_game(
        manager_game,
        groups,
        client,
        snapshot_store,
        manifest_store,
        now=generated_at,
    )
    return replace(result, player=player_refresh)


def refresh_group(
    group: GroupDefinition,
    client: HoldetClient,
    snapshot_store: SnapshotStore,
    manifest_store: ManifestStore,
    *,
    now: datetime | None = None,
) -> GroupRefreshResult:
    """Refresh every fixed member once and record the post-refresh state."""

    generated_at = now or datetime.now().astimezone()
    if generated_at.tzinfo is None:
        generated_at = generated_at.astimezone()
    before = snapshot_store.scan()
    results: list[TeamRefresh] = []
    for member in group.teams:
        try:
            team = client.fetch_team(member.reference(group.game))
            path = snapshot_store.save_team_json(team, now=generated_at)
            results.append(
                TeamRefresh(member.team_id, team.team_name, "success", path, team)
            )
        except Exception as exc:
            cached = before.newest(group.game, member.team_id)
            if cached is not None:
                results.append(
                    TeamRefresh(
                        member.team_id, cached.team.team_name, "cached_fallback",
                        cached.path, cached.team, str(exc),
                    )
                )
            else:
                results.append(
                    TeamRefresh(member.team_id, member.name, "failed", None, None, str(exc))
                )

    after = snapshot_store.scan()
    round_number = max(
        (
            snapshot.team.overview.current_round
            for member in group.teams
            if (snapshot := after.newest(group.game, member.team_id)) is not None
        ),
        default=0,
    )
    post_state = _group_refresh_state(group, after)
    manifest = {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "group": {
            "id": group.group_id,
            "name": group.name,
            "game_slug": group.game.slug,
            "type": group.kind,
            "revision": group.active_revision if group.kind == "tournament" else None,
        },
        "phase": post_state["phase"],
        "round": round_number,
        "latest_round": post_state["latest_round"],
        "active_team_ids": post_state["active_team_ids"],
        "eliminated_team_ids": post_state["eliminated_team_ids"],
        "champion_id": post_state["champion_id"],
        "champion_name": post_state["champion_name"],
        "attempted_team_ids": [member.team_id for member in group.teams],
        "skipped_team_ids": [],
        "teams": [
            {
                "team_id": item.team_id,
                "team_name": item.team_name,
                "status": item.status,
                "snapshot_path": str(item.snapshot_path) if item.snapshot_path else None,
                "error": item.error,
            }
            for item in results
        ],
    }
    manifest_path = manifest_store.save_manifest(
        group.game.slug, group.group_id, round_number, manifest, now=generated_at
    )
    return GroupRefreshResult(
        group, round_number, generated_at, tuple(results), manifest_path
    )
