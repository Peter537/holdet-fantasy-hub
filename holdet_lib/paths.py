"""Windows application-directory resolution for Holdet Fantasy Hub."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path
from typing import Any, Callable


APP_NAME = "Holdet Fantasy Hub"


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved paths. Constructing this object never touches the filesystem."""

    config_dir: Path
    accounts_file: Path
    groups_file: Path
    data_dir: Path
    snapshot_dir: Path
    manifest_dir: Path
    group_revision_dir: Path
    export_dir: Path
    player_export_dir: Path
    team_export_dir: Path


@dataclass(frozen=True, slots=True)
class PathOverrides:
    """Explicit frontend overrides, applied before environment variables."""

    data_root: Path | None = None
    config_dir: Path | None = None
    accounts_file: Path | None = None
    export_dir: Path | None = None


PlatformDirsFactory = Callable[..., Any]


def _optional_path(value: str | os.PathLike[str] | None) -> Path | None:
    if value is None:
        return None
    text = os.fspath(value).strip()
    return Path(text).expanduser() if text else None


def resolve_paths(
    *,
    overrides: PathOverrides | None = None,
    environ: Mapping[str, str] | None = None,
    platform_dirs_factory: PlatformDirsFactory | None = None,
) -> AppPaths:
    """Resolve effective paths without creating directories.

    A unified data root produces a deterministic layout useful for tests and
    custom drives. Otherwise Windows Roaming AppData is used for configuration
    and Local AppData for durable data and exports.
    """

    selected = overrides or PathOverrides()
    env = os.environ if environ is None else environ
    explicit_root = selected.data_root
    environment_root = _optional_path(env.get("HOLDET_DATA_DIR"))
    root = explicit_root or environment_root

    if root is not None:
        root = Path(root)
        default_config = root / "config"
        data_dir = root / "data"
        default_exports = root / "exports"
    else:
        if platform_dirs_factory is None:
            from platformdirs import PlatformDirs

            platform_dirs_factory = PlatformDirs
        roaming = platform_dirs_factory(
            APP_NAME,
            appauthor=False,
            version=None,
            roaming=True,
            ensure_exists=False,
        )
        local = platform_dirs_factory(
            APP_NAME,
            appauthor=False,
            version=None,
            roaming=False,
            ensure_exists=False,
        )
        default_config = Path(roaming.user_config_path) / "config"
        local_root = Path(local.user_data_path)
        data_dir = local_root / "data"
        default_exports = local_root / "exports"

    config_dir = (
        selected.config_dir
        or (Path(explicit_root) / "config" if explicit_root is not None else None)
        or _optional_path(env.get("HOLDET_CONFIG_DIR"))
        or default_config
    )
    export_dir = (
        selected.export_dir
        or (Path(explicit_root) / "exports" if explicit_root is not None else None)
        or _optional_path(env.get("HOLDET_OUTPUT_DIR"))
        or default_exports
    )
    accounts_file = selected.accounts_file or Path(config_dir) / "accounts.json"
    config_dir = Path(config_dir)
    export_dir = Path(export_dir)
    data_dir = Path(data_dir)

    return AppPaths(
        config_dir=config_dir,
        accounts_file=Path(accounts_file),
        groups_file=config_dir / "groups.json",
        data_dir=data_dir,
        snapshot_dir=data_dir / "snapshots",
        manifest_dir=data_dir / "manifests",
        group_revision_dir=data_dir / "group-revisions",
        export_dir=export_dir,
        player_export_dir=export_dir / "players",
        team_export_dir=export_dir / "teams",
    )


def open_in_explorer(path: Path) -> bool:
    """Create *path* on explicit request and open it in Windows Explorer."""

    resolved = Path(path).resolve()
    resolved.mkdir(parents=True, exist_ok=True)
    try:
        os.startfile(resolved)  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
    return True
