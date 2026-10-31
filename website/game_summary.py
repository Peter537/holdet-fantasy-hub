"""Cache-only summaries shared by game lists and game overviews."""

from dataclasses import dataclass
from datetime import datetime

from holdet_lib.refresh import RefreshMode, build_refresh_plan


@dataclass(frozen=True)
class ScopeOverview:
    round_number: int | None
    oldest_capture: datetime | None
    player_count: int
    missing_teams: int
    needs_update: bool

    @property
    def status(self) -> str:
        if self.oldest_capture is None:
            return "Ingen lokale data"
        if self.missing_teams:
            return f"{self.missing_teams} hold mangler data"
        return "Opdatering nødvendig" if self.needs_update else "Data er aktuelle"


def scope_overview(scope, teams, players, metadata, settings) -> ScopeOverview:
    latest_players = players.latest_round(scope.game)
    latest_teams = tuple(
        max(teams.for_team(scope.game, member.team_id),
            key=lambda item: (item.team.overview.current_round, item.generated_at), default=None)
        for member in scope.team_references
    )
    captures = [item.generated_at for item in latest_teams if item is not None]
    rounds = [item.team.overview.current_round for item in latest_teams if item is not None]
    if latest_players is not None:
        captures.append(latest_players.generated_at)
        rounds.append(latest_players.statistics.round_number)
    plan = build_refresh_plan(scope.manager_game, scope.groups, teams, players,
        metadata=metadata, settings=settings, mode=RefreshMode.STALE_ONLY,
        catch_up=True, competition_id=scope.competition_id)
    return ScopeOverview(
        max(rounds, default=None), min(captures, default=None),
        0 if latest_players is None else len(latest_players.statistics.entries),
        sum(item is None for item in latest_teams),
        any(step.selected and step.source != "postprocess" for step in plan.steps),
    )
