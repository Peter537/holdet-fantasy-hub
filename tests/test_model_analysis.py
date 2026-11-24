from dataclasses import replace

import pytest

from holdet_lib import GroupDefinition, GroupTeam, PlayerStatisticsIndex, SnapshotIndex
from holdet_lib.decision_analysis import (
    assess_model_transfer,
    build_bank_analysis,
    build_group_comparison,
    completed_model_vectors,
    historical_growth_windows,
    simulate_transfer_scenario,
)
from tests.test_decision_analysis import GAME, entry, player_snapshot, roster, summary, team_snapshot, verified_rules


def test_model_selects_one_latest_completed_correction_within_cutoff() -> None:
    revisions = tuple(player_snapshot(
        1, (entry(1, value), entry(2, 10)), offset=value,
    ) for value in range(1, 9))
    newer_live = player_snapshot(1, (entry(1, 900),), status="in_progress", offset=9)
    future = player_snapshot(2, (entry(1, 800),), offset=10)
    index = PlayerStatisticsIndex((newer_live, future, *revisions))
    vectors, positions = completed_model_vectors(index, GAME, 1)
    assert vectors == ((1, {"1": 8, "2": 10}),)
    assert set(positions) == {"1", "2"}
    result = simulate_transfer_scenario(vectors, ("1",), ("2",))
    assert result.status == "unavailable"
    assert result.provenance.rounds == (1,)
    assert result.backtest_observations == 0
    with pytest.raises(ValueError, match="én observation pr. runde"):
        simulate_transfer_scenario(vectors * 8, ("1",), ("2",))


@pytest.mark.parametrize(("rounds", "status", "simulations", "backtest"), [
    (3, "descriptive", 0, 0), (5, "complete", 20, 0), (8, "complete", 20, 3),
])
def test_model_qualification_uses_distinct_rounds(rounds, status, simulations, backtest) -> None:
    vectors = tuple((number, {"1": 1, "2": number}) for number in range(1, rounds + 1))
    result = simulate_transfer_scenario(vectors, ("1",), ("2",), simulations=20, seed=17)
    assert (result.status, result.simulations, result.backtest_observations) == (status, simulations, backtest)
    assert result.provenance.rounds == tuple(range(1, rounds + 1))
    hypothetical = simulate_transfer_scenario(
        vectors, ("1",), ("2",), simulations=20, seed=17, descriptive_only=True,
    )
    assert (hypothetical.status, hypothetical.simulations, hypothetical.backtest_observations) == ("descriptive", 0, 0)


@pytest.mark.parametrize(("own_status", "leader_status", "player_status", "certainty"), [
    ("complete", "complete", "complete", "final"),
    ("in_progress", "complete", "complete", "preliminary"),
    ("complete", "in_progress", "complete", "preliminary"),
    ("complete", "complete", "in_progress", "preliminary"),
    ("complete", "complete", None, "preliminary"),
])
def test_group_and_bank_use_corrected_bounded_history(
    own_status, leader_status, player_status, certainty,
) -> None:
    own = team_snapshot(101, 5, (roster(1, 5),), (replace(summary(5, 5), round_status=own_status),))
    leader = team_snapshot(102, 5, (roster(2, 12),), (replace(summary(5, 12), round_status=leader_status),))
    group = GroupDefinition("test", "Test", GAME, tuple(
        GroupTeam(item.team.reference.team_id, item.team.team_name, item.team.reference.source_url)
        for item in (own, leader)
    ))
    observations = [
        *(player_snapshot(number, (entry(1, number), entry(2, 2)))
          for number in range(1, 5)),
        player_snapshot(4, (entry(1, 40), entry(2, 2)), offset=50),
        player_snapshot(6, (entry(1, 600), entry(2, 2)), offset=60),
    ]
    if player_status is not None:
        observations.append(player_snapshot(5, (entry(1, 5), entry(2, 2)), status=player_status))
    players = PlayerStatisticsIndex(tuple(observations))
    result = build_group_comparison(group, SnapshotIndex((own, leader)), players, 101, 5)
    assert result is not None
    assert result.provenance.certainty == certainty
    assert result.actual_swing == (3 if player_status is not None else None)
    expected_own_form = 16 if player_status == "complete" else 15
    assert result.form_proxy_swing == expected_own_form - 2

    windows = historical_growth_windows(players, GAME, 5)
    assert windows[1] == (expected_own_form, 10.2 if player_status == "complete" else None)
    bank = build_bank_analysis(2_000_000, 1_000_000, verified_rules(), round_status=own_status)
    assert bank.provenance.certainty == ("final" if own_status == "complete" else "preliminary")


def test_model_swap_uses_verified_rules_current_prices_and_contracts() -> None:
    team = team_snapshot(101, 5, (roster(1, 1, role="captain"), roster(2, 10)), (summary(5, 11),))
    players = player_snapshot(5, (entry(1, 1), entry(2, 10), entry(3, 20)))
    rules = verified_rules(sport="soccer", free_through_round=1, fee_free_through_round=1)

    def assess(team_source=team, player_source=players, profile=rules, *, sale=2, final_round=None):
        return assess_model_transfer(
            team_source, player_source, profile, sold_id=sale, bought_id=3, final_round=final_round,
        )

    assert assess().status == "valid"
    assert assess().round_number == 6
    assert assess(sale=1).status == "invalid"
    false_captain = replace(team.team.roster[0], role="noncaptain")
    wrong_roles = replace(team.team, roster=(false_captain, team.team.roster[1]))
    assert assess(team_source=replace(team, team=wrong_roles)).status == "unverified"
    assert assess(final_round=5).status == "invalid"
    assert assess(profile=replace(rules, verified=False)).status == "unverified"
    assert assess(profile=replace(rules, transfer_fee_basis_points=None)).status == "unverified"
    live = replace(players, statistics=replace(players.statistics, round_status="in_progress"))
    assert assess(player_source=live).status == "unverified"
    for remaining, expected in ((None, "unverified"), (0, "invalid")):
        changes = replace(team.team.overview, substitutions_remaining=remaining)
        assert assess(team_source=replace(team, team=replace(team.team, overview=changes))).status == expected
    old_prices = player_snapshot(5, (entry(1, 1), entry(2, 10, value=1_100_000), entry(3, 20)))
    assert assess(player_source=old_prices).status == "unverified"


def test_bank_analysis_respects_verified_fee_free_rounds() -> None:
    rules = verified_rules(fee_free_through_round=3)
    free = build_bank_analysis(2_000_000, 1_000_000, rules, round_number=3, round_status="complete")
    charged = build_bank_analysis(2_000_000, 1_000_000, rules, round_number=4, round_status="complete")
    assert (free.transfer_fee, charged.transfer_fee) == (0, 10_000)
