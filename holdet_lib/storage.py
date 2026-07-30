"""Explicit immutable snapshot storage and cached snapshot indexing."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
from pathlib import Path

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
                    raise PayloadError("snapshot root must be an object")
                generated_raw = payload.get("generated_at")
                if not isinstance(generated_raw, str):
                    raise PayloadError("snapshot generated_at must be text")
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
                    raise PayloadError("player snapshot root must be an object")
                generated_raw = payload.get("generated_at")
                if not isinstance(generated_raw, str):
                    raise PayloadError("player snapshot generated_at must be text")
                generated_at = datetime.fromisoformat(generated_raw)
                if generated_at.tzinfo is None:
                    generated_at = generated_at.astimezone()
                statistics = player_statistics_from_dict(payload)
                if game is not None and (
                    statistics.game.locale.casefold(), statistics.game.slug
                ) != (game.locale.casefold(), game.slug):
                    raise PayloadError("player snapshot belongs to another game")
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
            raise PayloadError("player statistics round must be non-negative")
        if not statistics.entries:
            raise PayloadError("refusing to save empty player statistics")
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


class ManifestStore:
    """Explicitly publish immutable group-refresh manifests."""

    def __init__(self, manifest_dir: Path | str) -> None:
        self.manifest_dir = Path(manifest_dir)

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
