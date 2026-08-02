"""Fantasy-team discovery and current-team/history scraping."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from typing import Callable, Iterable
from urllib.parse import unquote, urlsplit

from .errors import PayloadError, UrlValidationError
from .flight import rendered_scalars, walk_flight
from .http import HttpClient
from .models import (
    NEXUS_HOST,
    AccountConfig,
    GameUrl,
    RosterEntry,
    RoundStatus,
    RoundSummary,
    ScheduleRound,
    ScrapedTeam,
    TeamOverview,
    TeamReference,
)
from .players import SUPPORTED_HOSTS, discover_variant, normalize_game_url
from .policies import GamePolicy, policy_from_ruleset


@dataclass(frozen=True, slots=True)
class GameContext:
    game: GameUrl
    variant: str
    format: str
    game_id: int
    schedule_id: int | None
    default_league_id: int | None
    salary_cap: int
    final_round: int | None = None
    display_name: str | None = None
    rounds: tuple[ScheduleRound, ...] = ()

    @property
    def unit(self) -> str:
        return "money" if self.salary_cap > 0 else "points"

    @property
    def policy(self) -> GamePolicy:
        return GamePolicy(self.variant, self.format, self.unit)


class _GameTitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_h1 = False
        self._parts: list[str] = []
        self.title: str | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if self.title is None and not self._in_h1 and tag.casefold() == "h1":
            self._in_h1 = True
            self._parts.clear()

    def handle_endtag(self, tag: str) -> None:
        if self._in_h1 and tag.casefold() == "h1":
            value = " ".join(" ".join(self._parts).split())
            self.title = value or None
            self._in_h1 = False

    def handle_data(self, data: str) -> None:
        if self._in_h1:
            self._parts.append(data)


def parse_game_display_name(html: str) -> str | None:
    """Return the first server-rendered H1 without requiring a browser."""

    parser = _GameTitleParser()
    parser.feed(html)
    parser.close()
    return parser.title


@dataclass(frozen=True, slots=True)
class TeamPageData:
    team_name: str | None
    owner_name: str | None
    owner_user_id: int | None
    current_round: int | None
    substitutions_remaining: int | None
    substitutions_limit: int | None
    top_percent: int | None
    roster: tuple[RosterEntry, ...]


def _require_int(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise PayloadError(f"{label} must be an integer")
    return value


def _optional_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_account_profile_user_id(profile_url: str) -> int:
    try:
        parsed = urlsplit(profile_url.strip())
    except ValueError as exc:
        raise UrlValidationError(f"invalid account profile URL: {exc}") from exc
    if (
        parsed.scheme.casefold() != "https"
        or parsed.hostname is None
        or parsed.hostname.casefold() not in SUPPORTED_HOSTS
    ):
        raise UrlValidationError("account profile must use https://www.holdet.dk")
    segments = [unquote(part) for part in parsed.path.split("/") if part]
    if len(segments) != 4 or segments[1] != "users" or segments[3] != "teams":
        raise UrlValidationError(
            "account profile must end with /<locale>/users/<id>/teams"
        )
    try:
        return int(segments[2])
    except ValueError as exc:
        raise UrlValidationError("account profile contains an invalid user ID") from exc


def load_accounts(path: Path) -> tuple[AccountConfig, ...]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise PayloadError(f"could not read accounts file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise PayloadError(f"invalid JSON in accounts file {path}") from exc
    items = raw.get("accounts") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise PayloadError("accounts file must contain an 'accounts' list")
    accounts: list[AccountConfig] = []
    seen_keys: set[str] = set()
    seen_ids: set[int] = set()
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise PayloadError(f"account {index} must be an object")
        key = str(item.get("key", "")).strip()
        label = str(item.get("label", "")).strip()
        profile_url = str(item.get("profile_url", "")).strip()
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", key):
            raise PayloadError(f"account {index} has an invalid key")
        if not label:
            raise PayloadError(f"account {index} has an empty label")
        user_id = parse_account_profile_user_id(profile_url)
        if key in seen_keys or user_id in seen_ids:
            raise PayloadError(f"duplicate account key or user ID: {key}")
        seen_keys.add(key)
        seen_ids.add(user_id)
        accounts.append(AccountConfig(key, label, profile_url, user_id))
    return tuple(accounts)


def select_accounts(
    accounts: Iterable[AccountConfig], selectors: Iterable[str]
) -> tuple[AccountConfig, ...]:
    values = tuple(accounts)
    requested = [selector.strip().casefold() for selector in selectors if selector.strip()]
    if not requested:
        return values
    selected: list[AccountConfig] = []
    unmatched = set(requested)
    for account in values:
        aliases = {account.key.casefold(), account.label.casefold(), str(account.user_id)}
        if aliases & unmatched:
            selected.append(account)
            unmatched -= aliases
    if unmatched:
        raise PayloadError("unknown account selector(s): " + ", ".join(sorted(unmatched)))
    return tuple(selected)


def _parse_team_href(href: str) -> tuple[GameUrl, int] | None:
    parsed = urlsplit(href)
    if parsed.scheme:
        if (
            parsed.scheme.casefold() != "https"
            or parsed.hostname is None
            or parsed.hostname.casefold() not in SUPPORTED_HOSTS
        ):
            return None
    segments = [unquote(part) for part in parsed.path.split("/") if part]
    if len(segments) < 5 or segments[1].casefold() != "fantasy":
        return None
    try:
        marker = segments.index("fantasyteams", 3)
    except ValueError:
        return None
    if marker + 1 >= len(segments) or not segments[marker + 1].isdecimal():
        return None
    game = normalize_game_url(
        f"https://www.holdet.dk/{segments[0]}/fantasy/{segments[2]}"
    )
    return game, int(segments[marker + 1])


def parse_direct_team_url(raw_url: str) -> TeamReference | None:
    parsed = _parse_team_href(raw_url.strip())
    if parsed is None:
        return None
    game, team_id = parsed
    return TeamReference(
        game=game,
        team_id=team_id,
        team_name=f"team-{team_id}",
        source_url=raw_url.strip(),
    )


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.href: str | None = None
        self.parts: list[str] = []
        self.links: list[tuple[str, str]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.casefold() == "a":
            self.href = dict(attrs).get("href")
            self.parts = []

    def handle_data(self, data: str) -> None:
        if self.href is not None:
            self.parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self.href is not None:
            self.links.append((self.href, " ".join(self.parts).strip()))
            self.href = None
            self.parts = []


def _team_name_from_scalars(values: list[str | int], team_id: int) -> str:
    for value in values:
        if not isinstance(value, str):
            continue
        candidate = value.strip()
        if (
            candidate
            and not candidate.startswith("/")
            and not re.fullmatch(r"[#\d.]+", candidate)
        ):
            return candidate
    return f"team-{team_id}"


def discover_profile_teams(
    html: str,
    account: AccountConfig,
    *,
    game: GameUrl | None = None,
) -> tuple[TeamReference, ...]:
    found: dict[tuple[str, str, int], TeamReference] = {}

    def add(href: str, name: str) -> None:
        parsed = _parse_team_href(href)
        if parsed is None:
            return
        link_game, team_id = parsed
        if game is not None and (
            link_game.locale.casefold() != game.locale.casefold()
            or link_game.slug != game.slug
        ):
            return
        canonical = (
            f"https://www.holdet.dk/{link_game.locale}/fantasy/{link_game.slug}"
            f"/fantasyteams/{team_id}"
        )
        found[(link_game.locale.casefold(), link_game.slug, team_id)] = TeamReference(
            game=link_game,
            team_id=team_id,
            team_name=name or f"team-{team_id}",
            source_url=canonical,
            account_key=account.key,
            account_label=account.label,
            account_user_id=account.user_id,
            profile_url=account.profile_url,
        )

    try:
        for node in walk_flight(html):
            if not isinstance(node, dict) or not isinstance(node.get("href"), str):
                continue
            parsed = _parse_team_href(node["href"])
            if parsed is None:
                continue
            _, team_id = parsed
            add(node["href"], _team_name_from_scalars(rendered_scalars(node), team_id))
    except PayloadError:
        pass

    parser = _AnchorParser()
    parser.feed(html)
    for href, text in parser.links:
        parsed = _parse_team_href(href)
        if parsed is not None:
            _, team_id = parsed
            add(href, text.strip() or f"team-{team_id}")
    return tuple(found.values())


def filter_team_references(
    references: Iterable[TeamReference], selectors: Iterable[str]
) -> tuple[TeamReference, ...]:
    requested = {value.strip().casefold() for value in selectors if value.strip()}
    values = tuple(references)
    if not requested:
        return values
    return tuple(
        reference
        for reference in values
        if reference.team_name.casefold() in requested
        or str(reference.team_id) in requested
    )


def _person_name(person: dict[str, object]) -> str:
    full_name = person.get("fullName")
    if isinstance(full_name, str) and full_name.strip():
        return full_name.strip()
    parts = [person.get("firstName"), person.get("lastName")]
    return " ".join(str(part).strip() for part in parts if isinstance(part, str)).strip()


def _looks_like_roster_player(value: object) -> bool:
    if not isinstance(value, dict) or not isinstance(value.get("person"), dict):
        return False
    person = value["person"]
    return bool(_person_name(person)) and isinstance(value.get("id"), int)


def _extract_roster(html: str, *, salary_cap: bool) -> tuple[RosterEntry, ...]:
    candidates: list[tuple[int, list[dict[str, object]]]] = []
    for node in walk_flight(html):
        if not isinstance(node, dict) or not isinstance(node.get("players"), list):
            continue
        players = node["players"]
        if not players or not all(_looks_like_roster_player(item) for item in players):
            continue
        score = len(players) * 100
        score += sum("sinceRound" in item for item in players) * 5
        score += sum("isCaptain" in item for item in players) * 3
        candidates.append((score, players))
    if not candidates:
        return ()
    players = max(candidates, key=lambda item: item[0])[1]
    result: list[RosterEntry] = []
    for index, raw in enumerate(players):
        person = raw["person"]
        assert isinstance(person, dict)
        team = raw.get("team")
        position = raw.get("position")
        team_name = str(team.get("name", "-")).strip() if isinstance(team, dict) else "-"
        position_name = "-"
        if isinstance(position, dict):
            position_name = str(position.get("title") or position.get("name") or "-").strip()
        value_key = "price" if salary_cap else "points"
        value = _optional_int(raw.get(value_key))
        if value is None:
            value = _optional_int(raw.get("price")) or 0
        is_captain = raw.get("isCaptain") is True or raw.get("captain") is True
        role = str(raw.get("role", "captain" if is_captain else "none"))
        if is_captain:
            role = "captain"
        result.append(
            RosterEntry(
                source_index=index,
                player_id=_require_int(raw.get("id"), f"roster player {index} ID"),
                name=_person_name(person),
                team=team_name,
                position=position_name,
                value=value,
                round_change=_optional_int(raw.get("growth")) or 0,
                since_purchase_change=_optional_int(raw.get("earnings")) or 0,
                purchase_round=_optional_int(raw.get("sinceRound")),
                role=role,
                is_active=raw.get("isActive", True) is not False,
                is_disabled=(raw.get("isDisabled") is True or raw.get("disabled") is True),
                is_injured=(raw.get("hasInjury") is True or bool(raw.get("injuries"))),
                has_suspension=(
                    raw.get("hasSuspension") is True or bool(raw.get("suspensions"))
                ),
            )
        )
    return tuple(result)


def _metric_numbers(html: str, label: str) -> tuple[int, ...]:
    best: list[str | int] | None = None
    for node in walk_flight(html):
        if not isinstance(node, dict) or "children" not in node:
            continue
        values = rendered_scalars(node.get("children"))
        if (
            len(values) < 2
            or str(values[0]).strip() != label
            or len(values) > 8
        ):
            continue
        if best is None or len(values) < len(best):
            best = values
    if best is None:
        return ()
    numbers: list[int] = []
    for value in best[1:]:
        if isinstance(value, int):
            numbers.append(value)
        elif isinstance(value, str):
            match = re.search(r"-?\d+", value.replace(".", ""))
            if match:
                numbers.append(int(match.group()))
    return tuple(numbers)


def extract_team_page(html: str, *, salary_cap: bool) -> TeamPageData:
    team_name: str | None = None
    owner_name: str | None = None
    owner_user_id: int | None = None
    current_round: int | None = None
    for node in walk_flight(html):
        if not isinstance(node, dict):
            continue
        if owner_name is None and isinstance(node.get("displayName"), str):
            owner_name = node["displayName"].strip()
            raw_owner_id = node.get("coreUserId")
            if isinstance(raw_owner_id, str) and raw_owner_id.isdecimal():
                owner_user_id = int(raw_owner_id)
            elif isinstance(raw_owner_id, int):
                owner_user_id = raw_owner_id
        class_name = node.get("className")
        if (
            team_name is None
            and isinstance(class_name, str)
            and "md:text-lg" in class_name
            and "font-bold" in class_name
        ):
            values = rendered_scalars(node.get("children"))
            if values and isinstance(values[0], str):
                team_name = values[0].strip()
        round_heading = node.get("roundHeading")
        if current_round is None and isinstance(round_heading, str):
            match = re.search(r"\d+", round_heading)
            if match:
                current_round = int(match.group())
    substitutions = _metric_numbers(html, "Udskiftninger")
    top = _metric_numbers(html, "Top")
    return TeamPageData(
        team_name=team_name,
        owner_name=owner_name,
        owner_user_id=owner_user_id,
        current_round=current_round,
        substitutions_remaining=substitutions[0] if substitutions else None,
        substitutions_limit=substitutions[1] if len(substitutions) > 1 else None,
        top_percent=top[0] if top else None,
        roster=_extract_roster(html, salary_cap=salary_cap),
    )


def _entity_by_id(collection: object, entity_id: int) -> dict[str, object] | None:
    if isinstance(collection, dict):
        direct = collection.get(str(entity_id)) or collection.get(entity_id)
        if isinstance(direct, dict):
            return direct
        values = collection.values()
    elif isinstance(collection, list):
        values = collection
    else:
        return None
    for value in values:
        if isinstance(value, dict) and value.get("id") == entity_id:
            return value
    return None


def parse_game_context(game: GameUrl, variant: str, payload: object) -> GameContext:
    if not isinstance(payload, dict):
        raise PayloadError("cartridge payload must be an object")
    game_id = _require_int(payload.get("gameId"), "cartridge gameId")
    league_id = _optional_int(payload.get("defaultFantasyLeagueId"))
    embedded = payload.get("_embedded")
    if not isinstance(embedded, dict):
        raise PayloadError("cartridge payload lacks embedded metadata")
    game_data = _entity_by_id(embedded.get("games"), game_id)
    if game_data is None:
        raise PayloadError(f"cartridge payload lacks game {game_id}")
    ruleset_id = _require_int(game_data.get("rulesetId"), "game rulesetId")
    schedule_id = _optional_int(game_data.get("scheduleId"))
    ruleset = _entity_by_id(embedded.get("rulesets"), ruleset_id)
    if ruleset is None:
        raise PayloadError(f"cartridge payload lacks ruleset {ruleset_id}")
    properties = ruleset.get("properties")
    ruleset_format = properties.get("Format") if isinstance(properties, dict) else None
    policy = policy_from_ruleset(
        variant,
        ruleset_format=ruleset_format,
        salary_cap=ruleset.get("salaryCap"),
    )
    salary_cap = _require_int(ruleset.get("salaryCap"), "game ruleset salaryCap")
    return GameContext(
        game,
        variant,
        policy.format,
        game_id,
        schedule_id,
        league_id,
        salary_cap,
    )


def parse_schedule_rounds(payload: object) -> tuple[ScheduleRound, ...]:
    """Return validated rounds from a public schedule payload."""

    if not isinstance(payload, dict):
        raise PayloadError("schedule payload must be an object")
    rounds = payload.get("rounds")
    if not isinstance(rounds, list) or not rounds:
        raise PayloadError("schedule payload lacks a non-empty rounds list")
    if any(not isinstance(item, dict) for item in rounds):
        raise PayloadError("schedule payload contains an invalid round")
    result: list[ScheduleRound] = []
    for index, item in enumerate(rounds, 1):
        assert isinstance(item, dict)
        round_number = item.get("round", index)
        if not isinstance(round_number, int) or isinstance(round_number, bool):
            raise PayloadError(f"schedule round {index} has an invalid number")
        timestamps: dict[str, datetime] = {}
        for field in ("start", "close", "end"):
            value = item.get(field)
            if not isinstance(value, str) or not value.strip():
                raise PayloadError(f"schedule round {round_number} lacks {field}")
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise PayloadError(
                    f"schedule round {round_number} has an invalid {field}"
                ) from exc
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            timestamps[field] = parsed
        if not (timestamps["start"] <= timestamps["close"] <= timestamps["end"]):
            raise PayloadError(f"schedule round {round_number} has invalid boundaries")
        result.append(
            ScheduleRound(
                round_number=round_number,
                start=timestamps["start"],
                close=timestamps["close"],
                end=timestamps["end"],
            )
        )
    return tuple(sorted(result, key=lambda value: value.round_number))


def parse_schedule_final_round(payload: object) -> int:
    """Return the authoritative final round from a public schedule payload."""

    return max(item.round_number for item in parse_schedule_rounds(payload))


def round_status_for(
    rounds: Iterable[ScheduleRound],
    round_number: int,
    *,
    at: datetime | None = None,
) -> tuple[RoundStatus, datetime | None]:
    """Freeze a round's completion state at the supplied observation time."""

    observed_at = at or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    for item in rounds:
        if item.round_number == round_number:
            return (
                "complete" if observed_at >= item.end else "in_progress",
                item.end,
            )
    return "unknown", None


def _rank_map(payload: object) -> dict[int, int]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise PayloadError("leaderboard payload lacks an items list")
    result: dict[int, int] = {}
    for item in payload["items"]:
        if not isinstance(item, dict):
            continue
        round_number = _optional_int(item.get("round"))
        rank = _optional_int(item.get("rank"))
        if round_number is not None and rank is not None:
            result[round_number] = rank
    return result


def parse_history(
    payload: object,
    *,
    salary_cap: bool,
    round_ranks: dict[int, int] | None = None,
    overall_ranks: dict[int, int] | None = None,
) -> tuple[RoundSummary, ...]:
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise PayloadError("team history payload lacks an items list")
    round_ranks = round_ranks or {}
    overall_ranks = overall_ranks or {}
    result: list[RoundSummary] = []
    for index, item in enumerate(payload["items"]):
        if not isinstance(item, dict):
            raise PayloadError(f"history item {index} must be an object")
        round_number = _require_int(item.get("round"), f"history item {index} round")
        bucket_name = "assets" if salary_cap else "points"
        bucket = item.get(bucket_name)
        if not isinstance(bucket, dict):
            raise PayloadError(f"history round {round_number} lacks {bucket_name}")
        total = _require_int(bucket.get("value"), f"history round {round_number} value")
        bank = _optional_int(bucket.get("balance")) if salary_cap else None
        player_value = total - bank if bank is not None else None
        rr = round_ranks.get(round_number)
        previous_rr = round_ranks.get(round_number - 1)
        overall = overall_ranks.get(round_number)
        previous_overall = overall_ranks.get(round_number - 1)
        result.append(
            RoundSummary(
                round_number=round_number,
                total=total,
                change=_optional_int(bucket.get("change")) or 0,
                bank=bank,
                player_value=player_value,
                bank_change=(
                    _optional_int(bucket.get("balanceChange")) if salary_cap else None
                ),
                interest=_optional_int(bucket.get("interest")) if salary_cap else None,
                player_change=_optional_int(bucket.get("playerChange")) or 0,
                transfer=_optional_int(bucket.get("transfer")) if salary_cap else None,
                captain_bonus=_optional_int(bucket.get("captainBonus")) or 0,
                special_bonus=_optional_int(bucket.get("specialBonus")) or 0,
                substitutions_used=_optional_int(item.get("totalSubstitutionsUsed")),
                round_rank=rr,
                overall_rank=overall,
                round_rank_change=(
                    previous_rr - rr
                    if rr is not None and previous_rr is not None
                    else None
                ),
                overall_rank_change=(
                    previous_overall - overall
                    if overall is not None and previous_overall is not None
                    else None
                ),
            )
        )
    return tuple(sorted(result, key=lambda value: value.round_number, reverse=True))


class TeamDataService:
    """Orchestrate public team data requests behind an injectable transport."""

    def __init__(
        self,
        *,
        text_fetcher: Callable[[str], str] | None = None,
        json_fetcher: Callable[[str], object] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        client = HttpClient()
        self.fetch_text = text_fetcher or client.fetch_text
        self.fetch_json = json_fetcher or client.fetch_json
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._contexts: dict[tuple[str, str], GameContext] = {}
        self._schedule_unavailable: set[tuple[str, str]] = set()

    def context(self, game: GameUrl) -> GameContext:
        key = (game.locale.casefold(), game.slug)
        if key not in self._contexts:
            variant = discover_variant(self.fetch_text(game.nexus_root_url))
            cartridge_url = f"https://{NEXUS_HOST}/api/cartridges/{game.slug}"
            self._contexts[key] = parse_game_context(
                game, variant, self.fetch_json(cartridge_url)
            )
        return self._contexts[key]

    def context_with_schedule(
        self, game: GameUrl, *, strict: bool = False
    ) -> GameContext:
        """Return context enriched with schedule data when it is available."""

        context = self.context(game)
        key = (game.locale.casefold(), game.slug)
        if context.rounds:
            return context
        if key in self._schedule_unavailable and not strict:
            return context
        if context.schedule_id is None:
            if strict:
                raise PayloadError("game metadata lacks scheduleId")
            self._schedule_unavailable.add(key)
            return context
        schedule_url = f"https://{NEXUS_HOST}/api/schedules/{context.schedule_id}"
        try:
            rounds = parse_schedule_rounds(self.fetch_json(schedule_url))
        except Exception:
            if strict:
                raise
            self._schedule_unavailable.add(key)
            return context
        context = replace(
            context,
            rounds=rounds,
            final_round=max(item.round_number for item in rounds),
        )
        self._contexts[key] = context
        return context

    def status_for(
        self, context: GameContext, round_number: int
    ) -> tuple[RoundStatus, datetime | None]:
        return round_status_for(context.rounds, round_number, at=self._clock())

    def game_info(self, game: GameUrl) -> GameContext:
        """Fetch and cache public schedule metadata and the official display name."""

        context = self.context_with_schedule(game, strict=True)
        updates: dict[str, object] = {}
        if context.display_name is None:
            display_name = parse_game_display_name(self.fetch_text(game.original))
            if display_name is not None:
                updates["display_name"] = display_name
        if updates:
            context = replace(context, **updates)
            self._contexts[(game.locale.casefold(), game.slug)] = context
        return context

    def _rank_payloads(
        self, context: GameContext, team_id: int
    ) -> tuple[dict[int, int], dict[int, int]]:
        if context.default_league_id is None:
            return {}, {}
        base = (
            f"https://{NEXUS_HOST}/api/fantasyteams/{team_id}"
            f"/fantasyleagues/{context.default_league_id}/leaderboards"
        )
        overall = _rank_map(self.fetch_json(f"{base}/overall"))
        round_ranks = _rank_map(self.fetch_json(f"{base}/round"))
        return round_ranks, overall

    def scrape(self, reference: TeamReference) -> ScrapedTeam:
        context = self.context_with_schedule(reference.game)
        team_url = reference.game.team_url(context.variant, reference.team_id)
        history_url = (
            f"https://{NEXUS_HOST}/api/fantasyteams/{reference.team_id}/history"
        )
        round_ranks, overall_ranks = self._rank_payloads(context, reference.team_id)

        page: TeamPageData | None = None
        history: tuple[RoundSummary, ...] = ()
        for attempt in range(2):
            page = extract_team_page(
                self.fetch_text(team_url), salary_cap=context.salary_cap > 0
            )
            history = parse_history(
                self.fetch_json(history_url),
                salary_cap=context.salary_cap > 0,
                round_ranks=round_ranks,
                overall_ranks=overall_ranks,
            )
            if not history or context.salary_cap <= 0 or not page.roster:
                break
            expected = history[0].player_value
            actual = sum(player.value for player in page.roster)
            if expected == actual:
                break
            if attempt == 1:
                raise PayloadError(
                    f"team {reference.team_id} roster value {actual} does not match "
                    f"history player value {expected}"
                )
        assert page is not None
        observed_at = self._clock()
        annotated_history: list[RoundSummary] = []
        for summary in history:
            status, round_end_at = round_status_for(
                context.rounds, summary.round_number, at=observed_at
            )
            annotated_history.append(
                replace(
                    summary,
                    round_status=status,
                    round_end_at=round_end_at,
                )
            )
        history = tuple(annotated_history)
        latest = history[0] if history else None
        current_round = (
            latest.round_number
            if latest is not None
            else (page.current_round if page.current_round is not None else 0)
        )
        overview = TeamOverview(
            current_round=current_round,
            unit=context.unit,
            player_value=latest.player_value if latest else None,
            bank=latest.bank if latest else None,
            total=latest.total if latest else None,
            current_change=latest.change if latest else None,
            rank=latest.overall_rank if latest else None,
            rank_change=latest.overall_rank_change if latest else None,
            top_percent=page.top_percent,
            substitutions_remaining=page.substitutions_remaining,
            substitutions_limit=page.substitutions_limit,
            substitutions_used=latest.substitutions_used if latest else None,
        )
        team_name = page.team_name or reference.team_name
        return ScrapedTeam(
            reference=replace(reference, team_name=team_name),
            variant=context.variant,
            game_id=context.game_id,
            team_name=team_name,
            owner_name=page.owner_name or reference.account_label,
            owner_user_id=page.owner_user_id or reference.account_user_id,
            overview=overview,
            roster=page.roster,
            history=history,
        )

