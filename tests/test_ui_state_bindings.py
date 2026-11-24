from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
import tomllib
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from streamlit.testing.v1 import AppTest

from holdet_lib.data_packages import DataPackage, DataTable
from holdet_lib.paths import PathOverrides, resolve_paths
from website import analysis_pages, data_sections


NOW = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)


def test_project_configuration_disables_streamlit_usage_telemetry() -> None:
    path = Path(__file__).resolve().parents[1] / ".streamlit" / "config.toml"
    with path.open("rb") as config:
        assert tomllib.load(config)["browser"]["gatherUsageStats"] is False


@pytest.fixture(autouse=True)
def app_script_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from streamlit.testing.v1 import app_test

    monkeypatch.setattr(app_test, "TMP_DIR", SimpleNamespace(name=str(tmp_path)))


def package(value: int = 1) -> DataPackage:
    return DataPackage(
        "manager_game_report", {"game": "demo"}, NOW, {},
        (DataTable("players", ("name", "value", "note"), (
            {"name": "Eksempel", "value": value, "note": "Lokal note"},
        )),),
    )


def report_ui() -> MagicMock:
    ui = MagicMock()
    ui.session_state = {}
    ui.button.return_value = False
    ui.segmented_control.return_value = "share"
    return ui


def render_report(ui: MagicMock, paths, source: DataPackage, *, title="Rapport", stem="demo") -> None:
    with patch.object(data_sections, "st", ui):
        data_sections._report_actions(source, title=title, stem=stem, paths=paths, key="report")


@pytest.mark.parametrize("change", ("privacy", "package", "generated_at", "title", "stem"))
def test_support_download_is_invalidated_when_prepared_inputs_change(tmp_path: Path, change: str) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    source = package()
    ui = report_ui()
    ui.button.side_effect = lambda label, **_: label == "Forbered supportpakke"
    render_report(ui, paths, source)
    assert any(call.args[0] == "Download supportpakke" for call in ui.download_button.call_args_list)

    ui.reset_mock()
    ui.button.side_effect = None
    ui.button.return_value = False
    if change == "privacy":
        ui.segmented_control.return_value = "debug"
    if change == "package":
        source = package(2)
    if change == "generated_at":
        source = replace(source, generated_at=NOW.replace(minute=1))
    render_report(ui, paths, source,
                  title="Anden rapport" if change == "title" else "Rapport",
                  stem="andet" if change == "stem" else "demo")

    assert not any(call.args[0] == "Download supportpakke" for call in ui.download_button.call_args_list)
    assert "report-support" not in ui.session_state


def test_unchanged_support_remains_downloadable_and_debug_can_be_prepared(tmp_path: Path) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    ui = report_ui()
    with patch.object(data_sections, "build_support_bundle", wraps=data_sections.build_support_bundle) as build:
        ui.button.side_effect = lambda label, **_: label == "Forbered supportpakke"
        render_report(ui, paths, package())
        (prepared,) = (
            call.args[1] for call in ui.download_button.call_args_list
            if call.args[0] == "Download supportpakke"
        )
        assert isinstance(prepared, bytes) and prepared.startswith(b"PK\x03\x04")
        assert next(
            call.kwargs["file_name"] for call in ui.download_button.call_args_list
            if call.args[0] == "Download supportpakke"
        ) == "holdet-support.zip"
        ui.download_button.reset_mock()
        ui.button.side_effect = None
        ui.button.return_value = False
        render_report(ui, paths, package())
        assert [
            call.args[1] for call in ui.download_button.call_args_list
            if call.args[0] == "Download supportpakke"
        ] == [prepared]
        assert build.call_count == 1
        ui.segmented_control.return_value = "debug"
        ui.button.side_effect = lambda label, **_: label == "Forbered supportpakke"
        render_report(ui, paths, package())
        assert build.call_args.args[1] == "debug"
        assert build.call_count == 2


@pytest.mark.parametrize("change", ("package", "title", "stem"))
def test_saved_report_link_is_bound_to_the_prepared_report(tmp_path: Path, change: str) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    ui = report_ui()
    ui.button.side_effect = lambda label, **_: label == "Gem rapport i eksportlageret"
    with patch.object(data_sections, "ReportStore") as store, patch.object(data_sections, "register_artifact") as register:
        store.return_value.save.return_value.path = tmp_path / "report.html"
        register.return_value.artifact_id = "registered-report"
        render_report(ui, paths, package())
    assert ui.link_button.called

    ui.reset_mock()
    ui.button.side_effect = None
    ui.button.return_value = False
    render_report(ui, paths, package(2) if change == "package" else package(),
                  title="Anden rapport" if change == "title" else "Rapport",
                  stem="andet" if change == "stem" else "demo")
    ui.link_button.assert_not_called()
    assert "report-artifact" not in ui.session_state


def test_legacy_report_session_shapes_are_discarded(tmp_path: Path) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    ui = report_ui()
    ui.session_state.update({"report-support": b"old zip", "report-artifact": ("old-id", "old.html")})
    render_report(ui, paths, package())
    assert not any(call.args[0] == "Download supportpakke" for call in ui.download_button.call_args_list)
    ui.link_button.assert_not_called()
    assert "report-support" not in ui.session_state
    assert "report-artifact" not in ui.session_state


def test_prepared_report_keeps_a_detached_labelled_snapshot_and_hides_on_game_change(tmp_path: Path) -> None:
    from holdet_lib import GameUrl, ManagerGame, SnapshotIndex

    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    first = ManagerGame(GameUrl("https://www.holdet.dk/da/fantasy/first", "da", "first"), "Første")
    second = ManagerGame(GameUrl("https://www.holdet.dk/da/fantasy/second", "da", "second"), "Andet")
    configuration = SimpleNamespace(games=(first, second), groups=())
    source = package()
    ui = report_ui()
    ui.segmented_control.side_effect = lambda label, *_args, **_: "game" if label == "Rapporttype" else "share"
    ui.selectbox.return_value = first
    ui.button.side_effect = lambda label, **_: label in {"Forbered managerspilrapport", "Forbered supportpakke"}
    with patch.object(data_sections, "st", ui), patch.object(data_sections, "build_manager_game_report_package", return_value=source) as build:
        data_sections.render_exports(configuration, SnapshotIndex(()), paths)
        source.scope["game"] = "changed"
        source.tables[0].rows[0]["value"] = 99
        ui.reset_mock()
        ui.button.side_effect = None
        ui.button.return_value = False
        data_sections.render_exports(configuration, SnapshotIndex(()), paths)
        assert build.call_count == 1
        snapshot = ui.session_state["prepared-manager-report"]
        assert snapshot.package.scope["game"] == "demo"
        assert snapshot.package.tables[0].rows[0]["value"] == 1
        assert any("øjebliksbillede forberedt" in call.args[0] for call in ui.caption.call_args_list)
        assert any(call.args[0] == "Download supportpakke" for call in ui.download_button.call_args_list)

        ui.reset_mock()
        ui.selectbox.return_value = second
        data_sections.render_exports(configuration, SnapshotIndex(()), paths)
        ui.download_button.assert_not_called()
        assert "prepared-manager-report" not in ui.session_state
        assert build.call_count == 1


def _ideal_app() -> None:
    from dataclasses import replace
    from datetime import date, datetime, timedelta, timezone
    from pathlib import Path

    import streamlit as st

    from holdet_lib import GameUrl, ManagerGame, PlayerStatisticsIndex, PlayerStatisticsSnapshot, ScrapedGame
    from holdet_lib.rules import GameRuleProfile
    from tests.test_decision_analysis import entry
    from website.analysis_pages import _ideal_panel

    locale = st.session_state.get("test-locale", "da")
    slug = st.session_state.get("test-slug", "demo")
    game = GameUrl(f"https://www.holdet.dk/{locale}/fantasy/{slug}", locale, slug)
    generated_at = datetime(2026, 11, 6, 12, tzinfo=timezone.utc) + timedelta(minutes=st.session_state.get("test-timestamp", 0))
    statistics = ScrapedGame(game, "soccer", st.session_state.get("test-round", 2),
        (entry(1, st.session_state.get("test-growth", 100)), entry(2, 200)),
        format="soccer", unit="money", round_status=st.session_state.get("test-status", "complete"))
    snapshot = PlayerStatisticsSnapshot(Path(st.session_state.get("test-path", "players.json")), generated_at, statistics)
    rules = GameRuleProfile(locale, slug, "Testregler", verified=True,
        source_url="https://www.holdet.dk/rules", accessed_on=date(2026, 11, 6),
        salary_cap=st.session_state.get("test-budget", 2_000_000), roster_size=1,
        budget_enabled=True, max_from_team=1, position_limits=(("forward", 1, 1),))
    if st.session_state.get("test-unverified", False):
        rules = replace(rules, verified=False)
    _ideal_panel(ManagerGame(game, "Testspil"), PlayerStatisticsIndex((snapshot,)), rules)


@pytest.mark.parametrize("change,value", (
    ("test-locale", "en"), ("test-slug", "other"), ("test-growth", 300),
    ("test-timestamp", 1), ("test-path", "corrected.json"), ("test-budget", 3_000_000),
    ("test-round", 3),
))
def test_ideal_result_is_hidden_after_input_changes_until_explicit_recalculation(change: str, value: object) -> None:
    with patch.object(analysis_pages, "optimize_ideal_team", wraps=analysis_pages.optimize_ideal_team) as optimize:
        app = AppTest.from_function(_ideal_app).run()
        next(item for item in app.button if item.label == "Beregn idealhold").click().run()
        assert not app.exception
        assert any("bevist optimalt" in item.value for item in app.success)
        assert optimize.call_count == 1

        app.run()
        assert any("bevist optimalt" in item.value for item in app.success)
        assert optimize.call_count == 1

        app.session_state[change] = value
        app.run()
        assert not app.exception
        assert not any("bevist optimalt" in item.value for item in app.success)
        assert optimize.call_count == 1
        next(item for item in app.button if item.label == "Beregn idealhold").click().run()
        assert not app.exception
        assert any("bevist optimalt" in item.value for item in app.success)
        assert optimize.call_count == 2


def test_ideal_timeout_changes_and_loss_of_verified_sources_clear_result() -> None:
    app = AppTest.from_function(_ideal_app).run()
    next(item for item in app.button if item.label == "Beregn idealhold").click().run()
    app.slider[0].set_value(6).run()
    assert not app.exception
    assert not app.success
    next(item for item in app.button if item.label == "Beregn idealhold").click().run()
    assert app.success
    app.session_state["test-unverified"] = True
    app.run()
    app.session_state["test-unverified"] = False
    app.run()
    assert not app.exception
    assert not app.success


def test_ideal_result_does_not_return_after_round_loses_completion() -> None:
    app = AppTest.from_function(_ideal_app).run()
    next(item for item in app.button if item.label == "Beregn idealhold").click().run()
    assert app.success
    app.session_state["test-status"] = "provisional"
    app.run()
    app.session_state["test-status"] = "complete"
    app.run()
    assert not app.exception
    assert not app.success


@pytest.mark.parametrize("operation", ("players", "team", "metadata", "discover", "history"))
def test_direct_data_mutations_respect_an_existing_data_lease(tmp_path: Path, operation: str) -> None:
    from holdet_lib.refresh_runs import RefreshRunBusyError, exclusive_data_operation
    from tests.test_library_storage import sample_team
    from website import ui as dashboard

    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    ui = MagicMock()
    ui.session_state = {}
    client = MagicMock()
    team = sample_team()
    with patch.object(dashboard, "APP_PATHS", paths), patch.object(dashboard, "OUTPUT_DIR", paths.snapshot_dir), patch.object(dashboard, "st", ui), patch.object(dashboard, "_client", return_value=client), exclusive_data_operation(paths.data_dir):
        if operation == "players":
            dashboard._fetch_player_statistics(team.reference.game, None)
            assert ui.session_state["player_statistics_errors"]
        elif operation == "team":
            dashboard._fetch_team_statistics(team.reference)
            assert ui.session_state["team_statistics_errors"]
        elif operation == "metadata":
            with pytest.raises(RefreshRunBusyError):
                dashboard._fetch_and_remember_game_info(team.reference.game)
        elif operation == "discover":
            with pytest.raises(RefreshRunBusyError):
                dashboard._discover_account_teams(team.reference.game)
        else:
            assert dashboard._freeze_complete_hall_of_fame((), include_round_wins=True) == 0
            assert ui.session_state["hall_of_fame_freeze_warning"]
    assert client.mock_calls == []
    assert not paths.snapshot_dir.exists()


@pytest.mark.parametrize("page", ("players", "teams"))
@pytest.mark.parametrize("reason", ("archived", "busy"))
def test_standalone_source_actions_use_selected_game_and_lease_state(page: str, reason: str) -> None:
    import holdet_lib as holdet
    from holdet_lib.refresh_runs import exclusive_data_operation
    from tests.test_website import APP_PATH, navigate, website_environment

    with website_environment() as (config, _):
        game = holdet.normalize_manager_game("source-control-test").game
        games = holdet.GroupStore(config / "groups.json")
        games.create_manager_game(game, "Kildespil")
        if reason == "archived":
            games.archive_manager_game(game)
        lease = exclusive_data_operation(holdet.resolve_paths().data_dir) if reason == "busy" else nullcontext()
        with lease:
            app = AppTest.from_file(APP_PATH).run(timeout=15)
            navigate(app, page, locale=game.locale, game=game.slug)

        assert not app.exception
        assert any(f"Valgt spil: {game.slug}" in item.value for item in app.caption)
        if page == "players":
            control = next(item for item in app.button if item.label == "Hent seneste spillerstatistik")
            assert control.disabled
        else:
            assert "Find hold på konfigurerede konti" not in [item.label for item in app.button]

        game_selector = next(item for item in app.selectbox if item.label == "Spil eller Holdet-URL")
        game_selector.set_value(None).run(timeout=15)
        assert not app.exception
        assert "game" not in app.query_params and "locale" not in app.query_params
        assert not any(item.value.startswith("Valgt spil:") for item in app.caption)


def test_stale_source_actions_recheck_archive_state(tmp_path: Path) -> None:
    import holdet_lib as holdet
    from tests.test_library_storage import sample_team
    from website import ui as dashboard

    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    team = sample_team()
    game = team.reference.game
    games = holdet.GroupStore(paths.groups_file)
    games.create_manager_game(game, "Kildespil")
    games.archive_manager_game(game)
    client = MagicMock()
    mocked_ui = MagicMock()
    mocked_ui.session_state = {}
    with (
        patch.object(dashboard, "APP_PATHS", paths),
        patch.object(dashboard, "OUTPUT_DIR", paths.snapshot_dir),
        patch.object(dashboard, "st", mocked_ui),
        patch.object(dashboard, "_client", return_value=client),
    ):
        dashboard._fetch_player_statistics(game, None)
        dashboard._fetch_team_statistics(team.reference)
        with pytest.raises(holdet.PayloadError, match="arkiveret"):
            dashboard._discover_account_teams(game)
        with pytest.raises(holdet.PayloadError, match="arkiveret"):
            dashboard._fetch_and_remember_game_info(game)
    for action in ("player", "team"):
        errors = mocked_ui.session_state[f"{action}_statistics_errors"]
        assert "arkiveret" in next(iter(errors.values()))["details"]
    assert not paths.snapshot_dir.exists()
    assert client.mock_calls == []


def test_alert_callback_rechecks_archive_state_before_mutation() -> None:
    import holdet_lib as holdet
    from tests.test_website import APP_PATH, button, enable_tools, navigate, website_environment

    with website_environment() as (config, _):
        game = holdet.normalize_manager_game("archived-alert-test").game
        games = holdet.GroupStore(config / "groups.json")
        games.create_manager_game(game, "Alarmspil")
        enable_tools(config, "alerts")
        inbox = holdet.AnalysisInboxStore(config / "analysis-inbox.json")
        unread = holdet.WatchlistAlert(
            alert_id="unread", game_locale=game.locale, game_slug=game.slug, player_key="player",
            player_name="Spiller", kind="injured", message="Skadet", detected_at=NOW, round_number=1,
        )
        dismissed = replace(unread, alert_id="dismissed", dismissed_at=NOW, read_at=NOW)
        inbox.save((unread, dismissed))
        before = inbox.path.read_bytes()
        app = AppTest.from_file(APP_PATH).run(timeout=15)
        navigate(app, "game", locale=game.locale, game=game.slug, section="alerts")
        stale_callback = button(app, "Markér som læst")

        games.archive_manager_game(game)
        stale_callback.click().run(timeout=15)

        assert not app.exception
        assert not {"Markér som læst", "Afvis", "Ryd afviste alarmer"}.intersection(
            item.label for item in app.button
        )
        assert inbox.path.read_bytes() == before


def test_refresh_tasks_capture_rules_at_each_game_start(tmp_path: Path) -> None:
    import holdet_lib as holdet
    from tests.test_player_statistics import sample_statistics
    from tests.test_refresh_runs import wait_for
    from website.game_access import editable_game
    from website.refresh_service import create_refresh_coordinator

    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    statistics = sample_statistics()
    games_store = holdet.GroupStore(paths.groups_file, paths.group_revision_dir)
    first = games_store.create_manager_game(statistics.game, "Første spil")
    second = games_store.create_manager_game("later-task-rules", "Næste spil")
    store = holdet.HubSettingsStore(paths.hub_settings_file)
    selected = (
        holdet.watchlist_entry(first.game, statistics.entries[0]),
        holdet.watchlist_entry(second.game, statistics.entries[0]),
    )

    def rules_with_threshold(threshold: float):
        rule = holdet.WatchRule("price-drop", "value_drop", threshold)
        return tuple(replace(entry, rules=(rule,)) for entry in selected)

    store.save(holdet.HubSettings(enabled_features=(holdet.HubFeature.ALERTS,), watchlist=rules_with_threshold(100)))
    first_started = Event()
    release_first = Event()
    observed = []

    def execute(manager, *_args, settings, **_kwargs):
        if manager.identity == first.identity:
            first_started.set()
            assert release_first.wait(10)
        entry = next(item for item in settings.watchlist if item.game_identity == manager.identity)
        observed.append((manager.game.slug, entry.rules[0].threshold))
        return SimpleNamespace(
            manifest_path=paths.manifest_dir / f"{manager.game.slug}.json", warnings=(), manifest=None,
            cancelled=False,
        )

    coordinator = create_refresh_coordinator(paths, client_factory=object)
    tasks = [{"key": item.game.slug, "locale": item.game.locale, "game": item.game.slug} for item in (first, second)]
    with patch("holdet_lib.refresh.catch_up_manager_game", side_effect=execute):
        run = coordinator.start(tasks)
        try:
            assert first_started.wait(5)
            with editable_game(paths, first.game):
                store.set_watchlist(store.load(), rules_with_threshold(200))
        finally:
            release_first.set()
            finished = wait_for(coordinator, run["run_id"], lambda state: state["status"] not in {"running", "unstarted"})

    assert finished["status"] == "success"
    assert observed == [(first.game.slug, 100.0), (second.game.slug, 200.0)]


def test_data_busy_probe_handles_stale_lock_without_changing_files(tmp_path: Path) -> None:
    from holdet_lib.refresh_runs import exclusive_data_operation
    from website.workflows import updates_running

    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    assert not updates_running(paths)
    assert list(tmp_path.iterdir()) == []

    with exclusive_data_operation(paths.data_dir):
        assert updates_running(paths)

    lock_path = paths.data_dir.parent / f".{paths.data_dir.name}-refresh.lock"
    lock_before = lock_path.read_bytes()
    assert lock_before
    assert not updates_running(paths)
    assert lock_path.read_bytes() == lock_before


def test_history_freeze_failure_is_shown_once_in_shared_notifications() -> None:
    from website import ui as dashboard
    from website.navigation import PageId

    ui = MagicMock()
    ui.session_state = {"hall_of_fame_freeze_warning": "Lokal skrivefejl"}
    context = SimpleNamespace(index=None, configuration_warnings=(), pairing_warnings=(), read_only=False)
    with patch.object(dashboard, "st", ui), patch.object(dashboard, "_warning_panel"):
        dashboard.render_shared_shell(context, PageId.ARCHIVE)
        dashboard.render_shared_shell(context, PageId.ARCHIVE)
    ui.warning.assert_called_once_with("Historikken kunne ikke fryses: Lokal skrivefejl")
    assert "hall_of_fame_freeze_warning" not in ui.session_state


def _manager_history_app(root: str) -> None:
    from pathlib import Path

    from holdet_lib import SnapshotIndex
    from holdet_lib.paths import PathOverrides, resolve_paths
    from website.hub_pages import managers_view

    managers_view((), SnapshotIndex(()), resolve_paths(overrides=PathOverrides(data_root=Path(root))))


def test_manager_history_publication_reports_busy_without_writing(tmp_path: Path) -> None:
    from holdet_lib.refresh_runs import exclusive_data_operation
    from website import hub_pages

    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    app = AppTest.from_function(_manager_history_app, args=(str(tmp_path),)).run()
    assert not app.exception
    with exclusive_data_operation(paths.data_dir), patch.object(hub_pages.HallOfFameStore, "freeze_complete") as freeze:
        next(item for item in app.button if item.label == "Genopbyg historik fra cache").click().run()
    assert not app.exception
    freeze.assert_not_called()
    assert any("Historikken kunne ikke genopbygges" in item.value for item in app.error)

