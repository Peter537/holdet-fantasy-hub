"""Pure, read-only projections for the loopback HTTP API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Callable, Mapping

from .groups import GroupStore
from .hall_of_fame import HallOfFameStore
from .hub_settings import HubSettingsStore, effective_manager_profiles
from .maintenance import build_storage_inventory
from .paths import AppPaths
from .seasons import SeasonStore, build_season_standings
from .standings import build_standings
from .storage import PlayerStatisticsStore, SnapshotStore


LOCAL_API_VERSION = "v1"
LOCAL_API_DATASETS = (
    "games",
    "rounds",
    "players",
    "teams",
    "team_history",
    "groups",
    "group_standings",
    "managers",
    "seasons",
    "season_standings",
    "storage_usage",
)


class ApiQueryError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class DatasetDefinition:
    name: str
    columns: tuple[str, ...]
    filters: tuple[str, ...]
    required_filters: tuple[str, ...] = ()


_DEFINITIONS = (
    DatasetDefinition("games", ("locale", "game", "name", "archived"), ("locale", "game")),
    DatasetDefinition(
        "rounds",
        ("locale", "game", "round", "status", "player_snapshots", "team_snapshots", "latest_at"),
        ("locale", "game", "round"),
    ),
    DatasetDefinition(
        "players",
        ("locale", "game", "round", "source_index", "entry_id", "person_id", "name", "team", "position", "value", "total_growth", "round_growth", "statuses", "snapshot_at"),
        ("locale", "game", "round"),
        ("game",),
    ),
    DatasetDefinition(
        "teams",
        ("locale", "game", "team_id", "team_name", "manager_name", "round", "unit", "rank", "total", "change", "snapshot_at"),
        ("locale", "game", "team_id", "round"),
    ),
    DatasetDefinition(
        "team_history",
        ("locale", "game", "team_id", "team_name", "manager_name", "round", "status", "total", "change", "overall_rank", "round_rank", "snapshot_at"),
        ("locale", "game", "team_id", "round"),
    ),
    DatasetDefinition(
        "groups",
        ("locale", "game", "group_id", "group_name", "type", "members", "active_revision"),
        ("locale", "game", "group"),
    ),
    DatasetDefinition(
        "group_standings",
        ("locale", "game", "group_id", "round", "rank", "team_id", "team_name", "manager_name", "total", "change", "distance", "status"),
        ("locale", "game", "group", "round"),
        ("group",),
    ),
    DatasetDefinition(
        "managers",
        ("manager_id", "manager_name", "identity_count"),
        (),
    ),
    DatasetDefinition(
        "seasons",
        ("season_id", "season_name", "competitions", "archived"),
        ("season",),
    ),
    DatasetDefinition(
        "season_standings",
        ("season_id", "season_name", "rank", "manager_id", "manager_name", "points", "titles", "podiums", "competitions", "round_wins"),
        ("season",),
        ("season",),
    ),
    DatasetDefinition(
        "storage_usage",
        ("game_scope", "category", "files", "bytes"),
        ("game",),
    ),
)
_BY_NAME = {definition.name: definition for definition in _DEFINITIONS}


def dataset_catalog() -> dict[str, object]:
    return {
        "api_version": LOCAL_API_VERSION,
        "datasets": [
            {
                "name": definition.name,
                "columns": list(definition.columns),
                "filters": list(definition.filters),
                "required_filters": list(definition.required_filters),
                "formats": ["json", "csv"],
                "max_limit": 5000,
            }
            for definition in _DEFINITIONS
        ],
    }


def dataset_definition(name: str) -> DatasetDefinition:
    try:
        return _BY_NAME[name]
    except KeyError as exc:
        raise ApiQueryError("Ukendt datasæt") from exc


def _matches(row: Mapping[str, object], filters: Mapping[str, str]) -> bool:
    mapping = {
        "group": "group_id",
        "season": "season_id",
    }
    for key, expected in filters.items():
        column = mapping.get(key, key)
        if key == "game" and "game" not in row and "game_scope" in row:
            column = "game_scope"
        value = row.get(column)
        if key in {"round", "team_id"}:
            try:
                numeric = int(expected)
            except ValueError as exc:
                raise ApiQueryError(f"Filteret {key} skal være et heltal") from exc
            if value != numeric:
                return False
        elif str(value).casefold() != expected.casefold():
            return False
    return True


def _sort_value(value: object) -> tuple[int, object]:
    if value is None:
        return (1, "")
    if isinstance(value, str):
        return (0, value.casefold())
    return (0, value)


class LocalDataApi:
    """Build deterministic rows exclusively from local stores."""

    def __init__(self, paths: AppPaths) -> None:
        self.paths = paths

    def _configuration(self):
        return GroupStore(
            self.paths.groups_file, self.paths.group_revision_dir
        ).load_configuration()

    def _snapshot_indexes(self):
        return (
            PlayerStatisticsStore(self.paths.snapshot_dir).scan(),
            SnapshotStore(self.paths.snapshot_dir).scan(),
        )

    def rows(self, dataset: str, filters: Mapping[str, str]) -> tuple[dict[str, object], ...]:
        definition = _BY_NAME.get(dataset)
        if definition is None:
            raise ApiQueryError("Ukendt datasæt")
        unsupported = set(filters) - set(definition.filters)
        if unsupported:
            raise ApiQueryError(
                "Ikke-understøttede filtre: " + ", ".join(sorted(unsupported))
            )
        missing = [name for name in definition.required_filters if not filters.get(name)]
        if missing:
            raise ApiQueryError("Påkrævede filtre mangler: " + ", ".join(missing))
        builder: Callable[[], list[dict[str, object]]] = getattr(self, f"_{dataset}")
        rows = [row for row in builder() if _matches(row, filters)]
        rows.sort(
            key=lambda row: tuple(_sort_value(row.get(column)) for column in definition.columns)
        )
        return tuple(rows)

    def _games(self) -> list[dict[str, object]]:
        return [
            {
                "locale": item.game.locale,
                "game": item.game.slug,
                "name": item.name,
                "archived": item.is_archived,
            }
            for item in self._configuration().games
        ]

    def _rounds(self) -> list[dict[str, object]]:
        players, teams = self._snapshot_indexes()
        grouped: dict[tuple[str, str, int], dict[str, object]] = {}
        times: dict[tuple[str, str, int], list[datetime]] = {}
        statuses: dict[tuple[str, str, int], set[str]] = {}
        for snapshot in players.snapshots:
            game = snapshot.statistics.game
            key = (game.locale, game.slug, snapshot.statistics.round_number)
            row = grouped.setdefault(
                key,
                {
                    "locale": game.locale,
                    "game": game.slug,
                    "round": snapshot.statistics.round_number,
                    "status": "unknown",
                    "player_snapshots": 0,
                    "team_snapshots": 0,
                    "latest_at": None,
                },
            )
            row["player_snapshots"] = int(row["player_snapshots"]) + 1
            times.setdefault(key, []).append(snapshot.generated_at)
            statuses.setdefault(key, set()).add(snapshot.statistics.round_status)
        for snapshot in teams.snapshots:
            team = snapshot.team
            game = team.reference.game
            for summary in team.history:
                key = (game.locale, game.slug, summary.round_number)
                row = grouped.setdefault(
                    key,
                    {
                        "locale": game.locale,
                        "game": game.slug,
                        "round": summary.round_number,
                        "status": "unknown",
                        "player_snapshots": 0,
                        "team_snapshots": 0,
                        "latest_at": None,
                    },
                )
                row["team_snapshots"] = int(row["team_snapshots"]) + 1
                times.setdefault(key, []).append(snapshot.generated_at)
                statuses.setdefault(key, set()).add(summary.round_status)
        for key, row in grouped.items():
            values = statuses.get(key, {"unknown"})
            row["status"] = "complete" if values == {"complete"} else (
                "in_progress" if "in_progress" in values else "unknown"
            )
            row["latest_at"] = max(times[key]).isoformat()
        return list(grouped.values())

    def _players(self) -> list[dict[str, object]]:
        index = PlayerStatisticsStore(self.paths.snapshot_dir).scan()
        latest: dict[tuple[str, str, int], object] = {}
        for snapshot in index.snapshots:
            latest.setdefault(snapshot.identity, snapshot)
        result: list[dict[str, object]] = []
        for snapshot in latest.values():
            statistics = snapshot.statistics  # type: ignore[attr-defined]
            for entry in statistics.entries:
                statuses = []
                if not entry.is_active:
                    statuses.append("inactive")
                if entry.is_disabled:
                    statuses.append("disabled")
                if entry.is_injured:
                    statuses.append("injured")
                if entry.has_suspension:
                    statuses.append("suspended")
                result.append(
                    {
                        "locale": statistics.game.locale,
                        "game": statistics.game.slug,
                        "round": statistics.round_number,
                        "source_index": entry.source_index,
                        "entry_id": entry.entry_id,
                        "person_id": entry.person_id,
                        "name": entry.name,
                        "team": entry.team,
                        "position": entry.position,
                        "value": entry.value,
                        "total_growth": entry.total_growth,
                        "round_growth": entry.round_growth,
                        "statuses": ";".join(statuses),
                        "snapshot_at": snapshot.generated_at.isoformat(),  # type: ignore[attr-defined]
                    }
                )
        return result

    def _latest_teams(self):
        index = SnapshotStore(self.paths.snapshot_dir).scan()
        latest: dict[tuple[str, str, int], object] = {}
        for snapshot in index.snapshots:
            latest.setdefault(snapshot.identity, snapshot)
        return tuple(latest.values())

    def _teams(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for snapshot in self._latest_teams():
            team = snapshot.team  # type: ignore[attr-defined]
            overview = team.overview
            result.append(
                {
                    "locale": team.reference.game.locale,
                    "game": team.reference.game.slug,
                    "team_id": team.reference.team_id,
                    "team_name": team.team_name,
                    "manager_name": team.owner_name,
                    "round": overview.current_round,
                    "unit": overview.unit,
                    "rank": overview.rank,
                    "total": overview.total,
                    "change": overview.current_change,
                    "snapshot_at": snapshot.generated_at.isoformat(),  # type: ignore[attr-defined]
                }
            )
        return result

    def _team_history(self) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        for snapshot in self._latest_teams():
            team = snapshot.team  # type: ignore[attr-defined]
            for summary in team.history:
                result.append(
                    {
                        "locale": team.reference.game.locale,
                        "game": team.reference.game.slug,
                        "team_id": team.reference.team_id,
                        "team_name": team.team_name,
                        "manager_name": team.owner_name,
                        "round": summary.round_number,
                        "status": summary.round_status,
                        "total": summary.total,
                        "change": summary.change,
                        "overall_rank": summary.overall_rank,
                        "round_rank": summary.round_rank,
                        "snapshot_at": snapshot.generated_at.isoformat(),  # type: ignore[attr-defined]
                    }
                )
        return result

    def _groups(self) -> list[dict[str, object]]:
        return [
            {
                "locale": group.game.locale,
                "game": group.game.slug,
                "group_id": group.group_id,
                "group_name": group.name,
                "type": group.kind,
                "members": len(group.teams),
                "active_revision": group.active_revision,
            }
            for group in self._configuration().groups
        ]

    def _group_standings(self) -> list[dict[str, object]]:
        configuration = self._configuration()
        index = SnapshotStore(self.paths.snapshot_dir).scan()
        result: list[dict[str, object]] = []
        for group in configuration.groups:
            rounds = [
                summary.round_number
                for member in group.teams
                for snapshot in index.for_team(group.game, member.team_id)
                for summary in snapshot.team.history
            ]
            for round_number in sorted(set(rounds)):
                for row in build_standings(group, index, round_number, "overall"):
                    result.append(
                        {
                            "locale": group.game.locale,
                            "game": group.game.slug,
                            "group_id": group.group_id,
                            "round": round_number,
                            "rank": row.rank,
                            "team_id": row.team_id,
                            "team_name": row.team_name,
                            "manager_name": row.owner_name,
                            "total": row.total,
                            "change": row.change,
                            "distance": row.distance,
                            "status": "missing" if row.summary is None else "ready",
                        }
                    )
        return result

    def _managers(self) -> list[dict[str, object]]:
        settings = HubSettingsStore(self.paths.hub_settings_file).load()
        return [
            {
                "manager_id": profile.manager_id,
                "manager_name": profile.display_name,
                "identity_count": len(profile.identity_keys),
            }
            for profile in effective_manager_profiles(settings)
        ]

    def _seasons(self) -> list[dict[str, object]]:
        return [
            {
                "season_id": season.season_id,
                "season_name": season.name,
                "competitions": len(season.competition_ids),
                "archived": season.is_archived,
            }
            for season in SeasonStore(self.paths.seasons_file).load()
        ]

    def _season_standings(self) -> list[dict[str, object]]:
        seasons = SeasonStore(self.paths.seasons_file).load()
        events, _ = HallOfFameStore(self.paths.hall_of_fame_dir).scan()
        settings = HubSettingsStore(self.paths.hub_settings_file).load()
        result: list[dict[str, object]] = []
        for season in seasons:
            for row in build_season_standings(
                season, events, settings.hall_of_fame_score
            ):
                result.append(
                    {
                        "season_id": season.season_id,
                        "season_name": season.name,
                        "rank": row.rank,
                        "manager_id": row.manager_id,
                        "manager_name": row.manager_name,
                        "points": row.points,
                        "titles": row.titles,
                        "podiums": row.podiums,
                        "competitions": row.competitions,
                        "round_wins": row.round_wins,
                    }
                )
        return result

    def _storage_usage(self) -> list[dict[str, object]]:
        return [
            {
                "game_scope": row.game_scope,
                "category": row.category,
                "files": row.files,
                "bytes": row.bytes,
            }
            for row in build_storage_inventory(self.paths).rows
        ]

    def last_modified(self) -> datetime | None:
        mtimes = [
            path.stat().st_mtime
            for root in (self.paths.config_dir, self.paths.data_dir, self.paths.export_dir)
            if root.exists()
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        ]
        return datetime.fromtimestamp(max(mtimes)).astimezone() if mtimes else None


@dataclass(frozen=True, slots=True)
class RegisteredArtifact:
    artifact_id: str
    relative_path: str
    size: int
    sha256: str


def _artifact_digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def register_artifact(paths: AppPaths, artifact: Path | str) -> RegisteredArtifact:
    """Register one existing export for path-free read-only download lookup."""

    from .persistence import replace_text_atomically

    path = Path(artifact).resolve()
    root = paths.export_dir.resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
        raise ValueError("Kun almindelige filer i eksportlageret kan registreres")
    relative = path.relative_to(root).as_posix()
    before = path.stat()
    digest = _artifact_digest(path)
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise OSError("Artifactet ændrede sig under registreringen")
    artifact_id = hashlib.sha256(
        f"{relative}|{before.st_size}|{digest}".encode("utf-8")
    ).hexdigest()[:24]
    try:
        payload = json.loads(paths.artifact_registry_file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        payload = {"schema_version": 1, "artifacts": []}
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("Artifactregisteret har et ukendt schema")
    records = payload.get("artifacts")
    if not isinstance(records, list):
        raise ValueError("Artifactregisteret er ugyldigt")
    record = {
        "id": artifact_id,
        "path": relative,
        "size": before.st_size,
        "sha256": digest,
    }
    retained = [item for item in records if isinstance(item, dict) and item.get("id") != artifact_id]
    replace_text_atomically(
        paths.artifact_registry_file,
        json.dumps(
            {"schema_version": 1, "artifacts": [*retained, record]},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )
    return RegisteredArtifact(artifact_id, relative, before.st_size, digest)


def resolve_registered_artifact(paths: AppPaths, artifact_id: str) -> Path | None:
    if len(artifact_id) != 24 or not all(character in "0123456789abcdef" for character in artifact_id):
        return None
    try:
        payload = json.loads(paths.artifact_registry_file.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    records = payload.get("artifacts") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        return None
    record = next(
        (item for item in records if isinstance(item, dict) and item.get("id") == artifact_id),
        None,
    )
    if record is None or not isinstance(record.get("path"), str):
        return None
    root = paths.export_dir.resolve()
    path = (root / record["path"]).resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
        return None
    try:
        if (
            path.stat().st_size != record.get("size")
            or _artifact_digest(path) != record.get("sha256")
        ):
            return None
    except OSError:
        return None
    return path
