from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

import pytest

from holdet_lib import GameMetadataStore, ManifestStore, RefreshManifest, RefreshManifestStep
from holdet_lib.persistence import publish_immutable
from tests.test_game_metadata_revisions import _metadata

NOW = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)


def test_revision_failure_leaves_current_metadata_unchanged(tmp_path, monkeypatch):
    store = GameMetadataStore(tmp_path)
    first = _metadata(fetched_at=NOW)
    store.save(first)
    second = _metadata(fetched_at=NOW + timedelta(hours=1), salary=200)
    def fail_revision(_revision):
        raise OSError('disk full')
    monkeypatch.setattr(store, '_save_revision', fail_revision)
    with pytest.raises(OSError):
        store.save(second)
    assert store.load(first.game) == first


def test_no_partial_immutable_file_after_link_and_copy_fail(tmp_path, monkeypatch):
    import holdet_lib.persistence as persistence
    target = tmp_path / 'capture.json'
    def no_link(*_args):
        raise OSError('unsupported')
    monkeypatch.setattr(persistence.os, 'link', no_link)
    def fail_rename(*_args):
        raise OSError('cannot publish')
    monkeypatch.setattr(persistence.os, 'rename', fail_rename)
    with pytest.raises(OSError):
        publish_immutable(target, b'complete data')
    assert not target.exists()


def test_schema_three_round_and_cancelled_survive_roundtrip(tmp_path):
    run_id = str(uuid4())
    step = RefreshManifestStep('players:round:2', 'players', 'Runde 2', 'cancelled', False, error='Afbrudt', requested_round=2)
    manifest = RefreshManifest(3, 'game', run_id, NOW, NOW, 'demo', 4, (step,), game_locale='da')
    store = ManifestStore(tmp_path)
    loaded = store.load(store.write(manifest))
    assert loaded.schema_version == 3
    assert loaded.steps[0].requested_round == 2
    assert loaded.steps[0].retryable
    assert loaded.result == 'failed'
    with pytest.raises(ValueError):
        store.write(replace(manifest, schema_version=2))


def test_metadata_promotion_failure_keeps_old_current_and_reports_revision(tmp_path, monkeypatch):
    import holdet_lib.game_metadata as module
    store = GameMetadataStore(tmp_path)
    first = _metadata(fetched_at=NOW)
    store.save(first)
    second = _metadata(fetched_at=NOW + timedelta(hours=1), salary=200)
    monkeypatch.setattr(module, 'replace_text_atomically', lambda *_: (_ for _ in ()).throw(OSError('disk full')))
    with pytest.raises(OSError, match='revisionen.*gemt'):
        store.save(second)
    assert store.load(first.game) == first
    revisions = store.revisions(first.game)[0]
    assert revisions[0].metadata == second
    assert revisions[0].promotion == 'unconfirmed_newer'
    assert revisions[1].promotion == 'current'


def test_retention_preserves_manifest_reference_and_alert_dependencies(tmp_path):
    import holdet_lib as h
    from tests.test_player_statistics import sample_statistics
    paths = h.resolve_paths(overrides=h.PathOverrides(data_root=tmp_path), environ={})
    store = h.PlayerStatisticsStore(paths.snapshot_dir)
    statistics = sample_statistics(3)
    first = store.save(statistics, now=NOW)
    second = store.save(statistics, now=NOW + timedelta(minutes=1))
    third = store.save(statistics, now=NOW + timedelta(minutes=2))
    run_id = str(uuid4())
    ref = 'snapshots/' + second.relative_to(paths.snapshot_dir).as_posix()
    steps = (
        RefreshManifestStep('players', 'players', 'Spillere', 'fetched', True, started_at=NOW, completed_at=NOW, data_reference=ref),
        RefreshManifestStep('postprocess', 'postprocess', 'Efterbehandling', 'failed_no_cache', True, started_at=NOW, completed_at=NOW, error='Statusalarmer: fejl'),
    )
    manifest = RefreshManifest(2, 'game', run_id, NOW, NOW, statistics.game.slug, 3, steps, game_locale='da')
    ManifestStore(paths.manifest_dir).write(manifest)
    plan = h.plan_snapshot_retention(paths)
    assert not plan.candidates
    assert {first.resolve(), second.resolve(), third.resolve()} <= set(plan.retained)


def test_retention_refuses_to_prune_if_manifest_unreadable(tmp_path):
    import holdet_lib as h
    from tests.test_player_statistics import sample_statistics
    paths = h.resolve_paths(overrides=h.PathOverrides(data_root=tmp_path), environ={})
    store = h.PlayerStatisticsStore(paths.snapshot_dir)
    store.save(sample_statistics(3), now=NOW)
    store.save(sample_statistics(3), now=NOW + timedelta(minutes=1))
    bad = paths.manifest_dir / 'da--demo' / 'game' / 'refresh-round3.json'
    bad.parent.mkdir(parents=True)
    bad.write_text('{bad', encoding='utf-8')
    plan = h.plan_snapshot_retention(paths)
    assert not plan.candidates
    assert plan.warnings


def test_quality_reports_partial_failure_and_stale_complete_cache(tmp_path):
    import holdet_lib as h
    from tests.test_library_storage import sample_team
    from tests.test_player_statistics import sample_statistics
    paths = h.resolve_paths(overrides=h.PathOverrides(data_root=tmp_path), environ={})
    team = sample_team(1, current_round=3)
    game = team.reference.game
    manager = h.ManagerGame(game, 'Demo')
    group = h.GroupDefinition('friends', 'Venner', game, (h.GroupTeam(1, team.team_name, team.reference.source_url),))
    teams = h.SnapshotStore(paths.snapshot_dir)
    players = h.PlayerStatisticsStore(paths.snapshot_dir)
    team_path = teams.save_team_json(team, now=NOW)
    player_path = players.save(replace(sample_statistics(3), round_status='complete'), now=NOW)
    run_id = str(uuid4())
    team_ref = 'snapshots/' + team_path.relative_to(paths.snapshot_dir).as_posix()
    player_ref = 'snapshots/' + player_path.relative_to(paths.snapshot_dir).as_posix()
    steps = (
        RefreshManifestStep('team:1', 'team', 'Hold', 'fetched', True, started_at=NOW, completed_at=NOW, team_id=1, data_reference=team_ref),
        RefreshManifestStep('players', 'players', 'Spillere', 'reused_after_error', True, started_at=NOW, completed_at=NOW, cache_reference=player_ref, cache_generated_at=NOW, error='offline'),
    )
    manifest = RefreshManifest(2, 'game', run_id, NOW, NOW, game.slug, 3, steps, game_locale='da')
    path = ManifestStore(paths.manifest_dir).write(manifest)
    report = h.build_data_quality_report((manager,), (group,), teams.scan(), players.scan(), manifest_dir=paths.manifest_dir, now=NOW + timedelta(days=3))
    assert report.rounds[0].readiness == 'error'
    path.unlink()
    report = h.build_data_quality_report((manager,), (group,), teams.scan(), players.scan(), manifest_dir=paths.manifest_dir, now=NOW + timedelta(days=3))
    assert report.rounds[0].readiness == 'stale'
    assert report.rounds[0].oldest_team_data == NOW


def test_retention_blocks_unfinished_journal(tmp_path):
    import json
    import holdet_lib as h
    from tests.test_player_statistics import sample_statistics
    paths = h.resolve_paths(overrides=h.PathOverrides(data_root=tmp_path), environ={})
    store = h.PlayerStatisticsStore(paths.snapshot_dir)
    store.save(sample_statistics(3), now=NOW)
    store.save(sample_statistics(3), now=NOW + timedelta(minutes=1))
    run_id = str(uuid4())
    journal = paths.data_dir / 'refresh-runs' / run_id / 'run.json'
    journal.parent.mkdir(parents=True)
    entry = {'game': {'key': 'da:demo'}, 'status': 'failed', 'manifest_paths': []}
    run = {'schema_version': 1, 'run_id': run_id, 'created_at': NOW.isoformat(), 'status': 'failed', 'games': [entry]}
    journal.write_text(json.dumps(run), encoding='utf-8')
    assert not h.plan_snapshot_retention(paths).candidates
    run['status'] = entry['status'] = 'success'
    journal.write_text(json.dumps(run), encoding='utf-8')
    assert len(h.plan_snapshot_retention(paths).candidates) == 1


def test_quality_includes_scheduled_target_missing_from_old_caches(tmp_path):
    import holdet_lib as h
    from tests.test_library_storage import sample_team
    from tests.test_player_statistics import sample_statistics
    paths = h.resolve_paths(overrides=h.PathOverrides(data_root=tmp_path), environ={})
    team = sample_team(1, current_round=3)
    manager = h.ManagerGame(team.reference.game, 'Demo')
    group = h.GroupDefinition('friends', 'Venner', manager.game, (h.GroupTeam(1, team.team_name, team.reference.source_url),))
    teams = h.SnapshotStore(paths.snapshot_dir)
    players = h.PlayerStatisticsStore(paths.snapshot_dir)
    teams.save_team_json(team, now=NOW)
    players.save(replace(sample_statistics(3), round_status='complete'), now=NOW)
    metadata = replace(_metadata(fetched_at=NOW + timedelta(hours=1)), game=manager.game, rounds=(h.ScheduleRound(4, NOW, NOW + timedelta(hours=1), NOW + timedelta(days=1)),))
    GameMetadataStore(paths.game_metadata_dir).save(metadata)
    report = h.build_data_quality_report((manager,), (group,), teams.scan(), players.scan(), manifest_dir=paths.manifest_dir, now=NOW + timedelta(hours=2))
    assert report.rounds[0].round_number == 4
    assert report.rounds[0].readiness == 'missing'


def test_cleanup_preserves_archive_containing_manifest_reference(tmp_path):
    import json
    import zipfile
    import holdet_lib as h
    paths = h.resolve_paths(overrides=h.PathOverrides(data_root=tmp_path), environ={})
    reference = 'snapshots/demo/players/player-round3.json'
    run_id = str(uuid4())
    step = RefreshManifestStep('players', 'players', 'Spillere', 'fetched', True, started_at=NOW, completed_at=NOW, data_reference=reference)
    ManifestStore(paths.manifest_dir).write(RefreshManifest(2, 'game', run_id, NOW, NOW, 'demo', 3, (step,), game_locale='da'))
    paths.archive_dir.mkdir(parents=True)
    protected = paths.archive_dir / 'protected.zip'
    other = paths.archive_dir / 'other.zip'
    for archive, stored in ((protected, reference), (other, 'snapshots/demo/players/unrelated.json')):
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('archive-manifest.json', json.dumps({'schema_version': 1, 'files': [{'path': stored}]}))
            output.writestr(stored, '{}')
    candidates = {item.path for item in h.list_cleanup_candidates(paths)}
    assert protected.resolve() not in candidates
    assert other.resolve() in candidates


def test_retention_holds_refresh_lease_until_source_removed(tmp_path, monkeypatch):
    import holdet_lib as h
    import holdet_lib.maintenance as maintenance
    from holdet_lib.refresh_runs import RefreshRunBusyError, RefreshRunCoordinator, RefreshRunResult
    from tests.test_player_statistics import sample_statistics
    paths = h.resolve_paths(overrides=h.PathOverrides(data_root=tmp_path), environ={})
    store = h.PlayerStatisticsStore(paths.snapshot_dir)
    first = store.save(sample_statistics(3), now=NOW)
    second = store.save(sample_statistics(3), now=NOW + timedelta(minutes=1))
    candidates = h.plan_snapshot_retention(paths).candidates
    assert len(candidates) == 1
    coordinator = RefreshRunCoordinator(paths.data_dir / 'refresh-runs', lambda *_a, **_k: RefreshRunResult('success'))
    original_validate = maintenance._validate_retention_archive
    observed = []
    def validate_during_archive(archive, expected):
        with pytest.raises(RefreshRunBusyError):
            coordinator.start([{'key': 'da:demo'}])
        observed.append(True)
        original_validate(archive, expected)
    monkeypatch.setattr(maintenance, '_validate_retention_archive', validate_during_archive)
    result = h.archive_retention_candidates(paths, candidates, now=NOW)
    assert observed and result.removed_files == 1
    assert not first.exists() and second.exists()


def test_retention_and_cleanup_refuse_while_refresh_owns_lease(tmp_path):
    import holdet_lib as h
    from holdet_lib.refresh_runs import exclusive_data_operation, RefreshRunBusyError
    from tests.test_player_statistics import sample_statistics
    paths = h.resolve_paths(overrides=h.PathOverrides(data_root=tmp_path), environ={})
    store = h.PlayerStatisticsStore(paths.snapshot_dir)
    first = store.save(sample_statistics(3), now=NOW)
    store.save(sample_statistics(3), now=NOW + timedelta(minutes=1))
    candidates = h.plan_snapshot_retention(paths).candidates
    paths.report_dir.mkdir(parents=True)
    export = paths.report_dir / 'test.html'
    export.write_text('test', encoding='utf-8')
    with exclusive_data_operation(paths.data_dir):
        with pytest.raises(RefreshRunBusyError):
            h.archive_retention_candidates(paths, candidates, now=NOW)
        with pytest.raises(RefreshRunBusyError):
            h.delete_derived_files(paths, (export,))
    assert first.exists() and export.exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows error codes")
@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_atomic_replace_recovers_from_transient_windows_sharing_error(tmp_path, monkeypatch, winerror):
    import holdet_lib.persistence as persistence
    target = tmp_path / 'settings.json'
    target.write_text('before', encoding='utf-8')
    original_replace = persistence.os.replace
    attempts = []
    def replace_after_sharing_violation(source, destination):
        attempts.append((source, destination))
        assert target.read_text(encoding='utf-8') == 'before'
        if len(attempts) < 3:
            raise OSError(13, 'sharing violation', None, winerror)
        original_replace(source, destination)
    monkeypatch.setattr(persistence.os, 'replace', replace_after_sharing_violation)
    persistence.replace_text_atomically(target, 'after')
    assert target.read_text(encoding='utf-8') == 'after'
    assert len(attempts) == 3
    assert not list(tmp_path.glob('.settings.json.*.tmp'))


@pytest.mark.skipif(os.name != "nt", reason="Windows error codes")
def test_atomic_replace_permanent_windows_denial_preserves_original(tmp_path, monkeypatch):
    import holdet_lib.persistence as persistence
    target = tmp_path / 'settings.json'
    target.write_text('before', encoding='utf-8')
    attempts = []
    def access_denied(*_):
        attempts.append(True)
        raise OSError(13, 'access denied', None, 5)
    monkeypatch.setattr(persistence.os, 'replace', access_denied)
    with pytest.raises(OSError) as caught:
        persistence.replace_text_atomically(target, 'after')
    assert caught.value.winerror == 5
    assert 1 < len(attempts) <= 6
    assert target.read_text(encoding='utf-8') == 'before'
    assert not list(tmp_path.glob('.settings.json.*.tmp'))


def test_atomic_replace_unrelated_error_is_not_retried(tmp_path, monkeypatch):
    import holdet_lib.persistence as persistence
    target = tmp_path / 'settings.json'
    target.write_text('before', encoding='utf-8')
    attempts = []
    def unrelated(*_):
        attempts.append(True)
        raise OSError(2, 'not found', None, 2)
    monkeypatch.setattr(persistence.os, 'replace', unrelated)
    with pytest.raises(OSError):
        persistence.replace_text_atomically(target, 'after')
    assert len(attempts) == 1
    assert target.read_text(encoding='utf-8') == 'before'
    assert not list(tmp_path.glob('.settings.json.*.tmp'))


def test_windows_exclusive_handle_blocks_atomic_replace_until_closed(tmp_path, monkeypatch):
    import ctypes
    from ctypes import wintypes
    import os
    import sys
    import holdet_lib.persistence as persistence
    if sys.platform != 'win32':
        pytest.skip('Windows sharing semantics')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    target = tmp_path / 'settings.json'
    target.write_text('before', encoding='utf-8')
    handle = kernel.CreateFileW(str(target), 0x80000000, 0, None, 3, 0, None)
    if handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    original_replace = os.replace
    failures = []
    handle_open = True
    def replace_and_release(source, destination):
        nonlocal handle_open
        try:
            original_replace(source, destination)
        except OSError as exc:
            failures.append(exc.winerror)
            kernel.CloseHandle(handle)
            handle_open = False
            raise
    monkeypatch.setattr(persistence.os, 'replace', replace_and_release)
    try:
        persistence.replace_text_atomically(target, 'after')
    finally:
        if handle_open:
            kernel.CloseHandle(handle)
    assert failures and failures[0] in (5, 32, 33)
    assert target.read_text(encoding='utf-8') == 'after'
