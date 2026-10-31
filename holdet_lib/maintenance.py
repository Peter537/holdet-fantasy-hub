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

from .errors import PayloadError
from .paths import AppPaths
from .persistence import aware_local
from .storage import ManifestStore, PlayerStatisticsStore, SnapshotStore


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


def _protected_snapshot_paths(
    paths: AppPaths, player_index, team_index
) -> tuple[set[Path], tuple[str, ...]]:
    """Keep manifest provenance and inputs needed by unfinished alert retries."""
    protected: set[Path] = set()
    warnings: list[str] = []
    all_snapshots = (*player_index.snapshots, *team_index.snapshots)
    run_root = paths.data_dir / "refresh-runs"
    if run_root.exists():
        from .refresh_runs import RefreshRunCoordinator

        try:
            if not run_root.is_dir():
                raise OSError("Opdateringsjournalens placering er ikke en mappe")
            runs, run_warnings = RefreshRunCoordinator(
                run_root, lambda *_args, **_kwargs: None
            ).scan_runs()
        except OSError as exc:
            warnings.append(f"Opdateringsjournalen kan ikke læses: {exc}; snapshots bevares")
            return {item.path.resolve() for item in all_snapshots}, tuple(warnings)
        unfinished = any(
            run["status"] != "success" or not run["games"]
            or any(game["status"] != "success" for game in run["games"])
            for run in runs
        )
        if run_warnings or unfinished:
            warnings.extend(run_warnings)
            warnings.append("En opdateringsjournal er uafsluttet eller ulæselig; snapshots bevares")
            return {item.path.resolve() for item in all_snapshots}, tuple(warnings)
    store = ManifestStore(paths.manifest_dir)
    if not paths.manifest_dir.exists():
        return protected, ()
    for path in paths.manifest_dir.rglob("refresh-round*.json"):
        try:
            manifest = store.load(path)
        except (OSError, ValueError, PayloadError) as exc:
            # An unreadable manifest can contain references we cannot identify.
            warnings.append(f"{path.name}: {exc}; snapshots bevares")
            return {item.path.resolve() for item in all_snapshots}, tuple(warnings)
        if manifest.schema_version == 1 or any(
            step.source == "postprocess" and step.retryable
            for step in manifest.steps
        ):
            # Legacy paths are not normalized; alert retries need the referenced
            # transition and its predecessors, including same-round captures.
            protected.update(
                item.path.resolve()
                for item in all_snapshots
                if (item.identity[1] == manifest.game_slug and (
                    not manifest.game_locale
                    or item.identity[0] == manifest.game_locale.casefold()
                ))
            )
        for step in manifest.steps:
            for reference in (step.data_reference, step.cache_reference):
                if not reference or not reference.startswith("snapshots/"):
                    continue
                target = (paths.data_dir / reference).resolve()
                if target.is_relative_to(paths.snapshot_dir.resolve()):
                    protected.add(target)
    return protected, tuple(warnings)


def plan_snapshot_retention(paths: AppPaths) -> RetentionPlan:
    """Select only older valid snapshots, keeping one newest per round."""

    candidates: list[RetentionCandidate] = []
    retained: list[Path] = []
    player_index = PlayerStatisticsStore(paths.snapshot_dir).scan()
    team_index = SnapshotStore(paths.snapshot_dir).scan()
    protected, manifest_warnings = _protected_snapshot_paths(paths, player_index, team_index)

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
            if path in protected:
                retained.append(path)
                continue
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
            if path in protected:
                retained.append(path)
                continue
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
        tuple((*player_index.warnings, *team_index.warnings, *manifest_warnings)),
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

    from .refresh_runs import exclusive_data_operation

    with exclusive_data_operation(paths.data_dir):
        return _archive_retention_candidates_locked(
            paths, candidates, destination=destination, now=now
        )


def _archive_retention_candidates_locked(
    paths: AppPaths,
    candidates: tuple[RetentionCandidate, ...],
    *,
    destination: Path | str | None,
    now: datetime | None,
) -> ArchiveResult:
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
    still_candidates = {
        candidate.path.resolve() for candidate in plan_snapshot_retention(paths).candidates
    }
    for candidate in candidates:
        try:
            if candidate.path.resolve() not in still_candidates:
                errors.append(f"{candidate.path.name}: snapshot er nu nødvendigt og blev bevaret")
                continue
            if candidate.path.stat().st_size != candidate.size or _sha256(candidate.path) != candidate.sha256:
                errors.append(f"{candidate.path.name}: filen er ændret og blev bevaret")
                continue
            candidate.path.unlink()
            removed += 1
        except OSError as exc:
            errors.append(f"{candidate.path.name}: {exc}")
    return ArchiveResult(target.resolve(), len(candidates), required, removed, tuple(errors))


def list_cleanup_candidates(paths: AppPaths) -> tuple[CleanupCandidate, ...]:
    # Old retention archives can contain a manifest's only remaining copy.
    # Do not offer such archives for deletion, including when provenance cannot
    # be established from legacy, incomplete or unreadable records.
    players = PlayerStatisticsStore(paths.snapshot_dir).scan()
    teams = SnapshotStore(paths.snapshot_dir).scan()
    protected, warnings = _protected_snapshot_paths(paths, players, teams)
    preserve_archives = bool(warnings)
    if paths.manifest_dir.exists():
        manifest_store = ManifestStore(paths.manifest_dir)
        for manifest_path in paths.manifest_dir.rglob("refresh-round*.json"):
            try:
                manifest = manifest_store.load(manifest_path)
            except (OSError, ValueError, PayloadError):
                preserve_archives = True
                break
            if manifest.schema_version == 1 or any(
                step.source == "postprocess" and step.retryable
                for step in manifest.steps
            ):
                preserve_archives = True
                break

    def archive_deletable(path: Path) -> bool:
        if preserve_archives:
            return False
        try:
            with zipfile.ZipFile(path) as archive:
                payload = json.loads(archive.read("archive-manifest.json"))
                files = payload.get("files") if isinstance(payload, dict) else None
                if not isinstance(files, list) or payload.get("schema_version") != 1:
                    return False
                for entry in files:
                    if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                        return False
                    target = (paths.data_dir / entry["path"]).resolve()
                    if not target.is_relative_to(paths.snapshot_dir.resolve()) or target in protected:
                        return False
        except (OSError, ValueError, KeyError, zipfile.BadZipFile):
            return False
        return True

    roots = (
        (paths.player_export_dir, "Afledt spillereksport"),
        (paths.team_export_dir, "Afledt holdeksport"),
        (paths.report_dir, "Afledt rapport"),
        (paths.archive_dir, "Mellemversionsarkiv"),
    )
    result: list[CleanupCandidate] = []
    for root, category in roots:
        for path in _files(root):
            if root == paths.archive_dir and not archive_deletable(path):
                continue
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

    from .refresh_runs import exclusive_data_operation

    with exclusive_data_operation(paths.data_dir):
        return _delete_derived_files_locked(paths, selected)


def _delete_derived_files_locked(paths: AppPaths, selected: tuple[Path, ...]) -> tuple[Path, ...]:
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
