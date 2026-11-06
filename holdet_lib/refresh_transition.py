"""Durable inputs and output for one refresh's player-alert transition."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from uuid import UUID, uuid4

from .errors import PayloadError
from .hub_settings import HubSettings, _settings_from_dict, _settings_to_dict
from .persistence import publish_immutable_text
from .player_serialization import player_statistics_from_dict, player_statistics_to_json


class RecoveryCheckpointError(RuntimeError):
    """A required recovery checkpoint could not be persisted."""


class PlayerTransition:
    def __init__(self, data_dir, run_id, game, player_store, checkpoint, save):
        self.data_dir = Path(data_dir).resolve()
        self.run_id = str(UUID(run_id))
        self.game = game
        self.player_store = player_store
        self.record = checkpoint
        self.save = save
        if checkpoint is not None:
            self._validate()

    def _relative(self, path):
        return Path(path).resolve().relative_to(self.data_dir).as_posix()

    def _path(self, raw, root):
        if not isinstance(raw, str) or Path(raw).is_absolute():
            raise PayloadError("Ugyldig reference i opdateringsjournalen")
        path = (self.data_dir / raw).resolve()
        if not path.is_relative_to(root.resolve()):
            raise PayloadError("Reference i opdateringsjournalen er uden for datamappen")
        return path

    def _validate(self):
        value = self.record
        try:
            attempt = str(UUID(value["attempt_id"]))
            if attempt != value["attempt_id"]:
                raise ValueError
            stage = f"refresh-runs/{self.run_id}/player-{attempt}.json"
            if value["stage_path"] != stage or not isinstance(value["baseline_paths"], list):
                raise ValueError
            if not isinstance(value["alerts_complete"], bool):
                raise ValueError
            self.alert_settings()
            requested = value.get("requested_round")
            if requested is not None and (isinstance(requested, bool) or not isinstance(requested, int) or requested < 0):
                raise ValueError
            self._path(stage, self.data_dir / "refresh-runs" / self.run_id)
            for raw in value["baseline_paths"]:
                self._path(raw, self.player_store.snapshot_dir)
            if value["snapshot_path"] is not None:
                self._path(value["snapshot_path"], self.player_store._game_directory(self.game))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise PayloadError("Ugyldig spillerjournal") from exc

    def _checkpoint(self):
        try:
            self.save(dict(self.record))
        except Exception as exc:
            raise RecoveryCheckpointError("Spillerjournalen kunne ikke gemmes") from exc

    def alert_settings(self):
        value = self.record.get("alert_settings")
        if not isinstance(value, dict):
            raise ValueError("Spillerjournalen mangler alarmindstillinger")
        return _settings_from_dict({**_settings_to_dict(HubSettings()), **value})

    def prepare(self, snapshots, requested_round=None, settings=None):
        if self.record is not None:
            return
        attempt = str(uuid4())
        serialized = _settings_to_dict(settings or HubSettings())
        alert_settings = {"schema_version": serialized["schema_version"],
                          "enabled_features": ["alerts"],
                          "watchlist": [item for item in serialized["watchlist"]
                                        if item["game_locale"].casefold() == self.game.locale.casefold()
                                        and item["game_slug"] == self.game.slug]}
        self.record = {"attempt_id": attempt,
                       "stage_path": f"refresh-runs/{self.run_id}/player-{attempt}.json",
                       "baseline_paths": [self._relative(item.path) for item in snapshots],
                       "snapshot_path": None, "alerts_complete": False,
                       "requested_round": requested_round, "alert_settings": alert_settings}
        self._checkpoint()

    def baseline(self, snapshots):
        expected = set(self.record["baseline_paths"])
        selected = tuple(item for item in snapshots if self._relative(item.path) in expected)
        if {self._relative(item.path) for item in selected} != expected:
            raise PayloadError("Spillerjournalens tidligere snapshots mangler")
        return selected

    def staged(self):
        return self.record is not None and self._path(
            self.record["stage_path"], self.data_dir / "refresh-runs" / self.run_id
        ).is_file()

    def stage(self, statistics, generated_at):
        path = self._path(self.record["stage_path"], self.data_dir / "refresh-runs" / self.run_id)
        content = player_statistics_to_json(statistics, generated_at=generated_at)
        publish_immutable_text(path, content)

    def publish(self, expected_round=None):
        path = self._path(self.record["stage_path"], self.data_dir / "refresh-runs" / self.run_id)
        try:
            content = path.read_text(encoding="utf-8")
            value = json.loads(content)
            statistics = player_statistics_from_dict(value)
            generated_at = datetime.fromisoformat(value["generated_at"])
            if (generated_at.utcoffset() is None or not statistics.entries
                or statistics.round_number < 0
                or (self.record.get("requested_round") is not None
                    and statistics.round_number != self.record["requested_round"])
                or (expected_round is not None and statistics.round_number != expected_round)
                or (statistics.game.locale.casefold(), statistics.game.slug)
                != (self.game.locale.casefold(), self.game.slug)):
                raise ValueError
        except (OSError, ValueError, TypeError, KeyError, PayloadError) as exc:
            raise PayloadError("Spillerjournalens gemte data er ugyldige") from exc
        directory = self.player_store._game_directory(self.game)
        suffix = f"_recovery-{self.record['attempt_id']}.json"
        if self.record["snapshot_path"] is None:
            target = directory / f"player-round{statistics.round_number}{suffix}"
        else:
            target = self._path(self.record["snapshot_path"], directory)
            if (target.parent != directory.resolve()
                or not target.name.startswith(f"player-round{statistics.round_number}_")
                or not target.name.endswith(suffix)):
                raise PayloadError("Spillerjournalens snapshot stemmer ikke med de gemte data")
        reference = self._relative(target)
        try:
            publish_immutable_text(target, content)
        except FileExistsError:
            if target.read_bytes() != content.encode("utf-8"):
                raise PayloadError("Spillersnapshottet stemmer ikke med journalen")
        self.record["snapshot_path"] = reference
        baseline = self.baseline(self.player_store.scan(self.game).for_game(self.game))
        if baseline and statistics.round_number < max(item.statistics.round_number for item in baseline):
            self.record["alerts_complete"] = True
        self._checkpoint()
        return statistics, target

    def complete_alerts(self):
        self.record["alerts_complete"] = True
        self._checkpoint()
