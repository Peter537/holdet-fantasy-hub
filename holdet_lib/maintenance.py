"""Read-only storage inventory and explicit preview-based cleanup services."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4
import zipfile

from .paths import AppPaths
from .persistence import aware_local
from .storage import PlayerStatisticsStore, SnapshotStore


@dataclass(frozen=True, slots=True)
class StorageUsage:
    game_scope: str
    category: str
    files: int
    bytes: int


@dataclass(frozen=True, slots=True)
class StorageInventory:
    rows: tuple[StorageUsage, ...]
    total_files: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class RetentionCandidate:
    path: Path
    relative_path: str
    game_scope: str
    snapshot_type: str
    round_number: int
    size: int
    sha256: str
    retained_path: Path


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    candidates: tuple[RetentionCandidate, ...]
    retained: tuple[Path, ...]
    warnings: tuple[str, ...]

    @property
    def reclaimable_bytes(self) -> int:
        return sum(candidate.size for candidate in self.candidates)


@dataclass(frozen=True, slots=True)
class ArchiveResult:
    path: Path
    archived_files: int
    archived_bytes: int
    removed_files: int
    removal_errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CleanupCandidate:
    path: Path
    category: str
    size: int
    modified_at: datetime


def _files(root: Path) -> tuple[Path, ...]:
    if not root.exists():
        return ()
    return tuple(
        sorted(
            (
                path.resolve()
                for path in root.rglob("*")
                if path.is_file() and not path.is_symlink()
            ),
            key=str,
        )
    )


def _game_from(root: Path, path: Path) -> str:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return "Fælles"
    return relative.parts[0] if len(relative.parts) > 1 else "Fælles"


def build_storage_inventory(paths: AppPaths) -> StorageInventory:
    """Calculate exact bytes and file counts without creating directories."""

    totals: dict[tuple[str, str], list[int]] = {}

    def collect(root: Path, category: str, *, scoped: bool) -> None:
        for path in _files(root):
            game = _game_from(root, path) if scoped else "Fælles"
            values = totals.setdefault((game, category), [0, 0])
            values[0] += 1
            values[1] += path.stat().st_size

    collect(paths.snapshot_dir, "Aktive snapshots", scoped=True)
    collect(paths.manifest_dir, "Manifester/revisioner", scoped=True)
    collect(paths.group_revision_dir, "Manifester/revisioner", scoped=False)
    collect(paths.tournament_pairing_dir, "Manifester/revisioner", scoped=False)
    collect(paths.import_dir, "Importerede data", scoped=False)
    collect(paths.player_export_dir, "Afledte eksporter", scoped=True)
    collect(paths.team_export_dir, "Afledte eksporter", scoped=True)
    collect(paths.report_dir, "Afledte eksporter", scoped=True)
    collect(paths.backup_dir, "Backups", scoped=False)
    collect(paths.archive_dir, "Arkiver", scoped=False)
    rows = tuple(
        StorageUsage(game, category, values[0], values[1])
        for (game, category), values in sorted(
            totals.items(), key=lambda item: (item[0][0].casefold(), item[0][1])
        )
    )
    return StorageInventory(
        rows,
        sum(row.files for row in rows),
        sum(row.bytes for row in rows),
    )


def _sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def plan_snapshot_retention(paths: AppPaths) -> RetentionPlan:
    """Select only older valid snapshots, keeping one newest per round."""

    candidates: list[RetentionCandidate] = []
    retained: list[Path] = []
    player_index = PlayerStatisticsStore(paths.snapshot_dir).scan()
    team_index = SnapshotStore(paths.snapshot_dir).scan()

    player_groups: dict[tuple[str, str, int], list[object]] = {}
    for snapshot in player_index.snapshots:
        player_groups.setdefault(snapshot.identity, []).append(snapshot)
    for (locale, slug, round_number), snapshots in sorted(player_groups.items()):
        ordered = sorted(
            snapshots,
            key=lambda item: (item.generated_at, item.path.name),
            reverse=True,
        )
        retained.append(ordered[0].path)
        for snapshot in ordered[1:]:
            path = snapshot.path.resolve()
            candidates.append(
                RetentionCandidate(
                    path,
                    path.relative_to(paths.data_dir.resolve()).as_posix(),
                    slug,
                    "players",
                    round_number,
                    path.stat().st_size,
                    _sha256(path),
                    ordered[0].path.resolve(),
                )
            )

    team_groups: dict[tuple[str, str, int, int], list[object]] = {}
    for snapshot in team_index.snapshots:
        team = snapshot.team
        key = (
            team.reference.game.locale.casefold(),
            team.reference.game.slug,
            team.reference.team_id,
            team.overview.current_round,
        )
        team_groups.setdefault(key, []).append(snapshot)
    for (_, slug, _, round_number), snapshots in sorted(team_groups.items()):
        ordered = sorted(
            snapshots,
            key=lambda item: (item.generated_at, item.path.name),
            reverse=True,
        )
        retained.append(ordered[0].path)
        for snapshot in ordered[1:]:
            path = snapshot.path.resolve()
            candidates.append(
                RetentionCandidate(
                    path,
                    path.relative_to(paths.data_dir.resolve()).as_posix(),
                    slug,
                    "teams",
                    round_number,
                    path.stat().st_size,
                    _sha256(path),
                    ordered[0].path.resolve(),
                )
            )
    candidates.sort(key=lambda item: item.relative_path)
    return RetentionPlan(
        tuple(candidates),
        tuple(sorted(set(path.resolve() for path in retained), key=str)),
        tuple((*player_index.warnings, *team_index.warnings)),
    )


def _validate_retention_archive(path: Path, expected: tuple[RetentionCandidate, ...]) -> None:
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("archive-manifest.json").decode("utf-8"))
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(files, list) or manifest.get("schema_version") != 1:
            raise ValueError("Arkivmanifestet er ugyldigt")
        expected_by_path = {candidate.relative_path: candidate for candidate in expected}
        if {item.get("path") for item in files if isinstance(item, dict)} != set(expected_by_path):
            raise ValueError("Arkivmanifestet matcher ikke kandidaterne")
        for item in files:
            assert isinstance(item, dict)
            relative = str(item["path"])
            hasher = hashlib.sha256()
            size = 0
            with archive.open(relative) as source:
                while chunk := source.read(1024 * 1024):
                    hasher.update(chunk)
                    size += len(chunk)
            candidate = expected_by_path[relative]
            if size != candidate.size or hasher.hexdigest() != candidate.sha256:
                raise ValueError(f"Arkivvalidering mislykkedes for {relative}")


def archive_retention_candidates(
    paths: AppPaths,
    candidates: tuple[RetentionCandidate, ...],
    *,
    destination: Path | str | None = None,
    now: datetime | None = None,
) -> ArchiveResult:
    """Archive, validate, then remove only the explicitly selected sources."""

    if not candidates:
        raise ValueError("Vælg mindst én retentionkandidat")
    snapshot_root = paths.snapshot_dir.resolve()
    current = {
        candidate.path.resolve(): candidate
        for candidate in plan_snapshot_retention(paths).candidates
    }
    selected_paths = tuple(candidate.path.resolve() for candidate in candidates)
    if any(path not in current for path in selected_paths):
        raise ValueError("Valget indeholder et snapshot, som retentionreglen skal bevare")
    candidates = tuple(current[path] for path in selected_paths)
    unique = {candidate.path.resolve(): candidate for candidate in candidates}
    if len(unique) != len(candidates):
        raise ValueError("Den samme fil er valgt flere gange")
    for path, candidate in unique.items():
        if not path.is_relative_to(snapshot_root) or not path.is_file() or path.is_symlink():
            raise ValueError(f"Ugyldig retentionkandidat: {path.name}")
        if path.stat().st_size != candidate.size or _sha256(path) != candidate.sha256:
            raise ValueError(f"Filen har ændret sig siden preview: {path.name}")
    generated = aware_local(now)
    target = Path(destination) if destination is not None else (
        paths.archive_dir / f"snapshot-mellemversioner-{generated.strftime('%Y%m%d-%H%M%S')}.zip"
    )
    if target.exists():
        raise FileExistsError(f"Arkivfilen findes allerede: {target}")
    if target.resolve().is_relative_to(paths.data_dir.resolve()):
        raise ValueError("Mellemversionsarkivet må ikke ligge i det kanoniske datatræ")
    target.parent.mkdir(parents=True, exist_ok=True)
    required = sum(candidate.size for candidate in candidates)
    if shutil.disk_usage(target.parent).free < required + 16 * 1024 * 1024:
        raise OSError("Der er ikke nok ledig diskplads til arkivet")
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    manifest_files: list[dict[str, object]] = []
    try:
        with zipfile.ZipFile(
            temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for candidate in candidates:
                before = candidate.path.stat()
                with candidate.path.open("rb") as source, archive.open(candidate.relative_path, "w") as output:
                    hasher = hashlib.sha256()
                    size = 0
                    while chunk := source.read(1024 * 1024):
                        output.write(chunk)
                        hasher.update(chunk)
                        size += len(chunk)
                after = candidate.path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise OSError(f"Filen ændrede sig under arkivering: {candidate.path.name}")
                if size != candidate.size or hasher.hexdigest() != candidate.sha256:
                    raise OSError(f"Checksum ændrede sig under arkivering: {candidate.path.name}")
                manifest_files.append(
                    {"path": candidate.relative_path, "size": size, "sha256": candidate.sha256}
                )
            archive.writestr(
                "archive-manifest.json",
                json.dumps(
                    {
                        "schema_version": 1,
                        "created_at": generated.isoformat(),
                        "files": manifest_files,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
        _validate_retention_archive(temporary, candidates)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    removed = 0
    errors: list[str] = []
    for candidate in candidates:
        try:
            candidate.path.unlink()
            removed += 1
        except OSError as exc:
            errors.append(f"{candidate.path.name}: {exc}")
    return ArchiveResult(target.resolve(), len(candidates), required, removed, tuple(errors))


def list_cleanup_candidates(paths: AppPaths) -> tuple[CleanupCandidate, ...]:
    roots = (
        (paths.player_export_dir, "Afledt spillereksport"),
        (paths.team_export_dir, "Afledt holdeksport"),
        (paths.report_dir, "Afledt rapport"),
        (paths.archive_dir, "Mellemversionsarkiv"),
    )
    result: list[CleanupCandidate] = []
    for root, category in roots:
        for path in _files(root):
            stat = path.stat()
            result.append(
                CleanupCandidate(
                    path,
                    category,
                    stat.st_size,
                    datetime.fromtimestamp(stat.st_mtime).astimezone(),
                )
            )
    backups = sorted(
        _files(paths.backup_dir),
        key=lambda path: (path.stat().st_mtime_ns, str(path)),
    )
    for path in backups[:-1]:
        file_stat = path.stat()
        result.append(
            CleanupCandidate(
                path,
                "Gammel backup",
                file_stat.st_size,
                datetime.fromtimestamp(file_stat.st_mtime).astimezone(),
            )
        )
    return tuple(sorted(result, key=lambda item: (item.modified_at, str(item.path))))


def delete_derived_files(paths: AppPaths, selected: tuple[Path, ...]) -> tuple[Path, ...]:
    """Delete only exact files under the explicitly allowed derived roots."""

    resolved = tuple(dict.fromkeys(Path(path).resolve() for path in selected))
    allowed = {candidate.path.resolve() for candidate in list_cleanup_candidates(paths)}
    for path in resolved:
        if path not in allowed:
            raise ValueError(f"Filen må ikke slettes via oprydning: {path.name}")
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"Oprydningsmålet er ikke en almindelig fil: {path.name}")
    for path in resolved:
        path.unlink()
    return resolved
