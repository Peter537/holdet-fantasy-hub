from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import threading
from unittest.mock import patch

import pytest

from holdet_lib.paths import PathOverrides, resolve_paths
from holdet_lib.persistence import exclusive_file_update, file_update_lock_path
from holdet_lib.strategies import StrategyStore, TeamStrategy
from tests.spawned_process import spawned_process
from tests.test_decision_analysis import GAME


STRATEGY = TeamStrategy(transfer_policy="fixed", captain_policy="fixed", fixed_captain_id=1)
SECOND_STRATEGY = TeamStrategy(transfer_policy="limited", max_transfers=2, captain_policy="rotate")


def paused_first_writer(path):
    store = StrategyStore(path)
    read, release = threading.Event(), threading.Event()
    load = store.load
    def paused_load():
        result = load()
        read.set()
        assert release.wait(10), "first writer was not released"
        return result
    store.load = paused_load
    return store, read, release


def test_overlapping_thread_saves_preserve_distinct_policies(tmp_path):
    path = tmp_path / "team-strategies.json"
    first, read, release = paused_first_writer(path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first.set, GAME, 101, STRATEGY)
        try:
            assert read.wait(5)
            b = pool.submit(StrategyStore(path).set, GAME, 202, SECOND_STRATEGY)
            with pytest.raises(FutureTimeout):
                b.result(timeout=0.3)
        finally:
            release.set()
        a.result(timeout=5)
        b.result(timeout=5)
    assert StrategyStore(path).load() == {
        (GAME.locale, GAME.slug, 101): STRATEGY,
        (GAME.locale, GAME.slug, 202): SECOND_STRATEGY,
    }


def test_separate_processes_with_shared_config_preserve_distinct_policies(tmp_path):
    config = tmp_path / "shared-config"
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path / "first", config_dir=config), environ={})
    other_paths = resolve_paths(overrides=PathOverrides(
        data_root=tmp_path / "second", config_dir=config / ".." / config.name,
    ), environ={})
    first, read, release = paused_first_writer(paths.strategy_file)
    with spawned_process(save_strategy_in_child, other_paths.strategy_file, GAME, 202, SECOND_STRATEGY) as (child, connection):
        assert connection.poll(30), "strategy writer did not finish startup"
        assert connection.recv() == "ready"
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(first.set, GAME, 101, STRATEGY)
            try:
                assert read.wait(5)
                connection.send("save")
                assert connection.poll(5), "second writer did not start its save"
                assert connection.recv() == "saving"
                assert not connection.poll(0.3), "second writer finished while the first held the lock"
            finally:
                release.set()
            pending.result(timeout=5)
        assert connection.poll(10), "second writer did not finish after release"
        assert connection.recv() == "saved"
        child.join(5)
        assert child.exitcode == 0
    assert StrategyStore(paths.strategy_file).load() == {
        (GAME.locale, GAME.slug, 101): STRATEGY,
        (GAME.locale, GAME.slug, 202): SECOND_STRATEGY,
    }


def save_strategy_in_child(path, game, team_id, strategy, connection):
    with connection:
        connection.send("ready")
        assert connection.recv() == "save"
        connection.send("saving")
        StrategyStore(path).set(game, team_id, strategy)
        connection.send("saved")


def _hold_strategy_file_lock(path, connection):
    with connection, exclusive_file_update(path):
        connection.send("locked")
        connection.recv()


def test_strategy_reads_do_not_create_files(tmp_path):
    root = tmp_path / "absent"
    store = StrategyStore(root / "team-strategies.json")
    assert store.load() == {} and store.get(GAME, 101) is None
    assert not root.exists()


def test_failed_strategy_publication_preserves_json_and_releases_lock(tmp_path):
    store = StrategyStore(tmp_path / "team-strategies.json")
    store.set(GAME, 101, STRATEGY)
    before = store.path.read_bytes()
    with patch("holdet_lib.strategies.replace_text_atomically", side_effect=OSError("synthetic write failure")):
        with pytest.raises(OSError, match="synthetic write failure"):
            store.set(GAME, 202, STRATEGY)
    assert store.path.read_bytes() == before
    store.set(GAME, 303, STRATEGY)
    assert {key[2] for key in store.load()} == {101, 303}


def test_failed_strategy_read_preserves_json_and_releases_lock(tmp_path):
    store = StrategyStore(tmp_path / "team-strategies.json")
    store.path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        store.set(GAME, 202, STRATEGY)
    assert store.path.read_bytes() == b"{"
    store.path.unlink()
    store.set(GAME, 101, STRATEGY)
    assert {key[2] for key in store.load()} == {101}


def test_lock_contention_preserves_json_and_process_exit_releases_os_lock(tmp_path):
    store = StrategyStore(tmp_path / "team-strategies.json")
    store.set(GAME, 101, STRATEGY)
    before = store.path.read_bytes()
    with spawned_process(_hold_strategy_file_lock, store.path) as (_, connection):
        assert connection.poll(30), "child did not acquire the OS lock"
        assert connection.recv() == "locked"
        with pytest.raises(TimeoutError, match="busy"):
            with exclusive_file_update(store.path, timeout=0):
                pytest.fail("accepted a competing process")
        assert store.path.read_bytes() == before
    with exclusive_file_update(store.path, timeout=0):
        pass
    store.set(GAME, 202, STRATEGY)
    assert {key[2] for key in store.load()} == {101, 202}
    assert file_update_lock_path(store.path).is_file()


def test_lock_release_on_exception_and_independent_file_updates(tmp_path):
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with exclusive_file_update(first):
            with exclusive_file_update(second, timeout=0):
                raise RuntimeError("synthetic failure")
    for path in (first, second):
        with exclusive_file_update(path, timeout=0):
            pass
