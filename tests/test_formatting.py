from __future__ import annotations

import unittest

from holdet_lib._formatting import count_label


class DanishCountLabelTests(unittest.TestCase):
    def test_zero_and_plural_counts_use_plural(self) -> None:
        self.assertEqual(count_label(0, "kamp", "kampe"), "0 kampe")
        self.assertEqual(count_label(2, "fil", "filer"), "2 filer")

    def test_one_uses_singular(self) -> None:
        self.assertEqual(count_label(1, "kamp", "kampe"), "1 kamp")


if __name__ == "__main__":
    unittest.main()
