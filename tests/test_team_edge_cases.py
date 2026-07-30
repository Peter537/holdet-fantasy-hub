from __future__ import annotations

import unittest

import holdet_lib as scraper


class TeamEdgeCaseTests(unittest.TestCase):
    def test_empty_pre_game_history_is_valid(self) -> None:
        self.assertEqual(
            scraper.parse_history(
                {"items": [], "pageInfo": {"hasNextPage": False}},
                salary_cap=True,
            ),
            (),
        )

    def test_history_rejects_missing_scoring_bucket(self) -> None:
        with self.assertRaisesRegex(scraper.PayloadError, "lacks assets"):
            scraper.parse_history(
                {"items": [{"round": 1, "points": {"value": 2}}]},
                salary_cap=True,
            )

    def test_direct_url_rejects_other_hosts(self) -> None:
        self.assertIsNone(
            scraper.parse_direct_team_url(
                "https://example.com/da/fantasy/game/fantasyteams/12"
            )
        )

    def test_team_filter_is_exact_case_insensitive(self) -> None:
        game = scraper.normalize_game_url(
            "https://www.holdet.dk/da/fantasy/tour-de-france-2026"
        )
        reference = scraper.TeamReference(
            game=game,
            team_id=1,
            team_name="Nordlysholdet",
            source_url="https://www.holdet.dk/da/fantasy/x/fantasyteams/1",
        )
        self.assertEqual(
            scraper.filter_team_references([reference], ["nordlysholdet"]),
            (reference,),
        )
        self.assertEqual(scraper.filter_team_references([reference], ["stjerneskud"]), ())


if __name__ == "__main__":
    unittest.main()
