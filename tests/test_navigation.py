"""Canonical Streamlit routes and stable component identity contracts."""

from __future__ import annotations

import ast
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import holdet_lib as holdet
from website import navigation
from website import ui


PROJECT_ROOT = Path(__file__).parents[1]

CANONICAL_PATHS = {
    navigation.PageId.HOME: "/",
    navigation.PageId.MANAGE_GAMES: "/manage-games",
    navigation.PageId.ARCHIVE: "/archive",
    navigation.PageId.PLAYERS: "/players",
    navigation.PageId.TEAMS: "/teams",
    navigation.PageId.MANAGERS: "/managers",
    navigation.PageId.CALENDAR: "/calendar",
    navigation.PageId.DATA: "/data",
    navigation.PageId.SETTINGS: "/settings",
    navigation.PageId.SCOUTING: "/scouting",
    navigation.PageId.GAME: "/game",
    navigation.PageId.GROUP: "/group",
    navigation.PageId.TEAM: "/team",
    navigation.PageId.PLAYER: "/player",
    navigation.PageId.ALERTS: "/alerts",
    navigation.PageId.NOT_FOUND: "/not-found",
}


@pytest.mark.parametrize(("page_id", "path"), CANONICAL_PATHS.items())
def test_canonical_route_matrix(page_id: navigation.PageId, path: str) -> None:
    assert navigation.relative_url(page_id) == path
    assert navigation.page_source(page_id).is_file()


@pytest.mark.parametrize(
    ("legacy", "page_id"),
    (
        ("home", navigation.PageId.HOME),
        ("manage-games", navigation.PageId.MANAGE_GAMES),
        ("archive", navigation.PageId.ARCHIVE),
        ("players", navigation.PageId.PLAYERS),
        ("teams", navigation.PageId.TEAMS),
        ("managers", navigation.PageId.MANAGERS),
        ("hall-of-fame", navigation.PageId.MANAGERS),
        ("calendar", navigation.PageId.CALENDAR),
        ("data", navigation.PageId.DATA),
        ("settings", navigation.PageId.SETTINGS),
        ("scouting", navigation.PageId.SCOUTING),
        ("game", navigation.PageId.GAME),
        ("group", navigation.PageId.GROUP),
        ("team", navigation.PageId.TEAM),
        ("player", navigation.PageId.PLAYER),
        ("alerts", navigation.PageId.ALERTS),
        ("removed-route", navigation.PageId.NOT_FOUND),
    ),
)
def test_complete_legacy_route_matrix(
    legacy: str,
    page_id: navigation.PageId,
) -> None:
    assert navigation.page_id_for_legacy_view(legacy) is page_id


def test_navigation_helpers_preserve_and_escape_query_context() -> None:
    url = navigation.relative_url(
        navigation.PageId.PLAYER,
        locale="da",
        game="tour & test",
        player="Søren/1",
        round=7,
        ignored=None,
        view="old",
    )
    assert url == (
        "/player?locale=da&game=tour+%26+test&player=S%C3%B8ren%2F1&round=7"
    )
    with patch.object(navigation.st, "switch_page") as switch:
        navigation.go_to(
            "hall-of-fame",
            manager="Åse",
            season="2026",
            view="discarded",
        )
    switch.assert_called_once_with(
        navigation.page_source(navigation.PageId.MANAGERS),
        query_params={"manager": "Åse", "season": "2026"},
    )


def test_all_streamlit_dataframes_have_stable_explicit_keys() -> None:
    forbidden_fragments = ("generated_at", "timestamp", "round_number")
    calls: list[tuple[Path, ast.Call]] = []
    for path in (PROJECT_ROOT / "website").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "dataframe"
            ):
                calls.append((path, node))
    assert calls
    for path, call in calls:
        key = next((item.value for item in call.keywords if item.arg == "key"), None)
        assert key is not None, f"{path}:{call.lineno} mangler dataframe-key"
        source = ast.unparse(key)
        assert not any(value in source for value in forbidden_fragments), (
            f"{path}:{call.lineno} har en flygtig dataframe-key: {source}"
        )


class _Tab:
    def __init__(self, opened: bool) -> None:
        self.open = opened

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        return None


@pytest.mark.parametrize(
    ("section", "selected_tab", "renderer"),
    (
        ("round-center", 0, "_game_overview"),
        ("groups", 1, "_game_groups_tab"),
        ("players", 2, "_player_statistics_tab"),
    ),
)
def test_core_game_tabs_do_not_render_inactive_or_optional_tools(
    section: str, selected_tab: int, renderer: str,
) -> None:
    manager_game = holdet.normalize_manager_game("tour-de-france-2026", "Tour")
    tabs = tuple(_Tab(index == selected_tab) for index in range(3))
    renderers = {name: MagicMock() for name in (
        "_game_overview", "_game_round_center_tab", "_game_groups_tab", "_player_statistics_tab",
        "alerts_view", "_team_statistics_game_tab", "game_history_panel",
        "analysis_panel", "_manage_game", "_game_settings_tab",
    )}
    with ExitStack() as stack:
        stack.enter_context(patch.multiple(ui, **renderers))
        stack.enter_context(patch.object(ui, "_stateful_tabs", return_value=tabs))
        stack.enter_context(patch.object(ui, "_entity_header"))
        stack.enter_context(patch.object(ui.st, "query_params", {"section": section}))
        ui._game_view(manager_game, (), holdet.SnapshotIndex((), ()), MagicMock())
    renderers[renderer].assert_called_once()
    for name, spy in renderers.items():
        if name != renderer:
            spy.assert_not_called()


@pytest.mark.parametrize(
    ("section", "renderer"),
    (
        ("teams", "_team_statistics_game_tab"),
        ("history", "game_history_panel"),
        ("administration", "_manage_game"),
        ("settings", "_game_settings_tab"),
        ("alerts", "alerts_view"),
        ("analysis", "analysis_panel"),
    ),
)
def test_optional_game_sections_keep_their_deep_link_destination(
    section: str, renderer: str,
) -> None:
    manager_game = holdet.normalize_manager_game("tour-de-france-2026", "Tour")
    with ExitStack() as stack:
        selected = stack.enter_context(patch.object(ui, renderer))
        tabs = stack.enter_context(patch.object(ui, "_stateful_tabs"))
        stack.enter_context(patch.object(ui, "_entity_header"))
        stack.enter_context(patch.object(ui, "page_link"))
        stack.enter_context(patch.object(ui, "require_tool", return_value=True))
        stack.enter_context(patch.object(ui.st, "query_params", {"section": section}))
        ui._game_view(manager_game, (), holdet.SnapshotIndex((), ()), MagicMock(), read_only=True)
    selected.assert_called_once()
    if section != "history":
        assert selected.call_args.kwargs["read_only"] is True
    tabs.assert_not_called()
