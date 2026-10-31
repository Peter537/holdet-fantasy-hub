from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import holdet_lib as h
from holdet_lib.hub_settings import OwnTeamSelection
from holdet_lib.refresh import catch_up_manager_game
from tests.test_library_storage import sample_team, temporary_directory
from tests.test_player_statistics import sample_statistics
from tests.test_refresh_foundation import _manager_game_and_group, _previous_manifest


NOW = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)


@pytest.fixture
def tmp_path():
    with temporary_directory() as root:
        yield root


def metadata(game: h.GameUrl, *, fetched_at: datetime = NOW) -> h.GameMetadata:
    return h.GameMetadata(
        game, "cycling", "classic", 7, 50_000_000, 4, "Tourspillet",
        tuple(
            h.ScheduleRound(
                number, NOW - timedelta(days=5 - number),
                NOW - timedelta(days=5 - number, hours=-1),
                NOW - timedelta(days=4 - number),
            )
            for number in range(1, 5)
        ),
        fetched_at,
    )


class Client:
    def __init__(self, game: h.GameUrl) -> None:
        self.game = game
        self.calls: list[tuple[str, int | None]] = []
        self.failed_round: int | None = None
        self.wrong_round: int | None = None

    def fetch_game_info(self, _game):
        self.calls.append(("metadata", None))
        return metadata(self.game)

    def fetch_players(self, _game, *, round_number=None):
        self.calls.append(("players", round_number))
        if round_number is not None and round_number == self.failed_round:
            raise h.FetchError("Runden er offline")
        number = 4 if round_number is None else round_number
        if round_number is not None and round_number == self.wrong_round:
            number = 4
        return replace(sample_statistics(number), round_status="complete")

    def fetch_team(self, reference):
        self.calls.append(("team", reference.team_id))
        return sample_team(reference.team_id, current_round=4)


def stores(root: Path):
    return (
        h.SnapshotStore(root / "snapshots"),
        h.PlayerStatisticsStore(root / "snapshots"),
        h.ManifestStore(root / "manifests"),
        h.GameMetadataStore(root / "metadata"),
    )


def test_plan_backfills_missing_and_finalizes_past_preliminary_rounds(tmp_path):
    manager, group = _manager_game_and_group(1)
    teams, players, _, info = stores(tmp_path)
    info.save(metadata(manager.game))
    players.save(replace(sample_statistics(1), round_status="complete"), now=NOW)
    players.save(replace(sample_statistics(2), round_status="in_progress"), now=NOW)
    players.save(
        replace(sample_statistics(3), round_status="complete"),
        now=NOW - timedelta(days=2),
    )
    plan = h.build_refresh_plan(
        manager, (group,), teams.scan(), players.scan(manager.game),
        metadata=info.load(manager.game), catch_up=True,
        mode=h.RefreshMode.STALE_ONLY, now=NOW,
    )
    assert [step.requested_round for step in plan.selected_steps if step.requested_round] == [2, 3, 4]
    assert not next(step for step in plan.steps if step.step_id == "players:round:1").selected


def test_metadata_latest_then_ascending_history_deduplicates_current_round(tmp_path):
    manager, group = _manager_game_and_group(1)
    teams, players, manifests, info = stores(tmp_path)
    info.save(metadata(manager.game, fetched_at=NOW - timedelta(days=2)))
    client = Client(manager.game)
    events = []
    result = catch_up_manager_game(
        manager, (group,), client, teams, players, manifests,
        metadata_store=info, progress=events.append, now=NOW,
    )
    assert client.calls == [
        ("metadata", None), ("players", None),
        ("players", 1), ("players", 2), ("players", 3), ("team", 1),
    ]
    assert players.scan(manager.game).rounds_for(manager.game) == (4, 3, 2, 1)
    assert result.manifest is not None and result.manifest.schema_version == 3
    assert result.manifest.result == "complete"
    assert not result.warnings
    restored = manifests.load(result.manifest_path)
    assert next(step for step in restored.steps if step.step_id == "players:round:2").requested_round == 2
    assert [event.step.requested_round for event in events if event.status == "running" and event.step.requested_round] == [1, 2, 3]
    assert [item.round_number for item in result.historical_players] == [1, 2, 3]


def test_failed_historical_round_uses_only_its_own_cache_and_retry_is_exact(tmp_path):
    manager, group = _manager_game_and_group(1)
    teams, players, manifests, info = stores(tmp_path)
    info.save(metadata(manager.game))
    players.save(replace(sample_statistics(2), round_status="in_progress"), now=NOW - timedelta(days=2))
    client = Client(manager.game)
    client.failed_round = 2
    result = catch_up_manager_game(
        manager, (group,), client, teams, players, manifests,
        metadata_store=info, now=NOW,
    )
    assert result.manifest is not None
    failed = next(step for step in result.manifest.failures if step.step_id == "players:round:2")
    assert failed.status == "reused_after_error" and failed.round_number == 2
    assert players.scan(manager.game).newest(manager.game, 3) is not None
    plan = h.build_refresh_plan(
        manager, (group,), teams.scan(), players.scan(manager.game),
        metadata=info.load(manager.game), mode=h.RefreshMode.RETRY_FAILED,
        previous_manifest=result.manifest, catch_up=True, now=NOW,
    )
    client.calls.clear()
    client.failed_round = None
    retried = catch_up_manager_game(
        manager, (group,), client, teams, players, manifests,
        metadata_store=info, plan=plan, now=NOW + timedelta(minutes=1),
    )
    assert client.calls == [("players", 2)]
    assert retried.manifest is not None and not retried.manifest.failures
    assert players.scan(manager.game).newest(manager.game, 2).statistics.round_status == "complete"


def test_wrong_historical_round_is_rejected_without_stopping_later_sources(tmp_path):
    manager, group = _manager_game_and_group(1)
    teams, players, manifests, info = stores(tmp_path)
    info.save(metadata(manager.game))
    client = Client(manager.game)
    client.wrong_round = 2
    result = catch_up_manager_game(
        manager, (group,), client, teams, players, manifests,
        metadata_store=info, now=NOW,
    )
    assert players.scan(manager.game).newest(manager.game, 2) is None
    assert ("team", 1) in client.calls
    assert result.manifest is not None and result.manifest.result == "partial"
    failure = next(step for step in result.manifest.failures if step.requested_round == 2)
    assert "i stedet for runde 2" in failure.error


def test_cancel_preserves_completed_sources_and_records_retryable_remainder(tmp_path):
    manager, group = _manager_game_and_group(1)
    teams, players, manifests, info = stores(tmp_path)
    info.save(metadata(manager.game))
    client = Client(manager.game)
    result = catch_up_manager_game(
        manager, (group,), client, teams, players, manifests,
        metadata_store=info, cancelled=lambda: len(client.calls) >= 1,
        postprocess=lambda: pytest.fail("cancelled postprocess must not run"), now=NOW,
    )
    assert client.calls == [("players", None)]
    assert result.cancelled
    assert result.attempted_team_ids == ()
    assert result.skipped_team_ids == (1,)
    assert players.scan(manager.game).newest(manager.game, 4) is not None
    manifest = manifests.load(result.manifest_path)
    assert manifest.failures
    assert all(step.status == "cancelled" and not step.attempted for step in manifest.failures)
    assert {step.requested_round for step in manifest.failures if step.source == "players"} == {1, 2, 3}
    retry = h.build_refresh_plan(
        manager, (group,), teams.scan(), players.scan(manager.game),
        metadata=info.load(manager.game), mode=h.RefreshMode.RETRY_FAILED,
        previous_manifest=manifest, catch_up=True, include_postprocess=True, now=NOW,
    )
    client.calls.clear()
    retried = catch_up_manager_game(
        manager, (group,), client, teams, players, manifests,
        metadata_store=info, plan=retry, postprocess=lambda: None, now=NOW,
    )
    assert not retried.cancelled and ("players", None) not in client.calls
    assert retried.manifest is not None and not retried.manifest.failures


def test_own_team_is_included_only_in_game_scope_and_archive_is_rejected(tmp_path):
    manager, group = _manager_game_and_group(1)
    teams, players, manifests, info = stores(tmp_path)
    settings = h.HubSettings(own_teams=(OwnTeamSelection(manager.game.locale, manager.game.slug, 9),))
    plan = h.build_refresh_plan(
        manager, (group,), teams.scan(), players.scan(manager.game), settings=settings,
        catch_up=True, competition_id=group.group_id, now=NOW,
    )
    assert plan.attempted_team_ids == (1,)
    client = Client(manager.game)
    result = catch_up_manager_game(
        manager, (group,), client, teams, players, manifests, settings=settings, now=NOW,
    )
    assert result.attempted_team_ids == (1, 9)
    with pytest.raises(ValueError, match="arkiveret"):
        catch_up_manager_game(
            replace(manager, archived_at=NOW.isoformat()), (group,), client,
            teams, players, manifests, settings=settings, now=NOW,
        )
    assert result.warnings


def test_disabled_alerts_and_historical_backfill_do_not_replay_notifications(tmp_path):
    manager, group = _manager_game_and_group(1)
    teams, players, manifests, info = stores(tmp_path)
    info.save(metadata(manager.game))

    class Inbox:
        def merge(self, _alerts):
            pytest.fail("disabled alerts must not write the inbox")

    settings = h.HubSettings(watchlist=(h.watchlist_entry(manager.game, sample_statistics().entries[0]),))
    result = catch_up_manager_game(
        manager, (group,), Client(manager.game), teams, players, manifests,
        metadata_store=info, settings=settings, inbox_store=Inbox(),
        alerts_enabled=True, now=NOW,
    )
    assert result.manifest is not None and result.manifest.result == "complete"
    assert all(item.alert_count == 0 for item in result.historical_players)


def test_original_player_retry_requests_original_round_after_source_advances(tmp_path):
    manager, group = _manager_game_and_group(1)
    teams, players, manifests, _ = stores(tmp_path)
    previous = _previous_manifest(manager, NOW, (
        h.RefreshManifestStep(
            "players", "players", "Spillere", "failed_no_cache", True,
            round_number=3, completed_at=NOW - timedelta(minutes=1), error="offline",
        ),
    ))
    plan = h.build_refresh_plan(
        manager, (group,), teams.scan(), players.scan(manager.game),
        mode=h.RefreshMode.RETRY_FAILED, previous_manifest=previous, now=NOW,
    )
    client = Client(manager.game)
    result = h.refresh_manager_game(
        manager, (group,), client, teams, players, manifests, plan=plan, now=NOW,
    )
    assert client.calls == [("players", 3)]
    assert players.scan(manager.game).newest(manager.game, 3) is not None
    assert result.manifest is not None and result.manifest.target_round == 3
    assert not result.manifest.failures


def _failed_latest_with_older_cache(root):
    manager, group = _manager_game_and_group(1)
    teams, players, manifests, info = stores(root)
    schedule = metadata(manager.game)
    info.save(replace(schedule, rounds=tuple(
        replace(item, close=NOW + timedelta(hours=1), end=NOW + timedelta(days=1))
        if item.round_number == 4 else item
        for item in schedule.rounds
    )))
    teams.save_team_json(sample_team(1, current_round=4), now=NOW)
    for number in (1, 2, 3):
        players.save(
            replace(sample_statistics(number), round_status="complete"),
            now=NOW - timedelta(hours=1),
        )

    class Offline(Client):
        def fetch_players(self, _game, *, round_number=None):
            self.calls.append(("players", round_number))
            raise h.FetchError("Seneste spillerrunde er offline")

    failed = catch_up_manager_game(
        manager, (group,), Offline(manager.game), teams, players, manifests,
        metadata_store=info, now=NOW,
    )
    return manager, group, teams, players, manifests, info, failed


def test_failed_latest_records_intended_request_separately_from_cached_round(tmp_path):
    *_, result = _failed_latest_with_older_cache(tmp_path)
    assert result.manifest.target_round == 4
    outcome = next(step for step in result.manifest.steps if step.step_id == "players")
    assert outcome.status == "reused_after_error" and outcome.round_number == 3
    assert outcome.requested_round == 4


@pytest.mark.parametrize("known_round_zero", (False, True))
def test_failed_latest_distinguishes_unknown_target_from_real_round_zero(tmp_path, known_round_zero):
    manager, _ = _manager_game_and_group(1)
    teams, players, manifests, info = stores(tmp_path)
    if known_round_zero:
        info.save(replace(metadata(manager.game), final_round=0, rounds=(h.ScheduleRound(
            0, NOW - timedelta(hours=1), NOW + timedelta(hours=1), NOW + timedelta(days=1),
        ),)))

    class Offline(Client):
        def fetch_players(self, _game, *, round_number=None):
            raise h.FetchError("Offline uden kendt runde")

    result = catch_up_manager_game(
        manager, (), Offline(manager.game), teams, players, manifests,
        metadata_store=info if known_round_zero else None, now=NOW,
    )
    outcome = next(step for step in result.manifest.steps if step.step_id == "players")
    assert result.manifest.target_round == 0
    assert outcome.requested_round == (0 if known_round_zero else None)


@pytest.mark.parametrize("schema_version", (None, 2, 3))
@pytest.mark.parametrize("mismatched_response", (False, True))
def test_latest_retry_uses_original_target_and_rejects_wrong_round(
    tmp_path, schema_version, mismatched_response,
):
    manager, group, teams, players, manifests, info, failed = _failed_latest_with_older_cache(tmp_path)
    if schema_version is None:
        previous = manifests.load(failed.manifest_path)
    else:
        legacy = replace(
            failed.manifest, schema_version=schema_version, path=None,
            steps=tuple(replace(step, requested_round=None) for step in failed.manifest.steps),
        )
        legacy_store = h.ManifestStore(tmp_path / "legacy-manifests")
        previous = legacy_store.load(legacy_store.write(legacy))
    plan = h.build_refresh_plan(
        manager, (group,), teams.scan(), players.scan(manager.game),
        metadata=info.load(manager.game), mode=h.RefreshMode.RETRY_FAILED,
        previous_manifest=previous, catch_up=True, now=NOW,
    )

    class AdvancedClient(Client):
        def fetch_players(self, _game, *, round_number=None):
            self.calls.append(("players", round_number))
            returned = 3 if mismatched_response else 6 if round_number is None else round_number
            return replace(sample_statistics(returned), round_status="complete")

    client = AdvancedClient(manager.game)
    result = catch_up_manager_game(
        manager, (group,), client, teams, players, manifests,
        metadata_store=info, plan=plan, now=NOW + timedelta(minutes=1),
    )
    assert client.calls == [("players", 4)]
    assert result.manifest.target_round == 4
    assert players.scan(manager.game).newest(manager.game, 6) is None
    if mismatched_response:
        assert players.scan(manager.game).newest(manager.game, 4) is None
        assert result.manifest.result == "partial"
        outcome = next(step for step in result.manifest.failures if step.step_id == "players")
        assert outcome.requested_round == 4 and "i stedet for runde 4" in outcome.error
    else:
        assert players.scan(manager.game).newest(manager.game, 4) is not None
        assert result.manifest.result == "complete"


def test_enabled_alerts_use_pre_run_current_baseline_not_new_history(tmp_path, monkeypatch):
    manager, group = _manager_game_and_group(1)
    teams, players, manifests, info = stores(tmp_path)
    info.save(metadata(manager.game))
    players.save(
        replace(sample_statistics(4), round_status="complete"),
        now=NOW - timedelta(hours=1),
    )
    players.save(
        replace(sample_statistics(3), round_status="complete"),
        now=NOW - timedelta(minutes=30),
    )
    observed = []

    def alerts(previous, current, *_args, **_kwargs):
        observed.append((previous.statistics.round_number, current.statistics.round_number))
        return ()

    class Inbox:
        def merge(self, _alerts):
            pass

    monkeypatch.setattr("holdet_lib.refresh.build_watchlist_alerts", alerts)
    settings = h.HubSettings(enabled_features=("alerts",))
    result = catch_up_manager_game(
        manager, (group,), Client(manager.game), teams, players, manifests,
        metadata_store=info, settings=settings, inbox_store=Inbox(),
        alerts_enabled=True, now=NOW,
    )
    assert result.manifest is not None and not result.manifest.failures
    assert observed == [(4, 4)]


def test_backfill_alone_does_not_evaluate_alerts_even_when_enabled(tmp_path, monkeypatch):
    manager, group = _manager_game_and_group(1)
    teams, players, manifests, info = stores(tmp_path)
    info.save(metadata(manager.game))
    players.save(replace(sample_statistics(4), round_status="complete"), now=NOW)
    monkeypatch.setattr(
        "holdet_lib.refresh.build_watchlist_alerts",
        lambda *_args, **_kwargs: pytest.fail("historical backfill evaluated alerts"),
    )
    settings = h.HubSettings(enabled_features=("alerts",))
    client = Client(manager.game)
    result = catch_up_manager_game(
        manager, (group,), client, teams, players, manifests,
        metadata_store=info, settings=settings,
        inbox_store=h.AnalysisInboxStore(tmp_path / "inbox.json"),
        alerts_enabled=True, now=NOW,
    )
    assert ("players", None) not in client.calls
    assert result.manifest is not None and not result.manifest.failures
    assert not (tmp_path / "inbox.json").exists()


def test_retry_metadata_discovers_history_that_original_run_could_not_plan(tmp_path):
    manager, group = _manager_game_and_group(1)
    teams, players, manifests, info = stores(tmp_path)

    class MetadataOffline(Client):
        def fetch_game_info(self, _game):
            raise h.FetchError("Tidsplanen er offline")

    first = catch_up_manager_game(
        manager, (group,), MetadataOffline(manager.game), teams, players, manifests,
        metadata_store=info, now=NOW,
    )
    assert first.warnings
    retry = h.build_refresh_plan(
        manager, (group,), teams.scan(), players.scan(manager.game),
        mode=h.RefreshMode.RETRY_FAILED, previous_manifest=first.manifest,
        catch_up=True, now=NOW,
    )
    client = Client(manager.game)
    result = catch_up_manager_game(
        manager, (group,), client, teams, players, manifests,
        metadata_store=info, plan=retry, now=NOW,
    )
    assert client.calls == [("metadata", None), ("players", 1), ("players", 2), ("players", 3)]
    assert result.manifest is not None and not result.manifest.failures
    assert not result.warnings


def test_competition_manifest_and_retry_cannot_escape_their_scope(tmp_path):
    manager, group = _manager_game_and_group(1)
    teams, players, manifests, info = stores(tmp_path)
    info.save(metadata(manager.game))
    client = Client(manager.game)
    client.failed_round = 2
    plan = h.build_refresh_plan(
        manager, (group,), teams.scan(), players.scan(manager.game),
        metadata=info.load(manager.game), catch_up=True,
        competition_id=group.group_id, now=NOW,
    )
    result = catch_up_manager_game(
        manager, (group,), client, teams, players, manifests,
        metadata_store=info, plan=plan, now=NOW,
    )
    stored = manifests.load_latest(
        manager.game.slug, game_locale=manager.game.locale,
        scope="group", group_id=group.group_id,
    )
    assert stored is not None and stored.run_id == result.manifest.run_id
    assert stored.scope == "group" and stored.group_id == group.group_id
    retry = h.build_refresh_plan(
        manager, (group,), teams.scan(), players.scan(manager.game),
        metadata=info.load(manager.game), catch_up=True,
        mode=h.RefreshMode.RETRY_FAILED, previous_manifest=stored,
        competition_id=group.group_id, now=NOW,
    )
    assert [step.requested_round for step in retry.selected_steps] == [2]
    other = replace(group, group_id="other")
    with pytest.raises(ValueError, match="Manifestet"):
        h.build_refresh_plan(
            manager, (other,), teams.scan(), players.scan(manager.game),
            catch_up=True, mode=h.RefreshMode.RETRY_FAILED,
            previous_manifest=stored, competition_id=other.group_id, now=NOW,
        )
