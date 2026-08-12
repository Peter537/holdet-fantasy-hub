from __future__ import annotations

from dataclasses import replace
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import shutil
from uuid import uuid4

import pytest

import holdet_lib as holdet


GAME = holdet.GameUrl(
    "https://www.holdet.dk/da/fantasy/scouting-test", "da", "scouting-test"
)
NOW = datetime(2026, 8, 12, 12, tzinfo=timezone.utc)


@contextmanager
def writable_directory():
    root = Path(__file__).parent / f"_test-scouting-{uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


def entry(
    player_id: int,
    *,
    value: int = 5_000_000,
    growth: int | None = 100,
    position: str = "Angriber",
    active: bool = True,
    disabled: bool = False,
    popularity: float | None = None,
) -> holdet.PlayerEntry:
    return holdet.PlayerEntry(
        player_id,
        f"Spiller {player_id}",
        f"Hold {player_id % 3}",
        position,
        value,
        is_active=active,
        is_disabled=disabled,
        entry_id=player_id,
        person_id=1_000 + player_id,
        total_growth=growth,
        round_growth=growth,
        popularity=popularity,
    )


def snapshot(
    round_number: int,
    entries: tuple[holdet.PlayerEntry, ...],
    *,
    minute: int = 0,
    status: holdet.RoundStatus = "complete",
) -> holdet.PlayerStatisticsSnapshot:
    generated = NOW + timedelta(days=round_number, minutes=minute)
    return holdet.PlayerStatisticsSnapshot(
        Path(f"round-{round_number}-{minute}.json"),
        generated,
        holdet.ScrapedGame(
            GAME,
            "soccer",
            round_number,
            entries,
            "soccer",
            "money",
            status,
        ),
    )


def scouting_index() -> holdet.PlayerStatisticsIndex:
    snapshots = []
    for round_number in range(1, 6):
        snapshots.append(
            snapshot(
                round_number,
                tuple(
                    entry(
                        player_id,
                        value=4_000_000 + player_id * 250_000,
                        growth=round_number * (10 + player_id),
                        popularity=(5, 10, 15, 25, 30, 40)[player_id - 1],
                    )
                    for player_id in range(1, 7)
                ),
            )
        )
    return holdet.PlayerStatisticsIndex(tuple(reversed(snapshots)))


def test_percentiles_use_average_rank_for_ties_and_gate_small_cohorts() -> None:
    assert holdet.average_rank_percentiles((1, 2, 2, 4)) == (0, 50, 50, 100)
    small = holdet.PlayerStatisticsIndex(
        (snapshot(1, tuple(entry(player_id) for player_id in range(1, 5))),)
    )
    metrics = holdet.build_scouting_metrics(small, GAME)
    assert metrics[0].metric("value").percentile is None
    assert metrics[0].metric("value").cohort_size == 4


def test_scouting_scores_peers_similarity_and_ownership_are_transparent() -> None:
    metrics = holdet.build_scouting_metrics(
        scouting_index(), GAME, own_team_player_keys=frozenset()
    )
    target = next(item for item in metrics if item.player_key.endswith("entry:1"))
    assert target.metric("value").percentile == 0
    assert target.potential.value is not None
    assert target.risk.value is not None
    assert target.ownership.label == "differential"
    assert target.ownership.ownership_risk is not None
    assert pytest.approx(sum(
        part.contribution or 0 for part in target.potential.components
    )) == target.potential.value

    peer = holdet.build_peer_comparison(metrics, target.player_key)
    assert peer is not None
    assert len(peer.alternatives) == 5
    assert dict(peer.medians)["value"] is not None
    similar = holdet.find_similar_players(metrics, target.player_key)
    assert len(similar) == 5
    assert all(item.shared_metrics >= 2 for item in similar)

    fractional = tuple(
        replace(player, popularity=0.10 if player.source_index == 1 else 0.20)
        for player in scouting_index().snapshots[0].statistics.entries
    )
    normalized = holdet.build_scouting_metrics(
        holdet.PlayerStatisticsIndex((snapshot(6, fractional),)),
        GAME,
        own_team_player_keys=frozenset(),
    )
    fractional_target = next(
        item for item in normalized if item.player_key.endswith("entry:1")
    )
    assert fractional_target.metric("popularity").value == 10
    assert fractional_target.ownership.label == "differential"


def test_peer_median_and_price_alternatives_exclude_inactive_players() -> None:
    players = tuple(
        entry(player_id, value=4_000_000 + player_id * 100_000)
        for player_id in range(1, 7)
    ) + (entry(7, value=1, active=False),)
    metrics = holdet.build_scouting_metrics(
        holdet.PlayerStatisticsIndex((snapshot(1, players),)), GAME
    )
    target = next(item for item in metrics if item.player_key.endswith("entry:1"))
    peer = holdet.build_peer_comparison(metrics, target.player_key)

    assert peer is not None
    assert dict(peer.medians)["value"] == 4_350_000
    assert all(not item.player_key.endswith("entry:7") for item in peer.alternatives)


def test_data_risk_distinguishes_final_preliminary_and_unverified_basis() -> None:
    complete = scouting_index()
    final_metric = holdet.build_scouting_metrics(complete, GAME)[0]
    assert next(
        item.value for item in final_metric.risk.components if item.name == "Datarisiko"
    ) == 0

    preliminary_snapshots = tuple(
        replace(
            item,
            statistics=replace(item.statistics, round_status="in_progress"),
        )
        if item is complete.snapshots[0]
        else item
        for item in complete.snapshots
    )
    preliminary = holdet.build_scouting_metrics(
        holdet.PlayerStatisticsIndex(preliminary_snapshots), GAME
    )[0]
    assert next(
        item.value for item in preliminary.risk.components if item.name == "Datarisiko"
    ) == 70

    insufficient = holdet.build_scouting_metrics(
        holdet.PlayerStatisticsIndex((snapshot(1, (entry(1),), status="unknown"),)),
        GAME,
    )[0]
    assert next(
        item.value for item in insufficient.risk.components if item.name == "Datarisiko"
    ) == 100


def test_smartlists_are_derived_and_recent_activation_uses_fetch_time() -> None:
    before = snapshot(5, (entry(1, active=False),), minute=0)
    after = snapshot(5, (entry(1, active=True),), minute=5)
    index = holdet.PlayerStatisticsIndex((after, before))
    lists = {item.list_id: item for item in holdet.build_smart_lists(
        index, GAME, now=after.generated_at
    )}
    assert lists["recently_activated"].player_keys == (
        holdet.player_identity(GAME, after.statistics.entries[0]),
    )


def test_formula_engine_allows_closed_grammar_and_fails_cells_closed() -> None:
    result = holdet.evaluate_player_formula(
        "ifelse(form_3 > 10 and value > 0, clamp(form_3 / 2, 0, 100), 0)",
        {"form_3": 30, "value": 5_000_000},
    )
    assert result.value == 15
    assert result.error is None
    assert holdet.evaluate_player_formula("value / 0", {"value": 1}).value is None
    assert holdet.evaluate_player_formula("form_3 + value", {"form_3": None, "value": 1}).error

    rejected = (
        "value.__class__",
        "value[0]",
        "__import__(1)",
        "[value for value in value]",
        "lambda: value",
        "value ** 2",
        "'text'",
        "another_computed + 1",
    )
    for expression in rejected:
        with pytest.raises(holdet.FormulaError):
            holdet.validate_player_formula(expression)


def test_formula_engine_rejects_size_depth_and_nonfinite_inputs() -> None:
    with pytest.raises(holdet.FormulaError, match="500"):
        holdet.validate_player_formula("1" * 501)
    deep = "value"
    for _ in range(20):
        deep = f"abs({deep})"
    with pytest.raises(holdet.FormulaError, match="dybde"):
        holdet.validate_player_formula(deep)
    assert holdet.evaluate_player_formula("value + 1", {"value": float("inf")}).value is None


def test_watch_rules_cross_reset_and_include_same_round_event_identity() -> None:
    prior = snapshot(1, (entry(1, value=100),), minute=0)
    previous = snapshot(1, (entry(1, value=101),), minute=1)
    current = snapshot(1, (entry(1, value=104),), minute=2)
    key = holdet.player_identity(GAME, current.statistics.entries[0])
    watched = replace(
        holdet.watchlist_entry(GAME, current.statistics.entries[0]),
        rules=(holdet.WatchRule("rise", "value_rise", 2),),
    )
    alerts = holdet.build_watchlist_alerts(
        previous, current, (watched,), prior=prior, now=NOW
    )
    assert len(alerts) == 1
    assert alerts[0].rule_id == "rise"
    assert alerts[0].previous_snapshot_generated_at == previous.generated_at
    assert alerts[0].transition == "1.00 → 3.00"

    repeated = snapshot(1, (entry(1, value=108),), minute=3)
    assert holdet.build_watchlist_alerts(
        current, repeated, (watched,), prior=previous, now=NOW
    ) == ()
    reset = snapshot(1, (entry(1, value=104),), minute=4)
    crossed_again = snapshot(1, (entry(1, value=108),), minute=5)
    assert len(holdet.build_watchlist_alerts(
        reset, crossed_again, (watched,), prior=repeated, now=NOW
    )) == 1
    assert alerts[0].player_key == key


def test_form_and_status_rules_detect_both_directions_without_first_baseline() -> None:
    previous_entry = replace(entry(1), is_injured=True)
    current_entry = entry(1)
    previous = snapshot(2, (previous_entry,), minute=0)
    current = snapshot(2, (current_entry,), minute=1)
    watched = replace(
        holdet.watchlist_entry(GAME, current_entry),
        rules=(
            holdet.WatchRule("status", "status_change"),
            holdet.WatchRule("form", "form3_above", 10),
        ),
    )
    alerts = holdet.build_watchlist_alerts(
        previous,
        current,
        (watched,),
        previous_forms={watched.player_key: (9, None)},
        current_forms={watched.player_key: (11, None)},
    )
    assert {item.kind for item in alerts} == {"recovered", "form3_above"}
    assert holdet.build_watchlist_alerts(
        None,
        current,
        (watched,),
        current_forms={watched.player_key: (11, None)},
    ) == ()


def test_cleared_watch_rules_remain_disabled() -> None:
    previous = snapshot(2, (replace(entry(1), is_injured=True),), minute=0)
    current = snapshot(2, (entry(1),), minute=1)
    watched = replace(holdet.watchlist_entry(GAME, entry(1)), rules=())

    assert holdet.build_watchlist_alerts(previous, current, (watched,)) == ()


def test_intra_round_diff_and_evidence_first_decomposition() -> None:
    before = replace(
        entry(1, value=100, growth=10),
        stats=(holdet.PlayerPerformanceStat("goals", 1),),
    )
    after = replace(
        entry(1, value=105, growth=12),
        stats=(holdet.PlayerPerformanceStat("goals", 2),),
    )
    previous = snapshot(3, (before,), minute=0)
    current = snapshot(3, (after,), minute=1)
    index = holdet.PlayerStatisticsIndex((current, previous))
    diff = holdet.build_intra_round_diff(index, GAME)
    assert diff is not None and diff.same_round
    key = holdet.player_identity(GAME, after)
    descriptive = holdet.build_player_change_explanation(previous, current, key)
    assert descriptive is not None
    assert descriptive.decomposition == "simultaneous"
    assert any(item.field == "stats.goals" and item.delta == 1 for item in descriptive.observations)

    profile = holdet.PerformanceRuleProfile(
        "verified",
        True,
        "round_growth",
        (("stats.goals", 2),),
        complete_weights=True,
    )
    causal = holdet.build_player_change_explanation(
        previous, current, key, rule_profile=profile
    )
    assert causal is not None and causal.decomposition == "causal"


def test_player_schema_four_preserves_optional_public_fields() -> None:
    item = replace(
        entry(1, popularity=12.5),
        popularity_change=-1.2,
        trend=3,
        index=88,
        stats=(holdet.PlayerPerformanceStat("assists", 4),),
        total_stats=(holdet.PlayerPerformanceStat("minutes", 900),),
    )
    game = snapshot(1, (item,)).statistics
    payload = holdet.player_statistics_to_dict(game, generated_at=NOW)
    assert payload["schema_version"] == 4
    assert holdet.player_statistics_from_dict(payload) == game


def test_settings_three_migrates_in_memory_without_startup_write_and_saves_four() -> None:
    legacy_entry = holdet.watchlist_entry(GAME, entry(1))
    legacy = {
        "schema_version": 3,
        "watchlist": [
            {
                "game_locale": legacy_entry.game_locale,
                "game_slug": legacy_entry.game_slug,
                "player_key": legacy_entry.player_key,
                "entry_id": legacy_entry.entry_id,
                "person_id": legacy_entry.person_id,
                "name": legacy_entry.name,
                "team": legacy_entry.team,
                "position": legacy_entry.position,
            }
        ],
        "manager_aliases": [],
        "manager_profiles": [],
        "hall_of_fame_score": {
            "group_points": [10, 6, 3, 1],
            "tournament_winner": 10,
            "tournament_finalist": 6,
            "tournament_semifinalist": 3,
            "global_round_win": 1,
        },
        "player_annotations": [],
        "saved_player_filters": [],
        "own_teams": [],
        "experimental_games": [],
    }
    with writable_directory() as root:
        path = root / "hub.json"
        path.write_text(json.dumps(legacy), encoding="utf-8")
        before = path.read_bytes()
        store = holdet.HubSettingsStore(path)
        settings = store.load()
        assert path.read_bytes() == before
        assert settings.watchlist[0].rules == (holdet.DEFAULT_STATUS_WATCH_RULE,)
        column = holdet.ComputedPlayerColumn(
            "da", GAME.slug, "form-price", "Form pr. pris", "form_3 / value"
        )
        updated = store.set_computed_player_columns(settings, (column,))
        assert updated.computed_player_columns == (column,)
        assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 4


def test_bulk_settings_write_is_all_or_nothing() -> None:
    with writable_directory() as root:
        path = root / "hub.json"
        store = holdet.HubSettingsStore(path)
        settings = holdet.HubSettings(watchlist=(holdet.watchlist_entry(GAME, entry(1)),))
        store.save(settings)
        before = path.read_bytes()
        duplicate = settings.watchlist[0]
        with pytest.raises(ValueError, match="dublerede"):
            store.apply_player_bulk_update(
                settings, watchlist=(duplicate, duplicate)
            )
        assert path.read_bytes() == before


def test_export_three_freezes_formula_definition_values_and_errors() -> None:
    statistics = snapshot(1, (entry(1, value=10), entry(2, value=0))).statistics
    good = holdet.ComputedPlayerColumn(
        "da", GAME.slug, "double", "Dobbelt", "value * 2"
    )
    failing = holdet.ComputedPlayerColumn(
        "da", GAME.slug, "ratio", "Ratio", "value / round_growth"
    )
    document = holdet.build_player_export(
        statistics,
        holdet.PlayerStatisticsQuery(
            computed_player_column_ids=("double", "ratio")
        ),
        generated_at=NOW,
        computed_columns=(good, failing),
        computed_contexts={2: {"round_growth": 0}},
    )
    payload = holdet.player_export_to_dict(document)
    assert payload["schema_version"] == 3
    assert payload["computed_columns"][0]["expression"] == "value * 2"
    assert payload["rows"][0]["double"] == 20
    assert payload["rows"][1]["ratio"] is None
    assert payload["formula_error_count"] == 1
