from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime
import io
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

import holdet_lib as scraper
from cli.main import main as cli_main


FIXTURES = Path(__file__).parent / "fixtures"


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


@contextmanager
def writable_test_directory():
    root = Path(__file__).parent / f"_test-output-{uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


class UrlNormalizationTests(unittest.TestCase):
    def test_accepts_root_and_nested_holdet_urls(self) -> None:
        root = scraper.normalize_game_url(
            "https://www.holdet.dk/da/fantasy/super-manager-fall-2026"
        )
        nested = scraper.normalize_game_url(
            "https://holdet.dk/da/fantasy/super-manager-fall-2026/"
            "soccer/statistics?ignored=yes#fragment"
        )

        self.assertEqual(root.locale, "da")
        self.assertEqual(root.slug, "super-manager-fall-2026")
        self.assertEqual(nested.slug, root.slug)
        self.assertEqual(
            root.nexus_root_url,
            "https://nexus-app-fantasy.holdet.dk/da/super-manager-fall-2026",
        )

    def test_rejects_invalid_input_urls(self) -> None:
        invalid = (
            "http://www.holdet.dk/da/fantasy/game",
            "https://evil.example/da/fantasy/game",
            "https://www.holdet.dk/da/not-fantasy/game",
            "https://www.holdet.dk/da/fantasy/Bad-Slug",
            "super-manager-fall-2026",
        )
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(scraper.UrlValidationError):
                    scraper.normalize_game_url(value)


class PayloadTests(unittest.TestCase):
    def test_extracts_each_supported_variant(self) -> None:
        cases = {
            "soccer": ("soccer", 3, 2),
            "cycling": ("cycling", 18, 1),
            "formula1": ("formula1", 11, 1),
            "golf": ("golf", 14, 1),
        }
        for fixture_name, (variant, round_number, count) in cases.items():
            with self.subTest(variant=variant):
                html = fixture(fixture_name)
                self.assertEqual(scraper.discover_variant(html), variant)
                entries, actual_round = scraper.extract_entries_and_round(html)
                self.assertEqual(actual_round, round_number)
                self.assertEqual(len(entries), count)

    def test_accepts_pre_game_round_zero(self) -> None:
        html = fixture("soccer").replace('\\"round\\":3', '\\"round\\":0')
        entries, round_number = scraper.extract_entries_and_round(html)
        self.assertEqual(round_number, 0)
        self.assertEqual(len(entries), 2)

    def test_accepts_legacy_cycling_manager_variant(self) -> None:
        payload = '<script>self.__next_f.push([1,"{\\"variant\\":\\"cycling_world_tour\\"}"]);</script>'
        self.assertEqual(
            scraper.discover_variant(payload),
            "cycling_world_tour",
        )
        self.assertEqual(
            scraper.format_for_variant("cycling_world_tour"),
            "cycling",
        )

    def test_rejects_missing_or_empty_player_rows(self) -> None:
        missing = '<script>self.__next_f.push([1,"{\\"variant\\":\\"soccer\\"}"]);</script>'
        empty = '<script>self.__next_f.push([1,"{\\"rows\\":[],\\"round\\":1}"]);</script>'
        for payload in (missing, empty):
            with self.subTest(payload=payload):
                with self.assertRaises(scraper.PayloadError):
                    scraper.extract_entries_and_round(payload)


class FormattingTests(unittest.TestCase):
    def test_formats_all_four_variants(self) -> None:
        soccer_entries, _ = scraper.extract_entries_and_round(fixture("soccer"))
        cycling_entries, _ = scraper.extract_entries_and_round(fixture("cycling"))
        motor_entries, _ = scraper.extract_entries_and_round(fixture("formula1"))
        golf_entries, _ = scraper.extract_entries_and_round(fixture("golf"))

        self.assertEqual(
            scraper.format_entry(soccer_entries[0], "soccer"),
            "S\u00f8ren \u00c6gir (FC K\u00f8benhavn, Angreb): 9.500.000 "
            "[inactive] [disabled] [injured] [suspended]",
        )
        self.assertEqual(
            scraper.format_entry(cycling_entries[0], "cycling"),
            "Tadej Poga\u010dar (UAE Team Emirates - XRG): 21.827.000",
        )
        self.assertEqual(
            scraper.format_entry(
                cycling_entries[0],
                "cycling",
                unit="points",
                game_format="cycling",
            ),
            "Tadej Poga\u010dar (UAE Team Emirates - XRG, Rytter): 21.827.000 p.",
        )
        self.assertEqual(
            scraper.format_entry(motor_entries[0], "formula1"),
            "Mercedes W17 (Mercedes, Konstrukt\u00f8r): 12.560.000",
        )
        self.assertEqual(
            scraper.format_entry(golf_entries[0], "golf"),
            "Eugenio Chacarra (Spain, Kategori 2): 829 p.",
        )

    def test_formats_negative_integer_with_danish_grouping(self) -> None:
        self.assertEqual(scraper.format_integer(-1234567), "-1.234.567")


class SortingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.entries = (
            scraper.PlayerEntry(0, "Zulu", "Team B", "Y", 10),
            scraper.PlayerEntry(1, "Alpha", "Team A", "Z", 20),
            scraper.PlayerEntry(2, "Beta", "Team A", "X", 20),
        )

    def names(self, field: str, order: str) -> list[str]:
        return [
            entry.name for entry in scraper.sort_entries(self.entries, field, order)
        ]

    def test_value_name_team_position_and_source_sorting(self) -> None:
        expected = {
            ("value", "desc"): ["Alpha", "Beta", "Zulu"],
            ("value", "asc"): ["Zulu", "Alpha", "Beta"],
            ("name", "asc"): ["Alpha", "Beta", "Zulu"],
            ("name", "desc"): ["Zulu", "Beta", "Alpha"],
            ("team", "asc"): ["Alpha", "Beta", "Zulu"],
            ("team", "desc"): ["Zulu", "Alpha", "Beta"],
            ("position", "asc"): ["Beta", "Zulu", "Alpha"],
            ("position", "desc"): ["Alpha", "Zulu", "Beta"],
            ("source", "asc"): ["Zulu", "Alpha", "Beta"],
            ("source", "desc"): ["Beta", "Alpha", "Zulu"],
        }
        for (field, order), names in expected.items():
            with self.subTest(field=field, order=order):
                self.assertEqual(self.names(field, order), names)


class CliTests(unittest.TestCase):
    def test_batch_continues_and_returns_nonzero(self) -> None:
        output = io.StringIO()
        errors = io.StringIO()
        with patch(
            "cli.main._fetch_and_export_players",
            side_effect=[Path("created.txt"), scraper.FetchError("network down")],
        ) as mocked:
            with redirect_stdout(output), redirect_stderr(errors):
                exit_code = cli_main(
                    [
                        "players",
                        "https://www.holdet.dk/da/fantasy/first-game",
                        "https://www.holdet.dk/da/fantasy/second-game",
                    ]
                )

        self.assertEqual(exit_code, 1)
        self.assertEqual(mocked.call_count, 2)
        self.assertIn("created.txt", output.getvalue())
        self.assertIn("second-game", errors.getvalue())
        self.assertIn("network down", errors.getvalue())

    def test_non_value_sort_defaults_to_ascending(self) -> None:
        with patch(
            "cli.main._fetch_and_export_players", return_value=Path("created.txt")
        ) as mocked:
            with redirect_stdout(io.StringIO()):
                exit_code = cli_main(
                    ["players", "https://www.holdet.dk/da/fantasy/game", "--sort", "name"]
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(mocked.call_args.kwargs["sort_order"], "asc")


if __name__ == "__main__":
    unittest.main()
