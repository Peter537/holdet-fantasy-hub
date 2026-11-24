"""Recoverable directory swaps for one validated Hub installation."""

from __future__ import annotations

from dataclasses import dataclass, replace
import json
from pathlib import Path
import shutil
from typing import Literal
from uuid import UUID, uuid4

from .errors import PayloadError
from .paths import AppPaths
from .persistence import (
    exclusive_configuration_update,
    replace_text_atomically,
    replace_with_retry,
    restore_journal_path,
)
from .refresh_runs import exclusive_data_operation
from .restore_fence import RestoreFence

RestorePhase = Literal["staging", "prepared", "committed"]


@dataclass(frozen=True, slots=True)
class RestoreTree:
    target: Path
    stage: Path
    previous: Path
    existed: bool

    @classmethod
    def from_target(cls, target: Path, kind: str, transaction: str, existed: bool) -> RestoreTree:
        return cls(
            target,
            target.parent / f".holdet-restore-{transaction[:12]}" / kind,
            target.with_name(f".{target.name}.old-{transaction}"),
            existed,
        )

    def install(self) -> None:
        if self.existed:
            replace_with_retry(self.target, self.previous)
        replace_with_retry(self.stage, self.target)

    def rollback(self) -> None:
        if self.previous.exists():
            if self.target.exists():
                if self.stage.exists():
                    raise PayloadError("Gendannelsens mapper kan ikke entydigt tilbageføres")
                replace_with_retry(self.target, self.stage)
            replace_with_retry(self.previous, self.target)
        elif self.existed:
            if not self.target.is_dir():
                raise PayloadError("Den oprindelige mappe mangler under tilbagerulning")
        elif self.target.exists():
            if self.stage.exists():
                raise PayloadError("Gendannelsens mapper kan ikke entydigt tilbageføres")
            replace_with_retry(self.target, self.stage)

    def clean(self) -> None:
        for directory in (self.stage.parent, self.previous):
            if directory.exists():
                shutil.rmtree(directory)


@dataclass(frozen=True, slots=True)
class RestoreTransaction:
    marker: Path
    identifier: str
    config: RestoreTree
    data: RestoreTree
    rollback: Path
    phase: RestorePhase

    @classmethod
    def start(cls, paths: AppPaths, rollback: Path) -> RestoreTransaction:
        identifier = uuid4().hex
        config = paths.config_dir.resolve()
        data = paths.data_dir.resolve()
        marker = restore_journal_path(config)
        if any(root.exists() and not root.is_dir() for root in (config, data)):
            raise PayloadError("Konfiguration og data skal være mapper")
        transaction = cls(
            marker, identifier,
            RestoreTree.from_target(config, "config", identifier, config.exists()),
            RestoreTree.from_target(data, "data", identifier, data.exists()),
            rollback, "staging",
        )
        if any(tree.stage.parent.exists() or tree.previous.exists()
               for tree in (transaction.config, transaction.data)):
            raise PayloadError("Gendannelsens arbejdsmapper findes allerede")
        transaction.save()
        return transaction

    @classmethod
    def load(cls, paths: AppPaths) -> RestoreTransaction | None:
        config, data = paths.config_dir.resolve(), paths.data_dir.resolve()
        marker = restore_journal_path(config)
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise PayloadError("Gendannelsesjournalen kunne ikke læses") from exc
        try:
            identifier = payload["id"]
            phase = payload["phase"]
            if (
                type(payload["schema_version"]) is not int or payload["schema_version"] != 1
                or UUID(hex=identifier).hex != identifier
                or phase not in {"staging", "prepared", "committed"}
                or payload["data_root"] != str(data)
                or type(payload["config_existed"]) is not bool
                or type(payload["data_existed"]) is not bool
                or not isinstance(payload["rollback"], str)
                or Path(payload["rollback"]).name != payload["rollback"]
            ):
                raise ValueError("Invalid restore journal")
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise PayloadError("Gendannelsesjournalen matcher ikke installationen") from exc
        return cls(
            marker, identifier,
            RestoreTree.from_target(config, "config", identifier, payload["config_existed"]),
            RestoreTree.from_target(data, "data", identifier, payload["data_existed"]),
            paths.backup_dir / payload["rollback"], phase,
        )

    def save(self) -> None:
        replace_text_atomically(self.marker, json.dumps({
            "schema_version": 1, "id": self.identifier, "phase": self.phase,
            "data_root": str(self.data.target), "config_existed": self.config.existed,
            "data_existed": self.data.existed, "rollback": self.rollback.name,
        }) + "\n", durable=True)

    def install(self) -> None:
        RestoreFence(self.identifier, self.config.target, pending=True).save(self.data.target)
        replace(self, phase="prepared").save()
        self.config.install()
        self.data.install()
        replace(self, phase="committed").save()

    def recover(self) -> None:
        if self.phase == "prepared":
            self.data.rollback()
            self.config.rollback()
        elif self.phase == "committed":
            if not self.config.target.is_dir() or not self.data.target.is_dir():
                raise PayloadError("En installeret gendannelsesmappe mangler")
        for tree in (self.config, self.data):
            tree.clean()
        self.marker.unlink()


def recover_restore(paths: AppPaths) -> bool:
    """Finish recovery before clients read an interrupted installation."""
    fence = RestoreFence.load(paths.data_dir)
    if not (
        (fence is not None and fence.pending)
        or restore_journal_path(paths.config_dir).exists()
    ):
        return False
    with exclusive_data_operation(paths.data_dir, allow_pending_restore=True), exclusive_configuration_update(
        paths.config_dir, allow_pending_restore=True,
    ):
        return recover_restore_locked(paths)


def recover_restore_locked(paths: AppPaths) -> bool:
    transaction = RestoreTransaction.load(paths)
    fence = RestoreFence.load(paths.data_dir)
    pending = fence is not None and fence.pending
    if pending and (
        fence.config_dir != paths.config_dir.resolve()
        or (transaction is not None and transaction.identifier != fence.identifier)
    ):
        raise PayloadError("Gendannelsens datatilstand matcher ikke installationen")
    if transaction is not None:
        transaction.recover()
    if pending:
        replace(fence, pending=False).save(paths.data_dir)
    return transaction is not None or pending
