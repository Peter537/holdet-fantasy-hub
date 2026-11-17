from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import re
import tomllib
import unittest

from cli.main import build_argument_parser, main as cli_main


class VersionAndDispatchTests(unittest.TestCase):
    def test_documentation_is_linked_and_uses_source_commands(self) -> None:
        root = Path(__file__).parents[1]
        documents = [root / "README.md", *sorted((root / "docs").glob("*.md"))]
        corpus = "\n".join(path.read_text(encoding="utf-8") for path in documents)
        self.assertNotIn("HH-mm-ss-ffff", corpus)
        self.assertIn("data-round<round>_<MMDD>_<HHmmss>[_N]", corpus)
        self.assertIn("team-round<round>_<MMDD>_<HHmmss>[_N]", corpus)
        self.assertIn("py -3.14 -m venv .venv", corpus)
        self.assertIn(".\\.venv\\Scripts\\python.exe .\\scripts\\install_locked_dependencies.py", corpus)
        self.assertIn(
            ".\\.venv\\Scripts\\python.exe -m pip install --no-deps --no-build-isolation --no-index -e .",
            corpus,
        )
        self.assertIn(".\\.venv\\Scripts\\python.exe .\\scripts\\refresh_dependency_lock.py --check", corpus)
        self.assertIn(".\\.venv\\Scripts\\python.exe -m pytest tests -q", corpus)
        self.assertNotIn("py -3.14 -m unittest discover", corpus)

        link_pattern = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
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
                inside_fence = not inside_fence
            self.assertFalse(inside_fence, f"unclosed code fence in {document}")

            for target in link_pattern.findall(text):
                if "://" in target or target.startswith("#"):
                    continue
                resolved = (document.parent / target.split("#", 1)[0]).resolve()
                self.assertTrue(resolved.is_file(), f"broken link in {document}: {target}")

    def test_global_version_prints_raw_version(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            with self.assertRaises(SystemExit) as raised:
                cli_main(["--version"])

        self.assertEqual(raised.exception.code, 0)
        with (Path(__file__).parents[1] / "pyproject.toml").open("rb") as manifest:
            version = tomllib.load(manifest)["project"]["version"]
        self.assertEqual(output.getvalue(), f"{version}\n")

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
        parser = build_argument_parser()
        url = "https://www.holdet.dk/da/fantasy/tour-de-france-2026"
        for command in ("players", "teams"):
            with self.subTest(command=command):
                errors = io.StringIO()
                with redirect_stderr(errors):
                    with self.assertRaises(SystemExit) as raised:
                        parser.parse_args([command, url, "--version"])
                self.assertEqual(raised.exception.code, 2)
                self.assertIn("--version", errors.getvalue().splitlines()[-1])


if __name__ == "__main__":
    unittest.main()
