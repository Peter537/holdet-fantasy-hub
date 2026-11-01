"""Cache-only discovery, complete review context, and proposal validation."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

from .decision_analysis import build_player_decision_analysis
from .errors import PayloadError
from .game_metadata import GameMetadataStore
from .groups import GroupStore, normalize_manager_game
from .hub_settings import HubSettingsStore, player_identity
from .models import RosterEntry, TeamReference
from .rule_pages import RulePageStore
from .rules import _basis_points, game_rule_to_dict, rule_profile_for_game
from .scope_exports import build_scope_export, prepare_scope_export
from .scopes import resolve_scope
from .storage import ManifestStore, PlayerStatisticsStore, SnapshotStore
from .strategies import StrategyStore, TeamStrategy, strategy_from_dict, strategy_to_dict
from .tournament_pairings import TournamentPairingStore
from .transfers import TransferScenario, simulate_transfers


def _positive_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _ids(value, label):
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    result = tuple(_positive_int(item, label) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{label} contains duplicates")
    return result


class AdvisoryData:
    def __init__(self, paths):
        self.paths = paths
        self.configuration, self.configuration_warnings = GroupStore(
            paths.groups_file, paths.group_revision_dir
        ).load_configuration_with_warnings()
        self.teams = SnapshotStore(paths.snapshot_dir).scan()
        self.strategies = StrategyStore(paths.strategy_file)
        self.rule_pages = RulePageStore(paths.rules_dir)

    def manager(self, selector):
        matches = [manager for manager in self.configuration.games
                   if selector.casefold() in {manager.game.slug.casefold(), manager.name.casefold(),
                                              manager.game.original.casefold()}]
        if len(matches) > 1:
            raise ValueError("Ambiguous game; use its full Holdet URL")
        if matches:
            return matches[0]
        manager = normalize_manager_game(selector)
        return next((item for item in self.configuration.games if item.identity == manager.identity), manager)

    def newest_team(self, game, team_id):
        return max(
            self.teams.for_team(game, team_id),
            key=lambda item: (item.team.overview.current_round, item.generated_at),
            default=None,
        )

    def references(self, manager):
        references = {}
        for locale, slug, team_id in self.teams.identities:
            if (locale, slug) == manager.identity:
                snapshot = self.newest_team(manager.game, team_id)
                references[team_id] = snapshot.team.reference
        for group in self.configuration.groups:
            if (group.game.locale.casefold(), group.game.slug) == manager.identity:
                for team in group.teams:
                    references.setdefault(team.team_id, team.reference(manager.game))
        settings = HubSettingsStore(self.paths.hub_settings_file).load()
        for selection in settings.own_teams:
            if (selection.game_locale.casefold(), selection.game_slug) == manager.identity:
                identifier = selection.team_id
                references.setdefault(identifier, TeamReference(
                    manager.game, identifier, f"Hold {identifier}",
                    f"{manager.game.original}/fantasyteams/{identifier}",
                ))
        return tuple(references.values())

    def team_ids(self, manager, selectors):
        references = self.references(manager)
        result = []
        for selector in selectors:
            matches = [reference.team_id for reference in references
                       if selector.casefold() in {str(reference.team_id), reference.team_name.casefold()}]
            if not matches and selector.isdecimal():
                matches = [_positive_int(int(selector), "team_id")]
            if len(matches) != 1:
                raise ValueError(f"Team selector is missing or ambiguous: {selector}; use an ID")
            if matches[0] not in result:
                result.append(matches[0])
        return tuple(result)

    def discover(self):
        strategies = self.strategies.load()
        games = []
        for manager in self.configuration.games:
            groups = [item for item in self.configuration.groups
                      if (item.game.locale.casefold(), item.game.slug) == manager.identity]
            tracked = {team.team_id for group in groups for team in group.teams}
            entries = []
            for reference in self.references(manager):
                snapshot = self.newest_team(manager.game, reference.team_id)
                strategy = strategies.get((*manager.identity, reference.team_id))
                entries.append({
                    "id": reference.team_id, "name": reference.team_name,
                    "url": reference.source_url, "in_competition": reference.team_id in tracked,
                    "round": snapshot.team.overview.current_round if snapshot else None,
                    "generated_at": snapshot.generated_at.isoformat() if snapshot else None,
                    "strategy": strategy_to_dict(strategy) if strategy else None,
                })
            games.append({
                "locale": manager.game.locale, "slug": manager.game.slug, "name": manager.name,
                "url": manager.game.original, "archived": manager.is_archived,
                "competitions": [{"id": item.group_id, "name": item.name, "type": item.kind}
                                 for item in groups],
                "teams": sorted(entries, key=lambda item: item["id"]),
            })
        return {
            "schema_version": 1, "games": games,
            "warnings": ([{"code": "unreadable_sources",
                           "count": len(self.configuration_warnings) + len(self.teams.warnings)}]
                         if self.configuration_warnings or self.teams.warnings else []),
        }

    def rules(self, manager, metadata=None, *, warnings=None):
        evidence_unreadable = False
        try:
            profile = self.rule_pages.profile(manager.game)
            latest = self.rule_pages.latest(manager.game)
        except (OSError, ValueError, PayloadError):
            profile, latest = None, None
            evidence_unreadable = True
            if warnings is not None:
                warnings.append("rules_evidence_unreadable")
        if profile is None:
            fallback = rule_profile_for_game(
                manager.game, salary_cap=metadata.salary_cap if metadata else None,
                game_id=metadata.game_id if metadata else None, label=manager.name,
            )
            profile = (metadata.rule_profile
                       if metadata and metadata.rule_profile and (metadata.rule_profile.verified or not fallback.verified)
                       else fallback)
            if latest is not None and profile.source_sha256 != latest["source_sha256"]:
                profile = replace(profile, verified=False)
        if evidence_unreadable:
            profile = replace(profile, verified=False)
        if profile.identity != manager.identity:
            raise PayloadError("Rule profile belongs to another game")
        if metadata and profile.game_id is not None and profile.game_id != metadata.game_id:
            raise PayloadError("Rule profile game ID does not match cached metadata")
        return profile

    def context(self, manager, *, competition=None, selectors=(), history=False, analysis=False):
        settings = HubSettingsStore(self.paths.hub_settings_file).load()
        scope = resolve_scope(manager, self.configuration.groups, self.teams,
                              settings, competition_id=competition)
        if selectors:
            ids = self.team_ids(manager, selectors)
            if competition and not set(ids).issubset({ref.team_id for ref in scope.team_references}):
                raise ValueError("Selected teams are outside the requested competition")
            known = {ref.team_id: ref for ref in self.references(manager)}
            scope = replace(scope, team_references=tuple(
                known.get(identifier) or TeamReference(
                    manager.game, identifier, f"Hold {identifier}",
                    f"{manager.game.original}/fantasyteams/{identifier}"
                ) for identifier in ids
            ))
        scope = prepare_scope_export(scope, self.teams, TournamentPairingStore(self.paths.tournament_pairing_dir))
        players = PlayerStatisticsStore(self.paths.snapshot_dir).scan(manager.game)
        warnings = []
        try:
            metadata = GameMetadataStore(self.paths.game_metadata_dir).load(manager.game)
        except (OSError, ValueError, PayloadError):
            metadata = None
            warnings.append("metadata_unreadable")
        try:
            manifest = ManifestStore(self.paths.manifest_dir).load_latest(
                manager.game.slug, game_locale=manager.game.locale,
                scope="group" if competition else "game", group_id=competition,
            )
        except (OSError, ValueError, PayloadError):
            manifest = None
            warnings.append("manifest_unreadable")
        rules = self.rules(manager, metadata, warnings=warnings)
        if metadata:
            metadata = replace(metadata, rule_profile=rules)
        result = build_scope_export(scope, self.teams, players, metadata=metadata,
                                    manifest=manifest, include_history=history,
                                    warnings=self.configuration_warnings)
        try:
            capture = self.rule_pages.latest(manager.game)
        except (OSError, ValueError, PayloadError):
            capture = None
            if "rules_evidence_unreadable" not in warnings:
                warnings.append("rules_evidence_unreadable")
        result["advisory"] = {
            "schema_version": 1, "rules": game_rule_to_dict(rules),
            "rules_evidence": ({
                key: capture[key] for key in ("source_url", "document_url", "source_sha256",
                                               "fetched_at", "extraction_complete", "missing")
            } if capture else None),
            "warnings": warnings,
            "strategies": [
                {"team_id": reference.team_id,
                 "strategy": strategy_to_dict(strategy) if (
                     strategy := self.strategies.get(manager.game, reference.team_id)
                 ) else None}
                for reference in scope.team_references
            ],
        }
        if analysis:
            source = players.latest_round(manager.game)
            result["advisory"]["player_metrics"] = [
                asdict(metric) for entry in (source.statistics.entries if source else ())
                if (metric := build_player_decision_analysis(
                    players, manager.game, player_identity(manager.game, entry)
                )) is not None
            ]
        return result

    def validate(self, manager, proposal):
        if (not isinstance(proposal, dict) or proposal.get("schema_version") != 1
                or proposal.get("game") != {"locale": manager.game.locale.casefold(), "slug": manager.game.slug}):
            raise ValueError("Proposal needs schema_version 1 and the exact game locale/slug")
        if unknown := proposal.keys() - {"schema_version", "game", "target_round", "teams", "max_shared_players"}:
            raise ValueError(f"Unknown proposal fields: {', '.join(sorted(unknown))}")
        if not isinstance(proposal.get("teams"), list) or not proposal["teams"]:
            raise ValueError("Proposal must contain at least one team")
        target = _positive_int(proposal.get("target_round"), "target_round")
        metadata = GameMetadataStore(self.paths.game_metadata_dir).load(manager.game)
        rule_warnings = []
        rules = self.rules(manager, metadata, warnings=rule_warnings)
        players = PlayerStatisticsStore(self.paths.snapshot_dir).scan(manager.game).latest_round(manager.game)
        if not players:
            raise ValueError("No cached player catalogue; explicitly fetch players first")
        available = {}
        for entry in players.statistics.entries:
            if entry.entry_id is not None:
                if entry.entry_id in available:
                    raise PayloadError("Player catalogue contains duplicate stable IDs")
                available[entry.entry_id] = entry
        results, lineups, identities = [], [], set()
        for index, item in enumerate(proposal["teams"]):
            if not isinstance(item, dict):
                raise ValueError("Each proposed team must be an object")
            allowed = {"team_id", "name", "player_ids", "captain_ids", "strategy", "emergency_reason"}
            if unknown := item.keys() - allowed:
                raise ValueError(f"Unknown team proposal fields: {', '.join(sorted(unknown))}")
            identifier = item.get("team_id")
            if identifier is not None:
                _positive_int(identifier, "team_id")
            key = str(identifier) if identifier is not None else item.get("name", f"new-{index + 1}")
            if not isinstance(key, str) or not key.strip() or key in identities:
                raise ValueError("Proposed team IDs/names must be distinct")
            identities.add(key)
            ids = _ids(item.get("player_ids"), "player_ids")
            captains = _ids(item.get("captain_ids"), "captain_ids")
            lineups.append(set(ids))
            saved = self.strategies.get(manager.game, identifier) if identifier is not None else None
            if "strategy" in item and not isinstance(item["strategy"], dict):
                raise ValueError("Inline strategy must be an object")
            strategy = strategy_from_dict({
                **(strategy_to_dict(saved) if saved else {}),
                **(item.get("strategy") or {}),
            })
            errors, missing, warnings = [], [], list(rule_warnings)
            unknown_ids = set(ids) - available.keys()
            snapshot = self.newest_team(manager.game, identifier) if identifier is not None else None
            if identifier is not None and snapshot is None:
                missing.append("current_team_snapshot")
            current_ids = {entry.player_id for entry in snapshot.team.roster} if snapshot else set()
            if snapshot:
                # Deactivated/removed players may be retained or sold, never newly bought.
                unknown_ids -= current_ids
            if unknown_ids:
                errors.append(f"Unknown player IDs: {sorted(unknown_ids)}")
            sold, bought = current_ids - set(ids), set(ids) - current_ids
            if not snapshot:
                sold, bought = set(), set()
            if strategy.transfer_policy == "unspecified":
                missing.append("strategy.transfer_policy")
            if strategy.captain_policy == "unspecified":
                missing.append("strategy.captain_policy")
            if strategy.transfer_policy == "limited" and strategy.max_transfers is None:
                missing.append("strategy.max_transfers")
            changed = bool(sold or bought)
            if changed and strategy.transfer_policy == "fixed":
                errors.append("Personal strategy forbids player changes")
            if changed and strategy.transfer_policy == "emergency_only" and not (
                isinstance(item.get("emergency_reason"), str) and item["emergency_reason"].strip()
            ):
                errors.append("Emergency-only transfers need an emergency_reason")
            if strategy.max_transfers is not None and len(sold) > strategy.max_transfers:
                errors.append("Personal transfer budget exceeded")
            if strategy.start_round is not None and target < strategy.start_round or strategy.end_round is not None and target > strategy.end_round:
                errors.append("Target round is outside the personal planning horizon")
            if metadata and metadata.final_round is not None and target > metadata.final_round:
                errors.append("Target round is after the game's final round")
            baseline_captains = {entry.player_id for entry in snapshot.team.roster
                                 if entry.role.casefold() in {"captain", "kaptajn"}} if snapshot else set()
            if strategy.captain_policy == "fixed":
                fixed = {strategy.fixed_captain_id} if strategy.fixed_captain_id else baseline_captains
                if not fixed:
                    missing.append("fixed_captain_id")
                elif set(captains) != fixed:
                    errors.append("Personal strategy forbids this captain change")
            if not set(strategy.required_player_ids).issubset(ids):
                errors.append("Required players are missing")
            if set(strategy.excluded_player_ids) & set(ids):
                errors.append("Excluded players are selected")
            if not rules.verified:
                missing.append("verified_season_rules")
            if not rules.sport:
                missing.append("rules.sport")
            if rules.sport in {"soccer", "nfl", "formula1"} and not rules.position_limits:
                missing.append("rules.position_limits")
            if rules.sport in {"soccer", "cycling"} and rules.max_from_team is None:
                missing.append("rules.max_from_team")
            if rules.sport == "formula1" and not rules.captain_positions:
                missing.append("rules.captain_positions")
            if rules.sport == "golf" and (rules.category_count is None or rules.category_size is None):
                missing.append("rules.categories")
            for field in ("roster_size", "budget_enabled", "captain_count", "free_through_round"):
                if getattr(rules, field) is None:
                    missing.append(f"rules.{field}")
            if rules.budget_enabled and rules.salary_cap is None:
                missing.append("rules.salary_cap")
            if players.statistics.unit == "points" and rules.budget_enabled is True or players.statistics.unit == "money" and rules.budget_enabled is False:
                missing.append("rules.unit_mismatch")
            if snapshot and players.statistics.round_number != snapshot.team.overview.current_round:
                missing.append("matching_team_and_player_rounds")
            if snapshot and any(
                entry.player_id in available and entry.value != available[entry.player_id].value
                for entry in snapshot.team.roster
            ):
                missing.append("matching_team_and_player_prices")
            if snapshot and target < snapshot.team.overview.current_round:
                errors.append("Target round precedes the current roster")
            if target < players.statistics.round_number:
                missing.append("source_prices_after_target_round")
            if snapshot and bought and rules.transfer_fee_basis_points is None:
                missing.append("rules.transfer_fee_basis_points")
            if snapshot and bought and rules.fee_free_through_round is None:
                missing.append("rules.fee_free_through_round")
            if snapshot and bought and rules.transfer_fee_rounding is None and any(
                available[entry_id].value * (rules.transfer_fee_basis_points or 0) % 10_000
                for entry_id in bought if entry_id in available
            ) and target > (rules.fee_free_through_round or 0):
                missing.append("rules.transfer_fee_rounding")
            contracts = snapshot.team.overview.substitutions_remaining if snapshot else None
            if contracts is None and snapshot and strategy.team_type == "basis":
                limit, used = rules.base_contracts, snapshot.team.overview.substitutions_used
                if limit is not None and used is not None:
                    contracts = limit - used
            if contracts is None and snapshot and strategy.team_type == "gold" and rules.gold_contracts is not None:
                used = snapshot.team.overview.substitutions_used
                if used is not None:
                    contracts = rules.gold_contracts - used
            unlimited = strategy.team_type == "gold" and rules.gold_unlimited
            if sold and target > (rules.free_through_round or 0) and contracts is None and not unlimited:
                missing.append("remaining_contracts")
            if contracts is not None and sold and target > (rules.free_through_round or 0) and contracts - len(sold) < strategy.reserve_contracts:
                errors.append("Transfer would spend reserved contracts")
            simulation = None
            initial_fee = 0
            roster_cost = sum(available[entry_id].value for entry_id in ids if entry_id in available)
            if snapshot:
                current_prices = {entry.player_id: entry.value for entry in snapshot.team.roster}
                roster_cost = sum(available[entry_id].value if entry_id in available else current_prices.get(entry_id, 0) for entry_id in ids)
            if strategy.budget is not None and roster_cost > strategy.budget:
                errors.append("Personal roster budget exceeded")
            math_blocked = any(reason.startswith((
                "rules.", "matching_", "current_team_snapshot", "verified_season_rules",
            )) for reason in missing)
            if not unknown_ids and not math_blocked and not (identifier is not None and snapshot is None):
                if snapshot:
                    initial = snapshot.team.roster
                    bank = snapshot.team.overview.bank
                    team_round = snapshot.team.overview.current_round
                    summary = next((row for row in snapshot.team.history if row.round_number == team_round), None)
                    team_status = summary.round_status if summary else "unknown"
                else:
                    initial = tuple(RosterEntry(
                        n, entry.entry_id, entry.name, entry.team, entry.position,
                        entry.value, 0, 0, target, "captain" if entry.entry_id in captains else "none",
                        entry.is_active, entry.is_disabled, entry.is_injured, entry.has_suspension,
                    ) for n, entry_id in enumerate(ids) if (entry := available[entry_id]))
                    initial_rate = rules.initial_roster_fee_basis_points
                    if target <= (rules.fee_free_through_round or 0) or rules.transfer_fee_basis_points == 0 or rules.budget_enabled is False:
                        initial_rate = 0
                    if initial_rate is None:
                        missing.append("rules.initial_roster_fee_basis_points")
                    else:
                        fees = [_basis_points(available[entry_id].value, initial_rate, rules.transfer_fee_rounding)
                                for entry_id in ids]
                        if any(value is None for value in fees):
                            missing.append("rules.transfer_fee_rounding")
                        else:
                            initial_fee = sum(fees)
                    bank = (rules.salary_cap - roster_cost - initial_fee) if rules.salary_cap is not None else None
                    team_round, team_status = players.statistics.round_number, players.statistics.round_status
                    if any(not available[entry_id].is_active or available[entry_id].is_disabled for entry_id in ids):
                        errors.append("New teams cannot buy inactive or disabled players")
                transfer_profile = rules.to_transfer_profile()
                if not snapshot:
                    transfer_profile = replace(transfer_profile, allow_club_excess_without_trades=False)
                simulation = simulate_transfers(transfer_profile, TransferScenario(
                    initial, players.statistics.entries, tuple(sorted(sold)), tuple(sorted(bought)),
                    bank, contracts, target, team_round, players.statistics.round_number, captains,
                    team_status, players.statistics.round_status,
                ))
                errors.extend(simulation.errors)
                warnings.extend(simulation.warnings)
                if simulation.status == "unverified":
                    missing.append("complete_roster_rules")
            if players.statistics.round_status != "complete":
                warnings.append("Player observations are preliminary; this is advice, not an executable trade")
            if datetime.now(timezone.utc) - players.generated_at.astimezone(timezone.utc) >= timedelta(hours=24):
                warnings.append("Player prices are at least 24 hours old")
            result = {
                "team_id": identifier, "name": key, "status": "invalid" if errors else "unverified" if missing else "valid",
                "strategy": strategy_to_dict(strategy), "roster_cost": roster_cost,
                "sold_player_ids": sorted(sold), "bought_player_ids": sorted(bought),
                "captain_ids": list(captains),
                "ending_bank": simulation.ending_bank if simulation and not any(
                    reason in missing for reason in ("rules.initial_roster_fee_basis_points", "rules.transfer_fee_rounding")
                ) else None,
                "transfer_fee": simulation.transfer_fee + initial_fee if simulation and not any(
                    reason in missing for reason in ("rules.initial_roster_fee_basis_points", "rules.transfer_fee_rounding")
                ) else None,
                "initial_purchase_fee": initial_fee if identifier is None and not any(
                    reason in missing for reason in ("rules.initial_roster_fee_basis_points", "rules.transfer_fee_rounding")
                ) else None,
                "contracts_used": simulation.contracts_used if simulation else None,
                "contracts_remaining": simulation.contracts_remaining if simulation else contracts,
                "errors": list(dict.fromkeys(errors)), "missing": list(dict.fromkeys(missing)),
                "warnings": list(dict.fromkeys(warnings)),
                "data_certainty": simulation.certainty if simulation else "unverified",
            }
            results.append(result)
        limit = proposal.get("max_shared_players")
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 0):
            raise ValueError("max_shared_players must be a non-negative integer")
        overlaps = [{"teams": [results[a]["name"], results[b]["name"]],
                     "shared_players": len(lineups[a] & lineups[b])}
                    for a in range(len(lineups)) for b in range(a + 1, len(lineups))]
        portfolio_errors = ["Requested player-overlap limit exceeded"] if limit is not None and any(row["shared_players"] > limit for row in overlaps) else []
        counts = Counter(identifier for lineup in lineups for identifier in lineup)
        return {
            "schema_version": 1, "document_type": "advisory_validation",
            "advice_only": True, "game": proposal["game"], "target_round": target,
            "status": "invalid" if portfolio_errors or any(row["status"] == "invalid" for row in results)
                      else "unverified" if any(row["status"] == "unverified" for row in results) else "valid",
            "source_round": players.statistics.round_number,
            "source_generated_at": players.generated_at.isoformat(),
            "rules": game_rule_to_dict(rules), "teams": results,
            "portfolio": {"errors": portfolio_errors, "overlaps": overlaps,
                          "exposure": [{"player_id": identifier, "teams": count}
                                       for identifier, count in sorted(counts.items())]},
        }
