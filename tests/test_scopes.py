from dataclasses import replace

import pytest

from holdet_lib.groups import GroupDefinition, GroupTeam, ManagerGame
from holdet_lib.hub_settings import HubSettings, OwnTeamSelection
from holdet_lib.models import GameUrl
from holdet_lib.scopes import resolve_scope
from holdet_lib.storage import SnapshotIndex
from tests.test_round_center import GAME, group, summary, team_snapshot


def test_game_scope_deduplicates_members_and_includes_own_team() -> None:
    manager = ManagerGame(GAME, "Spil")
    first = replace(group(1, 2), group_id="first")
    second = replace(group(2, 3), group_id="second")
    other_game = GameUrl("https://www.holdet.dk/da/fantasy/other", "da", "other")
    foreign = GroupDefinition("foreign", "Andet", other_game, (GroupTeam(8, "O", "url"),))
    own = team_snapshot(4, (summary(2, 500, 10, rank=1),))
    settings = HubSettings(own_teams=(OwnTeamSelection("DA", GAME.slug, 4),))
    scope = resolve_scope(manager, (first, second, foreign), SnapshotIndex((own,)), settings)
    assert [item.team_id for item in scope.team_references] == [1, 2, 3, 4]
    assert scope.team_references[-1] == own.team.reference
    assert scope.groups == (first, second)
    assert not scope.read_only


def test_competition_excludes_own_and_other_groups_and_validates_identity() -> None:
    manager = ManagerGame(GAME, "Spil", archived_at="2026-01-01")
    first = replace(group(1, 2), group_id="first")
    second = replace(group(2, 3), group_id="second")
    settings = HubSettings(own_teams=(OwnTeamSelection("da", GAME.slug, 4),))
    scope = resolve_scope(manager, (first, second), SnapshotIndex(()), settings, competition_id="first")
    assert scope.read_only
    assert scope.groups == (first,)
    assert [item.team_id for item in scope.team_references] == [1, 2]
    with pytest.raises(ValueError):
        resolve_scope(manager, (first,), SnapshotIndex(()), settings, competition_id="second")


def test_uncached_own_team_has_usable_public_reference() -> None:
    scope = resolve_scope(
        ManagerGame(GAME, "Spil"), (), SnapshotIndex(()),
        HubSettings(own_teams=(OwnTeamSelection("da", GAME.slug, 52),)),
    )
    assert scope.team_references[0].source_url == f"{GAME.original}/fantasyteams/52"


def test_duplicate_prefers_account_reference() -> None:
    direct = replace(group(1), group_id="direct")
    account_member = replace(direct.teams[0], account_key="account", account_label="Ejer")
    account = replace(direct, group_id="account", teams=(account_member,))
    scope = resolve_scope(ManagerGame(GAME, "Spil"), (direct, account), SnapshotIndex(()))
    assert len(scope.team_references) == 1
    assert scope.team_references[0].account_key == "account"
