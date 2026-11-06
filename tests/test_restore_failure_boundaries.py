from pathlib import Path
from unittest.mock import patch

import pytest

from holdet_lib.backup import create_backup_bytes, restore_backup, validate_backup
from holdet_lib.errors import PayloadError
from holdet_lib.groups import GroupStore
from holdet_lib.paths import PathOverrides, resolve_paths
from tests.test_decision_analysis import GAME, NOW


def setup_restore(tmp_path):
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path / "hub"), environ={})
    groups = GroupStore(paths.groups_file, paths.group_revision_dir)
    groups.create_manager_game(GAME, "Backup state")
    source = paths.import_dir / "archive-only" / "notes.txt"
    source.parent.mkdir(parents=True)
    source.write_text("backup", encoding="utf-8")
    backup, _ = create_backup_bytes(paths, now=NOW)
    groups.rename_manager_game(GAME, "Live state")
    source.write_text("live", encoding="utf-8")
    return paths, backup


def current_bytes(paths):
    return {
        (label, file.relative_to(root).as_posix()): file.read_bytes()
        for label, root in (("config", paths.config_dir), ("data", paths.data_dir))
        for file in root.rglob("*") if file.is_file()
    }


@pytest.mark.parametrize("boundary", ("move_config", "install_config", "move_data", "install_data"))
def test_restore_rolls_back_every_directory_transition(tmp_path, boundary):
    paths, backup = setup_restore(tmp_path)
    before = current_bytes(paths)
    from holdet_lib import backup as module

    replace = module.os.replace
    injected = False

    def fail_once(source, destination):
        nonlocal injected
        source, destination = Path(source), Path(destination)
        staged = source.parent.name.startswith(".holdet-restore-")
        selected = (
            boundary == "move_config" and source == paths.config_dir
            or boundary == "move_data" and source == paths.data_dir
            or boundary == "install_config" and staged and source.name == "config"
            or boundary == "install_data" and staged and source.name == "data"
        )
        if selected and not injected:
            injected = True
            raise OSError("synthetic directory transition failure")
        return replace(source, destination)

    with patch.object(module.os, "replace", side_effect=fail_once):
        with pytest.raises(PayloadError, match="den oprindelige installation er lagt tilbage"):
            restore_backup(backup, paths, now=NOW)
    assert injected
    assert current_bytes(paths) == before
    rollback = tuple(paths.backup_dir.glob("rollback-*.zip"))
    assert len(rollback) == 1 and validate_backup(rollback[0]).is_valid
    assert not tuple(paths.data_dir.parent.glob(".holdet-restore-*"))


def test_restore_reports_failed_rollback_and_retains_verified_archive(tmp_path):
    paths, backup = setup_restore(tmp_path)
    from holdet_lib import backup as module

    replace = module.os.replace

    def fail_install_and_rollback(source, destination):
        source, destination = Path(source), Path(destination)
        if source.parent.name.startswith(".holdet-restore-") and source.name == "data":
            raise OSError("synthetic installation failure")
        if source.name.startswith(f".{paths.config_dir.name}.old-") and destination == paths.config_dir:
            raise OSError("synthetic rollback failure")
        return replace(source, destination)

    with patch.object(module.os, "replace", side_effect=fail_install_and_rollback):
        with pytest.raises(PayloadError, match="tilbagerulningen mislykkedes.*Rollback-ZIP"):
            restore_backup(backup, paths, now=NOW)
    rollback = tuple(paths.backup_dir.glob("rollback-*.zip"))
    assert len(rollback) == 1 and validate_backup(rollback[0]).is_valid
