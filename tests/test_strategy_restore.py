from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

from holdet_lib.backup import create_backup_bytes, restore_backup
from holdet_lib.errors import PayloadError
from holdet_lib.groups import GroupStore
from holdet_lib.paths import PathOverrides, resolve_paths
from holdet_lib.persistence import exclusive_configuration_update
from holdet_lib.refresh_runs import exclusive_data_operation
from holdet_lib.strategies import StrategyStore, TeamStrategy
from tests.test_decision_analysis import GAME, NOW


@pytest.mark.parametrize("fail_install", [False, True])
def test_strategy_save_during_restore_waits_for_commit_or_rollback(tmp_path, fail_install):
    paths = resolve_paths(overrides=PathOverrides(
        data_root=tmp_path / "first-hub", config_dir=tmp_path / "shared-config",
    ), environ={})
    groups = GroupStore(paths.groups_file, paths.group_revision_dir)
    groups.create_manager_game(GAME, "Original registration")
    strategies = StrategyStore(paths.strategy_file)
    strategies.set(GAME, 101, TeamStrategy(transfer_policy="fixed"))
    backup, _ = create_backup_bytes(paths, now=NOW)
    strategies.set(GAME, 303, TeamStrategy(transfer_policy="active"))
    original_groups = paths.groups_file.read_bytes()
    program = """import sys
from pathlib import Path
from holdet_lib.paths import PathOverrides, resolve_paths
from holdet_lib.players import normalize_game_url
from holdet_lib.strategies import StrategyStore, TeamStrategy
paths = resolve_paths(overrides=PathOverrides(data_root=Path(sys.argv[1]), config_dir=Path(sys.argv[2])), environ={})
print('ready', flush=True)
StrategyStore(paths.strategy_file).set(normalize_game_url(sys.argv[3]), 202, TeamStrategy(transfer_policy='limited', max_transfers=2))
print('saved', flush=True)
"""
    real_replace = os.replace
    child = None
    with ThreadPoolExecutor(max_workers=1) as reader:
        def interleave(source, destination):
            nonlocal child
            source, destination = Path(source), Path(destination)
            staged = source.parent.name.startswith(".holdet-restore-")
            if staged and source.name == "config" and destination == paths.config_dir:
                assert not paths.config_dir.exists()
                child = subprocess.Popen(
                    [sys.executable, "-B", "-X", "utf8", "-c", program,
                     str(tmp_path / "second-hub"), str(paths.config_dir / ".." / paths.config_dir.name), GAME.original],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                )
                assert reader.submit(child.stdout.readline).result(timeout=5).strip() == "ready"
                try:
                    child.communicate(timeout=0.3)
                except subprocess.TimeoutExpired:
                    pass
            if fail_install and staged and source.name == "data" and destination == paths.data_dir:
                raise OSError("synthetic data installation failure")
            return real_replace(source, destination)

        try:
            with patch("holdet_lib.backup.os.replace", side_effect=interleave):
                if fail_install:
                    with pytest.raises(PayloadError, match="den oprindelige installation er lagt tilbage"):
                        restore_backup(backup, paths, now=NOW)
                else:
                    restore_backup(backup, paths, now=NOW)
        finally:
            if child is not None:
                try:
                    stdout, stderr = child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=5)
                    raise
    assert child is not None and child.returncode == 0, stderr
    assert stdout.strip() == "saved"
    assert paths.groups_file.read_bytes() == original_groups
    expected = {101, 202, 303} if fail_install else {101, 202}
    assert {key[2] for key in strategies.load()} == expected
    assert GroupStore(paths.groups_file, paths.group_revision_dir).load_configuration().games[0].name == "Original registration"


def test_restore_with_busy_configuration_leaves_settings_untouched_and_releases_data_lease(tmp_path):
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path / "hub"), environ={})
    GroupStore(paths.groups_file, paths.group_revision_dir).create_manager_game(GAME, "Original registration")
    StrategyStore(paths.strategy_file).set(GAME, 101, TeamStrategy(transfer_policy="fixed"))
    backup, _ = create_backup_bytes(paths, now=NOW)
    before = {path.relative_to(paths.config_dir): path.read_bytes() for path in paths.config_dir.rglob("*") if path.is_file()}
    with exclusive_configuration_update(paths.config_dir), patch(
        "holdet_lib.backup.exclusive_configuration_update",
        side_effect=lambda path: exclusive_configuration_update(path, timeout=0),
    ):
        with pytest.raises(TimeoutError, match="busy"):
            restore_backup(backup, paths, now=NOW)
        with exclusive_data_operation(paths.data_dir):
            pass
    assert {path.relative_to(paths.config_dir): path.read_bytes() for path in paths.config_dir.rglob("*") if path.is_file()} == before
    assert not paths.backup_dir.exists()
    restore_backup(backup, paths, now=NOW)
    assert {key[2] for key in StrategyStore(paths.strategy_file).load()} == {101}
