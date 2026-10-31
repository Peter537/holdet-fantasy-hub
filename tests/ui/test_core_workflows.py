"""Browser-level cache-only downloads for complete game and competition scopes."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, expect

from .conftest import UiServer


def _files(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for folder in (root / "config", root / "data") if folder.exists()
        for path in folder.rglob("*") if path.is_file()
    }


def _download(page: Page) -> dict:
    page.get_by_role("button", name="Download JSON", exact=True).click()
    with page.expect_download(timeout=20_000) as info:
        page.get_by_role("button", name="Download samlet JSON", exact=True).click()
    return json.loads(Path(info.value.path()).read_text(encoding="utf-8"))


@pytest.mark.ui
def test_home_game_picker_supports_keyboard_in_current_tab(page: Page, ui_server: UiServer) -> None:
    page.set_viewport_size({"width": 1920, "height": 910})
    page.goto(ui_server.base_url + "/", wait_until="domcontentloaded")
    picker = page.get_by_role("button", name="Åbn spil")
    expect(picker).to_be_visible(timeout=20_000)
    picker.focus()
    page.keyboard.press("Enter")
    options = page.get_by_role("menuitem")
    expect(options.first).to_be_visible()
    target = options.nth(1).inner_text().strip()
    page.keyboard.press("ArrowDown")
    expect(options.nth(1)).to_be_focused()
    page.keyboard.press("Enter")
    expect(page.get_by_role("heading", name=target, exact=True)).to_be_visible(timeout=20_000)


@pytest.mark.ui
def test_home_game_grid_opens_selected_game(page: Page, ui_server: UiServer) -> None:
    page.set_viewport_size({"width": 1920, "height": 910})
    page.goto(ui_server.base_url + "/", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="Mine managerspil", exact=True)).to_be_visible(timeout=20_000)
    grid = page.locator("[data-testid='stDataFrame']").first
    expect(grid).to_be_visible(timeout=20_000)
    canvas = grid.locator("canvas[data-testid='data-grid-canvas']")
    expect(canvas).to_be_visible()
    bounds = canvas.bounding_box()
    assert bounds is not None
    assert bounds["y"] + bounds["height"] <= 910
    page.mouse.click(bounds["x"] + 100, bounds["y"] + 60)
    expect(page.get_by_role("heading", name="Golf Manager", exact=True)).to_be_visible(timeout=20_000)


@pytest.mark.ui
def test_home_game_grid_keeps_correct_identity_after_sort(page: Page, ui_server: UiServer) -> None:
    page.set_viewport_size({"width": 1920, "height": 910})
    page.goto(ui_server.base_url + "/", wait_until="domcontentloaded")
    canvas = page.locator("[data-testid='stDataFrame'] canvas[data-testid='data-grid-canvas']").first
    expect(canvas).to_be_visible(timeout=20_000)
    bounds = canvas.bounding_box()
    assert bounds is not None
    page.mouse.click(bounds["x"] + 100, bounds["y"] + 18)
    page.mouse.click(bounds["x"] + 100, bounds["y"] + 18)
    page.mouse.click(bounds["x"] + 100, bounds["y"] + 60)
    expect(page.get_by_role("heading", name="Tourspillet 2026", exact=True)).to_be_visible(timeout=20_000)


@pytest.mark.ui
def test_home_comparison_table_reflows_without_document_overflow(page: Page, ui_server: UiServer) -> None:
    page.set_viewport_size({"width": 375, "height": 900})
    page.goto(ui_server.base_url + "/", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="Mine managerspil", exact=True)).to_be_visible(timeout=20_000)
    grid = page.locator("[data-testid='stDataFrame']").first
    expect(grid).to_be_visible(timeout=20_000)
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    )
    expect(grid.get_by_role("region", name=re.compile("Aktive managerspil"))).to_be_visible()


@pytest.mark.ui
def test_downloads_include_full_catalog_and_only_scope_teams_without_writes_or_network(page: Page, ui_server: UiServer) -> None:
    page.set_viewport_size({"width": 1920, "height": 910})
    requests = []
    page.on("request", lambda req: requests.append(req.url))
    before = _files(ui_server.data_root)
    page.goto(ui_server.base_url + "/game?locale=da&game=tour-de-france-2026&section=players", wait_until="domcontentloaded")
    expect(page.get_by_text("60 af 60 spillere", exact=False)).to_be_visible(timeout=20_000)
    search = page.get_by_role("textbox", name="Søg spillere")
    search.fill("Spiller 01")
    search.press("Enter")
    expect(page.get_by_text("1 af 60 spillere", exact=False)).to_be_visible(timeout=20_000)
    game = _download(page)
    assert game["scope"]["type"] == "game"
    assert len(game["players"]["current"]["entries"]) == 60
    assert {team["id"] for team in game["teams"]} == set(range(701, 709))
    assert {group["id"] for group in game["competitions"]} == {"ui-testliga", "ui-testcup"}

    page.goto(ui_server.base_url + "/group?group=ui-testcup", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="UI Testcup", exact=True)).to_be_visible(timeout=20_000)
    competition = _download(page)
    assert competition["scope"]["type"] == "competition"
    assert competition["scope"]["competition_id"] == "ui-testcup"
    assert len(competition["players"]["current"]["entries"]) == 60
    assert {team["id"] for team in competition["teams"]} == set(range(701, 705))
    assert [group["id"] for group in competition["competitions"]] == ["ui-testcup"]
    assert _files(ui_server.data_root) == before
    assert not (ui_server.data_root / "remote-attempts.log").exists()
    assert not [url for url in requests if urlparse(url).scheme in {"http", "https"} and urlparse(url).hostname not in {"127.0.0.1", "localhost", "::1"}]


@pytest.mark.ui
@pytest.mark.parametrize(("route", "last_round"), (
    ("/group?group=ui-testliga&section=history", 3),
    ("/group?group=ui-long-history&section=history", 20),
    ("/game?locale=da&game=tour-de-france-2026&section=history", 3),
))
def test_history_axis_uses_only_real_integer_rounds(page: Page, ui_server: UiServer, route: str, last_round: int) -> None:
    page.set_viewport_size({"width": 1920, "height": 910})
    page.goto(ui_server.base_url + route, wait_until="domcontentloaded")
    chart = page.locator("[data-testid='stVegaLiteChart']").first
    expect(chart).to_be_visible(timeout=20_000)
    ticks = chart.locator("g[aria-label^='X-axis'] g.role-axis-label text")
    expect(ticks.first).to_be_visible(timeout=20_000)
    labels = ticks.all_text_contents()
    assert labels and all(label.isascii() and label.isdecimal() for label in labels), labels
    values = [int(label) for label in labels]
    assert len(values) == len(set(values))
    assert all(1 <= value <= last_round for value in values)
    if route == "/group?group=ui-testliga&section=history":
        legend = chart.locator("g.role-legend")
        expect(legend).to_be_visible(timeout=20_000)
        legend_labels = legend.locator("text").all_text_contents()
        assert {"Nordlys 1", "Nordlys 2", "Nordlys 3"} <= set(legend_labels)
        first_point = chart.locator("[aria-label^='Runde:']").first.get_attribute("aria-label") or ""
        assert "Holdværdi (mio.): 359.99998" in first_point
        assert "Holdværdi: 359.999.980" in first_point


@pytest.mark.ui
def test_tournament_history_respects_selected_round_and_diagnostics_stay_collapsed(page: Page, ui_server: UiServer) -> None:
    page.set_viewport_size({"width": 1920, "height": 910})
    page.goto(ui_server.base_url + "/group?group=ui-testcup&section=history", wait_until="domcontentloaded")
    expect(page.get_by_role("heading", name="UI Testcup", exact=True)).to_be_visible(timeout=20_000)
    tabs = page.get_by_role("tablist").first
    expect(tabs).to_be_visible()
    bounds = tabs.bounding_box()
    assert bounds is not None and bounds["y"] + bounds["height"] <= 910
    diagnostics = page.get_by_text("Datastatus", exact=True)
    expect(diagnostics).to_be_visible()
    details = diagnostics.locator("xpath=ancestor::*[@data-testid='stExpander']").locator("details")
    assert not details.evaluate("node => node.open")

    chart = page.locator("[data-testid='stVegaLiteChart']").first
    expect(chart).to_be_visible(timeout=20_000)
    points = chart.locator("[aria-label^='Runde:']")
    expect(points.first).to_be_attached(timeout=20_000)
    assert any("Runde: 3;" in label for label in points.evaluate_all("nodes => nodes.map(n => n.getAttribute('aria-label'))"))
    cutoff = page.get_by_role("combobox", name="Vis turneringen til og med runde")
    cutoff.click()
    page.get_by_role("option", name="1", exact=True).click()
    expect(cutoff).to_have_value("1")
    page.wait_for_function("""() => {
        const labels = [...document.querySelectorAll('[data-testid="stVegaLiteChart"] [aria-label^="Runde:"]')]
            .map(node => node.getAttribute('aria-label'));
        return labels.length > 0 && labels.every(label => label.startsWith('Runde: 1;'));
    }""", timeout=20_000)
    labels = points.evaluate_all("nodes => nodes.map(n => n.getAttribute('aria-label'))")
    assert labels and all(label.startswith("Runde: 1;") for label in labels)
    assert any("Holdværdi: 359.999.980" in label for label in labels)
    assert not any("Holdværdi: 360.000.000" in label for label in labels)
