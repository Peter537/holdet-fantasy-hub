from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
from types import SimpleNamespace
import tempfile
import tomllib
import unittest
from unittest.mock import call, patch
from uuid import uuid4

from streamlit.testing.v1 import AppTest

import holdet_lib as holdet
from tests.test_library_storage import sample_team
from tests.test_player_statistics import sample_statistics
from website import app as dashboard
from website import data_page


PROJECT_ROOT = Path(__file__).parents[1]
APP_PATH = PROJECT_ROOT / "website" / "app.py"


@contextmanager
def website_environment(accounts: list[dict[str, str]] | None = None):
    root = Path(__file__).parent / f"_test-website-{uuid4().hex}"
    config = root / "config"
    output = root / "data" / "snapshots"
    test_temp = root / "temp"
    test_temp.mkdir(parents=True)
    previous_tempdir = tempfile.tempdir
    config.mkdir(parents=True)
    output.mkdir(parents=True)
    (config / "groups.json").write_text(
        '{"schema_version":1,"groups":[]}\n', encoding="utf-8"
    )
    (config / "accounts.json").write_text(
        json.dumps({"accounts": accounts or []}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    tempfile.tempdir = str(test_temp)
    with patch.dict(
        os.environ,
        {"HOLDET_DATA_DIR": str(root)},
    ):
        try:
            yield config, output
        finally:
            tempfile.tempdir = previous_tempdir
            shutil.rmtree(root)


def button(app: AppTest, label: str):
    return next(item for item in app.button if item.label == label)


def widget(app: AppTest, kind: str, label: str):
    return next(item for item in getattr(app, kind) if item.label == label)


def navigate(app: AppTest, view: str, **parameters: object) -> AppTest:
    app.query_params = {
        "view": view,
        **{key: str(value) for key, value in parameters.items()},
    }
    return app.run(timeout=15)
def select_game_tab(app: AppTest, game: holdet.GameContext, label: str) -> AppTest:
    app.session_state[f"game-tabs-{game.locale}-{game.slug}"] = label
    return app.run(timeout=15)




class DashboardTests(unittest.TestCase):
    def test_danish_table_integer_formatting_preserves_numeric_data(self) -> None:
        self.assertEqual(dashboard._format_table_integer(50_670_000), "50.670.000")
        self.assertEqual(dashboard._format_table_integer(670_000), "670.000")
        self.assertEqual(dashboard._format_table_integer(-227_000), "-227.000")
        self.assertEqual(dashboard._format_table_integer(0), "0")
        self.assertEqual(dashboard._format_table_integer(None), "–")
        with self.assertRaises(ValueError):
            dashboard._format_table_integer(1.5)

        styled = dashboard._style_integer_columns(
            [{"Rang": 1, "Værdi": 50_670_000, "Vækst": None}],
            ("Værdi", "Vækst"),
        )
        self.assertEqual(styled.data.loc[0, "Værdi"], 50_670_000)
        self.assertEqual(styled.data.loc[0, "Rang"], 1)
        self.assertTrue(styled.data["Værdi"].dtype.kind in "iu")

    def test_manager_game_display_sort_is_case_insensitive_and_stable(self) -> None:
        games = (
            holdet.normalize_manager_game("z-game", "Zulu"),
            holdet.normalize_manager_game(
                "https://www.holdet.dk/en/fantasy/a-game", "Alpha"
            ),
            holdet.normalize_manager_game("b-game", "alpha"),
            holdet.normalize_manager_game("unicode-game", "Åben"),
        )

        ordered = dashboard._sorted_manager_games(games)

        self.assertEqual(
            [(item.name, item.game.locale, item.game.slug) for item in ordered],
            [
                ("alpha", "da", "b-game"),
                ("Alpha", "en", "a-game"),
                ("Zulu", "da", "z-game"),
                ("Åben", "da", "unicode-game"),
            ],
        )
    def test_navigation_card_is_native_escaped_accessible_and_reload_free(self) -> None:
        game_name = "Spil " + chr(0xC5) + "r & test"
        with (
            patch.object(dashboard.st, "container") as container,
            patch.object(dashboard.st, "html") as render,
            patch.object(dashboard.st, "button", return_value=True) as card_button,
            patch.object(dashboard, "_navigate") as navigate_to_game,
        ):
            dashboard._navigation_card(
                card_key="game-da-test",
                title="<" + chr(0xC5) + "ge & Co>",
                subtitle="slug",
                detail="2 grupper",
                color="#123456",
                foreground="#ffffff",
                aria_label=chr(0xC5) + 'bn "test"',
                view="game",
                locale="da",
                game=game_name,
            )
        container.assert_called_once_with(key="nav-card-game-da-test")
        expected_label = (
            "**\\<" + chr(0xC5) + "ge & Co\\>**  \n"
            ":small[slug]  \n"
            "2 grupper  \n"
            "**" + chr(0xC5) + "bn " + chr(0x2192) + "**"
        )
        card_button.assert_called_once_with(
            expected_label,
            key="open-card-game-da-test",
            help=chr(0xC5) + 'bn "test"',
            icon=None,
            type="tertiary",
            width="stretch",
        )
        navigate_to_game.assert_called_once_with(
            "game", locale="da", game=game_name
        )
        markup = render.call_args.args[0]
        self.assertNotIn("<a", markup)
        self.assertNotIn("href=", markup)
        self.assertIn("linear-gradient(125deg, #123456, #20252d)", markup)
        self.assertEqual(dashboard._markdown_literal("<A_[x]*>"), "\\<A\\_\\[x\\]\\*\\>")

        with patch.object(dashboard.st, "markdown") as render_styles:
            dashboard._styles()
        css = render_styles.call_args.args[0]
        self.assertIn('div[class*="st-key-nav-card-"]', css)
        self.assertIn('div[class*="st-key-sidebar-group-"]', css)
        self.assertIn("margin-left: 1.25rem", css)
        self.assertIn('button[kind="primary"]', css)
        self.assertIn("border-left: 3px solid #ff4b4b", css)
        self.assertIn("button:hover {", css)
        self.assertIn("button:focus-visible {", css)

    def test_sidebar_orders_manager_actions_and_nested_groups(self) -> None:
        team = sample_team(58, name="Menupunkt")
        manager_game = holdet.ManagerGame(team.reference.game, "Menuspil")
        alpha_game = holdet.normalize_manager_game("alpha-game", "Alpha")
        group = holdet.GroupDefinition(
            "menu-group",
            "Undergruppe",
            team.reference.game,
            (holdet.GroupTeam(58, team.team_name, team.reference.source_url),),
        )
        with (
            patch.object(dashboard.st, "sidebar"),
            patch.object(dashboard.st, "button", return_value=False) as menu_button,
            patch.object(dashboard.st, "container"),
            patch.object(dashboard.st, "markdown"),
            patch.object(dashboard.st, "caption"),
            patch.object(dashboard.st, "divider"),
        ):
            dashboard._sidebar(
                (manager_game, alpha_game), (group,), manager_game, group, "game"
            )

        self.assertEqual(
            [item.args[0] for item in menu_button.call_args_list],
            [
                "Mine managerspil",
                "Tilføj managerspil",
                "Spillerstatistik",
                "Holdstatistik",
                "Alpha",
                "Menuspil",
                "Undergruppe",
                "Arkiverede managerspil",
                "Hall of Fame",
                "Data og lager",
            ],
        )
        data_call = next(
            item for item in menu_button.call_args_list
            if item.args[0] == "Data og lager"
        )
        self.assertEqual(data_call.kwargs["type"], "secondary")

        with (
            patch.object(dashboard.st, "sidebar"),
            patch.object(dashboard.st, "button", return_value=False) as data_buttons,
            patch.object(dashboard.st, "markdown"),
            patch.object(dashboard.st, "caption"),
            patch.object(dashboard.st, "divider"),
        ):
            dashboard._sidebar(
                (manager_game, alpha_game), (group,), manager_game, group, "data"
            )
        active_data_call = next(
            item for item in data_buttons.call_args_list
            if item.args[0] == "Data og lager"
        )
        self.assertEqual(active_data_call.kwargs["type"], "primary")

    def test_both_add_manager_game_actions_use_reload_free_navigation(self) -> None:
        with (
            patch.object(dashboard.st, "sidebar"),
            patch.object(
                dashboard.st,
                "button",
                side_effect=lambda label, **_kwargs: label == "Tilføj managerspil",
            ),
            patch.object(dashboard.st, "markdown"),
            patch.object(dashboard.st, "caption"),
            patch.object(dashboard.st, "divider"),
            patch.object(dashboard, "_navigate") as navigate,
        ):
            dashboard._sidebar((), (), None, None, "home")
        navigate.assert_called_once_with("manage-games")

        with (
            patch.object(dashboard.st, "container"),
            patch.object(
                dashboard.st, "button",
                side_effect=lambda label, **_kwargs: label == "Tilføj managerspil",
            ) as add_button,
            patch.object(dashboard.st, "markdown"),
            patch.object(dashboard.st, "info"),
            patch.object(dashboard, "_navigate") as navigate,
        ):
            dashboard._home((), (), holdet.SnapshotIndex(()))
        self.assertEqual(
            [call.args[0] for call in add_button.call_args_list],
            ["Spillerstatistik", "Holdstatistik", "Tilføj managerspil"],
        )
        add_call = add_button.call_args_list[2]
        self.assertEqual(add_call.kwargs["key"], "add-manager-game-home")
        self.assertEqual(add_call.kwargs["icon"], ":material/add:")
        self.assertEqual(add_call.kwargs["type"], "primary")
        navigate.assert_called_once_with("manage-games")
    def test_sidebar_groups_are_indented_native_buttons_without_arrow(self) -> None:
        team = sample_team(57, name="Sidehold")
        group = holdet.GroupDefinition(
            "sidebar-id",
            "Sidegruppe",
            team.reference.game,
            (holdet.GroupTeam(57, team.team_name, team.reference.source_url),),
        )
        with (
            patch.object(dashboard.st, "container") as container,
            patch.object(dashboard.st, "button", return_value=True) as group_button,
            patch.object(dashboard, "_navigate") as navigate_to_group,
        ):
            dashboard._sidebar_group_button(group, group)

        self.assertEqual(
            [call.kwargs["key"] for call in container.call_args_list],
            ["sidebar-group-sidebar-id"],
        )
        self.assertEqual(group_button.call_args_list[0].args, ("Sidegruppe",))
        self.assertEqual(group_button.call_args_list[0].kwargs["key"], "nav-sidebar-id")
        self.assertEqual(group_button.call_args_list[0].kwargs["type"], "primary")
        self.assertNotIn("↳", group_button.call_args_list[0].args[0])
        self.assertEqual(
            navigate_to_group.call_args_list,
            [call("group", group="sidebar-id")],
        )
    def test_manager_and_group_cards_use_stable_native_keys(self) -> None:
        team = sample_team(55, name="Nøglehold")
        manager_game = holdet.ManagerGame(team.reference.game, "Nøglespil")
        group = holdet.GroupDefinition(
            "gruppe-id",
            "Nøglegruppe",
            team.reference.game,
            (holdet.GroupTeam(55, team.team_name, team.reference.source_url),),
        )
        with patch.object(dashboard, "_navigation_card") as render:
            dashboard._manager_game_card(
                manager_game, (group,), holdet.SnapshotIndex(())
            )
            dashboard._group_card(group, holdet.SnapshotIndex(()))

        self.assertEqual(
            render.call_args_list[0].kwargs["card_key"],
            f"game-{team.reference.game.locale}-{team.reference.game.slug}",
        )
        self.assertEqual(
            render.call_args_list[1].kwargs["card_key"], "group-gruppe-id"
        )
        self.assertIn("1 gruppe ·", render.call_args_list[0].kwargs["detail"])
        game_card = render.call_args_list[0].kwargs
        self.assertIn("Ingen lokale data endnu", game_card["detail"])
        self.assertNotIn("runde 0", game_card["detail"].casefold())
        self.assertEqual(game_card["icon"], ":material/directions_bike:")
        self.assertEqual(game_card["action"], chr(0xC5) + "bn og opdater manuelt")

    def test_manager_game_group_count_uses_danish_singular_and_plural(self) -> None:
        self.assertEqual(dashboard._group_count_label(0), "0 grupper")
        self.assertEqual(dashboard._group_count_label(1), "1 gruppe")
        self.assertEqual(dashboard._group_count_label(2), "2 grupper")

    def test_tournament_card_uses_danish_match_singular_and_plural(self) -> None:
        team = sample_team(56, name="Turneringshold")
        group = holdet.GroupDefinition(
            "turnering-id",
            "Turnering",
            team.reference.game,
            (holdet.GroupTeam(56, team.team_name, team.reference.source_url),),
            "tournament",
            SimpleNamespace(),
        )
        for count, expected in ((1, "1 kamp"), (3, "3 kampe")):
            state = SimpleNamespace(
                champion_id=None,
                next_matches=tuple(
                    SimpleNamespace(round_numbers=(2,)) for _ in range(count)
                ),
                phase="Gruppespil",
            )
            with (
                self.subTest(count=count),
                patch.object(
                    dashboard, "build_tournament_state", return_value=state
                ),
                patch.object(
                    dashboard, "latest_tournament_round", return_value=1
                ),
                patch.object(dashboard, "_navigation_card") as render,
            ):
                dashboard._group_card(group, holdet.SnapshotIndex(()))
                detail = render.call_args.kwargs["detail"]
                self.assertIn(f"({expected})", detail)
                self.assertNotIn("kamp(e)", detail)

    def test_danish_user_facing_copy_has_no_parenthesis_plurals(self) -> None:
        pattern = re.compile(r"\((?:e|er|r)\)")
        paths = (
            PROJECT_ROOT / "website" / "app.py",
            PROJECT_ROOT / "website" / "data_page.py",
            PROJECT_ROOT / "holdet_lib" / "tournament.py",
        )
        for path in paths:
            with self.subTest(path=path.name):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))

    def test_dismissing_account_dialog_clears_pending_action(self) -> None:
        state = {"pending_account_dialog": ("rename", "konto")}
        with patch.object(dashboard.st, "session_state", state):
            data_page._clear_pending_account_dialog()
        self.assertNotIn("pending_account_dialog", state)

    def test_standing_row_selection_navigates_directly_to_team(self) -> None:
        team = sample_team(42, name="Direkte hold", total=500, change=25)
        group = holdet.GroupDefinition(
            "direkte",
            "Direkte",
            team.reference.game,
            (holdet.GroupTeam(42, team.team_name, team.reference.source_url),),
        )
        standing = SimpleNamespace(
            rank=1,
            owner_name="Manager",
            team_name=team.team_name,
            summary=SimpleNamespace(total=500),
            change=25,
            distance=0,
            team_id=42,
            warning=None,
            stale=False,
        )
        event = SimpleNamespace(selection=SimpleNamespace(rows=[0]))
        with (
            patch.object(dashboard, "build_standings", return_value=(standing,)),
            patch.object(dashboard, "_manifest_statuses", return_value=frozenset()),
            patch.object(dashboard.st, "dataframe", return_value=event),
            patch.object(dashboard.st, "caption") as caption_instruction,
            patch.object(dashboard, "_navigate") as navigate_to_team,
        ):
            dashboard._standings_table(
                group, holdet.SnapshotIndex(()), round_number=7, mode="overall"
            )
        navigate_to_team.assert_called_once_with(
            "team", group="direkte", team=42, round=7
        )
        caption_instruction.assert_called_once_with(
            "Klik p" + chr(0xE5) + " en r" + chr(0xE6) + "kke for at "
            + chr(0xE5) + "bne holdet"
        )

        tournament_row = SimpleNamespace(
            rank=1,
            owner_name="Manager",
            team_name=team.team_name,
            played=1,
            wins=1,
            draws=0,
            losses=0,
            growth_for=25,
            growth_against=10,
            growth_difference=15,
            points=3,
            team_id=42,
        )
        state = SimpleNamespace(standings=(tournament_row,))
        with (
            patch.object(dashboard.st, "dataframe", return_value=event),
            patch.object(dashboard.st, "caption") as caption_instruction,
            patch.object(dashboard, "_navigate") as navigate_to_team,
        ):
            dashboard._tournament_standings_table(group, state, round_number=8)
        navigate_to_team.assert_called_once_with(
            "team", group="direkte", team=42, round=8
        )
        caption_instruction.assert_called_once_with(
            "Klik p" + chr(0xE5) + " en r" + chr(0xE6) + "kke for at "
            + chr(0xE5) + "bne holdet"
        )

    def test_combines_missing_round_and_stale_warning(self) -> None:
        team = sample_team(99, name="Cachet")
        group = holdet.GroupDefinition(
            "warning",
            "Warning",
            team.reference.game,
            (holdet.GroupTeam(99, team.team_name, team.reference.source_url),),
        )
        row = holdet.build_standings(
            group,
            holdet.SnapshotIndex(()),
            4,
            "round",
            stale_team_ids=frozenset({99}),
        )[0]
        self.assertEqual(
            dashboard._standing_warning(row),
            "Cachet: Mangler data for runde 4 · "
            "Seneste opdatering mislykkedes; viser cachede data, hvor det er muligt",
        )

    def test_cached_startup_danish_navigation_and_group_creation(self) -> None:
        with website_environment() as (config, output):
            team = sample_team()
            holdet.SnapshotStore(output).save_team_json(
                team, now=datetime(2026, 7, 25, tzinfo=timezone.utc)
            )
            holdet.GroupStore(config / "groups.json").create_manager_game(
                team.reference.game, "Tourspillet 2026"
            )
            app = AppTest.from_file(APP_PATH).run(timeout=15)
            self.assertFalse(app.exception)
            self.assertTrue(any("Mine managerspil" in item.value for item in app.markdown))
            self.assertFalse(
                any(item.value == "Rundecenter" for item in (*app.title, *app.subheader))
            )
            tools_heading = "V" + chr(0xC6) + "RKT" + chr(0xD8) + "JER"
            self.assertFalse(
                any(tools_heading in item.value for item in app.markdown)
            )
            labels = [item.label for item in app.button]
            self.assertIn("Mine managerspil", labels)
            self.assertNotIn("Hjem", labels)
            self.assertEqual(labels.count("Tilføj managerspil"), 2)
            self.assertFalse(
                any("Intet opdateres uden dit klik" in item.value for item in app.markdown)
            )
            self.assertFalse(any("↳" in label for label in labels))
            self.assertEqual(len(app.dataframe), 0)
            self.assertFalse(
                any(
                    item.label in {
                        chr(0xC5) + "bn managerspil",
                        chr(0xC5) + "bn gruppe",
                    }
                    for item in app.button
                )
            )
            self.assertFalse(
                any(
                    item.value == "Ikke grupperede data"
                    for item in app.subheader
                )
            )
            self.assertFalse(
                any("Brug Administration" in item.value for item in app.caption)
            )
            navigate(app, "game", locale=team.reference.game.locale, game=team.reference.game.slug)
            self.assertTrue(any(item.value == "Tourspillet 2026" for item in app.title))
            self.assertEqual(
                [item.label for item in app.tabs],
                [
                    "Rundecenter",
                    "Grupper",
                    "Spillerstatistik",
                    "Holdstatistik",
                    "Historik",
                    "Administration",
                    "Indstillinger",
                ],
            )
            self.assertFalse(any(item.label == "Tilbage til forsiden" for item in app.button))
            self.assertFalse(any(item.label == "Administration" for item in app.button))
            self.assertFalse(any(item.label == "Indstillinger" for item in app.expander))
            select_game_tab(app, team.reference.game, "Administration")
            self.assertTrue(
                any(
                    str(team.reference.team_id) in option
                    for option in app.multiselect[0].options
                )
            )
            widget(app, "text_input", "Gruppenavn").input("Tour venner")
            next(
                item
                for item in app.text_area
                if item.label.startswith("Direkte fantasy-team")
            ).input(str(team.reference.team_id))
            button(app, "Opret gruppe").click()
            select_game_tab(app, team.reference.game, "Administration")
            groups = holdet.GroupStore(config / "groups.json").load()
            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0].name, "Tour venner")
            self.assertEqual(groups[0].teams[0].team_id, team.reference.team_id)

    def test_archive_page_is_available_when_empty(self) -> None:
        with website_environment():
            app = AppTest.from_file(APP_PATH).run(timeout=15)
            button(app, "Arkiverede managerspil").click().run(timeout=15)
            self.assertTrue(
                any(item.value == "Arkiverede managerspil" for item in app.title)
            )
            self.assertTrue(
                any("ingen arkiverede managerspil" in item.value.lower() for item in app.info)
            )

    def test_archive_dialog_read_only_routes_and_one_click_restore(self) -> None:
        with website_environment() as (config, output):
            team = sample_team(77, name="Arkivhold")
            holdet.SnapshotStore(output).save_team_json(team)
            store = holdet.GroupStore(config / "groups.json")
            manager_game = store.create_manager_game(team.reference.game, "Arkivspil")
            group = store.create(
                "Arkivgruppe",
                manager_game.game,
                (holdet.GroupTeam(77, team.team_name, team.reference.source_url),),
                group_id="arkivgruppe",
            )

            with patch("holdet_lib.HoldetClient") as client_type:
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                navigate(
                    app, "game", locale=manager_game.game.locale,
                    game=manager_game.game.slug,
                )
                select_game_tab(app, manager_game.game, "Indstillinger")
                button(app, "Arkiv" + chr(0xE9) + "r managerspil").click()
                select_game_tab(app, manager_game.game, "Indstillinger")
                self.assertTrue(
                    any("data bevares" in item.value for item in app.warning)
                )
                button(app, "Bekr" + chr(0xE6) + "ft arkivering").click()
                select_game_tab(app, manager_game.game, "Indstillinger")
                archived = store.load_configuration().games[0]
                self.assertTrue(archived.is_archived)
                self.assertEqual(store.load(), (group,))
                self.assertTrue(
                    any(item.value == "Arkiverede managerspil" for item in app.title)
                )
                self.assertFalse(
                    any(item.label == "Gendan Arkivspil" for item in app.button)
                )
                self.assertTrue(
                    any("Arkivspil" in item.label for item in app.button)
                )
                self.assertFalse(any(item.label == group.name for item in app.button))
                self.assertFalse(
                    any(item.value == "ARKIVERET" for item in app.caption)
                )

                navigate(
                    app, "archive", locale=manager_game.game.locale,
                    game=manager_game.game.slug,
                )
                self.assertFalse(any(item.label == group.name for item in app.button))

                navigate(
                    app, "game", locale=manager_game.game.locale,
                    game=manager_game.game.slug,
                )
                self.assertTrue(
                    any(item.label == "Gendan managerspil" for item in app.button)
                )
                self.assertFalse(any(item.label == group.name for item in app.button))
                self.assertFalse(
                    any(item.value == "ARKIVERET" for item in app.caption)
                )
                self.assertTrue(
                    any("er arkiveret" in item.value for item in app.warning)
                )
                self.assertTrue(button(app, "Opdater managerspil").disabled)
                select_game_tab(app, manager_game.game, "Administration")
                self.assertTrue(
                    any("skrivebeskyttet" in item.value for item in app.info)
                )
                select_game_tab(app, manager_game.game, "Indstillinger")
                self.assertTrue(
                    any("skrivebeskyttede" in item.value for item in app.info)
                )
                self.assertFalse(any(item.label == "Hent spilinfo" for item in app.button))
                self.assertFalse(any(item.label == "Gem navn" for item in app.button))
                self.assertFalse(any(item.label == "Arkivér managerspil" for item in app.button))

                self.assertFalse(any(item.label == "Opret gruppe" for item in app.button))
                tab_key = f"game-tabs-{manager_game.game.locale}-{manager_game.game.slug}"
                app.session_state[tab_key] = "Spillerstatistik"
                app.run(timeout=15)
                self.assertTrue(
                    button(app, "Hent seneste spillerstatistik").disabled
                )
                self.assertEqual(client_type.mock_calls, [])

                navigate(app, "group", group=group.group_id)
                self.assertTrue(
                    any("er arkiveret" in item.value for item in app.warning)
                )
                self.assertFalse(any(item.label == group.name for item in app.button))
                self.assertFalse(
                    any(item.value == "ARKIVERET" for item in app.caption)
                )
                self.assertTrue(
                    any(item.value == group.name for item in app.title)
                )
                self.assertFalse(
                    any(item.label == "Gendan managerspil" for item in app.button)
                )

                navigate(
                    app, "team", group=group.group_id,
                    team=team.reference.team_id,
                )
                self.assertFalse(any(item.label == group.name for item in app.button))
                self.assertFalse(
                    any(item.value == "ARKIVERET" for item in app.caption)
                )
                self.assertFalse(
                    any(item.label == "Gendan managerspil" for item in app.button)
                )

                navigate(
                    app, "game", locale=manager_game.game.locale,
                    game=manager_game.game.slug,
                )
                button(app, "Gendan managerspil").click().run(timeout=15)

            restored = store.load_configuration().games[0]
            self.assertFalse(restored.is_archived)
            self.assertTrue(any(item.value == "Arkivspil" for item in app.title))

    def test_manager_game_is_primary_persistent_and_navigation_is_offline(self) -> None:
        with website_environment() as (config, _output):
            with patch("holdet_lib.HoldetClient") as client_type:
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                self.assertTrue(any("Mine managerspil" in item.value for item in app.markdown))
                self.assertEqual(client_type.mock_calls, [])

                add_label = "Tilf" + chr(0xf8) + "j managerspil"
                add_buttons = [item for item in app.button if item.label == add_label]
                self.assertEqual(len(add_buttons), 2)
                add_buttons[1].click().run(timeout=15)
                widget(app, "text_input", "Holdet-URL eller slug").input(
                    "super-manager-fall-2026"
                )
                widget(app, "text_input", "Navn (valgfrit)").input(
                    "Superliga Efter" + chr(0xe5) + "r 2026"
                )
                add = "Tilf" + chr(0xf8) + "j managerspil"
                button(app, add).click().run(timeout=15)

                self.assertFalse(app.exception)
                self.assertTrue(
                    any(
                        item.value == "Superliga Efter" + chr(0xe5) + "r 2026"
                        for item in app.title
                    )
                )
                self.assertEqual(client_type.mock_calls, [])
                configuration = holdet.GroupStore(
                    config / "groups.json"
                ).load_configuration()
                self.assertEqual(len(configuration.games), 1)
                self.assertEqual(configuration.groups, ())
                self.assertEqual(configuration.games[0].game.locale, "da")

                select_game_tab(app, configuration.games[0].game, "Indstillinger")
                widget(app, "text_input", "Visningsnavn").input("Nyt navn")
                button(app, "Gem navn").click()
                select_game_tab(app, configuration.games[0].game, "Indstillinger")
                self.assertEqual(
                    holdet.GroupStore(config / "groups.json")
                    .load_configuration().games[0].name,
                    "Nyt navn",
                )
                self.assertEqual(client_type.mock_calls, [])

    def test_data_storage_page_is_side_effect_free_until_a_button_click(self) -> None:
        with website_environment() as (config, output):
            root = config.parent
            exports = root / "exports"
            manifests = root / "data" / "manifests"
            revisions = root / "data" / "group-revisions"
            self.assertFalse(exports.exists())
            self.assertFalse(manifests.exists())
            self.assertFalse(revisions.exists())

            app = AppTest.from_file(APP_PATH).run(timeout=15)
            button(app, "Data og lager").click().run(timeout=15)

            self.assertFalse(app.exception)
            self.assertTrue(any(item.value == "Data og lager" for item in app.title))
            self.assertEqual(
                [item.label for item in app.tabs],
                [
                    "Gemte konti",
                    "Datastatus",
                    "Lagerplaceringer",
                    "Backup og gendannelse",
                ],
            )
            app.session_state["data-storage-tabs"] = "Lagerplaceringer"
            app.run(timeout=15)
            displayed = {item.value for item in app.code}
            self.assertIn(str(config.resolve()), displayed)
            self.assertIn(str(output.resolve()), displayed)
            self.assertIn(str(exports.resolve()), displayed)
            self.assertFalse(exports.exists())
            self.assertFalse(manifests.exists())
            self.assertFalse(revisions.exists())
            self.assertFalse(
                any("Projektets tidligere" in item.value for item in app.info)
            )

    def test_saved_accounts_metrics_table_and_add_flow(self) -> None:
        accounts = [
            {
                "key": "konto-a",
                "label": "Åge",
                "profile_url": "https://www.holdet.dk/da/users/111/teams",
            }
        ]
        with website_environment(accounts) as (config, output):
            app = AppTest.from_file(APP_PATH).run(timeout=15)
            navigate(app, "data")
            metrics = {item.label: item.value for item in app.metric}
            self.assertEqual(metrics["Gemte konti"], "1")
            self.assertEqual(metrics["Managerspil"], "0")
            self.assertEqual(metrics["Grupper"], "0")
            self.assertEqual(metrics["Teamsnapshots"], "0")
            self.assertEqual(len(app.dataframe), 1)
            frame = app.dataframe[0].value
            self.assertEqual(
                list(frame.columns),
                [
                    "Navn", "Bruger-ID", "Teknisk nøgle",
                    "Gruppemedlemskaber", "Profil",
                ],
            )
            self.assertEqual(frame.loc[0, "Navn"], "Åge")
            self.assertEqual(frame.loc[0, "Bruger-ID"], 111)
            self.assertEqual(frame.loc[0, "Gruppemedlemskaber"], 0)

            app.session_state["discovered_teams"] = {("da", "test"): ("stale",)}
            button(app, "Tilføj konto").click().run(timeout=15)
            widget(app, "text_input", "Visningsnavn").input("Ærlige Ørn")
            widget(app, "text_input", "Holdet-profil-URL").input(
                "https://www.holdet.dk/da/users/222/teams"
            ).run(timeout=15)
            self.assertTrue(
                any("aerlige-orn" in item.value for item in app.caption),
                [item.value for item in app.caption] + [item.value for item in app.error],
            )
            button(app, "Gem konto").click().run(timeout=15)
            saved = holdet.AccountStore(config / "accounts.json").load()
            self.assertEqual([item.user_id for item in saved], [111, 222])
            self.assertEqual(saved[1].key, "aerlige-orn")
            self.assertNotIn("discovered_teams", app.session_state)

    def test_corrupt_accounts_file_is_visible_and_not_overwritten(self) -> None:
        with website_environment() as (config, output):
            path = config / "accounts.json"
            path.write_text("{broken", encoding="utf-8")
            app = AppTest.from_file(APP_PATH).run(timeout=15)
            navigate(app, "data")
            self.assertTrue(any("kan derfor ikke overskrives" in item.value for item in app.error))
            self.assertFalse(any(item.label == "Tilføj konto" for item in app.button))
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")
    def test_custom_game_sources_and_locale_aware_numeric_ids(self) -> None:
        game = dashboard._normalize_game_source("super-manager-fall-2026")
        self.assertEqual((game.locale, game.slug), ("da", "super-manager-fall-2026"))
        member = dashboard._parse_direct_lines("987654", game)[0]
        self.assertEqual(
            member.source_url,
            "https://www.holdet.dk/da/fantasy/"
            "super-manager-fall-2026/fantasyteams/987654",
        )

        nested = dashboard._normalize_game_source(
            "https://www.holdet.dk/en/fantasy/future-game-2030/rules"
        )
        self.assertEqual((nested.locale, nested.slug), ("en", "future-game-2030"))
        member = dashboard._parse_direct_lines("123456", nested)[0]
        self.assertEqual(
            member.source_url,
            "https://www.holdet.dk/en/fantasy/"
            "future-game-2030/fantasyteams/123456",
        )
        with self.assertRaises(holdet.PayloadError):
            dashboard._parse_direct_lines(
                "https://www.holdet.dk/da/fantasy/"
                "tour-de-france-2026/fantasyteams/123456",
                game,
            )

    def test_account_discovery_is_explicit_deduplicated_and_partial(self) -> None:
        accounts = [
            {
                "key": "konto-a",
                "label": "Åge",
                "profile_url": "https://www.holdet.dk/da/users/1001/teams",
            },
            {
                "key": "konto-b",
                "label": "Bente",
                "profile_url": "https://www.holdet.dk/da/users/1002/teams",
            },
            {
                "key": "konto-c",
                "label": "Claus",
                "profile_url": "https://www.holdet.dk/da/users/1003/teams",
            },
        ]

        class FakeClient:
            def __init__(self):
                self.calls: list[str] = []

            def discover_account_teams(self, account, *, game):
                self.calls.append(account.key)
                if account.key == "konto-c":
                    raise holdet.FetchError("midlertidig fejl")
                return (
                    holdet.TeamReference(
                        game=game,
                        team_id=777,
                        team_name="Ærlige Åge",
                        source_url=(
                            f"https://www.holdet.dk/{game.locale}/fantasy/"
                            f"{game.slug}/fantasyteams/777"
                        ),
                        account_key=account.key,
                        account_label=account.label,
                        account_user_id=account.user_id,
                        profile_url=account.profile_url,
                    ),
                )

        fake = FakeClient()
        with website_environment(accounts) as (config, output):
            game = holdet.GameUrl(
                "https://www.holdet.dk/da/fantasy/super-manager-fall-2026",
                "da",
                "super-manager-fall-2026",
            )
            cached = sample_team(888, name="Cachehold")
            cached_reference = replace(
                cached.reference,
                game=game,
                source_url=(
                    "https://www.holdet.dk/da/fantasy/"
                    "super-manager-fall-2026/fantasyteams/888"
                ),
            )
            holdet.SnapshotStore(output).save_team_json(
                replace(cached, reference=cached_reference)
            )
            holdet.GroupStore(config / "groups.json").create_manager_game(
                game, "Super Manager Efterår 2026"
            )
            with patch("holdet_lib.HoldetClient", return_value=fake):
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                self.assertEqual(fake.calls, [])
                navigate(app, "game", locale=game.locale, game=game.slug)
                self.assertEqual(fake.calls, [])
                select_game_tab(app, game, "Administration")
                self.assertEqual(fake.calls, [])
                button(app, "Find hold på konfigurerede konti").click()
                select_game_tab(app, game, "Administration")

                self.assertEqual(fake.calls, ["konto-a", "konto-b", "konto-c"])
                self.assertTrue(
                    any("1 hold fundet" in item.value for item in app.success)
                )
                self.assertTrue(
                    any("Claus: midlertidig fejl" in item.value for item in app.warning)
                )
                choices = widget(app, "multiselect", "Fundne hold")
                self.assertTrue(any("777" in option for option in choices.options))
                choices.select("da:super-manager-fall-2026:777")
                widget(app, "text_input", "Gruppenavn").input("Opdaget gruppe")
                button(app, "Opret gruppe").click()
                select_game_tab(app, game, "Administration")

            group = holdet.GroupStore(config / "groups.json").load()[0]
            self.assertEqual(len(group.teams), 1)
            self.assertEqual(group.teams[0].team_id, 777)
            self.assertEqual(group.teams[0].name, "Ærlige Åge")
            self.assertEqual(group.teams[0].account_key, "konto-a")

    def test_group_modes_selection_contract_and_partial_refresh_display(self) -> None:
        with website_environment() as (config, output):
            first = sample_team(1, name="Alpha", total=500, change=10)
            second = sample_team(2, name="Beta", total=400, change=30)
            snapshot_store = holdet.SnapshotStore(output)
            snapshot_store.save_team_json(first)
            snapshot_store.save_team_json(second)
            group = holdet.GroupStore(config / "groups.json").create(
                "Tour venner",
                first.reference.game,
                (
                    holdet.GroupTeam(1, first.team_name, first.reference.source_url),
                    holdet.GroupTeam(2, second.team_name, second.reference.source_url),
                ),
                group_id="tour-venner",
            )

            class FakeClient:
                def fetch_team(self, reference):
                    if reference.team_id == 2:
                        raise holdet.FetchError("testfejl")
                    return first

            with patch("holdet_lib.HoldetClient", return_value=FakeClient()):
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                navigate(app, "game", locale=first.reference.game.locale, game=first.reference.game.slug)
                button(app, "Opdater managerspil").click().run(timeout=20)
                self.assertTrue(any("bruger cache" in item.value for item in app.success))
                navigate(app, "group", group=group.group_id)
                self.assertFalse(app.exception)
                self.assertTrue(any(item.value == group.name for item in app.title))
                self.assertEqual({item.value for item in app.segmented_control}, {"Overall"})
                expected_columns = [
                    "Rang", "Manager", "Hold", "Værdi",
                    "Vækst", "Afstand", "Hold-ID",
                ]
                overall_frame = app.dataframe[0].value
                self.assertEqual(list(overall_frame.columns), expected_columns)
                self.assertEqual(
                    overall_frame[["Hold", "Værdi", "Vækst", "Afstand"]].to_dict("records"),
                    [
                        {"Hold": "Alpha", "Værdi": 500, "Vækst": 10, "Afstand": 0},
                        {"Hold": "Beta", "Værdi": 400, "Vækst": 30, "Afstand": -100},
                    ],
                )
                app.segmented_control[0].set_value("Runde").run(timeout=15)
                self.assertEqual(app.segmented_control[0].value, "Runde")
                self.assertEqual(len(app.dataframe), 1)
                self.assertTrue(app.dataframe[0].key.startswith("standing-"))
                round_frame = app.dataframe[0].value
                self.assertEqual(list(round_frame.columns), expected_columns)
                self.assertNotIn("Status", round_frame.columns)
                self.assertNotIn("Rundevækst", round_frame.columns)
                self.assertEqual(
                    round_frame[["Hold", "Værdi", "Vækst", "Afstand"]].to_dict("records"),
                    [
                        {"Hold": "Beta", "Værdi": 400, "Vækst": 30, "Afstand": 0},
                        {"Hold": "Alpha", "Værdi": 500, "Vækst": 10, "Afstand": -20},
                    ],
                )
                self.assertTrue(
                    any(
                        "Beta: Seneste opdatering mislykkedes" in item.value
                        for item in app.warning
                    )
                )
                manifests = list(
                    (
                        output.parent
                        / "manifests"
                        / first.reference.game.slug
                        / "game"
                    ).glob("refresh-round*.json")
                )
                self.assertEqual(len(manifests), 1)

    def test_player_statistics_rows_use_dynamic_labels_status_and_numeric_values(self) -> None:
        cycling = sample_statistics()
        rows, numeric = dashboard._player_statistics_rows(cycling)
        self.assertEqual(
            list(rows[0]),
            ["Navn", "Hold", "Position", "Pris", "Totalvækst", "Vækst", "Status"],
        )
        self.assertEqual(numeric, ("Pris", "Totalvækst", "Vækst"))
        self.assertEqual(rows[0]["Pris"], 12_345_000)
        self.assertEqual(rows[0]["Totalvækst"], 2_345_000)
        self.assertEqual(rows[0]["Vækst"], -125_000)
        self.assertEqual(
            rows[0]["Status"],
            "Inaktiv · Deaktiveret · Skadet · Karantæne",
        )

        golf_rows, golf_numeric = dashboard._player_statistics_rows(
            replace(cycling, variant="golf", format="golf", unit="points")
        )
        self.assertEqual(
            list(golf_rows[0]),
            [
                "Navn", "Land", "Kategori", "Point",
                "Totalændring", "Rundeændring", "Status",
            ],
        )
        self.assertEqual(
            golf_numeric, ("Point", "Totalændring", "Rundeændring")
        )

    def test_missing_player_round_batch_skips_cache_and_continues_after_error(self) -> None:
        class BatchClient:
            def __init__(self):
                self.calls: list[int] = []

            def fetch_players(self, game, *, round_number=None):
                assert round_number is not None
                self.calls.append(round_number)
                if round_number == 3:
                    raise holdet.FetchError("round 3 failed")
                return sample_statistics(round_number)

        with website_environment() as (_config, output):
            game = sample_statistics().game
            store = holdet.PlayerStatisticsStore(output)
            store.save(sample_statistics(2))
            client = BatchClient()
            result = dashboard._fetch_missing_player_rounds(
                game, 1, 4, store=store, client=client
            )

            self.assertEqual(client.calls, [1, 3, 4])
            self.assertEqual(result.fetched, (1, 4))
            self.assertEqual(result.skipped, (2,))
            self.assertEqual(result.failures[0][0], 3)
            self.assertEqual(store.scan(game).rounds_for(game), (4, 2, 1))

    def test_cached_player_tab_is_offline_and_historical_round_is_on_demand(self) -> None:
        class FakeClient:
            def __init__(self):
                self.calls: list[int | None] = []

            def fetch_players(self, game, *, round_number=None):
                self.calls.append(round_number)
                return sample_statistics(round_number or 8)

        fake = FakeClient()
        with website_environment() as (config, output):
            game = sample_statistics().game
            holdet.GroupStore(config / "groups.json").create_manager_game(
                game, "Tourspillet"
            )
            holdet.PlayerStatisticsStore(output).save(sample_statistics())
            with patch("holdet_lib.HoldetClient", return_value=fake):
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                navigate(app, "game", locale=game.locale, game=game.slug)
                self.assertEqual(fake.calls, [])
                tab_key = "game-tabs-da-tour-de-france-2026"
                app.session_state[tab_key] = "Spillerstatistik"
                app.run(timeout=15)
                self.assertEqual(fake.calls, [])
                frame = app.dataframe[0].value
                self.assertEqual(frame.loc[0, "Pris"], 12_345_000)
                self.assertEqual(frame.loc[0, "Totalvækst"], 2_345_000)
                self.assertEqual(frame.loc[0, "Vækst"], -125_000)
                self.assertTrue(frame["Pris"].dtype.kind in "iu")

                widget(app, "selectbox", "Runde").set_value(6)
                app.session_state[tab_key] = "Spillerstatistik"
                app.run(timeout=15)
                button(app, "Hent runde 6").click()
                app.session_state[tab_key] = "Spillerstatistik"
                app.run(timeout=15)
                self.assertEqual(fake.calls, [6])
                self.assertEqual(
                    holdet.PlayerStatisticsStore(output)
                    .scan(game)
                    .rounds_for(game),
                    (7, 6),
                )
                self.assertTrue(
                    any("Runde 6 blev hentet" in item.value for item in app.success)
                )

    def test_failed_player_refresh_keeps_cache_details_and_retries_exact_round(self) -> None:
        class FlakyClient:
            def __init__(self):
                self.calls = []

            def fetch_players(self, game, *, round_number=None):
                self.calls.append(round_number)
                if len(self.calls) == 1:
                    raise holdet.FetchError("midlertidig statistikfejl")
                return sample_statistics()

        with website_environment() as (config, output):
            game = sample_statistics().game
            holdet.GroupStore(config / "groups.json").create_manager_game(
                game, "Tourspillet"
            )
            holdet.PlayerStatisticsStore(output).save(sample_statistics())
            client = FlakyClient()
            with patch("holdet_lib.HoldetClient", return_value=client):
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                navigate(app, "game", locale=game.locale, game=game.slug)
                tab_key = "game-tabs-da-tour-de-france-2026"
                app.session_state[tab_key] = "Spillerstatistik"
                app.run(timeout=15)
                button(app, "Opdater runde 7").click()
                app.session_state[tab_key] = "Spillerstatistik"
                app.run(timeout=15)

                self.assertEqual(len(app.dataframe), 1)
                self.assertEqual(client.calls, [7])
                self.assertTrue(
                    any(
                        "Holdet kunne ikke kontaktes efter flere forsøg" in item.value
                        and "midlertidig statistikfejl" not in item.value
                        for item in app.warning
                    )
                )
                self.assertTrue(
                    any(item.label == "Tekniske detaljer" for item in app.expander)
                )
                self.assertTrue(
                    any(
                        "midlertidig statistikfejl" in item.value
                        for item in app.code
                    )
                )
                self.assertEqual(
                    app.session_state["player_statistics_errors"][
                        ("da", "tour-de-france-2026")
                    ]["round_number"],
                    7,
                )

                button(app, "Prøv igen").click()
                app.session_state[tab_key] = "Spillerstatistik"
                app.run(timeout=15)
                self.assertEqual(client.calls, [7, 7])
                self.assertFalse(
                    any(
                        "Holdet kunne ikke kontaktes efter flere forsøg" in item.value
                        for item in app.warning
                    )
                )
                self.assertFalse(
                    any(item.label == "Prøv igen" for item in app.button)
                )
                self.assertTrue(
                    any("Runde 7 blev hentet" in item.value for item in app.success)
                )

                app.session_state["player_statistics_errors"] = {
                    ("da", "tour-de-france-2026"): "gammel teknisk fejl"
                }
                app.session_state[tab_key] = "Spillerstatistik"
                app.run(timeout=15)
                self.assertTrue(
                    any("gammel teknisk fejl" in item.value for item in app.code)
                )


    def test_standalone_statistics_start_empty_and_show_technical_metadata(self) -> None:
        with website_environment() as (config, output):
            team = sample_team(707, name="Cached team")
            game = team.reference.game
            holdet.GroupStore(config / "groups.json").create_manager_game(
                game, "Tour display name"
            )
            holdet.SnapshotStore(output).save_team_json(team)
            holdet.PlayerStatisticsStore(output).save(sample_statistics())

            app = AppTest.from_file(APP_PATH).run(timeout=15)
            navigate(app, "players")
            player_game = widget(app, "selectbox", "Managerspil")
            self.assertIsNone(player_game.value)
            self.assertFalse(app.dataframe)

            app.session_state["standalone-player-game"] = game.original
            app.run(timeout=15)
            self.assertTrue(
                any(
                    game.slug in item.value and "sprog: da" in item.value
                    for item in app.caption
                )
            )

            navigate(app, "teams")
            team_game = widget(app, "selectbox", "Managerspil")
            self.assertIsNone(team_game.value)
            self.assertEqual([item.value for item in app.title], ["Holdstatistik"])

            app.session_state["standalone-team-game"] = game.original
            app.run(timeout=15)
            team_choice = widget(app, "selectbox", "Hold")
            self.assertIsNone(team_choice.value)
            self.assertEqual([item.value for item in app.title], ["Holdstatistik"])
            self.assertTrue(
                any(
                    game.slug in item.value and "sprog: da" in item.value
                    for item in app.caption
                )
            )

    def test_standalone_player_statistics_retries_without_registering_game(self) -> None:
        class FlakyClient:
            def __init__(self):
                self.calls = []

            def fetch_players(self, game, *, round_number=None):
                self.calls.append(round_number)
                if len(self.calls) == 1:
                    raise holdet.FetchError("forbindelsen blev afvist")
                return sample_statistics()

        with website_environment() as (config, output):
            game = sample_statistics().game
            holdet.PlayerStatisticsStore(output).save(sample_statistics())
            client = FlakyClient()
            with patch("holdet_lib.HoldetClient", return_value=client):
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                navigate(app, "players")
                app.session_state["standalone-player-game"] = game.original
                app.run(timeout=15)
                button(app, "Hent seneste spillerstatistik").click()
                app.run(timeout=15)

                self.assertEqual(client.calls, [None])
                self.assertEqual(len(app.dataframe), 1)
                self.assertTrue(
                    any(item.label == "Prøv igen" for item in app.button)
                )

                button(app, "Prøv igen").click()
                app.run(timeout=15)
                self.assertEqual(client.calls, [None, None])
                self.assertEqual(len(app.dataframe), 1)
                self.assertEqual(
                    holdet.PlayerStatisticsStore(output).scan(game).rounds_for(game),
                    (7,),
                )
                self.assertEqual(
                    holdet.GroupStore(config / "groups.json").load_configuration().games,
                    (),
                )


    def test_standalone_player_statistics_uses_cache_without_registering_game_and_exports(self) -> None:
        class OfflineClient:
            def fetch_players(self, *_args, **_kwargs):
                raise AssertionError("navigation must not contact Holdet")

        with website_environment() as (config, output):
            cached = sample_statistics()
            holdet.PlayerStatisticsStore(output).save(cached)
            with patch("holdet_lib.HoldetClient", return_value=OfflineClient()):
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                navigate(app, "players")
                self.assertEqual([item.value for item in app.title], ["Spillerstatistik"])
                app.session_state["standalone-player-game"] = cached.game.original
                app.run(timeout=15)

                self.assertFalse(app.exception)
                self.assertEqual(len(app.dataframe), 1)
                self.assertEqual(app.dataframe[0].value.loc[0, "Pris"], 12_345_000)
                self.assertEqual(
                    holdet.GroupStore(config / "groups.json").load_configuration().games,
                    (),
                )
                button(app, "Opret eksport").click()
                app.run(timeout=15)
                exports = list(
                    (output.parent.parent / "exports" / "players").rglob("*.txt")
                )
                self.assertEqual(len(exports), 1)
                self.assertIn("Spil: tour-de-france-2026", exports[0].read_text(encoding="utf-8"))
                self.assertTrue(
                    any(item.label == "Download TXT" for item in app.get("download_button"))
                )
    def test_streamlit_config_and_readme_use_short_local_command(self) -> None:
        config = tomllib.loads(
            (PROJECT_ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(config["server"]["address"], "127.0.0.1")
        self.assertNotIn("port", config["server"])
        self.assertEqual(config["client"]["toolbarMode"], "viewer")
        app_source = APP_PATH.read_text(encoding="utf-8")
        data_source = (PROJECT_ROOT / "website" / "data_page.py").read_text(
            encoding="utf-8"
        )
        for anchor in (
            "spillerstatistik",
            "holdstatistik",
            "arkiverede-managerspil",
            "administrer-managerspil",
        ):
            self.assertIn(f'anchor="{anchor}"', app_source)
        self.assertIn('anchor="data-og-lager"', data_source)

        readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
        command = "py -3.14 -m streamlit run .\\website\\app.py"
        self.assertIn(command, readme)
        self.assertNotIn("--server.address", readme)
        self.assertNotIn("--server.port", readme)
        self.assertIn("http://localhost:8501", readme)


if __name__ == "__main__":
    unittest.main()
