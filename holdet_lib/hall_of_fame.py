"""Immutable Hall of Fame events and score recalculation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Literal

from .errors import PayloadError
from .groups import GroupDefinition
from .hub_settings import (
    HallOfFameScoreProfile,
    HubSettings,
    resolve_manager_identity,
)
from .persistence import aware_local, publish_immutable_text
from .standings import build_standings
from .storage import SnapshotIndex
from .tournament import build_tournament_state


HALL_OF_FAME_EVENT_SCHEMA_VERSION = 1
EventKind = Literal["group", "tournament_group", "tournament", "round_win"]


@dataclass(frozen=True, slots=True)
class HallOfFamePlacement:
    manager_id: str
    manager_name: str
    team_id: int
    team_name: str
    rank: int
    value: int | None = None


@dataclass(frozen=True, slots=True)
class HallOfFameEvent:
    event_id: str
    kind: EventKind
    game_locale: str
    game_slug: str
    competition_id: str
    competition_name: str
    round_number: int | None
    placements: tuple[HallOfFamePlacement, ...]
    complete: bool
    captured_at: datetime
    source: str = "live"

    @property
    def game_identity(self) -> tuple[str, str]:
        return self.game_locale.casefold(), self.game_slug


@dataclass(frozen=True, slots=True)
class HallOfFameRow:
    rank: int
    manager_id: str
    manager_name: str
    points: int
    titles: int
    podiums: int
    competitions: int
    wins: int
    win_rate: float
    best_round: int | None
    longest_round_win_streak: int


@dataclass(frozen=True, slots=True)
class HallOfFame:
    rows: tuple[HallOfFameRow, ...]
    events: tuple[HallOfFameEvent, ...]
    warnings: tuple[str, ...] = ()


def _event_id(
    kind: str, game_locale: str, game_slug: str, competition_id: str, round_number: int | None
) -> str:
    raw = "|".join(
        (kind, game_locale.casefold(), game_slug, competition_id, str(round_number or ""))
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _manager_for_team(
    snapshots: SnapshotIndex,
    group: GroupDefinition,
    team_id: int,
    settings: HubSettings,
) -> tuple[str, str]:
    snapshot = snapshots.newest(group.game, team_id)
    member = next((item for item in group.teams if item.team_id == team_id), None)
    if snapshot is None:
        return resolve_manager_identity(
            settings,
            owner_user_id=None,
            account_user_id=None if member is None else member.account_user_id,
            account_key="" if member is None else member.account_key,
            owner_name="" if member is None else member.account_label,
        )
    team = snapshot.team
    return resolve_manager_identity(
        settings,
        owner_user_id=team.owner_user_id,
        account_user_id=team.reference.account_user_id,
        account_key=team.reference.account_key,
        owner_name=team.owner_name,
    )


def _deduplicate_placements(
    placements: tuple[HallOfFamePlacement, ...],
) -> tuple[HallOfFamePlacement, ...]:
    best: dict[str, HallOfFamePlacement] = {}
    for item in placements:
        previous = best.get(item.manager_id)
        if previous is None or (
            item.rank,
            -(item.value if item.value is not None else -10**18),
            item.team_id,
        ) < (
            previous.rank,
            -(previous.value if previous.value is not None else -10**18),
            previous.team_id,
        ):
            best[item.manager_id] = item
    ranked = sorted(
        best.values(),
        key=lambda item: (
            item.rank,
            -(item.value if item.value is not None else -10**18),
            item.manager_name.casefold(),
        ),
    )
    result: list[HallOfFamePlacement] = []
    previous_source_rank: int | None = None
    previous_rank = 0
    for position, item in enumerate(ranked, 1):
        rank = previous_rank if item.rank == previous_source_rank else position
        result.append(replace(item, rank=rank))
        previous_source_rank = item.rank
        previous_rank = rank
    return tuple(result)


def _standing_event(
    group: GroupDefinition,
    snapshots: SnapshotIndex,
    settings: HubSettings,
    *,
    round_number: int,
    kind: EventKind,
    captured_at: datetime,
) -> HallOfFameEvent:
    standings = build_standings(group, snapshots, round_number, "overall")
    placements = tuple(
        HallOfFamePlacement(
            *_manager_for_team(snapshots, group, row.team_id, settings),
            row.team_id,
            row.team_name,
            row.rank or len(group.teams) + 1,
            row.value,
        )
        for row in standings
        if row.value is not None
    )
    complete = bool(placements) and len(placements) == len(group.teams) and all(
        row.summary is not None and row.summary.round_status == "complete"
        for row in standings
    )
    competition_id = (
        f"{group.group_id}:group-stage"
        if kind == "tournament_group"
        else group.group_id
    )
    name = (
        f"{group.name} - gruppespil"
        if kind == "tournament_group"
        else group.name
    )
    return HallOfFameEvent(
        _event_id(kind, group.game.locale, group.game.slug, competition_id, round_number),
        kind,
        group.game.locale,
        group.game.slug,
        competition_id,
        name,
        round_number,
        _deduplicate_placements(placements),
        complete,
        captured_at,
    )


def _tournament_event(
    group: GroupDefinition,
    snapshots: SnapshotIndex,
    settings: HubSettings,
    *,
    captured_at: datetime,
) -> HallOfFameEvent:
    assert group.tournament is not None
    state = build_tournament_state(group, snapshots, group.tournament.final_round)
    placements: list[HallOfFamePlacement] = []
    final = next(
        (match for match in state.knockout_matches if match.stage == "Finale"),
        None,
    )
    if final is not None and final.winner_id is not None:
        loser = final.team_b_id if final.winner_id == final.team_a_id else final.team_a_id
        for rank, team_id in ((1, final.winner_id), (2, loser)):
            if team_id is None:
                continue
            manager_id, manager_name = _manager_for_team(
                snapshots, group, team_id, settings
            )
            name = next(
                (item.name for item in group.teams if item.team_id == team_id),
                str(team_id),
            )
            placements.append(
                HallOfFamePlacement(manager_id, manager_name, team_id, name, rank)
            )
    for match in state.knockout_matches:
        if match.stage != "Semifinaler" or match.winner_id is None:
            continue
        loser = match.team_b_id if match.winner_id == match.team_a_id else match.team_a_id
        if loser is None:
            continue
        manager_id, manager_name = _manager_for_team(
            snapshots, group, loser, settings
        )
        name = next(
            (item.name for item in group.teams if item.team_id == loser),
            str(loser),
        )
        placements.append(
            HallOfFamePlacement(manager_id, manager_name, loser, name, 3)
        )
    complete = (
        state.champion_id is not None
        and final is not None
        and final.complete
        and bool(placements)
    )
    return HallOfFameEvent(
        _event_id(
            "tournament",
            group.game.locale,
            group.game.slug,
            group.group_id,
            group.tournament.final_round,
        ),
        "tournament",
        group.game.locale,
        group.game.slug,
        group.group_id,
        group.name,
        group.tournament.final_round,
        _deduplicate_placements(tuple(placements)),
        complete,
        captured_at,
    )


def build_live_hall_of_fame_events(
    groups: tuple[GroupDefinition, ...],
    snapshots: SnapshotIndex,
    settings: HubSettings,
    *,
    final_rounds: dict[tuple[str, str], int] | None = None,
    now: datetime | None = None,
) -> tuple[HallOfFameEvent, ...]:
    """Build current raw results without freezing or writing them."""

    captured = aware_local(now)
    final_rounds = final_rounds or {}
    events: list[HallOfFameEvent] = []
    for group in groups:
        if group.kind == "tournament" and group.tournament is not None:
            events.append(
                _standing_event(
                    group,
                    snapshots,
                    settings,
                    round_number=group.tournament.group_end_round,
                    kind="tournament_group",
                    captured_at=captured,
                )
            )
            events.append(
                _tournament_event(
                    group, snapshots, settings, captured_at=captured
                )
            )
        else:
            final_round = final_rounds.get(
                (group.game.locale.casefold(), group.game.slug)
            )
            if final_round is None:
                rounds = snapshots.rounds_for(
                    group.game, tuple(item.team_id for item in group.teams)
                )
                final_round = max(rounds, default=0)
            if final_round:
                events.append(
                    _standing_event(
                        group,
                        snapshots,
                        settings,
                        round_number=final_round,
                        kind="group",
                        captured_at=captured,
                    )
                )

    game_teams: dict[tuple[str, str], dict[int, GroupDefinition]] = {}
    for group in groups:
        identity = (group.game.locale.casefold(), group.game.slug)
        for member in group.teams:
            game_teams.setdefault(identity, {}).setdefault(member.team_id, group)
    for identity, members in game_teams.items():
        sample_group = next(iter(members.values()))
        rounds = snapshots.rounds_for(sample_group.game, tuple(members))
        for round_number in rounds:
            placements: list[HallOfFamePlacement] = []
            all_complete = True
            for team_id, group in members.items():
                located = snapshots.summary_for(group.game, team_id, round_number)
                if (
                    located is None
                    or located[1].round_status != "complete"
                ):
                    all_complete = False
                    continue
                snapshot, summary = located
                manager_id, manager_name = _manager_for_team(
                    snapshots, group, team_id, settings
                )
                placements.append(
                    HallOfFamePlacement(
                        manager_id,
                        manager_name,
                        team_id,
                        snapshot.team.team_name,
                        0,
                        summary.change,
                    )
                )
            deduplicated = _deduplicate_placements(
                tuple(replace(item, rank=1) for item in placements)
            )
            if deduplicated:
                best_value = max(
                    item.value for item in deduplicated if item.value is not None
                )
                winners = tuple(
                    replace(item, rank=1)
                    for item in deduplicated
                    if item.value == best_value
                )
            else:
                winners = ()
            events.append(
                HallOfFameEvent(
                    _event_id(
                        "round_win",
                        sample_group.game.locale,
                        sample_group.game.slug,
                        f"round:{round_number}",
                        round_number,
                    ),
                    "round_win",
                    sample_group.game.locale,
                    sample_group.game.slug,
                    f"round:{round_number}",
                    f"Rundesejr - runde {round_number}",
                    round_number,
                    winners,
                    all_complete and bool(winners),
                    captured,
                )
            )
    return tuple(events)


def _points(event: HallOfFameEvent, rank: int, score: HallOfFameScoreProfile) -> int:
    if event.kind in {"group", "tournament_group"}:
        return score.group_points[rank - 1] if 1 <= rank <= 4 else 0
    if event.kind == "tournament":
        return {
            1: score.tournament_winner,
            2: score.tournament_finalist,
            3: score.tournament_semifinalist,
        }.get(rank, 0)
    return score.global_round_win if rank == 1 else 0


def build_hall_of_fame(
    events: tuple[HallOfFameEvent, ...],
    score_profile: HallOfFameScoreProfile | None = None,
    *,
    include_incomplete: bool = False,
) -> HallOfFame:
    """Recompute the global leaderboard from frozen raw placements."""

    score = score_profile or HallOfFameScoreProfile()
    selected = tuple(event for event in events if event.complete or include_incomplete)
    names: dict[str, str] = {}
    points: dict[str, int] = {}
    titles: dict[str, int] = {}
    podiums: dict[str, int] = {}
    competitions: dict[str, int] = {}
    wins: dict[str, int] = {}
    best_round: dict[str, int] = {}
    round_wins: dict[tuple[str, str], set[int]] = {}
    for event in selected:
        for placement in _deduplicate_placements(event.placements):
            manager = placement.manager_id
            names[manager] = placement.manager_name
            points[manager] = points.get(manager, 0) + _points(event, placement.rank, score)
            if event.kind != "round_win":
                competitions[manager] = competitions.get(manager, 0) + 1
                if placement.rank == 1:
                    titles[manager] = titles.get(manager, 0) + 1
                    wins[manager] = wins.get(manager, 0) + 1
                if placement.rank <= 3:
                    podiums[manager] = podiums.get(manager, 0) + 1
            elif placement.rank == 1 and event.round_number is not None:
                best_round[manager] = max(
                    best_round.get(manager, placement.value or 0),
                    placement.value or 0,
                )
                round_wins.setdefault((manager, event.game_slug), set()).add(
                    event.round_number
                )

    def streak(manager: str) -> int:
        longest = 0
        for (candidate, _), rounds in round_wins.items():
            if candidate != manager:
                continue
            current = 0
            previous: int | None = None
            for round_number in sorted(rounds):
                current = current + 1 if previous is not None and round_number == previous + 1 else 1
                longest = max(longest, current)
                previous = round_number
        return longest

    provisional = sorted(
        names,
        key=lambda manager: (
            -points.get(manager, 0),
            -titles.get(manager, 0),
            -podiums.get(manager, 0),
            names[manager].casefold(),
            manager,
        ),
    )
    rows: list[HallOfFameRow] = []
    previous_score: tuple[int, int, int] | None = None
    previous_rank = 0
    for position, manager in enumerate(provisional, 1):
        tie = (
            points.get(manager, 0),
            titles.get(manager, 0),
            podiums.get(manager, 0),
        )
        rank = previous_rank if tie == previous_score else position
        competition_count = competitions.get(manager, 0)
        win_count = wins.get(manager, 0)
        rows.append(
            HallOfFameRow(
                rank,
                manager,
                names[manager],
                points.get(manager, 0),
                titles.get(manager, 0),
                podiums.get(manager, 0),
                competition_count,
                win_count,
                win_count / competition_count if competition_count else 0.0,
                best_round.get(manager),
                streak(manager),
            )
        )
        previous_score = tie
        previous_rank = rank
    return HallOfFame(tuple(rows), selected)


def _event_to_dict(event: HallOfFameEvent) -> dict[str, object]:
    return {
        "schema_version": HALL_OF_FAME_EVENT_SCHEMA_VERSION,
        "event_id": event.event_id,
        "kind": event.kind,
        "game": {"locale": event.game_locale, "slug": event.game_slug},
        "competition": {
            "id": event.competition_id,
            "name": event.competition_name,
        },
        "round": event.round_number,
        "complete": event.complete,
        "captured_at": event.captured_at.isoformat(),
        "source": event.source,
        "placements": [
            {
                "manager_id": item.manager_id,
                "manager_name": item.manager_name,
                "team_id": item.team_id,
                "team_name": item.team_name,
                "rank": item.rank,
                "value": item.value,
            }
            for item in event.placements
        ],
    }


def _event_from_dict(payload: object) -> HallOfFameEvent:
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise PayloadError("ukendt schema for Hall of Fame-resultat")
    game = payload.get("game")
    competition = payload.get("competition")
    placements = payload.get("placements")
    if not isinstance(game, dict) or not isinstance(competition, dict) or not isinstance(placements, list):
        raise PayloadError("Hall of Fame-resultatet mangler felter")
    kind = payload.get("kind")
    if kind not in {"group", "tournament_group", "tournament", "round_win"}:
        raise PayloadError("ukendt Hall of Fame-resultattype")
    parsed: list[HallOfFamePlacement] = []
    for raw in placements:
        if not isinstance(raw, dict):
            raise PayloadError("ugyldig Hall of Fame-placering")
        parsed.append(
            HallOfFamePlacement(
                str(raw["manager_id"]),
                str(raw["manager_name"]),
                int(raw["team_id"]),
                str(raw["team_name"]),
                int(raw["rank"]),
                int(raw["value"]) if raw.get("value") is not None else None,
            )
        )
    captured = datetime.fromisoformat(str(payload["captured_at"]))
    if captured.tzinfo is None:
        captured = captured.astimezone()
    return HallOfFameEvent(
        str(payload["event_id"]),
        kind,
        str(game["locale"]),
        str(game["slug"]),
        str(competition["id"]),
        str(competition["name"]),
        int(payload["round"]) if payload.get("round") is not None else None,
        tuple(parsed),
        bool(payload.get("complete")),
        captured,
        str(payload.get("source") or "frozen"),
    )


class HallOfFameStore:
    """Publish complete raw events exactly once and scan them later."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def freeze(self, event: HallOfFameEvent) -> Path | None:
        if not event.complete:
            return None
        frozen = replace(event, source="frozen")
        path = self.directory / f"{frozen.event_id}.json"
        content = json.dumps(_event_to_dict(frozen), ensure_ascii=False, indent=2) + "\n"
        if path.exists():
            existing = _event_from_dict(json.loads(path.read_text(encoding="utf-8")))
            comparable = replace(frozen, captured_at=existing.captured_at)
            if existing != comparable:
                raise PayloadError("Hall of Fame-resultatet er allerede frosset med andre data")
            return path
        try:
            publish_immutable_text(path, content)
        except FileExistsError:
            return self.freeze(frozen)
        return path

    def freeze_complete(
        self, events: tuple[HallOfFameEvent, ...]
    ) -> tuple[Path, ...]:
        return tuple(
            path
            for event in events
            if (path := self.freeze(event)) is not None
        )

    def scan(self) -> tuple[tuple[HallOfFameEvent, ...], tuple[str, ...]]:
        if not self.directory.exists():
            return (), ()
        events: list[HallOfFameEvent] = []
        warnings: list[str] = []
        for path in self.directory.glob("*.json"):
            try:
                events.append(
                    _event_from_dict(json.loads(path.read_text(encoding="utf-8")))
                )
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, PayloadError) as exc:
                warnings.append(f"{path}: {exc}")
        events.sort(key=lambda item: (item.captured_at, item.event_id))
        return tuple(events), tuple(warnings)

