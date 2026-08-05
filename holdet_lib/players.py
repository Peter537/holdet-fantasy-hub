"""All-player statistics scraping and formatting."""

from __future__ import annotations

import re
from typing import Callable, Iterable
from urllib.parse import unquote, urlsplit

from .errors import PayloadError, UnsupportedGameError, UrlValidationError
from .flight import extract_flight_text
from .http import fetch_html
from .models import GameUrl, PlayerEntry, ScrapedGame
from .policies import KNOWN_ROUTE_VARIANTS, GamePolicy, legacy_policy


SUPPORTED_HOSTS = frozenset({"holdet.dk", "www.holdet.dk"})
SUPPORTED_VARIANTS = KNOWN_ROUTE_VARIANTS
# Kept as an empty public compatibility constant. No known variant is excluded.
EXCLUDED_VARIANTS: frozenset[str] = frozenset()
SORT_FIELDS = ("value", "name", "team", "position", "source")
SORT_ORDERS = ("asc", "desc")


def normalize_game_url(raw_url: str) -> GameUrl:
    value = raw_url.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise UrlValidationError(f"Ugyldig URL: {exc}") from exc

    if parsed.scheme.casefold() != "https":
        raise UrlValidationError("URL'en skal bruge https://")
    if parsed.username or parsed.password:
        raise UrlValidationError("URL'en må ikke indeholde loginoplysninger")
    if parsed.hostname is None or parsed.hostname.casefold() not in SUPPORTED_HOSTS:
        raise UrlValidationError("URL'ens værtsnavn skal være www.holdet.dk")
    if port not in (None, 443):
        raise UrlValidationError("URL'en må ikke bruge et ikke-standardiseret portnummer")

    segments = [unquote(segment) for segment in parsed.path.split("/") if segment]
    if len(segments) < 3 or segments[1].casefold() != "fantasy":
        raise UrlValidationError(
            "URL'en skal følge formatet https://www.holdet.dk/<locale>/fantasy/<game-slug>"
        )

    locale, slug = segments[0], segments[2]
    if not re.fullmatch(r"[A-Za-z]{2}(?:-[A-Za-z]{2})?", locale):
        raise UrlValidationError(f"Ikke-understøttet sprogsegment: {locale!r}")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", slug):
        raise UrlValidationError(f"Ugyldig spilslug: {slug!r}")
    return GameUrl(original=value, locale=locale, slug=slug)


def discover_variant(html: str) -> str:
    flight_text = extract_flight_text(html)
    found = list(
        dict.fromkeys(re.findall(r'"variant":"([A-Za-z0-9_]+)"', flight_text))
    )
    recognized = [
        variant
        for variant in found
        if variant in SUPPORTED_VARIANTS
    ]
    if len(recognized) > 1:
        raise PayloadError(
            "Spilpayloaden indeholdt modstridende varianter: "
            + ", ".join(recognized)
        )
    if recognized:
        variant = recognized[0]

        return variant
    if found:
        raise UnsupportedGameError(
            "Ikke-understøttet spilvariant fra Holdet.dk: " + ", ".join(found)
        )
    raise PayloadError("Spilvarianten kunne ikke findes i Nexus-payloaden")


def _looks_like_player_row(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    person = value.get("person")
    team = value.get("team")
    position = value.get("position")
    score = value.get("score")
    return (
        isinstance(person, dict)
        and isinstance(person.get("fullName"), str)
        and isinstance(team, dict)
        and isinstance(team.get("name"), str)
        and isinstance(position, dict)
        and isinstance(position.get("title"), str)
        and isinstance(score, (int, float))
        and not isinstance(score, bool)
    )


def _row_to_entry(row: dict[str, object], source_index: int) -> PlayerEntry:
    person = row["person"]
    team = row["team"]
    position = row["position"]
    score = row["score"]
    assert isinstance(person, dict)
    assert isinstance(team, dict)
    assert isinstance(position, dict)
    assert isinstance(score, (int, float)) and not isinstance(score, bool)
    score = _integer_value(score, f"entry {source_index} score")
    name = str(person["fullName"]).strip()
    team_name = str(team["name"]).strip()
    position_title = str(position["title"]).strip()
    if not name or not team_name or not position_title:
        raise PayloadError(f"Post {source_index} har et tomt påkrævet navn")
    return PlayerEntry(
        source_index=source_index,
        name=name,
        team=team_name,
        position=position_title,
        value=score,
        is_active=row.get("isActive", True) is not False,
        is_disabled=row.get("isDisabled", False) is True,
        is_injured=row.get("isInjured", False) is True,
        has_suspension=row.get("hasSuspension", False) is True,
        entry_id=_optional_integer_value(row.get("id"), f"entry {source_index} id"),
        person_id=_optional_integer_value(
            person.get("id"), f"entry {source_index} person id"
        ),
        total_growth=_optional_integer_value(
            row.get("totalGrowth"), f"entry {source_index} total growth"
        ),
        round_growth=_optional_integer_value(
            row.get("growth"), f"entry {source_index} round growth"
        ),
    )


def _integer_value(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PayloadError(f"{label} skal være et heltal")
    if isinstance(value, float):
        if not value.is_integer():
            raise PayloadError(f"{label} skal være et heltal: {value!r}")
        return int(value)
    return value


def _optional_integer_value(value: object, label: str) -> int | None:
    return None if value is None else _integer_value(value, label)


def extract_entries_and_round(html: str) -> tuple[tuple[PlayerEntry, ...], int]:
    import json

    flight_text = extract_flight_text(html)
    decoder = json.JSONDecoder()
    marker = '"rows":'
    candidates: list[tuple[list[dict[str, object]], int, int]] = []
    offset = 0
    while True:
        marker_index = flight_text.find(marker, offset)
        if marker_index < 0:
            break
        value_start = marker_index + len(marker)
        try:
            value, consumed = decoder.raw_decode(flight_text[value_start:])
        except json.JSONDecodeError:
            offset = value_start + 1
            continue
        value_end = value_start + consumed
        offset = value_end
        if (
            isinstance(value, list)
            and value
            and all(_looks_like_player_row(row) for row in value)
        ):
            round_match = re.search(
                r'[,}]"round":(-?\d+)', flight_text[value_end : value_end + 1000]
            )
            if round_match:
                candidates.append((value, int(round_match.group(1)), marker_index))
    if not candidates:
        raise PayloadError(
            "Kunne ikke finde en udfyldt liste med spillerrækker og en tilhørende runde"
        )
    rows, round_number, _ = max(candidates, key=lambda item: len(item[0]))
    if round_number < 0:
        raise PayloadError(f"Ugyldigt aktuelt rundenummer: {round_number}")
    return (
        tuple(_row_to_entry(row, index) for index, row in enumerate(rows)),
        round_number,
    )


def scrape_game(
    game: GameUrl,
    *,
    fetcher: Callable[[str], str] = fetch_html,
    round_number: int | None = None,
    policy: GamePolicy | None = None,
) -> ScrapedGame:
    if policy is None and fetcher is fetch_html:
        # A public network scrape must resolve both the Nexus route and the
        # ruleset policy. Cycling route names alone cannot distinguish
        # Tourspillet (money) from Tour Manager (points).
        from .teams import TeamDataService

        policy = TeamDataService(text_fetcher=fetcher).context(game).policy
    variant = (
        policy.route_variant
        if policy is not None
        else discover_variant(fetcher(game.nexus_root_url))
    )
    selected_policy = policy or legacy_policy(variant)
    entries, actual_round = extract_entries_and_round(
        fetcher(game.statistics_url(variant, round_number))
    )
    return ScrapedGame(
        game,
        variant,
        actual_round,
        entries,
        selected_policy.format,
        selected_policy.unit,
    )


def sort_entries(    entries: Iterable[PlayerEntry], field: str, order: str
) -> list[PlayerEntry]:
    if field not in SORT_FIELDS:
        raise ValueError(f"Ikke-understøttet sorteringsfelt: {field}")
    if order not in SORT_ORDERS:
        raise ValueError(f"Ikke-understøttet sorteringsrækkefølge: {order}")
    values = list(entries)
    reverse = order == "desc"
    if field == "source":
        return sorted(values, key=lambda entry: entry.source_index, reverse=reverse)
    if field == "name":
        return sorted(values, key=lambda entry: entry.name.casefold(), reverse=reverse)
    values.sort(key=lambda entry: entry.name.casefold())
    keys: dict[str, Callable[[PlayerEntry], object]] = {
        "value": lambda entry: entry.value,
        "team": lambda entry: entry.team.casefold(),
        "position": lambda entry: entry.position.casefold(),
    }
    values.sort(key=keys[field], reverse=reverse)
    return values


def format_integer(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def _status_suffix(entry: PlayerEntry) -> str:
    markers: list[str] = []
    if not entry.is_active:
        markers.append("[inactive]")
    if entry.is_disabled:
        markers.append("[disabled]")
    if entry.is_injured:
        markers.append("[injured]")
    if entry.has_suspension:
        markers.append("[suspended]")
    return f" {' '.join(markers)}" if markers else ""


def format_entry(
    entry: PlayerEntry,
    variant: str,
    *,
    unit: str | None = None,
    game_format: str | None = None,
) -> str:
    policy = (
        legacy_policy(variant)
        if unit is None and game_format is None
        else GamePolicy(variant, game_format or "", unit or "")
    )
    if policy.format == "cycling" and policy.unit == "money":
        line = f"{entry.name} ({entry.team}): {format_integer(entry.value)}"
    elif policy.format == "cycling" and policy.unit == "points":
        line = (
            f"{entry.name} ({entry.team}, {entry.position}): "
            f"{format_integer(entry.value)} p."
        )
    elif policy.format in {"soccer", "formula1"}:
        line = (
            f"{entry.name} ({entry.team}, {entry.position}): "
            f"{format_integer(entry.value)}"
            + (" p." if policy.unit == "points" else "")
        )
    elif policy.format == "golf":
        line = (
            f"{entry.name} ({entry.team}, {entry.position}): "
            f"{format_integer(entry.value)} p."
        )
    else:
        raise UnsupportedGameError(f"Kan ikke formatere en ikke-understøttet variant: {variant}")
    return line + _status_suffix(entry)
