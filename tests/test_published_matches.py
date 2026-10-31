from dataclasses import replace
import json
import unittest
from unittest.mock import patch

from holdet_lib.errors import PayloadError
from holdet_lib.tournament import (
    SwissParticipant,
    SwissPairingSearchLimitError,
    build_tournament_state,
    build_tournament_head_to_head,
    create_tournament_definition,
    generate_swiss_pairings,
    published_knockout_candidates,
)
from holdet_lib.tournament_pairings import TournamentPairingStore
from tests.test_library_storage import temporary_directory
from tests.test_manager_seasons_tournaments import group_for, snapshots, team_with_rounds
from tests.test_tournament import snapshot_index, team_with_changes, tournament_group


class PublishedMatchTests(unittest.TestCase):
    def test_swiss_backtracks_before_allowing_rematches(self):
        opponents = {1: (2, 3), 2: (1, 5), 3: (4, 1), 4: (3, 6), 5: (6, 2), 6: (5, 4)}
        points = {1: 6, 2: 0, 3: 3, 4: 3, 5: 6, 6: 0}
        scores = {1: 2, 2: 0, 3: 1, 4: 1, 5: 2, 6: 0}
        participants = tuple(SwissParticipant(
            i, match_points=points[i], score_for=scores[i], opponent_ids=opponents[i],
            entry_seed=i, buchholz=sum(points[o] for o in opponents[i]),
        ) for i in range(1, 7))
        fixtures = generate_swiss_pairings(participants, 3)
        self.assertEqual(len(fixtures), 3)
        self.assertTrue(all(item.team_b_id not in opponents[item.team_a_id] for item in fixtures))
        self.assertEqual(fixtures, generate_swiss_pairings(participants, 3))

    def test_swiss_search_limit_does_not_publish_an_unproven_rematch(self):
        participants = tuple(SwissParticipant(i, entry_seed=i) for i in range(1, 5))
        with patch("holdet_lib.tournament._SWISS_PAIRING_SEARCH_LIMIT", 0):
            with self.assertRaisesRegex(SwissPairingSearchLimitError, "søgegrænsen"):
                generate_swiss_pairings(participants, 2)

    def test_swiss_falls_back_when_all_pairs_are_rematches(self):
        participants = tuple(SwissParticipant(
            i, opponent_ids=tuple(j for j in range(1, 5) if i != j), entry_seed=i
        ) for i in range(1, 5))
        fixtures = generate_swiss_pairings(participants, 4)
        self.assertEqual(len(fixtures), 2)
        self.assertEqual(
            {team_id for item in fixtures for team_id in (item.team_a_id, item.team_b_id)},
            {1, 2, 3, 4},
        )

    def test_knockout_correction_preserves_opponents_and_blocks_progression(self):
        def team(i, first):
            return team_with_changes(i, {1: first, 2: 40-i, 3: 40-i, 4: 40-i, 5: 40-i})
        original = tuple(team(i, 40-i) for i in range(1, 5))
        group = tournament_group(original)
        original_index = snapshot_index(*original)
        baseline = published_knockout_candidates(group, original_index, 5)
        with temporary_directory() as root:
            store = TournamentPairingStore(root)
            self.assertFalse(store.has_revision(group.group_id, group.active_revision))
            store.publish_knockout_matches(group.group_id, 1, baseline, config=group.tournament, team_ids=(1, 2, 3, 4))
            protected = store.with_published_knockout_matches(group)
            self.assertEqual(store.with_published_knockout_matches(protected), protected)
            self.assertEqual(build_tournament_state(protected, original_index, 5).champion_id, 1)
            corrected = tuple(team(i, -100 if i == 1 else 40-i) for i in range(1, 5))
            state = build_tournament_state(protected, snapshot_index(*corrected), 5)
            self.assertTrue(state.pairing_conflicts)
            self.assertIsNone(state.champion_id)
            final = next(match for match in state.knockout_matches if match.stage == "Finale")
            old_final = next(match for match in baseline if match.stage == "Finale")
            self.assertEqual((final.team_a_id, final.team_b_id), (old_final.team_a_id, old_final.team_b_id))
            self.assertIsNotNone(final.team_a_change)
            self.assertTrue(final.blocked)
            self.assertIsNone(final.winner_id)
            with self.assertRaises(PayloadError):
                published_knockout_candidates(protected, snapshot_index(*corrected), 5)
            with self.assertRaises(PayloadError):
                store.publish_knockout_matches(group.group_id, 1, (replace(baseline[0], team_a_id=3),), config=group.tournament, team_ids=(1, 2, 3, 4))
            self.assertEqual(store.load(group.group_id, 1).knockout_matches, baseline)

    def test_double_elimination_correction_preserves_frozen_bracket(self):
        changes = {
            1: {1: 40, 2: 40, 3: 10, 4: 30, 5: 50},
            2: {1: 30, 2: 30, 3: 30, 4: 10, 5: 10},
            3: {1: 20, 2: 30, 3: 40, 4: 40, 5: 40},
            4: {1: 10, 2: 10, 3: 10, 4: 10, 5: 10},
        }
        teams = tuple(team_with_rounds(i, rounds) for i, rounds in changes.items())
        config = create_tournament_definition("double_elimination", (1, 2, 3, 4), 1, final_round=5, seed_rule="manual", seed_order=(1, 2, 3, 4))
        group = replace(group_for("double-frozen", *teams), kind="tournament", tournament=config)
        baseline = published_knockout_candidates(group, snapshots(*teams), 5)
        self.assertIn("GF2", {item.match_id for item in baseline})
        without_reset = tuple(item for item in baseline if item.match_id != "GF2")
        partial = replace(group, tournament=replace(config, published_matches=without_reset))
        self.assertIsNone(build_tournament_state(partial, snapshots(*teams), 5).champion_id)
        group = replace(group, tournament=replace(config, published_matches=baseline))
        self.assertEqual(build_tournament_state(group, snapshots(*teams), 5).champion_id, 1)
        changes[1][1] = 0
        corrected = tuple(team_with_rounds(i, rounds) for i, rounds in changes.items())
        state = build_tournament_state(group, snapshots(*corrected), 5)
        self.assertIn("W2-1", state.pairing_conflicts)
        self.assertIsNone(state.champion_id)
        final = next(item for item in state.knockout_matches if item.match_id == "GF1")
        expected = next(item for item in baseline if item.match_id == "GF1")
        self.assertEqual((final.team_a_id, final.team_b_id), (expected.team_a_id, expected.team_b_id))
        self.assertTrue(final.blocked)

    def test_schema_one_read_and_explicit_empty_knockout_baseline(self):
        with temporary_directory() as root:
            store = TournamentPairingStore(root)
            path = root / "old" / "revision-1.json"
            path.parent.mkdir()
            path.write_text(json.dumps({"schema_version": 1, "group_id": "old", "tournament_revision": 1,
                                        "pairings": [{"round": 2, "team_a_id": 1, "team_b_id": 2}]}))
            self.assertEqual(len(store.load("old", 1).pairings), 1)
            self.assertEqual(store.load("old", 1).knockout_matches, ())
            teams = tuple(team_with_changes(i, {1: 2, 2: 2, 3: 2, 4: 2, 5: 2}) for i in range(1, 5))
            group = tournament_group(teams)
            self.assertIsNone(store.with_published_knockout_matches(group).tournament.published_matches)
            store.publish_knockout_matches(group.group_id, 1, (), config=group.tournament, team_ids=(1, 2, 3, 4))
            self.assertEqual(store.with_published_knockout_matches(group).tournament.published_matches, ())
            self.assertEqual(json.loads((root / group.group_id / "revision-1.json").read_text())["schema_version"], 2)

    def test_empty_double_elimination_branches_do_not_block_real_matches(self):
        ids = tuple(range(1, 6))
        teams = tuple(team_with_rounds(i, {r: 100-i for r in range(1, 12)}) for i in ids)
        config = create_tournament_definition("double_elimination", ids, 1, final_round=11, seed_rule="manual", seed_order=ids)
        group = replace(group_for("odd", *teams), kind="tournament", tournament=config)
        index = snapshots(*teams)
        candidates = published_knockout_candidates(group, index, 11)
        with temporary_directory() as root:
            store = TournamentPairingStore(root)
            store.publish_knockout_matches(group.group_id, 1, candidates, config=config, team_ids=ids)
            state = build_tournament_state(store.with_published_knockout_matches(group), index, 11)
        self.assertEqual(state.champion_id, 1)
        self.assertEqual(state.pairing_conflicts, ())

    def test_unchanged_pairings_accept_result_correction(self):
        def teams_for(last):
            return tuple(team_with_changes(i, {1: 40-i, 2: 40-i, 3: 40-i, 4: 40-i, 5: last if i == 1 else 40-i}) for i in range(1, 5))
        original = teams_for(50)
        group = tournament_group(original)
        baseline = published_knockout_candidates(group, snapshot_index(*original), 5)
        group = replace(group, tournament=replace(group.tournament, published_matches=baseline))
        corrected = teams_for(45)
        state = build_tournament_state(group, snapshot_index(*corrected), 5)
        self.assertEqual(state.pairing_conflicts, ())
        self.assertEqual(state.champion_id, 1)
        final = next(item for item in state.knockout_matches if item.stage == "Finale")
        self.assertEqual(final.team_a_change, 45)

    def test_protected_bracket_preserves_confirmed_eliminations_before_final(self):
        teams = tuple(team_with_changes(i, {r: 40-i for r in range(1, 6)}) for i in range(1, 5))
        group = tournament_group(teams)
        index = snapshot_index(*teams)
        fixtures = published_knockout_candidates(group, index, 4)
        protected = replace(group, tournament=replace(group.tournament, published_matches=fixtures))
        state = build_tournament_state(protected, index, 4)
        self.assertEqual(state.active_team_ids, frozenset({1, 2}))
        self.assertEqual(state.eliminated_team_ids, frozenset({3, 4}))
        self.assertIsNone(state.champion_id)

    def test_frozen_participants_are_hidden_before_their_sources_resolve(self):
        teams = tuple(team_with_changes(i, {r: 40-i for r in range(1, 6)}) for i in range(1, 5))
        group = tournament_group(teams)
        index = snapshot_index(*teams)
        fixtures = published_knockout_candidates(group, index, 5)
        protected = replace(group, tournament=replace(group.tournament, published_matches=fixtures))
        early = build_tournament_state(protected, index, 1)
        self.assertTrue(all(m.team_a_id is None and m.team_b_id is None for m in early.knockout_matches))
        qualified = build_tournament_state(protected, index, 3)
        self.assertTrue(all(m.team_a_id is not None and m.team_b_id is not None for m in qualified.knockout_matches if m.stage == "Semifinaler"))
        final = next(m for m in qualified.knockout_matches if m.stage == "Finale")
        self.assertIsNone(final.team_a_id)
        self.assertIsNone(final.team_b_id)
        final = next(m for m in build_tournament_state(protected, index, 4).knockout_matches if m.stage == "Finale")
        self.assertEqual((final.team_a_id, final.team_b_id), (1, 2))

        incomplete = replace(teams[0], history=tuple(row for row in teams[0].history if row.round_number != 4))
        state = build_tournament_state(protected, snapshot_index(incomplete, *teams[1:]), 5)
        final = next(m for m in state.knockout_matches if m.stage == "Finale")
        self.assertIsNone(final.team_a_id)
        self.assertEqual(final.team_b_id, 2)

    def test_conflicted_frozen_match_is_excluded_from_head_to_head_totals(self):
        team = lambda i, first: team_with_changes(i, {1:first, 2:40-i, 3:40-i, 4:40-i, 5:40-i})
        original = tuple(team(i, 40-i) for i in range(1, 5))
        group = tournament_group(original)
        fixtures = published_knockout_candidates(group, snapshot_index(*original), 5)
        protected = replace(group, tournament=replace(group.tournament, published_matches=fixtures))
        corrected = tuple(team(i, -100 if i == 1 else 40-i) for i in range(1, 5))
        summary = build_tournament_head_to_head(protected, snapshot_index(*corrected), 1, 2, 5)
        final = next(m for m in summary.matches if m.phase == "Finale")
        self.assertIsNotNone(final.team_a_change)
        self.assertFalse(final.complete)
        self.assertTrue(final.blocked)
        self.assertIsNone(final.winner_id)
        self.assertEqual(summary.played, 1)

    def test_pairing_error_suppresses_official_results_for_all_templates(self):
        import holdet_lib as holdet
        ids = (1, 2, 3, 4)
        teams = tuple(team_with_rounds(i, {r: 100-i for r in range(1, 11)}) for i in ids)
        index = snapshots(*teams)
        for template in ("league", "swiss", "group_knockout", "double_elimination"):
            with self.subTest(template=template):
                options = {"swiss_rounds": 1} if template == "swiss" else {}
                config = create_tournament_definition(template, ids, 1, final_round=10, seed_rule="manual", seed_order=ids, **options)
                group = replace(group_for(template, *teams), kind="tournament", tournament=replace(config, pairing_error="Kampplanen kan ikke verificeres"))
                state = build_tournament_state(group, index, 10)
                self.assertIsNone(state.champion_id)
                self.assertEqual(state.active_team_ids, frozenset())
                self.assertEqual(state.eliminated_team_ids, frozenset())
                self.assertTrue(all(not m.complete and m.winner_id is None for m in (*state.group_matches, *state.knockout_matches)))
                self.assertTrue(all(row.played == 0 and row.points == 0 for row in state.standings))
                self.assertTrue(all(row.rank is None for row in state.standings))
                first = next(m for m in state.group_matches if m.fixture.team_b_id is not None) if state.group_matches else state.knockout_matches[0]
                a = first.fixture.team_a_id if state.group_matches else first.team_a_id
                b = first.fixture.team_b_id if state.group_matches else first.team_b_id
                summary = build_tournament_head_to_head(group, index, a, b, 10)
                self.assertEqual(summary.played, 0)
                self.assertTrue(all(match.blocked for match in summary.matches))
                events = holdet.build_live_hall_of_fame_events((group,), index, holdet.HubSettings())
                self.assertFalse(any(
                    event.complete for event in events
                    if event.kind in {"tournament", "tournament_group"}
                ))

    def test_double_elimination_counts_only_confirmed_losses_and_masks_future_sources(self):
        ids = (1, 2, 3, 4)
        teams = tuple(team_with_rounds(i, {r: 100-i for r in range(1, 6)}) for i in ids)
        index = snapshots(*teams)
        config = create_tournament_definition("double_elimination", ids, 1, final_round=5, seed_rule="manual", seed_order=ids)
        group = replace(group_for("double-history", *teams), kind="tournament", tournament=config)
        fixtures = published_knockout_candidates(group, index, 5)
        protected = replace(group, tournament=replace(config, published_matches=fixtures))
        state = build_tournament_state(protected, index, 2)
        self.assertEqual(state.eliminated_team_ids, frozenset({4}))
        self.assertEqual(state.active_team_ids, frozenset({1, 2, 3}))
        final = next(m for m in state.knockout_matches if m.match_id == "GF1")
        self.assertEqual((final.team_a_id, final.team_b_id), (1, None))

    def test_pairing_error_is_not_serialized_in_group_definition(self):
        from holdet_lib.groups import _tournament_to_dict
        config = create_tournament_definition("swiss", (1, 2), 1, swiss_rounds=1)
        payload = _tournament_to_dict(replace(config, pairing_error="data unavailable"))
        self.assertNotIn("pairing_error", payload)


if __name__ == "__main__":
    unittest.main()
