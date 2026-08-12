"""Explicit network canary for the public player payload contracts."""

from __future__ import annotations

import json

import pytest

from holdet_lib import HoldetClient
from holdet_lib.flight import extract_flight_text
from holdet_lib.http import HttpClient


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


def _public_rows(html: str) -> tuple[dict[str, object], ...]:
    flight = extract_flight_text(html)
    decoder = json.JSONDecoder()
    marker = '"rows":'
    offset = 0
    candidates: list[tuple[dict[str, object], ...]] = []
    while True:
        marker_index = flight.find(marker, offset)
        if marker_index < 0:
            break
        start = marker_index + len(marker)
        try:
            value, consumed = decoder.raw_decode(flight[start:])
        except json.JSONDecodeError:
            offset = start + 1
            continue
        if (
            isinstance(value, list)
            and value
            and all(isinstance(item, dict) for item in value)
            and any("person" in item for item in value)
        ):
            candidates.append(tuple(value))
        offset = start + consumed
    assert candidates, "Den offentlige statisticsside indeholder ingen spillerrækker"
    return max(candidates, key=len)


@pytest.mark.parser_canary
@pytest.mark.parametrize(("url", "expected_format", "expected_unit"), PUBLIC_GAMES)
def test_current_public_player_contract(
    url: str,
    expected_format: str,
    expected_unit: str,
) -> None:
    http = HttpClient()
    statistics_pages: list[str] = []

    def fetch_text(target: str, *, accept: str = "text/html") -> str:
        content = http.fetch_text(target, accept=accept)
        if "/statistics" in target:
            statistics_pages.append(content)
        return content

    result = HoldetClient(text_fetcher=fetch_text).fetch_players(url)

    assert result.format == expected_format
    assert result.unit == expected_unit
    assert result.round_number > 0
    assert result.entries
    for entry in result.entries:
        assert entry.name.strip()
        assert entry.team.strip()
        assert entry.position.strip()
    assert statistics_pages
    raw_rows = _public_rows(statistics_pages[-1])
    raw_stats = {
        field: all(
            field in item and isinstance(item[field], (dict, list))
            for item in raw_rows
        )
        for field in ("stats", "totalStats")
    }
    optional_contract = {
        "popularity": any(item.popularity is not None for item in result.entries),
        "popularityChange": any(
            item.popularity_change is not None for item in result.entries
        ),
        "trend": any(item.trend is not None for item in result.entries),
        "index": any(item.index is not None for item in result.entries),
        "stats": raw_stats["stats"]
        and (
            not any(item["stats"] not in ({}, []) for item in raw_rows)
            or any(item.stats for item in result.entries)
        ),
        "totalStats": raw_stats["totalStats"]
        and (
            not any(item["totalStats"] not in ({}, []) for item in raw_rows)
            or any(item.total_stats for item in result.entries)
        ),
    }
    assert all(optional_contract.values()), (
        "Offentlig spillerpayload har ændret de research-gated scoutingfelter: "
        f"{optional_contract}. Produktionsparseren skal fortsat være fail-closed."
    )
