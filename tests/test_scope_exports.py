from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path

import pytest

from holdet_lib.groups import ManagerGame
from holdet_lib.game_metadata import GameMetadata
from holdet_lib.hub_settings import HubSettings, OwnTeamSelection
from holdet_lib.models import PlayerPerformanceStat
from holdet_lib.scope_exports import build_scope_export, serialize_scope_export
from holdet_lib.scopes import resolve_scope
from holdet_lib.storage import PlayerStatisticsIndex, RefreshManifest, RefreshManifestStep, SnapshotIndex
from tests.test_round_center import GAME, NOW, group, player, player_snapshot, summary, team_snapshot


def test_export_is_complete_scoped_and_latest_corrected() -> None:
    first = replace(group(1, 2), group_id="first", name="Venner æøå")
    second = replace(group(2, 3), group_id="second")
    original = team_snapshot(1, (summary(1, 100, 10, rank=1), summary(3, 300, 25, rank=1)), generated_at=NOW)
    correction = team_snapshot(1, (summary(1, 125, 15, rank=2),), generated_at=NOW + timedelta(hours=1))
    indexed = SnapshotIndex((original, correction))
    scope = resolve_scope(ManagerGame(GAME, "Cykling æøå"), (first, second), indexed,
                          HubSettings(own_teams=(OwnTeamSelection("da", GAME.slug, 9),)), competition_id="first")
    entries = tuple(replace(player(i, name=f"Spiller {i}"), value=1_234_567,
                            popularity=1.25, stats=(PlayerPerformanceStat("Mål", 2),))
                    for i in range(1, 1102))
    current = player_snapshot(3, entries, generated_at=NOW)
    corrected_old = player_snapshot(1, (player(1),), generated_at=NOW + timedelta(hours=2))
    players = PlayerStatisticsIndex((corrected_old, current))
    data = json.loads(serialize_scope_export(scope, indexed, players, include_history=True,
                                              now=NOW + timedelta(hours=3)))
    assert data["scope"]["type"] == "competition"
    assert [team["id"] for team in data["teams"]] == [1, 2]
    assert [item["id"] for item in data["competitions"]] == ["first"]
    assert data["players"]["current"]["game"]["round"] == 3
    assert len(data["players"]["current"]["entries"]) == 1101
    entry = data["players"]["current"]["entries"][0]
    assert entry["value"] == 1_234_567 and entry["popularity"] == 1.25
    assert entry["total_growth"] is None and entry["stats"] == [{"name": "Mål", "value": 2.0}]
    assert [item["game"]["round"] for item in data["players"]["history"]] == [3, 1]
    assert data["teams"][0]["snapshot"]["game"]["current_round"] == 3
    assert data["teams"][0]["summaries"][1]["total"] == 125
    assert data["teams"][1]["quality"] == ["missing"]
    assert "Venner æøå" in json.dumps(data, ensure_ascii=False)
    assert "team-1.json" not in json.dumps(data)


def test_missing_sources_and_stale_state_are_explicit() -> None:
    indexed = SnapshotIndex((team_snapshot(1, (summary(1, 20, 5, rank=None),), generated_at=NOW),),
                            warnings=(str(Path("private") / "bad.json"),))
    scope = resolve_scope(ManagerGame(GAME, "Spil"), (group(1, 2),), indexed)
    data = build_scope_export(scope, indexed, PlayerStatisticsIndex(()), now=NOW + timedelta(days=2))
    assert data["players"]["current"] is None
    assert data["players"]["quality"] == ["missing"]
    assert "stale" in data["teams"][0]["quality"]
    assert "history" not in data["players"]
    assert "private" not in json.dumps(data)


def test_tournament_export_includes_results_and_definition() -> None:
    competition = group(1, 2, tournament=True)
    teams = SnapshotIndex((team_snapshot(1, (summary(1, 20, 5, rank=1),)),
                           team_snapshot(2, (summary(1, 15, 2, rank=2),))))
    scope = resolve_scope(ManagerGame(GAME, "Spil"), (competition,), teams,
                          competition_id=competition.group_id)
    exported = build_scope_export(scope, teams, PlayerStatisticsIndex(()), now=NOW)
    item = exported["competitions"][0]
    assert item["definition"]["group_fixtures"]
    assert item["state"]["standings"]
    assert item["state"]["group_matches"]


def test_tournament_as_of_does_not_regress_when_old_round_is_corrected() -> None:
    competition = group(1, 2, tournament=True)
    current = team_snapshot(1, (summary(1, 20, 5, rank=1), summary(2, 30, 10, rank=1)),
                            generated_at=NOW)
    correction = team_snapshot(1, (summary(1, 21, 6, rank=1),),
                               generated_at=NOW + timedelta(hours=1))
    teams = SnapshotIndex((current, correction))
    scope = resolve_scope(ManagerGame(GAME, "Spil"), (competition,), teams)
    data = build_scope_export(scope, teams, PlayerStatisticsIndex(()), now=NOW)
    assert data["competitions"][0]["as_of_round"] == 2


def test_points_and_pre_completion_snapshots_are_identified() -> None:
    end_at = NOW + timedelta(hours=1)
    recorded = summary(2, 77, 5, rank=1, end_at=end_at)
    source = team_snapshot(1, (recorded,), generated_at=NOW)
    source = replace(source, team=replace(source.team, overview=replace(source.team.overview, unit="points")))
    team_index = SnapshotIndex((source,))
    player_source = player_snapshot(1, (player(1),), generated_at=NOW)
    player_source = replace(player_source, statistics=replace(player_source.statistics, unit="points"))
    scope = resolve_scope(ManagerGame(GAME, "Point"), (group(1),), team_index)
    data = build_scope_export(scope, team_index, PlayerStatisticsIndex((player_source,)),
                              now=NOW + timedelta(hours=2))
    assert data["teams"][0]["snapshot"]["game"]["unit"] == "points"
    assert data["teams"][0]["summaries"][0]["total"] == 77
    assert "before_round_end" in data["teams"][0]["quality"]
    assert "older_round" in data["players"]["quality"]


def test_metadata_and_manifest_are_projected_without_paths_or_other_teams() -> None:
    teams = SnapshotIndex(())
    scope = resolve_scope(ManagerGame(GAME, "Spil"), (group(1),), teams, competition_id="gruppe")
    metadata = GameMetadata(GAME, "soccer", "soccer", 42, 1000, 4, "Spil", (), NOW)
    steps = (
        RefreshManifestStep("players", "players", "Spillere", "fetched", True,
                            data_reference="C:/private/snapshot.json"),
        RefreshManifestStep("team:1", "team", "Hold 1", "failed_no_cache", True,
                            team_id=1, error="C:/private/error"),
        RefreshManifestStep("team:2", "team", "Hold 2", "fetched", True, team_id=2),
    )
    manifest = RefreshManifest(2, "game", "run", NOW, NOW, GAME.slug, 2, steps,
                               game_locale="da", path=Path("C:/private/manifest.json"))
    data = build_scope_export(scope, teams, PlayerStatisticsIndex(()), now=NOW,
                              metadata=metadata, manifest=manifest,
                              warnings=("C:/private/pairing.json unavailable",))
    assert data["metadata"]["current"]["game"]["game_id"] == 42
    assert [item["id"] for item in data["refresh_manifest"]["steps"]] == ["players", "team:1"]
    assert "private" not in json.dumps(data)
    assert len(data["warnings"]) == 1


def test_legacy_manifest_locale_is_resolved_only_by_matching_competition_context() -> None:
    teams = SnapshotIndex((team_snapshot(1, (summary(2, 200, 20, rank=1),)),))
    scope = resolve_scope(
        ManagerGame(GAME, "Spil"), (group(1),), teams, competition_id="gruppe"
    )
    legacy = RefreshManifest(
        1, "group", "legacy-run", NOW, NOW, GAME.slug, 2, (), group_id="gruppe"
    )

    data = build_scope_export(scope, teams, PlayerStatisticsIndex(()), manifest=legacy, now=NOW)

    assert data["refresh_manifest"]["run_id"] == "legacy-run"
    assert data["refresh_manifest"]["group_id"] == "gruppe"
    assert data["teams"][0]["summaries"][0]["total"] == 200


def test_ambiguous_legacy_manifest_does_not_prevent_available_data_export() -> None:
    teams = SnapshotIndex((team_snapshot(1, (summary(2, 200, 20, rank=1),)),))
    scope = resolve_scope(ManagerGame(GAME, "Spil"), (group(1),), teams)
    legacy = RefreshManifest(1, "game", "legacy-run", NOW, NOW, GAME.slug, 2, ())

    data = json.loads(serialize_scope_export(
        scope, teams, PlayerStatisticsIndex(()), manifest=legacy, now=NOW
    ))

    assert data["refresh_manifest"] is None
    assert len(data["warnings"]) == 1
    assert data["teams"][0]["summaries"][0]["total"] == 200


@pytest.mark.parametrize("changed", (
    {"game_locale": "en"},
    {"game_slug": "another-game"},
    {"group_id": "another-group"},
    {"schema_version": 2},
))
def test_foreign_or_nonlegacy_manifest_identity_is_still_rejected(changed) -> None:
    scope = resolve_scope(
        ManagerGame(GAME, "Spil"), (group(1),), SnapshotIndex(()), competition_id="gruppe"
    )
    legacy = RefreshManifest(
        1, "group", "legacy-run", NOW, NOW, GAME.slug, 2, (), group_id="gruppe"
    )

    with pytest.raises(ValueError):
        build_scope_export(
            scope, SnapshotIndex(()), PlayerStatisticsIndex(()),
            manifest=replace(legacy, **changed), now=NOW,
        )
