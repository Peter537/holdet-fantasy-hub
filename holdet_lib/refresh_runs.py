"""Durable, single-worker orchestration for explicit refresh runs.

The caller supplies an action that opens its own clients and stores in the worker.
Reading a journal never starts work or repairs interrupted state on disk.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable
from uuid import UUID, uuid4

from .persistence import replace_text_atomically

_TERMINAL = frozenset({"success", "partial", "failed", "cancelled"})
_ACTIVE: dict[str, str] = {}
_GUARD = threading.RLock()
_IO_LOCK = threading.RLock()


@dataclass(frozen=True, slots=True)
class RefreshRunResult:
    status: str
    manifest_paths: tuple[str, ...] = ()
    detail: str | None = None

    def __post_init__(self) -> None:
        if self.status not in _TERMINAL:
            raise ValueError("Ugyldig status for opdatering")


class RefreshRunBusyError(RuntimeError):
    """Another worker owns the data root."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_id(value: str) -> str:
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Ugyldigt opdaterings-id") from exc
    if str(parsed) != value:
        raise ValueError("Ugyldigt opdaterings-id")
    return value


def _root_key(root: Path) -> str:
    return os.path.normcase(str(root.resolve()))


def _lock_path(root: Path) -> Path:
    data_dir = root.parent
    return data_dir.parent / f".{data_dir.name}-refresh.lock"


def _try_lock(handle: Any) -> bool:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
    else:
        import fcntl
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


def _unlock(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class _Lease:
    def __init__(self, root: Path, run_id: str) -> None:
        self.root = root
        self.key = _root_key(root)
        self.run_id = run_id
        self.handle: Any = None

    def acquire(self) -> None:
        with _GUARD:
            if self.key in _ACTIVE:
                raise RefreshRunBusyError("En opdatering er allerede i gang")
            path = _lock_path(self.root)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.handle = os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT, 0o600), "r+b")
            try:
                if not _try_lock(self.handle):
                    raise RefreshRunBusyError("En opdatering er allerede i gang")
                self.handle.seek(0)
                self.handle.write(b"0" + self.run_id.encode("ascii"))
                self.handle.truncate()
                self.handle.flush()
                _ACTIVE[self.key] = self.run_id
            except BaseException:
                self.handle.close()
                self.handle = None
                raise

    def release(self) -> None:
        with _GUARD:
            if self.handle is not None:
                try:
                    _unlock(self.handle)
                finally:
                    self.handle.close()
                    self.handle = None
                    _ACTIVE.pop(self.key, None)


@contextmanager
def exclusive_data_operation(data_dir: Path):
    """Serialize an explicit data mutation with refresh workers for this root.

    Read-only previews and scans must not enter this context. The lease is held
    until the operation exits, including when it raises.
    """
    lease = _Lease(Path(data_dir) / "refresh-runs", str(uuid4()))
    lease.acquire()
    try:
        yield
    finally:
        lease.release()


def _active(root: Path, run_id: str) -> bool:
    with _GUARD:
        active = _ACTIVE.get(_root_key(root))
        if active is not None:
            return active == run_id
        path = _lock_path(root)
        if not path.exists():
            return False
        try:
            with path.open("r+b") as handle:
                if not _try_lock(handle):
                    handle.seek(1)
                    return handle.read().decode("ascii", errors="replace") == run_id
                _unlock(handle)
        except FileNotFoundError:
            return False
        return False


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))


class RefreshRunCoordinator:
    """Start and reconnect to one worker per data root.

    `action(game, cancelled=..., progress=..., checkpoint=...)` executes in the
    worker and returns RefreshRunResult. Game, scope, checkpoints and progress
    must be JSON values; `game['key']` is unique within a run. The checkpoint
    carries the previous per-game journal on explicit resume, including any
    completed child manifests and the last source whose outcome is uncertain.
    """

    def __init__(self, root: Path, action: Callable[..., RefreshRunResult], *,
                 executor_factory: Callable[..., Any] = ThreadPoolExecutor) -> None:
        self.root = Path(root)
        self.action = action
        self.executor_factory = executor_factory

    def _directory(self, run_id: str) -> Path:
        return self.root / _run_id(run_id)

    def _read(self, run_id: str) -> dict[str, Any]:
        with _IO_LOCK:
            for attempt in range(20):
                try:
                    with (self._directory(run_id) / "run.json").open(encoding="utf-8") as handle:
                        value = json.load(handle)
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.01)
        if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("run_id") != run_id or not isinstance(value.get("games"), list)
            or not isinstance(value.get("created_at"), str)
            or value.get("status") not in _TERMINAL | {"running", "unstarted"}):
            raise ValueError("Ugyldig opdateringsjournal")
        for entry in value["games"]:
            if (not isinstance(entry, dict) or not isinstance(entry.get("game"), dict)
                or not isinstance(entry["game"].get("key"), str)
                or entry.get("status") not in _TERMINAL | {"running", "unstarted"}
                or not isinstance(entry.get("manifest_paths"), list)
                or not all(isinstance(path, str) for path in entry["manifest_paths"])):
                raise ValueError("Ugyldig spilstatus i opdateringsjournal")
        return value

    def _write(self, run: dict[str, Any]) -> None:
        run["updated_at"] = _now()
        content = json.dumps(run, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        with _IO_LOCK:
            for attempt in range(20):
                try:
                    replace_text_atomically(self._directory(run["run_id"]) / "run.json", content)
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.01)

    def snapshot(self, run_id: str) -> dict[str, Any]:
        run = self._read(run_id)
        if run["status"] in {"running", "unstarted"} and not _active(self.root, run_id):
            run["status"] = "interrupted"
            for game in run["games"]:
                if game["status"] == "running":
                    game["status"] = "interrupted"
                    game["uncertain"] = True
        run["cancel_requested"] = (self._directory(run_id) / "cancel.request").exists()
        return run

    def scan_runs(self) -> tuple[list[dict[str, Any]], list[str]]:
        """Return readable journals and errors, without creating or repairing files."""
        if not self.root.is_dir():
            return [], []
        result = []
        warnings = []
        for path in self.root.iterdir():
            if not path.is_dir():
                continue
            try:
                result.append(self.snapshot(path.name))
            except (ValueError, OSError, KeyError) as exc:
                warnings.append(f"{path.name}: {type(exc).__name__}")
        return sorted(result, key=lambda item: (item["created_at"], item["run_id"]), reverse=True), warnings

    def list_runs(self) -> list[dict[str, Any]]:
        return self.scan_runs()[0]

    def start(self, games: list[dict[str, Any]], *, scope: dict[str, Any] | None = None) -> dict[str, Any]:
        selected = _json_copy(games)
        scope = _json_copy(scope or {})
        if not selected or not all(isinstance(item, dict) and isinstance(item.get("key"), str) and item["key"] for item in selected):
            raise ValueError("Vælg mindst ét managerspil med et entydigt key")
        if len({item["key"] for item in selected}) != len(selected):
            raise ValueError("Et managerspil kan kun optræde én gang")
        run_id = str(uuid4())
        lease = _Lease(self.root, run_id)
        lease.acquire()
        try:
            run = {"schema_version": 1, "run_id": run_id, "scope": scope,
                   "created_at": _now(), "updated_at": _now(), "status": "unstarted",
                   "games": [{"game": game, "status": "unstarted", "progress": None,
                              "uncertain": False, "manifest_paths": [], "detail": None} for game in selected]}
            self._write(run)
            self._submit(run, lease)
        except BaseException:
            lease.release()
            raise
        return self.snapshot(run_id)

    def resume(self, run_id: str) -> dict[str, Any]:
        _run_id(run_id)
        lease = _Lease(self.root, run_id)
        lease.acquire()
        try:
            run = self._read(run_id)
            if run["status"] == "success":
                raise ValueError("En afsluttet opdatering kan ikke genoptages")
            for game in run["games"]:
                if game["status"] != "success":
                    if game["status"] == "running":
                        game["uncertain"] = True
                    game["status"] = "unstarted"
            run["status"] = "unstarted"
            (self._directory(run_id) / "cancel.request").unlink(missing_ok=True)
            self._write(run)
            self._submit(run, lease)
        except BaseException:
            lease.release()
            raise
        return self.snapshot(run_id)

    def cancel(self, run_id: str) -> dict[str, Any]:
        run = self.snapshot(run_id)
        if run["status"] in {"running", "unstarted"}:
            replace_text_atomically(self._directory(run_id) / "cancel.request", _now() + "\n")
        return self.snapshot(run_id)

    def _submit(self, run: dict[str, Any], lease: _Lease) -> None:
        executor = self.executor_factory(max_workers=1)
        try:
            executor.submit(self._work, run, lease, executor)
        except BaseException:
            executor.shutdown(wait=False)
            raise

    def _work(self, run: dict[str, Any], lease: _Lease, executor: Any) -> None:
        run_id = run["run_id"]
        marker = self._directory(run_id) / "cancel.request"
        try:
            run["status"] = "running"
            self._write(run)
            for entry in run["games"]:
                if entry["status"] == "success":
                    continue
                if marker.exists():
                    break
                checkpoint = _json_copy(entry)
                checkpoint["run_id"] = run_id
                entry["status"] = "running"
                self._write(run)

                def progress(event: dict[str, Any]) -> None:
                    entry["progress"] = _json_copy(event)
                    entry["uncertain"] = event.get("status") == "running"
                    if isinstance(event.get("manifest_path"), str):
                        entry["manifest_paths"] = list(dict.fromkeys(
                            [*entry["manifest_paths"], event["manifest_path"]]
                        ))
                    self._write(run)

                try:
                    result = self.action(_json_copy(entry["game"]), cancelled=marker.exists,
                                         progress=progress, checkpoint=checkpoint)
                    if not isinstance(result, RefreshRunResult):
                        raise TypeError("Opdateringen returnerede et ugyldigt resultat")
                    entry["status"] = result.status
                    entry["detail"] = result.detail
                    entry["manifest_paths"] = list(dict.fromkeys([*entry["manifest_paths"], *map(str, result.manifest_paths)]))
                    entry["uncertain"] = False
                except Exception as exc:
                    entry["status"] = "failed"
                    entry["detail"] = f"{type(exc).__name__}: {exc}"
                    entry["uncertain"] = True
                self._write(run)
                if entry["status"] == "cancelled":
                    break
            statuses = [item["status"] for item in run["games"]]
            if marker.exists() and any(item != "success" for item in statuses):
                run["status"] = "cancelled"
            elif "cancelled" in statuses:
                run["status"] = "cancelled"
            elif all(item == "success" for item in statuses):
                run["status"] = "success"
            elif any(item in {"success", "partial"} for item in statuses):
                run["status"] = "partial"
            else:
                run["status"] = "failed"
            self._write(run)
        finally:
            lease.release()
            executor.shutdown(wait=False)
