from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import replace
from io import BytesIO
import json
import threading
from unittest.mock import patch
import zipfile

import pytest

from holdet_lib.accounts import AccountStore, rename_account_and_groups
from holdet_lib.analysis_inbox import AnalysisInboxStore
from holdet_lib.backup import create_backup, create_backup_bytes
from holdet_lib.groups import GroupStore, GroupTeam
from holdet_lib.hub_settings import HubSettings, HubSettingsStore
from holdet_lib.integrity import preview_integrity_repair, repair_integrity_index
from holdet_lib.paths import PathOverrides, resolve_paths
from holdet_lib.persistence import exclusive_configuration_update
from holdet_lib.refresh_runs import RefreshRunBusyError, exclusive_data_operation
from holdet_lib.seasons import SeasonStore
from tests.test_decision_analysis import GAME


def paths_for(root):
    return resolve_paths(overrides=PathOverrides(data_root=root), environ={})


@pytest.mark.parametrize("kind", ("accounts", "groups", "settings", "inbox", "seasons"))
def test_configuration_writers_wait_for_namespace_owner(tmp_path, kind):
    paths = paths_for(tmp_path / "hub")
    operations = {
        "accounts": lambda: AccountStore(paths.accounts_file).save(()),
        "groups": lambda: GroupStore(paths.groups_file).create_manager_game(GAME),
        "settings": lambda: HubSettingsStore(paths.hub_settings_file).save(HubSettings()),
        "inbox": lambda: AnalysisInboxStore(paths.analysis_inbox_file).save(()),
        "seasons": lambda: SeasonStore(paths.seasons_file).save(()),
    }
    started = threading.Event()

    def write():
        started.set()
        operations[kind]()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with exclusive_configuration_update(paths.config_dir):
            pending = pool.submit(write)
            assert started.wait(5)
            with pytest.raises(FutureTimeout):
                pending.result(timeout=0.1)
        pending.result(timeout=5)


def test_group_read_modify_write_preserves_overlapping_creates(tmp_path):
    paths = paths_for(tmp_path / "hub")
    first = GroupStore(paths.groups_file)
    second = GroupStore(paths.groups_file)
    read, release = threading.Event(), threading.Event()
    load = first.load_configuration

    def paused_load(*args, **kwargs):
        value = load(*args, **kwargs)
        read.set()
        assert release.wait(5)
        return value

    first.load_configuration = paused_load
    with ThreadPoolExecutor(max_workers=2) as pool:
        a = pool.submit(first.create_manager_game, GAME, "First")
        try:
            assert read.wait(5)
            other_game = replace(GAME, slug="another-game")
            b = pool.submit(second.create_manager_game, other_game, "Second")
            with pytest.raises(FutureTimeout):
                b.result(timeout=0.1)
        finally:
            release.set()
        a.result(timeout=5)
        b.result(timeout=5)
    assert {item.name for item in second.load_configuration().games} == {"First", "Second"}


@pytest.mark.parametrize("bytes_output", (False, True))
def test_backup_refuses_busy_data_root_before_publication(tmp_path, bytes_output):
    paths = paths_for(tmp_path / "hub")
    GroupStore(paths.groups_file).create_manager_game(GAME)
    with exclusive_data_operation(paths.data_dir):
        with pytest.raises(RefreshRunBusyError):
            if bytes_output:
                create_backup_bytes(paths)
            else:
                create_backup(paths)
    assert not paths.backup_dir.exists()


def test_backup_waits_for_complete_account_and_group_rename(tmp_path):
    paths = paths_for(tmp_path / "hub")
    accounts = AccountStore(paths.accounts_file)
    account = accounts.create("Original", "https://www.holdet.dk/da/users/12345/teams")
    groups = GroupStore(paths.groups_file, paths.group_revision_dir)
    groups.create("Group", GAME, (GroupTeam(
        101, "Team", f"{GAME.original.rstrip('/')}/hold/101",
        account_key=account.key, account_label=account.label,
    ),), group_id="group")
    saved_accounts, release = threading.Event(), threading.Event()
    save_groups = groups.save_configuration

    def paused_save(value):
        saved_accounts.set()
        assert release.wait(5)
        return save_groups(value)

    with patch.object(groups, "save_configuration", side_effect=paused_save):
        with ThreadPoolExecutor(max_workers=2) as pool:
            renamed = pool.submit(rename_account_and_groups, accounts, groups, account.key, "Renamed")
            try:
                assert saved_accounts.wait(5)
                backup = pool.submit(create_backup_bytes, paths)
                with pytest.raises(FutureTimeout):
                    backup.result(timeout=0.1)
            finally:
                release.set()
            renamed.result(timeout=5)
            raw, _ = backup.result(timeout=5)
    with zipfile.ZipFile(BytesIO(raw)) as archive:
        stored_accounts = json.loads(archive.read("config/accounts.json"))
        stored_groups = json.loads(archive.read("config/groups.json"))
    assert stored_accounts["accounts"][0]["label"] == "Renamed"
    assert stored_groups["groups"][0]["teams"][0]["account_label"] == "Renamed"


def test_integrity_repair_waits_for_configuration_owner(tmp_path):
    paths = paths_for(tmp_path / "hub")
    GroupStore(paths.groups_file).create_manager_game(GAME)
    preview = preview_integrity_repair(paths)
    started = threading.Event()

    def repair():
        started.set()
        return repair_integrity_index(paths, preview)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with exclusive_configuration_update(paths.config_dir):
            pending = pool.submit(repair)
            assert started.wait(5)
            with pytest.raises(FutureTimeout):
                pending.result(timeout=0.1)
        pending.result(timeout=5)
    assert paths.integrity_index_file.exists()


@pytest.mark.parametrize("command", ("players", "teams", "rules"))
def test_cli_data_mutations_reject_busy_root_before_source_calls(tmp_path, command, capsys):
    from cli.main import main
    from unittest.mock import Mock

    paths = paths_for(tmp_path / "hub")
    GroupStore(paths.groups_file).create_manager_game(GAME)
    player_scraper, service_factory, fetch_rules = Mock(), Mock(), Mock()
    arguments = ["--data-dir", str(paths.data_dir.parent)]
    arguments += {
        "players": ["players", GAME.original],
        "teams": ["teams", f"{GAME.original}/fantasyteams/101"],
        "rules": ["rules", GAME.slug, "--fetch", "--save"],
    }[command]
    with exclusive_data_operation(paths.data_dir), patch("cli.advisor.fetch_rule_page", fetch_rules):
        result = main(arguments, player_scraper=player_scraper, service_factory=service_factory)
    assert result == 1 and "Traceback" not in capsys.readouterr().err
    player_scraper.assert_not_called()
    service_factory.assert_not_called()
    fetch_rules.assert_not_called()


@pytest.mark.parametrize("command", ("players", "teams"))
def test_cli_lock_io_failure_is_reported_without_traceback(tmp_path, command, capsys):
    from cli.main import main
    from unittest.mock import Mock

    source = GAME.original if command == "players" else f"{GAME.original}/fantasyteams/101"
    player_scraper, service_factory = Mock(), Mock()
    with patch("cli.main.exclusive_data_operation", side_effect=PermissionError("synthetic lock failure")):
        result = main(
            ["--data-dir", str(tmp_path / "hub"), command, source],
            player_scraper=player_scraper, service_factory=service_factory,
        )
    captured = capsys.readouterr()
    assert result == 1 and "synthetic lock failure" in captured.err and "Traceback" not in captured.err
    player_scraper.assert_not_called()
    service_factory.assert_not_called()
