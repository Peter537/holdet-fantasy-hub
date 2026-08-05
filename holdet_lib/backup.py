"""Whole-Hub ZIP backup validation and rollback-safe restoration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
from typing import BinaryIO
from uuid import uuid4
import zipfile

from .errors import PayloadError
from .paths import AppPaths
from .persistence import aware_local


BACKUP_SCHEMA_VERSION = 1
_MANIFEST_NAME = "backup-manifest.json"


@dataclass(frozen=True, slots=True)
class BackupManifestEntry:
    path: str
    size: int
    sha256: str


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
    roots = (
        ("config", paths.config_dir),
        ("data/snapshots", paths.snapshot_dir),
        ("data/manifests", paths.manifest_dir),
        ("data/group-revisions", paths.group_revision_dir),
        ("data/tournament-pairings", paths.tournament_pairing_dir),
        ("data/game-metadata", paths.game_metadata_dir),
        ("data/hall-of-fame", paths.hall_of_fame_dir),
    )
    result: list[tuple[str, Path]] = []
    for archive_root, root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file() and not path.is_symlink():
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
            {"path": item.path, "size": item.size, "sha256": item.sha256}
            for item in manifest.files
        ],
    }


def create_backup_bytes(
    paths: AppPaths, *, now: datetime | None = None
) -> tuple[bytes, BackupManifest]:
    """Build a deterministic canonical backup in memory."""

    entries: list[BackupManifestEntry] = []
    payloads: list[tuple[str, bytes]] = []
    for archive_path, source in _canonical_files(paths):
        data = source.read_bytes()
        payloads.append((archive_path, data))
        entries.append(
            BackupManifestEntry(
                archive_path,
                len(data),
                hashlib.sha256(data).hexdigest(),
            )
        )
    manifest = BackupManifest(
        BACKUP_SCHEMA_VERSION,
        aware_local(now),
        tuple(entries),
        sum(item.size for item in entries),
    )
    output = BytesIO()
    with zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
    ) as archive:
        for archive_path, data in payloads:
            archive.writestr(archive_path, data)
        archive.writestr(
            _MANIFEST_NAME,
            json.dumps(_manifest_dict(manifest), ensure_ascii=False, indent=2) + "\n",
        )
    return output.getvalue(), manifest


def create_backup(
    paths: AppPaths,
    destination: Path | str | None = None,
    *,
    now: datetime | None = None,
) -> Path:
    """Create one ZIP outside the canonical config/data trees."""

    generated = aware_local(now)
    if destination is None:
        destination = paths.backup_dir / (
            f"holdet-hub-{generated.strftime('%Y%m%d-%H%M%S')}.zip"
        )
    target = Path(destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    data, _ = create_backup_bytes(paths, now=generated)
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        temporary.write_bytes(data)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _source_bytes(source: bytes | bytearray | Path | str | BinaryIO) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        return bytes(source)
    if hasattr(source, "read"):
        value = source.read()
        if not isinstance(value, bytes):
            raise PayloadError("Backupkilden skal levere bytes")
        return value
    return Path(source).read_bytes()


def _safe_member(name: str) -> str | None:
    if not name or "\\" in name or name.startswith(("/", "\\")):
        return None
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    if path.parts[0] not in {"config", "data", _MANIFEST_NAME}:
        return None
    if len(path.parts) == 1 and name != _MANIFEST_NAME:
        return None
    return path.as_posix()


def _known_schema(path: str, data: bytes) -> str | None:
    if not path.endswith(".json"):
        return None
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return f"{path}: ugyldig JSON"
    if not isinstance(payload, dict):
        return f"{path}: JSON-roden skal være et objekt"
    version = payload.get("schema_version")
    if path == "config/groups.json" and version == 8:
        return None
    if path == "config/groups.json" and version not in {1, 2, 3, 4, 5, 6, 7}:
        return f"{path}: ukendt gruppeschema"
    if path == "config/groups.json" and version == 8:
        return None
    if path == "config/hub-settings.json" and version == 2:
        return None
    if path == "config/seasons.json" and version == 1:
        return None
    if path.startswith("data/tournament-pairings/") and version == 1:
        return None
    if path.startswith("data/hall-of-fame/") and version == 2:
        return None
    if path == "config/hub-settings.json" and version != 1:
        return f"{path}: ukendt Hub-schema"
    if path.startswith("data/game-metadata/") and version != 1:
        return f"{path}: ukendt metadataschema"
    if path.startswith("data/hall-of-fame/") and version != 1:
        return f"{path}: ukendt Hall of Fame-schema"
    if "/players/" in path and "player-round" in path and version not in {1, 2, 3}:
        return f"{path}: ukendt spillersnapshotschema"
    if "/teams/" in path and "team-round" in path and version not in {1, 2}:
        return f"{path}: ukendt teamsnapshotschema"
    if path.startswith("data/manifests/") and version != 1:
        return f"{path}: ukendt manifestschema"
    if path.startswith("data/group-revisions/") and version != 1:
        return f"{path}: ukendt revisionsschema"
    return None


def _parse_manifest(data: bytes) -> BackupManifest:
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PayloadError("Backupmanifestet indeholder ugyldig JSON") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise PayloadError("Ukendt skema for backupmanifest")
    files = payload.get("files")
    if not isinstance(files, list):
        raise PayloadError("Backupmanifestet mangler fillisten")
    parsed: list[BackupManifestEntry] = []
    for raw in files:
        if not isinstance(raw, dict):
            raise PayloadError("Backupmanifestet har en ugyldig filpost")
        path = raw.get("path")
        size = raw.get("size")
        digest = raw.get("sha256")
        if (
            not isinstance(path, str)
            or _safe_member(path) != path
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise PayloadError("Backupmanifestet har en ugyldig filpost")
        parsed.append(BackupManifestEntry(path, size, digest))
    try:
        created = datetime.fromisoformat(str(payload["created_at"]))
    except (KeyError, ValueError) as exc:
        raise PayloadError("Backupmanifestet har et ugyldigt tidspunkt") from exc
    if created.tzinfo is None:
        created = created.astimezone()
    total = payload.get("total_bytes")
    if not isinstance(total, int) or isinstance(total, bool) or total < 0:
        raise PayloadError("Backupmanifestet har en ugyldig totalstørrelse")
    return BackupManifest(1, created, tuple(parsed), total)


def validate_backup(
    source: bytes | bytearray | Path | str | BinaryIO,
) -> BackupValidation:
    """Validate all members, schemas, sizes and SHA-256 checksums."""

    errors: list[str] = []
    warnings: list[str] = []
    try:
        raw = _source_bytes(source)
        archive = zipfile.ZipFile(BytesIO(raw))
    except (OSError, zipfile.BadZipFile, PayloadError) as exc:
        return BackupValidation(None, (f"ZIP-filen kunne ikke åbnes: {exc}",))
    with archive:
        infos = archive.infolist()
        names: list[str] = []
        for info in infos:
            safe = _safe_member(info.filename)
            if safe is None or safe != info.filename:
                errors.append(f"Usikker sti i ZIP: {info.filename}")
                continue
            unix_mode = info.external_attr >> 16
            if stat.S_ISLNK(unix_mode):
                errors.append(f"Links er ikke tilladt i backup: {info.filename}")
            if info.is_dir():
                errors.append(f"Mappeposter er ikke tilladt: {info.filename}")
            if info.file_size > 512 * 1024 * 1024:
                errors.append(f"Filen er for stor: {info.filename}")
            names.append(info.filename)
        if len(names) != len(set(names)):
            errors.append("ZIP-filen indeholder dubletter")
        if _MANIFEST_NAME not in names:
            return BackupValidation(None, tuple((*errors, "Backupmanifestet mangler")))
        try:
            manifest = _parse_manifest(archive.read(_MANIFEST_NAME))
        except (KeyError, PayloadError) as exc:
            return BackupValidation(None, tuple((*errors, str(exc))))
        expected = {item.path: item for item in manifest.files}
        actual = set(names) - {_MANIFEST_NAME}
        if set(expected) != actual:
            errors.append("Fillisten i manifestet matcher ikke ZIP-indholdet")
        checked_total = 0
        for path, entry in expected.items():
            try:
                data = archive.read(path)
            except KeyError:
                continue
            checked_total += len(data)
            if len(data) != entry.size:
                errors.append(f"Filstørrelsen matcher ikke: {path}")
            if hashlib.sha256(data).hexdigest() != entry.sha256:
                errors.append(f"Checksum matcher ikke: {path}")
            if schema_error := _known_schema(path, data):
                errors.append(schema_error)
        if checked_total != manifest.total_bytes:
            errors.append("Totalstørrelsen i manifestet matcher ikke")
    return BackupValidation(manifest, tuple(dict.fromkeys(errors)), tuple(warnings))


def _extract_tree(archive: zipfile.ZipFile, prefix: str, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for info in archive.infolist():
        if not info.filename.startswith(prefix + "/"):
            continue
        relative = PurePosixPath(info.filename).relative_to(prefix)
        destination = target.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with archive.open(info, "r") as source, destination.open("wb") as output:
            shutil.copyfileobj(source, output)


def restore_backup(
    source: bytes | bytearray | Path | str | BinaryIO,
    paths: AppPaths,
    *,
    now: datetime | None = None,
) -> RestoreResult:
    """Replace config/data only after full validation, with automatic rollback."""

    raw = _source_bytes(source)
    validation = validate_backup(raw)
    if not validation.is_valid or validation.manifest is None:
        raise PayloadError("; ".join(validation.errors))
    rollback = create_backup(
        paths,
        paths.backup_dir
        / f"rollback-{aware_local(now).strftime('%Y%m%d-%H%M%S')}.zip",
        now=now,
    )
    config_parent = paths.config_dir.parent
    data_parent = paths.data_dir.parent
    config_parent.mkdir(parents=True, exist_ok=True)
    data_parent.mkdir(parents=True, exist_ok=True)
    config_temp = Path(tempfile.mkdtemp(prefix=".holdet-restore-", dir=config_parent))
    data_temp = Path(tempfile.mkdtemp(prefix=".holdet-restore-", dir=data_parent))
    staged_config = config_temp / "config"
    staged_data = data_temp / "data"
    old_config = config_parent / f".{paths.config_dir.name}.old-{uuid4().hex}"
    old_data = data_parent / f".{paths.data_dir.name}.old-{uuid4().hex}"
    moved_config = moved_data = installed_config = installed_data = False
    try:
        with zipfile.ZipFile(BytesIO(raw)) as archive:
            _extract_tree(archive, "config", staged_config)
            _extract_tree(archive, "data", staged_data)
        if paths.config_dir.exists():
            os.replace(paths.config_dir, old_config)
            moved_config = True
        os.replace(staged_config, paths.config_dir)
        installed_config = True
        if paths.data_dir.exists():
            os.replace(paths.data_dir, old_data)
            moved_data = True
        os.replace(staged_data, paths.data_dir)
        installed_data = True
    except Exception as exc:
        try:
            if installed_data and paths.data_dir.exists():
                shutil.rmtree(paths.data_dir)
            if moved_data and old_data.exists():
                os.replace(old_data, paths.data_dir)
            if installed_config and paths.config_dir.exists():
                shutil.rmtree(paths.config_dir)
            if moved_config and old_config.exists():
                os.replace(old_config, paths.config_dir)
        except Exception as rollback_exc:
            raise PayloadError(
                f"Gendannelsen mislykkedes, og tilbagerulningen mislykkedes: {rollback_exc}. "
                f"Rollback-ZIP: {rollback}"
            ) from exc
        raise PayloadError(
            f"Gendannelsen mislykkedes; den oprindelige installation er lagt tilbage: {exc}"
        ) from exc
    finally:
        shutil.rmtree(config_temp, ignore_errors=True)
        shutil.rmtree(data_temp, ignore_errors=True)
    shutil.rmtree(old_config, ignore_errors=True)
    shutil.rmtree(old_data, ignore_errors=True)
    return RestoreResult(
        len(validation.manifest.files),
        validation.manifest.total_bytes,
        rollback,
    )

