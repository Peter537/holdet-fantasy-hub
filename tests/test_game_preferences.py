"""Own-team preferences remain usable without optional analysis features."""
from __future__ import annotations

from unittest.mock import patch

import holdet_lib as holdet
from streamlit.testing.v1 import AppTest

from tests.test_website import APP_PATH, button, navigate, website_environment, widget


def _settings_page(app, game):
    return navigate(app, "game", locale=game.locale, game=game.slug, section="settings")


def _save(app, value: str) -> None:
    widget(app, "text_input", "Team-ID eller Holdet-URL").input(value)
    button(app, "Gem mit hold").click().run(timeout=15)
    assert not app.exception


def test_own_team_id_url_rejection_and_clear_preserve_other_preferences() -> None:
    with website_environment() as (config, _output):
        groups = holdet.GroupStore(config / "groups.json")
        first = groups.create_manager_game("tour-de-france-2026", "Tour")
        second = groups.create_manager_game("super-manager-fall-2026", "Super")
        settings_store = holdet.HubSettingsStore(config / "hub-settings.json")
        initial = holdet.HubSettings(
            own_teams=(holdet.OwnTeamSelection(second.game.locale, second.game.slug, 99),),
            enabled_features=(holdet.HubFeature.ALERTS,),
            experimental_games=((second.game.locale, second.game.slug),),
        )
        settings_store.save(initial)
        with patch("holdet_lib.HoldetClient") as client:
            app = AppTest.from_file(APP_PATH).run(timeout=15)
            _settings_page(app, first.game)
            assert not app.exception
            assert holdet.HubFeature.ANALYSIS not in settings_store.load().enabled_features
            _save(app, "42")
            assert holdet.OwnTeamSelection("da", first.game.slug, 42) in settings_store.load().own_teams
            assert any("gemt" in item.value for item in app.success)

            _save(app, f"https://www.holdet.dk/da/fantasy/{first.game.slug}/fantasyteams/73")
            selected = settings_store.load()
            assert holdet.OwnTeamSelection("da", first.game.slug, 73) in selected.own_teams
            assert holdet.OwnTeamSelection("da", first.game.slug, 42) not in selected.own_teams

            before = settings_store.path.read_bytes()
            _save(app, f"https://www.holdet.dk/da/fantasy/{second.game.slug}/fantasyteams/22")
            assert app.error
            assert settings_store.path.read_bytes() == before

            _save(app, "")
            after = settings_store.load()
            assert after.own_teams == initial.own_teams
            assert after.enabled_features == initial.enabled_features
            assert after.experimental_games == initial.experimental_games
            assert client.mock_calls == []


def test_archived_game_settings_are_read_only() -> None:
    with website_environment() as (config, _output):
        groups = holdet.GroupStore(config / "groups.json")
        game = groups.create_manager_game("tour-de-france-2026", "Tour")
        settings_store = holdet.HubSettingsStore(config / "hub-settings.json")
        settings_store.save(holdet.HubSettings(
            own_teams=(holdet.OwnTeamSelection("da", game.game.slug, 77),),
        ))
        groups.archive_manager_game(game.game)
        before = settings_store.path.read_bytes()
        with patch("holdet_lib.HoldetClient") as client:
            app = AppTest.from_file(APP_PATH).run(timeout=15)
            _settings_page(app, game.game)
            assert not app.exception
            assert not [item for item in app.text_input if item.label == "Team-ID eller Holdet-URL"]
            assert not [item for item in app.button if item.label == "Gem mit hold"]
            assert any("skrivebeskyttede" in item.value for item in app.info)
            assert settings_store.path.read_bytes() == before
            assert client.mock_calls == []
