from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from holdet_lib.game_metadata import GameMetadata, GameMetadataStore
from holdet_lib.groups import GroupStore, HubConfiguration, ManagerGame
from holdet_lib.hub_settings import HubSettings, OwnTeamSelection
from holdet_lib.paths import PathOverrides, resolve_paths
from holdet_lib.storage import PlayerStatisticsStore, SnapshotStore
from tests.test_round_center import GAME, NOW, group, player, player_snapshot, summary, team_snapshot
from website import workflows


@pytest.fixture
def export_cache(tmp_path: Path):
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    manager = ManagerGame(GAME, "Managerspil æøå")
    selected = replace(group(1, 2), group_id="selected", name="Valgt liga")
    other = replace(group(2, 3), group_id="other", name="Anden liga")
    GroupStore(paths.groups_file, paths.group_revision_dir).save_configuration(
        HubConfiguration((manager,), (selected, other))
    )
    teams = SnapshotStore(paths.snapshot_dir)
    team_paths = {}
    for team_id in (1, 2, 3, 9):
        snapshot = team_snapshot(
            team_id,
            (summary(1, 100 + team_id, 10, rank=team_id),
             summary(2, 200 + team_id, 20, rank=team_id)),
        )
        team_paths[team_id] = teams.save_team_json(snapshot.team, now=NOW)
    entries = tuple(player(index, entry_id=index) for index in range(1, 1102))
    players = PlayerStatisticsStore(paths.snapshot_dir)
    players.save(player_snapshot(1, entries[:5]).statistics, now=NOW)
    players.save(player_snapshot(2, entries).statistics, now=NOW + timedelta(minutes=1))
    metadata = GameMetadata(GAME, "soccer", "soccer", 42, 50_000_000, 5, manager.name, (), NOW)
    GameMetadataStore(paths.game_metadata_dir).save(metadata)
    return SimpleNamespace(
        paths=paths, manager=manager, selected=selected, teams=teams,
        team_paths=team_paths, players=players, entries=entries, metadata=metadata,
        settings=HubSettings(own_teams=(OwnTeamSelection(GAME.locale, GAME.slug, 9),)),
    )


def completed_run(cache, *, history: bool = True, status: str = "success") -> dict:
    return {
        "run_id": str(uuid4()),
        "status": status,
        "games": [{
            "game": {
                "locale": GAME.locale,
                "game": GAME.slug,
                "competition_id": cache.selected.group_id,
                "name": cache.manager.name,
            },
            "status": status,
        }],
        "scope": {"export_after": True, "export_history": history},
    }


def ready_download(cache, run):
    before = {path: path.read_bytes() for path in cache.paths.config_dir.parent.rglob("*.json")}
    with (
        patch.object(workflows, "settings_for", return_value=cache.settings),
        patch.object(workflows, "_coordinator", side_effect=AssertionError("Download must not start refresh")),
        patch.object(workflows.st, "download_button") as download,
        patch.object(workflows.st, "warning") as warning,
        patch.object(workflows.st, "caption") as caption,
    ):
        workflows._completed_export(cache.paths, run)
        warning.assert_not_called()
        download.assert_called_once()
        call = download.call_args
        assert callable(call.kwargs["data"])
        document = json.loads(call.kwargs["data"]())
    assert {path: path.read_bytes() for path in cache.paths.config_dir.parent.rglob("*.json")} == before
    return call, document, caption


@pytest.mark.parametrize("history", (True, False))
def test_completed_competition_download_is_ready_complete_and_honors_history(
    export_cache, history: bool
) -> None:
    run = completed_run(export_cache, history=history)
    call, document, caption = ready_download(export_cache, run)

    assert call.args[0] == "Download opdateret JSON"
    assert not call.kwargs.get("disabled", False)
    assert call.kwargs["mime"] == "application/json"
    assert call.kwargs["on_click"] == "ignore"
    assert call.kwargs["key"] == f"completed-export:{run['run_id']}"
    assert document["scope"]["type"] == "competition"
    assert document["scope"]["competition_id"] == "selected"
    assert [item["id"] for item in document["competitions"]] == ["selected"]
    assert [item["id"] for item in document["teams"]] == [1, 2]
    assert document["metadata"]["current"]["game"]["game_id"] == 42
    current = document["players"]["current"]
    assert current["game"]["round"] == 2
    assert [item["entry_id"] for item in current["entries"]] == list(range(1, 1102))
    if history:
        assert [item["game"]["round"] for item in document["players"]["history"]] == [2, 1]
    else:
        assert "history" not in document["players"]
    caption.assert_not_called()


def test_completed_export_rereads_current_stores_on_new_render(export_cache) -> None:
    run = completed_run(export_cache)
    _, before, _ = ready_download(export_cache, run)
    updated = team_snapshot(1, (summary(3, 999, 50, rank=1),))
    export_cache.teams.save_team_json(updated.team, now=NOW + timedelta(hours=1))
    export_cache.players.save(
        player_snapshot(3, (*export_cache.entries, player(1102, entry_id=1102))).statistics,
        now=NOW + timedelta(hours=1),
    )
    GameMetadataStore(export_cache.paths.game_metadata_dir).save(
        replace(export_cache.metadata, game_id=43, fetched_at=NOW + timedelta(hours=1))
    )

    _, after, _ = ready_download(export_cache, run)

    assert before["players"]["current"]["game"]["round"] == 2
    assert after["players"]["current"]["game"]["round"] == 3
    assert len(after["players"]["current"]["entries"]) == 1102
    assert [item["game"]["round"] for item in after["players"]["history"]] == [3, 2, 1]
    assert after["teams"][0]["snapshot"]["game"]["current_round"] == 3
    assert after["teams"][0]["summaries"][0]["total"] == 999
    assert after["metadata"]["current"]["game"]["game_id"] == 43


def test_partial_completed_run_offers_download_with_known_missing_team(export_cache) -> None:
    export_cache.team_paths[2].unlink()
    run = completed_run(export_cache, status="partial")

    call, document, caption = ready_download(export_cache, run)

    assert call.args[0] == "Download JSON med kendte mangler"
    assert not call.kwargs.get("disabled", False)
    missing = next(item for item in document["teams"] if item["id"] == 2)
    assert missing["snapshot"] is None
    assert missing["quality"] == ["missing"]
    assert len(document["players"]["current"]["entries"]) == 1101
    assert any("beskriver manglerne" in call.args[0] for call in caption.call_args_list)


def _download_document(cache, *, completed: bool, competition_id=None):
    from holdet_lib.groups import GroupStore

    run = completed_run(cache, status="partial")
    run["games"][0]["game"]["competition_id"] = competition_id
    with (
        patch.object(workflows, "settings_for", return_value=cache.settings),
        patch.object(workflows, "updates_running", return_value=False),
        patch.object(workflows, "st") as st,
    ):
        st.button.return_value = False
        st.checkbox.return_value = False
        if completed:
            workflows._completed_export(cache.paths, run)
        else:
            groups = GroupStore(cache.paths.groups_file, cache.paths.group_revision_dir).load()
            workflows.render_scope_actions(cache.paths, cache.manager, groups, cache.teams.scan(),
                                           competition_id=competition_id)
        st.download_button.assert_called_once()
        assert not st.download_button.call_args.kwargs.get("disabled", False)
        return json.loads(st.download_button.call_args.kwargs["data"]())


@pytest.mark.parametrize("completed", (False, True))
def test_bad_pairings_keep_readable_data_downloadable_and_results_unverified(export_cache, completed):
    cache = export_cache
    competition = replace(group(1, 2, tournament=True), group_id="selected")
    store = GroupStore(cache.paths.groups_file, cache.paths.group_revision_dir)
    store.save_configuration(HubConfiguration((cache.manager,), (competition,)))
    pairing_file = cache.paths.tournament_pairing_dir / "selected" / "revision-1.json"
    pairing_file.parent.mkdir(parents=True)
    pairing_file.write_text("{", encoding="utf-8")
    before = {path: path.read_bytes() for path in cache.paths.config_dir.parent.rglob("*.json")}

    document = _download_document(cache, completed=completed)

    assert [item["id"] for item in document["teams"]] == [1, 2, 9]
    assert len(document["players"]["current"]["entries"]) == 1101
    exported = document["competitions"][0]
    assert exported["definition"]["quality"] == ["unverified"]
    assert exported["state"]["champion_id"] is None
    assert all(row["rank"] is None for row in exported["state"]["standings"])
    assert exported["state"]["warnings"]
    assert document["warnings"]
    assert {path: path.read_bytes() for path in cache.paths.config_dir.parent.rglob("*.json")} == before


@pytest.mark.parametrize("completed", (False, True))
@pytest.mark.parametrize("corrupt_archive", (False, True))
def test_archived_swiss_pairings_use_archived_revision_and_members(export_cache, completed, corrupt_archive):
    from holdet_lib.groups import GroupTeam
    from holdet_lib.tournament import TournamentPairing
    from holdet_lib.tournament_pairings import TournamentPairingStore

    cache = export_cache
    store = GroupStore(cache.paths.groups_file, cache.paths.group_revision_dir)
    members = tuple(GroupTeam(i, f"Hold {i}", f"{GAME.original}/fantasyteams/{i}") for i in (1, 2, 3, 4))
    store.create_tournament("Swiss", GAME, members, start_round=1, final_round=5,
        rounds_per_tie=1, group_id="cup", template="swiss",
        definition_options={"seed_rule": "manual", "seed_order": (1, 2, 3, 4), "swiss_rounds": 2})
    pairings = TournamentPairingStore(cache.paths.tournament_pairing_dir)
    pairings.publish_round("cup", 1, 2, (TournamentPairing(2, 1, 3), TournamentPairing(2, 2, 4)))
    store.rebuild_tournament("cup", members[:2], final_round=5, template="league")
    if corrupt_archive:
        (cache.paths.tournament_pairing_dir / "cup" / "revision-1.json").write_text("{", encoding="utf-8")
    before = {path: path.read_bytes() for path in cache.paths.config_dir.parent.rglob("*.json")}

    document = _download_document(cache, completed=completed, competition_id="cup")

    competition = document["competitions"][0]
    assert competition["active_revision"] == 2
    old = competition["previous_revisions"][0]
    assert old["revision"] == 1
    assert [member["id"] for member in old["members"]] == [1, 2, 3, 4]
    if not corrupt_archive:
        round_two = {(f["team_a_id"], f["team_b_id"]) for f in old["definition"]["group_fixtures"] if f["round"] == 2}
        assert round_two == {(1, 3), (2, 4)}
        assert all(f["team_a_id"] in (1, 2) and f["team_b_id"] in (None, 1, 2)
                   for f in competition["definition"]["group_fixtures"])
    assert competition["definition"]["quality"] == []
    assert old["definition"]["quality"] == (["unverified"] if corrupt_archive else [])
    assert {path: path.read_bytes() for path in cache.paths.config_dir.parent.rglob("*.json")} == before
