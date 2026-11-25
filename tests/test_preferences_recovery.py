from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from holdet_lib.hub_settings import HubSettings, HubSettingsStore, watchlist_entry
from holdet_lib.paths import PathOverrides, resolve_paths
from tests.test_feature_preferences import GAME, PLAYER
from website import preferences


def fake_ui():
    ui = MagicMock()
    ui.session_state = {}
    ui.checkbox.return_value = False
    ui.form_submit_button.return_value = True
    return ui


def test_unreadable_settings_keep_safe_defaults_until_file_is_recovered(tmp_path: Path) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    paths.config_dir.mkdir()
    original = b'{"schema_version":5,"watchlist":['
    paths.hub_settings_file.write_bytes(original)
    ui = fake_ui()

    with patch.object(preferences, "st", ui):
        assert preferences.settings_for(paths) == HubSettings()
        assert not preferences.settings_readable(paths)
        ui.warning.assert_called()
        assert paths.hub_settings_file.read_bytes() == original
        recovered = HubSettings(enabled_features=("scouting",))
        HubSettingsStore(paths.hub_settings_file).save(recovered)
        assert preferences.settings_readable(paths)
        assert preferences.settings_for(paths) == recovered


def test_preference_save_cannot_replace_unreadable_records_with_defaults(tmp_path: Path) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    paths.config_dir.mkdir()
    original = b'{"schema_version":5,"watchlist":['
    paths.hub_settings_file.write_bytes(original)
    ui = fake_ui()

    with patch.object(preferences, "st", ui), patch.object(preferences, "page_link"):
        preferences.render_settings(paths)

    assert ui.form_submit_button.call_args.kwargs["disabled"]
    assert all(call.kwargs["disabled"] for call in ui.checkbox.call_args_list)
    assert paths.hub_settings_file.read_bytes() == original


def test_tool_enable_does_not_replace_unreadable_settings(tmp_path: Path) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    paths.config_dir.mkdir()
    original = b'{"schema_version":5,"watchlist":['
    paths.hub_settings_file.write_bytes(original)
    ui = fake_ui()
    ui.button.side_effect = lambda label, **_kwargs: label == "Aktivér værktøj"

    with patch.object(preferences, "st", ui):
        assert not preferences.require_tool(paths, "scouting")

    enable = next(call for call in ui.button.call_args_list if call.args[0] == "Aktivér værktøj")
    assert enable.kwargs["disabled"]
    assert paths.hub_settings_file.read_bytes() == original


def test_preference_submit_preserves_settings_corrupted_after_render(tmp_path: Path) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    HubSettingsStore(paths.hub_settings_file).save(HubSettings())
    ui = fake_ui()
    corrupt = b'{"schema_version":5,"watchlist":['

    with patch.object(preferences, "st", ui), patch.object(preferences, "page_link"):
        preferences.render_settings(paths)
        submitted = ui.form_submit_button.call_args.kwargs
        assert not submitted["disabled"]
        paths.hub_settings_file.write_bytes(corrupt)
        ui.session_state["feature:scouting"] = True
        submitted["on_click"](*submitted["args"])
        preferences.render_settings(paths)

    assert paths.hub_settings_file.read_bytes() == corrupt
    assert "kunne ikke gemmes" in ui.error.call_args.args[0]
    ui.success.assert_not_called()


def test_explicit_preference_save_revokes_temporary_grants_and_preserves_records(tmp_path: Path) -> None:
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    watched = watchlist_entry(GAME, PLAYER)
    store = HubSettingsStore(paths.hub_settings_file)
    store.save(HubSettings(watchlist=(watched,), enabled_features=("scouting",)))
    ui = fake_ui()
    ui.session_state = {
        "tool-open-once:scouting": True,
        "tool-open-once:calendar": True,
        "unrelated": "preserved",
    }

    with patch.object(preferences, "st", ui), patch.object(preferences, "page_link"):
        preferences.render_settings(paths)
        submitted = ui.form_submit_button.call_args.kwargs
        submitted["on_click"](*submitted["args"])
        assert not preferences.tool_visible(paths, "scouting")
        assert not preferences.tool_visible(paths, "calendar")

    assert store.load().enabled_features == ()
    assert store.load().watchlist == (watched,)
    assert ui.session_state["unrelated"] == "preserved"


def test_preference_submit_cannot_recreate_settings_removed_by_restore(tmp_path: Path) -> None:
    from holdet_lib.backup import create_backup_bytes, restore_backup

    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    backup, _ = create_backup_bytes(paths)
    store = HubSettingsStore(paths.hub_settings_file)
    store.save(HubSettings())
    ui = fake_ui()

    with patch.object(preferences, "st", ui), patch.object(preferences, "page_link"):
        preferences.render_settings(paths)
        submitted = ui.form_submit_button.call_args.kwargs
        restore_backup(backup, paths)
        ui.session_state["feature:scouting"] = True
        submitted["on_click"](*submitted["args"])
        preferences.render_settings(paths)

    assert not paths.hub_settings_file.exists()
    assert "genindlæs" in ui.error.call_args.args[0]
