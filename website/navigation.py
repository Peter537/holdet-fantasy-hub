"""Canonical native navigation for the Streamlit application."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import streamlit as st


class PageId(StrEnum):
    HOME = "home"
    MANAGE_GAMES = "manage-games"
    ARCHIVE = "archive"
    PLAYERS = "players"
    TEAMS = "teams"
    MANAGERS = "managers"
    CALENDAR = "calendar"
    DATA = "data"
    GAME = "game"
    GROUP = "group"
    TEAM = "team"
    PLAYER = "player"
    ALERTS = "alerts"
    NOT_FOUND = "not-found"


@dataclass(frozen=True)
class PageSpec:
    page_id: PageId
    filename: str
    title: str
    icon: str
    url_path: str | None = None
    default: bool = False
    hidden: bool = False


PAGE_SPECS = (
    PageSpec(PageId.HOME, "home.py", "Mine managerspil", ":material/home:", default=True),
    PageSpec(PageId.MANAGE_GAMES, "manage-games.py", "Administrér spil", ":material/add:", "manage-games"),
    PageSpec(PageId.ARCHIVE, "archive.py", "Arkiv", ":material/archive:", "archive"),
    PageSpec(PageId.PLAYERS, "players.py", "Spillerstatistik", ":material/query_stats:", "players"),
    PageSpec(PageId.TEAMS, "teams.py", "Holdstatistik", ":material/groups:", "teams"),
    PageSpec(PageId.MANAGERS, "managers.py", "Managers", ":material/military_tech:", "managers"),
    PageSpec(PageId.CALENDAR, "calendar.py", "Kalender", ":material/calendar_month:", "calendar"),
    PageSpec(PageId.DATA, "data.py", "Data og lager", ":material/database:", "data"),
    PageSpec(PageId.GAME, "game.py", "Managerspil", ":material/sports_soccer:", "game", hidden=True),
    PageSpec(PageId.GROUP, "group.py", "Gruppe", ":material/leaderboard:", "group", hidden=True),
    PageSpec(PageId.TEAM, "team.py", "Hold", ":material/group:", "team", hidden=True),
    PageSpec(PageId.PLAYER, "player.py", "Spiller", ":material/person:", "player", hidden=True),
    PageSpec(PageId.ALERTS, "alerts.py", "Statusalarmer", ":material/notifications:", "alerts", hidden=True),
    PageSpec(PageId.NOT_FOUND, "not-found.py", "Siden blev ikke fundet", ":material/error:", "not-found", hidden=True),
)

_SPEC_BY_ID = {spec.page_id: spec for spec in PAGE_SPECS}
_LEGACY_VIEWS = {
    "home": PageId.HOME,
    "manage-games": PageId.MANAGE_GAMES,
    "archive": PageId.ARCHIVE,
    "players": PageId.PLAYERS,
    "teams": PageId.TEAMS,
    "managers": PageId.MANAGERS,
    "hall-of-fame": PageId.MANAGERS,
    "calendar": PageId.CALENDAR,
    "data": PageId.DATA,
    "game": PageId.GAME,
    "group": PageId.GROUP,
    "team": PageId.TEAM,
    "player": PageId.PLAYER,
    "alerts": PageId.ALERTS,
}


def page_source(page_id: PageId) -> Path:
    return Path(__file__).with_name("app_pages") / _SPEC_BY_ID[page_id].filename


def create_pages() -> dict[PageId, Any]:
    return {
        spec.page_id: st.Page(
            page_source(spec.page_id),
            title=spec.title,
            icon=spec.icon,
            url_path=spec.url_path,
            default=spec.default,
            visibility="hidden" if spec.hidden else "visible",
        )
        for spec in PAGE_SPECS
    }


def selected_page_id(selected: Any, pages: dict[PageId, Any]) -> PageId:
    for page_id, page in pages.items():
        if selected is page or selected.url_path == page.url_path:
            return page_id
    return PageId.NOT_FOUND


def page_id_for_legacy_view(view: str) -> PageId:
    return _LEGACY_VIEWS.get(view, PageId.NOT_FOUND)


def normalized_query_params(parameters: dict[str, object]) -> dict[str, object]:
    return {
        key: value if isinstance(value, (str, list, tuple)) else str(value)
        for key, value in parameters.items()
        if value is not None and key != "view"
    }


def go_to(page_id: PageId | str, **parameters: object) -> None:
    target = (
        page_id_for_legacy_view(page_id)
        if isinstance(page_id, str)
        else page_id
    )
    st.switch_page(
        page_source(target),
        query_params=normalized_query_params(parameters),
    )


def page_link(
    page_id: PageId,
    label: str,
    *,
    icon: str | None = None,
    width: str = "content",
    **parameters: object,
) -> None:
    st.page_link(
        page_source(page_id),
        label=label,
        icon=icon,
        width=width,
        query_params=normalized_query_params(parameters),
    )


def relative_url(page_id: PageId, **parameters: object) -> str:
    spec = _SPEC_BY_ID[page_id]
    path = "/" if spec.default else f"/{spec.url_path}"
    query = urlencode(normalized_query_params(parameters), doseq=True)
    return f"{path}?{query}" if query else path


def redirect_legacy_query() -> None:
    view = st.query_params.get("view")
    if view is None:
        return
    parameters = st.query_params.to_dict()
    parameters.pop("view", None)
    go_to(page_id_for_legacy_view(str(view)), **parameters)
