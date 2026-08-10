"""Explicit immutable snapshot storage and cached snapshot indexing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from .errors import PayloadError
from .filenames import collision_suffix, round_output_stem
from .models import GameUrl, RoundSummary, ScrapedGame, ScrapedTeam
from .output import sanitize_path_component
from .persistence import aware_local, publish_immutable_text
from .player_serialization import (
    player_statistics_from_dict,
    player_statistics_to_json,
)
from .serialization import team_from_dict, team_to_json


@dataclass(frozen=True, slots=True)
class TeamSnapshot:
    path: Path
    generated_at: datetime
    team: ScrapedTeam

    @property
    def identity(self) -> tuple[str, str, int]:
        game = self.team.reference.game
        return game.locale.casefold(), game.slug, self.team.reference.team_id


GameSelector = GameUrl | tuple[str, str] | str


def _game_key(game: GameSelector) -> tuple[str | None, str]:
    if isinstance(game, GameUrl):
        return game.locale.casefold(), game.slug
    if isinstance(game, tuple):
        return game[0].casefold(), game[1]
    return None, game


@dataclass(frozen=True, slots=True)
class SnapshotIndex:
    snapshots: tuple[TeamSnapshot, ...]
    warnings: tuple[str, ...] = ()
    _by_team: dict[tuple[str, str, int], tuple[TeamSnapshot, ...]] = field(
        init=False, repr=False, compare=False
    )
    _summaries: dict[
        tuple[str, str, int, int], tuple[TeamSnapshot, RoundSummary]
    ] = field(init=False, repr=False, compare=False)
    _rosters: dict[tuple[str, str, int, int], TeamSnapshot] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        grouped: dict[tuple[str, str, int], list[TeamSnapshot]] = {}
        for snapshot in self.snapshots:
            grouped.setdefault(snapshot.identity, []).append(snapshot)
        by_team = {
            identity: tuple(
                sorted(values, key=lambda item: item.generated_at, reverse=True)
            )
            for identity, values in grouped.items()
        }
        summaries: dict[
            tuple[str, str, int, int], tuple[TeamSnapshot, RoundSummary]
        ] = {}
        rosters: dict[tuple[str, str, int, int], TeamSnapshot] = {}
        for identity, values in by_team.items():
            for snapshot in values:
                for summary in snapshot.team.history:
                    summaries.setdefault((*identity, summary.round_number), (snapshot, summary))
                rosters.setdefault(
                    (*identity, snapshot.team.overview.current_round), snapshot
                )
        object.__setattr__(self, "_by_team", by_team)
        object.__setattr__(self, "_summaries", summaries)
        object.__setattr__(self, "_rosters", rosters)

    @property
    def identities(self) -> tuple[tuple[str, str, int], ...]:
        return tuple(sorted(self._by_team))

    def _matching_identities(
        self, game: GameSelector, team_id: int
    ) -> tuple[tuple[str, str, int], ...]:
        locale, slug = _game_key(game)
        if locale is not None:
            return ((locale, slug, team_id),)
        return tuple(
            identity
            for identity in self._by_team
            if identity[1:] == (slug, team_id)
        )

    def for_team(
        self, game: GameSelector, team_id: int
    ) -> tuple[TeamSnapshot, ...]:
        values = [
            snapshot
            for identity in self._matching_identities(game, team_id)
            for snapshot in self._by_team.get(identity, ())
        ]
        return tuple(
            sorted(values, key=lambda item: item.generated_at, reverse=True)
        )

    def newest(self, game: GameSelector, team_id: int) -> TeamSnapshot | None:
        matches = self.for_team(game, team_id)
        return matches[0] if matches else None

    def summary_for(
        self, game: GameSelector, team_id: int, round_number: int
    ) -> tuple[TeamSnapshot, RoundSummary] | None:
        for identity in self._matching_identities(game, team_id):
            if located := self._summaries.get((*identity, round_number)):
                return located
        return None

    def roster_for(
        self, game: GameSelector, team_id: int, round_number: int
    ) -> TeamSnapshot | None:
        for identity in self._matching_identities(game, team_id):
            if snapshot := self._rosters.get((*identity, round_number)):
                return snapshot
        return None

    def rounds_for(
        self, game: GameSelector, team_ids: tuple[int, ...]
    ) -> tuple[int, ...]:
        locale, slug = _game_key(game)
        wanted = set(team_ids)
        return tuple(
            sorted(
                {
                    key[3]
                    for key in self._summaries
                    if key[1] == slug
                    and (locale is None or key[0] == locale)
                    and key[2] in wanted
                },
                reverse=True,
            )
        )


@dataclass(frozen=True, slots=True)
class PlayerStatisticsSnapshot:
    path: Path
    generated_at: datetime
    statistics: ScrapedGame

    @property
    def identity(self) -> tuple[str, str, int]:
        return (
            self.statistics.game.locale.casefold(),
            self.statistics.game.slug,
            self.statistics.round_number,
        )


@dataclass(frozen=True, slots=True)
class PlayerStatisticsIndex:
    snapshots: tuple[PlayerStatisticsSnapshot, ...]
    warnings: tuple[str, ...] = ()
    _by_game: dict[tuple[str, str], tuple[PlayerStatisticsSnapshot, ...]] = field(
        init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        grouped: dict[tuple[str, str], list[PlayerStatisticsSnapshot]] = {}
        for snapshot in self.snapshots:
            grouped.setdefault(snapshot.identity[:2], []).append(snapshot)
        object.__setattr__(
            self,
            "_by_game",
            {
                identity: tuple(
                    sorted(values, key=lambda item: item.generated_at, reverse=True)
                )
                for identity, values in grouped.items()
            },
        )

    def for_game(self, game: GameUrl) -> tuple[PlayerStatisticsSnapshot, ...]:
        return self._by_game.get((game.locale.casefold(), game.slug), ())

    def rounds_for(self, game: GameUrl) -> tuple[int, ...]:
        return tuple(
            sorted(
                {snapshot.statistics.round_number for snapshot in self.for_game(game)},
                reverse=True,
            )
        )

    def newest(
        self, game: GameUrl, round_number: int | None = None
    ) -> PlayerStatisticsSnapshot | None:
        for snapshot in self.for_game(game):
            if round_number is None or snapshot.statistics.round_number == round_number:
                return snapshot
        return None

def _team_directory(output_dir: Path, team: ScrapedTeam) -> Path:
    account_component = (
        team.reference.account_key
        if team.reference.account_key != "direct"
        else sanitize_path_component(team.owner_name, fallback="direct")
    )
    team_component = sanitize_path_component(
        team.team_name, fallback=f"team-{team.reference.team_id}"
    )
    return (
        output_dir
        / team.reference.game.slug
        / "teams"
        / sanitize_path_component(account_component, fallback="account")
        / f"{team_component}-{team.reference.team_id}"
    )


class SnapshotStore:
    """Read and explicitly publish immutable schema-versioned snapshots."""

    def __init__(self, output_dir: Path | str) -> None:
        self.output_dir = Path(output_dir)

    def scan(self) -> SnapshotIndex:
        snapshots: list[TeamSnapshot] = []
        warnings: list[str] = []
        if not self.output_dir.exists():
            return SnapshotIndex((), ())
        for path in self.output_dir.rglob("team-round*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise PayloadError("Snapshottets rod skal være et objekt")
                generated_raw = payload.get("generated_at")
                if not isinstance(generated_raw, str):
                    raise PayloadError("Snapshottets generated_at skal være tekst")
                generated_at = datetime.fromisoformat(generated_raw)
                if generated_at.tzinfo is None:
                    generated_at = generated_at.astimezone()
                snapshots.append(
                    TeamSnapshot(path.resolve(), generated_at, team_from_dict(payload))
                )
            except (OSError, ValueError, json.JSONDecodeError, PayloadError) as exc:
                warnings.append(f"{path}: {exc}")
        snapshots.sort(key=lambda item: item.generated_at, reverse=True)
        return SnapshotIndex(tuple(snapshots), tuple(warnings))

    def save_team_json(
        self, team: ScrapedTeam, *, now: datetime | None = None
    ) -> Path:
        generated_at = aware_local(now)
        target_dir = _team_directory(self.output_dir, team)
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = round_output_stem("team", team.overview.current_round, generated_at)
        content = team_to_json(team, generated_at=generated_at)
        collision_number = 0
        while True:
            suffix = collision_suffix(collision_number)
            candidate = target_dir / f"{stem}{suffix}.json"
            paired_text = target_dir / f"{stem}{suffix}.txt"
            if candidate.exists() or paired_text.exists():
                collision_number += 1
                continue
            try:
                publish_immutable_text(candidate, content)
            except FileExistsError:
                collision_number += 1
                continue
            return candidate


class PlayerStatisticsStore:
    """Read and explicitly publish immutable all-player statistics snapshots."""

    def __init__(self, snapshot_dir: Path | str) -> None:
        self.snapshot_dir = Path(snapshot_dir)

    def _game_directory(self, game: GameUrl) -> Path:
        return (
            self.snapshot_dir
            / sanitize_path_component(game.slug, fallback="game")
            / "players"
        )

    def scan(self, game: GameUrl | None = None) -> PlayerStatisticsIndex:
        snapshots: list[PlayerStatisticsSnapshot] = []
        warnings: list[str] = []
        root = self._game_directory(game) if game is not None else self.snapshot_dir
        if not root.exists():
            return PlayerStatisticsIndex((), ())
        paths = root.glob("player-round*.json") if game is not None else root.rglob("player-round*.json")
        for path in paths:
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict):
                    raise PayloadError("Spillersnapshottets rod skal være et objekt")
                generated_raw = payload.get("generated_at")
                if not isinstance(generated_raw, str):
                    raise PayloadError("Spillersnapshottets generated_at skal være tekst")
                generated_at = datetime.fromisoformat(generated_raw)
                if generated_at.tzinfo is None:
                    generated_at = generated_at.astimezone()
                statistics = player_statistics_from_dict(payload)
                if game is not None and (
                    statistics.game.locale.casefold(), statistics.game.slug
                ) != (game.locale.casefold(), game.slug):
                    raise PayloadError("Spillersnapshottet tilhører et andet spil")
                snapshots.append(
                    PlayerStatisticsSnapshot(
                        path.resolve(), generated_at, statistics
                    )
                )
            except (OSError, ValueError, json.JSONDecodeError, PayloadError) as exc:
                warnings.append(f"{path}: {exc}")
        snapshots.sort(
            key=lambda item: (item.generated_at, item.path.name), reverse=True
        )
        return PlayerStatisticsIndex(tuple(snapshots), tuple(warnings))

    def save(
        self, statistics: ScrapedGame, *, now: datetime | None = None
    ) -> Path:
        if statistics.round_number < 0:
            raise PayloadError("Spillerstatistikkens runde må ikke være negativ")
        if not statistics.entries:
            raise PayloadError("Tom spillerstatistik kan ikke gemmes")
        generated_at = aware_local(now)
        target_dir = self._game_directory(statistics.game)
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = round_output_stem(
            "player", statistics.round_number, generated_at
        )
        content = player_statistics_to_json(
            statistics, generated_at=generated_at
        )
        collision_number = 0
        while True:
            suffix = collision_suffix(collision_number)
            candidate = target_dir / f"{stem}{suffix}.json"
            if candidate.exists():
                collision_number += 1
                continue
            try:
                publish_immutable_text(candidate, content)
            except FileExistsError:
                collision_number += 1
                continue
            return candidate


ManifestScope = Literal["game", "group"]
ManifestStepSource = Literal["metadata", "players", "team", "postprocess"]
ManifestStepStatus = Literal[
    "fetched",
    "reused_current",
    "reused_after_error",
    "failed_no_cache",
    "skipped_unavailable",
    "not_recorded",
]


@dataclass(frozen=True, slots=True)
class RefreshMetadataChange:
    """One structured schedule/rule/metadata change recorded by a refresh."""

    path: str
    kind: Literal["added", "removed", "changed"]
    before: object = None
    after: object = None


@dataclass(frozen=True, slots=True)
class RefreshManifestStep:
    """One normalized source outcome from a refresh manifest."""

    step_id: str
    source: ManifestStepSource
    label: str
    status: ManifestStepStatus
    attempted: bool
    reason: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    team_id: int | None = None
    team_name: str | None = None
    round_number: int | None = None
    data_reference: str | None = None
    cache_reference: str | None = None
    cache_generated_at: datetime | None = None
    error: str | None = None
    origin_run_id: str | None = None

    @property
    def reused_cache(self) -> bool:
        return self.status in {"reused_current", "reused_after_error"}

    @property
    def retryable(self) -> bool:
        return self.status in {"reused_after_error", "failed_no_cache"}


@dataclass(frozen=True, slots=True)
class RefreshManifest:
    """Typed, dual-read projection of schema-1 and schema-2 manifests."""

    schema_version: int
    scope: ManifestScope
    run_id: str
    started_at: datetime
    completed_at: datetime
    game_slug: str
    target_round: int
    steps: tuple[RefreshManifestStep, ...]
    mode: str = "all"
    game_locale: str = ""
    game_name: str = ""
    game_url: str = ""
    attempted_team_ids: tuple[int, ...] = ()
    skipped_team_ids: tuple[int, ...] = ()
    groups: tuple[dict[str, object], ...] = ()
    group_id: str | None = None
    retry_of: str | None = None
    origin_run_id: str | None = None
    metadata_changes: tuple[RefreshMetadataChange, ...] = ()
    path: Path | None = field(default=None, compare=False)

    @property
    def generated_at(self) -> datetime:
        """Compatibility alias for readers that used schema-1 generated_at."""

        return self.completed_at

    @property
    def round_number(self) -> int:
        """Compatibility alias for schema-1 round."""

        return self.target_round

    @property
    def failures(self) -> tuple[RefreshManifestStep, ...]:
        return tuple(item for item in self.steps if item.retryable)

    @property
    def cache_reused(self) -> tuple[RefreshManifestStep, ...]:
        return tuple(item for item in self.steps if item.reused_cache)

    @property
    def result(self) -> Literal["complete", "partial", "failed"]:
        """Derive one stable overall outcome from the complete step set."""

        if not self.failures:
            return "complete"
        usable = any(
            item.status in {"fetched", "reused_current", "reused_after_error"}
            for item in self.steps
        )
        return "partial" if usable else "failed"


_MANIFEST_SOURCES = {"metadata", "players", "team", "postprocess"}
_MANIFEST_WIRE_STATUSES = {
    "fetched",
    "reused_current",
    "reused_after_error",
    "failed_no_cache",
    "skipped_unavailable",
}
_MANIFEST_STATUSES = {*_MANIFEST_WIRE_STATUSES, "not_recorded"}


def _manifest_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise PayloadError(f"Manifestet mangler {label}")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PayloadError(f"Manifestet har ugyldigt {label}") from exc
    return parsed.astimezone() if parsed.tzinfo is None else parsed


def _manifest_integer(value: object, label: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise PayloadError(f"Manifestet har ugyldigt {label}")
    return value


def _manifest_text(value: object, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PayloadError(f"Manifestet har ugyldigt {label}")
    return value.strip()


def _manifest_ids(value: object, label: str) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise PayloadError(f"Manifestet mangler {label}")
    result: list[int] = []
    for item in value:
        parsed = _manifest_integer(item, label)
        assert parsed is not None
        result.append(parsed)
    if len(result) != len(set(result)):
        raise PayloadError(f"Manifestet har dubletter i {label}")
    return tuple(result)


def _manifest_run_id(value: object, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    text = _manifest_text(value, label)
    try:
        UUID(str(text))
    except ValueError as exc:
        raise PayloadError(f"Manifestet har ugyldigt {label}") from exc
    return str(text)


def _manifest_reference(value: object, label: str) -> str | None:
    text = _manifest_text(value, label, optional=True)
    if text is None:
        return None
    candidate = Path(text)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise PayloadError(f"Manifestet har usikker {label}")
    return candidate.as_posix()


def _metadata_change_to_dict(change: RefreshMetadataChange) -> dict[str, object]:
    if not change.path.strip():
        raise ValueError("En metadataændring kræver path")
    if change.kind not in {"added", "removed", "changed"}:
        raise ValueError("En metadataændring har ukendt kind")
    return {
        "path": change.path,
        "kind": change.kind,
        "before": change.before,
        "after": change.after,
    }


def _metadata_change_from_dict(raw: object, index: int) -> RefreshMetadataChange:
    if not isinstance(raw, dict):
        raise PayloadError(f"Manifestets metadata_changes[{index}] skal være et objekt")
    path = _manifest_text(raw.get("path"), f"metadata_changes[{index}].path")
    kind = raw.get("kind")
    if kind not in {"added", "removed", "changed"}:
        raise PayloadError(f"Manifestets metadata_changes[{index}] har ukendt kind")
    return RefreshMetadataChange(
        str(path),
        kind,  # type: ignore[arg-type]
        raw.get("before"),
        raw.get("after"),
    )


def _manifest_step_to_dict(step: RefreshManifestStep) -> dict[str, object]:
    if step.status == "not_recorded":
        raise ValueError("not_recorded må ikke skrives i schema 2")
    if step.source not in _MANIFEST_SOURCES:
        raise ValueError(f"Ukendt manifestkilde: {step.source}")
    if step.status not in _MANIFEST_WIRE_STATUSES:
        raise ValueError(f"Ukendt manifeststatus: {step.status}")
    if not step.step_id.strip() or not step.label.strip():
        raise ValueError("Manifeststeps kræver step_id og label")
    if step.round_number is not None and step.round_number < 0:
        raise ValueError("Manifeststeps må ikke have en negativ runde")
    if step.source == "team" and step.team_id is None:
        raise ValueError("Et holdstep kræver team_id")
    if step.team_id is not None and step.team_id < 0:
        raise ValueError("Et holdstep må ikke have et negativt team_id")
    for label, timestamp in (
        ("started_at", step.started_at),
        ("completed_at", step.completed_at),
        ("cache_generated_at", step.cache_generated_at),
    ):
        if timestamp is not None and timestamp.utcoffset() is None:
            raise ValueError(f"Manifeststeppets {label} kræver tidszone")
    if step.started_at is not None and step.completed_at is not None:
        if step.completed_at < step.started_at:
            raise ValueError("Manifeststeppets completed_at ligger før started_at")
    if step.attempted and (step.started_at is None or step.completed_at is None):
        raise ValueError("Forsøgte manifeststeps kræver start- og sluttid")
    if not step.attempted and (
        step.started_at is not None or step.completed_at is not None
    ):
        raise ValueError("Ikke-forsøgte manifeststeps må ikke have kørselstid")
    if step.origin_run_id is not None:
        try:
            _manifest_run_id(step.origin_run_id, "origin_run_id")
        except PayloadError as exc:
            raise ValueError(str(exc)) from exc
    data_reference = _manifest_reference(step.data_reference, "data_reference")
    cache_reference = _manifest_reference(step.cache_reference, "cache_reference")
    if step.status in {"fetched", "reused_after_error", "failed_no_cache"}:
        if not step.attempted:
            raise ValueError(f"Status {step.status} kræver attempted=true")
    elif step.attempted:
        raise ValueError(f"Status {step.status} kræver attempted=false")
    if (
        step.status in {"reused_current", "reused_after_error"}
        and step.source != "postprocess"
        and (cache_reference is None or step.cache_generated_at is None)
    ):
        raise ValueError(f"Status {step.status} kræver cache-reference og tidspunkt")
    if (
        step.status == "reused_current"
        and step.source == "postprocess"
        and step.cache_generated_at is None
    ):
        raise ValueError("Genbrugt efterbehandling kræver cachetidspunkt")
    if (
        step.status == "fetched"
        and step.source != "postprocess"
        and data_reference is None
    ):
        raise ValueError("Et hentet kildestep kræver data_reference")
    return {
        "step_id": step.step_id,
        "source": step.source,
        "label": step.label,
        "status": step.status,
        "attempted": step.attempted,
        "retryable": step.retryable,
        "reason": step.reason,
        "started_at": (
            None if step.started_at is None else step.started_at.isoformat()
        ),
        "completed_at": (
            None if step.completed_at is None else step.completed_at.isoformat()
        ),
        "team_id": step.team_id,
        "team_name": step.team_name,
        "round": step.round_number,
        "data_reference": data_reference,
        "cache_reference": cache_reference,
        "cache_generated_at": (
            None
            if step.cache_generated_at is None
            else step.cache_generated_at.isoformat()
        ),
        "error": step.error,
        "origin_run_id": step.origin_run_id,
    }


def _manifest_step_from_dict(raw: object, index: int) -> RefreshManifestStep:
    if not isinstance(raw, dict):
        raise PayloadError(f"Manifestets steps[{index}] skal være et objekt")
    step_id = _manifest_text(raw.get("step_id"), f"steps[{index}].step_id")
    source = _manifest_text(raw.get("source"), f"steps[{index}].source")
    status = _manifest_text(raw.get("status"), f"steps[{index}].status")
    label = _manifest_text(raw.get("label"), f"steps[{index}].label")
    if source not in _MANIFEST_SOURCES:
        raise PayloadError(f"Manifestets steps[{index}] har ukendt kilde")
    if status not in _MANIFEST_WIRE_STATUSES:
        raise PayloadError(f"Manifestets steps[{index}] har ukendt status")
    attempted = raw.get("attempted")
    if not isinstance(attempted, bool):
        raise PayloadError(f"Manifestets steps[{index}] mangler attempted")
    retryable = raw.get("retryable")
    if retryable is not None and not isinstance(retryable, bool):
        raise PayloadError(
            f"Manifestets steps[{index}] har ugyldigt retryable"
        )
    team_id = _manifest_integer(
        raw.get("team_id"), f"steps[{index}].team_id", optional=True
    )
    round_number = _manifest_integer(
        raw.get("round"), f"steps[{index}].round", optional=True
    )
    parsed = RefreshManifestStep(
        step_id=str(step_id),
        source=source,  # type: ignore[arg-type]
        label=str(label),
        status=status,  # type: ignore[arg-type]
        attempted=attempted,
        reason=_manifest_text(
            raw.get("reason"), f"steps[{index}].reason", optional=True
        ),
        started_at=(
            None
            if raw.get("started_at") is None
            else _manifest_timestamp(
                raw.get("started_at"), f"steps[{index}].started_at"
            )
        ),
        completed_at=(
            None
            if raw.get("completed_at") is None
            else _manifest_timestamp(
                raw.get("completed_at"), f"steps[{index}].completed_at"
            )
        ),
        team_id=team_id,
        team_name=_manifest_text(
            raw.get("team_name"), f"steps[{index}].team_name", optional=True
        ),
        round_number=round_number,
        data_reference=_manifest_reference(
            raw.get("data_reference"), f"steps[{index}].data_reference"
        ),
        cache_reference=_manifest_reference(
            raw.get("cache_reference"), f"steps[{index}].cache_reference"
        ),
        cache_generated_at=(
            None
            if raw.get("cache_generated_at") is None
            else _manifest_timestamp(
                raw.get("cache_generated_at"),
                f"steps[{index}].cache_generated_at",
            )
        ),
        error=_manifest_text(
            raw.get("error"), f"steps[{index}].error", optional=True
        ),
        origin_run_id=_manifest_run_id(
            raw.get("origin_run_id"),
            f"steps[{index}].origin_run_id",
            optional=True,
        ),
    )
    try:
        _manifest_step_to_dict(parsed)
    except (PayloadError, ValueError) as exc:
        raise PayloadError(f"Manifestets steps[{index}] er ugyldigt: {exc}") from exc
    if retryable is not None and retryable != parsed.retryable:
        raise PayloadError(
            f"Manifestets steps[{index}].retryable matcher ikke status"
        )
    return parsed


def _manifest_to_dict(manifest: RefreshManifest) -> dict[str, object]:
    if manifest.schema_version != 2:
        raise ValueError("Kun schema-2 manifests kan skrives")
    if manifest.scope not in {"game", "group"}:
        raise ValueError("Manifestet har ukendt scope")
    if not manifest.game_slug.strip() or not manifest.game_locale.strip():
        raise ValueError("Manifestet kræver game_slug og game_locale")
    if manifest.started_at.utcoffset() is None or manifest.completed_at.utcoffset() is None:
        raise ValueError("Manifestets start- og sluttid kræver tidszone")
    if manifest.completed_at < manifest.started_at:
        raise ValueError("Manifestets completed_at ligger før started_at")
    if manifest.target_round < 0:
        raise ValueError("Manifestets target_round må ikke være negativ")
    if manifest.mode not in {"all", "stale_only", "retry_failed"}:
        raise ValueError("Manifestet har ukendt refresh-mode")
    if manifest.mode == "retry_failed" and manifest.retry_of is None:
        raise ValueError("Et retry-manifest kræver retry_of")
    if len({item.step_id for item in manifest.steps}) != len(manifest.steps):
        raise ValueError("Manifestet har dublerede step-id'er")
    attempted_ids = manifest.attempted_team_ids
    skipped_ids = manifest.skipped_team_ids
    if (
        any(item < 0 for item in (*attempted_ids, *skipped_ids))
        or len(set(attempted_ids)) != len(attempted_ids)
        or len(set(skipped_ids)) != len(skipped_ids)
        or set(attempted_ids) & set(skipped_ids)
    ):
        raise ValueError("Manifestets attempted/skipped team-id'er er ugyldige")
    for label, value in (
        ("run_id", manifest.run_id),
        ("retry_of", manifest.retry_of),
        ("origin_run_id", manifest.origin_run_id or manifest.run_id),
    ):
        try:
            _manifest_run_id(value, label, optional=label == "retry_of")
        except PayloadError as exc:
            raise ValueError(str(exc)) from exc
    game = {
        "name": manifest.game_name,
        "url": manifest.game_url,
        "locale": manifest.game_locale,
        "slug": manifest.game_slug,
    }
    team_steps = tuple(item for item in manifest.steps if item.source == "team")
    player_step = next(
        (item for item in manifest.steps if item.source == "players"), None
    )
    metadata_step = next(
        (item for item in manifest.steps if item.source == "metadata"), None
    )

    def compatibility_step(step: RefreshManifestStep) -> dict[str, object]:
        legacy_status = {
            "fetched": "success",
            "reused_current": "success",
            "reused_after_error": "cached_fallback",
            "failed_no_cache": "failed",
            "skipped_unavailable": "failed",
        }[step.status]
        return {
            "team_id": step.team_id,
            "team_name": step.team_name,
            "status": legacy_status,
            "snapshot_path": step.data_reference or step.cache_reference,
            "cache_path": step.cache_reference,
            "error": step.error,
        }

    payload: dict[str, object] = {
        "schema_version": 2,
        "scope": manifest.scope,
        "run_id": manifest.run_id,
        "started_at": manifest.started_at.isoformat(),
        "completed_at": manifest.completed_at.isoformat(),
        "generated_at": manifest.completed_at.isoformat(),
        "game": game,
        "target_round": manifest.target_round,
        "round": manifest.target_round,
        "mode": manifest.mode,
        "result": manifest.result,
        "attempted_team_ids": list(manifest.attempted_team_ids),
        "skipped_team_ids": list(manifest.skipped_team_ids),
        "steps": [_manifest_step_to_dict(item) for item in manifest.steps],
        "teams": [compatibility_step(item) for item in team_steps],
        "player": (
            None if player_step is None else _manifest_step_to_dict(player_step)
        ),
        "metadata": (
            None if metadata_step is None else _manifest_step_to_dict(metadata_step)
        ),
        "groups": list(manifest.groups),
        "retry_of": manifest.retry_of,
        "origin_run_id": manifest.origin_run_id or manifest.run_id,
        "metadata_changes": [
            _metadata_change_to_dict(item) for item in manifest.metadata_changes
        ],
    }
    if manifest.group_id is not None:
        payload["group_id"] = manifest.group_id
    return payload


def _legacy_manifest_from_dict(
    payload: dict[str, object], path: Path
) -> RefreshManifest:
    generated_at = _manifest_timestamp(payload.get("generated_at"), "generated_at")
    round_number = _manifest_integer(payload.get("round"), "round")
    assert round_number is not None
    game_raw = payload.get("game")
    group_raw = payload.get("group")
    if isinstance(game_raw, dict):
        scope: ManifestScope = "game"
        game_slug = str(_manifest_text(game_raw.get("slug"), "game.slug"))
        game_locale = str(game_raw.get("locale") or "")
        game_name = str(game_raw.get("name") or game_slug)
        game_url = str(game_raw.get("url") or "")
        group_id = None
    elif isinstance(group_raw, dict):
        scope = "group"
        game_slug = str(_manifest_text(group_raw.get("game_slug"), "group.game_slug"))
        game_locale = ""
        game_name = str(group_raw.get("name") or game_slug)
        game_url = ""
        group_id = str(_manifest_text(group_raw.get("id"), "group.id"))
    else:
        raise PayloadError("Manifestet mangler game eller group")
    raw_teams = payload.get("teams")
    if not isinstance(raw_teams, list):
        raise PayloadError("Manifestet mangler teams")
    steps: list[RefreshManifestStep] = [
        RefreshManifestStep(
            "metadata",
            "metadata",
            "Spilinfo",
            "not_recorded",
            False,
            reason="Schema 1 registrerede ikke spilinfo separat",
        ),
        RefreshManifestStep(
            "players",
            "players",
            "Spillere",
            "not_recorded",
            False,
            reason="Schema 1 registrerede ikke spillere separat",
        ),
    ]
    for index, raw in enumerate(raw_teams):
        if not isinstance(raw, dict):
            raise PayloadError(f"Manifestets teams[{index}] skal være et objekt")
        team_id = _manifest_integer(raw.get("team_id"), f"teams[{index}].team_id")
        assert team_id is not None
        status = _manifest_text(raw.get("status"), f"teams[{index}].status")
        if status not in {"success", "cached_fallback", "failed"}:
            raise PayloadError(f"Manifestets teams[{index}] har ukendt status")
        _manifest_text(
            raw.get("snapshot_path"), f"teams[{index}].snapshot_path", optional=True
        )
        steps.append(
            RefreshManifestStep(
                step_id=f"team:{team_id}",
                source="team",
                label=f"Hold {raw.get('team_name') or team_id}",
                status={
                    "success": "fetched",
                    "cached_fallback": "reused_after_error",
                    "failed": "failed_no_cache",
                }[str(status)],  # type: ignore[arg-type]
                attempted=True,
                team_id=team_id,
                team_name=str(raw.get("team_name") or f"Hold {team_id}"),
                data_reference=None,
                cache_reference=None,
                error=_manifest_text(
                    raw.get("error"), f"teams[{index}].error", optional=True
                ),
            )
        )
    attempted = payload.get("attempted_team_ids", [])
    skipped = payload.get("skipped_team_ids", [])
    groups_raw = payload.get("groups", [])
    groups = tuple(item for item in groups_raw if isinstance(item, dict)) if isinstance(groups_raw, list) else ()
    legacy_run_id = str(uuid5(NAMESPACE_URL, path.resolve().as_uri()))
    return RefreshManifest(
        schema_version=1,
        scope=scope,
        run_id=legacy_run_id,
        started_at=generated_at,
        completed_at=generated_at,
        game_slug=game_slug,
        target_round=round_number,
        steps=tuple(steps),
        game_locale=game_locale.casefold(),
        game_name=game_name,
        game_url=game_url,
        attempted_team_ids=_manifest_ids(attempted, "attempted_team_ids"),
        skipped_team_ids=_manifest_ids(skipped, "skipped_team_ids"),
        groups=groups,
        group_id=group_id,
        origin_run_id=legacy_run_id,
        path=path.resolve(),
    )


def _manifest_from_dict(payload: object, path: Path) -> RefreshManifest:
    if not isinstance(payload, dict):
        raise PayloadError("Manifestets rod skal være et objekt")
    version = payload.get("schema_version")
    if version == 1:
        return _legacy_manifest_from_dict(payload, path)
    if version != 2:
        raise PayloadError("Ukendt manifestschema")
    scope = payload.get("scope")
    if scope not in {"game", "group"}:
        raise PayloadError("Manifestet har ukendt scope")
    game_raw = payload.get("game")
    if not isinstance(game_raw, dict):
        raise PayloadError("Manifestet mangler game")
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        raise PayloadError("Manifestet mangler steps")
    steps = tuple(
        _manifest_step_from_dict(raw, index)
        for index, raw in enumerate(raw_steps)
    )
    if len({item.step_id for item in steps}) != len(steps):
        raise PayloadError("Manifestet har dublerede step-id'er")
    run_id = _manifest_run_id(payload.get("run_id"), "run_id")
    assert run_id is not None
    started_at = _manifest_timestamp(payload.get("started_at"), "started_at")
    completed_at = _manifest_timestamp(payload.get("completed_at"), "completed_at")
    if completed_at < started_at:
        raise PayloadError("Manifestets completed_at ligger før started_at")
    target_round = _manifest_integer(
        payload.get("target_round", payload.get("round")), "target_round"
    )
    assert target_round is not None
    groups_raw = payload.get("groups", [])
    if not isinstance(groups_raw, list) or any(
        not isinstance(item, dict) for item in groups_raw
    ):
        raise PayloadError("Manifestet har ugyldige groups")
    raw_changes = payload.get("metadata_changes", [])
    if not isinstance(raw_changes, list):
        raise PayloadError("Manifestet har ugyldige metadata_changes")
    retry_of = _manifest_run_id(payload.get("retry_of"), "retry_of", optional=True)
    origin_run_id = _manifest_run_id(
        payload.get("origin_run_id"), "origin_run_id", optional=True
    )
    mode = str(payload.get("mode") or "all")
    if mode not in {"all", "stale_only", "retry_failed"}:
        raise PayloadError("Manifestet har ukendt refresh-mode")
    if mode == "retry_failed" and retry_of is None:
        raise PayloadError("Et retry-manifest mangler retry_of")
    result = payload.get("result")
    if result is not None and result not in {"complete", "partial", "failed"}:
        raise PayloadError("Manifestet har ukendt overordnet resultat")
    manifest = RefreshManifest(
        schema_version=2,
        scope=scope,
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        game_slug=str(_manifest_text(game_raw.get("slug"), "game.slug")),
        target_round=target_round,
        steps=steps,
        mode=mode,
        game_locale=str(game_raw.get("locale") or "").casefold(),
        game_name=str(game_raw.get("name") or game_raw.get("slug") or ""),
        game_url=str(game_raw.get("url") or ""),
        attempted_team_ids=_manifest_ids(
            payload.get("attempted_team_ids", []), "attempted_team_ids"
        ),
        skipped_team_ids=_manifest_ids(
            payload.get("skipped_team_ids", []), "skipped_team_ids"
        ),
        groups=tuple(groups_raw),  # type: ignore[arg-type]
        group_id=_manifest_text(
            payload.get("group_id"), "group_id", optional=True
        ),
        retry_of=retry_of,
        origin_run_id=origin_run_id or run_id,
        metadata_changes=tuple(
            _metadata_change_from_dict(raw, index)
            for index, raw in enumerate(raw_changes)
        ),
        path=path.resolve(),
    )
    if result is not None and result != manifest.result:
        raise PayloadError("Manifestets overordnede resultat matcher ikke steps")
    return manifest


class ManifestStore:
    """Publish manifests and normalize schema 1/2 on read."""

    def __init__(self, manifest_dir: Path | str) -> None:
        self.manifest_dir = Path(manifest_dir)

    def write(self, manifest: RefreshManifest) -> Path:
        """Publish one typed schema-2 manifest immutably."""

        if manifest.schema_version != 2:
            raise ValueError("Kun schema-2 manifests kan skrives")
        if manifest.scope == "group" and not manifest.group_id:
            raise ValueError("Et gruppemanifest kræver group_id")
        if not manifest.game_locale.strip():
            raise ValueError("Et schema-2 manifest kræver game_locale")
        locale = sanitize_path_component(
            manifest.game_locale.casefold(), fallback="da"
        )
        slug = sanitize_path_component(manifest.game_slug, fallback="game")
        target_dir = (
            self.manifest_dir
            / f"{locale}--{slug}"
            / (
                "game"
                if manifest.scope == "game"
                else Path("groups")
                / sanitize_path_component(str(manifest.group_id), fallback="group")
            )
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = round_output_stem(
            "refresh", manifest.round_number, manifest.generated_at
        )
        content = json.dumps(
            _manifest_to_dict(manifest), ensure_ascii=False, indent=2
        ) + "\n"
        collision_number = 0
        while True:
            suffix = collision_suffix(collision_number)
            candidate = target_dir / f"{stem}{suffix}.json"
            if candidate.exists():
                collision_number += 1
                continue
            try:
                publish_immutable_text(candidate, content)
            except FileExistsError:
                collision_number += 1
                continue
            return candidate

    def load(self, path: Path | str) -> RefreshManifest:
        """Load one schema-1 or schema-2 manifest into the typed projection."""

        source = Path(path)
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PayloadError(f"Manifestet kunne ikke læses: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise PayloadError("Manifestet indeholder ugyldig JSON") from exc
        return _manifest_from_dict(payload, source)

    def scan(
        self,
        game_slug: str,
        *,
        game_locale: str | None = None,
        scope: ManifestScope = "game",
        group_id: str | None = None,
    ) -> tuple[tuple[RefreshManifest, ...], tuple[str, ...]]:
        """Read all matching manifests newest-first while isolating bad files."""

        slug = sanitize_path_component(game_slug, fallback="game")
        roots: list[Path] = [self.manifest_dir / slug]
        if game_locale is not None:
            locale = sanitize_path_component(game_locale.casefold(), fallback="da")
            roots.insert(0, self.manifest_dir / f"{locale}--{slug}")
        elif self.manifest_dir.exists():
            roots.extend(
                path
                for path in self.manifest_dir.glob(f"*--{slug}")
                if path.is_dir()
            )
        candidates: list[Path] = []
        for game_root in dict.fromkeys(roots):
            if scope == "game":
                root = game_root / "game"
                if root.exists():
                    candidates.extend(root.glob("refresh-round*.json"))
            elif group_id is not None:
                root = game_root / "groups" / sanitize_path_component(
                    group_id, fallback="group"
                )
                if root.exists():
                    candidates.extend(root.glob("refresh-round*.json"))
            else:
                root = game_root / "groups"
                if root.exists():
                    candidates.extend(root.rglob("refresh-round*.json"))
        values: list[RefreshManifest] = []
        warnings: list[str] = []
        for path in candidates:
            try:
                value = self.load(path)
                if value.scope != scope or value.game_slug != game_slug:
                    raise PayloadError("Manifestet tilhører et andet scope eller spil")
                if (
                    game_locale is not None
                    and value.game_locale
                    and value.game_locale != game_locale.casefold()
                ):
                    raise PayloadError("Manifestet tilhører et andet locale")
                if group_id is not None and value.group_id != group_id:
                    raise PayloadError("Manifestet tilhører en anden gruppe")
                values.append(value)
            except (OSError, ValueError, PayloadError) as exc:
                warnings.append(f"{path}: {exc}")
        values.sort(
            key=lambda item: (
                item.generated_at.astimezone(timezone.utc),
                item.run_id,
            ),
            reverse=True,
        )
        return tuple(values), tuple(warnings)

    def load_latest(
        self,
        game_slug: str,
        *,
        game_locale: str | None = None,
        scope: ManifestScope = "game",
        group_id: str | None = None,
    ) -> RefreshManifest | None:
        """Return the newest readable matching manifest."""

        values, _ = self.scan(
            game_slug,
            game_locale=game_locale,
            scope=scope,
            group_id=group_id,
        )
        return values[0] if values else None

    def save_game_manifest(
        self,
        game_slug: str,
        round_number: int,
        payload: dict[str, object],
        *,
        now: datetime | None = None,
    ) -> Path:
        """Publish one immutable manager-game refresh manifest."""

        generated_at = aware_local(now)
        target_dir = (
            self.manifest_dir
            / sanitize_path_component(game_slug, fallback="game")
            / "game"
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = round_output_stem("refresh", round_number, generated_at)
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        collision_number = 0
        while True:
            suffix = collision_suffix(collision_number)
            candidate = target_dir / f"{stem}{suffix}.json"
            if candidate.exists():
                collision_number += 1
                continue
            try:
                publish_immutable_text(candidate, content)
            except FileExistsError:
                collision_number += 1
                continue
            return candidate

    def save_manifest(
        self,
        game_slug: str,
        group_id: str,
        round_number: int,
        payload: dict[str, object],
        *,
        now: datetime | None = None,
    ) -> Path:
        generated_at = aware_local(now)
        target_dir = (
            self.manifest_dir
            / sanitize_path_component(game_slug, fallback="game")
            / "groups"
            / sanitize_path_component(group_id, fallback="group")
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = round_output_stem("refresh", round_number, generated_at)
        content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        collision_number = 0
        while True:
            suffix = collision_suffix(collision_number)
            candidate = target_dir / f"{stem}{suffix}.json"
            if candidate.exists():
                collision_number += 1
                continue
            try:
                publish_immutable_text(candidate, content)
            except FileExistsError:
                collision_number += 1
                continue
            return candidate
