from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import tempfile
import typing
import zipfile

import pytest

import holdet_lib as holdet


NOW = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
GAME = holdet.GameUrl("https://www.holdet.dk/da/fantasy/test-game", "da", "test-game")


def test_public_hub_interfaces_are_exported_and_type_hints_resolve() -> None:
    expected = {
        "BackupManifest",
        "BackupManifestEntry",
        "BackupValidation",
        "CYCLING_RULES",
        "DataQualityReport",
        "DataQualityRound",
        "GameMetadata",
        "GameMetadataStore",
        "FOOTBALL_RULES",
        "GOLF_RULES",
        "HallOfFame",
        "HallOfFameEvent",
        "HallOfFamePlacement",
        "HallOfFameRow",
        "HallOfFameScoreProfile",
        "HallOfFameStore",
        "HistoryPoint",
        "HubSettings",
        "HubSettingsStore",
        "ManagerAlias",
        "MOTOR_RULES",
        "PlayerHistoryPoint",
        "PlayerSnapshotChange",
        "RestoreResult",
        "ScenarioPlayer",
        "SnapshotDiff",
        "TeamRankChange",
        "TeamSnapshotDiff",
        "TransferRuleProfile",
        "TransferScenario",
        "TransferValidation",
        "UNKNOWN_RULES",
        "WatchlistEntry",
        "build_data_quality_report",
        "build_hall_of_fame",
        "build_history_series",
        "build_live_hall_of_fame_events",
        "build_player_history",
        "compare_round_snapshots",
        "compare_snapshots",
        "compare_team_rounds",
        "compare_team_snapshots",
        "create_backup",
        "create_backup_bytes",
        "game_metadata_from_context",
        "latest_snapshot_diff",
        "manager_identity_keys",
        "player_identity",
        "resolve_manager_identity",
        "restore_backup",
        "simulate_transfers",
        "transfer_rule_profile",
        "validate_backup",
        "watchlist_entry",
    }
    assert expected <= set(holdet.__all__)
    assert all(hasattr(holdet, name) for name in expected)

    snapshot_hints = typing.get_type_hints(holdet.SnapshotDiff)
    team_hints = typing.get_type_hints(holdet.TeamSnapshotDiff)
    quality_hints = typing.get_type_hints(holdet.DataQualityRound)
    assert "current_round_status" in snapshot_hints
    assert "current_round_status" in team_hints
    assert "player_round_status" in quality_hints


def roster_entry(
    player_id: int,
    position: str,
    *,
    team: str = "Club",
    value: int = 100,
    role: str = "",
) -> holdet.RosterEntry:
    return holdet.RosterEntry(
        player_id,
        player_id,
        f"Player {player_id}",
        team,
        position,
        value,
        0,
        0,
        1,
        role,
    )


def player_entry(
    player_id: int,
    position: str,
    *,
    team: str = "Club",
    value: int = 100,
    active: bool = True,
    disabled: bool = False,
) -> holdet.PlayerEntry:
    return holdet.PlayerEntry(
        player_id,
        f"Player {player_id}",
        team,
        position,
        value,
        is_active=active,
        is_disabled=disabled,
        entry_id=player_id,
        total_growth=10,
        round_growth=2,
    )


def scenario_for(
    roster: tuple[holdet.RosterEntry, ...],
    purchase: holdet.PlayerEntry,
    *,
    sold_id: int,
    bank: int | None = 500,
    contracts: int | None = 5,
    round_number: int = 3,
) -> holdet.TransferScenario:
    return holdet.TransferScenario(
        roster,
        (purchase,),
        (sold_id,),
        (purchase.entry_id,),
        bank,
        contracts,
        round_number,
        round_number,
        round_number,
    )


def test_all_transfer_profiles_and_fee_rounding() -> None:
    football = (
        roster_entry(1, "Goalkeeper", team="A"),
        *(roster_entry(i, "Defender", team=chr(65 + i % 4)) for i in range(2, 6)),
        *(roster_entry(i, "Midfielder", team=chr(65 + i % 4)) for i in range(6, 10)),
        roster_entry(10, "Forward", team="B"),
        roster_entry(11, "Forward", team="C", value=101),
    )
    result = holdet.simulate_transfers(
        holdet.FOOTBALL_RULES,
        scenario_for(
            football,
            player_entry(12, "Forward", team="D", value=101),
            sold_id=11,
        ),
    )
    assert result.status == "valid"
    assert result.transfer_fee == 2
    assert result.ending_bank == 498
    assert result.contracts_used == 1

    cycling = tuple(
        roster_entry(i, "Rider", team=f"T{i // 2}") for i in range(1, 9)
    )
    result = holdet.simulate_transfers(
        holdet.CYCLING_RULES,
        scenario_for(
            cycling,
            player_entry(9, "Rider", team="T5"),
            sold_id=8,
        ),
    )
    assert result.status == "valid"

    motor = (
        *(roster_entry(i, "Driver", team=f"M{i}") for i in range(1, 5)),
        roster_entry(5, "Constructor", team="C1"),
        roster_entry(6, "Constructor", team="C2"),
        roster_entry(7, "Pit crew", team="P1"),
    )
    result = holdet.simulate_transfers(
        holdet.MOTOR_RULES,
        scenario_for(
            motor,
            player_entry(8, "Driver", team="M8"),
            sold_id=4,
        ),
    )
    assert result.status == "valid"
    assert result.transfer_fee == 0

    golf = tuple(
        roster_entry(index + 1, f"Category {index // 3 + 1}", team=f"G{index}")
        for index in range(15)
    )
    result = holdet.simulate_transfers(
        holdet.GOLF_RULES,
        scenario_for(
            golf,
            player_entry(20, "Category 5", team="G20"),
            sold_id=15,
            bank=None,
        ),
    )
    assert result.status == "valid"
    assert result.ending_bank is None


def test_transfer_contract_round_mismatch_status_and_unknown_rules() -> None:
    roster = tuple(roster_entry(i, "Rider", team=f"T{i}") for i in range(1, 9))
    disabled = player_entry(9, "Rider", team="T9", disabled=True)
    scenario = replace(
        scenario_for(roster, disabled, sold_id=8, contracts=0),
        player_round=2,
    )
    result = holdet.simulate_transfers(holdet.CYCLING_RULES, scenario)
    assert result.status == "invalid"
    assert any("Datarunder" in error for error in result.errors)
    assert any("inaktiv" in error for error in result.errors)
    assert any("kontrakter" in error.lower() for error in result.errors)

    unverified = holdet.simulate_transfers(holdet.UNKNOWN_RULES, scenario)
    assert unverified.status == "unverified"
    assert unverified.is_valid is None

    captain_profile = holdet.TransferRuleProfile(
        "captain-test",
        "Captain test",
        1,
        budget_enabled=False,
        captain_count=1,
    )
    captain_scenario = holdet.TransferScenario(
        (roster_entry(1, "Any"),),
        (),
        captain_player_ids=(1,),
    )
    assert holdet.simulate_transfers(captain_profile, captain_scenario).status == "valid"


def player_snapshot(
    round_number: int,
    generated: datetime,
    entries: tuple[holdet.PlayerEntry, ...],
) -> holdet.PlayerStatisticsSnapshot:
    game = holdet.ScrapedGame(
        GAME,
        "soccer",
        round_number,
        entries,
        format="soccer",
        unit="money",
        round_status="complete",
    )
    return holdet.PlayerStatisticsSnapshot(Path(f"round-{round_number}.json"), generated, game)


def test_snapshot_diff_uses_entry_identity_and_legacy_fallback() -> None:
    old = player_snapshot(
        1,
        NOW,
        (
            player_entry(1, "Forward", value=100),
            replace(player_entry(2, "Forward"), entry_id=None, name="Legacy"),
        ),
    )
    new = player_snapshot(
        1,
        NOW + timedelta(hours=1),
        (
            replace(player_entry(1, "Forward", value=125), is_injured=True),
            replace(player_entry(2, "Forward"), entry_id=None, name="Legacy"),
            player_entry(3, "Forward"),
        ),
    )
    diff = holdet.compare_snapshots(new, old)
    assert [item.name for item in diff.added_players] == ["Player 3"]
    assert [item.name for item in diff.price_changes] == ["Player 1"]
    assert [item.name for item in diff.status_changes] == ["Player 1"]
    assert len(diff.removed_players) == 0


def team_snapshot(
    team_id: int,
    history: tuple[holdet.RoundSummary, ...],
) -> holdet.TeamSnapshot:
    reference = holdet.TeamReference(
        GAME,
        team_id,
        f"Team {team_id}",
        f"https://www.holdet.dk/da/fantasy/test-game/team/{team_id}",
        account_key=f"a{team_id}",
        account_label=f"Manager {team_id}",
        account_user_id=team_id,
    )
    team = holdet.ScrapedTeam(
        reference,
        "soccer",
        1,
        reference.team_name,
        f"Manager {team_id}",
        team_id,
        holdet.TeamOverview(
            max(item.round_number for item in history),
            "money",
            1000,
            100,
            history[-1].total,
            history[-1].change,
            team_id,
            0,
            10,
            5,
            5,
            0,
        ),
        (),
        history,
    )
    return holdet.TeamSnapshot(Path(f"team-{team_id}.json"), NOW, team)


def summary(round_number: int, total: int, change: int) -> holdet.RoundSummary:
    return holdet.RoundSummary(
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
        overall_rank=total,
        round_status="complete",
    )


def test_history_keeps_round_holes_and_calculates_group_rank() -> None:
    first = team_snapshot(1, (summary(1, 100, 10), summary(3, 130, 30)))
    second = team_snapshot(2, (summary(1, 90, 9), summary(3, 140, 50)))
    index = holdet.SnapshotIndex((first, second))
    group = holdet.GroupDefinition(
        "group",
        "Group",
        GAME,
        (
            holdet.GroupTeam(1, "Team 1", first.team.reference.source_url),
            holdet.GroupTeam(2, "Team 2", second.team.reference.source_url),
        ),
    )
    points = holdet.build_history_series(
        index,
        GAME,
        (1, 2),
        group=group,
    )
    holes = [item for item in points if item.round_number == 2]
    assert len(holes) == 2
    assert all(item.total is None for item in holes)
    latest = {item.team_id: item for item in points if item.round_number == 3}
    assert latest[2].group_rank == 1
    assert latest[1].group_rank == 2


def test_hall_of_fame_ties_deduplication_scoring_and_idempotent_freeze(tmp_path: Path) -> None:
    placements = (
        holdet.HallOfFamePlacement("m1", "One", 1, "A", 1, 100),
        holdet.HallOfFamePlacement("m1", "One", 2, "B", 2, 90),
        holdet.HallOfFamePlacement("m2", "Two", 3, "C", 2, 90),
    )
    event = holdet.HallOfFameEvent(
        "event",
        "group",
        "da",
        "test",
        "group",
        "Group",
        3,
        placements,
        True,
        NOW,
    )
    round_events = tuple(
        holdet.HallOfFameEvent(
            f"round-{number}",
            "round_win",
            "da",
            "test",
            f"round:{number}",
            f"Round {number}",
            number,
            (holdet.HallOfFamePlacement("m1", "One", 1, "A", 1, 20 + number),),
            True,
            NOW,
        )
        for number in (1, 2, 4)
    )
    board = holdet.build_hall_of_fame((event, *round_events))
    assert board.rows[0].manager_id == "m1"
    assert board.rows[0].points == 13
    assert board.rows[0].longest_round_win_streak == 2
    assert next(row for row in board.rows if row.manager_id == "m2").points == 6

    store = holdet.HallOfFameStore(tmp_path)
    first = store.freeze(event)
    second = store.freeze(replace(event, captured_at=NOW + timedelta(hours=1)))
    assert first == second
    assert len(store.scan()[0]) == 1


def paths_for(root: Path) -> holdet.AppPaths:
    return holdet.resolve_paths(
        overrides=holdet.PathOverrides(data_root=root),
        environ={},
    )


def test_backup_checksums_traversal_and_restore(tmp_path: Path) -> None:
    source_paths = paths_for(tmp_path / "source")
    source_paths.config_dir.mkdir(parents=True)
    source_paths.groups_file.write_text(
        json.dumps({"schema_version": 7, "games": [], "groups": []}),
        encoding="utf-8",
    )
    source_paths.hub_settings_file.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "watchlist": [],
                "manager_aliases": [],
                "hall_of_fame_score": {
                    "group_points": [10, 6, 3, 1],
                    "tournament_winner": 10,
                    "tournament_finalist": 6,
                    "tournament_semifinalist": 3,
                    "global_round_win": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    data, manifest = holdet.create_backup_bytes(source_paths, now=NOW)
    validation = holdet.validate_backup(data)
    assert validation.is_valid
    assert len(manifest.files) == 2

    target_paths = paths_for(tmp_path / "target")
    target_paths.config_dir.mkdir(parents=True)
    target_paths.groups_file.write_text("old", encoding="utf-8")
    result = holdet.restore_backup(data, target_paths, now=NOW)
    assert result.rollback_path.exists()
    assert json.loads(target_paths.groups_file.read_text(encoding="utf-8"))["schema_version"] == 7

    bad = BytesIO()
    with zipfile.ZipFile(bad, "w") as archive:
        archive.writestr("../escape.json", "{}")
        archive.writestr("backup-manifest.json", "{}")
    assert not holdet.validate_backup(bad.getvalue()).is_valid

    tampered = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as original, zipfile.ZipFile(tampered, "w") as changed:
        for info in original.infolist():
            payload = original.read(info.filename)
            if info.filename == "config/groups.json":
                payload += b"x"
            changed.writestr(info, payload)
    invalid = holdet.validate_backup(tampered.getvalue())
    assert not invalid.is_valid
    assert any("Checksum" in error or "Filst" in error for error in invalid.errors)


def test_settings_and_metadata_are_additive_and_versioned(tmp_path: Path) -> None:
    settings_store = holdet.HubSettingsStore(tmp_path / "hub.json")
    assert settings_store.load() == holdet.HubSettings()
    watched = holdet.watchlist_entry(GAME, player_entry(7, "Forward"))
    updated = settings_store.set_watchlist(holdet.HubSettings(), (watched,))
    assert settings_store.load() == updated

    context = holdet.GameContext(
        GAME,
        "soccer",
        "soccer",
        5,
        6,
        7,
        50_000_000,
        2,
        "Test",
        (
            holdet.ScheduleRound(
                1,
                NOW - timedelta(days=2),
                NOW - timedelta(days=1),
                NOW,
            ),
        ),
    )
    metadata_store = holdet.GameMetadataStore(tmp_path / "metadata")
    metadata_store.save(context, fetched_at=NOW)
    loaded = metadata_store.load(GAME)
    assert loaded is not None
    assert loaded.final_round == 2

    (tmp_path / "hub.json").write_text(
        json.dumps({"schema_version": 99}),
        encoding="utf-8",
    )
    with pytest.raises(holdet.PayloadError):
        settings_store.load()



def test_context_routes_are_cache_only_and_old_routes_are_not_found() -> None:
    from streamlit.testing.v1 import AppTest
    from tests.test_website import APP_PATH, navigate, website_environment
    from unittest.mock import patch

    with website_environment() as (config, output):
        manager_game = holdet.GroupStore(
            config / "groups.json"
        ).create_manager_game(GAME, "Testspil")
        before = tuple(
            sorted(
                path.relative_to(config.parent)
                for path in config.parent.rglob("*")
            )
        )
        with patch(
            "holdet_lib.HoldetClient",
            side_effect=AssertionError("network"),
        ):
            app = AppTest.from_file(APP_PATH).run(timeout=15)
            assert not app.exception
            for route in (
                "transfer",
                "compare",
                "history",
                "changes",
                "quality",
                "backup",
            ):
                navigate(app, route)
                assert not app.exception, route
                assert any(
                    item.value == "Siden findes ikke" for item in app.title
                )
            for section in (
                "round-center",
                "groups",
                "players",
                "teams",
                "history",
                "administration",
                "settings",
            ):
                navigate(
                    app,
                    "game",
                    locale=manager_game.game.locale,
                    game=manager_game.game.slug,
                    section=section,
                )
                assert not app.exception, section
            navigate(app, "hall-of-fame")
            assert not app.exception
            for section in ("quality", "backup"):
                navigate(app, "data", section=section)
                assert not app.exception, section
        after = tuple(
            sorted(
                path.relative_to(config.parent)
                for path in config.parent.rglob("*")
            )
        )
        assert after == before



def test_round_aware_diff_uses_previous_available_round_and_latest_fetch() -> None:
    round_one = player_snapshot(
        1,
        NOW,
        (player_entry(1, "Forward", value=100),),
    )
    older_round_three = player_snapshot(
        3,
        NOW + timedelta(hours=1),
        (player_entry(1, "Forward", value=120),),
    )
    newest_round_three = replace(
        player_snapshot(
            3,
            NOW + timedelta(hours=2),
            (player_entry(1, "Forward", value=140),),
        ),
        statistics=replace(
            player_snapshot(
                3,
                NOW + timedelta(hours=2),
                (player_entry(1, "Forward", value=140),),
            ).statistics,
            round_status="in_progress",
        ),
    )
    diff = holdet.compare_round_snapshots(
        holdet.PlayerStatisticsIndex(
            (older_round_three, round_one, newest_round_three)
        ),
        holdet.SnapshotIndex(()),
        GAME,
        3,
    )
    assert diff is not None
    assert diff.previous_round == 1
    assert diff.current_round == 3
    assert diff.price_changes[0].old_value == 100
    assert diff.price_changes[0].new_value == 140
    assert not diff.is_final


def test_transfer_certainty_is_separate_from_rule_validity() -> None:
    roster = tuple(
        roster_entry(i, "Rider", team=f"T{i}") for i in range(1, 9)
    )
    scenario = scenario_for(
        roster,
        player_entry(9, "Rider", team="T9"),
        sold_id=8,
    )
    preliminary = holdet.simulate_transfers(holdet.CYCLING_RULES, scenario)
    assert preliminary.status == "valid"
    assert preliminary.certainty == "preliminary"

    final = holdet.simulate_transfers(
        holdet.CYCLING_RULES,
        replace(
            scenario,
            team_round_status="complete",
            player_round_status="complete",
        ),
    )
    assert final.status == "valid"
    assert final.certainty == "final"

    unverified = holdet.simulate_transfers(
        holdet.CYCLING_RULES,
        replace(scenario, player_round=2),
    )
    assert unverified.status == "invalid"
    assert unverified.certainty == "unverified"


def test_data_quality_report_has_names_and_actionable_readiness(
    tmp_path: Path,
) -> None:
    manager_game = holdet.ManagerGame(GAME, "Testspil")
    first = team_snapshot(1, (summary(1, 100, 10),))
    group = holdet.GroupDefinition(
        "quality",
        "Kvalitet",
        GAME,
        (
            holdet.GroupTeam(1, "Team 1", first.team.reference.source_url),
            holdet.GroupTeam(
                2,
                "Team 2",
                "https://www.holdet.dk/da/fantasy/test-game/team/2",
            ),
        ),
    )
    report = holdet.build_data_quality_report(
        (manager_game,),
        (group,),
        holdet.SnapshotIndex((first,)),
        holdet.PlayerStatisticsIndex(()),
        manifest_dir=tmp_path,
        now=NOW,
    )
    assert len(report.rounds) == 1
    quality = report.rounds[0]
    assert quality.game_name == "Testspil"
    assert quality.readiness == "missing"
    assert quality.missing_team_names == ("Team 2",)
    assert "Spillersnapshot mangler" in quality.reasons
