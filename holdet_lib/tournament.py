"""Pure scheduling and result calculation for group-to-knockout tournaments."""

from __future__ import annotations

from dataclasses import dataclass
from math import log2
import random
import secrets
from typing import Callable, TYPE_CHECKING

from .errors import PayloadError
from .models import GameUrl
from .storage import SnapshotIndex

if TYPE_CHECKING:
    from .groups import GroupDefinition


STAGE_NAMES = {
    32: "Sekstendedelsfinaler",
    16: "Ottendedelsfinaler",
    8: "Kvartfinaler",
    4: "Semifinaler",
    2: "Finale",
}


@dataclass(frozen=True, slots=True)
class GroupFixture:
    round_number: int
    team_a_id: int
    team_b_id: int | None

    @property
    def is_bye(self) -> bool:
        return self.team_b_id is None


@dataclass(frozen=True, slots=True)
class TournamentConfig:
    start_round: int
    final_round: int
    rounds_per_tie: int
    knockout_size: int
    group_fixtures: tuple[GroupFixture, ...]
    draw_seed: str | None = None

    @property
    def knockout_stage_count(self) -> int:
        return int(log2(self.knockout_size))

    @property
    def group_end_round(self) -> int:
        return self.final_round - self.knockout_stage_count * self.rounds_per_tie


@dataclass(frozen=True, slots=True)
class GroupMatch:
    fixture: GroupFixture
    team_a_name: str
    team_b_name: str | None
    team_a_change: int | None
    team_b_change: int | None
    winner_id: int | None
    complete: bool


@dataclass(frozen=True, slots=True)
class TournamentStanding:
    rank: int
    team_id: int
    team_name: str
    owner_name: str
    played: int
    wins: int
    draws: int
    losses: int
    growth_for: int
    growth_against: int
    growth_difference: int
    points: int


@dataclass(frozen=True, slots=True)
class KnockoutMatch:
    stage: str
    match_index: int
    round_numbers: tuple[int, ...]
    team_a_id: int | None
    team_b_id: int | None
    team_a_name: str | None
    team_b_name: str | None
    team_a_seed: int | None
    team_b_seed: int | None
    team_a_change: int | None
    team_b_change: int | None
    winner_id: int | None
    complete: bool


@dataclass(frozen=True, slots=True)
class TournamentState:
    as_of_round: int
    phase: str
    group_matches: tuple[GroupMatch, ...]
    standings: tuple[TournamentStanding, ...]
    knockout_matches: tuple[KnockoutMatch, ...]
    active_team_ids: frozenset[int]
    eliminated_team_ids: frozenset[int]
    champion_id: int | None
    warnings: tuple[str, ...]

    @property
    def next_matches(self) -> tuple[GroupMatch | KnockoutMatch, ...]:
        pending: list[GroupMatch | KnockoutMatch] = [
            match
            for match in (*self.group_matches, *self.knockout_matches)
            if not match.complete
            and (
                isinstance(match, KnockoutMatch)
                or not match.fixture.is_bye
            )
        ]
        if not pending:
            return ()
        def first_round(match: GroupMatch | KnockoutMatch) -> int:
            return (
                match.fixture.round_number
                if isinstance(match, GroupMatch)
                else match.round_numbers[0]
            )
        earliest = min(first_round(match) for match in pending)
        return tuple(match for match in pending if first_round(match) == earliest)


@dataclass(frozen=True, slots=True)
class HeadToHeadMatch:
    round_number: int
    phase: str
    team_a_id: int
    team_b_id: int
    team_a_name: str
    team_b_name: str
    team_a_change: int | None
    team_b_change: int | None
    winner_id: int | None
    complete: bool
    advanced_by_seed_id: int | None = None


@dataclass(frozen=True, slots=True)
class HeadToHeadSummary:
    team_a_id: int
    team_b_id: int
    team_a_name: str
    team_b_name: str
    matches: tuple[HeadToHeadMatch, ...]
    played: int
    team_a_wins: int
    draws: int
    team_b_wins: int
    team_a_growth: int
    team_b_growth: int

    @property
    def growth_difference(self) -> int:
        return self.team_a_growth - self.team_b_growth


def knockout_size_for(team_count: int) -> int:
    if team_count < 2:
        raise PayloadError("en turnering kræver mindst to hold")
    size = 2
    while size * 2 <= team_count and size < 32:
        size *= 2
    return size


def bracket_seed_order(size: int) -> tuple[int, ...]:
    if size < 2 or size > 32 or size & (size - 1):
        raise PayloadError("knockoutstørrelsen skal være 2, 4, 8, 16 eller 32")
    order = [1, 2]
    current = 2
    while current < size:
        current *= 2
        order = [value for seed in order for value in (seed, current + 1 - seed)]
    return tuple(order)


def _validate_rounds(
    team_count: int, start_round: int, final_round: int, rounds_per_tie: int
) -> tuple[int, int]:
    if start_round < 1 or final_round < 1:
        raise PayloadError("start- og finalerunde skal være positive")
    if rounds_per_tie not in (1, 2):
        raise PayloadError("knockoutopgør skal vare én eller to runder")
    size = knockout_size_for(team_count)
    group_end = final_round - int(log2(size)) * rounds_per_tie
    if group_end < start_round:
        raise PayloadError("perioden skal indeholde mindst én gruppespilsrunde")
    return size, group_end


def generate_group_fixtures(
    team_ids: tuple[int, ...],
    start_round: int,
    end_round: int,
    *,
    shuffle: Callable[[list[int]], None] | None = None,
) -> tuple[GroupFixture, ...]:
    if len(team_ids) < 2 or len(set(team_ids)) != len(team_ids):
        raise PayloadError("turneringshold skal være mindst to unikke hold")
    if end_round < start_round:
        raise PayloadError("gruppespillet mangler runder")
    order = list(team_ids)
    (shuffle or secrets.SystemRandom().shuffle)(order)
    rotating: list[int | None] = [*order]
    if len(rotating) % 2:
        rotating.append(None)
    cycle: list[tuple[tuple[int, int | None], ...]] = []
    for _ in range(len(rotating) - 1):
        pairs: list[tuple[int, int | None]] = []
        for index in range(len(rotating) // 2):
            left = rotating[index]
            right = rotating[-1 - index]
            if left is None:
                assert right is not None
                pairs.append((right, None))
            else:
                pairs.append((left, right))
        cycle.append(tuple(pairs))
        rotating = [rotating[0], rotating[-1], *rotating[1:-1]]
    fixtures: list[GroupFixture] = []
    for offset, round_number in enumerate(range(start_round, end_round + 1)):
        for team_a, team_b in cycle[offset % len(cycle)]:
            fixtures.append(GroupFixture(round_number, team_a, team_b))
    return tuple(fixtures)


def generate_draw_seed() -> str:
    """Return a compact, user-visible seed for a tournament draw."""

    return secrets.token_hex(8)


def tournament_schedule_signature(
    fixtures: tuple[GroupFixture, ...],
) -> tuple[tuple[int, int, int | None], ...]:
    """Return a canonical signature that ignores irrelevant A/B ordering."""

    normalized: list[tuple[int, int, int | None]] = []
    for fixture in fixtures:
        if fixture.team_b_id is None:
            normalized.append((fixture.round_number, fixture.team_a_id, None))
        else:
            low, high = sorted((fixture.team_a_id, fixture.team_b_id))
            normalized.append((fixture.round_number, low, high))
    return tuple(
        sorted(normalized, key=lambda item: (item[0], item[1], item[2] or -1))
    )


def create_tournament_config(
    team_ids: tuple[int, ...],
    start_round: int,
    final_round: int,
    rounds_per_tie: int,
    *,
    draw_seed: str | None = None,
    shuffle: Callable[[list[int]], None] | None = None,
) -> TournamentConfig:
    size, group_end = _validate_rounds(
        len(team_ids), start_round, final_round, rounds_per_tie
    )
    resolved_seed = draw_seed
    resolved_shuffle = shuffle
    if resolved_shuffle is None:
        resolved_seed = (resolved_seed or generate_draw_seed()).strip()
        if not resolved_seed:
            raise PayloadError("lodtrækningsseed må ikke være tomt")
        resolved_shuffle = random.Random(resolved_seed).shuffle
    return TournamentConfig(
        start_round=start_round,
        final_round=final_round,
        rounds_per_tie=rounds_per_tie,
        knockout_size=size,
        group_fixtures=generate_group_fixtures(
            tuple(sorted(team_ids)), start_round, group_end, shuffle=resolved_shuffle
        ),
        draw_seed=resolved_seed,
    )


def validate_tournament_config(
    config: TournamentConfig, team_ids: tuple[int, ...]
) -> TournamentConfig:
    size, group_end = _validate_rounds(
        len(team_ids), config.start_round, config.final_round, config.rounds_per_tie
    )
    if config.knockout_size != size:
        raise PayloadError("turneringens knockoutstørrelse passer ikke til holdantallet")
    expected_rounds = set(range(config.start_round, group_end + 1))
    actual_rounds = {fixture.round_number for fixture in config.group_fixtures}
    if actual_rounds != expected_rounds:
        raise PayloadError("turneringens gruppespilsplan dækker ikke de korrekte runder")
    wanted = set(team_ids)
    for round_number in expected_rounds:
        seen: set[int] = set()
        for fixture in (
            item for item in config.group_fixtures if item.round_number == round_number
        ):
            participants = (fixture.team_a_id,) + (
                (fixture.team_b_id,) if fixture.team_b_id is not None else ()
            )
            if any(team_id not in wanted or team_id in seen for team_id in participants):
                raise PayloadError("turneringens gruppespilsplan indeholder ugyldige hold")
            seen.update(participants)
        if seen != wanted:
            raise PayloadError("alle hold skal have en kamp eller pause i hver runde")
    return config


def _member_maps(group: GroupDefinition, snapshots: SnapshotIndex):
    members = {member.team_id: member for member in group.teams}
    names: dict[int, str] = {}
    owners: dict[int, str] = {}
    for team_id, member in members.items():
        newest = snapshots.newest(group.game, team_id)
        names[team_id] = newest.team.team_name if newest else member.name
        owners[team_id] = newest.team.owner_name if newest else member.account_label
    return members, names, owners


def _change(
    snapshots: SnapshotIndex, game: GameUrl, team_id: int, round_number: int
) -> int | None:
    located = snapshots.summary_for(game, team_id, round_number)
    return located[1].change if located is not None else None


def _group_results(group: GroupDefinition, snapshots: SnapshotIndex, as_of_round: int):
    assert group.tournament is not None
    _, names, _ = _member_maps(group, snapshots)
    results: list[GroupMatch] = []
    for fixture in group.tournament.group_fixtures:
        if fixture.is_bye:
            results.append(
                GroupMatch(fixture, names[fixture.team_a_id], None, None, None, None, True)
            )
            continue
        assert fixture.team_b_id is not None
        a_change = (
            _change(snapshots, group.game, fixture.team_a_id, fixture.round_number)
            if fixture.round_number <= as_of_round
            else None
        )
        b_change = (
            _change(snapshots, group.game, fixture.team_b_id, fixture.round_number)
            if fixture.round_number <= as_of_round
            else None
        )
        complete = a_change is not None and b_change is not None
        winner = None
        if complete and a_change != b_change:
            winner = fixture.team_a_id if a_change > b_change else fixture.team_b_id
        results.append(
            GroupMatch(
                fixture,
                names[fixture.team_a_id],
                names[fixture.team_b_id],
                a_change,
                b_change,
                winner,
                complete,
            )
        )
    return tuple(results)


def _standings(
    group: GroupDefinition,
    snapshots: SnapshotIndex,
    matches: tuple[GroupMatch, ...],
) -> tuple[TournamentStanding, ...]:
    _, names, owners = _member_maps(group, snapshots)
    stats = {
        member.team_id: {"p": 0, "w": 0, "d": 0, "l": 0, "f": 0, "a": 0, "pts": 0}
        for member in group.teams
    }
    completed: list[GroupMatch] = []
    for match in matches:
        if match.fixture.is_bye or not match.complete:
            continue
        assert match.fixture.team_b_id is not None
        assert match.team_a_change is not None and match.team_b_change is not None
        completed.append(match)
        a = stats[match.fixture.team_a_id]
        b = stats[match.fixture.team_b_id]
        a["p"] += 1; b["p"] += 1
        a["f"] += match.team_a_change; a["a"] += match.team_b_change
        b["f"] += match.team_b_change; b["a"] += match.team_a_change
        if match.team_a_change == match.team_b_change:
            a["d"] += 1; b["d"] += 1; a["pts"] += 1; b["pts"] += 1
        elif match.team_a_change > match.team_b_change:
            a["w"] += 1; b["l"] += 1; a["pts"] += 3
        else:
            b["w"] += 1; a["l"] += 1; b["pts"] += 3

    primary_groups: dict[tuple[int, int, int], set[int]] = {}
    for team_id, item in stats.items():
        primary_groups.setdefault(
            (item["pts"], item["f"] - item["a"], item["f"]), set()
        ).add(team_id)
    head_points = {team_id: 0 for team_id in stats}
    for tied in primary_groups.values():
        if len(tied) < 2:
            continue
        for match in completed:
            a_id = match.fixture.team_a_id
            b_id = match.fixture.team_b_id
            if b_id not in tied or a_id not in tied:
                continue
            if match.team_a_change == match.team_b_change:
                head_points[a_id] += 1; head_points[b_id] += 1
            elif match.winner_id is not None:
                head_points[match.winner_id] += 3

    ordered = sorted(
        stats,
        key=lambda team_id: (
            -stats[team_id]["pts"],
            -(stats[team_id]["f"] - stats[team_id]["a"]),
            -stats[team_id]["f"],
            -head_points[team_id],
            names[team_id].casefold(),
            team_id,
        ),
    )
    return tuple(
        TournamentStanding(
            rank=index,
            team_id=team_id,
            team_name=names[team_id],
            owner_name=owners[team_id],
            played=stats[team_id]["p"],
            wins=stats[team_id]["w"],
            draws=stats[team_id]["d"],
            losses=stats[team_id]["l"],
            growth_for=stats[team_id]["f"],
            growth_against=stats[team_id]["a"],
            growth_difference=stats[team_id]["f"] - stats[team_id]["a"],
            points=stats[team_id]["pts"],
        )
        for index, team_id in enumerate(ordered, 1)
    )


def _stage_specs(config: TournamentConfig):
    size = config.knockout_size
    start = config.group_end_round + 1
    specs: list[tuple[str, tuple[int, ...], int]] = []
    while size >= 2:
        rounds = tuple(range(start, start + config.rounds_per_tie))
        specs.append((STAGE_NAMES[size], rounds, size // 2))
        start += config.rounds_per_tie
        size //= 2
    return tuple(specs)


def _knockout(
    group: GroupDefinition,
    snapshots: SnapshotIndex,
    as_of_round: int,
    standings: tuple[TournamentStanding, ...],
    group_complete: bool,
) -> tuple[KnockoutMatch, ...]:
    assert group.tournament is not None
    if not group_complete:
        return ()
    _, names, _ = _member_maps(group, snapshots)
    seeds = {standing.team_id: standing.rank for standing in standings}
    seed_to_team = {
        standing.rank: standing.team_id
        for standing in standings[: group.tournament.knockout_size]
    }
    ordered_ids = [seed_to_team[seed] for seed in bracket_seed_order(group.tournament.knockout_size)]
    previous: list[tuple[int | None, int | None]] = [
        (team_id, seeds[team_id]) for team_id in ordered_ids
    ]
    result: list[KnockoutMatch] = []
    for stage, rounds, match_count in _stage_specs(group.tournament):
        winners: list[tuple[int | None, int | None]] = []
        for match_index in range(match_count):
            a_id, a_seed = previous[match_index * 2]
            b_id, b_seed = previous[match_index * 2 + 1]
            a_total: int | None = 0 if a_id is not None else None
            b_total: int | None = 0 if b_id is not None else None
            if a_id is not None and b_id is not None:
                for round_number in rounds:
                    if round_number > as_of_round:
                        a_total = b_total = None
                        break
                    a_change = _change(snapshots, group.game, a_id, round_number)
                    b_change = _change(snapshots, group.game, b_id, round_number)
                    if a_change is None or b_change is None:
                        a_total = b_total = None
                        break
                    assert a_total is not None and b_total is not None
                    a_total += a_change
                    b_total += b_change
            complete = a_total is not None and b_total is not None
            winner: int | None = None
            winner_seed: int | None = None
            if complete:
                if a_total > b_total:
                    winner, winner_seed = a_id, a_seed
                elif b_total > a_total:
                    winner, winner_seed = b_id, b_seed
                elif a_seed is not None and b_seed is not None:
                    winner, winner_seed = (a_id, a_seed) if a_seed < b_seed else (b_id, b_seed)
            result.append(
                KnockoutMatch(
                    stage, match_index + 1, rounds, a_id, b_id,
                    names.get(a_id) if a_id is not None else None,
                    names.get(b_id) if b_id is not None else None,
                    a_seed, b_seed, a_total, b_total, winner, complete,
                )
            )
            winners.append((winner, winner_seed))
        previous = winners
    return tuple(result)


def build_tournament_state(
    group: GroupDefinition, snapshots: SnapshotIndex, as_of_round: int
) -> TournamentState:
    if group.kind != "tournament" or group.tournament is None:
        raise PayloadError("gruppen er ikke en turnering")
    validate_tournament_config(group.tournament, tuple(team.team_id for team in group.teams))
    as_of = max(0, as_of_round)
    group_matches = _group_results(group, snapshots, as_of)
    standings = _standings(group, snapshots, group_matches)
    required_group_matches = tuple(match for match in group_matches if not match.fixture.is_bye)
    group_complete = (
        as_of >= group.tournament.group_end_round
        and all(match.complete for match in required_group_matches)
    )
    knockout = _knockout(group, snapshots, as_of, standings, group_complete)
    all_ids = {team.team_id for team in group.teams}
    eliminated: set[int] = set()
    champion: int | None = None
    if group_complete:
        qualified = {row.team_id for row in standings[: group.tournament.knockout_size]}
        eliminated.update(all_ids - qualified)
        for match in knockout:
            if match.complete and match.winner_id is not None:
                loser = match.team_b_id if match.winner_id == match.team_a_id else match.team_a_id
                if loser is not None:
                    eliminated.add(loser)
        finals = [match for match in knockout if match.stage == "Finale"]
        if finals and finals[0].complete:
            champion = finals[0].winner_id
    active = set() if champion is not None else all_ids - eliminated
    warnings: list[str] = []
    missing_group = [
        match for match in required_group_matches
        if match.fixture.round_number <= as_of and not match.complete
    ]
    if missing_group:
        warnings.append(f"{len(missing_group)} gruppespilskamp(e) afventer rundedata")
    missing_knockout = [
        match for match in knockout
        if match.round_numbers[-1] <= as_of and not match.complete
    ]
    if missing_knockout:
        warnings.append(f"{len(missing_knockout)} knockoutkamp(e) afventer rundedata")
    if champion is not None:
        phase = "Afsluttet"
    elif as_of < group.tournament.start_round:
        phase = "Ikke startet"
    elif as_of <= group.tournament.group_end_round or not group_complete:
        phase = "Gruppespil"
    else:
        phase = next(
            (stage for stage, rounds, _ in _stage_specs(group.tournament) if as_of <= rounds[-1]),
            "Finale",
        )
    return TournamentState(
        as_of, phase, group_matches, standings, knockout,
        frozenset(active), frozenset(eliminated), champion, tuple(warnings)
    )


def build_tournament_head_to_head(
    group: GroupDefinition,
    snapshots: SnapshotIndex,
    team_a_id: int,
    team_b_id: int,
    as_of_round: int,
) -> HeadToHeadSummary:
    """Build a historical head-to-head view from cached tournament data."""

    if team_a_id == team_b_id:
        raise PayloadError("H2H kræver to forskellige hold")
    members = {member.team_id for member in group.teams}
    if team_a_id not in members or team_b_id not in members:
        raise PayloadError("H2H-holdet findes ikke i turneringen")
    state = build_tournament_state(group, snapshots, as_of_round)
    _, names, _ = _member_maps(group, snapshots)
    wanted = {team_a_id, team_b_id}
    matches: list[HeadToHeadMatch] = []

    for match in state.group_matches:
        fixture = match.fixture
        if (
            fixture.team_b_id is None
            or fixture.round_number > state.as_of_round
            or {fixture.team_a_id, fixture.team_b_id} != wanted
        ):
            continue
        changes = {
            fixture.team_a_id: match.team_a_change,
            fixture.team_b_id: match.team_b_change,
        }
        a_change = changes[team_a_id]
        b_change = changes[team_b_id]
        complete = a_change is not None and b_change is not None
        winner = None
        if complete and a_change != b_change:
            winner = team_a_id if a_change > b_change else team_b_id
        matches.append(
            HeadToHeadMatch(
                fixture.round_number, "Gruppespil", team_a_id, team_b_id,
                names[team_a_id], names[team_b_id], a_change, b_change,
                winner, complete,
            )
        )

    for match in state.knockout_matches:
        if (
            match.team_a_id is None
            or match.team_b_id is None
            or {match.team_a_id, match.team_b_id} != wanted
        ):
            continue
        aggregate_tie = (
            match.complete
            and match.team_a_change == match.team_b_change
            and match.winner_id is not None
        )
        for round_number in match.round_numbers:
            if round_number > state.as_of_round:
                continue
            a_change = _change(snapshots, group.game, team_a_id, round_number)
            b_change = _change(snapshots, group.game, team_b_id, round_number)
            complete = a_change is not None and b_change is not None
            winner = None
            if complete and a_change != b_change:
                winner = team_a_id if a_change > b_change else team_b_id
            matches.append(
                HeadToHeadMatch(
                    round_number, match.stage, team_a_id, team_b_id,
                    names[team_a_id], names[team_b_id], a_change, b_change,
                    winner, complete,
                    (
                        match.winner_id
                        if aggregate_tie and round_number == match.round_numbers[-1]
                        else None
                    ),
                )
            )

    matches.sort(key=lambda item: (item.round_number, item.phase, item.team_a_id))
    completed = tuple(match for match in matches if match.complete)
    return HeadToHeadSummary(
        team_a_id, team_b_id, names[team_a_id], names[team_b_id], tuple(matches),
        len(completed),
        sum(match.winner_id == team_a_id for match in completed),
        sum(match.winner_id is None for match in completed),
        sum(match.winner_id == team_b_id for match in completed),
        sum(match.team_a_change or 0 for match in completed),
        sum(match.team_b_change or 0 for match in completed),
    )


def latest_tournament_round(group: GroupDefinition, snapshots: SnapshotIndex) -> int:
    assert group.tournament is not None
    rounds = [
        snapshot.team.overview.current_round
        for member in group.teams
        for snapshot in snapshots.for_team(group.game, member.team_id)[:1]
    ]
    if not rounds:
        return group.tournament.start_round
    return min(group.tournament.final_round, max(group.tournament.start_round, max(rounds)))
