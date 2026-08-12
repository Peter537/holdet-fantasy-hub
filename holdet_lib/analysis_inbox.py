"""Persistent watchlist alerts created only by explicit refresh actions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
from statistics import mean
from typing import Literal

from .errors import PayloadError
from .hub_settings import WatchRule, WatchlistEntry, player_identity
from .persistence import aware_local, replace_text_atomically
from .storage import PlayerStatisticsIndex, PlayerStatisticsSnapshot


ANALYSIS_INBOX_SCHEMA_VERSION = 2
AlertKind = Literal[
    "injured", "disabled", "inactive", "suspended", "removed", "sold",
    "activated", "recovered", "status_change", "value_drop", "value_rise",
    "form3_above", "form3_below", "form5_above", "form5_below",
]


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
    rule_id: str | None = None
    previous_snapshot_generated_at: datetime | None = None
    transition: str | None = None

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
    "activated": "er blevet aktiveret",
    "recovered": "har fået en forbedret status",
    "status_change": "har ændret status",
    "value_drop": "har krydset den valgte tærskel for prisfald",
    "value_rise": "har krydset den valgte tærskel for prisstigning",
    "form3_above": "har krydset Form 3-tærsklen opad",
    "form3_below": "har krydset Form 3-tærsklen nedad",
    "form5_above": "har krydset Form 5-tærsklen opad",
    "form5_below": "har krydset Form 5-tærsklen nedad",
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
    *,
    rule_id: str | None = None,
    previous_generated_at: datetime | None = None,
    current_generated_at: datetime | None = None,
    transition: str | None = None,
) -> str:
    value = "|".join(
        (
            game_locale.casefold(), game_slug, player_key, kind,
            str(round_number), rule_id or "legacy",
            previous_generated_at.isoformat() if previous_generated_at else "",
            current_generated_at.isoformat() if current_generated_at else "",
            transition or "",
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


def _status_label(entry) -> str:
    values = sorted(_statuses(entry))
    return ",".join(values) if values else "active"


def _price_signal(rule: WatchRule, before, after) -> float | None:
    if before is None or after is None:
        return None
    delta = float(after.value - before.value)
    if rule.threshold_unit == "percent":
        if before.value == 0:
            return None
        delta = delta / abs(before.value) * 100
    return -delta if rule.kind == "value_drop" else delta


def _form_value(
    values: tuple[float | None, float | None] | None, kind: str
) -> float | None:
    if values is None:
        return None
    return values[0] if kind.startswith("form3") else values[1]


def _watch_rule_transition(
    rule: WatchRule,
    earlier,
    previous,
    current,
    earlier_forms: tuple[float | None, float | None] | None,
    previous_forms: tuple[float | None, float | None] | None,
    current_forms: tuple[float | None, float | None] | None,
) -> tuple[AlertKind, str] | None:
    if rule.kind == "status_change":
        if previous is None:
            return None
        if current is None:
            return "removed", f"{_status_label(previous)} → removed"
        before, after = _status_label(previous), _status_label(current)
        if before == after:
            return None
        added = _statuses(current) - _statuses(previous)
        if added:
            kind = sorted(added)[0]
        elif after == "active":
            kind = (
                "activated"
                if not previous.is_active or previous.is_disabled
                else "recovered"
            )
        elif len(_statuses(current)) < len(_statuses(previous)):
            kind = "recovered"
        else:
            kind = "status_change"
        return kind, f"{before} → {after}"
    assert rule.threshold is not None
    if rule.kind in {"value_drop", "value_rise"}:
        old_signal = _price_signal(rule, earlier, previous)
        new_signal = _price_signal(rule, previous, current)
        if old_signal is None or new_signal is None:
            return None
        if old_signal < rule.threshold <= new_signal:
            suffix = "%" if rule.threshold_unit == "percent" else ""
            return rule.kind, f"{old_signal:.2f}{suffix} → {new_signal:.2f}{suffix}"
        return None
    old_form = _form_value(previous_forms, rule.kind)
    new_form = _form_value(current_forms, rule.kind)
    if old_form is None or new_form is None:
        return None
    crossed = (
        old_form <= rule.threshold < new_form
        if rule.kind.endswith("above")
        else old_form >= rule.threshold > new_form
    )
    return (
        (rule.kind, f"{old_form:.2f} → {new_form:.2f}")
        if crossed
        else None
    )


def watch_form_signals(
    index: PlayerStatisticsIndex,
    target: PlayerStatisticsSnapshot,
) -> dict[str, tuple[float | None, float | None]]:
    """Return Form 3/5 as known at one immutable snapshot timestamp."""

    game = target.statistics.game
    completed: dict[str, dict[int, int]] = {}
    for snapshot in sorted(index.for_game(game), key=lambda item: item.generated_at):
        if snapshot.generated_at > target.generated_at:
            break
        if snapshot.statistics.round_status != "complete":
            continue
        for entry in snapshot.statistics.entries:
            if entry.round_growth is not None:
                completed.setdefault(player_identity(game, entry), {})[
                    snapshot.statistics.round_number
                ] = entry.round_growth
    result: dict[str, tuple[float | None, float | None]] = {}
    for key, rounds in completed.items():
        values = [value for _, value in sorted(rounds.items())]
        result[key] = (
            mean(values[-3:]) if len(values) >= 3 else None,
            mean(values[-5:]) if len(values) >= 5 else None,
        )
    return result


def build_watchlist_alerts(
    previous: PlayerStatisticsSnapshot | None,
    current: PlayerStatisticsSnapshot,
    watchlist: tuple[WatchlistEntry, ...],
    *,
    now: datetime | None = None,
    prior: PlayerStatisticsSnapshot | None = None,
    previous_forms: dict[str, tuple[float | None, float | None]] | None = None,
    current_forms: dict[str, tuple[float | None, float | None]] | None = None,
    prior_forms: dict[str, tuple[float | None, float | None]] | None = None,
) -> tuple[WatchlistAlert, ...]:
    """Return threshold crossings for watched players without writing."""

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
    prior_entries = (
        {}
        if prior is None
        else {
            player_identity(game, entry): entry
            for entry in prior.statistics.entries
        }
    )
    new_entries = {
        player_identity(game, entry): entry
        for entry in current.statistics.entries
    }
    detected = aware_local(now)
    alerts: list[WatchlistAlert] = []
    for key, watched_entry in watched.items():
        earlier = prior_entries.get(key)
        old = old_entries.get(key)
        new = new_entries.get(key)
        rules = watched_entry.rules
        transitions: list[tuple[WatchRule, AlertKind, str]] = []
        for rule in rules:
            transition = _watch_rule_transition(
                rule,
                earlier,
                old,
                new,
                None if prior_forms is None else prior_forms.get(key),
                None if previous_forms is None else previous_forms.get(key),
                None if current_forms is None else current_forms.get(key),
            )
            if transition is not None:
                transitions.append((rule, *transition))
        for rule, kind, transition in transitions:
            player_name = new.name if new is not None else watched_entry.name
            alerts.append(
                WatchlistAlert(
                    _alert_id(
                        game.locale,
                        game.slug,
                        key,
                        kind,
                        current.statistics.round_number,
                        rule_id=rule.rule_id,
                        previous_generated_at=(
                            previous.generated_at if previous is not None else None
                        ),
                        current_generated_at=current.generated_at,
                        transition=transition,
                    ),
                    game.locale.casefold(),
                    game.slug,
                    key,
                    player_name,
                    kind,
                    f"{player_name} {_STATUS_MESSAGES[kind]} ({transition}).",
                    detected,
                    current.statistics.round_number,
                    current.generated_at,
                    None,
                    None,
                    rule.rule_id,
                    previous.generated_at if previous is not None else None,
                    transition,
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
        if not isinstance(raw, dict) or raw.get("schema_version") not in {
            1, ANALYSIS_INBOX_SCHEMA_VERSION
        }:
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
        str(raw["rule_id"]) if isinstance(raw.get("rule_id"), str) else None,
        _timestamp(
            raw.get("previous_snapshot_generated_at"),
            "previous_snapshot_generated_at",
            optional=True,
        ),
        str(raw["transition"]) if isinstance(raw.get("transition"), str) else None,
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
        "rule_id": item.rule_id,
        "previous_snapshot_generated_at": (
            item.previous_snapshot_generated_at.isoformat()
            if item.previous_snapshot_generated_at
            else None
        ),
        "transition": item.transition,
    }
