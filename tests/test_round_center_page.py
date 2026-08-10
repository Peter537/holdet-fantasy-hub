from __future__ import annotations

from datetime import datetime, timedelta, timezone

import holdet_lib as holdet
from website.round_center_page import _live_target_round, _manifest_health


def test_manifest_health_ignores_failed_removed_team_step() -> None:
    started_at = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    completed_at = started_at + timedelta(seconds=5)
    manifest = holdet.RefreshManifest(
        schema_version=2,
        scope="game",
        run_id="current-run",
        started_at=started_at,
        completed_at=completed_at,
        game_slug="demo",
        target_round=3,
        steps=(
            holdet.RefreshManifestStep(
                "team:1",
                "team",
                "Hold: Aktuelt",
                "fetched",
                True,
                team_id=1,
            ),
            holdet.RefreshManifestStep(
                "team:99",
                "team",
                "Hold: Fjernet",
                "failed_no_cache",
                True,
                team_id=99,
                error="Holdet er ikke længere med i managerspillet",
            ),
        ),
    )

    assert _manifest_health(manifest) == (None, completed_at)
    assert _manifest_health(
        manifest,
        relevant_step_ids=frozenset({"team:1"}),
    ) == (completed_at, None)


def test_live_target_uses_latest_started_round_not_future_schedule() -> None:
    current = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    game = holdet.GameUrl(
        "https://www.holdet.dk/da/fantasy/demo",
        "da",
        "demo",
    )
    metadata = holdet.GameMetadata(
        game=game,
        variant="cycling",
        format="classic",
        game_id=7,
        salary_cap=50_000_000,
        final_round=4,
        display_name="Demo",
        rounds=(
            holdet.ScheduleRound(
                3,
                current - timedelta(hours=2),
                current - timedelta(hours=1),
                current + timedelta(hours=1),
            ),
            holdet.ScheduleRound(
                4,
                current + timedelta(hours=2),
                current + timedelta(hours=3),
                current + timedelta(hours=4),
            ),
        ),
        fetched_at=current,
    )

    assert _live_target_round(
        metadata,
        holdet.TradingWindowView("closed", round_number=3),
        2,
        now=current,
    ) == 3
