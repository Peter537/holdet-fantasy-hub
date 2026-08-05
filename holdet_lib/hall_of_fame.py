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
    build_effective_manager_settings,
    effective_manager_profiles,
    manager_identity_keys,
    resolve_manager_identity,
)
from .persistence import aware_local, publish_immutable_text
from .standings import build_standings
from .storage import SnapshotIndex
from .tournament import build_tournament_state, resolve_knockout_tie


HALL_OF_FAME_EVENT_SCHEMA_VERSION = 2
EventKind = Literal["group", "tournament_group", "tournament", "round_win"]


@dataclass(frozen=True, slots=True)
class HallOfFamePlacement:
    manager_id: str
    manager_name: str
    team_id: int
    team_name: str
    rank: int
    value: int | None = None
    identity_keys: tuple[str, ...] = ()


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
    revision: int = 1
    supersedes_revision: int | None = None
    competition_revision: int = 1
    match_id: str | None = None

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
            fallback_key=f"{group.game.locale}:{group.game.slug}:team:{team_id}",
        )
    team = snapshot.team
    return resolve_manager_identity(
        settings,
        owner_user_id=team.owner_user_id,
        account_user_id=team.reference.account_user_id,
        account_key=team.reference.account_key,
        owner_name=team.owner_name,
        fallback_key=f"{group.game.locale}:{group.game.slug}:team:{team_id}",
    )



def _manager_keys_for_team(
    snapshots: SnapshotIndex,
    group: GroupDefinition,
    team_id: int,
) -> tuple[str, ...]:
    snapshot = snapshots.newest(group.game, team_id)
    member = next((item for item in group.teams if item.team_id == team_id), None)
    if snapshot is None:
        return manager_identity_keys(
            owner_user_id=None,
            account_user_id=None if member is None else member.account_user_id,
            account_key="" if member is None else member.account_key,
            owner_name="" if member is None else member.account_label,
        )
    team = snapshot.team
    return manager_identity_keys(
        owner_user_id=team.owner_user_id,
        account_user_id=(
            team.reference.account_user_id
            if team.reference.account_user_id is not None
            else None if member is None else member.account_user_id
        ),
        account_key=(
            team.reference.account_key
            or ("" if member is None else member.account_key)
        ),
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
    placements = tuple(
        replace(
            item,
            identity_keys=_manager_keys_for_team(
                snapshots,
                group,
                item.team_id,
            ),
        )
        for item in placements
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
    finals = tuple(
        match for match in state.knockout_matches if match.stage == "Finale"
    )
    final = finals[-1] if finals else None
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
    if final is None and state.champion_id is not None:
        for rank, standing in enumerate(state.standings[:3], 1):
            manager_id, manager_name = _manager_for_team(
                snapshots, group, standing.team_id, settings
            )
            placements.append(
                HallOfFamePlacement(
                    manager_id,
                    manager_name,
                    standing.team_id,
                    standing.team_name,
                    rank,
                    standing.points,
                )
            )
    bronze = next(
        (match for match in state.knockout_matches if match.stage == "Bronzekamp"),
        None,
    )
    bronze_team_id = (
        state.standings[2].team_id
        if group.tournament.template == "double_elimination"
        and state.champion_id is not None
        and len(state.standings) >= 3
        else bronze.winner_id if bronze is not None and bronze.complete else None
    )
    if bronze is None and group.tournament.template != "double_elimination":
        semifinal_losers: list[tuple[int, int]] = []
        for match in state.knockout_matches:
            if match.stage != "Semifinaler" or match.winner_id is None:
                continue
            loser = (
                match.team_b_id
                if match.winner_id == match.team_a_id
                else match.team_a_id
            )
            loser_seed = (
                match.team_b_seed
                if match.winner_id == match.team_a_id
                else match.team_a_seed
            )
            if loser is not None:
                semifinal_losers.append((loser_seed or 10**9, loser))
        if len(semifinal_losers) == 2:
            (a_seed, a_id), (b_seed, b_id) = semifinal_losers
            semifinal_round = max(
                match.round_numbers[-1]
                for match in state.knockout_matches
                if match.stage == "Semifinaler"
            )
            bronze_team_id = resolve_knockout_tie(
                group.tournament,
                snapshots,
                group.game,
                a_id,
                b_id,
                a_seed,
                b_seed,
                semifinal_round,
            )
        elif semifinal_losers:
            bronze_team_id = semifinal_losers[0][1]
    if bronze_team_id is not None:
        manager_id, manager_name = _manager_for_team(
            snapshots, group, bronze_team_id, settings
        )
        name = next(
            (item.name for item in group.teams if item.team_id == bronze_team_id),
            str(bronze_team_id),
        )
        placements.append(
            HallOfFamePlacement(
                manager_id, manager_name, bronze_team_id, name, 3
            )
        )
    placements = [
        replace(
            item,
            identity_keys=_manager_keys_for_team(
                snapshots,
                group,
                item.team_id,
            ),
        )
        for item in placements
    ]
    complete = (
        state.champion_id is not None
        and bool(placements)
        and (
            (
                final is not None
                and final.complete
                and (bronze is None or bronze.complete)
            )
            or final is None
        )
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
    settings = build_effective_manager_settings(settings, groups, snapshots)
    events: list[HallOfFameEvent] = []
    for group in groups:
        if group.kind == "tournament" and group.tournament is not None:
            if group.tournament.template == "group_knockout":
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
                        _manager_keys_for_team(
                            snapshots,
                            group,
                            team_id,
                        ),
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



def remap_manager_events(
    events: tuple[HallOfFameEvent, ...],
    settings: HubSettings,
) -> tuple[HallOfFameEvent, ...]:
    """Map immutable placements through the current manager profiles."""

    profiles = effective_manager_profiles(settings)
    by_identity = {
        key: profile
        for profile in profiles
        for key in (*profile.identity_keys, profile.manager_id)
    }
    remapped: list[HallOfFameEvent] = []
    for event in events:
        placements: list[HallOfFamePlacement] = []
        for placement in event.placements:
            profile = next(
                (
                    by_identity[key]
                    for key in (*placement.identity_keys, placement.manager_id)
                    if key in by_identity
                ),
                None,
            )
            placements.append(
                placement
                if profile is None
                else replace(
                    placement,
                    manager_id=profile.manager_id,
                    manager_name=profile.display_name,
                )
            )
        remapped.append(
            replace(event, placements=_deduplicate_placements(tuple(placements)))
        )
    return tuple(remapped)


def build_hall_of_fame(
    events: tuple[HallOfFameEvent, ...],
    score_profile: HallOfFameScoreProfile | None = None,
    *,
    include_incomplete: bool = False,
    settings: HubSettings | None = None,
) -> HallOfFame:
    """Recompute the global leaderboard from frozen raw placements."""

    score = score_profile or HallOfFameScoreProfile()
    if settings is not None:
        events = remap_manager_events(events, settings)
    selected = tuple(event for event in events if event.complete or include_incomplete)
    selected = current_event_revisions(selected)
    names: dict[str, str] = {}
    points: dict[str, int] = {}
    titles: dict[str, int] = {}
    podiums: dict[str, int] = {}
    competitions: dict[str, int] = {}
    wins: dict[str, int] = {}
    best_round: dict[str, int] = {}
    round_wins: dict[tuple[str, str, str], set[int]] = {}
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
                round_wins.setdefault(
                    (manager, event.game_locale.casefold(), event.game_slug), set()
                ).add(
                    event.round_number
                )

    def streak(manager: str) -> int:
        longest = 0
        for (candidate, _, _), rounds in round_wins.items():
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
        "revision": event.revision,
        "supersedes_revision": event.supersedes_revision,
        "competition_revision": event.competition_revision,
        "match_id": event.match_id,
        "placements": [
            {
                "manager_id": item.manager_id,
                "manager_name": item.manager_name,
                "team_id": item.team_id,
                "team_name": item.team_name,
                "rank": item.rank,
                "value": item.value,
                "identity_keys": list(item.identity_keys),
            }
            for item in event.placements
        ],
    }


def _event_from_dict(payload: object) -> HallOfFameEvent:
    event_schema_version = payload.get("schema_version") if isinstance(payload, dict) else None
    if event_schema_version == HALL_OF_FAME_EVENT_SCHEMA_VERSION:
        payload = dict(payload)
        payload["schema_version"] = 1
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise PayloadError("Ukendt skema for Hall of Fame-resultat")
    game = payload.get("game")
    competition = payload.get("competition")
    placements = payload.get("placements")
    if not isinstance(game, dict) or not isinstance(competition, dict) or not isinstance(placements, list):
        raise PayloadError("Hall of Fame-resultatet mangler felter")
    kind = payload.get("kind")
    if kind not in {"group", "tournament_group", "tournament", "round_win"}:
        raise PayloadError("Ukendt Hall of Fame-resultattype")
    parsed: list[HallOfFamePlacement] = []
    for raw in placements:
        if not isinstance(raw, dict):
            raise PayloadError("Ugyldig Hall of Fame-placering")
        manager_id = str(raw["manager_id"])
        identity_keys = raw.get("identity_keys", [manager_id])
        if not isinstance(identity_keys, list) or not all(
            isinstance(value, str) and value.strip()
            for value in identity_keys
        ):
            raise PayloadError("Managerplaceringens identitetsn\u00f8gler er ugyldige")
        parsed.append(
            HallOfFamePlacement(
                manager_id,
                str(raw["manager_name"]),
                int(raw["team_id"]),
                str(raw["team_name"]),
                int(raw["rank"]),
                int(raw["value"]) if raw.get("value") is not None else None,
                tuple(dict.fromkeys(value.strip() for value in identity_keys)),
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
        revision=int(payload.get("revision", 1)),
        supersedes_revision=(
            int(payload["supersedes_revision"])
            if payload.get("supersedes_revision") is not None
            else None
        ),
        competition_revision=int(payload.get("competition_revision", 1)),
        match_id=(
            str(payload["match_id"])
            if payload.get("match_id") is not None
            else None
        ),
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
        if frozen.revision > 1:
            path = self.directory / f"{frozen.event_id}-r{frozen.revision}.json"
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
        published: list[Path] = []
        for event in events:
            try:
                path = self.freeze(event)
            except PayloadError:
                path = self.freeze_revision(event)
            if path is not None:
                published.append(path)
        return tuple(published)

    def freeze_revision(self, event: HallOfFameEvent) -> Path | None:
        """Append a correction without overwriting a prior complete result."""

        existing, _ = self.scan()
        revisions = tuple(
            item for item in existing if item.event_id == event.event_id
        )
        if not revisions:
            return self.freeze(event)
        current = max(revisions, key=lambda item: item.revision)
        comparable = replace(
            event,
            source=current.source,
            revision=current.revision,
            supersedes_revision=current.supersedes_revision,
            captured_at=current.captured_at,
        )
        if current == comparable:
            suffix = "" if current.revision == 1 else f"-r{current.revision}"
            return self.directory / f"{current.event_id}{suffix}.json"
        corrected = replace(
            event,
            revision=current.revision + 1,
            supersedes_revision=current.revision,
        )
        return self.freeze(corrected)

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



def current_event_revisions(
    events: tuple[HallOfFameEvent, ...],
) -> tuple[HallOfFameEvent, ...]:
    """Select the latest non-superseded revision for each logical event."""

    latest: dict[str, HallOfFameEvent] = {}
    for event in events:
        previous = latest.get(event.event_id)
        if previous is None or event.revision > previous.revision:
            latest[event.event_id] = event
    return tuple(
        sorted(latest.values(), key=lambda item: (item.captured_at, item.event_id))
    )
