from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

import holdet_lib as scraper
from cli.main import main as cli_main


FICTIONAL_ACCOUNT_KEY = "nordlys-konto"
FICTIONAL_ACCOUNT_LABEL = "Nordlysmanager"
FICTIONAL_SECOND_ACCOUNT_KEY = "maane-konto"
FICTIONAL_SECOND_ACCOUNT_LABEL = "Månemanager"
FICTIONAL_USER_ID = 900_000_000_001
FICTIONAL_SECOND_USER_ID = 900_000_000_002
FICTIONAL_TEAM_ID = 800_000_000_001
FICTIONAL_TEAM_NAME = "Nordlysholdet"


def flight_html(*records: object) -> str:
    text = "\n".join(
        f"{index:x}:{json.dumps(record, ensure_ascii=False, separators=(',', ':'))}"
        for index, record in enumerate(records, 1)
    )
    push = json.dumps([1, text + "\n"], ensure_ascii=False)
    return f"<html><script>self.__next_f.push({push})</script></html>"


def variant_html(variant: str) -> str:
    return flight_html({"variant": variant})


def roster_player(
    player_id: int,
    name: str,
    price: int,
    *,
    points: int = 0,
    captain: bool = False,
    disabled: bool = False,
) -> dict[str, object]:
    first, _, last = name.partition(" ")
    return {
        "id": player_id,
        "person": {"firstName": first, "lastName": last},
        "team": {"name": "Team Ægir"},
        "position": {"name": "Rytter"},
        "price": price,
        "points": points,
        "growth": 25,
        "earnings": 125,
        "sinceRound": 2,
        "isCaptain": captain,
        "isActive": not disabled,
        "isDisabled": disabled,
        "hasInjury": disabled,
        "hasSuspension": disabled,
    }


def team_page_html(*, golf: bool = False) -> str:
    players = [
        roster_player(10, "Søren Ægir", 100, points=12, captain=True),
        roster_player(11, "Béla Velo", 200, points=8, disabled=True),
    ]
    header = [
        "$",
        "div",
        None,
        {
            "children": [
                [
                    "$",
                    "div",
                    None,
                    {
                        "className": "md:text-lg flex font-bold",
                        "children": [FICTIONAL_TEAM_NAME, None],
                    },
                ],
                {
                    "coreUserId": str(FICTIONAL_USER_ID),
                    "displayName": FICTIONAL_ACCOUNT_LABEL,
                    "children": FICTIONAL_ACCOUNT_LABEL,
                },
            ]
        },
    ]
    substitutions = [
        "$",
        "div",
        None,
        {
            "children": [
                ["$", "div", None, {"children": "Udskiftninger"}],
                [
                    "$",
                    "span",
                    None,
                    {
                        "children": [
                            1,
                            ["$", "span", None, {"children": ["/", 8]}],
                        ]
                    },
                ],
            ]
        },
    ]
    top = [
        "$",
        "div",
        None,
        {
            "children": [
                ["$", "div", None, {"children": "Top"}],
                ["$", "div", None, {"children": "21%"}],
            ]
        },
    ]
    roster = [
        "$",
        "Roster",
        None,
        {"roundHeading": "Runde 3", "players": players, "golf": golf},
    ]
    return flight_html(header, substitutions, top, roster)


def history_payload(*, points: bool = False) -> dict[str, object]:
    items = []
    for round_number, total, change, bank in (
        (2, 320, 20, 20),
        (3, 350, 30, 50),
    ):
        assets = {
            "value": total,
            "change": change,
            "balance": bank,
            "balanceChange": 5,
            "playerChange": 15,
            "interest": 2,
            "transfer": 1,
            "captainBonus": 10,
            "specialBonus": 3,
        }
        point_data = {
            "value": 20 if round_number == 3 else 12,
            "change": 8,
            "playerChange": 5,
            "captainBonus": 2,
            "specialBonus": 1,
        }
        items.append(
            {
                "round": round_number,
                "assets": assets,
                "points": point_data,
                "totalSubstitutionsUsed": round_number,
            }
        )
    return {"items": items, "pageInfo": {"hasNextPage": False}}


def cartridge_payload(*, salary_cap: int = 500) -> dict[str, object]:
    return {
        "gameId": 7,
        "defaultFantasyLeagueId": 99,
        "_embedded": {
            "games": {"7": {"id": 7, "rulesetId": 8}},
            "rulesets": {"8": {"id": 8, "salaryCap": salary_cap}},
        },
    }


@contextmanager
def writable_test_directory():
    root = Path(__file__).parent / f"_test-team-output-{uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


class AccountAndDiscoveryTests(unittest.TestCase):
    @staticmethod
    def fictional_account() -> scraper.AccountConfig:
        return scraper.AccountConfig(
            FICTIONAL_ACCOUNT_KEY,
            FICTIONAL_ACCOUNT_LABEL,
            f"https://www.holdet.dk/da/users/{FICTIONAL_USER_ID}/teams",
            FICTIONAL_USER_ID,
        )

    def test_loads_accounts_and_selects_key_label_or_id(self) -> None:
        payload = {
            "accounts": [
                {
                    "key": FICTIONAL_ACCOUNT_KEY,
                    "label": FICTIONAL_ACCOUNT_LABEL,
                    "profile_url": (
                        f"https://www.holdet.dk/da/users/{FICTIONAL_USER_ID}/teams"
                    ),
                },
                {
                    "key": FICTIONAL_SECOND_ACCOUNT_KEY,
                    "label": FICTIONAL_SECOND_ACCOUNT_LABEL,
                    "profile_url": (
                        "https://www.holdet.dk/da/users/"
                        f"{FICTIONAL_SECOND_USER_ID}/teams"
                    ),
                },
            ]
        }
        with writable_test_directory() as temporary:
            accounts_path = temporary / "accounts.json"
            accounts_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            accounts = scraper.load_accounts(accounts_path)

        self.assertEqual(len(accounts), 2)
        self.assertEqual(accounts[0].user_id, FICTIONAL_USER_ID)
        self.assertEqual(
            scraper.select_accounts(accounts, [FICTIONAL_ACCOUNT_LABEL]),
            (accounts[0],),
        )
        self.assertEqual(
            scraper.select_accounts(accounts, [str(FICTIONAL_SECOND_USER_ID)]),
            (accounts[1],),
        )
        with self.assertRaisesRegex(scraper.PayloadError, "Ukendte kontovalg"):
            scraper.select_accounts(accounts, ["missing"])

    def test_discovers_flight_and_me_links_without_duplicates(self) -> None:
        account = self.fictional_account()
        href = (
            "/da/fantasy/tour-de-france-2026/me/fantasyteams/"
            f"{FICTIONAL_TEAM_ID}"
        )
        link = [
            "$",
            "a",
            None,
            {
                "href": href,
                "children": [
                    ["$", "div", None, {"children": [FICTIONAL_TEAM_NAME, None]}],
                    "11.626",
                ],
            },
        ]
        html = flight_html(link, link)
        game = scraper.normalize_game_url(
            "https://www.holdet.dk/da/fantasy/tour-de-france-2026"
        )
        teams = scraper.discover_profile_teams(html, account, game=game)
        self.assertEqual(len(teams), 1)
        self.assertEqual(teams[0].team_id, FICTIONAL_TEAM_ID)
        self.assertEqual(teams[0].team_name, FICTIONAL_TEAM_NAME)
        self.assertEqual(teams[0].account_key, FICTIONAL_ACCOUNT_KEY)

    def test_parses_direct_team_urls(self) -> None:
        direct = scraper.parse_direct_team_url(
            "https://www.holdet.dk/da/fantasy/tour-de-france-2026/"
            f"me/fantasyteams/{FICTIONAL_TEAM_ID}"
        )
        self.assertIsNotNone(direct)
        assert direct is not None
        self.assertEqual(direct.team_id, FICTIONAL_TEAM_ID)
        self.assertEqual(direct.game.slug, "tour-de-france-2026")


class TeamPayloadTests(unittest.TestCase):
    def test_extracts_full_salary_roster_and_metrics(self) -> None:
        page = scraper.extract_team_page(team_page_html(), salary_cap=True)
        self.assertEqual(page.team_name, FICTIONAL_TEAM_NAME)
        self.assertEqual(page.owner_name, FICTIONAL_ACCOUNT_LABEL)
        self.assertEqual(page.owner_user_id, FICTIONAL_USER_ID)
        self.assertEqual(page.current_round, 3)
        self.assertEqual(page.substitutions_remaining, 1)
        self.assertEqual(page.substitutions_limit, 8)
        self.assertEqual(page.top_percent, 21)
        self.assertEqual([player.value for player in page.roster], [100, 200])
        self.assertEqual(page.roster[0].role, "captain")
        self.assertEqual(
            page.roster[1].statuses,
            ("inactive", "disabled", "injured", "suspended"),
        )

    def test_uses_points_for_golf_and_supports_all_variant_policies(self) -> None:
        for variant in ("soccer", "cycling", "formula1"):
            with self.subTest(variant=variant):
                page = scraper.extract_team_page(team_page_html(), salary_cap=True)
                self.assertEqual(page.roster[0].value, 100)
        golf = scraper.extract_team_page(team_page_html(golf=True), salary_cap=False)
        self.assertEqual([player.value for player in golf.roster], [12, 8])

    def test_parses_asset_and_point_history_with_rank_movements(self) -> None:
        assets = scraper.parse_history(
            history_payload(),
            salary_cap=True,
            round_ranks={2: 20, 3: 15},
            overall_ranks={2: 100, 3: 80},
        )
        self.assertEqual(assets[0].round_number, 3)
        self.assertEqual(assets[0].player_value, 300)
        self.assertEqual(assets[0].round_rank_change, 5)
        self.assertEqual(assets[0].overall_rank_change, 20)
        points = scraper.parse_history(history_payload(points=True), salary_cap=False)
        self.assertEqual(points[0].total, 20)
        self.assertIsNone(points[0].bank)
        self.assertIsNone(points[0].player_value)


class TeamServiceAndOutputTests(unittest.TestCase):
    def make_service(self) -> tuple[scraper.TeamDataService, scraper.TeamReference]:
        game = scraper.normalize_game_url(
            "https://www.holdet.dk/da/fantasy/tour-de-france-2026"
        )
        reference = scraper.TeamReference(
            game=game,
            team_id=FICTIONAL_TEAM_ID,
            team_name=FICTIONAL_TEAM_NAME,
            source_url=(
                "https://www.holdet.dk/da/fantasy/tour-de-france-2026/"
                f"fantasyteams/{FICTIONAL_TEAM_ID}"
            ),
            account_key=FICTIONAL_ACCOUNT_KEY,
            account_label=FICTIONAL_ACCOUNT_LABEL,
            account_user_id=FICTIONAL_USER_ID,
        )
        text = {
            game.nexus_root_url: variant_html("cycling"),
            game.team_url("cycling", FICTIONAL_TEAM_ID): team_page_html(),
        }
        base = "https://nexus-app-fantasy.holdet.dk"
        json_data = {
            f"{base}/api/cartridges/{game.slug}": cartridge_payload(),
            f"{base}/api/fantasyteams/{FICTIONAL_TEAM_ID}/history": history_payload(),
            f"{base}/api/fantasyteams/{FICTIONAL_TEAM_ID}/fantasyleagues/99/leaderboards/overall": {
                "items": [{"round": 2, "rank": 100}, {"round": 3, "rank": 80}]
            },
            f"{base}/api/fantasyteams/{FICTIONAL_TEAM_ID}/fantasyleagues/99/leaderboards/round": {
                "items": [{"round": 2, "rank": 20}, {"round": 3, "rank": 15}]
            },
        }
        return (
            scraper.TeamDataService(
                text_fetcher=lambda url: text[url],
                json_fetcher=lambda url: json_data[url],
            ),
            reference,
        )

    def test_service_merges_current_roster_history_and_ranks(self) -> None:
        service, reference = self.make_service()
        team = service.scrape(reference)
        self.assertEqual(team.team_name, FICTIONAL_TEAM_NAME)
        self.assertEqual(team.overview.player_value, 300)
        self.assertEqual(team.overview.bank, 50)
        self.assertEqual(team.overview.rank, 80)
        self.assertEqual(team.overview.rank_change, 20)
        self.assertEqual(len(team.roster), 2)
        self.assertEqual(len(team.history), 2)

    def test_danish_text_json_and_paired_collision_names(self) -> None:
        service, reference = self.make_service()
        team = service.scrape(reference)
        fixed = datetime(2026, 7, 23, 12, 34, 56, 987654)
        with writable_test_directory() as temporary:
            document = scraper.build_team_export(
                team,
                generated_at=fixed.astimezone(),
                source_generated_at=fixed.astimezone(),
                roster_generated_at=fixed.astimezone(),
            )
            first = tuple(
                item.path
                for item in scraper.TeamExportStore(temporary).save(
                    document, ("txt", "json")
                )
            )
            second = tuple(
                item.path
                for item in scraper.TeamExportStore(temporary).save(
                    document, ("txt", "json")
                )
            )
            self.assertEqual(first[0].name, "team-round3_0723_123456.txt")
            self.assertEqual(first[1].suffix, ".json")
            self.assertEqual(second[0].name, "team-round3_0723_123456_1.txt")
            self.assertEqual(second[1].name, "team-round3_0723_123456_1.json")
            text = first[0].read_text(encoding="utf-8")
            data = json.loads(first[1].read_text(encoding="utf-8"))
            self.assertIn("Spillerværdi: 300", text)
            self.assertIn("Søren Ægir", text)
            self.assertEqual(data["schema_version"], 1)
            self.assertEqual(data["team"]["id"], FICTIONAL_TEAM_ID)
            self.assertEqual(data["history"][0]["overall_rank"], 80)

    def test_team_subcommand_accepts_direct_url(self) -> None:
        service, reference = self.make_service()
        with writable_test_directory() as temporary:
            exit_code = cli_main(
                [
                    "teams",
                    reference.source_url,
                    "--team",
                    str(FICTIONAL_TEAM_ID),
                    "--output-dir",
                    str(temporary / "exports"),
                    "--data-dir",
                    str(temporary / "appdata"),
                ],
                service_factory=lambda: service,
            )
            self.assertEqual(exit_code, 0)
            self.assertEqual(len(list((temporary / "exports").rglob("*.txt"))), 1)
            self.assertEqual(len(list((temporary / "exports").rglob("*.json"))), 1)
            self.assertEqual(
                len(list((temporary / "appdata" / "data" / "snapshots").rglob("*.json"))),
                1,
            )
            exported_json = next((temporary / "exports").rglob("*.json"))
            snapshot_json = next(
                (temporary / "appdata" / "data" / "snapshots").rglob("*.json")
            )
            exported = json.loads(exported_json.read_text(encoding="utf-8"))
            snapshot = json.loads(snapshot_json.read_text(encoding="utf-8"))
            self.assertEqual(exported["document_type"], "team_export")
            self.assertEqual(exported["team"]["id"], snapshot["team"]["id"])

    def test_team_cli_supports_markdown_and_round_scope(self) -> None:
        service, reference = self.make_service()
        with writable_test_directory() as temporary:
            exit_code = cli_main(
                [
                    "teams", reference.source_url, "--team", str(FICTIONAL_TEAM_ID),
                    "--round", "2", "--format", "md", "--format", "json",
                    "--output-dir", str(temporary / "exports"),
                    "--data-dir", str(temporary / "appdata"),
                ],
                service_factory=lambda: service,
            )
            self.assertEqual(exit_code, 0)
            markdown = next((temporary / "exports").rglob("*.md"))
            exported = json.loads(next((temporary / "exports").rglob("*.json")).read_text(encoding="utf-8"))
            self.assertIn("# Holdstatistik", markdown.read_text(encoding="utf-8"))
            self.assertEqual(exported["scope"], "round")
            self.assertEqual(exported["selected_round"], 2)
            self.assertFalse(exported["roster_available"])
            self.assertEqual(len(list((temporary / "appdata" / "data" / "snapshots").rglob("*.json"))), 1)
    def test_sanitizes_windows_names(self) -> None:
        self.assertEqual(scraper.sanitize_path_component("CON", fallback="team"), "team")
        self.assertEqual(
            scraper.sanitize_path_component('Nordlys: Hold / Test?', fallback="team"),
            "nordlys-hold-test",
        )


def schedule_payload(count: int = 3) -> dict[str, object]:
    return {
        "rounds": [
            {
                "round": round_number,
                "start": f"2026-08-{round_number:02d}T08:00:00Z",
                "close": f"2026-08-{round_number:02d}T10:00:00Z",
                "end": f"2026-08-{round_number:02d}T12:00:00Z",
            }
            for round_number in range(1, count + 1)
        ]
    }


class GameInfoTests(unittest.TestCase):
    def test_schedule_metadata_uses_structured_round_count(self) -> None:
        game = scraper.normalize_game_url(
            "https://www.holdet.dk/da/fantasy/super-manager-fall-2026"
        )
        cartridge = cartridge_payload()
        cartridge["_embedded"]["games"]["7"]["scheduleId"] = 623
        base = "https://nexus-app-fantasy.holdet.dk"
        calls: list[str] = []

        def fetch_json(url: str):
            calls.append(url)
            if url.endswith("/api/cartridges/super-manager-fall-2026"):
                return cartridge
            if url.endswith("/api/schedules/623"):
                return schedule_payload(17)
            raise AssertionError(url)

        def fetch_text(url: str) -> str:
            if url == game.original:
                return "<h1>Super Manager <span>Efter" + chr(0xe5) + "r 2026</span></h1>"
            return variant_html("soccer")

        client = scraper.HoldetClient(
            text_fetcher=fetch_text,
            json_fetcher=fetch_json,
        )
        info = client.fetch_game_info(game)
        self.assertEqual(info.schedule_id, 623)
        self.assertEqual(info.final_round, 17)
        self.assertEqual(len(info.rounds), 17)
        self.assertEqual(info.display_name, "Super Manager Efter" + chr(0xe5) + "r 2026")
        self.assertEqual(calls[-1], f"{base}/api/schedules/623")
        self.assertIs(client.fetch_game_info(game), info)
        self.assertEqual(len(calls), 2)

    def test_ruleset_policy_detects_units_aliases_and_conflicts(self) -> None:
        game = scraper.normalize_game_url(
            "https://www.holdet.dk/da/fantasy/tour-manager"
        )
        point_payload = cartridge_payload(salary_cap=0)
        point_payload["_embedded"]["rulesets"]["8"]["properties"] = {
            "Format": "cycling"
        }
        point_context = scraper.parse_game_context(
            game, "cycling_world_tour", point_payload
        )
        self.assertEqual(
            (
                point_context.variant,
                point_context.format,
                point_context.unit,
            ),
            ("cycling_world_tour", "cycling", "points"),
        )

        money_payload = cartridge_payload(salary_cap=50_000_000)
        money_payload["_embedded"]["rulesets"]["8"]["properties"] = {
            "Format": "cycling"
        }
        money_context = scraper.parse_game_context(
            game, "cycling", money_payload
        )
        self.assertEqual((money_context.format, money_context.unit), ("cycling", "money"))

        conflicting = cartridge_payload(salary_cap=0)
        conflicting["_embedded"]["rulesets"]["8"]["properties"] = {
            "Format": "golf"
        }
        with self.assertRaisesRegex(scraper.PayloadError, "i konflikt"):
            scraper.parse_game_context(game, "cycling", conflicting)

        missing_salary = cartridge_payload()
        del missing_salary["_embedded"]["rulesets"]["8"]["salaryCap"]
        with self.assertRaisesRegex(scraper.PayloadError, "salaryCap"):
            scraper.parse_game_context(game, "cycling", missing_salary)
    def test_schedule_parser_and_completion_boundary(self) -> None:
        rounds = scraper.parse_schedule_rounds(schedule_payload())
        self.assertEqual([item.round_number for item in rounds], [1, 2, 3])
        before = datetime(2026, 8, 2, 11, 59, tzinfo=timezone.utc)
        boundary = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(
            scraper.round_status_for(rounds, 2, at=before)[0],
            "in_progress",
        )
        status, end_at = scraper.round_status_for(rounds, 2, at=boundary)
        self.assertEqual(status, "complete")
        self.assertEqual(end_at, boundary)
        self.assertEqual(
            scraper.round_status_for(rounds, 99, at=boundary),
            ("unknown", None),
        )

    def test_schedule_parser_rejects_empty_or_invalid_payloads(self) -> None:
        self.assertEqual(
            scraper.parse_schedule_final_round(schedule_payload()), 3
        )
        for payload in ({}, {"rounds": []}, {"rounds": [None]}, {"rounds": [{}]}):
            with self.subTest(payload=payload):
                with self.assertRaises(scraper.PayloadError):
                    scraper.parse_schedule_final_round(payload)


if __name__ == "__main__":
    unittest.main()
