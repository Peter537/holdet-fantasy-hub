"""Regression coverage for the shared UI and official-result boundaries."""

from dataclasses import replace
import json
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

import holdet_lib as holdet
from tests.test_completed_scope_export import completed_run, export_cache
from tests.test_manager_seasons_tournaments import group_for, snapshots, team_with_rounds
from tests.test_refresh_foundation import _manager_game_and_group, _manifest
from tests.test_tournament import snapshot_index, team_with_changes, tournament_group
from tests.test_website import APP_PATH, navigate, website_environment, widget
from website import ui, workflows


def test_unreadable_pairings_block_results_and_competition_awards():
    teams = tuple(team_with_changes(i, {n: 40 - i for n in range(1, 6)}) for i in range(1, 5))
    group = tournament_group(teams)
    index = snapshot_index(*teams)
    assert holdet.build_tournament_state(group, index, 5).champion_id is not None
    with patch.object(holdet.TournamentPairingStore, "with_published_knockout_matches", side_effect=holdet.PayloadError("unreadable")):
        resolved, warnings = ui._with_published_tournament_pairings((group,), index)
    assert warnings and resolved[0].tournament.pairing_error
    assert group.tournament.pairing_error is None
    state = holdet.build_tournament_state(resolved[0], index, 5)
    assert state.champion_id is None
    events = holdet.build_live_hall_of_fame_events(resolved, index, holdet.HubSettings())
    assert not any(event.complete and event.kind in {"tournament", "tournament_group"} for event in events)


def test_pairing_error_excludes_manager_official_meetings_but_keeps_shared_rounds():
    first = team_with_rounds(1, {1: 10, 2: 20, 3: 30, 4: 40}, owner_user_id=900)
    second = team_with_rounds(2, {1: 20, 2: 10, 3: 20, 4: 30}, owner_user_id=901)
    config = holdet.create_tournament_config((1, 2), 1, 4, 2, shuffle=lambda values: None)
    group = replace(group_for("blocked-official", first, second), kind="tournament", tournament=config)
    index = snapshots(first, second)
    before = holdet.build_manager_head_to_head("owner:900", "owner:901", (group,), index, holdet.HubSettings())
    blocked = replace(group, tournament=replace(config, pairing_error="review required"))
    after = holdet.build_manager_head_to_head("owner:900", "owner:901", (blocked,), index, holdet.HubSettings())
    assert before.official
    assert not after.official
    assert after.shared_rounds == before.shared_rounds


def test_completed_export_survives_unreadable_metadata(export_cache):
    with (
        patch.object(workflows.GameMetadataStore, "load", side_effect=holdet.PayloadError("bad metadata")),
        patch.object(workflows.st, "download_button") as download,
        patch.object(workflows.st, "warning") as warning,
    ):
        workflows._completed_export(export_cache.paths, completed_run(export_cache))
        download.assert_called_once()
        data = json.loads(download.call_args.kwargs["data"]())
    assert data["metadata"]["current"] is None
    assert data["metadata"]["quality"] == ["missing"]
    assert len(data["players"]["current"]["entries"]) == 1101
    warning.assert_called_once()


def test_corrupt_settings_keep_core_pages_readable_and_settings_bytes_intact():
    with website_environment() as (config, output):
        manager = holdet.GroupStore(config / "groups.json").create_manager_game("demo-game", "Spil")
        settings_path = config / "hub-settings.json"
        original = b'{"schema_version":broken}'
        settings_path.write_bytes(original)
        with patch("holdet_lib.http.HttpClient.fetch_text", side_effect=AssertionError("network")):
            app = AppTest.from_file(APP_PATH).run(timeout=15)
            assert not app.exception
            navigate(app, "game", locale=manager.game.locale, game=manager.game.slug, section="settings")
            assert not app.exception
            assert widget(app, "button", "Gem mit hold").disabled
            navigate(app, "settings")
            assert not app.exception
            assert widget(app, "button", "Gem værktøjer").disabled
        assert settings_path.read_bytes() == original


def test_round_center_retry_queues_saved_manifest_and_rejects_other_scopes(tmp_path):
    from datetime import datetime, timezone
    paths = holdet.resolve_paths(overrides=holdet.PathOverrides(data_root=tmp_path))
    manager, group = _manager_game_and_group(1)
    manifest = replace(_manifest(datetime.now(timezone.utc)), game_slug=manager.game.slug)
    store = holdet.ManifestStore(paths.manifest_dir)
    stored = store.load(store.write(manifest))
    with patch.object(workflows, "_start") as start, patch.object(workflows.st, "error") as error:
        workflows.start_manager_update(paths, manager, retry_manifest=stored)
        task = start.call_args.args[1][0]
        assert paths.data_dir / task["retry_manifest_path"] == stored.path
        error.assert_not_called()
        start.reset_mock()
        foreign = replace(manifest, scope="group", group_id=group.group_id)
        workflows.start_manager_update(paths, manager, retry_manifest=store.load(store.write(foreign)))
        start.assert_not_called()
        error.assert_called_once()


def _own_team_retry_dialog(manager, groups, manifest):
    from website import ui

    ui._configure_paths()
    ui._manager_game_refresh_dialog(manager, groups, manifest)


def test_round_center_can_retry_saved_own_team_outside_leagues():
    from datetime import datetime, timezone
    from tests.test_refresh_foundation import _previous_manifest

    with website_environment() as (config, _output):
        manager, group = _manager_game_and_group(1)
        holdet.GroupStore(config / "groups.json").save_configuration(
            holdet.HubConfiguration((manager,), (group,))
        )
        holdet.HubSettingsStore(config / "hub-settings.json").save(holdet.HubSettings(
            own_teams=(holdet.OwnTeamSelection(manager.game.locale, manager.game.slug, 9),),
        ))
        manifest = _previous_manifest(manager, datetime.now(timezone.utc), (
            holdet.RefreshManifestStep("team:9", "team", "Mit hold", "failed_no_cache", True,
                                       team_id=9, error="offline"),
        ))
        before = {path: path.read_bytes() for path in config.parent.rglob("*.json")}
        with (
            patch.object(ui, "updates_running", return_value=False),
            patch.object(ui, "start_manager_update") as start,
            patch("holdet_lib.http.HttpClient.fetch_text", side_effect=AssertionError("network")),
        ):
            app = AppTest.from_function(
                _own_team_retry_dialog, args=(manager, (group,), manifest),
            ).run(timeout=15)
        assert not app.exception
        assert not widget(app, "button", "Start opdatering").disabled
        preview = next(item.value for item in app.dataframe if "Datakilde" in item.value.columns)
        own_team = next(row for row in preview.to_dict("records") if row["Datakilde"] == "Hold: Hold 9")
        assert own_team["Handling"] == "Hentes"
        start.assert_not_called()
        assert {path: path.read_bytes() for path in config.parent.rglob("*.json")} == before
