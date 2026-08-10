"""Pure round-center projections built from local metadata and snapshots.

The builders in this module never fetch data or write files.  They deliberately
keep presentation concerns (Streamlit widgets, formatted durations and routes)
out of the domain layer while exposing enough typed state for those concerns.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal

from .analytics import SnapshotDiff, compare_snapshots
from .game_metadata import MetadataChange
from .groups import GroupDefinition
from .hub_settings import player_identity
from .models import GameUrl, PlayerEntry, RoundStatus, ScheduleRound
from .standings import build_standings
from .storage import PlayerStatisticsIndex, SnapshotIndex
from .tournament import GroupMatch, KnockoutMatch, build_tournament_state


TradingWindowStatus = Literal["unverified", "closed", "opens", "open"]
TradingTransitionKind = Literal["opens", "closes"]
ReadinessStatus = Literal[
    "ready",
    "preliminary",
    "missing",
    "failed",
    "unverified",
    "completed_needs_refresh",
]
NextBestActionKind = Literal[
    "fetch_metadata",
    "refresh_stale",
    "review_alerts",
    "review_team",
    "none",
]
DeviationCategory = Literal[
    "rank", "injury", "club", "missing_team", "rules_schedule"
]
DeviationSeverity = Literal["critical", "warning", "info"]
GroupMatrixMetric = Literal["overall_total", "tournament_points"]
OpponentStatus = Literal[
    "scheduled",
    "bye",
    "awaiting",
    "unpublished",
    "eliminated",
    "complete",
    "no_schedule",
]


def _local(value: datetime, *, timezone_source: datetime) -> datetime:
    """Make timestamps comparable without guessing a non-local timezone."""

    if value.tzinfo is None:
        return value.replace(tzinfo=timezone_source.tzinfo)
    return value.astimezone(timezone_source.tzinfo)


def _instant(value: datetime, *, timezone_source: datetime) -> datetime:
    """Normalize an aware instant to UTC so DST folds/gaps compare correctly."""

    return _local(value, timezone_source=timezone_source).astimezone(timezone.utc)


def _now(value: datetime | None) -> datetime:
    current = value or datetime.now().astimezone()
    return current.astimezone() if current.tzinfo is not None else current.astimezone()


@dataclass(frozen=True, slots=True)
class TradingWindowView:
    """Resolved window state.

    ``status == "opens"`` is the closed-before-start case; the UI maps it to
    its "Åbner om …" state. ``transition_kind`` distinguishes that countdown
    from the always-visible close countdown while the window is open.
    """

    status: TradingWindowStatus
    round_number: int | None = None
    start_at: datetime | None = None
    close_at: datetime | None = None
    end_at: datetime | None = None
    transition_at: datetime | None = None
    transition_kind: TradingTransitionKind | None = None

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    def seconds_until_transition(self, *, now: datetime | None = None) -> int | None:
        if self.transition_at is None:
            return None
        current = _now(now)
        transition = _instant(self.transition_at, timezone_source=current)
        current_instant = _instant(current, timezone_source=current)
        return max(0, int((transition - current_instant).total_seconds()))


def build_trading_window_view(
    rounds: Iterable[ScheduleRound], *, now: datetime | None = None
) -> TradingWindowView:
    """Resolve the current trading window from validated schedule boundaries.

    A window is open from ``start`` (inclusive) until ``close`` (exclusive).
    While open, the transition is always the close time, allowing the UI to
    render both an "open" badge and a closes-in countdown without a threshold.
    """

    current = _now(now)
    values = tuple(rounds)
    if not values:
        return TradingWindowView("unverified")

    normalized = tuple(
        (
            item,
            _local(item.start, timezone_source=current),
            _local(item.close, timezone_source=current),
            _local(item.end, timezone_source=current),
        )
        for item in values
    )
    current_instant = _instant(current, timezone_source=current)
    opened = tuple(
        value
        for value in normalized
        if _instant(value[1], timezone_source=current)
        <= current_instant
        < _instant(value[2], timezone_source=current)
    )
    if opened:
        item, start, close, end = min(
            opened,
            key=lambda value: (
                _instant(value[2], timezone_source=current),
                -value[0].round_number,
            ),
        )
        return TradingWindowView(
            "open", item.round_number, start, close, end, close, "closes"
        )

    future = tuple(
        value
        for value in normalized
        if _instant(value[1], timezone_source=current) > current_instant
    )
    if future:
        item, start, close, end = min(
            future,
            key=lambda value: (
                _instant(value[1], timezone_source=current),
                value[0].round_number,
            ),
        )
        return TradingWindowView(
            "opens", item.round_number, start, close, end, start, "opens"
        )

    past = tuple(
        value
        for value in normalized
        if _instant(value[2], timezone_source=current) <= current_instant
    )
    if past:
        item, start, close, end = max(
            past,
            key=lambda value: (
                _instant(value[2], timezone_source=current),
                value[0].round_number,
            ),
        )
        return TradingWindowView("closed", item.round_number, start, close, end)
    return TradingWindowView("closed")


@dataclass(frozen=True, slots=True)
class RoundCenterReadiness:
    status: ReadinessStatus
    round_number: int
    reasons: tuple[str, ...] = ()
    expected_team_ids: tuple[int, ...] = ()
    missing_team_ids: tuple[int, ...] = ()
    team_snapshot_count: int = 0
    player_snapshot: bool = False
    newest_data_at: datetime | None = None
    round_end_at: datetime | None = None
    stale_source_ids: tuple[str, ...] = ()

    @property
    def completed_needs_refresh(self) -> bool:
        return self.status == "completed_needs_refresh"

    @property
    def is_stale(self) -> bool:
        return bool(self.stale_source_ids)

    @property
    def needs_refresh(self) -> bool:
        return self.status in {
            "missing",
            "failed",
            "unverified",
            "completed_needs_refresh",
        } or self.is_stale


def build_round_center_readiness(
    game: GameUrl,
    round_number: int,
    expected_team_ids: Iterable[int],
    teams: SnapshotIndex,
    players: PlayerStatisticsIndex,
    *,
    round_end_at: datetime | None,
    now: datetime | None = None,
    player_required: bool = True,
    last_success_at: datetime | None = None,
    last_error_at: datetime | None = None,
    stale_source_ids: Iterable[str] = (),
) -> RoundCenterReadiness:
    """Combine exact-round coverage, completion and schedule freshness.

    ``completed_needs_refresh`` is deliberately derived rather than persisted
    as a new :class:`RoundStatus`: an ended round needs another fetch when any
    expected source is missing, predates the end, remains non-complete, or has a
    newer failed attempt than its latest success.
    """

    current = _now(now)
    stale_sources = tuple(dict.fromkeys(stale_source_ids))
    wanted = tuple(sorted(set(expected_team_ids)))
    located = {
        team_id: teams.summary_for(game, team_id, round_number)
        for team_id in wanted
    }
    missing_teams = tuple(
        team_id for team_id in wanted if located[team_id] is None
    )
    team_values = tuple(
        value for value in located.values() if value is not None
    )
    player = players.newest(game, round_number)
    missing_player = player_required and player is None
    generated = [value[0].generated_at for value in team_values]
    if player is not None:
        generated.append(player.generated_at)
    newest = max(generated, default=None)
    statuses: tuple[RoundStatus, ...] = tuple(
        value[1].round_status for value in team_values
    ) + (() if player is None else (player.statistics.round_status,))

    recent_failure = last_error_at is not None and (
        last_success_at is None
        or _instant(last_error_at, timezone_source=current)
        > _instant(last_success_at, timezone_source=current)
    )
    reasons: list[str] = []
    if missing_teams:
        reasons.append(f"{len(missing_teams)} hold mangler rundedata")
    if missing_player:
        reasons.append("Spillersnapshot mangler")
    if recent_failure:
        reasons.append("Seneste opdateringsforsøg mislykkedes")
    if stale_sources:
        reasons.append(
            f"{len(stale_sources)} datakilder er forældede"
        )

    ended = False
    stale_after_end = False
    normalized_end: datetime | None = None
    if round_end_at is not None:
        normalized_end = _local(round_end_at, timezone_source=current)
        end_instant = _instant(normalized_end, timezone_source=current)
        ended = _instant(current, timezone_source=current) >= end_instant
        if ended:
            stale_after_end = any(
                _instant(value, timezone_source=current) < end_instant
                for value in generated
            )
            if stale_after_end:
                reasons.append("Data er hentet før rundens sluttid")
            if any(status != "complete" for status in statuses):
                reasons.append("Rundestatus er ikke bekræftet som afsluttet")

    missing = bool(missing_teams) or missing_player
    non_complete = any(status != "complete" for status in statuses)
    if ended and (
        missing
        or stale_after_end
        or non_complete
        or recent_failure
        or stale_sources
    ):
        status: ReadinessStatus = "completed_needs_refresh"
    elif recent_failure:
        status = "failed"
    elif missing:
        status = "missing"
    elif any(value == "in_progress" for value in statuses):
        status = "preliminary"
    elif round_end_at is None or any(value == "unknown" for value in statuses):
        if round_end_at is None:
            reasons.append("Rundens sluttid er ikke verificeret")
        if any(value == "unknown" for value in statuses):
            reasons.append("Rundestatus er ukendt")
        status = "unverified"
    else:
        status = "ready"

    return RoundCenterReadiness(
        status,
        round_number,
        tuple(dict.fromkeys(reasons)),
        wanted,
        missing_teams,
        len(team_values),
        player is not None,
        newest,
        normalized_end,
        stale_sources,
    )


@dataclass(frozen=True, slots=True)
class NextBestAction:
    kind: NextBestActionKind
    title: str
    reason: str
    round_number: int | None = None
    unread_alerts: int | None = 0


def build_next_best_action(
    trading: TradingWindowView,
    readiness: RoundCenterReadiness,
    *,
    unread_alerts: int | None = 0,
) -> NextBestAction:
    """Apply the deadline-safe action priority used by Rundecenter."""

    if unread_alerts is not None and unread_alerts < 0:
        raise ValueError("Antallet af ulæste alarmer må ikke være negativt")
    target_round = (
        readiness.round_number
        if readiness.round_number > 0
        else trading.round_number
    )
    if trading.status == "unverified" or readiness.round_end_at is None:
        return NextBestAction(
            "fetch_metadata",
            "Hent spilinfo og data",
            (
                "Handelsvindue og deadline kan ikke verificeres uden spilmetadata."
                if trading.status == "unverified"
                else "Den valgte rundes sluttid mangler i spilmetadata."
            ),
            target_round,
            unread_alerts,
        )
    if readiness.needs_refresh:
        return NextBestAction(
            "refresh_stale",
            "Opdater forældede data",
            readiness.reasons[0]
            if readiness.reasons
            else "Datagrundlaget bør opdateres.",
            target_round,
            unread_alerts,
        )
    if unread_alerts is None:
        return NextBestAction(
            "review_alerts",
            "Kontrollér statusalarmer",
            "Alarmindbakken kunne ikke verificeres lokalt.",
            target_round,
            None,
        )
    if unread_alerts:
        return NextBestAction(
            "review_alerts",
            "Gennemgå ulæste alarmer",
            f"{unread_alerts} ulæste statusalarmer kræver opmærksomhed.",
            target_round,
            unread_alerts,
        )
    if trading.is_open:
        return NextBestAction(
            "review_team",
            "Gennemgå hold før deadline",
            "Data er klar, og handelsvinduet er åbent.",
            target_round,
        )
    return NextBestAction(
        "none",
        "Ingen akut handling",
        "Data er klar, og handelsvinduet er lukket.",
        target_round,
    )


@dataclass(frozen=True, slots=True)
class RoundDeviation:
    deviation_id: str
    category: DeviationCategory
    severity: DeviationSeverity
    title: str
    explanation: str
    round_number: int
    previous_round: int | None = None
    team_id: int | None = None
    team_name: str | None = None
    player_key: str | None = None
    player_name: str | None = None
    group_ids: tuple[str, ...] = ()
    previous_value: int | str | None = None
    current_value: int | str | None = None
    magnitude: int | None = None
    preliminary: bool = False
    previous_generated_at: datetime | None = None
    current_generated_at: datetime | None = None


_SEVERITY_ORDER = {"critical": 0, "warning": 1, "info": 2}
_CATEGORY_ORDER = {
    "missing_team": 0,
    "rules_schedule": 1,
    "injury": 2,
    "club": 3,
    "rank": 4,
}


def _deviation_sort_key(item: RoundDeviation) -> tuple[object, ...]:
    return (
        _SEVERITY_ORDER[item.severity],
        _CATEGORY_ORDER[item.category],
        -(item.magnitude or 0),
        item.deviation_id,
    )


def _previous_round(rounds: Iterable[int], target_round: int) -> int | None:
    return max((value for value in rounds if value < target_round), default=None)


def build_rank_deviations(
    snapshots: SnapshotIndex,
    game: GameUrl,
    team_ids: Iterable[int],
    target_round: int,
    *,
    previous_round: int | None = None,
) -> tuple[RoundDeviation, ...]:
    """Return exact-round rank movements with no inferred missing ranks."""

    wanted = tuple(sorted(set(team_ids)))
    baseline = previous_round
    if baseline is None:
        baseline = _previous_round(snapshots.rounds_for(game, wanted), target_round)
    if baseline is None:
        return ()
    result: list[RoundDeviation] = []
    for team_id in wanted:
        old = snapshots.summary_for(game, team_id, baseline)
        new = snapshots.summary_for(game, team_id, target_round)
        if old is None or new is None:
            continue
        old_rank = old[1].overall_rank
        new_rank = new[1].overall_rank
        if old_rank is None or new_rank is None or old_rank == new_rank:
            continue
        movement = old_rank - new_rank
        direction = "steg" if movement > 0 else "faldt"
        name = new[0].team.team_name
        result.append(
            RoundDeviation(
                f"rank:{game.locale.casefold()}:{game.slug}:{target_round}:{team_id}",
                "rank",
                "info",
                f"{name} {direction} {abs(movement)} placeringer",
                f"Samlet placering ændrede sig fra {old_rank} til {new_rank}.",
                target_round,
                baseline,
                team_id,
                name,
                previous_value=old_rank,
                current_value=new_rank,
                magnitude=abs(movement),
                preliminary=(
                    old[1].round_status != "complete"
                    or new[1].round_status != "complete"
                ),
                previous_generated_at=old[0].generated_at,
                current_generated_at=new[0].generated_at,
            )
        )
    return tuple(sorted(result, key=_deviation_sort_key))


def _player_snapshots(
    snapshots: PlayerStatisticsIndex, game: GameUrl, target_round: int
):
    current = snapshots.newest(game, target_round)
    baseline = _previous_round(snapshots.rounds_for(game), target_round)
    previous = None if baseline is None else snapshots.newest(game, baseline)
    return previous, current


def build_injury_deviations(
    snapshots: PlayerStatisticsIndex,
    game: GameUrl,
    target_round: int,
) -> tuple[RoundDeviation, ...]:
    """Return newly observed injuries and suspensions.

    Existing adverse statuses are not repeated. Stable IDs are matched first;
    the legacy name/team/position identity is allowed only when neither side
    has a stable ID, so ambiguous legacy rows cannot overwrite each other.
    """

    previous, current = _player_snapshots(snapshots, game, target_round)
    if previous is None or current is None:
        return ()
    result: list[RoundDeviation] = []
    matches = _matched_players(
        game,
        previous.statistics.entries,
        current.statistics.entries,
        allow_legacy=True,
    )
    for key, before, after in matches:
        adverse = (
            (
                "injury",
                before.is_injured,
                after.is_injured,
                f"{after.name} er markeret som skadet",
                "skadet",
            ),
            (
                "suspension",
                before.has_suspension,
                after.has_suspension,
                f"{after.name} er markeret som karantæneramt",
                "karantæne",
            ),
        )
        for status_key, was_adverse, is_adverse, title, status_label in adverse:
            if was_adverse or not is_adverse:
                continue
            result.append(
                RoundDeviation(
                    (
                        f"{status_key}:{game.locale.casefold()}:{game.slug}:"
                        f"{key}:{target_round}"
                    ),
                    "injury",
                    "warning",
                    title,
                    f"Status ændrede sig til {status_label} hos {after.team}.",
                    target_round,
                    previous.statistics.round_number,
                    team_name=after.team,
                    player_key=key,
                    player_name=after.name,
                    previous_value="aktiv",
                    current_value=status_label,
                    preliminary=(
                        previous.statistics.round_status != "complete"
                        or current.statistics.round_status != "complete"
                    ),
                    previous_generated_at=previous.generated_at,
                    current_generated_at=current.generated_at,
                )
            )
    return tuple(sorted(result, key=_deviation_sort_key))


def _unique_ids(
    entries: tuple[PlayerEntry, ...], attribute: Literal["person_id", "entry_id"]
) -> dict[int, tuple[int, PlayerEntry]]:
    values = tuple(
        (index, int(identifier), entry)
        for index, entry in enumerate(entries)
        if (identifier := getattr(entry, attribute)) is not None
    )
    counts = Counter(identifier for _, identifier, _ in values)
    return {
        identifier: (index, entry)
        for index, identifier, entry in values
        if counts[identifier] == 1
    }


def _unique_legacy_players(
    game: GameUrl, entries: tuple[PlayerEntry, ...]
) -> dict[str, tuple[int, PlayerEntry]]:
    values = tuple(
        (index, player_identity(game, entry), entry)
        for index, entry in enumerate(entries)
        if entry.person_id is None and entry.entry_id is None
    )
    counts = Counter(key for _, key, _ in values)
    return {
        key: (index, entry)
        for index, key, entry in values
        if counts[key] == 1
    }


def _matched_players(
    game: GameUrl,
    previous: tuple[PlayerEntry, ...],
    current: tuple[PlayerEntry, ...],
    *,
    allow_legacy: bool,
) -> tuple[tuple[str, PlayerEntry, PlayerEntry], ...]:
    """Match unique person IDs, then unique entry IDs, and optionally legacy IDs."""

    used_previous: set[int] = set()
    used_current: set[int] = set()
    matches: list[tuple[str, PlayerEntry, PlayerEntry]] = []

    old_people = _unique_ids(previous, "person_id")
    new_people = _unique_ids(current, "person_id")
    for identifier in sorted(old_people.keys() & new_people.keys()):
        old_index, before = old_people[identifier]
        new_index, after = new_people[identifier]
        used_previous.add(old_index)
        used_current.add(new_index)
        matches.append((f"person:{identifier}", before, after))

    old_entries = _unique_ids(previous, "entry_id")
    new_entries = _unique_ids(current, "entry_id")
    for identifier in sorted(old_entries.keys() & new_entries.keys()):
        old_index, before = old_entries[identifier]
        new_index, after = new_entries[identifier]
        if old_index in used_previous or new_index in used_current:
            continue
        # Conflicting person IDs are stronger evidence than a reused entry ID.
        if (
            before.person_id is not None
            and after.person_id is not None
            and before.person_id != after.person_id
        ):
            continue
        used_previous.add(old_index)
        used_current.add(new_index)
        matches.append((f"entry:{identifier}", before, after))

    if allow_legacy:
        old_legacy = _unique_legacy_players(game, previous)
        new_legacy = _unique_legacy_players(game, current)
        for key in sorted(old_legacy.keys() & new_legacy.keys()):
            old_index, before = old_legacy[key]
            new_index, after = new_legacy[key]
            if old_index in used_previous or new_index in used_current:
                continue
            matches.append((key, before, after))
    return tuple(matches)


def build_club_change_deviations(
    snapshots: PlayerStatisticsIndex,
    game: GameUrl,
    target_round: int,
) -> tuple[RoundDeviation, ...]:
    """Return club changes for unique stable IDs, never inferred from names."""

    previous, current = _player_snapshots(snapshots, game, target_round)
    if previous is None or current is None:
        return ()
    result: list[RoundDeviation] = []
    matches = _matched_players(
        game,
        previous.statistics.entries,
        current.statistics.entries,
        allow_legacy=False,
    )
    for key, before, after in matches:
        if before.team.strip().casefold() == after.team.strip().casefold():
            continue
        result.append(
            RoundDeviation(
                f"club:{game.locale.casefold()}:{game.slug}:{target_round}:{key}",
                "club",
                "info",
                f"{after.name} skiftede klub",
                f"Klub ændrede sig fra {before.team} til {after.team}.",
                target_round,
                previous.statistics.round_number,
                team_name=after.team,
                player_key=key,
                player_name=after.name,
                previous_value=before.team,
                current_value=after.team,
                preliminary=(
                    previous.statistics.round_status != "complete"
                    or current.statistics.round_status != "complete"
                ),
                previous_generated_at=previous.generated_at,
                current_generated_at=current.generated_at,
            )
        )
    return tuple(sorted(result, key=_deviation_sort_key))


def build_missing_team_deviations(
    groups: Iterable[GroupDefinition],
    snapshots: SnapshotIndex,
    game: GameUrl,
    target_round: int,
) -> tuple[RoundDeviation, ...]:
    """Return one missing-data deviation per team across all affected groups."""

    names: dict[int, str] = {}
    memberships: dict[int, set[str]] = defaultdict(set)
    identity = (game.locale.casefold(), game.slug)
    for group in groups:
        if (group.game.locale.casefold(), group.game.slug) != identity:
            continue
        for member in group.teams:
            names.setdefault(member.team_id, member.name)
            memberships[member.team_id].add(group.group_id)
    result = [
        RoundDeviation(
            f"missing-team:{game.locale.casefold()}:{game.slug}:{target_round}:{team_id}",
            "missing_team",
            "critical",
            f"{names[team_id]} mangler rundedata",
            f"Der findes intet holdsammendrag for runde {target_round}.",
            target_round,
            team_id=team_id,
            team_name=names[team_id],
            group_ids=tuple(sorted(memberships[team_id])),
        )
        for team_id in sorted(memberships)
        if snapshots.summary_for(game, team_id, target_round) is None
    ]
    return tuple(sorted(result, key=_deviation_sort_key))


def _metadata_value(value: object | None) -> int | str | None:
    if value is None or isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def build_rules_schedule_deviations(
    changes: Iterable[MetadataChange],
    target_round: int,
    *,
    game: GameUrl | None = None,
) -> tuple[RoundDeviation, ...]:
    """Turn explainable rule and schedule metadata changes into deviations."""

    result: list[RoundDeviation] = []
    labels = {"rules": "Spilregler", "schedule": "Kampplan"}
    scope = (
        "local"
        if game is None
        else f"{game.locale.casefold()}:{game.slug}"
    )
    for change in changes:
        if change.kind not in labels:
            continue
        affected_round = change.round_number
        scope = (
            f" for runde {affected_round}"
            if affected_round is not None
            else ""
        )
        old_value = _metadata_value(change.old_value)
        new_value = _metadata_value(change.new_value)
        result.append(
            RoundDeviation(
                (
                    f"metadata:{scope}:{target_round}:{change.kind}:"
                    f"{affected_round or 'all'}:{change.field}"
                ),
                "rules_schedule",
                "warning",
                f"{labels[change.kind]} er ændret{scope}",
                (
                    f"Metadatafeltet {change.field} ændrede sig fra "
                    f"{old_value!s} til {new_value!s}."
                ),
                target_round,
                previous_value=old_value,
                current_value=new_value,
            )
        )
    return tuple(sorted(result, key=_deviation_sort_key))


def build_round_deviations(
    groups: Iterable[GroupDefinition],
    teams: SnapshotIndex,
    players: PlayerStatisticsIndex,
    game: GameUrl,
    target_round: int,
    *,
    metadata_changes: Iterable[MetadataChange] = (),
) -> tuple[RoundDeviation, ...]:
    selected_groups = tuple(groups)
    team_ids = tuple(
        sorted(
            {
                member.team_id
                for group in selected_groups
                if (group.game.locale.casefold(), group.game.slug)
                == (game.locale.casefold(), game.slug)
                for member in group.teams
            }
        )
    )
    values = (
        *build_missing_team_deviations(selected_groups, teams, game, target_round),
        *build_injury_deviations(players, game, target_round),
        *build_club_change_deviations(players, game, target_round),
        *build_rules_schedule_deviations(
            metadata_changes,
            target_round,
            game=game,
        ),
        *build_rank_deviations(teams, game, team_ids, target_round),
    )
    return tuple(sorted(values, key=_deviation_sort_key))


def select_round_deviations(
    deviations: Iterable[RoundDeviation],
    *,
    categories: Iterable[DeviationCategory] | None = None,
    limit: int = 5,
) -> tuple[RoundDeviation, ...]:
    if limit < 1:
        raise ValueError("Top N skal være mindst 1")
    selected = None if categories is None else frozenset(categories)
    filtered = tuple(
        item for item in deviations if selected is None or item.category in selected
    )
    selected_rank = sorted(
        (item for item in filtered if item.category == "rank"),
        key=_deviation_sort_key,
    )[:limit]
    selected_other = tuple(
        item for item in filtered if item.category != "rank"
    )
    return tuple(
        sorted((*selected_other, *selected_rank), key=_deviation_sort_key)
    )


@dataclass(frozen=True, slots=True)
class TeamRoundComparison:
    team_id: int
    team_name: str
    previous_total: int
    current_total: int
    previous_change: int
    current_change: int
    previous_rank: int | None
    current_rank: int | None
    rank_movement: int | None
    previous_status: RoundStatus
    current_status: RoundStatus


@dataclass(frozen=True, slots=True)
class RoundComparison:
    game: GameUrl
    current_round: int
    previous_round: int | None
    teams: tuple[TeamRoundComparison, ...] = ()
    player_diff: SnapshotDiff | None = None
    missing_current_team_ids: tuple[int, ...] = ()
    missing_previous_team_ids: tuple[int, ...] = ()
    missing_current_players: bool = True
    missing_previous_players: bool = True
    is_final: bool = False

    @property
    def preliminary(self) -> bool:
        return not self.is_final


def build_round_comparison(
    team_snapshots: SnapshotIndex,
    player_snapshots: PlayerStatisticsIndex,
    game: GameUrl,
    team_ids: Iterable[int],
    target_round: int,
    *,
    player_required: bool = True,
) -> RoundComparison:
    """Compare one target round with one shared previous available round."""

    wanted = tuple(sorted(set(team_ids)))
    known = set(team_snapshots.rounds_for(game, wanted))
    known.update(player_snapshots.rounds_for(game))
    previous_round = _previous_round(known, target_round)
    if previous_round is None:
        return RoundComparison(game, target_round, None)

    current_player = player_snapshots.newest(game, target_round)
    previous_player = player_snapshots.newest(game, previous_round)
    player_diff = (
        compare_snapshots(current_player, previous_player)
        if current_player is not None and previous_player is not None
        else None
    )
    rows: list[TeamRoundComparison] = []
    missing_current: list[int] = []
    missing_previous: list[int] = []
    for team_id in wanted:
        current = team_snapshots.summary_for(game, team_id, target_round)
        previous = team_snapshots.summary_for(game, team_id, previous_round)
        if current is None:
            missing_current.append(team_id)
        if previous is None:
            missing_previous.append(team_id)
        if current is None or previous is None:
            continue
        old = previous[1]
        new = current[1]
        movement = (
            old.overall_rank - new.overall_rank
            if old.overall_rank is not None and new.overall_rank is not None
            else None
        )
        rows.append(
            TeamRoundComparison(
                team_id,
                current[0].team.team_name,
                old.total,
                new.total,
                old.change,
                new.change,
                old.overall_rank,
                new.overall_rank,
                movement,
                old.round_status,
                new.round_status,
            )
        )
    rows.sort(key=lambda item: (item.team_name.casefold(), item.team_id))
    team_statuses = tuple(
        status
        for item in rows
        for status in (item.previous_status, item.current_status)
    )
    final = (
        not missing_current
        and not missing_previous
        and (not player_required or player_diff is not None)
        and all(status == "complete" for status in team_statuses)
        and (
            not player_required
            or (
                current_player is not None
                and previous_player is not None
                and current_player.statistics.round_status == "complete"
                and previous_player.statistics.round_status == "complete"
            )
        )
    )
    return RoundComparison(
        game,
        target_round,
        previous_round,
        tuple(rows),
        player_diff,
        tuple(missing_current),
        tuple(missing_previous),
        current_player is None,
        previous_player is None,
        final,
    )


@dataclass(frozen=True, slots=True)
class GroupMatrixRow:
    rank: int | None
    team_id: int
    team_name: str
    owner_name: str
    value: int | None
    distance: int | None
    next_opponent_id: int | None = None
    next_opponent_name: str | None = None
    next_round: int | None = None
    opponent_status: OpponentStatus = "no_schedule"
    warning: str | None = None


@dataclass(frozen=True, slots=True)
class GroupMatrix:
    group_id: str
    group_name: str
    round_number: int
    metric: GroupMatrixMetric
    rows: tuple[GroupMatrixRow, ...]
    warnings: tuple[str, ...] = ()


def _match_round(
    match: GroupMatch | KnockoutMatch,
    *,
    as_of_round: int,
) -> int:
    return (
        match.fixture.round_number
        if isinstance(match, GroupMatch)
        else next(
            (
                round_number
                for round_number in match.round_numbers
                if round_number > as_of_round
            ),
            match.round_numbers[-1],
        )
    )


def _next_opponent(
    team_id: int,
    state,
    *,
    template: str,
) -> tuple[int | None, str | None, int | None, OpponentStatus]:
    candidates: list[GroupMatch | KnockoutMatch] = []
    for match in (*state.group_matches, *state.knockout_matches):
        if match.complete:
            continue
        participants = (
            (match.fixture.team_a_id, match.fixture.team_b_id)
            if isinstance(match, GroupMatch)
            else (match.team_a_id, match.team_b_id)
        )
        if team_id in participants:
            candidates.append(match)
    if candidates:
        match = min(
            candidates,
            key=lambda item: (
                _match_round(item, as_of_round=state.as_of_round),
                repr(item),
            ),
        )
        round_number = _match_round(
            match,
            as_of_round=state.as_of_round,
        )
        if isinstance(match, GroupMatch):
            if match.fixture.is_bye:
                return None, "Fri", round_number, "bye"
            if match.fixture.team_a_id == team_id:
                return (
                    match.fixture.team_b_id,
                    match.team_b_name,
                    round_number,
                    "scheduled",
                )
            return (
                match.fixture.team_a_id,
                match.team_a_name,
                round_number,
                "scheduled",
            )
        if match.team_a_id == team_id:
            opponent_id, opponent_name = match.team_b_id, match.team_b_name
        else:
            opponent_id, opponent_name = match.team_a_id, match.team_a_name
        return (
            opponent_id,
            opponent_name,
            round_number,
            "scheduled" if opponent_id is not None else "awaiting",
        )
    if team_id in state.eliminated_team_ids:
        return None, None, None, "eliminated"
    if state.champion_id == team_id:
        return None, None, None, "complete"
    if template == "swiss" and state.champion_id is None:
        return None, None, None, "unpublished"
    if team_id in state.active_team_ids:
        return None, None, None, "awaiting"
    return None, None, None, "complete"


def build_group_matrix(
    group: GroupDefinition,
    snapshots: SnapshotIndex,
    round_number: int,
) -> GroupMatrix:
    """Build an all-member group matrix using each group's canonical scoring."""

    if group.tournament is None:
        standings = build_standings(group, snapshots, round_number, "overall")
        return GroupMatrix(
            group.group_id,
            group.name,
            round_number,
            "overall_total",
            tuple(
                GroupMatrixRow(
                    item.rank,
                    item.team_id,
                    item.team_name,
                    item.owner_name,
                    item.value,
                    item.distance,
                    next_opponent_name="Ingen kampplan",
                    warning=item.warning,
                )
                for item in standings
            ),
        )

    state = build_tournament_state(group, snapshots, round_number)
    standings_by_team = {item.team_id: item for item in state.standings}
    leader_points = max(
        (item.points for item in state.standings), default=None
    )
    rows: list[GroupMatrixRow] = []
    for member in group.teams:
        standing = standings_by_team.get(member.team_id)
        opponent_id, opponent_name, next_round, opponent_status = _next_opponent(
            member.team_id,
            state,
            template=group.tournament.template,
        )
        if standing is None:
            newest = snapshots.newest(group.game, member.team_id)
            rows.append(
                GroupMatrixRow(
                    None,
                    member.team_id,
                    member.name,
                    newest.team.owner_name if newest is not None else member.account_label,
                    None,
                    None,
                    opponent_id,
                    opponent_name,
                    next_round,
                    opponent_status,
                    "Turneringsstillingen mangler holdet",
                )
            )
            continue
        rows.append(
            GroupMatrixRow(
                standing.rank,
                standing.team_id,
                standing.team_name,
                standing.owner_name,
                standing.points,
                (
                    standing.points - leader_points
                    if leader_points is not None
                    else None
                ),
                opponent_id,
                opponent_name,
                next_round,
                opponent_status,
            )
        )
    rows.sort(
        key=lambda item: (
            item.rank is None,
            item.rank if item.rank is not None else 0,
            item.team_name.casefold(),
            item.team_id,
        )
    )
    return GroupMatrix(
        group.group_id,
        group.name,
        round_number,
        "tournament_points",
        tuple(rows),
        state.warnings,
    )
