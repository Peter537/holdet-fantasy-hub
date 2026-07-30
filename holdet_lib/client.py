"""High-level, side-effect-free Holdet.dk client."""

from __future__ import annotations

from collections.abc import Iterable

from .http import HttpClient
from .models import AccountConfig, GameUrl, ScrapedGame, ScrapedTeam, TeamReference
from .players import normalize_game_url, scrape_game
from .teams import GameContext, TeamDataService, discover_profile_teams, parse_direct_team_url


class HoldetClient:
    """Fetch Holdet data as domain models without writing to the filesystem."""

    def __init__(
        self,
        http_client: HttpClient | None = None,
        *,
        text_fetcher=None,
        json_fetcher=None,
    ) -> None:
        client = http_client or HttpClient()
        self._fetch_text = text_fetcher or client.fetch_text
        self._fetch_json = json_fetcher or client.fetch_json
        self._teams = TeamDataService(
            text_fetcher=self._fetch_text,
            json_fetcher=self._fetch_json,
        )

    def fetch_players(
        self, source: str | GameUrl, *, round_number: int | None = None
    ) -> ScrapedGame:
        """Fetch all player/entity rows for one game or one historical round."""

        game = source if isinstance(source, GameUrl) else normalize_game_url(source)
        context = self._teams.context(game)
        return scrape_game(
            game,
            fetcher=self._fetch_text,
            round_number=round_number,
            policy=context.policy,
        )

    def discover_account_teams(
        self,
        account: AccountConfig,
        *,
        game: str | GameUrl | None = None,
    ) -> tuple[TeamReference, ...]:
        """Discover public fantasy teams on one configured account profile."""

        normalized = normalize_game_url(game) if isinstance(game, str) else game
        return discover_profile_teams(
            self._fetch_text(account.profile_url),
            account,
            game=normalized,
        )

    def discover_teams(
        self,
        accounts: Iterable[AccountConfig],
        *,
        game: str | GameUrl | None = None,
    ) -> tuple[TeamReference, ...]:
        """Discover and deduplicate teams across configured accounts."""

        found: dict[tuple[str, str, int], TeamReference] = {}
        for account in accounts:
            for reference in self.discover_account_teams(account, game=game):
                found.setdefault((reference.game.locale.casefold(), reference.game.slug, reference.team_id), reference)
        return tuple(found.values())

    def fetch_team(self, source: str | TeamReference) -> ScrapedTeam:
        """Fetch current roster, overview and all public round summaries."""

        if isinstance(source, TeamReference):
            reference = source
        else:
            reference = parse_direct_team_url(source)
            if reference is None:
                raise ValueError(
                    "team source must be a Holdet.dk fantasyteams/<id> URL"
                )
        return self._teams.scrape(reference)

    def fetch_game_info(self, source: str | GameUrl) -> GameContext:
        """Fetch a game's public cartridge and authoritative schedule metadata."""

        game = source if isinstance(source, GameUrl) else normalize_game_url(source)
        return self._teams.game_info(game)

