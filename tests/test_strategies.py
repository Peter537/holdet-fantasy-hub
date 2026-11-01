from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
import subprocess
import sys
import threading
import time
from unittest.mock import patch

import pytest

from holdet_lib.paths import PathOverrides, resolve_paths
from holdet_lib.persistence import exclusive_file_update, file_update_lock_path
from holdet_lib.strategies import StrategyStore, TeamStrategy
from tests.test_decision_analysis import GAME


STRATEGY = TeamStrategy(transfer_policy="fixed", captain_policy="fixed", fixed_captain_id=1)


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
            b = pool.submit(StrategyStore(path).set, GAME, 202, STRATEGY)
            try:
                b.result(timeout=0.3)
            except FutureTimeout:
                pass
        finally:
            release.set()
        a.result(timeout=5)
        b.result(timeout=5)
    assert {key[2] for key in StrategyStore(path).load()} == {101, 202}


def test_separate_processes_with_shared_config_preserve_distinct_policies(tmp_path):
    config = tmp_path / "shared-config"
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path / "first", config_dir=config), environ={})
    first, read, release = paused_first_writer(paths.strategy_file)
    program = """import sys
from pathlib import Path
from holdet_lib.paths import PathOverrides, resolve_paths
from holdet_lib.strategies import StrategyStore, TeamStrategy
from holdet_lib.players import normalize_game_url
paths = resolve_paths(overrides=PathOverrides(data_root=Path(sys.argv[1]), config_dir=Path(sys.argv[2])), environ={})
print('ready', flush=True)
StrategyStore(paths.strategy_file).set(normalize_game_url(sys.argv[3]), 202,
    TeamStrategy(transfer_policy='fixed', captain_policy='fixed', fixed_captain_id=1))
print('saved')
"""
    child = None
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(first.set, GAME, 101, STRATEGY)
        try:
            assert read.wait(5)
            child = subprocess.Popen(
                [sys.executable, "-B", "-X", "utf8", "-c", program, str(tmp_path / "second"),
                 str(config / ".." / config.name), GAME.original],
                cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            ready = pool.submit(child.stdout.readline)
            assert ready.result(timeout=5).strip() == "ready"
            try:
                child.communicate(timeout=0.3)
            except subprocess.TimeoutExpired:
                pass
        finally:
            release.set()
            if child is not None:
                try:
                    stdout, stderr = child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.communicate(timeout=5)
                    raise
        pending.result(timeout=5)
    assert child.returncode == 0, stderr
    assert stdout.strip() == "saved"
    assert {key[2] for key in StrategyStore(paths.strategy_file).load()} == {101, 202}


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
    ready = tmp_path / "ready"
    program = """import sys, time
from pathlib import Path
from holdet_lib.persistence import exclusive_file_update
with exclusive_file_update(Path(sys.argv[1])):
    Path(sys.argv[2]).write_text('ready', encoding='utf-8')
    time.sleep(30)
"""
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", program, str(store.path), str(ready)],
        cwd=Path(__file__).resolve().parents[1], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), "child did not acquire the OS lock"
        with pytest.raises(TimeoutError, match="busy"):
            with exclusive_file_update(store.path, timeout=0):
                pytest.fail("accepted a competing process")
        assert store.path.read_bytes() == before
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=5)
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
