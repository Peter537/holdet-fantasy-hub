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


def _expected_stats(raw: dict | list) -> dict[str, float]:
    if isinstance(raw, dict):
        pairs = raw.items()
    else:
        def first(item, keys):
            return next((item[k] for k in keys if item.get(k) is not None), None)

        pairs = (
            (first(item, ("name", "key", "title", "label", "stat")),
             first(item, ("value", "score", "total")))
            for item in raw if isinstance(item, dict)
        )
    result = {}
    seen = set()
    for name, value in pairs:
        if not isinstance(name, str):
            continue
        name = name.strip()
        if (not name or len(name) > 80 or any(ord(c) < 32 for c in name)
                or name.casefold() in seen or isinstance(value, bool)
                or not isinstance(value, (int, float)) or not isfinite(value)):
            continue
        result[name] = float(value)
        seen.add(name.casefold())
    return result


def _assert_player_stats(raw_rows, entries):
    for raw, entry in zip(raw_rows, entries, strict=True):
        if raw.get("id") is not None:
            assert int(raw["id"]) == entry.entry_id
        for field, parsed in (("stats", entry.stats), ("totalStats", entry.total_stats)):
            source = raw.get(field)
            assert isinstance(source, (dict, list)), (entry.name, field)
            expected = _expected_stats(source)
            assert len(parsed) == len(expected), (entry.name, field)
            assert {stat.name: stat.value for stat in parsed} == expected, (entry.name, field)
    for field, attribute in (("stats", "stats"), ("totalStats", "total_stats")):
        if any(raw[field] for raw in raw_rows):
            assert any(getattr(entry, attribute) for entry in entries), field


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
    _assert_player_stats(raw_rows, result.entries)
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
    assert all(optional_contract.values()), (
        "Offentlig spillerpayload har ændret de research-gated scoutingfelter: "
        f"{optional_contract}. Produktionsparseren skal fortsat være fail-closed."
    )
