"""Pillow-backed screenshot assertions with small, explicit tolerances."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image, ImageChops
from playwright.sync_api import Page
import pytest


BASELINES = Path(__file__).with_name("baselines") / "chromium-win32"
ARTIFACTS = Path(__file__).with_name("artifacts")
UPDATE_COMMAND = (
    "py -3.14 -m pytest tests/ui -q --run-ui --browser chromium "
    "--update-ui-snapshots"
)


def assert_ui_snapshot(
    page: Page,
    name: str,
    request: pytest.FixtureRequest,
) -> None:
    actual = Image.open(
        BytesIO(
            page.screenshot(
                full_page=True,
                animations="disabled",
                caret="hide",
            )
        )
    ).convert("RGB")
    baseline_path = BASELINES / f"{name}.png"
    if request.config.getoption("--update-ui-snapshots"):
        baseline_path.parent.mkdir(parents=True, exist_ok=True)
        actual.save(baseline_path)
        return
    if not baseline_path.exists():
        pytest.fail(
            f"Manglende lokal UI-baseline: {baseline_path}. "
            f"Opret lokale, ignorerede baselines med: {UPDATE_COMMAND}"
        )
    expected = Image.open(baseline_path).convert("RGB")
    if actual.size != expected.size:
        _save_failure(name, actual, None)
        pytest.fail(
            f"UI-billedet {name} har størrelse {actual.size}; "
            f"baseline er {expected.size}."
        )

    difference = ImageChops.difference(actual, expected)
    changed = difference.point(lambda value: 255 if value > 10 else 0)
    changed_pixels = sum(
        1
        for red, green, blue in changed.get_flattened_data()
        if red or green or blue
    )
    ratio = changed_pixels / (actual.width * actual.height)
    if ratio > 0.001:
        _save_failure(name, actual, changed)
        pytest.fail(
            f"UI-billedet {name} ændrede {ratio:.3%} pixels "
            "(tilladt: 0,1 % efter kanal-tolerance 10)."
        )


def _save_failure(
    name: str,
    actual: Image.Image,
    difference: Image.Image | None,
) -> None:
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    actual.save(ARTIFACTS / f"{name}-actual.png")
    if difference is not None:
        difference.save(ARTIFACTS / f"{name}-diff.png")
