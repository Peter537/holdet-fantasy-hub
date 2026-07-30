"""Command-line dispatch for player and fantasy-team exports."""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path
import sys
from typing import Callable, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from holdet_lib.errors import ScraperError
from holdet_lib.http import HttpClient
from holdet_lib.client import HoldetClient
from holdet_lib.models import TeamReference
from holdet_lib.player_exports import (
    MISSING_VALUE_MODES,
    PLAYER_COLUMNS,
    PLAYER_EXPORT_FORMATS,
    PLAYER_SORT_FIELDS,
    PLAYER_STATUSES,
    STATUS_RULES,
    PlayerStatisticsQuery,
    PlayerExportStore,
    build_player_export,
)
from holdet_lib.team_exports import (
    TEAM_EXPORT_FORMATS,
    TeamExportStore,
    build_team_export,
)
from holdet_lib.paths import PathOverrides, open_in_explorer, resolve_paths
from holdet_lib.storage import PlayerStatisticsStore, SnapshotStore
from holdet_lib.players import SORT_ORDERS, normalize_game_url
from holdet_lib.teams import (
    TeamDataService,
    discover_profile_teams,
    filter_team_references,
    load_accounts,
    parse_direct_team_url,
    select_accounts,
)
from holdet_lib.version import VERSION


def _status_rule(value: str) -> tuple[str, str]:
    status, separator, rule = value.casefold().partition("=")
    if not separator or status not in PLAYER_STATUSES or rule not in STATUS_RULES:
        raise argparse.ArgumentTypeError(
            "status must be inactive|disabled|injured|suspended="
            "ignore|require|exclude"
        )
    return status, rule


def _add_player_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "urls",
        metavar="URL",
        nargs="+",
        help="full https://www.holdet.dk/<locale>/fantasy/<slug> URL",
    )
    parser.add_argument(
        "--sort",
        choices=PLAYER_SORT_FIELDS,
        default="value",
        help="sort field (default: value)",
    )
    parser.add_argument(
        "--order",
        choices=SORT_ORDERS,
        default=None,
        help="sort direction (default: desc for value, asc otherwise)",
    )
    parser.add_argument("--round", type=int, default=None, help="fetch a specific historical round")
    parser.add_argument(
        "--format", action="append", choices=PLAYER_EXPORT_FORMATS,
        dest="formats", default=None,
        help="export format; repeat for multiple formats (default: txt)",
    )
    parser.add_argument(
        "--column", action="append", choices=PLAYER_COLUMNS,
        dest="columns", default=None,
        help="column to include; repeat as needed (name is required)",
    )
    parser.add_argument("--search", default="", help="search name, team and position")
    parser.add_argument("--team", action="append", default=[], help="include team/land (repeatable)")
    parser.add_argument("--position", action="append", default=[], help="include position/category (repeatable)")
    parser.add_argument("--min-value", type=int)
    parser.add_argument("--max-value", type=int)
    parser.add_argument("--min-total-growth", type=int)
    parser.add_argument("--max-total-growth", type=int)
    parser.add_argument("--min-round-growth", type=int)
    parser.add_argument("--max-round-growth", type=int)
    parser.add_argument("--missing-total-growth", choices=MISSING_VALUE_MODES, default="include")
    parser.add_argument("--missing-round-growth", choices=MISSING_VALUE_MODES, default="include")
    parser.add_argument(
        "--status", action="append", type=_status_rule, default=[],
        help="STATUS=ignore|require|exclude (repeatable)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="player export directory (default: Windows application data)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="override the complete application-data root",
    )


def _add_team_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "sources",
        metavar="SOURCE",
        nargs="+",
        help="game URL or direct fantasyteams/<id> URL",
    )
    parser.add_argument(
        "--accounts-file",
        type=Path,
        default=None,
        help="account configuration (default: Windows Roaming AppData)",
    )
    parser.add_argument(
        "--account",
        action="append",
        default=[],
        metavar="SELECTOR",
        help="configured account key, label, or user ID (repeatable)",
    )
    parser.add_argument(
        "--team",
        action="append",
        default=[],
        metavar="SELECTOR",
        help="exact team name or team ID (repeatable)",
    )
    parser.add_argument(
        "--format",
        action="append",
        choices=TEAM_EXPORT_FORMATS,
        dest="formats",
        default=None,
        help="export format; repeat for multiple formats (default: txt and json)",
    )
    parser.add_argument(
        "--round",
        type=int,
        default=None,
        help="export one historical round instead of the complete snapshot",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="team export directory (default: Windows application data)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="override the complete application-data root",
    )


def build_argument_parser(*, prog: str | None = None) -> argparse.ArgumentParser:
    """Build the root parser with required workflow subcommands."""

    parser = argparse.ArgumentParser(
        prog=prog,
        description="Export public Holdet.dk player statistics and fantasy teams.",
    )
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)
    players = subparsers.add_parser(
        "players",
        help="export every player/entity from fantasy statistics",
        description=(
            "Export every player/entity from one or more public Holdet.dk "
            "fantasy statistics pages."
        ),
    )
    _add_player_arguments(players)
    teams = subparsers.add_parser(
        "teams",
        help="export current fantasy teams and round summaries",
        description=(
            "Export current fantasy-team rosters and complete round summaries "
            "from public Holdet.dk data."
        ),
    )
    _add_team_arguments(teams)
    data = subparsers.add_parser(
        "data",
        help="show or open Holdet Fantasy Hub data locations",
    )
    data.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="override the complete application-data root",
    )
    data_commands = data.add_subparsers(dest="data_command", required=True)
    data_commands.add_parser("paths", help="print all effective data paths")
    open_parser = data_commands.add_parser(
        "open", help="open an application-data folder in Windows Explorer"
    )
    targets = open_parser.add_mutually_exclusive_group()
    targets.add_argument("--config", action="store_const", const="config", dest="target")
    targets.add_argument(
        "--snapshots", action="store_const", const="snapshots", dest="target"
    )
    targets.add_argument("--exports", action="store_const", const="exports", dest="target")
    open_parser.set_defaults(target="data")
    return parser


def _player_query(args: argparse.Namespace) -> PlayerStatisticsQuery:
    columns = tuple(args.columns or PLAYER_COLUMNS)
    return PlayerStatisticsQuery(
        search=args.search,
        teams=tuple(args.team), positions=tuple(args.position),
        min_value=args.min_value, max_value=args.max_value,
        min_total_growth=args.min_total_growth, max_total_growth=args.max_total_growth,
        min_round_growth=args.min_round_growth, max_round_growth=args.max_round_growth,
        missing_total_growth=args.missing_total_growth,
        missing_round_growth=args.missing_round_growth,
        status_rules=tuple(args.status), columns=columns,
        sort_field=args.sort,
        sort_order=args.order or ("desc" if args.sort == "value" else "asc"),
    )


def _fetch_and_export_players(
    raw_url: str,
    *,
    output_dir: Path,
    round_number: int | None,
    query: PlayerStatisticsQuery,
    formats: tuple[str, ...],
    snapshot_dir: Path,
    **_unused: object,
) -> tuple[Path, ...]:
    """Fetch once, then explicitly persist the canonical and filtered outputs."""

    statistics = HoldetClient().fetch_players(raw_url, round_number=round_number)
    generated_at = datetime.now().astimezone()
    PlayerStatisticsStore(snapshot_dir).save(statistics, now=generated_at)
    document = build_player_export(
        statistics,
        query,
        generated_at=generated_at,
        source_generated_at=generated_at,
    )
    return tuple(
        artifact.path
        for artifact in PlayerExportStore(output_dir).save(document, formats)
    )

def _run_players(
    args: argparse.Namespace,
    *,
    player_scraper: Callable[..., Path | tuple[Path, ...]],
    snapshot_dir: Path,
) -> int:
    try:
        query = _player_query(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    failed = False
    for raw_url in args.urls:
        try:
            created = player_scraper(
                raw_url, output_dir=args.output_dir,
                sort_field=query.sort_field, sort_order=query.sort_order,
                round_number=args.round, query=query,
                formats=tuple(args.formats or ("txt",)),
                snapshot_dir=snapshot_dir,
            )
        except (ScraperError, OSError, ValueError) as exc:
            print(f"error: {raw_url}: {exc}", file=sys.stderr)
            failed = True
        else:
            paths = (created,) if isinstance(created, Path) else created
            for path in paths:
                print(path.resolve())
    return 1 if failed else 0
def _deduplicate(references: list[TeamReference]) -> tuple[TeamReference, ...]:
    result: dict[tuple[str, str, int], TeamReference] = {}
    for reference in references:
        result.setdefault((reference.game.locale.casefold(), reference.game.slug, reference.team_id), reference)
    return tuple(result.values())


def _selector_matches(reference: TeamReference, selectors: set[str]) -> bool:
    return (
        not selectors
        or reference.team_name.casefold() in selectors
        or str(reference.team_id) in selectors
    )


def _run_teams(
    args: argparse.Namespace,
    *,
    service_factory: Callable[[], TeamDataService],
    snapshot_store: SnapshotStore,
) -> int:
    failed = False
    references: list[TeamReference] = []
    game_sources: list[tuple[str, object]] = []
    for raw_source in args.sources:
        try:
            direct = parse_direct_team_url(raw_source)
            if direct is not None:
                references.append(direct)
            else:
                game_sources.append((raw_source, normalize_game_url(raw_source)))
        except ScraperError as exc:
            print(f"error: {raw_source}: {exc}", file=sys.stderr)
            failed = True

    accounts = ()
    if game_sources or args.account:
        try:
            accounts = select_accounts(
                load_accounts(args.accounts_file), args.account
            )
        except (ScraperError, OSError) as exc:
            print(f"error: {args.accounts_file}: {exc}", file=sys.stderr)
            return 1

    client = HttpClient()
    for raw_source, game_object in game_sources:
        game = game_object
        for account in accounts:
            try:
                discovered = discover_profile_teams(
                    client.fetch_text(account.profile_url), account, game=game
                )
            except (ScraperError, OSError) as exc:
                print(
                    f"error: {raw_source}: {account.label}: {exc}",
                    file=sys.stderr,
                )
                failed = True
                continue
            if not discovered:
                print(
                    f"warning: {account.label} has no team in {game.slug}",
                    file=sys.stderr,
                )
            references.extend(discovered)

    references_tuple = _deduplicate(references)
    requested = {value.strip().casefold() for value in args.team if value.strip()}
    known_filtered = filter_team_references(
        (ref for ref in references_tuple if ref.account_key != "direct"),
        args.team,
    )
    direct_refs = tuple(ref for ref in references_tuple if ref.account_key == "direct")
    candidates = _deduplicate([*known_filtered, *direct_refs])
    matched: set[str] = set()
    service = service_factory()

    for reference in candidates:
        try:
            team = service.scrape(reference)
            if requested and not _selector_matches(team.reference, requested):
                continue
            matched.update(
                selector
                for selector in requested
                if selector in {team.team_name.casefold(), str(team.reference.team_id)}
            )
            generated_at = datetime.now().astimezone()
            snapshot_path = snapshot_store.save_team_json(team, now=generated_at)
            scope = "round" if args.round is not None else "full"
            roster = (
                tuple(team.roster)
                if args.round is not None
                and team.overview.current_round == args.round
                else None
            )
            document = build_team_export(
                team,
                scope=scope,
                round_number=args.round,
                roster=roster,
                generated_at=generated_at,
                source_generated_at=generated_at,
                roster_generated_at=(generated_at if roster is not None else None),
            )
            artifacts = TeamExportStore(args.output_dir).save(
                document, tuple(args.formats or ("txt", "json"))
            )
            paths = (snapshot_path, *(artifact.path for artifact in artifacts))
        except (ScraperError, OSError, ValueError) as exc:
            print(
                f"error: {reference.source_url}: {exc}",
                file=sys.stderr,
            )
            failed = True
        else:
            for path in paths:
                print(path.resolve())

    unmatched = requested - matched
    if unmatched:
        print(
            "error: unmatched team selector(s): " + ", ".join(sorted(unmatched)),
            file=sys.stderr,
        )
        failed = True
    return 1 if failed else 0


def _run_data(args: argparse.Namespace, app_paths) -> int:
    if args.data_command == "paths":
        values = (
            ("Konfiguration", app_paths.config_dir),
            ("Konti", app_paths.accounts_file),
            ("Grupper", app_paths.groups_file),
            ("Snapshots", app_paths.snapshot_dir),
            ("Manifester", app_paths.manifest_dir),
            ("Revisioner", app_paths.group_revision_dir),
            ("Eksporter", app_paths.export_dir),
        )
        for label, path in values:
            print(f"{label}: {Path(path).resolve()}")
        return 0
    targets = {
        "config": app_paths.config_dir,
        "snapshots": app_paths.snapshot_dir,
        "exports": app_paths.export_dir,
        "data": app_paths.data_dir,
    }
    path = Path(targets[args.target]).resolve()
    if not open_in_explorer(path):
        print(f"Kunne ikke åbne Windows Stifinder. Mappe: {path}", file=sys.stderr)
    print(path)
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    player_scraper: Callable[..., Path] | None = None,
    service_factory: Callable[[], TeamDataService] | None = None,
) -> int:
    args = build_argument_parser(prog="holdet").parse_args(argv)
    app_paths = resolve_paths(
        overrides=PathOverrides(data_root=getattr(args, "data_dir", None))
    )
    if args.command == "data":
        return _run_data(args, app_paths)
    if args.command == "teams":
        args.accounts_file = args.accounts_file or app_paths.accounts_file
        args.output_dir = args.output_dir or app_paths.team_export_dir
        return _run_teams(
            args,
            service_factory=service_factory or TeamDataService,
            snapshot_store=SnapshotStore(app_paths.snapshot_dir),
        )
    args.output_dir = args.output_dir or app_paths.player_export_dir
    return _run_players(
        args,
        player_scraper=player_scraper or _fetch_and_export_players,
        snapshot_dir=app_paths.snapshot_dir,
    )


if __name__ == "__main__":
    raise SystemExit(main())
