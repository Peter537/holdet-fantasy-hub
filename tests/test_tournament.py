from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

import holdet_lib as holdet
from tests.test_library_storage import sample_team, temporary_directory


def team_with_changes(team_id: int, changes: dict[int, int], *, name: str | None = None):
    current_round = max(changes)
    base = sample_team(
        team_id,
        name=name or f"Hold {team_id}",
        current_round=current_round,
        history_rounds=tuple(sorted(changes, reverse=True)),
    )
    history = tuple(
        replace(summary, change=changes[summary.round_number])
        for summary in base.history
    )
    latest = next(item for item in history if item.round_number == current_round)
    return replace(
        base,
        overview=replace(
            base.overview,
            current_round=current_round,
            current_change=latest.change,
        ),
        history=history,
    )


def tournament_group(teams, *, start=1, final=5, legs=1):
    members = tuple(
        holdet.GroupTeam(
            team.reference.team_id, team.team_name, team.reference.source_url
        )
        for team in teams
    )
    config = holdet.create_tournament_config(
        tuple(team.reference.team_id for team in teams),
        start,
        final,
        legs,
        shuffle=lambda values: None,
    )
    return holdet.GroupDefinition(
        "cup", "Cup", teams[0].reference.game, members, "tournament", config
    )


def snapshot_index(*teams):
    return holdet.SnapshotIndex(
        tuple(
            holdet.TeamSnapshot(
                Path(f"team-{team.reference.team_id}.json"),
                datetime(2026, 7, 26, tzinfo=timezone.utc),
                team,
            )
            for team in teams
        )
    )


class TournamentScheduleTests(unittest.TestCase):
    def test_knockout_sizes_and_standard_seed_order(self):
        expected = {2: 2, 3: 2, 4: 4, 7: 4, 8: 8, 15: 8, 16: 16, 31: 16, 32: 32, 99: 32}
        self.assertEqual(
            {count: holdet.knockout_size_for(count) for count in expected}, expected
        )
        self.assertEqual(
            holdet.bracket_seed_order(8), (1, 8, 4, 5, 2, 7, 3, 6)
        )

    def test_circle_schedule_balances_even_odd_partial_and_repeated_cycles(self):
        even = holdet.generate_group_fixtures(
            (1, 2, 3, 4), 1, 5, shuffle=lambda values: None
        )
        first_cycle_pairs = {
            frozenset((item.team_a_id, item.team_b_id))
            for item in even
            if item.round_number <= 3 and item.team_b_id is not None
        }
        self.assertEqual(len(first_cycle_pairs), 6)
        even_counts = {
            team_id: sum(
                team_id in (item.team_a_id, item.team_b_id) for item in even
            )
            for team_id in (1, 2, 3, 4)
        }
        self.assertEqual(set(even_counts.values()), {5})

        odd = holdet.generate_group_fixtures(
            (1, 2, 3, 4, 5), 1, 3, shuffle=lambda values: None
        )
        odd_counts = {
            team_id: sum(
                team_id in (item.team_a_id, item.team_b_id)
                and item.team_b_id is not None
                for item in odd
            )
            for team_id in (1, 2, 3, 4, 5)
        }
        self.assertLessEqual(max(odd_counts.values()) - min(odd_counts.values()), 1)
        played_pairs = [
            frozenset((item.team_a_id, item.team_b_id))
            for item in odd
            if item.team_b_id is not None
        ]
        self.assertEqual(len(played_pairs), len(set(played_pairs)))

    def test_period_validation_requires_a_group_round(self):
        with self.assertRaisesRegex(holdet.PayloadError, "gruppespilsrunde"):
            holdet.create_tournament_config((1, 2, 3, 4), 1, 2, 1)
        with self.assertRaisesRegex(holdet.PayloadError, "mindst to"):
            holdet.create_tournament_config((1,), 1, 5, 1)


    def test_draw_seed_is_reproducible_and_order_independent(self):
        first = holdet.create_tournament_config(
            (1, 2, 3, 4, 5, 6, 7, 8), 1, 10, 1, draw_seed="seed-a"
        )
        repeated = holdet.create_tournament_config(
            (8, 7, 6, 5, 4, 3, 2, 1), 1, 10, 1, draw_seed="seed-a"
        )
        different = holdet.create_tournament_config(
            (1, 2, 3, 4, 5, 6, 7, 8), 1, 10, 1, draw_seed="seed-b"
        )
        self.assertEqual(first.draw_seed, "seed-a")
        self.assertEqual(first.group_fixtures, repeated.group_fixtures)
        self.assertNotEqual(
            holdet.tournament_schedule_signature(first.group_fixtures),
            holdet.tournament_schedule_signature(different.group_fixtures),
        )

    def test_signature_ignores_irrelevant_home_away_order(self):
        first = (holdet.GroupFixture(1, 10, 20), holdet.GroupFixture(1, 30, None))
        reversed_sides = (
            holdet.GroupFixture(1, 20, 10), holdet.GroupFixture(1, 30, None)
        )
        self.assertEqual(
            holdet.tournament_schedule_signature(first),
            holdet.tournament_schedule_signature(reversed_sides),
        )

    def test_store_redraws_against_active_and_archived_plans(self):
        teams = tuple(sample_team(team_id) for team_id in range(1, 6))
        members = tuple(
            holdet.GroupTeam(
                team.reference.team_id, team.team_name, team.reference.source_url
            )
            for team in teams
        )
        with temporary_directory() as temporary:
            store = holdet.GroupStore(temporary / "groups.json")
            original = store.create_tournament(
                "F?rste", teams[0].reference.game, members[:4],
                start_round=1, final_round=6, rounds_per_tie=1,
                group_id="first", draw_seed="seed-a",
            )
            store.rebuild_tournament(
                "first", members, final_round=6, seed_generator=lambda: "seed-c"
            )
            redraw = store.create_tournament(
                "Anden", teams[0].reference.game, members[:4],
                start_round=1, final_round=6, rounds_per_tie=1,
                group_id="second", draw_seed="seed-a",
                seed_generator=lambda: "seed-b",
            )
            self.assertEqual(original.tournament.draw_seed, "seed-a")
            self.assertEqual(redraw.tournament.draw_seed, "seed-b")
            self.assertNotEqual(
                holdet.tournament_schedule_signature(
                    original.tournament.group_fixtures
                ),
                holdet.tournament_schedule_signature(
                    redraw.tournament.group_fixtures
                ),
            )
            payload = json.loads(
                (temporary / "groups.json").read_text(encoding="utf-8")
            )
            self.assertEqual(payload["schema_version"], 7)
            self.assertEqual(
                payload["groups"][1]["tournament"]["draw_seed"], "seed-b"
            )

    def test_two_team_draw_explains_the_only_possible_schedule_in_ui_contract(self):
        teams = tuple(sample_team(team_id) for team_id in (1, 2))
        members = tuple(
            holdet.GroupTeam(
                team.reference.team_id, team.team_name, team.reference.source_url
            )
            for team in teams
        )
        with temporary_directory() as temporary:
            store = holdet.GroupStore(temporary / "groups.json")
            first = store.create_tournament(
                "En", teams[0].reference.game, members, start_round=1,
                final_round=3, rounds_per_tie=1, draw_seed="seed-a",
            )
            second = store.create_tournament(
                "To", teams[0].reference.game, members, start_round=1,
                final_round=3, rounds_per_tie=1, draw_seed="seed-b",
            )
            self.assertEqual(
                holdet.tournament_schedule_signature(first.tournament.group_fixtures),
                holdet.tournament_schedule_signature(second.tournament.group_fixtures),
            )


class TournamentResultTests(unittest.TestCase):
    def test_group_table_seeded_knockout_tie_and_champion(self):
        teams = tuple(
            team_with_changes(
                team_id,
                {
                    1: 110 - team_id * 10,
                    2: 110 - team_id * 10,
                    3: 110 - team_id * 10,
                    4: {1: 5, 2: 0, 3: 10, 4: 5}[team_id],
                    5: {1: 1, 2: 0, 3: 2, 4: 0}[team_id],
                },
            )
            for team_id in range(1, 5)
        )
        group = tournament_group(teams)
        state = holdet.build_tournament_state(group, snapshot_index(*teams), 5)
        self.assertEqual([row.team_id for row in state.standings], [1, 2, 3, 4])
        self.assertEqual(
            [(row.played, row.wins, row.points) for row in state.standings],
            [(3, 3, 9), (3, 2, 6), (3, 1, 3), (3, 0, 0)],
        )
        semifinals = [m for m in state.knockout_matches if m.stage == "Semifinaler"]
        self.assertEqual(
            [(m.team_a_seed, m.team_b_seed) for m in semifinals], [(1, 4), (2, 3)]
        )
        self.assertEqual(semifinals[0].winner_id, 1)
        self.assertEqual(state.champion_id, 3)
        self.assertEqual(state.phase, "Afsluttet")
        self.assertEqual(state.active_team_ids, frozenset())

    def test_missing_data_waits_and_keeps_everyone_active(self):
        teams = tuple(
            team_with_changes(team_id, {1: team_id, 2: team_id, 3: team_id})
            for team_id in range(1, 5)
        )
        group = tournament_group(teams)
        incomplete = replace(teams[3], history=teams[3].history[1:])
        state = holdet.build_tournament_state(
            group, snapshot_index(*teams[:3], incomplete), 3
        )
        self.assertEqual(state.knockout_matches, ())
        self.assertEqual(state.active_team_ids, frozenset({1, 2, 3, 4}))
        self.assertTrue(any("afventer rundedata" in warning for warning in state.warnings))

    def test_two_round_ties_are_aggregated_and_high_seed_advances(self):
        teams = tuple(
            team_with_changes(
                team_id,
                {
                    1: 100 - team_id,
                    2: 100 - team_id,
                    3: 100 - team_id,
                    4: {1: 5, 2: 4, 3: 2, 4: 1}[team_id],
                    5: {1: 0, 2: 1, 3: 3, 4: 4}[team_id],
                    6: 1,
                    7: 1,
                },
            )
            for team_id in range(1, 5)
        )
        group = tournament_group(teams, final=7, legs=2)
        state = holdet.build_tournament_state(group, snapshot_index(*teams), 7)
        first = state.knockout_matches[0]
        self.assertEqual(first.round_numbers, (4, 5))
        self.assertEqual(first.team_a_change, first.team_b_change)
        self.assertEqual(first.winner_id, first.team_a_id)
        self.assertEqual(state.champion_id, 1)


    def test_newer_snapshot_recalculates_historical_winner(self):
        teams = tuple(
            team_with_changes(
                team_id,
                {
                    1: 110 - team_id * 10,
                    2: 110 - team_id * 10,
                    3: 110 - team_id * 10,
                    4: {1: 5, 2: 0, 3: 10, 4: 5}[team_id],
                    5: {1: 1, 2: 0, 3: 2, 4: 0}[team_id],
                },
            )
            for team_id in range(1, 5)
        )
        group = tournament_group(teams)
        old_time = datetime(2026, 7, 25, tzinfo=timezone.utc)
        old = tuple(
            holdet.TeamSnapshot(Path(f"old-{team.reference.team_id}"), old_time, team)
            for team in teams
        )
        self.assertEqual(
            holdet.build_tournament_state(
                group, holdet.SnapshotIndex(old), 5
            ).champion_id,
            3,
        )
        corrected = replace(
            teams[0],
            history=tuple(
                replace(item, change=100 if item.round_number == 5 else item.change)
                for item in teams[0].history
            ),
        )
        newer = holdet.TeamSnapshot(
            Path("corrected-1"),
            datetime(2026, 7, 26, tzinfo=timezone.utc),
            corrected,
        )
        self.assertEqual(
            holdet.build_tournament_state(
                group, holdet.SnapshotIndex((*old, newer)), 5
            ).champion_id,
            1,
        )

    def test_refresh_fetches_all_teams_and_records_post_refresh_final(self):
        old_teams = tuple(
            team_with_changes(
                team_id,
                {
                    1: 110 - team_id * 10,
                    2: 110 - team_id * 10,
                    3: 110 - team_id * 10,
                    4: {1: 5, 2: 0, 3: 10, 4: 1}[team_id],
                },
            )
            for team_id in range(1, 5)
        )
        refreshed_teams = tuple(
            team_with_changes(
                team_id,
                {
                    1: 110 - team_id * 10,
                    2: 110 - team_id * 10,
                    3: 110 - team_id * 10,
                    4: {1: 5, 2: 0, 3: 10, 4: 1}[team_id],
                    5: {1: 1, 2: 0, 3: 20, 4: 0}[team_id],
                },
            )
            for team_id in range(1, 5)
        )
        group = tournament_group(old_teams)
        fetched: list[int] = []

        class FakeClient:
            def fetch_team(self, reference):
                fetched.append(reference.team_id)
                return next(
                    team for team in refreshed_teams
                    if team.reference.team_id == reference.team_id
                )

        with temporary_directory() as temporary:
            store = holdet.SnapshotStore(temporary)
            for team in old_teams:
                store.save_team_json(team)
            result = holdet.refresh_group(
                group, FakeClient(), store, holdet.ManifestStore(temporary / "manifests")
            )
            self.assertEqual(set(fetched), {1, 2, 3, 4})
            self.assertEqual({item.team_id for item in result.teams}, {1, 2, 3, 4})
            self.assertEqual(result.round_number, 5)
            manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["group"]["type"], "tournament")
            self.assertEqual(manifest["group"]["revision"], 1)
            self.assertEqual(manifest["phase"], "Afsluttet")
            self.assertEqual(manifest["latest_round"], 5)
            self.assertEqual(manifest["skipped_team_ids"], [])
            self.assertIsNotNone(manifest["champion_id"])
            self.assertEqual(
                set(manifest["eliminated_team_ids"]),
                {1, 2, 3, 4} - {manifest["champion_id"]},
            )

    def test_head_to_head_includes_group_and_knockout_rounds(self):
        teams = tuple(
            team_with_changes(
                team_id,
                {
                    1: 110 - team_id * 10,
                    2: 110 - team_id * 10,
                    3: 110 - team_id * 10,
                    4: {1: 5, 2: 0, 3: 10, 4: 1}[team_id],
                    5: {1: 1, 2: 0, 3: 2, 4: 0}[team_id],
                },
            )
            for team_id in range(1, 5)
        )
        group = tournament_group(teams)
        summary = holdet.build_tournament_head_to_head(
            group, snapshot_index(*teams), 1, 4, 5
        )
        self.assertEqual(
            [(match.round_number, match.phase) for match in summary.matches],
            [(1, "Gruppespil"), (4, "Semifinaler")],
        )
        self.assertEqual(summary.played, 2)
        self.assertEqual(summary.team_a_wins, 2)
        self.assertEqual(summary.draws, 0)
        self.assertEqual(summary.team_b_wins, 0)
        self.assertEqual(
            summary.growth_difference,
            summary.team_a_growth - summary.team_b_growth,
        )

    def test_head_to_head_two_leg_tie_counts_two_matches_and_notes_seed(self):
        teams = tuple(
            team_with_changes(
                team_id,
                {
                    1: 100 - team_id,
                    2: 100 - team_id,
                    3: 100 - team_id,
                    4: {1: 5, 2: 4, 3: 2, 4: 1}[team_id],
                    5: {1: 0, 2: 1, 3: 3, 4: 4}[team_id],
                    6: 1,
                    7: 1,
                },
            )
            for team_id in range(1, 5)
        )
        group = tournament_group(teams, final=7, legs=2)
        summary = holdet.build_tournament_head_to_head(
            group, snapshot_index(*teams), 1, 4, 5
        )
        knockout = [match for match in summary.matches if match.phase == "Semifinaler"]
        self.assertEqual([match.round_number for match in knockout], [4, 5])
        self.assertEqual(len(knockout), 2)
        self.assertEqual(knockout[-1].advanced_by_seed_id, 1)
        self.assertEqual(summary.played, 3)

    def test_head_to_head_pending_match_is_visible_but_not_counted(self):
        teams = tuple(
            team_with_changes(team_id, {1: team_id, 2: team_id, 3: team_id})
            for team_id in range(1, 5)
        )
        group = tournament_group(teams)
        incomplete = replace(
            teams[3],
            history=tuple(
                item for item in teams[3].history if item.round_number != 1
            ),
        )
        summary = holdet.build_tournament_head_to_head(
            group, snapshot_index(*teams[:3], incomplete), 1, 4, 1
        )
        self.assertEqual(len(summary.matches), 1)
        self.assertFalse(summary.matches[0].complete)
        self.assertEqual(summary.played, 0)


    def test_head_to_head_handles_negative_growth_and_draws(self):
        first = team_with_changes(1, {1: -5, 2: -3})
        second = team_with_changes(2, {1: -10, 2: -3})
        group = tournament_group((first, second), final=3)
        summary = holdet.build_tournament_head_to_head(
            group, snapshot_index(first, second), 1, 2, 2
        )
        self.assertEqual(summary.played, 2)
        self.assertEqual(summary.team_a_wins, 1)
        self.assertEqual(summary.draws, 1)
        self.assertEqual(summary.team_b_wins, 0)
        self.assertEqual(summary.team_a_growth, -8)
        self.assertEqual(summary.team_b_growth, -13)


class TournamentStorageTests(unittest.TestCase):
    def test_schema_one_loads_and_next_save_writes_schema_five(self):
        team = sample_team(1)
        member = holdet.GroupTeam(1, team.team_name, team.reference.source_url)
        with temporary_directory() as temporary:
            path = temporary / "groups.json"
            path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "groups": [
                            {
                                "id": "old",
                                "name": "Old",
                                "game": {
                                    "url": team.reference.game.original,
                                    "locale": team.reference.game.locale,
                                    "slug": team.reference.game.slug,
                                },
                                "teams": [
                                    {
                                        "id": 1,
                                        "name": team.team_name,
                                        "source_url": team.reference.source_url,
                                    }
                                ],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            store = holdet.GroupStore(path)
            self.assertEqual(store.load()[0].kind, "standings")
            second = sample_team(2)
            tournament = store.create_tournament(
                "Cup",
                team.reference.game,
                (
                    member,
                    holdet.GroupTeam(2, second.team_name, second.reference.source_url),
                ),
                start_round=1,
                final_round=3,
                rounds_per_tie=1,
                group_id="cup",
                shuffle=lambda values: None,
            )
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 7)
            loaded = store.load()[1]
            self.assertEqual(loaded.tournament, tournament.tournament)
            store.update(replace(loaded, name="Nyt navn"))
            with self.assertRaisesRegex(holdet.PayloadError, "ny revision"):
                store.update(replace(store.load()[1], teams=(member,)))


    def test_rebuild_archives_revision_and_recalculates_membership(self):
        teams = tuple(sample_team(team_id) for team_id in (1, 2, 3))
        members = tuple(
            holdet.GroupTeam(t.reference.team_id, t.team_name, t.reference.source_url)
            for t in teams
        )
        with temporary_directory() as temporary:
            path = temporary / "groups.json"
            store = holdet.GroupStore(path)
            original = store.create_tournament(
                "Cup", teams[0].reference.game, members[:2],
                start_round=1, final_round=4, rounds_per_tie=1,
                group_id="cup", shuffle=lambda values: None,
            )
            renamed = store.rebuild_tournament(
                "cup", members[:2], final_round=4, name="Nyt navn"
            )
            self.assertEqual(renamed.active_revision, 1)
            self.assertEqual(renamed.archived_revisions, ())

            rebuilt = store.rebuild_tournament(
                "cup", members, final_round=5,
                now=datetime(2026, 7, 27, tzinfo=timezone.utc),
                shuffle=lambda values: None,
            )
            self.assertEqual(rebuilt.active_revision, 2)
            self.assertEqual(len(rebuilt.archived_revisions), 1)
            self.assertEqual(rebuilt.archived_revisions[0].teams, original.teams)
            self.assertEqual(rebuilt.archived_revisions[0].tournament, original.tournament)
            self.assertEqual(tuple(t.team_id for t in rebuilt.teams), (1, 2, 3))
            loaded = store.load()[0]
            self.assertEqual(loaded, rebuilt)
            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], 7)
            revision_entry = payload["groups"][0]["tournament_revisions"][0]
            self.assertEqual(revision_entry["file"], "cup/revision-1.json")
            self.assertNotIn("teams", revision_entry)
            revision_path = temporary / "group-revisions" / "cup" / "revision-1.json"
            self.assertTrue(revision_path.exists())
            revision_payload = json.loads(revision_path.read_text(encoding="utf-8"))
            self.assertEqual(revision_payload["schema_version"], 1)
            self.assertEqual(revision_payload["revision"], 1)

    def test_invalid_rebuild_leaves_active_revision_untouched(self):
        teams = tuple(sample_team(team_id) for team_id in (1, 2, 3, 4))
        members = tuple(
            holdet.GroupTeam(t.reference.team_id, t.team_name, t.reference.source_url)
            for t in teams
        )
        with temporary_directory() as temporary:
            store = holdet.GroupStore(temporary / "groups.json")
            original = store.create_tournament(
                "Cup", teams[0].reference.game, members[:2],
                start_round=1, final_round=3, rounds_per_tie=1,
                group_id="cup", shuffle=lambda values: None,
            )
            with self.assertRaisesRegex(holdet.PayloadError, "gruppespilsrunde"):
                store.rebuild_tournament(
                    "cup", members, final_round=2, shuffle=lambda values: None
                )
            self.assertEqual(store.load()[0], original)


if __name__ == "__main__":
    unittest.main()
