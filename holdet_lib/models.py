"""Domain models shared by the player and fantasy-team workflows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Literal
from urllib.parse import quote


NEXUS_HOST = "nexus-app-fantasy.holdet.dk"
RoundStatus = Literal["complete", "in_progress", "unknown"]


@dataclass(frozen=True, slots=True)
class ScheduleRound:
    round_number: int
    start: datetime
    close: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class GameUrl:
    original: str
    locale: str
    slug: str

    @property
    def nexus_root_url(self) -> str:
        return (
            f"https://{NEXUS_HOST}/"
            f"{quote(self.locale, safe='-')}/{quote(self.slug, safe='-')}"
        )

    def statistics_url(self, variant: str, round_number: int | None = None) -> str:
        url = f"{self.nexus_root_url}/{quote(variant, safe='_')}/statistics"
        if round_number is None:
            return url
        if isinstance(round_number, bool) or round_number < 0:
            raise ValueError("Rundenummeret skal være et ikke-negativt heltal")
        return f"{url}?round={round_number}"

    def team_url(self, variant: str, team_id: int) -> str:
        return (
            f"{self.nexus_root_url}/{quote(variant, safe='_')}"
            f"/fantasyteams/{team_id}"
        )


@dataclass(frozen=True, slots=True)
class PlayerPerformanceStat:
    """One immutable, numeric performance field from the public player payload."""

    name: str
    value: float

    def __post_init__(self) -> None:
        name = self.name.strip()
        if (
            not name
            or len(name) > 80
            or any(ord(character) < 32 for character in name)
        ):
            raise ValueError("Et præstationsfeltnavn skal være 1-80 synlige tegn")
        if not isinstance(self.value, (int, float)) or isinstance(self.value, bool):
            raise ValueError("Et præstationsfelt skal have en numerisk værdi")
        value = float(self.value)
        if not isfinite(value):
            raise ValueError("Et præstationsfelt skal have en endelig værdi")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "value", value)


@dataclass(frozen=True, slots=True)
class PlayerEntry:
    source_index: int
    name: str
    team: str
    position: str
    value: int
    is_active: bool = True
    is_disabled: bool = False
    is_injured: bool = False
    has_suspension: bool = False
    entry_id: int | None = None
    person_id: int | None = None
    total_growth: int | None = None
    round_growth: int | None = None
    popularity: float | None = None
    popularity_change: float | None = None
    trend: float | None = None
    index: float | None = None
    stats: tuple[PlayerPerformanceStat, ...] = ()
    total_stats: tuple[PlayerPerformanceStat, ...] = ()

    def __post_init__(self) -> None:
        for label, value in (
            ("popularity", self.popularity),
            ("popularity_change", self.popularity_change),
            ("trend", self.trend),
            ("index", self.index),
        ):
            if value is None:
                continue
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not isfinite(float(value))
            ):
                raise ValueError(f"Spillerfeltet {label} skal være et endeligt tal")
            object.__setattr__(self, label, float(value))
        for label, values in (("stats", self.stats), ("total_stats", self.total_stats)):
            names = [item.name.casefold() for item in values]
            if len(names) != len(set(names)):
                raise ValueError(f"Spillerfeltet {label} må ikke have dublerede navne")

    def stat_values(self, *, total: bool = False) -> dict[str, float]:
        """Return a detached name/value map for formula and diff consumers."""

        values = self.total_stats if total else self.stats
        return {item.name: item.value for item in values}


@dataclass(frozen=True, slots=True)
class ScrapedGame:
    game: GameUrl
    variant: str
    round_number: int
    entries: tuple[PlayerEntry, ...]
    format: str | None = None
    unit: str | None = None
    round_status: RoundStatus = "unknown"
    round_end_at: datetime | None = None

    def __post_init__(self) -> None:
        from .policies import GamePolicy, legacy_policy

        if self.format is None and self.unit is None:
            policy = legacy_policy(self.variant)
        elif self.format is None or self.unit is None:
            raise ValueError("Spillerstatistikkens format og enhed skal angives sammen")
        else:
            policy = GamePolicy(self.variant, self.format, self.unit)
        object.__setattr__(self, "format", policy.format)
        object.__setattr__(self, "unit", policy.unit)


@dataclass(frozen=True, slots=True)
class AccountConfig:
    key: str
    label: str
    profile_url: str
    user_id: int


@dataclass(frozen=True, slots=True)
class TeamReference:
    game: GameUrl
    team_id: int
    team_name: str
    source_url: str
    account_key: str = "direct"
    account_label: str = "Direkte URL"
    account_user_id: int | None = None
    profile_url: str | None = None


@dataclass(frozen=True, slots=True)
class RosterEntry:
    source_index: int
    player_id: int
    name: str
    team: str
    position: str
    value: int
    round_change: int
    since_purchase_change: int
    purchase_round: int | None
    role: str
    is_active: bool = True
    is_disabled: bool = False
    is_injured: bool = False
    has_suspension: bool = False

    @property
    def statuses(self) -> tuple[str, ...]:
        result: list[str] = []
        if not self.is_active:
            result.append("inactive")
        if self.is_disabled:
            result.append("disabled")
        if self.is_injured:
            result.append("injured")
        if self.has_suspension:
            result.append("suspended")
        return tuple(result)


@dataclass(frozen=True, slots=True)
class RoundSummary:
    round_number: int
    total: int
    change: int
    bank: int | None
    player_value: int | None
    bank_change: int | None
    interest: int | None
    player_change: int
    transfer: int | None
    captain_bonus: int
    special_bonus: int
    substitutions_used: int | None
    round_rank: int | None = None
    overall_rank: int | None = None
    round_rank_change: int | None = None
    overall_rank_change: int | None = None
    round_status: RoundStatus = "unknown"
    round_end_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class TeamOverview:
    current_round: int
    unit: str
    player_value: int | None
    bank: int | None
    total: int | None
    current_change: int | None
    rank: int | None
    rank_change: int | None
    top_percent: int | None
    substitutions_remaining: int | None
    substitutions_limit: int | None
    substitutions_used: int | None


@dataclass(frozen=True, slots=True)
class ScrapedTeam:
    reference: TeamReference
    variant: str
    game_id: int
    team_name: str
    owner_name: str
    owner_user_id: int | None
    overview: TeamOverview
    roster: tuple[RosterEntry, ...]
    history: tuple[RoundSummary, ...]

