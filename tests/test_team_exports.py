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
        source_time = self.now.replace(hour=6)
        roster_time = self.now.replace(hour=5)
        document = build_team_export(
            self.team, generated_at=self.now,
            source_generated_at=source_time, roster_generated_at=roster_time,
        )
        for formats in (("txt", "json"), ("md", "txt", "json")):
            with self.subTest(formats=formats), fixtures.writable_test_directory() as root:
                store = TeamExportStore(root)
                for suffix in ("", "_1"):
                    artifacts = store.save(document, formats)
                    self.assertEqual(
                        [artifact.path.name for artifact in artifacts],
                        [f"team-round3_0709_080706{suffix}.{fmt}" for fmt in formats],
                    )
                    for artifact in artifacts:
                        self.assertEqual(artifact.path.read_bytes(), artifact.content)
                    paths = {artifact.path.suffix: artifact.path for artifact in artifacts}
                    text = paths[".txt"].read_text(encoding="utf-8")
                    self.assertIn("Spillerværdi: 300", text)
                    self.assertIn("Søren Ægir", text)
                    payload = json.loads(paths[".json"].read_text(encoding="utf-8"))
                    self.assertEqual(payload["schema_version"], 1)
                    self.assertIn("Søren Ægir", [entry["name"] for entry in payload["roster"]])
                    self.assertEqual(payload["team"]["id"], fixtures.FICTIONAL_TEAM_ID)
                    self.assertEqual(payload["history"][0]["overall_rank"], 80)
                    self.assertEqual(payload["generated_at"], self.now.isoformat())
                    self.assertEqual(payload["source_generated_at"], source_time.isoformat())
                    self.assertEqual(payload["roster_generated_at"], roster_time.isoformat())

    def test_missing_round_summary_is_an_error(self) -> None:
        with self.assertRaisesRegex(Exception, "ingen runde 99"):
            build_team_export(self.team, scope="round", round_number=99)


if __name__ == "__main__":
    unittest.main()
