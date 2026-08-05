"""Persistent group definitions, cached standings and on-demand refreshes."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import json
from pathlib import Path
import re
from uuid import uuid4
from urllib.parse import urlsplit

from .errors import PayloadError
from .models import GameUrl, TeamReference
from .persistence import publish_immutable_text, replace_text_atomically
from .players import normalize_game_url
from .teams import parse_direct_team_url
from .storage import TeamSnapshot
from .tournament import (
    GroupFixture,
    TournamentConfig,
    create_tournament_config,
    generate_draw_seed,
    tournament_schedule_signature,
    validate_tournament_config,
    create_tournament_definition,
)


GROUP_SCHEMA_VERSION = 8


@dataclass(frozen=True, slots=True)
class GroupTeam:
    team_id: int
    name: str
    source_url: str
    account_key: str = "direct"
    account_label: str = "Direkte URL"
    account_user_id: int | None = None
    profile_url: str | None = None

    def reference(self, game: GameUrl) -> TeamReference:
        return TeamReference(
            game=game,
            team_id=self.team_id,
            team_name=self.name,
            source_url=self.source_url,
            account_key=self.account_key,
            account_label=self.account_label,
            account_user_id=self.account_user_id,
            profile_url=self.profile_url,
        )


@dataclass(frozen=True, slots=True)
class TournamentRevision:
    revision: int
    archived_at: str
    reason: str
    teams: tuple[GroupTeam, ...]
    tournament: TournamentConfig


@dataclass(frozen=True, slots=True)
class ManagerGame:
    game: GameUrl
    name: str
    archived_at: str | None = None

    @property
    def identity(self) -> tuple[str, str]:
        return self.game.locale.casefold(), self.game.slug

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


@dataclass(frozen=True, slots=True)
class HubConfiguration:
    games: tuple[ManagerGame, ...]
    groups: tuple["GroupDefinition", ...]


@dataclass(frozen=True, slots=True)
class GroupDefinition:
    group_id: str
    name: str
    game: GameUrl
    teams: tuple[GroupTeam, ...]
    kind: str = "standings"
    tournament: TournamentConfig | None = None
    active_revision: int = 1
    archived_revisions: tuple[TournamentRevision, ...] = ()
    official_url: str | None = None
    official_link_type: str | None = None


def _validate_group_id(value: str) -> str:
    normalized = value.strip().casefold()
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", normalized):
        raise PayloadError("Gruppe-ID'et må kun indeholde små bogstaver, tal og bindestreger")
    return normalized


def validate_official_group_url(
    value: str | None,
    locale: str,
    link_type: str | None,
) -> tuple[str | None, str | None]:
    """Validate a manually supplied Holdet group or minileague URL."""

    if value is None or not value.strip():
        if link_type not in {None, ""}:
            raise PayloadError("Den officielle linktype kræver en officiel URL")
        return None, None
    if link_type not in {"group", "minileague"}:
        raise PayloadError("Den officielle linktype skal være group eller minileague")
    parsed = urlsplit(value.strip())
    try:
        port = parsed.port
    except ValueError as exc:
        raise PayloadError("Den officielle URL indeholder et ugyldigt portnummer") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != "www.holdet.dk"
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise PayloadError("Den officielle URL skal bruge HTTPS på www.holdet.dk og må ikke indeholde loginoplysninger")
    if not parsed.path.casefold().startswith(f"/{locale.casefold()}/"):
        raise PayloadError("Den officielle URL skal bruge managerspillets sprogpræfiks")
    return value.strip(), link_type


def _game_identity(game: GameUrl) -> tuple[str, str]:
    return game.locale.casefold(), game.slug


def normalize_manager_game(source: str | GameUrl, name: str = "") -> ManagerGame:
    if isinstance(source, GameUrl):
        game = source
    else:
        value = source.strip()
        if not value:
            raise PayloadError("Managerspillets URL eller slug må ikke være tom")
        if "://" not in value:
            value = f"https://www.holdet.dk/da/fantasy/{value}"
        game = normalize_game_url(value)
    game = GameUrl(
        f"https://www.holdet.dk/{game.locale}/fantasy/{game.slug}",
        game.locale,
        game.slug,
    )
    return ManagerGame(game, name.strip() or game.slug)


def _manager_game_to_dict(manager_game: ManagerGame) -> dict[str, object]:
    return {
        "name": manager_game.name,
        "archived_at": manager_game.archived_at,
        "game": {
            "url": manager_game.game.original,
            "locale": manager_game.game.locale,
            "slug": manager_game.game.slug,
        },
    }


def _manager_game_from_dict(raw: object) -> ManagerGame:
    if not isinstance(raw, dict) or not isinstance(raw.get("game"), dict):
        raise PayloadError("Posten for managerspillet skal indeholde spilmetadata")
    game_raw = raw["game"]
    game = GameUrl(
        original=str(game_raw.get("url", "")).strip(),
        locale=str(game_raw.get("locale", "")).strip(),
        slug=str(game_raw.get("slug", "")).strip(),
    )
    if not all((game.original, game.locale, game.slug)):
        raise PayloadError("Managerspillet har ugyldige spilmetadata")
    name = str(raw.get("name", "")).strip() or game.slug
    archived_at = raw.get("archived_at")
    if archived_at is not None:
        if not isinstance(archived_at, str) or not archived_at.strip():
            raise PayloadError("Managerspillet har et ugyldigt arkiveringstidspunkt")
        try:
            parsed = datetime.fromisoformat(archived_at)
        except ValueError as exc:
            raise PayloadError("Managerspillet har et ugyldigt arkiveringstidspunkt") from exc
        if parsed.tzinfo is None:
            raise PayloadError("Managerspillets arkiveringstidspunkt skal indeholde en tidszone")
    return replace(normalize_manager_game(game, name), archived_at=archived_at)


def _team_to_dict(team: GroupTeam) -> dict[str, object]:
    return {
        "id": team.team_id,
        "name": team.name,
        "source_url": team.source_url,
        "account_key": team.account_key,
        "account_label": team.account_label,
        "account_user_id": team.account_user_id,
        "profile_url": team.profile_url,
    }


def _teams_from_raw(group_id: str, raw: object) -> tuple[GroupTeam, ...]:
    if not isinstance(raw, list):
        raise PayloadError(f"Holdene i gruppe {group_id} skal være en liste")
    teams: dict[int, GroupTeam] = {}
    for item in raw:
        if not isinstance(item, dict):
            raise PayloadError(f"Et hold i gruppe {group_id} skal være et objekt")
        team_id = item.get("id")
        if not isinstance(team_id, int) or isinstance(team_id, bool):
            raise PayloadError(f"Et hold-ID i gruppe {group_id} skal være et heltal")
        source_url = str(item.get("source_url", "")).strip()
        if not source_url:
            raise PayloadError(f"Hold {team_id} i gruppe {group_id} mangler source_url")
        teams.setdefault(
            team_id,
            GroupTeam(
                team_id=team_id,
                name=str(item.get("name", "")).strip() or f"team-{team_id}",
                source_url=source_url,
                account_key=str(item.get("account_key") or "direct"),
                account_label=str(item.get("account_label") or "Direkte URL"),
                account_user_id=(
                    item.get("account_user_id")
                    if isinstance(item.get("account_user_id"), int)
                    else None
                ),
                profile_url=(
                    item.get("profile_url")
                    if isinstance(item.get("profile_url"), str)
                    else None
                ),
            ),
        )
    return tuple(teams.values())


def _tournament_to_dict(config: TournamentConfig) -> dict[str, object]:
    return {
        "start_round": config.start_round,
        "final_round": config.final_round,
        "rounds_per_tie": config.rounds_per_tie,
        "knockout_size": config.knockout_size,
        "draw_seed": config.draw_seed,
        "template": config.template,
        "match_points": list(config.match_points),
        "seed_rule": config.seed_rule,
        "seed_order": list(config.seed_order),
        "standings_tiebreakers": list(config.standings_tiebreakers),
        "knockout_tiebreakers": list(config.knockout_tiebreakers),
        "league_legs": config.league_legs,
        "swiss_rounds": config.swiss_rounds,
        "group_count": config.group_count,
        "qualifiers_per_group": config.qualifiers_per_group,
        "bronze_match": config.bronze_match,
        "group_fixtures": [
            {
                "round": fixture.round_number,
                "team_a_id": fixture.team_a_id,
                "team_b_id": fixture.team_b_id,
                "group_index": fixture.group_index,
            }
            for fixture in config.group_fixtures
        ],
    }


def _tournament_from_dict(group_id: str, raw: object) -> TournamentConfig:
    if not isinstance(raw, dict):
        raise PayloadError(f"Gruppe {group_id} mangler turneringsindstillinger")
    fixtures_raw = raw.get("group_fixtures")
    if not isinstance(fixtures_raw, list):
        raise PayloadError(f"Gruppe {group_id} mangler turneringskampe")
    fixtures: list[GroupFixture] = []
    for item in fixtures_raw:
        if not isinstance(item, dict):
            raise PayloadError(f"Gruppe {group_id} har en ugyldig turneringskamp")
        round_number = item.get("round")
        team_a_id = item.get("team_a_id")
        team_b_id = item.get("team_b_id")
        group_index = item.get("group_index", 0)
        if (
            not isinstance(round_number, int)
            or isinstance(round_number, bool)
            or not isinstance(team_a_id, int)
            or isinstance(team_a_id, bool)
            or (
                team_b_id is not None
                and (not isinstance(team_b_id, int) or isinstance(team_b_id, bool))
            )
            or not isinstance(group_index, int)
            or isinstance(group_index, bool)
        ):
            raise PayloadError(f"Gruppe {group_id} har en ugyldig turneringskamp")
        fixtures.append(GroupFixture(round_number, team_a_id, team_b_id, group_index))
    values = (
        raw.get("start_round"),
        raw.get("final_round"),
        raw.get("rounds_per_tie"),
        raw.get("knockout_size"),
    )
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        raise PayloadError(f"Gruppe {group_id} har ugyldige turneringsindstillinger")
    draw_seed = raw.get("draw_seed")
    if draw_seed is not None and (
        not isinstance(draw_seed, str) or not draw_seed.strip()
    ):
        raise PayloadError(f"Gruppe {group_id} har et ugyldigt seed til turneringslodtrækningen")
    stored_template = raw.get("template")
    template = str(stored_template or "group_knockout")
    match_points_raw = raw.get("match_points", [3, 1, 0])
    seed_order_raw = raw.get("seed_order", [])
    standings_raw = raw.get(
        "standings_tiebreakers",
        ["score_difference", "score_for", "head_to_head", "entry_seed"],
    )
    knockout_raw = raw.get(
        "knockout_tiebreakers",
        (
            ["higher_seed"]
            if stored_template is None
            else ["last_round_growth", "overall_total", "higher_seed"]
        ),
    )
    if (
        not isinstance(match_points_raw, list)
        or len(match_points_raw) != 3
        or any(not isinstance(item, int) or isinstance(item, bool) for item in match_points_raw)
        or not isinstance(seed_order_raw, list)
        or any(not isinstance(item, int) or isinstance(item, bool) for item in seed_order_raw)
        or not isinstance(standings_raw, list)
        or not all(isinstance(item, str) for item in standings_raw)
        or not isinstance(knockout_raw, list)
        or not all(isinstance(item, str) for item in knockout_raw)
    ):
        raise PayloadError(f"Gruppe {group_id} har ugyldige indstillinger for turneringsformatet")
    return TournamentConfig(
        *values,
        tuple(fixtures),
        draw_seed,
        template=template,
        match_points=tuple(match_points_raw),
        seed_rule=str(raw.get("seed_rule") or "random"),
        seed_order=tuple(seed_order_raw),
        standings_tiebreakers=tuple(standings_raw),
        knockout_tiebreakers=tuple(knockout_raw),
        league_legs=int(raw.get("league_legs", 1)),
        swiss_rounds=(
            int(raw["swiss_rounds"])
            if isinstance(raw.get("swiss_rounds"), int)
            else None
        ),
        group_count=int(raw.get("group_count", 1)),
        qualifiers_per_group=(
            int(raw["qualifiers_per_group"])
            if isinstance(raw.get("qualifiers_per_group"), int)
            else None
        ),
        bronze_match=bool(raw.get("bronze_match", False)),
    )
    return TournamentConfig(*values, tuple(fixtures), draw_seed)


def _revision_to_dict(
    group_id: str,
    revision: TournamentRevision,
    *,
    external: bool,
) -> dict[str, object]:
    result: dict[str, object] = {
        "revision": revision.revision,
        "archived_at": revision.archived_at,
        "reason": revision.reason,
    }
    if external:
        result["file"] = f"{group_id}/revision-{revision.revision}.json"
    else:
        result["group_id"] = group_id
        result["teams"] = [_team_to_dict(team) for team in revision.teams]
        result["tournament"] = _tournament_to_dict(revision.tournament)
    return result


def _revision_from_dict(group_id: str, raw: object) -> TournamentRevision:
    if not isinstance(raw, dict):
        raise PayloadError(f"Gruppe {group_id} har en ugyldig revision")
    revision = raw.get("revision")
    archived_at = raw.get("archived_at")
    if (
        not isinstance(revision, int)
        or isinstance(revision, bool)
        or revision < 1
        or not isinstance(archived_at, str)
        or not archived_at.strip()
    ):
        raise PayloadError(f"Gruppe {group_id} har en ugyldig revision")
    stored_group_id = raw.get("group_id")
    if stored_group_id is not None and stored_group_id != group_id:
        raise PayloadError(f"Revisionen for gruppe {group_id} tilhører en anden gruppe")
    return TournamentRevision(
        revision,
        archived_at,
        str(raw.get("reason") or "Turneringen blev genberegnet"),
        _teams_from_raw(group_id, raw.get("teams")),
        _tournament_from_dict(group_id, raw.get("tournament")),
    )


def _group_to_dict(group: GroupDefinition, *, external_revisions: bool = False) -> dict[str, object]:
    result: dict[str, object] = {
        "id": group.group_id,
        "name": group.name,
        "type": group.kind,
        "game": {
            "url": group.game.original,
            "locale": group.game.locale,
            "slug": group.game.slug,
        },
        "teams": [_team_to_dict(team) for team in group.teams],
    }
    result.update({
        "official_url": group.official_url,
        "official_link_type": group.official_link_type,
    })
    if group.tournament is not None:
        result["active_revision"] = group.active_revision
        result["tournament"] = _tournament_to_dict(group.tournament)
        result["tournament_revisions"] = [
            _revision_to_dict(
                group.group_id, revision, external=external_revisions
            )
            for revision in group.archived_revisions
        ]
    return result


def _group_from_dict(
    raw: object,
    *,
    revision_dir: Path | None = None,
    revision_warnings: list[str] | None = None,
) -> GroupDefinition:
    if not isinstance(raw, dict):
        raise PayloadError("Gruppeposten skal være et objekt")
    game_raw = raw.get("game")
    if not isinstance(game_raw, dict):
        raise PayloadError("Gruppen skal indeholde spil og hold")
    group_id = _validate_group_id(str(raw.get("id", "")))
    name = str(raw.get("name", "")).strip()
    if not name:
        raise PayloadError(f"Gruppe {group_id} har et tomt navn")
    game = GameUrl(
        original=str(game_raw.get("url", "")).strip(),
        locale=str(game_raw.get("locale", "")).strip(),
        slug=str(game_raw.get("slug", "")).strip(),
    )
    if not all((game.original, game.locale, game.slug)):
        raise PayloadError(f"Gruppe {group_id} har ugyldige spilmetadata")
    teams = _teams_from_raw(group_id, raw.get("teams"))
    kind = str(raw.get("type") or "standings")
    if kind not in {"standings", "tournament"}:
        raise PayloadError(f"Gruppe {group_id} har en ikke-understøttet type")
    tournament: TournamentConfig | None = None
    active_revision = 1
    archived: list[TournamentRevision] = []
    if kind == "tournament":
        tournament = _tournament_from_dict(group_id, raw.get("tournament"))
        active_raw = raw.get("active_revision", 1)
        if not isinstance(active_raw, int) or isinstance(active_raw, bool) or active_raw < 1:
            raise PayloadError(f"Gruppe {group_id} har en ugyldig aktiv revision")
        active_revision = active_raw
        revisions_raw = raw.get("tournament_revisions", [])
        if not isinstance(revisions_raw, list):
            raise PayloadError(f"Gruppe {group_id} har ugyldige turneringsrevisioner")
        for item in revisions_raw:
            try:
                if not isinstance(item, dict):
                    raise PayloadError(f"Gruppe {group_id} har en ugyldig revision")
                relative = item.get("file")
                if relative is None:
                    archived.append(_revision_from_dict(group_id, item))
                    continue
                revision = item.get("revision")
                if not isinstance(revision, int) or isinstance(revision, bool):
                    raise PayloadError(f"Gruppe {group_id} har en ugyldig revision")
                expected = f"{group_id}/revision-{revision}.json"
                if relative != expected or revision_dir is None:
                    raise PayloadError(f"Gruppe {group_id} har en ugyldig revisionsfil")
                path = revision_dir / Path(expected)
                try:
                    revision_raw = json.loads(path.read_text(encoding="utf-8"))
                except OSError as exc:
                    raise PayloadError(
                        f"Turneringsrevisionen {path} kunne ikke læses: {exc}"
                    ) from exc
                except json.JSONDecodeError as exc:
                    raise PayloadError(f"Turneringsrevisionen {path} indeholder ugyldig JSON") from exc
                loaded = _revision_from_dict(group_id, revision_raw)
                if (
                    loaded.revision != revision
                    or loaded.archived_at != item.get("archived_at")
                    or loaded.reason != str(item.get("reason") or "Turneringen blev genberegnet")
                ):
                    raise PayloadError(f"Revisionsmetadata for gruppe {group_id} er i konflikt")
                archived.append(loaded)
            except PayloadError as exc:
                if revision_warnings is None:
                    raise
                revision_warnings.append(
                    f"{group_id}: den arkiverede turneringsrevision blev sprunget over: {exc}"
                )
    return _validate_group(
        GroupDefinition(
            group_id, name, game, teams, kind, tournament,
            active_revision, tuple(archived),
            official_url=raw.get("official_url") if isinstance(raw.get("official_url"), str) else None,
            official_link_type=(
                raw.get("official_link_type")
                if isinstance(raw.get("official_link_type"), str)
                else None
            ),
        )
    )


def _publish_revision_file(
    revision_dir: Path,
    group_id: str,
    revision: TournamentRevision,
) -> None:
    payload = {
        "schema_version": 1,
        **_revision_to_dict(group_id, revision, external=False),
    }
    target = revision_dir / group_id / f"revision-{revision.revision}.json"
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PayloadError(f"Turneringsrevisionen {target} kunne ikke valideres: {exc}") from exc
        if existing != payload:
            raise PayloadError(f"Turneringsrevisionen findes allerede med andre data: {target}")
        return
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    try:
        publish_immutable_text(target, content)
    except FileExistsError:
        _publish_revision_file(revision_dir, group_id, revision)


class GroupStore:
    """Atomically maintain manager games and editable group definitions."""

    def __init__(self, path: Path | str, revision_dir: Path | str | None = None) -> None:
        self.path = Path(path)
        if revision_dir is not None:
            self.revision_dir = Path(revision_dir)
        elif self.path.parent.name.casefold() == "config":
            self.revision_dir = self.path.parent.parent / "data" / "group-revisions"
        else:
            self.revision_dir = self.path.parent / "group-revisions"

    def load_configuration(
        self,
        *,
        revision_warnings: list[str] | None = None,
    ) -> HubConfiguration:
        if not self.path.exists():
            return HubConfiguration((), ())
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PayloadError(f"Gruppekonfigurationen kunne ikke læses: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise PayloadError("Gruppekonfigurationen indeholder ugyldig JSON") from exc
        if isinstance(payload, dict) and payload.get("schema_version") == 8:
            payload = dict(payload)
            payload["schema_version"] = 7
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") not in {1, 2, 3, 4, 5, 6, 7}
        ):
            raise PayloadError("Ikke-understøttet skema for gruppekonfiguration")
        groups_raw = payload.get("groups")
        if not isinstance(groups_raw, list):
            raise PayloadError("Gruppekonfigurationen skal indeholde en groups-liste")
        groups = tuple(
            _group_from_dict(
                item,
                revision_dir=self.revision_dir,
                revision_warnings=revision_warnings,
            )
            for item in groups_raw
        )
        group_ids = [group.group_id for group in groups]
        if len(group_ids) != len(set(group_ids)):
            raise PayloadError("Gruppekonfigurationen indeholder duplikerede ID'er")

        if payload["schema_version"] in {5, 6, 7}:
            games_raw = payload.get("games")
            if not isinstance(games_raw, list):
                raise PayloadError("Konfiguration med skema 5 skal indeholde en games-liste")
            games = tuple(_manager_game_from_dict(item) for item in games_raw)
        else:
            inferred: dict[tuple[str, str], ManagerGame] = {}
            for group in groups:
                if group.teams:
                    inferred.setdefault(
                        _game_identity(group.game),
                        normalize_manager_game(group.game),
                    )
            games = tuple(inferred.values())
        identities = [manager_game.identity for manager_game in games]
        if len(identities) != len(set(identities)):
            raise PayloadError("Gruppekonfigurationen indeholder duplikerede managerspil")
        return HubConfiguration(games, groups)

    def load_configuration_with_warnings(
        self,
    ) -> tuple[HubConfiguration, tuple[str, ...]]:
        """Load active configuration while isolating bad archived revisions."""

        warnings: list[str] = []
        configuration = self.load_configuration(revision_warnings=warnings)
        return configuration, tuple(warnings)

    def load(self) -> tuple[GroupDefinition, ...]:
        """Return groups for backward compatibility."""

        return self.load_configuration().groups

    def save_configuration(self, configuration: HubConfiguration) -> None:
        group_ids = [group.group_id for group in configuration.groups]
        if len(group_ids) != len(set(group_ids)):
            raise PayloadError("Duplikerede gruppe-ID'er kan ikke gemmes")
        identities = [manager_game.identity for manager_game in configuration.games]
        if len(identities) != len(set(identities)):
            raise PayloadError("Duplikerede managerspil kan ikke gemmes")
        if any(not manager_game.name.strip() for manager_game in configuration.games):
            raise PayloadError("Managerspillets navn må ikke være tomt")
        for group in configuration.groups:
            for revision in group.archived_revisions:
                _publish_revision_file(self.revision_dir, group.group_id, revision)
        payload = {
            "schema_version": GROUP_SCHEMA_VERSION,
            "games": [
                _manager_game_to_dict(manager_game)
                for manager_game in configuration.games
            ],
            "groups": [
                _group_to_dict(group, external_revisions=True)
                for group in configuration.groups
            ],
        }
        replace_text_atomically(
            self.path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        )

    def save(self, groups: tuple[GroupDefinition, ...]) -> None:
        """Save groups while preserving all persistent manager games."""

        current = self.load_configuration()
        games = list(current.games)
        known = {manager_game.identity for manager_game in games}
        for group in groups:
            identity = _game_identity(group.game)
            if group.teams and identity not in known:
                games.append(normalize_manager_game(group.game))
                known.add(identity)
        self.save_configuration(HubConfiguration(tuple(games), groups))

    def create_manager_game(
        self, source: str | GameUrl, name: str = ""
    ) -> ManagerGame:
        configuration = self.load_configuration()
        manager_game = normalize_manager_game(source, name)
        if any(item.identity == manager_game.identity for item in configuration.games):
            raise PayloadError(f"Managerspillet findes allerede: {manager_game.game.slug}")
        self.save_configuration(
            replace(configuration, games=(*configuration.games, manager_game))
        )
        return manager_game

    def rename_manager_game(self, game: GameUrl, name: str) -> ManagerGame:
        configuration = self.load_configuration()
        resolved_name = name.strip() or game.slug
        games = list(configuration.games)
        for index, manager_game in enumerate(games):
            if manager_game.identity == _game_identity(game):
                renamed = replace(manager_game, name=resolved_name)
                games[index] = renamed
                self.save_configuration(replace(configuration, games=tuple(games)))
                return renamed
        raise PayloadError(f"Ukendt managerspil: {game.slug}")

    def archive_manager_game(
        self, game: GameUrl, *, now: datetime | None = None
    ) -> ManagerGame:
        configuration = self.load_configuration()
        archived_at = now or datetime.now().astimezone()
        if archived_at.tzinfo is None:
            archived_at = archived_at.astimezone()
        games = list(configuration.games)
        for index, manager_game in enumerate(games):
            if manager_game.identity != _game_identity(game):
                continue
            if manager_game.is_archived:
                raise PayloadError(f"Managerspillet er allerede arkiveret: {game.slug}")
            archived = replace(manager_game, archived_at=archived_at.isoformat())
            games[index] = archived
            self.save_configuration(replace(configuration, games=tuple(games)))
            return archived
        raise PayloadError(f"Ukendt managerspil: {game.slug}")

    def restore_manager_game(self, game: GameUrl) -> ManagerGame:
        configuration = self.load_configuration()
        games = list(configuration.games)
        for index, manager_game in enumerate(games):
            if manager_game.identity != _game_identity(game):
                continue
            if not manager_game.is_archived:
                raise PayloadError(f"Managerspillet er ikke arkiveret: {game.slug}")
            restored = replace(manager_game, archived_at=None)
            games[index] = restored
            self.save_configuration(replace(configuration, games=tuple(games)))
            return restored
        raise PayloadError(f"Ukendt managerspil: {game.slug}")

    def delete_manager_game(self, game: GameUrl) -> None:
        configuration = self.load_configuration()
        identity = _game_identity(game)
        if any(_game_identity(group.game) == identity for group in configuration.groups):
            raise PayloadError("Managerspillet kan ikke slettes, mens det har grupper")
        remaining = tuple(
            manager_game
            for manager_game in configuration.games
            if manager_game.identity != identity
        )
        if len(remaining) == len(configuration.games):
            raise PayloadError(f"Ukendt managerspil: {game.slug}")
        self.save_configuration(replace(configuration, games=remaining))

    def create(
        self,
        name: str,
        game: GameUrl,
        teams: tuple[GroupTeam, ...] = (),
        *,
        group_id: str | None = None,
        kind: str = "standings",
        tournament: TournamentConfig | None = None,
        official_url: str | None = None,
        official_link_type: str | None = None,
    ) -> GroupDefinition:
        configuration = self.load_configuration()
        current = configuration.groups
        resolved_id = _validate_group_id(group_id or uuid4().hex[:12])
        if any(group.group_id == resolved_id for group in current):
            raise PayloadError(f"Gruppe-ID'et findes allerede: {resolved_id}")
        group = _validate_group(
            GroupDefinition(
                resolved_id,
                name.strip(),
                game,
                _validate_teams(game, teams),
                kind,
                tournament,
                official_url=official_url,
                official_link_type=official_link_type,
            )
        )
        if not group.name:
            raise PayloadError("Gruppens navn må ikke være tomt")
        games = configuration.games
        if not any(item.identity == _game_identity(game) for item in games):
            games = (*games, normalize_manager_game(game))
        self.save_configuration(
            HubConfiguration(games, (*current, group))
        )
        return group

    def plan_tournament(
        self,
        game: GameUrl,
        teams: tuple[GroupTeam, ...],
        *,
        start_round: int,
        final_round: int,
        rounds_per_tie: int,
        draw_seed: str | None = None,
        seed_generator=None,
        template: str = "group_knockout",
        definition_options: dict[str, object] | None = None,
    ) -> TournamentConfig:
        """Build a reproducible draw distinct from comparable saved draws."""
        definition_options = definition_options or {}
        if template != "group_knockout" or definition_options:
            validated = _validate_teams(game, teams)
            return create_tournament_definition(
                template,
                tuple(team.team_id for team in validated),
                start_round,
                final_round=final_round,
                rounds_per_tie=rounds_per_tie,
                draw_seed=draw_seed,
                **definition_options,
            )
        validated = _validate_teams(game, teams)
        team_ids = tuple(sorted(team.team_id for team in validated))
        forbidden = _matching_tournament_signatures(
            self.load_configuration(), game, team_ids, start_round,
            final_round, rounds_per_tie,
        )
        generator = seed_generator or generate_draw_seed
        next_seed = draw_seed
        for _ in range(128):
            candidate = create_tournament_config(
                team_ids, start_round, final_round, rounds_per_tie,
                draw_seed=next_seed or generator(),
            )
            if (
                tournament_schedule_signature(candidate.group_fixtures) not in forbidden
                or len(team_ids) == 2
            ):
                return candidate
            next_seed = None
        raise PayloadError(
            "Kunne ikke finde en ny lodtrækning; prøv at generere et nyt seed"
        )

    def create_tournament(
        self,
        name: str,
        game: GameUrl,
        teams: tuple[GroupTeam, ...],
        *,
        start_round: int,
        final_round: int,
        rounds_per_tie: int,
        group_id: str | None = None,
        draw_seed: str | None = None,
        seed_generator=None,
        template: str = "group_knockout",
        definition_options: dict[str, object] | None = None,
        shuffle=None,
    ) -> GroupDefinition:
        validated = _validate_teams(game, teams)
        if shuffle is None:
            config = self.plan_tournament(
                game, validated, start_round=start_round, final_round=final_round,
                rounds_per_tie=rounds_per_tie, draw_seed=draw_seed,
                seed_generator=seed_generator,
                template=template,
                definition_options=definition_options,
            )
        else:
            config = create_tournament_config(
                tuple(team.team_id for team in validated), start_round, final_round,
                rounds_per_tie, shuffle=shuffle,
            )
        return self.create(
            name,
            game,
            validated,
            group_id=group_id,
            kind="tournament",
            tournament=config,
        )

    def update(self, updated: GroupDefinition) -> None:
        configuration = self.load_configuration()
        current = list(configuration.groups)
        for index, group in enumerate(current):
            if group.group_id != updated.group_id:
                continue
            if not updated.name.strip():
                raise PayloadError("Gruppens navn må ikke være tomt")
            candidate = replace(
                updated, teams=_validate_teams(updated.game, updated.teams)
            )
            cosmetic = replace(
                group,
                name=candidate.name,
                official_url=candidate.official_url,
                official_link_type=candidate.official_link_type,
            )
            if candidate == cosmetic:
                current[index] = _validate_group(candidate)
                self.save_configuration(replace(configuration, groups=tuple(current)))
                return
            if group.kind == "tournament" and candidate != replace(
                group, name=candidate.name
            ):
                raise PayloadError(
                    "Turneringsændringer skal gemmes som en ny revision"
                )
            current[index] = _validate_group(candidate)
            self.save_configuration(replace(configuration, groups=tuple(current)))
            return
        raise PayloadError(f"Ukendt gruppe: {updated.group_id}")

    def rebuild_tournament(
        self, group_id: str, teams: tuple[GroupTeam, ...], *, final_round: int,
        name: str | None = None, now: datetime | None = None,
        reason: str = "Medlemsliste eller finalerunde ændret", shuffle=None,
        seed_generator=None,
        template: str | None = None,
        definition_options: dict[str, object] | None = None,
        start_round: int | None = None,
        rounds_per_tie: int | None = None,
    ) -> GroupDefinition:
        """Archive the active draw and publish a fully recalculated revision."""
        configuration = self.load_configuration()
        current = list(configuration.groups)
        for index, group in enumerate(current):
            if group.group_id != group_id:
                continue
            if group.kind != "tournament" or group.tournament is None:
                raise PayloadError("Gruppen er ikke en turnering")
            validated = _validate_teams(group.game, teams)
            resolved_name = group.name if name is None else name.strip()
            if not resolved_name:
                raise PayloadError("Gruppens navn må ikke være tomt")
            old_ids = tuple(team.team_id for team in group.teams)
            new_ids = tuple(team.team_id for team in validated)
            target_template = template or group.tournament.template
            target_start = start_round or group.tournament.start_round
            target_rounds_per_tie = (
                rounds_per_tie or group.tournament.rounds_per_tie
            )
            structural_override = (
                target_template != group.tournament.template
                or target_start != group.tournament.start_round
                or target_rounds_per_tie != group.tournament.rounds_per_tie
                or definition_options is not None
            )
            if (
                set(old_ids) == set(new_ids)
                and final_round == group.tournament.final_round
                and not structural_override
            ):
                renamed = replace(group, name=resolved_name)
                current[index] = renamed
                self.save_configuration(replace(configuration, groups=tuple(current)))
                return renamed
            if shuffle is None:
                prior_order = group.tournament.seed_order or old_ids
                revised_order = tuple(
                    team_id for team_id in prior_order if team_id in set(new_ids)
                )
                revised_order += tuple(
                    team_id for team_id in new_ids if team_id not in set(revised_order)
                )
                options: dict[str, object] = {
                    "match_points": group.tournament.match_points,
                    "seed_rule": group.tournament.seed_rule,
                    "seed_order": revised_order,
                    "standings_tiebreakers": group.tournament.standings_tiebreakers,
                    "knockout_tiebreakers": group.tournament.knockout_tiebreakers,
                    "league_legs": group.tournament.league_legs,
                    "swiss_rounds": group.tournament.swiss_rounds,
                    "group_count": group.tournament.group_count,
                    "qualifiers_per_group": group.tournament.qualifiers_per_group,
                    "bronze_match": group.tournament.bronze_match,
                }
                if definition_options is not None:
                    options.update(definition_options)
                config = self.plan_tournament(
                    group.game,
                    validated,
                    start_round=target_start,
                    final_round=final_round,
                    rounds_per_tie=target_rounds_per_tie,
                    seed_generator=seed_generator,
                    template=target_template,
                    definition_options=options,
                )
            else:
                if target_template != "group_knockout":
                    raise PayloadError("Brugerdefineret blanding understøttes kun for legacy-turneringer")
                config = create_tournament_config(
                    new_ids,
                    target_start,
                    final_round,
                    target_rounds_per_tie,
                    shuffle=shuffle,
                )
            archived_at = now or datetime.now().astimezone()
            if archived_at.tzinfo is None:
                archived_at = archived_at.astimezone()
            archive = TournamentRevision(
                group.active_revision, archived_at.isoformat(),
                reason.strip() or "Turneringen blev genberegnet",
                group.teams, group.tournament,
            )
            rebuilt = _validate_group(replace(
                group, name=resolved_name, teams=validated, tournament=config,
                active_revision=group.active_revision + 1,
                archived_revisions=(*group.archived_revisions, archive),
            ))
            current[index] = rebuilt
            self.save_configuration(replace(configuration, groups=tuple(current)))
            return rebuilt
        raise PayloadError(f"Ukendt gruppe: {group_id}")

    def delete(self, group_id: str) -> None:
        configuration = self.load_configuration()
        remaining = tuple(
            group for group in configuration.groups if group.group_id != group_id
        )
        if len(remaining) == len(configuration.groups):
            raise PayloadError(f"Ukendt gruppe: {group_id}")
        self.save_configuration(replace(configuration, groups=remaining))


def _dedupe_teams(teams: tuple[GroupTeam, ...]) -> tuple[GroupTeam, ...]:
    result: dict[int, GroupTeam] = {}
    for team in teams:
        result.setdefault(team.team_id, team)
    return tuple(result.values())


def _matching_tournament_signatures(
    configuration: HubConfiguration,
    game: GameUrl,
    team_ids: tuple[int, ...],
    start_round: int,
    final_round: int,
    rounds_per_tie: int,
) -> set[tuple[tuple[int, int, int | None], ...]]:
    identity = _game_identity(game)
    expected_ids = set(team_ids)
    signatures: set[tuple[tuple[int, int, int | None], ...]] = set()
    for group in configuration.groups:
        if group.kind != "tournament" or _game_identity(group.game) != identity:
            continue
        candidates = ((group.teams, group.tournament), *(
            (revision.teams, revision.tournament)
            for revision in group.archived_revisions
        ))
        for members, config in candidates:
            if config is None or {team.team_id for team in members} != expected_ids:
                continue
            if (
                config.start_round != start_round
                or config.final_round != final_round
                or config.rounds_per_tie != rounds_per_tie
            ):
                continue
            signatures.add(tournament_schedule_signature(config.group_fixtures))
    return signatures

def _validate_teams(game: GameUrl, teams: tuple[GroupTeam, ...]) -> tuple[GroupTeam, ...]:
    values = _dedupe_teams(teams)
    for team in values:
        parsed = parse_direct_team_url(team.source_url)
        if parsed is not None and parsed.game.slug != game.slug:
            raise PayloadError(
                f"Hold {team.team_id} tilhører {parsed.game.slug}, ikke {game.slug}"
            )
    return values


def _validate_group(group: GroupDefinition) -> GroupDefinition:
    official_url, official_link_type = validate_official_group_url(
        group.official_url,
        group.game.locale,
        group.official_link_type,
    )
    group = replace(
        group,
        official_url=official_url,
        official_link_type=official_link_type,
    )
    if group.kind not in {"standings", "tournament"}:
        raise PayloadError("Gruppetypen skal være standings eller tournament")
    if group.kind == "standings":
        if (group.tournament is not None or group.active_revision != 1
                or group.archived_revisions):
            raise PayloadError(
                "En almindelig gruppe må ikke have turneringsindstillinger"
            )
        return group
    if group.tournament is None:
        raise PayloadError("En turnering mangler indstillinger")
    validate_tournament_config(
        group.tournament, tuple(team.team_id for team in group.teams)
    )
    revisions = [item.revision for item in group.archived_revisions]
    if (group.active_revision < 1 or len(revisions) != len(set(revisions))
            or any(revision >= group.active_revision for revision in revisions)):
        raise PayloadError("Turneringen har ugyldige revisionsnumre")
    for revision in group.archived_revisions:
        validate_tournament_config(
            revision.tournament, tuple(team.team_id for team in revision.teams)
        )
    return group


def group_team_from_snapshot(snapshot: TeamSnapshot) -> GroupTeam:
    reference = snapshot.team.reference
    return GroupTeam(
        team_id=reference.team_id,
        name=snapshot.team.team_name,
        source_url=reference.source_url,
        account_key=reference.account_key,
        account_label=reference.account_label,
        account_user_id=reference.account_user_id,
        profile_url=reference.profile_url,
    )
