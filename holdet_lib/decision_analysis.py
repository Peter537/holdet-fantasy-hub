"""Pure, cache-only decision analyses for player, team and group snapshots."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from functools import cache
import hashlib
from itertools import combinations
import json
from math import isfinite
import random
from statistics import mean, median, stdev
from time import perf_counter
from typing import Iterable, Literal, Mapping

from .errors import UnsupportedGameError
from .groups import GroupDefinition
from .hub_settings import player_identity
from .models import GameUrl, PlayerEntry, RoundStatus, RoundSummary
from .rules import AnalysisProvenance, GameRuleProfile
from .sport_adapters import get_sport_adapter, normalize_position_limits
from .standings import build_standings
from .storage import PlayerStatisticsIndex, PlayerStatisticsSnapshot, SnapshotIndex, TeamSnapshot
from .transfers import (
    TransferScenario,
    captain_position_allowed,
    normalize_roster_position,
    roster_prices_match,
    simulate_transfers,
)


@dataclass(frozen=True, slots=True)
class PlayerValueObservation:
    """A round's value and status from the same newest eligible snapshot."""

    round_number: int
    value: int
    round_status: RoundStatus
    generated_at: datetime


@dataclass(frozen=True, slots=True)
class PlayerDecisionAnalysis:
    player_key: str
    name: str
    latest_value: int | None
    total_growth: int | None
    growth_per_million: float | None
    form_3: float | None
    form_5: float | None
    stability_score: int | None
    stability_label: str | None
    curve: tuple[tuple[int, int], ...]
    provenance: AnalysisProvenance
    source_round: int | None = None
    source_generated_at: datetime | None = None
    source_round_status: RoundStatus = "unknown"
    present_in_selected_round: bool = True
    observations: tuple[PlayerValueObservation, ...] = ()
    provenances: tuple[tuple[str, AnalysisProvenance], ...] = ()

    def metric_provenance(self, name: str) -> AnalysisProvenance:
        """Explain value, total_growth, growth_per_million, form_3/5 or stability."""

        return dict(self.provenances)[name]

    def observation_gaps(self, name: str) -> tuple[int, ...]:
        """Identify unavailable rounds inside a metric's actual observation window."""

        rounds = self.metric_provenance(name).rounds
        if len(rounds) < 2:
            return ()
        present = set(rounds)
        return tuple(
            number
            for number in range(min(rounds), max(rounds) + 1)
            if number not in present
        )


@dataclass(frozen=True, slots=True)
class CaptainAlternative:
    player_id: int
    name: str
    bonus: int
    total_change: int
    player_ids: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class CaptainAnalysis:
    actual_bonus: int
    computed_actual_bonus: int | None
    mismatch: bool
    alternatives: tuple[CaptainAlternative, ...]
    provenance: AnalysisProvenance


@dataclass(frozen=True, slots=True)
class BankAnalysis:
    available_bank: int
    investment: int
    transfer_fee: int | None
    remaining_bank: int | None
    full_bank_interest: int | None
    remaining_bank_interest: int | None
    lost_interest: int | None
    break_even_growth: int | None
    break_even_percent: float | None
    actual_interest: int | None
    beat_share_3: float | None
    beat_share_5: float | None
    compared_players_3: int
    compared_players_5: int
    provenance: AnalysisProvenance


@dataclass(frozen=True, slots=True)
class TransferDecision:
    round_number: int
    bought: tuple[str, ...]
    sold: tuple[str, ...]
    bought_growth: int | None
    sold_counterfactual_growth: int | None
    fee_cost: int | None
    actual_change: int | None
    decision_delta: int | None
    no_trade_change: int | None
    provenance: AnalysisProvenance


@dataclass(frozen=True, slots=True)
class TeamDecisionLedger:
    team_id: int
    decisions: tuple[TransferDecision, ...]
    best: TransferDecision | None
    worst: TransferDecision | None
    verified_total: int | None


@dataclass(frozen=True, slots=True)
class ExposureRow:
    player_id: int
    name: str
    owners: int
    covered_teams: int

    @property
    def ownership_percent(self) -> float:
        return 100.0 * self.owners / self.covered_teams if self.covered_teams else 0.0


@dataclass(frozen=True, slots=True)
class GroupExposure:
    rows: tuple[ExposureRow, ...]
    covered_team_ids: tuple[int, ...]
    missing_team_ids: tuple[int, ...]
    total_teams: int
    provenance: AnalysisProvenance


@dataclass(frozen=True, slots=True)
class GroupComparison:
    own_team_id: int
    leader_team_id: int
    own_team_name: str
    leader_team_name: str
    common_players: tuple[str, ...]
    own_only: tuple[str, ...]
    leader_only: tuple[str, ...]
    actual_swing: int | None
    form_proxy_swing: float | None
    provenance: AnalysisProvenance


@dataclass(frozen=True, slots=True)
class IdealTeamResult:
    status: Literal["optimal", "timeout", "infeasible", "unverified"]
    players: tuple[PlayerEntry, ...]
    objective: int | None
    objective_upper_bound: int | None
    total_cost: int | None
    excluded_missing_growth: int
    explored_nodes: int
    provenance: AnalysisProvenance


@dataclass(frozen=True, slots=True)
class SimulationResult:
    status: Literal["complete", "descriptive", "unavailable"]
    simulations: int
    horizon: int
    seed: int
    median_delta: float | None
    p10: float | None
    p90: float | None
    probability_better: float | None
    observed_deltas: tuple[int, ...]
    input_coverage: float
    backtest_observations: int
    backtest_model_mae: float | None
    backtest_latest_mae: float | None
    backtest_form_3_mae: float | None
    provenance: AnalysisProvenance


@dataclass(frozen=True, slots=True)
class ModelTransferFeasibility:
    status: Literal["valid", "invalid", "unverified"]
    round_number: int
    reasons: tuple[str, ...] = ()


def _entries(snapshot) -> dict[str, PlayerEntry]:
    game = snapshot.statistics.game
    return {player_identity(game, entry): entry for entry in snapshot.statistics.entries}


def build_player_decision_analysis(
    snapshots: PlayerStatisticsIndex,
    game: GameUrl,
    player_key: str,
    *,
    round_number: int | None = None,
    generated_at: datetime | None = None,
) -> PlayerDecisionAnalysis | None:
    """Build player metrics within inclusive round and optional fetch-time bounds.

    Current values use the highest eligible round, even after historical
    backfill. Form uses each round's newest eligible snapshot only if complete.
    A missing player retains last-known values with unverified provenance.
    """

    history = snapshots.through(
        game, round_number=round_number, generated_at=generated_at
    )
    rounds = history.per_round(game)
    if not rounds:
        return None
    selected_round = (
        round_number
        if round_number is not None
        else rounds[-1].statistics.round_number
    )
    selected = next(
        (
            snapshot
            for snapshot in rounds
            if snapshot.statistics.round_number == selected_round
        ),
        None,
    )
    present_in_selected_round = selected is not None and player_key in _entries(selected)
    latest_entry: PlayerEntry | None = None
    source = None
    for snapshot in sorted(
        history.for_game(game),
        key=lambda item: (item.statistics.round_number, item.generated_at),
    ):
        entry = _entries(snapshot).get(player_key)
        if entry is not None:
            latest_entry = entry
            source = snapshot
    if latest_entry is None or source is None:
        return None

    observations: list[PlayerValueObservation] = []
    completed: list[tuple[int, int]] = []
    for snapshot in rounds:
        entry = _entries(snapshot).get(player_key)
        if entry is None:
            continue
        statistics = snapshot.statistics
        observations.append(
            PlayerValueObservation(
                statistics.round_number,
                entry.value,
                statistics.round_status,
                snapshot.generated_at,
            )
        )
        if statistics.round_status == "complete" and entry.round_growth is not None:
            completed.append((statistics.round_number, entry.round_growth))
    ordered = tuple(completed)
    values = [value for _, value in ordered]
    recent = values[-5:]
    stability_score: int | None = None
    stability_label: str | None = None
    if len(recent) >= 3:
        dispersion = stdev(recent)
        scale = max(mean(abs(value) for value in recent), 1)
        stability_score = round(100 / (1 + dispersion / scale))
        stability_label = (
            "Stabil" if stability_score >= 70 else
            "Balanceret" if stability_score >= 40 else "Boom/bust"
        )
    money = source.statistics.unit == "money"
    growth_per_million = (
        latest_entry.total_growth / (latest_entry.value / 1_000_000)
        if money
        and latest_entry.value > 0
        and latest_entry.total_growth is not None
        else None
    )
    source_rounds = (source.statistics.round_number,)
    certainty = (
        "unverified"
        if not present_in_selected_round or source.statistics.round_status == "unknown"
        else "final" if source.statistics.round_status == "complete" else "preliminary"
    )
    source_reasons: tuple[str, ...] = ()
    if not present_in_selected_round:
        source_reasons = (
            f"Spilleren mangler i det valgte snapshot for runde {selected_round}",
        )
    elif source.statistics.round_status == "unknown":
        source_reasons = ("Kildens rundestatus er ikke verificeret",)

    def source_provenance(reason: str | None = None) -> AnalysisProvenance:
        return AnalysisProvenance(
            "unverified" if reason else certainty,
            source_rounds,
            0 if reason else 1,
            source_reasons + ((reason,) if reason else ()),
        )

    def window_provenance(size: int, minimum: int) -> AnalysisProvenance:
        used = tuple(number for number, _ in ordered[-size:])
        return AnalysisProvenance(
            "final" if len(used) >= minimum else "unverified",
            used,
            len(used),
            () if len(used) >= minimum else (
                f"Kræver {minimum} afsluttede observationer; kun {len(used)} er tilgængelige",
            ),
        )

    efficiency_reason = (
        "Vækst pr. mio. bruges kun i pengespil" if not money
        else "Prisen skal være større end nul" if latest_entry.value <= 0
        else "Totalvækst mangler" if latest_entry.total_growth is None
        else None
    )
    provenances = (
        ("value", source_provenance()),
        (
            "total_growth",
            source_provenance(
                "Totalvækst mangler" if latest_entry.total_growth is None else None
            ),
        ),
        ("growth_per_million", source_provenance(efficiency_reason)),
        ("form_3", window_provenance(3, 3)),
        ("form_5", window_provenance(5, 5)),
        ("stability", window_provenance(5, 3)),
    )
    form_rounds = tuple(number for number, _ in ordered[-5:])
    missing = source_reasons + (
        () if ordered else ("Ingen afsluttede runder med spillervækst",)
    )
    return PlayerDecisionAnalysis(
        player_key,
        latest_entry.name,
        latest_entry.value,
        latest_entry.total_growth,
        growth_per_million,
        mean(values[-3:]) if len(values) >= 3 else None,
        mean(values[-5:]) if len(values) >= 5 else None,
        stability_score,
        stability_label,
        tuple((point.round_number, point.value) for point in observations),
        AnalysisProvenance(certainty, form_rounds, len(form_rounds), missing),
        source.statistics.round_number,
        source.generated_at,
        source.statistics.round_status,
        present_in_selected_round,
        tuple(observations),
        provenances,
    )


def build_captain_analysis(
    snapshot: TeamSnapshot,
    summary: RoundSummary,
    rules: GameRuleProfile,
) -> CaptainAnalysis:
    reasons: list[str] = []
    if not rules.verified:
        reasons.append("Sæsonens kaptajnregler er ikke verificeret")
    if rules.sport is not None:
        try:
            get_sport_adapter(rules.sport)
        except UnsupportedGameError:
            reasons.append("Spillets sport understøttes ikke")
    if (
        rules.captain_count is None
        or rules.captain_count < 1
        or rules.captain_multiplier is None
    ):
        reasons.append("Antal kaptajner eller multiplikator mangler")
    actual = tuple(
        entry for entry in snapshot.team.roster
        if entry.role.strip().casefold() in {"captain", "kaptajn"}
    )
    profile = rules.to_transfer_profile()
    eligible = snapshot.team.roster
    if not reasons:
        if rules.requires_captain_positions and not profile.captain_positions:
            reasons.append("Kaptajnens tilladte positioner mangler")
        elif profile.captain_positions:
            eligible = tuple(
                entry for entry in snapshot.team.roster
                if captain_position_allowed(profile, entry.position)
            )
            if any(entry not in eligible for entry in actual):
                reasons.append("En kaptajn mangler en tilladt position i rundetruppen")
    computed = None
    mismatch = False
    alternatives: tuple[CaptainAlternative, ...] = ()
    if not reasons and len(actual) == rules.captain_count:
        extra = rules.captain_multiplier - 1
        def bonus(entries):
            return sum(
                (max(0, entry.round_change) if rules.captain_positive_only else entry.round_change) * extra
                for entry in entries
            )
        computed = bonus(actual)
        mismatch = computed != summary.captain_bonus
        if not mismatch:
            alternatives = tuple(
                CaptainAlternative(
                    group[0].player_id,
                    ", ".join(entry.name for entry in group),
                    bonus(group),
                    summary.change
                    - summary.captain_bonus
                    + bonus(group),
                    tuple(sorted(entry.player_id for entry in group)),
                )
                for group in sorted(
                    combinations(eligible, rules.captain_count),
                    key=lambda values: (
                        -sum(item.round_change for item in values),
                        tuple(item.name.casefold() for item in values),
                        tuple(item.player_id for item in values),
                    ),
                )
            )
        elif mismatch:
            reasons.append("Beregnet kaptajnbonus matcher ikke Holdets officielle bonus")
    elif not reasons:
        reasons.append("Rundetrup og verificeret kaptajnantal matcher ikke")
    certainty = (
        "final"
        if not reasons and summary.round_status == "complete"
        else "preliminary" if not reasons else "unverified"
    )
    return CaptainAnalysis(
        summary.captain_bonus,
        computed,
        mismatch,
        alternatives,
        AnalysisProvenance(certainty, (summary.round_number,), 1, tuple(reasons)),
    )


def build_bank_analysis(
    available_bank: int,
    investment: int,
    rules: GameRuleProfile,
    *,
    actual_interest: int | None = None,
    round_number: int | None = None,
    round_status: RoundStatus | None = None,
    historical_windows: Iterable[tuple[float | None, float | None]] = (),
) -> BankAnalysis:
    reasons: list[str] = []
    if investment <= 0:
        reasons.append("Investeringen skal være positiv")
    fee = rules.transfer_fee(investment, round_number=round_number)
    full_interest = rules.interest(available_bank)
    if fee is None:
        reasons.append("Sæsonens transfergebyr er ikke verificeret")
    if full_interest is None:
        reasons.append("Sæsonens bankrente er ikke verificeret")
    remaining = None if fee is None else available_bank - investment - fee
    if remaining is not None and remaining < 0:
        reasons.append("Investeringen overstiger bank og gebyr")
    remaining_interest = (
        rules.interest(remaining)
        if remaining is not None and remaining >= 0
        else None
    )
    if remaining is not None and remaining >= 0 and remaining_interest is None:
        reasons.append("Bankrente eller afrunding for restsaldo er ikke verificeret")
    lost = (
        full_interest - remaining_interest
        if full_interest is not None and remaining_interest is not None
        else None
    )
    break_even = fee + lost if fee is not None and lost is not None else None
    percent = 100 * break_even / investment if break_even is not None and investment > 0 else None
    windows = tuple(historical_windows)
    form_3 = tuple(value[0] for value in windows if value[0] is not None)
    form_5 = tuple(value[1] for value in windows if value[1] is not None)
    beat_share_3 = (
        sum(value > break_even for value in form_3) / len(form_3)
        if break_even is not None and form_3
        else None
    )
    beat_share_5 = (
        sum(value > break_even for value in form_5) / len(form_5)
        if break_even is not None and form_5
        else None
    )
    if reasons or round_status == "unknown":
        certainty: Literal["final", "preliminary", "unverified"] = "unverified"
    elif round_status not in {None, "complete"}:
        certainty = "preliminary"
    else:
        certainty = "final"
    if round_status not in {None, "complete"}:
        reasons.append("Bankens rundedata er ikke afsluttet")
    return BankAnalysis(
        available_bank,
        investment,
        fee,
        remaining if remaining is not None and remaining >= 0 else None,
        full_interest,
        remaining_interest,
        lost,
        break_even,
        percent,
        actual_interest,
        beat_share_3,
        beat_share_5,
        len(form_3),
        len(form_5),
        AnalysisProvenance(
            certainty,
            () if round_number is None else (round_number,),
            1 if round_number is not None else 0,
            tuple(dict.fromkeys(reasons)),
        ),
    )


def _player_sort_key(entry: PlayerEntry) -> tuple[bool, int]:
    if entry.entry_id is not None:
        return False, entry.entry_id
    return True, entry.source_index


def build_team_decision_ledger(
    teams: SnapshotIndex,
    players: PlayerStatisticsIndex,
    game: GameUrl,
    team_id: int,
    rules: GameRuleProfile,
) -> TeamDecisionLedger:
    rounds = tuple(sorted(teams.rounds_for(game, (team_id,))))
    decisions: list[TransferDecision] = []
    for previous_round, current_round in zip(rounds, rounds[1:]):
        previous = teams.roster_for(game, team_id, previous_round)
        current = teams.roster_for(game, team_id, current_round)
        located = teams.summary_for(game, team_id, current_round)
        if previous is None or current is None or located is None:
            continue
        old = {entry.player_id: entry for entry in previous.team.roster}
        new = {entry.player_id: entry for entry in current.team.roster}
        bought_ids = sorted(new.keys() - old.keys())
        sold_ids = sorted(old.keys() - new.keys())
        if not bought_ids and not sold_ids:
            continue
        reasons: list[str] = []
        if current_round != previous_round + 1:
            reasons.append("Runderne er ikke sammenhængende")
        player_snapshot = players.newest(game, current_round)
        player_map = (
            {} if player_snapshot is None else
            {entry.entry_id: entry for entry in player_snapshot.statistics.entries if entry.entry_id is not None}
        )
        bought_values = [player_map.get(player_id) for player_id in bought_ids]
        sold_values = [player_map.get(player_id) for player_id in sold_ids]
        if player_snapshot is None:
            reasons.append("Spillersnapshot mangler")
        if any(entry is None or entry.round_growth is None for entry in (*bought_values, *sold_values)):
            reasons.append("Rundevækst mangler for en købt eller solgt spiller")
        summary = located[1]
        if summary.round_status != "complete" or (
            player_snapshot is not None
            and player_snapshot.statistics.round_status != "complete"
        ):
            reasons.append("Runden er ikke afsluttet")
        purchase_total = sum(new[player_id].value for player_id in bought_ids)
        fee = (
            abs(summary.transfer)
            if rules.verified and rules.transfer_summary_is_fee and summary.transfer is not None
            else rules.transfer_fee(purchase_total, round_number=current_round)
        )
        if fee is None:
            reasons.append("Transfergebyret er ikke verificeret")
        bought_growth = (
            sum(entry.round_growth for entry in bought_values if entry is not None and entry.round_growth is not None)
            if not any(entry is None or entry.round_growth is None for entry in bought_values)
            else None
        )
        sold_growth = (
            sum(entry.round_growth for entry in sold_values if entry is not None and entry.round_growth is not None)
            if not any(entry is None or entry.round_growth is None for entry in sold_values)
            else None
        )
        delta = (
            bought_growth - fee - sold_growth
            if bought_growth is not None and sold_growth is not None and fee is not None and not reasons
            else None
        )
        decisions.append(
            TransferDecision(
                current_round,
                tuple(new[player_id].name for player_id in bought_ids),
                tuple(old[player_id].name for player_id in sold_ids),
                bought_growth,
                sold_growth,
                fee,
                summary.change,
                delta,
                summary.change - delta if delta is not None else None,
                AnalysisProvenance(
                    "final" if delta is not None else "unverified",
                    (previous_round, current_round),
                    2,
                    tuple(dict.fromkeys(reasons)),
                ),
            )
        )
    verified = tuple(item for item in decisions if item.decision_delta is not None)
    best = max(verified, key=lambda item: (item.decision_delta, -item.round_number), default=None)
    worst = min(verified, key=lambda item: (item.decision_delta, item.round_number), default=None)
    return TeamDecisionLedger(
        team_id,
        tuple(decisions),
        best,
        worst,
        sum(item.decision_delta for item in verified) if verified else None,
    )


def build_group_exposure(
    group: GroupDefinition,
    snapshots: SnapshotIndex,
    round_number: int,
) -> GroupExposure:
    counts: Counter[int] = Counter()
    names: dict[int, str] = {}
    covered: list[int] = []
    missing: list[int] = []
    final = True
    for member in group.teams:
        snapshot = snapshots.roster_for(group.game, member.team_id, round_number)
        if snapshot is None:
            missing.append(member.team_id)
            continue
        covered.append(member.team_id)
        summary = snapshots.summary_for(group.game, member.team_id, round_number)
        final = final and summary is not None and summary[1].round_status == "complete"
        for entry in snapshot.team.roster:
            counts[entry.player_id] += 1
            names.setdefault(entry.player_id, entry.name)
    rows = tuple(
        ExposureRow(player_id, names[player_id], owners, len(covered))
        for player_id, owners in sorted(
            counts.items(), key=lambda item: (-item[1], names[item[0]].casefold(), item[0])
        )
    )
    reasons = () if not missing else (f"{len(missing)} hold mangler rundetrup",)
    certainty = "final" if covered and final and not missing else "preliminary" if covered else "unverified"
    return GroupExposure(
        rows,
        tuple(covered),
        tuple(missing),
        len(group.teams),
        AnalysisProvenance(certainty, (round_number,), len(covered), reasons),
    )


def historical_growth_windows(
    players: PlayerStatisticsIndex, game: GameUrl, round_number: int
) -> dict[int, tuple[float | None, float | None]]:
    """Build stable-player form windows through an inclusive historical round."""
    values: dict[int, list[int]] = {}
    for snapshot in players.per_round(game, round_number=round_number, completed_only=True):
        for player_id, growth in {
            entry.entry_id: entry.round_growth
            for entry in snapshot.statistics.entries
            if entry.entry_id is not None and entry.round_growth is not None
        }.items():
            values.setdefault(player_id, []).append(growth)
    return {
        player_id: (
            mean(series[-3:]) if len(series) >= 3 else None,
            mean(series[-5:]) if len(series) >= 5 else None,
        )
        for player_id, series in values.items()
    }


def build_group_comparison(
    group: GroupDefinition,
    snapshots: SnapshotIndex,
    players: PlayerStatisticsIndex,
    own_team_id: int,
    round_number: int,
) -> GroupComparison | None:
    standings = build_standings(group, snapshots, round_number, "overall")
    if not standings:
        return None
    leader = standings[0]
    own = snapshots.roster_for(group.game, own_team_id, round_number)
    lead = snapshots.roster_for(group.game, leader.team_id, round_number)
    if own is None or lead is None:
        return None
    own_map = {entry.player_id: entry for entry in own.team.roster}
    leader_map = {entry.player_id: entry for entry in lead.team.roster}
    common = sorted(own_map.keys() & leader_map.keys())
    own_only_ids = sorted(own_map.keys() - leader_map.keys())
    leader_only_ids = sorted(leader_map.keys() - own_map.keys())
    player_snapshot = players.newest(group.game, round_number)
    growth = (
        {} if player_snapshot is None else
        {entry.entry_id: entry.round_growth for entry in player_snapshot.statistics.entries if entry.entry_id is not None}
    )
    differential = (*own_only_ids, *leader_only_ids)
    actual = 0 if not differential else None
    if differential and all(growth.get(player_id) is not None for player_id in differential):
        actual = sum(growth[player_id] for player_id in own_only_ids) - sum(
            growth[player_id] for player_id in leader_only_ids
        )
    windows = historical_growth_windows(players, group.game, round_number) if differential else {}
    own_forms = [windows.get(player_id, (None, None))[0] for player_id in own_only_ids]
    leader_forms = [windows.get(player_id, (None, None))[0] for player_id in leader_only_ids]
    form_proxy = 0.0 if not differential else None
    if differential and all(value is not None for value in (*own_forms, *leader_forms)):
        form_proxy = sum(value for value in own_forms if value is not None) - sum(
            value for value in leader_forms if value is not None
        )
    reasons: list[str] = []
    if actual is None:
        reasons.append("Rundevækst mangler for en eller flere differentspillere")
    if form_proxy is None:
        reasons.append("Tre afsluttede formrunder mangler for en eller flere differentspillere")
    summary = snapshots.summary_for(group.game, own_team_id, round_number)
    own_complete = summary is not None and summary[1].round_status == "complete"
    leader_complete = leader.summary is not None and leader.summary.round_status == "complete"
    players_complete = not differential or (
        player_snapshot is not None and player_snapshot.statistics.round_status == "complete"
    )
    if not own_complete:
        reasons.append("Eget holds rundedata er ikke afsluttet")
    if not leader_complete:
        reasons.append("Gruppeførerens rundedata er ikke afsluttet")
    if not players_complete:
        reasons.append("Spillernes rundedata er ikke afsluttet")
    certainty: Literal["final", "preliminary", "unverified"]
    if actual is not None and own_complete and leader_complete and players_complete:
        certainty = "final"
    elif actual is not None or form_proxy is not None:
        certainty = "preliminary"
    else:
        certainty = "unverified"
    return GroupComparison(
        own_team_id,
        leader.team_id,
        own.team.team_name,
        lead.team.team_name,
        tuple(own_map[player_id].name for player_id in common),
        tuple(own_map[player_id].name for player_id in own_only_ids),
        tuple(leader_map[player_id].name for player_id in leader_only_ids),
        actual,
        form_proxy,
        AnalysisProvenance(certainty, (round_number,), len(differential), tuple(reasons)),
    )


def optimize_ideal_team(
    entries: Iterable[PlayerEntry],
    rules: GameRuleProfile,
    *,
    round_number: int | None = None,
    round_complete: bool = True,
    timeout_seconds: float = 5.0,
) -> IdealTeamResult:
    reasons: list[str] = []
    if not rules.verified:
        reasons.append("Spillets sæsonregler er ikke verificeret")
    messages = {
        "roster_size": "Trupstørrelsen mangler",
        "budget_enabled": "Budgetreglen er ikke verificeret",
        "salary_cap": "Budgetgrænsen mangler",
        "formation": "Formation eller kategoriregler mangler",
        "position_limits": "Positionsgrænserne mangler",
        "max_from_team": "Grænsen for spillere pr. hold mangler",
        "categories": "Kategorireglerne mangler",
        "sport": "Spillets sport understøttes ikke",
    }
    reasons.extend(messages[field] for field in rules.missing_roster_rules())
    if not round_complete:
        reasons.append("Runden er ikke afsluttet")
    source = tuple(entries)
    missing_growth = sum(entry.round_growth is None for entry in source)
    stable_ids = [entry.entry_id for entry in source if entry.entry_id is not None]
    if len(stable_ids) != len(set(stable_ids)):
        reasons.append("Spillersnapshotet indeholder gentagne spiller-ID'er")
    if reasons:
        return IdealTeamResult(
            "unverified", (), None, None, None, missing_growth, 0,
            AnalysisProvenance("unverified", () if round_number is None else (round_number,), 0, tuple(reasons)),
        )
    assert rules.roster_size is not None
    profile = rules.to_transfer_profile()

    @cache
    def position_of(value: str) -> str:
        return normalize_roster_position(profile, value)

    limits = normalize_position_limits(rules.position_limits, position_of)
    candidates = [entry for entry in source if entry.round_growth is not None]
    if limits:
        candidates = [entry for entry in candidates if position_of(entry.position) in limits]
    candidates.sort(
        key=lambda entry: (
            -int(entry.round_growth or 0), entry.value,
            _player_sort_key(entry), entry.name.casefold(),
        )
    )
    root_upper_bound = (
        sum(int(item.round_growth or 0) for item in candidates[:rules.roster_size])
        if len(candidates) >= rules.roster_size
        else None
    )
    deadline = perf_counter() + max(0.0, min(timeout_seconds, 30.0))
    best: tuple[PlayerEntry, ...] | None = None
    best_objective: int | None = None
    best_cost: int | None = None
    best_keys: tuple[tuple[bool, int], ...] | None = None
    explored = 0
    timed_out = timeout_seconds <= 0

    def valid_final(selected: tuple[PlayerEntry, ...], counts: Counter[str]) -> bool:
        if len(selected) != rules.roster_size:
            return False
        if any(not (minimum <= counts[position] <= maximum) for position, (minimum, maximum) in limits.items()):
            return False
        if rules.category_count is not None and rules.category_size is not None:
            categories = Counter(position_of(entry.position) for entry in selected)
            if len(categories) != rules.category_count or any(value != rules.category_size for value in categories.values()):
                return False
        return True

    def better(objective: int, cost: int, keys: tuple[tuple[bool, int], ...]) -> bool:
        return best_objective is None or (
            objective > best_objective
            or objective == best_objective and (
                best_cost is None or cost < best_cost
                or cost == best_cost and (best_keys is None or keys < best_keys)
            )
        )

    frontier: list[tuple[int, tuple[PlayerEntry, ...], int, int]] = [(0, (), 0, 0)]
    while frontier and not timed_out:
        index, selected, objective, cost = frontier.pop()
        explored += 1
        if explored % 1024 == 0 and perf_counter() >= deadline:
            timed_out = True
            break
        need = rules.roster_size - len(selected)
        if len(candidates) - index < need:
            continue
        if rules.budget_enabled and rules.salary_cap is not None and cost > rules.salary_cap:
            continue
        if need:
            top_bound = objective + sum(int(item.round_growth or 0) for item in candidates[index:index + need])
            if best_objective is not None and top_bound < best_objective:
                continue
            if rules.budget_enabled and rules.salary_cap is not None:
                cheapest = sorted(item.value for item in candidates[index:])[:need]
                if len(cheapest) < need or cost + sum(cheapest) > rules.salary_cap:
                    continue
        positions = Counter(position_of(item.position) for item in selected)
        if limits:
            remaining_positions = Counter(position_of(item.position) for item in candidates[index:])
            if any(
                positions[position] > maximum or positions[position] + remaining_positions[position] < minimum
                for position, (minimum, maximum) in limits.items()
            ):
                continue
        if need == 0:
            if valid_final(selected, positions):
                keys = tuple(sorted(_player_sort_key(item) for item in selected))
                if better(objective, cost, keys):
                    best = selected
                    best_objective = objective
                    best_cost = cost
                    best_keys = keys
            continue
        entry = candidates[index]
        position = position_of(entry.position)
        team = entry.team.strip().casefold()
        can_include = not limits or positions[position] < limits[position][1]
        if rules.category_size is not None and positions[position] >= rules.category_size:
            can_include = False
        if rules.max_from_team is not None and team and sum(
            item.team.strip().casefold() == team for item in selected
        ) >= rules.max_from_team:
            can_include = False
        frontier.append((index + 1, selected, objective, cost))
        if can_include:
            frontier.append((
                index + 1,
                (*selected, entry),
                objective + int(entry.round_growth or 0),
                cost + entry.value,
            ))
    status: Literal["optimal", "timeout", "infeasible", "unverified"] = (
        "timeout" if timed_out else "optimal" if best is not None else "infeasible"
    )
    certainty = "final" if status == "optimal" else "preliminary" if status == "timeout" else "unverified"
    return IdealTeamResult(
        status,
        best or (),
        best_objective,
        (
            best_objective
            if status == "optimal"
            else root_upper_bound if status == "timeout" else None
        ),
        best_cost,
        missing_growth,
        explored,
        AnalysisProvenance(
            certainty,
            () if round_number is None else (round_number,),
            len(candidates),
            () if status == "optimal" else (("Søgningen nåede tidsgrænsen",) if status == "timeout" else ("Ingen gyldig trup blev fundet",)),
        ),
    )


def _percentile(values: list[int], percentile: float) -> float:
    if not values:
        raise ValueError("Percentil kræver observationer")
    ordered = sorted(values)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def completed_model_vectors(
    players: PlayerStatisticsIndex, game: GameUrl, through_round: int
) -> tuple[tuple[tuple[int, dict[str, int | None]], ...], dict[str, str]]:
    """Use one completed correction per round and stable catalogue identities."""
    vectors = []
    positions: dict[str, str] = {}
    for snapshot in players.per_round(game, round_number=through_round, completed_only=True):
        vector = {}
        for entry in snapshot.statistics.entries:
            if entry.entry_id is not None:
                key = str(entry.entry_id)
                vector[key] = entry.round_growth
                positions[key] = entry.position
        vectors.append((snapshot.statistics.round_number, vector))
    return tuple(vectors), positions


def assess_model_transfer(
    team_snapshot: TeamSnapshot,
    player_snapshot: PlayerStatisticsSnapshot,
    rules: GameRuleProfile,
    *,
    sold_id: int,
    bought_id: int,
    final_round: int | None = None,
) -> ModelTransferFeasibility:
    """Check a next-round swap using current local evidence and the same captain."""
    team = team_snapshot.team
    players = player_snapshot.statistics
    current_round = team.overview.current_round
    target_round = current_round + 1
    if final_round is not None and target_round > final_round:
        return ModelTransferFeasibility("invalid", target_round, ("Spillet har ingen efterfølgende runde",))

    profile = rules.to_transfer_profile()
    if not profile.known or rules.captain_count is None or rules.free_through_round is None:
        return ModelTransferFeasibility("unverified", target_round, ("Verificerede transferregler mangler",))

    if (
        team_snapshot.identity[:2] != player_snapshot.identity[:2]
        or team_snapshot.identity[:2] != rules.identity
        or rules.game_id is not None and team.game_id != rules.game_id
        or current_round < 1
        or players.round_number != current_round
        or players.unit != team.overview.unit
        or rules.budget_enabled != (players.unit == "money")
    ):
        return ModelTransferFeasibility("unverified", target_round, ("Spil, rundedata og regler stemmer ikke overens",))

    team_round = next((summary for summary in team.history if summary.round_number == current_round), None)
    if team_round is None or team_round.round_status != "complete" or players.round_status != "complete":
        return ModelTransferFeasibility("unverified", target_round, ("Byttet kræver afsluttede hold- og spillersnapshots",))

    roster = {entry.player_id: entry for entry in team.roster}
    catalogue = {entry.entry_id: entry for entry in players.entries if entry.entry_id is not None}
    if len(roster) != len(team.roster) or len(catalogue) != sum(entry.entry_id is not None for entry in players.entries):
        return ModelTransferFeasibility("unverified", target_round, ("Kilderne har gentagne spiller-ID'er",))
    if sold_id not in roster or bought_id not in catalogue or bought_id in roster:
        return ModelTransferFeasibility("invalid", target_round, ("Byttet kræver en spiller på holdet og et nyt køb fra kataloget",))

    if rules.budget_enabled and (
        team.overview.bank is None
        or rules.transfer_fee_basis_points is None
        or rules.transfer_fee_basis_points > 0 and rules.fee_free_through_round is None
        or not roster_prices_match(team.roster, catalogue)
    ):
        return ModelTransferFeasibility("unverified", target_round, ("Banksaldo, priser eller gebyrregler mangler eller er uforenelige",))
    if target_round > rules.free_through_round and team.overview.substitutions_remaining is None:
        return ModelTransferFeasibility("unverified", target_round, ("Den resterende kontraktsaldo mangler",))

    captain_ids = tuple(
        entry.player_id for entry in team.roster
        if entry.role.strip().casefold() in {"captain", "kaptajn"}
    )
    if len(captain_ids) != rules.captain_count:
        return ModelTransferFeasibility("unverified", target_round, ("De registrerede kaptajnroller mangler eller er uforenelige",))
    validation = simulate_transfers(profile, TransferScenario(
        initial_roster=team.roster,
        available_players=tuple(catalogue.values()),
        sold_player_ids=(sold_id,),
        bought_player_ids=(bought_id,),
        starting_bank=team.overview.bank,
        contracts_remaining=team.overview.substitutions_remaining,
        target_round=target_round,
        team_round=current_round,
        player_round=players.round_number,
        captain_player_ids=captain_ids,
        team_round_status=team_round.round_status,
        player_round_status=players.round_status,
    ))
    if validation.status == "valid":
        return ModelTransferFeasibility("valid", target_round)
    return ModelTransferFeasibility(validation.status, target_round, validation.errors or validation.warnings)


def simulate_transfer_scenario(
    round_vectors: Iterable[tuple[int, Mapping[str, int | None]]],
    baseline_player_keys: Iterable[str],
    scenario_player_keys: Iterable[str],
    *,
    positions: Mapping[str, str] | None = None,
    simulations: int = 10_000,
    horizon: int = 3,
    seed: int | None = None,
    descriptive_only: bool = False,
) -> SimulationResult:
    vectors = tuple(sorted(round_vectors, key=lambda item: item[0]))
    if len({round_number for round_number, _ in vectors}) != len(vectors):
        raise ValueError("Modellen kræver én observation pr. runde")
    baseline = tuple(sorted(set(baseline_player_keys)))
    scenario = tuple(sorted(set(scenario_player_keys)))
    differential = tuple(sorted(set(baseline) ^ set(scenario)))
    total_cells = len(vectors) * len(differential)
    present_cells = sum(
        vector.get(key) is not None
        for _, vector in vectors
        for key in differential
    )
    input_coverage = present_cells / total_cells if total_cells else 0.0
    reasons: list[str] = []
    if not differential:
        reasons.append("Scenariet er identisk med baseline")
    if descriptive_only:
        reasons.append("Hypotetisk sammenligning uden verificeret bytte")
    prepared: list[tuple[int, int]] = []
    position_map = positions or {}
    for round_number, vector in vectors:
        resolved = dict(vector)
        for key in differential:
            if resolved.get(key) is not None:
                continue
            position = position_map.get(key)
            peers = [
                value for peer, value in vector.items()
                if value is not None and position and position_map.get(peer) == position
            ]
            if len(peers) >= 3:
                resolved[key] = round(median(peers))
        if all(resolved.get(key) is not None for key in differential):
            prepared.append(
                (
                    round_number,
                    sum(int(resolved[key]) for key in scenario if resolved.get(key) is not None)
                    - sum(int(resolved[key]) for key in baseline if resolved.get(key) is not None),
                )
            )
    if len(prepared) < 3:
        reasons.append("Mindst tre dækkende, afsluttede runder kræves")
    canonical = json.dumps(
        {"rounds": prepared, "baseline": baseline, "scenario": scenario, "horizon": horizon},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    chosen_seed = seed if seed is not None else int.from_bytes(hashlib.sha256(canonical).digest()[:8], "big")
    observed = tuple(value for _, value in prepared)
    model_errors: list[float] = []
    latest_errors: list[float] = []
    form_errors: list[float] = []
    if len(observed) >= 8 and not descriptive_only:
        for position in range(5, len(observed)):
            history = observed[:position]
            actual = observed[position]
            model_errors.append(abs(float(median(history)) - actual))
            latest_errors.append(abs(history[-1] - actual))
            form_errors.append(abs(float(mean(history[-3:])) - actual))
    backtest_count = len(model_errors)
    backtest_model = mean(model_errors) if model_errors else None
    backtest_latest = mean(latest_errors) if latest_errors else None
    backtest_form = mean(form_errors) if form_errors else None
    provenance = AnalysisProvenance(
        "final" if len(prepared) >= 5 and not descriptive_only
        else "preliminary" if len(prepared) >= 3 else "unverified",
        tuple(round_number for round_number, _ in prepared),
        len(prepared),
        tuple(dict.fromkeys(reasons)),
        source="bootstrap_model",
    )
    if len(prepared) < 3 or not differential:
        return SimulationResult(
            status="unavailable",
            simulations=0,
            horizon=horizon,
            seed=chosen_seed,
            median_delta=None,
            p10=None,
            p90=None,
            probability_better=None,
            observed_deltas=observed,
            input_coverage=input_coverage,
            backtest_observations=backtest_count,
            backtest_model_mae=backtest_model,
            backtest_latest_mae=backtest_latest,
            backtest_form_3_mae=backtest_form,
            provenance=provenance,
        )
    if len(prepared) < 5 or descriptive_only:
        return SimulationResult(
            status="descriptive",
            simulations=0,
            horizon=horizon,
            seed=chosen_seed,
            median_delta=float(median(observed)),
            p10=float(min(observed)),
            p90=float(max(observed)),
            probability_better=sum(value > 0 for value in observed) / len(observed),
            observed_deltas=observed,
            input_coverage=input_coverage,
            backtest_observations=backtest_count,
            backtest_model_mae=backtest_model,
            backtest_latest_mae=backtest_latest,
            backtest_form_3_mae=backtest_form,
            provenance=provenance,
        )
    if simulations < 1 or horizon < 1:
        raise ValueError("Simulationer og horisont skal være positive")
    rng = random.Random(chosen_seed)
    outcomes = [sum(rng.choice(observed) for _ in range(horizon)) for _ in range(simulations)]
    result = SimulationResult(
        status="complete",
        simulations=simulations,
        horizon=horizon,
        seed=chosen_seed,
        median_delta=float(median(outcomes)),
        p10=_percentile(outcomes, 0.10),
        p90=_percentile(outcomes, 0.90),
        probability_better=sum(value > 0 for value in outcomes) / simulations,
        observed_deltas=observed,
        input_coverage=input_coverage,
        backtest_observations=backtest_count,
        backtest_model_mae=backtest_model,
        backtest_latest_mae=backtest_latest,
        backtest_form_3_mae=backtest_form,
        provenance=provenance,
    )
    if not all(
        value is None or isfinite(value)
        for value in (result.median_delta, result.p10, result.p90, result.probability_better)
    ):
        raise ValueError("Simulationen producerede en ikke-endelig værdi")
    return result
