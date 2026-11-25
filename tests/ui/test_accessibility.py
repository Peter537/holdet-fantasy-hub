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
    for _ in range(40):
        visible_focus = page.evaluate(
            """() => {
                const node = document.activeElement;
                if (!node || node === document.body) return false;
                const style = getComputedStyle(node);
                return node.matches(':focus-visible') && (
                    (style.outlineStyle !== 'none' && parseFloat(style.outlineWidth) > 0)
                    || style.boxShadow !== 'none'
                );
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
    expect(page.get_by_text("60 af 60 spillere")).to_be_visible()
    page.get_by_role("button", name="Flere filtre og kolonner").click()
    page.get_by_text("Avancerede filtre og kolonner", exact=True).click()
    sorter = page.get_by_role("combobox", name="Sortér efter")
    expect(sorter).to_have_value("Pris")
    sorter.click()
    page.get_by_role("option", name="Navn", exact=True).click()
    page.get_by_role("spinbutton", name="Minimum pris", exact=True).fill("8000000")

    scroller = page.get_by_test_id("stDataFrame").locator(".dvn-scroller")
    before = scroller.evaluate(
        """node => {
            node.scrollTop = 120;
            return node.scrollTop;
        }""",
    )
    assert before > 0
    page.get_by_role("button", name="Anvend filtre").click()
    expect(page.get_by_text("54 af 60 spillere")).to_be_visible(timeout=20_000)
    expect(page.get_by_role("combobox", name="Sortering", exact=True)).to_have_value("Navn")
    page.get_by_test_id("stStatusWidget").wait_for(state="detached", timeout=20_000)
    expect(scroller).to_be_visible()
    assert scroller.evaluate("node => node.scrollTop") == before


@pytest.mark.ui
@pytest.mark.parametrize("width", (375, 1280))
def test_round_center_is_responsive_and_keyboard_named(
    page: Page,
    ui_server: UiServer,
    width: int,
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(ui_server.base_url + ROUND_CENTER_ROUTE, wait_until="domcontentloaded")
    page.get_by_text("Datadetaljer og rundecenter", exact=True).click(timeout=20_000)
    expect(
        page.get_by_role("heading", name="Rundecenter", exact=True).first
    ).to_be_visible(timeout=20_000)
    page.locator("[data-testid='stStatusWidget']").wait_for(
        state="detached",
        timeout=20_000,
    )

    heading_items = page.locator("h1,h2,h3,h4,h5,h6").evaluate_all(
        "nodes => nodes.map(node => ({level: Number(node.tagName.slice(1)), text: node.innerText}))"
    )
    headings = [item["level"] for item in heading_items]
    assert headings and all(
        current <= previous + 1
        for previous, current in zip(headings, headings[1:])
    ), heading_items
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
    titles = ("Pris mod Form 3", "Vækst mod stabilitet", "Potentiale mod risiko")
    plots = page.get_by_test_id("stVegaLiteChart")
    expect(plots).to_have_count(len(titles), timeout=20_000)
    for index, title in enumerate(titles):
        expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
        expect(plots.nth(index).locator("svg")).to_be_visible()
        expander = page.locator("details").filter(
            has=page.get_by_text(f"Datatabel · {title}", exact=True)
        )
        disclosure = expander.locator("summary")
        disclosure.press("Enter")
        table = expander.get_by_test_id("stDataFrame")
        expect(table).to_be_visible(timeout=20_000)
        expect(disclosure).to_be_focused()
        disclosure.press("Tab")
        columns = table.get_by_role("button", name="Show/hide columns", exact=True)
        expect(columns).to_be_focused()
        assert columns.evaluate("node => node.matches(':focus-visible')")
        columns.press("Shift+Tab")
        expect(disclosure).to_be_focused()
    page.evaluate("document.body.style.zoom = '2'")
    page.wait_for_timeout(100)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    )
