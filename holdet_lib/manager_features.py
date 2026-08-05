"""Manager identities, ratings, achievements, stories and head-to-head builders."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime
from itertools import combinations
from typing import Iterable, Literal

from .errors import PayloadError
from .groups import GroupDefinition
from .hall_of_fame import (
    HallOfFameEvent,
    HallOfFamePlacement,
    current_event_revisions,
)
from .hub_settings import (
    HubSettings,
    build_effective_manager_settings,
    resolve_manager_identity,
)
from .tournament import build_tournament_state
from .storage import SnapshotIndex, TeamSnapshot


# ManagerEvent is the canonical schema-2 event type; the historical
# HallOfFameEvent name remains a compatibility alias to the same value.
ManagerEvent = HallOfFameEvent


@dataclass(frozen=True, slots=True)
class ManagerRoundResult:
    manager_id: str
    manager_name: str
    game_locale: str
    game_slug: str
    round_number: int
    team_id: int
    team_name: str
    total: int
    change: int
    rank: int
    group_ids: tuple[str, ...]
    ended_at: datetime
    used_time_fallback: bool = False
    complete: bool = True


@dataclass(frozen=True, slots=True)
class ManagerRating:
    manager_id: str
    manager_name: str
    rating: float
    periods: int
    wins: int
    draws: int
    losses: int
    last_change: float
    provisional: bool


@dataclass(frozen=True, slots=True)
class ManagerCareer:
    manager_id: str
    manager_name: str
    gold: int = 0
    silver: int = 0
    bronze: int = 0
    titles: int = 0
    podiums: int = 0
    wooden_spoons: int = 0
    round_wins: int = 0
    longest_win_streak: int = 0
    first_place_rounds: int = 0
    longest_first_place_streak: int = 0


@dataclass(frozen=True, slots=True)
class HeadToHeadMeeting:
    meeting_id: str
    track: Literal["official", "shared_round"]
    game_slug: str
    competition_id: str
    round_numbers: tuple[int, ...]
    manager_score: int
    opponent_score: int
    occurred_at: datetime
    game_locale: str = "da"

    @property
    def result(self) -> int:
        return (self.manager_score > self.opponent_score) - (
            self.manager_score < self.opponent_score
        )


@dataclass(frozen=True, slots=True)
class ManagerHeadToHead:
    manager_id: str
    opponent_id: str
    official: tuple[HeadToHeadMeeting, ...]
    shared_rounds: tuple[HeadToHeadMeeting, ...]

    def meetings(self, track: str = "official") -> tuple[HeadToHeadMeeting, ...]:
        return self.official if track == "official" else self.shared_rounds

    def summary(self, track: str = "official") -> tuple[int, int, int]:
        meetings = self.meetings(track)
        wins = sum(item.result > 0 for item in meetings)
        draws = sum(item.result == 0 for item in meetings)
        losses = sum(item.result < 0 for item in meetings)
        return wins, draws, losses

    def total_growth(self, track: str = "official") -> tuple[int, int]:
        meetings = self.meetings(track)
        return (
            sum(item.manager_score for item in meetings),
            sum(item.opponent_score for item in meetings),
        )

    def biggest_win(self, track: str = "official") -> HeadToHeadMeeting | None:
        wins = tuple(item for item in self.meetings(track) if item.result > 0)
        return max(
            wins,
            key=lambda item: (
                item.manager_score - item.opponent_score,
                -item.occurred_at.timestamp(),
                item.meeting_id,
            ),
            default=None,
        )

    def closest_meeting(
        self,
        track: str = "official",
    ) -> HeadToHeadMeeting | None:
        return min(
            self.meetings(track),
            key=lambda item: (
                abs(item.manager_score - item.opponent_score),
                item.occurred_at,
                item.meeting_id,
            ),
            default=None,
        )


@dataclass(frozen=True, slots=True)
class RoundAward:
    kind: Literal["comeback", "growth", "closest"]
    title: str
    manager_ids: tuple[str, ...]
    manager_names: tuple[str, ...]
    value: int
    detail: str
    preliminary: bool = False


@dataclass(frozen=True, slots=True)
class RoundStory:
    game_slug: str
    round_number: int
    headline: str
    paragraphs: tuple[str, ...]
    awards: tuple[RoundAward, ...]
    preliminary: bool = False
    game_locale: str = "da"


@dataclass(frozen=True, slots=True)
class _Candidate:
    manager_id: str
    manager_name: str
    team_id: int
    team_name: str
    total: int
    change: int
    ended_at: datetime
    fallback: bool
    complete: bool


@dataclass(frozen=True, slots=True)
class _Period:
    game_locale: str
    game_slug: str
    round_number: int
    ended_at: datetime
    results: tuple[ManagerRoundResult, ...]
    pairs: tuple[tuple[str, str], ...]


def manager_events_from_hall_of_fame(
    events: Iterable[HallOfFameEvent],
) -> tuple[ManagerEvent, ...]:
    """Expose the legacy name as the canonical manager-event type."""

    return tuple(events)


def _identity(
    group: GroupDefinition,
    snapshot: TeamSnapshot,
    settings: HubSettings,
) -> tuple[str, str]:
    member = next(item for item in group.teams if item.team_id == snapshot.team.reference.team_id)
    return resolve_manager_identity(
        settings,
        owner_user_id=snapshot.team.owner_user_id,
        account_user_id=member.account_user_id,
        account_key=member.account_key,
        owner_name=snapshot.team.owner_name,
        fallback_key=f"{group.game.locale}:{group.game.slug}:team:{snapshot.team.reference.team_id}",
    )


def _build_periods(
    groups: Iterable[GroupDefinition],
    snapshots: SnapshotIndex,
    settings: HubSettings,
    *,
    include_incomplete: bool = False,
) -> tuple[_Period, ...]:
    group_tuple = tuple(groups)
    settings = build_effective_manager_settings(settings, group_tuple, snapshots)
    candidates: dict[tuple[str, str, int], dict[str, _Candidate]] = defaultdict(dict)
    shared_pairs: dict[tuple[str, str, int], set[tuple[str, str]]] = defaultdict(set)
    memberships: dict[tuple[str, str, int, str], set[str]] = defaultdict(set)

    for group in group_tuple:
        all_team_ids = tuple(item.team_id for item in group.teams)
        scopes = (all_team_ids,)
        if (
            group.tournament is not None
            and group.tournament.template == "group_knockout"
            and group.tournament.group_count > 1
        ):
            pools = tuple(
                tuple(sorted({
                    team_id
                    for fixture in group.tournament.group_fixtures
                    if fixture.group_index == group_index
                    for team_id in (
                        fixture.team_a_id,
                        fixture.team_b_id,
                    )
                    if team_id is not None
                }))
                for group_index in range(group.tournament.group_count)
            )
            scopes = tuple(pool for pool in pools if pool)

        for team_ids in scopes:
            for round_number in snapshots.rounds_for(
                group.game,
                team_ids,
            ):
                located = {
                    team_id: snapshots.summary_for(
                        group.game,
                        team_id,
                        round_number,
                    )
                    for team_id in team_ids
                }
                complete = bool(located) and all(
                    item is not None
                    and item[1].round_status == "complete"
                    for item in located.values()
                )
                if not complete and not include_incomplete:
                    continue
                period_key = (
                    group.game.locale.casefold(),
                    group.game.slug,
                    round_number,
                )
                group_managers: set[str] = set()
                for team_id, item in located.items():
                    if item is None:
                        continue
                    snapshot, summary = item
                    manager_id, manager_name = _identity(
                        group,
                        snapshot,
                        settings,
                    )
                    group_managers.add(manager_id)
                    ended_at = summary.round_end_at or snapshot.generated_at
                    candidate = _Candidate(
                        manager_id,
                        manager_name,
                        team_id,
                        snapshot.team.team_name,
                        summary.total,
                        summary.change,
                        ended_at,
                        summary.round_end_at is None,
                        complete,
                    )
                    previous = candidates[period_key].get(manager_id)
                    if previous is None or (
                        candidate.change,
                        candidate.total,
                        -candidate.team_id,
                    ) > (
                        previous.change,
                        previous.total,
                        -previous.team_id,
                    ):
                        candidates[period_key][manager_id] = candidate
                    memberships[
                        (*period_key, manager_id)
                    ].add(group.group_id)
                for first, second in combinations(
                    sorted(group_managers),
                    2,
                ):
                    if first != second:
                        shared_pairs[period_key].add((first, second))

    periods: list[_Period] = []
    for key, managers in candidates.items():
        rows = sorted(
            managers.values(),
            key=lambda item: (-item.total, -item.change, item.manager_id),
        )
        ranked: list[ManagerRoundResult] = []
        previous_total: int | None = None
        previous_rank = 0
        for position, item in enumerate(rows, 1):
            rank = previous_rank if item.total == previous_total else position
            ranked.append(
                ManagerRoundResult(
                    item.manager_id,
                    item.manager_name,
                    key[0],
                    key[1],
                    key[2],
                    item.team_id,
                    item.team_name,
                    item.total,
                    item.change,
                    rank,
                    tuple(sorted(memberships[(*key, item.manager_id)])),
                    item.ended_at,
                    item.fallback,
                    item.complete,
                )
            )
            previous_total = item.total
            previous_rank = rank
        ended_at = max(item.ended_at for item in rows)
        periods.append(
            _Period(
                key[0],
                key[1],
                key[2],
                ended_at,
                tuple(ranked),
                tuple(sorted(shared_pairs[key])),
            )
        )
    return tuple(
        sorted(
            periods,
            key=lambda item: (
                item.ended_at,
                item.game_locale,
                item.game_slug,
                item.round_number,
            ),
        )
    )


def build_manager_round_results(
    groups: Iterable[GroupDefinition],
    snapshots: SnapshotIndex,
    settings: HubSettings,
    *,
    include_incomplete: bool = False,
) -> tuple[ManagerRoundResult, ...]:
    return tuple(
        result
        for period in _build_periods(
            groups, snapshots, settings, include_incomplete=include_incomplete
        )
        for result in period.results
    )


def build_manager_ratings(
    groups: Iterable[GroupDefinition],
    snapshots: SnapshotIndex,
    settings: HubSettings,
    *,
    initial: float = 1500.0,
    k_factor: float = 32.0,
) -> tuple[ManagerRating, ...]:
    """Calculate period-batched Elo from deduplicated shared group rounds."""

    ratings: dict[str, float] = {}
    names: dict[str, str] = {}
    periods: dict[str, int] = defaultdict(int)
    records: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    last_change: dict[str, float] = defaultdict(float)
    for period in _build_periods(groups, snapshots, settings):
        by_manager = {item.manager_id: item for item in period.results}
        outcomes: dict[str, list[tuple[float, str]]] = defaultdict(list)
        for first, second in period.pairs:
            if first not in by_manager or second not in by_manager:
                continue
            first_score = by_manager[first].change
            second_score = by_manager[second].change
            actual = 1.0 if first_score > second_score else 0.0 if first_score < second_score else 0.5
            outcomes[first].append((actual, second))
            outcomes[second].append((1.0 - actual, first))
            if actual == 1.0:
                records[first][0] += 1
                records[second][2] += 1
            elif actual == 0.0:
                records[second][0] += 1
                records[first][2] += 1
            else:
                records[first][1] += 1
                records[second][1] += 1
        before = {manager: ratings.get(manager, initial) for manager in by_manager}
        for manager, comparisons in outcomes.items():
            actual = sum(value for value, _ in comparisons) / len(comparisons)
            expected = sum(
                1.0 / (1.0 + 10.0 ** ((before[opponent] - before[manager]) / 400.0))
                for _, opponent in comparisons
            ) / len(comparisons)
            delta = k_factor * (actual - expected)
            ratings[manager] = before[manager] + delta
            last_change[manager] = delta
            periods[manager] += 1
        for item in period.results:
            names[item.manager_id] = item.manager_name
            ratings.setdefault(item.manager_id, initial)

    rows = [
        ManagerRating(
            manager,
            names.get(manager, manager),
            round(value, 1),
            periods[manager],
            records[manager][0],
            records[manager][1],
            records[manager][2],
            round(last_change[manager], 1),
            periods[manager] < 5,
        )
        for manager, value in ratings.items()
    ]
    return tuple(
        sorted(rows, key=lambda item: (-item.rating, item.manager_name.casefold(), item.manager_id))
    )


def _longest_consecutive(values: Iterable[tuple[str, int]]) -> int:
    longest = current = 0
    previous: tuple[str, int] | None = None
    for value in sorted(set(values)):
        current = current + 1 if previous and value[0] == previous[0] and value[1] == previous[1] + 1 else 1
        longest = max(longest, current)
        previous = value
    return longest


def build_manager_careers(
    events: Iterable[HallOfFameEvent],
    round_results: Iterable[ManagerRoundResult] = (),
) -> tuple[ManagerCareer, ...]:
    stats: dict[str, dict[str, object]] = defaultdict(
        lambda: {
            "name": "",
            "gold": 0,
            "silver": 0,
            "bronze": 0,
            "titles": 0,
            "podiums": 0,
            "spoons": 0,
            "round_wins": 0,
            "wins": [],
            "firsts": [],
        }
    )
    events = current_event_revisions(tuple(events))
    for event in events:
        if not event.complete or not event.placements:
            continue
        placements = sorted(
            event.placements,
            key=lambda item: (
                item.rank,
                -(item.value if item.value is not None else -10**18),
                item.manager_id,
            ),
        )
        for placement in placements:
            item = stats[placement.manager_id]
            item["name"] = placement.manager_name
        if event.kind == "round_win":
            winner = placements[0]
            item = stats[winner.manager_id]
            item["round_wins"] = int(item["round_wins"]) + 1
            if event.round_number is not None:
                wins = item["wins"]
                assert isinstance(wins, list)
                wins.append((f"{event.game_locale.casefold()}:{event.game_slug}", event.round_number))
            continue
        for position, placement in enumerate(placements, 1):
            item = stats[placement.manager_id]
            if position == 1:
                item["gold"] = int(item["gold"]) + 1
                item["titles"] = int(item["titles"]) + 1
            elif position == 2:
                item["silver"] = int(item["silver"]) + 1
            elif position == 3:
                item["bronze"] = int(item["bronze"]) + 1
            if position <= 3:
                item["podiums"] = int(item["podiums"]) + 1
        spoon = stats[placements[-1].manager_id]
        spoon["spoons"] = int(spoon["spoons"]) + 1
    for result in round_results:
        item = stats[result.manager_id]
        item["name"] = result.manager_name
        if result.rank == 1:
            cast = item["firsts"]
            assert isinstance(cast, list)
            cast.append((f"{result.game_locale.casefold()}:{result.game_slug}", result.round_number))
    rows = []
    for manager, item in stats.items():
        wins = item["wins"]
        firsts = item["firsts"]
        assert isinstance(wins, list) and isinstance(firsts, list)
        rows.append(
            ManagerCareer(
                manager,
                str(item["name"]) or manager,
                int(item["gold"]),
                int(item["silver"]),
                int(item["bronze"]),
                int(item["titles"]),
                int(item["podiums"]),
                int(item["spoons"]),
                int(item["round_wins"]),
                _longest_consecutive(wins),
                len(set(firsts)),
                _longest_consecutive(firsts),
            )
        )
    return tuple(sorted(rows, key=lambda item: (-item.titles, -item.podiums, item.manager_name.casefold())))


def build_manager_head_to_head(
    manager_id: str,
    opponent_id: str,
    groups: Iterable[GroupDefinition],
    snapshots: SnapshotIndex,
    settings: HubSettings,
) -> ManagerHeadToHead:
    group_tuple = tuple(groups)
    settings = build_effective_manager_settings(settings, group_tuple, snapshots)
    if manager_id == opponent_id:
        return ManagerHeadToHead(manager_id, opponent_id, (), ())

    shared: list[HeadToHeadMeeting] = []
    for period in _build_periods(group_tuple, snapshots, settings):
        pair = tuple(sorted((manager_id, opponent_id)))
        if pair not in period.pairs:
            continue
        values = {item.manager_id: item for item in period.results}
        if manager_id not in values or opponent_id not in values:
            continue
        first, second = values[manager_id], values[opponent_id]
        competition = ",".join(
            sorted(set(first.group_ids) & set(second.group_ids))
        )
        shared.append(
            HeadToHeadMeeting(
                (
                    f"{period.game_locale}:{period.game_slug}:"
                    f"{period.round_number}:{pair[0]}:{pair[1]}"
                ),
                "shared_round",
                period.game_slug,
                competition,
                (period.round_number,),
                first.change,
                second.change,
                period.ended_at,
                period.game_locale,
            )
        )

    official: list[HeadToHeadMeeting] = []
    for group in group_tuple:
        if group.tournament is None:
            continue
        published_fixtures = (
            ()
            if group.tournament.template == "double_elimination"
            else group.tournament.group_fixtures
        )
        for index, fixture in enumerate(published_fixtures, 1):
            if fixture.team_b_id is None:
                continue
            first = snapshots.summary_for(
                group.game,
                fixture.team_a_id,
                fixture.round_number,
            )
            second = snapshots.summary_for(
                group.game,
                fixture.team_b_id,
                fixture.round_number,
            )
            if (
                first is None
                or second is None
                or first[1].round_status != "complete"
                or second[1].round_status != "complete"
            ):
                continue
            first_manager = _identity(group, first[0], settings)[0]
            second_manager = _identity(group, second[0], settings)[0]
            if {first_manager, second_manager} != {manager_id, opponent_id}:
                continue
            a_score, b_score = first[1].change, second[1].change
            official.append(
                HeadToHeadMeeting(
                    (
                        f"{group.game.locale.casefold()}:{group.group_id}:"
                        f"fixture:{index}"
                    ),
                    "official",
                    group.game.slug,
                    group.group_id,
                    (fixture.round_number,),
                    a_score if first_manager == manager_id else b_score,
                    b_score if first_manager == manager_id else a_score,
                    max(
                        first[1].round_end_at or first[0].generated_at,
                        second[1].round_end_at or second[0].generated_at,
                    ),
                    group.game.locale.casefold(),
                )
            )
        state = build_tournament_state(
            group,
            snapshots,
            group.tournament.final_round,
        )
        for index, match in enumerate(state.knockout_matches, 1):
            if (
                not match.complete
                or match.team_a_id is None
                or match.team_b_id is None
                or match.team_a_change is None
                or match.team_b_change is None
            ):
                continue
            last_round = match.round_numbers[-1]
            first = snapshots.summary_for(group.game, match.team_a_id, last_round)
            second = snapshots.summary_for(group.game, match.team_b_id, last_round)
            if first is None or second is None:
                continue
            first_manager = _identity(group, first[0], settings)[0]
            second_manager = _identity(group, second[0], settings)[0]
            if {first_manager, second_manager} != {manager_id, opponent_id}:
                continue
            official.append(
                HeadToHeadMeeting(
                    (
                        f"{group.game.locale.casefold()}:{group.group_id}:"
                        f"knockout:{match.stage}:{index}"
                    ),
                    "official",
                    group.game.slug,
                    group.group_id,
                    match.round_numbers,
                    (
                        match.team_a_change
                        if first_manager == manager_id
                        else match.team_b_change
                    ),
                    (
                        match.team_b_change
                        if first_manager == manager_id
                        else match.team_a_change
                    ),
                    max(
                        first[1].round_end_at or first[0].generated_at,
                        second[1].round_end_at or second[0].generated_at,
                    ),
                    group.game.locale.casefold(),
                )
            )
    return ManagerHeadToHead(
        manager_id,
        opponent_id,
        tuple(
            sorted(
                official,
                key=lambda item: (
                    item.occurred_at,
                    item.game_locale,
                    item.meeting_id,
                ),
            )
        ),
        tuple(
            sorted(
                shared,
                key=lambda item: (
                    item.occurred_at,
                    item.game_locale,
                    item.meeting_id,
                ),
            )
        ),
    )



def _round_is_complete(
    groups: tuple[GroupDefinition, ...],
    snapshots: SnapshotIndex,
    round_number: int,
) -> bool:
    expected = [
        (group, member.team_id)
        for group in groups
        for member in group.teams
    ]
    return bool(expected) and all(
        (
            located := snapshots.summary_for(
                group.game,
                team_id,
                round_number,
            )
        )
        is not None
        and located[1].round_status == "complete"
        for group, team_id in expected
    )


def build_round_awards(
    groups: Iterable[GroupDefinition],
    snapshots: SnapshotIndex,
    settings: HubSettings,
    round_number: int,
) -> tuple[RoundAward, ...]:
    """Build deterministic round awards from cached snapshots only."""

    group_tuple = tuple(groups)
    settings = build_effective_manager_settings(settings, group_tuple, snapshots)
    current = [
        item
        for item in build_manager_round_results(group_tuple, snapshots, settings, include_incomplete=True)
        if item.round_number == round_number
    ]
    previous = {
        (item.game_locale, item.game_slug, item.manager_id): item
        for item in build_manager_round_results(group_tuple, snapshots, settings, include_incomplete=True)
        if item.round_number == round_number - 1
    }
    preliminary = not _round_is_complete(group_tuple, snapshots, round_number)
    awards: list[RoundAward] = []
    if current:
        growth = min(current, key=lambda item: (-item.change, item.manager_id))
        awards.append(
            RoundAward(
                "growth",
                "H\u00f8jeste v\u00e6kst",
                (growth.manager_id,),
                (growth.manager_name,),
                growth.change,
                f"{growth.manager_name} voksede {growth.change:+d}.",
                preliminary,
            )
        )
        comeback_rows = [
            (previous[(item.game_locale, item.game_slug, item.manager_id)].rank - item.rank, item)
            for item in current
            if (item.game_locale, item.game_slug, item.manager_id) in previous
        ]
        if comeback_rows:
            places, comeback = min(
                comeback_rows,
                key=lambda value: (-value[0], -value[1].change, value[1].rank, value[1].manager_id),
            )
            awards.append(
                RoundAward(
                    "comeback",
                    "St\u00f8rste comeback",
                    (comeback.manager_id,),
                    (comeback.manager_name,),
                    places,
                    f"{comeback.manager_name} vandt {places} placeringer.",
                    preliminary,
                )
            )
        closest: tuple[int, ManagerRoundResult, ManagerRoundResult] | None = None
        ordinary_group_ids = {
            group.group_id for group in group_tuple if group.tournament is None
        }
        scheduled_pairs: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for group in group_tuple:
            if group.tournament is None:
                continue
            for fixture in group.tournament.group_fixtures:
                if fixture.round_number != round_number or fixture.team_b_id is None:
                    continue
                first_snapshot = snapshots.newest(group.game, fixture.team_a_id)
                second_snapshot = snapshots.newest(group.game, fixture.team_b_id)
                if first_snapshot is None or second_snapshot is None:
                    continue
                pair = tuple(sorted((
                    _identity(group, first_snapshot, settings)[0],
                    _identity(group, second_snapshot, settings)[0],
                )))
                if pair[0] != pair[1]:
                    scheduled_pairs[(group.game.locale.casefold(), group.game.slug)].add(pair)

        by_period: dict[tuple[str, str, int], list[ManagerRoundResult]] = defaultdict(list)
        for item in current:
            by_period[(item.game_locale, item.game_slug, item.round_number)].append(item)
        for rows in by_period.values():
            for first, second in combinations(rows, 2):
                shared_groups = set(first.group_ids) & set(second.group_ids)
                pair = tuple(sorted((first.manager_id, second.manager_id)))
                if not (
                    shared_groups & ordinary_group_ids
                    or pair in scheduled_pairs[(first.game_locale, first.game_slug)]
                ):
                    continue
                candidate = (abs(first.change - second.change), first, second)
                if closest is None or (candidate[0], first.manager_id, second.manager_id) < (
                    closest[0],
                    closest[1].manager_id,
                    closest[2].manager_id,
                ):
                    closest = candidate
        if closest:
            difference, first, second = closest
            awards.append(
                RoundAward(
                    "closest",
                    "T\u00e6tteste duel",
                    (first.manager_id, second.manager_id),
                    (first.manager_name, second.manager_name),
                    difference,
                    f"{first.manager_name} og {second.manager_name} var kun {difference} fra hinanden.",
                    preliminary,
                )
            )
    return tuple(awards)


def build_round_story(
    groups: Iterable[GroupDefinition],
    snapshots: SnapshotIndex,
    settings: HubSettings,
    game_slug: str,
    round_number: int,
    *,
    game_locale: str | None = None,
) -> RoundStory:
    matching_groups = tuple(
        group
        for group in groups
        if group.game.slug == game_slug
    )
    matching_locales = {
        group.game.locale.casefold()
        for group in matching_groups
    }
    if game_locale is None and len(matching_locales) > 1:
        raise PayloadError(
            "game_locale skal angives, når samme game_slug findes "
            "på flere sprog"
        )
    resolved_locale = (
        game_locale.casefold()
        if game_locale is not None
        else next(iter(matching_locales), "da")
    )
    selected_groups = tuple(
        group
        for group in matching_groups
        if group.game.locale.casefold() == resolved_locale
    )
    awards = build_round_awards(selected_groups, snapshots, settings, round_number)
    all_results = build_manager_round_results(
        selected_groups, snapshots, settings, include_incomplete=True
    )
    results = [
        item for item in all_results if item.round_number == round_number
    ]
    preliminary = not _round_is_complete(
        selected_groups, snapshots, round_number
    )
    if not results:
        return RoundStory(
            game_slug,
            round_number,
            f"Runde {round_number} mangler data",
            ("Der er endnu ikke cached data nok til at skrive historien.",),
            (),
            True,
            resolved_locale,
        )
    winner = min(results, key=lambda item: (-item.change, item.manager_id))
    paragraphs = [f"{winner.manager_name} satte rundens h\u00f8jeste v\u00e6kst med {winner.change:+d}."]
    leader = min(results, key=lambda item: (item.rank, item.manager_id))
    previous_leaders = [
        item
        for item in all_results
        if item.round_number == round_number - 1 and item.rank == 1
    ]
    if previous_leaders:
        previous_leader = min(previous_leaders, key=lambda item: item.manager_id)
        if previous_leader.manager_id != leader.manager_id:
            paragraphs.append(
                f"Føringen skiftede fra {previous_leader.manager_name} "
                f"til {leader.manager_name}."
            )
    historical_growth = [
        item.change for item in all_results if item.round_number < round_number
    ]
    if historical_growth and winner.change > max(historical_growth):
        paragraphs.append(
            f"{winner.manager_name} satte samtidig ny vækstrekord."
        )
    leading_rounds = sorted({
        item.round_number
        for item in all_results
        if item.manager_id == leader.manager_id
        and item.rank == 1
        and item.round_number <= round_number
    })
    streak = 0
    expected = round_number
    for candidate in reversed(leading_rounds):
        if candidate != expected:
            break
        streak += 1
        expected -= 1
    if streak >= 2:
        paragraphs.append(
            f"{leader.manager_name} har nu ført i {streak} runder i træk."
        )
    paragraphs.extend(item.detail for item in awards if item.kind != "growth")
    prefix = "Forel\u00f8big: " if preliminary else ""
    return RoundStory(
        game_slug,
        round_number,
        f"{prefix}{winner.manager_name} tog runden",
        tuple(paragraphs),
        awards,
        preliminary,
        resolved_locale,
    )
