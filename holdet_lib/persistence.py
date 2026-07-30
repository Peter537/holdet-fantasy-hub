"""Small, explicit primitives for atomic and immutable local persistence."""

from __future__ import annotations

from datetime import datetime
import os
from pathlib import Path
from uuid import uuid4


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
            with path.open("xb") as handle:
                handle.write(content)
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
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)