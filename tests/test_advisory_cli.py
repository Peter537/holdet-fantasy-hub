from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from cli.main import main
from cli import advisor
from holdet_lib.advisory import AdvisoryData
from holdet_lib.game_metadata import GameMetadata, GameMetadataStore
from holdet_lib.groups import GroupStore, GroupTeam
from holdet_lib.models import PlayerPerformanceStat
from holdet_lib.paths import PathOverrides, resolve_paths
from holdet_lib.refresh_runs import RefreshRunBusyError, RefreshRunCoordinator, RefreshRunResult
from holdet_lib.rules import game_rule_to_dict, rule_profile_for_game
from holdet_lib.storage import PlayerStatisticsStore, SnapshotStore
from holdet_lib.strategies import TeamStrategy, strategy_from_dict
from tests.test_decision_analysis import (
    GAME, NOW, entry, player_snapshot, roster, summary, team_snapshot, verified_rules,
)


@pytest.fixture
def seeded(tmp_path, request):
    round_number = getattr(request, "param", 3)
    paths = resolve_paths(overrides=PathOverrides(data_root=tmp_path / "hub"), environ={})
    group_store = GroupStore(paths.groups_file, paths.group_revision_dir)
    manager = group_store.create_manager_game(GAME, "My game")
    team = team_snapshot(101, round_number, (roster(1, 20, role="captain"), roster(2, 10, role="none")),
                         (summary(round_number, 30),)).team
    team = replace(team, reference=replace(team.reference,
                   source_url=f"{GAME.original}/fantasyteams/101"))
    SnapshotStore(paths.snapshot_dir).save_team_json(team, now=NOW)
    group_store.create("League", GAME, (GroupTeam(101, team.team_name, team.reference.source_url),), group_id="league")
    players = tuple(replace(entry(identifier, identifier * 10),
                            stats=(PlayerPerformanceStat("Goals", 2),),
                            popularity=0.25, index=7) for identifier in (1, 2, 3))
    PlayerStatisticsStore(paths.snapshot_dir).save(player_snapshot(round_number, players).statistics, now=NOW)
    GameMetadataStore(paths.game_metadata_dir).save(GameMetadata(
        GAME, "soccer", "soccer", 1, 3_000_000, 17, "My game", (), NOW,
        verified_rules(sport="soccer", free_through_round=1, fee_free_through_round=1),
    ))
    return paths, manager


def proposal(*, ids=(1, 2), captains=(1,), team_id=101, policy="active", captain_policy="fixed", **strategy):
    item = {"player_ids": list(ids), "captain_ids": list(captains),
            "strategy": {"transfer_policy": policy, "captain_policy": captain_policy, **strategy}}
    if team_id is not None:
        item["team_id"] = team_id
    return {"schema_version": 1, "game": {"locale": GAME.locale, "slug": GAME.slug},
            "target_round": 4, "teams": [item]}


def run_cli(capsys, paths, args):
    result = main(["--data-dir", str(paths.data_dir.parent), *args])
    captured = capsys.readouterr()
    return result, json.loads(captured.out) if captured.out else None, captured.err


def fingerprints(root):
    return {path.relative_to(root): sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*") if path.is_file()}


def test_review_commands_are_offline_complete_and_leave_sources_untouched(seeded, capsys, tmp_path):
    paths, _ = seeded
    before = fingerprints(paths.data_dir.parent)
    query = tmp_path / "proposal.json"
    query.write_text(json.dumps(proposal()), encoding="utf-8")
    with patch("holdet_lib.http.HttpClient.fetch_text", side_effect=AssertionError("network")):
        status, games, _ = run_cli(capsys, paths, ["games"])
        assert status == 0 and games["games"][0]["teams"][0]["id"] == 101
        status, context, _ = run_cli(capsys, paths, ["context", "My game", "--history", "--analysis"])
        assert status == 0
        player = context["players"]["current"]["entries"][0]
        assert player["entry_id"] == 1
        assert player["stats"] == [{"name": "Goals", "value": 2.0}]
        assert player["popularity"] == 0.25 and player["index"] == 7
        assert context["players"]["history"]
        assert context["advisory"]["player_metrics"][0]["source_round"] == 3
        assert context["teams"][0]["snapshot"]
        for args in (["rules", GAME.slug], ["strategy", GAME.slug, "--team", "101"],
                     ["refresh", GAME.slug], ["validate", GAME.slug, "--proposal", str(query)]):
            status, value, errors = run_cli(capsys, paths, args)
            assert status == 0, errors
            assert value
    assert fingerprints(paths.data_dir.parent) == before


def test_context_can_select_an_untracked_cached_team_but_not_outside_a_competition(seeded):
    paths, manager = seeded
    second = team_snapshot(202, 3, (roster(1, 10, role="captain"), roster(2, 20, role="none")), (summary(3, 30),))
    second = replace(second, team=replace(second.team, reference=replace(second.team.reference,
                     source_url=f"{GAME.original}/fantasyteams/202")))
    SnapshotStore(paths.snapshot_dir).save_team_json(second.team, now=NOW)
    data = AdvisoryData(paths)
    assert [row["id"] for row in data.context(manager, selectors=["202"])["teams"]] == [202]
    with pytest.raises(ValueError, match="outside"):
        data.context(manager, selectors=["202"], competition="league")


def test_context_does_not_replace_current_data_with_late_historical_backfill(seeded):
    paths, manager = seeded
    older = team_snapshot(101, 1, (roster(3, 4, role="captain"),), (summary(1, 4),))
    older = replace(older, team=replace(older.team, reference=replace(older.team.reference,
                    source_url=f"{GAME.original}/fantasyteams/101")))
    SnapshotStore(paths.snapshot_dir).save_team_json(older.team, now=NOW + timedelta(days=1))
    PlayerStatisticsStore(paths.snapshot_dir).save(player_snapshot(1, (entry(3, 4),)).statistics,
                                                   now=NOW + timedelta(days=1))
    data = AdvisoryData(paths)
    context = data.context(manager, history=True)
    assert context["teams"][0]["snapshot"]["game"]["current_round"] == 3
    assert context["players"]["current"]["game"]["round"] == 3
    assert data.validate(manager, proposal())["status"] == "valid"


@pytest.mark.parametrize("corrupt_archive", [False, True])
def test_context_reports_omitted_archived_revisions_without_leaking_source_paths(seeded, capsys, corrupt_archive):
    paths, _ = seeded
    store = GroupStore(paths.groups_file, paths.group_revision_dir)
    members = tuple(GroupTeam(identifier, f"Team {identifier}", f"{GAME.original}/fantasyteams/{identifier}")
                    for identifier in (101, 202, 303, 404))
    store.create_tournament("Cup", GAME, members, start_round=1, final_round=5,
        rounds_per_tie=1, group_id="cup", template="swiss", definition_options={
            "seed_rule": "manual", "seed_order": (101, 202, 303, 404), "swiss_rounds": 2,
        })
    store.rebuild_tournament("cup", members[:2], final_round=5, template="league")
    revision = paths.group_revision_dir / "cup" / "revision-1.json"
    if corrupt_archive:
        revision.write_text("{", encoding="utf-8")
    before = fingerprints(paths.data_dir.parent)
    status, result, errors = run_cli(capsys, paths, ["context", GAME.slug, "--competition", "cup"])
    assert status == 0, errors
    assert result["competitions"][0]["active_revision"] == 2
    assert len(result["competitions"][0]["previous_revisions"]) == (0 if corrupt_archive else 1)
    source_warnings = [warning for warning in result["warnings"] if "source warning(s)" in warning]
    assert source_warnings == (["1 source warning(s); see the application for details"] if corrupt_archive else [])
    assert all(str(revision) not in warning and "revision-1.json" not in warning for warning in result["warnings"])
    assert fingerprints(paths.data_dir.parent) == before


@pytest.mark.parametrize(("query", "status", "reason"), [
    (proposal(ids=(1, 3), policy="fixed"), "invalid", "forbids player changes"),
    (proposal(captains=(2,)), "invalid", "captain change"),
    (proposal(ids=(1, 3), max_transfers=0), "invalid", "transfer budget"),
    (proposal(ids=(1, 3), reserve_contracts=3), "invalid", "reserved contracts"),
    (proposal(ids=(1, 3), policy="emergency_only"), "invalid", "emergency_reason"),
    (proposal(policy="limited"), "unverified", "strategy.max_transfers"),
    (proposal(required_player_ids=[3]), "invalid", "Required players"),
    (proposal(excluded_player_ids=[2]), "invalid", "Excluded players"),
    (proposal(end_round=3), "invalid", "planning horizon"),
    (proposal(), "valid", None),
    (proposal(ids=(1, 3)), "valid", None),
])
def test_personal_restrictions_are_independent_from_official_allowances(seeded, query, status, reason):
    paths, manager = seeded
    result = AdvisoryData(paths).validate(manager, query)
    assert result["status"] == status
    if reason:
        assert reason in " ".join(result["teams"][0]["errors"] + result["teams"][0]["missing"])
    if status == "valid" and query["teams"][0]["player_ids"] == [1, 3]:
        assert result["teams"][0]["transfer_fee"] == 10_000
        assert result["teams"][0]["ending_bank"] == 1_990_000
        assert result["teams"][0]["contracts_remaining"] == 2


def test_emergency_exception_is_explicit_and_cannot_override_completely_fixed(seeded):
    paths, manager = seeded
    data = AdvisoryData(paths)
    query = proposal(ids=(1, 3), policy="emergency_only")
    query["teams"][0]["emergency_reason"] = "Player 2 has left the league"
    assert data.validate(manager, query)["status"] == "valid"
    query["teams"][0]["strategy"]["transfer_policy"] = "fixed"
    assert data.validate(manager, query)["status"] == "invalid"


def test_saved_strategy_is_read_and_inline_explicit_updates_take_precedence(seeded, capsys, tmp_path):
    paths, manager = seeded
    source = tmp_path / "strategy.json"
    source.write_text(json.dumps({"transfer_policy": "fixed", "captain_policy": "fixed",
                                  "fixed_captain_id": 1, "identity": "GC focus"}), encoding="utf-8")
    status, saved, _ = run_cli(capsys, paths, ["strategy", GAME.slug, "--team", "101", "--set", str(source)])
    assert status == 0 and saved["strategy"]["identity"] == "GC focus"
    data = AdvisoryData(paths)
    query = proposal(ids=(1, 3))
    query["teams"][0].pop("strategy")
    assert data.validate(manager, query)["status"] == "invalid"
    query["teams"][0]["strategy"] = {"transfer_policy": "active"}
    assert data.validate(manager, query)["status"] == "valid"
    assert data.strategies.get(GAME, 101).transfer_policy == "fixed"


def test_missing_contracts_do_not_become_unlimited(seeded):
    paths, manager = seeded
    team = AdvisoryData(paths).newest_team(GAME, 101).team
    team = replace(team, overview=replace(team.overview, substitutions_remaining=None, substitutions_used=None))
    SnapshotStore(paths.snapshot_dir).save_team_json(team, now=NOW + timedelta(hours=1))
    data = AdvisoryData(paths)
    result = data.validate(manager, proposal(ids=(1, 3)))
    assert result["status"] == "unverified"
    assert "remaining_contracts" in result["teams"][0]["missing"]
    assert data.validate(manager, proposal())["status"] == "valid"


def test_round_and_price_mismatches_prevent_certification(seeded):
    paths, manager = seeded
    PlayerStatisticsStore(paths.snapshot_dir).save(player_snapshot(4, (entry(1, 1), entry(2, 1), entry(3, 1))).statistics,
                                                   now=NOW + timedelta(hours=1))
    result = AdvisoryData(paths).validate(manager, proposal())
    assert result["status"] == "unverified"
    assert "matching_team_and_player_rounds" in result["teams"][0]["missing"]


@pytest.mark.parametrize("seeded", [1], indirect=True)
def test_new_roster_checks_budget_disabled_players_and_permanent_captain(seeded):
    paths, manager = seeded
    query = proposal(ids=(1, 3), team_id=None, policy="fixed", fixed_captain_id=1)
    query["target_round"] = 1
    result = AdvisoryData(paths).validate(manager, query)
    assert result["status"] == "valid"
    assert result["teams"][0]["ending_bank"] == 1_000_000
    assert result["teams"][0]["contracts_used"] == 0
    bad = replace(entry(3, 1), is_disabled=True)
    PlayerStatisticsStore(paths.snapshot_dir).save(player_snapshot(1, (entry(1, 1), entry(2, 1), bad)).statistics,
                                                   now=NOW + timedelta(hours=1))
    assert AdvisoryData(paths).validate(manager, query)["status"] == "invalid"


@pytest.mark.parametrize("seeded", [1], indirect=True)
def test_diversity_limits_are_optional_and_exposure_is_reported(seeded):
    paths, manager = seeded
    query = proposal(team_id=None, policy="fixed", fixed_captain_id=1)
    query["target_round"] = 1
    query["teams"][0]["name"] = "Main"
    query["teams"].append({**query["teams"][0], "name": "Alternative"})
    data = AdvisoryData(paths)
    result = data.validate(manager, query)
    assert result["status"] == "valid"
    assert result["portfolio"]["exposure"] == [{"player_id": 1, "teams": 2}, {"player_id": 2, "teams": 2}]
    query["max_shared_players"] = 1
    assert data.validate(manager, query)["status"] == "invalid"


def test_unverified_rules_are_preserved_instead_of_using_format_defaults(seeded):
    paths, manager = seeded
    store = GameMetadataStore(paths.game_metadata_dir)
    store.save(replace(store.load(GAME), rule_profile=rule_profile_for_game(GAME)))
    result = AdvisoryData(paths).validate(manager, proposal())
    assert result["status"] == "unverified"
    assert "verified_season_rules" in result["teams"][0]["missing"]


def test_new_team_after_start_uses_documented_fees_instead_of_guessing(seeded):
    paths, manager = seeded
    query = proposal(team_id=None, policy="fixed", fixed_captain_id=1)
    result = AdvisoryData(paths).validate(manager, query)
    assert result["status"] == "unverified"
    assert result["teams"][0]["ending_bank"] is None
    store = GameMetadataStore(paths.game_metadata_dir)
    metadata = store.load(GAME)
    store.save(replace(metadata, rule_profile=replace(metadata.rule_profile, transfer_fee_basis_points=0)))
    result = AdvisoryData(paths).validate(manager, query)
    assert result["status"] == "valid"
    assert result["teams"][0]["transfer_fee"] == 0
    assert result["teams"][0]["ending_bank"] == 1_000_000


def test_club_retention_exception_never_applies_to_a_new_roster(seeded):
    paths, manager = seeded
    store = GameMetadataStore(paths.game_metadata_dir)
    metadata = store.load(GAME)
    store.save(replace(metadata, rule_profile=replace(metadata.rule_profile,
                      max_from_team=1, allow_club_excess_without_trades=True, initial_roster_fee_basis_points=0)))
    data = AdvisoryData(paths)
    assert data.validate(manager, proposal())["status"] == "valid"
    assert data.validate(manager, proposal(team_id=None, policy="fixed", fixed_captain_id=1))["status"] == "invalid"


def test_unknown_fractional_fee_rounding_cannot_produce_a_valid_trade(seeded):
    paths, manager = seeded
    players = PlayerStatisticsStore(paths.snapshot_dir)
    catalogue = players.scan(GAME).latest_round(GAME).statistics
    players.save(replace(catalogue, entries=tuple(
        replace(row, value=1_000_001) if row.entry_id == 3 else row for row in catalogue.entries
    )), now=NOW + timedelta(hours=1))
    store = GameMetadataStore(paths.game_metadata_dir)
    metadata = store.load(GAME)
    store.save(replace(metadata, rule_profile=replace(metadata.rule_profile, transfer_fee_rounding=None)))
    result = AdvisoryData(paths).validate(manager, proposal(ids=(1, 3)))
    assert result["status"] == "unverified"
    assert result["teams"][0]["transfer_fee"] is None


def test_strategy_and_audited_evidence_survive_backup_restore(seeded, tmp_path):
    from holdet_lib.backup import create_backup_bytes, restore_backup
    from holdet_lib.integrity import canonical_local_files
    from holdet_lib.rule_pages import RulePageStore
    from holdet_lib.strategies import StrategyStore
    from tests.rule_page_fixtures import capture
    from holdet_lib.rules import game_rule_to_dict
    paths, _ = seeded
    strategy = TeamStrategy(transfer_policy="fixed", captain_policy="fixed", fixed_captain_id=1)
    StrategyStore(paths.strategy_file).set(GAME, 101, strategy)
    rules = RulePageStore(paths.rules_dir)
    audited = rules.audit(GAME, capture(), game_rule_to_dict(verified_rules(sport="soccer")), "Reviewed all relevant sections")
    content, manifest = create_backup_bytes(paths, now=NOW)
    assert any(item.path.startswith("data/rules/da/test-season/audit-") for item in manifest.files)
    assert "config/team-strategies.json" in {item.path for item in manifest.files}
    assert not any(item.path.endswith(".lock") for item in manifest.files)
    assert not any(relative.endswith(".lock") for relative, _ in canonical_local_files(paths))
    restored = resolve_paths(overrides=PathOverrides(data_root=tmp_path / "restored"), environ={})
    restore_backup(content, restored, now=NOW)
    assert StrategyStore(restored.strategy_file).get(GAME, 101) == strategy
    assert RulePageStore(restored.rules_dir).profile(GAME) == audited
    assert any(relative.startswith("data/rules/") for relative, _ in canonical_local_files(restored))


@pytest.mark.parametrize("bad", [
    {"transfer_policy": "daily-ish"}, {"captain_policy": "never"},
    {"max_transfers": True}, {"reserve_contracts": None},
    {"required_player_ids": [1, 1]}, {"required_player_ids": [False]},
    {"required_player_ids": [1], "excluded_player_ids": [1]},
    {"end_round": 2, "start_round": 3}, {"typo": "fixed"},
])
def test_malformed_strategy_is_rejected(bad):
    with pytest.raises(ValueError):
        strategy_from_dict(bad)


def test_json_stdin_and_invalid_proposals_have_useful_exit_codes(seeded, capsys):
    from io import StringIO
    paths, _ = seeded
    with patch("sys.stdin", StringIO(json.dumps(proposal(captains=(2,))))):
        status, result, errors = run_cli(capsys, paths, ["validate", GAME.slug, "--proposal", "-"])
    assert status == 1 and result["status"] == "invalid" and not errors


def test_shared_refresh_worker_reports_partial_failure_without_polluting_stdout(seeded, capsys):
    paths, _ = seeded
    seen = []
    def action(task, *, progress, **kwargs):
        seen.append(task)
        progress({"source": "players", "status": "failed"})
        return RefreshRunResult("partial", detail="one source failed")
    coordinator = RefreshRunCoordinator(paths.refresh_run_dir, action)
    with patch("website.refresh_service.create_refresh_coordinator", return_value=coordinator):
        status, result, _ = run_cli(capsys, paths, ["refresh", GAME.slug, "--execute"])
    assert status == 1 and result["status"] == "partial"
    assert seen[0]["game"] == GAME.slug
    status, value, _ = run_cli(capsys, paths, ["refresh", "--status", result["run_id"]])
    assert status == 1 and value["status"] == "partial"


def test_separate_configuration_override_and_json_path_preflight(tmp_path, capsys):
    root, config = tmp_path / "root", tmp_path / "separate"
    status = main(["--data-dir", str(root), "--config-dir", str(config), "data", "paths", "--json"])
    result = json.loads(capsys.readouterr().out)
    assert status == 0 and result["paths"]["config_dir"]["path"] == str(config.resolve())
    assert not root.exists() and not config.exists()


def test_sandbox_redirection_uses_desktop_cache_only_without_explicit_root(tmp_path):
    from argparse import Namespace
    native = tmp_path / "desktop"
    (native / "data" / "snapshots").mkdir(parents=True)
    sandbox = resolve_paths(overrides=PathOverrides(data_root=tmp_path / "Packages" / "sandbox.test"), environ={})
    with patch.object(advisor, "resolve_paths", side_effect=[sandbox, resolve_paths(overrides=PathOverrides(data_root=native), environ={})]):
        paths, warnings = advisor.resolve_cli_paths(Namespace(), environ={}, native_root=native)
    assert paths.snapshot_dir == native / "data" / "snapshots" and warnings
    with patch.object(advisor, "resolve_paths", return_value=sandbox):
        paths, warnings = advisor.resolve_cli_paths(Namespace(data_dir=tmp_path / "chosen"), environ={}, native_root=native)
    assert paths == sandbox and not warnings


@pytest.mark.parametrize("attribute", ["team", "position"])
def test_retained_players_use_current_catalogue_attributes_for_roster_limits(seeded, attribute):
    paths, manager = seeded
    metadata_store = GameMetadataStore(paths.game_metadata_dir)
    metadata = metadata_store.load(GAME)
    if attribute == "team":
        metadata_store.save(replace(metadata, rule_profile=replace(
            metadata.rule_profile, max_from_team=1, allow_club_excess_without_trades=True,
        )))
    players = PlayerStatisticsStore(paths.snapshot_dir)
    catalogue = players.scan(GAME).latest_round(GAME).statistics
    entries = tuple(
        replace(row, team="B") if attribute == "team" else
        replace(row, position="Defender") if row.entry_id == 1 else row
        for row in catalogue.entries
    )
    players.save(replace(catalogue, entries=entries), now=NOW + timedelta(minutes=1))
    data = AdvisoryData(paths)
    result = data.validate(manager, proposal(ids=(1, 3)))
    assert result["status"] == "invalid"
    assert result["teams"][0]["transfer_fee"] == 10_000
    assert result["teams"][0]["ending_bank"] == 1_990_000
    assert result["teams"][0]["errors"]
    if attribute == "team":
        assert data.validate(manager, proposal())["status"] == "valid"


@pytest.mark.parametrize("seeded", [1], indirect=True)
@pytest.mark.parametrize("new_team", [False, True])
def test_injury_and_suspension_warnings_cover_retained_and_initial_selections(seeded, new_team):
    paths, manager = seeded
    players = PlayerStatisticsStore(paths.snapshot_dir)
    catalogue = players.scan(GAME).latest_round(GAME).statistics
    players.save(replace(catalogue, entries=tuple(
        replace(row, is_injured=row.entry_id == 1, has_suspension=row.entry_id == 2)
        for row in catalogue.entries
    )), now=NOW + timedelta(minutes=1))
    query = proposal(team_id=None if new_team else 101, fixed_captain_id=1)
    query["target_round"] = 1
    result = AdvisoryData(paths).validate(manager, query)
    assert result["status"] == "valid"
    warnings = result["teams"][0]["warnings"]
    assert any("Player 1" in warning and "skadet" in warning for warning in warnings)
    assert any("Player 2" in warning and "karantæne" in warning for warning in warnings)
    sold = AdvisoryData(paths).validate(manager, proposal(ids=(1, 3)))
    assert not any("karantæne" in warning for warning in sold["teams"][0]["warnings"])


@pytest.mark.parametrize("damaged", ["historical", "latest", "header", "audit"])
def test_context_is_readable_when_rule_evidence_is_damaged(seeded, damaged):
    from holdet_lib.rule_pages import RulePageStore
    from holdet_lib.rules import game_rule_to_dict
    from tests.rule_page_fixtures import HTML, capture
    paths, manager = seeded
    store = RulePageStore(paths.rules_dir)
    old_path = store.save(GAME, capture(HTML + "<!-- previous -->"))
    current = capture(now=NOW + timedelta(hours=1))
    store.audit(GAME, current, game_rule_to_dict(verified_rules(sport="soccer")), "Reviewed rules")
    path = (old_path if damaged in {"historical", "header"} else
            next(paths.rules_dir.rglob("audit-*.json")) if damaged == "audit" else store.save(GAME, current))
    if damaged in {"header", "audit"}:
        path.write_text("{", encoding="utf-8")
    else:
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["html"] += "tampered"
        path.write_text(json.dumps(raw), encoding="utf-8")
    data = AdvisoryData(paths)
    result = data.context(manager)
    assert result["players"]["current"] and result["teams"][0]["snapshot"]
    assert result["advisory"]["rules"]["verified"] is (damaged == "historical")
    if damaged != "historical":
        assert "rules_evidence_unreadable" in result["advisory"]["warnings"]
        validation = data.validate(manager, proposal())
        assert validation["status"] == "unverified"
        assert "rules_evidence_unreadable" in validation["teams"][0]["warnings"]


def test_refresh_can_plan_and_execute_a_metadata_repair(seeded, capsys):
    paths, _ = seeded
    for path in paths.game_metadata_dir.rglob("*.json"):
        path.write_text("{", encoding="utf-8")
    seen = []
    def action(task, **kwargs):
        seen.append(task)
        return RefreshRunResult("success")
    coordinator = RefreshRunCoordinator(paths.refresh_run_dir, action)
    with patch("website.refresh_service.create_refresh_coordinator", return_value=coordinator):
        status, plan, errors = run_cli(capsys, paths, ["refresh", GAME.slug])
        assert status == 0, errors
        assert "metadata_unreadable" in plan["games"][0]["warnings"]
        assert any(step["step_id"] == "metadata" and step["selected"] for step in plan["games"][0]["steps"])
        status, result, errors = run_cli(capsys, paths, ["refresh", GAME.slug, "--execute"])
    assert status == 0 and result["status"] == "success", errors
    assert seen[0]["game"] == GAME.slug


@pytest.mark.parametrize("action", ["status", "cancel", "resume"])
def test_refresh_journal_actions_do_not_load_damaged_groups(seeded, capsys, action):
    from unittest.mock import Mock
    paths, _ = seeded
    paths.groups_file.write_text("{", encoding="utf-8")
    coordinator = Mock()
    getattr(coordinator, "snapshot" if action == "status" else action).return_value = {
        "run_id": "existing-run", "status": "success", "games": [],
    }
    with patch("website.refresh_service.create_refresh_coordinator", return_value=coordinator):
        status, result, errors = run_cli(capsys, paths, ["refresh", "--" + action, "existing-run"])
    assert status == 0 and result["run_id"] == "existing-run", errors
    getattr(coordinator, "snapshot" if action == "status" else action).assert_called_once_with("existing-run")


@pytest.mark.parametrize("custom_exports", [False, True])
def test_sandbox_fallback_preserves_explicit_export_environment(tmp_path, custom_exports):
    from argparse import Namespace
    from types import SimpleNamespace
    native, config = tmp_path / "desktop", tmp_path / "config"
    sandbox = tmp_path / "Packages" / "sandbox.test"
    (native / "data" / "snapshots").mkdir(parents=True)
    env = {"HOLDET_CONFIG_DIR": str(config)}
    if custom_exports:
        env["HOLDET_OUTPUT_DIR"] = str(tmp_path / "custom-exports")
    def platform_dirs(*args, **kwargs):
        return SimpleNamespace(user_config_path=sandbox, user_data_path=sandbox)
    with patch("platformdirs.PlatformDirs", side_effect=platform_dirs):
        paths, warnings = advisor.resolve_cli_paths(Namespace(), environ=env, native_root=native)
    assert paths.snapshot_dir == native / "data" / "snapshots" and warnings
    assert paths.config_dir == config
    assert paths.export_dir == (tmp_path / "custom-exports" if custom_exports else native / "exports")


@pytest.mark.parametrize("action", ["start", "resume"])
def test_busy_refresh_returns_a_cli_error_without_a_traceback(seeded, capsys, action):
    from unittest.mock import Mock
    paths, _ = seeded
    coordinator = Mock()
    getattr(coordinator, action).side_effect = RefreshRunBusyError("Refresh already running")
    args = [GAME.slug, "--execute"] if action == "start" else ["--resume", "existing-run"]
    with patch("website.refresh_service.create_refresh_coordinator", return_value=coordinator):
        status, result, errors = run_cli(capsys, paths, ["refresh", *args])
    assert status == 1 and result is None
    assert errors.strip() == "error: Refresh already running"


@pytest.mark.parametrize("field", ["bank_interest_rounding", "transfer_fee_rounding"])
@pytest.mark.parametrize("source", ["audit", "metadata"])
def test_malformed_rounding_in_saved_evidence_does_not_abort_context(seeded, capsys, field, source):
    from holdet_lib.rule_pages import RulePageStore
    from tests.rule_page_fixtures import capture
    paths, manager = seeded
    if source == "audit":
        RulePageStore(paths.rules_dir).audit(GAME, capture(), game_rule_to_dict(verified_rules(sport="soccer")), "Reviewed synthetic evidence")
        path = next(paths.rules_dir.rglob("audit-*.json"))
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["profile"][field] = []
    else:
        path = next(paths.game_metadata_dir.glob("*.json"))
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["rules"][field] = {}
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = fingerprints(paths.data_dir.parent)
    status, result, errors = run_cli(capsys, paths, ["context", GAME.slug])
    assert status == 0, errors
    assert result["players"]["current"] and result["teams"][0]["snapshot"]
    assert not result["advisory"]["rules"]["verified"]
    assert ("rules_evidence_unreadable" if source == "audit" else "metadata_unreadable") in result["advisory"]["warnings"]
    assert fingerprints(paths.data_dir.parent) == before


@pytest.mark.parametrize("action", ["cached", "fetch", "save", "audit"])
def test_rules_operations_return_results_when_optional_metadata_is_corrupt(seeded, capsys, tmp_path, action):
    from holdet_lib.rule_pages import RulePageStore
    from tests.rule_page_fixtures import capture
    paths, _ = seeded
    evidence = capture()
    store = RulePageStore(paths.rules_dir)
    store.audit(GAME, evidence, game_rule_to_dict(verified_rules(sport="soccer")), "Reviewed synthetic evidence")
    for path in paths.game_metadata_dir.glob("*.json"):
        path.write_text("{", encoding="utf-8")
    before = fingerprints(paths.data_dir.parent)
    args = ["rules", GAME.slug]
    if action in {"fetch", "save"}:
        args.append("--fetch")
    if action == "save":
        args.append("--save")
    if action == "audit":
        profile = tmp_path / "profile.json"
        profile.write_text(json.dumps(game_rule_to_dict(verified_rules(sport="soccer"))), encoding="utf-8")
        args.extend(["--audit", str(profile), "--review-note", "Reviewed synthetic evidence"])
    original_save = RulePageStore.save
    with patch.object(advisor, "fetch_rule_page", return_value=evidence) as fetch, patch.object(
        RulePageStore, "save", autospec=True, side_effect=original_save,
    ) as save:
        status, result, errors = run_cli(capsys, paths, args)
    assert status == 0, errors
    assert result["capture"]["source_sha256"] == evidence["source_sha256"]
    assert result["profile"]["verified"]
    assert "metadata_unreadable" in result["warnings"] and "metadata_unreadable" in errors
    assert fetch.call_count == (1 if action in {"fetch", "save"} else 0)
    assert save.call_count == (1 if action in {"save", "audit"} else 0)
    if action in {"cached", "fetch"}:
        assert fingerprints(paths.data_dir.parent) == before


@pytest.mark.parametrize("field", ["bank_interest_rounding", "transfer_fee_rounding"])
def test_rules_audit_rejects_malformed_rounding_without_publishing(seeded, capsys, tmp_path, field):
    from holdet_lib.rule_pages import RulePageStore
    from tests.rule_page_fixtures import capture
    paths, _ = seeded
    RulePageStore(paths.rules_dir).save(GAME, capture())
    raw = game_rule_to_dict(verified_rules(sport="soccer"))
    raw[field] = []
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps(raw), encoding="utf-8")
    before = fingerprints(paths.data_dir.parent)
    status, result, errors = run_cli(capsys, paths, ["rules", GAME.slug, "--audit", str(profile), "--review-note", "Synthetic review"])
    assert status == 1 and result is None and "error:" in errors
    assert "Traceback" not in errors
    assert fingerprints(paths.data_dir.parent) == before


def test_busy_strategy_save_is_a_cli_error_and_preserves_saved_policy(seeded, capsys, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from holdet_lib.persistence import exclusive_configuration_update
    from holdet_lib.strategies import StrategyStore
    paths, _ = seeded
    StrategyStore(paths.strategy_file).set(GAME, 101, TeamStrategy(transfer_policy="fixed"))
    before = paths.strategy_file.read_bytes()
    source = tmp_path / "strategy.json"
    source.write_text(json.dumps({"transfer_policy": "active"}), encoding="utf-8")
    held, release = threading.Event(), threading.Event()

    def hold_configuration():
        with exclusive_configuration_update(paths.config_dir):
            held.set()
            assert release.wait(10)

    with ThreadPoolExecutor(max_workers=1) as pool:
        owner = pool.submit(hold_configuration)
        try:
            assert held.wait(5)
            with patch(
                "holdet_lib.strategies.exclusive_configuration_update",
                side_effect=lambda path: exclusive_configuration_update(path, timeout=0),
            ):
                status, result, errors = run_cli(capsys, paths, ["strategy", GAME.slug, "--team", "101", "--set", str(source)])
        finally:
            release.set()
        owner.result(timeout=5)
    assert status == 1 and result is None and "busy" in errors
    assert "Traceback" not in errors and paths.strategy_file.read_bytes() == before
    status, result, errors = run_cli(capsys, paths, ["strategy", GAME.slug, "--team", "101", "--set", str(source)])
    assert status == 0 and result["strategy"]["transfer_policy"] == "active", errors


@pytest.mark.parametrize("failure", ["fetch", "save", "audit"])
def test_rules_operation_failures_remain_errors(seeded, capsys, tmp_path, failure):
    from holdet_lib.errors import PayloadError
    from holdet_lib.rule_pages import RulePageStore
    from tests.rule_page_fixtures import capture
    paths, _ = seeded
    evidence = capture()
    store = RulePageStore(paths.rules_dir)
    args = ["rules", GAME.slug]
    if failure == "audit":
        store.save(GAME, evidence)
        source = tmp_path / "profile.json"
        source.write_text("[]", encoding="utf-8")
        args.extend(["--audit", str(source), "--review-note", "Synthetic review"])
        status, result, errors = run_cli(capsys, paths, args)
    else:
        args.append("--fetch")
        if failure == "save":
            args.append("--save")
        with patch.object(advisor, "fetch_rule_page", side_effect=PayloadError("synthetic fetch failure") if failure == "fetch" else None,
                          return_value=evidence), patch.object(RulePageStore, "save", side_effect=OSError("synthetic write failure")):
            status, result, errors = run_cli(capsys, paths, args)
    assert status == 1 and result is None and "error:" in errors
    assert not list(paths.rules_dir.rglob("audit-*.json"))
