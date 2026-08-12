"""Small accessibility and fragment-state smoke suite."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from .conftest import UiServer


GAME_ROUTE = "/game?locale=da&game=tour-de-france-2026&section=players"
ROUND_CENTER_ROUTE = (
    "/game?locale=da&game=tour-de-france-2026&section=round-center"
)
SCOUTING_ROUTE = (
    "/game?locale=da&game=tour-de-france-2026&section=players&panel=scouting"
)


def _open_players(page: Page, server: UiServer, width: int = 1280) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(server.base_url + GAME_ROUTE, wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="Tourspillet 2026", exact=True)).to_be_visible(
        timeout=20_000
    )
    expect(page.locator("[data-testid='stDataFrame']")).to_be_visible(timeout=20_000)


@pytest.mark.ui
def test_accessibility_smoke(page: Page, ui_server: UiServer) -> None:
    _open_players(page, ui_server)
    expect(page.locator("h1")).to_have_count(1)
    levels = page.locator("h1,h2,h3,h4,h5,h6").evaluate_all(
        "nodes => nodes.map(node => Number(node.tagName.slice(1)))"
    )
    levels = levels[levels.index(1) :]
    assert levels[0] == 1
    assert all(current <= previous + 1 for previous, current in zip(levels, levels[1:]))

    unnamed = page.get_by_role("button").evaluate_all(
        "nodes => nodes.filter(node => !(node.getAttribute('aria-label') || node.innerText.trim())).length"
    )
    assert unnamed == 0

    tabs = page.get_by_role("tab")
    assert tabs.count() >= 2
    for tablist in page.get_by_role("tablist").all():
        assert tablist.locator("[role='tab'][aria-selected='true']").count() == 1
    for panel in page.get_by_role("tabpanel").all():
        expect(panel).to_be_visible()

    page.keyboard.press("Tab")
    for _ in range(12):
        visible_focus = page.evaluate(
            """() => {
                const node = document.activeElement;
                if (!node || node === document.body) return false;
                const style = getComputedStyle(node);
                return style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0;
            }"""
        )
        if visible_focus:
            break
        page.keyboard.press("Tab")
    assert visible_focus

    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    )
    page.evaluate("document.body.style.zoom = '2'")
    page.wait_for_timeout(100)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    )


@pytest.mark.ui
def test_player_sort_and_dataframe_scroll_survive_fragment_rerun(
    page: Page,
    ui_server: UiServer,
) -> None:
    _open_players(page, ui_server)
    page.get_by_text("Filtre · 0", exact=True).click()
    page.get_by_text("Avancerede filtre og kolonner", exact=True).click()
    sorter = page.get_by_role("combobox", name="Sortér efter")
    expect(sorter).to_have_value("Pris")

    table = page.locator("[data-testid='stDataFrame']")
    before = table.evaluate(
        """root => {
            const nodes = [root, ...root.querySelectorAll('*')]
                .filter(node => node.scrollHeight > node.clientHeight + 20);
            const scroll = nodes.sort(
                (left, right) => (right.scrollHeight - right.clientHeight)
                    - (left.scrollHeight - left.clientHeight)
            )[0];
            if (!scroll) return null;
            scroll.scrollTop = Math.min(120, scroll.scrollHeight - scroll.clientHeight);
            return scroll.scrollTop;
        }"""
    )
    assert before and before > 0
    page.get_by_role("button", name="Anvend filtre").click()
    expect(page.locator("[data-testid='stDataFrame']")).to_be_visible(timeout=20_000)
    expect(page.get_by_role("combobox", name="Sortér efter")).to_have_value("Pris")
    after = page.locator("[data-testid='stDataFrame']").evaluate(
        """root => {
            const nodes = [root, ...root.querySelectorAll('*')]
                .filter(node => node.scrollHeight > node.clientHeight + 20);
            const scroll = nodes.sort(
                (left, right) => (right.scrollHeight - right.clientHeight)
                    - (left.scrollHeight - left.clientHeight)
            )[0];
            return scroll ? scroll.scrollTop : null;
        }"""
    )
    assert after == before


@pytest.mark.ui
@pytest.mark.parametrize("width", (375, 1280))
def test_round_center_is_responsive_and_keyboard_named(
    page: Page,
    ui_server: UiServer,
    width: int,
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(ui_server.base_url + ROUND_CENTER_ROUTE, wait_until="domcontentloaded")
    expect(
        page.get_by_role("heading", name="Rundecenter", exact=True).first
    ).to_be_visible(timeout=20_000)
    page.locator("[data-testid='stStatusWidget']").wait_for(
        state="detached",
        timeout=20_000,
    )

    headings = page.locator("h1,h2,h3,h4,h5,h6").evaluate_all(
        "nodes => nodes.map(node => Number(node.tagName.slice(1)))"
    )
    assert headings and all(
        current <= previous + 1
        for previous, current in zip(headings, headings[1:])
    )
    unnamed = page.get_by_role("button").evaluate_all(
        "nodes => nodes.filter(node => !(node.getAttribute('aria-label') || node.innerText.trim())).length"
    )
    assert unnamed == 0
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    )

    if width == 1280:
        page.evaluate("document.body.style.zoom = '2'")
        page.wait_for_timeout(100)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
        )


@pytest.mark.ui
def test_scouting_charts_have_tables_keyboard_focus_and_zoom(
    page: Page, ui_server: UiServer
) -> None:
    page.set_viewport_size({"width": 1280, "height": 900})
    page.goto(ui_server.base_url + SCOUTING_ROUTE, wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="Tourspillet 2026", exact=True)).to_be_visible(
        timeout=20_000
    )
    expect(page.get_by_text("Pris mod Form 3", exact=True)).to_be_visible(timeout=20_000)
    expect(page.locator("canvas").first).to_be_visible(timeout=20_000)
    page.get_by_text("Datatabel · Pris mod Form 3", exact=True).click()
    expect(page.locator("[data-testid='stDataFrame']").last).to_be_visible(timeout=20_000)
    page.keyboard.press("Tab")
    assert page.evaluate("document.activeElement !== document.body")
    page.evaluate("document.body.style.zoom = '2'")
    page.wait_for_timeout(100)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    )
