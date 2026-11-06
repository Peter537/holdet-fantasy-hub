"""Preview-first import of backups, snapshots and legacy Hub exports."""

from __future__ import annotations

from .refresh_runs import exclusive_data_operation

from dataclasses import dataclass, field
from datetime import datetime
import hashlib
from io import BytesIO
import json
from pathlib import Path, PurePosixPath
import re
import stat
from typing import BinaryIO, Literal
import zipfile

from .backup import RestoreResult, restore_backup, validate_backup
from .errors import PayloadError
from .filenames import collision_suffix
from .output import sanitize_path_component
from .paths import AppPaths
from .persistence import publish_immutable
from .player_serialization import player_statistics_from_dict
from .serialization import team_from_dict


ImportKind = Literal[
    "backup", "canonical_snapshots", "legacy_json", "archive_only", "invalid"
]

MAX_IMPORT_BYTES = 2 * 1024 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 20_000
MAX_ARCHIVE_MEMBER_BYTES = 512 * 1024 * 1024
MAX_ARCHIVE_TOTAL_BYTES = 4 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
MAX_CANONICAL_JSON_BYTES = 64 * 1024 * 1024
_COPY_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class ImportOperation:
    source_name: str
    target: Path
    size: int
    sha256: str
    action: Literal["write", "skip"]


@dataclass(frozen=True, slots=True)
class ImportPreview:
    filename: str
    kind: ImportKind
    checksum: str
    operations: tuple[ImportOperation, ...]
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    restorable: bool = False
    _payloads: tuple[tuple[Path, bytes], ...] = field(
        default=(), repr=False, compare=False
    )

    @property
    def can_apply(self) -> bool:
        return self.kind != "invalid" and not self.errors

    @property
    def write_count(self) -> int:
        return sum(operation.action == "write" for operation in self.operations)

    @property
    def skipped_count(self) -> int:
        return sum(operation.action == "skip" for operation in self.operations)


@dataclass(frozen=True, slots=True)
class ImportResult:
    kind: ImportKind
    written_files: int
    skipped_files: int
    restored: RestoreResult | None = None


def _source_bytes(source: bytes | bytearray | Path | str | BinaryIO) -> bytes:
    if isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
    elif hasattr(source, "read"):
        raw = source.read()
        if not isinstance(raw, bytes):
            raise PayloadError("Importkilden skal levere bytes")
    else:
        path = Path(source)
        if path.stat().st_size > MAX_IMPORT_BYTES:
            raise PayloadError("Importfilen er større end den tilladte grænse")
        raw = path.read_bytes()
    if len(raw) > MAX_IMPORT_BYTES:
        raise PayloadError("Importfilen er større end den tilladte grænse")
    return raw


def _safe_filename(filename: str) -> str:
    name = Path(filename).name
    if not name or name in {".", ".."}:
        return "import.bin"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip(".-") or "import.bin"


def _operation(target: Path, source_name: str, content: bytes) -> tuple[ImportOperation, str | None]:
    digest = hashlib.sha256(content).hexdigest()
    if not target.exists():
        return ImportOperation(source_name, target.resolve(), len(content), digest, "write"), None
    try:
        existing = target.read_bytes()
    except OSError as exc:
        return (
            ImportOperation(source_name, target.resolve(), len(content), digest, "write"),
            f"Eksisterende mål kunne ikke læses: {target.name}: {exc}",
        )
    if hashlib.sha256(existing).hexdigest() == digest:
        return ImportOperation(source_name, target.resolve(), len(content), digest, "skip"), None
    return (
        ImportOperation(source_name, target.resolve(), len(content), digest, "write"),
        f"Identitetskollision med andet indhold: {target.name}",
    )


def _generated_at(payload: dict[str, object]) -> datetime:
    value = payload.get("generated_at")
    if not isinstance(value, str):
        raise PayloadError("Det kanoniske snapshot mangler generated_at")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PayloadError("Det kanoniske snapshot har ugyldigt generated_at") from exc
    return result.astimezone() if result.tzinfo is None else result


def _canonical_target(
    payload: dict[str, object], paths: AppPaths
) -> tuple[Path, str]:
    generated = _generated_at(payload)
    timestamp = generated.strftime("%m%d_%H%M%S")
    if "entries" in payload:
        statistics = player_statistics_from_dict(payload)
        target = (
            paths.snapshot_dir
            / sanitize_path_component(statistics.game.slug, fallback="game")
            / "players"
            / f"player-round{statistics.round_number}_{timestamp}.json"
        )
        identity = (
            f"players:{statistics.game.locale.casefold()}:{statistics.game.slug}:"
            f"{statistics.round_number}:{generated.isoformat()}"
        )
        return target, identity
    team = team_from_dict(payload)
    account = (
        team.reference.account_key
        if team.reference.account_key != "direct"
        else sanitize_path_component(team.owner_name, fallback="direct")
    )
    team_name = sanitize_path_component(
        team.team_name, fallback=f"team-{team.reference.team_id}"
    )
    target = (
        paths.snapshot_dir
        / sanitize_path_component(team.reference.game.slug, fallback="game")
        / "teams"
        / sanitize_path_component(account, fallback="account")
        / f"{team_name}-{team.reference.team_id}"
        / f"team-round{team.overview.current_round}_{timestamp}.json"
    )
    identity = (
        f"teams:{team.reference.game.locale.casefold()}:{team.reference.game.slug}:"
        f"{team.reference.team_id}:{team.overview.current_round}:{generated.isoformat()}"
    )
    return target, identity


class _CanonicalImportPlanner:
    """Reserve distinct immutable targets across one canonical import preview."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths
        self._families: dict[Path, tuple[tuple[Path, str, str], ...]] = {}
        self._identities: dict[Path, dict[str, tuple[tuple[Path, str, str], ...]]] = {}
        self._reserved: set[Path] = set()
        self._next_collision: dict[Path, int] = {}

    def _family(self, base: Path) -> tuple[tuple[Path, str, str], ...]:
        if base not in self._families:
            pattern = re.compile(
                rf"{re.escape(base.stem)}(?:_[0-9]+)?\.json", re.IGNORECASE
            )
            existing: list[tuple[Path, str, str]] = []
            for path in sorted(base.parent.glob(f"{base.stem}*.json")):
                if pattern.fullmatch(path.name) is None:
                    continue
                try:
                    if path.is_symlink():
                        raise PayloadError("Snapshotmålet må ikke være et symbolsk link")
                    content = path.read_bytes()
                    payload = json.loads(content.decode("utf-8-sig"))
                    if not isinstance(payload, dict):
                        raise PayloadError("JSON-roden skal være et objekt")
                    _, identity = _canonical_target(payload, self.paths)
                except (OSError, UnicodeDecodeError, ValueError, TypeError, PayloadError) as exc:
                    raise PayloadError(
                        f"Eksisterende snapshot kunne ikke læses eller klassificeres: {path.name}: {exc}"
                    ) from exc
                existing.append((path.resolve(), hashlib.sha256(content).hexdigest(), identity))
            self._families[base] = tuple(existing)
        return self._families[base]

    def _observations(self, base: Path, identity: str) -> tuple[tuple[Path, str, str], ...]:
        teams = identity.startswith("teams:")
        root = base.parents[2] if teams else base.parent
        if root not in self._identities:
            grouped: dict[str, list[tuple[Path, str, str]]] = {}
            candidates = root.rglob("team-round*.json") if teams else root.glob("player-round*.json")
            for path in sorted(candidates):
                try:
                    if path.is_symlink():
                        continue
                    content = path.read_bytes()
                    payload = json.loads(content.decode("utf-8"))
                    if not isinstance(payload, dict):
                        continue
                    if teams:
                        team_from_dict(payload)
                    else:
                        player_statistics_from_dict(payload)
                    _, observed_identity = _canonical_target(payload, self.paths)
                except (OSError, UnicodeDecodeError, ValueError, TypeError, PayloadError):
                    # Unrelated damaged snapshots are excluded by the store scan too.
                    # Occupied files in the target family are checked strictly above.
                    continue
                grouped.setdefault(observed_identity, []).append(
                    (path.resolve(), hashlib.sha256(content).hexdigest(), observed_identity)
                )
            self._identities[root] = {
                key: tuple(values) for key, values in grouped.items()
            }
        return self._identities[root].get(identity, ())

    def plan(
        self, base: Path, identity: str, source_name: str, content: bytes
    ) -> tuple[ImportOperation, str | None]:
        base = base.resolve()
        digest = hashlib.sha256(content).hexdigest()
        try:
            family = self._family(base)
        except (OSError, PayloadError) as exc:
            return ImportOperation(source_name, base, len(content), digest, "write"), str(exc)
        family = (*family, *self._observations(base, identity))
        for target, existing_digest, existing_identity in family:
            if existing_identity == identity and existing_digest != digest:
                return (
                    ImportOperation(source_name, target, len(content), digest, "write"),
                    f"Identitetskollision med andet indhold: {target.name}",
                )
        # Search the whole family before filling a gap left by retention.
        for target, existing_digest, _ in family:
            if existing_digest == digest:
                return ImportOperation(source_name, target, len(content), digest, "skip"), None
        collision_number = self._next_collision.get(base, 0)
        while True:
            target = base.with_name(
                f"{base.stem}{collision_suffix(collision_number)}{base.suffix}"
            )
            paired_text = target.with_suffix(".txt")
            occupied = target in self._reserved or target.exists() or target.is_symlink()
            if identity.startswith("teams:"):
                occupied = occupied or paired_text.exists() or paired_text.is_symlink()
            if not occupied:
                self._reserved.add(target)
                self._next_collision[base] = collision_number + 1
                return ImportOperation(source_name, target, len(content), digest, "write"), None
            collision_number += 1


def _single_file_preview(
    raw: bytes, filename: str, paths: AppPaths
) -> ImportPreview:
    digest = hashlib.sha256(raw).hexdigest()
    extension = Path(filename).suffix.casefold()
    if extension in {".txt", ".md", ".markdown", ".csv"}:
        target = (
            paths.import_dir
            / "archive-only"
            / f"{digest[:16]}-{_safe_filename(filename)}"
        )
        operation, error = _operation(target, filename, raw)
        return ImportPreview(
            filename,
            "archive_only",
            digest,
            (operation,),
            (() if error is None else (error,)),
            ("Filen arkiveres uændret og parses ikke til domænedata.",),
            False,
            ((target, raw),),
        )
    if extension != ".json":
        return ImportPreview(
            filename,
            "invalid",
            digest,
            (),
            ("Filtypen kan ikke importeres.",),
        )
    try:
        payload = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ImportPreview(filename, "invalid", digest, (), (f"Ugyldig JSON: {exc}",))
    if not isinstance(payload, dict):
        return ImportPreview(filename, "invalid", digest, (), ("JSON-roden skal være et objekt.",))
    try:
        target, identity = _canonical_target(payload, paths)
    except (PayloadError, ValueError, TypeError):
        target = paths.import_dir / "legacy-json" / f"{digest[:16]}-{_safe_filename(filename)}"
        operation, error = _operation(target, filename, raw)
        return ImportPreview(
            filename,
            "legacy_json",
            digest,
            (operation,),
            (() if error is None else (error,)),
            (
                "JSON-eksporten gemmes som et read-only historisk datasæt og blandes ikke ind i analyser.",
            ),
            False,
            ((target, raw),),
        )
    operation, error = _CanonicalImportPlanner(paths).plan(target, identity, filename, raw)
    return ImportPreview(
        filename,
        "canonical_snapshots",
        digest,
        (operation,),
        (() if error is None else (error,)),
        (),
        True,
        ((operation.target, raw),),
    )


def _safe_archive_name(name: str) -> bool:
    if not name or "\\" in name or name.startswith("/"):
        return False
    path = PurePosixPath(name)
    return not path.is_absolute() and all(part not in {"", ".", ".."} for part in path.parts)


def _read_archive_member(
    archive: zipfile.ZipFile, info: zipfile.ZipInfo, *, limit: int
) -> bytes:
    content = bytearray()
    with archive.open(info) as member:
        while chunk := member.read(_COPY_CHUNK):
            content.extend(chunk)
            if len(content) > limit:
                raise PayloadError(f"ZIP-medlemmet er for stort: {info.filename}")
    if len(content) != info.file_size:
        raise PayloadError(f"ZIP-medlemmets størrelse ændrede sig: {info.filename}")
    return bytes(content)


def _support_package_errors(
    archive: zipfile.ZipFile, names: tuple[str, ...]
) -> list[str]:
    errors: list[str] = []
    manifest_info = archive.getinfo("support-manifest.json")
    if manifest_info.file_size > MAX_MANIFEST_BYTES:
        return ["Supportmanifestet er for stort"]
    try:
        manifest = json.loads(
            _read_archive_member(
                archive, manifest_info, limit=MAX_MANIFEST_BYTES
            ).decode("utf-8")
        )
    except (UnicodeDecodeError, json.JSONDecodeError, PayloadError, RuntimeError) as exc:
        return [f"Ugyldigt supportmanifest: {exc}"]
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or manifest.get("restorable") is not False
        or manifest.get("privacy_profile") not in {"share", "debug"}
        or not isinstance(files, list)
    ):
        return ["Supportpakken har et ukendt eller gendanneligt manifest"]
    expected: set[str] = set()
    for item in files:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or not isinstance(item.get("size"), int)
            or isinstance(item.get("size"), bool)
            or not isinstance(item.get("sha256"), str)
        ):
            errors.append("Supportmanifestet har en ugyldig filpost")
            continue
        member_name = item["path"]
        if not _safe_archive_name(member_name) or member_name == "support-manifest.json":
            errors.append("Supportmanifestet har en usikker filsti")
            continue
        if member_name in expected:
            errors.append("Supportmanifestet har duplikerede filstier")
            continue
        expected.add(member_name)
        if member_name not in names:
            errors.append(f"Supportfilen mangler: {member_name}")
            continue
        try:
            content = _read_archive_member(
                archive,
                archive.getinfo(member_name),
                limit=MAX_ARCHIVE_MEMBER_BYTES,
            )
        except (KeyError, OSError, PayloadError, RuntimeError) as exc:
            errors.append(f"Supportfilen kunne ikke læses: {member_name}: {exc}")
            continue
        if item["size"] != len(content) or item["sha256"] != hashlib.sha256(content).hexdigest():
            errors.append(f"Checksum eller størrelse matcher ikke: {member_name}")
    if set(names) - {"support-manifest.json"} != expected:
        errors.append("Supportmanifestet matcher ikke ZIP-indholdet")
    return errors


def _snapshot_package_preview(raw: bytes, filename: str, paths: AppPaths) -> ImportPreview:
    digest = hashlib.sha256(raw).hexdigest()
    errors: list[str] = []
    operations: list[ImportOperation] = []
    payloads: list[tuple[Path, bytes]] = []
    identities: set[str] = set()
    planner = _CanonicalImportPlanner(paths)
    try:
        archive = zipfile.ZipFile(BytesIO(raw))
    except zipfile.BadZipFile as exc:
        return ImportPreview(filename, "invalid", digest, (), (f"Ugyldig ZIP: {exc}",))
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_ARCHIVE_MEMBERS:
            errors.append("ZIP-filen indeholder for mange medlemmer")
        names = tuple(info.filename for info in infos)
        if len(names) != len(set(names)):
            errors.append("ZIP-filen indeholder duplikerede stier")
        for info in infos:
            if not _safe_archive_name(info.filename) or info.is_dir():
                errors.append(f"Usikker eller ugyldig ZIP-post: {info.filename}")
            if stat.S_ISLNK(info.external_attr >> 16):
                errors.append(f"ZIP-filen indeholder et symbolsk link: {info.filename}")
            if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
                errors.append(f"ZIP-medlemmet er for stort: {info.filename}")
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                errors.append(f"ZIP-medlemmet har mistænkelig kompressionsratio: {info.filename}")
        if sum(info.file_size for info in infos) > MAX_ARCHIVE_TOTAL_BYTES:
            errors.append("ZIP-filens udpakkede størrelse er for stor")
        manifest_name = next(
            (name for name in ("snapshot-package-manifest.json", "support-manifest.json") if name in names),
            None,
        )
        if manifest_name == "support-manifest.json":
            errors.extend(_support_package_errors(archive, names))
            target = paths.import_dir / "archive-only" / f"{digest[:16]}-{_safe_filename(filename)}"
            operation, error = _operation(target, filename, raw)
            return ImportPreview(
                filename,
                "archive_only",
                digest,
                (operation,),
                tuple(errors + ([] if error is None else [error])),
                ("Supportpakken er markeret som ikke-gendannelig og arkiveres uændret.",),
                False,
                ((target, raw),),
            )
        if manifest_name is None:
            errors.append("ZIP-filen er hverken en Hub-backup eller en snapshotpakke")
            return ImportPreview(filename, "invalid", digest, (), tuple(errors))
        manifest_info = archive.getinfo(manifest_name)
        if manifest_info.file_size > MAX_MANIFEST_BYTES:
            errors.append("Snapshotmanifestet er for stort")
            return ImportPreview(filename, "invalid", digest, (), tuple(errors))
        try:
            manifest = json.loads(
                _read_archive_member(
                    archive, manifest_info, limit=MAX_MANIFEST_BYTES
                ).decode("utf-8")
            )
        except (UnicodeDecodeError, json.JSONDecodeError, PayloadError, RuntimeError) as exc:
            errors.append(f"Ugyldigt snapshotmanifest: {exc}")
            return ImportPreview(filename, "invalid", digest, (), tuple(errors))
        entries = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or not isinstance(entries, list):
            errors.append("Ukendt snapshotpakkeschema")
            return ImportPreview(filename, "invalid", digest, (), tuple(errors))
        expected_names: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
                errors.append("Snapshotmanifestet har en ugyldig filpost")
                continue
            member = entry["path"]
            expected_names.add(member)
            if member not in names or not member.endswith(".json"):
                errors.append(f"Snapshotfilen mangler eller har forkert type: {member}")
                continue
            member_info = archive.getinfo(member)
            if member_info.file_size > MAX_CANONICAL_JSON_BYTES:
                errors.append(f"Det kanoniske snapshot er for stort: {member}")
                continue
            try:
                content = _read_archive_member(
                    archive, member_info, limit=MAX_CANONICAL_JSON_BYTES
                )
            except (OSError, PayloadError, RuntimeError) as exc:
                errors.append(f"Snapshotfilen kunne ikke læses: {member}: {exc}")
                continue
            if entry.get("size") != len(content) or entry.get("sha256") != hashlib.sha256(content).hexdigest():
                errors.append(f"Checksum eller størrelse matcher ikke: {member}")
                continue
            try:
                payload = json.loads(content.decode("utf-8-sig"))
                if not isinstance(payload, dict):
                    raise PayloadError("JSON-roden skal være et objekt")
                target, identity = _canonical_target(payload, paths)
            except (UnicodeDecodeError, json.JSONDecodeError, PayloadError, ValueError, TypeError) as exc:
                errors.append(f"Ugyldigt kanonisk snapshot {member}: {exc}")
                continue
            if identity in identities:
                errors.append(f"Snapshotpakken har en duplikeret identitet: {member}")
                continue
            identities.add(identity)
            operation, collision = planner.plan(target, identity, member, content)
            operations.append(operation)
            payloads.append((operation.target, content))
            if collision:
                errors.append(collision)
        unexpected = set(names) - {manifest_name} - expected_names
        if unexpected:
            errors.append("Snapshotmanifestet matcher ikke ZIP-indholdet")
    return ImportPreview(
        filename,
        "canonical_snapshots" if not errors else "invalid",
        digest,
        tuple(operations),
        tuple(dict.fromkeys(errors)),
        (),
        not errors,
        tuple(payloads),
    )


def preview_import(
    source: bytes | bytearray | Path | str | BinaryIO,
    paths: AppPaths,
    *,
    filename: str | None = None,
) -> ImportPreview:
    """Inspect and preflight an import without writing to the filesystem."""

    raw = _source_bytes(source)
    selected_name = filename or (Path(source).name if isinstance(source, (Path, str)) else "upload.bin")
    digest = hashlib.sha256(raw).hexdigest()
    if raw.startswith(b"PK\x03\x04") or Path(selected_name).suffix.casefold() == ".zip":
        validation = validate_backup(raw)
        if validation.is_valid and validation.manifest is not None:
            operations = tuple(
                ImportOperation(item.path, paths.data_dir, item.size, item.sha256, "write")
                for item in validation.manifest.files
            )
            return ImportPreview(
                selected_name,
                "backup",
                digest,
                operations,
                (),
                validation.warnings,
                True,
                (),
            )
        return _snapshot_package_preview(raw, selected_name, paths)
    return _single_file_preview(raw, selected_name, paths)


def apply_import(
    preview: ImportPreview,
    source: bytes | bytearray | Path | str | BinaryIO,
    paths: AppPaths,
) -> ImportResult:
    """Apply an unchanged, valid preview as one fail-before-write operation."""

    raw = _source_bytes(source)
    if hashlib.sha256(raw).hexdigest() != preview.checksum:
        raise PayloadError("Importfilen har ændret sig siden forhåndsvisningen")
    if preview.kind == "backup":
        current = preview_import(raw, paths, filename=preview.filename)
        if not current.can_apply or current.kind != preview.kind:
            raise PayloadError("Importen kan ikke længere anvendes: " + "; ".join(current.errors))
        restored = restore_backup(raw, paths)
        return ImportResult("backup", restored.restored_files, 0, restored)
    with exclusive_data_operation(paths.data_dir):
        current = preview_import(raw, paths, filename=preview.filename)
        if not current.can_apply or current.kind != preview.kind:
            raise PayloadError("Importen kan ikke længere anvendes: " + "; ".join(current.errors))
        published: list[Path] = []
        try:
            for operation, (target, content) in zip(current.operations, current._payloads, strict=True):
                if operation.action == "skip":
                    continue
                publish_immutable(target, content)
                published.append(target)
        except Exception:
            for path in published:
                path.unlink(missing_ok=True)
            raise
        return ImportResult(current.kind, len(published), current.skipped_count)
