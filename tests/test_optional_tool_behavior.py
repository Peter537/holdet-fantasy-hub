from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest

from holdet_lib.hub_settings import HubSettings, HubSettingsStore, watchlist_entry
from holdet_lib.paths import PathOverrides, resolve_paths
from tests.test_scouting import GAME, entry


@pytest.fixture(autouse=True)
def app_script_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from streamlit.testing.v1 import app_test

    monkeypatch.setattr(app_test, "TMP_DIR", SimpleNamespace(name=str(tmp_path)))


def _watchlist_app(root: str, single: bool, read_only: bool = False) -> None:
    from pathlib import Path

    from holdet_lib.paths import PathOverrides, resolve_paths
    from tests.test_scouting import GAME, entry, scouting_index
    from website.scouting_page import (
        render_contextual_watchlist_panel,
        render_player_watchlist_editor,
    )

    paths = resolve_paths(overrides=PathOverrides(data_root=Path(root)))
    if single:
        render_player_watchlist_editor(GAME, entry(1), paths, read_only=read_only)
    else:
        render_contextual_watchlist_panel(
            GAME, scouting_index(), paths, read_only=read_only
        )


def _bulk_watchlist_app(root: str) -> None:
    from pathlib import Path

    from holdet_lib import player_identity
    from holdet_lib.paths import PathOverrides, resolve_paths
    from tests.test_scouting import GAME, entry
    from website.scouting_page import render_player_bulk_controls

    paths = resolve_paths(overrides=PathOverrides(data_root=Path(root)))
    player = entry(1)
    key = player_identity(GAME, player)
    render_player_bulk_controls(
        scope="test", selected_keys=(key,), game=GAME, entries={key: player}, paths=paths
    )


def _transfer_app() -> None:
    from datetime import datetime, timezone
    from pathlib import Path

    import streamlit as st

    import holdet_lib as holdet
    from tests.test_library_storage import sample_team
    from website.hub_pages import transfer_lab_panel

    price = st.number_input("Testpris", value=100, step=1)
    team = sample_team(current_round=3)
    generated = datetime(2026, 10, 29, 12, tzinfo=timezone.utc)
    players = holdet.ScrapedGame(
        team.reference.game,
        "cycling",
        3,
        (holdet.PlayerEntry(1, "Kandidat", "Klub", "Rytter", price, entry_id=1),),
        "cycling",
        "money",
        "complete",
    )
    transfer_lab_panel(
        holdet.TeamSnapshot(Path("team.json"), generated, team),
        holdet.PlayerStatisticsSnapshot(Path("players.json"), generated, players),
        team_round_status="complete",
    )


def _quality_app(root: str) -> None:
    from pathlib import Path

    from holdet_lib import ManagerGame, SnapshotIndex
    from holdet_lib.paths import PathOverrides, resolve_paths
    from tests.test_scouting import GAME
    from website.hub_pages import data_quality_panel

    paths = resolve_paths(overrides=PathOverrides(data_root=Path(root)))
    data_quality_panel((ManagerGame(GAME, "Testspil"),), (), SnapshotIndex(()), paths)


def _formula_app(root: str) -> None:
    from pathlib import Path

    from holdet_lib.hub_settings import HubSettingsStore
    from holdet_lib.paths import PathOverrides, resolve_paths
    from tests.test_scouting import GAME
    from website.scouting_page import _formula_editor

    paths = resolve_paths(overrides=PathOverrides(data_root=Path(root)))
    store = HubSettingsStore(paths.hub_settings_file)
    _formula_editor(GAME, store, store.load(), paths)


def _fetch_players_app() -> None:
    import streamlit as st

    from tests.test_scouting import GAME
    from website.ui import _fetch_player_statistics

    if st.button("Hent testdata"):
        _fetch_player_statistics(GAME, None)


@pytest.mark.parametrize("single", (True, False))
def test_custom_alarm_inputs_are_editable_before_saving(tmp_path: Path, single: bool) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    settings = HubSettings(enabled_features=("scouting", "alerts"))
    watched = replace(watchlist_entry(GAME, entry(1), settings), rules=())
    store = HubSettingsStore(paths.hub_settings_file)
    store.save(replace(settings, watchlist=(watched,)))
    original = paths.hub_settings_file.read_bytes()
    app = AppTest.from_function(_watchlist_app, args=(str(tmp_path), single)).run()

    assert not app.exception
    rule_kind = next(item for item in app.selectbox if item.label in {"Regeltype", "Ny regel"})
    threshold = next(item for item in app.number_input if "tærskel" in item.label.casefold())
    assert not rule_kind.disabled
    assert not threshold.disabled
    assert paths.hub_settings_file.read_bytes() == original

    next(item for item in app.checkbox if "Tilføj" in item.label).check()
    rule_kind.select("value_drop")
    threshold.set_value(12.0)
    next(item for item in app.button if item.label == "Gem watchlist").click().run()

    assert not app.exception
    saved_rules = store.load().watchlist[0].rules
    assert len(saved_rules) == 1
    assert saved_rules[0].kind == "value_drop"
    assert saved_rules[0].threshold == 12


@pytest.mark.parametrize("changed_input", ("Runde for handlerne", "Testpris"))
def test_transfer_results_disappear_when_scenario_or_source_changes(changed_input: str) -> None:
    app = AppTest.from_function(_transfer_app).run()
    app.multiselect[0].set_value([1010]).run()
    app.multiselect[1].set_value([1]).run()
    next(item for item in app.button if item.label == "Simulér scenarie").click().run()
    assert not app.exception
    assert any(item.label == "Bank efter handler" for item in app.metric)

    changed = next(item for item in app.number_input if item.label == changed_input)
    changed.set_value(changed.value + 1).run()

    assert not app.exception
    assert not any(item.label == "Bank efter handler" for item in app.metric)
    assert any("Simulér igen" in item.value for item in app.info)


@pytest.mark.parametrize("single", (True, False))
def test_alert_editing_requires_saved_opt_in_and_preserves_rules_when_notes_change(
    tmp_path: Path, single: bool
) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    watched = watchlist_entry(GAME, entry(1))
    store = HubSettingsStore(paths.hub_settings_file)
    store.save(HubSettings(watchlist=(watched,), enabled_features=("scouting",)))
    app = AppTest.from_function(_watchlist_app, args=(str(tmp_path), single))
    app.session_state["tool-open-once:alerts"] = True
    app.run()

    assert not app.exception
    assert all(item.disabled for item in app.checkbox)
    assert all(item.disabled for item in app.number_input)
    next(item for item in app.text_area).set_value("Ny begrundelse")
    next(item for item in app.button if item.label == "Gem watchlist").click().run()

    saved = store.load()
    assert saved.watchlist[0].reason_note == "Ny begrundelse"
    assert saved.watchlist[0].rules == watched.rules
    assert saved.enabled_features == ("scouting",)


@pytest.mark.parametrize("single", (True, False))
def test_archived_watchlists_cannot_be_changed(tmp_path: Path, single: bool) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    watched = watchlist_entry(GAME, entry(1))
    store = HubSettingsStore(paths.hub_settings_file)
    store.save(HubSettings(watchlist=(watched,), enabled_features=("scouting", "alerts")))
    original = paths.hub_settings_file.read_bytes()
    app = AppTest.from_function(_watchlist_app, args=(str(tmp_path), single, True)).run()

    assert not app.exception
    assert all(item.disabled for item in app.button)
    assert all(item.disabled for item in app.text_area)
    assert all(item.disabled for item in app.checkbox)
    assert next(item for item in app.button if item.label == "Gem watchlist").disabled
    assert paths.hub_settings_file.read_bytes() == original


@pytest.mark.parametrize("single", (True, False))
def test_adding_watchlist_entries_with_alerts_off_creates_no_rules(
    tmp_path: Path, single: bool
) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    store = HubSettingsStore(paths.hub_settings_file)
    if single:
        app = AppTest.from_function(_watchlist_app, args=(str(tmp_path), True)).run()
        label = "Føj til watchlist"
    else:
        app = AppTest.from_function(_bulk_watchlist_app, args=(str(tmp_path),)).run()
        label = "Anvend på valgte"
        assert "Tilføj alarmregel" not in app.selectbox[0].options
    next(item for item in app.button if item.label == label).click().run()

    assert not app.exception
    assert store.load().watchlist[0].rules == ()


def test_empty_watchlist_recovery_opens_the_same_games_player_list(tmp_path: Path) -> None:
    from website.navigation import PageId

    with patch("website.scouting_page.page_link") as link:
        app = AppTest.from_function(_watchlist_app, args=(str(tmp_path), False)).run()

    assert not app.exception
    assert link.call_args.args[0] == PageId.PLAYERS
    assert link.call_args.kwargs["game"] == GAME.slug
    assert link.call_args.kwargs["locale"] == GAME.locale
    assert link.call_args.kwargs["panel"] == "list"


def test_data_quality_renders_stale_status_and_oldest_team_observation(tmp_path: Path) -> None:
    from holdet_lib.analytics import DataQualityReport, DataQualityRound
    from website.presentation import format_relative_precise

    now = datetime.now(timezone.utc)
    oldest = now - timedelta(hours=30)
    newest = now - timedelta(minutes=2)
    row = DataQualityRound(
        GAME.slug, 5, 2, 2, True, 2, 0, 0, (), game_locale=GAME.locale,
        readiness="stale", oldest_team_data=oldest, newest_team_data=newest,
    )
    report = DataQualityReport(now, (row,), newest, newest, newest, None, None)
    with (
        patch("website.hub_pages.build_data_quality_report", return_value=report),
        patch("website.hub_pages.page_link"),
    ):
        app = AppTest.from_function(_quality_app, args=(str(tmp_path),)).run()

    assert not app.exception
    assert any(":orange-badge[" in item.value and "Forældet" in item.value for item in app.markdown)
    oldest_metric = next(item for item in app.metric if item.label == "Ældste holdcache")
    assert oldest_metric.value == format_relative_precise(oldest)


def test_formula_authoring_is_visible_only_with_scouting_access(tmp_path: Path) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    store = HubSettingsStore(paths.hub_settings_file)
    store.save(HubSettings())
    app = AppTest.from_function(_formula_app, args=(str(tmp_path),)).run()
    assert not app.exception
    assert not any(
        item.label == "Egne beregnede kolonner"
        for kind in ("expander", "status")
        for item in app.get(kind)
    )

    store.set_enabled_features(store.load(), ("scouting",))
    app.run()
    assert not app.exception
    assert any(
        item.label == "Egne beregnede kolonner"
        for kind in ("expander", "status")
        for item in app.get(kind)
    )


@pytest.mark.parametrize("alerts_enabled", (False, True))
def test_direct_player_refresh_generates_alerts_only_with_saved_opt_in(
    tmp_path: Path, alerts_enabled: bool
) -> None:
    from holdet_lib import AnalysisInboxStore, PlayerStatisticsStore, ScrapedGame
    from website import ui

    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    healthy = entry(1)
    previous = ScrapedGame(
        GAME, "cycling", 1, (healthy,), format="cycling", unit="money", round_status="complete"
    )
    current = replace(previous, round_number=2, entries=(replace(healthy, is_injured=True),))
    players = PlayerStatisticsStore(paths.snapshot_dir)
    players.save(previous)
    HubSettingsStore(paths.hub_settings_file).save(HubSettings(
        watchlist=(watchlist_entry(GAME, healthy),),
        enabled_features=("alerts",) if alerts_enabled else (),
    ))
    client = SimpleNamespace(fetch_players=lambda *_args, **_kwargs: current)
    with (
        patch.object(ui, "APP_PATHS", paths),
        patch.object(ui, "OUTPUT_DIR", paths.snapshot_dir),
        patch.object(ui, "_client", return_value=client),
        patch.object(ui, "_save_game_metadata_if_available"),
        patch.object(ui, "build_watchlist_alerts", wraps=ui.build_watchlist_alerts) as build_alerts,
    ):
        app = AppTest.from_function(_fetch_players_app)
        app.session_state["tool-open-once:alerts"] = True
        app.run()
        app.button[0].click().run()

    assert not app.exception
    assert players.scan(GAME).newest(GAME, 2) is not None
    if alerts_enabled:
        build_alerts.assert_called_once()
        assert len(AnalysisInboxStore(paths.analysis_inbox_file).load()) == 1
    else:
        build_alerts.assert_not_called()
        assert not paths.analysis_inbox_file.exists()
