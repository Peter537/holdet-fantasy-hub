"""Cache-only fantasy calendar builders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, Literal
from urllib.parse import urlencode

from .game_metadata import GameMetadata
from .groups import GroupDefinition
from .models import ScheduleRound
from .storage import SnapshotIndex
from .tournament import TournamentConfig, _knockout_layout, build_tournament_state
from .tournament_pairings import TournamentPairingRevision, validate_tournament_pairing_revision


CalendarStatus = Literal["scheduled", "published", "provisional", "conditional"]


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
    round_numbers: tuple[int, ...] = ()
    status: CalendarStatus = "scheduled"

    @property
    def missing_time(self) -> bool:
        return self.start is None or self.deadline is None or self.end is None


def _calendar_time_key(value: datetime | None) -> tuple[int, int, int, int, int, int, int]:
    """Sort even sentinel dates that cannot be converted by the host timezone."""

    if value is None:
        return (9999, 12, 31, 23, 59, 59, 999999)
    return (
        value.year, value.month, value.day, value.hour, value.minute,
        value.second, value.microsecond,
    )


def _event(
    group: GroupDefinition,
    schedule: dict[int, ScheduleRound],
    event_key: str,
    title: str,
    rounds: tuple[int, ...],
    participant_ids: tuple[int, ...],
    labels: tuple[str, ...],
    *,
    kind: Literal["fixture", "group_round"] = "fixture",
    status: CalendarStatus = "scheduled",
) -> CalendarEvent:
    first_round, final_round = rounds[0], rounds[-1]
    first = schedule.get(first_round)
    last = schedule.get(final_round)
    query = urlencode({"view": "group", "group": group.group_id, "round": first_round})
    return CalendarEvent(
        event_id=f"{group.group_id}:{event_key}",
        kind=kind,
        title=title,
        game_locale=group.game.locale,
        game_slug=group.game.slug,
        group_id=group.group_id,
        round_number=first_round,
        participant_ids=participant_ids,
        participant_names=labels,
        start=first.start if first else None,
        deadline=first.close if first else None,
        end=last.end if last else None,
        internal_url=f"?{query}",
        official_url=group.official_url,
        round_numbers=rounds,
        status=status,
    )


def _slot_participant(
    source: str, config: TournamentConfig, team_names: dict[int, str]
) -> tuple[int | None, str | None]:
    if source == "bye":
        return None, None
    kind, reference = source.split(":", 1)
    if kind == "seed":
        if config.template == "double_elimination":
            team_id = config.seed_order[int(reference) - 1]
            return team_id, team_names[team_id]
        return None, f"Seed {reference}"
    label = "Vinder" if kind == "winner" else "Taber"
    return None, f"{label} af {reference}"


def _reset_unnecessary(
    group: GroupDefinition, config: TournamentConfig, snapshots: SnapshotIndex | None
) -> bool:
    if snapshots is None or config.pairing_error is not None:
        return False
    state = build_tournament_state(group, snapshots, config.final_round)
    first_final = next((match for match in state.knockout_matches if match.stage == "Finale"), None)
    return bool(
        first_final is not None and first_final.complete
        and first_final.winner_id is not None and first_final.winner_id == first_final.team_a_id
    )


def _knockout_events(
    group: GroupDefinition,
    config: TournamentConfig,
    schedule: dict[int, ScheduleRound],
    team_names: dict[int, str],
    snapshots: SnapshotIndex | None,
) -> Iterable[CalendarEvent]:
    if config.pairing_error is None and config.published_matches is not None:
        revision = validate_tournament_pairing_revision(
            TournamentPairingRevision(
                group.group_id, group.active_revision, knockout_matches=config.published_matches,
            ),
            config,
            tuple(team_names),
        )
        published = {match.match_id: match for match in revision.knockout_matches}
    else:
        published = {}
    event_kind = "double" if config.template == "double_elimination" else "knockout"

    for slot in _knockout_layout(config):
        match = published.get(slot.match_id)
        is_reset = slot.match_id == "GF2"
        if match is not None:
            ids = tuple(team_id for team_id in (match.team_a_id, match.team_b_id) if team_id is not None)
            labels = tuple(team_names[team_id] for team_id in ids)
            rounds = match.round_numbers
            status: CalendarStatus = "published"
        else:
            if is_reset and _reset_unnecessary(group, config, snapshots):
                continue
            participants = tuple(
                _slot_participant(source, config, team_names) for source in (slot.source_a, slot.source_b)
            )
            ids = tuple(team_id for team_id, _ in participants if team_id is not None)
            labels = tuple(label for _, label in participants if label is not None)
            rounds = slot.round_numbers
            status = "conditional" if is_reset else "provisional"

        if not labels:
            continue
        title = " mod ".join(labels) if len(labels) == 2 else f"{labels[0]} har fri"
        if is_reset:
            title = f"{title} (reset-finale{' hvis nødvendig' if match is None else ''})"
        yield _event(
            group, schedule, f"{event_kind}:{slot.match_id}", title, rounds, ids, labels,
            status=status,
        )


def build_calendar_events(
    groups: Iterable[GroupDefinition],
    metadata: Iterable[GameMetadata],
    *,
    snapshots: SnapshotIndex | None = None,
) -> tuple[CalendarEvent, ...]:
    """Project cached dates and validated fixtures without fetching or persisting anything."""

    schedules = {
        item.identity: {round_.round_number: round_ for round_ in item.rounds}
        for item in metadata
    }
    events: list[CalendarEvent] = []
    for group in groups:
        schedule = schedules.get((group.game.locale.casefold(), group.game.slug), {})
        team_names = {item.team_id: item.name for item in group.teams}
        config = group.tournament
        if config is None:
            events.extend(
                _event(
                    group, schedule, f"round:{round_number}", f"{group.name} - runde {round_number}",
                    (round_number,), tuple(team_names), tuple(team_names.values()), kind="group_round",
                )
                for round_number in schedule
            )
            continue
        fixtures = () if config.template == "double_elimination" else config.group_fixtures
        for index, fixture in enumerate(fixtures, 1):
            ids = tuple(
                team_id for team_id in (fixture.team_a_id, fixture.team_b_id) if team_id is not None
            )
            labels = tuple(team_names.get(team_id, f"Seed {team_id}") for team_id in ids)
            title = " mod ".join(labels) if len(labels) == 2 else f"{labels[0]} har fri"
            events.append(_event(
                group, schedule, f"fixture:{index}", title, (fixture.round_number,), ids, labels,
            ))
        if config.template in {"group_knockout", "double_elimination"}:
            events.extend(_knockout_events(group, config, schedule, team_names, snapshots))

    return tuple(sorted(
        events,
        key=lambda item: (
            item.start is None, _calendar_time_key(item.start), item.game_slug,
            item.group_id, item.round_number, item.event_id,
        ),
    ))
