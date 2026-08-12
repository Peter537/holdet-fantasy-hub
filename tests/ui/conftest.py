"""Deterministic local Streamlit server used by the opt-in Playwright suite."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

import pytest

import holdet_lib as holdet
from tests.test_library_storage import sample_team
from tests.test_player_statistics import sample_statistics


PROJECT_ROOT = Path(__file__).parents[2]
FIXED_TIME = datetime(2026, 8, 7, 12, tzinfo=timezone.utc)


@dataclass(frozen=True)
class UiServer:
    base_url: str
    data_root: Path


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _seed_data(root: Path) -> None:
    paths = holdet.resolve_paths(
        overrides=holdet.PathOverrides(data_root=root),
        environ={},
    )
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    paths.accounts_file.write_text('{"accounts":[]}\n', encoding="utf-8")
    paths.groups_file.write_text(
        json.dumps({"schema_version": 1, "groups": []}) + "\n",
        encoding="utf-8",
    )

    store = holdet.GroupStore(paths.groups_file, paths.group_revision_dir)
    game = sample_statistics(round_number=3).game
    store.create_manager_game(game, "Tourspillet 2026")
    store.create_manager_game("super-manager-fall-2026", "Super Manager")
    store.create_manager_game("golf-manager-2026", "Golf Manager")

    teams = tuple(
        sample_team(
            701 + index,
            name=f"Nordlys {index + 1}",
            current_round=3,
            total=360_000_000 - index * 7_500_000,
            change=3_000_000 - index * 125_000,
            history_rounds=(3, 2, 1),
        )
        for index in range(8)
    )
    members = tuple(
        holdet.GroupTeam(
            team.reference.team_id,
            team.team_name,
            team.reference.source_url,
            team.reference.account_key,
            team.reference.account_label,
            team.reference.account_user_id,
            team.reference.profile_url,
        )
        for team in teams
    )
    store.create("UI Testliga", game, members, group_id="ui-testliga")

    team_store = holdet.SnapshotStore(paths.snapshot_dir)
    for index, team in enumerate(teams):
        team_store.save_team_json(
            team,
            now=FIXED_TIME.replace(minute=index),
        )

    base = sample_statistics(round_number=3)
    base_entry = base.entries[0]
    entries = tuple(
        replace(
            base_entry,
            source_index=index,
            name=f"Spiller {index + 1:02d}",
            team=f"Team {(index % 8) + 1}",
            position=("Rytter", "Kaptajn", "Sprinter")[index % 3],
            value=7_000_000 + index * 175_000,
            is_active=index % 11 != 0,
            is_disabled=index % 13 == 0,
            is_injured=index % 17 == 0,
            has_suspension=index % 19 == 0,
            entry_id=50_000 + index,
            person_id=60_000 + index,
            total_growth=500_000 - index * 8_000,
            round_growth=75_000 - index * 2_000,
            popularity=float((index * 3) % 45),
            popularity_change=float((index % 7) - 3),
            trend=float((index % 9) - 4),
            index=float(40 + index),
        )
        for index in range(60)
    )
    player_store = holdet.PlayerStatisticsStore(paths.snapshot_dir)
    for round_number in (1, 2, 3):
        round_entries = tuple(
            replace(
                entry,
                value=entry.value - (3 - round_number) * 100_000,
                round_growth=(entry.round_growth or 0) - (3 - round_number) * 5_000,
            )
            for entry in entries
        )
        player_store.save(
            replace(
                base,
                round_number=round_number,
                entries=round_entries,
                round_status="complete",
            ),
            now=FIXED_TIME.replace(hour=9 + round_number),
        )
    # Preserve a second immutable fetch in the same round for intra-round UI.
    player_store.save(
        replace(
            base,
            round_number=3,
            entries=tuple(
                replace(entry, value=entry.value + 25_000)
                for entry in entries
            ),
            round_status="complete",
        ),
        now=FIXED_TIME.replace(hour=12, minute=30),
    )
    watched = holdet.watchlist_entry(game, entries[0])
    watched = replace(
        watched,
        reasons=("kaptajnkandidat",),
        reason_note="Følg næste prisbevægelse",
    )
    holdet.HubSettingsStore(paths.hub_settings_file).save(
        holdet.HubSettings(
            watchlist=(watched,),
            player_annotations=(
                holdet.PlayerAnnotation(
                    game.locale,
                    game.slug,
                    watched.player_key,
                    "Stærk kandidat til næste runde",
                    ("overvej", "kaptajn"),
                    FIXED_TIME,
                ),
            ),
        )
    )


@pytest.fixture(scope="session")
def ui_server(tmp_path_factory: pytest.TempPathFactory) -> UiServer:
    root = tmp_path_factory.mktemp("holdet-ui-data")
    _seed_data(root)
    port = _free_loopback_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = root / "streamlit-ui.log"
    environment = {
        **os.environ,
        "HOLDET_DATA_DIR": str(root),
        "PYTHONUTF8": "1",
        "STREAMLIT_BROWSER_GATHER_USAGE_STATS": "false",
        "STREAMLIT_SERVER_FILE_WATCHER_TYPE": "none",
    }
    command = (
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(PROJECT_ROOT / "website" / "server.py"),
        "--server.address",
        "127.0.0.1",
        "--server.port",
        str(port),
        "--server.headless",
        "true",
    )
    with log_path.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        deadline = time.monotonic() + 35
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                with urlopen(f"{base_url}/api/v1/health", timeout=1) as response:
                    if response.status == 200:
                        last_error = None
                        break
            except (OSError, URLError) as exc:
                last_error = exc
                time.sleep(0.2)
        else:
            process.terminate()
            process.wait(timeout=10)
            pytest.fail(f"UI-serveren startede ikke: {last_error}\n{log_path.read_text(encoding='utf-8')}")
        if process.poll() is not None:
            pytest.fail(
                "UI-serveren stoppede under opstart:\n"
                + log_path.read_text(encoding="utf-8")
            )
        try:
            yield UiServer(base_url, root)
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, object]) -> dict[str, object]:
    return {
        **browser_context_args,
        "reduced_motion": "reduce",
        "locale": "da-DK",
        "timezone_id": "Europe/Copenhagen",
        "color_scheme": "dark",
    }
