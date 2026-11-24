from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import hashlib
import os
from threading import Barrier, Event
import time
from pathlib import Path
import zipfile

import httpx2
import pytest
from starlette.testclient import TestClient

import holdet_lib as holdet
from holdet_lib.local_api import LocalDataApi
import website.server as server
from tests.test_player_statistics import sample_statistics


def paths_for(root: Path) -> holdet.AppPaths:
    return holdet.resolve_paths(
        overrides=holdet.PathOverrides(data_root=root),
        environ={},
    )


def test_integrity_repair_is_deterministic_and_never_changes_canonical_data(tmp_path: Path) -> None:
    paths = paths_for(tmp_path / "hub")
    source = paths.import_dir / "archive-only" / "notes.txt"
    source.parent.mkdir(parents=True)
    source.write_text("før", encoding="utf-8")

    quick = holdet.quick_integrity_check(paths)
    assert not quick.is_clean
    assert {issue.code for issue in quick.issues} >= {"index_missing", "extra"}

    preview = holdet.preview_integrity_repair(paths)
    before = source.read_bytes()
    repaired = holdet.repair_integrity_index(paths, preview)
    assert repaired == paths.integrity_index_file.resolve()
    assert source.read_bytes() == before
    assert holdet.quick_integrity_check(paths).is_clean
    assert holdet.preview_integrity_repair(paths).content == preview.content

    source.write_text("efter", encoding="utf-8")
    full = holdet.full_integrity_check(paths)
    assert not full.is_clean
    assert any(issue.code == "checksum" for issue in full.issues)
    assert source.read_text(encoding="utf-8") == "efter"


def test_storage_inventory_and_cleanup_are_scoped(tmp_path: Path) -> None:
    paths = paths_for(tmp_path / "hub")
    export = paths.player_export_dir / "game-a" / "players.csv"
    export.parent.mkdir(parents=True)
    export.write_bytes(b"12345")
    canonical = paths.config_dir / "groups.json"
    canonical.parent.mkdir(parents=True)
    canonical.write_text('{"schema_version":8,"games":[],"groups":[]}', encoding="utf-8")

    inventory = holdet.build_storage_inventory(paths)
    usage = next(row for row in inventory.rows if row.game_scope == "game-a")
    assert (usage.category, usage.files, usage.bytes) == ("Afledte eksporter", 1, 5)
    candidates = holdet.list_cleanup_candidates(paths)
    assert [item.path for item in candidates] == [export]
    try:
        holdet.delete_derived_files(paths, (canonical,))
    except ValueError:
        pass
    else:
        raise AssertionError("Kanonisk konfiguration måtte ikke kunne slettes")
    assert canonical.exists()
    assert holdet.delete_derived_files(paths, (export,)) == (export.resolve(),)


@pytest.mark.parametrize(
    ("relative", "game", "category"),
    [
        ("data/fixtures/da--demo.json", "demo", "Fixturecache"),
        (
            "data/refresh-runs/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa/"
            "player-bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb.json",
            "Fælles",
            "Opdateringsjournaler",
        ),
    ],
)
def test_isolated_fixture_or_player_stage_is_in_storage_inventory(
    tmp_path: Path, relative: str, game: str, category: str,
) -> None:
    paths = paths_for(tmp_path)
    artifact = tmp_path / relative
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"seven!!")
    assert holdet.build_storage_inventory(paths) == holdet.StorageInventory(
        (holdet.StorageUsage(game, category, 1, 7),), 1, 7,
    )


def test_inventory_counts_owned_files_once_and_excludes_working_files(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    paths = replace(
        paths,
        accounts_file=tmp_path / "external-accounts.json",
        team_export_dir=paths.player_export_dir,
        report_dir=paths.config_dir,
        archive_dir=paths.refresh_run_dir,
        artifact_registry_file=paths.player_export_dir / "artifact-registry.json",
    )
    owned = {
        paths.accounts_file: b"accounts",
        paths.seasons_file: b"season",
        paths.refresh_run_dir / "run" / "run.json": b"journal",
        paths.game_metadata_dir / "da--demo.json": b"metadata",
        paths.game_metadata_dir / "revisions" / "da--demo" / "revision.json": b"revision",
        paths.hall_of_fame_dir / "events.json": b"events",
        paths.player_export_dir / "demo" / "players.csv": b"players",
        paths.integrity_index_file: b"integrity",
        paths.artifact_registry_file: b"registry",
    }
    working = (
        paths.refresh_run_dir / "run" / "cancel.request",
        paths.config_dir / ".team-strategies.json.lock",
        paths.config_dir / ".holdet-restore-test" / "config" / "old.json",
        paths.player_export_dir / "demo" / ".players.tmp",
    )
    for path, content in (*owned.items(), *((path, b"working") for path in working)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    inventory = holdet.build_storage_inventory(paths)
    assert inventory.total_files == len(owned)
    assert inventory.total_bytes == sum(map(len, owned.values()))
    rows = {(row.game_scope, row.category): row for row in inventory.rows}
    assert rows["demo", "Spilmetadata"].files == 2
    assert rows["Fælles", "Konfiguration"].files == 2
    assert rows["Fælles", "Afledt integritetsindeks"].files == 1
    assert rows["Fælles", "Afledt eksportregister"].files == 1


def test_cleanup_keeps_newest_usable_backup_alongside_unknown_and_damaged_files(tmp_path: Path) -> None:
    paths = paths_for(tmp_path / "hub")
    oldest = datetime(2026, 7, 1, tzinfo=timezone.utc)
    newest = datetime(2026, 7, 2, tzinfo=timezone.utc)
    older_backup = holdet.create_backup(paths, paths.backup_dir / "older.zip", now=oldest)
    newest_backup = holdet.create_backup(paths, paths.backup_dir / "rollback-newest.zip", now=newest)
    unknown = paths.backup_dir / "unknown.zip"
    with zipfile.ZipFile(unknown, "w") as bundle:
        bundle.writestr("example.txt", "other archive")
    partial = paths.backup_dir / "incoming.zip.partial"
    partial.write_bytes(newest_backup.read_bytes())
    corrupt = paths.backup_dir / "corrupt.zip"
    corrupt.write_bytes(b"incomplete zip")
    paths.config_dir.mkdir(exist_ok=True)
    paths.hub_settings_file.write_text('{"schema_version":999}', encoding="utf-8")
    damaged_state, _ = holdet.create_backup_bytes(paths)
    emergency = paths.backup_dir / "rollback-damaged.zip"
    emergency.write_bytes(damaged_state)
    current = time.time()
    os.utime(newest_backup, (current - 10, current - 10))
    for path in (older_backup, unknown, partial, corrupt, emergency):
        os.utime(path, (current, current))

    candidates = holdet.list_cleanup_candidates(paths)
    assert [candidate.path for candidate in candidates] == [older_backup.resolve()]
    for path in (newest_backup, unknown, partial, corrupt, emergency):
        with pytest.raises(ValueError, match="må ikke slettes"):
            holdet.delete_derived_files(paths, (path,))
        assert path.is_file()

    assert holdet.delete_derived_files(paths, (older_backup,)) == (older_backup.resolve(),)
    assert newest_backup.is_file()


def test_cleanup_revalidates_last_usable_backup_after_preview(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    older = holdet.create_backup(
        paths, paths.backup_dir / "older.zip", now=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )
    newer = holdet.create_backup(
        paths, paths.backup_dir / "newer.zip", now=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    assert [item.path for item in holdet.list_cleanup_candidates(paths)] == [older.resolve()]
    newer.write_bytes(b"damaged after preview")
    with pytest.raises(ValueError, match="må ikke slettes"):
        holdet.delete_derived_files(paths, (older,))
    assert holdet.validate_backup(older).is_valid


def test_retention_candidate_cannot_be_forged(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    newest_snapshot = paths.snapshot_dir / "game" / "players" / "newest.json"
    newest_snapshot.parent.mkdir(parents=True)
    newest_snapshot.write_bytes(b"canonical")
    forged = holdet.RetentionCandidate(
        newest_snapshot,
        newest_snapshot.relative_to(paths.data_dir).as_posix(),
        "game",
        "players",
        1,
        newest_snapshot.stat().st_size,
        hashlib.sha256(newest_snapshot.read_bytes()).hexdigest(),
        newest_snapshot,
    )
    try:
        holdet.archive_retention_candidates(paths, (forged,))
    except ValueError:
        pass
    else:
        raise AssertionError("Et snapshot uden for retentionpreviewet måtte ikke kunne arkiveres")
    assert newest_snapshot.exists()


def test_backup_target_cannot_be_inside_canonical_tree(tmp_path: Path) -> None:
    paths = paths_for(tmp_path / "hub")
    target = paths.data_dir / "unsafe.zip"
    try:
        holdet.create_backup(paths, target)
    except ValueError:
        pass
    else:
        raise AssertionError("Backup i det kanoniske træ måtte ikke accepteres")
    assert not target.exists()


def test_backup_schema_two_and_path_restore(tmp_path: Path) -> None:
    source = paths_for(tmp_path / "source")
    source.config_dir.mkdir(parents=True)
    source.groups_file.write_text(
        '{"schema_version":8,"games":[],"groups":[]}',
        encoding="utf-8",
    )
    backup = holdet.create_backup(source)
    validation = holdet.validate_backup(backup)
    assert validation.is_valid
    assert validation.manifest is not None
    assert validation.manifest.schema_version == 2

    target = paths_for(tmp_path / "target")
    result = holdet.restore_backup(backup, target)
    assert result.restored_files == 1
    assert json.loads(target.groups_file.read_text(encoding="utf-8"))["schema_version"] == 8


def test_registered_download_rejects_same_size_tampering(tmp_path: Path) -> None:
    paths = paths_for(tmp_path / "hub")
    artifact = paths.report_dir / "game" / "report.html"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"safe")
    registered = holdet.register_artifact(paths, artifact)
    assert holdet.resolve_registered_artifact(paths, registered.artifact_id) == artifact.resolve()
    artifact.write_bytes(b"evil")
    assert holdet.resolve_registered_artifact(paths, registered.artifact_id) is None


def test_concurrent_artifact_registrations_keep_every_export(tmp_path: Path) -> None:
    paths = paths_for(tmp_path)
    paths.report_dir.mkdir(parents=True)
    artifacts = [paths.report_dir / f"report-{index}.html" for index in range(8)]
    for artifact in artifacts:
        artifact.write_text(artifact.name, encoding="utf-8")
    start = Barrier(len(artifacts))

    def register(artifact):
        start.wait(timeout=5)
        return holdet.register_artifact(paths, artifact)

    with ThreadPoolExecutor(max_workers=len(artifacts)) as executor:
        records = list(executor.map(register, artifacts))

    assert holdet.register_artifact(paths, artifacts[0]) == records[0]
    assert len({record.artifact_id for record in records}) == len(artifacts)
    for artifact, record in zip(artifacts, records, strict=True):
        assert holdet.resolve_registered_artifact(paths, record.artifact_id) == artifact.resolve()
    registry = json.loads(paths.artifact_registry_file.read_text(encoding="utf-8"))
    assert len(registry["artifacts"]) == len(artifacts)


def test_local_api_catalog_filters_and_security_headers(tmp_path: Path, monkeypatch) -> None:
    paths = paths_for(tmp_path / "hub")
    paths.config_dir.mkdir(parents=True)
    paths.groups_file.write_text(
        json.dumps({"schema_version": 8, "games": [], "groups": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(server, "APP_PATHS", paths)
    monkeypatch.setattr(server, "DATA_API", LocalDataApi(paths))

    client = TestClient(server.app, base_url="http://127.0.0.1:8501", client=("127.0.0.1", 41000))
    health = client.get("/api/v1/health")
    assert health.status_code == 200
    assert health.json()["mode"] == "read-only"
    assert health.headers["x-content-type-options"] == "nosniff"
    assert "access-control-allow-origin" not in health.headers

    catalog = client.get("/api/v1/catalog").json()
    assert {item["name"] for item in catalog["datasets"]} == set(holdet.LOCAL_API_DATASETS)
    openapi = client.get("/api/v1/openapi.json").json()
    assert openapi["servers"] == [{"url": "/"}]
    invalid = client.get("/api/v1/data/players")
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "invalid_query"

    data = client.get("/api/v1/data/games?limit=1&format=json")
    assert data.status_code == 200
    assert data.json()["rows"] == []
    assert data.headers["etag"]
    unchanged = client.get(
        "/api/v1/data/games?limit=1&format=json",
        headers={"If-None-Match": data.headers["etag"]},
    )
    assert unchanged.status_code == 304
    head = client.head("/api/v1/data/games?limit=1&format=csv")
    assert head.status_code == 200
    assert head.content == b""
    assert int(head.headers["content-length"]) > 0

    blocked = TestClient(server.app, base_url="http://evil.example", client=("203.0.113.5", 41000))
    denied = blocked.get("/api/v1/health", headers={"Host": "evil.example"})
    assert denied.status_code == 403


def test_api_numeric_filters_are_validated_with_or_without_rows(tmp_path: Path, monkeypatch) -> None:
    from tests.test_decision_analysis import summary, team_snapshot

    paths = paths_for(tmp_path)
    monkeypatch.setattr(server, "APP_PATHS", paths)
    monkeypatch.setattr(server, "DATA_API", LocalDataApi(paths))
    client = TestClient(server.app, base_url="http://127.0.0.1:8501", client=("127.0.0.1", 41000))

    def assert_invalid_queries(expected_rows):
        for dataset, query, valid in (("rounds", "round", 7), ("teams", "team_id", 123)):
            url = f"/api/v1/data/{dataset}"
            assert client.get(url, params={query: valid}).json()["total"] == expected_rows
            get = client.get(url, params={query: "invalid"})
            head = client.head(url, params={query: "invalid"})
            assert get.status_code == head.status_code == 400
            assert get.json()["error"] == {
                "code": "invalid_query", "message": f"Filteret {query} skal være et heltal",
            }
            assert head.headers["content-length"] == get.headers["content-length"]
            assert head.content == b""

    try:
        assert_invalid_queries(0)
        holdet.PlayerStatisticsStore(paths.snapshot_dir).save(sample_statistics(7))
        team = team_snapshot(123, 3, (), (summary(3, 100),))
        holdet.SnapshotStore(paths.snapshot_dir).save_team_json(team.team)
        assert_invalid_queries(1)
    finally:
        client.close()


@pytest.mark.parametrize("output_format", ["json", "csv"])
def test_api_if_none_match_accepts_weak_tags_lists_and_wildcard(
    output_format: str, tmp_path: Path, monkeypatch
) -> None:
    paths = paths_for(tmp_path)
    monkeypatch.setattr(server, "APP_PATHS", paths)
    monkeypatch.setattr(server, "DATA_API", LocalDataApi(paths))
    client = TestClient(server.app, base_url="http://127.0.0.1:8501", client=("127.0.0.1", 41000))
    try:
        url = f"/api/v1/data/games?format={output_format}"
        original = client.get(url)
        assert original.status_code == 200
        tag = original.headers["etag"]
        other_format = "csv" if output_format == "json" else "json"
        other_tag = client.get(f"/api/v1/data/games?format={other_format}").headers["etag"]
        for request in (client.get, client.head):
            for value in (tag, f"W/{tag}", f'"tag,with,commas", W/{tag}', "*"):
                response = request(url, headers={"if-none-match": value})
                assert response.status_code == 304, value
                assert response.headers["etag"] == tag
                assert response.content == b""
            repeated = request(url, headers=[("if-none-match", '"absent"'), ("if-none-match", f"W/{tag}")])
            assert repeated.status_code == 304
            for value in (f"W/{other_tag}", f'"absent", {other_tag}', f"{tag}, mismatch"):
                response = request(url, headers={"if-none-match": value})
                assert response.status_code == 200, value
                assert response.headers["etag"] == tag
                assert response.headers["content-length"] == original.headers["content-length"]
        assert client.head(url).content == b""
    finally:
        client.close()


@pytest.mark.parametrize("work", ["data", "download"])
def test_api_health_remains_responsive_while_worker_is_busy(
    work: str, tmp_path: Path, monkeypatch
) -> None:
    paths = paths_for(tmp_path)
    api = LocalDataApi(paths)
    monkeypatch.setattr(server, "APP_PATHS", paths)
    monkeypatch.setattr(server, "DATA_API", api)
    entered, release = Event(), Event()

    def block_until_released(function):
        def blocking_call(*args):
            entered.set()
            assert release.wait(timeout=6)
            return function(*args)

        return blocking_call

    if work == "data":
        statistics = sample_statistics()
        holdet.PlayerStatisticsStore(paths.snapshot_dir).save(statistics)
        url = f"/api/v1/data/players?game={statistics.game.slug}"
        monkeypatch.setattr(api, "rows", block_until_released(api.rows))
    else:
        artifact = paths.report_dir / "test-report.html"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("Report", encoding="utf-8")
        identifier = holdet.register_artifact(paths, artifact).artifact_id
        url = f"/downloads/{identifier}"
        monkeypatch.setattr(
            server, "resolve_registered_artifact",
            block_until_released(server.resolve_registered_artifact),
        )

    async def check_concurrent_requests():
        transport = httpx2.ASGITransport(app=server.app, client=("127.0.0.1", 41000))
        async with httpx2.AsyncClient(transport=transport, base_url="http://127.0.0.1:8501") as client:
            busy = asyncio.create_task(client.get(url))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                health = await asyncio.wait_for(client.get("/api/v1/health"), timeout=2)
                assert health.status_code == 200
                assert not busy.done()
            finally:
                release.set()
                response = await asyncio.wait_for(busy, timeout=6)
            assert response.status_code == 200

    asyncio.run(check_concurrent_requests())


def test_local_api_delays_data_access_while_restore_is_pending(tmp_path: Path, monkeypatch) -> None:
    from holdet_lib.persistence import restore_journal_path

    paths = paths_for(tmp_path / "hub")
    paths.config_dir.parent.mkdir(parents=True)
    marker = restore_journal_path(paths.config_dir)
    marker.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(server, "APP_PATHS", paths)
    monkeypatch.setattr(server, "DATA_API", LocalDataApi(paths))
    client = TestClient(server.app, base_url="http://127.0.0.1:8501", client=("127.0.0.1", 41000))
    assert client.get("/api/v1/health").status_code == 200
    response = client.get("/api/v1/data/games")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "restore_pending"
    assert marker.read_text(encoding="utf-8") == "{}"


def test_local_api_discards_response_if_restore_completes_during_read(tmp_path: Path, monkeypatch) -> None:
    paths = paths_for(tmp_path / "hub")
    api = LocalDataApi(paths)
    config = holdet.GroupStore(paths.groups_file, paths.group_revision_dir)
    game = sample_statistics().game
    config.create_manager_game(game, "Restored game")
    backup, _ = holdet.create_backup_bytes(paths)
    config.rename_manager_game(game, "Before restore")
    monkeypatch.setattr(server, "APP_PATHS", paths)
    monkeypatch.setattr(server, "DATA_API", api)
    original_rows = api.rows
    entered, release = Event(), Event()

    def captured_rows(*args):
        rows = original_rows(*args)
        entered.set()
        assert release.wait(15)
        return rows

    monkeypatch.setattr(api, "rows", captured_rows)
    client = TestClient(server.app, base_url="http://127.0.0.1:8501", client=("127.0.0.1", 41000))
    with ThreadPoolExecutor(max_workers=1) as executor:
        response = executor.submit(client.get, "/api/v1/data/games")
        try:
            assert entered.wait(10)
            holdet.restore_backup(backup, paths)
        finally:
            release.set()
        assert response.result(timeout=10).status_code == 503
    rows = client.get("/api/v1/data/games").json()["rows"]
    assert rows[0]["name"] == "Restored game"


def test_api_round_status_uses_latest_capture_from_each_source(tmp_path: Path, monkeypatch) -> None:
    from tests.test_decision_analysis import entry, player_snapshot, summary, team_snapshot

    live_players = player_snapshot(3, (entry(1, 1),), status="in_progress")
    complete_players = player_snapshot(3, (entry(1, 2),), offset=2)
    complete_players = replace(complete_players, statistics=replace(
        complete_players.statistics, game=replace(complete_players.statistics.game, locale="DA"),
    ))
    completed = summary(3, 100)
    old_team = replace(
        team_snapshot(101, 3, (), (replace(completed, round_status="in_progress"),)),
        generated_at=live_players.generated_at,
    )
    new_team = replace(old_team, generated_at=complete_players.generated_at,
                       team=replace(old_team.team, history=(completed,)))
    players = holdet.PlayerStatisticsIndex((live_players, complete_players))
    api = LocalDataApi(paths_for(tmp_path))
    monkeypatch.setattr(api, "_snapshot_indexes", lambda: (players, holdet.SnapshotIndex((new_team, old_team))))
    row, = api.rows("rounds", {})
    assert (row["locale"], row["status"], row["player_snapshots"], row["team_snapshots"]) == ("da", "complete", 2, 2)
    assert row["latest_at"] == new_team.generated_at.isoformat()

    other_team = team_snapshot(102, 3, (), (replace(completed, round_status="in_progress"),))
    monkeypatch.setattr(api, "_snapshot_indexes", lambda: (players, holdet.SnapshotIndex((new_team, old_team, other_team))))
    row, = api.rows("rounds", {})
    assert row["status"] == "in_progress"


def test_local_players_api_exposes_optional_public_fields_read_only(
    tmp_path: Path, monkeypatch
) -> None:
    paths = paths_for(tmp_path / "hub")
    paths.config_dir.mkdir(parents=True)
    paths.groups_file.write_text(
        json.dumps({"schema_version": 8, "games": [], "groups": []}),
        encoding="utf-8",
    )
    statistics = sample_statistics(variant="soccer")
    enriched = replace(
        statistics,
        entries=(
            replace(
                statistics.entries[0],
                popularity=0.25,
                popularity_change=0.05,
                trend=2,
                index=91,
            ),
        ),
    )
    holdet.PlayerStatisticsStore(paths.snapshot_dir).save(enriched)
    monkeypatch.setattr(server, "APP_PATHS", paths)
    monkeypatch.setattr(server, "DATA_API", LocalDataApi(paths))
    client = TestClient(
        server.app,
        base_url="http://127.0.0.1:8501",
        client=("127.0.0.1", 41000),
    )

    response = client.get(
        f"/api/v1/data/players?game={statistics.game.slug}&format=json"
    )

    assert response.status_code == 200
    row = response.json()["rows"][0]
    assert (
        row["popularity"],
        row["popularity_change"],
        row["trend"],
        row["index"],
    ) == (0.25, 0.05, 2.0, 91.0)
    assert "computed_columns" not in row


def test_players_api_scans_requested_game_and_keeps_locale_and_round_contracts(
    tmp_path: Path, monkeypatch
) -> None:
    paths = paths_for(tmp_path)
    store = holdet.PlayerStatisticsStore(paths.snapshot_dir)
    statistics = sample_statistics(0, variant="soccer")
    statistics = replace(
        statistics,
        game=holdet.GameUrl("https://www.holdet.dk/da/fantasy/sample-league", "da", "sample-league"),
    )
    at = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
    store.save(statistics, now=at)
    newest = replace(statistics, entries=(replace(statistics.entries[0], name="Current"),))
    store.save(newest, now=at + timedelta(hours=1))
    store.save(replace(statistics, round_number=1), now=at)
    other = replace(statistics.game, slug="different-game")
    other_path = store.save(replace(statistics, game=other), now=at)

    english = replace(statistics.game, locale="en")
    legacy = store.save(replace(statistics, game=english), now=at)
    legacy_payload = json.loads(legacy.read_text(encoding="utf-8"))
    legacy_payload["schema_version"] = 1
    legacy.write_text(json.dumps(legacy_payload), encoding="utf-8")

    read_text = Path.read_text

    def scoped_read(path, *args, **kwargs):
        assert path != other_path, "The request opened an unrelated game snapshot"
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", scoped_read)
    api = LocalDataApi(paths)
    filters = {"game": statistics.game.slug.upper(), "round": "0"}
    rows = api.rows("players", filters)
    assert [(row["locale"], row["round"], row["name"]) for row in rows] == [
        ("da", 0, "Current"), ("en", 0, statistics.entries[0].name),
    ]
    assert api.rows("players", {**filters, "locale": "DA"}) == rows[:1]


def test_players_api_keeps_selected_bucket_in_configured_store(tmp_path: Path, monkeypatch) -> None:
    paths = paths_for(tmp_path)
    outside = holdet.PlayerStatisticsStore(tmp_path / "other-store")
    statistics = sample_statistics()
    bucket = outside.save(statistics).parent
    monkeypatch.setattr(holdet.PlayerStatisticsStore, "_game_directory", lambda _self, _game: bucket)

    assert LocalDataApi(paths).rows("players", {"game": statistics.game.slug}) == ()
