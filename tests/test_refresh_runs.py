from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from holdet_lib.refresh_runs import (
    RefreshRunBusyError, RefreshRunCoordinator, RefreshRunResult,
    exclusive_data_operation,
)
from tests.test_library_storage import temporary_directory


@pytest.fixture
def tmp_path():
    with temporary_directory() as path:
        yield path


def wait_for(coordinator, run_id, predicate):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        state = coordinator.snapshot(run_id)
        if predicate(state):
            return state
        time.sleep(0.01)
    raise AssertionError(f"Opdateringen nåede ikke forventet tilstand: {state}")


def test_worker_survives_coordinator_recreation_and_continues_after_failure(tmp_path):
    entered = threading.Event()
    proceed = threading.Event()
    calls = []

    def action(game, *, cancelled, progress, checkpoint):
        calls.append(game["key"])
        progress({"source": "players", "status": "running"})
        if game["key"] == "first":
            entered.set()
            assert proceed.wait(5)
            return RefreshRunResult("failed", detail="offline")
        return RefreshRunResult("success", ("data/manifests/second.json",))

    coordinator = RefreshRunCoordinator(tmp_path / "data" / "refresh-runs", action)
    initial = coordinator.start([{"key": "first"}, {"key": "second"}])
    assert entered.wait(5)
    reconnected = RefreshRunCoordinator(tmp_path / "data" / "refresh-runs", action)
    current = reconnected.snapshot(initial["run_id"])
    assert current["status"] == "running"
    assert current["games"][0]["progress"]["source"] == "players"
    with pytest.raises(RefreshRunBusyError):
        reconnected.start([{"key": "third"}])
    proceed.set()
    complete = wait_for(reconnected, initial["run_id"], lambda x: x["status"] == "partial")
    assert calls == ["first", "second"]
    assert [item["status"] for item in complete["games"]] == ["failed", "success"]
    assert complete["games"][1]["manifest_paths"] == ["data/manifests/second.json"]


def test_cancel_is_durable_and_resume_retains_checkpoint_and_success(tmp_path):
    entered = threading.Event()
    proceed = threading.Event()
    calls = []

    def action(game, *, cancelled, progress, checkpoint):
        calls.append((game["key"], checkpoint))
        if game["key"] == "first":
            return RefreshRunResult("success", ("first-manifest.json",))
        if len(calls) == 2:
            progress({"source": "players:round:2", "status": "running"})
            entered.set()
            assert proceed.wait(5)
            assert cancelled()
            return RefreshRunResult("cancelled", ("partial-manifest.json",))
        if game["key"] == "second":
            assert checkpoint["manifest_paths"] == ["partial-manifest.json"]
        return RefreshRunResult("success", ("retry-manifest.json",))

    coordinator = RefreshRunCoordinator(tmp_path / "data" / "refresh-runs", action)
    run_id = coordinator.start([{"key": "first"}, {"key": "second"}, {"key": "third"}])["run_id"]
    assert entered.wait(5)
    assert coordinator.cancel(run_id)["cancel_requested"]
    proceed.set()
    stopped = wait_for(coordinator, run_id, lambda x: x["status"] == "cancelled")
    assert [game["status"] for game in stopped["games"]] == ["success", "cancelled", "unstarted"]
    coordinator.resume(run_id)
    completed = wait_for(coordinator, run_id, lambda x: x["status"] == "success")
    assert [key for key, _ in calls] == ["first", "second", "second", "third"]
    assert completed["games"][1]["manifest_paths"] == ["partial-manifest.json", "retry-manifest.json"]
    assert not completed["cancel_requested"]


def test_interrupted_snapshot_is_read_only_and_resume_marks_uncertain(tmp_path):
    root = tmp_path / "data" / "refresh-runs"
    run_id = "00000000-0000-0000-0000-000000000001"
    path = root / run_id / "run.json"
    path.parent.mkdir(parents=True)
    original = {"schema_version": 1, "run_id": run_id, "scope": {}, "created_at": "2026", "updated_at": "2026", "status": "running", "games": [{"game": {"key": "one"}, "status": "running", "progress": {"source": "team:1", "status": "running"}, "uncertain": False, "manifest_paths": ["previous.json"], "detail": None}]}
    path.write_text(json.dumps(original), encoding="utf-8")
    before = path.read_bytes()
    checkpoints = []
    def action(game, *, cancelled, progress, checkpoint):
        checkpoints.append(checkpoint)
        return RefreshRunResult("success", ("new.json",))
    coordinator = RefreshRunCoordinator(root, action)
    state = coordinator.snapshot(run_id)
    assert state["status"] == "interrupted"
    assert state["games"][0]["uncertain"]
    assert path.read_bytes() == before
    coordinator.resume(run_id)
    complete = wait_for(coordinator, run_id, lambda x: x["status"] == "success")
    assert checkpoints[0]["uncertain"]
    assert checkpoints[0]["progress"]["source"] == "team:1"
    assert complete["games"][0]["manifest_paths"] == ["previous.json", "new.json"]


def test_process_lock_blocks_other_worker_and_poll_does_not_rewrite(tmp_path):
    root = tmp_path / "data" / "refresh-runs"
    entered = threading.Event()
    proceed = threading.Event()
    def action(game, *, cancelled, progress, checkpoint):
        entered.set()
        assert proceed.wait(5)
        return RefreshRunResult("success")
    coordinator = RefreshRunCoordinator(root, action)
    run_id = coordinator.start([{"key": "one"}])["run_id"]
    assert entered.wait(5)
    old_id = "00000000-0000-0000-0000-000000000002"
    old_path = root / old_id / "run.json"
    old_path.parent.mkdir()
    old_path.write_text(json.dumps({"schema_version": 1, "run_id": old_id,
        "created_at": "2026", "status": "running", "games": []}), encoding="utf-8")
    program = '''import sys
from pathlib import Path
from holdet_lib.refresh_runs import RefreshRunBusyError, RefreshRunCoordinator
c = RefreshRunCoordinator(Path(sys.argv[1]), lambda *a, **k: None)
assert c.snapshot(sys.argv[2])["status"] == "running"
assert c.snapshot(sys.argv[3])["status"] == "interrupted"
try:
    c.start([{"key":"other"}])
except RefreshRunBusyError:
    print("busy")
else:
    raise AssertionError("concurrent worker accepted")
'''
    path = root / run_id / "run.json"
    before = path.read_bytes()
    child = subprocess.run([sys.executable, "-c", program, str(root), run_id, old_id], capture_output=True, text=True, timeout=10)
    try:
        assert child.returncode == 0, child.stderr
        assert child.stdout.strip() == "busy"
        assert path.read_bytes() == before
    finally:
        proceed.set()
    wait_for(coordinator, run_id, lambda x: x["status"] == "success")


def test_invalid_run_id_and_empty_or_duplicate_games_are_rejected_without_writes(tmp_path):
    root = tmp_path / "data" / "refresh-runs"
    coordinator = RefreshRunCoordinator(root, lambda *_args, **_kwargs: None)
    assert coordinator.scan_runs() == ([], [])
    assert not root.exists()
    with pytest.raises(ValueError):
        coordinator.snapshot("../other")
    with pytest.raises(ValueError):
        coordinator.start([])
    with pytest.raises(ValueError):
        coordinator.start([{"key": "same"}, {"key": "same"}])
    assert not root.exists()


def test_explicit_data_operation_and_worker_exclude_each_other_and_release_on_error(tmp_path):
    data_dir = tmp_path / "data"
    entered = threading.Event()
    proceed = threading.Event()
    def action(game, *, cancelled, progress, checkpoint):
        entered.set()
        assert proceed.wait(5)
        return RefreshRunResult("success")
    coordinator = RefreshRunCoordinator(data_dir / "refresh-runs", action)

    with pytest.raises(RuntimeError, match="abort"):
        with exclusive_data_operation(data_dir):
            with pytest.raises(RefreshRunBusyError):
                coordinator.start([{"key": "one"}])
            raise RuntimeError("abort")

    run_id = coordinator.start([{"key": "one"}])["run_id"]
    assert entered.wait(5)
    try:
        with pytest.raises(RefreshRunBusyError):
            with exclusive_data_operation(data_dir):
                pass
    finally:
        proceed.set()
    wait_for(coordinator, run_id, lambda value: value["status"] == "success")


def test_replacing_data_directory_does_not_replace_held_lease(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "old.txt").write_text("old", encoding="utf-8")
    coordinator = RefreshRunCoordinator(data_dir / "refresh-runs", lambda *a, **kw: RefreshRunResult("success"))
    with exclusive_data_operation(data_dir):
        data_dir.rename(tmp_path / "old-data")
        data_dir.mkdir()
        (data_dir / "new.txt").write_text("new", encoding="utf-8")
        with pytest.raises(RefreshRunBusyError):
            coordinator.start([{"key": "game"}])
        program = '''import sys
from pathlib import Path
from holdet_lib.refresh_runs import exclusive_data_operation, RefreshRunBusyError
try:
    with exclusive_data_operation(Path(sys.argv[1])):
        pass
except RefreshRunBusyError:
    print("busy")
else:
    raise AssertionError("restore lease was bypassed")
'''
        child = subprocess.run([sys.executable, "-c", program, str(data_dir)], capture_output=True, text=True, timeout=10)
        assert child.returncode == 0, child.stderr
        assert child.stdout.strip() == "busy"
    with exclusive_data_operation(data_dir):
        assert (data_dir / "new.txt").read_text(encoding="utf-8") == "new"
