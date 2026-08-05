"""Pure scheduling and result calculation for group-to-knockout tournaments."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from math import log2
import random
import secrets
from typing import Callable, TYPE_CHECKING
from math import ceil
from typing import Literal

from ._formatting import count_label
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
    group_index: int = 0

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
    template: str = "group_knockout"
    match_points: tuple[int, int, int] = (3, 1, 0)
    seed_rule: str = "random"
    seed_order: tuple[int, ...] = ()
    standings_tiebreakers: tuple[str, ...] = (
        "score_difference",
        "score_for",
        "head_to_head",
        "entry_seed",
    )
    knockout_tiebreakers: tuple[str, ...] = ("higher_seed",)
    league_legs: int = 1
    swiss_rounds: int | None = None
    group_count: int = 1
    qualifiers_per_group: int | None = None
    bronze_match: bool = False

    @property
    def knockout_stage_count(self) -> int:
        return int(log2(self.knockout_size))

    @property
    def group_end_round(self) -> int:
        if self.template != "group_knockout":
            return self.final_round
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
    group_index: int = 0
    head_to_head_points: int = 0
    buchholz: int = 0
    entry_seed: int = 0


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
        raise PayloadError("En turnering kræver mindst to hold")
    size = 2
    while size * 2 <= team_count and size < 32:
        size *= 2
    return size


def bracket_seed_order(size: int) -> tuple[int, ...]:
    if size < 2 or size > 32 or size & (size - 1):
        raise PayloadError("Knockoutstørrelsen skal være 2, 4, 8, 16 eller 32")
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
        raise PayloadError("Start- og finalerunde skal være positive")
    if rounds_per_tie not in (1, 2):
        raise PayloadError("Knockoutopgør skal vare én eller to runder")
    size = knockout_size_for(team_count)
    group_end = final_round - int(log2(size)) * rounds_per_tie
    if group_end < start_round:
        raise PayloadError("Perioden skal indeholde mindst én gruppespilsrunde")
    return size, group_end


def generate_group_fixtures(
    team_ids: tuple[int, ...],
    start_round: int,
    end_round: int,
    *,
    shuffle: Callable[[list[int]], None] | None = None,
) -> tuple[GroupFixture, ...]:
    if len(team_ids) < 2 or len(set(team_ids)) != len(team_ids):
        raise PayloadError("Turneringen skal have mindst to unikke hold")
    if end_round < start_round:
        raise PayloadError("Gruppespillet mangler runder")
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
) -> tuple[tuple[int, int, int, int | None], ...]:
    """Return a canonical signature that ignores irrelevant A/B ordering."""

    normalized: list[tuple[int, int, int, int | None]] = []
    for fixture in fixtures:
        if fixture.team_b_id is None:
            normalized.append((fixture.round_number, fixture.group_index, fixture.team_a_id, None))
        else:
            low, high = sorted((fixture.team_a_id, fixture.team_b_id))
            normalized.append((fixture.round_number, fixture.group_index, low, high))
    return tuple(
        sorted(normalized, key=lambda item: (item[0], item[1], item[2], item[3] or -1))
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
            raise PayloadError("Lodtrækningsseed må ikke være tomt")
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
    if config.template != "group_knockout" or config.group_count > 1 or config.qualifiers_per_group is not None:
        return _validate_template_config(config, team_ids)
    size, group_end = _validate_rounds(
        len(team_ids), config.start_round, config.final_round, config.rounds_per_tie
    )
    if config.knockout_size != size:
        raise PayloadError("Turneringens knockoutstørrelse passer ikke til holdantallet")
    expected_rounds = set(range(config.start_round, group_end + 1))
    actual_rounds = {fixture.round_number for fixture in config.group_fixtures}
    if actual_rounds != expected_rounds:
        raise PayloadError("Turneringens gruppespilsplan dækker ikke de korrekte runder")
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
                raise PayloadError("Turneringens gruppespilsplan indeholder ugyldige hold")
            seen.update(participants)
        if seen != wanted:
            raise PayloadError("Alle hold skal have en kamp eller pause i hver runde")
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
    if located is None:
        return None
    summary = located[1]
    return summary.change if summary.round_status == "complete" else None



def _total(
    snapshots: SnapshotIndex, game: GameUrl, team_id: int, round_number: int
) -> int | None:
    located = snapshots.summary_for(game, team_id, round_number)
    if located is None or located[1].round_status != "complete":
        return None
    return located[1].total

def resolve_knockout_tie(
    config: TournamentConfig,
    snapshots: SnapshotIndex,
    game: GameUrl,
    team_a_id: int,
    team_b_id: int,
    team_a_seed: int,
    team_b_seed: int,
    round_number: int,
) -> int:
    """Resolve a tied knockout match with the configured deterministic rules."""

    for rule in config.knockout_tiebreakers:
        if rule == "last_round_growth":
            a_value = _change(snapshots, game, team_a_id, round_number)
            b_value = _change(snapshots, game, team_b_id, round_number)
        elif rule == "overall_total":
            a_value = _total(snapshots, game, team_a_id, round_number)
            b_value = _total(snapshots, game, team_b_id, round_number)
        else:
            a_value, b_value = -team_a_seed, -team_b_seed
        if a_value is not None and b_value is not None and a_value != b_value:
            return team_a_id if a_value > b_value else team_b_id
    return min(team_a_id, team_b_id)


def _group_results(group: GroupDefinition, snapshots: SnapshotIndex, as_of_round: int):
    assert group.tournament is not None
    _, names, _ = _member_maps(group, snapshots)
    results: list[GroupMatch] = []
    for fixture in group.tournament.group_fixtures:
        if fixture.is_bye:
            results.append(
                GroupMatch(
                    fixture,
                    names[fixture.team_a_id],
                    None,
                    None,
                    None,
                    fixture.team_a_id if fixture.round_number <= as_of_round else None,
                    fixture.round_number <= as_of_round,
                )
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
    assert group.tournament is not None
    if group.tournament.group_count > 1:
        pooled: list[TournamentStanding] = []
        for group_index in range(group.tournament.group_count):
            pool_fixtures = tuple(
                item
                for item in group.tournament.group_fixtures
                if item.group_index == group_index
            )
            pool_ids = {
                team_id
                for fixture in pool_fixtures
                for team_id in (fixture.team_a_id, fixture.team_b_id)
                if team_id is not None
            }
            pool_group = replace(
                group,
                teams=tuple(item for item in group.teams if item.team_id in pool_ids),
                tournament=replace(
                    group.tournament,
                    group_count=1,
                    group_fixtures=pool_fixtures,
                ),
            )
            pool_matches = tuple(
                item for item in matches if item.fixture.group_index == group_index
            )
            pooled.extend(
                replace(item, group_index=group_index)
                for item in _standings(pool_group, snapshots, pool_matches)
            )
        return tuple(pooled)
    stats = {
        member.team_id: {"p": 0, "w": 0, "d": 0, "l": 0, "f": 0, "a": 0, "pts": 0}
        for member in group.teams
    }
    completed: list[GroupMatch] = []
    for match in matches:
        if not match.complete:
            continue
        if match.fixture.is_bye:
            if group.tournament.template == "swiss":
                bye = stats[match.fixture.team_a_id]
                bye["p"] += 1
                bye["w"] += 1
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
            a["d"] += 1; b["d"] += 1
        elif match.team_a_change > match.team_b_change:
            a["w"] += 1; b["l"] += 1
        else:
            b["w"] += 1; a["l"] += 1

    assert group.tournament is not None
    win_points, draw_points, loss_points = group.tournament.match_points
    for item in stats.values():
        item["pts"] = (
            item["w"] * win_points
            + item["d"] * draw_points
            + item["l"] * loss_points
        )
    head_points = {team_id: 0 for team_id in stats}
    opponents: dict[int, list[int]] = {team_id: [] for team_id in stats}
    for match in completed:
        a_id = match.fixture.team_a_id
        assert match.fixture.team_b_id is not None
        b_id = match.fixture.team_b_id
        opponents[a_id].append(b_id)
        opponents[b_id].append(a_id)
        if stats[a_id]["pts"] != stats[b_id]["pts"]:
            continue
        if match.team_a_change == match.team_b_change:
            head_points[a_id] += draw_points
            head_points[b_id] += draw_points
        elif match.winner_id is not None:
            loser_id = b_id if match.winner_id == a_id else a_id
            head_points[match.winner_id] += win_points
            head_points[loser_id] += loss_points
    buchholz = {
        team_id: sum(stats[opponent]["pts"] for opponent in faced)
        for team_id, faced in opponents.items()
    }
    seed_positions = {
        team_id: index
        for index, team_id in enumerate(group.tournament.seed_order, 1)
    }

    def ordering_key(team_id: int) -> tuple[int, ...]:
        values = [-stats[team_id]["pts"]]
        for rule in group.tournament.standings_tiebreakers:
            if rule == "score_difference":
                value = stats[team_id]["f"] - stats[team_id]["a"]
            elif rule == "score_for":
                value = stats[team_id]["f"]
            elif rule == "head_to_head":
                value = head_points[team_id]
            elif rule == "buchholz":
                value = buchholz[team_id]
            else:
                value = -seed_positions.get(team_id, len(stats) + 1)
            values.append(-value)
        values.append(team_id)
        return tuple(values)

    ordered = sorted(stats, key=ordering_key)
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
            head_to_head_points=head_points[team_id],
            buchholz=buchholz[team_id],
            entry_seed=seed_positions.get(team_id, len(stats) + 1),
        )
        for index, team_id in enumerate(ordered, 1)
    )



def tournament_standing_sort_key(
    config: TournamentConfig,
    standing: TournamentStanding,
) -> tuple[int, ...]:
    """Return the configured cross-group ordering with stable fallbacks."""

    seed_positions = {
        team_id: index
        for index, team_id in enumerate(config.seed_order, 1)
    }
    values = [-standing.points]
    for rule in config.standings_tiebreakers:
        if rule == "score_difference":
            values.append(-standing.growth_difference)
        elif rule == "score_for":
            values.append(-standing.growth_for)
        elif rule == "head_to_head":
            values.append(-standing.head_to_head_points)
        elif rule == "buchholz":
            values.append(-standing.buchholz)
        else:
            values.append(
                standing.entry_seed
                or seed_positions.get(
                    standing.team_id,
                    len(config.seed_order) + 1,
                )
            )
    values.extend((standing.group_index, standing.team_id))
    return tuple(values)


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


def _qualified_standings(
    config: TournamentConfig,
    standings: tuple[TournamentStanding, ...],
) -> tuple[TournamentStanding, ...]:
    if config.group_count <= 1:
        return standings[: config.knockout_size]
    by_group = {
        group_index: tuple(item for item in standings if item.group_index == group_index)
        for group_index in range(config.group_count)
    }
    direct_count = config.qualifiers_per_group or 0
    direct = [
        by_group[group_index][rank_index]
        for rank_index in range(direct_count)
        for group_index in range(config.group_count)
        if rank_index < len(by_group[group_index])
    ]
    direct_ids = {item.team_id for item in direct}
    remaining = sorted(
        (item for item in standings if item.team_id not in direct_ids),
        key=lambda item: tournament_standing_sort_key(config, item),
    )
    wildcard_count = config.knockout_size - len(direct)
    return tuple((*direct, *remaining[:wildcard_count]))


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
    qualified = _qualified_standings(group.tournament, standings)
    seeds = {standing.team_id: index for index, standing in enumerate(qualified, 1)}
    seed_to_team = {index: standing.team_id for index, standing in enumerate(qualified, 1)}
    ordered_ids = [
        seed_to_team[seed]
        for seed in bracket_seed_order(group.tournament.knockout_size)
    ]
    previous: list[tuple[int | None, int | None]] = [
        (team_id, seeds[team_id]) for team_id in ordered_ids
    ]
    result: list[KnockoutMatch] = []
    semifinal_losers: list[tuple[int, int]] = []
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
                    assert a_id is not None and b_id is not None
                    winner = resolve_knockout_tie(
                        group.tournament,
                        snapshots,
                        group.game,
                        a_id,
                        b_id,
                        a_seed,
                        b_seed,
                        rounds[-1],
                    )
                    winner_seed = a_seed if winner == a_id else b_seed
            result.append(
                KnockoutMatch(
                    stage, match_index + 1, rounds, a_id, b_id,
                    names.get(a_id) if a_id is not None else None,
                    names.get(b_id) if b_id is not None else None,
                    a_seed, b_seed, a_total, b_total, winner, complete,
                )
            )
            if (
                stage == "Semifinaler"
                and complete
                and winner is not None
                and a_id is not None
                and b_id is not None
                and a_seed is not None
                and b_seed is not None
            ):
                semifinal_losers.append(
                    (b_id, b_seed) if winner == a_id else (a_id, a_seed)
                )
            winners.append((winner, winner_seed))
        previous = winners
    if group.tournament.bronze_match and len(semifinal_losers) == 2:
        (a_id, a_seed), (b_id, b_seed) = semifinal_losers
        final_rounds = _stage_specs(group.tournament)[-1][1]
        a_total: int | None = 0
        b_total: int | None = 0
        for round_number in final_rounds:
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
        if complete:
            if a_total > b_total:
                winner = a_id
            elif b_total > a_total:
                winner = b_id
            else:
                winner = resolve_knockout_tie(
                    group.tournament,
                    snapshots,
                    group.game,
                    a_id,
                    b_id,
                    a_seed,
                    b_seed,
                    final_rounds[-1],
                )
        result.append(
            KnockoutMatch(
                "Bronzekamp", 1, final_rounds, a_id, b_id,
                names.get(a_id), names.get(b_id), a_seed, b_seed,
                a_total, b_total, winner, complete,
            )
        )
    return tuple(result)


def build_tournament_state(
    group: GroupDefinition, snapshots: SnapshotIndex, as_of_round: int
) -> TournamentState:
    if group.kind != "tournament" or group.tournament is None:
        raise PayloadError("Gruppen er ikke en turnering")
    validate_tournament_config(group.tournament, tuple(team.team_id for team in group.teams))
    if group.tournament.template != "group_knockout":
        return _build_template_state(group, snapshots, as_of_round)
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
        qualified = {row.team_id for row in _qualified_standings(group.tournament, standings)}
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
        warnings.append(
            f"{count_label(len(missing_group), 'gruppespilskamp', 'gruppespilskampe')} "
            "afventer rundedata"
        )
    missing_knockout = [
        match for match in knockout
        if match.round_numbers[-1] <= as_of and not match.complete
    ]
    if missing_knockout:
        warnings.append(
            f"{count_label(len(missing_knockout), 'knockoutkamp', 'knockoutkampe')} "
            "afventer rundedata"
        )
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



TournamentTemplate = Literal["league", "swiss", "group_knockout", "double_elimination"]
SeedRule = Literal["random", "manual", "elo"]


@dataclass(frozen=True, slots=True)
class LeagueTemplateConfig:
    legs: int = 1


@dataclass(frozen=True, slots=True)
class SwissTemplateConfig:
    rounds: int | None = None


@dataclass(frozen=True, slots=True)
class GroupKnockoutTemplateConfig:
    group_count: int = 1
    qualifiers_per_group: int | None = None
    bronze_match: bool = False


@dataclass(frozen=True, slots=True)
class DoubleEliminationTemplateConfig:
    reset_final: bool = True


@dataclass(frozen=True, slots=True)
class SwissParticipant:
    team_id: int
    match_points: int = 0
    score_for: int = 0
    opponent_ids: tuple[int, ...] = ()
    had_bye: bool = False
    entry_seed: int = 0
    buchholz: int = 0


@dataclass(frozen=True, slots=True)
class TournamentPairing:
    round_number: int
    team_a_id: int
    team_b_id: int | None
    published: bool = True


@dataclass(frozen=True, slots=True)
class SwissPairingConflict:
    """A published Swiss round that differs from corrected prior results."""

    round_number: int
    published: tuple[TournamentPairing, ...]
    expected: tuple[TournamentPairing, ...]


@dataclass(frozen=True, slots=True)
class DoubleEliminationMatch:
    match_id: str
    bracket: Literal["winners", "losers", "final"]
    bracket_round: int
    team_a_seed: int | None = None
    team_b_seed: int | None = None
    source_a: str | None = None
    source_b: str | None = None
    reset_final: bool = False


TournamentDefinition = TournamentConfig
TournamentTemplateConfig = (
    LeagueTemplateConfig
    | SwissTemplateConfig
    | GroupKnockoutTemplateConfig
    | DoubleEliminationTemplateConfig
)


def tournament_template_config(
    config: TournamentDefinition,
    team_ids: tuple[int, ...] | None = None,
) -> TournamentTemplateConfig:
    """Project the schema-8 runtime config to its validated template view."""

    resolved_team_ids = team_ids or config.seed_order or tuple(
        sorted({
            team_id
            for fixture in config.group_fixtures
            for team_id in (fixture.team_a_id, fixture.team_b_id)
            if team_id is not None
        })
    )
    validate_tournament_config(config, resolved_team_ids)
    if config.template == "league":
        return LeagueTemplateConfig(config.league_legs)
    if config.template == "swiss":
        return SwissTemplateConfig(config.swiss_rounds)
    if config.template == "group_knockout":
        return GroupKnockoutTemplateConfig(
            config.group_count,
            config.qualifiers_per_group,
            config.bronze_match,
        )
    return DoubleEliminationTemplateConfig(reset_final=True)


def _seeded_order(
    team_ids: tuple[int, ...],
    seed_rule: SeedRule,
    draw_seed: str | None,
    seed_order: tuple[int, ...],
) -> tuple[tuple[int, ...], str | None]:
    wanted = set(team_ids)
    if seed_rule in {"manual", "elo"}:
        if len(seed_order) != len(team_ids) or set(seed_order) != wanted:
            raise PayloadError("Manuel seedning og Elo-seedning kræver hver deltager præcis én gang")
        return seed_order, draw_seed
    resolved_seed = (draw_seed or generate_draw_seed()).strip()
    if not resolved_seed:
        raise PayloadError("Lodtrækningsseed må ikke være tomt")
    ordered = sorted(team_ids)
    random.Random(resolved_seed).shuffle(ordered)
    return tuple(ordered), resolved_seed


def generate_league_fixtures(
    team_ids: tuple[int, ...],
    start_round: int,
    *,
    legs: int = 1,
) -> tuple[GroupFixture, ...]:
    if legs not in {1, 2}:
        raise PayloadError("En liga skal have én eller to kampe mellem hvert deltagerpar")
    if len(team_ids) < 2 or len(team_ids) != len(set(team_ids)):
        raise PayloadError("Ligaens deltagere skal være entydige")
    ordered = tuple(team_ids)
    cycle_length = len(ordered) - 1 if len(ordered) % 2 == 0 else len(ordered)
    first_leg = generate_group_fixtures(
        ordered,
        start_round,
        start_round + cycle_length - 1,
        shuffle=lambda values: None,
    )
    if legs == 1:
        return first_leg
    second_leg = tuple(
        GroupFixture(
            item.round_number + cycle_length,
            item.team_b_id if item.team_b_id is not None else item.team_a_id,
            item.team_a_id if item.team_b_id is not None else None,
        )
        for item in first_leg
    )
    return (*first_leg, *second_leg)


def generate_swiss_pairings(
    participants: tuple[SwissParticipant, ...],
    round_number: int,
) -> tuple[GroupFixture, ...]:
    """Pair one Swiss round with deterministic rematch avoidance and a fair bye."""

    if len({item.team_id for item in participants}) != len(participants):
        raise PayloadError("Deltagerne i Swiss skal være entydige")
    ranked = sorted(
        participants,
        key=lambda item: (
            -item.match_points,
            -item.buchholz,
            -item.score_for,
            item.entry_seed,
            item.team_id,
        ),
    )
    bye: SwissParticipant | None = None
    if len(ranked) % 2:
        bye = next((item for item in reversed(ranked) if not item.had_bye), None)
        if bye is None:
            bye = ranked[-1]
        ranked.remove(bye)

    def search(remaining: tuple[SwissParticipant, ...]) -> list[tuple[int, int]] | None:
        if not remaining:
            return []
        first = remaining[0]
        candidates = sorted(
            remaining[1:],
            key=lambda item: (
                item.team_id in first.opponent_ids,
                abs(item.match_points - first.match_points),
                item.entry_seed,
                item.team_id,
            ),
        )
        for opponent in candidates:
            if opponent.team_id in first.opponent_ids:
                continue
            rest = tuple(item for item in remaining[1:] if item.team_id != opponent.team_id)
            tail = search(rest)
            if tail is not None:
                return [(first.team_id, opponent.team_id), *tail]
        for opponent in candidates:
            rest = tuple(item for item in remaining[1:] if item.team_id != opponent.team_id)
            tail = search(rest)
            if tail is not None:
                return [(first.team_id, opponent.team_id), *tail]
        return None

    pairs = search(tuple(ranked)) or []
    fixtures = [GroupFixture(round_number, first, second) for first, second in pairs]
    if bye is not None:
        fixtures.append(GroupFixture(round_number, bye.team_id, None))
    return tuple(fixtures)


def calculate_buchholz(
    participants: tuple[SwissParticipant, ...],
) -> dict[int, int]:
    points = {item.team_id: item.match_points for item in participants}
    return {
        item.team_id: sum(points.get(opponent, 0) for opponent in item.opponent_ids)
        for item in participants
    }

def build_swiss_participants(
    config: TournamentConfig,
    matches: Iterable[GroupMatch],
) -> tuple[SwissParticipant, ...]:
    """Rebuild Swiss score groups from complete published fixtures."""

    if config.template != "swiss":
        raise PayloadError("Swiss-stillingen kræver en Swiss-definition")
    stats = {
        team_id: {
            "points": 0,
            "score_for": 0,
            "opponents": [],
            "bye": False,
        }
        for team_id in config.seed_order
    }
    win_points, draw_points, loss_points = config.match_points
    for match in matches:
        if not match.complete:
            continue
        first_id = match.fixture.team_a_id
        second_id = match.fixture.team_b_id
        if second_id is None:
            stats[first_id]["points"] += win_points
            stats[first_id]["bye"] = True
            continue
        if match.team_a_change is None or match.team_b_change is None:
            continue
        stats[first_id]["score_for"] += match.team_a_change
        stats[second_id]["score_for"] += match.team_b_change
        stats[first_id]["opponents"].append(second_id)
        stats[second_id]["opponents"].append(first_id)
        if match.team_a_change > match.team_b_change:
            stats[first_id]["points"] += win_points
            stats[second_id]["points"] += loss_points
        elif match.team_b_change > match.team_a_change:
            stats[first_id]["points"] += loss_points
            stats[second_id]["points"] += win_points
        else:
            stats[first_id]["points"] += draw_points
            stats[second_id]["points"] += draw_points
    participants = tuple(
        SwissParticipant(
            team_id,
            match_points=int(stats[team_id]["points"]),
            score_for=int(stats[team_id]["score_for"]),
            opponent_ids=tuple(stats[team_id]["opponents"]),
            had_bye=bool(stats[team_id]["bye"]),
            entry_seed=seed,
        )
        for seed, team_id in enumerate(config.seed_order, 1)
    )
    buchholz = calculate_buchholz(participants)
    return tuple(
        replace(item, buchholz=buchholz[item.team_id])
        for item in participants
    )


def build_swiss_pairing_conflicts(
    group: GroupDefinition,
    snapshots: SnapshotIndex,
) -> tuple[SwissPairingConflict, ...]:
    """Compare frozen Swiss pairings with pairings implied by corrected results."""

    config = group.tournament
    if config is None or config.template != "swiss":
        return ()
    conflicts: list[SwissPairingConflict] = []
    published_rounds = sorted({item.round_number for item in config.group_fixtures})
    for round_number in published_rounds:
        if round_number == config.start_round:
            continue
        prefix = tuple(
            item
            for item in config.group_fixtures
            if item.round_number < round_number
        )
        prefix_group = replace(
            group,
            tournament=replace(config, group_fixtures=prefix),
        )
        previous_state = _build_template_state(
            prefix_group,
            snapshots,
            round_number - 1,
            require_full_schedule=False,
        )
        previous_matches = tuple(
            item
            for item in previous_state.group_matches
            if item.fixture.round_number == round_number - 1
        )
        if not previous_matches or not all(item.complete for item in previous_matches):
            continue
        participants = build_swiss_participants(config, previous_state.group_matches)
        expected = tuple(
            TournamentPairing(item.round_number, item.team_a_id, item.team_b_id)
            for item in generate_swiss_pairings(participants, round_number)
        )
        published = tuple(
            TournamentPairing(item.round_number, item.team_a_id, item.team_b_id)
            for item in config.group_fixtures
            if item.round_number == round_number
        )

        def normalize(
            values: tuple[TournamentPairing, ...],
        ) -> tuple[TournamentPairing, ...]:
            return tuple(
                sorted(
                    values,
                    key=lambda item: (
                        item.team_a_id,
                        -1 if item.team_b_id is None else item.team_b_id,
                    ),
                )
            )

        if normalize(published) != normalize(expected):
            conflicts.append(SwissPairingConflict(round_number, published, expected))
    return tuple(conflicts)


def build_double_elimination_bracket(
    team_count: int,
) -> tuple[DoubleEliminationMatch, ...]:
    """Return a frozen abstract bracket, including the conditional reset final."""

    if team_count < 2 or team_count > 32:
        raise PayloadError("Double elimination understøtter 2-32 deltagere")
    size = 2
    while size < team_count:
        size *= 2
    matches: list[DoubleEliminationMatch] = []
    winners_rounds = int(log2(size))
    previous_winners: list[str] = []
    seed_order = bracket_seed_order(size)
    for index in range(size // 2):
        match_id = f"W1-{index + 1}"
        first_seed = seed_order[index * 2]
        second_seed = seed_order[index * 2 + 1]
        matches.append(
            DoubleEliminationMatch(
                match_id,
                "winners",
                1,
                first_seed if first_seed <= team_count else None,
                second_seed if second_seed <= team_count else None,
            )
        )
        previous_winners.append(match_id)
    for round_index in range(2, winners_rounds + 1):
        current: list[str] = []
        for index in range(len(previous_winners) // 2):
            match_id = f"W{round_index}-{index + 1}"
            matches.append(
                DoubleEliminationMatch(
                    match_id,
                    "winners",
                    round_index,
                    source_a=f"winner:{previous_winners[index * 2]}",
                    source_b=f"winner:{previous_winners[index * 2 + 1]}",
                )
            )
            current.append(match_id)
        previous_winners = current
    if winners_rounds == 1:
        losers_final_source = "loser:W1-1"
    else:
        losers_round = 1
        previous_losers: list[str] = []
        for index in range(size // 4):
            match_id = f"L{losers_round}-{index + 1}"
            matches.append(
                DoubleEliminationMatch(
                    match_id,
                    "losers",
                    losers_round,
                    source_a=f"loser:W1-{index * 2 + 1}",
                    source_b=f"loser:W1-{index * 2 + 2}",
                )
            )
            previous_losers.append(match_id)
        for winners_round in range(2, winners_rounds + 1):
            losers_round += 1
            injected: list[str] = []
            for index, previous_match in enumerate(previous_losers):
                match_id = f"L{losers_round}-{index + 1}"
                matches.append(
                    DoubleEliminationMatch(
                        match_id,
                        "losers",
                        losers_round,
                        source_a=f"winner:{previous_match}",
                        source_b=f"loser:W{winners_round}-{index + 1}",
                    )
                )
                injected.append(match_id)
            previous_losers = injected
            if winners_round < winners_rounds:
                losers_round += 1
                consolidated: list[str] = []
                for index in range(len(previous_losers) // 2):
                    match_id = f"L{losers_round}-{index + 1}"
                    matches.append(
                        DoubleEliminationMatch(
                            match_id,
                            "losers",
                            losers_round,
                            source_a=f"winner:{previous_losers[index * 2]}",
                            source_b=f"winner:{previous_losers[index * 2 + 1]}",
                        )
                    )
                    consolidated.append(match_id)
                previous_losers = consolidated
        losers_final_source = f"winner:{previous_losers[0]}"
    winners_final = previous_winners[0]
    matches.append(
        DoubleEliminationMatch(
            "GF1",
            "final",
            1,
            source_a=f"winner:{winners_final}",
            source_b=losers_final_source,
        )
    )
    matches.append(
        DoubleEliminationMatch(
            "GF2",
            "final",
            2,
            source_a="winner:GF1",
            source_b="loser:GF1",
            reset_final=True,
        )
    )
    return tuple(matches)


def _double_elimination_levels(
    matches: tuple[DoubleEliminationMatch, ...],
) -> dict[str, int]:
    """Map a topologically ordered double-elimination bracket to fantasy periods."""

    levels: dict[str, int] = {}
    for match in matches:
        dependencies: list[int] = []
        for source in (match.source_a, match.source_b):
            if source is None:
                continue
            _, source_match = source.split(":", 1)
            dependencies.append(levels[source_match])
        levels[match.match_id] = 1 + max(dependencies, default=0)
    return levels

def generate_group_stage_fixtures(
    seed_order: tuple[int, ...],
    start_round: int,
    end_round: int,
    group_count: int,
) -> tuple[tuple[tuple[int, ...], ...], tuple[GroupFixture, ...]]:
    """Create balanced serpentine pools and a deterministic schedule."""

    if (
        group_count < 1
        or group_count > 8
        or group_count > len(seed_order)
        or len(seed_order) != len(set(seed_order))
    ):
        raise PayloadError("Antallet af grupper skal være 1-8 og må ikke overstige deltagerantallet")
    if start_round < 1 or end_round < start_round:
        raise PayloadError("Gruppespillet kræver mindst én positiv fantasy-runde")
    pools: list[list[int]] = [[] for _ in range(group_count)]
    for index, team_id in enumerate(seed_order):
        cycle, offset = divmod(index, group_count)
        group_index = offset if cycle % 2 == 0 else group_count - 1 - offset
        pools[group_index].append(team_id)
    if max(map(len, pools)) - min(map(len, pools)) > 1:
        raise PayloadError("Gruppestørrelserne må højst afvige med én")
    fixtures: list[GroupFixture] = []
    for group_index, pool in enumerate(pools):
        if len(pool) == 1:
            fixtures.extend(
                GroupFixture(round_number, pool[0], None, group_index)
                for round_number in range(start_round, end_round + 1)
            )
            continue
        fixtures.extend(
            replace(item, group_index=group_index)
            for item in generate_group_fixtures(
                tuple(pool), start_round, end_round, shuffle=lambda values: None
            )
        )
    return tuple(tuple(pool) for pool in pools), tuple(fixtures)



def create_tournament_definition(
    template: TournamentTemplate,
    team_ids: tuple[int, ...],
    start_round: int,
    *,
    final_round: int | None = None,
    rounds_per_tie: int = 1,
    match_points: tuple[int, int, int] = (3, 1, 0),
    seed_rule: SeedRule = "random",
    draw_seed: str | None = None,
    seed_order: tuple[int, ...] = (),
    standings_tiebreakers: tuple[str, ...] = (
        "score_difference",
        "score_for",
        "head_to_head",
        "entry_seed",
    ),
    knockout_tiebreakers: tuple[str, ...] = (
        "last_round_growth",
        "overall_total",
        "higher_seed",
    ),
    league_legs: int = 1,
    swiss_rounds: int | None = None,
    group_count: int = 1,
    qualifiers_per_group: int | None = None,
    bronze_match: bool = False,
) -> TournamentDefinition:
    if template not in {"league", "swiss", "group_knockout", "double_elimination"}:
        raise PayloadError("Ukendt turneringsformat")
    if len(team_ids) < 2 or len(team_ids) > 32 or len(team_ids) != len(set(team_ids)):
        raise PayloadError("En turnering kræver 2-32 unikke deltagere")
    ordered, resolved_seed = _seeded_order(team_ids, seed_rule, draw_seed, seed_order)
    if template == "group_knockout":
        if final_round is None:
            raise PayloadError("Gruppespil med knockout kræver en finalerunde")
        if group_count == 1 and qualifiers_per_group is None:
            legacy = create_tournament_config(
                ordered,
                start_round,
                final_round,
                rounds_per_tie,
                draw_seed=resolved_seed,
                shuffle=lambda values: None,
            )
            result = replace(
                legacy,
                template=template,
                match_points=match_points,
                seed_rule=seed_rule,
                seed_order=ordered,
                standings_tiebreakers=standings_tiebreakers,
                knockout_tiebreakers=knockout_tiebreakers,
                bronze_match=bronze_match,
            )
        else:
            if rounds_per_tie not in {1, 2}:
                raise PayloadError("Knockoutopgør kræver én eller to fantasy-runder")
            target_size = knockout_size_for(len(ordered))
            qualifier_count = (
                target_size // group_count
                if qualifiers_per_group is None
                else qualifiers_per_group
            )
            if qualifier_count < 0:
                raise PayloadError("Antallet af kvalificerede fra hver gruppe må ikke være negativt")
            direct_slots = group_count * qualifier_count
            if qualifiers_per_group is None:
                knockout_size = target_size
            else:
                knockout_size = 2
                while knockout_size < max(2, direct_slots):
                    knockout_size *= 2
            if direct_slots > len(ordered) or knockout_size > len(ordered):
                raise PayloadError("Den valgte kvalifikation passer ikke til deltagerfeltet")
            group_end = final_round - int(log2(knockout_size)) * rounds_per_tie
            pools, fixtures = generate_group_stage_fixtures(
                ordered, start_round, group_end, group_count
            )
            if qualifier_count > min(map(len, pools)):
                raise PayloadError("En gruppe kan ikke kvalificere flere deltagere, end den indeholder")
            result = TournamentConfig(
                start_round=start_round,
                final_round=final_round,
                rounds_per_tie=rounds_per_tie,
                knockout_size=knockout_size,
                group_fixtures=fixtures,
                draw_seed=resolved_seed,
                template=template,
                match_points=match_points,
                seed_rule=seed_rule,
                seed_order=ordered,
                standings_tiebreakers=standings_tiebreakers,
                knockout_tiebreakers=knockout_tiebreakers,
                group_count=group_count,
                qualifiers_per_group=qualifier_count,
                bronze_match=bronze_match,
            )
    elif template == "league":
        fixtures = generate_league_fixtures(ordered, start_round, legs=league_legs)
        last_round = max(item.round_number for item in fixtures)
        if final_round is not None and final_round < last_round:
            raise PayloadError("Ligaen kan ikke afvikles i de valgte runder")
        result = TournamentConfig(
            start_round,
            last_round,
            1,
            2,
            fixtures,
            resolved_seed,
            template,
            match_points,
            seed_rule,
            ordered,
            standings_tiebreakers,
            knockout_tiebreakers,
            league_legs,
        )
    elif template == "swiss":
        count = swiss_rounds or ceil(log2(len(ordered)))
        if count < 1:
            raise PayloadError("Swiss kræver mindst én runde")
        participants = tuple(
            SwissParticipant(team_id, entry_seed=index)
            for index, team_id in enumerate(ordered, 1)
        )
        last_round = start_round + count - 1
        if final_round is not None and final_round < last_round:
            raise PayloadError("Swiss-turneringen kan ikke afvikles i de valgte runder")

        fixtures = generate_swiss_pairings(participants, start_round)
        result = TournamentConfig(
            start_round,
            last_round,
            1,
            2,
            fixtures,
            resolved_seed,
            template,
            match_points,
            seed_rule,
            ordered,
            standings_tiebreakers,
            knockout_tiebreakers,
            1,
            count,
        )
    else:
        abstract = build_double_elimination_bracket(len(ordered))
        first_round = [
            item
            for item in abstract
            if item.bracket == "winners" and item.bracket_round == 1
        ]
        fixtures = tuple(
            GroupFixture(
                start_round,
                ordered[item.team_a_seed - 1],
                (
                    ordered[item.team_b_seed - 1]
                    if item.team_b_seed is not None
                    else None
                ),
            )
            for item in first_round
            if item.team_a_seed is not None
        )
        levels = _double_elimination_levels(abstract)
        required = max(levels.values()) * rounds_per_tie
        last_round = start_round + required - 1
        if final_round is not None and final_round < last_round:
            raise PayloadError("Double elimination kan ikke afvikles i de valgte runder")

        result = TournamentConfig(
            start_round,
            last_round,
            rounds_per_tie,
            2,
            fixtures,
            resolved_seed,
            template,
            match_points,
            seed_rule,
            ordered,
            standings_tiebreakers,
            knockout_tiebreakers,
        )
    return _validate_template_config(result, team_ids)


def _validate_template_config(
    config: TournamentDefinition,
    team_ids: tuple[int, ...],
) -> TournamentDefinition:
    if config.template not in {"league", "swiss", "group_knockout", "double_elimination"}:
        raise PayloadError("Ukendt turneringsformat")
    if config.rounds_per_tie not in {1, 2}:
        raise PayloadError("En turneringskamp skal bruge én eller to fantasy-runder")
    if (
        len(config.match_points) != 3
        or any(isinstance(value, bool) or not isinstance(value, int) for value in config.match_points)
        or not (config.match_points[0] > config.match_points[1] >= config.match_points[2])
    ):
        raise PayloadError("Kamppoint skal angives i rækkefølgen sejr, uafgjort, nederlag")
    allowed_standings = {
        "score_difference",
        "score_for",
        "head_to_head",
        "buchholz",
        "entry_seed",
    }
    if (
        len(config.standings_tiebreakers) != len(set(config.standings_tiebreakers))
        or not set(config.standings_tiebreakers) <= allowed_standings
    ):
        raise PayloadError("Stillingens tie-breakers skal være kendte og entydige")
    allowed_knockout = {"last_round_growth", "overall_total", "higher_seed"}
    if (
        not config.knockout_tiebreakers
        or len(config.knockout_tiebreakers) != len(set(config.knockout_tiebreakers))
        or not set(config.knockout_tiebreakers) <= allowed_knockout
        or config.knockout_tiebreakers[-1] != "higher_seed"
    ):
        raise PayloadError("higher_seed skal være den sidste tie-breaker i knockout")
    if config.seed_rule not in {"random", "manual", "elo"}:
        raise PayloadError("Ukendt seedningsregel")
    wanted = set(team_ids)
    if (
        config.template != "group_knockout"
        and (
            len(config.seed_order) != len(team_ids)
            or set(config.seed_order) != wanted
        )
    ):
        raise PayloadError("Den frosne seedrækkefølge skal indeholde alle deltagere")
    for round_number in {item.round_number for item in config.group_fixtures}:
        seen: set[int] = set()
        for fixture in (item for item in config.group_fixtures if item.round_number == round_number):
            participants = (fixture.team_a_id,) + (
                (fixture.team_b_id,) if fixture.team_b_id is not None else ()
            )
            if any(team_id not in wanted or team_id in seen for team_id in participants):
                raise PayloadError("En deltager kan ikke spille to gange i samme fantasy-runde")
            seen.update(participants)
    if config.template == "league":
        counts: dict[tuple[int, int], int] = {}
        for item in config.group_fixtures:
            if item.team_b_id is None:
                continue
            pair = tuple(sorted((item.team_a_id, item.team_b_id)))
            counts[pair] = counts.get(pair, 0) + 1
        if set(counts.values()) != {config.league_legs}:
            raise PayloadError("Ligaprogrammet indeholder ikke det valgte antal indbyrdes kampe")
    if config.template == "swiss":
        expected_count = config.final_round - config.start_round + 1
        if config.swiss_rounds != expected_count:
            raise PayloadError("Antallet af Swiss-runder passer ikke til rundeintervallet")
        published_rounds = sorted(
            {item.round_number for item in config.group_fixtures}
        )
        if (
            not published_rounds
            or published_rounds[0] != config.start_round
            or published_rounds
            != list(range(config.start_round, published_rounds[-1] + 1))
            or published_rounds[-1] > config.final_round
        ):
            raise PayloadError("Swiss-runder skal publiceres fortløbende")
        for round_number in published_rounds:
            fixtures = tuple(
                item
                for item in config.group_fixtures
                if item.round_number == round_number
            )
            seen = {
                team_id
                for fixture in fixtures
                for team_id in (fixture.team_a_id, fixture.team_b_id)
                if team_id is not None
            }
            if seen != wanted or sum(item.is_bye for item in fixtures) > 1:
                raise PayloadError("Alle Swiss-deltagere skal have én kamp eller bye pr. runde")
    if config.template == "group_knockout":
        if config.group_count < 1 or config.group_count > 8:
            raise PayloadError("Antallet af grupper skal være 1-8")
        if (
            config.knockout_size < 2
            or config.knockout_size > len(team_ids)
            or config.knockout_size & (config.knockout_size - 1)
        ):
            raise PayloadError("Kvalifikationen skal give et deltagerfelt med en toerpotens")
        if config.group_count == 1 and config.qualifiers_per_group is None:
            return config
        if config.qualifiers_per_group is None or config.qualifiers_per_group < 0:
            raise PayloadError("Antallet af kvalificerede fra grupperne mangler eller er ugyldigt")
        group_end = (
            config.final_round
            - int(log2(config.knockout_size)) * config.rounds_per_tie
        )
        expected_rounds = set(range(config.start_round, group_end + 1))
        actual_rounds = {item.round_number for item in config.group_fixtures}
        if group_end < config.start_round or actual_rounds != expected_rounds:
            raise PayloadError("Gruppespillet kan ikke afvikles i de valgte fantasy-runder")
        indexes = {item.group_index for item in config.group_fixtures}
        if indexes != set(range(config.group_count)):
            raise PayloadError("Alle konfigurerede grupper skal have publicerede kampe")
        for round_number in expected_rounds:
            seen = {
                team_id
                for fixture in config.group_fixtures
                if fixture.round_number == round_number
                for team_id in (fixture.team_a_id, fixture.team_b_id)
                if team_id is not None
            }
            if seen != wanted:
                raise PayloadError("Alle deltagere skal have en kamp eller bye i hver grupperunde")
        return config
    return config


def _double_elimination_state(
    group: GroupDefinition,
    snapshots: SnapshotIndex,
    as_of_round: int,
) -> TournamentState:
    """Resolve a frozen two-loss bracket from complete cached fantasy rounds."""

    assert group.tournament is not None
    config = group.tournament
    as_of = max(0, as_of_round)
    abstract = build_double_elimination_bracket(len(config.seed_order))
    levels = _double_elimination_levels(abstract)
    _, names, owners = _member_maps(group, snapshots)
    seeds = {team_id: index for index, team_id in enumerate(config.seed_order, 1)}
    outcomes: dict[str, tuple[bool, int | None, int | None]] = {}
    matches: list[KnockoutMatch] = []
    losses = {team_id: 0 for team_id in config.seed_order}
    wins = {team_id: 0 for team_id in config.seed_order}
    growth_for = {team_id: 0 for team_id in config.seed_order}
    growth_against = {team_id: 0 for team_id in config.seed_order}
    elimination_order: list[int] = []
    last_losers_bracket_loser: int | None = None
    champion: int | None = None

    def resolve_slot(
        seed: int | None, source: str | None
    ) -> tuple[bool, int | None]:
        if source is None:
            return True, config.seed_order[seed - 1] if seed is not None else None
        kind, source_match = source.split(":", 1)
        complete, winner, loser = outcomes[source_match]
        if not complete:
            return False, None
        return True, winner if kind == "winner" else loser

    for abstract_match in abstract:
        if abstract_match.reset_final:
            grand_final = next(
                (item for item in reversed(matches) if item.stage == "Finale"),
                None,
            )
            if grand_final is None or not grand_final.complete:
                continue
            if grand_final.winner_id == grand_final.team_a_id:
                champion = grand_final.winner_id
                continue

        a_ready, a_id = resolve_slot(
            abstract_match.team_a_seed, abstract_match.source_a
        )
        b_ready, b_id = resolve_slot(
            abstract_match.team_b_seed, abstract_match.source_b
        )
        first_round = (
            config.start_round
            + (levels[abstract_match.match_id] - 1) * config.rounds_per_tie
        )
        rounds = tuple(range(first_round, first_round + config.rounds_per_tie))
        if abstract_match.bracket == "winners":
            stage = f"Vinderbracket · runde {abstract_match.bracket_round}"
        elif abstract_match.bracket == "losers":
            stage = f"Taberbracket · runde {abstract_match.bracket_round}"
        else:
            stage = "Finale"
        match_index = (
            int(abstract_match.match_id.rsplit("-", 1)[1])
            if "-" in abstract_match.match_id
            else abstract_match.bracket_round
        )

        ready = a_ready and b_ready
        a_total: int | None = None
        b_total: int | None = None
        winner: int | None = None
        loser: int | None = None
        complete = False
        if ready and (a_id is None or b_id is None):
            complete = True
            winner = a_id if a_id is not None else b_id
            a_total = 0 if a_id is not None else None
            b_total = 0 if b_id is not None else None
        elif ready and a_id is not None and b_id is not None:
            a_total = 0
            b_total = 0
            for round_number in rounds:
                if round_number > as_of:
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
            if complete:
                if a_total > b_total:
                    winner, loser = a_id, b_id
                elif b_total > a_total:
                    winner, loser = b_id, a_id
                else:
                    winner = resolve_knockout_tie(
                        config,
                        snapshots,
                        group.game,
                        a_id,
                        b_id,
                        seeds[a_id],
                        seeds[b_id],
                        rounds[-1],
                    )
                    loser = b_id if winner == a_id else a_id
                assert winner is not None and loser is not None
                wins[winner] += 1
                losses[loser] += 1
                growth_for[a_id] += a_total
                growth_for[b_id] += b_total
                growth_against[a_id] += b_total
                growth_against[b_id] += a_total
                if losses[loser] == 2:
                    elimination_order.append(loser)
                if abstract_match.bracket == "losers":
                    last_losers_bracket_loser = loser

        outcomes[abstract_match.match_id] = (complete, winner, loser)
        matches.append(
            KnockoutMatch(
                stage,
                match_index,
                rounds,
                a_id,
                b_id,
                names.get(a_id) if a_id is not None else None,
                names.get(b_id) if b_id is not None else None,
                seeds.get(a_id) if a_id is not None else None,
                seeds.get(b_id) if b_id is not None else None,
                a_total,
                b_total,
                winner,
                complete,
            )
        )
        if abstract_match.match_id == "GF1" and complete and winner == a_id:
            champion = winner
        elif abstract_match.match_id == "GF2" and complete:
            champion = winner

    final_match = next(
        (item for item in reversed(matches) if item.stage == "Finale" and item.complete),
        None,
    )
    runner_up = None
    if champion is not None and final_match is not None:
        runner_up = (
            final_match.team_b_id
            if champion == final_match.team_a_id
            else final_match.team_a_id
        )

    all_ids = tuple(config.seed_order)
    if champion is not None:
        ordered_ids: list[int] = []
        for team_id in (champion, runner_up, last_losers_bracket_loser):
            if team_id is not None and team_id not in ordered_ids:
                ordered_ids.append(team_id)
        for team_id in reversed(elimination_order):
            if team_id not in ordered_ids:
                ordered_ids.append(team_id)
        ordered_ids.extend(team_id for team_id in all_ids if team_id not in ordered_ids)
    else:
        ordered_ids = sorted(
            all_ids,
            key=lambda team_id: (
                losses[team_id] >= 2,
                losses[team_id],
                -wins[team_id],
                -(growth_for[team_id] - growth_against[team_id]),
                seeds[team_id],
                team_id,
            ),
        )
    win_points, _, loss_points = config.match_points
    standings = tuple(
        TournamentStanding(
            rank=index,
            team_id=team_id,
            team_name=names[team_id],
            owner_name=owners[team_id],
            played=wins[team_id] + losses[team_id],
            wins=wins[team_id],
            draws=0,
            losses=losses[team_id],
            growth_for=growth_for[team_id],
            growth_against=growth_against[team_id],
            growth_difference=growth_for[team_id] - growth_against[team_id],
            points=wins[team_id] * win_points + losses[team_id] * loss_points,
            entry_seed=seeds[team_id],
        )
        for index, team_id in enumerate(ordered_ids, 1)
    )
    eliminated = frozenset(
        team_id for team_id, loss_count in losses.items() if loss_count >= 2
    )
    active = (
        frozenset()
        if champion is not None
        else frozenset(team_id for team_id in all_ids if team_id not in eliminated)
    )
    missing = tuple(
        item
        for item in matches
        if item.round_numbers[-1] <= as_of
        and item.team_a_id is not None
        and item.team_b_id is not None
        and not item.complete
    )
    warnings = (
        (f"{len(missing)} double-elimination matches await round data",)
        if missing
        else ()
    )
    if champion is not None:
        phase = "Afsluttet"
    elif as_of < config.start_round:
        phase = "Ikke startet"
    else:
        phase = next(
            (
                item.stage
                for item in matches
                if not item.complete and item.round_numbers[0] <= as_of <= item.round_numbers[-1]
            ),
            "Double elimination",
        )
    return TournamentState(
        as_of,
        phase,
        (),
        standings,
        tuple(matches),
        active,
        eliminated,
        champion,
        warnings,
    )


def _build_template_state(
    group: GroupDefinition,
    snapshots: SnapshotIndex,
    as_of_round: int,
    *,
    require_full_schedule: bool = True,
) -> TournamentState:
    assert group.tournament is not None
    matches = _group_results(group, snapshots, as_of_round)
    if group.tournament.template == "double_elimination":
        return _double_elimination_state(group, snapshots, as_of_round)
    standings = _standings(group, snapshots, matches)
    required = tuple(item for item in matches if not item.fixture.is_bye)
    expected_rounds = set(
        range(group.tournament.start_round, group.tournament.final_round + 1)
    )
    published_rounds = {item.fixture.round_number for item in matches}
    full_schedule = (
        group.tournament.template != "swiss"
        or not require_full_schedule
        or published_rounds == expected_rounds
    )
    complete = (
        as_of_round >= group.tournament.final_round
        and full_schedule
        and bool(required)
        and all(item.complete for item in matches)
    )
    champion = standings[0].team_id if complete and standings else None
    all_ids = frozenset(item.team_id for item in group.teams)
    phase_names = {
        "league": "Liga",
        "swiss": "Swiss",
        "double_elimination": "Double elimination",
    }
    phase = "Afsluttet" if champion is not None else phase_names[group.tournament.template]
    missing = tuple(
        item
        for item in required
        if item.fixture.round_number <= as_of_round and not item.complete
    )
    warnings: tuple[str, ...] = ()
    if missing:
        warnings += (f"{len(missing)} publicerede kampe afventer rundedata",)
    if group.tournament.template == "swiss" and not full_schedule:
        remaining = len(expected_rounds - published_rounds)
        warnings += (f"{remaining} Swiss-runder mangler at blive publiceret",)
    return TournamentState(
        max(0, as_of_round),
        phase,
        matches,
        standings,
        (),
        all_ids if champion is None else frozenset(),
        frozenset(),
        champion,
        warnings,
    )
