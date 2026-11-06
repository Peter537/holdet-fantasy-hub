"""Small, explicit primitives for atomic and immutable local persistence."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from functools import wraps
from inspect import get_annotations
import errno
import os
from pathlib import Path
import time
import threading
from uuid import uuid4


_FILE_UPDATE_LOCKS: dict[str, threading.Lock] = {}
_FILE_UPDATE_GUARD = threading.Lock()
_OWNED_FILE_LOCKS = threading.local()


def file_update_lock_path(path: Path) -> Path:
    resolved = Path(path).resolve()
    return resolved.with_name(f".{resolved.name}.lock")


@contextmanager
def exclusive_file_update(path: Path, *, timeout: float = 5.0):
    """Serialize a read/update/replace transaction across threads and processes.

    The sidecar stays in place so waiters always lock the same file, even when
    the protected JSON is atomically replaced. Reads need no lock or sidecar.
    """
    if timeout < 0:
        raise ValueError("File update timeout must be non-negative")
    resolved = Path(path).resolve()
    key = os.path.normcase(str(resolved))
    owned = getattr(_OWNED_FILE_LOCKS, "paths", None)
    if owned is not None and key in owned:
        yield
        return
    deadline = time.monotonic() + timeout
    with _FILE_UPDATE_GUARD:
        thread_lock = _FILE_UPDATE_LOCKS.setdefault(key, threading.Lock())
    busy = f"Settings file is busy; try again: {resolved.name}"
    if not thread_lock.acquire(timeout=max(0, deadline - time.monotonic())):
        raise TimeoutError(busy)
    try:
        sidecar = file_update_lock_path(resolved)
        sidecar.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(os.open(sidecar, os.O_RDWR | os.O_CREAT, 0o600), "r+b") as handle:
            if os.name == "nt":
                import msvcrt
                def lock():
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                def unlock():
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                def lock():
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                def unlock():
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            while True:
                try:
                    lock()
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(busy) from exc
                    time.sleep(min(0.05, remaining))
            try:
                handle.seek(0)
                handle.write(b"0")
                handle.flush()
                if owned is None:
                    owned = set()
                    _OWNED_FILE_LOCKS.paths = owned
                owned.add(key)
                try:
                    yield
                finally:
                    owned.remove(key)
            finally:
                unlock()
    finally:
        thread_lock.release()


@contextmanager
def exclusive_configuration_update(config_dir: Path, *, timeout: float = 5.0):
    """Lock the config namespace using a sibling outside the replaced directory.

    Writers retain this through read/modify/write or rollback. Same-thread
    nesting lets a composed transaction call another store's guarded methods.
    """
    with exclusive_file_update(config_dir, timeout=timeout):
        yield


def configuration_transaction(method):
    """Hold a store's configuration namespace through its complete mutation."""

    @wraps(method)
    def guarded(store, *args, **kwargs):
        with exclusive_configuration_update(store.path.parent):
            return method(store, *args, **kwargs)

    guarded.__annotations__ = get_annotations(method, eval_str=True)
    return guarded


def aware_local(value: datetime | None = None) -> datetime:
    """Return an aware local timestamp without touching the filesystem."""

    resolved = value or datetime.now().astimezone()
    return resolved.astimezone() if resolved.tzinfo is None else resolved


def publish_immutable(path: Path, content: bytes) -> None:
    """Publish bytes exactly once, raising ``FileExistsError`` on collision."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{uuid4().hex}.tmp"
    try:
        temporary.write_bytes(content)
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise
        except OSError:
            # Windows rename refuses to overwrite an existing destination. On
            # other platforms rename may replace it, so fail rather than break
            # the immutable/no-partial-publication contract.
            if os.name != "nt":
                raise
            os.rename(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def publish_immutable_text(path: Path, content: str) -> None:
    publish_immutable(path, content.encode("utf-8"))


def replace_text_atomically(path: Path, content: str) -> None:
    """Replace one UTF-8 text file atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    try:
        temporary.write_text(content, encoding="utf-8", newline="\n")
        for delay in (0.025, 0.05, 0.1, 0.2, None):
            try:
                os.replace(temporary, path)
                break
            except OSError as exc:
                if (
                    os.name != "nt"
                    or getattr(exc, "winerror", None) not in {5, 32, 33}
                    or delay is None
                ):
                    raise
                time.sleep(delay)
    finally:
        temporary.unlink(missing_ok=True)
