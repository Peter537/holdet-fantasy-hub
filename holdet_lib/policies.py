"""Known Holdet game formats and value-unit policies."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import PayloadError, UnsupportedGameError


KNOWN_GAME_FORMATS = frozenset({"soccer", "cycling", "formula1", "golf"})
ROUTE_VARIANT_FORMATS = {
    "soccer": "soccer",
    "cycling": "cycling",
    "cycling_world_tour": "cycling",
    "formula1": "formula1",
    "golf": "golf",
}
KNOWN_ROUTE_VARIANTS = frozenset(ROUTE_VARIANT_FORMATS)
VALUE_UNITS = frozenset({"money", "points"})


@dataclass(frozen=True, slots=True)
class GamePolicy:
    """How a Nexus route maps to a presentation format and value unit."""

    route_variant: str
    format: str
    unit: str

    def __post_init__(self) -> None:
        if self.route_variant not in KNOWN_ROUTE_VARIANTS:
            raise UnsupportedGameError(
                f"Ikke-understøttet spilvariant fra Holdet.dk: {self.route_variant}"
            )
        if self.format not in KNOWN_GAME_FORMATS:
            raise UnsupportedGameError(
                f"Ikke-understøttet spilformat fra Holdet.dk: {self.format}"
            )
        expected = ROUTE_VARIANT_FORMATS[self.route_variant]
        if self.format != expected:
            raise PayloadError(
                "Nexus-rutevarianten er i konflikt med ruleset-formatet: "
                f"{self.route_variant} != {self.format}"
            )
        if self.unit not in VALUE_UNITS:
            raise PayloadError(f"Ikke-understøttet værdienhed fra Holdet: {self.unit}")


def format_for_variant(route_variant: str) -> str:
    try:
        return ROUTE_VARIANT_FORMATS[route_variant]
    except KeyError as exc:
        raise UnsupportedGameError(
            f"Ikke-understøttet spilvariant fra Holdet.dk: {route_variant}"
        ) from exc


def policy_from_ruleset(
    route_variant: str,
    *,
    ruleset_format: object,
    salary_cap: object,
) -> GamePolicy:
    """Build a strict policy from the public cartridge ruleset."""

    if not isinstance(salary_cap, int) or isinstance(salary_cap, bool):
        raise PayloadError("Spillets ruleset-felt salaryCap skal være et heltal")
    if salary_cap < 0:
        raise PayloadError("Spillets ruleset-felt salaryCap må ikke være negativt")
    if ruleset_format is None:
        normalized_format = format_for_variant(route_variant)
    elif not isinstance(ruleset_format, str) or not ruleset_format.strip():
        raise PayloadError("Spillets ruleset-felt properties.Format skal være udfyldt tekst")
    else:
        normalized_format = ruleset_format.strip().casefold()
    return GamePolicy(
        route_variant=route_variant,
        format=normalized_format,
        unit="money" if salary_cap > 0 else "points",
    )


def legacy_policy(route_variant: str) -> GamePolicy:
    """Compatibility policy for old in-memory callers without cartridge data."""

    game_format = format_for_variant(route_variant)
    return GamePolicy(
        route_variant=route_variant,
        format=game_format,
        unit="points" if game_format == "golf" else "money",
    )
