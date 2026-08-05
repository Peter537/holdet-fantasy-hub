"""Pure team-export documents, serializers, and explicit immutable storage."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Sequence

from .errors import PayloadError
from .filenames import collision_suffix, round_output_stem
from .models import RosterEntry, RoundSummary, ScrapedTeam
from .output import format_change, format_value, sanitize_path_component, team_to_dict
from .players import format_integer
from .persistence import aware_local, publish_immutable


TEAM_EXPORT_SCHEMA_VERSION = 1
TEAM_EXPORT_FORMATS = ("txt", "json", "md")
TEAM_EXPORT_SCOPES = ("full", "round")

STATUS_LABELS_DA = {
    "inactive": "Inaktiv",
    "disabled": "Deaktiveret",
    "injured": "Skadet",
    "suspended": "Karantæne",
}


@dataclass(frozen=True, slots=True)
class TeamExportDocument:
    team: ScrapedTeam
    scope: str
    round_number: int
    summary: RoundSummary | None
    roster: tuple[RosterEntry, ...] | None
    generated_at: datetime
    source_generated_at: datetime | None = None
    roster_generated_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.scope not in TEAM_EXPORT_SCOPES:
            raise ValueError(f"Ikke-understøttet omfang for holdeksport: {self.scope}")
        if self.round_number < 0:
            raise ValueError("Runden for holdeksport må ikke være negativ")
        if self.scope == "round" and self.summary is None:
            raise ValueError("Rundeafgrænset holdeksport kræver et rundesammendrag")


@dataclass(frozen=True, slots=True)
class TeamExportArtifact:
    format: str
    path: Path
    content: bytes
    mime_type: str


def build_team_export(
    team: ScrapedTeam,
    *,
    scope: str = "full",
    round_number: int | None = None,
    roster: tuple[RosterEntry, ...] | None = None,
    generated_at: datetime | None = None,
    source_generated_at: datetime | None = None,
    roster_generated_at: datetime | None = None,
) -> TeamExportDocument:
    """Build a complete or selected-round export without touching the filesystem."""

    if scope not in TEAM_EXPORT_SCOPES:
        raise ValueError(f"Ikke-understøttet omfang for holdeksport: {scope}")
    selected_round = (
        team.overview.current_round if round_number is None else int(round_number)
    )
    if selected_round < 0:
        raise ValueError("Runden for holdeksport må ikke være negativ")
    summary = next(
        (item for item in team.history if item.round_number == selected_round),
        None,
    )
    if scope == "round" and summary is None:
        raise PayloadError(f"Holdhistorikken har ingen runde {selected_round}")
    selected_roster = tuple(team.roster) if scope == "full" else roster
    if scope == "round" and roster is None and team.overview.current_round == selected_round:
        selected_roster = tuple(team.roster)
        roster_generated_at = roster_generated_at or source_generated_at
    return TeamExportDocument(
        team=team,
        scope=scope,
        round_number=selected_round,
        summary=summary,
        roster=selected_roster,
        generated_at=aware_local(generated_at),
        source_generated_at=source_generated_at,
        roster_generated_at=roster_generated_at,
    )


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


def _roster_dict(entry: RosterEntry) -> dict[str, object]:
    return {
        "source_index": entry.source_index,
        "player_id": entry.player_id,
        "name": entry.name,
        "team": entry.team,
        "position": entry.position,
        "value": entry.value,
        "round_change": entry.round_change,
        "since_purchase_change": entry.since_purchase_change,
        "purchase_round": entry.purchase_round,
        "role": entry.role,
        "statuses": list(entry.statuses),
    }


def team_export_to_dict(document: TeamExportDocument) -> dict[str, object]:
    snapshot = team_to_dict(document.team, generated_at=document.generated_at)
    history = (
        document.team.history
        if document.scope == "full"
        else (() if document.summary is None else (document.summary,))
    )
    return {
        "schema_version": TEAM_EXPORT_SCHEMA_VERSION,
        "document_type": "team_export",
        "scope": document.scope,
        "generated_at": document.generated_at.isoformat(),
        "source_generated_at": (
            document.source_generated_at.isoformat()
            if document.source_generated_at is not None
            else None
        ),
        "roster_generated_at": (
            document.roster_generated_at.isoformat()
            if document.roster_generated_at is not None
            else None
        ),
        "source": snapshot["source"],
        "game": snapshot["game"],
        "account": snapshot["account"],
        "team": snapshot["team"],
        "selected_round": document.round_number,
        "overview": snapshot["overview"] if document.scope == "full" else None,
        "round_summary": (
            _round_dict(document.summary) if document.summary is not None else None
        ),
        "roster_available": document.roster is not None,
        "roster": (
            [_roster_dict(entry) for entry in document.roster]
            if document.roster is not None
            else None
        ),
        "history": [_round_dict(summary) for summary in history],
    }


def _timestamp(value: datetime | None) -> str:
    if value is None:
        return "–"
    return value.astimezone().strftime("%d.%m.%Y %H:%M:%S")


def _status(entry: RosterEntry) -> str:
    return " · ".join(STATUS_LABELS_DA[value] for value in entry.statuses) or "Aktiv"


def _role(entry: RosterEntry) -> str:
    if entry.role == "captain":
        return "Kaptajn"
    if entry.role == "none":
        return "Ingen"
    return entry.role


def _metadata(document: TeamExportDocument) -> list[tuple[str, str]]:
    team = document.team
    return [
        ("Hold", team.team_name),
        ("Hold-ID", str(team.reference.team_id)),
        ("Manager", team.owner_name),
        ("Konto", team.reference.account_label),
        ("Spil", team.reference.game.slug),
        ("Sprog", team.reference.game.locale),
        ("Variant", team.variant),
        ("Enhed", team.overview.unit),
        ("Omfang", "Komplet snapshot" if document.scope == "full" else "Valgt runde"),
        ("Runde", str(document.round_number)),
        ("Kilde", team.reference.source_url),
        ("Snapshot gemt", _timestamp(document.source_generated_at)),
        ("Opstilling gemt", _timestamp(document.roster_generated_at)),
        ("Eksport oprettet", _timestamp(document.generated_at)),
    ]


def _value(value: int | None, unit: str) -> str:
    return format_value(value, unit=unit) if value is not None else "–"


def _change(value: int | None, unit: str) -> str:
    return format_change(value, unit=unit) if value is not None else "–"


def _rank(value: int | None) -> str:
    return "–" if value is None else f"#{format_integer(value)}"


def _summary_fields(summary: RoundSummary, unit: str) -> list[tuple[str, str]]:
    fields = [
        ("Runde", str(summary.round_number)),
        ("Total", _value(summary.total, unit)),
        ("Vækst", _change(summary.change, unit)),
        ("Spillervækst", _change(summary.player_change, unit)),
        ("Kaptajnbonus", _change(summary.captain_bonus, unit)),
        ("Speciel bonus", _change(summary.special_bonus, unit)),
        ("Udskiftninger", "–" if summary.substitutions_used is None else str(summary.substitutions_used)),
        ("Runderangering", _rank(summary.round_rank)),
        ("Samlet placering", _rank(summary.overall_rank)),
        ("Rangeringsændring, runde", _change(summary.round_rank_change, "money")),
        ("Ændring i samlet placering", _change(summary.overall_rank_change, "money")),
    ]
    if unit == "money":
        fields[3:3] = [
            ("Spillerværdi", _value(summary.player_value, unit)),
            ("Bank", _value(summary.bank, unit)),
            ("Bankændring", _change(summary.bank_change, unit)),
            ("Rente", _change(summary.interest, unit)),
            ("Transfer", _change(summary.transfer, unit)),
        ]
    return fields


def _roster_rows(document: TeamExportDocument) -> list[list[str]]:
    unit = document.team.overview.unit
    if document.roster is None:
        return []
    return [
        [
            str(index),
            entry.name,
            entry.team,
            entry.position,
            _value(entry.value, unit),
            _change(entry.round_change, unit),
            _change(entry.since_purchase_change, unit),
            "–" if entry.purchase_round is None else str(entry.purchase_round),
            _role(entry),
            _status(entry),
        ]
        for index, entry in enumerate(document.roster, 1)
    ]


ROSTER_HEADERS = (
    "#", "Navn", "Hold/land", "Position/kategori", "Værdi/point",
    "Rundevækst", "Siden køb", "Købsrunde", "Rolle", "Status",
)


def team_export_to_txt(document: TeamExportDocument) -> str:
    lines = [f"{label}: {value}" for label, value in _metadata(document)]
    if document.scope == "full":
        overview = document.team.overview
        lines.extend(("", "OVERBLIK"))
        overview_fields = [
            ("Rangering", _rank(overview.rank)),
            ("Rangeringsændring", _change(overview.rank_change, "money")),
            ("Top", "–" if overview.top_percent is None else f"{overview.top_percent}%"),
            ("Total", _value(overview.total, overview.unit)),
            ("Seneste vækst", _change(overview.current_change, overview.unit)),
        ]
        if overview.unit == "money":
            overview_fields[3:3] = [
                ("Spillerværdi", _value(overview.player_value, overview.unit)),
                ("Bank", _value(overview.bank, overview.unit)),
            ]
        lines.extend(f"{label}: {value}" for label, value in overview_fields)
    if document.summary is not None:
        lines.extend(("", "VALGT RUNDE"))
        lines.extend(
            f"{label}: {value}"
            for label, value in _summary_fields(document.summary, document.team.overview.unit)
        )
    lines.extend(("", "HOLDOPSTILLING"))
    if document.roster is None:
        lines.append("Ingen opstilling blev gemt præcis i denne runde.")
    else:
        lines.append("\t".join(ROSTER_HEADERS))
        lines.extend("\t".join(row) for row in _roster_rows(document))
    lines.extend(("", "HISTORIK"))
    history = document.team.history if document.scope == "full" else (() if document.summary is None else (document.summary,))
    if not history:
        lines.append("Ingen afsluttede runder.")
    else:
        for summary in history:
            lines.append(" | ".join(f"{label}: {value}" for label, value in _summary_fields(summary, document.team.overview.unit)))
    return "\n".join(lines) + "\n"


def _md(value: str) -> str:
    return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def _markdown_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return [
        "| " + " | ".join(_md(value) for value in headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
        *("| " + " | ".join(_md(value) for value in row) + " |" for row in rows),
    ]


def team_export_to_markdown(document: TeamExportDocument) -> str:
    lines = [f"# Holdstatistik – {document.team.team_name}", ""]
    lines.extend(f"- **{label}:** {_md(value)}" for label, value in _metadata(document))
    if document.scope == "full":
        overview = document.team.overview
        overview_rows = [
            ["Rangering", _rank(overview.rank)],
            ["Rangeringsændring", _change(overview.rank_change, "money")],
            ["Top", "–" if overview.top_percent is None else f"{overview.top_percent}%"],
            ["Spillerværdi", _value(overview.player_value, overview.unit)],
            ["Bank", _value(overview.bank, overview.unit)],
            ["Total", _value(overview.total, overview.unit)],
            ["Seneste vækst", _change(overview.current_change, overview.unit)],
        ]
        if overview.unit != "money":
            overview_rows = [row for row in overview_rows if row[0] not in {"Spillerværdi", "Bank"}]
        lines.extend(("", "## Overblik", ""))
        lines.extend(_markdown_table(("Felt", "Værdi"), overview_rows))
    if document.summary is not None:
        lines.extend(("", "## Valgt runde", ""))
        lines.extend(_markdown_table(("Felt", "Værdi"), _summary_fields(document.summary, document.team.overview.unit)))
    lines.extend(("", "## Holdopstilling", ""))
    if document.roster is None:
        lines.append("Ingen opstilling blev gemt præcis i denne runde.")
    else:
        lines.extend(_markdown_table(ROSTER_HEADERS, _roster_rows(document)))
    lines.extend(("", "## Historik", ""))
    history = document.team.history if document.scope == "full" else (() if document.summary is None else (document.summary,))
    history_headers = tuple(label for label, _ in _summary_fields(history[0], document.team.overview.unit)) if history else ()
    history_rows = [[value for _, value in _summary_fields(item, document.team.overview.unit)] for item in history]
    if history:
        lines.extend(_markdown_table(history_headers, history_rows))
    else:
        lines.append("Ingen afsluttede runder.")
    return "\n".join(lines) + "\n"


def serialize_team_export(document: TeamExportDocument, format: str) -> bytes:
    selected = format.casefold()
    if selected == "txt":
        content = team_export_to_txt(document)
    elif selected == "md":
        content = team_export_to_markdown(document)
    elif selected == "json":
        content = json.dumps(team_export_to_dict(document), ensure_ascii=False, indent=2) + "\n"
    else:
        raise ValueError(f"Ikke-understøttet eksportformat for hold: {format}")
    return content.encode("utf-8")


class TeamExportStore:
    """Explicitly publish immutable team exports in one atomic format set."""

    MIME_TYPES = {
        "txt": "text/plain; charset=utf-8",
        "json": "application/json",
        "md": "text/markdown; charset=utf-8",
    }

    def __init__(self, export_dir: Path | str) -> None:
        self.export_dir = Path(export_dir)

    def save(
        self,
        document: TeamExportDocument,
        formats: Sequence[str],
    ) -> tuple[TeamExportArtifact, ...]:
        selected = tuple(dict.fromkeys(value.casefold() for value in formats))
        if not selected or any(value not in TEAM_EXPORT_FORMATS for value in selected):
            raise ValueError("Vælg et eller flere understøttede eksportformater for hold")
        team = document.team
        account = (
            team.reference.account_key
            if team.reference.account_key != "direct"
            else sanitize_path_component(team.owner_name, fallback="direct")
        )
        team_name = sanitize_path_component(
            team.team_name, fallback=f"team-{team.reference.team_id}"
        )
        target_dir = (
            self.export_dir
            / sanitize_path_component(team.reference.game.slug, fallback="game")
            / sanitize_path_component(account, fallback="account")
            / f"{team_name}-{team.reference.team_id}"
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = round_output_stem("team", document.round_number, document.generated_at)
        contents = {value: serialize_team_export(document, value) for value in selected}
        collision_number = 0
        while True:
            suffix = collision_suffix(collision_number)
            paths = {value: target_dir / f"{stem}{suffix}.{value}" for value in selected}
            if any(path.exists() for path in paths.values()):
                collision_number += 1
                continue
            published: list[Path] = []
            try:
                for value in selected:
                    publish_immutable(paths[value], contents[value])
                    published.append(paths[value])
            except FileExistsError:
                for path in published:
                    path.unlink(missing_ok=True)
                collision_number += 1
                continue
            except Exception:
                for path in published:
                    path.unlink(missing_ok=True)
                raise
            return tuple(
                TeamExportArtifact(
                    value,
                    paths[value].resolve(),
                    contents[value],
                    self.MIME_TYPES[value],
                )
                for value in selected
            )
