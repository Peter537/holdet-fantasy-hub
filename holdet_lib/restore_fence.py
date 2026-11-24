"""Coordinate restore with readers and data writers across process boundaries."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
from uuid import UUID

from .errors import PayloadError
from .paths import AppPaths
from .persistence import exclusive_configuration_update, replace_text_atomically, restore_journal_path


def _fence_path(data_dir: Path) -> Path:
    data_dir = data_dir.resolve()
    return data_dir.with_name(f".{data_dir.name}.restore-state.json")


@dataclass(frozen=True, slots=True)
class RestoreFence:
    identifier: str
    config_dir: Path
    pending: bool

    @classmethod
    def load(cls, data_dir: Path) -> RestoreFence | None:
        try:
            value = json.loads(_fence_path(data_dir).read_text(encoding="utf-8"))
            identifier, config, pending = value["id"], value["config_dir"], value["pending"]
            if UUID(hex=identifier).hex != identifier or type(pending) is not bool:
                raise ValueError("Invalid restore state")
            config_dir = Path(config)
            if not config_dir.is_absolute():
                raise ValueError("Invalid config path")
        except FileNotFoundError:
            return None
        except (OSError, AttributeError, KeyError, TypeError, ValueError) as exc:
            raise PayloadError("Gendannelsens datatilstand kunne ikke læses") from exc
        return cls(identifier, config_dir, pending)

    def save(self, data_dir: Path) -> None:
        replace_text_atomically(_fence_path(data_dir), json.dumps({
            "id": self.identifier, "config_dir": str(self.config_dir), "pending": self.pending,
        }) + "\n", durable=True)


def restore_read_version(paths: AppPaths) -> str | None:
    """Record a consistent restore version before or after reading multiple files."""
    state = RestoreFence.load(paths.data_dir)
    if (state is not None and state.pending) or restore_journal_path(paths.config_dir).exists():
        raise PayloadError("Gendannelsen skal afsluttes, før data kan læses")
    return state.identifier if state is not None else None


@contextmanager
def update_current_installation(paths: AppPaths, version: str | None):
    """Keep a form update in the installation generation the user saw."""
    with exclusive_configuration_update(paths.config_dir):
        if restore_read_version(paths) != version:
            raise PayloadError("Installationens data er gendannet; genindlæs og prøv igen")
        yield
