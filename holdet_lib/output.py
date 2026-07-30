"""Human-readable and structured fantasy-team output."""

from __future__ import annotations

from datetime import datetime
import re
import unicodedata

from .models import RosterEntry, RoundSummary, ScrapedTeam
from .players import format_integer


WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}


def sanitize_path_component(value: str, *, fallback: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    normalized = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "-", normalized)
    normalized = re.sub(r"[^\w.-]+", "-", normalized, flags=re.UNICODE)
    normalized = re.sub(r"-+", "-", normalized).strip(" .-")
    if not normalized or normalized in WINDOWS_RESERVED:
        return fallback
    return normalized[:100].rstrip(" .-") or fallback


def format_change(value: int | None, *, unit: str) -> str:
    if value is None:
        return "-"
    prefix = "+" if value > 0 else ""
    postfix = " p." if unit == "points" else ""
    return f"{prefix}{format_integer(value)}{postfix}"


def format_value(value: int | None, *, unit: str) -> str:
    if value is None:
        return "-"
    postfix = " p." if unit == "points" else ""
    return f"{format_integer(value)}{postfix}"


def _format_rank(value: int | None) -> str:
    return "-" if value is None else f"#{format_integer(value)}"


def _roster_status(entry: RosterEntry) -> str:
    return " ".join(f"[{status}]" for status in entry.statuses)


def format_team_text(team: ScrapedTeam) -> str:
    unit = team.overview.unit
    latest = team.history[0] if team.history else None
    lines = [
        f"Konto: {team.reference.account_label}",
        f"Ejer: {team.owner_name}",
        f"Hold: {team.team_name}",
        f"Hold-ID: {team.reference.team_id}",
        f"Spil: {team.reference.game.slug}",
        f"Variant: {team.variant}",
        f"Runde: {team.overview.current_round}",
        "",
        "OVERBLIK",
        f"Rangering: {_format_rank(team.overview.rank)}",
        f"Spring: {format_change(team.overview.rank_change, unit='money')}",
    ]
    if team.overview.top_percent is not None:
        lines.append(f"Top: {team.overview.top_percent}%")
    substitutions: list[str] = []
    if team.overview.substitutions_remaining is not None:
        substitutions.append(f"{team.overview.substitutions_remaining} tilbage")
    if team.overview.substitutions_used is not None:
        substitutions.append(f"{team.overview.substitutions_used} brugt")
    if team.overview.substitutions_limit is not None:
        substitutions.append(f"maks. {team.overview.substitutions_limit}")
    lines.append("Udskiftninger: " + (", ".join(substitutions) or "-"))
    if unit == "money":
        lines.extend(
            [
                f"Spillerværdier: {format_value(team.overview.player_value, unit=unit)}",
                f"Bank: {format_value(team.overview.bank, unit=unit)}",
                f"Total: {format_value(team.overview.total, unit=unit)}",
            ]
        )
    else:
        lines.append(f"Total: {format_value(team.overview.total, unit=unit)}")
    lines.append(
        f"Seneste vækst: {format_change(team.overview.current_change, unit=unit)}"
    )
    if latest is not None:
        lines.extend(
            [
                f"Spillervækst: {format_change(latest.player_change, unit=unit)}",
                f"Kaptajnbonus: {format_change(latest.captain_bonus, unit=unit)}",
                f"Speciel bonus: {format_change(latest.special_bonus, unit=unit)}",
            ]
        )
        if unit == "money":
            lines.extend(
                [
                    f"Bankændring: {format_change(latest.bank_change, unit=unit)}",
                    f"Bankrente: {format_change(latest.interest, unit=unit)}",
                    f"Transfer: {format_change(latest.transfer, unit=unit)}",
                ]
            )

    lines.extend(["", "SPILLERE"])
    if not team.roster:
        lines.append("Ingen spillere på holdet.")
    for index, player in enumerate(team.roster, 1):
        role = (
            "Kaptajn"
            if player.role == "captain"
            else ("Ingen" if player.role == "none" else player.role)
        )
        purchase = (
            "-" if player.purchase_round is None else str(player.purchase_round)
        )
        status = _roster_status(player)
        first = (
            f"{index}. {player.name} ({player.team}, {player.position})"
            f" — {format_value(player.value, unit=unit)}"
        )
        if status:
            first += f" {status}"
        lines.extend(
            [
                first,
                f"   Rolle: {role}; Seneste runde: "
                f"{format_change(player.round_change, unit=unit)}; "
                f"Siden køb: {format_change(player.since_purchase_change, unit=unit)}; "
                f"Købt i runde: {purchase}",
            ]
        )

    lines.extend(["", "HISTORIK"])
    if not team.history:
        lines.append("Ingen afsluttede runder.")
    for summary in team.history:
        lines.append(_format_round(summary, unit=unit))
    return "\n".join(lines) + "\n"


def _format_round(summary: RoundSummary, *, unit: str) -> str:
    fields = [
        f"Runde {summary.round_number}",
        f"Total {format_value(summary.total, unit=unit)}",
        f"Vækst {format_change(summary.change, unit=unit)}",
    ]
    if unit == "money":
        fields.extend(
            [
                f"Spillerværdi {format_value(summary.player_value, unit=unit)}",
                f"Bank {format_value(summary.bank, unit=unit)}",
                f"Rente {format_change(summary.interest, unit=unit)}",
                f"Transfer {format_change(summary.transfer, unit=unit)}",
            ]
        )
    fields.extend(
        [
            f"Spillervækst {format_change(summary.player_change, unit=unit)}",
            f"Kaptajnbonus {format_change(summary.captain_bonus, unit=unit)}",
            f"Speciel bonus {format_change(summary.special_bonus, unit=unit)}",
            f"Runderangering {_format_rank(summary.round_rank)}",
            f"Overall {_format_rank(summary.overall_rank)}",
        ]
    )
    return " | ".join(fields)


def team_to_dict(team: ScrapedTeam, *, generated_at: datetime) -> dict[str, object]:
    latest = team.history[0] if team.history else None
    return {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "source": {
            "game_url": team.reference.game.original,
            "team_url": team.reference.source_url,
            "profile_url": team.reference.profile_url,
        },
        "game": {
            "locale": team.reference.game.locale,
            "slug": team.reference.game.slug,
            "variant": team.variant,
            "game_id": team.game_id,
            "current_round": team.overview.current_round,
            "unit": team.overview.unit,
        },
        "account": {
            "key": team.reference.account_key,
            "label": team.reference.account_label,
            "configured_user_id": team.reference.account_user_id,
            "owner_name": team.owner_name,
            "owner_user_id": team.owner_user_id,
        },
        "team": {"id": team.reference.team_id, "name": team.team_name},
        "overview": {
            "rank": team.overview.rank,
            "rank_change": team.overview.rank_change,
            "top_percent": team.overview.top_percent,
            "substitutions": {
                "remaining": team.overview.substitutions_remaining,
                "used": team.overview.substitutions_used,
                "limit": team.overview.substitutions_limit,
            },
            "player_value": team.overview.player_value,
            "bank": team.overview.bank,
            "total": team.overview.total,
            "current_change": team.overview.current_change,
            "current_growth": _round_growth_dict(latest) if latest else None,
        },
        "roster": [
            {
                "source_index": player.source_index,
                "player_id": player.player_id,
                "name": player.name,
                "team": player.team,
                "position": player.position,
                "value": player.value,
                "round_change": player.round_change,
                "since_purchase_change": player.since_purchase_change,
                "purchase_round": player.purchase_round,
                "role": player.role,
                "statuses": list(player.statuses),
            }
            for player in team.roster
        ],
        "history": [_round_dict(summary) for summary in team.history],
    }


def _round_growth_dict(summary: RoundSummary) -> dict[str, int | None]:
    return {
        "change": summary.change,
        "bank_change": summary.bank_change,
        "interest": summary.interest,
        "player_change": summary.player_change,
        "transfer": summary.transfer,
        "captain_bonus": summary.captain_bonus,
        "special_bonus": summary.special_bonus,
    }


def _round_dict(summary: RoundSummary) -> dict[str, int | None]:
    return {
        "round": summary.round_number,
        "total": summary.total,
        "change": summary.change,
        "bank": summary.bank,
        "player_value": summary.player_value,
        "bank_change": summary.bank_change,
        "interest": summary.interest,
        "player_change": summary.player_change,
        "transfer": summary.transfer,
        "captain_bonus": summary.captain_bonus,
        "special_bonus": summary.special_bonus,
        "substitutions_used": summary.substitutions_used,
        "round_rank": summary.round_rank,
        "overall_rank": summary.overall_rank,
        "round_rank_change": summary.round_rank_change,
        "overall_rank_change": summary.overall_rank_change,
    }
