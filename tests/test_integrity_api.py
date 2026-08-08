from __future__ import annotations

import json
import hashlib
import os
import time
from pathlib import Path

from starlette.testclient import TestClient

import holdet_lib as holdet
from holdet_lib.local_api import LocalDataApi
import website.server as server


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


def test_cleanup_keeps_newest_backup_and_retention_cannot_be_forged(tmp_path: Path) -> None:
    paths = paths_for(tmp_path / "hub")
    older_backup = paths.backup_dir / "older.zip"
    newest_backup = paths.backup_dir / "newest.zip"
    older_backup.parent.mkdir(parents=True)
    older_backup.write_bytes(b"old")
    newest_backup.write_bytes(b"new")
    current = time.time()
    os.utime(older_backup, (current - 10, current - 10))
    os.utime(newest_backup, (current, current))
    candidates = holdet.list_cleanup_candidates(paths)
    assert older_backup.resolve() in {item.path for item in candidates}
    assert newest_backup.resolve() not in {item.path for item in candidates}
    try:
        holdet.delete_derived_files(paths, (newest_backup,))
    except ValueError:
        pass
    else:
        raise AssertionError("Den nyeste backup måtte ikke kunne slettes")

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
