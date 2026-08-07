"""Closed registry of built-in sports presentation adapters.

Adapters hold sport-level defaults and presentation only.  Season-specific
rules remain owned by :mod:`holdet_lib.rules` and adapter capabilities are
therefore explicitly unverified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Mapping

from .errors import UnsupportedGameError


@dataclass(frozen=True, slots=True)
class SportCapabilities:
    budget: bool
    positions: bool
    categories: bool
    captain: bool
    fixtures: bool
    rules_certainty: str = "unverified"

    def __post_init__(self) -> None:
        if self.rules_certainty != "unverified":
            raise ValueError("Adapterregler må kun være uverificerede defaults")


@dataclass(frozen=True, slots=True)
class SportAdapter:
    key: str
    route_variants: tuple[str, ...]
    default_unit: str
    capabilities: SportCapabilities
    labels: Mapping[str, str]
    position_aliases: Mapping[str, str] = field(default_factory=dict)
    entry_formatter: Callable[[str, str, str, int, str], str] | None = None

    def __post_init__(self) -> None:
        if not self.key or not self.route_variants:
            raise ValueError("En sportsadapter skal have nøgle og rutevarianter")
        object.__setattr__(self, "labels", MappingProxyType(dict(self.labels)))
        object.__setattr__(
            self, "position_aliases", MappingProxyType(dict(self.position_aliases))
        )

    def normalize_position(self, value: str) -> str:
        normalized = " ".join(value.strip().casefold().split())
        return self.position_aliases.get(normalized, normalized)

    def column_labels(self, *, unit: str | None = None) -> dict[str, str]:
        selected_unit = unit or self.default_unit
        labels = dict(self.labels)
        if self.key == "cycling" and selected_unit == "points":
            labels["position"] = "Kategori"
        labels["value"] = "Point" if selected_unit == "points" else "Pris"
        labels["total_growth"] = (
            "Totalændring" if selected_unit == "points" else "Totalvækst"
        )
        labels["round_growth"] = (
            "Rundeændring" if selected_unit == "points" else "Vækst"
        )
        return labels

    def format_entry(
        self, name: str, team: str, position: str, value: int, *, unit: str
    ) -> str:
        if self.entry_formatter is not None:
            return self.entry_formatter(name, team, position, value, unit)
        suffix = " p." if unit == "points" else ""
        return f"{name} ({team}, {position}): {_format_integer(value)}{suffix}"


def _format_integer(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def _cycling_entry(name: str, team: str, position: str, value: int, unit: str) -> str:
    if unit == "money":
        return f"{name} ({team}): {_format_integer(value)}"
    return f"{name} ({team}, {position}): {_format_integer(value)} p."


_COMMON_LABELS = {
    "name": "Navn",
    "team": "Hold",
    "position": "Position",
    "value": "Pris",
    "total_growth": "Totalvækst",
    "round_growth": "Vækst",
    "status": "Status",
}

_POSITION_ALIASES = {
    "målmand": "goalkeeper",
    "m\u0061almand": "goalkeeper",
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
    "k\u006ferer": "driver",
    "driver": "driver",
    "konstruktør": "constructor",
    "konstruktoer": "constructor",
    "constructor": "constructor",
    "pit crew": "pitcrew",
    "pitcrew": "pitcrew",
    "pit-crew": "pitcrew",
}


def _adapter(
    key: str,
    variants: tuple[str, ...],
    unit: str,
    capabilities: SportCapabilities,
    *,
    labels: Mapping[str, str] | None = None,
    formatter: Callable[[str, str, str, int, str], str] | None = None,
) -> SportAdapter:
    return SportAdapter(
        key,
        variants,
        unit,
        capabilities,
        {**_COMMON_LABELS, **(labels or {})},
        _POSITION_ALIASES,
        formatter,
    )


_ADAPTERS = (
    _adapter(
        "soccer",
        ("soccer",),
        "money",
        SportCapabilities(True, True, False, True, True),
    ),
    _adapter(
        "cycling",
        ("cycling", "cycling_world_tour"),
        "money",
        SportCapabilities(True, False, True, False, True),
        formatter=_cycling_entry,
    ),
    _adapter(
        "formula1",
        ("formula1",),
        "money",
        SportCapabilities(True, True, False, False, True),
    ),
    _adapter(
        "golf",
        ("golf",),
        "points",
        SportCapabilities(False, False, True, False, True),
        labels={"team": "Land", "position": "Kategori"},
    ),
)

_BY_KEY = {adapter.key: adapter for adapter in _ADAPTERS}
_BY_VARIANT: dict[str, SportAdapter] = {}
for _registered in _ADAPTERS:
    for _variant in _registered.route_variants:
        if _variant in _BY_VARIANT:
            raise RuntimeError(f"Duplikeret indbygget sportsvariant: {_variant}")
        _BY_VARIANT[_variant] = _registered


def registered_sport_adapters() -> tuple[SportAdapter, ...]:
    return _ADAPTERS


def get_sport_adapter(value: object) -> SportAdapter:
    """Resolve by adapter key, route variant, format, policy or snapshot."""

    candidates: list[str] = []
    if isinstance(value, str):
        candidates.append(value)
    else:
        for attribute in ("route_variant", "variant", "format"):
            candidate = getattr(value, attribute, None)
            if isinstance(candidate, str):
                candidates.append(candidate)
    for candidate in candidates:
        normalized = candidate.strip().casefold()
        if normalized in _BY_VARIANT:
            return _BY_VARIANT[normalized]
        if normalized in _BY_KEY:
            return _BY_KEY[normalized]
    shown = candidates[0] if candidates else repr(value)
    raise UnsupportedGameError(f"Ikke-understøttet sportsadapter: {shown}")
