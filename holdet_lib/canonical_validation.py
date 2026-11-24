"""Decode canonical backup and integrity entries with their runtime readers."""

from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import UUID

from .errors import PayloadError, ScraperError
from .filenames import PLAYER_SNAPSHOT_GLOB, TEAM_SNAPSHOT_GLOB, game_cache_stem
from .models import GameUrl


def is_refresh_run_file(relative: str) -> bool:
    """Recognize journal data, including a run-owned immutable player stage."""

    parts = PurePosixPath(relative).parts
    name = parts[-1] if parts else ""
    if name == "run.json" or (name.startswith("pending-") and name.endswith(".json")):
        return True
    if len(parts) != 2 or not name.startswith("player-") or not name.endswith(".json"):
        return False
    try:
        return str(UUID(parts[0])) == parts[0] and str(UUID(name[7:-5])) == name[7:-5]
    except ValueError:
        return False


def canonical_encoding(path: str) -> str:
    parts = path.casefold().split("/")
    if parts[:2] == ["data", "snapshots"] or parts[:3] == ["data", "imports", "legacy-json"]:
        return "utf-8-sig"
    return "utf-8"


def _configuration_decoder(name: str) -> Callable[[object], object] | None:
    from .analysis_inbox import _inbox_from_dict
    from .hub_settings import _settings_from_dict
    from .seasons import _seasons_from_dict
    from .strategies import _strategies_from_dict
    from .teams import _accounts_from_dict

    return {
        "config/accounts.json": _accounts_from_dict,
        "config/analysis-inbox.json": _inbox_from_dict,
        "config/hub-settings.json": _settings_from_dict,
        "config/seasons.json": _seasons_from_dict,
        "config/team-strategies.json": _strategies_from_dict,
    }.get(name)


def _revision_number(filename: str) -> int:
    number = filename.removeprefix("revision-").removesuffix(".json")
    if not filename.startswith("revision-") or not number.isdecimal() or int(number) < 1:
        raise PayloadError("Ugyldigt navn for turneringsrevision")
    return int(number)


def _validate_snapshot(payload: object, *, kind: Literal["player", "team"]) -> None:
    from .player_serialization import player_statistics_from_dict
    from .serialization import team_from_dict
    from .storage import _snapshot_generated_at

    _snapshot_generated_at(payload)
    if kind == "team":
        team_from_dict(payload)
    else:
        player_statistics_from_dict(payload)


def _validate_rule_file(parts: list[str], payload: dict[str, object]) -> None:
    from .rule_pages import _evidence_time, validate_capture
    from .rules import game_rule_from_dict

    if payload.get("schema_version") != 1 or len(parts) != 5:
        raise PayloadError("Ukendt rådgivningsschema")
    locale, slug, filename = parts[2:]
    if filename.startswith("capture-"):
        validate_capture(payload, GameUrl(f"https://www.holdet.dk/{locale}/fantasy/{slug}", locale, slug))
    elif filename.startswith("audit-"):
        _evidence_time(payload.get("reviewed_at"), "audit")
        profile = game_rule_from_dict(payload.get("profile"))
        if not profile.verified or profile.identity != (locale, slug):
            raise PayloadError("Regelgennemgangen har et ugyldigt spil eller status")


def _validate_cache_identity(stem: str, game: GameUrl, error: str) -> None:
    # Historical metadata may use a descriptive stem without a game address.
    if "--" in stem and stem.casefold() != game_cache_stem(game):
        raise PayloadError(error)


def _validate_document(
    path: str, payload: object, read_revision: Callable[[str], object] | None
) -> None:
    normalized = path.casefold()
    parts = path.split("/")
    area = tuple(normalized.split("/")[:2])
    if area[0] == "config" and (decoder := _configuration_decoder(normalized)) is not None:
        decoder(payload)
        return
    if not isinstance(payload, dict):
        raise PayloadError("JSON-roden skal være et objekt")
    if normalized == "config/groups.json":
        from .groups import _configuration_from_dict

        _configuration_from_dict(payload, read_revision=read_revision)
    elif area == ("data", "manifests"):
        from .storage import _manifest_from_dict

        _manifest_from_dict(payload, Path(path))
    elif area == ("data", "game-metadata"):
        from .game_metadata import _metadata_from_dict, _revision_from_dict

        if len(parts) > 3 and parts[2].casefold() == "revisions":
            revision = _revision_from_dict(payload)
            if len(parts) == 5 and parts[-1].casefold().startswith("metadata-"):
                _validate_cache_identity(
                    parts[3], revision.metadata.game, "Metadatarevisionen tilhører et andet spil"
                )
        else:
            metadata = _metadata_from_dict(payload)
            if len(parts) == 3:
                _validate_cache_identity(
                    PurePosixPath(parts[-1]).stem, metadata.game, "Spilmetadata tilhører et andet spil"
                )
    elif area == ("data", "fixtures"):
        from .fixtures import _fixture_from_dict

        fixtures = _fixture_from_dict(payload)
        if len(parts) == 3:
            _validate_cache_identity(
                PurePosixPath(parts[-1]).stem, fixtures.game, "Fixturecachen tilhører et andet spil"
            )
    elif area == ("data", "hall-of-fame"):
        from .hall_of_fame import _event_from_dict

        _event_from_dict(payload)
    elif area == ("data", "snapshots"):
        leaf = PurePosixPath(parts[-1])
        if leaf.match(PLAYER_SNAPSHOT_GLOB, case_sensitive=False):
            _validate_snapshot(payload, kind="player")
        elif leaf.match(TEAM_SNAPSHOT_GLOB, case_sensitive=False):
            _validate_snapshot(payload, kind="team")
    elif area == ("data", "group-revisions"):
        from .groups import _revision_from_dict

        if len(parts) != 4 or payload.get("schema_version") != 1:
            raise PayloadError("Ukendt revisionsschema")
        revision = _revision_from_dict(parts[2], payload)
        if revision.revision != _revision_number(parts[-1]):
            raise PayloadError("Turneringsrevisionen svarer ikke til filnavnet")
    elif area == ("data", "tournament-pairings"):
        from .tournament_pairings import _pairings_from_dict

        if len(parts) != 4:
            raise PayloadError("Ugyldigt lager for turneringsparringer")
        _pairings_from_dict(payload, parts[2], _revision_number(parts[-1]))
    elif area == ("data", "rules"):
        _validate_rule_file(parts, payload)
    elif area == ("data", "refresh-runs"):
        _validate_run_file(parts, payload)


def _validate_run_file(parts: list[str], payload: dict[str, object]) -> None:
    from .refresh_runs import _journal_from_dict
    from .refresh_transition import _staged_player_snapshot
    from .storage import _manifest_from_dict

    relative = "/".join(parts[2:])
    if len(parts) != 4 or not is_refresh_run_file(relative):
        raise PayloadError("Ukendt journalfil")
    filename = parts[-1]
    if filename == "run.json":
        _journal_from_dict(payload, parts[-2])
    elif filename.startswith("pending-"):
        _manifest_from_dict(payload, Path(*parts))
    else:
        _staged_player_snapshot(payload)


def known_payload_error(
    path: str, payload: object, *, read_revision: Callable[[str], object] | None = None
) -> str | None:
    try:
        _validate_document(path, payload, read_revision)
    except (OSError, ScraperError, ValueError, TypeError, KeyError, RecursionError) as exc:
        return f"{path}: {exc}"
    return None


def known_schema_error(
    path: str, data: bytes, *, read_revision: Callable[[str], object] | None = None
) -> str | None:
    if not path.casefold().endswith(".json"):
        return None
    try:
        payload = json.loads(data.decode(canonical_encoding(path)))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return f"{path}: ugyldig JSON"
    return known_payload_error(path, payload, read_revision=read_revision)
