from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

import holdet_lib as holdet
from cli.main import main as cli_main


FIXTURES = Path(__file__).parent / "fixtures"


@contextmanager
def writable_directory():
    root = Path(__file__).parent / f"_test-player-export-{uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


def statistics(
    *, variant: str = "soccer", unit: str | None = None
) -> holdet.ScrapedGame:
    game = holdet.GameUrl(
        "https://www.holdet.dk/da/fantasy/example-game", "da", "example-game"
    )
    entries = (
        holdet.PlayerEntry(0, "Alpha", "Team Æ", "Angreb", 6_000_000, total_growth=1_000_000, round_growth=100_000),
        holdet.PlayerEntry(1, "Beta", "Team B", "Forsvar", 5_000_000, is_disabled=True, total_growth=None, round_growth=-50_000),
        holdet.PlayerEntry(2, "Gamma", "Team B", "Angreb", 7_000_000, is_disabled=True, is_injured=True, total_growth=2_000_000, round_growth=None),
        holdet.PlayerEntry(3, "Delta", "Team D", "Målmand", 4_000_000, is_active=False, total_growth=-1_000, round_growth=0),
    )
    if unit is None:
        return holdet.ScrapedGame(game, variant, 7, entries)
    game_format = "cycling" if variant == "cycling_world_tour" else variant
    return holdet.ScrapedGame(game, variant, 7, entries, game_format, unit)


class PlayerFilterTests(unittest.TestCase):
    def test_combines_text_team_value_growth_and_status_filters(self) -> None:
        query = holdet.PlayerStatisticsQuery(
            search="team b",
            teams=("Team B",),
            min_value=4_500_000,
            max_value=5_500_000,
            missing_total_growth="only",
            status_rules=(("disabled", "require"), ("injured", "exclude")),
        )
        self.assertEqual(
            [entry.name for entry in holdet.filter_player_statistics(statistics(), query)],
            ["Beta"],
        )

    def test_required_statuses_are_all_required_and_exclusions_win(self) -> None:
        required = holdet.PlayerStatisticsQuery(
            status_rules=(("disabled", "require"), ("injured", "require"))
        )
        excluded = holdet.PlayerStatisticsQuery(
            status_rules=(("disabled", "require"), ("injured", "exclude"))
        )
        self.assertEqual(
            [entry.name for entry in holdet.filter_player_statistics(statistics(), required)],
            ["Gamma"],
        )
        self.assertEqual(
            [entry.name for entry in holdet.filter_player_statistics(statistics(), excluded)],
            ["Beta"],
        )

    def test_growth_sort_keeps_missing_values_last(self) -> None:
        query = holdet.PlayerStatisticsQuery(sort_field="round_growth", sort_order="asc")
        self.assertEqual(
            [entry.name for entry in holdet.filter_player_statistics(statistics(), query)],
            ["Beta", "Delta", "Alpha", "Gamma"],
        )

    def test_name_is_required_and_ranges_are_validated(self) -> None:
        with self.assertRaisesRegex(ValueError, "Navnekolonnen"):
            holdet.PlayerStatisticsQuery(columns=("value",))
        with self.assertRaisesRegex(ValueError, "Minimum for value"):
            holdet.PlayerStatisticsQuery(min_value=10, max_value=5)

    def test_dynamic_golf_labels_and_danish_statuses(self) -> None:
        rows, integer_columns = holdet.player_display_rows(
            statistics(variant="golf"),
            holdet.PlayerStatisticsQuery(columns=("name", "team", "position", "value", "total_growth", "round_growth", "status")),
        )
        self.assertEqual(
            tuple(rows[0]),
            ("Navn", "Land", "Kategori", "Point", "Totalændring", "Rundeændring", "Status"),
        )
        self.assertEqual(integer_columns, ("Point", "Totalændring", "Rundeændring"))
        beta = next(row for row in rows if row["Navn"] == "Beta")
        self.assertEqual(beta["Status"], "Deaktiveret")


class PlayerExportTests(unittest.TestCase):
    def test_all_formats_share_stem_collisions_and_exact_bytes(self) -> None:
        fixed = datetime(2026, 7, 29, 12, 34, 56).astimezone()
        query = holdet.PlayerStatisticsQuery(
            min_value=5_000_000,
            columns=("name", "value", "total_growth", "status"),
        )
        document = holdet.build_player_export(
            statistics(), query, generated_at=fixed, source_generated_at=fixed
        )
        with writable_directory() as root:
            store = holdet.PlayerExportStore(root)
            first = store.save(document, ("txt", "json", "md"))
            second = store.save(document, ("txt", "json", "md"))

            self.assertEqual(
                [item.path.name for item in first],
                [
                    "data-round7_0729_123456.txt",
                    "data-round7_0729_123456.json",
                    "data-round7_0729_123456.md",
                ],
            )
            self.assertTrue(all("_1." in item.path.name for item in second))
            for artifact in first:
                self.assertEqual(artifact.path.read_bytes(), artifact.content)
                self.assertEqual(
                    artifact.content,
                    holdet.serialize_player_export(document, artifact.format),
                )

            payload = json.loads(first[1].content)
            self.assertEqual(payload["schema_version"], 3)
            self.assertEqual(payload["game"]["format"], "soccer")
            self.assertEqual(payload["game"]["unit"], "money")
            self.assertEqual(payload["row_count"], 3)
            self.assertEqual(payload["columns"], ["name", "value", "total_growth", "status"])
            self.assertIsInstance(payload["rows"][0]["value"], int)
            text = first[0].content.decode("utf-8")
            self.assertIn("Spil: example-game", text)
            self.assertIn("Navn\tPris\tTotalvækst\tStatus", text)
            self.assertIn("7.000.000", text)
            markdown = first[2].content.decode("utf-8")
            self.assertIn("| Navn | Pris | Totalvækst | Status |", markdown)

    def test_point_cycling_uses_point_and_category_labels(self) -> None:
        point_game = statistics(variant="cycling", unit="points")
        rows, integer_columns = holdet.player_display_rows(
            point_game,
            holdet.PlayerStatisticsQuery(),
        )
        self.assertEqual(
            tuple(rows[0]),
            (
                "Navn",
                "Hold",
                "Kategori",
                "Point",
                "Totalændring",
                "Rundeændring",
                "Status",
            ),
        )
        self.assertEqual(
            integer_columns,
            ("Point", "Totalændring", "Rundeændring"),
        )
        document = holdet.build_player_export(
            point_game,
            holdet.PlayerStatisticsQuery(columns=("name", "position", "value")),
        )
        self.assertIn(
            "Navn\tKategori\tPoint",
            holdet.player_export_to_txt(document),
        )
    def test_store_is_side_effect_free_until_save(self) -> None:
        with writable_directory() as root:
            target = root / "exports"
            store = holdet.PlayerExportStore(target)
            self.assertFalse(target.exists())
            document = holdet.build_player_export(
                statistics(), holdet.PlayerStatisticsQuery()
            )
            store.save(document, ("txt",))
            self.assertTrue(target.exists())


class PlayerCliArgumentTests(unittest.TestCase):
    def test_cli_builds_shared_query_formats_round_and_snapshot_path(self) -> None:
        with writable_directory() as root:
            with patch(
                "cli.main._fetch_and_export_players",
                return_value=(root / "one.txt", root / "one.json"),
            ) as scrape:
                with redirect_stdout(io.StringIO()):
                    exit_code = cli_main(
                        [
                            "players",
                            "https://www.holdet.dk/da/fantasy/example-game",
                            "--round", "5",
                            "--format", "txt", "--format", "json",
                            "--column", "name", "--column", "value",
                            "--min-value", "5000000",
                            "--status", "disabled=exclude",
                            "--data-dir", str(root / "appdata"),
                        ]
                    )
            self.assertEqual(exit_code, 0)
            call = scrape.call_args
            self.assertEqual(call.kwargs["round_number"], 5)
            self.assertEqual(call.kwargs["formats"], ("txt", "json"))
            self.assertEqual(call.kwargs["query"].columns, ("name", "value"))
            self.assertEqual(call.kwargs["query"].min_value, 5_000_000)
            self.assertEqual(
                call.kwargs["query"].status_rules,
                (("disabled", "exclude"),),
            )
            self.assertEqual(
                call.kwargs["snapshot_dir"], root / "appdata" / "data" / "snapshots"
            )

class SharedCliExportTests(unittest.TestCase):
    def test_cli_exports_table_and_canonical_snapshot(self) -> None:
        with writable_directory() as root:
            with patch("cli.main.HoldetClient") as client_type:
                client_type.return_value.fetch_players.return_value = statistics()
                with redirect_stdout(io.StringIO()):
                    exit_code = cli_main([
                        "players",
                        "https://www.holdet.dk/da/fantasy/example-game",
                        "--format", "txt", "--format", "json",
                        "--column", "name", "--column", "value",
                        "--data-dir", str(root),
                    ])
            self.assertEqual(exit_code, 0)
            snapshots = holdet.PlayerStatisticsStore(root / "data" / "snapshots").scan()
            self.assertEqual(len(snapshots.snapshots), 1)
            exports = tuple((root / "exports" / "players").rglob("data-*"))
            self.assertEqual({path.suffix for path in exports}, {".txt", ".json"})

if __name__ == "__main__":
    unittest.main()
