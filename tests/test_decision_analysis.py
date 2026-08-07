from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from itertools import combinations
from pathlib import Path

import holdet_lib as holdet
import pytest


NOW = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
GAME = holdet.GameUrl(
    "https://www.holdet.dk/da/fantasy/test-season",
    "da",
    "test-season",
)


def entry(
    player_id: int,
    growth: int | None,
    *,
    value: int = 1_000_000,
    position: str = "Forward",
    team: str = "A",
) -> holdet.PlayerEntry:
    return holdet.PlayerEntry(
        player_id,
        f"Player {player_id}",
        team,
        position,
        value,
        entry_id=player_id,
        total_growth=200_000,
        round_growth=growth,
    )


def player_snapshot(
    round_number: int,
    entries: tuple[holdet.PlayerEntry, ...],
    *,
    status: str = "complete",
    offset: int = 0,
) -> holdet.PlayerStatisticsSnapshot:
    statistics = holdet.ScrapedGame(
        GAME,
        "soccer",
        round_number,
        entries,
        format="soccer",
        unit="money",
        round_status=status,
    )
    return holdet.PlayerStatisticsSnapshot(
        Path(f"players-{round_number}-{offset}.json"),
        NOW + timedelta(minutes=offset),
        statistics,
    )


def roster(player_id: int, growth: int, *, role: str = "") -> holdet.RosterEntry:
    return holdet.RosterEntry(
        player_id,
        player_id,
        f"Player {player_id}",
        "A",
        "Forward",
        1_000_000,
        growth,
        growth,
        1,
        role,
    )


def summary(round_number: int, change: int, *, captain_bonus: int = 0) -> holdet.RoundSummary:
    return holdet.RoundSummary(
        round_number,
        10_000_000 + change,
        change,
        2_000_000,
        8_000_000,
        0,
        20_000,
        change - captain_bonus,
        0,
        captain_bonus,
        0,
        0,
        round_status="complete",
    )


def team_snapshot(
    team_id: int,
    round_number: int,
    players: tuple[holdet.RosterEntry, ...],
    histories: tuple[holdet.RoundSummary, ...],
) -> holdet.TeamSnapshot:
    reference = holdet.TeamReference(
        GAME,
        team_id,
        f"Team {team_id}",
        f"https://www.holdet.dk/da/fantasy/test-season/team/{team_id}",
    )
    team = holdet.ScrapedTeam(
        reference,
        "soccer",
        1,
        reference.team_name,
        f"Manager {team_id}",
        team_id,
        holdet.TeamOverview(
            round_number,
            "money",
            8_000_000,
            2_000_000,
            histories[-1].total,
            histories[-1].change,
            team_id,
            0,
            10,
            3,
            3,
            0,
        ),
        players,
        histories,
    )
    return holdet.TeamSnapshot(
        Path(f"team-{team_id}-{round_number}.json"),
        NOW + timedelta(minutes=round_number),
        team,
    )


def verified_rules(**overrides) -> holdet.GameRuleProfile:
    values = dict(
        game_locale="da",
        game_slug=GAME.slug,
        label="Test",
        verified=True,
        source_url="https://example.test/rules",
        accessed_on=date(2026, 8, 7),
        bank_interest_basis_points=100,
        transfer_fee_basis_points=100,
        salary_cap=3_000_000,
        roster_size=2,
        max_from_team=2,
        position_limits=(("forward", 2, 2),),
        budget_enabled=True,
        captain_count=1,
        captain_multiplier=2,
    )
    values.update(overrides)
    return holdet.GameRuleProfile(**values)


def test_player_form_value_and_stability_use_completed_distinct_rounds() -> None:
    snapshots = holdet.PlayerStatisticsIndex(
        tuple(
            player_snapshot(round_number, (entry(1, growth),), offset=round_number)
            for round_number, growth in enumerate((10, 20, 30, 40, 50), start=1)
        )
    )
    key = holdet.player_identity(GAME, entry(1, 0))
    analysis = holdet.build_player_decision_analysis(snapshots, GAME, key)
    assert analysis is not None
    assert analysis.growth_per_million == 200_000
    assert analysis.form_3 == 40
    assert analysis.form_5 == 30
    assert analysis.stability_score is not None
    assert analysis.provenance.certainty == "final"


def test_player_windows_use_observations_without_interpolating_round_gaps() -> None:
    snapshots = holdet.PlayerStatisticsIndex(
        tuple(
            player_snapshot(round_number, (entry(1, growth),), offset=round_number)
            for round_number, growth in ((1, -10), (3, 20), (7, 50))
        )
    )
    analysis = holdet.build_player_decision_analysis(
        snapshots, GAME, holdet.player_identity(GAME, entry(1, 0))
    )
    assert analysis is not None
    assert analysis.form_3 == 20
    assert analysis.form_5 is None
    assert analysis.provenance.rounds == (1, 3, 7)
    assert 0 <= (analysis.stability_score or 0) <= 100

    zero = replace(entry(2, 10), value=0)
    zero_analysis = holdet.build_player_decision_analysis(
        holdet.PlayerStatisticsIndex((player_snapshot(1, (zero,)),)),
        GAME,
        holdet.player_identity(GAME, zero),
    )
    assert zero_analysis is not None
    assert zero_analysis.growth_per_million is None


def test_bank_and_captain_fail_closed_and_use_verified_rules() -> None:
    rules = verified_rules()
    bank = holdet.build_bank_analysis(2_000_000, 1_000_000, rules)
    assert bank.transfer_fee == 10_000
    assert bank.full_bank_interest == 20_000
    assert bank.remaining_bank == 990_000
    assert bank.break_even_growth == 20_100

    current = team_snapshot(
        1,
        1,
        (roster(1, 10, role="captain"), roster(2, 5)),
        (summary(1, 20, captain_bonus=10),),
    )
    captain = holdet.build_captain_analysis(current, current.team.history[0], rules)
    assert not captain.mismatch
    assert captain.alternatives[1].bonus == 5

    unknown = holdet.build_bank_analysis(
        2_000_000,
        1_000_000,
        holdet.rule_profile_for_game(GAME),
    )
    assert unknown.break_even_growth is None
    assert unknown.provenance.certainty == "unverified"


@pytest.mark.parametrize(
    ("basis_points", "expected_interest"),
    ((0, 0), (50, 5_000), (100, 10_000)),
)
def test_bank_interest_rates_round_and_report_historical_hit_share(
    basis_points: int,
    expected_interest: int,
) -> None:
    rules = verified_rules(
        bank_interest_basis_points=basis_points,
        transfer_fee_basis_points=0,
    )
    result = holdet.build_bank_analysis(
        1_000_000,
        500_000,
        rules,
        historical_windows=((20_000, 10_000), (1_000, None)),
    )
    assert result.full_bank_interest == expected_interest
    fee_rules = verified_rules(transfer_fee_basis_points=basis_points)
    assert fee_rules.transfer_fee(1_000_000) == expected_interest
    assert result.compared_players_3 == 2
    assert result.compared_players_5 == 1
    assert result.beat_share_3 is not None


def test_basis_point_rounding_modes_are_explicit() -> None:
    floor = verified_rules(
        transfer_fee_basis_points=50,
        transfer_fee_rounding="floor",
    )
    ceil = verified_rules(
        transfer_fee_basis_points=50,
        transfer_fee_rounding="ceil",
    )
    nearest = verified_rules(
        transfer_fee_basis_points=50,
        transfer_fee_rounding="nearest",
    )
    assert floor.transfer_fee(1) == 0
    assert ceil.transfer_fee(1) == 1
    assert nearest.transfer_fee(1) == 0


def test_captain_mismatch_disables_scenarios_and_two_captains_are_combined() -> None:
    current = team_snapshot(
        1,
        1,
        (roster(1, 10, role="captain"), roster(2, 5), roster(3, 2)),
        (summary(1, 20, captain_bonus=9),),
    )
    mismatch = holdet.build_captain_analysis(
        current, current.team.history[0], verified_rules()
    )
    assert mismatch.mismatch
    assert mismatch.alternatives == ()
    assert mismatch.actual_bonus == 9

    two = replace(
        current,
        team=replace(
            current.team,
            roster=(
                roster(1, 10, role="captain"),
                roster(2, 5, role="captain"),
                roster(3, 2),
            ),
            history=(summary(1, 20, captain_bonus=15),),
        ),
    )
    combined = holdet.build_captain_analysis(
        two,
        two.team.history[0],
        verified_rules(captain_count=2),
    )
    assert not combined.mismatch
    assert combined.alternatives[0].bonus == 15
    assert combined.alternatives[0].player_ids == (1, 2)


def test_transfer_ledger_computes_one_round_counterfactual_and_rejects_gaps() -> None:
    first = team_snapshot(
        1,
        1,
        (roster(1, 40_000), roster(2, 10_000)),
        (summary(1, 50_000),),
    )
    second = team_snapshot(
        1,
        2,
        (roster(2, 10_000), roster(3, 100_000)),
        (summary(1, 50_000), summary(2, 200_000)),
    )
    players = holdet.PlayerStatisticsIndex(
        (
            player_snapshot(
                2,
                (entry(1, 40_000), entry(2, 10_000), entry(3, 100_000)),
            ),
        )
    )
    ledger = holdet.build_team_decision_ledger(
        holdet.SnapshotIndex((first, second)),
        players,
        GAME,
        1,
        verified_rules(),
    )
    assert ledger.decisions[0].decision_delta == 50_000
    assert ledger.decisions[0].no_trade_change == 150_000

    gap = replace(
        second,
        team=replace(
            second.team,
            overview=replace(second.team.overview, current_round=3),
            history=(summary(1, 50_000), summary(3, 200_000)),
        ),
    )
    gap_ledger = holdet.build_team_decision_ledger(
        holdet.SnapshotIndex((first, gap)), players, GAME, 1, verified_rules()
    )
    assert gap_ledger.decisions[0].decision_delta is None
    assert "sammenhængende" in " ".join(
        gap_ledger.decisions[0].provenance.missing_reasons
    )


def test_exposure_reports_coverage_denominator() -> None:
    first = team_snapshot(1, 1, (roster(1, 10), roster(2, 5)), (summary(1, 15),))
    second = team_snapshot(2, 1, (roster(1, 10), roster(3, 2)), (summary(1, 12),))
    group = holdet.GroupDefinition(
        "g",
        "Group",
        GAME,
        (
            holdet.GroupTeam(1, "Team 1", first.team.reference.source_url),
            holdet.GroupTeam(2, "Team 2", second.team.reference.source_url),
            holdet.GroupTeam(3, "Missing", "https://example.test/team/3"),
        ),
    )
    result = holdet.build_group_exposure(
        group, holdet.SnapshotIndex((first, second)), 1
    )
    assert result.rows[0].owners == 2
    assert result.rows[0].covered_teams == 2
    assert result.rows[0].ownership_percent == 100
    assert result.missing_team_ids == (3,)


def test_ideal_team_is_exact_and_deterministic() -> None:
    candidates = (
        entry(1, 10, value=1_500_000, team="A"),
        entry(2, 8, value=1_000_000, team="B"),
        entry(3, 8, value=900_000, team="C"),
    )
    result = holdet.optimize_ideal_team(candidates, verified_rules(), round_number=1)
    assert result.status == "optimal"
    assert tuple(item.entry_id for item in result.players) == (1, 3)
    assert result.objective == 18
    assert result.total_cost == 2_400_000
    assert result.objective_upper_bound == result.objective

    timeout = holdet.optimize_ideal_team(
        candidates, verified_rules(), round_number=1, timeout_seconds=0
    )
    assert timeout.status == "timeout"
    assert timeout.objective_upper_bound == 18
    infeasible = holdet.optimize_ideal_team(
        candidates[:1], verified_rules(), round_number=1
    )
    assert infeasible.status == "infeasible"


def test_ideal_team_matches_brute_force_and_handles_four_hundred_candidates() -> None:
    candidates = tuple(
        entry(
            player_id,
            (player_id * 7) % 31 - 5,
            value=700_000 + (player_id % 4) * 100_000,
            team=f"Club {player_id % 10}",
        )
        for player_id in range(1, 9)
    )
    rules = verified_rules(salary_cap=2_000_000)
    result = holdet.optimize_ideal_team(candidates, rules)
    feasible = [
        group
        for group in combinations(candidates, 2)
        if sum(item.value for item in group) <= 2_000_000
    ]
    expected = max(
        feasible,
        key=lambda group: (
            sum(item.round_growth or 0 for item in group),
            -sum(item.value for item in group),
            tuple(-int(item.entry_id or 0) for item in sorted(group, key=lambda item: item.entry_id or 0)),
        ),
    )
    assert result.objective == sum(item.round_growth or 0 for item in expected)

    large = tuple(
        entry(
            player_id,
            1_000 - player_id,
            value=1_000_000,
            team=f"Club {player_id}",
        )
        for player_id in range(1, 401)
    )
    benchmark = holdet.optimize_ideal_team(large, verified_rules())
    assert benchmark.status == "optimal"
    assert benchmark.objective == 1_997


def test_bootstrap_is_reproducible_and_gated() -> None:
    vectors = tuple(
        (round_number, {"a": round_number, "b": 0, "c": round_number * 2})
        for round_number in range(1, 6)
    )
    first = holdet.simulate_transfer_scenario(vectors, ("a", "b"), ("a", "c"))
    second = holdet.simulate_transfer_scenario(vectors, ("a", "b"), ("a", "c"))
    assert first.status == "complete"
    assert first.seed == second.seed
    assert first.median_delta == second.median_delta
    assert first.probability_better == 1

    descriptive = holdet.simulate_transfer_scenario(
        vectors[:4], ("a", "b"), ("a", "c")
    )
    assert descriptive.status == "descriptive"
    assert descriptive.simulations == 0


def test_bootstrap_preserves_common_player_cancellation_and_backtests() -> None:
    vectors = tuple(
        (
            round_number,
            {"common": round_number * 1_000, "old": 0, "new": round_number},
        )
        for round_number in range(1, 9)
    )
    result = holdet.simulate_transfer_scenario(
        vectors,
        ("common", "old"),
        ("common", "new"),
        seed=42,
    )
    assert result.seed == 42
    assert result.observed_deltas == tuple(range(1, 9))
    assert result.input_coverage == 1
    assert result.backtest_observations == 3
    assert result.backtest_model_mae is not None
    assert result.backtest_latest_mae is not None


def test_inbox_deduplicates_and_persists_state(tmp_path: Path) -> None:
    watched_entry = holdet.watchlist_entry(GAME, entry(1, 0))
    previous = player_snapshot(1, (entry(1, 1),))
    current = player_snapshot(
        1,
        (replace(entry(1, 1), is_injured=True),),
        offset=1,
    )
    alerts = holdet.build_watchlist_alerts(previous, current, (watched_entry,), now=NOW)
    assert len(alerts) == 1
    store = holdet.AnalysisInboxStore(tmp_path / "analysis-inbox.json")
    assert len(store.merge(alerts)) == 1
    assert len(store.merge(alerts)) == 1
    read = store.mark_read(alerts[0].alert_id, now=NOW + timedelta(minutes=1))
    assert not read[0].is_unread
    dismissed = store.dismiss(alerts[0].alert_id, now=NOW + timedelta(minutes=2))
    assert dismissed[0].dismissed_at is not None
    assert store.clear_dismissed() == ()


def test_inbox_can_clear_dismissed_for_one_game_only(tmp_path: Path) -> None:
    store = holdet.AnalysisInboxStore(tmp_path / "analysis-inbox.json")
    first = holdet.WatchlistAlert(
        "first",
        "da",
        "test-season",
        "player:1",
        "Player 1",
        "injured",
        "Player 1 er blevet markeret som skadet.",
        NOW,
        dismissed_at=NOW,
    )
    second = replace(
        first,
        alert_id="second",
        game_locale="en",
        game_slug="other-season",
    )
    unread = replace(
        first,
        alert_id="unread",
        dismissed_at=None,
    )
    store.save((first, second, unread))

    remaining = store.clear_dismissed(
        game_identity=("DA", "test-season")
    )

    assert {item.alert_id for item in remaining} == {"second", "unread"}
    assert store.clear_dismissed() == (unread,)


def test_alert_baseline_prefers_same_or_latest_earlier_round_over_late_backfill() -> None:
    round_two = player_snapshot(2, (entry(1, 2),), offset=1)
    late_round_one = player_snapshot(1, (entry(1, 1),), offset=20)
    index = holdet.PlayerStatisticsIndex((late_round_one, round_two))
    assert holdet.select_alert_baseline(index, GAME, 3) == round_two
    assert holdet.select_alert_baseline(index, GAME, 2) == round_two
    assert holdet.select_alert_baseline(index, GAME, 0) is None


def test_settings_schema_two_loads_without_rewrite_and_schema_three_roundtrips(
    tmp_path: Path,
) -> None:
    path = tmp_path / "hub.json"
    legacy = {
        "schema_version": 2,
        "watchlist": [],
        "manager_aliases": [],
        "manager_profiles": [],
        "hall_of_fame_score": {
            "group_points": [10, 6, 3, 1],
            "tournament_winner": 10,
            "tournament_finalist": 6,
            "tournament_semifinalist": 3,
            "global_round_win": 1,
        },
    }
    path.write_text(json.dumps(legacy), encoding="utf-8")
    before = path.read_bytes()
    store = holdet.HubSettingsStore(path)
    settings = store.load()
    assert path.read_bytes() == before

    annotation = holdet.PlayerAnnotation("da", GAME.slug, "player", "Note", ("Overvej",))
    settings = store.set_player_annotations(settings, (annotation,))
    settings = store.set_own_team(
        settings, holdet.OwnTeamSelection("da", GAME.slug, 42)
    )
    settings = store.set_saved_player_filters(
        settings,
        (
            holdet.SavedPlayerFilter(
                "filter-1",
                "Aktive",
                "da",
                GAME.slug,
                holdet.PlayerStatisticsQuery(
                    status_rules=(("inactive", "exclude"),),
                    sort_field="total_growth",
                ),
            ),
        ),
    )
    assert store.load() == settings
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 3

    with pytest.raises(ValueError, match="12 tags"):
        holdet.PlayerAnnotation(
            "da", GAME.slug, "player", tags=tuple(str(value) for value in range(13))
        )


def test_rule_profile_roundtrip_preserves_basis_points() -> None:
    rules = verified_rules(bank_interest_basis_points=50)
    parsed = holdet.game_rule_from_dict(holdet.game_rule_to_dict(rules))
    assert parsed == rules
    assert parsed.interest(1_000_000) == 5_000


def test_verified_rule_requires_audited_source() -> None:
    with pytest.raises(ValueError, match="kilde-URL"):
        holdet.GameRuleProfile("da", GAME.slug, "Test", verified=True)


def test_fixture_adapter_is_fail_closed_and_preserves_official_difficulty(
    tmp_path: Path,
) -> None:
    unverified = holdet.FixtureSourceProfile(
        "https://example.test/fixtures", date(2026, 8, 7), True, False
    )
    payload = [
        {
            "round": 1,
            "team": "A",
            "opponent": "B",
            "home_away": "home",
            "start_at": "2026-08-08T12:00:00+00:00",
            "difficulty": 4,
        }
    ]
    with pytest.raises(holdet.PayloadError, match="ikke verificeret"):
        holdet.parse_fixture_records(payload, unverified)
    source = replace(
        unverified,
        parser_fixture_verified=True,
        official_difficulty_field="difficulty",
        difficulty_documentation_url="https://example.test/difficulty",
    )
    records = holdet.parse_fixture_records(payload, source)
    assert records[0].official_difficulty == 4
    snapshot = holdet.FixtureSnapshot(GAME, records, source, NOW)
    store = holdet.FixtureStore(tmp_path)
    store.save(snapshot)
    assert store.load(GAME) == snapshot
