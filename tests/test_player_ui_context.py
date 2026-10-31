"""Acceptance checks for numeric player comparisons and historical detail links."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import holdet_lib as holdet
from holdet_lib.paths import PathOverrides, resolve_paths
from tests.test_website import APP_PATH, button, navigate, widget


GAME = holdet.GameUrl(
    "https://www.holdet.dk/da/fantasy/player-ui-context", "da", "player-ui-context"
)
NOW = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)
SCOPE = f"player-filter-da-{GAME.slug}"


def statistics(round_number: int, *, growth_factor: int = 1) -> holdet.ScrapedGame:
    entries = tuple(
        holdet.PlayerEntry(
            source,
            name,
            "Club",
            "Forward",
            price,
            entry_id=identity,
            total_growth=None if growth is None else round_number * growth * growth_factor,
            round_growth=None if growth is None else growth * growth_factor,
        )
        for identity, source, name, price, growth in (
            (1, 2, "Alpha", 1_000_000, 100),
            (2, 1, "Beta", 2_000_000, 20),
            (3, 0, "Gamma", 3_000_000, 9),
            (4, 3, "Missing", 4_000_000, None),
        )
    )
    return holdet.ScrapedGame(
        GAME, "soccer", round_number, entries, "soccer", "money", "complete"
    )


@pytest.fixture
def player_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from streamlit.testing.v1 import app_test
    from website import ui

    monkeypatch.setattr(app_test, "TMP_DIR", SimpleNamespace(name=str(tmp_path)))
    monkeypatch.setenv("HOLDET_DATA_DIR", str(tmp_path))
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path))
    monkeypatch.setattr(ui, "APP_PATHS", paths)
    monkeypatch.setattr(ui, "OUTPUT_DIR", paths.snapshot_dir)
    monkeypatch.setattr(ui, "PLAYER_EXPORT_DIR", paths.player_export_dir)
    store = holdet.PlayerStatisticsStore(paths.snapshot_dir)
    for number in range(1, 6):
        store.save(
            statistics(number, growth_factor=1 if number <= 3 else 100),
            now=NOW + timedelta(minutes=number),
        )
    holdet.GroupStore(paths.groups_file).create_manager_game(GAME, "Player context")
    return paths


def _list_app(root: str) -> None:
    from pathlib import Path

    from holdet_lib import PlayerStatisticsStore
    from holdet_lib.paths import PathOverrides, resolve_paths
    from tests.test_player_ui_context import GAME
    from website import ui

    paths = resolve_paths(overrides=PathOverrides(data_root=Path(root)))
    selected = PlayerStatisticsStore(paths.snapshot_dir).scan(GAME).newest(GAME, 3)
    ui._player_list_panel(selected, "test")


def test_default_player_table_is_compact_and_keeps_source_numbers_and_links(player_paths):
    with patch("holdet_lib.HoldetClient") as client:
        app = AppTest.from_function(_list_app, args=(str(player_paths.config_dir.parent),)).run()

    assert not app.exception
    assert client.mock_calls == []
    frame = app.dataframe[0].value
    assert len(frame.columns) <= 10
    assert "Form 3" not in frame.columns
    assert frame["Pris"].dtype.kind in "iu"
    assert frame["Totalvækst"].dtype.kind in "iuf"
    assert frame["Navn"].tolist() == ["Missing", "Gamma", "Beta", "Alpha"]
    for _, row in frame.iterrows():
        query = parse_qs(urlparse(row["Detaljer"]).query)
        assert query["round"] == ["3"]
        assert query["snapshot"] == [(NOW + timedelta(minutes=3)).isoformat()]
        expected = next(item for item in statistics(3).entries if item.name == row["Navn"])
        assert query["player"] == [holdet.player_identity(GAME, expected)]


def test_derived_sort_is_numeric_nulls_last_and_preserves_row_identity(player_paths):
    app = AppTest.from_function(_list_app, args=(str(player_paths.config_dir.parent),)).run()
    widget(app, "multiselect", "Ekstra kolonner").set_value(["Form 3", "Vækst pr. mio."]).run()
    widget(app, "selectbox", "Sortering").set_value("form_3").run()

    assert not app.exception
    frame = app.dataframe[0].value
    assert frame["Form 3"].dtype.kind in "iuf"
    assert frame["Vækst pr. mio."].dtype.kind in "iuf"
    assert frame["Navn"].tolist() == ["Alpha", "Beta", "Gamma", "Missing"]
    assert frame["Form 3"].iloc[:3].tolist() == [100, 20, 9]
    assert pd.isna(frame["Form 3"].iloc[-1])
    for _, row in frame.iterrows():
        query = parse_qs(urlparse(row["Detaljer"]).query)
        expected = next(item for item in statistics(3).entries if item.name == row["Navn"])
        assert query["player"] == [holdet.player_identity(GAME, expected)]
        assert row["Pris"] == expected.value


def test_player_export_keeps_the_visible_derived_sort_order(player_paths):
    app = AppTest.from_function(_list_app, args=(str(player_paths.config_dir.parent),)).run()
    widget(app, "selectbox", "Sortering").set_value("form_3").run()
    visible_names = app.dataframe[0].value["Navn"].tolist()
    button(app, "Opret eksport").click().run()

    assert not app.exception
    artifacts = app.session_state[f"{SCOPE}-ready-export-3"]
    text = artifacts[0].content.decode("utf-8")
    exported_positions = [text.index(name) for name in visible_names]
    assert exported_positions == sorted(exported_positions)


def test_exported_computed_form_uses_the_selected_round_and_fetch(player_paths):
    store = holdet.HubSettingsStore(player_paths.hub_settings_file)
    store.set_computed_player_columns(
        store.load(),
        (holdet.ComputedPlayerColumn("da", GAME.slug, "form", "Historical form", "form_3"),),
    )
    app = AppTest.from_function(_list_app, args=(str(player_paths.config_dir.parent),)).run()
    widget(app, "multiselect", "Beregnede kolonner").set_value(["form"]).run()
    button(app, "Opret eksport").click().run()

    assert not app.exception
    text = app.session_state[f"{SCOPE}-ready-export-3"][0].content.decode("utf-8")
    alpha_row = next(line for line in text.splitlines() if line.startswith("Alpha\t"))
    assert float(alpha_row.split("\t")[-1]) == 100


def test_display_export_order_is_only_a_permutation_of_the_filtered_source_rows():
    from holdet_lib.player_exports import player_export_data_package, player_export_to_dict

    source = statistics(3)
    query = holdet.PlayerStatisticsQuery(max_value=3_000_000)
    alpha, beta, gamma, excluded = source.entries
    ordered = (beta, gamma, alpha)
    document = holdet.build_player_export(source, query, ordered_entries=ordered)
    assert document.entries == ordered
    assert player_export_to_dict(document)["output_order"] == "display"
    assert player_export_data_package(document).provenance["output_order"] == "display"
    assert holdet.build_player_export(source, query).output_order == "query"
    for invalid_order in (
        (alpha, beta),
        (alpha, beta, excluded),
        (alpha, alpha, gamma),
        (replace(alpha, value=999), beta, gamma),
    ):
        with pytest.raises(holdet.PayloadError, match="præcis de filtrerede spillere"):
            holdet.build_player_export(source, query, ordered_entries=invalid_order)


def test_applied_profile_keeps_its_sort_direction_in_quick_controls(player_paths):
    store = holdet.HubSettingsStore(player_paths.hub_settings_file)
    store.set_saved_player_filters(
        store.load(),
        (
            holdet.SavedPlayerFilter(
                "growth-order", "Lowest growth", "da", GAME.slug,
                holdet.PlayerStatisticsQuery(sort_field="total_growth", sort_order="asc"),
            ),
        ),
    )
    app = AppTest.from_function(_list_app, args=(str(player_paths.config_dir.parent),)).run()
    widget(app, "selectbox", "Filterprofil").set_value("saved-growth-order").run()
    button(app, "Anvend profil").click().run()

    assert not app.exception
    assert widget(app, "selectbox", "Sortering").value == "total_growth"
    assert app.dataframe[0].value["Navn"].tolist() == ["Gamma", "Beta", "Alpha", "Missing"]
    widget(app, "multiselect", "Ekstra kolonner").set_value(["Form 3"]).run()
    widget(app, "selectbox", "Sortering").set_value("form_3").run()
    assert app.dataframe[0].value["Navn"].tolist() == ["Gamma", "Beta", "Alpha", "Missing"]


def test_invalid_advanced_range_keeps_table_and_allows_correction(player_paths):
    app = AppTest.from_function(_list_app, args=(str(player_paths.config_dir.parent),)).run()
    widget(app, "number_input", "Minimum pris").set_value(3_000_000)
    widget(app, "number_input", "Maksimum pris").set_value(1_000_000)
    button(app, "Anvend filtre").click().run()

    assert not app.exception
    assert app.error
    assert app.dataframe
    assert not button(app, "Anvend filtre").disabled
    widget(app, "number_input", "Maksimum pris").set_value(4_000_000)
    button(app, "Anvend filtre").click().run()
    assert not app.exception
    assert not app.error
    assert app.dataframe[0].value["Navn"].tolist() == ["Missing", "Gamma"]


def test_historical_detail_link_keeps_round_and_exact_fetch_after_refetch(player_paths):
    store = holdet.PlayerStatisticsStore(player_paths.snapshot_dir)
    changed = replace(statistics(3), entries=statistics(3).entries[1:])
    store.save(changed, now=NOW + timedelta(minutes=9))
    player = statistics(3).entries[0]
    with patch("holdet_lib.HoldetClient") as client:
        app = AppTest.from_file(APP_PATH).run(timeout=15)
        navigate(
            app, "player", locale="da", game=GAME.slug,
            player=holdet.player_identity(GAME, player), round=3,
            snapshot=(NOW + timedelta(minutes=3)).isoformat(),
        )

    assert not app.exception
    assert client.mock_calls == []
    assert app.title[0].value == "Alpha"
    assert next(item for item in app.metric if item.label == "Aktuel pris").value == "1.000.000"
    assert not any("ikke i den valgte spillerliste" in item.value for item in app.warning)
    curve = next(item.value for item in app.dataframe if "Pris" in item.value and "Runde" in item.value)
    assert curve["Runde"].tolist() == [1, 2, 3]


def test_removed_same_round_player_detail_keeps_last_known_observation(player_paths):
    store = holdet.PlayerStatisticsStore(player_paths.snapshot_dir)
    store.save(
        replace(statistics(3), entries=statistics(3).entries[1:]),
        now=NOW + timedelta(minutes=9),
    )
    player = statistics(3).entries[0]
    with patch("holdet_lib.HoldetClient") as client:
        app = AppTest.from_file(APP_PATH).run(timeout=15)
        navigate(
            app, "player", locale="da", game=GAME.slug,
            player=holdet.player_identity(GAME, player), round=3,
        )

    assert not app.exception
    assert client.mock_calls == []
    assert app.title[0].value == "Alpha"
    assert any("ikke i den valgte spillerliste" in item.value for item in app.warning)
    assert next(item for item in app.metric if item.label == "Sikkerhed").value == "Ikke verificeret"
    curve = next(item.value for item in app.dataframe if "Pris" in item.value and "Runde" in item.value)
    assert curve["Runde"].tolist() == [1, 2]


def test_unknown_round_remains_unverified_in_player_detail_history(player_paths):
    store = holdet.PlayerStatisticsStore(player_paths.snapshot_dir)
    store.save(
        replace(statistics(3), round_status="unknown"),
        now=NOW + timedelta(minutes=9),
    )
    player = statistics(3).entries[0]
    app = AppTest.from_file(APP_PATH).run(timeout=15)
    navigate(
        app, "player", locale="da", game=GAME.slug,
        player=holdet.player_identity(GAME, player), round=3,
    )

    assert not app.exception
    assert next(item for item in app.metric if item.label == "Sikkerhed").value == "Ikke verificeret"
    curve = next(item.value for item in app.dataframe if "Pris" in item.value and "Runde" in item.value)
    assert curve.loc[curve["Runde"] == 3, "Datastatus"].iloc[0] == "Ikke verificeret"
