"""Pure player scouting metrics, peers, smart lists and evidence-first diffs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import isfinite
from statistics import median, mean, stdev
from typing import Iterable, Literal, Mapping

from .hub_settings import player_identity
from .models import GameUrl, PlayerEntry
from .sport_adapters import get_sport_adapter
from .storage import PlayerStatisticsIndex, PlayerStatisticsSnapshot


ScoutingMetricName = Literal[
    "value",
    "total_growth",
    "form_3",
    "form_5",
    "stability",
    "popularity",
    "growth_per_million",
]


@dataclass(frozen=True, slots=True)
class PercentileMetric:
    value: float | None
    percentile: float | None
    cohort_size: int
    missing_reason: str | None = None


@dataclass(frozen=True, slots=True)
class ScoreComponent:
    name: str
    value: float | None
    weight: float
    contribution: float | None
    missing_reason: str | None = None


@dataclass(frozen=True, slots=True)
class CompositeScore:
    value: float | None
    components: tuple[ScoreComponent, ...]
    missing_reason: str | None = None


@dataclass(frozen=True, slots=True)
class OwnershipSignal:
    label: Literal["differential", "template"] | None
    popularity_percent: float | None
    ownership_risk: float | None
    missing_reason: str | None = None


@dataclass(frozen=True, slots=True)
class PlayerScoutingMetrics:
    player_key: str
    name: str
    team: str
    position: str
    normalized_position: str
    is_active: bool
    is_disabled: bool
    value: int
    total_growth: int | None
    form_3: float | None
    form_5: float | None
    stability: float | None
    completed_observations: int
    metrics: tuple[tuple[str, PercentileMetric], ...]
    potential: CompositeScore
    risk: CompositeScore
    ownership: OwnershipSignal

    def metric(self, name: str) -> PercentileMetric:
        return dict(self.metrics).get(
            name, PercentileMetric(None, None, 0, "Metrikken findes ikke")
        )


@dataclass(frozen=True, slots=True)
class PriceAlternative:
    player_key: str
    name: str
    team: str
    value: int
    price_delta: int
    form_3: float | None


@dataclass(frozen=True, slots=True)
class PeerComparison:
    player_key: str
    normalized_position: str
    medians: tuple[tuple[str, float | None], ...]
    cohort_sizes: tuple[tuple[str, int], ...]
    alternatives: tuple[PriceAlternative, ...]


@dataclass(frozen=True, slots=True)
class SimilarPlayerResult:
    player_key: str
    name: str
    team: str
    distance: float
    shared_metrics: int
    price_percentile_delta: float | None
    form_3_percentile_delta: float | None
    stability_percentile_delta: float | None


@dataclass(frozen=True, slots=True)
class SmartList:
    list_id: Literal[
        "cheapest_active_forwards", "low_volatility", "recently_activated"
    ]
    label: str
    player_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ObservedPlayerDelta:
    field: str
    previous: float | str | None
    current: float | str | None
    delta: float | None
    evidence: Literal["observed", "causal"] = "observed"


@dataclass(frozen=True, slots=True)
class PerformanceRuleProfile:
    profile_id: str
    verified: bool
    target_field: Literal["round_growth", "total_growth"]
    weights: tuple[tuple[str, float], ...]
    complete_weights: bool = False

    def __post_init__(self) -> None:
        if not self.profile_id.strip():
            raise ValueError("En præstationsprofil kræver et id")
        fields = [field for field, _ in self.weights]
        if (
            not fields
            or len(fields) != len(set(fields))
            or any(not field.strip() for field in fields)
            or any(not isfinite(float(weight)) for _, weight in self.weights)
        ):
            raise ValueError("Præstationsprofilens vægte skal være komplette, entydige tal")


@dataclass(frozen=True, slots=True)
class PlayerChangeExplanation:
    player_key: str
    previous_generated_at: datetime
    current_generated_at: datetime
    observations: tuple[ObservedPlayerDelta, ...]
    contributions: tuple[ObservedPlayerDelta, ...]
    decomposition: Literal["causal", "simultaneous"]
    reconciliation_reason: str


def _entry_map(snapshot: PlayerStatisticsSnapshot) -> dict[str, PlayerEntry]:
    game = snapshot.statistics.game
    return {
        player_identity(game, entry): entry for entry in snapshot.statistics.entries
    }


def _normalize_position(snapshot: PlayerStatisticsSnapshot, value: str) -> str:
    return get_sport_adapter(snapshot.statistics).normalize_position(value)


def average_rank_percentiles(values: Iterable[float]) -> tuple[float, ...]:
    """Return 0-100 ascending percentiles with average rank for ties."""

    source = tuple(float(value) for value in values)
    if not source:
        return ()
    if any(not isfinite(value) for value in source):
        raise ValueError("Percentilgrundlaget skal bestå af endelige tal")
    positions: dict[float, list[int]] = {}
    for rank, value in enumerate(sorted(source), 1):
        positions.setdefault(value, []).append(rank)
    if len(source) == 1:
        ranks = {value: 50.0 for value in positions}
    else:
        ranks = {
            value: (mean(items) - 1) / (len(source) - 1) * 100
            for value, items in positions.items()
        }
    return tuple(ranks[value] for value in source)


def _percentile_map(
    values: Mapping[str, float | int | None], *, minimum: int = 5
) -> dict[str, PercentileMetric]:
    numeric = [(key, float(value)) for key, value in values.items() if value is not None]
    size = len(numeric)
    if size < minimum:
        return {
            key: PercentileMetric(
                None if value is None else float(value),
                None,
                size,
                "Kræver mindst fem numeriske spillere i samme position",
            )
            for key, value in values.items()
        }
    percentiles = average_rank_percentiles(value for _, value in numeric)
    ranked = {
        key: PercentileMetric(value, percentile, size)
        for (key, value), percentile in zip(numeric, percentiles, strict=True)
    }
    for key, value in values.items():
        if value is None:
            ranked[key] = PercentileMetric(
                None, None, size, "Spilleren mangler en numerisk værdi"
            )
    return ranked


@dataclass(frozen=True, slots=True)
class _RawMetrics:
    entry: PlayerEntry
    position: str
    form_3: float | None
    form_5: float | None
    stability: float | None
    completed_observations: int
    growth_per_million: float | None


def _raw_metrics(
    index: PlayerStatisticsIndex, latest: PlayerStatisticsSnapshot
) -> dict[str, _RawMetrics]:
    game = latest.statistics.game
    latest_entries = _entry_map(latest)
    completed: dict[str, dict[int, int]] = {key: {} for key in latest_entries}
    for snapshot in sorted(index.for_game(game), key=lambda item: item.generated_at):
        if snapshot.statistics.round_status != "complete":
            continue
        for key, entry in _entry_map(snapshot).items():
            if key in completed and entry.round_growth is not None:
                completed[key][snapshot.statistics.round_number] = entry.round_growth
    result: dict[str, _RawMetrics] = {}
    for key, entry in latest_entries.items():
        values = [value for _, value in sorted(completed[key].items())]
        recent = values[-5:]
        stability = None
        if len(recent) >= 3:
            dispersion = stdev(recent)
            scale = max(mean(abs(value) for value in recent), 1)
            stability = round(100 / (1 + dispersion / scale))
        result[key] = _RawMetrics(
            entry,
            _normalize_position(latest, entry.position),
            mean(values[-3:]) if len(values) >= 3 else None,
            mean(values[-5:]) if len(values) >= 5 else None,
            stability,
            len(values),
            (
                entry.total_growth / (entry.value / 1_000_000)
                if latest.statistics.unit == "money"
                and entry.value > 0
                and entry.total_growth is not None
                else None
            ),
        )
    return result


def _popularity_percent(popularity: float | None) -> float | None:
    percentage = popularity
    if percentage is not None and 0 <= percentage <= 1:
        percentage *= 100
    return percentage if percentage is not None and 0 <= percentage <= 100 else None


def _ownership(
    popularity: float | None,
    potential: float | None,
    player_key: str,
    own_team_player_keys: frozenset[str] | None,
) -> OwnershipSignal:
    percentage = _popularity_percent(popularity)
    label = (
        "differential"
        if percentage is not None and percentage <= 10
        else "template"
        if percentage is not None and percentage >= 25
        else None
    )
    if percentage is None:
        return OwnershipSignal(label, None, None, "Popularitet mangler")
    if potential is None:
        return OwnershipSignal(label, percentage, None, "Potentiale mangler")
    if own_team_player_keys is None:
        return OwnershipSignal(label, percentage, None, "Eget hold er ikke valgt")
    if player_key in own_team_player_keys:
        return OwnershipSignal(
            label, percentage, None, "Spilleren er allerede på eget hold"
        )
    return OwnershipSignal(label, percentage, percentage * potential / 100)


def build_scouting_metrics(
    index: PlayerStatisticsIndex,
    game: GameUrl,
    *,
    own_team_player_keys: frozenset[str] | None = None,
) -> tuple[PlayerScoutingMetrics, ...]:
    """Build all position-relative metrics from the newest cached snapshot."""

    latest = index.newest(game)
    if latest is None:
        return ()
    raw = _raw_metrics(index, latest)
    eligible = {
        key: item
        for key, item in raw.items()
        if item.entry.is_active and not item.entry.is_disabled
    }
    percentile_fields: dict[str, dict[str, PercentileMetric]] = {
        name: {}
        for name in (
            "value",
            "total_growth",
            "form_3",
            "form_5",
            "stability",
            "popularity",
            "growth_per_million",
        )
    }
    positions = sorted({item.position for item in eligible.values()})
    for position in positions:
        cohort = {
            key: item for key, item in eligible.items() if item.position == position
        }
        values_by_field = {
            "value": {key: item.entry.value for key, item in cohort.items()},
            "total_growth": {
                key: item.entry.total_growth for key, item in cohort.items()
            },
            "form_3": {key: item.form_3 for key, item in cohort.items()},
            "form_5": {key: item.form_5 for key, item in cohort.items()},
            "stability": {key: item.stability for key, item in cohort.items()},
            "popularity": {
                key: _popularity_percent(item.entry.popularity)
                for key, item in cohort.items()
            },
            "growth_per_million": {
                key: (
                    item.growth_per_million
                    if latest.statistics.unit == "money"
                    else item.entry.total_growth
                )
                for key, item in cohort.items()
            },
        }
        for name, values in values_by_field.items():
            percentile_fields[name].update(_percentile_map(values))
    result: list[PlayerScoutingMetrics] = []
    for key, item in raw.items():
        metrics = {
            name: values.get(
                key,
                PercentileMetric(
                    getattr(item.entry, name, None),
                    None,
                    0,
                    "Inaktive eller deaktiverede spillere er uden for kohorten",
                ),
            )
            for name, values in percentile_fields.items()
        }
        form3 = metrics["form_3"].percentile
        potential_inputs = (
            ("Form 3", form3, 0.50),
            ("Form 5", metrics["form_5"].percentile, 0.20),
            (
                "Værdieffektivitet",
                metrics["growth_per_million"].percentile,
                0.30,
            ),
        )
        available = [part for part in potential_inputs if part[1] is not None]
        potential_value = None
        potential_reason = None
        potential_components: list[ScoreComponent] = []
        if form3 is None or len(available) < 2:
            potential_reason = "Kræver Form 3 og mindst én yderligere komponent"
            for name, value, weight in potential_inputs:
                potential_components.append(
                    ScoreComponent(
                        name,
                        value,
                        weight,
                        None,
                        None if value is not None else "Percentilgrundlag mangler",
                    )
                )
        else:
            total_weight = sum(weight for _, value, weight in available if value is not None)
            for name, value, weight in potential_inputs:
                contribution = (
                    value * weight / total_weight if value is not None else None
                )
                potential_components.append(
                    ScoreComponent(
                        name,
                        value,
                        weight / total_weight if value is not None else 0,
                        contribution,
                        None if value is not None else "Percentilgrundlag mangler",
                    )
                )
            potential_value = sum(
                component.contribution or 0 for component in potential_components
            )
        stability_percentile = metrics["stability"].percentile
        inverse_stability = (
            100 - stability_percentile
            if stability_percentile is not None
            else 100.0
        )
        status_risks = [0]
        if not item.entry.is_active:
            status_risks.append(60)
        if item.entry.is_injured or item.entry.has_suspension:
            status_risks.append(80)
        if item.entry.is_disabled:
            status_risks.append(100)
        status_risk = float(max(status_risks))
        if (
            latest.statistics.round_status == "unknown"
            or item.completed_observations < 3
        ):
            data_risk = 100.0
            data_reason = "Utilstrækkeligt eller uverificeret grundlag"
        elif latest.statistics.round_status != "complete":
            data_risk = 70.0
            data_reason = "Nyeste data er foreløbige"
        elif item.completed_observations >= 5:
            data_risk = 0.0
            data_reason = None
        else:
            data_risk = 40.0
            data_reason = "Kun 3-4 afsluttede observationer"
        risk_components = (
            ScoreComponent(
                "Omvendt stabilitet",
                inverse_stability,
                0.70,
                inverse_stability * 0.70,
                None
                if stability_percentile is not None
                else "Stabilitetspercentil mangler; fail-closed til 100",
            ),
            ScoreComponent("Statusrisiko", status_risk, 0.20, status_risk * 0.20),
            ScoreComponent("Datarisiko", data_risk, 0.10, data_risk * 0.10, data_reason),
        )
        risk_value = sum(component.contribution or 0 for component in risk_components)
        potential = CompositeScore(potential_value, tuple(potential_components), potential_reason)
        result.append(
            PlayerScoutingMetrics(
                key,
                item.entry.name,
                item.entry.team,
                item.entry.position,
                item.position,
                item.entry.is_active,
                item.entry.is_disabled,
                item.entry.value,
                item.entry.total_growth,
                item.form_3,
                item.form_5,
                item.stability,
                item.completed_observations,
                tuple(metrics.items()),
                potential,
                CompositeScore(risk_value, risk_components),
                _ownership(
                    item.entry.popularity,
                    potential_value,
                    key,
                    own_team_player_keys,
                ),
            )
        )
    return tuple(sorted(result, key=lambda item: (item.name.casefold(), item.player_key)))


def build_peer_comparison(
    metrics: Iterable[PlayerScoutingMetrics], player_key: str
) -> PeerComparison | None:
    source = tuple(metrics)
    target = next((item for item in source if item.player_key == player_key), None)
    if target is None:
        return None
    peers = tuple(
        item
        for item in source
        if item.normalized_position == target.normalized_position
        and item.is_active
        and not item.is_disabled
    )
    fields = {
        "value": [float(item.value) for item in peers],
        "total_growth": [
            float(item.total_growth) for item in peers if item.total_growth is not None
        ],
        "form_3": [float(item.form_3) for item in peers if item.form_3 is not None],
        "form_5": [float(item.form_5) for item in peers if item.form_5 is not None],
        "stability": [
            float(item.stability) for item in peers if item.stability is not None
        ],
        "popularity": [
            float(value)
            for item in peers
            if (value := item.metric("popularity").value) is not None
        ],
    }
    medians = tuple(
        (name, median(values) if len(values) >= 3 else None)
        for name, values in fields.items()
    )
    alternatives = sorted(
        (item for item in peers if item.player_key != player_key),
        key=lambda item: (
            abs(item.value - target.value),
            item.value,
            -(item.form_3 if item.form_3 is not None else float("-inf")),
            item.name.casefold(),
            item.player_key,
        ),
    )[:5]
    return PeerComparison(
        player_key,
        target.normalized_position,
        medians,
        tuple((name, len(values)) for name, values in fields.items()),
        tuple(
            PriceAlternative(
                item.player_key,
                item.name,
                item.team,
                item.value,
                item.value - target.value,
                item.form_3,
            )
            for item in alternatives
        ),
    )


def find_similar_players(
    metrics: Iterable[PlayerScoutingMetrics], player_key: str
) -> tuple[SimilarPlayerResult, ...]:
    source = tuple(metrics)
    target = next((item for item in source if item.player_key == player_key), None)
    if target is None:
        return ()
    specifications = (
        ("value", 0.40),
        ("form_3", 0.35),
        ("stability", 0.25),
    )
    result: list[SimilarPlayerResult] = []
    for candidate in source:
        if (
            candidate.player_key == player_key
            or candidate.normalized_position != target.normalized_position
        ):
            continue
        deltas: list[float | None] = []
        shared = 0
        distance = 0.0
        for name, weight in specifications:
            left = target.metric(name).percentile
            right = candidate.metric(name).percentile
            delta = abs(left - right) if left is not None and right is not None else None
            deltas.append(delta)
            if delta is None:
                distance += 100 * weight
            else:
                shared += 1
                distance += delta * weight
        if shared < 2:
            continue
        result.append(
            SimilarPlayerResult(
                candidate.player_key,
                candidate.name,
                candidate.team,
                distance,
                shared,
                deltas[0],
                deltas[1],
                deltas[2],
            )
        )
    return tuple(
        sorted(
            result,
            key=lambda item: (item.distance, item.name.casefold(), item.player_key),
        )[:5]
    )


def build_smart_lists(
    index: PlayerStatisticsIndex,
    game: GameUrl,
    *,
    now: datetime | None = None,
) -> tuple[SmartList, ...]:
    latest = index.newest(game)
    if latest is None:
        return (
            SmartList("cheapest_active_forwards", "Billigste aktive angribere", ()),
            SmartList("low_volatility", "Lav volatilitet", ()),
            SmartList("recently_activated", "Nyligt aktiverede", ()),
        )
    raw = _raw_metrics(index, latest)
    forwards = sorted(
        (
            (key, item)
            for key, item in raw.items()
            if item.position == "forward"
            and item.entry.is_active
            and not item.entry.is_disabled
        ),
        key=lambda pair: (pair[1].entry.value, pair[0]),
    )
    stable = sorted(
        (
            (key, item)
            for key, item in raw.items()
            if item.completed_observations >= 3
            and item.stability is not None
            and item.stability >= 70
        ),
        key=lambda pair: (-float(pair[1].stability or 0), pair[1].entry.value, pair[0]),
    )
    selected_now = now or datetime.now(timezone.utc)
    if selected_now.tzinfo is None:
        selected_now = selected_now.astimezone()
    activation_cutoff = selected_now - timedelta(days=7)
    activated: dict[str, datetime] = {}
    chronological = sorted(index.for_game(game), key=lambda item: item.generated_at)
    for previous, current in zip(chronological, chronological[1:], strict=False):
        before, after = _entry_map(previous), _entry_map(current)
        for key in before.keys() & after.keys():
            old, new = before[key], after[key]
            old_inactive = not old.is_active or old.is_disabled
            new_active = new.is_active and not new.is_disabled
            if old_inactive and new_active and current.generated_at >= activation_cutoff:
                activated[key] = current.generated_at
    recently = tuple(
        key
        for key, _ in sorted(
            activated.items(), key=lambda pair: (-pair[1].timestamp(), pair[0])
        )
    )
    return (
        SmartList(
            "cheapest_active_forwards",
            "Billigste aktive angribere",
            tuple(key for key, _ in forwards),
        ),
        SmartList("low_volatility", "Lav volatilitet", tuple(key for key, _ in stable)),
        SmartList("recently_activated", "Nyligt aktiverede", recently),
    )


def _status(entry: PlayerEntry) -> str:
    values: list[str] = []
    if not entry.is_active:
        values.append("inactive")
    if entry.is_disabled:
        values.append("disabled")
    if entry.is_injured:
        values.append("injured")
    if entry.has_suspension:
        values.append("suspended")
    return ",".join(values) if values else "active"


def _observed(
    field: str,
    previous: float | str | None,
    current: float | str | None,
) -> ObservedPlayerDelta:
    delta = (
        float(current) - float(previous)
        if isinstance(previous, (int, float))
        and not isinstance(previous, bool)
        and isinstance(current, (int, float))
        and not isinstance(current, bool)
        else None
    )
    return ObservedPlayerDelta(field, previous, current, delta)


def build_player_change_explanation(
    previous: PlayerStatisticsSnapshot,
    current: PlayerStatisticsSnapshot,
    player_key: str,
    *,
    rule_profile: PerformanceRuleProfile | None = None,
    tolerance: float = 1e-6,
) -> PlayerChangeExplanation | None:
    """Explain observed changes, only upgrading to causal after reconciliation."""

    before = _entry_map(previous).get(player_key)
    after = _entry_map(current).get(player_key)
    if before is None or after is None:
        return None
    observations = [
        _observed("value", before.value, after.value),
        _observed("total_growth", before.total_growth, after.total_growth),
        _observed("round_growth", before.round_growth, after.round_growth),
        _observed("status", _status(before), _status(after)),
        _observed("popularity", before.popularity, after.popularity),
        _observed(
            "popularity_change", before.popularity_change, after.popularity_change
        ),
        _observed("trend", before.trend, after.trend),
        _observed("index", before.index, after.index),
    ]
    for prefix, old_values, new_values in (
        ("stats", before.stat_values(), after.stat_values()),
        ("total_stats", before.stat_values(total=True), after.stat_values(total=True)),
    ):
        for name in sorted(old_values.keys() | new_values.keys(), key=str.casefold):
            observations.append(
                _observed(f"{prefix}.{name}", old_values.get(name), new_values.get(name))
            )
    if (
        rule_profile is None
        or not rule_profile.verified
        or not rule_profile.complete_weights
    ):
        reason = (
            "Ingen verificeret sæsonprofil; felterne er samtidige observationer"
            if rule_profile is None or not rule_profile.verified
            else "Sæsonprofilens vægte er ikke markeret komplette"
        )
        return PlayerChangeExplanation(
            player_key,
            previous.generated_at,
            current.generated_at,
            tuple(observations),
            (),
            "simultaneous",
            reason,
        )
    target = next(
        (item for item in observations if item.field == rule_profile.target_field), None
    )
    if target is None or target.delta is None:
        reason = "Målændringen kan ikke beregnes"
    else:
        contribution_rows: list[ObservedPlayerDelta] = []
        for field, weight in rule_profile.weights:
            observed = next((item for item in observations if item.field == field), None)
            if observed is None or observed.delta is None or not isfinite(weight):
                reason = "Regelprofilen mangler et fuldt numerisk bidragsgrundlag"
                break
            contribution_rows.append(
                ObservedPlayerDelta(
                    field,
                    observed.previous,
                    observed.current,
                    observed.delta * weight,
                    "causal",
                )
            )
        else:
            total = sum(item.delta or 0 for item in contribution_rows)
            if abs(total - target.delta) <= tolerance:
                return PlayerChangeExplanation(
                    player_key,
                    previous.generated_at,
                    current.generated_at,
                    tuple(observations),
                    tuple(contribution_rows),
                    "causal",
                    "Verificerede vægte afstemmer den observerede målændring",
                )
            reason = "Bidragene kan ikke afstemmes mod den observerede målændring"
    return PlayerChangeExplanation(
        player_key,
        previous.generated_at,
        current.generated_at,
        tuple(observations),
        (),
        "simultaneous",
        reason,
    )
