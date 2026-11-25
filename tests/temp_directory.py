"""Short temporary paths for tests that also create sibling lock files."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory


@contextmanager
def temporary_directory() -> Iterator[Path]:
    with TemporaryDirectory(prefix="h-") as parent:
        root = Path(parent) / "hub"
        root.mkdir()
        yield root
