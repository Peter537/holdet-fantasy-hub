from dataclasses import replace

import pytest

from holdet_lib.hub_settings import HubSettings, HubSettingsStore
from holdet_lib.groups import GroupStore, GroupTeam
from holdet_lib.seasons import SeasonStore
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


def test_absent_file_accepts_supplied_baseline(tmp_path):
    store = HubSettingsStore(tmp_path / "hub-settings.json")
    baseline = replace(HubSettings(), enabled_features=("alerts",))
    updated = store.set_enabled_features(baseline, ("managers",))
    assert store.load() == updated


def test_group_update_rejects_stale_baseline(tmp_path):
    store = GroupStore(tmp_path / "groups.json")
    group = store.create("Original", GAME,
        (GroupTeam(101, "Team", f"{GAME.original}/fantasyteams/101"),), group_id="league")
    changed = replace(group, name="Newer")
    store.update(changed)
    with pytest.raises(ValueError, match="ændret"):
        store.update(replace(group, name="Stale"), expected=group)
    assert store.load()[0] == changed
