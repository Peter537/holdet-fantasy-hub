"""Explicit network canary for the public player payload contracts."""

from __future__ import annotations

import pytest

from holdet_lib import HoldetClient


PUBLIC_GAMES = (
    ("https://www.holdet.dk/da/fantasy/super-manager-fall-2026", "soccer", "money"),
    ("https://www.holdet.dk/da/fantasy/tour-de-france-2026", "cycling", "money"),
    (
        "https://www.holdet.dk/da/fantasy/tour-de-france-manager-2026",
        "cycling",
        "points",
    ),
    ("https://www.holdet.dk/da/fantasy/motor-manager-2026", "formula1", "money"),
    ("https://www.holdet.dk/da/fantasy/golf-manager-2026", "golf", "points"),
)


@pytest.mark.parser_canary
@pytest.mark.parametrize(("url", "expected_format", "expected_unit"), PUBLIC_GAMES)
def test_current_public_player_contract(
    url: str,
    expected_format: str,
    expected_unit: str,
) -> None:
    result = HoldetClient().fetch_players(url)

    assert result.format == expected_format
    assert result.unit == expected_unit
    assert result.round_number > 0
    assert result.entries
    for entry in result.entries:
        assert entry.name.strip()
        assert entry.team.strip()
        assert entry.position.strip()
