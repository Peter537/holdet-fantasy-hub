"""Pure, cache-only decision analyses for player, team and group snapshots."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import hashlib
from itertools import combinations
import json
from math import isfinite
import random
from statistics import mean, median, stdev
from time import perf_counter
from typing import Iterable, Literal, Mapping

from .groups import GroupDefinition
from .hub_settings import player_identity
from .models import GameUrl, PlayerEntry, RoundSummary
from .rules import AnalysisProvenance, GameRuleProfile
from .standings import build_standings
from .storage import PlayerStatisticsIndex, SnapshotIndex, TeamSnapshot


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


def _entries(snapshot) -> dict[str, PlayerEntry]:
    game = snapshot.statistics.game
    return {player_identity(game, entry): entry for entry in snapshot.statistics.entries}


def build_player_decision_analysis(
    snapshots: PlayerStatisticsIndex,
    game: GameUrl,
    player_key: str,
) -> PlayerDecisionAnalysis | None:
    """Build form, stability and value metrics from completed local rounds."""

    latest_entry: PlayerEntry | None = None
    latest_unit: str | None = None
    curve: list[tuple[int, int]] = []
    completed: list[tuple[int, int]] = []
    for snapshot in sorted(
        snapshots.for_game(game),
        key=lambda item: (item.statistics.round_number, item.generated_at),
    ):
        entry = _entries(snapshot).get(player_key)
        if entry is None:
            continue
        latest_entry = entry
        latest_unit = snapshot.statistics.unit
        if entry.value is not None:
            curve.append((snapshot.statistics.round_number, entry.value))
        if snapshot.statistics.round_status == "complete" and entry.round_growth is not None:
            completed.append((snapshot.statistics.round_number, entry.round_growth))
    if latest_entry is None:
        return None
    # Multiple fetches of a round contribute one newest observation.
    by_round = {round_number: value for round_number, value in completed}
    ordered = tuple(sorted(by_round.items()))
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
    money = latest_unit == "money"
    growth_per_million = (
        latest_entry.total_growth / (latest_entry.value / 1_000_000)
        if money
        and latest_entry.value > 0
        and latest_entry.total_growth is not None
        else None
    )
    rounds = tuple(round_number for round_number, _ in ordered[-5:])
    certainty = "final" if ordered else "unverified"
    missing = () if ordered else ("Ingen afsluttede runder med spillervækst",)
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
        tuple(dict(curve).items()),
        AnalysisProvenance(certainty, rounds, len(rounds), missing),
    )


def build_captain_analysis(
    snapshot: TeamSnapshot,
    summary: RoundSummary,
    rules: GameRuleProfile,
) -> CaptainAnalysis:
    reasons: list[str] = []
    if not rules.verified:
        reasons.append("Sæsonens kaptajnregler er ikke verificeret")
    if (
        rules.captain_count is None
        or rules.captain_count < 1
        or rules.captain_multiplier is None
    ):
        reasons.append("Antal kaptajner eller multiplikator mangler")
    actual = tuple(
        entry for entry in snapshot.team.roster
        if "captain" in entry.role.casefold() or "kaptajn" in entry.role.casefold()
    )
    computed = None
    mismatch = False
    alternatives: tuple[CaptainAlternative, ...] = ()
    if not reasons and len(actual) == rules.captain_count:
        extra = rules.captain_multiplier - 1
        computed = sum(entry.round_change * extra for entry in actual)
        mismatch = computed != summary.captain_bonus
        if not mismatch:
            alternatives = tuple(
                CaptainAlternative(
                    group[0].player_id,
                    ", ".join(entry.name for entry in group),
                    sum(entry.round_change * extra for entry in group),
                    summary.change
                    - summary.captain_bonus
                    + sum(entry.round_change * extra for entry in group),
                    tuple(sorted(entry.player_id for entry in group)),
                )
                for group in sorted(
                    combinations(snapshot.team.roster, rules.captain_count),
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
    historical_windows: Iterable[tuple[float | None, float | None]] = (),
) -> BankAnalysis:
    reasons: list[str] = []
    if investment <= 0:
        reasons.append("Investeringen skal være positiv")
    fee = rules.transfer_fee(investment)
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
            "final" if not reasons else "unverified",
            () if round_number is None else (round_number,),
            1 if round_number is not None else 0,
            tuple(dict.fromkeys(reasons)),
        ),
    )


def _player_id(entry: PlayerEntry) -> int:
    return entry.entry_id if entry.entry_id is not None else entry.source_index


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
            {_player_id(entry): entry for entry in player_snapshot.statistics.entries}
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
            else rules.transfer_fee(purchase_total)
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


def _form_for_player(
    players: PlayerStatisticsIndex, game: GameUrl, player_id: int
) -> float | None:
    values: dict[int, int] = {}
    for snapshot in players.for_game(game):
        if snapshot.statistics.round_status != "complete":
            continue
        entry = next(
            (item for item in snapshot.statistics.entries if _player_id(item) == player_id),
            None,
        )
        if entry is not None and entry.round_growth is not None:
            values[snapshot.statistics.round_number] = entry.round_growth
    ordered = [value for _, value in sorted(values.items())]
    return mean(ordered[-3:]) if len(ordered) >= 3 else None


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
        {_player_id(entry): entry.round_growth for entry in player_snapshot.statistics.entries}
    )
    differential = (*own_only_ids, *leader_only_ids)
    actual = 0 if not differential else None
    if differential and all(growth.get(player_id) is not None for player_id in differential):
        actual = sum(growth[player_id] for player_id in own_only_ids) - sum(
            growth[player_id] for player_id in leader_only_ids
        )
    own_forms = [_form_for_player(players, group.game, player_id) for player_id in own_only_ids]
    leader_forms = [_form_for_player(players, group.game, player_id) for player_id in leader_only_ids]
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
    certainty = "final" if actual is not None and summary and summary[1].round_status == "complete" else "preliminary"
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


def _position(value: str) -> str:
    aliases = {
        "målmand": "goalkeeper", "keeper": "goalkeeper",
        "forsvar": "defender", "forsvarer": "defender",
        "midt": "midfielder", "midtbane": "midfielder",
        "angreb": "forward", "angriber": "forward", "attacker": "forward",
        "kører": "driver", "konstruktør": "constructor",
        "konstruktoer": "constructor", "pit crew": "pitcrew", "pit-crew": "pitcrew",
    }
    normalized = " ".join(value.strip().casefold().split())
    return aliases.get(normalized, normalized)


def optimize_ideal_team(
    entries: Iterable[PlayerEntry],
    rules: GameRuleProfile,
    *,
    round_number: int | None = None,
    round_complete: bool = True,
    timeout_seconds: float = 5.0,
) -> IdealTeamResult:
    reasons: list[str] = []
    if not rules.verified or rules.roster_size is None:
        reasons.append("Spillets sæsonregler er ikke verificeret")
    if rules.budget_enabled is None:
        reasons.append("Budgetreglen er ikke verificeret")
    if not rules.position_limits and (
        rules.category_count is None or rules.category_size is None
    ):
        reasons.append("Formation eller kategoriregler mangler")
    if not round_complete:
        reasons.append("Runden er ikke afsluttet")
    if rules.budget_enabled and rules.salary_cap is None:
        reasons.append("Budgetgrænsen mangler")
    source = tuple(entries)
    missing_growth = sum(entry.round_growth is None for entry in source)
    if reasons:
        return IdealTeamResult(
            "unverified", (), None, None, None, missing_growth, 0,
            AnalysisProvenance("unverified", () if round_number is None else (round_number,), 0, tuple(reasons)),
        )
    assert rules.roster_size is not None
    limits = {position: (minimum, maximum) for position, minimum, maximum in rules.position_limits}
    candidates = [entry for entry in source if entry.round_growth is not None]
    if limits:
        candidates = [entry for entry in candidates if _position(entry.position) in limits]
    candidates.sort(
        key=lambda entry: (
            -int(entry.round_growth or 0), entry.value,
            _player_id(entry), entry.name.casefold(),
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
    best_keys: tuple[int, ...] | None = None
    explored = 0
    timed_out = timeout_seconds <= 0

    def valid_final(selected: list[PlayerEntry], counts: Counter[str]) -> bool:
        if len(selected) != rules.roster_size:
            return False
        if any(not (minimum <= counts[position] <= maximum) for position, (minimum, maximum) in limits.items()):
            return False
        if rules.category_count is not None and rules.category_size is not None:
            categories = Counter(_position(entry.position) for entry in selected)
            if len(categories) != rules.category_count or any(value != rules.category_size for value in categories.values()):
                return False
        return True

    def better(objective: int, cost: int, keys: tuple[int, ...]) -> bool:
        return best_objective is None or (
            objective > best_objective
            or objective == best_objective and (
                best_cost is None or cost < best_cost
                or cost == best_cost and (best_keys is None or keys < best_keys)
            )
        )

    def search(
        index: int,
        selected: list[PlayerEntry],
        objective: int,
        cost: int,
        positions: Counter[str],
        teams: Counter[str],
    ) -> None:
        nonlocal best, best_objective, best_cost, best_keys, explored, timed_out
        explored += 1
        if explored % 1024 == 0 and perf_counter() >= deadline:
            timed_out = True
            return
        need = rules.roster_size - len(selected)
        remaining = len(candidates) - index
        if need < 0 or remaining < need or timed_out:
            return
        if rules.budget_enabled and rules.salary_cap is not None and cost > rules.salary_cap:
            return
        if need:
            top_bound = objective + sum(int(item.round_growth or 0) for item in candidates[index:index + need])
            if best_objective is not None and top_bound < best_objective:
                return
            if rules.budget_enabled and rules.salary_cap is not None:
                cheapest = sorted(item.value for item in candidates[index:])[:need]
                if len(cheapest) < need or cost + sum(cheapest) > rules.salary_cap:
                    return
        if limits:
            remaining_positions = Counter(_position(item.position) for item in candidates[index:])
            for position, (minimum, maximum) in limits.items():
                if positions[position] > maximum or positions[position] + remaining_positions[position] < minimum:
                    return
        if need == 0:
            if valid_final(selected, positions):
                keys = tuple(sorted(_player_id(item) for item in selected))
                if better(objective, cost, keys):
                    best = tuple(selected)
                    best_objective = objective
                    best_cost = cost
                    best_keys = keys
            return
        if index >= len(candidates):
            return
        entry = candidates[index]
        position = _position(entry.position)
        team = entry.team.strip().casefold()
        can_include = not limits or positions[position] < limits[position][1]
        if rules.category_size is not None and positions[position] >= rules.category_size:
            can_include = False
        if rules.max_from_team is not None and team and teams[team] >= rules.max_from_team:
            can_include = False
        if can_include:
            selected.append(entry)
            positions[position] += 1
            teams[team] += 1
            search(
                index + 1,
                selected,
                objective + int(entry.round_growth or 0),
                cost + entry.value,
                positions,
                teams,
            )
            teams[team] -= 1
            positions[position] -= 1
            selected.pop()
        search(index + 1, selected, objective, cost, positions, teams)

    if not timed_out:
        search(0, [], 0, 0, Counter(), Counter())
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


def simulate_transfer_scenario(
    round_vectors: Iterable[tuple[int, Mapping[str, int | None]]],
    baseline_player_keys: Iterable[str],
    scenario_player_keys: Iterable[str],
    *,
    positions: Mapping[str, str] | None = None,
    simulations: int = 10_000,
    horizon: int = 3,
    seed: int | None = None,
) -> SimulationResult:
    vectors = tuple(sorted(round_vectors, key=lambda item: item[0]))
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
    if len(observed) >= 8:
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
        "final" if len(prepared) >= 5 else "preliminary" if len(prepared) >= 3 else "unverified",
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
    if len(prepared) < 5:
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
