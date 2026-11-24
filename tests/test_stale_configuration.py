from dataclasses import replace

import pytest

from holdet_lib.hub_settings import HubSettings, HubSettingsStore
from holdet_lib.groups import GroupStore, GroupTeam
from holdet_lib.seasons import SeasonStore
from holdet_lib.backup import create_backup_bytes, restore_backup
from holdet_lib.paths import PathOverrides, resolve_paths
from tests.test_decision_analysis import GAME


def test_settings_update_rejects_stale_baseline_and_preserves_newer_value(tmp_path):
    store = HubSettingsStore(tmp_path / "hub-settings.json")
    original = HubSettings()
    store.save(original)
    changed = replace(original, enabled_features=("alerts",))
    store.save(changed)
    with pytest.raises(ValueError, match="ændret"):
        store.set_enabled_features(original, ("managers",))
    assert store.load() == changed


def test_season_update_rejects_stale_baseline_and_preserves_newer_value(tmp_path):
    store = SeasonStore(tmp_path / "seasons.json")
    original = store.load()
    changed, _ = store.create(original, "First", ("competition-a",))
    with pytest.raises(ValueError, match="ændret"):
        store.create(original, "Second", ("competition-b",))
    assert store.load() == changed


def test_explicit_save_initializes_a_nondefault_baseline(tmp_path):
    store = HubSettingsStore(tmp_path / "hub-settings.json")
    baseline = HubSettings(enabled_features=("alerts",), experimental_games=(("da", "demo"),))
    store.save(baseline)
    updated = store.set_enabled_features(baseline, ("managers",))
    expected = replace(baseline, enabled_features=("managers",))
    assert updated == expected
    assert store.load() == expected


def test_restore_to_absent_files_rejects_old_settings_and_season_edits(tmp_path):
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path), environ={})
    source, _ = create_backup_bytes(paths)
    settings = HubSettingsStore(paths.hub_settings_file)
    previous_settings = settings.set_enabled_features(settings.load(), ("alerts",))
    seasons = SeasonStore(paths.seasons_file)
    previous_seasons, season = seasons.create(seasons.load(), "Before restore", ("competition",))

    restore_backup(source, paths)

    with pytest.raises(ValueError, match="ændret"):
        settings.set_enabled_features(previous_settings, ("scouting",))
    with pytest.raises(ValueError, match="ændret"):
        seasons.archive(previous_seasons, season.season_id)
    assert not settings.path.exists()
    assert not seasons.path.exists()
    assert settings.set_enabled_features(settings.load(), ("scouting",)).enabled_features == ("scouting",)
    created, _ = seasons.create(seasons.load(), "After restore", ("competition",))
    assert seasons.load() == created


def test_group_update_rejects_stale_baseline(tmp_path):
    store = GroupStore(tmp_path / "groups.json")
    group = store.create("Original", GAME,
        (GroupTeam(101, "Team", f"{GAME.original}/fantasyteams/101"),), group_id="league")
    changed = replace(group, name="Newer")
    store.update(changed)
    with pytest.raises(ValueError, match="ændret"):
        store.update(replace(group, name="Stale"), expected=group)
    assert store.load()[0] == changed
