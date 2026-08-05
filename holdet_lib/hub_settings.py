"""Versioned Hub settings: watchlists, manager aliases and score rules."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
import json
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .groups import GroupDefinition
    from .storage import SnapshotIndex

from .errors import PayloadError
from .models import GameUrl, PlayerEntry
from .persistence import replace_text_atomically


HUB_SETTINGS_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class HallOfFameScoreProfile:
    """Editable scoring weights applied to frozen raw results."""

    group_points: tuple[int, int, int, int] = (10, 6, 3, 1)
    tournament_winner: int = 10
    tournament_finalist: int = 6
    tournament_semifinalist: int = 3
    global_round_win: int = 1

    def __post_init__(self) -> None:
        values = (
            *self.group_points,
            self.tournament_winner,
            self.tournament_finalist,
            self.tournament_semifinalist,
            self.global_round_win,
        )
        if any(isinstance(value, bool) or value < 0 for value in values):
            raise ValueError("Hall of Fame-point skal være ikke-negative heltal")


@dataclass(frozen=True, slots=True)
class WatchlistEntry:
    game_locale: str
    game_slug: str
    player_key: str
    entry_id: int | None
    person_id: int | None
    name: str
    team: str
    position: str

    @property
    def game_identity(self) -> tuple[str, str]:
        return self.game_locale.casefold(), self.game_slug


@dataclass(frozen=True, slots=True)
class ManagerAlias:
    canonical_id: str
    display_name: str
    identity_keys: tuple[str, ...]

@dataclass(frozen=True, slots=True)
class ManagerProfile:
    """A user-managed person shared by teams, games and seasons."""

    manager_id: str
    display_name: str
    identity_keys: tuple[str, ...]
    profile_urls: tuple[str, ...] = ()
    manual_identity_keys: tuple[str, ...] = ()

    @property
    def canonical_id(self) -> str:
        """Compatibility with the former ManagerAlias interface."""

        return self.manager_id


@dataclass(frozen=True, slots=True)
class HubSettings:
    watchlist: tuple[WatchlistEntry, ...] = ()
    manager_aliases: tuple[ManagerAlias, ...] = ()
    hall_of_fame_score: HallOfFameScoreProfile = HallOfFameScoreProfile()
    manager_profiles: tuple[ManagerProfile, ...] = ()


def effective_manager_profiles(settings: HubSettings) -> tuple[ManagerProfile, ...]:
    """Return schema-2 profiles plus schema-1 aliases not yet migrated on disk."""

    profiles = {profile.manager_id: profile for profile in settings.manager_profiles}
    for alias in settings.manager_aliases:
        profiles.setdefault(
            alias.canonical_id,
            ManagerProfile(alias.canonical_id, alias.display_name, alias.identity_keys),
        )
    return tuple(
        sorted(
            profiles.values(),
            key=lambda item: (item.display_name.casefold(), item.manager_id),
        )
    )



def _authoritative_manager_keys(keys: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            key
            for key in keys
            if key and not key.startswith("legacy-name:")
        )
    )


def build_effective_manager_settings(
    settings: HubSettings,
    groups: Iterable[GroupDefinition],
    snapshots: SnapshotIndex,
) -> HubSettings:
    """Union co-occurring authoritative identities without using names."""

    observations: list[tuple[tuple[str, ...], str, tuple[str, ...]]] = []
    for group in groups:
        for member in group.teams:
            team_snapshots = snapshots.for_team(group.game, member.team_id)
            if not team_snapshots:
                keys = manager_identity_keys(
                    owner_user_id=None,
                    account_user_id=member.account_user_id,
                    account_key=member.account_key,
                    owner_name=member.account_label,
                )
                observations.append(
                    (
                        _authoritative_manager_keys(keys),
                        member.account_label,
                        (member.profile_url,) if member.profile_url else (),
                    )
                )
                continue
            for snapshot in team_snapshots:
                team = snapshot.team
                keys = manager_identity_keys(
                    owner_user_id=team.owner_user_id,
                    account_user_id=(
                        team.reference.account_user_id
                        if team.reference.account_user_id is not None
                        else member.account_user_id
                    ),
                    account_key=team.reference.account_key or member.account_key,
                    owner_name=team.owner_name,
                )
                urls = tuple(
                    dict.fromkeys(
                        value
                        for value in (
                            team.reference.profile_url,
                            member.profile_url,
                        )
                        if value
                    )
                )
                observations.append(
                    (
                        _authoritative_manager_keys(keys),
                        team.owner_name or member.account_label,
                        urls,
                    )
                )

    profiles = list(effective_manager_profiles(settings))
    parent: dict[str, str] = {}

    def find(key: str) -> str:
        parent.setdefault(key, key)
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(keys: tuple[str, ...]) -> None:
        if not keys:
            return
        root = find(keys[0])
        for key in keys[1:]:
            other = find(key)
            if root != other:
                parent[other] = root

    for keys, _, _ in observations:
        union(keys)
    for profile in profiles:
        union(_authoritative_manager_keys(profile.identity_keys))

    component_keys: dict[str, set[str]] = {}
    component_names: dict[str, list[str]] = {}
    component_urls: dict[str, set[str]] = {}
    for keys, name, urls in observations:
        if not keys:
            continue
        root = find(keys[0])
        component_keys.setdefault(root, set()).update(keys)
        if name.strip():
            component_names.setdefault(root, []).append(name.strip())
        component_urls.setdefault(root, set()).update(urls)
    for key in tuple(parent):
        component_keys.setdefault(find(key), set()).add(key)

    remaining = set(range(len(profiles)))
    resolved: list[ManagerProfile] = []
    for root in sorted(component_keys):
        keys = component_keys[root]
        matched = [
            index
            for index, profile in enumerate(profiles)
            if keys.intersection(profile.identity_keys)
        ]
        if matched:
            target_index = min(
                matched,
                key=lambda index: (
                    -len(profiles[index].manual_identity_keys),
                    profiles[index].manager_id,
                ),
            )
            target = profiles[target_index]
            identity_keys = set(keys)
            urls = set(component_urls.get(root, ()))
            manual_keys: set[str] = set()
            for index in matched:
                identity_keys.update(profiles[index].identity_keys)
                urls.update(profiles[index].profile_urls)
                manual_keys.update(profiles[index].manual_identity_keys)
                remaining.discard(index)
            resolved.append(
                replace(
                    target,
                    identity_keys=tuple(sorted(identity_keys)),
                    profile_urls=tuple(sorted(urls)),
                    manual_identity_keys=tuple(sorted(manual_keys)),
                )
            )
        else:
            ordered = sorted(
                keys,
                key=lambda key: (
                    0 if key.startswith("owner:") else
                    1 if key.startswith("account-user:") else 2,
                    key,
                ),
            )
            resolved.append(
                ManagerProfile(
                    ordered[0],
                    component_names.get(root, ["Ukendt manager"])[0],
                    tuple(sorted(keys)),
                    tuple(sorted(component_urls.get(root, ()))),
                )
            )
    resolved.extend(profiles[index] for index in sorted(remaining))
    return replace(
        settings,
        manager_aliases=(),
        manager_profiles=tuple(
            sorted(
                resolved,
                key=lambda item: (item.display_name.casefold(), item.manager_id),
            )
        ),
    )


def player_identity(game: GameUrl, entry: PlayerEntry) -> str:
    """Return the stable game-scoped player identity with a legacy fallback."""

    game_key = f"{game.locale.casefold()}:{game.slug}"
    if entry.entry_id is not None:
        return f"{game_key}:entry:{entry.entry_id}"
    legacy = "|".join(
        value.strip().casefold() for value in (entry.name, entry.team, entry.position)
    )
    return f"{game_key}:legacy:{legacy}"


def watchlist_entry(game: GameUrl, entry: PlayerEntry) -> WatchlistEntry:
    return WatchlistEntry(
        game.locale.casefold(),
        game.slug,
        player_identity(game, entry),
        entry.entry_id,
        entry.person_id,
        entry.name,
        entry.team,
        entry.position,
    )


def manager_identity_keys(
    *,
    owner_user_id: int | None,
    account_user_id: int | None,
    account_key: str,
    owner_name: str,
) -> tuple[str, ...]:
    """Return manager keys in authoritative-to-legacy order."""

    keys: list[str] = []
    if owner_user_id is not None:
        keys.append(f"owner:{owner_user_id}")
    if account_user_id is not None:
        keys.append(f"account-user:{account_user_id}")
    if account_key.strip():
        keys.append(f"account-key:{account_key.strip().casefold()}")
    if owner_name.strip():
        keys.append(f"legacy-name:{owner_name.strip().casefold()}")
    return tuple(dict.fromkeys(keys))


def resolve_manager_identity(
    settings: HubSettings,
    *,
    owner_user_id: int | None,
    account_user_id: int | None,
    account_key: str,
    owner_name: str,
    fallback_key: str = "",
) -> tuple[str, str]:
    keys = manager_identity_keys(
        owner_user_id=owner_user_id,
        account_user_id=account_user_id,
        account_key=account_key,
        owner_name=owner_name,
    )
    aliases = {
        key: profile
        for profile in effective_manager_profiles(settings)
        for key in profile.identity_keys
    }
    for key in keys:
        if alias := aliases.get(key):
            return alias.canonical_id, alias.display_name
    authoritative = next((key for key in keys if not key.startswith("legacy-name:")), None)
    canonical = authoritative or (
        f"unresolved:{fallback_key.strip().casefold()}"
        if fallback_key.strip()
        else "unresolved:anonymous"
    )
    return canonical, owner_name.strip() or "Ukendt manager"


def _require_object(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PayloadError(f"{label} skal være et objekt")
    return value


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PayloadError(f"{label} skal være tekst")
    return value.strip()


def _optional_integer(value: object, label: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise PayloadError(f"{label} skal være et heltal")
    return value


def _score_from_dict(raw: object) -> HallOfFameScoreProfile:
    item = _require_object(raw, "hall_of_fame_score")
    group_raw = item.get("group_points")
    if (
        not isinstance(group_raw, list)
        or len(group_raw) != 4
        or any(not isinstance(value, int) or isinstance(value, bool) for value in group_raw)
    ):
        raise PayloadError("group_points skal indeholde fire heltal")
    try:
        return HallOfFameScoreProfile(
            tuple(group_raw),
            int(item.get("tournament_winner")),
            int(item.get("tournament_finalist")),
            int(item.get("tournament_semifinalist")),
            int(item.get("global_round_win")),
        )
    except (TypeError, ValueError) as exc:
        raise PayloadError(f"ugyldig Hall of Fame-pointprofil: {exc}") from exc



def _validate_manager_profiles(
    profiles: tuple[ManagerProfile, ...],
) -> tuple[ManagerProfile, ...]:
    ids = [item.manager_id for item in profiles]
    keys = [key for item in profiles for key in item.identity_keys]
    if len(ids) != len(set(ids)):
        raise PayloadError("Managerprofiler skal have entydige ID'er")
    if len(keys) != len(set(keys)):
        raise PayloadError(
            "En manageridentitet kan kun tilh\u00f8re \u00e9n profil"
        )
    if any(
        not item.display_name.strip()
        or not item.identity_keys
        or not set(item.manual_identity_keys) <= set(item.identity_keys)
        for item in profiles
    ):
        raise PayloadError(
            "Managerprofiler skal have navne, identiteter og gyldig linkproveniens"
        )
    return profiles


def _settings_from_dict(payload: object) -> HubSettings:
    root = _require_object(payload, "Hub-indstillinger")
    source_schema_version = root.get("schema_version")
    if source_schema_version == 1:
        root = dict(root)
        root["schema_version"] = HUB_SETTINGS_SCHEMA_VERSION
    if root.get("schema_version") != HUB_SETTINGS_SCHEMA_VERSION:
        raise PayloadError("ukendt skema for Hub-indstillinger")
    raw_watchlist = root.get("watchlist", [])
    raw_aliases = root.get("manager_aliases", [])
    if not isinstance(raw_watchlist, list) or not isinstance(raw_aliases, list):
        raise PayloadError("watchlist og manager_aliases skal være lister")
    watched: list[WatchlistEntry] = []
    for index, raw in enumerate(raw_watchlist):
        item = _require_object(raw, f"watchlist[{index}]")
        watched.append(
            WatchlistEntry(
                _require_text(item.get("game_locale"), "game_locale").casefold(),
                _require_text(item.get("game_slug"), "game_slug"),
                _require_text(item.get("player_key"), "player_key"),
                _optional_integer(item.get("entry_id"), "entry_id"),
                _optional_integer(item.get("person_id"), "person_id"),
                _require_text(item.get("name"), "name"),
                _require_text(item.get("team"), "team"),
                _require_text(item.get("position"), "position"),
            )
        )
    aliases: list[ManagerAlias] = []
    for index, raw in enumerate(raw_aliases):
        item = _require_object(raw, f"manager_aliases[{index}]")
        keys = item.get("identity_keys")
        if not isinstance(keys, list) or not keys or not all(
            isinstance(value, str) and value.strip() for value in keys
        ):
            raise PayloadError("manageralias skal have identity_keys")
        aliases.append(
            ManagerAlias(
                _require_text(item.get("canonical_id"), "canonical_id"),
                _require_text(item.get("display_name"), "display_name"),
                tuple(dict.fromkeys(value.strip() for value in keys)),
            )
        )
    profiles: list[ManagerProfile] = []
    if source_schema_version == HUB_SETTINGS_SCHEMA_VERSION:
        raw_profiles = root.get("manager_profiles", [])
        if not isinstance(raw_profiles, list):
            raise PayloadError("manager_profiles skal være en liste")
        for index, raw in enumerate(raw_profiles):
            item = _require_object(raw, f"manager_profiles[{index}]")
            keys = item.get("identity_keys")
            urls = item.get("profile_urls", [])
            manual_keys = item.get("manual_identity_keys", keys)
            if not isinstance(keys, list) or not keys or not all(
                isinstance(value, str) and value.strip() for value in keys
            ):
                raise PayloadError("En managerprofil skal have identity_keys")
            if not isinstance(urls, list) or not all(
                isinstance(value, str) and value.strip() for value in urls
            ):
                raise PayloadError("Managerprofilernes URL'er skal v\u00e6re tekst")
            if not isinstance(manual_keys, list) or not all(
                isinstance(value, str) and value.strip() for value in manual_keys
            ):
                raise PayloadError(
                    "Managerprofilernes manuelle identiteter skal v\u00e6re tekst"
                )
            profiles.append(
                ManagerProfile(
                    _require_text(item.get("manager_id"), "manager_id"),
                    _require_text(item.get("display_name"), "display_name"),
                    tuple(dict.fromkeys(value.strip() for value in keys)),
                    tuple(dict.fromkeys(value.strip() for value in urls)),
                    tuple(dict.fromkeys(value.strip() for value in manual_keys)),
                )
            )
    return HubSettings(
        tuple(watched),
        tuple(aliases),
        _score_from_dict(root.get("hall_of_fame_score", {})),
        _validate_manager_profiles(tuple(profiles)),
    )


def _settings_to_dict(settings: HubSettings) -> dict[str, object]:
    score = settings.hall_of_fame_score
    profiles = effective_manager_profiles(settings)
    return {
        "schema_version": HUB_SETTINGS_SCHEMA_VERSION,
        "watchlist": [
            {
                "game_locale": item.game_locale,
                "game_slug": item.game_slug,
                "player_key": item.player_key,
                "entry_id": item.entry_id,
                "person_id": item.person_id,
                "name": item.name,
                "team": item.team,
                "position": item.position,
            }
            for item in settings.watchlist
        ],
        "manager_aliases": [],
        "manager_profiles": [
            {
                "manager_id": item.manager_id,
                "display_name": item.display_name,
                "identity_keys": list(item.identity_keys),
                "profile_urls": list(item.profile_urls),
                "manual_identity_keys": list(item.manual_identity_keys),
            }
            for item in profiles
        ],
        "hall_of_fame_score": {
            "group_points": list(score.group_points),
            "tournament_winner": score.tournament_winner,
            "tournament_finalist": score.tournament_finalist,
            "tournament_semifinalist": score.tournament_semifinalist,
            "global_round_win": score.global_round_win,
        },
    }


class HubSettingsStore:
    """Atomically load and save additive Hub settings."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> HubSettings:
        if not self.path.exists():
            return HubSettings()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise PayloadError(f"Hub-indstillinger kunne ikke læses: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise PayloadError("Hub-indstillinger indeholder ugyldig JSON") from exc
        return _settings_from_dict(payload)

    def save(self, settings: HubSettings) -> None:
        payload = _settings_to_dict(settings)
        replace_text_atomically(
            self.path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )

    def set_watchlist(
        self, settings: HubSettings, watchlist: tuple[WatchlistEntry, ...]
    ) -> HubSettings:
        unique = {item.player_key: item for item in watchlist}
        updated = replace(
            settings,
            watchlist=tuple(sorted(unique.values(), key=lambda item: item.player_key)),
        )
        self.save(updated)
        return updated

    def set_manager_profiles(
        self, settings: HubSettings, profiles: tuple[ManagerProfile, ...]
    ) -> HubSettings:
        """Persist validated profiles and retire schema-1 aliases."""

        profiles = _validate_manager_profiles(profiles)
        updated = replace(
            settings,
            manager_aliases=(),
            manager_profiles=tuple(
                sorted(
                    profiles,
                    key=lambda item: (item.display_name.casefold(), item.manager_id),
                )
            ),
        )
        self.save(updated)
        return updated
