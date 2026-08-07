"""Pure, cache-only transfer scenario simulation for supported game formats."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import ceil
from typing import Literal

from .models import PlayerEntry, RosterEntry, RoundStatus


@dataclass(frozen=True, slots=True)
class TransferRuleProfile:
    key: str
    label: str
    roster_size: int
    fee_percent: int = 0
    max_from_team: int | None = None
    position_limits: tuple[tuple[str, int, int], ...] = ()
    category_count: int | None = None
    category_size: int | None = None
    budget_enabled: bool = True
    base_contracts: int | None = None
    gold_contracts: int | None = None
    free_through_round: int = 1
    captain_count: int = 0
    known: bool = True
    fee_basis_points: int | None = None


@dataclass(frozen=True, slots=True)
class ScenarioPlayer:
    player_id: int
    name: str
    team: str
    position: str
    value: int
    is_active: bool
    is_disabled: bool
    is_injured: bool
    has_suspension: bool
    source: Literal["current", "purchase"]


@dataclass(frozen=True, slots=True)
class TransferScenario:
    initial_roster: tuple[RosterEntry, ...]
    available_players: tuple[PlayerEntry, ...]
    sold_player_ids: tuple[int, ...] = ()
    bought_player_ids: tuple[int, ...] = ()
    starting_bank: int | None = None
    contracts_remaining: int | None = None
    target_round: int = 1
    team_round: int | None = None
    player_round: int | None = None
    captain_player_ids: tuple[int, ...] = ()
    team_round_status: RoundStatus = "unknown"
    player_round_status: RoundStatus = "unknown"


@dataclass(frozen=True, slots=True)
class TransferValidation:
    profile: TransferRuleProfile
    status: Literal["valid", "invalid", "unverified"]
    ending_roster: tuple[ScenarioPlayer, ...]
    ending_bank: int | None
    purchase_total: int
    sale_total: int
    transfer_fee: int
    contracts_used: int
    contracts_remaining: int | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    certainty: Literal["final", "preliminary", "unverified"] = "preliminary"

    @property
    def is_valid(self) -> bool | None:
        if self.status == "unverified":
            return None
        return self.status == "valid"


FOOTBALL_RULES = TransferRuleProfile(
    "football",
    "Fodbold",
    11,
    fee_percent=1,
    max_from_team=4,
    position_limits=(
        ("goalkeeper", 1, 1),
        ("defender", 3, 5),
        ("midfielder", 3, 5),
        ("forward", 1, 3),
    ),
    base_contracts=3,
    gold_contracts=None,
    free_through_round=1,
)

CYCLING_RULES = TransferRuleProfile(
    "cycling",
    "Tourspillet",
    8,
    fee_percent=1,
    max_from_team=2,
    base_contracts=8,
    gold_contracts=None,
    free_through_round=1,
)

MOTOR_RULES = TransferRuleProfile(
    "motor",
    "Motor Manager",
    7,
    position_limits=(
        ("driver", 4, 4),
        ("constructor", 2, 2),
        ("pitcrew", 1, 1),
    ),
    base_contracts=0,
    gold_contracts=25,
    free_through_round=2,
)

GOLF_RULES = TransferRuleProfile(
    "golf",
    "Golf Manager",
    15,
    category_count=5,
    category_size=3,
    budget_enabled=False,
    base_contracts=0,
    gold_contracts=50,
    free_through_round=1,
)

UNKNOWN_RULES = TransferRuleProfile(
    "unknown",
    "Ukendt format",
    0,
    budget_enabled=False,
    known=False,
)


def transfer_rule_profile(
    *, variant: str = "", game_format: str = "", game_slug: str = ""
) -> TransferRuleProfile:
    value = " ".join((variant, game_format, game_slug)).casefold()
    if any(marker in value for marker in ("soccer", "football", "super-manager")):
        return FOOTBALL_RULES
    if any(marker in value for marker in ("cycling", "tour")):
        return CYCLING_RULES
    if any(marker in value for marker in ("motor", "formula", "f1")):
        return MOTOR_RULES
    if "golf" in value:
        return GOLF_RULES
    return UNKNOWN_RULES


_POSITION_ALIASES = {
    "målmand": "goalkeeper",
    "maalmand": "goalkeeper",
    "goalkeeper": "goalkeeper",
    "keeper": "goalkeeper",
    "forsvar": "defender",
    "forsvarer": "defender",
    "defender": "defender",
    "back": "defender",
    "midt": "midfielder",
    "midtbane": "midfielder",
    "midfielder": "midfielder",
    "angreb": "forward",
    "angriber": "forward",
    "forward": "forward",
    "attacker": "forward",
    "kører": "driver",
    "koerer": "driver",
    "driver": "driver",
    "konstruktør": "constructor",
    "konstruktoer": "constructor",
    "constructor": "constructor",
    "pit crew": "pitcrew",
    "pitcrew": "pitcrew",
    "pit-crew": "pitcrew",
}


def _position(value: str) -> str:
    normalized = " ".join(value.strip().casefold().split())
    return _POSITION_ALIASES.get(normalized, normalized)


def _roster_player(entry: RosterEntry) -> ScenarioPlayer:
    return ScenarioPlayer(
        entry.player_id,
        entry.name,
        entry.team,
        entry.position,
        entry.value,
        entry.is_active,
        entry.is_disabled,
        entry.is_injured,
        entry.has_suspension,
        "current",
    )


def _available_id(entry: PlayerEntry) -> int:
    return entry.entry_id if entry.entry_id is not None else entry.source_index


def _purchase_player(entry: PlayerEntry) -> ScenarioPlayer:
    return ScenarioPlayer(
        _available_id(entry),
        entry.name,
        entry.team,
        entry.position,
        entry.value,
        entry.is_active,
        entry.is_disabled,
        entry.is_injured,
        entry.has_suspension,
        "purchase",
    )


def simulate_transfers(
    profile: TransferRuleProfile, scenario: TransferScenario
) -> TransferValidation:
    """Simulate transfers without writing snapshots, config or remote data."""

    errors: list[str] = []
    warnings: list[str] = []
    sold_ids = set(scenario.sold_player_ids)
    bought_ids = tuple(scenario.bought_player_ids)

    if len(sold_ids) != len(scenario.sold_player_ids):
        errors.append("Den samme spiller er solgt flere gange")
    if len(set(bought_ids)) != len(bought_ids):
        errors.append("Den samme spiller er købt flere gange")

    current = {entry.player_id: entry for entry in scenario.initial_roster}
    unknown_sales = sold_ids - current.keys()
    if unknown_sales:
        errors.append("Et eller flere salg findes ikke i holdsnapshotet")

    available = {_available_id(entry): entry for entry in scenario.available_players}
    unknown_buys = set(bought_ids) - available.keys()
    if unknown_buys:
        errors.append("Et eller flere køb findes ikke i spillersnapshotet")

    if (
        scenario.team_round is not None
        and scenario.player_round is not None
        and scenario.team_round != scenario.player_round
    ):
        errors.append(
            f"Datarunder matcher ikke: hold runde {scenario.team_round}, "
            f"spillere runde {scenario.player_round}"
        )

    kept = [
        _roster_player(entry)
        for entry in scenario.initial_roster
        if entry.player_id not in sold_ids
    ]
    purchases = [
        _purchase_player(available[player_id])
        for player_id in bought_ids
        if player_id in available
    ]
    for player in purchases:
        if not player.is_active or player.is_disabled:
            errors.append(f"{player.name} kan ikke købes, fordi spilleren er inaktiv")
        if player.is_injured:
            warnings.append(f"{player.name} er markeret som skadet")
        if player.has_suspension:
            warnings.append(f"{player.name} er markeret med karantæne")

    ending = tuple((*kept, *purchases))
    ending_ids = [entry.player_id for entry in ending]
    if len(ending_ids) != len(set(ending_ids)):
        errors.append("Slutholdet indeholder den samme spiller flere gange")

    sale_total = sum(
        entry.value for player_id, entry in current.items() if player_id in sold_ids
    )
    purchase_total = sum(entry.value for entry in purchases)
    fee_basis_points = (
        profile.fee_basis_points
        if profile.fee_basis_points is not None
        else profile.fee_percent * 100
    )
    transfer_fee = sum(
        ceil(entry.value * fee_basis_points / 10_000) for entry in purchases
    )
    ending_bank = None
    if profile.budget_enabled:
        if scenario.starting_bank is None:
            errors.append("Startsaldo mangler")
        else:
            ending_bank = (
                scenario.starting_bank + sale_total - purchase_total - transfer_fee
            )
            if ending_bank < 0:
                errors.append("Budgettet er overskredet")

    contracts_used = (
        0
        if scenario.target_round <= profile.free_through_round
        else len(sold_ids)
    )
    remaining = (
        None
        if scenario.contracts_remaining is None
        else scenario.contracts_remaining - contracts_used
    )
    if remaining is not None and remaining < 0:
        errors.append("Der er ikke nok kontrakter til scenariet")
    if (
        scenario.contracts_remaining is None
        and contracts_used
        and profile.known
    ):
        warnings.append("Kontraktsaldo mangler; vælg basis/guld eller indtast restsaldo")

    if profile.known:
        if len(ending) != profile.roster_size:
            errors.append(
                f"Slutholdet skal have {profile.roster_size} pladser; det har {len(ending)}"
            )
        counts = Counter(_position(entry.position) for entry in ending)
        for position, minimum, maximum in profile.position_limits:
            count = counts[position]
            if count < minimum or count > maximum:
                errors.append(
                    f"{position}: {count} valgt, tilladt {minimum}-{maximum}"
                )
        if profile.category_count is not None and profile.category_size is not None:
            category_counts = Counter(_position(entry.position) for entry in ending)
            if len(category_counts) != profile.category_count or any(
                count != profile.category_size for count in category_counts.values()
            ):
                errors.append(
                    f"Der skal være {profile.category_size} spillere i hver af "
                    f"{profile.category_count} kategorier"
                )
        if profile.max_from_team is not None:
            team_counts = Counter(entry.team.strip().casefold() for entry in ending)
            offenders = [
                team for team, count in team_counts.items()
                if team and count > profile.max_from_team
            ]
            if offenders:
                errors.append(
                    f"Maks. {profile.max_from_team} fra samme klub/hold er overskredet"
                )
        captains = set(scenario.captain_player_ids)
        if len(captains) != profile.captain_count:
            errors.append(f"Der skal vælges {profile.captain_count} kaptajn(er)")
        if not captains.issubset(set(ending_ids)):
            errors.append("Kaptajnen skal være på slutholdet")
        status: Literal["valid", "invalid", "unverified"] = (
            "invalid" if errors else "valid"
        )
    else:
        warnings.append("Regler kan ikke valideres for dette fremtidige format")
        status = "unverified"

    if (
        not profile.known
        or scenario.team_round is None
        or scenario.player_round is None
        or scenario.team_round != scenario.player_round
    ):
        certainty: Literal["final", "preliminary", "unverified"] = "unverified"
    elif (
        scenario.team_round_status == "complete"
        and scenario.player_round_status == "complete"
    ):
        certainty = "final"
    else:
        certainty = "preliminary"

    return TransferValidation(
        profile,
        status,
        ending,
        ending_bank,
        purchase_total,
        sale_total,
        transfer_fee,
        contracts_used,
        remaining,
        tuple(dict.fromkeys(errors)),
        tuple(dict.fromkeys(warnings)),
        certainty,
    )
