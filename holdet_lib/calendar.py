"""Cache-only fantasy calendar builders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Literal
from urllib.parse import urlencode

from .game_metadata import GameMetadata
from .groups import GroupDefinition
from .tournament import _double_elimination_levels, build_double_elimination_bracket


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    event_id: str
    kind: Literal["fixture", "group_round"]
    title: str
    game_locale: str
    game_slug: str
    group_id: str
    round_number: int
    participant_ids: tuple[int, ...]
    participant_names: tuple[str, ...]
    start: datetime | None
    deadline: datetime | None
    end: datetime | None
    internal_url: str
    official_url: str | None = None

    @property
    def missing_time(self) -> bool:
        return self.start is None or self.deadline is None or self.end is None


def _calendar_time_key(value: datetime | None) -> tuple[int, int, int, int, int, int, int]:
    """Sort even sentinel dates that cannot be converted by the host timezone."""

    if value is None:
        return (9999, 12, 31, 23, 59, 59, 999999)
    return (
        value.year,
        value.month,
        value.day,
        value.hour,
        value.minute,
        value.second,
        value.microsecond,

    )

def build_calendar_events(
    groups: Iterable[GroupDefinition],
    metadata: Iterable[GameMetadata],
) -> tuple[CalendarEvent, ...]:
    """Build a deterministic view without fetching or persisting anything."""

    schedules = {
        item.identity: {round_.round_number: round_ for round_ in item.rounds}
        for item in metadata
    }
    events: list[CalendarEvent] = []
    for group in groups:
        schedule = schedules.get((group.game.locale.casefold(), group.game.slug), {})
        official_url = getattr(group, "official_url", None)
        team_names = {item.team_id: item.name for item in group.teams}
        if group.tournament is not None:
            if group.tournament.template == "double_elimination":
                bracket = build_double_elimination_bracket(
                    len(group.tournament.seed_order)
                )
                levels = _double_elimination_levels(bracket)
                possible_teams: dict[str, frozenset[int]] = {}

                def slot_details(
                    seed: int | None, source: str | None
                ) -> tuple[frozenset[int], str | None]:
                    if seed is not None:
                        team_id = group.tournament.seed_order[seed - 1]
                        return frozenset({team_id}), team_names.get(
                            team_id, f"Seed {seed}"
                        )
                    if source is None:
                        return frozenset(), None
                    kind, source_match = source.split(":", 1)
                    label = "Vinder" if kind == "winner" else "Taber"
                    return possible_teams[source_match], f"{label} af {source_match}"

                for match in bracket:
                    first_ids, first_label = slot_details(
                        match.team_a_seed, match.source_a
                    )
                    second_ids, second_label = slot_details(
                        match.team_b_seed, match.source_b
                    )
                    possible_teams[match.match_id] = first_ids | second_ids
                    labels = tuple(
                        label
                        for label in (first_label, second_label)
                        if label is not None
                    )
                    first_round = (
                        group.tournament.start_round
                        + (levels[match.match_id] - 1)
                        * group.tournament.rounds_per_tie
                    )
                    last_round = (
                        first_round + group.tournament.rounds_per_tie - 1
                    )
                    first_window = schedule.get(first_round)
                    last_window = schedule.get(last_round)
                    query = urlencode(
                        {
                            "view": "group",
                            "group": group.group_id,
                            "round": first_round,
                        }
                    )
                    title = (
                        " mod ".join(labels)
                        if len(labels) == 2
                        else f"{labels[0]} har fri"
                    )
                    if match.reset_final:
                        title = f"{title} (reset-finale hvis nødvendig)"
                    events.append(
                        CalendarEvent(
                            f"{group.group_id}:double:{match.match_id}",
                            "fixture",
                            title,
                            group.game.locale,
                            group.game.slug,
                            group.group_id,
                            first_round,
                            tuple(sorted(possible_teams[match.match_id])),
                            labels,
                            None if first_window is None else first_window.start,
                            None if first_window is None else first_window.close,
                            None if last_window is None else last_window.end,
                            f"?{query}",
                            official_url,
                        )
                    )
                continue
            for index, fixture in enumerate(group.tournament.group_fixtures, 1):
                participant_ids = tuple(
                    team_id
                    for team_id in (fixture.team_a_id, fixture.team_b_id)
                    if team_id is not None
                )
                participants = tuple(team_names.get(team_id, f"Seed {team_id}") for team_id in participant_ids)
                window = schedule.get(fixture.round_number)
                query = urlencode(
                    {
                        "view": "group",
                        "group": group.group_id,
                        "round": fixture.round_number,
                    }
                )
                events.append(
                    CalendarEvent(
                        f"{group.group_id}:fixture:{index}",
                        "fixture",
                        " mod ".join(participants) if len(participants) == 2 else f"{participants[0]} har fri",
                        group.game.locale,
                        group.game.slug,
                        group.group_id,
                        fixture.round_number,
                        participant_ids,
                        participants,
                        None if window is None else window.start,
                        None if window is None else window.close,
                        None if window is None else window.end,
                        f"?{query}",
                        official_url,
                    )
                )
        else:
            team_ids = tuple(item.team_id for item in group.teams)
            team_labels = tuple(item.name for item in group.teams)
            for round_number, window in schedule.items():
                query = urlencode(
                    {
                        "view": "group",
                        "group": group.group_id,
                        "round": round_number,
                    }
                )
                events.append(
                    CalendarEvent(
                        f"{group.group_id}:round:{round_number}",
                        "group_round",
                        f"{group.name} - runde {round_number}",
                        group.game.locale,
                        group.game.slug,
                        group.group_id,
                        round_number,
                        team_ids,
                        team_labels,
                        window.start,
                        window.close,
                        window.end,
                        f"?{query}",
                        official_url,
                    )
                )
    return tuple(
        sorted(
            events,
            key=lambda item: (
                item.start is None,
                _calendar_time_key(item.start),
                item.game_slug,
                item.group_id,
                item.round_number,
                item.event_id,
            ),
        )
    )
