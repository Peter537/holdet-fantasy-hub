from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4

from cli.main import main as cli_main


class DataCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(__file__).parent / f"_test-data-cli-{uuid4().hex}"

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_paths_reports_absolute_locations_without_creating_them(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            result = cli_main(
                ["data", "--data-dir", str(self.root), "paths"]
            )

        self.assertEqual(result, 0)
        self.assertFalse(self.root.exists())
        rendered = output.getvalue()
        self.assertIn(str((self.root / "config").resolve()), rendered)
        self.assertIn(str((self.root / "data" / "snapshots").resolve()), rendered)
        self.assertIn(str((self.root / "exports").resolve()), rendered)

    def test_open_snapshots_uses_explicit_resolved_location(self) -> None:
        output = io.StringIO()
        with patch("cli.main.open_in_explorer", return_value=True) as opener:
            with redirect_stdout(output):
                result = cli_main(
                    [
                        "data",
                        "--data-dir",
                        str(self.root),
                        "open",
                        "--snapshots",
                    ]
                )

        self.assertEqual(result, 0)
        opener.assert_called_once_with(self.root / "data" / "snapshots")
        self.assertIn(str((self.root / "data" / "snapshots").resolve()), output.getvalue())

    def test_explorer_failure_is_not_a_data_failure(self) -> None:
        errors = io.StringIO()
        with patch("cli.main.open_in_explorer", return_value=False):
            with redirect_stdout(io.StringIO()), redirect_stderr(errors):
                result = cli_main(
                    ["data", "--data-dir", str(self.root), "open", "--exports"]
                )

        self.assertEqual(result, 0)
