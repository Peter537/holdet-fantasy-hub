from __future__ import annotations

from datetime import datetime
import json
import unittest

from holdet_lib import TeamExportStore, build_team_export, serialize_team_export

try:
    import tests.test_team_scraper as fixtures
except ModuleNotFoundError:
    import test_team_scraper as fixtures


class TeamExportTests(unittest.TestCase):
    def setUp(self) -> None:
        service, reference = fixtures.TeamServiceAndOutputTests().make_service()
        self.team = service.scrape(reference)
        self.now = datetime(2026, 7, 9, 8, 7, 6).astimezone()

    def test_full_export_has_separate_document_schema_and_unicode(self) -> None:
        document = build_team_export(self.team, generated_at=self.now)
        payload = json.loads(serialize_team_export(document, "json"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["document_type"], "team_export")
        self.assertEqual(payload["scope"], "full")
        self.assertEqual(len(payload["history"]), 2)
        self.assertIn("Søren Ægir", serialize_team_export(document, "md").decode())

    def test_round_export_allows_missing_exact_roster(self) -> None:
        document = build_team_export(
            self.team, scope="round", round_number=2, generated_at=self.now
        )
        payload = json.loads(serialize_team_export(document, "json"))
        self.assertFalse(payload["roster_available"])
        self.assertIsNone(payload["roster"])
        self.assertEqual([item["round"] for item in payload["history"]], [2])
        self.assertIn(
            "Ingen opstilling blev gemt præcis i denne runde",
            serialize_team_export(document, "txt").decode(),
        )

    def test_selected_formats_share_collision_suffix_and_download_bytes(self) -> None:
        document = build_team_export(self.team, generated_at=self.now)
        with fixtures.writable_test_directory() as root:
            store = TeamExportStore(root)
            first = store.save(document, ("txt", "json", "md"))
            second = store.save(document, ("txt", "json", "md"))
            self.assertEqual(
                {item.path.stem for item in first}, {"team-round3_0709_080706"}
            )
            self.assertEqual(
                {item.path.stem for item in second}, {"team-round3_0709_080706_1"}
            )
            for artifact in first + second:
                self.assertEqual(artifact.path.read_bytes(), artifact.content)

    def test_missing_round_summary_is_an_error(self) -> None:
        with self.assertRaisesRegex(Exception, "ingen runde 99"):
            build_team_export(self.team, scope="round", round_number=99)


if __name__ == "__main__":
    unittest.main()
