"""Derived integrity index with quick/full checks and atomic repair."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .backup import known_schema_error
from .paths import AppPaths
from .persistence import replace_text_atomically


INTEGRITY_INDEX_SCHEMA_VERSION = 1
_HASH_CHUNK = 1024 * 1024


@dataclass(frozen=True, slots=True)
class IntegrityEntry:
    path: str
    kind: str
    game_scope: str | None
    size: int
    mtime_ns: int
    schema_version: int | None
    sha256: str


@dataclass(frozen=True, slots=True)
class IntegrityIssue:
    code: str
    path: str
    message: str
    severity: str = "error"


@dataclass(frozen=True, slots=True)
class IntegrityCheck:
    mode: str
    entries: tuple[IntegrityEntry, ...]
    issues: tuple[IntegrityIssue, ...]

    @property
    def is_clean(self) -> bool:
        return not self.issues


@dataclass(frozen=True, slots=True)
class IntegrityRepairPreview:
    entries: tuple[IntegrityEntry, ...]
    issues: tuple[IntegrityIssue, ...]
    added: int
    removed: int
    changed: int
    content: str


def _roots(paths: AppPaths) -> tuple[tuple[str, Path], ...]:
    return (
        ("config", paths.config_dir),
        ("data/snapshots", paths.snapshot_dir),
        ("data/manifests", paths.manifest_dir),
        ("data/group-revisions", paths.group_revision_dir),
        ("data/tournament-pairings", paths.tournament_pairing_dir),
        ("data/game-metadata", paths.game_metadata_dir),
        ("data/fixtures", paths.fixture_dir),
        ("data/hall-of-fame", paths.hall_of_fame_dir),
        ("data/imports", paths.import_dir),
    )


def canonical_local_files(paths: AppPaths) -> tuple[tuple[str, Path], ...]:
    result: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for prefix, root in _roots(paths):
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            resolved = path.resolve()
            if resolved in seen or resolved == paths.integrity_index_file.resolve():
                continue
            seen.add(resolved)
            result.append((f"{prefix}/{path.relative_to(root).as_posix()}", resolved))
    result.sort(key=lambda item: item[0])
    return tuple(result)


def _kind(relative: str) -> str:
    if relative.startswith("config/"):
        return "configuration"
    if relative.startswith("data/snapshots/"):
        return "snapshot"
    if relative.startswith("data/manifests/"):
        return "manifest"
    if relative.startswith("data/group-revisions/"):
        return "revision"
    if relative.startswith("data/imports/"):
        return "import"
    if relative.startswith("data/"):
        return "canonical_data"
    return "other"


def _game_scope(relative: str) -> str | None:
    parts = relative.split("/")
    if len(parts) >= 4 and parts[0] == "data" and parts[1] in {
        "snapshots",
        "manifests",
        "game-metadata",
        "fixtures",
    }:
        return parts[2]
    return None


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            hasher.update(chunk)
    return hasher.hexdigest()


def _schema(relative: str, path: Path) -> tuple[int | None, str | None]:
    if path.suffix.casefold() != ".json":
        return None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"Ugyldig JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "JSON-roden skal være et objekt"
    version = payload.get("schema_version")
    parsed_version = version if isinstance(version, int) and not isinstance(version, bool) else None
    return parsed_version, known_schema_error(relative, json.dumps(payload).encode("utf-8"))


def _entry(relative: str, path: Path) -> tuple[IntegrityEntry, IntegrityIssue | None]:
    stat = path.stat()
    schema_version, schema_problem = _schema(relative, path)
    entry = IntegrityEntry(
        relative,
        _kind(relative),
        _game_scope(relative),
        stat.st_size,
        stat.st_mtime_ns,
        schema_version,
        _digest(path),
    )
    issue = (
        IntegrityIssue("schema", relative, schema_problem)
        if schema_problem is not None
        else None
    )
    return entry, issue


def _index_dict(entries: tuple[IntegrityEntry, ...]) -> dict[str, object]:
    return {
        "schema_version": INTEGRITY_INDEX_SCHEMA_VERSION,
        "files": [
            {
                "path": entry.path,
                "type": entry.kind,
                "game_scope": entry.game_scope,
                "size": entry.size,
                "mtime_ns": entry.mtime_ns,
                "schema_version": entry.schema_version,
                "sha256": entry.sha256,
            }
            for entry in entries
        ],
    }


def load_integrity_index(path: Path | str) -> tuple[IntegrityEntry, ...]:
    selected = Path(path)
    payload = json.loads(selected.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != INTEGRITY_INDEX_SCHEMA_VERSION:
        raise ValueError("Ukendt integritetsindeksschema")
    files = payload.get("files")
    if not isinstance(files, list):
        raise ValueError("Integritetsindekset mangler fillisten")
    entries: list[IntegrityEntry] = []
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Integritetsindekset har en ugyldig filpost")
        entry = IntegrityEntry(
            str(item["path"]),
            str(item["type"]),
            None if item.get("game_scope") is None else str(item["game_scope"]),
            int(item["size"]),
            int(item["mtime_ns"]),
            None if item.get("schema_version") is None else int(item["schema_version"]),
            str(item["sha256"]),
        )
        if entry.path.startswith(("/", "\\")) or ".." in Path(entry.path).parts:
            raise ValueError("Integritetsindekset har en usikker sti")
        entries.append(entry)
    if len({entry.path for entry in entries}) != len(entries):
        raise ValueError("Integritetsindekset har duplikerede stier")
    return tuple(sorted(entries, key=lambda entry: entry.path))


def quick_integrity_check(paths: AppPaths) -> IntegrityCheck:
    issues: list[IntegrityIssue] = []
    try:
        indexed = load_integrity_index(paths.integrity_index_file)
    except FileNotFoundError:
        indexed = ()
        issues.append(
            IntegrityIssue(
                "index_missing",
                "integrity-index.json",
                "Integritetsindekset mangler. Vis og godkend en indeksreparation.",
                "warning",
            )
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        indexed = ()
        issues.append(
            IntegrityIssue(
                "index_invalid",
                "integrity-index.json",
                f"Integritetsindekset kan ikke læses: {exc}",
            )
        )
    expected = {entry.path: entry for entry in indexed}
    actual = dict(canonical_local_files(paths))
    for relative, entry in expected.items():
        path = actual.get(relative)
        if path is None:
            issues.append(IntegrityIssue("missing", relative, "Filen mangler"))
            continue
        try:
            stat = path.stat()
        except OSError as exc:
            issues.append(IntegrityIssue("unreadable", relative, f"Filen kan ikke læses: {exc}"))
            continue
        if stat.st_size != entry.size or stat.st_mtime_ns != entry.mtime_ns:
            issues.append(
                IntegrityIssue(
                    "stale",
                    relative,
                    "Størrelse eller ændringstid matcher ikke indekset",
                )
            )
        _, schema_problem = _schema(relative, path)
        if schema_problem:
            issues.append(IntegrityIssue("schema", relative, schema_problem))
    for relative in sorted(set(actual) - set(expected)):
        issues.append(IntegrityIssue("extra", relative, "Filen findes ikke i indekset", "warning"))
    return IntegrityCheck("quick", indexed, tuple(issues))


def full_integrity_check(paths: AppPaths) -> IntegrityCheck:
    issues: list[IntegrityIssue] = []
    entries: list[IntegrityEntry] = []
    for relative, path in canonical_local_files(paths):
        try:
            entry, schema_issue = _entry(relative, path)
        except OSError as exc:
            issues.append(IntegrityIssue("unreadable", relative, f"Filen kan ikke læses: {exc}"))
            continue
        entries.append(entry)
        if schema_issue is not None:
            issues.append(schema_issue)
    try:
        indexed = {entry.path: entry for entry in load_integrity_index(paths.integrity_index_file)}
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        indexed = {}
    actual = {entry.path: entry for entry in entries}
    for relative in sorted(set(indexed) - set(actual)):
        issues.append(IntegrityIssue("missing", relative, "Indekseret fil mangler"))
    for relative in sorted(set(actual) - set(indexed)):
        issues.append(IntegrityIssue("extra", relative, "Filen findes ikke i indekset", "warning"))
    for relative in sorted(set(actual).intersection(indexed)):
        if actual[relative].sha256 != indexed[relative].sha256:
            issues.append(IntegrityIssue("checksum", relative, "SHA-256 matcher ikke indekset"))
    return IntegrityCheck("full", tuple(entries), tuple(issues))


def preview_integrity_repair(paths: AppPaths) -> IntegrityRepairPreview:
    check = full_integrity_check(paths)
    try:
        old = {entry.path: entry for entry in load_integrity_index(paths.integrity_index_file)}
    except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        old = {}
    new = {entry.path: entry for entry in check.entries}
    added = len(set(new) - set(old))
    removed = len(set(old) - set(new))
    changed = sum(old[path] != new[path] for path in set(old).intersection(new))
    content = json.dumps(_index_dict(check.entries), ensure_ascii=False, indent=2) + "\n"
    return IntegrityRepairPreview(check.entries, check.issues, added, removed, changed, content)


def repair_integrity_index(
    paths: AppPaths, preview: IntegrityRepairPreview
) -> Path:
    """Atomically replace only the derived index after rechecking the preview."""

    current = preview_integrity_repair(paths)
    if current.content != preview.content:
        raise ValueError("Filerne har ændret sig siden forhåndsvisningen")
    replace_text_atomically(paths.integrity_index_file, preview.content)
    return paths.integrity_index_file.resolve()
