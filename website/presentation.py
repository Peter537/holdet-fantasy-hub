"""Shared presentation rules for the Streamlit user interface."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Mapping

import pandas as pd
import streamlit as st
from pandas.io.formats.style import Styler


DataStatus = Literal[
    "Aktuel",
    "Foreløbig",
    "Forældet",
    "Mangler",
    "Fejlet",
    "Ikke verificeret",
]


@dataclass(frozen=True, slots=True)
class StatusBadge:
    label: DataStatus
    color: Literal["green", "orange", "red", "gray"]
    icon: str


@dataclass(frozen=True, slots=True)
class ScheduleMilestone:
    round_number: int
    kind: Literal["start", "deadline", "end"]
    timestamp: datetime


_STATUS_BADGES: dict[DataStatus, StatusBadge] = {
    "Aktuel": StatusBadge("Aktuel", "green", ":material/check_circle:"),
    "Foreløbig": StatusBadge("Foreløbig", "orange", ":material/pending:"),
    "Forældet": StatusBadge("Forældet", "orange", ":material/history:"),
    "Mangler": StatusBadge("Mangler", "red", ":material/database_off:"),
    "Fejlet": StatusBadge("Fejlet", "red", ":material/error:"),
    "Ikke verificeret": StatusBadge(
        "Ikke verificeret", "gray", ":material/help:"
    ),
}

_DATA_STATUS_LABELS: dict[str, DataStatus] = {
    "ready": "Aktuel",
    "complete": "Aktuel",
    "final": "Aktuel",
    "current": "Aktuel",
    "preliminary": "Foreløbig",
    "in_progress": "Foreløbig",
    "stale": "Forældet",
    "missing": "Mangler",
    "error": "Fejlet",
    "failed": "Fejlet",
    "unknown": "Ikke verificeret",
    "unverified": "Ikke verificeret",
}

_COLUMN_HELP = {
    "Rang": "Placering i den viste rangliste. En tankestreg betyder, at placeringen endnu ikke kan beregnes.",
    "Elo": "Managerens Elo-rating, afrundet til nærmeste heltal til visning.",
    "Afstand": "Forskel til den førende placering i den viste opgørelse.",
    "Form 3": "Gennemsnitlig udvikling over de seneste tre tilgængelige runder.",
    "Form 5": "Gennemsnitlig udvikling over de seneste fem tilgængelige runder.",
    "Stabilitet": "Stabilitetsscore fra 0 til 100 baseret på spillerens historiske udsving.",
    "Grundlag": "Antal observationer, der ligger bag den afledte beregning.",
    "Datastatus": "Om datagrundlaget er aktuelt, foreløbigt, forældet, mangler, fejlet eller ikke verificeret.",
    "Start": "Tidspunktet hvor handel eller rundens aktive periode starter.",
    "Deadline": "Sidste tidspunkt for at foretage ændringer i runden.",
    "Slut": "Tidspunktet hvor runden afsluttes.",
    "Størrelse": "Samlet lagerforbrug for filen eller datasættet.",
    "Buchholz": "Summen af modstandernes point; bruges som tie-break i Swiss-turneringer.",
    "Dækning": "Andelen af forventede datapunkter, der er tilgængelige.",
}

_INTEGER_COLUMNS = {
    "Rang",
    "Runde",
    "Point",
    "Perioder",
    "Titler",
    "Podier",
    "Grundlag",
    "For",
    "Imod",
    "Forskel",
    "Hold-ID",
    "Filer",
    "Kampe",
    "Sejre",
    "Uafgjorte",
    "Nederlag",
    "Buchholz",
}

_MONEY_MARKERS = (
    "Pris",
    "Værdi",
    "Vækst",
    "Ændring",
    "Bank",
    "Afstand",
)

_DATETIME_COLUMNS = {
    "Start",
    "Deadline",
    "Slut",
    "Gemt",
    "Hentet",
    "Oprettet",
    "Ændret",
    "Seneste succes",
    "Seneste fejl",
}


def _local(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=datetime.now().astimezone().tzinfo)
    try:
        return value.astimezone()
    except (OSError, OverflowError, ValueError):
        # Windows cannot convert some aware min/max sentinel dates. They remain
        # comparable as aware datetimes and are only technical schedule bounds.
        return value


def format_precise_time(value: datetime | None) -> str:
    if value is None:
        return "–"
    return _local(value).strftime("%d.%m kl. %H.%M")


def _duration_label(seconds: int) -> str:
    seconds = max(0, seconds)
    if seconds < 60:
        return "under ét minut"
    if seconds < 3_600:
        minutes = seconds // 60
        return "1 minut" if minutes == 1 else f"{minutes} minutter"
    if seconds < 86_400:
        hours = seconds // 3_600
        return "1 time" if hours == 1 else f"{hours} timer"
    days = seconds // 86_400
    return "1 dag" if days == 1 else f"{days} dage"


def format_relative_precise(
    value: datetime | None, *, now: datetime | None = None
) -> str:
    if value is None:
        return "Mangler"
    current = _local(now or datetime.now().astimezone())
    local_value = _local(value)
    seconds = int((current - local_value).total_seconds())
    if seconds < 0:
        return f"om {_duration_label(abs(seconds))} · {format_precise_time(local_value)}"
    return f"{_duration_label(seconds)} siden · {format_precise_time(local_value)}"


def latest_passed_milestone(
    metadata: Any | None, *, now: datetime | None = None
) -> ScheduleMilestone | None:
    if metadata is None:
        return None
    current = _local(now or datetime.now().astimezone())
    candidates: list[ScheduleMilestone] = []
    for round_item in metadata.rounds:
        for kind, timestamp in (
            ("start", round_item.start),
            ("deadline", round_item.close),
            ("end", round_item.end),
        ):
            local_timestamp = _local(timestamp)
            if local_timestamp <= current:
                candidates.append(
                    ScheduleMilestone(
                        round_number=round_item.round_number,
                        kind=kind,
                        timestamp=local_timestamp,
                    )
                )
    return max(candidates, key=lambda item: item.timestamp, default=None)


def freshness_status(
    generated_at: datetime | None,
    round_number: int | None,
    metadata: Any | None,
    *,
    now: datetime | None = None,
) -> DataStatus:
    if generated_at is None:
        return "Mangler"
    milestone = latest_passed_milestone(metadata, now=now)
    if milestone is None:
        return "Ikke verificeret"
    if (
        _local(generated_at) < milestone.timestamp
        or round_number is None
        or round_number < milestone.round_number
    ):
        return "Forældet"
    return "Aktuel"


def data_status_badges(
    *,
    generated_at: datetime | None,
    round_number: int | None,
    round_status: str | None,
    metadata: Any | None,
    missing: bool = False,
    last_success: datetime | None = None,
    last_error: datetime | None = None,
    now: datetime | None = None,
) -> tuple[StatusBadge, ...]:
    labels: list[DataStatus] = []
    if last_error is not None and (
        last_success is None or _local(last_error) > _local(last_success)
    ):
        labels.append("Fejlet")
    if missing or generated_at is None:
        labels.append("Mangler")
    else:
        labels.append(
            freshness_status(
                generated_at,
                round_number,
                metadata,
                now=now,
            )
        )
    if round_status == "in_progress":
        labels.append("Foreløbig")
    elif round_status in {None, "unknown"} and generated_at is not None:
        labels.append("Ikke verificeret")
    unique = tuple(dict.fromkeys(labels))
    return tuple(_STATUS_BADGES[label] for label in unique)


def render_status_badges(
    badges: tuple[StatusBadge, ...], *, help: str | None = None
) -> None:
    with st.container(horizontal=True, gap="small"):
        for badge in badges:
            st.badge(
                badge.label,
                color=badge.color,
                icon=badge.icon,
                help=help,
            )


def data_status_label(value: object) -> str:
    if value is None:
        return "Mangler"
    normalized = str(value).strip().casefold()
    existing = next(
        (label for label in _STATUS_BADGES if label.casefold() == normalized),
        None,
    )
    return existing or _DATA_STATUS_LABELS.get(normalized, "Ikke verificeret")


def next_schedule_action(
    metadata: Any | None, *, now: datetime | None = None
) -> str | None:
    if metadata is None:
        return None
    current = _local(now or datetime.now().astimezone())
    open_rounds = tuple(
        round_item
        for round_item in metadata.rounds
        if _local(round_item.start) <= current < _local(round_item.close)
    )
    if open_rounds:
        deadline = min(_local(item.close) for item in open_rounds)
        return f"Deadline {format_relative_precise(deadline, now=current)}"
    future_starts = tuple(
        _local(item.start)
        for item in metadata.rounds
        if _local(item.start) > current
    )
    if future_starts:
        start = min(future_starts)
        return f"Handel åbner {format_relative_precise(start, now=current)}"
    return None


def sport_label(slug: str) -> str:
    labels = {
        "super-manager": "Fodbold",
        "motor": "Motorsport",
        "golf": "Golf",
        "tour": "Cykling",
    }
    return next((label for marker, label in labels.items() if marker in slug), "Managerspil")


def format_elo(value: object) -> str:
    if value is None or bool(pd.isna(value)):
        return "–"
    return str(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _columns(data: object) -> tuple[str, ...]:
    if isinstance(data, Styler):
        return tuple(str(value) for value in data.data.columns)
    if isinstance(data, pd.DataFrame):
        return tuple(str(value) for value in data.columns)
    try:
        return tuple(str(value) for value in pd.DataFrame(data).columns)
    except (TypeError, ValueError):
        return ()


def _column_config_for(columns: tuple[str, ...]) -> dict[str, object]:
    config: dict[str, object] = {}
    for column in columns:
        help_text = _COLUMN_HELP.get(column)
        if column == "Elo":
            config[column] = st.column_config.NumberColumn(
                column, format="%.0f", help=help_text
            )
        elif column in _INTEGER_COLUMNS or any(
            marker in column for marker in _MONEY_MARKERS
        ):
            config[column] = st.column_config.NumberColumn(
                column, format="localized", help=help_text
            )
        elif column in _DATETIME_COLUMNS:
            config[column] = st.column_config.DatetimeColumn(
                column,
                format="DD.MM.YYYY [kl.] HH.mm",
                help=help_text,
            )
        elif help_text is not None:
            config[column] = st.column_config.TextColumn(column, help=help_text)
    return config


def _friendly_data(data: object) -> object:
    if isinstance(data, Styler):
        return data
    if isinstance(data, pd.DataFrame):
        frame = data.copy()
    elif isinstance(data, (list, tuple)):
        frame = pd.DataFrame(data)
    else:
        return data
    for column in frame.columns:
        series = frame[column]
        if (
            pd.api.types.is_object_dtype(series.dtype)
            or pd.api.types.is_string_dtype(series.dtype)
        ):
            frame[column] = series.map(
                lambda value: "–"
                if value is None or (not isinstance(value, (list, tuple, dict)) and bool(pd.isna(value)))
                else value
            )
    return frame


def dataframe(
    data: object,
    *,
    key: str,
    column_config: Mapping[str, object] | None = None,
    **kwargs: object,
):
    """Render a consistently formatted dataframe with a stable key."""

    columns = _columns(data)
    merged = _column_config_for(columns)
    if column_config:
        merged.update(column_config)
    return st.dataframe(
        _friendly_data(data),
        key=key,
        column_config=merged or None,
        **kwargs,
    )
