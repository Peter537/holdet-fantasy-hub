"""Season-scoped, provenance-aware fantasy rule contracts.

Rules in this module are deliberately fail-closed.  A game that is not present
in the audited registry receives an unverified profile with unknown fields;
format-level defaults must never be presented as official season rules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from .models import GameUrl
from .transfers import TransferRuleProfile


RoundingMode = Literal["floor", "ceil", "nearest"]
AnalysisCertainty = Literal["final", "preliminary", "unverified"]


def _rounding(value: object) -> RoundingMode | None:
    if value is not None and (not isinstance(value, str) or value not in {"floor", "ceil", "nearest"}):
        raise ValueError("Ukendt afrundingsregel")
    return value  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class AnalysisProvenance:
    """Explain the data window and certainty behind an analysis result."""

    certainty: AnalysisCertainty
    rounds: tuple[int, ...] = ()
    sample_size: int = 0
    missing_reasons: tuple[str, ...] = ()
    source: str = "local_snapshots"


@dataclass(frozen=True, slots=True)
class GameRuleProfile:
    """Audited rules for one exact Holdet game/season.

    Optional fields remain unknown until a public source for the exact season
    has been recorded.  ``verified`` therefore describes the profile identity,
    while individual calculators still require their own fields.
    """

    game_locale: str
    game_slug: str
    label: str
    game_id: int | None = None
    verified: bool = False
    source_url: str | None = None
    accessed_on: date | None = None
    bank_interest_basis_points: int | None = None
    bank_interest_rounding: RoundingMode | None = "floor"
    transfer_fee_basis_points: int | None = None
    transfer_fee_rounding: RoundingMode | None = "ceil"
    transfer_summary_is_fee: bool = False
    salary_cap: int | None = None
    roster_size: int | None = None
    max_from_team: int | None = None
    position_limits: tuple[tuple[str, int, int], ...] = ()
    category_count: int | None = None
    category_size: int | None = None
    budget_enabled: bool | None = None
    base_contracts: int | None = None
    gold_contracts: int | None = None
    free_through_round: int | None = None
    captain_count: int | None = None
    captain_multiplier: int | None = None
    sport: str | None = None
    captain_positions: tuple[str, ...] = ()
    captain_positive_only: bool = False
    fee_free_through_round: int | None = None
    gold_unlimited: bool = False
    allow_club_excess_without_trades: bool = False
    source_sha256: str | None = None
    document_url: str | None = None
    initial_roster_fee_basis_points: int | None = None

    def __post_init__(self) -> None:
        if not self.game_locale.strip() or not self.game_slug.strip():
            raise ValueError("Regelprofilen skal have spilidentitet")
        for name in ("game_id", "bank_interest_basis_points", "transfer_fee_basis_points",
                     "salary_cap", "roster_size", "max_from_team", "category_count",
                     "category_size", "base_contracts", "gold_contracts",
                     "free_through_round", "fee_free_through_round", "captain_count",
                     "captain_multiplier", "initial_roster_fee_basis_points"):
            value = getattr(self, name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} skal være et ikke-negativt heltal")
        _rounding(self.bank_interest_rounding)
        _rounding(self.transfer_fee_rounding)
        if self.verified and (
            not self.source_url
            or not self.source_url.startswith(("https://", "http://"))
            or self.accessed_on is None
        ):
            raise ValueError(
                "En verificeret regelprofil kræver officiel kilde-URL og adgangsdato"
            )
        if self.bank_interest_basis_points is not None and self.bank_interest_basis_points < 0:
            raise ValueError("Bankrenten skal være ikke-negativ")
        if self.transfer_fee_basis_points is not None and self.transfer_fee_basis_points < 0:
            raise ValueError("Transfergebyret skal være ikke-negativt")
        if self.roster_size is not None and self.roster_size < 1:
            raise ValueError("Trupstørrelsen skal være positiv")
        if self.captain_count is not None and self.captain_count < 0:
            raise ValueError("Antallet af kaptajner skal være ikke-negativt")
        if self.captain_multiplier is not None and self.captain_multiplier < 1:
            raise ValueError("Kaptajnmultiplikatoren skal være mindst 1")
        for position, minimum, maximum in self.position_limits:
            if (not isinstance(position, str) or not position.strip()
                    or isinstance(minimum, bool) or not isinstance(minimum, int)
                    or isinstance(maximum, bool) or not isinstance(maximum, int)
                    or minimum < 0 or maximum < minimum):
                raise ValueError("Positionsgrænserne er ugyldige")
        if (self.category_count is None) != (self.category_size is None):
            raise ValueError("Kategoriantal og kategoristørrelse skal angives sammen")
        if self.category_count is not None and (
            self.category_count < 1 or self.category_size is None or self.category_size < 1
        ):
            raise ValueError("Kategorireglerne skal være positive")

    @property
    def identity(self) -> tuple[str, str]:
        return self.game_locale.casefold(), self.game_slug

    def interest(self, amount: int) -> int | None:
        if not self.verified or self.bank_interest_basis_points is None:
            return None
        return _basis_points(amount, self.bank_interest_basis_points, self.bank_interest_rounding)

    def transfer_fee(self, amount: int) -> int | None:
        if not self.verified or self.transfer_fee_basis_points is None:
            return None
        return _basis_points(amount, self.transfer_fee_basis_points, self.transfer_fee_rounding)

    def to_transfer_profile(self) -> TransferRuleProfile:
        """Project the audited contract onto the legacy transfer API."""

        known = self.verified and self.roster_size is not None
        basis_points = self.transfer_fee_basis_points if self.verified else None
        return TransferRuleProfile(
            key=f"season:{self.game_locale.casefold()}:{self.game_slug}",
            label=self.label,
            roster_size=self.roster_size or 0,
            fee_percent=(basis_points or 0) // 100,
            max_from_team=self.max_from_team,
            position_limits=self.position_limits,
            category_count=self.category_count,
            category_size=self.category_size,
            budget_enabled=bool(self.budget_enabled),
            base_contracts=self.base_contracts,
            gold_contracts=self.gold_contracts,
            free_through_round=self.free_through_round or 0,
            captain_count=self.captain_count or 0,
            known=known,
            fee_basis_points=basis_points,
            fee_rounding=self.transfer_fee_rounding,
            fee_free_through_round=self.fee_free_through_round,
            sport=self.sport,
            captain_positions=self.captain_positions,
            allow_club_excess_without_trades=self.allow_club_excess_without_trades,
        )


def _basis_points(amount: int, basis_points: int, rounding: RoundingMode | None) -> int | None:
    if amount < 0:
        raise ValueError("Beløbet skal være ikke-negativt")
    numerator = amount * basis_points
    if rounding is None:
        return numerator // 10_000 if numerator % 10_000 == 0 else None
    if rounding == "floor":
        return numerator // 10_000
    if rounding == "ceil":
        return (numerator + 9_999) // 10_000
    return (numerator + 5_000) // 10_000


# No season profiles are shipped; reviewed rules are stored in local evidence.
AUDITED_RULE_PROFILES: tuple[GameRuleProfile, ...] = ()


def rule_profile_for_game(
    game: GameUrl,
    *,
    game_id: int | None = None,
    salary_cap: int | None = None,
    label: str | None = None,
) -> GameRuleProfile:
    """Return an exact audited profile or an explicitly unverified contract."""

    identity = (game.locale.casefold(), game.slug)
    for profile in AUDITED_RULE_PROFILES:
        if profile.identity == identity and (
            profile.game_id is None or game_id is None or profile.game_id == game_id
        ):
            return profile
    return GameRuleProfile(
        game.locale.casefold(),
        game.slug,
        label or game.slug,
        game_id=game_id,
        salary_cap=salary_cap,
        budget_enabled=None if salary_cap is None else salary_cap > 0,
    )


def game_rule_to_dict(profile: GameRuleProfile) -> dict[str, object]:
    return {
        "game_locale": profile.game_locale,
        "game_slug": profile.game_slug,
        "label": profile.label,
        "game_id": profile.game_id,
        "verified": profile.verified,
        "source_url": profile.source_url,
        "accessed_on": profile.accessed_on.isoformat() if profile.accessed_on else None,
        "bank_interest_basis_points": profile.bank_interest_basis_points,
        "bank_interest_rounding": profile.bank_interest_rounding,
        "transfer_fee_basis_points": profile.transfer_fee_basis_points,
        "transfer_fee_rounding": profile.transfer_fee_rounding,
        "transfer_summary_is_fee": profile.transfer_summary_is_fee,
        "salary_cap": profile.salary_cap,
        "roster_size": profile.roster_size,
        "max_from_team": profile.max_from_team,
        "position_limits": [list(item) for item in profile.position_limits],
        "category_count": profile.category_count,
        "category_size": profile.category_size,
        "budget_enabled": profile.budget_enabled,
        "base_contracts": profile.base_contracts,
        "gold_contracts": profile.gold_contracts,
        "free_through_round": profile.free_through_round,
        "captain_count": profile.captain_count,
        "captain_multiplier": profile.captain_multiplier,
        "sport": profile.sport,
        "captain_positions": list(profile.captain_positions),
        "captain_positive_only": profile.captain_positive_only,
        "fee_free_through_round": profile.fee_free_through_round,
        "gold_unlimited": profile.gold_unlimited,
        "allow_club_excess_without_trades": profile.allow_club_excess_without_trades,
        "source_sha256": profile.source_sha256,
        "document_url": profile.document_url,
        "initial_roster_fee_basis_points": profile.initial_roster_fee_basis_points,
    }


def game_rule_from_dict(raw: object) -> GameRuleProfile:
    if not isinstance(raw, dict):
        raise ValueError("Regelprofilen skal være et objekt")
    limits_raw = raw.get("position_limits", [])
    if not isinstance(limits_raw, list):
        raise ValueError("position_limits skal være en liste")
    limits: list[tuple[str, int, int]] = []
    for item in limits_raw:
        if (
            not isinstance(item, list)
            or len(item) != 3
            or not isinstance(item[0], str)
            or not isinstance(item[1], int)
            or not isinstance(item[2], int)
        ):
            raise ValueError("Ugyldig positionsgrænse")
        limits.append((item[0], item[1], item[2]))
    accessed = raw.get("accessed_on")
    return GameRuleProfile(
        str(raw.get("game_locale", "")),
        str(raw.get("game_slug", "")),
        str(raw.get("label", "")),
        game_id=_optional_int(raw.get("game_id")),
        verified=_boolean(raw, "verified"),
        source_url=_optional_text(raw.get("source_url")),
        accessed_on=date.fromisoformat(accessed) if isinstance(accessed, str) else None,
        bank_interest_basis_points=_optional_int(raw.get("bank_interest_basis_points")),
        bank_interest_rounding=_rounding(raw.get("bank_interest_rounding", "floor")),
        transfer_fee_basis_points=_optional_int(raw.get("transfer_fee_basis_points")),
        transfer_fee_rounding=_rounding(raw.get("transfer_fee_rounding", "ceil")),
        transfer_summary_is_fee=bool(raw.get("transfer_summary_is_fee", False)),
        salary_cap=_optional_int(raw.get("salary_cap")),
        roster_size=_optional_int(raw.get("roster_size")),
        max_from_team=_optional_int(raw.get("max_from_team")),
        position_limits=tuple(limits),
        category_count=_optional_int(raw.get("category_count")),
        category_size=_optional_int(raw.get("category_size")),
        budget_enabled=(raw.get("budget_enabled") if isinstance(raw.get("budget_enabled"), bool) else None),
        base_contracts=_optional_int(raw.get("base_contracts")),
        gold_contracts=_optional_int(raw.get("gold_contracts")),
        free_through_round=_optional_int(raw.get("free_through_round")),
        captain_count=_optional_int(raw.get("captain_count")),
        captain_multiplier=_optional_int(raw.get("captain_multiplier")),
        sport=_optional_text(raw.get("sport")),
        captain_positions=_string_tuple(raw.get("captain_positions", [])),
        captain_positive_only=_boolean(raw, "captain_positive_only"),
        fee_free_through_round=_optional_int(raw.get("fee_free_through_round")),
        gold_unlimited=_boolean(raw, "gold_unlimited"),
        allow_club_excess_without_trades=_boolean(raw, "allow_club_excess_without_trades"),
        source_sha256=_optional_text(raw.get("source_sha256")),
        document_url=_optional_text(raw.get("document_url")),
        initial_roster_fee_basis_points=_optional_int(raw.get("initial_roster_fee_basis_points")),
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError("Regelfeltet skal være et heltal")
    return value


def _boolean(raw: dict, key: str) -> bool:
    value = raw.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"{key} skal være en boolsk værdi")
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ValueError("captain_positions skal være en liste af positioner")
    return tuple(value)


def _optional_text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None
