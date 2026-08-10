"""Explicit, previewable on-demand refresh orchestration."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable, Literal, TypeAlias
from uuid import uuid4

from .analysis_inbox import (
    AnalysisInboxStore,
    build_watchlist_alerts,
)
from .client import HoldetClient
from .errors import PayloadError
from .game_metadata import (
    GameMetadata,
    GameMetadataStore,
    compare_game_metadata,
)
from .groups import GroupDefinition, GroupTeam, ManagerGame
from .hub_settings import HubSettings
from .models import GameUrl, ScrapedTeam
from .output import sanitize_path_component
from .persistence import aware_local
from .storage import (
    ManifestStore,
    PlayerStatisticsIndex,
    PlayerStatisticsSnapshot,
    PlayerStatisticsStore,
    RefreshManifest,
    RefreshManifestStep,
    RefreshMetadataChange,
    SnapshotIndex,
    SnapshotStore,
)
from .tournament import build_tournament_state, latest_tournament_round


class RefreshMode(StrEnum):
    ALL = "all"
    STALE_ONLY = "stale_only"
    RETRY_FAILED = "retry_failed"


# Daily workflows must not silently treat indefinitely old cache as current.
# The boundary is inclusive: data is stale when it is at least 24 hours old.
DAILY_REFRESH_MAX_AGE = timedelta(hours=24)


RefreshSource = Literal["metadata", "players", "team", "postprocess"]
RefreshProgressStatus = Literal[
    "running",
    "fetched",
    "reused_current",
    "reused_after_error",
    "failed_no_cache",
    "skipped_unavailable",
]


@dataclass(frozen=True, slots=True)
class RefreshStep:
    """One previewed source operation in a refresh plan."""

    step_id: str
    source: RefreshSource
    label: str
    selected: bool
    stale: bool
    reason: str
    team_id: int | None = None
    team_name: str | None = None
    current_generated_at: datetime | None = None
    available: bool = True

    def __post_init__(self) -> None:
        if self.source not in {"metadata", "players", "team", "postprocess"}:
            raise ValueError("RefreshStep har ukendt kilde")
        if (
            not self.step_id.strip()
            or not self.label.strip()
            or not self.reason.strip()
        ):
            raise ValueError("RefreshStep kræver step_id, label og reason")
        if self.source == "team" and self.team_id is None:
            raise ValueError("Et holdstep kræver team_id")
        if not self.available and self.selected:
            raise ValueError("Et utilgængeligt refreshstep må ikke vælges")
        if self.team_id is not None and self.team_id < 0:
            raise ValueError("Et holdstep må ikke have negativt team_id")
        if (
            self.current_generated_at is not None
            and self.current_generated_at.utcoffset() is None
        ):
            raise ValueError("RefreshStep.current_generated_at kræver tidszone")


@dataclass(frozen=True, slots=True)
class RefreshPlan:
    """Immutable preview that can be passed directly to refresh_manager_game."""

    manager_game: ManagerGame
    mode: RefreshMode
    created_at: datetime
    target_round: int
    steps: tuple[RefreshStep, ...]
    retry_of: str | None = None
    origin_run_id: str | None = None
    previous_manifest: RefreshManifest | None = None
    include_postprocess: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", RefreshMode(self.mode))
        if self.created_at.utcoffset() is None:
            raise ValueError("RefreshPlan.created_at skal indeholde tidszone")
        if self.target_round < 0:
            raise ValueError("RefreshPlan.target_round må ikke være negativ")
        if len({item.step_id for item in self.steps}) != len(self.steps):
            raise ValueError("RefreshPlan har dublerede step-id'er")
        if self.mode == RefreshMode.RETRY_FAILED and not self.retry_of:
            raise ValueError("En retry-plan kræver retry_of")

    @property
    def selected_steps(self) -> tuple[RefreshStep, ...]:
        return tuple(item for item in self.steps if item.selected)

    @property
    def attempted_team_ids(self) -> tuple[int, ...]:
        return tuple(
            item.team_id
            for item in self.selected_steps
            if item.source == "team" and item.team_id is not None
        )

    @property
    def skipped_team_ids(self) -> tuple[int, ...]:
        return tuple(
            item.team_id
            for item in self.steps
            if item.source == "team"
            and not item.selected
            and item.team_id is not None
        )

    @property
    def selected_team_count(self) -> int:
        """Number of teams the preview says will be requested."""

        return len(self.attempted_team_ids)

    @property
    def selected_source_count(self) -> int:
        """Number of selected source operations, including teams."""

        return len(self.selected_steps)


@dataclass(frozen=True, slots=True)
class RefreshProgressEvent:
    step: RefreshStep
    status: RefreshProgressStatus
    completed_steps: int
    total_steps: int
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str | None = None


RefreshProgressCallback: TypeAlias = Callable[[RefreshProgressEvent], None]


class ManifestWriteError(RuntimeError):
    """Expose a complete in-memory manifest for an idempotent local retry."""

    def __init__(self, manifest: RefreshManifest, cause: Exception) -> None:
        super().__init__(f"Refresh-manifestet kunne ikke gemmes: {cause}")
        self.manifest = manifest
        self.cause = cause


@dataclass(frozen=True, slots=True)
class TeamRefresh:
    team_id: int
    team_name: str
    status: str
    snapshot_path: Path | None
    team: ScrapedTeam | None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class PlayerRefresh:
    status: str
    round_number: int | None
    snapshot_path: Path | None
    alert_count: int = 0
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MetadataRefresh:
    status: str
    fetched_at: datetime | None
    changes: tuple[RefreshMetadataChange, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class GameRefreshResult:
    manager_game: ManagerGame
    round_number: int
    generated_at: datetime
    teams: tuple[TeamRefresh, ...]
    attempted_team_ids: tuple[int, ...]
    skipped_team_ids: tuple[int, ...]
    manifest_path: Path
    player: PlayerRefresh | None = None
    metadata: MetadataRefresh | None = None
    plan: RefreshPlan | None = None
    manifest: RefreshManifest | None = None

    @property
    def failures(self) -> tuple[TeamRefresh, ...]:
        return tuple(item for item in self.teams if item.status != "success")


@dataclass(frozen=True, slots=True)
class GroupRefreshResult:
    group: GroupDefinition
    round_number: int
    generated_at: datetime
    teams: tuple[TeamRefresh, ...]
    manifest_path: Path

    @property
    def failures(self) -> tuple[TeamRefresh, ...]:
        return tuple(item for item in self.teams if item.status != "success")


def _game_identity(game: GameUrl) -> tuple[str, str]:
    return game.locale.casefold(), game.slug


def _members_for_game(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
) -> tuple[tuple[GroupDefinition, ...], dict[int, GroupTeam]]:
    selected_groups = tuple(
        group
        for group in groups
        if _game_identity(group.game) == manager_game.identity
    )
    members: dict[int, GroupTeam] = {}
    for group in selected_groups:
        for member in group.teams:
            existing = members.get(member.team_id)
            if existing is None or (
                existing.account_key == "direct" and member.account_key != "direct"
            ):
                members[member.team_id] = member
    return selected_groups, members


def _instant(value: datetime) -> datetime:
    """Normalize timestamps to UTC before elapsed-time and order comparisons."""

    resolved = value.astimezone() if value.tzinfo is None else value
    return resolved.astimezone(timezone.utc)


def _latest_milestone(
    metadata: GameMetadata | None, current: datetime
) -> tuple[int, str, datetime] | None:
    if metadata is None:
        return None
    current_instant = _instant(current)
    candidates = [
        (item.round_number, kind, _instant(timestamp))
        for item in metadata.rounds
        for kind, timestamp in (
            ("start", item.start),
            ("deadline", item.close),
            ("end", item.end),
        )
        if _instant(timestamp) <= current_instant
    ]
    kind_order = {"start": 0, "deadline": 1, "end": 2}
    return max(
        candidates,
        key=lambda item: (item[2], item[0], kind_order[item[1]]),
        default=None,
    )


def _target_round(
    manager_game: ManagerGame,
    members: dict[int, GroupTeam],
    teams: SnapshotIndex,
    players: PlayerStatisticsIndex,
    metadata: GameMetadata | None,
    current: datetime,
) -> int:
    milestone = _latest_milestone(metadata, current)
    candidates = [0]
    if milestone is not None:
        candidates.append(milestone[0])
    candidates.extend(players.rounds_for(manager_game.game))
    candidates.extend(teams.rounds_for(manager_game.game, tuple(members)))
    return max(candidates)


def _source_is_stale(
    *,
    generated_at: datetime | None,
    round_number: int | None,
    round_status: str | None,
    metadata: GameMetadata | None,
    current: datetime,
) -> tuple[bool, str]:
    if generated_at is None:
        return True, "Data mangler"
    current_instant = _instant(current)
    generated_instant = _instant(generated_at)
    if current_instant - generated_instant >= DAILY_REFRESH_MAX_AGE:
        return True, "Data er mindst 24 timer gamle"
    if metadata is None:
        return True, "Tidsplanen mangler, så friskhed kan ikke verificeres"
    milestone = _latest_milestone(metadata, current)
    if milestone is None:
        return False, "Ingen tidsplansmilepæl er passeret"
    milestone_round, milestone_kind, timestamp = milestone
    if generated_instant < timestamp or (
        round_number is not None and round_number < milestone_round
    ):
        return True, f"Data er ældre end seneste {milestone_kind}"
    if milestone_kind == "end" and round_status not in {None, "complete"}:
        return True, "Runden er afsluttet, men data er ikke bekræftet"
    return False, "Data er aktuelle"


def _newer_retryable_failure(
    previous_manifest: RefreshManifest | None,
    step_id: str,
    current_generated_at: datetime | None,
) -> RefreshManifestStep | None:
    """Return a retryable outcome only when it is newer than current cache."""

    if previous_manifest is None:
        return None
    outcome = next(
        (
            item
            for item in previous_manifest.steps
            if item.step_id == step_id and item.retryable
        ),
        None,
    )
    if outcome is None:
        return None
    failed_at = outcome.completed_at or previous_manifest.completed_at
    if (
        current_generated_at is not None
        and _instant(failed_at) <= _instant(current_generated_at)
    ):
        return None
    return outcome


def _apply_manifest_failure(
    previous_manifest: RefreshManifest | None,
    step_id: str,
    generated_at: datetime | None,
    stale: bool,
    reason: str,
) -> tuple[bool, str]:
    outcome = _newer_retryable_failure(
        previous_manifest, step_id, generated_at
    )
    if outcome is None:
        return stale, reason
    if outcome.status == "reused_after_error":
        return True, "Seneste opdatering fejlede og genbrugte ældre cache"
    return True, "Seneste opdatering fejlede uden brugbar cache"


def build_refresh_plan(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    teams: SnapshotIndex,
    players: PlayerStatisticsIndex,
    *,
    metadata: GameMetadata | None = None,
    mode: RefreshMode | str = RefreshMode.ALL,
    previous_manifest: RefreshManifest | None = None,
    include_metadata: bool = True,
    include_postprocess: bool = False,
    now: datetime | None = None,
) -> RefreshPlan:
    """Build a cache-only refresh preview; no files or network are touched."""

    selected_mode = RefreshMode(mode)
    current = aware_local(now)
    selected_groups, members = _members_for_game(manager_game, groups)
    del selected_groups
    if previous_manifest is not None and (
        previous_manifest.scope != "game"
        or previous_manifest.game_slug != manager_game.game.slug
        or (
            previous_manifest.game_locale
            and previous_manifest.game_locale != manager_game.game.locale.casefold()
        )
    ):
        raise ValueError("Manifestet tilhører et andet managerspil")
    if selected_mode == RefreshMode.RETRY_FAILED and previous_manifest is None:
        raise ValueError("RETRY_FAILED kræver previous_manifest")
    retryable = (
        set()
        if previous_manifest is None
        else {item.step_id for item in previous_manifest.failures}
    )
    previous_step_ids = (
        set()
        if previous_manifest is None
        else {item.step_id for item in previous_manifest.steps}
    )
    target_round = (
        previous_manifest.target_round
        if selected_mode == RefreshMode.RETRY_FAILED and previous_manifest is not None
        else _target_round(manager_game, members, teams, players, metadata, current)
    )
    failure_manifest = (
        previous_manifest
        if previous_manifest is not None
        and (
            selected_mode == RefreshMode.RETRY_FAILED
            or previous_manifest.target_round == target_round
        )
        else None
    )

    def selected(step_id: str, stale: bool, *, metadata_step: bool = False) -> bool:
        if metadata_step and not include_metadata:
            return False
        if selected_mode == RefreshMode.ALL:
            return True
        if selected_mode == RefreshMode.STALE_ONLY:
            return stale
        return step_id in retryable

    metadata_stale = metadata is None
    metadata_reason = "Spilinfo mangler"
    milestone = _latest_milestone(metadata, current)
    if metadata is not None:
        if (
            _instant(current) - _instant(metadata.fetched_at)
            >= DAILY_REFRESH_MAX_AGE
        ):
            metadata_stale = True
            metadata_reason = "Spilinfo er mindst 24 timer gammel"
        elif milestone is not None and _instant(metadata.fetched_at) < milestone[2]:
            metadata_stale = True
            metadata_reason = "Spilinfo er ældre end seneste tidsplansmilepæl"
        elif target_round > 0 and target_round not in {
            item.round_number for item in metadata.rounds
        }:
            metadata_stale = True
            metadata_reason = (
                f"Tidsplanen mangler den aktuelle cacherunde {target_round}"
            )
        else:
            metadata_stale = False
            metadata_reason = "Spilinfo er aktuel"
    metadata_stale, metadata_reason = _apply_manifest_failure(
        failure_manifest,
        "metadata",
        None if metadata is None else metadata.fetched_at,
        metadata_stale,
        metadata_reason,
    )
    steps: list[RefreshStep] = [
        RefreshStep(
            "metadata",
            "metadata",
            "Spilinfo, regler og tidsplan",
            selected("metadata", metadata_stale, metadata_step=True),
            metadata_stale,
            (
                "Ikke en del af det kompatible refresh-kald"
                if not include_metadata
                else metadata_reason
            ),
            current_generated_at=None if metadata is None else metadata.fetched_at,
        )
    ]
    exact_player_snapshot = (
        players.newest(manager_game.game, target_round)
        if target_round
        else players.newest(manager_game.game)
    )
    player_snapshot = exact_player_snapshot
    if player_snapshot is None:
        player_snapshot = players.newest(manager_game.game)
    player_stale, player_reason = _source_is_stale(
        generated_at=(
            None if player_snapshot is None else player_snapshot.generated_at
        ),
        round_number=(
            None if player_snapshot is None else player_snapshot.statistics.round_number
        ),
        round_status=(
            None if player_snapshot is None else player_snapshot.statistics.round_status
        ),
        metadata=metadata,
        current=current,
    )
    if target_round > 0 and exact_player_snapshot is None:
        player_stale = True
        player_reason = f"Spillerdata for runde {target_round} mangler"
    player_stale, player_reason = _apply_manifest_failure(
        failure_manifest,
        "players",
        None if player_snapshot is None else player_snapshot.generated_at,
        player_stale,
        player_reason,
    )
    steps.append(
        RefreshStep(
            "players",
            "players",
            "Spillere",
            selected("players", player_stale),
            player_stale,
            player_reason,
            current_generated_at=(
                None if player_snapshot is None else player_snapshot.generated_at
            ),
        )
    )
    for team_id, member in sorted(members.items()):
        located = (
            teams.summary_for(manager_game.game, team_id, target_round)
            if target_round
            else None
        )
        newest = teams.newest(manager_game.game, team_id)
        snapshot = None if located is None else located[0]
        summary = None if located is None else located[1]
        if snapshot is None:
            snapshot = newest
        team_stale, team_reason = _source_is_stale(
            generated_at=None if snapshot is None else snapshot.generated_at,
            round_number=(
                None
                if summary is None
                else summary.round_number
            ),
            round_status=None if summary is None else summary.round_status,
            metadata=metadata,
            current=current,
        )
        if target_round > 0 and located is None:
            team_stale = True
            team_reason = f"Rundedata for runde {target_round} mangler"
        step_id = f"team:{team_id}"
        team_stale, team_reason = _apply_manifest_failure(
            failure_manifest,
            step_id,
            None if snapshot is None else snapshot.generated_at,
            team_stale,
            team_reason,
        )
        available = not (
            selected_mode == RefreshMode.RETRY_FAILED
            and step_id not in previous_step_ids
        )
        if not available:
            team_reason = (
                "Holdet er tilføjet efter den oprindelige kørsel og hentes "
                "ved en normal opdatering"
            )
        steps.append(
            RefreshStep(
                step_id,
                "team",
                f"Hold: {member.name}",
                selected(step_id, team_stale) and available,
                team_stale,
                team_reason,
                team_id,
                member.name,
                None if snapshot is None else snapshot.generated_at,
                available,
            )
        )
    if selected_mode == RefreshMode.RETRY_FAILED and previous_manifest is not None:
        current_team_ids = set(members)
        removed_steps = sorted(
            (
                item
                for item in previous_manifest.steps
                if item.source == "team"
                and item.team_id is not None
                and item.team_id not in current_team_ids
            ),
            key=lambda item: (item.team_id or 0, item.step_id),
        )
        for old_step in removed_steps:
            steps.append(
                RefreshStep(
                    old_step.step_id,
                    "team",
                    old_step.label,
                    False,
                    old_step.retryable,
                    "Holdet er ikke længere med i managerspillets grupper",
                    old_step.team_id,
                    old_step.team_name,
                    None,
                    False,
                )
            )
    if include_postprocess:
        previous_postprocess = (
            None
            if failure_manifest is None
            else next(
                (
                    item
                    for item in failure_manifest.steps
                    if item.step_id == "postprocess"
                    and item.source == "postprocess"
                ),
                None,
            )
        )
        upstream_selected = any(
            item.selected and item.source != "postprocess" for item in steps
        )
        postprocess_failed = bool(
            previous_postprocess is not None and previous_postprocess.retryable
        )
        postprocess_selected = (
            selected_mode == RefreshMode.ALL
            or (
                selected_mode == RefreshMode.STALE_ONLY
                and (upstream_selected or postprocess_failed)
            )
            or (
                selected_mode == RefreshMode.RETRY_FAILED
                and (postprocess_failed or upstream_selected)
            )
        )
        if postprocess_failed:
            postprocess_reason = "Seneste efterbehandling fejlede"
        elif upstream_selected:
            postprocess_reason = "Køres efter de valgte datakilder"
        else:
            postprocess_reason = "Efterbehandlingen er allerede aktuel"
        steps.append(
            RefreshStep(
                "postprocess",
                "postprocess",
                "Efterbehandling",
                postprocess_selected,
                postprocess_failed or upstream_selected,
                postprocess_reason,
            )
        )
    return RefreshPlan(
        manager_game,
        selected_mode,
        current,
        target_round,
        tuple(steps),
        retry_of=(
            None if previous_manifest is None else previous_manifest.run_id
        ) if selected_mode == RefreshMode.RETRY_FAILED else None,
        origin_run_id=(
            previous_manifest.origin_run_id or previous_manifest.run_id
            if selected_mode == RefreshMode.RETRY_FAILED
            and previous_manifest is not None
            else None
        ),
        previous_manifest=previous_manifest,
        include_postprocess=include_postprocess,
    )


def _revalidate_stale_plan_after_metadata(
    plan: RefreshPlan,
    groups: tuple[GroupDefinition, ...],
    teams: SnapshotIndex,
    players: PlayerStatisticsIndex,
    metadata: GameMetadata,
    *,
    now: datetime,
) -> RefreshPlan:
    """Rebuild stale-only upstream choices against newly fetched schedule data."""

    if plan.mode != RefreshMode.STALE_ONLY:
        return plan
    rebuilt = build_refresh_plan(
        plan.manager_game,
        groups,
        teams,
        players,
        metadata=metadata,
        mode=plan.mode,
        previous_manifest=plan.previous_manifest,
        include_metadata=True,
        include_postprocess=plan.include_postprocess,
        now=now,
    )
    completed_metadata = next(
        (item for item in plan.steps if item.source == "metadata"),
        None,
    )
    if completed_metadata is None:
        return rebuilt
    downstream = tuple(
        replace(
            item,
            selected=True,
            stale=True,
            reason="Køres efter den hentede spilinfo",
        )
        if item.source == "postprocess" and completed_metadata.selected
        else item
        for item in rebuilt.steps
        if item.source != "metadata"
    )
    return replace(
        rebuilt,
        created_at=plan.created_at,
        steps=(
            completed_metadata,
            *downstream,
        ),
    )


def _group_refresh_state(
    group: GroupDefinition, snapshots: SnapshotIndex
) -> dict[str, object]:
    member_ids = {member.team_id for member in group.teams}
    if group.kind != "tournament":
        return {
            "id": group.group_id,
            "name": group.name,
            "type": group.kind,
            "revision": None,
            "phase": None,
            "latest_round": max(
                (
                    snapshot.team.overview.current_round
                    for team_id in member_ids
                    if (snapshot := snapshots.newest(group.game, team_id))
                    is not None
                ),
                default=0,
            ),
            "active_team_ids": sorted(member_ids),
            "eliminated_team_ids": [],
            "champion_id": None,
            "champion_name": None,
        }
    latest_round = latest_tournament_round(group, snapshots)
    state = build_tournament_state(group, snapshots, latest_round)
    champion = next(
        (row.team_name for row in state.standings if row.team_id == state.champion_id),
        None,
    )
    return {
        "id": group.group_id,
        "name": group.name,
        "type": group.kind,
        "revision": group.active_revision,
        "phase": state.phase,
        "latest_round": latest_round,
        "active_team_ids": sorted(state.active_team_ids),
        "eliminated_team_ids": sorted(state.eliminated_team_ids),
        "champion_id": state.champion_id,
        "champion_name": champion,
    }


def _metadata_changes(
    before: GameMetadata | None, after: GameMetadata
) -> tuple[RefreshMetadataChange, ...]:
    if before is None:
        return ()
    return tuple(
        RefreshMetadataChange(
            path=(
                f"schedule.rounds.{item.round_number}.{item.field}"
                if item.kind == "schedule" and item.round_number is not None
                else f"{item.kind}.{item.field}"
            ),
            kind=(
                "added"
                if item.old_value is None and item.new_value is not None
                else "removed"
                if item.old_value is not None and item.new_value is None
                else "changed"
            ),
            before=item.old_value,
            after=item.new_value,
        )
        for item in compare_game_metadata(before, after)
    )


def _relative_reference(root: Path, path: Path, prefix: str) -> str | None:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return (Path(prefix) / relative).as_posix()


def _metadata_reference(store: GameMetadataStore, game: GameUrl) -> str:
    locale = sanitize_path_component(game.locale.casefold(), fallback="da")
    slug = sanitize_path_component(game.slug, fallback="game")
    return f"game-metadata/{locale}--{slug}.json"


def _legacy_manifest_payload(
    manager_game: ManagerGame,
    round_number: int,
    generated_at: datetime,
    groups: list[dict[str, object]],
    results: list[TeamRefresh],
    attempted: tuple[int, ...],
    skipped: tuple[int, ...],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "scope": "game",
        "generated_at": generated_at.isoformat(),
        "game": {
            "name": manager_game.name,
            "url": manager_game.game.original,
            "locale": manager_game.game.locale,
            "slug": manager_game.game.slug,
        },
        "round": round_number,
        "groups": groups,
        "attempted_team_ids": list(attempted),
        "skipped_team_ids": list(skipped),
        "teams": [
            {
                "team_id": item.team_id,
                "team_name": item.team_name,
                "status": item.status,
                "snapshot_path": (
                    str(item.snapshot_path) if item.snapshot_path else None
                ),
                "error": item.error,
            }
            for item in results
        ],
    }


def refresh_game(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    client: HoldetClient,
    snapshot_store: SnapshotStore,
    manifest_store: ManifestStore,
    *,
    now: datetime | None = None,
) -> GameRefreshResult:
    """Compatibility team-only refresh using the schema-1 wire format."""

    selected_groups, members = _members_for_game(manager_game, groups)
    generated_at = aware_local(now)
    before = snapshot_store.scan()
    attempted = tuple(sorted(members))
    results: list[TeamRefresh] = []
    for team_id in attempted:
        member = members[team_id]
        try:
            team = client.fetch_team(member.reference(manager_game.game))
            path = snapshot_store.save_team_json(team, now=generated_at)
            results.append(TeamRefresh(team_id, team.team_name, "success", path, team))
        except Exception as exc:
            cached = before.newest(manager_game.game, team_id)
            if cached is not None:
                results.append(
                    TeamRefresh(
                        team_id,
                        cached.team.team_name,
                        "cached_fallback",
                        cached.path,
                        cached.team,
                        str(exc),
                    )
                )
            else:
                results.append(
                    TeamRefresh(team_id, member.name, "failed", None, None, str(exc))
                )
    after = snapshot_store.scan()
    round_number = max(
        (
            snapshot.team.overview.current_round
            for team_id in attempted
            if (snapshot := after.newest(manager_game.game, team_id)) is not None
        ),
        default=0,
    )
    group_states = [_group_refresh_state(group, after) for group in selected_groups]
    manifest_path = manifest_store.save_game_manifest(
        manager_game.game.slug,
        round_number,
        _legacy_manifest_payload(
            manager_game,
            round_number,
            generated_at,
            group_states,
            results,
            attempted,
            (),
        ),
        now=generated_at,
    )
    return GameRefreshResult(
        manager_game,
        round_number,
        generated_at,
        tuple(results),
        attempted,
        (),
        manifest_path,
    )


def refresh_manager_game(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    client: HoldetClient,
    snapshot_store: SnapshotStore,
    player_store: PlayerStatisticsStore,
    manifest_store: ManifestStore,
    *,
    settings: HubSettings | None = None,
    inbox_store: AnalysisInboxStore | None = None,
    metadata_store: GameMetadataStore | None = None,
    plan: RefreshPlan | None = None,
    progress: RefreshProgressCallback | None = None,
    postprocess: Callable[[], None] | None = None,
    now: datetime | None = None,
) -> GameRefreshResult:
    """Refresh a previewed subset while retaining the legacy call contract."""

    started_at = aware_local(now)
    run_id = str(uuid4())
    selected_groups, members = _members_for_game(manager_game, groups)
    before_teams = snapshot_store.scan()
    before_players = player_store.scan(manager_game.game)
    metadata_load_error: str | None = None
    if metadata_store is None:
        before_metadata = None
    else:
        try:
            before_metadata = metadata_store.load(manager_game.game)
        except (OSError, PayloadError, ValueError) as exc:
            before_metadata = None
            metadata_load_error = str(exc)
    if plan is None:
        plan = build_refresh_plan(
            manager_game,
            groups,
            before_teams,
            before_players,
            metadata=before_metadata,
            mode=RefreshMode.ALL,
            include_metadata=metadata_store is not None,
            include_postprocess=(
                postprocess is not None
                or (settings is not None and inbox_store is not None)
            ),
            now=started_at,
        )
    elif metadata_load_error is not None and plan.mode == RefreshMode.STALE_ONLY:
        plan = replace(
            plan,
            steps=tuple(
                replace(
                    item,
                    selected=True,
                    stale=True,
                    reason=(
                        "Lokal spilinfo kunne ikke læses og skal genhentes: "
                        f"{metadata_load_error}"
                    ),
                    current_generated_at=None,
                )
                if item.source == "metadata"
                else item
                for item in plan.steps
            ),
        )
    if (
        not plan.include_postprocess
        and settings is not None
        and inbox_store is not None
        and any(
            item.source == "players" and item.selected
            for item in plan.steps
        )
    ):
        plan = replace(
            plan,
            include_postprocess=True,
            steps=(
                *plan.steps,
                RefreshStep(
                    "postprocess",
                    "postprocess",
                    "Efterbehandling",
                    True,
                    True,
                    "Køres efter den valgte spillerkilde",
                ),
            ),
        )
    if plan.manager_game.identity != manager_game.identity:
        raise ValueError("Refresh-planen tilhører et andet managerspil")
    planned_team_ids = {
        item.team_id
        for item in plan.steps
        if item.source == "team" and item.team_id is not None
    }
    unavailable_team_ids = {
        item.team_id
        for item in plan.steps
        if item.source == "team"
        and item.team_id is not None
        and not item.available
    }
    unknown_team_ids = planned_team_ids - set(members) - unavailable_team_ids
    if unknown_team_ids:
        raise ValueError("Refresh-planen indeholder hold uden for managerspillet")

    total_steps = len(plan.selected_steps)
    completed_steps = 0
    manifest_steps: dict[str, RefreshManifestStep] = {}
    team_results: list[TeamRefresh] = []
    player_result: PlayerRefresh | None = None
    metadata_result: MetadataRefresh | None = None
    changes: tuple[RefreshMetadataChange, ...] = ()

    alert_snapshot_reference: str | None = None

    def snapshot_reference(snapshot: PlayerStatisticsSnapshot) -> str | None:
        return _relative_reference(
            player_store.snapshot_dir,
            snapshot.path,
            "snapshots",
        )

    def referenced_player_snapshot(
        reference: str | None,
    ) -> tuple[PlayerStatisticsSnapshot | None, PlayerStatisticsSnapshot | None]:
        if reference is None:
            return None, None
        snapshots = player_store.scan(manager_game.game).for_game(
            manager_game.game
        )
        current_index = next(
            (
                index
                for index, item in enumerate(snapshots)
                if snapshot_reference(item) == reference
            ),
            None,
        )
        if current_index is None:
            return None, None
        current_snapshot = snapshots[current_index]
        previous_snapshot = next(
            (
                item
                for item in snapshots[current_index + 1 :]
                if item.statistics.round_number
                <= current_snapshot.statistics.round_number
            ),
            None,
        )
        return previous_snapshot, current_snapshot

    def merge_player_alerts(reference: str | None) -> int:
        """Derive alerts for one immutable, explicitly referenced transition."""

        if reference is None or settings is None or inbox_store is None:
            return 0
        previous_snapshot, current_snapshot = referenced_player_snapshot(
            reference
        )
        if current_snapshot is None:
            raise PayloadError(
                "Spillersnapshottet til statusalarmer er ikke tilgængeligt"
            )
        alerts = build_watchlist_alerts(
            previous_snapshot,
            current_snapshot,
            settings.watchlist,
            now=started_at,
        )
        inbox_store.merge(alerts)
        return len(alerts)

    def emit(
        step: RefreshStep,
        status: RefreshProgressStatus,
        *,
        started: datetime | None = None,
        completed: datetime | None = None,
        error: str | None = None,
    ) -> None:
        if progress is not None:
            try:
                progress(
                    RefreshProgressEvent(
                        step,
                        status,
                        completed_steps,
                        total_steps,
                        started,
                        completed,
                        error,
                    )
                )
            except Exception:
                # Progress observers are telemetry only and must never turn a
                # successfully persisted source into a refresh failure.
                return

    def finish(
        step: RefreshStep,
        status: RefreshProgressStatus,
        *,
        step_started: datetime,
        error: str | None = None,
        round_number: int | None = None,
        data_reference: str | None = None,
        cache_reference: str | None = None,
        cache_generated_at: datetime | None = None,
    ) -> None:
        nonlocal completed_steps
        completed = aware_local(now)
        completed_steps += 1
        manifest_steps[step.step_id] = RefreshManifestStep(
            step.step_id,
            step.source,
            step.label,
            status,
            True,
            reason=step.reason,
            started_at=step_started,
            completed_at=completed,
            team_id=step.team_id,
            team_name=step.team_name,
            round_number=round_number,
            data_reference=data_reference,
            cache_reference=cache_reference,
            cache_generated_at=cache_generated_at,
            error=error,
            origin_run_id=run_id,
        )
        emit(
            step,
            status,
            started=step_started,
            completed=completed,
            error=error,
        )

    step_index = 0
    while step_index < len(plan.steps):
        step = plan.steps[step_index]
        step_index += 1
        if not step.selected:
            continue
        step_started = aware_local(now)
        emit(step, "running", started=step_started)
        if step.source == "metadata":
            if metadata_store is None:
                error = "MetadataStore er ikke tilgængelig"
                metadata_result = MetadataRefresh("failed", None, error=error)
                finish(
                    step,
                    "failed_no_cache",
                    step_started=step_started,
                    error=error,
                )
                continue
            try:
                fetch = getattr(client, "fetch_game_info")
                fetched = metadata_store.save(
                    fetch(manager_game.game), fetched_at=started_at
                )
                changes = _metadata_changes(before_metadata, fetched)
                metadata_result = MetadataRefresh(
                    "success", fetched.fetched_at, changes
                )
                before_metadata = fetched
                plan = _revalidate_stale_plan_after_metadata(
                    plan,
                    groups,
                    before_teams,
                    before_players,
                    fetched,
                    now=step_started,
                )
                total_steps = completed_steps + 1 + sum(
                    item.selected and item.source != "metadata"
                    for item in plan.steps
                )
                finish(
                    step,
                    "fetched",
                    step_started=step_started,
                    data_reference=_metadata_reference(
                        metadata_store, manager_game.game
                    ),
                )
            except Exception as exc:
                error = str(exc)
                if metadata_load_error is not None:
                    error = (
                        f"{error}; lokal metadata-cache kunne ikke læses: "
                        f"{metadata_load_error}"
                    )
                if before_metadata is not None:
                    metadata_result = MetadataRefresh(
                        "cached_fallback", before_metadata.fetched_at, error=error
                    )
                    finish(
                        step,
                        "reused_after_error",
                        step_started=step_started,
                        cache_reference=_metadata_reference(
                            metadata_store, manager_game.game
                        ),
                        cache_generated_at=before_metadata.fetched_at,
                        error=error,
                    )
                else:
                    metadata_result = MetadataRefresh("failed", None, error=error)
                    finish(
                        step,
                        "failed_no_cache",
                        step_started=step_started,
                        error=error,
                    )
            continue
        if step.source == "players":
            try:
                statistics = client.fetch_players(manager_game.game)
                player_path = player_store.save(statistics, now=started_at)
                alert_snapshot_reference = _relative_reference(
                    player_store.snapshot_dir,
                    player_path,
                    "snapshots",
                )
                alert_count = 0
                alert_error = None
                if not plan.include_postprocess:
                    try:
                        alert_count = merge_player_alerts(
                            alert_snapshot_reference
                        )
                    except Exception as exc:
                        alert_error = f"Statusalarmer: {exc}"
                player_result = PlayerRefresh(
                    "success",
                    statistics.round_number,
                    player_path,
                    alert_count,
                    alert_error,
                )
                finish(
                    step,
                    "fetched",
                    step_started=step_started,
                    round_number=statistics.round_number,
                    data_reference=_relative_reference(
                        player_store.snapshot_dir,
                        player_path,
                        "snapshots",
                    ),
                    error=alert_error,
                )
            except Exception as exc:
                error = str(exc)
                cached_rounds = before_players.rounds_for(manager_game.game)
                previous = (
                    before_players.newest(manager_game.game, max(cached_rounds))
                    if cached_rounds
                    else None
                )
                player_result = PlayerRefresh(
                    "cached_fallback" if previous is not None else "failed",
                    None if previous is None else previous.statistics.round_number,
                    None if previous is None else previous.path,
                    error=error,
                )
                finish(
                    step,
                    "reused_after_error" if previous is not None else "failed_no_cache",
                    step_started=step_started,
                    round_number=(
                        None if previous is None else previous.statistics.round_number
                    ),
                    cache_reference=(
                        None
                        if previous is None
                        else _relative_reference(
                            player_store.snapshot_dir,
                            previous.path,
                            "snapshots",
                        )
                    ),
                    cache_generated_at=(
                        None if previous is None else previous.generated_at
                    ),
                    error=error,
                )
            continue
        if step.source == "team" and step.team_id is not None:
            member = members[step.team_id]
            try:
                team = client.fetch_team(member.reference(manager_game.game))
                path = snapshot_store.save_team_json(team, now=started_at)
                team_results.append(
                    TeamRefresh(step.team_id, team.team_name, "success", path, team)
                )
                finish(
                    step,
                    "fetched",
                    step_started=step_started,
                    round_number=team.overview.current_round,
                    data_reference=_relative_reference(
                        snapshot_store.output_dir, path, "snapshots"
                    ),
                )
            except Exception as exc:
                error = str(exc)
                cached = before_teams.newest(manager_game.game, step.team_id)
                if cached is not None:
                    team_results.append(
                        TeamRefresh(
                            step.team_id,
                            cached.team.team_name,
                            "cached_fallback",
                            cached.path,
                            cached.team,
                            error,
                        )
                    )
                    finish(
                        step,
                        "reused_after_error",
                        step_started=step_started,
                        round_number=cached.team.overview.current_round,
                        cache_reference=_relative_reference(
                            snapshot_store.output_dir,
                            cached.path,
                            "snapshots",
                        ),
                        cache_generated_at=cached.generated_at,
                        error=error,
                    )
                else:
                    team_results.append(
                        TeamRefresh(
                            step.team_id,
                            member.name,
                            "failed",
                            None,
                            None,
                            error,
                        )
                    )
                    finish(
                        step,
                        "failed_no_cache",
                        step_started=step_started,
                        error=error,
                    )
            continue
        if step.source == "postprocess":
            errors: list[str] = []
            alert_count = 0
            retrying_alerts = False
            if plan.mode == RefreshMode.RETRY_FAILED:
                prior_postprocess = next(
                    (
                        item
                        for item in (
                            ()
                            if plan.previous_manifest is None
                            else plan.previous_manifest.steps
                        )
                        if item.step_id == "postprocess"
                    ),
                    None,
                )
                retrying_alerts = bool(
                    prior_postprocess is not None
                    and prior_postprocess.error
                    and "Statusalarmer:" in prior_postprocess.error
                )
                if retrying_alerts and alert_snapshot_reference is None:
                    prior_players = next(
                        (
                            item
                            for item in (
                                ()
                                if plan.previous_manifest is None
                                else plan.previous_manifest.steps
                            )
                            if item.step_id == "players"
                        ),
                        None,
                    )
                    if prior_players is not None:
                        alert_snapshot_reference = (
                            prior_players.data_reference
                            or prior_players.cache_reference
                        )
            try:
                if alert_snapshot_reference is not None or retrying_alerts:
                    alert_count = merge_player_alerts(
                        alert_snapshot_reference
                    )
            except Exception as exc:
                errors.append(f"Statusalarmer: {exc}")
            if player_result is not None and alert_count:
                player_result = replace(
                    player_result,
                    alert_count=alert_count,
                )
            if postprocess is not None:
                try:
                    postprocess()
                except Exception as exc:
                    errors.append(str(exc))
            if not errors:
                finish(
                    step,
                    "fetched",
                    step_started=step_started,
                )
            else:
                finish(
                    step,
                    "failed_no_cache",
                    step_started=step_started,
                    error=" | ".join(errors),
                )
            continue

    after_teams = snapshot_store.scan()
    after_players = player_store.scan(manager_game.game)
    for step in plan.steps:
        if step.selected:
            continue
        previous_step = (
            None
            if plan.previous_manifest is None
            else next(
                (
                    item
                    for item in plan.previous_manifest.steps
                    if item.step_id == step.step_id
                ),
                None,
            )
        )
        carried_origin = (
            None
            if previous_step is None or plan.previous_manifest is None
            else previous_step.origin_run_id
            or plan.previous_manifest.origin_run_id
            or plan.previous_manifest.run_id
        )
        if step.source == "metadata":
            current_metadata = before_metadata
            manifest_steps[step.step_id] = RefreshManifestStep(
                step.step_id,
                step.source,
                step.label,
                "reused_current" if current_metadata is not None else "skipped_unavailable",
                False,
                reason=step.reason,
                cache_reference=(
                    None
                    if current_metadata is None or metadata_store is None
                    else _metadata_reference(metadata_store, manager_game.game)
                ),
                cache_generated_at=(
                    None if current_metadata is None else current_metadata.fetched_at
                ),
                origin_run_id=carried_origin or run_id,
            )
        elif step.source == "players":
            current_player = after_players.newest(manager_game.game)
            carried_reference = (
                None
                if previous_step is None
                else previous_step.data_reference
                or previous_step.cache_reference
            )
            carried_generated_at = (
                None
                if previous_step is None
                else previous_step.cache_generated_at
                or previous_step.completed_at
            )
            carry_previous = bool(
                plan.mode == RefreshMode.RETRY_FAILED
                and carried_reference is not None
                and carried_generated_at is not None
            )
            manifest_steps[step.step_id] = RefreshManifestStep(
                step.step_id,
                step.source,
                step.label,
                "reused_current" if current_player is not None else "skipped_unavailable",
                False,
                reason=step.reason,
                round_number=(
                    previous_step.round_number
                    if carry_previous and previous_step is not None
                    else None
                    if current_player is None
                    else current_player.statistics.round_number
                ),
                cache_reference=(
                    carried_reference
                    if carry_previous
                    else None
                    if current_player is None
                    else _relative_reference(
                        player_store.snapshot_dir,
                        current_player.path,
                        "snapshots",
                    )
                ),
                cache_generated_at=(
                    carried_generated_at
                    if carry_previous
                    else None
                    if current_player is None
                    else current_player.generated_at
                ),
                origin_run_id=carried_origin or run_id,
            )
        elif step.source == "team" and step.team_id is not None:
            if not step.available:
                manifest_steps[step.step_id] = RefreshManifestStep(
                    step.step_id,
                    step.source,
                    step.label,
                    "skipped_unavailable",
                    False,
                    reason=step.reason,
                    team_id=step.team_id,
                    team_name=step.team_name,
                    origin_run_id=carried_origin or run_id,
                )
                continue
            current_team = after_teams.newest(manager_game.game, step.team_id)
            manifest_steps[step.step_id] = RefreshManifestStep(
                step.step_id,
                step.source,
                step.label,
                "reused_current" if current_team is not None else "skipped_unavailable",
                False,
                reason=step.reason,
                team_id=step.team_id,
                team_name=step.team_name,
                round_number=(
                    None
                    if current_team is None
                    else current_team.team.overview.current_round
                ),
                cache_reference=(
                    None
                    if current_team is None
                    else _relative_reference(
                        snapshot_store.output_dir,
                        current_team.path,
                        "snapshots",
                    )
                ),
                cache_generated_at=(
                    None if current_team is None else current_team.generated_at
                ),
                origin_run_id=carried_origin or run_id,
            )
        elif step.source == "postprocess":
            previous_postprocess = (
                None
                if plan.previous_manifest is None
                else next(
                    (
                        item
                        for item in plan.previous_manifest.steps
                        if item.step_id == "postprocess"
                        and item.source == "postprocess"
                    ),
                    None,
                )
            )
            reusable = bool(
                previous_postprocess is not None
                and previous_postprocess.status in {"fetched", "reused_current"}
            )
            manifest_steps[step.step_id] = RefreshManifestStep(
                step.step_id,
                step.source,
                step.label,
                "reused_current" if reusable else "skipped_unavailable",
                False,
                reason=step.reason,
                cache_generated_at=(
                    None
                    if not reusable or plan.previous_manifest is None
                    else previous_postprocess.completed_at
                        or plan.previous_manifest.completed_at
                ),
                origin_run_id=carried_origin or run_id,
            )

    round_number = max(
        (
            *(item.team.overview.current_round for item in team_results if item.team),
            *(
                (player_result.round_number,)
                if player_result is not None and player_result.round_number is not None
                else ()
            ),
            plan.target_round,
        ),
        default=0,
    )
    group_states = tuple(
        _group_refresh_state(group, after_teams) for group in selected_groups
    )
    completed_at = aware_local(now)
    manifest = RefreshManifest(
        schema_version=2,
        scope="game",
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        game_slug=manager_game.game.slug,
        target_round=round_number,
        steps=tuple(manifest_steps[item.step_id] for item in plan.steps),
        mode=plan.mode.value,
        game_locale=manager_game.game.locale.casefold(),
        game_name=manager_game.name,
        game_url=manager_game.game.original,
        attempted_team_ids=plan.attempted_team_ids,
        skipped_team_ids=plan.skipped_team_ids,
        groups=group_states,
        retry_of=plan.retry_of,
        origin_run_id=plan.origin_run_id or run_id,
        metadata_changes=changes,
    )
    try:
        manifest_path = manifest_store.write(manifest)
    except Exception as exc:
        raise ManifestWriteError(manifest, exc) from exc
    manifest = replace(manifest, path=manifest_path.resolve())
    return GameRefreshResult(
        manager_game,
        round_number,
        started_at,
        tuple(team_results),
        plan.attempted_team_ids,
        plan.skipped_team_ids,
        manifest_path,
        player_result,
        metadata_result,
        plan,
        manifest,
    )


def refresh_group(
    group: GroupDefinition,
    client: HoldetClient,
    snapshot_store: SnapshotStore,
    manifest_store: ManifestStore,
    *,
    now: datetime | None = None,
) -> GroupRefreshResult:
    """Refresh every fixed member once and record the post-refresh state."""

    generated_at = aware_local(now)
    before = snapshot_store.scan()
    results: list[TeamRefresh] = []
    for member in group.teams:
        try:
            team = client.fetch_team(member.reference(group.game))
            path = snapshot_store.save_team_json(team, now=generated_at)
            results.append(
                TeamRefresh(member.team_id, team.team_name, "success", path, team)
            )
        except Exception as exc:
            cached = before.newest(group.game, member.team_id)
            if cached is not None:
                results.append(
                    TeamRefresh(
                        member.team_id,
                        cached.team.team_name,
                        "cached_fallback",
                        cached.path,
                        cached.team,
                        str(exc),
                    )
                )
            else:
                results.append(
                    TeamRefresh(
                        member.team_id,
                        member.name,
                        "failed",
                        None,
                        None,
                        str(exc),
                    )
                )
    after = snapshot_store.scan()
    round_number = max(
        (
            snapshot.team.overview.current_round
            for member in group.teams
            if (snapshot := after.newest(group.game, member.team_id)) is not None
        ),
        default=0,
    )
    post_state = _group_refresh_state(group, after)
    manifest = {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "group": {
            "id": group.group_id,
            "name": group.name,
            "game_slug": group.game.slug,
            "type": group.kind,
            "revision": (
                group.active_revision if group.kind == "tournament" else None
            ),
        },
        "phase": post_state["phase"],
        "round": round_number,
        "latest_round": post_state["latest_round"],
        "active_team_ids": post_state["active_team_ids"],
        "eliminated_team_ids": post_state["eliminated_team_ids"],
        "champion_id": post_state["champion_id"],
        "champion_name": post_state["champion_name"],
        "attempted_team_ids": [member.team_id for member in group.teams],
        "skipped_team_ids": [],
        "teams": [
            {
                "team_id": item.team_id,
                "team_name": item.team_name,
                "status": item.status,
                "snapshot_path": (
                    str(item.snapshot_path) if item.snapshot_path else None
                ),
                "error": item.error,
            }
            for item in results
        ],
    }
    manifest_path = manifest_store.save_manifest(
        group.game.slug,
        group.group_id,
        round_number,
        manifest,
        now=generated_at,
    )
    return GroupRefreshResult(
        group, round_number, generated_at, tuple(results), manifest_path
    )
