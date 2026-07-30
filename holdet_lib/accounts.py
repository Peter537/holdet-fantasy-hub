"""Atomic persistence and validation for configured Holdet accounts."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
import unicodedata

from .errors import PayloadError
from .groups import GroupStore, HubConfiguration
from .persistence import replace_text_atomically
from .models import AccountConfig
from .teams import load_accounts, parse_account_profile_user_id


_KEY_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")


def account_key_from_label(
    label: str,
    user_id: int,
    existing_keys: tuple[str, ...] | list[str] | set[str] = (),
) -> str:
    """Create a stable CLI-safe account key, disambiguated by user ID."""

    transliteration = str.maketrans({
        "\u00e6": "ae", "\u00c6": "Ae",
        "\u00f8": "o", "\u00d8": "O",
        "\u00df": "ss", "\u1e9e": "SS",
    })
    transliterated = label.strip().translate(transliteration)
    normalized = unicodedata.normalize("NFKD", transliterated)
    ascii_label = normalized.encode("ascii", "ignore").decode("ascii").casefold()
    base = re.sub(r"[^a-z0-9]+", "-", ascii_label).strip("-") or "konto"
    used = set(existing_keys)
    if base not in used:
        return base
    candidate = f"{base}-{user_id}"
    counter = 2
    while candidate in used:
        candidate = f"{base}-{user_id}-{counter}"
        counter += 1
    return candidate


def _validate_accounts(accounts: tuple[AccountConfig, ...]) -> None:
    seen_keys: set[str] = set()
    seen_ids: set[int] = set()
    for index, account in enumerate(accounts):
        if not _KEY_PATTERN.fullmatch(account.key):
            raise PayloadError(f"account {index} has an invalid key")
        if not account.label.strip():
            raise PayloadError(f"account {index} has an empty label")
        parsed_user_id = parse_account_profile_user_id(account.profile_url)
        if parsed_user_id != account.user_id:
            raise PayloadError(f"account {index} profile and user ID do not match")
        if account.key in seen_keys or account.user_id in seen_ids:
            raise PayloadError(f"duplicate account key or user ID: {account.key}")
        seen_keys.add(account.key)
        seen_ids.add(account.user_id)


class AccountStore:
    """Load and atomically maintain ``accounts.json`` without implicit writes."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def load(self) -> tuple[AccountConfig, ...]:
        return load_accounts(self.path)

    def save(self, accounts: tuple[AccountConfig, ...]) -> None:
        accounts = tuple(accounts)
        _validate_accounts(accounts)
        payload = {
            "accounts": [
                {
                    "key": account.key,
                    "label": account.label.strip(),
                    "profile_url": account.profile_url,
                }
                for account in accounts
            ]
        }
        replace_text_atomically(
            self.path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        )

    def prepare_create(self, label: str, profile_url: str) -> tuple[tuple[AccountConfig, ...], AccountConfig]:
        clean_label = label.strip()
        if not clean_label:
            raise PayloadError("account name cannot be empty")
        clean_url = profile_url.strip()
        user_id = parse_account_profile_user_id(clean_url)
        accounts = self.load()
        if any(account.user_id == user_id for account in accounts):
            raise PayloadError(f"account user ID already exists: {user_id}")
        key = account_key_from_label(
            clean_label, user_id, {account.key for account in accounts}
        )
        created = AccountConfig(key, clean_label, clean_url, user_id)
        updated = (*accounts, created)
        _validate_accounts(updated)
        return updated, created

    def create(self, label: str, profile_url: str) -> AccountConfig:
        accounts, created = self.prepare_create(label, profile_url)
        self.save(accounts)
        return created

    def prepare_rename(self, key: str, label: str) -> tuple[tuple[AccountConfig, ...], AccountConfig]:
        clean_label = label.strip()
        if not clean_label:
            raise PayloadError("account name cannot be empty")
        accounts = list(self.load())
        for index, account in enumerate(accounts):
            if account.key == key:
                renamed = replace(account, label=clean_label)
                accounts[index] = renamed
                updated = tuple(accounts)
                _validate_accounts(updated)
                return updated, renamed
        raise PayloadError(f"unknown account: {key}")

    def rename(self, key: str, label: str) -> AccountConfig:
        accounts, renamed = self.prepare_rename(key, label)
        self.save(accounts)
        return renamed

    def delete(self, key: str) -> AccountConfig:
        accounts = self.load()
        removed = next((account for account in accounts if account.key == key), None)
        if removed is None:
            raise PayloadError(f"unknown account: {key}")
        self.save(tuple(account for account in accounts if account.key != key))
        return removed


def rename_account_and_groups(
    account_store: AccountStore,
    group_store: GroupStore,
    key: str,
    label: str,
) -> AccountConfig:
    """Rename an account and its live group labels as one recoverable operation."""

    original_accounts = account_store.load()
    updated_accounts, renamed = account_store.prepare_rename(key, label)
    configuration = group_store.load_configuration()
    groups = tuple(
        replace(
            group,
            teams=tuple(
                replace(team, account_label=renamed.label)
                if team.account_key == key
                else team
                for team in group.teams
            ),
        )
        for group in configuration.groups
    )
    updated_configuration = HubConfiguration(configuration.games, groups)

    account_store.save(updated_accounts)
    try:
        group_store.save_configuration(updated_configuration)
    except Exception as exc:
        try:
            account_store.save(original_accounts)
        except Exception as rollback_exc:
            raise PayloadError(
                "could not save groups and could not roll back accounts: "
                f"{rollback_exc}"
            ) from exc
        raise
    return renamed
