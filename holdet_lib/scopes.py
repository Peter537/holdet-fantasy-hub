"""Resolve the local entities that belong to a game or competition."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from .groups import GroupDefinition, ManagerGame
from .hub_settings import HubSettings
from .models import GameUrl, TeamReference
from .storage import SnapshotIndex


@dataclass(frozen=True, slots=True)
class ResolvedScope:
    manager_game: ManagerGame
    groups: tuple[GroupDefinition, ...]
    team_references: tuple[TeamReference, ...]
    competition_id: str | None = None

    @property
    def game(self) -> GameUrl:
        return self.manager_game.game

    @property
    def read_only(self) -> bool:
        return self.manager_game.is_archived


def resolve_scope(
    manager_game: ManagerGame,
    groups: tuple[GroupDefinition, ...],
    teams: SnapshotIndex,
    settings: HubSettings | None = None,
    *,
    competition_id: str | None = None,
) -> ResolvedScope:
    """Resolve membership without fetching, writing, or adding unrelated teams.

    The caller may supply groups enriched with persisted tournament pairings.
    An archived scope is still resolvable, but is marked read-only.
    """
    game = manager_game.game
    game_groups = tuple(
        group for group in groups
        if (group.game.locale.casefold(), group.game.slug) == manager_game.identity
    )
    if competition_id is not None:
        selected = tuple(group for group in game_groups if group.group_id == competition_id)
        if len(selected) != 1:
            raise ValueError("Konkurrencen findes ikke entydigt i managerspillet")
        game_groups = selected

    references: dict[int, TeamReference] = {}
    def remember(reference: TeamReference) -> None:
        existing = references.get(reference.team_id)
        if existing is None or (existing.account_key == "direct" and reference.account_key != "direct"):
            references[reference.team_id] = reference

    for group in game_groups:
        for member in group.teams:
            remember(member.reference(game))

    if competition_id is None and settings is not None:
        selections = tuple(
            item for item in settings.own_teams
            if (item.game_locale.casefold(), item.game_slug) == manager_game.identity
        )
        if len(selections) > 1:
            raise ValueError("Managerspillet har flere valgte egne hold")
        if selections:
            team_id = selections[0].team_id
            snapshot = teams.newest(game, team_id)
            if snapshot is not None:
                reference = snapshot.team.reference
            else:
                url = (
                    f"https://www.holdet.dk/{quote(game.locale, safe='-')}/fantasy/"
                    f"{quote(game.slug, safe='-')}/fantasyteams/{team_id}"
                )
                reference = TeamReference(game, team_id, f"Hold {team_id}", url)
            remember(reference)

    return ResolvedScope(manager_game, game_groups, tuple(references.values()), competition_id)
