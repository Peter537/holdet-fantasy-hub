"""Pure filtering, tabular serialization and explicit player exports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Iterable, Sequence

from .errors import PayloadError
from .filenames import collision_suffix, round_output_stem
from .models import PlayerEntry, ScrapedGame
from .output import sanitize_path_component
from .players import format_integer
from .persistence import publish_immutable
from .policies import GamePolicy, legacy_policy


PLAYER_EXPORT_SCHEMA_VERSION = 2
PLAYER_COLUMNS = (
    "name",
    "team",
    "position",
    "value",
    "total_growth",
    "round_growth",
    "status",
)
PLAYER_SORT_FIELDS = (
    "value",
    "name",
    "team",
    "position",
    "total_growth",
    "round_growth",
    "source",
)
PLAYER_STATUSES = ("inactive", "disabled", "injured", "suspended")
STATUS_RULES = ("ignore", "require", "exclude")
MISSING_VALUE_MODES = ("include", "exclude", "only")
PLAYER_EXPORT_FORMATS = ("txt", "json", "md")

STATUS_LABELS_DA = {
    "inactive": "Inaktiv",
    "disabled": "Deaktiveret",
    "injured": "Skadet",
    "suspended": "Karantæne",
}


@dataclass(frozen=True, slots=True)
class PlayerStatisticsQuery:
    """A validated, serializable query over one all-player snapshot."""

    search: str = ""
    teams: tuple[str, ...] = ()
    positions: tuple[str, ...] = ()
    min_value: int | None = None
    max_value: int | None = None
    min_total_growth: int | None = None
    max_total_growth: int | None = None
    min_round_growth: int | None = None
    max_round_growth: int | None = None
    missing_total_growth: str = "include"
    missing_round_growth: str = "include"
    status_rules: tuple[tuple[str, str], ...] = ()
    columns: tuple[str, ...] = PLAYER_COLUMNS
    sort_field: str = "value"
    sort_order: str = "desc"

    def __post_init__(self) -> None:
        if "name" not in self.columns:
            raise ValueError("Navnekolonnen er påkrævet")
        if len(set(self.columns)) != len(self.columns) or any(
            value not in PLAYER_COLUMNS for value in self.columns
        ):
            raise ValueError("Ikke-understøttet eller duplikeret spillerkolonne")
        if self.sort_field not in PLAYER_SORT_FIELDS:
            raise ValueError(f"Ikke-understøttet sorteringsfelt for spillere: {self.sort_field}")
        if self.sort_order not in {"asc", "desc"}:
            raise ValueError(f"Ikke-understøttet sorteringsrækkefølge for spillere: {self.sort_order}")
        for mode in (self.missing_total_growth, self.missing_round_growth):
            if mode not in MISSING_VALUE_MODES:
                raise ValueError(f"Ikke-understøttet tilstand for manglende værdier: {mode}")
        rules = dict(self.status_rules)
        if len(rules) != len(self.status_rules):
            raise ValueError("Duplikeret spillerstatusregel")
        for status, rule in self.status_rules:
            if status not in PLAYER_STATUSES or rule not in STATUS_RULES:
                raise ValueError(f"Ikke-understøttet spillerstatusregel: {status}={rule}")
        for lower, upper, label in (
            (self.min_value, self.max_value, "value"),
            (self.min_total_growth, self.max_total_growth, "total growth"),
            (self.min_round_growth, self.max_round_growth, "round growth"),
        ):
            if lower is not None and upper is not None and lower > upper:
                raise ValueError(f"Minimum for {label} må ikke overstige maksimum")

    def status_rule(self, status: str) -> str:
        return dict(self.status_rules).get(status, "ignore")


@dataclass(frozen=True, slots=True)
class PlayerExportDocument:
    statistics: ScrapedGame
    query: PlayerStatisticsQuery
    entries: tuple[PlayerEntry, ...]
    generated_at: datetime
    source_generated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class PlayerExportArtifact:
    format: str
    path: Path
    content: bytes
    mime_type: str


def entry_statuses(entry: PlayerEntry) -> tuple[str, ...]:
    statuses: list[str] = []
    if not entry.is_active:
        statuses.append("inactive")
    if entry.is_disabled:
        statuses.append("disabled")
    if entry.is_injured:
        statuses.append("injured")
    if entry.has_suspension:
        statuses.append("suspended")
    return tuple(statuses)


def format_player_status(entry: PlayerEntry) -> str:
    labels = [STATUS_LABELS_DA[value] for value in entry_statuses(entry)]
    return " · ".join(labels) if labels else "–"


def player_column_labels(
    statistics_or_variant: ScrapedGame | str,
    *,
    unit: str | None = None,
    game_format: str | None = None,
) -> dict[str, str]:
    if isinstance(statistics_or_variant, ScrapedGame):
        resolved_unit = statistics_or_variant.unit
        resolved_format = statistics_or_variant.format
    else:
        policy = (
            legacy_policy(statistics_or_variant)
            if unit is None and game_format is None
            else GamePolicy(statistics_or_variant, game_format or "", unit or "")
        )
        resolved_unit = policy.unit
        resolved_format = policy.format
    points = resolved_unit == "points"
    golf = resolved_format == "golf"
    categorical = golf or (resolved_format == "cycling" and points)
    return {
        "name": "Navn",
        "team": "Land" if golf else "Hold",
        "position": "Kategori" if categorical else "Position",
        "value": "Point" if points else "Pris",
        "total_growth": "Totalændring" if points else "Totalvækst",
        "round_growth": "Rundeændring" if points else "Vækst",
        "status": "Status",
    }


def _matches_optional_number(
    value: int | None,
    lower: int | None,
    upper: int | None,
    missing_mode: str,
) -> bool:
    if value is None:
        return missing_mode in {"include", "only"}
    if missing_mode == "only":
        return False
    return (lower is None or value >= lower) and (upper is None or value <= upper)


def _matches(entry: PlayerEntry, query: PlayerStatisticsQuery) -> bool:
    needle = query.search.strip().casefold()
    if needle and needle not in " ".join(
        (entry.name, entry.team, entry.position)
    ).casefold():
        return False
    if query.teams and entry.team.casefold() not in {
        value.casefold() for value in query.teams
    }:
        return False
    if query.positions and entry.position.casefold() not in {
        value.casefold() for value in query.positions
    }:
        return False
    if query.min_value is not None and entry.value < query.min_value:
        return False
    if query.max_value is not None and entry.value > query.max_value:
        return False
    if not _matches_optional_number(
        entry.total_growth,
        query.min_total_growth,
        query.max_total_growth,
        query.missing_total_growth,
    ):
        return False
    if not _matches_optional_number(
        entry.round_growth,
        query.min_round_growth,
        query.max_round_growth,
        query.missing_round_growth,
    ):
        return False
    statuses = set(entry_statuses(entry))
    for status in PLAYER_STATUSES:
        rule = query.status_rule(status)
        if rule == "require" and status not in statuses:
            return False
        if rule == "exclude" and status in statuses:
            return False
    return True


def _sort_entries(
    entries: Iterable[PlayerEntry], query: PlayerStatisticsQuery
) -> tuple[PlayerEntry, ...]:
    values = sorted(entries, key=lambda entry: entry.name.casefold())
    if query.sort_field == "name":
        return tuple(
            sorted(
                values,
                key=lambda entry: entry.name.casefold(),
                reverse=query.sort_order == "desc",
            )
        )
    getter = {
        "value": lambda entry: entry.value,
        "team": lambda entry: entry.team.casefold(),
        "position": lambda entry: entry.position.casefold(),
        "total_growth": lambda entry: entry.total_growth,
        "round_growth": lambda entry: entry.round_growth,
        "source": lambda entry: entry.source_index,
    }[query.sort_field]
    present = [entry for entry in values if getter(entry) is not None]
    missing = [entry for entry in values if getter(entry) is None]
    present.sort(key=getter, reverse=query.sort_order == "desc")
    return tuple(present + missing)


def filter_player_statistics(
    statistics: ScrapedGame, query: PlayerStatisticsQuery
) -> tuple[PlayerEntry, ...]:
    return _sort_entries(
        (entry for entry in statistics.entries if _matches(entry, query)), query
    )


def build_player_export(
    statistics: ScrapedGame,
    query: PlayerStatisticsQuery,
    *,
    generated_at: datetime | None = None,
    source_generated_at: datetime | None = None,
) -> PlayerExportDocument:
    entries = filter_player_statistics(statistics, query)
    if not entries:
        raise PayloadError("Ingen spillere matcher de valgte filtre")
    timestamp = generated_at or datetime.now().astimezone()
    if timestamp.tzinfo is None:
        timestamp = timestamp.astimezone()
    return PlayerExportDocument(
        statistics, query, entries, timestamp, source_generated_at
    )


def player_row(
    entry: PlayerEntry, columns: Sequence[str], *, json_values: bool = False
) -> dict[str, object]:
    values: dict[str, object] = {
        "name": entry.name,
        "team": entry.team,
        "position": entry.position,
        "value": entry.value,
        "total_growth": entry.total_growth,
        "round_growth": entry.round_growth,
        "status": list(entry_statuses(entry)) if json_values else format_player_status(entry),
    }
    return {column: values[column] for column in columns}


def player_display_rows(
    statistics: ScrapedGame, query: PlayerStatisticsQuery
) -> tuple[list[dict[str, object]], tuple[str, ...]]:
    labels = player_column_labels(statistics)
    rows = [
        {labels[key]: value for key, value in player_row(entry, query.columns).items()}
        for entry in filter_player_statistics(statistics, query)
    ]
    integer_columns = tuple(
        labels[key]
        for key in query.columns
        if key in {"value", "total_growth", "round_growth"}
    )
    return rows, integer_columns


def player_query_to_dict(query: PlayerStatisticsQuery) -> dict[str, object]:
    return {
        "search": query.search,
        "teams": list(query.teams),
        "positions": list(query.positions),
        "value": {"min": query.min_value, "max": query.max_value},
        "total_growth": {
            "min": query.min_total_growth,
            "max": query.max_total_growth,
            "missing": query.missing_total_growth,
        },
        "round_growth": {
            "min": query.min_round_growth,
            "max": query.max_round_growth,
            "missing": query.missing_round_growth,
        },
        "statuses": {
            status: query.status_rule(status) for status in PLAYER_STATUSES
        },
        "columns": list(query.columns),
        "sort": {"field": query.sort_field, "order": query.sort_order},
    }


def player_export_to_dict(document: PlayerExportDocument) -> dict[str, object]:
    statistics = document.statistics
    return {
        "schema_version": PLAYER_EXPORT_SCHEMA_VERSION,
        "generated_at": document.generated_at.isoformat(),
        "source_generated_at": (
            document.source_generated_at.isoformat()
            if document.source_generated_at is not None
            else None
        ),
        "source": {"game_url": statistics.game.original},
        "game": {
            "locale": statistics.game.locale,
            "slug": statistics.game.slug,
            "variant": statistics.variant,
            "format": statistics.format,
            "unit": statistics.unit,
            "round": statistics.round_number,
        },
        "filters": player_query_to_dict(document.query),
        "row_count": len(document.entries),
        "columns": list(document.query.columns),
        "rows": [
            player_row(entry, document.query.columns, json_values=True)
            for entry in document.entries
        ],
    }


def _metadata_lines(document: PlayerExportDocument) -> list[tuple[str, str]]:
    statistics = document.statistics
    source_time = (
        document.source_generated_at.astimezone().strftime("%d.%m.%Y %H:%M:%S")
        if document.source_generated_at is not None
        else "–"
    )
    return [
        ("Spil", statistics.game.slug),
        ("Sprog", statistics.game.locale),
        ("Variant", statistics.variant),
        ("Format", statistics.format or "–"),
        ("Enhed", statistics.unit or "–"),
        ("Runde", str(statistics.round_number)),
        ("Kilde", statistics.game.original),
        ("Snapshot gemt", source_time),
        ("Eksport oprettet", document.generated_at.astimezone().strftime("%d.%m.%Y %H:%M:%S")),
        ("Rækker", str(len(document.entries))),
        (
            "Filtre",
            json.dumps(player_query_to_dict(document.query), ensure_ascii=False, separators=(",", ":")),
        ),
    ]


def _cell_text(value: object) -> str:
    if value is None:
        return "–"
    if isinstance(value, int) and not isinstance(value, bool):
        return format_integer(value)
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def player_export_to_txt(document: PlayerExportDocument) -> str:
    labels = player_column_labels(document.statistics)
    lines = [f"{label}: {value}" for label, value in _metadata_lines(document)]
    lines.extend(("", "\t".join(labels[key] for key in document.query.columns)))
    for entry in document.entries:
        row = player_row(entry, document.query.columns)
        lines.append("\t".join(_cell_text(row[key]) for key in document.query.columns))
    return "\n".join(lines) + "\n"


def _markdown_cell(value: object) -> str:
    return _cell_text(value).replace("\\", "\\\\").replace("|", "\\|")


def player_export_to_markdown(document: PlayerExportDocument) -> str:
    labels = player_column_labels(document.statistics)
    lines = [f"# Spillerstatistik – {document.statistics.game.slug}", ""]
    lines.extend(f"- **{label}:** {value}" for label, value in _metadata_lines(document))
    headers = [labels[key] for key in document.query.columns]
    lines.extend(("", "| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"))
    for entry in document.entries:
        row = player_row(entry, document.query.columns)
        lines.append(
            "| " + " | ".join(_markdown_cell(row[key]) for key in document.query.columns) + " |"
        )
    return "\n".join(lines) + "\n"


def serialize_player_export(document: PlayerExportDocument, format: str) -> bytes:
    if format == "txt":
        content = player_export_to_txt(document)
    elif format == "md":
        content = player_export_to_markdown(document)
    elif format == "json":
        content = json.dumps(
            player_export_to_dict(document), ensure_ascii=False, indent=2
        ) + "\n"
    else:
        raise ValueError(f"Ikke-understøttet eksportformat for spillere: {format}")
    return content.encode("utf-8")


class PlayerExportStore:
    """Explicitly publish immutable filtered player exports."""

    MIME_TYPES = {
        "txt": "text/plain; charset=utf-8",
        "json": "application/json",
        "md": "text/markdown; charset=utf-8",
    }

    def __init__(self, export_dir: Path | str) -> None:
        self.export_dir = Path(export_dir)

    def save(
        self,
        document: PlayerExportDocument,
        formats: Sequence[str],
    ) -> tuple[PlayerExportArtifact, ...]:
        selected = tuple(dict.fromkeys(value.casefold() for value in formats))
        if not selected or any(value not in PLAYER_EXPORT_FORMATS for value in selected):
            raise ValueError("Vælg et eller flere understøttede eksportformater for spillere")
        target_dir = self.export_dir / sanitize_path_component(
            document.statistics.game.slug, fallback="game"
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        stem = round_output_stem(
            "data", document.statistics.round_number, document.generated_at
        )
        contents = {
            format: serialize_player_export(document, format) for format in selected
        }
        collision_number = 0
        while True:
            suffix = collision_suffix(collision_number)
            paths = {
                format: target_dir / f"{stem}{suffix}.{format}"
                for format in selected
            }
            if any(path.exists() for path in paths.values()):
                collision_number += 1
                continue
            published: list[Path] = []
            try:
                for format in selected:
                    publish_immutable(paths[format], contents[format])
                    published.append(paths[format])
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
                PlayerExportArtifact(
                    format,
                    paths[format].resolve(),
                    contents[format],
                    self.MIME_TYPES[format],
                )
                for format in selected
            )

