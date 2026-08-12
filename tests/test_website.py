from __future__ import annotations

from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
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
from website import ui as dashboard
from website import data_page
from website.navigation import PageId, page_source


PROJECT_ROOT = Path(__file__).parents[1]
APP_PATH = PROJECT_ROOT / "website" / "app.py"
UI_PATH = PROJECT_ROOT / "website" / "ui.py"


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


def navigate(app: AppTest, route: str, **parameters: object) -> AppTest:
    page_id = {
        "home": PageId.HOME,
        "manage-games": PageId.MANAGE_GAMES,
        "archive": PageId.ARCHIVE,
        "players": PageId.PLAYERS,
        "scouting": PageId.SCOUTING,
        "teams": PageId.TEAMS,
        "managers": PageId.MANAGERS,
        "hall-of-fame": PageId.MANAGERS,
        "calendar": PageId.CALENDAR,
        "data": PageId.DATA,
        "game": PageId.GAME,
        "group": PageId.GROUP,
        "team": PageId.TEAM,
        "player": PageId.PLAYER,
        "alerts": PageId.ALERTS,
    }.get(route, PageId.NOT_FOUND)
    app.query_params = {key: str(value) for key, value in parameters.items()}
    relative_page = page_source(page_id).relative_to(APP_PATH.parent)
    return app.switch_page(str(relative_page)).run(timeout=15)
def select_game_tab(app: AppTest, game: holdet.GameContext, label: str) -> AppTest:
    app.session_state[f"game-tabs-{game.locale}-{game.slug}"] = label
    return app.run(timeout=15)


def _refresh_dialog_test_app(manager_game, groups) -> None:
    """Render the real dialog on every AppTest full rerun.

    AppTest does not emulate fragment-only dialog reruns, so this harness keeps
    the production dialog mounted while its Start button is exercised.
    """

    from website import ui as app_ui

    app_ui._configure_paths()
    app_ui._manager_game_refresh_dialog(manager_game, groups)




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

        with patch.object(dashboard.st, "html") as render_styles:
            dashboard._styles()
        css = render_styles.call_args.args[0]
        self.assertIn('[class*="st-key-nav-card-"]', css)
        self.assertIn('[class*="st-key-sidebar-group-"]', css)
        self.assertIn("margin-left: 1.25rem", css)
        self.assertIn("button:hover {", css)
        self.assertIn("button:focus-visible {", css)
        self.assertIn("prefers-reduced-motion: reduce", css)
        self.assertNotIn("data-testid", css)
        self.assertNotIn(".stApp", css)

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
            patch.object(dashboard, "_unread_alert_counts", return_value={}),
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
                "Scouting",
                "Holdstatistik",
                "Alpha",
                "Menuspil",
                "Undergruppe",
                "Arkiverede managerspil",
                "Managers",
                "Kalender",
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
            patch.object(dashboard, "_unread_alert_counts", return_value={}),
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
            ["Spillerstatistik", "Scouting", "Holdstatistik", "Tilføj managerspil"],
        )
        add_call = add_button.call_args_list[3]
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
        self.assertIn("Spillerdata mangler", game_card["signals"])
        self.assertIn("Tidsplan er ikke verificeret", game_card["signals"])
        self.assertNotIn("runde 0", game_card["detail"].casefold())
        self.assertEqual(game_card["icon"], ":material/directions_bike:")
        self.assertEqual(game_card["subtitle"], "Cykling")
        self.assertEqual(game_card["metadata"], team.reference.game.slug)
        self.assertEqual(game_card["action"], chr(0xC5) + "bn managerspil")

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
            UI_PATH,
            PROJECT_ROOT / "website" / "data_page.py",
            PROJECT_ROOT / "holdet_lib" / "tournament.py",
        )
        for path in paths:
            with self.subTest(path=path.name):
                self.assertIsNone(pattern.search(path.read_text(encoding="utf-8")))

    def test_danish_user_facing_copy_has_no_ascii_transliterations_or_mojibake(self) -> None:
        transliterations = re.compile(
            r"\b(?:aabn|aaben|aabne|aendre|aendring|aendringer|afhaengig|"
            r"begraens|foer|foerste|gennemfoer|goer|hoej|hoejere|koer|koerer|"
            r"laes|laeser|laest|loeb|loeser|maal|maalmand|noedvendig|ophoev|"
            r"praecis|saeson|saesoner|schemaer|stoerre|stoerste|taet|taettere|"
            r"taetteste|tilfoej|tilfoejet|udfoer|vaelg|vaelges|vaekst|undgaa|"
            r"undgaaelse)\b",
            re.IGNORECASE,
        )
        mojibake = re.compile(r"\ufffd|Ã|Â|â€|ðŸ")
        paths = [
            PROJECT_ROOT / "README.md",
            *sorted((PROJECT_ROOT / "docs").glob("*.md")),
            *sorted((PROJECT_ROOT / "website").glob("*.py")),
            *sorted((PROJECT_ROOT / "holdet_lib").glob("*.py")),
        ]
        paths = [path for path in paths if path.name != "transfers.py"]
        for path in paths:
            text = path.read_text(encoding="utf-8-sig")
            with self.subTest(path=path.relative_to(PROJECT_ROOT)):
                self.assertIsNone(transliterations.search(text))
                self.assertIsNone(mojibake.search(text))

    def test_danish_navigation_and_tournament_copy_uses_native_letters(self) -> None:
        app_copy = UI_PATH.read_text(encoding="utf-8")
        hub_copy = (PROJECT_ROOT / "website" / "hub_pages.py").read_text(
            encoding="utf-8"
        )
        for expected in (
            '"Åbn officiel gruppe"',
            '"Scoregrupper med rematch-undgåelse og fair bye."',
            '"Fjern gruppen fra den aktive sæson før sletning."',
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, app_copy)
        for expected in (
            '"Åbn spilinfo"',
            "stier, skemaer, størrelser og kontrolsummer",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, hub_copy)

    def test_dismissing_account_dialog_clears_pending_action(self) -> None:
        state = {"pending_account_dialog": ("rename", "konto")}
        with patch.object(dashboard.st, "session_state", state):
            data_page._clear_pending_account_dialog()
        self.assertNotIn("pending_account_dialog", state)

    def test_local_store_health_isolates_corrupt_additive_stores(self) -> None:
        with website_environment() as (config, _output):
            root = config.parent
            paths = holdet.resolve_paths(
                overrides=holdet.PathOverrides(data_root=root),
                environ={},
            )
            group_store = holdet.GroupStore(
                paths.groups_file,
                paths.group_revision_dir,
            )
            configuration = group_store.load_configuration()
            paths.seasons_file.write_text("{", encoding="utf-8")
            paths.hall_of_fame_dir.mkdir(parents=True)
            (paths.hall_of_fame_dir / "bad.json").write_text(
                "{",
                encoding="utf-8",
            )

            rows = data_page._local_store_health(
                group_store,
                configuration,
                paths,
            )

        by_store = {row["Lager"]: row for row in rows}
        self.assertEqual(by_store["S\u00e6soner"]["Status"], "Fejl")
        self.assertEqual(
            by_store["Managerhistorik"]["Status"],
            "Advarsel",
        )
        self.assertEqual(
            by_store["Turneringsrevisioner"]["Status"],
            "OK",
        )
        self.assertEqual(
            by_store["Turneringsparringer"]["Status"],
            "OK",
        )

    def test_manager_calendar_story_and_season_routes_use_cached_data(self) -> None:
        class OfflineClient:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("navigation must not contact Holdet")

        with website_environment() as (config, output):
            first = sample_team(801, name="F\u00f8rste hold")
            second = sample_team(802, name="Andet hold")
            store = holdet.GroupStore(config / "groups.json")
            store.create_manager_game(first.reference.game, "Testspil")
            members = tuple(
                holdet.GroupTeam(
                    team.reference.team_id,
                    team.team_name,
                    team.reference.source_url,
                    team.reference.account_key,
                    team.reference.account_label,
                    team.reference.account_user_id,
                    team.reference.profile_url,
                )
                for team in (first, second)
            )
            group = store.create_tournament(
                "Testliga",
                first.reference.game,
                members,
                start_round=1,
                final_round=3,
                rounds_per_tie=1,
                group_id="cached-manager-routes",
                template="league",
                definition_options={
                    "seed_rule": "manual",
                    "seed_order": (801, 802),
                },
            )
            store.update(
                replace(
                    group,
                    official_url=(
                        "https://www.holdet.dk/da/fantasy/test/group/1"
                    ),
                    official_link_type="group",
                )
            )
            snapshot_store = holdet.SnapshotStore(output)
            snapshot_store.save_team_json(first)
            snapshot_store.save_team_json(second)
            paths = holdet.resolve_paths(
                overrides=holdet.PathOverrides(data_root=config.parent),
                environ={},
            )
            holdet.SeasonStore(paths.seasons_file).save(
                (
                    holdet.SeasonDefinition(
                        "season-one",
                        "Tests\u00e6son",
                        ("cached-manager-routes",),
                    ),
                )
            )

            with patch("holdet_lib.HoldetClient", OfflineClient):
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                navigate(app, "managers", section="ranking")
                self.assertFalse(app.exception)
                self.assertEqual([item.value for item in app.title], ["Managers"])
                self.assertTrue(app.dataframe)

                navigate(app, "managers", section="compare")
                self.assertFalse(app.exception)
                self.assertTrue(
                    any(
                        item.label == "F\u00e6lles runder V-U-T"
                        for item in app.metric
                    )
                )

                navigate(
                    app,
                    "managers",
                    section="seasons",
                    season="season-one",
                )
                self.assertFalse(app.exception)
                self.assertTrue(
                    any(item.label == "S\u00e6son" for item in app.selectbox)
                )

                navigate(app, "managers", section="identities")
                self.assertFalse(app.exception)
                self.assertTrue(
                    any(
                        item.label == "Identiteter for samme person"
                        for item in app.multiselect
                    )
                )
                identity_picker = widget(
                    app,
                    "multiselect",
                    "Identiteter for samme person",
                )
                identity_picker.set_value(
                    [
                        f"owner:{first.owner_user_id}",
                        f"owner:{second.owner_user_id}",
                    ]
                )
                widget(
                    app,
                    "text_input",
                    "Visningsnavn",
                ).set_value("Samlet manager")
                button(app, "Saml identiteter").click()
                app.run(timeout=15)
                saved_settings = holdet.HubSettingsStore(
                    paths.hub_settings_file
                ).load()
                merged = next(
                    item
                    for item in saved_settings.manager_profiles
                    if item.display_name == "Samlet manager"
                )
                self.assertTrue(
                    merged.manager_id.startswith("manager:")
                )
                self.assertEqual(
                    set(merged.manual_identity_keys),
                    {
                        f"owner:{first.owner_user_id}",
                        f"owner:{second.owner_user_id}",
                    },
                )
                self.assertIn(
                    f"account-user:{first.reference.account_user_id}",
                    merged.identity_keys,
                )

                navigate(app, "calendar")
                self.assertFalse(app.exception)
                labels = {
                    item.label
                    for item in (
                        *app.get("link_button"),
                        *app.get("page_link"),
                    )
                }
                self.assertIn("\u00c5bn spilinfo", labels)
                self.assertIn("Officiel gruppe", labels)

                navigate(
                    app,
                    "group",
                    group="cached-manager-routes",
                )
                self.assertFalse(app.exception)
                self.assertTrue(
                    any(
                        item.value == "Rundens historie"
                        for item in app.subheader
                    )
                )

    def test_swiss_pairing_conflict_is_visible_without_rewriting_pairings(self) -> None:
        teams = tuple(
            sample_team(
                team_id,
                name=f"Swiss {team_id}",
                current_round=1,
                total=1000 - team_id,
                change=100 - team_id,
            )
            for team_id in range(811, 815)
        )
        with website_environment() as (config, output):
            store = holdet.GroupStore(config / "groups.json")
            members = tuple(
                holdet.GroupTeam(
                    team.reference.team_id,
                    team.team_name,
                    team.reference.source_url,
                    team.reference.account_key,
                    team.reference.account_label,
                    team.reference.account_user_id,
                    team.reference.profile_url,
                )
                for team in teams
            )
            group = store.create_tournament(
                "Swiss konflikt",
                teams[0].reference.game,
                members,
                start_round=1,
                final_round=2,
                rounds_per_tie=1,
                group_id="swiss-conflict-ui",
                template="swiss",
                definition_options={
                    "seed_rule": "manual",
                    "seed_order": tuple(
                        team.reference.team_id for team in teams
                    ),
                    "swiss_rounds": 2,
                },
            )
            snapshot_store = holdet.SnapshotStore(output)
            for team in teams:
                snapshot_store.save_team_json(team)
            index = snapshot_store.scan()
            state = holdet.build_tournament_state(group, index, 1)
            expected = holdet.generate_swiss_pairings(
                holdet.build_swiss_participants(
                    group.tournament,
                    state.group_matches,
                ),
                2,
            )
            ids = tuple(team.reference.team_id for team in teams)
            alternatives = (
                ((ids[0], ids[1]), (ids[2], ids[3])),
                ((ids[0], ids[2]), (ids[1], ids[3])),
                ((ids[0], ids[3]), (ids[1], ids[2])),
            )
            expected_pairs = {
                frozenset((item.team_a_id, item.team_b_id))
                for item in expected
            }
            wrong = next(
                candidate
                for candidate in alternatives
                if {frozenset(pair) for pair in candidate}
                != expected_pairs
            )
            paths = holdet.resolve_paths(
                overrides=holdet.PathOverrides(data_root=config.parent),
                environ={},
            )
            pairing_store = holdet.TournamentPairingStore(
                paths.tournament_pairing_dir
            )
            pairing_store.save(
                holdet.TournamentPairingRevision(
                    group.group_id,
                    group.active_revision,
                    tuple(
                        holdet.TournamentPairing(2, first, second)
                        for first, second in wrong
                    ),
                )
            )
            before = tuple(
                path.read_bytes()
                for path in paths.tournament_pairing_dir.rglob("*.json")
            )

            app = AppTest.from_file(APP_PATH).run(timeout=15)
            navigate(app, "group", group=group.group_id)

            after = tuple(
                path.read_bytes()
                for path in paths.tournament_pairing_dir.rglob("*.json")
            )
            self.assertFalse(app.exception)
            self.assertTrue(
                any(
                    "afviger fra de korrigerede resultater" in item.value
                    for item in app.warning
                )
            )
            self.assertEqual(after, before)

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
            self.assertIn("Mine managerspil", [item.value for item in app.title])
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
                    "Grupper · 0",
                    "Spillerstatistik",
                    "Statusalarmer · 0",
                    "Holdstatistik",
                    "Historik",
                    "Analyse",
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
                self.assertIn("Mine managerspil", [item.value for item in app.title])
                self.assertEqual(client_type.mock_calls, [])

                add_label = "Tilf" + chr(0xf8) + "j managerspil"
                add_buttons = [item for item in app.button if item.label == add_label]
                self.assertEqual(len(add_buttons), 2)
                navigate(app, "manage-games")
                widget(app, "text_input", "Holdet-URL eller slug").input(
                    "super-manager-fall-2026"
                )
                widget(app, "text_input", "Navn (valgfrit)").input(
                    "Superliga Efter" + chr(0xe5) + "r 2026"
                )
                add = "Tilf" + chr(0xf8) + "j managerspil"
                button(app, add).click().run(timeout=15)

                self.assertFalse(app.exception)
                configuration = holdet.GroupStore(
                    config / "groups.json"
                ).load_configuration()
                navigate(
                    app,
                    "game",
                    locale=configuration.games[0].game.locale,
                    game=configuration.games[0].game.slug,
                )
                self.assertTrue(
                    any(
                        item.value == "Superliga Efter" + chr(0xe5) + "r 2026"
                        for item in app.title
                    )
                )
                self.assertEqual(client_type.mock_calls, [])
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
            self.assertEqual([item.label for item in app.tabs], [])
            area = next(item for item in app.selectbox if item.label == "Område")
            self.assertEqual(area.value, "accounts")
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

    def test_data_storage_areas_and_legacy_deeplinks_are_read_only(self) -> None:
        with website_environment() as (config, _output):
            root = config.parent
            app = AppTest.from_file(APP_PATH).run(timeout=15)
            expected = {
                "overview": "overview",
                "exports": "exports",
                "import": "import",
                "integrity": "integrity",
                "api": "api",
                "accounts": "accounts",
                "quality": "overview",
                "locations": "accounts",
                "backup": "import",
            }
            for requested, selected in expected.items():
                with self.subTest(section=requested):
                    navigate(app, "data", section=requested)
                    self.assertFalse(app.exception)
                    area = next(item for item in app.selectbox if item.label == "Område")
                    self.assertEqual(area.value, selected)
            self.assertFalse((root / "exports").exists())
            self.assertFalse((root / "data" / "integrity-index.json").exists())

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
            group_store = holdet.GroupStore(config / "groups.json")
            manager_game = group_store.create_manager_game(
                first.reference.game, "Tourspillet"
            )
            group = group_store.create(
                "Tour venner",
                first.reference.game,
                (
                    holdet.GroupTeam(1, first.team_name, first.reference.source_url),
                    holdet.GroupTeam(2, second.team_name, second.reference.source_url),
                ),
                group_id="tour-venner",
            )
            refresh_milestone = datetime.now().astimezone()

            class FakeClient:
                def __init__(self):
                    self.calls: list[tuple[str, int | None]] = []

                def fetch_game_info(self, game):
                    self.calls.append(("metadata", None))
                    return holdet.GameContext(
                        game,
                        "cycling",
                        "classic",
                        7,
                        None,
                        None,
                        50_000_000,
                        3,
                        "Tourspillet",
                        (
                            holdet.ScheduleRound(
                                3,
                                refresh_milestone - timedelta(hours=2),
                                refresh_milestone - timedelta(hours=1),
                                refresh_milestone,
                            ),
                        ),
                    )

                def fetch_players(self, _game):
                    self.calls.append(("players", None))
                    return replace(
                        sample_statistics(3), round_status="complete"
                    )

                def fetch_team(self, reference):
                    self.calls.append(("team", reference.team_id))
                    if reference.team_id == 2:
                        raise holdet.FetchError("testfejl")
                    return first

            client = FakeClient()
            with patch("holdet_lib.HoldetClient", return_value=client):
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                navigate(app, "game", locale=first.reference.game.locale, game=first.reference.game.slug)
                button(app, "Opdater managerspil").click().run(timeout=20)
                self.assertEqual(len(app.get("dialog")), 1)
                self.assertEqual(client.calls, [])
                self.assertFalse(
                    list((output.parent / "manifests").rglob("refresh-round*.json"))
                )
                preview = next(
                    item.value
                    for item in app.dataframe
                    if "Datakilde" in item.value.columns
                )
                self.assertEqual(
                    preview["Datakilde"].tolist(),
                    [
                        "Spilinfo, regler og tidsplan",
                        "Spillere",
                        "Hold: Alpha",
                        "Hold: Beta",
                        "Efterbehandling",
                    ],
                )
                self.assertFalse(button(app, "Start opdatering").disabled)

                # AppTest.from_function materializes a temporary script. Keep
                # that harness inside the test-owned HOLDET_DATA_DIR as well.
                with patch(
                    "streamlit.testing.v1.app_test.TMP_DIR",
                    SimpleNamespace(name=str(config.parent / "temp")),
                ):
                    dialog = AppTest.from_function(
                        _refresh_dialog_test_app,
                        args=(manager_game, (group,)),
                        default_timeout=30,
                    ).run(timeout=20)
                button(dialog, "Start opdatering").click().run(timeout=30)
                self.assertFalse(dialog.exception)
                self.assertEqual(
                    client.calls,
                    [
                        ("metadata", None),
                        ("players", None),
                        ("team", 1),
                        ("team", 2),
                    ],
                )
                manifests = list(
                    (
                        output.parent
                        / "manifests"
                        / f"{first.reference.game.locale}--{first.reference.game.slug}"
                        / "game"
                    ).glob("refresh-round*.json")
                )
                self.assertEqual(len(manifests), 1)
                manifest = holdet.ManifestStore(
                    output.parent / "manifests"
                ).load(manifests[0])
                self.assertEqual(manifest.schema_version, 2)
                self.assertEqual(
                    [(item.step_id, item.status) for item in manifest.steps],
                    [
                        ("metadata", "fetched"),
                        ("players", "fetched"),
                        ("team:1", "fetched"),
                        ("team:2", "reused_after_error"),
                        ("postprocess", "fetched"),
                    ],
                )

                navigate(app, "group", group=group.group_id)
                self.assertFalse(app.exception)
                self.assertTrue(any(item.value == group.name for item in app.title))
                self.assertEqual({item.value for item in app.segmented_control}, {"Samlet"})
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

    def test_round_center_refresh_preview_is_cache_only_and_noop_when_current(
        self,
    ) -> None:
        with website_environment() as (config, output):
            current = datetime.now().astimezone()
            team = sample_team(41, name="Aktuelt hold", current_round=3)
            group_store = holdet.GroupStore(config / "groups.json")
            manager_game = group_store.create_manager_game(
                team.reference.game, "Aktuelt spil"
            )
            group_store.create(
                "Aktuel gruppe",
                team.reference.game,
                (
                    holdet.GroupTeam(
                        team.reference.team_id,
                        team.team_name,
                        team.reference.source_url,
                    ),
                ),
                group_id="aktuel-gruppe",
            )
            holdet.SnapshotStore(output).save_team_json(team, now=current)
            holdet.PlayerStatisticsStore(output).save(
                replace(sample_statistics(3), round_status="complete"),
                now=current,
            )
            holdet.GameMetadataStore(output.parent / "game-metadata").save(
                holdet.GameContext(
                    manager_game.game,
                    "cycling",
                    "classic",
                    7,
                    None,
                    None,
                    50_000_000,
                    4,
                    "Aktuelt spil",
                    (
                        holdet.ScheduleRound(
                            3,
                            current - timedelta(days=3),
                            current - timedelta(days=2),
                            current - timedelta(days=1),
                        ),
                        holdet.ScheduleRound(
                            4,
                            current + timedelta(days=1),
                            current + timedelta(days=2),
                            current + timedelta(days=3),
                        ),
                    ),
                ),
                fetched_at=current,
            )
            manifest_dir = output.parent / "manifests"

            with patch("holdet_lib.HoldetClient") as client_type:
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                navigate(
                    app,
                    "game",
                    locale=manager_game.game.locale,
                    game=manager_game.game.slug,
                )
                button(app, "Opdater managerspil").click().run(timeout=20)

                self.assertFalse(app.exception)
                self.assertEqual(len(app.get("dialog")), 1)
                preview = next(
                    item.value
                    for item in app.dataframe
                    if "Datakilde" in item.value.columns
                )
                self.assertEqual(
                    preview[["Datakilde", "Handling"]].to_dict("records"),
                    [
                        {
                            "Datakilde": "Spilinfo, regler og tidsplan",
                            "Handling": "Cache genbruges",
                        },
                        {
                            "Datakilde": "Spillere",
                            "Handling": "Cache genbruges",
                        },
                        {
                            "Datakilde": "Hold: Aktuelt hold",
                            "Handling": "Cache genbruges",
                        },
                        {
                            "Datakilde": "Efterbehandling",
                            "Handling": "Ikke nødvendig",
                        },
                    ],
                )
                self.assertTrue(button(app, "Start opdatering").disabled)
                self.assertTrue(
                    any(
                        "Der oprettes ikke et manifest" in item.value
                        for item in app.info
                    )
                )
                self.assertEqual(client_type.mock_calls, [])
                self.assertFalse(manifest_dir.exists())

                button(app, "Luk").click().run(timeout=15)
                self.assertEqual(client_type.mock_calls, [])
                self.assertFalse(manifest_dir.exists())

    def test_round_center_time_machine_is_read_only_and_network_free(self) -> None:
        with website_environment() as (config, output):
            first = sample_team(
                51,
                name="Historisk Alpha",
                current_round=3,
                history_rounds=(3, 2),
            )
            second = sample_team(
                52,
                name="Historisk Beta",
                current_round=3,
                history_rounds=(3, 2),
            )
            group_store = holdet.GroupStore(config / "groups.json")
            manager_game = group_store.create_manager_game(
                first.reference.game, "Historisk spil"
            )
            group_store.create(
                "Historisk gruppe",
                manager_game.game,
                (
                    holdet.GroupTeam(
                        first.reference.team_id,
                        first.team_name,
                        first.reference.source_url,
                    ),
                    holdet.GroupTeam(
                        second.reference.team_id,
                        second.team_name,
                        second.reference.source_url,
                    ),
                ),
                group_id="historisk-gruppe",
            )
            snapshots = holdet.SnapshotStore(output)
            snapshots.save_team_json(first)
            snapshots.save_team_json(second)
            players = holdet.PlayerStatisticsStore(output)
            players.save(replace(sample_statistics(2), round_status="complete"))
            players.save(replace(sample_statistics(3), round_status="complete"))

            with patch("holdet_lib.HoldetClient") as client_type:
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                navigate(
                    app,
                    "game",
                    locale=manager_game.game.locale,
                    game=manager_game.game.slug,
                )
                selector = widget(app, "selectbox", "Vis Rundecenter")
                self.assertIn("Runde 2 · senest korrigeret", selector.options)
                root = config.parent
                before = {
                    path.relative_to(root): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                }

                selector.set_value(2).run(timeout=20)

                self.assertFalse(app.exception)
                self.assertEqual(
                    widget(app, "selectbox", "Vis Rundecenter").value, 2
                )
                self.assertEqual(app.query_params["round"], ["2"])
                self.assertTrue(
                    any(
                        "Runde 2 er rekonstrueret med seneste rettelser"
                        in item.value
                        for item in app.info
                    )
                )
                self.assertTrue(
                    any(
                        item.label == "Runde" and item.value == "2"
                        for item in app.metric
                    )
                )
                self.assertFalse(
                    any(
                        item.label in {
                            "Opdater managerspil",
                            "Hent spilinfo og data",
                            "Start opdatering",
                        }
                        for item in app.button
                    )
                )
                after = {
                    path.relative_to(root): path.read_bytes()
                    for path in root.rglob("*")
                    if path.is_file()
                }
                self.assertEqual(after, before)
                self.assertEqual(client_type.mock_calls, [])

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

    def test_analysis_player_and_alert_routes_are_cache_only_and_edit_explicitly(self) -> None:
        class OfflineClient:
            def __init__(self):
                self.calls = 0

            def fetch_players(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("Navigation må ikke hente data")

            def fetch_team(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("Navigation må ikke hente data")

        client = OfflineClient()
        with website_environment() as (config, output):
            statistics = sample_statistics()
            game = statistics.game
            holdet.GroupStore(config / "groups.json").create_manager_game(
                game, "Tourspillet"
            )
            holdet.PlayerStatisticsStore(output).save(statistics)
            settings_path = config / "hub-settings.json"
            with patch("holdet_lib.HoldetClient", return_value=client):
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                navigate(app, "game", locale=game.locale, game=game.slug)
                select_game_tab(app, game, "Analyse")
                self.assertFalse(app.exception)
                self.assertEqual(client.calls, 0)
                self.assertFalse(settings_path.exists())
                self.assertTrue(
                    any(item.value == "Analyse og beslutninger" for item in app.header)
                )
                self.assertTrue(
                    any(item.label == "Analyseområde" for item in app.segmented_control)
                )

                player = statistics.entries[0]
                player_key = holdet.player_identity(game, player)
                navigate(
                    app,
                    "player",
                    locale=game.locale,
                    game=game.slug,
                    player=player_key,
                    round=statistics.round_number,
                )
                self.assertFalse(app.exception)
                self.assertEqual([item.value for item in app.title], [player.name])
                self.assertEqual(client.calls, 0)
                widget(app, "text_area", "Note").input("Hold øje med rollen")
                widget(app, "text_input", "Tags, adskilt med komma").input(
                    "overvej, langsigtet"
                )
                button(app, "Gem note og tags").click().run(timeout=15)
                saved = holdet.HubSettingsStore(settings_path).load()
                self.assertEqual(saved.player_annotations[0].note, "Hold øje med rollen")
                self.assertEqual(
                    saved.player_annotations[0].tags,
                    ("overvej", "langsigtet"),
                )

                navigate(
                    app,
                    "game",
                    locale=game.locale,
                    game=game.slug,
                    section="alerts",
                )
                self.assertFalse(app.exception)
                self.assertEqual([item.value for item in app.title], ["Tourspillet"])
                self.assertTrue(
                    any(item.value == "Statusalarmer" for item in app.header)
                )
                self.assertFalse(
                    any(item.label == "Managerspil" for item in app.selectbox)
                )
                self.assertEqual(client.calls, 0)

    def test_manager_alerts_are_scoped_badged_and_clear_only_current_game(self) -> None:
        with website_environment() as (config, output):
            statistics = sample_statistics()
            game = statistics.game
            other_game = holdet.normalize_manager_game(
                "other-alert-game", "Andet spil"
            )
            game_store = holdet.GroupStore(config / "groups.json")
            game_store.create_manager_game(game, "Tourspillet")
            game_store.create_manager_game(other_game.game, other_game.name)
            holdet.PlayerStatisticsStore(output).save(statistics)

            player = statistics.entries[0]
            player_key = holdet.player_identity(game, player)
            settings_store = holdet.HubSettingsStore(config / "hub-settings.json")
            settings_store.set_watchlist(
                holdet.HubSettings(),
                (holdet.watchlist_entry(game, player),),
            )
            detected = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
            current_unread = holdet.WatchlistAlert(
                alert_id="current-unread",
                game_locale=game.locale.casefold(),
                game_slug=game.slug,
                player_key=player_key,
                player_name=player.name,
                kind="injured",
                message=f"{player.name} er blevet markeret som skadet.",
                detected_at=detected,
                round_number=statistics.round_number,
            )
            current_dismissed = replace(
                current_unread,
                alert_id="current-dismissed",
                kind="disabled",
                dismissed_at=detected,
                read_at=detected,
            )
            other_dismissed = replace(
                current_dismissed,
                alert_id="other-dismissed",
                game_locale=other_game.game.locale.casefold(),
                game_slug=other_game.game.slug,
            )
            inbox = holdet.AnalysisInboxStore(config / "analysis-inbox.json")
            inbox.save((current_unread, current_dismissed, other_dismissed))

            app = AppTest.from_file(APP_PATH).run(timeout=15)
            sidebar_labels = [item.label for item in app.button]
            self.assertIn("Tourspillet (1 ulæste)", sidebar_labels)
            self.assertNotIn("Statusalarmer", sidebar_labels)

            navigate(app, "alerts")
            self.assertFalse(app.exception)
            self.assertNotIn("view", app.query_params)
            self.assertEqual(app.query_params["section"], ["alerts"])
            self.assertEqual([item.value for item in app.title], ["Tourspillet"])
            self.assertTrue(
                any(item.value == "Statusalarmer" for item in app.header)
            )
            self.assertIn("Statusalarmer · 1", [item.label for item in app.tabs])
            self.assertFalse(
                any(item.label == "Managerspil" for item in app.selectbox)
            )
            self.assertTrue(
                any(
                    item.label == "Administrér watchlist"
                    for item in app.get("page_link")
                )
            )
            self.assertTrue(
                any(item.label == "Se spiller" for item in app.get("link_button"))
            )

            button(app, "Markér som læst").click().run(timeout=15)
            self.assertIn("Statusalarmer · 1", [item.label for item in app.tabs])
            self.assertNotIn(
                "Tourspillet (1 ulæste)", [item.label for item in app.button]
            )
            button(app, "Ryd afviste alarmer").click().run(timeout=15)
            self.assertEqual(
                {item.alert_id for item in inbox.load()},
                {"current-unread", "other-dismissed"},
            )

    def test_hidden_alert_route_supports_non_manager_games_and_inbox_errors(self) -> None:
        with website_environment() as (config, output):
            statistics = sample_statistics()
            game = statistics.game
            holdet.PlayerStatisticsStore(output).save(statistics)
            player = statistics.entries[0]
            player_key = holdet.player_identity(game, player)
            holdet.AnalysisInboxStore(config / "analysis-inbox.json").save(
                (
                    holdet.WatchlistAlert(
                        alert_id="standalone-alert",
                        game_locale=game.locale.casefold(),
                        game_slug=game.slug,
                        player_key=player_key,
                        player_name=player.name,
                        kind="injured",
                        message=f"{player.name} er blevet markeret som skadet.",
                        detected_at=datetime(2026, 8, 7, 12, tzinfo=timezone.utc),
                        round_number=statistics.round_number,
                    ),
                )
            )
            app = AppTest.from_file(APP_PATH).run(timeout=15)
            navigate(
                app,
                "alerts",
                locale=game.locale,
                game=game.slug,
            )
            self.assertFalse(app.exception)
            self.assertEqual([item.value for item in app.title], ["Statusalarmer"])
            self.assertFalse(
                any(item.label == "Managerspil" for item in app.selectbox)
            )

            navigate(
                app,
                "players",
                locale=game.locale,
                game=game.slug,
                panel="compare",
            )
            self.assertFalse(app.exception)
            self.assertEqual(
                widget(app, "selectbox", "Spil eller Holdet-URL").value,
                game.original,
            )

            navigate(
                app,
                "player",
                locale=game.locale,
                game=game.slug,
                player=player_key,
                round=statistics.round_number,
            )
            self.assertFalse(app.exception)
            self.assertEqual([item.value for item in app.title], [player.name])

            (config / "analysis-inbox.json").write_text(
                "{not valid json", encoding="utf-8"
            )
            navigate(
                app,
                "alerts",
                locale=game.locale,
                game=game.slug,
            )
            self.assertFalse(app.exception)
            self.assertTrue(
                any("Alarmindbakken kunne ikke læses" in item.value for item in app.error)
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
            player_game = widget(app, "selectbox", "Spil eller Holdet-URL")
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
            team_game = widget(app, "selectbox", "Spil eller Holdet-URL")
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

    def test_scouting_route_is_cache_only_and_keeps_navigation_side_effect_free(self) -> None:
        class OfflineClient:
            def __init__(self):
                self.calls = 0

            def fetch_players(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("scouting-navigation må ikke kontakte Holdet")

        with website_environment() as (config, output):
            base = sample_statistics(variant="soccer")
            players = tuple(
                replace(
                    base.entries[0],
                    source_index=index,
                    entry_id=10_000 + index,
                    person_id=20_000 + index,
                    name=f"Scout {index}",
                    position="Angriber",
                    value=4_000_000 + index * 250_000,
                    popularity=float(index * 5),
                    is_active=True,
                    is_disabled=False,
                    is_injured=False,
                    has_suspension=False,
                )
                for index in range(6)
            )
            store = holdet.PlayerStatisticsStore(output)
            for round_number in range(1, 6):
                store.save(
                    replace(
                        base,
                        round_number=round_number,
                        entries=tuple(
                            replace(
                                item,
                                round_growth=round_number * (item.source_index + 1),
                            )
                            for item in players
                        ),
                        round_status="complete",
                    ),
                    now=datetime(2026, 8, round_number, tzinfo=timezone.utc),
                )
            before = {
                path.relative_to(config.parent): path.read_bytes()
                for path in config.parent.rglob("*")
                if path.is_file()
            }
            client = OfflineClient()
            with patch("holdet_lib.HoldetClient", return_value=client):
                app = AppTest.from_file(APP_PATH).run(timeout=15)
                navigate(
                    app,
                    "scouting",
                    locale=base.game.locale,
                    game=base.game.slug,
                    view="smartlists",
                )
            self.assertFalse(app.exception)
            self.assertEqual([item.value for item in app.title], ["Scouting"])
            self.assertTrue(any(item.value == "Smartlister" for item in app.segmented_control))
            self.assertTrue(app.dataframe)
            self.assertEqual(client.calls, 0)
            after = {
                path.relative_to(config.parent): path.read_bytes()
                for path in config.parent.rglob("*")
                if path.is_file()
            }
            self.assertEqual(after, before)
    def test_streamlit_config_and_readme_use_short_local_command(self) -> None:
        config = tomllib.loads(
            (PROJECT_ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(config["server"]["address"], "127.0.0.1")
        self.assertNotIn("port", config["server"])
        self.assertEqual(config["client"]["toolbarMode"], "viewer")
        app_source = UI_PATH.read_text(encoding="utf-8")
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
        command = "py -3.14 -m streamlit run .\\website\\server.py"
        self.assertIn(command, readme)
        self.assertNotIn("--server.address", readme)
        self.assertNotIn("--server.port", readme)
        self.assertIn("http://localhost:8501", readme)


if __name__ == "__main__":
    unittest.main()
