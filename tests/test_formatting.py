from __future__ import annotations

import unittest

from holdet_lib._formatting import count_label


class DanishCountLabelTests(unittest.TestCase):
    def test_zero_and_plural_counts_use_plural(self) -> None:
        self.assertEqual(count_label(0, "kamp", "kampe"), "0 kampe")
        self.assertEqual(count_label(2, "kamp", "kampe"), "2 kampe")

    def test_one_uses_singular(self) -> None:
        self.assertEqual(count_label(1, "kamp", "kampe"), "1 kamp")

    def test_irregular_ui_nouns_are_supported(self) -> None:
        cases = (
            ("fil", "filer"),
            ("spillerfil", "spillerfiler"),
            ("gruppemedlemskab", "gruppemedlemskaber"),
            ("knockoutfase", "knockoutfaser"),
            ("runde", "runder"),
            ("gruppespilskamp", "gruppespilskampe"),
            ("knockoutkamp", "knockoutkampe"),
        )
        for singular, plural in cases:
            with self.subTest(noun=singular):
                self.assertEqual(count_label(1, singular, plural), f"1 {singular}")
                self.assertEqual(count_label(2, singular, plural), f"2 {plural}")


if __name__ == "__main__":
    unittest.main()
