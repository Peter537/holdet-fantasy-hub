from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import threading
from unittest.mock import patch

import pytest

from holdet_lib.backup import create_backup_bytes, restore_backup
from holdet_lib.errors import PayloadError
from holdet_lib.groups import GroupStore
from holdet_lib.paths import PathOverrides, resolve_paths
from holdet_lib.persistence import exclusive_configuration_update
from holdet_lib.refresh_runs import exclusive_data_operation
from holdet_lib.strategies import StrategyStore, TeamStrategy
from tests.spawned_process import spawned_process
from tests.test_decision_analysis import GAME, NOW
from tests.test_strategies import save_strategy_in_child


@pytest.mark.parametrize("fail_install", [False, True])
def test_strategy_save_during_restore_waits_for_commit_or_rollback(tmp_path, fail_install):
    paths = resolve_paths(overrides=PathOverrides(
        data_root=tmp_path / "first-hub", config_dir=tmp_path / "shared-config",
    ), environ={})
    groups = GroupStore(paths.groups_file, paths.group_revision_dir)
    groups.create_manager_game(GAME, "Original registration")
    strategies = StrategyStore(paths.strategy_file)
    saved_in_backup = TeamStrategy(transfer_policy="fixed")
    added_after_backup = TeamStrategy(transfer_policy="active")
    strategies.set(GAME, 101, saved_in_backup)
    backup, _ = create_backup_bytes(paths, now=NOW)
    strategies.set(GAME, 303, added_after_backup)
    original_groups = paths.groups_file.read_bytes()
    contender = TeamStrategy(transfer_policy="limited", max_transfers=2)
    other_paths = resolve_paths(overrides=PathOverrides(
        data_root=tmp_path / "second-hub",
        config_dir=paths.config_dir / ".." / paths.config_dir.name,
    ), environ={})
    real_replace = os.replace
    with spawned_process(save_strategy_in_child, other_paths.strategy_file, GAME, 202, contender) as (child, connection):
        assert connection.poll(30), "strategy writer did not finish startup"
        assert connection.recv() == "ready"

        def interleave(source, destination):
            source, destination = Path(source), Path(destination)
            staged = source.parent.name.startswith(".holdet-restore-")
            if staged and source.name == "config" and destination == paths.config_dir:
                assert not paths.config_dir.exists()
                connection.send("save")
                assert connection.poll(5), "writer did not start its save"
                assert connection.recv() == "saving"
                assert not connection.poll(0.3), "writer finished during restore"
            if fail_install and staged and source.name == "data" and destination == paths.data_dir:
                raise OSError("synthetic data installation failure")
            return real_replace(source, destination)

        with patch("holdet_lib.backup.os.replace", side_effect=interleave):
            if fail_install:
                with pytest.raises(PayloadError, match="den oprindelige installation er lagt tilbage"):
                    restore_backup(backup, paths, now=NOW)
            else:
                restore_backup(backup, paths, now=NOW)
        assert connection.poll(10), "writer did not finish after restore"
        assert connection.recv() == "saved"
        child.join(5)
        assert child.exitcode == 0
    assert paths.groups_file.read_bytes() == original_groups
    expected = {
        (GAME.locale, GAME.slug, 101): saved_in_backup,
        (GAME.locale, GAME.slug, 202): contender,
    }
    if fail_install:
        expected[(GAME.locale, GAME.slug, 303)] = added_after_backup
    assert StrategyStore(paths.strategy_file).load() == expected
    assert GroupStore(paths.groups_file, paths.group_revision_dir).load_configuration().games[0].name == "Original registration"


def test_restore_with_busy_configuration_leaves_settings_untouched_and_releases_data_lease(tmp_path):
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path / "hub"), environ={})
    GroupStore(paths.groups_file, paths.group_revision_dir).create_manager_game(GAME, "Original registration")
    StrategyStore(paths.strategy_file).set(GAME, 101, TeamStrategy(transfer_policy="fixed"))
    backup, _ = create_backup_bytes(paths, now=NOW)
    before = {path.relative_to(paths.config_dir): path.read_bytes() for path in paths.config_dir.rglob("*") if path.is_file()}
    held, release = threading.Event(), threading.Event()

    def hold_configuration():
        with exclusive_configuration_update(paths.config_dir):
            held.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(hold_configuration)
        try:
            assert held.wait(5)
            with patch(
                "holdet_lib.backup.exclusive_configuration_update",
                side_effect=lambda path, **kwargs: exclusive_configuration_update(path, timeout=0, **kwargs),
            ):
                with pytest.raises(TimeoutError, match="busy"):
                    restore_backup(backup, paths, now=NOW)
                with exclusive_data_operation(paths.data_dir):
                    pass
        finally:
            release.set()
        owner.result(timeout=5)
    assert {path.relative_to(paths.config_dir): path.read_bytes() for path in paths.config_dir.rglob("*") if path.is_file()} == before
    assert not paths.backup_dir.exists()
    restore_backup(backup, paths, now=NOW)
    assert {key[2] for key in StrategyStore(paths.strategy_file).load()} == {101}
