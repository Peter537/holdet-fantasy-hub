import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from holdet_lib.backup import create_backup_bytes, restore_backup, validate_backup
from holdet_lib.errors import PayloadError
from holdet_lib.groups import GroupStore
from holdet_lib.paths import PathOverrides, resolve_paths
from holdet_lib.persistence import restore_journal_path
from holdet_lib.refresh_runs import RefreshRunBusyError, RefreshRunCoordinator, exclusive_data_operation
from holdet_lib.restore_transaction import recover_restore
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


@pytest.mark.skipif(os.name != "nt", reason="Windows rename semantics")
@pytest.mark.parametrize("during_rollback", (False, True))
def test_restore_retries_transient_windows_directory_denial(tmp_path, during_rollback):
    paths, backup = setup_restore(tmp_path)
    before = current_bytes(paths)
    replace = os.replace
    denied = 0

    def deny_once(source, destination):
        nonlocal denied
        source, destination = Path(source), Path(destination)
        if destination == paths.data_dir:
            installing = source.parent.name.startswith(".holdet-restore-")
            if installing and during_rollback:
                raise OSError("synthetic install failure")
            recovering = source.name.startswith(".data.old-")
            if denied == 0 and (recovering if during_rollback else installing):
                denied += 1
                error = PermissionError("temporary Windows directory denial")
                error.winerror = 5
                raise error
        return replace(source, destination)

    with patch.object(os, "replace", side_effect=deny_once):
        if during_rollback:
            with pytest.raises(PayloadError, match="oprindelige installation"):
                restore_backup(backup, paths)
            assert current_bytes(paths) == before
        else:
            restore_backup(backup, paths)
            assert GroupStore(paths.groups_file, paths.group_revision_dir).load_configuration().games[0].name == "Backup state"
    assert denied == 1
    assert not restore_journal_path(paths.config_dir).exists()


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


@pytest.mark.parametrize("boundary", ("move_config", "install_config", "move_data", "install_data", "commit", "recovery", "finished"))
def test_restart_recovers_a_terminated_restore(tmp_path, boundary):
    paths, backup = setup_restore(tmp_path)
    before = current_bytes(paths)
    source = tmp_path / "source.zip"
    source.write_bytes(backup)
    program = """
import json
import os
from pathlib import Path
import sys
from holdet_lib.backup import restore_backup
from holdet_lib.paths import PathOverrides, resolve_paths
from holdet_lib.persistence import restore_journal_path
from holdet_lib.restore_transaction import recover_restore

root, source, boundary = sys.argv[1:]
paths = resolve_paths(overrides=PathOverrides(data_root=Path(root)), environ={})
replace = os.replace
def stop_after_replace(source, destination):
    source, destination = Path(source), Path(destination)
    staged = source.parent.name.startswith(".holdet-restore-")
    selected = (
        boundary == "move_config" and source == paths.config_dir
        or boundary == "move_data" and source == paths.data_dir
        or boundary == "install_config" and staged and destination == paths.config_dir
        or boundary == "install_data" and staged and destination == paths.data_dir
        or boundary == "commit" and destination == restore_journal_path(paths.config_dir)
        and json.loads(source.read_text(encoding="utf-8"))["phase"] == "committed"
        or boundary == "recovery" and source.name.startswith(".data.old-") and destination == paths.data_dir
    )
    replace(source, destination)
    if selected:
        os._exit(71)
os.replace = stop_after_replace
unlink = Path.unlink
def stop_after_unlink(path, *args, **kwargs):
    unlink(path, *args, **kwargs)
    if boundary == "finished" and path == restore_journal_path(paths.config_dir):
        os._exit(71)
Path.unlink = stop_after_unlink
if boundary == "recovery":
    recover_restore(paths)
else:
    restore_backup(Path(source), paths)
"""
    command = [sys.executable, "-B", "-c", program, str(paths.data_dir.parent), str(source)]
    boundaries = ("install_data", "recovery") if boundary == "recovery" else (boundary,)
    for interruption in boundaries:
        process = subprocess.run([*command, interruption], timeout=30, capture_output=True)
        assert process.returncode == 71, process.stderr.decode(errors="replace")
    assert restore_journal_path(paths.config_dir).is_file() == (boundary != "finished")
    with pytest.raises(RefreshRunBusyError, match="gendannelse"):
        with exclusive_data_operation(paths.data_dir):
            pytest.fail("interrupted restore did not fence data mutations")
    coordinator = RefreshRunCoordinator(
        paths.data_dir / "refresh-runs",
        lambda *_args, **_kwargs: pytest.fail("interrupted restore started a new run"),
    )
    with pytest.raises(RefreshRunBusyError, match="gendannelse"):
        coordinator.start([{"key": "one"}])
    assert recover_restore(paths)
    assert not recover_restore(paths)
    with exclusive_data_operation(paths.data_dir):
        pass
    if boundary in {"commit", "finished"}:
        groups = GroupStore(paths.groups_file, paths.group_revision_dir)
        assert groups.load_configuration().games[0].name == "Backup state"
        assert (paths.import_dir / "archive-only" / "notes.txt").read_text(encoding="utf-8") == "backup"
    else:
        assert current_bytes(paths) == before
    rollback, = paths.backup_dir.glob("rollback-*.zip")
    assert validate_backup(rollback).is_valid
    for pattern in (".holdet-restore-*", ".*.old-*"):
        assert not list(paths.data_dir.parent.glob(pattern))
