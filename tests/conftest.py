"""Opt-in controls shared by the offline, UI, and parser-canary suites."""

from __future__ import annotations

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-parser-canary",
        action="store_true",
        default=False,
        help="contact current public Holdet payloads",
    )
    parser.addoption(
        "--run-ui",
        action="store_true",
        default=False,
        help="run the local Windows/Chromium UI suite",
    )
    parser.addoption(
        "--update-ui-snapshots",
        action="store_true",
        default=False,
        help="create or replace local ignored UI screenshot baselines",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "parser_canary: explicitly contacts public Holdet payloads",
    )
    config.addinivalue_line(
        "markers",
        "ui: local opt-in Playwright UI regression and accessibility test",
    )


def pytest_collection_modifyitems(
    config: pytest.Config,
    items: list[pytest.Item],
) -> None:
    gates = (
        (
            "parser_canary",
            "--run-parser-canary",
            "parser canary requires --run-parser-canary",
        ),
        ("ui", "--run-ui", "UI tests require --run-ui"),
    )
    for marker, option, reason in gates:
        if config.getoption(option):
            continue
        skip = pytest.mark.skip(reason=reason)
        for item in items:
            if item.get_closest_marker(marker) is not None:
                item.add_marker(skip)
