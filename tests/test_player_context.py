"""Player comparisons must use one explicit round and fetch context."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import holdet_lib as holdet


GAME = holdet.GameUrl(
    "https://www.holdet.dk/da/fantasy/context-season", "da", "context-season"
)
NOW = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)


def snapshot(round_number, *, minute=None, value=None, growth=None, status="complete", entries=None):
    player = holdet.PlayerEntry(
        0,
        "Player",
        "Club",
        "Forward",
        value if value is not None else round_number * 1_000_000,
        entry_id=1,
        total_growth=round_number * 100_000,
        round_growth=growth if growth is not None else round_number * 10,
    )
    generated = NOW + timedelta(minutes=minute if minute is not None else round_number)
    return holdet.PlayerStatisticsSnapshot(
        Path(f"round-{round_number}-{generated.minute}.json"),
        generated,
        holdet.ScrapedGame(
            GAME, "soccer", round_number, (player,) if entries is None else entries,
            "soccer", "money", status,
        ),
    )


def player_key():
    return holdet.player_identity(GAME, snapshot(1).statistics.entries[0])


def test_latest_round_selection_does_not_change_newest_fetch_semantics():
    current = snapshot(3)
    backfill = snapshot(1, minute=9)
    other = replace(
        snapshot(9, minute=10),
        statistics=replace(
            snapshot(9).statistics,
            game=holdet.GameUrl(
                "https://www.holdet.dk/da/fantasy/other-season", "da", "other-season"
            ),
        ),
    )
    index = holdet.PlayerStatisticsIndex((current, backfill, other))

    assert index.newest(GAME) is backfill
    assert index.latest_round(GAME) is current
    assert index.latest_round(GAME, round_number=2) is backfill
    assert index.per_round(GAME) == (backfill, current)


def test_round_and_fetch_cutoffs_are_inclusive_and_keep_snapshot_fields_paired():
    original = snapshot(2, minute=4, status="in_progress")
    correction = snapshot(2, minute=8, value=2_500_000)
    late_backfill = snapshot(1, minute=9)
    early_future_round = snapshot(3, minute=3)
    index = holdet.PlayerStatisticsIndex(
        (original, correction, late_backfill, early_future_round), ("warning",)
    )

    bounded = index.through(GAME, round_number=2, generated_at=original.generated_at)
    assert bounded.snapshots == (original,)
    assert bounded.warnings == ("warning",)
    assert bounded.latest_round(GAME) is original
    assert index.per_round(GAME, round_number=2) == (late_backfill, correction)
    assert index.latest_round(GAME, round_number=0) is None
    for invalid_round in (-1, True, 1.5):
        with pytest.raises(ValueError):
            index.through(GAME, round_number=invalid_round)


def test_analysis_bounds_exclude_future_rounds_and_later_corrections():
    source = snapshot(3)
    correction = snapshot(2, minute=9, value=9_000_000, growth=900)
    index = holdet.PlayerStatisticsIndex(
        tuple(snapshot(number) for number in range(1, 6)) + (correction,)
    )

    analysis = holdet.build_player_decision_analysis(
        index, GAME, player_key(), round_number=3, generated_at=source.generated_at
    )
    assert analysis is not None
    assert analysis.latest_value == 3_000_000
    assert analysis.form_3 == 20
    assert analysis.form_5 is None
    assert analysis.curve == ((1, 1_000_000), (2, 2_000_000), (3, 3_000_000))
    assert analysis.source_round == 3
    assert analysis.source_generated_at == source.generated_at
    assert analysis.source_round_status == "complete"
    assert analysis.metric_provenance("form_3").rounds == (1, 2, 3)

    corrected = holdet.build_player_decision_analysis(
        index, GAME, player_key(), round_number=3
    )
    assert corrected is not None
    assert corrected.form_3 == pytest.approx((10 + 900 + 30) / 3)
    assert corrected.source_round == 3
    assert index.newest(GAME) is correction


@pytest.mark.parametrize(
    ("old_status", "new_status"),
    (("in_progress", "complete"), ("complete", "in_progress")),
)
def test_refetch_value_status_and_form_use_the_same_newest_snapshot(old_status, new_status):
    previous = snapshot(3, minute=3, value=3_000_000, status=old_status)
    newest = snapshot(3, minute=4, value=3_500_000, status=new_status)
    index = holdet.PlayerStatisticsIndex((snapshot(1), snapshot(2), previous, newest))

    analysis = holdet.build_player_decision_analysis(index, GAME, player_key())
    assert analysis is not None
    assert analysis.observations[-1].value == 3_500_000
    assert analysis.observations[-1].round_status == new_status
    assert analysis.observations[-1].generated_at == newest.generated_at
    assert analysis.curve[-1] == (3, 3_500_000)
    assert analysis.form_3 == (20 if new_status == "complete" else None)
    assert analysis.metric_provenance("form_3").sample_size == (
        3 if new_status == "complete" else 2
    )


@pytest.mark.parametrize(("status", "certainty"), (("in_progress", "preliminary"), ("unknown", "unverified")))
def test_latest_inputs_determine_value_certainty_without_downgrading_completed_form(status, certainty):
    index = holdet.PlayerStatisticsIndex(
        tuple(snapshot(number) for number in range(1, 4))
        + (snapshot(4, status=status),)
    )

    analysis = holdet.build_player_decision_analysis(index, GAME, player_key())
    assert analysis is not None
    assert analysis.growth_per_million == 100_000
    assert analysis.provenance.certainty == certainty
    assert analysis.metric_provenance("growth_per_million").certainty == certainty
    assert analysis.metric_provenance("growth_per_million").rounds == (4,)
    assert analysis.metric_provenance("form_3").certainty == "final"
    assert analysis.metric_provenance("form_3").rounds == (1, 2, 3)
    assert analysis.metric_provenance("form_5").certainty == "unverified"
    assert analysis.metric_provenance("form_5").missing_reasons


def test_form_exposes_sparse_observation_windows_and_insufficient_samples():
    index = holdet.PlayerStatisticsIndex(tuple(snapshot(number) for number in (1, 3, 7)))
    analysis = holdet.build_player_decision_analysis(index, GAME, player_key())

    assert analysis is not None
    assert analysis.form_3 == pytest.approx(110 / 3)
    assert analysis.form_5 is None
    assert analysis.metric_provenance("form_3").rounds == (1, 3, 7)
    assert analysis.metric_provenance("form_5").sample_size == 3
    assert analysis.observation_gaps("form_3") == (2, 4, 5, 6)
    assert analysis.observation_gaps("value") == ()


@pytest.mark.parametrize(
    ("unit", "value", "total_growth", "reason"),
    (
        ("money", 0, 100_000, "Prisen"),
        ("money", 1_000_000, None, "Totalvækst"),
        ("points", 123, 20, "pengespil"),
    ),
)
def test_unavailable_efficiency_is_missing_with_an_explicit_reason(unit, value, total_growth, reason):
    base = snapshot(1, value=value)
    player = replace(base.statistics.entries[0], total_growth=total_growth)
    current = replace(
        base,
        statistics=replace(
            base.statistics,
            entries=(player,),
            unit=unit,
            variant="golf" if unit == "points" else "soccer",
            format="golf" if unit == "points" else "soccer",
        ),
    )

    analysis = holdet.build_player_decision_analysis(
        holdet.PlayerStatisticsIndex((current,)), GAME, player_key()
    )
    assert analysis is not None
    assert analysis.growth_per_million is None
    provenance = analysis.metric_provenance("growth_per_million")
    assert provenance.certainty == "unverified"
    assert reason in provenance.missing_reasons[0]


def test_removed_player_retains_last_known_values_without_claiming_current_presence():
    index = holdet.PlayerStatisticsIndex(
        tuple(snapshot(number) for number in range(1, 4))
        + (snapshot(4, entries=()),)
    )
    analysis = holdet.build_player_decision_analysis(index, GAME, player_key())

    assert analysis is not None
    assert analysis.latest_value == 3_000_000
    assert analysis.source_round == 3
    assert not analysis.present_in_selected_round
    assert analysis.metric_provenance("value").certainty == "unverified"
    assert analysis.metric_provenance("value").missing_reasons
    assert analysis.metric_provenance("form_3").certainty == "final"
    assert analysis.curve[-1] == (3, 3_000_000)


def test_removed_same_round_player_does_not_resurrect_obsolete_curve_or_form():
    old = snapshot(1)
    index = holdet.PlayerStatisticsIndex((old, snapshot(1, minute=2, entries=())))
    analysis = holdet.build_player_decision_analysis(index, GAME, player_key())

    assert analysis is not None
    assert analysis.latest_value == old.statistics.entries[0].value
    assert not analysis.present_in_selected_round
    assert analysis.curve == ()
    assert analysis.metric_provenance("form_3").sample_size == 0


def test_scouting_uses_the_same_round_and_fetch_scope_as_player_analysis():
    correction = snapshot(2, minute=9, growth=900)
    index = holdet.PlayerStatisticsIndex(
        tuple(snapshot(number) for number in range(1, 6)) + (correction,)
    )

    metrics = holdet.build_scouting_metrics(
        index, GAME, round_number=3, generated_at=snapshot(3).generated_at
    )
    assert len(metrics) == 1
    assert metrics[0].value == 3_000_000
    assert metrics[0].form_3 == 20
    assert metrics[0].form_5 is None
    assert holdet.build_scouting_metrics(index, GAME)[0].value == 5_000_000
