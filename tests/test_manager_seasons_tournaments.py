from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import unittest

import holdet_lib as holdet
from tests.test_library_storage import sample_team, temporary_directory


def team_with_rounds(
    team_id: int,
    changes: dict[int, int],
    *,
    owner_user_id: int | None = None,
):
    team = sample_team(
        team_id,
        current_round=max(changes),
        history_rounds=tuple(sorted(changes, reverse=True)),
    )
    history = tuple(
        replace(
            item,
            change=changes[item.round_number],
            total=1000 + changes[item.round_number],
            round_end_at=datetime(2026, 7, item.round_number, tzinfo=timezone.utc),
        )
        for item in team.history
    )
    return replace(
        team,
        owner_user_id=team.owner_user_id if owner_user_id is None else owner_user_id,
        history=history,
    )


def group_for(group_id, *teams):
    members = tuple(
        holdet.GroupTeam(
            item.reference.team_id,
            item.team_name,
            item.reference.source_url,
            item.reference.account_key,
            item.reference.account_label,
            item.reference.account_user_id,
            item.reference.profile_url,
        )
        for item in teams
    )
    return holdet.GroupDefinition(group_id, group_id, teams[0].reference.game, members)


def snapshots(*teams):
    return holdet.SnapshotIndex(
        tuple(
            holdet.TeamSnapshot(
                Path(f"{item.reference.team_id}.json"),
                datetime(2026, 8, 1, tzinfo=timezone.utc),
                item,
            )
            for item in teams
        )
    )


class ManagerIdentityAndRatingTests(unittest.TestCase):
    def test_schema_one_aliases_migrate_only_when_saved(self):
        with temporary_directory() as root:
            path = root / "hub-settings.json"
            payload = {
                "schema_version": 1,
                "watchlist": [],
                "manager_aliases": [
                    {
                        "canonical_id": "manager-one",
                        "display_name": "Manager One",
                        "identity_keys": ["owner:10"],
                    }
                ],
                "hall_of_fame_score": {
                    "group_points": [10, 6, 3, 1],
                    "tournament_winner": 10,
                    "tournament_finalist": 6,
                    "tournament_semifinalist": 3,
                    "global_round_win": 1,
                },
            }
            path.write_text(json.dumps(payload), encoding="utf-8")
            store = holdet.HubSettingsStore(path)
            settings = store.load()
            self.assertEqual(json.loads(path.read_text())["schema_version"], 1)
            profiles = holdet.hub_settings.effective_manager_profiles(settings)
            self.assertEqual(profiles[0].manager_id, "manager-one")
            store.set_manager_profiles(settings, profiles)
            saved = json.loads(path.read_text())
            self.assertEqual(saved["schema_version"], 2)
            self.assertEqual(saved["manager_aliases"], [])

    def test_best_team_and_overlap_are_deduplicated(self):
        first = team_with_rounds(1, {1: 20, 2: 40}, owner_user_id=900)
        second = team_with_rounds(2, {1: 50, 2: 10}, owner_user_id=900)
        opponent = team_with_rounds(3, {1: 30, 2: 30}, owner_user_id=901)
        groups = (
            group_for("a", first, second, opponent),
            group_for("b", first, opponent),
        )
        index = snapshots(first, second, opponent)
        results = holdet.build_manager_round_results(groups, index, holdet.HubSettings())
        manager_rows = [item for item in results if item.manager_id == "owner:900"]
        self.assertEqual([item.team_id for item in manager_rows], [2, 1])
        ratings = holdet.build_manager_ratings(groups, index, holdet.HubSettings())
        by_id = {item.manager_id: item for item in ratings}
        self.assertEqual(by_id["owner:900"].periods, 2)
        self.assertEqual(
            by_id["owner:900"].wins + by_id["owner:900"].draws + by_id["owner:900"].losses,
            2,
        )

    def test_tied_period_keeps_rating(self):
        first = team_with_rounds(1, {1: 10}, owner_user_id=900)
        second = team_with_rounds(2, {1: 10}, owner_user_id=901)
        ratings = holdet.build_manager_ratings(
            (group_for("a", first, second),),
            snapshots(first, second),
            holdet.HubSettings(),
        )
        self.assertEqual({item.rating for item in ratings}, {1500.0})
        self.assertTrue(all(item.provisional for item in ratings))

    def test_names_never_link_unresolved_people_automatically(self):
        settings = holdet.HubSettings()
        first = holdet.resolve_manager_identity(
            settings,
            owner_user_id=None,
            account_user_id=None,
            account_key="",
            owner_name="Samme navn",
            fallback_key="da:game:team:1",
        )
        second = holdet.resolve_manager_identity(
            settings,
            owner_user_id=None,
            account_user_id=None,
            account_key="",
            owner_name="Samme navn",
            fallback_key="da:game:team:2",
        )
        self.assertNotEqual(first[0], second[0])

    def test_tied_awards_have_one_recipient_and_latest_revision_counts(self):
        placements = (
            holdet.HallOfFamePlacement("b", "B", 2, "B", 1, 100),
            holdet.HallOfFamePlacement("a", "A", 1, "A", 1, 100),
            holdet.HallOfFamePlacement("c", "C", 3, "C", 3, 50),
        )
        original = holdet.HallOfFameEvent(
            "event", "group", "da", "game", "group", "Group", 1, placements,
            True, datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        corrected = replace(
            original, revision=2, supersedes_revision=1, captured_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
        )
        careers = {item.manager_id: item for item in holdet.build_manager_careers((original, corrected))}
        self.assertEqual(careers["a"].gold, 1)
        self.assertEqual(careers["b"].silver, 1)
        self.assertEqual(careers["c"].bronze, 1)
        self.assertEqual(careers["c"].wooden_spoons, 1)

    def test_rebuild_appends_one_correction_revision_and_is_idempotent(self):
        original = holdet.HallOfFameEvent(
            "revision-event",
            "group",
            "da",
            "game",
            "group",
            "Group",
            1,
            (holdet.HallOfFamePlacement("m1", "One", 1, "One", 1, 10),),
            True,
            datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        corrected = replace(
            original,
            placements=(
                holdet.HallOfFamePlacement("m1", "One", 1, "One", 1, 20),
            ),
            captured_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
        )
        with temporary_directory() as root:
            store = holdet.HallOfFameStore(root)
            store.freeze_complete((original,))
            store.freeze_complete((corrected,))
            store.freeze_complete((corrected,))
            events, warnings = store.scan()
        self.assertFalse(warnings)
        self.assertEqual([item.revision for item in events], [1, 2])
        self.assertEqual(events[-1].supersedes_revision, 1)

    def test_h2h_counts_a_multi_round_knockout_tie_once(self):
        first = team_with_rounds(
            1, {1: 10, 2: 20, 3: 30, 4: 40}, owner_user_id=900
        )
        second = team_with_rounds(
            2, {1: 20, 2: 10, 3: 20, 4: 30}, owner_user_id=901
        )
        config = holdet.create_tournament_config(
            (1, 2),
            1,
            4,
            2,
            shuffle=lambda values: None,
        )
        group = replace(
            group_for("cup-h2h", first, second),
            kind="tournament",
            tournament=config,
        )
        h2h = holdet.build_manager_head_to_head(
            "owner:900",
            "owner:901",
            (group,),
            snapshots(first, second),
            holdet.HubSettings(),
        )
        knockout = [
            item
            for item in h2h.official
            if ":knockout:" in item.meeting_id
        ]
        self.assertEqual(len(knockout), 1)
        self.assertEqual(knockout[0].round_numbers, (3, 4))
        self.assertEqual(
            (knockout[0].manager_score, knockout[0].opponent_score),
            (70, 50),
        )
        self.assertEqual(h2h.summary("official"), (2, 0, 1))
        self.assertEqual(h2h.total_growth("official"), (100, 80))
        self.assertEqual(h2h.biggest_win("official"), knockout[0])
        self.assertEqual(
            h2h.closest_meeting("official").round_numbers,
            (1,),
        )

    def test_incomplete_round_story_is_preliminary(self):
        first = team_with_rounds(1, {1: 20}, owner_user_id=900)
        first = replace(
            first,
            history=tuple(replace(item, round_status="in_progress") for item in first.history),
        )
        second = team_with_rounds(2, {1: 10}, owner_user_id=901)
        group = group_for("preview", first, second)
        story = holdet.build_round_story(
            (group,), snapshots(first, second), holdet.HubSettings(), group.game.slug, 1,
        )
        self.assertTrue(story.preliminary)


class SeasonAndTournamentTests(unittest.TestCase):
    def test_season_recalculates_with_score_profile(self):
        placement = holdet.HallOfFamePlacement("m1", "One", 1, "One", 1, 100)
        event = holdet.HallOfFameEvent(
            "event",
            "group",
            "da",
            "game",
            "group-a",
            "Group A",
            1,
            (placement,),
            True,
            datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        season = holdet.SeasonDefinition("s1", "Season", ("group-a",))
        low = holdet.build_season_standings(
            season,
            (event,),
            holdet.HallOfFameScoreProfile(group_points=(1, 0, 0, 0)),
        )
        high = holdet.build_season_standings(
            season,
            (event,),
            holdet.HallOfFameScoreProfile(group_points=(20, 0, 0, 0)),
        )
        self.assertEqual(low[0].points, 1)
        self.assertEqual(high[0].points, 20)

    def test_season_includes_round_wins_from_selected_games(self):
        placement = holdet.HallOfFamePlacement("m1", "One", 1, "One", 1, 100)
        competition = holdet.HallOfFameEvent(
            "competition",
            "group",
            "da",
            "game",
            "group-a",
            "Group A",
            1,
            (placement,),
            True,
            datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        round_win = replace(
            competition,
            event_id="round",
            kind="round_win",
            competition_id="round:1",
        )
        standing = holdet.build_season_standings(
            holdet.SeasonDefinition("s1", "Season", ("group-a",)),
            (competition, round_win),
            holdet.HallOfFameScoreProfile(
                group_points=(1, 0, 0, 0),
                global_round_win=5,
            ),
        )
        self.assertEqual(standing[0].points, 6)
        self.assertEqual(standing[0].round_wins, 1)

    def test_swiss_avoids_rematches_and_assigns_fair_bye(self):
        participants = (
            holdet.SwissParticipant(1, 3, opponent_ids=(2,), entry_seed=1),
            holdet.SwissParticipant(2, 3, opponent_ids=(1,), entry_seed=2),
            holdet.SwissParticipant(3, 0, had_bye=True, entry_seed=3),
            holdet.SwissParticipant(4, 0, entry_seed=4),
            holdet.SwissParticipant(5, 0, entry_seed=5),
        )
        pairings = holdet.generate_swiss_pairings(participants, 2)
        pairs = {
            frozenset((item.team_a_id, item.team_b_id))
            for item in pairings
            if item.team_b_id is not None
        }
        self.assertNotIn(frozenset((1, 2)), pairs)
        bye = next(item.team_a_id for item in pairings if item.team_b_id is None)
        self.assertNotEqual(bye, 3)

    def test_all_templates_freeze_seed_and_valid_schedules(self):
        ids = (1, 2, 3, 4, 5)
        for template in ("league", "swiss", "group_knockout", "double_elimination"):
            config = holdet.create_tournament_definition(
                template,
                ids,
                1,
                final_round=16,
                draw_seed="fixed-seed",
            )
            self.assertEqual(config.template, template)
            self.assertEqual(set(config.seed_order), set(ids))
            projection = holdet.tournament_template_config(config, ids)
            expected_projection = {
                "league": holdet.LeagueTemplateConfig,
                "swiss": holdet.SwissTemplateConfig,
                "group_knockout": holdet.GroupKnockoutTemplateConfig,
                "double_elimination": holdet.DoubleEliminationTemplateConfig,
            }[template]
            self.assertIsInstance(projection, expected_projection)
            for round_number in {item.round_number for item in config.group_fixtures}:
                participants = [
                    team_id
                    for item in config.group_fixtures
                    if item.round_number == round_number
                    for team_id in (item.team_a_id, item.team_b_id)
                    if team_id is not None
                ]
                self.assertEqual(len(participants), len(set(participants)))
        bracket = holdet.build_double_elimination_bracket(5)
        self.assertTrue(bracket[-1].reset_final)
        self.assertEqual(len(bracket), 15)
        published: set[str] = set()
        for match in bracket:
            for source in (match.source_a, match.source_b):
                if source is None:
                    continue
                _, source_match = source.split(":", 1)
                self.assertIn(source_match, published)
            published.add(match.match_id)
        losers_rounds = [item.bracket_round for item in bracket if item.bracket == "losers"]
        self.assertEqual(losers_rounds, sorted(losers_rounds))

    def test_multi_group_schedule_is_balanced_and_cross_seeded(self):
        ids = tuple(range(1, 9))
        config = holdet.create_tournament_definition(
            "group_knockout",
            ids,
            1,
            final_round=5,
            seed_rule="manual",
            seed_order=ids,
            group_count=2,
            qualifiers_per_group=2,
            bronze_match=True,
        )
        group_sizes = {
            group_index: len({
                team_id
                for item in config.group_fixtures
                if item.group_index == group_index
                for team_id in (item.team_a_id, item.team_b_id)
                if team_id is not None
            })
            for group_index in range(2)
        }
        self.assertEqual(group_sizes, {0: 4, 1: 4})
        for round_number in range(1, 4):
            participants = [
                team_id
                for item in config.group_fixtures
                if item.round_number == round_number
                for team_id in (item.team_a_id, item.team_b_id)
                if team_id is not None
            ]
            self.assertEqual(set(participants), set(ids))

        teams = tuple(
            team_with_rounds(
                team_id,
                {round_number: 100 - team_id for round_number in range(1, 6)},
            )
            for team_id in ids
        )
        group = replace(
            group_for("multi", *teams),
            kind="tournament",
            tournament=config,
        )
        state = holdet.build_tournament_state(group, snapshots(*teams), 5)
        semifinals = tuple(item for item in state.knockout_matches if item.stage == "Semifinaler")
        self.assertEqual(len(semifinals), 2)
        self.assertTrue(all(item.team_a_id != item.team_b_id for item in semifinals))
        bronze = next(item for item in state.knockout_matches if item.stage == "Bronzekamp")
        self.assertTrue(bronze.complete)

    def test_double_elimination_requires_second_loss_and_reset_final(self):
        changes = {
            1: {1: 40, 2: 40, 3: 10, 4: 30, 5: 50},
            2: {1: 30, 2: 30, 3: 30, 4: 10, 5: 10},
            3: {1: 20, 2: 30, 3: 40, 4: 40, 5: 40},
            4: {1: 10, 2: 10, 3: 10, 4: 10, 5: 10},
        }
        teams = tuple(
            team_with_rounds(team_id, round_changes)
            for team_id, round_changes in changes.items()
        )
        config = holdet.create_tournament_definition(
            "double_elimination",
            (1, 2, 3, 4),
            1,
            final_round=5,
            seed_rule="manual",
            seed_order=(1, 2, 3, 4),
        )
        group = replace(
            group_for("double", *teams),
            kind="tournament",
            tournament=config,
        )

        state = holdet.build_tournament_state(group, snapshots(*teams), 5)

        self.assertEqual(config.final_round, 5)
        self.assertEqual(state.champion_id, 1)
        self.assertEqual(state.phase, "Afsluttet")
        self.assertEqual(
            [item.team_id for item in state.standings[:3]],
            [1, 3, 2],
        )
        finals = [item for item in state.knockout_matches if item.stage == "Finale"]
        self.assertEqual(len(finals), 2)
        self.assertEqual([item.winner_id for item in finals], [3, 1])
        self.assertEqual(state.eliminated_team_ids, frozenset({2, 3, 4}))
        self.assertEqual(state.active_team_ids, frozenset())
        calendar = holdet.build_calendar_events((group,), ())
        self.assertEqual(len(calendar), 7)
        self.assertTrue(any("reset-finale" in item.title for item in calendar))
        event = holdet.build_live_hall_of_fame_events(
            (group,), snapshots(*teams), holdet.HubSettings()
        )[0]
        self.assertEqual(
            [item.team_id for item in event.placements],
            [1, 3, 2],
        )
        head_to_head = holdet.build_manager_head_to_head(
            "owner:1001", "owner:1003",
            (group,), snapshots(*teams), holdet.HubSettings(),
        )
        self.assertEqual(len(head_to_head.official), 2)
        self.assertTrue(all(item.round_numbers in {(4,), (5,)} for item in head_to_head.official))

    def test_rebuild_preserves_template_and_format_changes_create_revisions(self):
        teams = tuple(
            team_with_rounds(team_id, {1: 10}) for team_id in range(1, 6)
        )
        all_members = group_for("members", *teams).teams
        with temporary_directory() as root:
            store = holdet.GroupStore(root / "groups.json")
            created = store.create_tournament(
                "Double",
                teams[0].reference.game,
                all_members[:4],
                start_round=1,
                final_round=5,
                rounds_per_tie=1,
                group_id="revision-double",
                template="double_elimination",
                definition_options={
                    "seed_rule": "manual",
                    "seed_order": (1, 2, 3, 4),
                },
            )
            revised = store.rebuild_tournament(
                created.group_id, all_members, final_round=7
            )
            self.assertEqual(revised.tournament.template, "double_elimination")
            self.assertEqual(revised.active_revision, 2)
            self.assertEqual(revised.archived_revisions[0].tournament.template, "double_elimination")

            league = store.rebuild_tournament(
                created.group_id,
                all_members,
                final_round=5,
                template="league",
                definition_options={"league_legs": 1},
            )
            self.assertEqual(league.tournament.template, "league")
            self.assertEqual(league.active_revision, 3)
            self.assertEqual(len(league.archived_revisions), 2)
    def test_official_group_url_is_strict(self):
        url, kind = holdet.validate_official_group_url(
            "https://www.holdet.dk/da/test/group/1",
            "da",
            "group",
        )
        self.assertEqual(kind, "group")
        self.assertTrue(url.startswith("https://"))
        with self.assertRaises(holdet.PayloadError):
            holdet.validate_official_group_url(
                "https://user@www.holdet.dk/da/test",
                "da",
                "group",
            )
    def test_calendar_keeps_missing_fixture_times_separate(self):
        first = team_with_rounds(1, {1: 10}, owner_user_id=900)
        second = team_with_rounds(2, {1: 20}, owner_user_id=901)
        group = replace(
            group_for("calendar", first, second),
            tournament=holdet.create_tournament_definition(
                "league", (1, 2), 1, final_round=2,
            ),
        )
        events = holdet.build_calendar_events((group,), ())
        self.assertTrue(events)
        self.assertTrue(all(item.missing_time for item in events))

    def test_published_swiss_pairings_are_frozen_per_revision(self):
        with temporary_directory() as root:
            store = holdet.TournamentPairingStore(root)
            first = (
                holdet.TournamentPairing(1, 1, 2),
                holdet.TournamentPairing(1, 3, None),
            )
            published = store.publish_round("swiss", 2, 1, first)
            self.assertEqual(published.published_rounds, (1,))
            self.assertEqual(
                store.publish_round("swiss", 2, 1, first),
                published,
            )
            with self.assertRaises(holdet.PayloadError):
                store.publish_round(
                    "swiss",
                    2,
                    1,
                    (holdet.TournamentPairing(1, 1, 3),),
                )
            with self.assertRaises(holdet.PayloadError):
                store.publish_round(
                    "swiss",
                    2,
                    2,
                    (holdet.TournamentPairing(2, 1, 3),),
                    previous_round_complete=False,
                )





class AuditRegressionTests(unittest.TestCase):
    def test_swiss_requires_every_configured_round_before_completion(self):
        teams = tuple(
            team_with_rounds(team_id, {1: 30 - team_id, 2: 40 - team_id})
            for team_id in range(1, 5)
        )
        config = holdet.create_tournament_definition(
            "swiss",
            tuple(range(1, 5)),
            1,
            swiss_rounds=2,
            seed_rule="manual",
            seed_order=tuple(range(1, 5)),
        )
        group = replace(
            group_for("swiss-incomplete", *teams),
            kind="tournament",
            tournament=config,
        )

        state = holdet.build_tournament_state(group, snapshots(*teams), 2)

        self.assertIsNone(state.champion_id)
        self.assertNotEqual(state.phase, "Afsluttet")
        self.assertIn(
            "1 Swiss-runder mangler at blive publiceret",
            state.warnings,
        )

    def test_swiss_bye_scores_only_when_reached_and_uses_custom_points(self):
        teams = tuple(
            team_with_rounds(team_id, {1: 30 - team_id})
            for team_id in range(1, 4)
        )
        config = holdet.create_tournament_definition(
            "swiss",
            (1, 2, 3),
            1,
            swiss_rounds=1,
            match_points=(5, 2, 0),
            seed_rule="manual",
            seed_order=(1, 2, 3),
        )
        bye_id = next(
            item.team_a_id
            for item in config.group_fixtures
            if item.team_b_id is None
        )
        group = replace(
            group_for("swiss-bye", *teams),
            kind="tournament",
            tournament=config,
        )

        before = {
            item.team_id: item
            for item in holdet.build_tournament_state(
                group, snapshots(*teams), 0
            ).standings
        }
        reached = {
            item.team_id: item
            for item in holdet.build_tournament_state(
                group, snapshots(*teams), 1
            ).standings
        }

        self.assertEqual((before[bye_id].played, before[bye_id].points), (0, 0))
        self.assertEqual(
            (reached[bye_id].played, reached[bye_id].wins, reached[bye_id].points),
            (1, 1, 5),
        )

    def test_corrected_results_report_frozen_swiss_pairing_conflict(self):
        teams = tuple(
            team_with_rounds(team_id, {1: 50 - team_id, 2: 60 - team_id})
            for team_id in range(1, 5)
        )
        config = holdet.create_tournament_definition(
            "swiss",
            (1, 2, 3, 4),
            1,
            swiss_rounds=2,
            seed_rule="manual",
            seed_order=(1, 2, 3, 4),
        )
        base_group = replace(
            group_for("swiss-conflict", *teams),
            kind="tournament",
            tournament=config,
        )
        first_state = holdet.build_tournament_state(
            base_group, snapshots(*teams), 1
        )
        participants = holdet.build_swiss_participants(
            config, first_state.group_matches
        )
        expected = holdet.generate_swiss_pairings(participants, 2)
        expected_pairs = {
            frozenset((item.team_a_id, item.team_b_id))
            for item in expected
        }
        alternatives = (
            ((1, 2), (3, 4)),
            ((1, 3), (2, 4)),
            ((1, 4), (2, 3)),
        )
        wrong_pairs = next(
            candidate
            for candidate in alternatives
            if {frozenset(pair) for pair in candidate} != expected_pairs
        )
        wrong = tuple(
            holdet.GroupFixture(2, first, second)
            for first, second in wrong_pairs
        )
        corrected_group = replace(
            base_group,
            tournament=replace(
                config,
                group_fixtures=(*config.group_fixtures, *wrong),
            ),
        )

        conflicts = holdet.build_swiss_pairing_conflicts(
            corrected_group, snapshots(*teams)
        )

        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].round_number, 2)
        self.assertNotEqual(conflicts[0].published, conflicts[0].expected)

    def test_persisted_pairings_are_context_validated(self):
        config = holdet.create_tournament_definition(
            "swiss",
            (1, 2, 3, 4),
            1,
            swiss_rounds=2,
            seed_rule="manual",
            seed_order=(1, 2, 3, 4),
        )
        invalid_values = (
            (
                holdet.TournamentPairing(2, 1, 2),
                holdet.TournamentPairing(2, 1, 3),
            ),
            (
                holdet.TournamentPairing(2, 1, None),
                holdet.TournamentPairing(2, 2, None),
                holdet.TournamentPairing(2, 3, 4),
            ),
            (holdet.TournamentPairing(2, 1, 99),),
            (holdet.TournamentPairing(3, 1, 2),),
        )
        for pairings in invalid_values:
            with self.subTest(pairings=pairings):
                with self.assertRaises(holdet.PayloadError):
                    holdet.validate_tournament_pairing_revision(
                        holdet.TournamentPairingRevision(
                            "swiss", 1, pairings
                        ),
                        config,
                        (1, 2, 3, 4),
                    )

    def test_identity_graph_preserves_profile_id_and_never_uses_name(self):
        first = team_with_rounds(1, {1: 10}, owner_user_id=900)
        second = team_with_rounds(2, {1: 20}, owner_user_id=901)
        same_name = "Samme navn"
        first = replace(first, owner_name=same_name)
        second = replace(second, owner_name=same_name)
        group = group_for("identities", first, second)
        settings = holdet.HubSettings(
            manager_profiles=(
                holdet.ManagerProfile(
                    "stable-profile",
                    "Beholdt navn",
                    ("owner:900",),
                    manual_identity_keys=("owner:900",),
                ),
            )
        )

        effective = holdet.build_effective_manager_settings(
            settings, (group,), snapshots(first, second)
        )
        profiles = {
            item.manager_id: item
            for item in effective.manager_profiles
        }

        self.assertIn("stable-profile", profiles)
        self.assertIn("account-user:1001", profiles["stable-profile"].identity_keys)
        self.assertNotEqual(
            holdet.resolve_manager_identity(
                effective,
                owner_user_id=900,
                account_user_id=1001,
                account_key="account-1",
                owner_name=same_name,
            )[0],
            holdet.resolve_manager_identity(
                effective,
                owner_user_id=901,
                account_user_id=1002,
                account_key="account-2",
                owner_name=same_name,
            )[0],
        )

    def test_legacy_event_remaps_without_rewriting_ledger(self):
        event = holdet.HallOfFameEvent(
            "legacy",
            "group",
            "da",
            "game",
            "group",
            "Group",
            1,
            (
                holdet.HallOfFamePlacement(
                    "owner:900", "Gammelt navn", 1, "Team", 1, 10
                ),
            ),
            True,
            datetime(2026, 8, 1, tzinfo=timezone.utc),
        )
        settings = holdet.HubSettings(
            manager_profiles=(
                holdet.ManagerProfile(
                    "stable-profile",
                    "Nyt navn",
                    ("owner:900",),
                ),
            )
        )
        with temporary_directory() as root:
            store = holdet.HallOfFameStore(root)
            path = store.freeze(event)
            before = path.read_bytes()
            loaded, warnings = store.scan()
            remapped = holdet.remap_manager_events(loaded, settings)
            after = path.read_bytes()

        self.assertFalse(warnings)
        self.assertEqual(before, after)
        self.assertEqual(remapped[0].placements[0].manager_id, "stable-profile")
        self.assertEqual(remapped[0].placements[0].manager_name, "Nyt navn")

    def test_identical_slugs_are_isolated_by_locale(self):
        da_first = team_with_rounds(1, {1: 20}, owner_user_id=900)
        da_second = team_with_rounds(2, {1: 10}, owner_user_id=901)
        en_game = replace(
            da_first.reference.game,
            original="https://www.holdet.dk/en/fantasy/"
            + da_first.reference.game.slug,
            locale="en",
        )
        en_first = replace(
            da_first,
            reference=replace(da_first.reference, game=en_game),
            history=tuple(replace(item, change=5) for item in da_first.history),
        )
        en_second = replace(
            da_second,
            reference=replace(da_second.reference, game=en_game),
            history=tuple(replace(item, change=30) for item in da_second.history),
        )
        groups = (
            group_for("da-group", da_first, da_second),
            group_for("en-group", en_first, en_second),
        )
        index = snapshots(da_first, da_second, en_first, en_second)

        h2h = holdet.build_manager_head_to_head(
            "owner:900",
            "owner:901",
            groups,
            index,
            holdet.HubSettings(),
        )
        ratings = holdet.build_manager_ratings(
            groups, index, holdet.HubSettings()
        )
        stories = (
            holdet.build_round_story(
                groups,
                index,
                holdet.HubSettings(),
                da_first.reference.game.slug,
                1,
                game_locale=locale,
            )
            for locale in ("da", "en")
        )

        self.assertEqual(
            {item.game_locale for item in h2h.shared_rounds},
            {"da", "en"},
        )
        self.assertEqual(
            {item.periods for item in ratings},
            {2},
        )
        self.assertEqual(
            {story.game_locale for story in stories},
            {"da", "en"},
        )

    def test_missing_entire_group_keeps_round_story_preliminary(self):
        complete = (
            team_with_rounds(1, {1: 20}, owner_user_id=900),
            team_with_rounds(2, {1: 10}, owner_user_id=901),
        )
        missing = (
            team_with_rounds(3, {1: 30}, owner_user_id=902),
            team_with_rounds(4, {1: 40}, owner_user_id=903),
        )
        groups = (
            group_for("complete", *complete),
            group_for("missing", *missing),
        )
        story = holdet.build_round_story(
            groups,
            snapshots(*complete),
            holdet.HubSettings(),
            complete[0].reference.game.slug,
            1,
            game_locale="da",
        )

        self.assertTrue(story.preliminary)
        self.assertTrue(all(item.preliminary for item in story.awards))

    def test_multi_group_elo_only_compares_managers_in_same_pool(self):
        teams = tuple(
            team_with_rounds(
                team_id,
                {1: 100 - team_id},
                owner_user_id=900 + team_id,
            )
            for team_id in range(1, 9)
        )
        config = holdet.create_tournament_definition(
            "group_knockout",
            tuple(range(1, 9)),
            1,
            final_round=5,
            seed_rule="manual",
            seed_order=tuple(range(1, 9)),
            group_count=2,
            qualifiers_per_group=2,
        )
        group = replace(
            group_for("elo-pools", *teams),
            kind="tournament",
            tournament=config,
        )
        index = snapshots(*teams)

        ratings = {
            item.manager_id: item
            for item in holdet.build_manager_ratings(
                (group,), index, holdet.HubSettings()
            )
        }
        across_pools = holdet.build_manager_head_to_head(
            "owner:901",
            "owner:902",
            (group,),
            index,
            holdet.HubSettings(),
        )

        self.assertEqual(
            ratings["owner:901"].wins
            + ratings["owner:901"].draws
            + ratings["owner:901"].losses,
            3,
        )
        self.assertEqual(across_pools.shared_rounds, ())

    def test_round_story_rejects_ambiguous_locale_without_locale_key(self):
        first = team_with_rounds(1, {1: 20}, owner_user_id=900)
        second = team_with_rounds(2, {1: 10}, owner_user_id=901)
        en_game = replace(
            first.reference.game,
            original="https://www.holdet.dk/en/fantasy/"
            + first.reference.game.slug,
            locale="en",
        )
        en_first = replace(
            first,
            reference=replace(first.reference, game=en_game),
        )
        en_second = replace(
            second,
            reference=replace(second.reference, game=en_game),
        )
        groups = (
            group_for("story-da", first, second),
            group_for("story-en", en_first, en_second),
        )

        with self.assertRaisesRegex(
            holdet.PayloadError,
            "game_locale skal angives",
        ):
            holdet.build_round_story(
                groups,
                snapshots(first, second, en_first, en_second),
                holdet.HubSettings(),
                first.reference.game.slug,
                1,
            )

    def test_season_update_preserves_id_and_archive_state(self):
        with temporary_directory() as root:
            store = holdet.SeasonStore(root / "seasons.json")
            original = holdet.SeasonDefinition(
                "season-id",
                "F\u00f8r",
                ("group-a",),
                "2026-08-01T00:00:00+00:00",
            )

            updated = store.update(
                (original,),
                "season-id",
                name="Efter",
                competition_ids=("group-b", "group-b"),
            )

            self.assertEqual(updated[0].season_id, "season-id")
            self.assertEqual(updated[0].archived_at, original.archived_at)
            self.assertEqual(updated[0].competition_ids, ("group-b",))
            self.assertEqual(store.load(), updated)

    def test_bad_archived_revision_does_not_hide_active_group(self):
        teams = tuple(
            team_with_rounds(team_id, {1: 10})
            for team_id in range(1, 5)
        )
        with temporary_directory() as root:
            store = holdet.GroupStore(root / "groups.json")
            created = store.create_tournament(
                "Cup",
                teams[0].reference.game,
                group_for("members", *teams).teams,
                start_round=1,
                final_round=3,
                rounds_per_tie=1,
                group_id="revision-isolation",
            )
            store.rebuild_tournament(
                created.group_id,
                group_for("members", *teams).teams,
                final_round=4,
            )
            revision_path = (
                root
                / "group-revisions"
                / created.group_id
                / "revision-1.json"
            )
            revision_path.write_text("{", encoding="utf-8")

            with self.assertRaises(holdet.PayloadError):
                store.load_configuration()
            configuration, warnings = (
                store.load_configuration_with_warnings()
            )

        self.assertEqual(
            [item.group_id for item in configuration.groups],
            ["revision-isolation"],
        )
        self.assertTrue(warnings)
        self.assertEqual(
            configuration.groups[0].archived_revisions,
            (),
        )


class DanishValidationCopyTests(unittest.TestCase):
    def test_season_store_reports_invalid_json_in_danish(self):
        with temporary_directory() as root:
            path = root / "seasons.json"
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(
                holdet.PayloadError,
                "Sæsonlageret indeholder ugyldig JSON",
            ):
                holdet.SeasonStore(path).load()

    def test_manager_profiles_report_duplicate_identity_in_danish(self):
        with temporary_directory() as root:
            profiles = (
                holdet.ManagerProfile("manager-a", "Manager A", ("owner:1",)),
                holdet.ManagerProfile("manager-b", "Manager B", ("owner:1",)),
            )
            with self.assertRaisesRegex(
                holdet.PayloadError,
                "En manageridentitet kan kun tilhøre én profil",
            ):
                holdet.HubSettingsStore(root / "hub-settings.json").set_manager_profiles(
                    holdet.HubSettings(),
                    profiles,
                )

    def test_official_group_url_reports_credentials_in_danish(self):
        with self.assertRaisesRegex(
            holdet.PayloadError,
            "må ikke indeholde loginoplysninger",
        ):
            holdet.validate_official_group_url(
                "https://user@www.holdet.dk/da/test",
                "da",
                "group",
            )

    def test_pairing_store_reports_invalid_json_in_danish(self):
        with temporary_directory() as root:
            path = root / "group-a" / "revision-1.json"
            path.parent.mkdir(parents=True)
            path.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(
                holdet.PayloadError,
                "Turneringsparringerne indeholder ugyldig JSON",
            ):
                holdet.TournamentPairingStore(root).load("group-a", 1)

    def test_swiss_publication_error_is_danish(self):
        with temporary_directory() as root:
            store = holdet.TournamentPairingStore(root)
            with self.assertRaisesRegex(
                holdet.PayloadError,
                "Den forrige Swiss-runde skal være komplet før publicering",
            ):
                store.publish_round(
                    "swiss",
                    1,
                    2,
                    (holdet.TournamentPairing(2, 1, 2),),
                    previous_round_complete=False,
                )

if __name__ == "__main__":
    unittest.main()
