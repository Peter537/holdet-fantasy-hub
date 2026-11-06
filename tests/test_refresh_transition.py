from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import shutil

import pytest

import holdet_lib as h
from holdet_lib.refresh_transition import PlayerTransition, RecoveryCheckpointError
from tests.test_library_storage import sample_team, temporary_directory
from tests.test_player_statistics import sample_statistics
from tests.test_refresh_foundation import _manager_game_and_group


def test_staged_snapshot_is_republished_idempotently_after_checkpoint_failure():
    with temporary_directory() as root:
        game = sample_team(1).reference.game
        players = h.PlayerStatisticsStore(root / "snapshots")
        saved = []
        transition = PlayerTransition(root, str(uuid4()), game, players, None, lambda value: saved.append(value.copy()))
        transition.prepare(())
        transition.stage(sample_statistics(3), datetime.now(timezone.utc))
        def fail(_value):
            raise OSError("journal unavailable")
        transition.save = fail
        with pytest.raises(RecoveryCheckpointError):
            transition.publish()
        restored = PlayerTransition(root, transition.run_id, game, players, saved[-1], lambda value: saved.append(value.copy()))
        _, first = restored.publish()
        _, second = restored.publish()
        assert first == second
        assert len(players.scan(game).for_game(game)) == 1


@pytest.mark.parametrize("durable", [False, True])
def test_cancelled_postprocess_replays_original_player_transition(monkeypatch, durable):
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager, group = _manager_game_and_group(1)
    with temporary_directory() as root:
        teams = h.SnapshotStore(root / "snapshots")
        players = h.PlayerStatisticsStore(root / "snapshots")
        manifests = h.ManifestStore(root / "manifests")
        baseline = players.save(sample_statistics(3), now=now - timedelta(hours=1))
        settings = h.HubSettings(enabled_features=("alerts",))
        observed = []
        monkeypatch.setattr("holdet_lib.refresh.build_watchlist_alerts",
                            lambda previous, current, *_args, **_kwargs: observed.append((previous.path, current.path)) or ())
        class Inbox:
            def merge(self, _alerts):
                pass
        class Client:
            calls = 0
            def fetch_players(self, _game):
                self.calls += 1
                return sample_statistics(3)
        client = Client()
        saved = []
        transition = (PlayerTransition(root, str(uuid4()), manager.game, players, None, lambda value: saved.append(value.copy()))
                      if durable else None)
        plan = h.RefreshPlan(manager, h.RefreshMode.ALL, now, 3,
            (h.RefreshStep("players", "players", "Spillere", True, True, "Hent"),
             h.RefreshStep("postprocess", "postprocess", "Efterbehandling", True, True, "Hent")),
            include_postprocess=True)
        first = h.refresh_manager_game(manager, (group,), client, teams, players, manifests,
            settings=settings, inbox_store=Inbox(), plan=plan,
            cancelled=lambda: client.calls > 0, now=now, player_transition=transition)
        assert first.cancelled and observed == []
        retry = h.build_refresh_plan(manager, (group,), teams.scan(), players.scan(manager.game),
            mode=h.RefreshMode.RETRY_FAILED, previous_manifest=first.manifest,
            include_metadata=False, include_postprocess=True, now=now + timedelta(minutes=1))
        restored = (PlayerTransition(root, transition.run_id, manager.game, players, saved[-1], lambda value: saved.append(value.copy()))
                    if transition is not None else None)
        result = h.refresh_manager_game(manager, (group,), client, teams, players, manifests,
            settings=settings, inbox_store=Inbox(), plan=retry, now=now + timedelta(minutes=1), player_transition=restored)
        assert not result.manifest.failures
        assert client.calls == 1
        current = next(step.data_reference for step in first.manifest.steps if step.step_id == "players")
        assert observed == [(baseline.resolve(), (root / current).resolve())]
        if restored is not None:
            assert restored.record["alerts_complete"]


def test_transition_rejects_paths_outside_run():
    with temporary_directory() as root:
        game = sample_team(1).reference.game
        players = h.PlayerStatisticsStore(root / "snapshots")
        transition = PlayerTransition(root, str(uuid4()), game, players, None, lambda _value: None)
        transition.prepare(())
        value = {**transition.record, "stage_path": "../outside.json"}
        with pytest.raises(h.PayloadError):
            PlayerTransition(root, transition.run_id, game, players, value, lambda _value: None)


def test_transition_survives_relocation_and_rejects_wrong_round():
    with temporary_directory() as root:
        source = root / "source"
        game = sample_team(1).reference.game
        players = h.PlayerStatisticsStore(source / "snapshots")
        saved = []
        transition = PlayerTransition(source, str(uuid4()), game, players, None, lambda value: saved.append(value.copy()))
        transition.prepare((), requested_round=3)
        transition.stage(sample_statistics(3), datetime.now(timezone.utc))
        destination = root / "restored"
        shutil.copytree(source, destination)
        restored = PlayerTransition(destination, transition.run_id, game,
            h.PlayerStatisticsStore(destination / "snapshots"), saved[-1], lambda _value: None)
        with pytest.raises(h.PayloadError):
            restored.publish(expected_round=4)
        assert restored.publish(expected_round=3)[1].is_relative_to(destination)


def test_corrupt_stage_is_not_published():
    with temporary_directory() as root:
        game = sample_team(1).reference.game
        players = h.PlayerStatisticsStore(root / "snapshots")
        transition = PlayerTransition(root, str(uuid4()), game, players, None, lambda _value: None)
        transition.prepare(())
        stage = root / transition.record["stage_path"]
        stage.parent.mkdir(parents=True)
        stage.write_text('{"generated_at": "not a date"}', encoding="utf-8")
        with pytest.raises(h.PayloadError):
            transition.publish()
        assert not players.scan(game).snapshots


def test_failed_transition_intent_stops_before_player_fetch():
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager, group = _manager_game_and_group(1)
    with temporary_directory() as root:
        teams = h.SnapshotStore(root / "snapshots")
        players = h.PlayerStatisticsStore(root / "snapshots")
        manifests = h.ManifestStore(root / "manifests")
        def fail(_value):
            raise OSError("journal unavailable")
        transition = PlayerTransition(root, str(uuid4()), manager.game, players, None, fail)
        class Client:
            def fetch_players(self, _game):
                pytest.fail("network started before durable intent")
        plan = h.RefreshPlan(manager, h.RefreshMode.ALL, now, 3,
            (h.RefreshStep("players", "players", "Spillere", True, True, "Hent"),))
        with pytest.raises(RecoveryCheckpointError):
            h.refresh_manager_game(manager, (group,), Client(), teams, players, manifests,
                settings=h.HubSettings(enabled_features=("alerts",)), plan=plan,
                now=now, player_transition=transition)


@pytest.mark.parametrize("checkpoint_published", (False, True))
def test_recovery_publication_is_stable_when_the_local_timezone_changes(
    monkeypatch, checkpoint_published
):
    import holdet_lib.filenames as filenames

    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    with temporary_directory() as root:
        game = sample_team(1).reference.game
        players = h.PlayerStatisticsStore(root / "snapshots")
        saved = []
        transition = PlayerTransition(root, str(uuid4()), game, players, None,
            lambda value: saved.append(value.copy()))
        transition.prepare(())
        transition.stage(sample_statistics(3), now)
        monkeypatch.setattr(filenames, "compact_local_timestamp", lambda value:
            value.astimezone(timezone(timedelta(hours=2))).strftime("%m%d_%H%M%S"))
        if checkpoint_published:
            _, original = transition.publish()
        else:
            def fail(_value):
                raise OSError("journal unavailable")
            transition.save = fail
            with pytest.raises(RecoveryCheckpointError):
                transition.publish()
            original = players.scan(game).snapshots[0].path
        monkeypatch.setattr(filenames, "compact_local_timestamp", lambda value:
            value.astimezone(timezone(timedelta(hours=-5))).strftime("%m%d_%H%M%S"))

        restored = PlayerTransition(root, transition.run_id, game, players, saved[-1],
            lambda _value: None)
        assert restored.publish()[1] == original
        assert len(players.scan(game).snapshots) == 1


def test_recovery_reuses_a_valid_preexisting_checkpoint_filename(monkeypatch):
    import holdet_lib.filenames as filenames

    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(filenames, "compact_local_timestamp", lambda value:
        value.astimezone(timezone(timedelta(hours=2))).strftime("%m%d_%H%M%S"))
    with temporary_directory() as root:
        game = sample_team(1).reference.game
        players = h.PlayerStatisticsStore(root / "snapshots")
        transition = PlayerTransition(root, str(uuid4()), game, players, None, lambda _value: None)
        transition.prepare(())
        transition.stage(sample_statistics(3), now)
        _, original = transition.publish()
        legacy = original.with_name(f"player-round3_0809_070000_recovery-{transition.record['attempt_id']}.json")
        original.rename(legacy)
        record = {**transition.record, "snapshot_path": legacy.relative_to(root).as_posix()}

        restored = PlayerTransition(root, transition.run_id, game, players, record, lambda _value: None)
        assert restored.publish()[1] == legacy
        assert len(players.scan(game).snapshots) == 1


@pytest.mark.parametrize("mismatch", ("round", "attempt", "directory"))
def test_recovery_rejects_a_checkpoint_filename_for_another_observation(mismatch):
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    with temporary_directory() as root:
        game = sample_team(1).reference.game
        players = h.PlayerStatisticsStore(root / "snapshots")
        transition = PlayerTransition(root, str(uuid4()), game, players, None, lambda _value: None)
        transition.prepare(())
        transition.stage(sample_statistics(3), now)
        _, original = transition.publish()
        name = (
            f"player-round4_recovery-{transition.record['attempt_id']}.json"
            if mismatch == "round" else f"player-round3_recovery-{uuid4()}.json"
        )
        target = (
            original.parent / "nested" / original.name
            if mismatch == "directory" else original.with_name(name)
        )
        record = {**transition.record,
            "snapshot_path": target.relative_to(root).as_posix()}
        restored = PlayerTransition(root, transition.run_id, game, players, record, lambda _value: None)

        with pytest.raises(h.PayloadError, match="snapshot stemmer ikke"):
            restored.publish()
        assert len(players.scan(game).snapshots) == 1
        assert original.is_file()


@pytest.mark.parametrize("intervening_history", (False, True))
def test_durable_form_alert_uses_only_recorded_history_and_current_output(intervening_history):
    now = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    manager, group = _manager_game_and_group(1)

    def statistics(round_number, growth):
        value = sample_statistics(round_number)
        return replace(value, round_status="complete",
            entries=(replace(value.entries[0], round_growth=growth),))

    with temporary_directory() as root:
        teams = h.SnapshotStore(root / "snapshots")
        players = h.PlayerStatisticsStore(root / "snapshots")
        manifests = h.ManifestStore(root / "manifests")
        inbox = h.AnalysisInboxStore(root / "config" / "analysis-inbox.json")
        for round_number in (1, 2, 3):
            players.save(statistics(round_number, 0), now=now - timedelta(hours=4 - round_number))
        watched = replace(h.watchlist_entry(manager.game, statistics(3, 0).entries[0]),
            rules=(h.WatchRule("form", "form3_above", 20),))
        settings = h.HubSettings(enabled_features=("alerts",), watchlist=(watched,))
        transition = PlayerTransition(root, str(uuid4()), manager.game, players, None, lambda _value: None)
        transition.prepare(players.scan(manager.game).for_game(manager.game), settings=settings)
        transition.stage(statistics(4, 90), now)
        transition.publish()
        if intervening_history:
            # An imported older capture arrives after interruption but was absent
            # from the original transition's recorded history.
            players.save(statistics(2, -300), now=now - timedelta(minutes=30))
        plan = h.RefreshPlan(manager, h.RefreshMode.ALL, now, 4,
            (h.RefreshStep("postprocess", "postprocess", "Efterbehandling", True, True, "Hent"),),
            include_postprocess=True)

        result = h.refresh_manager_game(manager, (group,), object(), teams, players, manifests,
            settings=settings, inbox_store=inbox, plan=plan, now=now + timedelta(minutes=1),
            player_transition=transition)
        assert not result.manifest.failures
        alerts = inbox.load()
        assert len(alerts) == 1
        assert alerts[0].transition == "0.00 → 30.00"
        assert transition.record["alerts_complete"]
