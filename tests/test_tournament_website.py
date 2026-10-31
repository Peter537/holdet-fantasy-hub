from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
from unittest.mock import patch
from types import SimpleNamespace
import unittest

from streamlit.testing.v1 import AppTest

import holdet_lib as holdet
from tests.test_library_storage import sample_team
from tests.test_player_statistics import sample_statistics
from tests.test_tournament import team_with_changes
from tests.test_website import (
    APP_PATH, button, navigate, select_game_section, website_environment, widget,
)


class TournamentDashboardTests(unittest.TestCase):
    def test_create_historical_tournament_and_render_four_views(self):
        with website_environment() as (config, output):
            first = sample_team(1, name="Alpha", current_round=3, history_rounds=(3, 2, 1))
            second = sample_team(2, name="Beta", current_round=3, history_rounds=(3, 2, 1))
            snapshots = holdet.SnapshotStore(output)
            fixed = datetime(2026, 7, 26, tzinfo=timezone.utc)
            snapshots.save_team_json(first, now=fixed)
            snapshots.save_team_json(second, now=fixed)
            holdet.GroupStore(config / "groups.json").create_manager_game(
                first.reference.game, "Tourspillet 2026"
            )

            from website import app as dashboard
            with patch("holdet_lib.HoldetClient") as client_type:
                client_type.return_value.fetch_team.side_effect = AssertionError(
                    "ordinary navigation must not contact Holdet"
                )
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                navigate(app, "game", locale=first.reference.game.locale, game=first.reference.game.slug)
                select_game_section(app, first.reference.game, "administration")
                widget(app, "segmented_control", "Gruppetype").set_value(
                    "Turnering"
                )
                select_game_section(app, first.reference.game, "administration")
                choices = widget(app, "multiselect", "Fundne hold")
                choices.set_value(list(choices.options))
                select_game_section(app, first.reference.game, "administration")
                client_type.return_value.fetch_game_info.return_value.final_round = 4
                button(app, "Hent spilinfo").click()
                select_game_section(app, first.reference.game, "administration")
                widget(app, "text_input", "Gruppenavn").input("Sommercup")
                self.assertTrue(any(item.label == "Ny lodtrækning" for item in app.button))
                self.assertTrue(app.code)
                button(app, "Opret gruppe").click()
                select_game_section(app, first.reference.game, "administration")

            group = holdet.GroupStore(config / "groups.json").load()[0]
            self.assertEqual(group.kind, "tournament")
            self.assertEqual(group.tournament.start_round, 1)
            self.assertEqual(group.tournament.final_round, 4)
            self.assertEqual(group.tournament.knockout_size, 2)
            self.assertEqual(group.tournament.group_end_round, 3)
            self.assertIsNotNone(group.tournament.draw_seed)

            app = AppTest.from_file(APP_PATH).run(timeout=15)
            navigate(app, "game", locale=first.reference.game.locale, game=first.reference.game.slug)
            navigate(app, "group", group=group.group_id)
            self.assertFalse(app.exception)
            self.assertTrue(any(item.value == group.name for item in app.title))
            self.assertEqual(
                [item.label for item in app.tabs],
                ["Overblik", "Gruppestilling", "Kampe", "Knockout", "Historik"],
            )
            self.assertEqual(
                widget(app, "selectbox", "Vis turneringen til og med runde").value,
                3,
            )
            self.assertTrue(any(item.value == "Gruppespil" for item in app.metric))
            app.session_state[f"tournament-tabs-{group.group_id}"] = "Gruppestilling"
            app.run(timeout=15)
            standings = app.dataframe[0].value
            self.assertEqual(
                list(standings.columns),
                [
                    "Pulje", "Plac.", "Manager", "Hold", "K", "V", "U", "T",
                    "For", "Imod", "Forskel", "Point", "Hold-ID",
                ],
            )

    def test_historical_selector_limits_chart_without_hiding_later_cached_data(self):
        with website_environment() as (config, output):
            teams = tuple(sample_team(number, name=f"Hold {number}", current_round=3,
                                      history_rounds=(1, 3)) for number in (1, 2))
            snapshots = holdet.SnapshotStore(output)
            for team in teams:
                snapshots.save_team_json(team)
            members = tuple(holdet.GroupTeam(team.reference.team_id, team.team_name,
                                            team.reference.source_url) for team in teams)
            group = holdet.GroupStore(config / "groups.json").create_tournament(
                "Historisk cup", teams[0].reference.game, members,
                start_round=1, final_round=4, rounds_per_tie=1,
                group_id="history-cutoff", shuffle=lambda values: None,
            )
            before = {path.relative_to(config.parent): path.read_bytes()
                      for path in config.parent.rglob("*") if path.is_file()}
            with (
                patch("holdet_lib.http.HttpClient.fetch_text", side_effect=AssertionError("network")) as fetch,
                patch("website.hub_pages.st.altair_chart") as render_chart,
            ):
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                navigate(app, "group", group=group.group_id, section="history")
                self.assertFalse(app.exception)
                self.assertEqual(render_chart.call_args.args[0].data["Runde"].max(), 3)
                render_chart.reset_mock()
                widget(app, "selectbox", "Vis turneringen til og med runde").set_value(1).run(timeout=15)
                self.assertFalse(app.exception)
                render_chart.assert_called_once()
                self.assertEqual(set(render_chart.call_args.args[0].data["Runde"]), {1})
                render_chart.reset_mock()
                widget(app, "selectbox", "Vis turneringen til og med runde").set_value(3).run(timeout=15)
                self.assertFalse(app.exception)
                history = render_chart.call_args.args[0].data
                self.assertEqual(set(history["Runde"]), {1, 2, 3})
                self.assertTrue(history.loc[history["Runde"] == 2, "Værdi/point"].isna().all())
                fetch.assert_not_called()
            after = {path.relative_to(config.parent): path.read_bytes()
                     for path in config.parent.rglob("*") if path.is_file()}
            self.assertEqual(after, before)

    def test_ended_schedule_keeps_unfinished_capture_in_collapsed_diagnostics(self):
        with website_environment() as (config, output):
            teams = tuple(sample_team(number, name=f"Hold {number}", current_round=3,
                                      history_rounds=(1, 2, 3)) for number in (1, 2))
            teams = tuple(replace(team, history=tuple(
                replace(item, round_status="in_progress" if item.round_number == 3 else "complete")
                for item in team.history)) for team in teams)
            snapshots = holdet.SnapshotStore(output)
            for team in teams:
                snapshots.save_team_json(team)
            game = teams[0].reference.game
            members = tuple(holdet.GroupTeam(team.reference.team_id, team.team_name,
                                            team.reference.source_url) for team in teams)
            store = holdet.GroupStore(config / "groups.json")
            group = store.create_tournament("Afventer cup", game, members,
                                           start_round=1, final_round=4, rounds_per_tie=1,
                                           group_id="ended-round", shuffle=lambda values: None)
            now = datetime.now(timezone.utc)
            holdet.GameMetadataStore(output.parent / "game-metadata").save(
                holdet.GameContext(game, "cycling", "classic", 7, None, None,
                                   50_000_000, 4, "Testspil", (
                                       holdet.ScheduleRound(3, now - timedelta(days=3),
                                                            now - timedelta(days=2), now - timedelta(days=1)),
                                       holdet.ScheduleRound(4, now + timedelta(days=1),
                                                            now + timedelta(days=2), now + timedelta(days=3)),
                                   )), fetched_at=now)
            before = {path.relative_to(config.parent): path.read_bytes()
                      for path in config.parent.rglob("*") if path.is_file()}
            with patch("holdet_lib.http.HttpClient.fetch_text", side_effect=AssertionError("network")) as fetch:
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                navigate(app, "group", group=group.group_id)
                self.assertFalse(app.exception)
                self.assertTrue(any(item.value == "Afventer resultat" for item in app.header))
                diagnostics = next(item for item in app.expander if item.label == "Datastatus")
                self.assertFalse(diagnostics.proto.expanded)
                status = diagnostics.table[0].value
                self.assertEqual(status["Afventer afsluttende data"].tolist(), ["3", "3"])
                self.assertEqual(status["Foreløbige data"].tolist(), ["–", "–"])
                self.assertTrue(diagnostics.warning)
                self.assertFalse(app.success)
                fetch.assert_not_called()
            after = {path.relative_to(config.parent): path.read_bytes()
                     for path in config.parent.rglob("*") if path.is_file()}
            self.assertEqual(after, before)

    def test_direct_refresh_reaches_final_round_and_exposes_h2h(self):
        with website_environment() as (config, output):
            old_teams = tuple(
                team_with_changes(
                    team_id,
                    {
                        1: 110 - team_id * 10,
                        2: 110 - team_id * 10,
                        3: 110 - team_id * 10,
                        4: {1: 5, 2: 0, 3: 10, 4: 1}[team_id],
                    },
                    name=f"Hold {team_id}",
                )
                for team_id in range(1, 5)
            )
            refreshed = tuple(
                team_with_changes(
                    team_id,
                    {
                        1: 110 - team_id * 10,
                        2: 110 - team_id * 10,
                        3: 110 - team_id * 10,
                        4: {1: 5, 2: 0, 3: 10, 4: 1}[team_id],
                        5: {1: 1, 2: 0, 3: 20, 4: 0}[team_id],
                    },
                    name=f"Hold {team_id}",
                )
                for team_id in range(1, 5)
            )
            snapshots = holdet.SnapshotStore(output)
            for team in old_teams:
                snapshots.save_team_json(team)
            members = tuple(
                holdet.GroupTeam(
                    team.reference.team_id, team.team_name, team.reference.source_url
                )
                for team in old_teams
            )
            group = holdet.GroupStore(config / "groups.json").create_tournament(
                "Finalecup", old_teams[0].reference.game, members,
                start_round=1, final_round=5, rounds_per_tie=1,
                group_id="finalecup", shuffle=lambda values: None,
            )
            fetched: list[int] = []
            current = datetime.now(timezone.utc)

            class FakeClient:
                def fetch_game_info(self, game):
                    return holdet.GameContext(
                        game, "cycling", "classic", 7, None, None, 50_000_000, 5,
                        "Cupspil",
                        tuple(holdet.ScheduleRound(
                            number,
                            current - timedelta(days=8 - number),
                            current - timedelta(days=7 - number),
                            current - timedelta(days=6 - number),
                        ) for number in range(1, 6)),
                    )

                def fetch_players(self, game, *, round_number=None):
                    return replace(sample_statistics(round_number or 5), round_status="complete")

                def fetch_team(self, reference):
                    fetched.append(reference.team_id)
                    return next(
                        team for team in refreshed
                        if team.reference.team_id == reference.team_id
                    )

            class ImmediateExecutor:
                def __init__(self, *, max_workers):
                    assert max_workers == 1

                def submit(self, action, *args):
                    action(*args)

                def shutdown(self, *, wait):
                    pass

            from website.refresh_service import create_refresh_coordinator
            paths = holdet.resolve_paths(overrides=holdet.PathOverrides(data_root=config.parent), environ={})
            coordinator = create_refresh_coordinator(paths, client_factory=FakeClient)
            coordinator.executor_factory = ImmediateExecutor
            with (
                patch("website.workflows._coordinator", return_value=coordinator),
                patch("holdet_lib.HoldetClient", side_effect=AssertionError("network")),
            ):
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                navigate(app, "game", locale=old_teams[0].reference.game.locale, game=old_teams[0].reference.game.slug)
                navigate(app, "group", group=group.group_id)
                self.assertEqual(
                    widget(app, "selectbox", "Vis turneringen til og med runde").value,
                    4,
                )
                button(app, "Opdater data").click().run(timeout=30)
                run = coordinator.snapshot(app.session_state["visible-refresh-run"])
                manifest = holdet.ManifestStore(paths.manifest_dir).load(
                    paths.data_dir / run["games"][0]["manifest_paths"][-1]
                )
                self.assertEqual(run["status"], "success", manifest)
                self.assertEqual(manifest.groups[0]["phase"], "Afsluttet")
                self.assertIsNotNone(manifest.groups[0]["champion_id"])
                app.run(timeout=15)
                selector = widget(app, "selectbox", "Vis turneringen til og med runde")
                self.assertIn("5", selector.options)
                selector.set_value(5).run(timeout=15)

            self.assertEqual(set(fetched), {1, 2, 3, 4})
            self.assertEqual(
                widget(app, "selectbox", "Vis turneringen til og med runde").value,
                5,
            )
            self.assertTrue(
                any("har vundet turneringen" in item.value for item in app.success),
                [item.value for item in app.success],
            )
            tab_key = f"tournament-tabs-{group.group_id}"
            app.session_state[tab_key] = "Kampe"
            app.run(timeout=15)
            self.assertEqual(app.session_state[tab_key], "Kampe")
            self.assertEqual(widget(app, "selectbox", "Hold A").value, 1)
            self.assertTrue(
                any(item.value == "Indbyrdes sammenligning" for item in (*app.header, *app.subheader))
            )
            self.assertTrue(any(item.label == "Hold B" for item in app.selectbox))
            manifest = holdet.ManifestStore(paths.manifest_dir).load_latest(
                group.game.slug, game_locale=group.game.locale,
                scope="group", group_id=group.group_id,
            )
            self.assertIsNotNone(manifest)
            self.assertEqual(manifest.schema_version, 3)
            self.assertEqual(manifest.round_number, 5)
            self.assertEqual(manifest.result, "complete")
            self.assertEqual(manifest.scope, "group")
            self.assertEqual(manifest.group_id, group.group_id)

    def test_archived_tournament_is_read_only_without_network_calls(self):
        with website_environment() as (config, output):
            teams = (sample_team(1), sample_team(2))
            members = tuple(
                holdet.GroupTeam(
                    team.reference.team_id, team.team_name, team.reference.source_url
                )
                for team in teams
            )
            store = holdet.GroupStore(config / "groups.json")
            store.create_manager_game(teams[0].reference.game, "Arkiveret cupspil")
            group = store.create_tournament(
                "Arkivcup", teams[0].reference.game, members,
                start_round=1, final_round=3, rounds_per_tie=1,
                group_id="arkivcup", shuffle=lambda values: None,
            )
            for team in teams:
                holdet.SnapshotStore(output).save_team_json(team)
            store.archive_manager_game(teams[0].reference.game)

            with patch("holdet_lib.HoldetClient") as client_type:
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                from tests.test_website import navigate
                navigate(app, "group", group=group.group_id)
                self.assertTrue(button(app, "Opdater data").disabled)
                self.assertTrue(
                    any("er arkiveret" in item.value for item in app.warning)
                )
                self.assertEqual(client_type.mock_calls, [])

    def test_tournament_editor_exposes_members_and_revisions(self):
        with website_environment() as (config, output):
            teams = (sample_team(1), sample_team(2))
            members = tuple(
                holdet.GroupTeam(
                    team.reference.team_id,
                    team.team_name,
                    team.reference.source_url,
                )
                for team in teams
            )
            group = holdet.GroupStore(config / "groups.json").create_tournament(
                "Cup",
                teams[0].reference.game,
                members,
                start_round=1,
                final_round=3,
                rounds_per_tie=1,
                group_id="cup",
                shuffle=lambda values: None,
            )
            app = AppTest.from_file(APP_PATH).run(timeout=15)
            navigate(app, "game", locale=teams[0].reference.game.locale, game=teams[0].reference.game.slug)
            select_game_section(app, teams[0].reference.game, "administration")
            self.assertTrue(
                any(item.label == "Gem ændringer" for item in app.button)
            )
            self.assertTrue(any(item.label == "Hold" for item in app.multiselect))
            self.assertTrue(
                any("Aktiv revision 1" in item.value for item in app.caption)
            )
            self.assertEqual(group.kind, "tournament")


    def test_membership_change_requires_dialog_and_creates_revision(self):
        with website_environment() as (config, output):
            teams = tuple(sample_team(team_id, name=f"Hold {team_id}") for team_id in (1, 2, 3))
            snapshots = holdet.SnapshotStore(output)
            for team in teams:
                snapshots.save_team_json(team)
            members = tuple(
                holdet.GroupTeam(
                    team.reference.team_id, team.team_name, team.reference.source_url
                )
                for team in teams
            )
            holdet.GroupStore(config / "groups.json").create_tournament(
                "Cup", teams[0].reference.game, members[:2], start_round=1,
                final_round=4, rounds_per_tie=1, group_id="cup",
                shuffle=lambda values: None,
            )
            from website import app as dashboard
            fake = SimpleNamespace(
                fetch_game_info=lambda game: SimpleNamespace(final_round=5)
            )
            with patch("holdet_lib.HoldetClient", return_value=fake):
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                navigate(app, "game", locale=teams[0].reference.game.locale, game=teams[0].reference.game.slug)
                select_game_section(app, teams[0].reference.game, "administration")
                button(app, "Hent aktuel spilinfo").click()
                select_game_section(app, teams[0].reference.game, "administration")
                choices = widget(app, "multiselect", "Hold")
                self.assertEqual(len(choices.options), 3)
                choices.set_value(list(choices.options))
                button(app, "Gem ændringer").click()
                select_game_section(app, teams[0].reference.game, "administration")
                pending = app.session_state["pending_tournament_rebuild"]
                self.assertEqual({member.team_id for member in pending[2]}, {1, 2, 3})
                self.assertEqual(
                    len(holdet.GroupStore(config / "groups.json").load()[0].teams), 2
                )
                self.assertTrue(
                    any("Hele turneringen genberegnes" in item.value for item in app.warning)
                )
                button(app, "Genberegn turnering").click()
                select_game_section(app, teams[0].reference.game, "administration")

            rebuilt = holdet.GroupStore(config / "groups.json").load()[0]
            self.assertEqual(rebuilt.active_revision, 2)
            self.assertEqual(len(rebuilt.archived_revisions), 1)
            self.assertEqual(len(rebuilt.teams), 3)
            self.assertEqual(rebuilt.tournament.final_round, 5)


if __name__ == "__main__":
    unittest.main()
