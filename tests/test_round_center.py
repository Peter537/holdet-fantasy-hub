from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import holdet_lib.round_center as round_center_module
from holdet_lib.game_metadata import MetadataChange
from holdet_lib.groups import GroupDefinition, GroupTeam
from holdet_lib.models import (
    GameUrl,
    PlayerEntry,
    RoundSummary,
    ScheduleRound,
    ScrapedGame,
    ScrapedTeam,
    TeamOverview,
    TeamReference,
)
from holdet_lib.round_center import (
    RoundCenterReadiness,
    RoundDeviation,
    TradingWindowView,
    build_club_change_deviations,
    build_group_matrix,
    build_injury_deviations,
    build_missing_team_deviations,
    build_next_best_action,
    build_rank_deviations,
    build_round_center_readiness,
    build_round_comparison,
    build_rules_schedule_deviations,
    build_trading_window_view,
    select_round_deviations,
)
from holdet_lib.storage import (
    PlayerStatisticsIndex,
    PlayerStatisticsSnapshot,
    SnapshotIndex,
    TeamSnapshot,
)
from holdet_lib.tournament import (
    KnockoutMatch,
    TournamentState,
    create_tournament_config,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 9, 12, tzinfo=UTC)
GAME = GameUrl(
    "https://www.holdet.dk/da/fantasy/test-season",
    "da",
    "test-season",
)


def summary(
    round_number: int,
    total: int,
    change: int,
    *,
    rank: int | None,
    status: str = "complete",
    end_at: datetime | None = None,
) -> RoundSummary:
    return RoundSummary(
        round_number,
        total,
        change,
        100,
        total - 100,
        0,
        0,
        change,
        0,
        0,
        0,
        0,
        overall_rank=rank,
        round_status=status,
        round_end_at=end_at,
    )


def team_snapshot(
    team_id: int,
    history: tuple[RoundSummary, ...],
    *,
    generated_at: datetime = NOW,
) -> TeamSnapshot:
    latest = max(history, key=lambda item: item.round_number)
    reference = TeamReference(
        GAME,
        team_id,
        f"Hold {team_id}",
        f"https://example.test/team/{team_id}",
        account_label=f"Manager {team_id}",
    )
    team = ScrapedTeam(
        reference,
        "soccer",
        1,
        reference.team_name,
        f"Ejer {team_id}",
        team_id,
        TeamOverview(
            latest.round_number,
            "money",
            1_000,
            100,
            latest.total,
            latest.change,
            latest.overall_rank,
            None,
            10,
            3,
            3,
            0,
        ),
        (),
        history,
    )
    return TeamSnapshot(Path(f"team-{team_id}.json"), generated_at, team)


def player(
    source_index: int,
    *,
    name: str | None = None,
    team: str = "A",
    entry_id: int | None = None,
    person_id: int | None = None,
    injured: bool = False,
    suspended: bool = False,
) -> PlayerEntry:
    return PlayerEntry(
        source_index,
        name or f"Spiller {source_index}",
        team,
        "Forward",
        1_000_000,
        is_injured=injured,
        has_suspension=suspended,
        entry_id=entry_id,
        person_id=person_id,
    )


def player_snapshot(
    round_number: int,
    entries: tuple[PlayerEntry, ...],
    *,
    generated_at: datetime | None = None,
    status: str = "complete",
) -> PlayerStatisticsSnapshot:
    statistics = ScrapedGame(
        GAME,
        "soccer",
        round_number,
        entries,
        format="soccer",
        unit="money",
        round_status=status,
    )
    return PlayerStatisticsSnapshot(
        Path(f"players-{round_number}.json"),
        generated_at or NOW + timedelta(minutes=round_number),
        statistics,
    )


def group(*team_ids: int, tournament=False) -> GroupDefinition:
    members = tuple(
        GroupTeam(
            team_id,
            f"Hold {team_id}",
            f"https://example.test/team/{team_id}",
            account_label=f"Manager {team_id}",
        )
        for team_id in team_ids
    )
    config = (
        create_tournament_config(
            team_ids, 1, 2, 1, shuffle=lambda values: None
        )
        if tournament
        else None
    )
    return GroupDefinition(
        "gruppe",
        "Gruppen",
        GAME,
        members,
        "tournament" if tournament else "standings",
        config,
    )


def test_trading_window_boundaries_and_countdowns() -> None:
    schedule = ScheduleRound(
        3,
        NOW + timedelta(hours=1),
        NOW + timedelta(hours=3),
        NOW + timedelta(hours=5),
    )

    before = build_trading_window_view((schedule,), now=NOW)
    assert before.status == "opens"
    assert before.transition_kind == "opens"
    assert before.seconds_until_transition(now=NOW) == 3600

    opened = build_trading_window_view((schedule,), now=schedule.start)
    assert opened.status == "open"
    assert opened.transition_kind == "closes"
    assert opened.seconds_until_transition(now=schedule.start) == 7200

    closed = build_trading_window_view((schedule,), now=schedule.close)
    assert closed.status == "closed"
    assert closed.transition_at is None
    assert build_trading_window_view((), now=NOW).status == "unverified"


def test_trading_window_uses_absolute_time_across_dst_gap_and_fold() -> None:
    copenhagen = ZoneInfo("Europe/Copenhagen")
    spring = ScheduleRound(
        4,
        datetime(2026, 3, 29, 1, 30, tzinfo=copenhagen),
        datetime(2026, 3, 29, 3, 30, tzinfo=copenhagen),
        datetime(2026, 3, 29, 5, 0, tzinfo=copenhagen),
    )
    spring_now = datetime(2026, 3, 29, 1, 45, tzinfo=copenhagen)
    spring_view = build_trading_window_view((spring,), now=spring_now)
    assert spring_view.status == "open"
    assert spring_view.seconds_until_transition(now=spring_now) == 45 * 60

    autumn = ScheduleRound(
        9,
        datetime(2026, 10, 25, 2, 15, tzinfo=copenhagen, fold=0),
        datetime(2026, 10, 25, 2, 15, tzinfo=copenhagen, fold=1),
        datetime(2026, 10, 25, 4, 0, tzinfo=copenhagen),
    )
    autumn_now = datetime(2026, 10, 25, 2, 45, tzinfo=copenhagen, fold=0)
    autumn_view = build_trading_window_view((autumn,), now=autumn_now)
    assert autumn_view.status == "open"
    assert autumn_view.seconds_until_transition(now=autumn_now) == 30 * 60


def test_completed_round_with_pre_end_data_needs_refresh() -> None:
    end_at = NOW - timedelta(hours=1)
    team = team_snapshot(
        1,
        (summary(3, 120, 20, rank=10),),
        generated_at=end_at - timedelta(minutes=1),
    )
    players = player_snapshot(
        3,
        (player(1, entry_id=1),),
        generated_at=end_at - timedelta(minutes=1),
    )

    readiness = build_round_center_readiness(
        GAME,
        3,
        (1,),
        SnapshotIndex((team,)),
        PlayerStatisticsIndex((players,)),
        round_end_at=end_at,
        now=NOW,
    )

    assert readiness.status == "completed_needs_refresh"
    assert readiness.completed_needs_refresh
    assert readiness.needs_refresh
    assert any("før rundens sluttid" in reason for reason in readiness.reasons)


def test_readiness_is_ready_only_with_post_end_complete_coverage() -> None:
    end_at = NOW - timedelta(hours=1)
    generated = end_at + timedelta(minutes=1)
    teams = SnapshotIndex(
        (
            team_snapshot(
                1,
                (summary(3, 120, 20, rank=10),),
                generated_at=generated,
            ),
        )
    )
    players = PlayerStatisticsIndex(
        (
            player_snapshot(
                3,
                (player(1, entry_id=1),),
                generated_at=generated,
            ),
        )
    )

    readiness = build_round_center_readiness(
        GAME,
        3,
        (1,),
        teams,
        players,
        round_end_at=end_at,
        now=NOW,
    )

    assert readiness.status == "ready"
    assert not readiness.needs_refresh


def test_refresh_plan_staleness_is_exposed_without_inventing_missing_data() -> None:
    teams = SnapshotIndex(
        (team_snapshot(1, (summary(3, 120, 20, rank=10),)),)
    )
    players = PlayerStatisticsIndex(
        (player_snapshot(3, (player(1, entry_id=1),)),)
    )

    readiness = build_round_center_readiness(
        GAME,
        3,
        (1,),
        teams,
        players,
        round_end_at=NOW + timedelta(hours=1),
        now=NOW,
        stale_source_ids=("players", "team:1"),
    )

    assert readiness.status == "ready"
    assert readiness.is_stale
    assert readiness.needs_refresh
    assert build_next_best_action(
        TradingWindowView("open", 3), readiness
    ).kind == "refresh_stale"


def test_next_best_action_uses_deadline_safe_priority() -> None:
    ready = RoundCenterReadiness("ready", 3, round_end_at=NOW)
    stale = RoundCenterReadiness(
        "completed_needs_refresh",
        3,
        ("Data er forældet",),
        round_end_at=NOW,
    )
    unknown = TradingWindowView("unverified")
    opened = TradingWindowView("open", 3)

    assert build_next_best_action(unknown, stale, unread_alerts=2).kind == (
        "fetch_metadata"
    )
    fetch = build_next_best_action(unknown, ready)
    assert fetch.title == "Hent spilinfo og data"
    assert build_next_best_action(opened, stale, unread_alerts=2).kind == (
        "refresh_stale"
    )
    between_rounds = build_next_best_action(
        TradingWindowView("opens", 4), stale
    )
    assert between_rounds.round_number == 3
    assert build_next_best_action(opened, ready, unread_alerts=2).kind == (
        "review_alerts"
    )
    assert build_next_best_action(opened, ready).kind == "review_team"
    missing_schedule = RoundCenterReadiness(
        "unverified",
        4,
        ("Rundens sluttid er ikke verificeret",),
    )
    assert build_next_best_action(
        TradingWindowView("closed", 3), missing_schedule
    ).kind == "fetch_metadata"
    with pytest.raises(ValueError):
        build_next_best_action(opened, ready, unread_alerts=-1)


def test_safe_deviation_builders_use_exact_rounds_and_stable_ids() -> None:
    teams = SnapshotIndex(
        (
            team_snapshot(
                1,
                (
                    summary(2, 100, 10, rank=100),
                    summary(3, 130, 30, rank=20),
                ),
            ),
        )
    )
    old_entries = (
        player(1, team="A", entry_id=10, person_id=100),
        player(2, team="X", entry_id=20),
        player(3, name="Navn Uden ID", team="L", entry_id=None),
    )
    new_entries = (
        player(
            1,
            team="B",
            entry_id=99,
            person_id=100,
            injured=True,
            suspended=True,
        ),
        player(2, team="Y", entry_id=20),
        player(3, name="Navn Uden ID", team="M", entry_id=None),
    )
    players = PlayerStatisticsIndex(
        (player_snapshot(2, old_entries), player_snapshot(3, new_entries))
    )

    rank = build_rank_deviations(teams, GAME, (1,), 3)
    adverse = build_injury_deviations(players, GAME, 3)
    clubs = build_club_change_deviations(players, GAME, 3)

    assert len(rank) == 1
    assert rank[0].magnitude == 80
    assert {item.current_value for item in adverse} == {"skadet", "karantæne"}
    assert {item.player_key for item in clubs} == {"person:100", "entry:20"}
    assert all(item.player_name != "Navn Uden ID" for item in clubs)


def test_missing_teams_are_deduplicated_across_groups() -> None:
    one = group(1, 2)
    two = replace(group(2), group_id="anden", name="Anden")
    snapshots = SnapshotIndex(
        (team_snapshot(1, (summary(3, 100, 10, rank=1),)),)
    )

    deviations = build_missing_team_deviations(
        (one, two), snapshots, GAME, 3
    )

    assert len(deviations) == 1
    assert deviations[0].team_id == 2
    assert deviations[0].group_ids == ("anden", "gruppe")


def test_rules_schedule_builder_ignores_unrelated_game_metadata() -> None:
    changes = (
        MetadataChange("rules", "rule_profile", "før", "efter", 3),
        MetadataChange("schedule", "close", "12:00", "13:00", 3),
        MetadataChange("game", "display_name", "A", "B", 3),
    )

    deviations = build_rules_schedule_deviations(changes, 3)

    assert len(deviations) == 2
    assert {item.category for item in deviations} == {"rules_schedule"}
    assert {item.previous_value for item in deviations} == {"før", "12:00"}


def test_top_n_caps_only_rank_deviations() -> None:
    ranks = tuple(
        RoundDeviation(
            f"rank:{index}",
            "rank",
            "info",
            f"Rang {index}",
            "Ændring",
            3,
            magnitude=index,
        )
        for index in range(1, 5)
    )
    other = tuple(
        RoundDeviation(
            f"injury:{index}",
            "injury",
            "warning",
            f"Skade {index}",
            "Ændring",
            3,
        )
        for index in range(3)
    )

    selected = select_round_deviations((*ranks, *other), limit=2)

    assert sum(item.category == "rank" for item in selected) == 2
    assert sum(item.category == "injury" for item in selected) == 3
    assert {item.magnitude for item in selected if item.category == "rank"} == {
        3,
        4,
    }


def test_round_comparison_uses_one_exact_previous_round() -> None:
    teams = SnapshotIndex(
        (
            team_snapshot(
                1,
                (
                    summary(2, 100, 10, rank=20),
                    summary(3, 130, 30, rank=12),
                ),
            ),
            team_snapshot(
                2,
                (
                    summary(2, 110, 20, rank=10),
                    summary(3, 125, 15, rank=15),
                ),
            ),
        )
    )
    players = PlayerStatisticsIndex(
        (
            player_snapshot(2, (player(1, entry_id=1),)),
            player_snapshot(3, (player(1, entry_id=1, injured=True),)),
        )
    )

    comparison = build_round_comparison(teams, players, GAME, (1, 2), 3)

    assert comparison.previous_round == 2
    assert comparison.is_final
    assert [item.rank_movement for item in comparison.teams] == [8, -5]
    assert comparison.player_diff is not None
    assert comparison.player_diff.previous_round == 2


def test_ordinary_group_matrix_has_all_members_and_no_schedule_copy() -> None:
    snapshots = SnapshotIndex(
        (
            team_snapshot(1, (summary(3, 120, 20, rank=10),)),
            team_snapshot(2, (summary(3, 100, 10, rank=20),)),
        )
    )

    matrix = build_group_matrix(group(1, 2, 3), snapshots, 3)

    assert matrix.metric == "overall_total"
    assert [item.team_id for item in matrix.rows] == [1, 2, 3]
    assert [item.distance for item in matrix.rows] == [0, -20, None]
    assert all(item.next_opponent_name == "Ingen kampplan" for item in matrix.rows)


def test_tournament_group_matrix_uses_points_and_scheduled_fallbacks() -> None:
    matrix = build_group_matrix(group(1, 2, 3, tournament=True), SnapshotIndex(()), 0)

    assert matrix.metric == "tournament_points"
    assert len(matrix.rows) == 3
    assert all(item.value == 0 and item.distance == 0 for item in matrix.rows)
    assert {item.opponent_status for item in matrix.rows} == {"scheduled", "bye"}
    bye = next(item for item in matrix.rows if item.opponent_status == "bye")
    assert bye.next_opponent_name == "Fri"


def test_group_matrix_prioritizes_pending_bronze_match_and_next_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team_ids = (1, 2, 3, 4)
    config = replace(
        create_tournament_config(
            team_ids,
            1,
            5,
            2,
            shuffle=lambda values: None,
        ),
        bronze_match=True,
    )
    tournament_group = GroupDefinition(
        "bronze",
        "Bronzeturnering",
        GAME,
        tuple(
            GroupTeam(
                team_id,
                f"Hold {team_id}",
                f"https://example.test/team/{team_id}",
                account_label=f"Manager {team_id}",
            )
            for team_id in team_ids
        ),
        "tournament",
        config,
    )
    bronze = KnockoutMatch(
        stage="Bronzekamp",
        match_index=1,
        round_numbers=(4, 5),
        team_a_id=3,
        team_b_id=4,
        team_a_name="Hold 3",
        team_b_name="Hold 4",
        team_a_seed=3,
        team_b_seed=4,
        team_a_change=None,
        team_b_change=None,
        winner_id=None,
        complete=False,
    )
    state = TournamentState(
        as_of_round=4,
        phase="Finale",
        group_matches=(),
        standings=(),
        knockout_matches=(bronze,),
        active_team_ids=frozenset({1, 2}),
        eliminated_team_ids=frozenset({3, 4}),
        champion_id=None,
        warnings=(),
    )
    monkeypatch.setattr(
        round_center_module,
        "build_tournament_state",
        lambda *_args, **_kwargs: state,
    )

    matrix = build_group_matrix(tournament_group, SnapshotIndex(()), 4)
    bronze_rows = {row.team_id: row for row in matrix.rows if row.team_id in {3, 4}}

    assert bronze_rows[3].opponent_status == "scheduled"
    assert bronze_rows[3].next_opponent_id == 4
    assert bronze_rows[3].next_round == 5
    assert bronze_rows[4].opponent_status == "scheduled"
    assert bronze_rows[4].next_opponent_id == 3
    assert bronze_rows[4].next_round == 5
