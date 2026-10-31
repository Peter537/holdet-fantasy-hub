from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path

import pytest

from holdet_lib.errors import PayloadError
from holdet_lib.hub_settings import (
    DEFAULT_STATUS_WATCH_RULE,
    HubFeature,
    HubSettings,
    HubSettingsStore,
    effective_manager_profiles,
    feature_enabled,
    watchlist_entry,
)
from holdet_lib.models import GameUrl, PlayerEntry


GAME = GameUrl("https://www.holdet.dk/da/fantasy/test-game", "da", "test-game")
PLAYER = PlayerEntry(1, "Spiller", "Klub", "Angriber", 100, entry_id=1)


def legacy_payload(version: int) -> dict[str, object]:
    watched = {
        "game_locale": "da",
        "game_slug": "test-game",
        "player_key": "da:test-game:entry:1",
        "entry_id": 1,
        "person_id": 101,
        "name": "Spiller",
        "team": "Klub",
        "position": "Angriber",
    }
    payload: dict[str, object] = {
        "schema_version": version,
        "watchlist": [watched],
        "manager_aliases": [
            {
                "canonical_id": "legacy-manager",
                "display_name": "Legacy manager",
                "identity_keys": ["name:legacy"],
            }
        ],
        "hall_of_fame_score": {
            "group_points": [20, 12, 6, 2],
            "tournament_winner": 20,
            "tournament_finalist": 12,
            "tournament_semifinalist": 6,
            "global_round_win": 2,
        },
    }
    if version >= 2:
        payload["manager_profiles"] = [
            {
                "manager_id": "manager-2",
                "display_name": "Manager 2",
                "identity_keys": ["user:2"],
                "manual_identity_keys": ["user:2"],
                "profile_urls": ["https://www.holdet.dk/da/user/2"],
            }
        ]
    if version >= 3:
        payload.update(
            player_annotations=[
                {
                    "game_locale": "da",
                    "game_slug": "test-game",
                    "player_key": "da:test-game:entry:1",
                    "note": "Behold efter prisfald",
                    "tags": ["langsigtet"],
                    "updated_at": "2026-10-20T12:00:00+00:00",
                }
            ],
            saved_player_filters=[
                {
                    "filter_id": "cheap",
                    "name": "Billige spillere",
                    "game_locale": "da",
                    "game_slug": "test-game",
                    "query": {"value": {"max": 5_000_000}},
                }
            ],
            own_teams=[{"game_locale": "da", "game_slug": "test-game", "team_id": 7}],
            experimental_games=[{"game_locale": "da", "game_slug": "test-game"}],
        )
    if version >= 4:
        watched.update(
            reasons=["vent på prisfald"],
            reason_note="Køb under grænsen",
            rules=[
                {
                    "rule_id": "price-drop",
                    "kind": "value_drop",
                    "threshold": 10,
                    "threshold_unit": "percent",
                }
            ],
        )
        payload["computed_player_columns"] = [
            {
                "game_locale": "da",
                "game_slug": "test-game",
                "column_id": "custom-value",
                "name": "Vækst pr. pris",
                "expression": "total_growth / coalesce(value, 1)",
                "decimals": 3,
            }
        ]
    return payload


def write_payload(path: Path, payload: dict[str, object]) -> bytes:
    content = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    path.write_bytes(content)
    return content


def test_missing_preferences_start_disabled_without_creating_a_file(tmp_path: Path) -> None:
    path = tmp_path / "hub-settings.json"
    settings = HubSettingsStore(path).load()

    assert settings.enabled_features == ()
    assert all(not feature_enabled(settings, feature) for feature in HubFeature)
    assert not path.exists()


@pytest.mark.parametrize("version", (1, 2, 3, 4))
def test_legacy_settings_load_disabled_without_rewriting_or_losing_records(
    tmp_path: Path, version: int
) -> None:
    path = tmp_path / "hub-settings.json"
    original = write_payload(path, legacy_payload(version))
    settings = HubSettingsStore(path).load()

    assert path.read_bytes() == original
    assert settings.enabled_features == ()
    assert settings.watchlist[0].name == "Spiller"
    assert settings.hall_of_fame_score.group_points == (20, 12, 6, 2)
    assert any(item.manager_id == "legacy-manager" for item in effective_manager_profiles(settings))
    if version >= 2:
        assert settings.manager_profiles[0].profile_urls == (
            "https://www.holdet.dk/da/user/2",
        )
    if version >= 3:
        assert settings.player_annotations[0].note == "Behold efter prisfald"
        assert settings.saved_player_filters[0].query.max_value == 5_000_000
        assert settings.own_teams[0].team_id == 7
        assert settings.experimental_games == (("da", "test-game"),)
    if version >= 4:
        assert settings.watchlist[0].reason_note == "Køb under grænsen"
        assert settings.watchlist[0].rules[0].threshold == 10
        assert settings.computed_player_columns[0].decimals == 3
    else:
        assert settings.watchlist[0].rules == (DEFAULT_STATUS_WATCH_RULE,)


def test_explicit_feature_changes_roundtrip_schema_five_and_preserve_all_records(
    tmp_path: Path,
) -> None:
    path = tmp_path / "hub-settings.json"
    write_payload(path, legacy_payload(4))
    store = HubSettingsStore(path)
    settings = store.load()
    expected = replace(
        settings,
        manager_aliases=(),
        manager_profiles=effective_manager_profiles(settings),
    )
    inbox = tmp_path / "analysis-inbox.json"
    inbox.write_text("existing inbox", encoding="utf-8")

    enabled = store.set_enabled_features(settings, reversed(tuple(HubFeature)))
    assert all(feature_enabled(enabled, feature.value) for feature in HubFeature)
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 5
    assert store.load() == replace(expected, enabled_features=tuple(HubFeature))

    disabled = store.set_enabled_features(enabled, ())
    assert disabled == settings
    assert store.load() == expected
    assert inbox.read_text(encoding="utf-8") == "existing inbox"
    assert {item.name for item in tmp_path.iterdir()} == {
        "hub-settings.json",
        "analysis-inbox.json",
    }


def test_feature_ids_are_normalized_and_saved_as_known_strings(tmp_path: Path) -> None:
    path = tmp_path / "hub-settings.json"
    store = HubSettingsStore(path)
    settings = store.set_enabled_features(
        HubSettings(), ("stories", HubFeature.ALERTS, "alerts", "scouting")
    )

    assert settings.enabled_features == (
        HubFeature.SCOUTING,
        HubFeature.ALERTS,
        HubFeature.STORIES,
    )
    assert json.loads(path.read_text(encoding="utf-8"))["enabled_features"] == [
        "scouting", "alerts", "stories"
    ]
    assert store.load() == settings
    assert not feature_enabled(settings, "analysis")


@pytest.mark.parametrize("features", ("alerts", None, ["unknown"], [False]))
def test_invalid_saved_preferences_do_not_modify_the_file(
    tmp_path: Path, features: object
) -> None:
    path = tmp_path / "hub-settings.json"
    original = write_payload(path, {"schema_version": 5, "enabled_features": features})

    with pytest.raises(PayloadError):
        HubSettingsStore(path).load()
    assert path.read_bytes() == original


def test_invalid_explicit_preference_change_preserves_saved_preferences(tmp_path: Path) -> None:
    path = tmp_path / "hub-settings.json"
    store = HubSettingsStore(path)
    settings = store.set_enabled_features(HubSettings(), ("scouting",))
    original = path.read_bytes()

    with pytest.raises(ValueError, match="Ukendt tilvalgsfunktion"):
        store.set_enabled_features(settings, ("alerts", "unknown"))
    assert path.read_bytes() == original


def test_new_bookmarks_follow_alert_preference_and_legacy_call_contract() -> None:
    disabled = HubSettings()
    enabled = HubSettings(enabled_features=(HubFeature.ALERTS,))

    assert watchlist_entry(GAME, PLAYER, disabled).rules == ()
    assert watchlist_entry(GAME, PLAYER, enabled).rules == (DEFAULT_STATUS_WATCH_RULE,)
    assert watchlist_entry(GAME, PLAYER, enabled, alerts_enabled=False).rules == ()
    assert watchlist_entry(GAME, PLAYER, disabled, alerts_enabled=True).rules == (
        DEFAULT_STATUS_WATCH_RULE,
    )
    assert watchlist_entry(GAME, PLAYER).rules == (DEFAULT_STATUS_WATCH_RULE,)
