"""Persistent watchlist alerts created only by explicit refresh actions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Literal

from .errors import PayloadError
from .hub_settings import WatchlistEntry, player_identity
from .persistence import aware_local, replace_text_atomically
from .storage import PlayerStatisticsIndex, PlayerStatisticsSnapshot


ANALYSIS_INBOX_SCHEMA_VERSION = 1
AlertKind = Literal["injured", "disabled", "inactive", "suspended", "removed", "sold"]


@dataclass(frozen=True, slots=True)
class WatchlistAlert:
    alert_id: str
    game_locale: str
    game_slug: str
    player_key: str
    player_name: str
    kind: AlertKind
    message: str
    detected_at: datetime
    round_number: int | None = None
    snapshot_generated_at: datetime | None = None
    read_at: datetime | None = None
    dismissed_at: datetime | None = None

    @property
    def is_unread(self) -> bool:
        return self.read_at is None and self.dismissed_at is None


_STATUS_MESSAGES: dict[AlertKind, str] = {
    "injured": "er blevet markeret som skadet",
    "disabled": "er blevet deaktiveret",
    "inactive": "er ikke længere aktiv",
    "suspended": "er blevet markeret med karantæne",
    "removed": "er fjernet fra spillerlisten; det kan være salg eller udtræden",
    "sold": "er blevet solgt",
}


def _statuses(entry) -> set[AlertKind]:
    result: set[AlertKind] = set()
    if not entry.is_active:
        result.add("inactive")
    if entry.is_disabled:
        result.add("disabled")
    if entry.is_injured:
        result.add("injured")
    if entry.has_suspension:
        result.add("suspended")
    return result


def _alert_id(
    game_locale: str,
    game_slug: str,
    player_key: str,
    kind: AlertKind,
    round_number: int | None,
) -> str:
    value = f"{game_locale.casefold()}|{game_slug}|{player_key}|{kind}|{round_number}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def build_watchlist_alerts(
    previous: PlayerStatisticsSnapshot | None,
    current: PlayerStatisticsSnapshot,
    watchlist: tuple[WatchlistEntry, ...],
    *,
    now: datetime | None = None,
) -> tuple[WatchlistAlert, ...]:
    """Return new adverse transitions for watched players without writing."""

    game = current.statistics.game
    watched = {
        item.player_key: item
        for item in watchlist
        if item.game_identity == (game.locale.casefold(), game.slug)
    }
    if not watched:
        return ()
    old_entries = (
        {}
        if previous is None
        else {
            player_identity(game, entry): entry
            for entry in previous.statistics.entries
        }
    )
    new_entries = {
        player_identity(game, entry): entry
        for entry in current.statistics.entries
    }
    detected = aware_local(now)
    alerts: list[WatchlistAlert] = []
    for key, watched_entry in watched.items():
        old = old_entries.get(key)
        new = new_entries.get(key)
        kinds: set[AlertKind] = set()
        if old is not None and new is None:
            kinds.add("removed")
        elif new is not None:
            kinds.update(_statuses(new) - (_statuses(old) if old is not None else set()))
        for kind in sorted(kinds):
            alerts.append(
                WatchlistAlert(
                    _alert_id(
                        game.locale,
                        game.slug,
                        key,
                        kind,
                        current.statistics.round_number,
                    ),
                    game.locale.casefold(),
                    game.slug,
                    key,
                    new.name if new is not None else watched_entry.name,
                    kind,
                    f"{new.name if new is not None else watched_entry.name} {_STATUS_MESSAGES[kind]}.",
                    detected,
                    current.statistics.round_number,
                    current.generated_at,
                )
            )
    return tuple(alerts)


def select_alert_baseline(
    index: PlayerStatisticsIndex,
    game,
    current_round: int,
) -> PlayerStatisticsSnapshot | None:
    """Choose newest same-round state, otherwise the latest earlier round."""

    eligible_rounds = tuple(
        snapshot.statistics.round_number
        for snapshot in index.for_game(game)
        if snapshot.statistics.round_number <= current_round
    )
    if not eligible_rounds:
        return None
    return index.newest(game, max(eligible_rounds))


class AnalysisInboxStore:
    """Atomically store alert state; loading is side-effect free."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> tuple[WatchlistAlert, ...]:
        if not self.path.exists():
            return ()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PayloadError(f"Alarmindbakken kunne ikke læses: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise PayloadError("Alarmindbakken indeholder ugyldig JSON") from exc
        if not isinstance(raw, dict) or raw.get("schema_version") != ANALYSIS_INBOX_SCHEMA_VERSION:
            raise PayloadError("Ukendt skema for alarmindbakken")
        items = raw.get("alerts", [])
        if not isinstance(items, list):
            raise PayloadError("Alarmindbakken mangler alerts-listen")
        return tuple(_alert_from_dict(item, index) for index, item in enumerate(items))

    def save(self, alerts: tuple[WatchlistAlert, ...]) -> None:
        unique = {item.alert_id: item for item in alerts}
        ordered = tuple(
            sorted(unique.values(), key=lambda item: (item.detected_at, item.alert_id), reverse=True)
        )
        payload = {
            "schema_version": ANALYSIS_INBOX_SCHEMA_VERSION,
            "alerts": [_alert_to_dict(item) for item in ordered],
        }
        replace_text_atomically(
            self.path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def merge(self, alerts: tuple[WatchlistAlert, ...]) -> tuple[WatchlistAlert, ...]:
        existing = self.load()
        by_id = {item.alert_id: item for item in existing}
        added = False
        for item in alerts:
            if item.alert_id not in by_id:
                by_id[item.alert_id] = item
                added = True
        result = tuple(sorted(by_id.values(), key=lambda item: (item.detected_at, item.alert_id), reverse=True))
        if added:
            self.save(result)
        return result

    def mark_read(self, alert_id: str, *, now: datetime | None = None) -> tuple[WatchlistAlert, ...]:
        timestamp = aware_local(now)
        updated = tuple(
            replace(item, read_at=item.read_at or timestamp)
            if item.alert_id == alert_id else item
            for item in self.load()
        )
        self.save(updated)
        return updated

    def dismiss(self, alert_id: str, *, now: datetime | None = None) -> tuple[WatchlistAlert, ...]:
        timestamp = aware_local(now)
        updated = tuple(
            replace(item, dismissed_at=timestamp, read_at=item.read_at or timestamp)
            if item.alert_id == alert_id else item
            for item in self.load()
        )
        self.save(updated)
        return updated

    def clear_dismissed(
        self,
        *,
        game_identity: tuple[str, str] | None = None,
    ) -> tuple[WatchlistAlert, ...]:
        """Remove dismissed alerts globally or for one exact game identity."""

        normalized_identity = (
            None
            if game_identity is None
            else (game_identity[0].casefold(), game_identity[1])
        )
        updated = tuple(
            item
            for item in self.load()
            if item.dismissed_at is None
            or (
                normalized_identity is not None
                and (item.game_locale, item.game_slug) != normalized_identity
            )
        )
        self.save(updated)
        return updated


def _timestamp(value: object, label: str, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if not isinstance(value, str):
        raise PayloadError(f"{label} skal være et tidspunkt")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PayloadError(f"{label} skal være et ISO-tidspunkt") from exc
    return parsed.astimezone() if parsed.tzinfo is None else parsed


def _alert_from_dict(raw: object, index: int) -> WatchlistAlert:
    if not isinstance(raw, dict):
        raise PayloadError(f"alerts[{index}] skal være et objekt")
    required = ("alert_id", "game_locale", "game_slug", "player_key", "player_name", "kind", "message")
    if any(not isinstance(raw.get(key), str) or not str(raw[key]).strip() for key in required):
        raise PayloadError(f"alerts[{index}] mangler tekstfelter")
    kind = str(raw["kind"])
    if kind not in _STATUS_MESSAGES:
        raise PayloadError(f"alerts[{index}] har ukendt type")
    round_number = raw.get("round_number")
    if round_number is not None and (not isinstance(round_number, int) or isinstance(round_number, bool)):
        raise PayloadError(f"alerts[{index}].round_number skal være et heltal")
    return WatchlistAlert(
        str(raw["alert_id"]), str(raw["game_locale"]).casefold(), str(raw["game_slug"]),
        str(raw["player_key"]), str(raw["player_name"]), kind, str(raw["message"]),
        _timestamp(raw.get("detected_at"), "detected_at"),
        round_number,
        _timestamp(raw.get("snapshot_generated_at"), "snapshot_generated_at", optional=True),
        _timestamp(raw.get("read_at"), "read_at", optional=True),
        _timestamp(raw.get("dismissed_at"), "dismissed_at", optional=True),
    )


def _alert_to_dict(item: WatchlistAlert) -> dict[str, object]:
    return {
        "alert_id": item.alert_id,
        "game_locale": item.game_locale,
        "game_slug": item.game_slug,
        "player_key": item.player_key,
        "player_name": item.player_name,
        "kind": item.kind,
        "message": item.message,
        "detected_at": item.detected_at.isoformat(),
        "round_number": item.round_number,
        "snapshot_generated_at": item.snapshot_generated_at.isoformat() if item.snapshot_generated_at else None,
        "read_at": item.read_at.isoformat() if item.read_at else None,
        "dismissed_at": item.dismissed_at.isoformat() if item.dismissed_at else None,
    }
