"""Whole-Hub ZIP backup validation and rollback-safe restoration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
from typing import BinaryIO
from uuid import uuid4
import zipfile
from zlib import error as CompressionError

from .archive_validation import COPY_CHUNK, bounded_chunks, member_chunks, portable_parts, preflight_archive, read_member
from .canonical_validation import is_refresh_run_file, known_schema_error
from .errors import PayloadError
from .paths import AppPaths
from .persistence import aware_local, exclusive_configuration_update, file_update_lock_path
from .refresh_runs import exclusive_data_operation
from .restore_transaction import RestoreTransaction, recover_restore_locked


BACKUP_SCHEMA_VERSION = 2
_MANIFEST_NAME = "backup-manifest.json"
MAX_BACKUP_MEMBERS = 20_000
MAX_BACKUP_MEMBER_BYTES = 512 * 1024 * 1024
MAX_BACKUP_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_BACKUP_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_BACKUP_COMPRESSION_RATIO = 200
MAX_JSON_VALIDATION_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BackupManifestEntry:
    path: str
    size: int
    sha256: str
    mtime_ns: int | None = None


@dataclass(frozen=True, slots=True)
class BackupManifest:
    schema_version: int
    created_at: datetime
    files: tuple[BackupManifestEntry, ...]
    total_bytes: int


@dataclass(frozen=True, slots=True)
class BackupValidation:
    manifest: BackupManifest | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return self.manifest is not None and not self.errors


@dataclass(frozen=True, slots=True)
class RestoreResult:
    restored_files: int
    restored_bytes: int
    rollback_path: Path


def _canonical_files(paths: AppPaths) -> tuple[tuple[str, Path], ...]:
    operational_locks = {file_update_lock_path(paths.strategy_file), file_update_lock_path(paths.config_dir)}
    roots = (
        ("config", paths.config_dir),
        ("data/snapshots", paths.snapshot_dir),
        ("data/manifests", paths.manifest_dir),
        ("data/refresh-runs", paths.refresh_run_dir),
        ("data/group-revisions", paths.group_revision_dir),
        ("data/tournament-pairings", paths.tournament_pairing_dir),
        ("data/game-metadata", paths.game_metadata_dir),
        ("data/fixtures", paths.fixture_dir),
        ("data/rules", paths.rules_dir),
        ("data/hall-of-fame", paths.hall_of_fame_dir),
        ("data/imports", paths.import_dir),
    )
    result: list[tuple[str, Path]] = []
    for archive_root, root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
                if path.resolve() in operational_locks:
                    continue
                if archive_root == "data/refresh-runs" and not is_refresh_run_file(path.relative_to(root).as_posix()):
                    continue
                relative = path.relative_to(root).as_posix()
                result.append((f"{archive_root}/{relative}", path))
    result.sort(key=lambda item: item[0])
    return tuple(result)


def _manifest_dict(manifest: BackupManifest) -> dict[str, object]:
    return {
        "schema_version": manifest.schema_version,
        "created_at": manifest.created_at.isoformat(),
        "total_bytes": manifest.total_bytes,
        "files": [
            {
                "path": item.path,
                "size": item.size,
                "sha256": item.sha256,
                **({"mtime_ns": item.mtime_ns} if item.mtime_ns is not None else {}),
            }
            for item in manifest.files
        ],
    }


def _write_backup_zip(
    paths: AppPaths, target: Path | BinaryIO, *, now: datetime
) -> BackupManifest:
    sources = _canonical_files(paths)
    if len(sources) + 1 > MAX_BACKUP_MEMBERS:
        raise PayloadError("Backupkilden indeholder for mange filer")
    sizes = tuple(source.stat().st_size for _, source in sources)
    if any(size > MAX_BACKUP_MEMBER_BYTES for size in sizes):
        raise PayloadError("En fil i backupkilden er større end den tilladte grænse")
    if sum(sizes) > MAX_BACKUP_TOTAL_BYTES:
        raise PayloadError("Backupkildens samlede størrelse er for stor")
    entries: list[BackupManifestEntry] = []
    with zipfile.ZipFile(
        target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        for archive_path, source in sources:
            before = source.stat()
            hasher = hashlib.sha256()
            size = 0
            with source.open("rb") as input_file, archive.open(archive_path, "w") as output:
                while chunk := input_file.read(COPY_CHUNK):
                    output.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
            after = source.stat()
            if (before.st_size, before.st_mtime_ns) != (
                after.st_size,
                after.st_mtime_ns,
            ):
                raise OSError(f"Filen ændrede sig under backup: {source}")
            if size != before.st_size:
                raise OSError(f"Filen kunne ikke læses stabilt under backup: {source}")
            entries.append(
                BackupManifestEntry(
                    archive_path,
                    size,
                    hasher.hexdigest(),
                    before.st_mtime_ns,
                )
            )
        manifest = BackupManifest(
            BACKUP_SCHEMA_VERSION,
            now,
            tuple(entries),
            sum(item.size for item in entries),
        )
        archive.writestr(
            _MANIFEST_NAME,
            json.dumps(_manifest_dict(manifest), ensure_ascii=False, indent=2) + "\n",
        )
    return manifest


def create_backup_bytes(
    paths: AppPaths, *, now: datetime | None = None
) -> tuple[bytes, BackupManifest]:
    """Compatibility facade; the archive itself is assembled on disk."""

    _validate_accounts_location(paths)
    with exclusive_data_operation(paths.data_dir), exclusive_configuration_update(paths.config_dir):
        generated = aware_local(now)
        output = BytesIO()
        manifest = _write_backup_zip(paths, output, now=generated)
        return output.getvalue(), manifest


def create_backup(
    paths: AppPaths,
    destination: Path | str | None = None,
    *,
    now: datetime | None = None,
) -> Path:
    """Create one ZIP outside the canonical config/data trees."""

    _validate_accounts_location(paths)
    with exclusive_data_operation(paths.data_dir), exclusive_configuration_update(paths.config_dir):
        return _create_backup_locked(paths, destination, now=now)


def _create_backup_locked(paths, destination, *, now):
    generated = aware_local(now)
    explicit_destination = destination is not None
    if destination is None:
        base = paths.backup_dir / (
            f"holdet-hub-{generated.strftime('%Y%m%d-%H%M%S')}.zip"
        )
        destination = base
        number = 2
        while Path(destination).exists():
            destination = base.with_name(f"{base.stem}-{number}{base.suffix}")
            number += 1
    target = Path(destination)
    resolved_target = target.resolve()
    for canonical_root in (paths.config_dir.resolve(), paths.data_dir.resolve()):
        if resolved_target.is_relative_to(canonical_root):
            raise ValueError(
                "Backupmålet må ikke ligge i det kanoniske data- eller konfigurationstræ"
            )
    if explicit_destination and target.exists():
        raise FileExistsError(f"Backupfilen findes allerede: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    estimated = sum(source.stat().st_size for _, source in _canonical_files(paths))
    if shutil.disk_usage(target.parent).free < estimated + 16 * 1024 * 1024:
        raise OSError("Der er ikke nok ledig diskplads til backupfilen")
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        _write_backup_zip(paths, temporary, now=generated)
        validation = validate_backup(temporary)
        if not validation.is_valid:
            raise PayloadError(
                "Den nye backup bestod ikke valideringen: "
                + "; ".join(validation.errors)
            )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _backup_member_parts(name: str) -> tuple[str, ...]:
    parts = portable_parts(name)
    if len(parts) < 2 or parts[0] not in {"config", "data"}:
        raise PayloadError(f"Usikker sti i ZIP: {name}")
    return parts


def _parse_manifest(data: bytes) -> BackupManifest:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise PayloadError("Backupmanifestet indeholder ugyldig JSON") from exc
    schema_version = payload.get("schema_version") if isinstance(payload, dict) else None
    if type(schema_version) is not int or schema_version not in {1, 2}:
        raise PayloadError("Ukendt skema for backupmanifest")
    files = payload.get("files")
    if not isinstance(files, list):
        raise PayloadError("Backupmanifestet mangler fillisten")
    parsed: list[BackupManifestEntry] = []
    seen: set[str] = set()
    for raw in files:
        if not isinstance(raw, dict):
            raise PayloadError("Backupmanifestet har en ugyldig filpost")
        path = raw.get("path")
        size = raw.get("size")
        digest = raw.get("sha256")
        if (
            not isinstance(path, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise PayloadError("Backupmanifestet har en ugyldig filpost")
        _backup_member_parts(path)
        if path in seen:
            raise PayloadError("Backupmanifestet har duplikerede filstier")
        seen.add(path)
        mtime = raw.get("mtime_ns")
        if mtime is not None and (
            not isinstance(mtime, int) or isinstance(mtime, bool) or mtime < 0
        ):
            raise PayloadError("Backupmanifestet har en ugyldig ændringstid")
        parsed.append(BackupManifestEntry(path, size, digest, mtime))
    try:
        created = datetime.fromisoformat(str(payload["created_at"]))
    except (KeyError, ValueError) as exc:
        raise PayloadError("Backupmanifestet har et ugyldigt tidspunkt") from exc
    if created.tzinfo is None:
        created = created.astimezone()
    total = payload.get("total_bytes")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise PayloadError("Backupmanifestet har en ugyldig totalstørrelse")
    return BackupManifest(schema_version, created, tuple(parsed), total)


def validate_backup(
    source: bytes | bytearray | Path | str | BinaryIO,
    *,
    _validate_schemas: bool = True,
) -> BackupValidation:
    """Validate all members, schemas, sizes and SHA-256 checksums."""

    try:
        with _open_backup(source) as archive:
            return _validate_archive(archive, validate_schemas=_validate_schemas)
    except (OSError, ValueError, zipfile.BadZipFile, PayloadError) as exc:
        return BackupValidation(None, (f"ZIP-filen kunne ikke åbnes: {exc}",))


def _open_backup(source: bytes | bytearray | Path | str | BinaryIO) -> zipfile.ZipFile:
    if isinstance(source, (bytes, bytearray)):
        size = len(source)
        archive_source = BytesIO(source)
    elif isinstance(source, (Path, str)):
        archive_source = Path(source)
        size = archive_source.stat().st_size
    else:
        archive_source = source
        size = source.seek(0, os.SEEK_END)
        source.seek(0)
    if size > MAX_BACKUP_TOTAL_BYTES:
        raise PayloadError("Backupkilden er større end den tilladte grænse")
    return zipfile.ZipFile(archive_source)


def _validate_archive(
    archive: zipfile.ZipFile, *, validate_schemas: bool
) -> BackupValidation:
    try:
        members = preflight_archive(
            archive,
            max_members=MAX_BACKUP_MEMBERS,
            max_member_bytes=MAX_BACKUP_MEMBER_BYTES,
            max_total_bytes=MAX_BACKUP_TOTAL_BYTES,
            max_compression_ratio=MAX_BACKUP_COMPRESSION_RATIO,
        )
        for name in members:
            if name != _MANIFEST_NAME:
                _backup_member_parts(name)
        if _MANIFEST_NAME not in members:
            raise PayloadError("Backupmanifestet mangler")
        manifest = _parse_manifest(
            read_member(archive, members[_MANIFEST_NAME], limit=MAX_BACKUP_MANIFEST_BYTES)
        )
    except (OSError, RuntimeError, zipfile.BadZipFile, CompressionError, PayloadError) as exc:
        return BackupValidation(None, (str(exc),))

    if {entry.path for entry in manifest.files} != members.keys() - {_MANIFEST_NAME}:
        return BackupValidation(manifest, ("Fillisten i manifestet matcher ikke ZIP-indholdet",))
    errors: list[str] = []
    if sum(members[entry.path].file_size for entry in manifest.files) != manifest.total_bytes:
        errors.append("Totalstørrelsen i manifestet matcher ikke")
    for entry in manifest.files:
        member = members[entry.path]
        if member.file_size != entry.size:
            errors.append(f"Filstørrelsen matcher ikke: {entry.path}")
        if validate_schemas and entry.path.casefold().endswith(".json") and entry.size > MAX_JSON_VALIDATION_BYTES:
            errors.append(f"JSON-filen er for stor til skemavalidering: {entry.path}")
    if errors:
        return BackupValidation(manifest, tuple(errors))

    expected = {entry.path.casefold(): entry for entry in manifest.files}
    checked_revisions: set[str] = set()

    def read_revision(name: str) -> object:
        key = f"data/group-revisions/{name}".casefold()
        entry = expected.get(key)
        if entry is None:
            raise PayloadError(f"Turneringsrevisionen mangler i backup: {name}")
        if key in checked_revisions:
            raise PayloadError(f"Turneringsrevisionen er refereret flere gange: {name}")
        checked_revisions.add(key)
        try:
            content = _verify_member(archive, members[entry.path], entry, collect=True)
            return json.loads(content.decode("utf-8"))
        except (OSError, RuntimeError, ValueError, zipfile.BadZipFile, CompressionError) as exc:
            raise PayloadError(f"Turneringsrevisionen kunne ikke læses: {name}") from exc

    for entry in manifest.files:
        compare_schema = validate_schemas and entry.path.casefold().endswith(".json")
        try:
            data = _verify_member(archive, members[entry.path], entry, collect=compare_schema)
        except (OSError, RuntimeError, zipfile.BadZipFile, CompressionError, PayloadError) as exc:
            errors.append(str(exc))
            continue
        if compare_schema and (problem := known_schema_error(entry.path, data, read_revision=read_revision)):
            errors.append(problem)
    return BackupValidation(manifest, tuple(errors))


def _verify_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    entry: BackupManifestEntry,
    *,
    collect: bool = False,
    output: BinaryIO | None = None,
) -> bytes:
    digest = hashlib.sha256()
    data = bytearray()
    for chunk in member_chunks(archive, info, limit=entry.size):
        digest.update(chunk)
        if collect:
            data.extend(chunk)
        if output is not None:
            output.write(chunk)
    if info.file_size != entry.size or digest.hexdigest() != entry.sha256:
        raise PayloadError(f"Checksum matcher ikke: {entry.path}")
    return bytes(data)


def _extract_backup(
    archive: zipfile.ZipFile, manifest: BackupManifest, config: Path, data: Path
) -> None:
    roots = {"config": config.resolve(), "data": data.resolve()}
    for root in roots.values():
        root.mkdir()
    for entry in manifest.files:
        tree, *parts = _backup_member_parts(entry.path)
        root = roots[tree]
        destination = root.joinpath(*parts)
        resolved = destination.resolve()
        if not resolved.is_relative_to(root) or resolved != destination:
            raise PayloadError(f"Usikker sti i ZIP: {entry.path}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.parent.resolve(strict=True) != destination.parent:
            raise PayloadError(f"Usikker sti i ZIP: {entry.path}")
        with destination.open("xb") as output:
            _verify_member(archive, archive.getinfo(entry.path), entry, output=output)


def _validate_accounts_location(paths: AppPaths) -> None:
    if paths.accounts_file.resolve().is_relative_to(paths.data_dir.resolve()):
        raise PayloadError("Kontofilen skal ligge uden for den datamappe der gendannes")


def validate_restore_layout(paths: AppPaths) -> None:
    """Keep canonical trees and exports disjoint before taking any write locks."""

    config, data = paths.config_dir.resolve(), paths.data_dir.resolve()
    if config.is_relative_to(data) or data.is_relative_to(config):
        raise PayloadError("Konfigurations- og datamapperne skal være adskilte ved gendannelse")
    _validate_accounts_location(paths)
    exports = (
        paths.export_dir, paths.backup_dir, paths.player_export_dir,
        paths.team_export_dir, paths.report_dir, paths.archive_dir,
        paths.artifact_registry_file,
    )
    for path in exports:
        export = path.resolve()
        if any(export.is_relative_to(root) or root.is_relative_to(export) for root in (config, data)):
            raise PayloadError("Eksporter og rollback-backup skal ligge uden for mapperne der gendannes")


def restore_backup(
    source: bytes | bytearray | Path | str | BinaryIO,
    paths: AppPaths,
    *,
    now: datetime | None = None,
) -> RestoreResult:
    """Replace config/data only after full validation, with automatic rollback."""

    validate_restore_layout(paths)
    if not isinstance(source, (bytes, bytearray, str, Path)):
        source = b"".join(
            bounded_chunks(source, MAX_BACKUP_TOTAL_BYTES, error="Backupkilden er for stor")
        )
    try:
        archive = _open_backup(source)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise PayloadError(f"ZIP-filen kunne ikke åbnes: {exc}") from exc
    with archive:
        validation = _validate_archive(archive, validate_schemas=True)
        if not validation.is_valid or validation.manifest is None:
            raise PayloadError("; ".join(validation.errors))
        return _restore_validated(archive, validation.manifest, paths, now=now)


def _restore_validated(
    archive: zipfile.ZipFile, manifest: BackupManifest, paths: AppPaths, *, now: datetime | None
) -> RestoreResult:
    with exclusive_data_operation(paths.data_dir, allow_pending_restore=True), exclusive_configuration_update(
        paths.config_dir, allow_pending_restore=True,
    ):
        recover_restore_locked(paths)
        config_parent = paths.config_dir.parent
        data_parent = paths.data_dir.parent
        config_parent.mkdir(parents=True, exist_ok=True)
        data_parent.mkdir(parents=True, exist_ok=True)
        required = manifest.total_bytes + 32 * 1024 * 1024
        for parent in {config_parent.resolve(), data_parent.resolve()}:
            if shutil.disk_usage(parent).free < required:
                raise PayloadError("Der er ikke nok ledig diskplads til sikker gendannelse")
        rollback_base = paths.backup_dir / (
            f"rollback-{aware_local(now).strftime('%Y%m%d-%H%M%S')}.zip"
        )
        rollback_target = rollback_base
        number = 2
        while rollback_target.exists():
            rollback_target = rollback_base.with_name(
                f"{rollback_base.stem}-{number}{rollback_base.suffix}"
            )
            number += 1
        rollback_target.parent.mkdir(parents=True, exist_ok=True)
        rollback_temporary = rollback_target.parent / (
            f".{rollback_target.name}.{uuid4().hex}.tmp"
        )
        try:
            _write_backup_zip(paths, rollback_temporary, now=aware_local(now))
            rollback_validation = validate_backup(
                rollback_temporary, _validate_schemas=False
            )
            if not rollback_validation.is_valid:
                raise PayloadError(
                    "Rollback-backuppen bestod ikke checksumvalideringen: "
                    + "; ".join(rollback_validation.errors)
                )
            os.replace(rollback_temporary, rollback_target)
        finally:
            rollback_temporary.unlink(missing_ok=True)
        rollback = rollback_target.resolve()
        transaction = RestoreTransaction.start(paths, rollback)
        try:
            for parent in {transaction.config.stage.parent, transaction.data.stage.parent}:
                parent.mkdir()
            _extract_backup(archive, manifest, transaction.config.stage, transaction.data.stage)
            archive.close()
            transaction.install()
        except Exception as exc:
            try:
                recover_restore_locked(paths)
            except Exception as rollback_exc:
                raise PayloadError(
                    f"Gendannelsen mislykkedes, og tilbagerulningen mislykkedes: {rollback_exc}. "
                    f"Rollback-ZIP: {rollback}"
                ) from exc
            raise PayloadError(
                f"Gendannelsen mislykkedes; den oprindelige installation er lagt tilbage: {exc}"
            ) from exc
        recover_restore_locked(paths)
        return RestoreResult(
            len(manifest.files),
            manifest.total_bytes,
            rollback,
        )

