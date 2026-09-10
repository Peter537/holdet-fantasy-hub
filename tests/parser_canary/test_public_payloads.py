"""Explicit network canary for the public player payload contracts."""

from __future__ import annotations

import json
from math import isfinite

import pytest

from holdet_lib import HoldetClient
from holdet_lib.flight import extract_flight_text
from holdet_lib.http import HttpClient


PUBLIC_GAMES = (
    ("https://www.holdet.dk/da/fantasy/premier-league-fantasy-fall-2026", "soccer", "money"),
    ("https://www.holdet.dk/da/fantasy/super-manager-fall-2026", "soccer", "money"),
    ("https://www.holdet.dk/da/fantasy/tour-de-france-2026", "cycling", "money"),
    (
        "https://www.holdet.dk/da/fantasy/tour-de-france-manager-2026",
        "cycling",
        "points",
    ),
    ("https://www.holdet.dk/da/fantasy/motor-manager-2026", "formula1", "money"),
    ("https://www.holdet.dk/da/fantasy/golf-manager-2026", "golf", "points"),
    ("https://www.holdet.dk/da/fantasy/football-fantasy-2026", "nfl", "money"),
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
    assert len(raw_rows) == len(result.entries), (
        "Den offentlige statisticssides rækker matcher ikke parserresultatet"
    )
    raw_stats = {
        field: all(
            field in item and isinstance(item[field], (dict, list))
            for item in raw_rows
        )
        for field in ("stats", "totalStats")
    }
    optional_contract: dict[str, bool] = {}
    for field, attribute in (
        ("popularity", "popularity"),
        ("popularityChange", "popularity_change"),
        ("trend", "trend"),
        ("index", "index"),
    ):
        found = False
        aligned_values = True
        for raw, parsed in zip(raw_rows, result.entries, strict=True):
            if field not in raw:
                continue
            found = True
            raw_value = raw[field]
            parsed_value = getattr(parsed, attribute)
            if raw_value is None:
                aligned_values = aligned_values and parsed_value is None
            elif (
                isinstance(raw_value, (int, float))
                and not isinstance(raw_value, bool)
                and isfinite(raw_value)
            ):
                aligned_values = aligned_values and parsed_value == float(raw_value)
            else:
                aligned_values = aligned_values and parsed_value is None
        optional_contract[field] = found and aligned_values
    optional_contract.update(
        {
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
    )
    assert all(optional_contract.values()), (
        "Offentlig spillerpayload har ændret de research-gated scoutingfelter: "
        f"{optional_contract}. Produktionsparseren skal fortsat være fail-closed."
    )
