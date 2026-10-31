from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import holdet_lib as holdet
import holdet_lib.refresh as refresh
from holdet_lib.refresh import RefreshPlan, RefreshStep, RefreshMode
from holdet_lib.refresh_runs import RefreshRunResult
from tests.test_library_storage import sample_team, temporary_directory
from website import refresh_service


def make_paths(root):
    return holdet.resolve_paths(overrides=holdet.PathOverrides(data_root=root), environ={})


def plan_for(manager, *args, **kwargs):
    return RefreshPlan(manager, RefreshMode.STALE_ONLY, datetime.now(timezone.utc), 3,
        (RefreshStep("players", "players", "Spillere", True, True, "Mangler"),))


def test_scope_archives_guard_and_worker_resources(monkeypatch):
    with temporary_directory() as root:
        paths = make_paths(root)
        team = sample_team(1)
        store = holdet.GroupStore(paths.groups_file, paths.group_revision_dir)
        manager = store.create_manager_game(team.reference.game, "Spil")
        first = store.create("Første", manager.game,
            (holdet.GroupTeam(1, team.team_name, team.reference.source_url),), group_id="first")
        store.create("Anden", manager.game,
            (holdet.GroupTeam(2, "Anden", team.reference.source_url.replace("/1", "/2")),), group_id="second")
        paths_seen = []
        caught = {}
        def build(manager, groups, team_index, players, **kwargs):
            caught["groups"] = groups
            caught["competition_id"] = kwargs["competition_id"]
            return plan_for(manager)
        def client_factory():
            paths_seen.append("client")
            return object()
        def catchup(manager, groups, client, snapshots, players, manifests, **kwargs):
            assert paths_seen == ["client"]
            assert snapshots.output_dir == paths.snapshot_dir
            caught["alerts_enabled"] = kwargs["alerts_enabled"]
            kwargs["progress"](SimpleNamespace(step=SimpleNamespace(step_id="players", label="Spillere"), status="running", completed_steps=0, total_steps=1))
            kwargs["postprocess"]()
            return SimpleNamespace(cancelled=False, manifest=SimpleNamespace(failures=()), manifest_path=paths.manifest_dir / "child.json", warnings=())
        monkeypatch.setattr(refresh_service, "build_refresh_plan", build)
        monkeypatch.setattr(refresh, "catch_up_manager_game", catchup, raising=False)
        coordinator = refresh_service.create_refresh_coordinator(paths, client_factory=client_factory)
        assert not (paths.data_dir / "refresh-runs").exists()
        events = []
        result = coordinator.action({"key":"da:tour", "locale":"da", "game":manager.game.slug, "competition_id":"first"},
            cancelled=lambda: False, progress=events.append, checkpoint={"run_id":"00000000-0000-0000-0000-000000000001", "manifest_paths":[]})
        assert result.status == "success"
        assert tuple(group.group_id for group in caught["groups"]) == (first.group_id,)
        assert caught["competition_id"] == "first"
        assert caught["alerts_enabled"] is False
        assert events[0]["source"] == "players"
        store.archive_manager_game(manager.game)
        with pytest.raises(holdet.PayloadError, match="skrivebeskyttede"):
            coordinator.action({"key":"da:tour", "locale":"da", "game":manager.game.slug},
                cancelled=lambda: False, progress=events.append, checkpoint={"run_id":"00000000-0000-0000-0000-000000000001", "manifest_paths":[]})
        assert paths_seen == ["client"]


@pytest.mark.parametrize("competition_scope", (False, True))
@pytest.mark.parametrize("force", (False, True))
def test_new_run_uses_latest_scope_failure_without_becoming_retry(competition_scope, force):
    from unittest.mock import patch
    from uuid import uuid4
    from tests.test_player_statistics import sample_statistics
    from tests.test_refresh_foundation import _future_metadata, _previous_manifest

    with temporary_directory() as root:
        now = datetime.now(timezone.utc)
        paths = make_paths(root)
        team = sample_team(1)
        config = holdet.GroupStore(paths.groups_file, paths.group_revision_dir)
        manager = config.create_manager_game(team.reference.game, "Spil")
        group = config.create("Liga", manager.game,
            (holdet.GroupTeam(1, team.team_name, team.reference.source_url),), group_id="league")
        holdet.SnapshotStore(paths.snapshot_dir).save_team_json(team, now=now)
        holdet.PlayerStatisticsStore(paths.snapshot_dir).save(sample_statistics(3), now=now)
        holdet.GameMetadataStore(paths.game_metadata_dir).save(_future_metadata(manager.game, now))
        previous = _previous_manifest(manager, now, (
            holdet.RefreshManifestStep("postprocess", "postprocess", "Efterbehandling",
                "failed_no_cache", True, started_at=now, completed_at=now,
                error="pairing disk failure"),
        ))
        if competition_scope:
            previous = replace(previous, scope="group", group_id=group.group_id)
        manifests = holdet.ManifestStore(paths.manifest_dir)
        manifests.write(previous)
        # A newer failure from a different scope must not replace this run's context.
        foreign = replace(previous, run_id=str(uuid4()),
                          scope="game" if competition_scope else "group",
                          group_id=None if competition_scope else group.group_id)
        manifests.write(foreign)
        task = {"key": "probe", "locale": manager.game.locale, "game": manager.game.slug,
                "competition_id": group.group_id if competition_scope else None, "force": force}
        calls = []

        class Client:
            def fetch_game_info(self, game):
                calls.append("metadata")
                return _future_metadata(game, now)

            def fetch_players(self, game, *, round_number=None):
                calls.append("players")
                return sample_statistics(round_number or 3)

            def fetch_team(self, reference):
                calls.append("team")
                return sample_team(reference.team_id)

        with patch.object(refresh_service, "_postprocess") as postprocess:
            result = refresh_service.create_refresh_coordinator(paths, client_factory=Client).action(
                task, cancelled=lambda: False, progress=lambda event: None,
                checkpoint={"run_id": str(uuid4()), "manifest_paths": []},
            )
        postprocess.assert_called_once()
        assert len(result.manifest_paths) == 1
        child = manifests.load(paths.data_dir / result.manifest_paths[0])
        assert child.mode == (RefreshMode.ALL if force else RefreshMode.STALE_ONLY)
        assert child.retry_of is None
        assert child.scope == previous.scope and child.group_id == previous.group_id
        assert next(step for step in child.steps if step.source == "postprocess").status == "fetched"
        assert result.status == "success"
        if not force:
            assert calls == []
            assert not any(step.attempted for step in child.steps if step.source != "postprocess")
        else:
            assert set(calls) == {"metadata", "players", "team"}


def test_legacy_tournament_baseline_is_saved_before_client_creation(monkeypatch):
    with temporary_directory() as root:
        paths = make_paths(root)
        teams = (sample_team(1, current_round=3), sample_team(2, current_round=3))
        store = holdet.GroupStore(paths.groups_file, paths.group_revision_dir)
        manager = store.create_manager_game(teams[0].reference.game, "Spil")
        group = store.create_tournament("Cup", manager.game,
            tuple(holdet.GroupTeam(t.reference.team_id, t.team_name, t.reference.source_url) for t in teams),
            start_round=1, final_round=3, rounds_per_tie=1,
            group_id="cup", shuffle=lambda values: None)
        for team in teams:
            holdet.SnapshotStore(paths.snapshot_dir).save_team_json(team)
        pairings = holdet.TournamentPairingStore(paths.tournament_pairing_dir)
        assert not pairings.has_revision(group.group_id, group.active_revision)
        def client_factory():
            assert pairings.has_revision(group.group_id, group.active_revision)
            return object()
        def catchup(manager, groups, client, *args, **kwargs):
            assert groups[0].tournament.published_matches is not None
            return SimpleNamespace(cancelled=False, manifest=SimpleNamespace(failures=()), manifest_path=paths.manifest_dir / "child.json", warnings=())
        monkeypatch.setattr(refresh_service, "build_refresh_plan", lambda manager, *args, **kwargs: plan_for(manager))
        monkeypatch.setattr(refresh, "catch_up_manager_game", catchup, raising=False)
        coordinator = refresh_service.create_refresh_coordinator(paths, client_factory=client_factory)
        result = coordinator.action({"key":"game", "locale":"da", "game":manager.game.slug},
            cancelled=lambda: False, progress=lambda event: None, checkpoint={"run_id":"00000000-0000-0000-0000-000000000001", "manifest_paths":[]})
        assert result.status == "success"


@pytest.mark.parametrize("retry_from_manifest", (False, True))
def test_real_catchup_worker_persists_partial_then_resumes(monkeypatch, retry_from_manifest):
    from tests.test_player_statistics import sample_statistics
    from tests.test_refresh_foundation import _future_metadata
    from tests.test_refresh_runs import wait_for
    calls = []
    class Client:
        def fetch_game_info(self, game):
            calls.append("metadata")
            if calls.count("metadata") == 1:
                raise holdet.FetchError("ingen spilinfo")
            return _future_metadata(game, datetime.now(timezone.utc))
        def fetch_players(self, game, *, round_number=None):
            calls.append(f"players:{round_number}")
            return replace(sample_statistics(round_number or 3), round_status="complete")
        def fetch_team(self, reference):
            calls.append(f"team:{reference.team_id}")
            return sample_team(reference.team_id, current_round=3)
    with temporary_directory() as root:
        paths = make_paths(root)
        team = sample_team(1, current_round=3)
        store = holdet.GroupStore(paths.groups_file, paths.group_revision_dir)
        manager = store.create_manager_game(team.reference.game, "Spil")
        store.create("Liga", manager.game,
            (holdet.GroupTeam(1, team.team_name, team.reference.source_url),), group_id="league")
        coordinator = refresh_service.create_refresh_coordinator(paths, client_factory=Client)
        run = coordinator.start([{"key":"da:tour", "locale":"da", "game":manager.game.slug}])
        done = wait_for(coordinator, run["run_id"], lambda x: x["status"] in {"partial", "failed", "success"})
        assert done["status"] == "partial", done
        assert "ingen spilinfo" in done["games"][0]["detail"]
        assert "metadata" in calls and "team:1" in calls
        child = done["games"][0]["manifest_paths"][-1]
        manifest = holdet.ManifestStore(paths.manifest_dir).load(paths.data_dir / child)
        assert manifest.failures
        assert any(item.step_id == "metadata" for item in manifest.failures)
        assert any(item.step_id == "team:1" and item.status == "fetched" for item in manifest.steps)
        if retry_from_manifest:
            run = coordinator.start([{"key": "da:tour", "locale": "da", "game": manager.game.slug,
                                      "retry_manifest_path": child}])
        else:
            coordinator.resume(run["run_id"])
        resumed = wait_for(coordinator, run["run_id"], lambda x: x["status"] == "success")
        assert calls.count("metadata") == 2
        assert calls.count("team:1") == 1
        assert len(resumed["games"][0]["manifest_paths"]) == (1 if retry_from_manifest else 2)


def test_manifest_failure_recovers_locally_without_repeating_fetch(monkeypatch):
    from tests.test_refresh_foundation import _manifest
    from tests.test_refresh_runs import wait_for
    calls = []
    with temporary_directory() as root:
        paths = make_paths(root)
        team = sample_team(1)
        store = holdet.GroupStore(paths.groups_file, paths.group_revision_dir)
        manager = store.create_manager_game(team.reference.game, "Spil")
        manifest = replace(_manifest(datetime.now(timezone.utc)), game_slug=manager.game.slug,
                           game_url=manager.game.original)
        def catchup(*args, **kwargs):
            calls.append("fetch")
            raise refresh.ManifestWriteError(manifest, OSError("låst"))
        monkeypatch.setattr(refresh_service, "build_refresh_plan", lambda manager, *args, **kwargs: plan_for(manager))
        monkeypatch.setattr(refresh, "catch_up_manager_game", catchup)
        coordinator = refresh_service.create_refresh_coordinator(paths, client_factory=lambda: object())
        run = coordinator.start([{"key":"game", "locale":"da", "game":manager.game.slug}])
        partial = wait_for(coordinator, run["run_id"], lambda value: value["status"] == "partial")
        pending = partial["games"][0]["manifest_paths"][-1]
        assert "pending-" in pending
        assert holdet.ManifestStore(paths.manifest_dir).load(paths.data_dir / pending).run_id == manifest.run_id
        coordinator.resume(run["run_id"])
        final = wait_for(coordinator, run["run_id"], lambda value: value["status"] == "success")
        assert calls == ["fetch"]
        assert holdet.ManifestStore(paths.manifest_dir).load(paths.data_dir / final["games"][0]["manifest_paths"][-1]).run_id == manifest.run_id


def test_interrupted_after_child_manifest_reuses_valid_completed_manifest(monkeypatch):
    from tests.test_refresh_foundation import _manifest
    with temporary_directory() as root:
        paths = make_paths(root)
        team = sample_team(1)
        store = holdet.GroupStore(paths.groups_file, paths.group_revision_dir)
        manager = store.create_manager_game(team.reference.game, "Spil")
        child = replace(_manifest(datetime.now(timezone.utc)), game_slug=manager.game.slug,
                        game_url=manager.game.original)
        child_path = holdet.ManifestStore(paths.manifest_dir).write(child).resolve()
        def no_client():
            raise AssertionError("A completed child must not be fetched again")
        coordinator = refresh_service.create_refresh_coordinator(paths, client_factory=no_client)
        result = coordinator.action({"key":"game", "locale":"da", "game":manager.game.slug},
            cancelled=lambda: False, progress=lambda event: None,
            checkpoint={"run_id":"00000000-0000-0000-0000-000000000001", "uncertain":True,
                        "manifest_paths":[str(child_path)], "progress":{"source":"manifest", "status":"fetched", "manifest_path":str(child_path)}})
        assert result.status == "success"


def test_failed_checkpoint_requests_stop_at_next_source(monkeypatch):
    with temporary_directory() as root:
        paths = make_paths(root)
        manager = holdet.GroupStore(paths.groups_file, paths.group_revision_dir).create_manager_game(sample_team(1).reference.game, "Spil")
        def catchup(manager, groups, client, *args, **kwargs):
            event = SimpleNamespace(step=SimpleNamespace(step_id="players", label="Spillere"), status="running", completed_steps=0, total_steps=1)
            kwargs["progress"](event)
            assert kwargs["cancelled"]()
            return SimpleNamespace(cancelled=True, manifest=SimpleNamespace(failures=()), manifest_path=paths.manifest_dir / "child.json", warnings=())
        monkeypatch.setattr(refresh_service, "build_refresh_plan", lambda manager, *args, **kwargs: plan_for(manager))
        monkeypatch.setattr(refresh, "catch_up_manager_game", catchup)
        def broken_progress(event):
            raise OSError("journal unavailable")
        coordinator = refresh_service.create_refresh_coordinator(paths, client_factory=lambda: object())
        result = coordinator.action({"key":"game", "locale":"da", "game":manager.game.slug},
            cancelled=lambda: False, progress=broken_progress,
            checkpoint={"run_id":"00000000-0000-0000-0000-000000000001", "manifest_paths":[]})
        assert result.status == "partial"
        assert "Journalen" in result.detail


def test_pending_manifest_survives_relocation_of_data_root(monkeypatch):
    import shutil
    from tests.test_refresh_foundation import _manifest
    from tests.test_refresh_runs import wait_for
    with temporary_directory() as root:
        paths = make_paths(root / "original")
        team = sample_team(1)
        manager = holdet.GroupStore(paths.groups_file, paths.group_revision_dir).create_manager_game(team.reference.game, "Spil")
        manifest = replace(_manifest(datetime.now(timezone.utc)), game_slug=manager.game.slug, game_url=manager.game.original)
        def catchup(*args, **kwargs):
            raise refresh.ManifestWriteError(manifest, OSError("låst"))
        monkeypatch.setattr(refresh_service, "build_refresh_plan", lambda manager, *args, **kwargs: plan_for(manager))
        monkeypatch.setattr(refresh, "catch_up_manager_game", catchup)
        original = refresh_service.create_refresh_coordinator(paths, client_factory=lambda: object())
        run = original.start([{"key":"game", "locale":"da", "game":manager.game.slug}])
        wait_for(original, run["run_id"], lambda value: value["status"] == "partial")
        shutil.copytree(root / "original", root / "restored", ignore=shutil.ignore_patterns(".data-refresh.lock"))
        restored_paths = make_paths(root / "restored")
        restored = refresh_service.create_refresh_coordinator(restored_paths, client_factory=lambda: (_ for _ in ()).throw(AssertionError("network")))
        restored.resume(run["run_id"])
        final = wait_for(restored, run["run_id"], lambda value: value["status"] == "success")
        ref = final["games"][0]["manifest_paths"][-1]
        assert holdet.ManifestStore(restored_paths.manifest_dir).load(restored_paths.data_dir / ref).run_id == manifest.run_id


def test_competition_partial_resume_retries_failed_source_only():
    from tests.test_player_statistics import sample_statistics
    from tests.test_refresh_foundation import _future_metadata
    from tests.test_refresh_runs import wait_for

    calls = []
    class Client:
        def fetch_game_info(self, game):
            calls.append("metadata")
            if calls.count("metadata") == 1:
                raise holdet.FetchError("midlertidigt offline")
            return _future_metadata(game, datetime.now(timezone.utc))

        def fetch_players(self, game, *, round_number=None):
            calls.append(f"players:{round_number}")
            return replace(sample_statistics(round_number or 3), round_status="complete")

        def fetch_team(self, reference):
            calls.append(f"team:{reference.team_id}")
            return sample_team(reference.team_id, current_round=3)

    with temporary_directory() as root:
        paths = make_paths(root)
        first, second = sample_team(1, current_round=3), sample_team(2, current_round=3)
        store = holdet.GroupStore(paths.groups_file, paths.group_revision_dir)
        manager = store.create_manager_game(first.reference.game, "Spil")
        for team, group_id in ((first, "first"), (second, "second")):
            store.create(group_id, manager.game,
                (holdet.GroupTeam(team.reference.team_id, team.team_name, team.reference.source_url),),
                group_id=group_id)
        coordinator = refresh_service.create_refresh_coordinator(paths, client_factory=Client)
        run_id = coordinator.start([{"key":"first", "locale":"da", "game":manager.game.slug,
                                    "competition_id":"first"}])["run_id"]
        partial = wait_for(coordinator, run_id, lambda value: value["status"] in {"partial", "failed", "success"})
        assert partial["status"] == "partial", partial
        first_path = paths.data_dir / partial["games"][0]["manifest_paths"][-1]
        first_manifest = holdet.ManifestStore(paths.manifest_dir).load(first_path)
        assert first_manifest.scope == "group" and first_manifest.group_id == "first"
        assert any(step.step_id == "metadata" for step in first_manifest.failures)
        assert "team:1" in calls and "team:2" not in calls
        coordinator.resume(run_id)
        completed = wait_for(coordinator, run_id, lambda value: value["status"] == "success")
        last_path = paths.data_dir / completed["games"][0]["manifest_paths"][-1]
        last_manifest = holdet.ManifestStore(paths.manifest_dir).load(last_path)
        assert last_manifest.scope == "group" and last_manifest.group_id == "first"
        assert last_manifest.retry_of == first_manifest.run_id
        assert calls.count("metadata") == 2 and calls.count("team:1") == 1
        assert "team:2" not in calls


@pytest.mark.parametrize(("scope", "group_id"), (("group", "second"), ("game", None)))
def test_competition_rejects_manifest_for_other_scope_before_client(scope, group_id):
    from tests.test_refresh_foundation import _manifest
    with temporary_directory() as root:
        paths = make_paths(root)
        team = sample_team(1)
        store = holdet.GroupStore(paths.groups_file, paths.group_revision_dir)
        manager = store.create_manager_game(team.reference.game, "Spil")
        store.create("Første", manager.game,
            (holdet.GroupTeam(1, team.team_name, team.reference.source_url),), group_id="first")
        manifest = replace(_manifest(datetime.now(timezone.utc)), scope=scope, group_id=group_id,
                           game_slug=manager.game.slug, game_url=manager.game.original)
        path = holdet.ManifestStore(paths.manifest_dir).write(manifest)
        def no_client():
            raise AssertionError("client must not be created for a mismatched manifest")
        coordinator = refresh_service.create_refresh_coordinator(paths, client_factory=no_client)
        with pytest.raises(holdet.PayloadError, match="manifest"):
            coordinator.action({"key":"first", "locale":"da", "game":manager.game.slug,
                                "competition_id":"first"},
                cancelled=lambda: False, progress=lambda event: None,
                checkpoint={"run_id":"00000000-0000-0000-0000-000000000001",
                            "manifest_paths":[str(path.resolve())]})
