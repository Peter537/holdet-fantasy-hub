"""Pure historical standings calculation for fixed-membership groups."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol

from .models import GameUrl, RoundSummary
from .storage import SnapshotIndex, TeamSnapshot


class StandingMember(Protocol):
    team_id: int
    name: str
    account_label: str


class StandingGroup(Protocol):
    game: GameUrl
    teams: tuple[StandingMember, ...]


@dataclass(frozen=True, slots=True)
class StandingRow:
    rank: int | None
    team_id: int
    team_name: str
    owner_name: str
    total: int | None
    value: int | None
    change: int | None
    distance: int | None
    snapshot: TeamSnapshot | None
    summary: RoundSummary | None
    stale: bool = False
    warning: str | None = None


def build_standings(
    group: StandingGroup,
    snapshots: SnapshotIndex,
    round_number: int,
    mode: str,
    *,
    stale_team_ids: frozenset[int] = frozenset(),
) -> tuple[StandingRow, ...]:
    """Build competition-ranked standings for one historical round."""

    if mode not in {"overall", "round"}:
        raise ValueError("Stillingstilstanden skal være 'overall' eller 'round'")
    provisional: list[StandingRow] = []
    for member in group.teams:
        located = snapshots.summary_for(group.game, member.team_id, round_number)
        newest = snapshots.newest(group.game, member.team_id)
        if located is None:
            provisional.append(StandingRow(
                rank=None, team_id=member.team_id, team_name=member.name,
                owner_name=newest.team.owner_name if newest else member.account_label,
                total=None, value=None, change=None, distance=None,
                snapshot=newest, summary=None,
                stale=member.team_id in stale_team_ids,
                warning=f"Mangler data for runde {round_number}",
            ))
            continue
        snapshot, summary = located
        active_value = summary.total if mode == "overall" else summary.change
        provisional.append(StandingRow(
            rank=0, team_id=member.team_id, team_name=snapshot.team.team_name,
            owner_name=snapshot.team.owner_name, total=summary.total,
            value=active_value, change=summary.change, distance=0,
            snapshot=snapshot, summary=summary,
            stale=member.team_id in stale_team_ids,
        ))

    ranked = sorted(
        (row for row in provisional if row.value is not None),
        key=lambda row: (-int(row.value), row.team_name.casefold(), row.team_id),
    )
    leader = ranked[0].value if ranked else None
    completed: list[StandingRow] = []
    previous_value: int | None = None
    previous_rank = 0
    for index, row in enumerate(ranked, 1):
        rank = previous_rank if row.value == previous_value else index
        completed.append(replace(
            row, rank=rank,
            distance=row.value - leader if leader is not None else None,
        ))
        previous_value = row.value
        previous_rank = rank
    missing = sorted(
        (row for row in provisional if row.value is None),
        key=lambda row: (row.team_name.casefold(), row.team_id),
    )
    return tuple((*completed, *missing))