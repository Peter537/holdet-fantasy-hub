from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import re
import unittest

import holdet_lib as scraper
from cli.main import main as cli_main


class VersionAndDispatchTests(unittest.TestCase):
    def test_documentation_is_linked_and_uses_source_commands(self) -> None:
        root = Path(__file__).parents[1]
        documents = [root / "README.md", *sorted((root / "docs").glob("*.md"))]
        expected_docs = {
            "architecture.md",
            "clients.md",
            "data-retrieval.md",
            "data-storage.md",
            "groups-and-tournaments.md",
            "player-statistics.md",
            "team-statistics.md",
            "testing.md",
        }
        self.assertEqual({path.name for path in documents[1:]}, expected_docs)
        self.assertFalse((root / "examples").exists())

        corpus = "\n".join(path.read_text(encoding="utf-8") for path in documents)
        self.assertNotIn("examples/", corpus)
        self.assertNotIn("HH-mm-ss-ffff", corpus)
        self.assertIn("data-round<round>_<MMDD>_<HHmmss>[_N]", corpus)
        self.assertIn("team-round<round>_<MMDD>_<HHmmss>[_N]", corpus)

        link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
        mermaid_blocks = 0
        for document in documents:
            text = document.read_text(encoding="utf-8")
            for block in text.split("```powershell")[1:]:
                commands = block.split("```", 1)[0]
                for line in commands.splitlines():
                    self.assertFalse(line.rstrip().endswith(chr(96)))
                    self.assertFalse(line.strip().casefold().startswith("holdet "))

            inside_fence = False
            for line in text.splitlines():
                if not line.startswith("```"):
                    continue
                if not inside_fence and line.strip() == "```mermaid":
                    mermaid_blocks += 1
                inside_fence = not inside_fence
            self.assertFalse(inside_fence, f"unclosed code fence in {document}")

            for target in link_pattern.findall(text):
                if "://" in target or target.startswith("#"):
                    continue
                resolved = (document.parent / target.split("#", 1)[0]).resolve()
                self.assertTrue(resolved.is_file(), f"broken link in {document}: {target}")

        self.assertGreaterEqual(mermaid_blocks, 4)

    def test_global_version_prints_raw_version(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                cli_main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        self.assertEqual(output.getvalue(), "0.1.0\n")

    def test_bare_url_is_rejected(self) -> None:
        errors = io.StringIO()
        with redirect_stderr(errors):
            with self.assertRaises(SystemExit) as raised:
                cli_main(
                    ["https://www.holdet.dk/da/fantasy/tour-de-france-2026"]
                )

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("invalid choice", errors.getvalue())

    def test_version_is_not_a_subcommand_option(self) -> None:
        for command in ("players", "teams"):
            with self.subTest(command=command):
                with redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        cli_main([command, "--version"])
                self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()