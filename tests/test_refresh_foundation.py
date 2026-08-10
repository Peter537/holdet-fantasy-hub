from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

import holdet_lib as holdet
from holdet_lib.backup import known_schema_error
from tests.test_library_storage import sample_team, temporary_directory
from tests.test_player_statistics import sample_statistics


def _manager_game_and_group(*team_ids: int) -> tuple[holdet.ManagerGame, holdet.GroupDefinition]:
    teams = tuple(sample_team(team_id, current_round=3) for team_id in team_ids)
    manager_game = holdet.ManagerGame(teams[0].reference.game, "Tourspillet")
    group = holdet.GroupDefinition(
        "friends",
        "Venner",
        manager_game.game,
        tuple(
            holdet.GroupTeam(
                team.reference.team_id,
                team.team_name,
                team.reference.source_url,
            )
            for team in teams
        ),
    )
    return manager_game, group


def _metadata(game: holdet.GameUrl, now: datetime) -> holdet.GameMetadata:
    return holdet.GameMetadata(
        game=game,
        variant="cycling",
        format="classic",
        game_id=7,
        salary_cap=50_000_000,
        final_round=3,
        display_name="Tourspillet",
        rounds=(
            holdet.ScheduleRound(
                3,
                now - timedelta(days=1),
                now - timedelta(hours=2),
                now + timedelta(days=1),
            ),
        ),
        fetched_at=now - timedelta(hours=1),
    )


def _manifest(now: datetime, *, status: str = "fetched") -> holdet.RefreshManifest:
    run_id = str(uuid4())
    reference = "snapshots/demo/teams/one.json"
    return holdet.RefreshManifest(
        schema_version=2,
        scope="game",
        run_id=run_id,
        started_at=now,
        completed_at=now + timedelta(seconds=1),
        game_slug="demo",
        target_round=3,
        steps=(
            holdet.RefreshManifestStep(
                "team:1",
                "team",
                "Hold: Et",
                status,  # type: ignore[arg-type]
                True,
                started_at=now,
                completed_at=now + timedelta(seconds=1),
                team_id=1,
                team_name="Et",
                round_number=3,
                data_reference=reference if status == "fetched" else None,
                cache_reference=(
                    reference if status == "reused_after_error" else None
                ),
                cache_generated_at=(
                    now if status == "reused_after_error" else None
                ),
                error="offline" if status == "reused_after_error" else None,
            ),
        ),
        game_locale="da",
        game_name="Demo",
        game_url="https://www.holdet.dk/da/fantasy/demo",
        attempted_team_ids=(1,),
        origin_run_id=run_id,
    )


def _previous_manifest(
    manager_game: holdet.ManagerGame,
    now: datetime,
    steps: tuple[holdet.RefreshManifestStep, ...],
) -> holdet.RefreshManifest:
    run_id = str(uuid4())
    return holdet.RefreshManifest(
        schema_version=2,
        scope="game",
        run_id=run_id,
        started_at=now - timedelta(minutes=2),
        completed_at=now - timedelta(minutes=1),
        game_slug=manager_game.game.slug,
        target_round=3,
        steps=steps,
        game_locale=manager_game.game.locale,
        game_name=manager_game.name,
        game_url=manager_game.game.original,
        origin_run_id=run_id,
    )


def _future_metadata(
    game: holdet.GameUrl,
    now: datetime,
    *,
    fetched_at: datetime | None = None,
) -> holdet.GameMetadata:
    return holdet.GameMetadata(
        game=game,
        variant="cycling",
        format="classic",
        game_id=7,
        salary_cap=50_000_000,
        final_round=3,
        display_name="Tourspillet",
        rounds=(
            holdet.ScheduleRound(
                3,
                now + timedelta(hours=1),
                now + timedelta(hours=2),
                now + timedelta(days=1),
            ),
        ),
        fetched_at=fetched_at or now,
    )


def test_stale_preview_and_retry_plan_select_only_relevant_sources() -> None:
    current = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager_game, group = _manager_game_and_group(1, 2)
    with temporary_directory() as root:
        snapshots = holdet.SnapshotStore(root / "snapshots")
        players = holdet.PlayerStatisticsStore(root / "snapshots")
        snapshots.save_team_json(sample_team(1, current_round=3), now=current)
        players.save(
            replace(sample_statistics(3), round_status="complete"),
            now=current - timedelta(hours=3),
        )
        plan = holdet.build_refresh_plan(
            manager_game,
            (group,),
            snapshots.scan(),
            players.scan(manager_game.game),
            metadata=_metadata(manager_game.game, current),
            mode=holdet.RefreshMode.STALE_ONLY,
            now=current,
        )

        assert [item.step_id for item in plan.selected_steps] == [
            "players",
            "team:2",
        ]
        assert plan.selected_team_count == 1
        assert plan.selected_source_count == 2
        assert plan.target_round == 3

        failed = replace(
            _manifest(current, status="reused_after_error"),
            game_slug=manager_game.game.slug,
            game_locale=manager_game.game.locale,
        )
        retry = holdet.build_refresh_plan(
            manager_game,
            (group,),
            snapshots.scan(),
            players.scan(manager_game.game),
            metadata=_metadata(manager_game.game, current),
            mode=holdet.RefreshMode.RETRY_FAILED,
            previous_manifest=failed,
            now=current,
        )
        assert [item.step_id for item in retry.selected_steps] == ["team:1"]
        assert retry.retry_of == failed.run_id
        assert retry.origin_run_id == failed.run_id


def test_refresh_progress_and_manifest_distinguish_cache_and_failure() -> None:
    current = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager_game, group = _manager_game_and_group(1, 2)
    cached_team = sample_team(1, current_round=3)
    cached_players = replace(sample_statistics(3), round_status="complete")
    events: list[holdet.RefreshProgressEvent] = []

    class OfflineClient:
        def fetch_players(self, _game):
            raise holdet.FetchError("spillerlisten er offline")

        def fetch_team(self, reference):
            raise holdet.FetchError(f"hold {reference.team_id} er offline")

    with temporary_directory() as root:
        snapshots = holdet.SnapshotStore(root / "snapshots")
        players = holdet.PlayerStatisticsStore(root / "snapshots")
        manifests = holdet.ManifestStore(root / "manifests")
        snapshots.save_team_json(cached_team, now=current - timedelta(hours=1))
        players.save(cached_players, now=current - timedelta(hours=1))

        result = holdet.refresh_manager_game(
            manager_game,
            (group,),
            OfflineClient(),
            snapshots,
            players,
            manifests,
            progress=events.append,
            now=current,
        )

        assert [item.status for item in result.teams] == [
            "cached_fallback",
            "failed",
        ]
        assert result.player is not None
        assert result.player.status == "cached_fallback"
        assert result.manifest is not None
        assert result.manifest_path.parent == (
            root
            / "manifests"
            / f"da--{manager_game.game.slug}"
            / "game"
        )
        assert [item.status for item in result.manifest.steps] == [
            "skipped_unavailable",
            "reused_after_error",
            "reused_after_error",
            "failed_no_cache",
        ]
        assert [item.status for item in result.manifest.failures] == [
            "reused_after_error",
            "reused_after_error",
            "failed_no_cache",
        ]
        assert [item.status for item in events] == [
            "running",
            "reused_after_error",
            "running",
            "reused_after_error",
            "running",
            "failed_no_cache",
        ]
        assert [item.completed_steps for item in events] == [0, 1, 1, 2, 2, 3]
        raw = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert raw["schema_version"] == 2
        assert all(
            not Path(reference).is_absolute()
            for step in raw["steps"]
            for reference in (step["data_reference"], step["cache_reference"])
            if reference is not None
        )


def test_manifest_store_dual_reads_and_rejects_unsafe_schema_two() -> None:
    current = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    with temporary_directory() as root:
        store = holdet.ManifestStore(root / "manifests")
        path = store.write(_manifest(current))
        loaded = store.load(path)
        assert loaded.schema_version == 2
        assert loaded.steps[0].status == "fetched"
        assert loaded.origin_run_id == loaded.run_id

        legacy_path = store.save_game_manifest(
            "demo",
            2,
            {
                "schema_version": 1,
                "scope": "game",
                "generated_at": (current - timedelta(days=1)).isoformat(),
                "game": {
                    "name": "Demo",
                    "url": "https://www.holdet.dk/da/fantasy/demo",
                    "locale": "da",
                    "slug": "demo",
                },
                "round": 2,
                "attempted_team_ids": [1],
                "skipped_team_ids": [],
                "groups": [],
                "teams": [
                    {
                        "team_id": 1,
                        "team_name": "Et",
                        "status": "cached_fallback",
                        "snapshot_path": "C:\\private\\snapshot.json",
                        "error": "offline",
                    }
                ],
            },
            now=current - timedelta(days=1),
        )
        legacy = store.load(legacy_path)
        assert [item.status for item in legacy.steps[:2]] == [
            "not_recorded",
            "not_recorded",
        ]
        assert legacy.steps[2].status == "reused_after_error"
        assert legacy.steps[2].cache_reference is None

        values, warnings = store.scan("demo", game_locale="da")
        assert not warnings
        assert [item.schema_version for item in values] == [2, 1]
        assert store.load_latest("demo", game_locale="da") == values[0]

        unsafe = replace(
            _manifest(current),
            steps=(
                replace(
                    _manifest(current).steps[0],
                    data_reference="C:\\private\\snapshot.json",
                ),
            ),
        )
        with pytest.raises((ValueError, holdet.PayloadError), match="reference"):
            store.write(unsafe)


def test_backup_validation_accepts_both_manifest_schemas() -> None:
    assert known_schema_error(
        "data/manifests/da--demo/game/refresh-round3.json",
        b'{"schema_version": 1}',
    ) is None
    assert known_schema_error(
        "data/manifests/da--demo/game/refresh-round3.json",
        b'{"schema_version": 2}',
    ) is None
    assert "ukendt manifestschema" in str(
        known_schema_error(
            "data/manifests/da--demo/game/refresh-round3.json",
            b'{"schema_version": 3}',
        )
    )


def test_daily_age_boundary_and_newer_manifest_failure_drive_stale_plan() -> None:
    current = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager_game, group = _manager_game_and_group(1)
    metadata = _future_metadata(manager_game.game, current)
    with temporary_directory() as root:
        snapshots = holdet.SnapshotStore(root / "snapshots")
        players = holdet.PlayerStatisticsStore(root / "snapshots")
        snapshots.save_team_json(
            sample_team(1, current_round=3),
            now=current - holdet.DAILY_REFRESH_MAX_AGE,
        )
        players.save(
            replace(sample_statistics(3), round_status="complete"),
            now=current - holdet.DAILY_REFRESH_MAX_AGE,
        )
        boundary = holdet.build_refresh_plan(
            manager_game,
            (group,),
            snapshots.scan(),
            players.scan(manager_game.game),
            metadata=metadata,
            mode=holdet.RefreshMode.STALE_ONLY,
            now=current,
        )
        assert [item.step_id for item in boundary.selected_steps] == [
            "players",
            "team:1",
        ]
        assert all(
            "24 timer" in item.reason for item in boundary.selected_steps
        )
        snapshots.save_team_json(
            sample_team(1, current_round=3),
            now=current - holdet.DAILY_REFRESH_MAX_AGE + timedelta(seconds=1),
        )
        players.save(
            replace(sample_statistics(3), round_status="complete"),
            now=current - holdet.DAILY_REFRESH_MAX_AGE + timedelta(seconds=1),
        )
        inside_boundary = holdet.build_refresh_plan(
            manager_game,
            (group,),
            snapshots.scan(),
            players.scan(manager_game.game),
            metadata=metadata,
            mode=holdet.RefreshMode.STALE_ONLY,
            now=current,
        )
        assert not inside_boundary.selected_steps

    with temporary_directory() as root:
        snapshots = holdet.SnapshotStore(root / "snapshots")
        players = holdet.PlayerStatisticsStore(root / "snapshots")
        cache_time = current - timedelta(minutes=30)
        snapshots.save_team_json(sample_team(1, current_round=3), now=cache_time)
        players.save(
            replace(sample_statistics(3), round_status="complete"),
            now=cache_time,
        )
        failure = _previous_manifest(
            manager_game,
            current,
            (
                holdet.RefreshManifestStep(
                    "team:1",
                    "team",
                    "Hold: Et",
                    "reused_after_error",
                    True,
                    started_at=current - timedelta(minutes=2),
                    completed_at=current - timedelta(minutes=1),
                    team_id=1,
                    team_name="Et",
                    round_number=3,
                    cache_reference="snapshots/cache.json",
                    cache_generated_at=cache_time,
                    error="offline",
                ),
            ),
        )
        failed_plan = holdet.build_refresh_plan(
            manager_game,
            (group,),
            snapshots.scan(),
            players.scan(manager_game.game),
            metadata=metadata,
            mode=holdet.RefreshMode.STALE_ONLY,
            previous_manifest=failure,
            now=current,
        )
        assert [item.step_id for item in failed_plan.selected_steps] == ["team:1"]
        assert "Seneste opdatering fejlede" in failed_plan.selected_steps[0].reason


def test_successful_metadata_fetch_revalidates_remaining_stale_steps() -> None:
    current = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager_game, group = _manager_game_and_group(1)
    old_metadata = holdet.GameMetadata(
        game=manager_game.game,
        variant="cycling",
        format="classic",
        game_id=7,
        salary_cap=50_000_000,
        final_round=3,
        display_name="Tourspillet",
        rounds=(
            holdet.ScheduleRound(
                3,
                current - timedelta(days=1),
                current - timedelta(minutes=30),
                current + timedelta(days=1),
            ),
        ),
        fetched_at=current - timedelta(hours=1),
    )
    corrected_metadata = replace(
        old_metadata,
        rounds=(
            holdet.ScheduleRound(
                3,
                current - timedelta(days=1),
                current + timedelta(hours=2),
                current + timedelta(days=1),
            ),
        ),
        fetched_at=current,
    )
    calls: list[str] = []

    class MetadataOnlyClient:
        def fetch_game_info(self, _game):
            calls.append("metadata")
            return corrected_metadata

        def fetch_players(self, _game):
            raise AssertionError("Spillere blev ikke revalideret")

        def fetch_team(self, _reference):
            raise AssertionError("Hold blev ikke revalideret")

    with temporary_directory() as root:
        snapshots = holdet.SnapshotStore(root / "snapshots")
        players = holdet.PlayerStatisticsStore(root / "snapshots")
        metadata_store = holdet.GameMetadataStore(root / "metadata")
        snapshots.save_team_json(
            sample_team(1, current_round=3),
            now=current - timedelta(hours=1),
        )
        players.save(
            replace(sample_statistics(3), round_status="complete"),
            now=current - timedelta(hours=1),
        )
        metadata_store.save(old_metadata)
        plan = holdet.build_refresh_plan(
            manager_game,
            (group,),
            snapshots.scan(),
            players.scan(manager_game.game),
            metadata=old_metadata,
            mode=holdet.RefreshMode.STALE_ONLY,
            include_postprocess=True,
            now=current,
        )
        assert [item.step_id for item in plan.selected_steps] == [
            "metadata",
            "players",
            "team:1",
            "postprocess",
        ]

        result = holdet.refresh_manager_game(
            manager_game,
            (group,),
            MetadataOnlyClient(),
            snapshots,
            players,
            holdet.ManifestStore(root / "manifests"),
            metadata_store=metadata_store,
            plan=plan,
            postprocess=lambda: calls.append("postprocess"),
            now=current,
        )
        assert calls == ["metadata", "postprocess"]
        assert result.plan is not None
        assert [item.step_id for item in result.plan.selected_steps] == [
            "metadata",
            "postprocess",
        ]
        assert result.manifest is not None
        assert [item.status for item in result.manifest.steps] == [
            "fetched",
            "reused_current",
            "reused_current",
            "fetched",
        ]


def test_corrupt_metadata_is_isolated_and_other_sources_continue() -> None:
    current = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager_game, group = _manager_game_and_group(1)
    team = sample_team(1, current_round=3)

    class Client:
        def fetch_game_info(self, _game):
            return _future_metadata(manager_game.game, current)

        def fetch_players(self, _game):
            return replace(sample_statistics(3), round_status="complete")

        def fetch_team(self, _reference):
            return team

    with temporary_directory() as root:
        metadata_dir = root / "metadata"
        metadata_dir.mkdir()
        metadata_path = metadata_dir / f"da--{manager_game.game.slug}.json"
        metadata_path.write_text("{broken", encoding="utf-8")
        result = holdet.refresh_manager_game(
            manager_game,
            (group,),
            Client(),
            holdet.SnapshotStore(root / "snapshots"),
            holdet.PlayerStatisticsStore(root / "snapshots"),
            holdet.ManifestStore(root / "manifests"),
            metadata_store=holdet.GameMetadataStore(metadata_dir),
            now=current,
        )
        assert result.metadata is not None
        assert result.metadata.status == "success"
        assert result.player is not None and result.player.status == "success"
        assert [item.status for item in result.teams] == ["success"]
        assert metadata_path.read_text(encoding="utf-8") != "{broken"
        assert holdet.GameMetadataStore(metadata_dir).load(manager_game.game)


def test_retry_keeps_removed_team_and_reruns_dependent_postprocess() -> None:
    current = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager_game, group = _manager_game_and_group(1)
    previous = _previous_manifest(
        manager_game,
        current,
        (
            holdet.RefreshManifestStep(
                "team:1",
                "team",
                "Hold: Et",
                "failed_no_cache",
                True,
                started_at=current - timedelta(minutes=2),
                completed_at=current - timedelta(minutes=1),
                team_id=1,
                team_name="Et",
                error="offline",
            ),
            holdet.RefreshManifestStep(
                "team:2",
                "team",
                "Hold: Fjernet",
                "failed_no_cache",
                True,
                started_at=current - timedelta(minutes=2),
                completed_at=current - timedelta(minutes=1),
                team_id=2,
                team_name="Fjernet",
                error="offline",
            ),
            holdet.RefreshManifestStep(
                "postprocess",
                "postprocess",
                "Efterbehandling",
                "fetched",
                True,
                started_at=current - timedelta(minutes=2),
                completed_at=current - timedelta(minutes=1),
            ),
        ),
    )
    plan = holdet.build_refresh_plan(
        manager_game,
        (group,),
        holdet.SnapshotIndex(()),
        holdet.PlayerStatisticsIndex(()),
        mode=holdet.RefreshMode.RETRY_FAILED,
        previous_manifest=previous,
        include_metadata=False,
        include_postprocess=True,
        now=current,
    )
    assert [item.step_id for item in plan.selected_steps] == [
        "team:1",
        "postprocess",
    ]
    removed = next(item for item in plan.steps if item.step_id == "team:2")
    assert not removed.available and not removed.selected

    calls: list[int] = []
    postprocess_calls: list[str] = []

    class Client:
        def fetch_players(self, _game):
            raise AssertionError("Spillere må ikke retries")

        def fetch_team(self, reference):
            calls.append(reference.team_id)
            return sample_team(reference.team_id, current_round=3)

    with temporary_directory() as root:
        result = holdet.refresh_manager_game(
            manager_game,
            (group,),
            Client(),
            holdet.SnapshotStore(root / "snapshots"),
            holdet.PlayerStatisticsStore(root / "snapshots"),
            holdet.ManifestStore(root / "manifests"),
            plan=plan,
            postprocess=lambda: postprocess_calls.append("postprocess"),
            now=current,
        )
        assert calls == [1]
        assert postprocess_calls == ["postprocess"]
        assert result.manifest is not None
        statuses = {item.step_id: item.status for item in result.manifest.steps}
        assert statuses["team:1"] == "fetched"
        assert statuses["team:2"] == "skipped_unavailable"
        assert statuses["postprocess"] == "fetched"
        assert result.manifest.attempted_team_ids == (1,)
        assert result.manifest.skipped_team_ids == (2,)
        assert result.manifest.retry_of == previous.run_id


def test_postprocess_failure_isolated_and_retryable() -> None:
    current = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager_game, group = _manager_game_and_group(1)

    class Client:
        def fetch_players(self, _game):
            return replace(sample_statistics(3), round_status="complete")

        def fetch_team(self, _reference):
            return sample_team(1, current_round=3)

    with temporary_directory() as root:
        snapshots = holdet.SnapshotStore(root / "snapshots")
        players = holdet.PlayerStatisticsStore(root / "snapshots")

        def fail_postprocess() -> None:
            raise RuntimeError("historik kunne ikke publiceres")

        result = holdet.refresh_manager_game(
            manager_game,
            (group,),
            Client(),
            snapshots,
            players,
            holdet.ManifestStore(root / "manifests"),
            postprocess=fail_postprocess,
            now=current,
        )
        assert result.plan is not None and result.plan.include_postprocess
        assert result.manifest is not None
        outcome = next(
            item for item in result.manifest.steps if item.step_id == "postprocess"
        )
        assert outcome.status == "failed_no_cache"
        assert "historik" in str(outcome.error)

        retry = holdet.build_refresh_plan(
            manager_game,
            (group,),
            snapshots.scan(),
            players.scan(manager_game.game),
            mode=holdet.RefreshMode.RETRY_FAILED,
            previous_manifest=result.manifest,
            include_metadata=False,
            include_postprocess=True,
            now=current + timedelta(minutes=1),
        )
        assert [item.step_id for item in retry.selected_steps] == ["postprocess"]

        stale = holdet.build_refresh_plan(
            manager_game,
            (group,),
            snapshots.scan(),
            players.scan(manager_game.game),
            metadata=_future_metadata(manager_game.game, current),
            mode=holdet.RefreshMode.STALE_ONLY,
            previous_manifest=result.manifest,
            include_postprocess=True,
            now=current + timedelta(minutes=1),
        )
        assert [item.step_id for item in stale.selected_steps] == ["postprocess"]


def test_alert_persistence_failure_does_not_reclassify_fetched_players() -> None:
    current = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager_game, group = _manager_game_and_group(1)
    adverse = replace(sample_statistics(3), round_status="complete")
    healthy_entry = replace(
        adverse.entries[0],
        is_active=True,
        is_disabled=False,
        is_injured=False,
        has_suspension=False,
    )
    baseline = replace(adverse, entries=(healthy_entry,))
    settings = holdet.HubSettings(
        watchlist=(holdet.watchlist_entry(manager_game.game, healthy_entry),)
    )

    class Inbox:
        fail = True
        calls = 0
        snapshot_times: list[datetime | None] = []

        def merge(self, alerts):
            self.calls += 1
            assert alerts
            self.snapshot_times.append(alerts[0].snapshot_generated_at)
            if self.fail:
                raise OSError("indbakken er låst")

    inbox = Inbox()

    class Client:
        def fetch_players(self, _game):
            return adverse

        def fetch_team(self, _reference):
            return sample_team(1, current_round=3)

    with temporary_directory() as root:
        snapshots = holdet.SnapshotStore(root / "snapshots")
        players = holdet.PlayerStatisticsStore(root / "snapshots")
        players.save(baseline, now=current - timedelta(hours=1))
        manifests = holdet.ManifestStore(root / "manifests")
        result = holdet.refresh_manager_game(
            manager_game,
            (group,),
            Client(),
            snapshots,
            players,
            manifests,
            settings=settings,
            inbox_store=inbox,  # type: ignore[arg-type]
            postprocess=lambda: None,
            now=current,
        )

        assert result.player is not None
        assert result.player.status == "success"
        assert result.manifest is not None
        statuses = {item.step_id: item.status for item in result.manifest.steps}
        assert statuses["players"] == "fetched"
        assert statuses["postprocess"] == "failed_no_cache"
        original_players = next(
            item for item in result.manifest.steps if item.step_id == "players"
        )
        assert original_players.data_reference is not None
        assert players.scan(manager_game.game).newest(manager_game.game) is not None

        retry = holdet.build_refresh_plan(
            manager_game,
            (group,),
            snapshots.scan(),
            players.scan(manager_game.game),
            mode=holdet.RefreshMode.RETRY_FAILED,
            previous_manifest=result.manifest,
            include_metadata=False,
            include_postprocess=True,
            now=current + timedelta(minutes=1),
        )
        assert [item.step_id for item in retry.selected_steps] == ["postprocess"]
        newer_path = players.save(adverse, now=current + timedelta(seconds=30))
        inbox.fail = False

        class NoNetworkClient:
            def __getattr__(self, name):
                raise AssertionError(f"Netværk måtte ikke bruges: {name}")

        retried = holdet.refresh_manager_game(
            manager_game,
            (group,),
            NoNetworkClient(),  # type: ignore[arg-type]
            snapshots,
            players,
            manifests,
            settings=settings,
            inbox_store=inbox,  # type: ignore[arg-type]
            plan=retry,
            postprocess=lambda: None,
            now=current + timedelta(minutes=1),
        )
        assert retried.manifest is not None
        retried_postprocess = next(
            item
            for item in retried.manifest.steps
            if item.step_id == "postprocess"
        )
        assert retried_postprocess.status == "fetched"
        retried_players = next(
            item for item in retried.manifest.steps if item.step_id == "players"
        )
        assert retried_players.cache_reference == original_players.data_reference
        assert newer_path.name not in str(retried_players.cache_reference)
        assert inbox.calls == 2
        assert inbox.snapshot_times == [current, current]


def test_non_player_postprocess_does_not_replay_old_alert_transition() -> None:
    current = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager_game, group = _manager_game_and_group(1)
    adverse = replace(sample_statistics(3), round_status="complete")
    healthy = replace(
        adverse,
        entries=(
            replace(
                adverse.entries[0],
                is_active=True,
                is_disabled=False,
                is_injured=False,
                has_suspension=False,
            ),
        ),
    )
    settings = holdet.HubSettings(
        watchlist=(holdet.watchlist_entry(manager_game.game, healthy.entries[0]),)
    )
    prior = _previous_manifest(
        manager_game,
        current,
        (
            holdet.RefreshManifestStep(
                "team:1",
                "team",
                "Hold: Et",
                "failed_no_cache",
                True,
                started_at=current - timedelta(minutes=2),
                completed_at=current - timedelta(minutes=1),
                team_id=1,
                error="offline",
            ),
            holdet.RefreshManifestStep(
                "postprocess",
                "postprocess",
                "Efterbehandling",
                "fetched",
                True,
                started_at=current - timedelta(minutes=2),
                completed_at=current - timedelta(minutes=1),
            ),
        ),
    )

    class Inbox:
        calls = 0

        def merge(self, _alerts):
            self.calls += 1
            raise AssertionError("Gammel spillertransition blev genafspillet")

    class TeamOnlyClient:
        def fetch_team(self, reference):
            return sample_team(reference.team_id, current_round=3)

        def fetch_players(self, _game):
            raise AssertionError("Spillere måtte ikke hentes")

    inbox = Inbox()
    with temporary_directory() as root:
        snapshots = holdet.SnapshotStore(root / "snapshots")
        players = holdet.PlayerStatisticsStore(root / "snapshots")
        snapshots.save_team_json(sample_team(1, current_round=3), now=current)
        players.save(healthy, now=current - timedelta(hours=2))
        players.save(adverse, now=current - timedelta(hours=1))
        plan = holdet.build_refresh_plan(
            manager_game,
            (group,),
            snapshots.scan(),
            players.scan(manager_game.game),
            mode=holdet.RefreshMode.RETRY_FAILED,
            previous_manifest=prior,
            include_metadata=False,
            include_postprocess=True,
            now=current,
        )
        result = holdet.refresh_manager_game(
            manager_game,
            (group,),
            TeamOnlyClient(),
            snapshots,
            players,
            holdet.ManifestStore(root / "manifests"),
            settings=settings,
            inbox_store=inbox,  # type: ignore[arg-type]
            plan=plan,
            postprocess=lambda: None,
            now=current,
        )

    assert [step.step_id for step in plan.selected_steps] == [
        "team:1",
        "postprocess",
    ]
    assert inbox.calls == 0
    assert result.manifest is not None
    postprocess_step = next(
        step for step in result.manifest.steps if step.step_id == "postprocess"
    )
    assert postprocess_step.status == "fetched"


def test_progress_observer_failure_cannot_change_refresh_outcomes() -> None:
    current = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager_game, group = _manager_game_and_group(1)

    class Client:
        def fetch_players(self, _game):
            return replace(sample_statistics(3), round_status="complete")

        def fetch_team(self, _reference):
            return sample_team(1, current_round=3)

    def broken_progress(_event) -> None:
        raise RuntimeError("UI-observer fejlede")

    with temporary_directory() as root:
        result = holdet.refresh_manager_game(
            manager_game,
            (group,),
            Client(),
            holdet.SnapshotStore(root / "snapshots"),
            holdet.PlayerStatisticsStore(root / "snapshots"),
            holdet.ManifestStore(root / "manifests"),
            progress=broken_progress,
            now=current,
        )

        assert result.player is not None and result.player.status == "success"
        assert [item.status for item in result.teams] == ["success"]
        assert result.manifest is not None
        assert [item.status for item in result.manifest.steps] == [
            "skipped_unavailable",
            "fetched",
            "fetched",
        ]


def test_refresh_age_uses_absolute_time_across_dst_gap_and_fold() -> None:
    copenhagen = ZoneInfo("Europe/Copenhagen")
    manager_game, group = _manager_game_and_group(1)
    cases = (
        (
            datetime(2026, 3, 29, 3, 0, tzinfo=copenhagen),
            datetime(2026, 3, 28, 2, 30, tzinfo=copenhagen),
            False,
        ),
        (
            datetime(2026, 10, 25, 2, 30, tzinfo=copenhagen, fold=1),
            datetime(2026, 10, 24, 3, 0, tzinfo=copenhagen),
            True,
        ),
    )
    for current, generated_at, expected_stale in cases:
        with temporary_directory() as root:
            snapshots = holdet.SnapshotStore(root / "snapshots")
            players = holdet.PlayerStatisticsStore(root / "snapshots")
            snapshots.save_team_json(
                sample_team(1, current_round=3), now=generated_at
            )
            players.save(
                replace(sample_statistics(3), round_status="complete"),
                now=generated_at,
            )
            plan = holdet.build_refresh_plan(
                manager_game,
                (group,),
                snapshots.scan(),
                players.scan(manager_game.game),
                metadata=_future_metadata(manager_game.game, current),
                mode=holdet.RefreshMode.STALE_ONLY,
                now=current,
            )
            selected = {item.step_id for item in plan.selected_steps}
            assert ("players" in selected) is expected_stale
            assert ("team:1" in selected) is expected_stale


def test_metadata_is_stale_when_cache_target_round_is_missing_from_schedule() -> None:
    current = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager_game, group = _manager_game_and_group(1)
    metadata = holdet.GameMetadata(
        game=manager_game.game,
        variant="cycling",
        format="classic",
        game_id=7,
        salary_cap=50_000_000,
        final_round=3,
        display_name="Tourspillet",
        rounds=(
            holdet.ScheduleRound(
                3,
                current - timedelta(days=1),
                current + timedelta(hours=1),
                current + timedelta(days=1),
            ),
        ),
        fetched_at=current,
    )
    with temporary_directory() as root:
        snapshots = holdet.SnapshotStore(root / "snapshots")
        players = holdet.PlayerStatisticsStore(root / "snapshots")
        snapshots.save_team_json(
            sample_team(1, current_round=4), now=current
        )
        players.save(
            replace(sample_statistics(4), round_status="complete"), now=current
        )
        plan = holdet.build_refresh_plan(
            manager_game,
            (group,),
            snapshots.scan(),
            players.scan(manager_game.game),
            metadata=metadata,
            mode=holdet.RefreshMode.STALE_ONLY,
            now=current,
        )
        assert [item.step_id for item in plan.selected_steps] == ["metadata"]
        assert "cacherunde 4" in plan.selected_steps[0].reason


def test_refresh_target_prefers_next_round_when_end_and_start_are_equal() -> None:
    boundary = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager_game, group = _manager_game_and_group(1)
    metadata = holdet.GameMetadata(
        game=manager_game.game,
        variant="cycling",
        format="classic",
        game_id=7,
        salary_cap=50_000_000,
        final_round=4,
        display_name="Tourspillet",
        rounds=(
            holdet.ScheduleRound(
                3,
                boundary - timedelta(days=2),
                boundary - timedelta(days=1),
                boundary,
            ),
            holdet.ScheduleRound(
                4,
                boundary,
                boundary + timedelta(hours=1),
                boundary + timedelta(days=1),
            ),
        ),
        fetched_at=boundary,
    )
    with temporary_directory() as root:
        snapshots = holdet.SnapshotStore(root / "snapshots")
        players = holdet.PlayerStatisticsStore(root / "snapshots")
        plan = holdet.build_refresh_plan(
            manager_game,
            (group,),
            snapshots.scan(),
            players.scan(manager_game.game),
            metadata=metadata,
            mode=holdet.RefreshMode.STALE_ONLY,
            now=boundary,
        )

    assert plan.target_round == 4
    metadata_step = next(step for step in plan.steps if step.step_id == "metadata")
    assert not metadata_step.selected


def test_recent_team_without_target_round_summary_is_stale() -> None:
    current = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager_game, group = _manager_game_and_group(1)
    with temporary_directory() as root:
        snapshots = holdet.SnapshotStore(root / "snapshots")
        players = holdet.PlayerStatisticsStore(root / "snapshots")
        snapshots.save_team_json(
            sample_team(1, current_round=2),
            now=current - timedelta(minutes=1),
        )
        players.save(
            replace(sample_statistics(3), round_status="complete"),
            now=current,
        )
        plan = holdet.build_refresh_plan(
            manager_game,
            (group,),
            snapshots.scan(),
            players.scan(manager_game.game),
            metadata=_future_metadata(manager_game.game, current),
            mode=holdet.RefreshMode.STALE_ONLY,
            now=current,
        )

    assert [step.step_id for step in plan.selected_steps] == ["team:1"]
    assert plan.selected_steps[0].reason == "Rundedata for runde 3 mangler"


def test_retry_marks_players_stale_when_target_round_is_missing() -> None:
    current = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager_game, group = _manager_game_and_group(1)
    failed = _previous_manifest(
        manager_game,
        current,
        (
            holdet.RefreshManifestStep(
                "players",
                "players",
                "Spillere",
                "failed_no_cache",
                True,
                completed_at=current - timedelta(minutes=1),
                round_number=3,
                error="offline",
            ),
        ),
    )
    with temporary_directory() as root:
        snapshots = holdet.SnapshotStore(root / "snapshots")
        players = holdet.PlayerStatisticsStore(root / "snapshots")
        snapshots.save_team_json(sample_team(1, current_round=3), now=current)
        players.save(
            replace(sample_statistics(4), round_status="complete"),
            now=current,
        )
        plan = holdet.build_refresh_plan(
            manager_game,
            (group,),
            snapshots.scan(),
            players.scan(manager_game.game),
            metadata=_future_metadata(manager_game.game, current),
            mode=holdet.RefreshMode.RETRY_FAILED,
            previous_manifest=failed,
            now=current,
        )

    assert [step.step_id for step in plan.selected_steps] == ["players"]
    assert plan.selected_steps[0].stale
    assert plan.selected_steps[0].reason == "Spillerdata for runde 3 mangler"


def test_manifest_scan_uses_run_id_as_equal_timestamp_tie_break() -> None:
    current = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    low_id = "00000000-0000-0000-0000-000000000001"
    high_id = "00000000-0000-0000-0000-000000000002"
    with temporary_directory() as root:
        store = holdet.ManifestStore(root / "manifests")
        store.write(
            replace(_manifest(current), run_id=high_id, origin_run_id=high_id)
        )
        store.write(
            replace(_manifest(current), run_id=low_id, origin_run_id=low_id)
        )
        values, warnings = store.scan("demo", game_locale="da")
        assert not warnings
        assert [item.run_id for item in values] == [high_id, low_id]


def test_manifest_write_failure_exposes_complete_retryable_manifest() -> None:
    current = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager_game, group = _manager_game_and_group(1)
    calls: list[str] = []

    class Client:
        def fetch_players(self, _game):
            calls.append("players")
            return replace(sample_statistics(3), round_status="complete")

        def fetch_team(self, reference):
            calls.append(f"team:{reference.team_id}")
            return sample_team(reference.team_id, current_round=3)

    class FailOnceManifestStore(holdet.ManifestStore):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.write_attempts = 0

        def write(self, manifest):
            self.write_attempts += 1
            if self.write_attempts == 1:
                raise OSError("manifestmappen er midlertidigt låst")
            return super().write(manifest)

    with temporary_directory() as root:
        manifests = FailOnceManifestStore(root / "manifests")
        with pytest.raises(holdet.ManifestWriteError) as caught:
            holdet.refresh_manager_game(
                manager_game,
                (group,),
                Client(),
                holdet.SnapshotStore(root / "snapshots"),
                holdet.PlayerStatisticsStore(root / "snapshots"),
                manifests,
                now=current,
            )

        manifest = caught.value.manifest
        assert manifest.path is None
        assert manifest.game_slug == manager_game.game.slug
        assert manifest.target_round == 3
        assert [step.status for step in manifest.steps] == [
            "skipped_unavailable",
            "fetched",
            "fetched",
        ]
        assert all(
            step.origin_run_id == manifest.run_id for step in manifest.steps
        )
        calls_after_refresh = tuple(calls)

        path = manifests.write(manifest)
        loaded = manifests.load(path)

        assert tuple(calls) == calls_after_refresh == ("players", "team:1")
        assert [step.status for step in loaded.steps] == [
            "skipped_unavailable",
            "fetched",
            "fetched",
        ]
        assert [step.origin_run_id for step in loaded.steps] == [
            manifest.run_id,
            manifest.run_id,
            manifest.run_id,
        ]
