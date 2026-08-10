"""Cached, versioned game metadata written only after explicit fetches."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
from typing import Any, Literal

from .errors import PayloadError
from .models import GameUrl, ScheduleRound
from .output import sanitize_path_component
from .persistence import aware_local, publish_immutable_text, replace_text_atomically
from .rules import (
    GameRuleProfile,
    game_rule_from_dict,
    game_rule_to_dict,
    rule_profile_for_game,
)
from .teams import GameContext


GAME_METADATA_SCHEMA_VERSION = 2
GAME_METADATA_REVISION_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class GameMetadata:
    game: GameUrl
    variant: str
    format: str
    game_id: int
    salary_cap: int
    final_round: int | None
    display_name: str | None
    rounds: tuple[ScheduleRound, ...]
    fetched_at: datetime
    rule_profile: GameRuleProfile | None = None

    @property
    def identity(self) -> tuple[str, str]:
        return self.game.locale.casefold(), self.game.slug

    @property
    def unit(self) -> str:
        return "money" if self.salary_cap > 0 else "points"

    @property
    def active_round(self) -> int | None:
        now = datetime.now().astimezone()
        active = [item.round_number for item in self.rounds if item.start <= now <= item.end]
        return max(active, default=None)

    @property
    def next_deadline(self) -> datetime | None:
        now = datetime.now().astimezone()
        future = [item.close for item in self.rounds if item.close > now]
        return min(future, default=None)


@dataclass(frozen=True, slots=True)
class MetadataChange:
    """One explainable rules or schedule change between metadata fetches."""

    kind: Literal["game", "rules", "schedule"]
    field: str
    old_value: object | None
    new_value: object | None
    round_number: int | None = None


@dataclass(frozen=True, slots=True)
class GameMetadataRevision:
    """Immutable metadata capture created by an explicit refresh."""

    metadata: GameMetadata
    changes: tuple[MetadataChange, ...] = ()


def _target_round(metadata: GameMetadata) -> int | None:
    fetched_at = metadata.fetched_at
    active = [
        item.round_number
        for item in metadata.rounds
        if item.start <= fetched_at <= item.end
    ]
    if active:
        return max(active)
    future = [
        item.round_number for item in metadata.rounds if item.start > fetched_at
    ]
    return min(future, default=metadata.final_round)


def compare_game_metadata(
    previous: GameMetadata,
    current: GameMetadata,
) -> tuple[MetadataChange, ...]:
    """Return the substantive fields the local parser can explain safely."""

    changes: list[MetadataChange] = []
    target_round = _target_round(current)
    scalar_fields = (
        ("variant", previous.variant, current.variant),
        ("format", previous.format, current.format),
        ("game_id", previous.game_id, current.game_id),
        ("salary_cap", previous.salary_cap, current.salary_cap),
        ("final_round", previous.final_round, current.final_round),
    )
    for field, old_value, new_value in scalar_fields:
        if old_value != new_value:
            changes.append(
                MetadataChange(
                    "game", field, old_value, new_value, target_round
                )
            )
    old_rules = (
        None
        if previous.rule_profile is None
        else game_rule_to_dict(previous.rule_profile)
    )
    new_rules = (
        None
        if current.rule_profile is None
        else game_rule_to_dict(current.rule_profile)
    )
    if old_rules != new_rules:
        changes.append(
            MetadataChange(
                "rules", "rule_profile", old_rules, new_rules, target_round
            )
        )

    old_rounds = {item.round_number: item for item in previous.rounds}
    new_rounds = {item.round_number: item for item in current.rounds}
    for round_number in sorted(old_rounds.keys() | new_rounds.keys()):
        old_round = old_rounds.get(round_number)
        new_round = new_rounds.get(round_number)
        if old_round is None:
            changes.append(
                MetadataChange(
                    "schedule", "round_added", None, round_number, round_number
                )
            )
            continue
        if new_round is None:
            changes.append(
                MetadataChange(
                    "schedule", "round_removed", round_number, None, round_number
                )
            )
            continue
        for field in ("start", "close", "end"):
            old_value = getattr(old_round, field)
            new_value = getattr(new_round, field)
            if old_value != new_value:
                changes.append(
                    MetadataChange(
                        "schedule",
                        field,
                        old_value.isoformat(),
                        new_value.isoformat(),
                        round_number,
                    )
                )
    return tuple(changes)


def game_metadata_from_context(
    context: GameContext, *, fetched_at: datetime | None = None
) -> GameMetadata:
    game = context.game
    return GameMetadata(
        game,
        context.variant,
        context.format,
        context.game_id,
        context.salary_cap,
        context.final_round,
        context.display_name,
        context.rounds,
        aware_local(fetched_at),
        rule_profile_for_game(
            game,
            game_id=context.game_id,
            salary_cap=context.salary_cap,
            label=context.display_name,
        ),
    )


def _metadata_to_dict(metadata: GameMetadata) -> dict[str, object]:
    return {
        "schema_version": GAME_METADATA_SCHEMA_VERSION,
        "fetched_at": metadata.fetched_at.isoformat(),
        "game": {
            "url": metadata.game.original,
            "locale": metadata.game.locale,
            "slug": metadata.game.slug,
            "variant": metadata.variant,
            "format": metadata.format,
            "game_id": metadata.game_id,
            "salary_cap": metadata.salary_cap,
            "final_round": metadata.final_round,
            "display_name": metadata.display_name,
        },
        "rounds": [
            {
                "round": item.round_number,
                "start": item.start.isoformat(),
                "close": item.close.isoformat(),
                "end": item.end.isoformat(),
            }
            for item in metadata.rounds
        ],
        "rules": (
            game_rule_to_dict(metadata.rule_profile)
            if metadata.rule_profile is not None
            else None
        ),
    }


def _text(value: object, label: str, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value.strip():
        raise PayloadError(f"Metadatafeltet {label} skal være tekst")
    return value.strip()


def _integer(value: object, label: str, *, optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise PayloadError(f"Metadatafeltet {label} skal være et heltal")
    return value


def _timestamp(value: object, label: str) -> datetime:
    text = _text(value, label)
    assert text is not None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PayloadError(f"Metadatafeltet {label} skal være et ISO-tidspunkt") from exc
    return parsed.astimezone() if parsed.tzinfo is None else parsed


def _metadata_from_dict(payload: object) -> GameMetadata:
    if not isinstance(payload, dict):
        raise PayloadError("Metadataroden skal være et objekt")
    schema_version = payload.get("schema_version")
    if schema_version not in {1, GAME_METADATA_SCHEMA_VERSION}:
        raise PayloadError("Ukendt skema for spilmetadata")
    game = payload.get("game")
    rounds = payload.get("rounds")
    if not isinstance(game, dict) or not isinstance(rounds, list):
        raise PayloadError("Spilmetadata mangler game eller rounds")
    parsed_rounds: list[ScheduleRound] = []
    for index, raw in enumerate(rounds):
        if not isinstance(raw, dict):
            raise PayloadError(f"Metadatafeltet rounds[{index}] skal være et objekt")
        round_number = _integer(raw.get("round"), "round")
        assert round_number is not None
        parsed_rounds.append(
            ScheduleRound(
                round_number,
                _timestamp(raw.get("start"), "start"),
                _timestamp(raw.get("close"), "close"),
                _timestamp(raw.get("end"), "end"),
            )
        )
    url = _text(game.get("url"), "game.url")
    locale = _text(game.get("locale"), "game.locale")
    slug = _text(game.get("slug"), "game.slug")
    variant = _text(game.get("variant"), "game.variant")
    game_format = _text(game.get("format"), "game.format")
    game_id = _integer(game.get("game_id"), "game.game_id")
    salary_cap = _integer(game.get("salary_cap"), "game.salary_cap")
    assert all(value is not None for value in (url, locale, slug, variant, game_format))
    assert game_id is not None and salary_cap is not None
    rule_profile = None
    if schema_version == GAME_METADATA_SCHEMA_VERSION and payload.get("rules") is not None:
        try:
            rule_profile = game_rule_from_dict(payload.get("rules"))
        except ValueError as exc:
            raise PayloadError(f"Ugyldig regelprofil: {exc}") from exc
    return GameMetadata(
        GameUrl(url, locale, slug),
        variant,
        game_format,
        game_id,
        salary_cap,
        _integer(game.get("final_round"), "game.final_round", optional=True),
        _text(game.get("display_name"), "game.display_name", optional=True),
        tuple(sorted(parsed_rounds, key=lambda item: item.round_number)),
        _timestamp(payload.get("fetched_at"), "fetched_at"),
        rule_profile,
    )


def _revision_to_dict(revision: GameMetadataRevision) -> dict[str, object]:
    return {
        "schema_version": GAME_METADATA_REVISION_SCHEMA_VERSION,
        "metadata": _metadata_to_dict(revision.metadata),
        "changes": [
            {
                "kind": item.kind,
                "field": item.field,
                "old_value": item.old_value,
                "new_value": item.new_value,
                "round": item.round_number,
            }
            for item in revision.changes
        ],
    }


def _revision_from_dict(payload: object) -> GameMetadataRevision:
    if not isinstance(payload, dict):
        raise PayloadError("Metadatarevisionen skal være et objekt")
    if payload.get("schema_version") != GAME_METADATA_REVISION_SCHEMA_VERSION:
        raise PayloadError("Ukendt skema for metadatarevision")
    metadata = _metadata_from_dict(payload.get("metadata"))
    raw_changes = payload.get("changes", [])
    if not isinstance(raw_changes, list):
        raise PayloadError("Metadatarevisionens changes skal være en liste")
    changes: list[MetadataChange] = []
    for index, raw in enumerate(raw_changes):
        if not isinstance(raw, dict):
            raise PayloadError(f"Metadataændring {index} skal være et objekt")
        kind = raw.get("kind")
        field = raw.get("field")
        round_number = raw.get("round")
        if kind not in {"game", "rules", "schedule"}:
            raise PayloadError(f"Metadataændring {index} har ukendt type")
        if not isinstance(field, str) or not field:
            raise PayloadError(f"Metadataændring {index} mangler felt")
        if round_number is not None and (
            not isinstance(round_number, int) or isinstance(round_number, bool)
        ):
            raise PayloadError(f"Metadataændring {index} har ugyldig runde")
        changes.append(
            MetadataChange(
                kind,
                field,
                raw.get("old_value"),
                raw.get("new_value"),
                round_number,
            )
        )
    return GameMetadataRevision(metadata, tuple(changes))


class GameMetadataStore:
    """Maintain one current metadata document per game, without implicit writes."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)

    def _path(self, game: GameUrl) -> Path:
        locale = sanitize_path_component(game.locale.casefold(), fallback="da")
        slug = sanitize_path_component(game.slug, fallback="game")
        return self.directory / f"{locale}--{slug}.json"

    def _revision_directory(self, game: GameUrl) -> Path:
        locale = sanitize_path_component(game.locale.casefold(), fallback="da")
        slug = sanitize_path_component(game.slug, fallback="game")
        return self.directory / "revisions" / f"{locale}--{slug}"

    def _save_revision(self, revision: GameMetadataRevision) -> Path:
        directory = self._revision_directory(revision.metadata.game)
        stamp = revision.metadata.fetched_at.astimezone().strftime(
            "%Y%m%dT%H%M%S%f%z"
        )
        base = directory / f"metadata-{stamp}.json"
        candidate = base
        suffix = 1
        content = (
            json.dumps(
                _revision_to_dict(revision), ensure_ascii=False, indent=2
            )
            + "\n"
        )
        while True:
            if candidate.exists():
                try:
                    if candidate.read_text(encoding="utf-8") == content:
                        return candidate
                except OSError:
                    pass
                candidate = base.with_name(
                    f"{base.stem}-{suffix}{base.suffix}"
                )
                suffix += 1
                continue
            try:
                publish_immutable_text(candidate, content)
            except FileExistsError:
                candidate = base.with_name(
                    f"{base.stem}-{suffix}{base.suffix}"
                )
                suffix += 1
                continue
            return candidate

    def save(
        self, value: GameMetadata | GameContext, *, fetched_at: datetime | None = None
    ) -> GameMetadata:
        metadata = (
            value
            if isinstance(value, GameMetadata)
            else game_metadata_from_context(value, fetched_at=fetched_at)
        )
        try:
            previous = self.load(metadata.game)
        except (PayloadError, ValueError):
            # An explicit, validated refresh is the recovery path for a
            # corrupt canonical metadata document. No invalid baseline is
            # promoted into the immutable revision history.
            previous = None
        had_revision = False
        if previous is not None:
            revision_directory = self._revision_directory(metadata.game)
            had_revision = revision_directory.exists() and any(
                revision_directory.glob("metadata-*.json")
            )
            if not had_revision:
                self._save_revision(GameMetadataRevision(previous))
        changes = (
            ()
            if previous is None or not had_revision
            else compare_game_metadata(previous, metadata)
        )
        replace_text_atomically(
            self._path(metadata.game),
            json.dumps(_metadata_to_dict(metadata), ensure_ascii=False, indent=2) + "\n",
        )
        self._save_revision(GameMetadataRevision(metadata, changes))
        return metadata

    def load(self, game: GameUrl) -> GameMetadata | None:
        path = self._path(game)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PayloadError(f"Spilmetadata kunne ikke læses: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise PayloadError("Spilmetadata indeholder ugyldig JSON") from exc
        metadata = _metadata_from_dict(payload)
        if metadata.identity != (game.locale.casefold(), game.slug):
            raise PayloadError("Spilmetadata tilhører et andet spil")
        return metadata

    def scan(self) -> tuple[tuple[GameMetadata, ...], tuple[str, ...]]:
        if not self.directory.exists():
            return (), ()
        values: list[GameMetadata] = []
        warnings: list[str] = []
        for path in self.directory.glob("*.json"):
            try:
                values.append(_metadata_from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, PayloadError, ValueError) as exc:
                warnings.append(f"{path}: {exc}")
        values.sort(key=lambda item: (item.fetched_at, item.game.slug), reverse=True)
        return tuple(values), tuple(warnings)

    def revisions(
        self, game: GameUrl
    ) -> tuple[tuple[GameMetadataRevision, ...], tuple[str, ...]]:
        directory = self._revision_directory(game)
        if not directory.exists():
            return (), ()
        values: list[GameMetadataRevision] = []
        warnings: list[str] = []
        for path in directory.glob("metadata-*.json"):
            try:
                revision = _revision_from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
                if revision.metadata.identity != (
                    game.locale.casefold(),
                    game.slug,
                ):
                    raise PayloadError("Metadatarevisionen tilhører et andet spil")
                values.append(revision)
            except (OSError, json.JSONDecodeError, PayloadError, ValueError) as exc:
                warnings.append(f"{path}: {exc}")
        values.sort(key=lambda item: item.metadata.fetched_at, reverse=True)
        return tuple(values), tuple(warnings)

