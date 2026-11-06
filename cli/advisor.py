"""JSON commands for the source-based Holdet advisory workflow."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from dataclasses import asdict
from datetime import date, datetime
import json
import os
from pathlib import Path
import sys
import time

from holdet_lib.advisory import AdvisoryData
from holdet_lib.errors import PayloadError
from holdet_lib.game_metadata import GameMetadataStore
from holdet_lib.groups import GroupStore
from holdet_lib.hub_settings import HubSettingsStore
from holdet_lib.paths import APP_NAME, PathOverrides, resolve_paths
from holdet_lib.persistence import replace_text_atomically
from holdet_lib.refresh import RefreshMode, build_refresh_plan
from holdet_lib.refresh_runs import exclusive_data_operation
from holdet_lib.rule_pages import fetch_rule_page, public_capture, reviewed_rule_profile, validate_capture
from holdet_lib.rules import game_rule_to_dict
from holdet_lib.storage import ManifestStore, PlayerStatisticsStore
from holdet_lib.strategies import strategy_from_dict, strategy_to_dict


COMMANDS = {"games", "context", "rules", "strategy", "validate", "refresh"}


def _paths(parser):
    parser.add_argument("--data-dir", type=Path, default=argparse.SUPPRESS,
                        help="application root containing data/ and exports/")
    parser.add_argument("--config-dir", type=Path, default=argparse.SUPPRESS,
                        help="configuration directory (can be separate from --data-dir)")
    parser.add_argument("--output", type=Path, help="write JSON to this file instead of stdout")


def add_parsers(subparsers):
    games = subparsers.add_parser("games", help="list saved games, competitions, teams and strategies (cache only)")
    _paths(games)
    games.add_argument("--add", metavar="URL", help="explicitly register a manager game")
    games.add_argument("--name", default="", help="display name for --add")
    context = subparsers.add_parser("context", help="complete cached game/competition/team JSON")
    _paths(context)
    context.add_argument("game", help="saved game name, slug or full URL")
    context.add_argument("--competition", help="competition ID")
    context.add_argument("--team", action="append", default=[], help="team ID or exact cached name (repeatable)")
    context.add_argument("--history", action="store_true", help="include corrected player snapshots per round")
    context.add_argument("--analysis", action="store_true", help="include descriptive player metrics")
    rules = subparsers.add_parser("rules", help="read cached rules, fetch complete official text, or save a reviewed profile")
    _paths(rules)
    rules.add_argument("game")
    mode = rules.add_mutually_exclusive_group()
    mode.add_argument("--fetch", action="store_true", help="fetch the rules page and its official iframe document")
    mode.add_argument("--audit", type=Path, metavar="PROFILE", help="save a manually reviewed season profile JSON")
    rules.add_argument("--save", action="store_true", help="persist fetched evidence; otherwise --fetch only prints JSON")
    rules.add_argument("--raw", action="store_true", help="include original HTML in JSON")
    rules.add_argument("--capture", type=Path, help="raw evidence JSON to audit; defaults to the saved latest capture")
    rules.add_argument("--review-note", help="what was checked against the captured official text")
    strategy = subparsers.add_parser("strategy", help="read or explicitly save a team's personal strategy")
    _paths(strategy)
    strategy.add_argument("game")
    strategy.add_argument("--team", required=True, help="team ID or unambiguous cached name")
    strategy.add_argument("--set", type=Path, metavar="FILE", help="save strategy JSON; omitted means read only")
    validate = subparsers.add_parser("validate", help="validate proposed rosters/transfers against cached data and strategies")
    _paths(validate)
    validate.add_argument("game")
    validate.add_argument("--proposal", required=True, help="proposal JSON file, or - for stdin")
    refresh = subparsers.add_parser("refresh", help="preview or explicitly run the shared catch-up worker")
    _paths(refresh)
    refresh.add_argument("games", nargs="*", help="saved game names, slugs or URLs")
    refresh.add_argument("--all", action="store_true", help="all active registered games")
    refresh.add_argument("--competition", help="one competition ID (requires one game)")
    refresh.add_argument("--force", action="store_true", help="also fetch currently fresh sources")
    refresh.add_argument("--execute", action="store_true", help="perform the previewed refresh and wait for its result")
    action = refresh.add_mutually_exclusive_group()
    action.add_argument("--resume", metavar="RUN_ID", help="explicitly resume a previous run and wait")
    action.add_argument("--status", metavar="RUN_ID", help="read a run without changing it")
    action.add_argument("--cancel", metavar="RUN_ID", help="request cancellation")


def resolve_cli_paths(args, *, environ=None, native_root=None):
    """Keep explicit paths authoritative; detect Windows sandbox redirection."""
    env = os.environ if environ is None else environ
    root = getattr(args, "data_dir", None)
    config = getattr(args, "config_dir", None)
    paths = resolve_paths(overrides=PathOverrides(data_root=root, config_dir=config), environ=env)
    warnings = []
    normal = str(paths.data_dir).replace("\\", "/").casefold()
    desktop = Path(native_root) if native_root else Path.home() / "AppData" / "Local" / APP_NAME
    if (root is None and not env.get("HOLDET_DATA_DIR") and "/packages/sandbox." in normal
            and (desktop / "data" / "snapshots").is_dir()):
        paths = resolve_paths(overrides=PathOverrides(
            data_root=desktop, config_dir=paths.config_dir,
            export_dir=paths.export_dir if env.get("HOLDET_OUTPUT_DIR", "").strip() else None,
        ), environ=env)
        warnings.append(f"Using the desktop cache at {desktop}; Windows redirected the default location")
    return paths, warnings


def json_default(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value.resolve())
    raise TypeError(f"Unsupported JSON value: {type(value).__name__}")


def emit(value, output=None):
    content = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=json_default) + "\n"
    if output is None or str(output) == "-":
        sys.stdout.write(content)
    else:
        replace_text_atomically(output, content)
        print(f"Saved {output.resolve()}", file=sys.stderr)


def load_json(source):
    if str(source) == "-":
        return json.load(sys.stdin)
    return json.loads(Path(source).read_text(encoding="utf-8-sig"))


def path_report(paths, warnings=()):
    return {
        "schema_version": 1,
        "paths": {name: {"path": str(getattr(paths, name).resolve()),
                         "exists": getattr(paths, name).exists()}
                  for name in ("config_dir", "data_dir", "snapshot_dir", "game_metadata_dir",
                               "manifest_dir", "export_dir", "accounts_file", "groups_file")},
        "strategy_file": str(paths.strategy_file.resolve()),
        "rules_dir": str(paths.rules_dir.resolve()),
        "warnings": list(warnings),
    }


def _refresh(args, data, *, paths=None, coordinator_factory=None):
    # This worker module has no Streamlit imports and does not start a server.
    if coordinator_factory is None:
        from website.refresh_service import create_refresh_coordinator
        coordinator_factory = create_refresh_coordinator
    coordinator = coordinator_factory(data.paths if data is not None else paths)
    management = args.resume or args.status or args.cancel
    if management:
        if args.games or args.all or args.competition or args.force or args.execute:
            raise ValueError("Run management cannot be combined with game refresh options")
        if args.status:
            return coordinator.snapshot(args.status)
        if args.cancel:
            return coordinator.cancel(args.cancel)
        run = coordinator.resume(args.resume)
    else:
        if bool(args.games) == bool(args.all):
            raise ValueError("Select game(s), or --all")
        managers = ([manager for manager in data.configuration.games if not manager.is_archived]
                    if args.all else [data.manager(selector) for selector in args.games])
        if not managers:
            raise ValueError("No active games selected")
        if len({manager.identity for manager in managers}) != len(managers):
            raise ValueError("The same game was selected more than once")
        if args.competition and len(managers) != 1:
            raise ValueError("--competition requires exactly one game")
        if any(manager not in data.configuration.games for manager in managers):
            raise ValueError("Refresh requires a saved game; register it with games --add URL")
        if any(manager.is_archived for manager in managers):
            raise ValueError("Archived games are read only")
        tasks, plans = [], []
        settings = HubSettingsStore(data.paths.hub_settings_file).load()
        for manager in managers:
            groups = data.configuration.groups
            warnings = []
            try:
                metadata = GameMetadataStore(data.paths.game_metadata_dir).load(manager.game)
            except (OSError, ValueError, PayloadError):
                metadata = None
                warnings.append("metadata_unreadable")
                print(f"warning: {manager.game.slug}: metadata_unreadable; metadata refresh included in plan", file=sys.stderr)
            previous = ManifestStore(data.paths.manifest_dir).load_latest(
                manager.game.slug, game_locale=manager.game.locale,
                scope="group" if args.competition else "game", group_id=args.competition,
            )
            plan = build_refresh_plan(
                manager, groups, data.teams, PlayerStatisticsStore(data.paths.snapshot_dir).scan(manager.game),
                metadata=metadata, previous_manifest=previous, settings=settings, catch_up=True,
                mode=RefreshMode.ALL if args.force else RefreshMode.STALE_ONLY,
                competition_id=args.competition, include_postprocess=True,
            )
            plans.append({
                "game": manager.game.slug, "locale": manager.game.locale,
                "target_round": plan.target_round,
                "steps": [asdict(step) for step in plan.steps],
                "warnings": warnings,
            })
            tasks.append({
                "key": f"{manager.game.locale}:{manager.game.slug}:{args.competition or 'game'}",
                "locale": manager.game.locale, "game": manager.game.slug,
                "name": manager.name, "competition_id": args.competition, "force": args.force,
            })
        if not args.execute:
            return {"schema_version": 1, "preview": True, "games": plans}
        run = coordinator.start(tasks)
    previous_progress = None
    try:
        while run["status"] in {"running", "unstarted"}:
            progress = [(entry["game"]["key"], entry.get("progress"), entry["status"]) for entry in run["games"]]
            if progress != previous_progress:
                print(json.dumps({"run_id": run["run_id"], "progress": progress},
                                 ensure_ascii=False, default=json_default), file=sys.stderr)
                previous_progress = progress
            time.sleep(0.1)
            run = coordinator.snapshot(run["run_id"])
    except KeyboardInterrupt:
        coordinator.cancel(run["run_id"])
        raise
    return run


def run(args, paths):
    journal_only = args.command == "refresh" and (args.resume or args.status or args.cancel)
    data = None if journal_only else AdvisoryData(paths)
    status = 0
    if args.command == "games":
        if args.add:
            GroupStore(paths.groups_file, paths.group_revision_dir).create_manager_game(args.add, args.name)
            data = AdvisoryData(paths)
        value = data.discover()
    elif args.command == "refresh":
        value = _refresh(args, data, paths=paths)
        if "status" in value and value["status"] not in {"success", "running", "unstarted"}:
            status = 1
    else:
        manager = data.manager(args.game)
        if args.command == "context":
            value = data.context(manager, competition=args.competition, selectors=args.team,
                                 history=args.history, analysis=args.analysis)
        elif args.command == "strategy":
            team_id, = data.team_ids(manager, [args.team])
            if args.set:
                data.strategies.set(manager.game, team_id, strategy_from_dict(load_json(args.set)))
            strategy = data.strategies.get(manager.game, team_id)
            value = {"schema_version": 1, "game": {"locale": manager.game.locale, "slug": manager.game.slug},
                     "team_id": team_id, "strategy": strategy_to_dict(strategy) if strategy else None}
        elif args.command == "rules":
            if args.save and not args.fetch:
                raise ValueError("--save requires --fetch")
            if args.capture and not args.audit or args.review_note and not args.audit:
                raise ValueError("--capture and --review-note require --audit")
            supplied_capture = validate_capture(load_json(args.capture), manager.game) if args.capture else None
            raw = load_json(args.audit) if args.audit else None
            if args.audit:
                if not isinstance(raw, dict):
                    raise ValueError("Rule profile must be an object")
                preflight = supplied_capture or data.rule_pages.latest(manager.game)
                if preflight is not None:
                    reviewed_rule_profile(manager.game, preflight, raw, args.review_note)
            with exclusive_data_operation(paths.data_dir) if args.save or args.audit else nullcontext():
                capture = fetch_rule_page(manager.game) if args.fetch else data.rule_pages.latest(manager.game)
                if args.audit:
                    review_capture = supplied_capture or capture
                    if review_capture is None:
                        raise ValueError("Fetch and save rules evidence before auditing a profile")
                    reviewed_rule_profile(manager.game, review_capture, raw, args.review_note)
                if args.fetch and args.save:
                    data.rule_pages.save(manager.game, capture)
                if args.audit:
                    data.rule_pages.audit(manager.game, review_capture, raw, args.review_note)
                    capture = review_capture
            warnings = []
            try:
                metadata = GameMetadataStore(paths.game_metadata_dir).load(manager.game)
            except (OSError, ValueError, PayloadError):
                metadata = None
                warnings.append("metadata_unreadable")
            profile = data.rules(manager, metadata, warnings=warnings)
            # A fresh unsaved capture can invalidate a cached audit too.
            if capture and profile.source_sha256 != capture["source_sha256"]:
                from dataclasses import replace
                profile = replace(profile, verified=False)
            value = {"schema_version": 1, "profile": game_rule_to_dict(profile),
                     "capture": (capture if args.raw else public_capture(capture)) if capture else None,
                     "warnings": warnings}
            for warning in warnings:
                print(f"warning: {manager.game.slug}: {warning}", file=sys.stderr)
        else:
            value = data.validate(manager, load_json(args.proposal))
            status = 0 if value["status"] == "valid" else 1
    emit(value, args.output)
    return status
