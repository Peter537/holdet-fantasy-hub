from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import tomllib

from website.presentation import (
    data_status_badges,
    data_status_label,
    format_elo,
    format_precise_time,
    format_relative_precise,
    freshness_status,
    next_schedule_action,
)


def _metadata(*, start: datetime, close: datetime, end: datetime):
    return SimpleNamespace(
        rounds=(
            SimpleNamespace(
                round_number=4,
                start=start,
                close=close,
                end=end,
            ),
        )
    )


def test_theme_uses_supported_heading_weights() -> None:
    config_path = Path(__file__).parents[1] / ".streamlit" / "config.toml"
    config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    weights = config["theme"]["headingFontWeights"]
    assert weights == [700, 600, 600, 600, 600, 600]
    assert all(weight in {100, 200, 300, 400, 500, 600, 700, 800, 900} for weight in weights)


def test_relative_freshness_includes_relative_and_precise_time() -> None:
    offset = timezone(timedelta(hours=2))
    now = datetime(2026, 8, 9, 14, 11, tzinfo=offset)
    generated = datetime(2026, 8, 4, 22, 11, tzinfo=offset)

    assert format_precise_time(generated) == "04.08 kl. 22.11"
    assert format_relative_precise(generated, now=now) == (
        "4 dage siden · 04.08 kl. 22.11"
    )


def test_status_vocabulary_and_freshness_gate() -> None:
    offset = timezone(timedelta(hours=2))
    now = datetime(2026, 8, 9, 12, tzinfo=offset)
    metadata = _metadata(
        start=datetime(2026, 8, 7, 12, tzinfo=offset),
        close=datetime(2026, 8, 8, 12, tzinfo=offset),
        end=datetime(2026, 8, 10, 12, tzinfo=offset),
    )

    assert data_status_label("ready") == "Aktuel"
    assert data_status_label("preliminary") == "Foreløbig"
    assert data_status_label("stale") == "Forældet"
    assert data_status_label("missing") == "Mangler"
    assert data_status_label("failed") == "Fejlet"
    assert data_status_label("unknown") == "Ikke verificeret"
    assert data_status_label("future-status") == "Ikke verificeret"
    assert freshness_status(
        datetime(2026, 8, 7, 22, tzinfo=offset), 4, metadata, now=now
    ) == "Forældet"
    assert freshness_status(
        datetime(2026, 8, 9, 9, tzinfo=offset), 4, metadata, now=now
    ) == "Aktuel"


def test_badges_keep_failure_missing_and_preliminary_as_separate_signals() -> None:
    offset = timezone(timedelta(hours=2))
    now = datetime(2026, 8, 9, 12, tzinfo=offset)
    labels = tuple(
        badge.label
        for badge in data_status_badges(
            generated_at=None,
            round_number=None,
            round_status="in_progress",
            metadata=None,
            missing=True,
            last_success=datetime(2026, 8, 8, 10, tzinfo=offset),
            last_error=datetime(2026, 8, 9, 10, tzinfo=offset),
            now=now,
        )
    )
    assert labels == ("Fejlet", "Mangler", "Foreløbig")


def test_schedule_action_and_elo_display_are_consistent() -> None:
    offset = timezone(timedelta(hours=2))
    now = datetime(2026, 8, 9, 12, tzinfo=offset)
    metadata = _metadata(
        start=datetime(2026, 8, 9, 10, tzinfo=offset),
        close=datetime(2026, 8, 11, 12, tzinfo=offset),
        end=datetime(2026, 8, 12, 12, tzinfo=offset),
    )
    action = next_schedule_action(metadata, now=now)

    assert action == "Deadline om 2 dage · 11.08 kl. 12.00"
    assert format_elo(1412.5) == "1413"
    assert format_elo(1412.49) == "1412"
    assert format_elo(None) == "–"


def test_schedule_action_accepts_extreme_aware_sentinel_dates() -> None:
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    metadata = _metadata(
        start=datetime.min.replace(tzinfo=timezone.utc),
        close=datetime.max.replace(tzinfo=timezone.utc),
        end=datetime.max.replace(tzinfo=timezone.utc),
    )

    action = next_schedule_action(metadata, now=now)

    assert action is not None
    assert action.startswith("Deadline om ")
