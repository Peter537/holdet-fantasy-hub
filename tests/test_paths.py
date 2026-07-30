from __future__ import annotations

from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

import holdet_lib as holdet


class _FakePlatformDirs:
    def __init__(
        self,
        appname,
        appauthor=None,
        version=None,
        roaming=False,
        ensure_exists=False,
    ) -> None:
        self.appname = appname
        self.appauthor = appauthor
        self.version = version
        self.roaming = roaming
        self.ensure_exists = ensure_exists
        base = Path("R:/Roaming") if roaming else Path("L:/Local")
        self.user_config_path = base / appname
        self.user_data_path = base / appname
        self.user_cache_path = Path("L:/Cache") / appname
        self.user_log_path = Path("L:/Logs") / appname


class AppPathTests(unittest.TestCase):
    def test_windows_roaming_local_split_is_side_effect_free(self) -> None:
        paths = holdet.resolve_paths(
            environ={}, platform_dirs_factory=_FakePlatformDirs
        )
        self.assertEqual(
            paths.accounts_file,
            Path("R:/Roaming/Holdet Fantasy Hub/config/accounts.json"),
        )
        self.assertEqual(
            paths.snapshot_dir,
            Path("L:/Local/Holdet Fantasy Hub/data/snapshots"),
        )
        self.assertEqual(
            paths.export_dir,
            Path("L:/Local/Holdet Fantasy Hub/exports"),
        )
        self.assertFalse(paths.config_dir.exists())
        self.assertFalse(paths.data_dir.exists())

    def test_unified_root_and_specific_overrides_have_clear_precedence(self) -> None:
        paths = holdet.resolve_paths(
            overrides=holdet.PathOverrides(
                data_root=Path("D:/Hub data"),
                accounts_file=Path("E:/Ægir/accounts.json"),
            ),
            environ={
                "HOLDET_DATA_DIR": "X:/ignored",
                "HOLDET_CONFIG_DIR": "Y:/specific-config",
                "HOLDET_OUTPUT_DIR": "Z:/exports",
            },
            platform_dirs_factory=_FakePlatformDirs,
        )
        self.assertEqual(paths.config_dir, Path("D:/Hub data/config"))
        self.assertEqual(paths.accounts_file, Path("E:/Ægir/accounts.json"))
        self.assertEqual(paths.snapshot_dir, Path("D:/Hub data/data/snapshots"))
        self.assertEqual(paths.export_dir, Path("D:/Hub data/exports"))

    def test_explicit_specific_overrides_beat_environment(self) -> None:
        paths = holdet.resolve_paths(
            overrides=holdet.PathOverrides(
                data_root=Path("D:/root"),
                config_dir=Path("D:/chosen-config"),
                export_dir=Path("D:/chosen-exports"),
            ),
            environ={
                "HOLDET_CONFIG_DIR": "E:/ignored-config",
                "HOLDET_OUTPUT_DIR": "E:/ignored-exports",
            },
            platform_dirs_factory=_FakePlatformDirs,
        )
        self.assertEqual(paths.config_dir, Path("D:/chosen-config"))
        self.assertEqual(paths.export_dir, Path("D:/chosen-exports"))

    def test_environment_specific_paths_beat_environment_root(self) -> None:
        paths = holdet.resolve_paths(
            environ={
                "HOLDET_DATA_DIR": "D:/environment-root",
                "HOLDET_CONFIG_DIR": "E:/chosen-config",
                "HOLDET_OUTPUT_DIR": "F:/chosen-exports",
            },
            platform_dirs_factory=_FakePlatformDirs,
        )
        self.assertEqual(paths.config_dir, Path("E:/chosen-config"))
        self.assertEqual(paths.snapshot_dir, Path("D:/environment-root/data/snapshots"))
        self.assertEqual(paths.export_dir, Path("F:/chosen-exports"))

    def test_open_in_explorer_creates_only_after_explicit_action(self) -> None:
        target = Path(__file__).parent / f"_test-paths-{uuid4().hex}" / "Dansk mappe"
        try:
            self.assertFalse(target.exists())
            with patch("holdet_lib.paths.os.startfile", create=True) as startfile:
                self.assertTrue(holdet.open_in_explorer(target))
            self.assertTrue(target.is_dir())
            startfile.assert_called_once_with(target.resolve())
        finally:
            shutil.rmtree(target.parent, ignore_errors=True)


