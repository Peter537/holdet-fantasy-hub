from dataclasses import replace
from datetime import timedelta

import pytest
from streamlit.testing.v1 import AppTest

import holdet_lib as holdet
from holdet_lib.local_api import LocalDataApi
from tests.test_import_planning import NOW, paths_for
from tests.test_manager_seasons_tournaments import group_for, snapshots, team_with_rounds


def _published_group(store, group, index):
    matches = holdet.published_knockout_candidates(group, index, group.tournament.final_round)
    store.publish_knockout_matches(
        group.group_id, group.active_revision, matches, config=group.tournament,
        team_ids=tuple(member.team_id for member in group.teams),
    )
    return store.with_published_matches(group), matches


def _calendar_metadata(game, rounds):
    return holdet.GameMetadata(
        game=game,
        game_id=1,
        variant="cycling",
        format="cycling",
        display_name="Demo",
        final_round=rounds,
        salary_cap=100,
        fetched_at=NOW,
        rounds=tuple(
            holdet.ScheduleRound(
                n, NOW + timedelta(days=n),
                NOW + timedelta(days=n, hours=1), NOW + timedelta(days=n, hours=3),
            )
            for n in range(1, rounds + 1)
        ),
    )


def test_calendar_uses_frozen_knockout_participants_and_round_ranges(tmp_path):
    ids = tuple(range(1, 9))
    teams = tuple(team_with_rounds(team, {n: 100 - team for n in range(1, 8)}) for team in ids)
    config = holdet.create_tournament_definition(
        "group_knockout", ids, 1, final_round=7, rounds_per_tie=2,
        seed_rule="manual", seed_order=ids, group_count=2, qualifiers_per_group=2,
    )
    group = replace(group_for("cup", *teams), kind="tournament", tournament=config)
    store = holdet.TournamentPairingStore(tmp_path)
    group, matches = _published_group(store, group, snapshots(*teams))
    metadata = _calendar_metadata(group.game, 7)
    events = holdet.build_calendar_events((group,), (metadata,))
    by_key = {event.event_id: event for event in events}

    assert len(matches) == 3
    assert sum(event.status == "scheduled" for event in events) == len(config.group_fixtures)
    for match in matches:
        event = by_key[f"cup:knockout:{match.match_id}"]
        assert event.status == "published"
        assert event.participant_ids == (match.team_a_id, match.team_b_id)
        assert event.round_numbers == match.round_numbers
        assert event.round_number == match.round_numbers[0]
        assert event.start == metadata.rounds[match.round_numbers[0] - 1].start
        assert event.deadline == metadata.rounds[match.round_numbers[0] - 1].close
        assert event.end == metadata.rounds[match.round_numbers[-1] - 1].end
        assert all(team_id in ids for team_id in event.participant_ids)


@pytest.mark.parametrize("requires_reset", [True, False])
def test_double_elimination_calendar_projects_reset_when_required(tmp_path, requires_reset):
    ids = (1, 2, 3, 4)
    if requires_reset:
        changes = {
            1: {1: 40, 2: 40, 3: 10, 4: 30, 5: 50},
            2: {1: 30, 2: 30, 3: 30, 4: 10, 5: 10},
            3: {1: 20, 2: 30, 3: 40, 4: 40, 5: 40},
            4: {1: 10, 2: 10, 3: 10, 4: 10, 5: 10},
        }
    else:
        changes = {team: {n: 100 - team for n in range(1, 6)} for team in ids}
    teams = tuple(team_with_rounds(team, changes[team]) for team in ids)
    config = holdet.create_tournament_definition(
        "double_elimination", ids, 1, final_round=5, seed_rule="manual", seed_order=ids,
    )
    group = replace(group_for("double", *teams), kind="tournament", tournament=config)
    preview = {event.event_id: event for event in holdet.build_calendar_events((group,), ())}
    assert preview["double:double:GF1"].status == "provisional"
    assert preview["double:double:GF1"].participant_ids == ()
    assert preview["double:double:GF2"].status == "conditional"

    index = snapshots(*teams)
    group, matches = _published_group(holdet.TournamentPairingStore(tmp_path), group, index)
    final = next(match for match in matches if match.match_id == "GF1")
    events = {
        item.event_id: item
        for item in holdet.build_calendar_events((group,), (), snapshots=index)
    }
    if requires_reset:
        reset = next(match for match in matches if match.match_id == "GF2")
        assert events["double:double:GF2"].status == "published"
        assert events["double:double:GF2"].participant_ids == (reset.team_a_id, reset.team_b_id)
    else:
        assert "double:double:GF2" not in events
    assert events["double:double:GF1"].status == "published"
    assert events["double:double:GF1"].participant_ids == (final.team_a_id, final.team_b_id)


def test_calendar_keeps_published_swiss_fixtures(tmp_path):
    teams = tuple(team_with_rounds(team, {1: 10}) for team in (1, 2, 3, 4))
    config = holdet.create_tournament_definition("swiss", (1, 2, 3, 4), 1, swiss_rounds=2)
    group = replace(group_for("swiss", *teams), kind="tournament", tournament=config)
    first_round = tuple(
        holdet.TournamentPairing(item.round_number, item.team_a_id, item.team_b_id)
        for item in config.group_fixtures
    )
    first_opponents = {tuple(sorted((item.team_a_id, item.team_b_id))) for item in first_round}
    options = (((1, 2), (3, 4)), ((1, 3), (2, 4)), ((1, 4), (2, 3)))
    other_pairs = next(pairs for pairs in options if not first_opponents.intersection(pairs))
    store = holdet.TournamentPairingStore(tmp_path)
    store.publish_round("swiss", group.active_revision, 1, first_round)
    store.publish_round(
        "swiss", group.active_revision, 2,
        tuple(holdet.TournamentPairing(2, team_a, team_b) for team_a, team_b in other_pairs),
    )
    events = holdet.build_calendar_events((store.with_published_matches(group),), ())
    assert len(events) == 4
    assert {item.participant_ids for item in events if item.round_numbers == (2,)} == set(other_pairs)


def _season_dashboard(root: str):
    from pathlib import Path

    import holdet_lib as holdet
    from website.hub_pages import managers_view

    paths = holdet.resolve_paths(overrides=holdet.PathOverrides(data_root=Path(root)), environ={})
    groups = holdet.GroupStore(paths.groups_file, paths.group_revision_dir).load()
    managers_view(groups, holdet.SnapshotStore(paths.snapshot_dir).scan(), paths)


def test_season_dashboard_api_and_report_share_corrected_current_identities(tmp_path):
    paths = paths_for(tmp_path / "hub")
    first = team_with_rounds(101, {1: 50}, owner_user_id=900)
    second = team_with_rounds(202, {1: 40}, owner_user_id=901)
    group = group_for("comp", first, second)
    holdet.GroupStore(paths.groups_file, paths.group_revision_dir).create(
        "Competition", group.game, group.teams, group_id=group.group_id,
    )
    snapshot_store = holdet.SnapshotStore(paths.snapshot_dir)
    for team in (first, second):
        snapshot_store.save_team_json(team, now=NOW)
    season = holdet.SeasonDefinition("season", "Season", ("comp",))
    holdet.SeasonStore(paths.seasons_file).save((season,))

    a = holdet.HallOfFamePlacement("owner:900", "Old Alice", 101, "A", 1, 50, ("owner:900",))
    b = holdet.HallOfFamePlacement("owner:901", "Old Bob", 202, "B", 2, 40, ("owner:901",))
    original = holdet.HallOfFameEvent(
        "result", "group", group.game.locale, group.game.slug, "comp", "Original", 1,
        (a, b), True, NOW,
    )
    corrected = replace(
        original, competition_name="Corrected", placements=(replace(b, rank=1), replace(a, rank=2)),
        revision=2, supersedes_revision=1, captured_at=NOW + timedelta(hours=1),
    )
    ledger = holdet.HallOfFameStore(paths.hall_of_fame_dir)
    files = tuple(ledger.freeze(event) for event in (original, corrected))
    before = tuple(file.read_bytes() for file in files)
    events, warnings = ledger.scan()
    assert not warnings

    alice = holdet.ManagerProfile("alice", "Alice", ("owner:900",))
    bob = holdet.ManagerProfile("bob", "Bob", ("owner:901",))
    merged = replace(alice, display_name="Merged", identity_keys=("owner:900", "owner:901"))
    renamed = replace(merged, display_name="Renamed")
    settings_store = holdet.HubSettingsStore(paths.hub_settings_file)
    app = AppTest.from_function(_season_dashboard, args=(str(paths.data_dir.parent),), default_timeout=20)
    app.query_params["section"] = "seasons"
    for profiles, expected in (
        ((merged,), [(1, "Merged", 10, 1)]),
        ((renamed,), [(1, "Renamed", 10, 1)]),
        ((alice, bob), [(1, "Bob", 10, 1), (2, "Alice", 6, 0)]),
    ):
        settings = holdet.HubSettings(manager_profiles=profiles)
        settings_store.save(settings)
        app.run()
        assert not app.exception
        table = next(frame.value for frame in app.dataframe if "Titler" in frame.value)
        assert list(table[["Rang", "Manager", "Point", "Titler"]].itertuples(index=False, name=None)) == expected

        api_rows = LocalDataApi(paths).rows("season_standings", {"season": season.season_id})
        assert [
            (row["rank"], row["manager_name"], row["points"], row["titles"])
            for row in api_rows
        ] == expected
        effective = holdet.build_effective_manager_settings(settings, (group,), snapshot_store.scan())
        report = holdet.build_season_report_package(season, events, effective.hall_of_fame_score, settings=effective)
        table_by_name = {table.name: table for table in report.tables}
        assert [
            (row["rank"], row["manager_name"], row["points"], row["titles"])
            for row in table_by_name["season_standings"].rows
        ] == expected
        assert table_by_name["competitions"].rows[0]["events"] == 1
        assert table_by_name["competitions"].rows[0]["competition_name"] == "Corrected"
        html = holdet.render_html_report(report, title="Season")
        assert "Old Alice" not in html and "Old Bob" not in html
        assert all(name in html for _, name, _, _ in expected)
    assert tuple(file.read_bytes() for file in files) == before
