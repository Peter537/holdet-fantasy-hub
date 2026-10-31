"""Build a complete, cache-only JSON export for a resolved scope."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json

from .game_metadata import GameMetadata, _metadata_to_dict
from .groups import GroupDefinition, _tournament_to_dict
from .output import _round_dict, team_to_dict
from .player_serialization import player_statistics_to_dict
from .scopes import ResolvedScope
from .standings import build_standings
from .storage import PlayerStatisticsIndex, RefreshManifest, SnapshotIndex
from .tournament import TournamentConfig, build_tournament_state


def _instant(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _quality(generated_at: datetime, round_number: int, target: int | None,
             status: str, now: datetime, end_at: datetime | None = None) -> list[str]:
    issues: list[str] = []
    if _instant(now) - _instant(generated_at) >= timedelta(hours=24):
        issues.append("stale")
    if target is not None and round_number < target:
        issues.append("older_round")
    if (end_at is not None and _instant(now) >= _instant(end_at)
            and _instant(generated_at) < _instant(end_at)):
        issues.append("before_round_end")
    if status != "complete":
        issues.append("preliminary" if status == "in_progress" else "unverified")
    return issues


def _tournament_definition(config: TournamentConfig) -> dict[str, object]:
    result = _tournament_to_dict(config)
    published = config.published_matches
    result["published_matches"] = (
        None if published is None else [asdict(match) for match in published]
    )
    result["quality"] = ["unverified"] if config.pairing_error else []
    return result


def _competition(group: GroupDefinition, teams: SnapshotIndex) -> dict[str, object]:
    ids = tuple(member.team_id for member in group.teams)
    rounds = teams.rounds_for(group.game, ids)
    current_round = max(rounds, default=0)
    result: dict[str, object] = {
        "id": group.group_id,
        "name": group.name,
        "type": group.kind,
        "official_url": group.official_url,
        "official_link_type": group.official_link_type,
        "members": [{"id": member.team_id, "name": member.name} for member in group.teams],
        "as_of_round": current_round if rounds else None,
    }
    if group.tournament is not None:
        result["active_revision"] = group.active_revision
        result["definition"] = _tournament_definition(group.tournament)
        result["previous_revisions"] = [
            {
                "revision": revision.revision,
                "archived_at": revision.archived_at,
                "reason": revision.reason,
                "members": [{"id": member.team_id, "name": member.name} for member in revision.teams],
                "definition": _tournament_definition(revision.tournament),
            }
            for revision in group.archived_revisions
        ]
        overview_rounds = (
            snapshot.team.overview.current_round
            for team_id in ids
            for snapshot in teams.for_team(group.game, team_id)
        )
        as_of_round = min(
            group.tournament.final_round,
            max(group.tournament.start_round, current_round, *overview_rounds),
        )
        state = build_tournament_state(group, teams, as_of_round)
        result["as_of_round"] = state.as_of_round
        result["state"] = {
            "phase": state.phase,
            "standings": [asdict(item) for item in state.standings],
            "group_matches": [asdict(item) for item in state.group_matches],
            "knockout_matches": [asdict(item) for item in state.knockout_matches],
            "active_team_ids": sorted(state.active_team_ids),
            "eliminated_team_ids": sorted(state.eliminated_team_ids),
            "champion_id": state.champion_id,
            "warnings": list(state.warnings),
            "pairing_conflicts": list(state.pairing_conflicts),
        }
    else:
        rows = build_standings(group, teams, current_round, "overall")
        result["standings"] = [
            {
                "rank": row.rank, "team_id": row.team_id,
                "team_name": row.team_name, "owner_name": row.owner_name,
                "total": row.total, "value": row.value, "change": row.change,
                "distance": row.distance, "warning": row.warning,
            }
            for row in rows
        ]
    return result


def build_scope_export(
    scope: ResolvedScope,
    teams: SnapshotIndex,
    players: PlayerStatisticsIndex,
    *,
    include_history: bool = False,
    now: datetime | None = None,
    warnings: tuple[str, ...] = (),
    metadata: GameMetadata | None = None,
    manifest: RefreshManifest | None = None,
) -> dict[str, object]:
    """Return version 1 data, independent of UI filters and pagination.

    Active and archived revisions must already contain their validated persisted
    pairings, or carry pairing_error when those fixtures could not be verified.
    Caller warnings are counted but their potentially sensitive text is omitted.
    """
    current = now or datetime.now().astimezone()
    if metadata is not None and metadata.identity != scope.manager_game.identity:
        raise ValueError("Spilmetadata tilhører et andet managerspil")
    if manifest is not None and manifest.game_slug != scope.game.slug:
        raise ValueError("Opdateringsmanifestet tilhører et andet managerspil")
    if (manifest is not None and scope.competition_id is not None
            and manifest.scope == "group" and manifest.group_id != scope.competition_id):
        raise ValueError("Opdateringsmanifestet tilhører en anden konkurrence")
    omitted_legacy_manifest = False
    if manifest is not None and manifest.schema_version == 1 and not manifest.game_locale:
        matching_groups = tuple(
            group for group in scope.groups
            if group.group_id == scope.competition_id
            and (group.game.locale.casefold(), group.game.slug) == scope.manager_game.identity
        )
        verified_group_context = (
            manifest.scope == "group"
            and scope.competition_id is not None
            and manifest.group_id == scope.competition_id
            and len(matching_groups) == 1
        )
        if not verified_group_context:
            manifest = None
            omitted_legacy_manifest = True
    elif manifest is not None and manifest.game_locale.casefold() != scope.game.locale.casefold():
        raise ValueError("Opdateringsmanifestet tilhører et andet managerspil")
    player_rounds = players.rounds_for(scope.game)
    player_current = players.newest(scope.game, player_rounds[0]) if player_rounds else None
    team_ids = tuple(item.team_id for item in scope.team_references)
    team_rounds = teams.rounds_for(scope.game, team_ids)
    overview_rounds = tuple(
        snapshot.team.overview.current_round
        for team_id in team_ids
        for snapshot in teams.for_team(scope.game, team_id)
    )
    target = max((*player_rounds, *team_rounds, *overview_rounds), default=None)
    player_data = (
        None if player_current is None else player_statistics_to_dict(
            player_current.statistics, generated_at=player_current.generated_at
        )
    )
    player_issues = (
        ["missing"] if player_current is None else _quality(
            player_current.generated_at, player_current.statistics.round_number,
            target, player_current.statistics.round_status, current,
            player_current.statistics.round_end_at,
        )
    )
    history = []
    if include_history:
        for round_number in player_rounds:
            snapshot = players.newest(scope.game, round_number)
            assert snapshot is not None
            history.append(player_statistics_to_dict(snapshot.statistics, generated_at=snapshot.generated_at))

    team_data: list[dict[str, object]] = []
    for reference in scope.team_references:
        snapshots = teams.for_team(scope.game, reference.team_id)
        # A later correction of an old round must not replace the current roster.
        snapshot = max(
            snapshots,
            key=lambda item: (item.team.overview.current_round, _instant(item.generated_at)),
            default=None,
        )
        if snapshot is None:
            team_data.append({"id": reference.team_id, "name": reference.team_name,
                              "snapshot": None, "summaries": [], "quality": ["missing"]})
            continue
        serialized = team_to_dict(snapshot.team, generated_at=snapshot.generated_at)
        # Account configuration and profile URLs are not part of the export.
        serialized["source"] = {"game_url": scope.game.original, "team_url": reference.source_url}
        serialized["account"] = {"owner_name": snapshot.team.owner_name,
                                 "owner_user_id": snapshot.team.owner_user_id}
        serialized.pop("history", None)
        summaries = []
        for round_number in teams.rounds_for(scope.game, (reference.team_id,)):
            located = teams.summary_for(scope.game, reference.team_id, round_number)
            assert located is not None
            source, summary = located
            summaries.append({**_round_dict(summary), "generated_at": source.generated_at.isoformat()})
        current_summary = teams.summary_for(scope.game, reference.team_id, snapshot.team.overview.current_round)
        status = "unknown" if current_summary is None else current_summary[1].round_status
        end_at = None if current_summary is None else current_summary[1].round_end_at
        team_data.append({
            "id": reference.team_id, "name": snapshot.team.team_name,
            "snapshot": serialized, "summaries": summaries,
            "quality": _quality(snapshot.generated_at, snapshot.team.overview.current_round,
                                target, status, current, end_at),
        })

    source_warnings = []
    if omitted_legacy_manifest:
        source_warnings.append(
            "Legacy refresh provenance omitted because its locale could not be verified"
        )
    if warnings:
        source_warnings.append(
            f"{len(warnings)} source warning(s); see the application for details"
        )
    if players.warnings:
        source_warnings.append(f"{len(players.warnings)} player snapshot(s) could not be read")
    if teams.warnings:
        source_warnings.append(f"{len(teams.warnings)} team snapshot(s) could not be read")
    unverified_revisions = sum(
        bool(config.pairing_error)
        for group in scope.groups
        for config in (group.tournament, *(revision.tournament for revision in group.archived_revisions))
        if config is not None
    )
    if unverified_revisions:
        source_warnings.append(f"{unverified_revisions} tournament revision(s) could not be verified")
    manifest_data = None
    if manifest is not None:
        manifest_data = {
            "scope": manifest.scope, "group_id": manifest.group_id,
            "run_id": manifest.run_id, "mode": manifest.mode,
            "result": manifest.result, "target_round": manifest.target_round,
            "started_at": manifest.started_at.isoformat(),
            "completed_at": manifest.completed_at.isoformat(),
            "steps": [
                {
                    "id": step.step_id, "source": step.source,
                    "status": step.status, "attempted": step.attempted,
                    "team_id": step.team_id, "round": step.round_number,
                    "requested_round": step.requested_round,
                    "started_at": None if step.started_at is None else step.started_at.isoformat(),
                    "completed_at": None if step.completed_at is None else step.completed_at.isoformat(),
                    "cache_generated_at": None if step.cache_generated_at is None else step.cache_generated_at.isoformat(),
                }
                for step in manifest.steps
                if step.source != "team" or step.team_id in team_ids
            ],
        }
    result: dict[str, object] = {
        "schema_version": 1,
        "generated_at": current.isoformat(),
        "scope": {
            "type": "competition" if scope.competition_id is not None else "game",
            "competition_id": scope.competition_id,
            "game": {"locale": scope.game.locale, "slug": scope.game.slug,
                     "name": scope.manager_game.name, "url": scope.game.original},
            "read_only": scope.read_only,
        },
        "players": {"current": player_data, "quality": player_issues},
        "metadata": {
            "current": None if metadata is None else _metadata_to_dict(metadata),
            "quality": (["missing"] if metadata is None else
                        (["stale"] if _instant(current) - _instant(metadata.fetched_at) >= timedelta(hours=24) else [])),
        },
        "refresh_manifest": manifest_data,
        "teams": team_data,
        "competitions": [_competition(group, teams) for group in scope.groups],
        "warnings": source_warnings,
    }
    if include_history:
        result["players"]["history"] = history
    return result


def serialize_scope_export(
    scope: ResolvedScope,
    teams: SnapshotIndex,
    players: PlayerStatisticsIndex,
    *,
    include_history: bool = False,
    now: datetime | None = None,
    warnings: tuple[str, ...] = (),
    metadata: GameMetadata | None = None,
    manifest: RefreshManifest | None = None,
) -> bytes:
    """Serialize one complete scope export as strict UTF-8 JSON."""
    data = build_scope_export(scope, teams, players, include_history=include_history,
                              now=now, warnings=warnings, metadata=metadata,
                              manifest=manifest)
    return (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
