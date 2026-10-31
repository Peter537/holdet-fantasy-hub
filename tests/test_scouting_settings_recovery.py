from __future__ import annotations

from contextlib import nullcontext
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from holdet_lib.hub_settings import HubSettings, HubSettingsStore, OwnTeamSelection, watchlist_entry
from holdet_lib.paths import PathOverrides, resolve_paths
from holdet_lib.player_formulas import ComputedPlayerColumn
from tests.test_scouting import GAME, entry, scouting_index
from website import preferences, scouting_page


def edit_ui():
    ui = MagicMock()
    ui.session_state = {"tool-open-once:scouting": True}
    ui.expander.return_value = nullcontext(SimpleNamespace(open=True))
    ui.form_submit_button.return_value = True
    ui.button.return_value = False
    ui.multiselect.return_value = ()
    ui.selectbox.side_effect = lambda label, *_args, **_kwargs: (
        watchlist_entry(GAME, entry(1)).player_key if label == "Redigér spiller"
        else "add_watchlist" if label == "Handling" else "status_change"
    )
    ui.text_area.return_value = "Opdateret begrundelse"
    ui.text_input.side_effect = lambda label, **_kwargs: "Ny kolonne" if label == "Kolonnenavn" else "value"
    ui.number_input.return_value = 2
    ui.segmented_control.return_value = "absolute"
    ui.checkbox.return_value = False
    return ui


@pytest.mark.parametrize("surface", ("single", "bulk", "formula"))
def test_unreadable_settings_cannot_be_overwritten_by_scouting_edits(
    tmp_path: Path, surface: str
) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    paths.config_dir.mkdir()
    original = b'{"schema_version":5,"watchlist":['
    paths.hub_settings_file.write_bytes(original)
    ui = edit_ui()
    ui.button.return_value = True
    player = entry(1)
    key = watchlist_entry(GAME, player).player_key

    with (
        patch.object(scouting_page, "st", ui),
        patch.object(preferences, "st", ui),
    ):
        if surface == "single":
            scouting_page.render_player_watchlist_editor(GAME, player, paths)
            assert ui.button.call_args.kwargs["disabled"]
        elif surface == "bulk":
            scouting_page.render_player_bulk_controls(
                scope="test", selected_keys=(key,), game=GAME, entries={key: player}, paths=paths
            )
            assert all(call.kwargs["disabled"] for call in ui.form_submit_button.call_args_list)
        else:
            scouting_page._formula_editor(
                GAME, HubSettingsStore(paths.hub_settings_file), HubSettings(), paths
            )
            assert ui.form_submit_button.call_args.kwargs["disabled"]

    assert paths.hub_settings_file.read_bytes() == original


@pytest.mark.parametrize("single", (True, False))
def test_watchlist_edit_preserves_settings_added_after_form_render(
    tmp_path: Path, single: bool
) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    store = HubSettingsStore(paths.hub_settings_file)
    first = watchlist_entry(GAME, entry(1))
    added = watchlist_entry(GAME, entry(2))
    initial = HubSettings(watchlist=(first,), enabled_features=("scouting",))
    store.save(initial)
    own = OwnTeamSelection(GAME.locale, GAME.slug, 9)
    ui = edit_ui()

    def submit(*_args, **_kwargs):
        store.save(replace(initial, watchlist=(first, added), own_teams=(own,)))
        return True

    ui.form_submit_button.side_effect = submit
    with (
        patch.object(scouting_page, "st", ui),
        patch.object(preferences, "st", ui),
        patch.object(scouting_page, "dataframe"),
    ):
        if single:
            scouting_page.render_player_watchlist_editor(GAME, entry(1), paths)
        else:
            scouting_page.render_contextual_watchlist_panel(GAME, scouting_index(), paths)

    saved = store.load()
    assert saved.own_teams == (own,)
    assert added in saved.watchlist
    assert next(item for item in saved.watchlist if item.player_key == first.player_key).reason_note == "Opdateret begrundelse"


def test_bulk_bookmark_uses_latest_preferences_and_keeps_other_records(tmp_path: Path) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    store = HubSettingsStore(paths.hub_settings_file)
    other = watchlist_entry(GAME, entry(2))
    own = OwnTeamSelection(GAME.locale, GAME.slug, 9)
    actual = HubSettings(watchlist=(other,), own_teams=(own,), enabled_features=("alerts",))
    store.save(actual)
    player = entry(1)
    key = watchlist_entry(GAME, player).player_key
    ui = edit_ui()

    with patch.object(scouting_page, "st", ui):
        scouting_page._bulk_controls(
            scope="test", selected_keys=(key,), game=GAME, entries={key: player},
            store=store, settings=HubSettings(),
        )

    saved = store.load()
    assert saved.own_teams == actual.own_teams
    assert saved.enabled_features == actual.enabled_features
    assert other in saved.watchlist
    assert next(item for item in saved.watchlist if item.player_key == key).rules


def test_formula_add_preserves_columns_and_preferences_missing_from_render_snapshot(tmp_path: Path) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    store = HubSettingsStore(paths.hub_settings_file)
    existing = ComputedPlayerColumn(GAME.locale, GAME.slug, "existing", "Eksisterende", "value")
    own = OwnTeamSelection(GAME.locale, GAME.slug, 9)
    actual = HubSettings(
        own_teams=(own,), enabled_features=("scouting",), computed_player_columns=(existing,)
    )
    store.save(actual)
    ui = edit_ui()

    with patch.object(scouting_page, "st", ui), patch.object(preferences, "st", ui):
        scouting_page._formula_editor(GAME, store, HubSettings(), paths)

    saved = store.load()
    assert saved.own_teams == actual.own_teams
    assert saved.enabled_features == actual.enabled_features
    assert existing in saved.computed_player_columns
    assert any(item.name == "Ny kolonne" for item in saved.computed_player_columns)


def test_settings_becoming_unreadable_during_submit_are_not_overwritten(tmp_path: Path) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    store = HubSettingsStore(paths.hub_settings_file)
    store.save(HubSettings(enabled_features=("scouting",)))
    corrupt = b'{"schema_version":5,"watchlist":['
    player = entry(1)
    key = watchlist_entry(GAME, player).player_key
    ui = edit_ui()

    def submit(*_args, **_kwargs):
        paths.hub_settings_file.write_bytes(corrupt)
        return True

    ui.form_submit_button.side_effect = submit
    with patch.object(scouting_page, "st", ui), patch.object(preferences, "st", ui):
        scouting_page.render_player_bulk_controls(
            scope="test", selected_keys=(key,), game=GAME, entries={key: player}, paths=paths
        )

    assert paths.hub_settings_file.read_bytes() == corrupt
    ui.error.assert_called()
    ui.rerun.assert_not_called()
