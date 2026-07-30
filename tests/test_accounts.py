from __future__ import annotations

import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from uuid import uuid4
from contextlib import contextmanager

import holdet_lib as holdet


@contextmanager
def account_environment():
    root = Path(__file__).parent / f"_test-accounts-{uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        shutil.rmtree(root)


class AccountStoreTests(unittest.TestCase):
    def test_unicode_key_generation_and_collision_suffix(self) -> None:
        self.assertEqual(holdet.account_key_from_label("Åge & Co", 123), "age-co")
        self.assertEqual(
            holdet.account_key_from_label("Åge & Co", 123, {"age-co"}),
            "age-co-123",
        )
        self.assertEqual(holdet.account_key_from_label("東京", 9), "konto")

    def test_missing_file_is_side_effect_free_and_crud_preserves_order(self) -> None:
        with account_environment() as root:
            path = root / "nested" / "accounts.json"
            store = holdet.AccountStore(path)
            self.assertEqual(store.load(), ())
            self.assertFalse(path.parent.exists())

            first = store.create(
                "Åge & Co", "https://www.holdet.dk/da/users/123/teams"
            )
            second = store.create(
                "Beta", "https://www.holdet.dk/da/users/456/teams"
            )
            self.assertEqual((first.key, second.key), ("age-co", "beta"))
            renamed = store.rename(first.key, "Ærlige Åge")
            self.assertEqual(renamed.key, first.key)
            self.assertEqual(renamed.user_id, 123)
            self.assertEqual([item.key for item in store.load()], ["age-co", "beta"])
            self.assertIn("Ærlige Åge", path.read_text(encoding="utf-8"))

            removed = store.delete(second.key)
            self.assertEqual(removed.user_id, 456)
            self.assertEqual(store.load(), (renamed,))

    def test_duplicate_identity_invalid_profile_and_corrupt_file_are_rejected(self) -> None:
        with account_environment() as root:
            path = root / "accounts.json"
            store = holdet.AccountStore(path)
            store.create("Alpha", "https://www.holdet.dk/da/users/10/teams")
            with self.assertRaises(holdet.PayloadError):
                store.create("Anden", "https://www.holdet.dk/da/users/10/teams")
            with self.assertRaises(holdet.UrlValidationError):
                store.create("Forkert", "https://example.com/da/users/11/teams")

            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(holdet.PayloadError):
                store.load()
            with self.assertRaises(holdet.PayloadError):
                store.create("Ny", "https://www.holdet.dk/da/users/12/teams")
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_failed_atomic_replace_keeps_existing_file(self) -> None:
        with account_environment() as root:
            path = root / "accounts.json"
            store = holdet.AccountStore(path)
            store.create("Alpha", "https://www.holdet.dk/da/users/10/teams")
            before = path.read_bytes()
            with patch("holdet_lib.persistence.os.replace", side_effect=OSError("locked")):
                with self.assertRaises(OSError):
                    store.rename("alpha", "Ændret")
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob(".*.tmp")), [])

    def test_rename_updates_group_labels_and_rolls_back_on_group_failure(self) -> None:
        with account_environment() as root:
            account_store = holdet.AccountStore(root / "config" / "accounts.json")
            account = account_store.create(
                "Alpha", "https://www.holdet.dk/da/users/10/teams"
            )
            group_store = holdet.GroupStore(root / "config" / "groups.json")
            game = holdet.normalize_manager_game("super-manager-fall-2026").game
            group_store.create(
                "Venner",
                game,
                (
                    holdet.GroupTeam(
                        99,
                        "Holdet",
                        "https://www.holdet.dk/da/fantasy/super-manager-fall-2026/fantasyteams/99",
                        account.key,
                        account.label,
                        account.user_id,
                        account.profile_url,
                    ),
                ),
            )

            renamed = holdet.rename_account_and_groups(
                account_store, group_store, account.key, "Nyt navn"
            )
            self.assertEqual(renamed.key, account.key)
            self.assertEqual(group_store.load()[0].teams[0].account_label, "Nyt navn")

            with patch.object(
                group_store, "save_configuration", side_effect=OSError("locked")
            ):
                with self.assertRaises(OSError):
                    holdet.rename_account_and_groups(
                        account_store, group_store, account.key, "Må ikke gemmes"
                    )
            self.assertEqual(account_store.load()[0].label, "Nyt navn")
            self.assertEqual(group_store.load()[0].teams[0].account_label, "Nyt navn")

    def test_delete_does_not_change_groups(self) -> None:
        with account_environment() as root:
            account_store = holdet.AccountStore(root / "config" / "accounts.json")
            account = account_store.create(
                "Alpha", "https://www.holdet.dk/da/users/10/teams"
            )
            group_store = holdet.GroupStore(root / "config" / "groups.json")
            game = holdet.normalize_manager_game("super-manager-fall-2026").game
            group_store.create(
                "Venner",
                game,
                (
                    holdet.GroupTeam(
                        99, "Holdet", "direct", account.key, account.label
                    ),
                ),
            )
            before = (root / "config" / "groups.json").read_bytes()
            account_store.delete(account.key)
            self.assertEqual((root / "config" / "groups.json").read_bytes(), before)
            self.assertEqual(group_store.load()[0].teams[0].account_key, account.key)


if __name__ == "__main__":
    unittest.main()
