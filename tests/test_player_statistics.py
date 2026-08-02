from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import shutil
import unittest
from uuid import uuid4

import holdet_lib as holdet


@contextmanager
def writable_directory():
    root = Path(__file__).parent / f"_test-player-statistics-{uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


def flight_html(payload: object) -> str:
    flight = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return (
        "<script>self.__next_f.push([1,"
        + json.dumps(flight, ensure_ascii=False)
        + "]);</script>"
    )


def statistics_html(*, round_number: int = 7, include_growth: bool = True) -> str:
    row = {
        "id": 50622,
        "person": {"id": 1234, "fullName": "Søren Pogačar"},
        "team": {"name": "Team Ægir"},
        "position": {"title": "Rytter"},
        "score": 12_345_000,
        "isActive": False,
        "isDisabled": True,
        "isInjured": True,
        "hasSuspension": True,
    }
    if include_growth:
        row.update({"totalGrowth": 2_345_000, "growth": -125_000})
    return flight_html({"rows": [row], "round": round_number})


def sample_statistics(round_number: int = 7, *, variant: str = "cycling") -> holdet.ScrapedGame:
    game = holdet.GameUrl(
        "https://www.holdet.dk/da/fantasy/tour-de-france-2026",
        "da",
        "tour-de-france-2026",
    )
    entry = holdet.PlayerEntry(
        0,
        "Søren Pogačar",
        "Team Ægir",
        "Rytter",
        12_345_000,
        False,
        True,
        True,
        True,
        50622,
        1234,
        2_345_000,
        -125_000,
    )
    return holdet.ScrapedGame(game, variant, round_number, (entry,))


class PlayerPayloadTests(unittest.TestCase):
    def test_parses_ids_growth_unicode_and_statuses(self) -> None:
        entries, round_number = holdet.extract_entries_and_round(statistics_html())
        self.assertEqual(round_number, 7)
        entry = entries[0]
        self.assertEqual(
            (
                entry.entry_id,
                entry.person_id,
                entry.total_growth,
                entry.round_growth,
            ),
            (50622, 1234, 2_345_000, -125_000),
        )
        self.assertEqual((entry.name, entry.team), ("Søren Pogačar", "Team Ægir"))
        self.assertFalse(entry.is_active)
        self.assertTrue(entry.is_disabled)
        self.assertTrue(entry.is_injured)
        self.assertTrue(entry.has_suspension)

    def test_missing_growth_is_preserved_as_none(self) -> None:
        entry = holdet.extract_entries_and_round(
            statistics_html(include_growth=False)
        )[0][0]
        self.assertIsNone(entry.total_growth)
        self.assertIsNone(entry.round_growth)

    def test_historical_url_and_client_request(self) -> None:
        game = sample_statistics().game
        self.assertEqual(
            game.statistics_url("cycling", 6),
            "https://nexus-app-fantasy.holdet.dk/da/"
            "tour-de-france-2026/cycling/statistics?round=6",
        )
        requested: list[str] = []

        def fetch(url: str) -> str:
            requested.append(url)
            if url == game.nexus_root_url:
                return flight_html({"variant": "cycling"})
            return statistics_html(round_number=6)

        cartridge = {
            "gameId": 7,
            "_embedded": {
                "games": {"7": {"id": 7, "rulesetId": 8}},
                "rulesets": {
                    "8": {
                        "id": 8,
                        "salaryCap": 50_000_000,
                        "properties": {"Format": "cycling"},
                    }
                },
            },
        }
        result = holdet.HoldetClient(
            text_fetcher=fetch, json_fetcher=lambda _url: cartridge
        ).fetch_players(game, round_number=6)
        self.assertEqual(result.round_number, 6)
        self.assertEqual(requested[-1], game.statistics_url("cycling", 6))
        self.assertEqual(result.round_status, "unknown")


class PlayerSerializationTests(unittest.TestCase):
    def test_unicode_round_trip_and_unknown_schema(self) -> None:
        original = sample_statistics()
        generated = datetime(2026, 7, 28, 12, 34, 56)
        payload = json.loads(
            holdet.player_statistics_to_json(original, generated_at=generated)
        )
        self.assertEqual(payload["schema_version"], 3)
        self.assertEqual(payload["game"]["format"], "cycling")
        self.assertEqual(payload["game"]["unit"], "money")
        self.assertEqual(holdet.player_statistics_from_dict(payload), original)
        legacy = json.loads(json.dumps(payload))
        legacy["schema_version"] = 2
        legacy["game"].pop("round_status")
        legacy["game"].pop("round_end_at")
        restored_legacy = holdet.player_statistics_from_dict(legacy)
        self.assertEqual(restored_legacy.round_status, "unknown")
        self.assertIsNone(restored_legacy.round_end_at)
        self.assertEqual(payload["entries"][0]["name"], "Søren Pogačar")
        with self.assertRaises(holdet.PayloadError):
            holdet.player_statistics_from_dict({"schema_version": 4})

    def test_schema_one_cycling_requires_refetch_but_other_formats_load(self) -> None:
        generated = datetime(2026, 7, 28, 12, 34, 56)
        cycling = json.loads(
            holdet.player_statistics_to_json(
                sample_statistics(), generated_at=generated
            )
        )
        cycling["schema_version"] = 1
        cycling["game"].pop("format")
        cycling["game"].pop("unit")
        with self.assertRaisesRegex(holdet.PayloadError, "fetch this round again"):
            holdet.player_statistics_from_dict(cycling)

        soccer = json.loads(
            holdet.player_statistics_to_json(
                sample_statistics(variant="soccer"), generated_at=generated
            )
        )
        soccer["schema_version"] = 1
        soccer["game"].pop("format")
        soccer["game"].pop("unit")
        restored = holdet.player_statistics_from_dict(soccer)
        self.assertEqual((restored.format, restored.unit), ("soccer", "money"))
    def test_store_is_side_effect_free_until_save_and_uses_collisions(self) -> None:
        fixed = datetime(2026, 7, 28, 12, 34, 56)
        with writable_directory() as root:
            snapshot_root = Path(root) / "snapshots"
            store = holdet.PlayerStatisticsStore(snapshot_root)
            self.assertFalse(snapshot_root.exists())
            self.assertEqual(store.scan().snapshots, ())
            self.assertFalse(snapshot_root.exists())

            first = store.save(sample_statistics(), now=fixed)
            second = store.save(sample_statistics(), now=fixed)
            self.assertEqual(first.name, "player-round7_0728_123456.json")
            self.assertEqual(second.name, "player-round7_0728_123456_1.json")
            self.assertEqual(
                first.parent,
                snapshot_root / "tour-de-france-2026" / "players",
            )
            index = store.scan(sample_statistics().game)
            self.assertEqual(index.rounds_for(sample_statistics().game), (7,))
            self.assertEqual(index.newest(sample_statistics().game, 7).path, second)

    def test_corrupt_snapshot_warns_without_hiding_valid_data(self) -> None:
        with writable_directory() as root:
            store = holdet.PlayerStatisticsStore(root)
            store.save(sample_statistics())
            corrupt = (
                Path(root)
                / "tour-de-france-2026"
                / "players"
                / "player-round6_0101_000000.json"
            )
            corrupt.write_text("{bad", encoding="utf-8")
            index = store.scan(sample_statistics().game)
            self.assertEqual(len(index.snapshots), 1)
            self.assertEqual(len(index.warnings), 1)


if __name__ == "__main__":
    unittest.main()
