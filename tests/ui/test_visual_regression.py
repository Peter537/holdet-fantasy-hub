"""Local-only responsive visual baselines for the Windows UI."""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from .conftest import UiServer
from .snapshot import assert_ui_snapshot


CASES = (
    ("home-375", 375, "/", "Mine managerspil"),
    ("home-768", 768, "/", "Mine managerspil"),
    ("home-1280", 1280, "/", "Mine managerspil"),
    ("home-1920", 1920, "/", "Mine managerspil"),
    ("game-1920", 1920, "/game?locale=da&game=tour-de-france-2026", "Tourspillet 2026"),
    ("league-1920", 1920, "/group?group=ui-testliga", "UI Testliga"),
    ("history-1920", 1920, "/group?group=ui-testliga&section=history", "UI Testliga"),
    ("tournament-1920", 1920, "/group?group=ui-testcup", "UI Testcup"),
    ("settings-1920", 1920, "/settings", "Indstillinger"),
    (
        "round-center-375",
        375,
        "/game?locale=da&game=tour-de-france-2026&section=round-center",
        "Rundecenter",
    ),
    (
        "round-center-1280",
        1280,
        "/game?locale=da&game=tour-de-france-2026&section=round-center",
        "Rundecenter",
    ),
    (
        "player-list-dense-1280",
        1280,
        "/game?locale=da&game=tour-de-france-2026&section=players",
        "Tourspillet 2026",
    ),
    (
        "scouting-375",
        375,
        "/scouting?locale=da&game=tour-de-france-2026&view=watchlist",
        "Scouting",
    ),
    (
        "scouting-1280",
        1280,
        "/scouting?locale=da&game=tour-de-france-2026&view=smartlists",
        "Scouting",
    ),
    (
        "player-detail-1280",
        1280,
        "/player?locale=da&game=tour-de-france-2026&player=da%3Atour-de-france-2026%3Aentry%3A50000",
        "Spiller 01",
    ),
)


@pytest.mark.ui
@pytest.mark.parametrize(("name", "width", "route", "heading"), CASES)
def test_visual_baseline(
    page: Page,
    ui_server: UiServer,
    request: pytest.FixtureRequest,
    name: str,
    width: int,
    route: str,
    heading: str,
) -> None:
    page.set_viewport_size({"width": width, "height": 910 if width == 1920 else 900})
    page.goto(ui_server.base_url + route, wait_until="domcontentloaded")
    if name.startswith("round-center-"):
        page.get_by_text("Datadetaljer og rundecenter", exact=True).click(timeout=20_000)
    expect(page.get_by_role("heading", name=heading, exact=True).first).to_be_visible(
        timeout=20_000
    )
    expect(page.locator("[data-testid='stException']")).to_have_count(0)
    page.locator("[data-testid='stStatusWidget']").wait_for(
        state="detached",
        timeout=20_000,
    )
    if name == "player-list-dense-1280":
        # Streamlit can finish its rerun before the dataframe canvas is laid out.
        page.wait_for_function(
            """() => {
                const canvas = document.querySelector('[data-testid="stDataFrame"] canvas');
                if (!canvas) return false;
                const rect = canvas.getBoundingClientRect();
                return rect.width > 200 && rect.height > 100;
            }""",
            timeout=20_000,
        )
    if name == "history-1920":
        expect(page.locator("[data-testid='stVegaLiteChart'] svg").first).to_be_visible(timeout=20_000)
    if name.startswith("round-center-"):
        # The expander is below the fold; Playwright scrolls it into view before
        # clicking. Capture the same top-of-page position on every test run.
        page.evaluate("""() => {
            window.scrollTo(0, 0);
            document.querySelectorAll('*').forEach(node => {
                if (node.scrollHeight > node.clientHeight) node.scrollTop = 0;
            });
        }""")
    page.wait_for_timeout(300)
    assert_ui_snapshot(page, name, request)
