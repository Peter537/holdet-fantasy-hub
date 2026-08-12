"""Pure snapshot differences, history series and data quality reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Literal

from .groups import GroupDefinition, ManagerGame
from .hub_settings import player_identity
from .models import GameUrl, PlayerEntry, RoundStatus
from .standings import build_standings
from .storage import (
    ManifestStore,
    PlayerStatisticsIndex,
    PlayerStatisticsSnapshot,
    SnapshotIndex,
    TeamSnapshot,
)


@dataclass(frozen=True, slots=True)
class PlayerSnapshotChange:
    player_key: str
    name: str
    team: str
    position: str
    old_value: int | None
    new_value: int | None
    old_statuses: tuple[str, ...]
    new_statuses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TeamRankChange:
    team_id: int
    team_name: str
    old_rank: int | None
    new_rank: int | None
    movement: int | None
    old_generated_at: datetime
    new_generated_at: datetime


@dataclass(frozen=True, slots=True)
class SnapshotDiff:
    game: GameUrl
    previous_generated_at: datetime
    current_generated_at: datetime
    added_players: tuple[PlayerSnapshotChange, ...] = ()
    removed_players: tuple[PlayerSnapshotChange, ...] = ()
    price_changes: tuple[PlayerSnapshotChange, ...] = ()
    status_changes: tuple[PlayerSnapshotChange, ...] = ()
    rank_changes: tuple[TeamRankChange, ...] = ()
    previous_round: int | None = None
    current_round: int | None = None
    previous_round_status: RoundStatus = "unknown"
    current_round_status: RoundStatus = "unknown"

    @property
    def is_final(self) -> bool:
        return (
            self.previous_round_status == "complete"
            and self.current_round_status == "complete"
        )


@dataclass(frozen=True, slots=True)
class IntraRoundDiff:
    """The two newest chronological player fetches, including within a round."""

    diff: SnapshotDiff
    same_round: bool


@dataclass(frozen=True, slots=True)
class TeamSnapshotDiff:
    game: GameUrl
    team_id: int
    team_name: str
    previous_round: int
    current_round: int
    previous_generated_at: datetime
    current_generated_at: datetime
    previous_round_status: RoundStatus
    current_round_status: RoundStatus
    old_total: int | None
    new_total: int | None
    old_player_value: int | None
    new_player_value: int | None
    old_rank: int | None
    new_rank: int | None
    rank_movement: int | None
    added_players: tuple[str, ...] = ()
    removed_players: tuple[str, ...] = ()
    roster_comparable: bool = False

    @property
    def is_final(self) -> bool:
        return (
            self.previous_round_status == "complete"
            and self.current_round_status == "complete"
        )


@dataclass(frozen=True, slots=True)
class HistoryPoint:
    round_number: int
    team_id: int
    team_name: str
    total: int | None
    round_growth: int | None
    overall_rank: int | None
    group_rank: int | None


@dataclass(frozen=True, slots=True)
class PlayerHistoryPoint:
    round_number: int
    player_key: str
    name: str
    value: int | None
    total_growth: int | None
    round_growth: int | None
    statuses: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class DataQualityRound:
    game_slug: str
    round_number: int
    expected_teams: int
    team_snapshots: int
    player_snapshot: bool
    complete: int
    in_progress: int
    unknown: int
    missing_team_ids: tuple[int, ...]
    game_locale: str = ""
    game_name: str = ""
    missing_team_names: tuple[str, ...] = ()
    player_round_status: RoundStatus | None = None
    readiness: Literal["ready", "preliminary", "missing", "error"] = "missing"
    reasons: tuple[str, ...] = ()
    newest_team_data: datetime | None = None
    newest_player_data: datetime | None = None
    last_success: datetime | None = None
    last_error: datetime | None = None
    last_error_message: str | None = None


    @property
    def coverage(self) -> float:
        return (
            self.team_snapshots / self.expected_teams
            if self.expected_teams
            else 0.0
        )


@dataclass(frozen=True, slots=True)
class DataQualityReport:
    generated_at: datetime
    rounds: tuple[DataQualityRound, ...]
    newest_team_data: datetime | None
    newest_player_data: datetime | None
    last_success: datetime | None
    last_error: datetime | None
    last_error_message: str | None
    warnings: tuple[str, ...] = ()


def _statuses(entry: PlayerEntry) -> tuple[str, ...]:
    result: list[str] = []
    if not entry.is_active:
        result.append("inactive")
    if entry.is_disabled:
        result.append("disabled")
    if entry.is_injured:
        result.append("injured")
    if entry.has_suspension:
        result.append("suspended")
    return tuple(result)


def _entry_map(snapshot: PlayerStatisticsSnapshot) -> dict[str, PlayerEntry]:
    game = snapshot.statistics.game
    return {player_identity(game, entry): entry for entry in snapshot.statistics.entries}


def _player_change(
    key: str, old: PlayerEntry | None, new: PlayerEntry | None
) -> PlayerSnapshotChange:
    selected = new or old
    assert selected is not None
    return PlayerSnapshotChange(
        key,
        selected.name,
        selected.team,
        selected.position,
        None if old is None else old.value,
        None if new is None else new.value,
        () if old is None else _statuses(old),
        () if new is None else _statuses(new),
    )


def compare_snapshots(
    current: PlayerStatisticsSnapshot,
    previous: PlayerStatisticsSnapshot,
    *,
    rank_changes: tuple[TeamRankChange, ...] = (),
) -> SnapshotDiff:
    """Compare two player fetches for the same game."""

    current_game = current.statistics.game
    previous_game = previous.statistics.game
    if (
        current_game.locale.casefold(),
        current_game.slug,
    ) != (
        previous_game.locale.casefold(),
        previous_game.slug,
    ):
        raise ValueError("snapshots skal tilhøre det samme spil")
    new_map = _entry_map(current)
    old_map = _entry_map(previous)
    added = tuple(
        _player_change(key, None, new_map[key])
        for key in sorted(new_map.keys() - old_map.keys())
    )
    removed = tuple(
        _player_change(key, old_map[key], None)
        for key in sorted(old_map.keys() - new_map.keys())
    )
    shared = sorted(old_map.keys() & new_map.keys())
    prices = tuple(
        _player_change(key, old_map[key], new_map[key])
        for key in shared
        if old_map[key].value != new_map[key].value
    )
    statuses = tuple(
        _player_change(key, old_map[key], new_map[key])
        for key in shared
        if _statuses(old_map[key]) != _statuses(new_map[key])
    )
    return SnapshotDiff(
        game=current_game,
        previous_generated_at=previous.generated_at,
        current_generated_at=current.generated_at,
        added_players=added,
        removed_players=removed,
        price_changes=prices,
        status_changes=statuses,
        rank_changes=rank_changes,
        previous_round=previous.statistics.round_number,
        current_round=current.statistics.round_number,
        previous_round_status=previous.statistics.round_status,
        current_round_status=current.statistics.round_status,
    )


def build_intra_round_diff(
    players: PlayerStatisticsIndex, game: GameUrl
) -> IntraRoundDiff | None:
    """Compare the two newest fetches without collapsing equal round numbers."""

    snapshots = players.for_game(game)
    if len(snapshots) < 2:
        return None
    current, previous = snapshots[0], snapshots[1]
    diff = compare_snapshots(current, previous)
    return IntraRoundDiff(
        diff,
        current.statistics.round_number == previous.statistics.round_number,
    )


def compare_round_snapshots(
    players: PlayerStatisticsIndex,
    teams: SnapshotIndex,
    game: GameUrl,
    target_round: int,
) -> SnapshotDiff | None:
    """Compare the newest target-round fetch with the previous cached round."""

    current = players.newest(game, target_round)
    previous_round = next(
        (
            round_number
            for round_number in players.rounds_for(game)
            if round_number < target_round
        ),
        None,
    )
    if current is None or previous_round is None:
        return None
    previous = players.newest(game, previous_round)
    if previous is None:
        return None
    rank_changes: list[TeamRankChange] = []
    for identity in teams.identities:
        if identity[:2] != (game.locale.casefold(), game.slug):
            continue
        old = teams.summary_for(game, identity[2], previous_round)
        new = teams.summary_for(game, identity[2], target_round)
        if old is None or new is None:
            continue
        old_rank = old[1].overall_rank
        new_rank = new[1].overall_rank
        if old_rank == new_rank:
            continue
        rank_changes.append(
            TeamRankChange(
                identity[2],
                new[0].team.team_name,
                old_rank,
                new_rank,
                old_rank - new_rank
                if old_rank is not None and new_rank is not None
                else None,
                old[0].generated_at,
                new[0].generated_at,
            )
        )
    return compare_snapshots(
        current,
        previous,
        rank_changes=tuple(
            sorted(
                rank_changes,
                key=lambda item: (item.team_name.casefold(), item.team_id),
            )
        ),
    )


def compare_team_snapshots(
    current: TeamSnapshot, previous: TeamSnapshot
) -> TeamRankChange:
    if current.identity != previous.identity:
        raise ValueError("teamsnapshots skal tilhøre det samme hold")
    old_rank = previous.team.overview.rank
    new_rank = current.team.overview.rank
    movement = (
        old_rank - new_rank
        if old_rank is not None and new_rank is not None
        else None
    )
    return TeamRankChange(
        current.team.reference.team_id,
        current.team.team_name,
        old_rank,
        new_rank,
        movement,
        previous.generated_at,
        current.generated_at,
    )


def compare_team_rounds(
    snapshots: SnapshotIndex,
    game: GameUrl,
    team_id: int,
    target_round: int,
) -> TeamSnapshotDiff | None:
    """Compare one team's target round with its previous available round."""

    rounds = snapshots.rounds_for(game, (team_id,))
    previous_round = next(
        (round_number for round_number in rounds if round_number < target_round),
        None,
    )
    current = snapshots.summary_for(game, team_id, target_round)
    if current is None or previous_round is None:
        return None
    previous = snapshots.summary_for(game, team_id, previous_round)
    if previous is None:
        return None
    current_roster = snapshots.roster_for(game, team_id, target_round)
    previous_roster = snapshots.roster_for(game, team_id, previous_round)
    added: tuple[str, ...] = ()
    removed: tuple[str, ...] = ()
    roster_comparable = current_roster is not None and previous_roster is not None
    if roster_comparable:
        old_players = {
            entry.player_id: entry.name for entry in previous_roster.team.roster
        }
        new_players = {
            entry.player_id: entry.name for entry in current_roster.team.roster
        }
        added = tuple(
            new_players[player_id]
            for player_id in sorted(new_players.keys() - old_players.keys())
        )
        removed = tuple(
            old_players[player_id]
            for player_id in sorted(old_players.keys() - new_players.keys())
        )
    old_summary = previous[1]
    new_summary = current[1]
    return TeamSnapshotDiff(
        game=game,
        team_id=team_id,
        team_name=current[0].team.team_name,
        previous_round=previous_round,
        current_round=target_round,
        previous_generated_at=previous[0].generated_at,
        current_generated_at=current[0].generated_at,
        previous_round_status=old_summary.round_status,
        current_round_status=new_summary.round_status,
        old_total=old_summary.total,
        new_total=new_summary.total,
        old_player_value=old_summary.player_value,
        new_player_value=new_summary.player_value,
        old_rank=old_summary.overall_rank,
        new_rank=new_summary.overall_rank,
        rank_movement=(
            old_summary.overall_rank - new_summary.overall_rank
            if old_summary.overall_rank is not None
            and new_summary.overall_rank is not None
            else None
        ),
        added_players=added,
        removed_players=removed,
        roster_comparable=roster_comparable,
    )


def latest_snapshot_diff(
    players: PlayerStatisticsIndex,
    teams: SnapshotIndex,
    game: GameUrl,
) -> SnapshotDiff | None:
    player_snapshots = players.for_game(game)
    if len(player_snapshots) < 2:
        return None
    rank_changes: list[TeamRankChange] = []
    for identity in teams.identities:
        if identity[:2] != (game.locale.casefold(), game.slug):
            continue
        values = teams.for_team(game, identity[2])
        if len(values) >= 2:
            change = compare_team_snapshots(values[0], values[1])
            if (
                change.old_rank != change.new_rank
                or change.old_generated_at != change.new_generated_at
            ):
                rank_changes.append(change)
    return compare_snapshots(
        player_snapshots[0],
        player_snapshots[1],
        rank_changes=tuple(
            sorted(rank_changes, key=lambda item: (item.team_name.casefold(), item.team_id))
        ),
    )


def build_history_series(
    snapshots: SnapshotIndex,
    game: GameUrl,
    team_ids: tuple[int, ...],
    *,
    group: GroupDefinition | None = None,
    rounds: Iterable[int] | None = None,
) -> tuple[HistoryPoint, ...]:
    """Build complete round grids; missing snapshots remain explicit holes."""

    known = {
        summary.round_number
        for team_id in team_ids
        for snapshot in snapshots.for_team(game, team_id)
        for summary in snapshot.team.history
    }
    if rounds is None:
        selected_rounds = (
            tuple(range(min(known), max(known) + 1)) if known else ()
        )
    else:
        selected_rounds = tuple(sorted(set(rounds)))
    result: list[HistoryPoint] = []
    for round_number in selected_rounds:
        group_ranks: dict[int, int | None] = {}
        if group is not None:
            group_ranks = {
                row.team_id: row.rank
                for row in build_standings(group, snapshots, round_number, "overall")
            }
        for team_id in team_ids:
            located = snapshots.summary_for(game, team_id, round_number)
            newest = snapshots.newest(game, team_id)
            summary = located[1] if located is not None else None
            name = (
                located[0].team.team_name
                if located is not None
                else newest.team.team_name if newest is not None else str(team_id)
            )
            result.append(
                HistoryPoint(
                    round_number,
                    team_id,
                    name,
                    None if summary is None else summary.total,
                    None if summary is None else summary.change,
                    None if summary is None else summary.overall_rank,
                    group_ranks.get(team_id),
                )
            )
    return tuple(result)


def build_player_history(
    snapshots: PlayerStatisticsIndex,
    game: GameUrl,
    player_keys: tuple[str, ...],
) -> tuple[PlayerHistoryPoint, ...]:
    rounds = snapshots.rounds_for(game)
    if not rounds:
        return ()
    selected_rounds = range(min(rounds), max(rounds) + 1)
    by_round = {
        round_number: snapshots.newest(game, round_number)
        for round_number in selected_rounds
    }
    latest_entries = {
        key: entry
        for snapshot in snapshots.for_game(game)
        for key, entry in _entry_map(snapshot).items()
        if key in player_keys
    }
    result: list[PlayerHistoryPoint] = []
    for round_number in selected_rounds:
        snapshot = by_round[round_number]
        entries = {} if snapshot is None else _entry_map(snapshot)
        for key in player_keys:
            entry = entries.get(key)
            fallback = latest_entries.get(key)
            result.append(
                PlayerHistoryPoint(
                    round_number,
                    key,
                    entry.name if entry is not None else fallback.name if fallback else key,
                    None if entry is None else entry.value,
                    None if entry is None else entry.total_growth,
                    None if entry is None else entry.round_growth,
                    None if entry is None else _statuses(entry),
                )
            )
    return tuple(result)


def _manifest_status(
    store: ManifestStore,
    game_slug: str,
    *,
    game_locale: str | None = None,
) -> tuple[datetime | None, datetime | None, str | None, tuple[str, ...]]:
    """Read schema-1/2 refresh health through the canonical manifest store."""

    success: datetime | None = None
    failure: datetime | None = None
    failure_message: str | None = None
    manifests, raw_warnings = store.scan(
        game_slug,
        game_locale=game_locale,
        scope="game",
    )
    for manifest in manifests:
        generated = manifest.completed_at
        if any(
            step.status in {"fetched", "reused_current"}
            for step in manifest.steps
        ) and (success is None or generated > success):
            success = generated
        errors = tuple(
            step.error
            for step in manifest.steps
            if step.status in {"reused_after_error", "failed_no_cache"}
            and step.error
        )
        if errors and (failure is None or generated > failure):
            failure = generated
            failure_message = errors[0]
    warnings = tuple(
        (
            f"{Path(source).name}: {detail.replace(source, Path(source).name)}"
            if separator
            else "Et refresh-manifest kunne ikke læses"
        )
        for warning in raw_warnings
        for source, separator, detail in (warning.rpartition(": "),)
    )
    return success, failure, failure_message, warnings


def build_data_quality_report(
    games: tuple[ManagerGame, ...],
    groups: tuple[GroupDefinition, ...],
    teams: SnapshotIndex,
    players: PlayerStatisticsIndex,
    *,
    manifest_dir: Path | str,
    include_archived: bool = False,
    now: datetime | None = None,
) -> DataQualityReport:
    """Report cached coverage only; it never invokes Holdet."""

    selected_games = tuple(
        game for game in games if include_archived or not game.is_archived
    )
    rows: list[DataQualityRound] = []
    manifest_store = ManifestStore(manifest_dir)
    manifest_warnings: list[str] = []
    game_health: list[
        tuple[datetime | None, datetime | None, str | None]
    ] = []
    for manager_game in selected_games:
        game_groups = tuple(
            group
            for group in groups
            if (group.game.locale.casefold(), group.game.slug)
            == manager_game.identity
        )
        team_labels: dict[int, str] = {}
        for group in game_groups:
            for member in group.teams:
                team_labels.setdefault(member.team_id, member.name)
        team_ids = tuple(team_labels)
        rounds = set(teams.rounds_for(manager_game.game, team_ids))
        rounds.update(players.rounds_for(manager_game.game))
        game_success, game_failure, game_error, game_warnings = _manifest_status(
            manifest_store,
            manager_game.game.slug,
            game_locale=manager_game.game.locale,
        )
        manifest_warnings.extend(game_warnings)
        game_health.append((game_success, game_failure, game_error))
        for round_number in sorted(rounds, reverse=True):
            located = [
                teams.summary_for(manager_game.game, team_id, round_number)
                for team_id in team_ids
            ]
            summaries = [item[1] for item in located if item is not None]
            statuses = [item.round_status for item in summaries]
            player_snapshot = players.newest(manager_game.game, round_number)
            missing_ids = tuple(
                team_id
                for team_id, item in zip(team_ids, located)
                if item is None
            )
            reasons: list[str] = []
            readiness: Literal[
                "ready", "preliminary", "missing", "error"
            ]
            if (
                game_failure is not None
                and (game_success is None or game_failure > game_success)
            ):
                readiness = "error"
                reasons.append(game_error or "Seneste opdatering mislykkedes")
            elif missing_ids or player_snapshot is None:
                readiness = "missing"
                if missing_ids:
                    reasons.append(
                        f"{len(missing_ids)} hold mangler rundedata"
                    )
                if player_snapshot is None:
                    reasons.append("Spillersnapshot mangler")
            elif (
                any(status != "complete" for status in statuses)
                or player_snapshot.statistics.round_status != "complete"
            ):
                readiness = "preliminary"
                reasons.append(
                    "Runden er i gang eller endnu ikke bekræftet ved genhentning"
                )
            else:
                readiness = "ready"
            rows.append(
                DataQualityRound(
                    game_slug=manager_game.game.slug,
                    round_number=round_number,
                    expected_teams=len(team_ids),
                    team_snapshots=len(summaries),
                    player_snapshot=player_snapshot is not None,
                    complete=statuses.count("complete"),
                    in_progress=statuses.count("in_progress"),
                    unknown=statuses.count("unknown"),
                    missing_team_ids=missing_ids,
                    game_locale=manager_game.game.locale.casefold(),
                    game_name=manager_game.name,
                    missing_team_names=tuple(
                        team_labels.get(team_id, f"Hold {team_id}")
                        for team_id in missing_ids
                    ),
                    player_round_status=(
                        None
                        if player_snapshot is None
                        else player_snapshot.statistics.round_status
                    ),
                    readiness=readiness,
                    reasons=tuple(reasons),
                    newest_team_data=max(
                        (
                            item[0].generated_at
                            for item in located
                            if item is not None
                        ),
                        default=None,
                    ),
                    newest_player_data=(
                        None
                        if player_snapshot is None
                        else player_snapshot.generated_at
                    ),
                    last_success=game_success,
                    last_error=game_failure,
                    last_error_message=game_error,
                )
            )
    success = max(
        (item[0] for item in game_health if item[0] is not None),
        default=None,
    )
    failure_row = max(
        (item for item in game_health if item[1] is not None),
        key=lambda item: item[1],
        default=(None, None, None),
    )
    failure, error = failure_row[1], failure_row[2]
    return DataQualityReport(
        now or datetime.now().astimezone(),
        tuple(rows),
        max((item.generated_at for item in teams.snapshots), default=None),
        max((item.generated_at for item in players.snapshots), default=None),
        success,
        failure,
        error,
        tuple(
            (
                *teams.warnings,
                *players.warnings,
                *manifest_warnings,
            )
        ),
    )

