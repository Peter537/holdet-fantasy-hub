from codecs import BOM_UTF8
from dataclasses import replace
import hashlib
from io import BytesIO
import json
import zipfile

import pytest

import holdet_lib as holdet
from holdet_lib import backup, imports
from holdet_lib.archive_validation import portable_parts
from tests.test_decision_analysis import GAME
from tests.test_game_metadata_revisions import _metadata
from tests.test_import_planning import NOW, paths_for, snapshot_bytes, snapshot_package


def test_archive_components_follow_portable_windows_rules():
    assert portable_parts("data/ordinary-name/report.json") == (
        "data", "ordinary-name", "report.json"
    )
    for name in ("data/ordinary-name/report.json ", "data/Notes:Draft.json", r"data/current\notes.json"):
        with pytest.raises(holdet.PayloadError, match="Usikker sti"):
            portable_parts(name)


@pytest.mark.parametrize("members", [
    ("data/Summary.json", "data/summary.json"),
    ("data/notes", "data/notes/report.json"),
])
def test_archive_inventory_conflicts_fail_without_opening_members(tmp_path, monkeypatch, members):
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in (*members, "backup-manifest.json"):
            archive.writestr(name, "{}")
    paths = paths_for(tmp_path / "hub")
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("unchanged", encoding="utf-8")

    def unexpected_read(*args, **kwargs):
        pytest.fail("failed archive preflight opened a member")

    monkeypatch.setattr(zipfile.ZipFile, "open", unexpected_read)
    content = buffer.getvalue()
    assert not holdet.validate_backup(content).is_valid
    assert not holdet.preview_import(content, paths, filename="backup.zip").can_apply
    with pytest.raises(holdet.PayloadError, match="stikonflikt"):
        holdet.restore_backup(content, paths)
    assert sentinel.read_text(encoding="utf-8") == "unchanged"
    assert not paths.data_dir.parent.exists()


@pytest.mark.parametrize("layout", ["same", "nested", "exports_inside", "accounts_inside"])
def test_restore_rejects_overlapping_paths_before_locks(tmp_path, layout):
    source = paths_for(tmp_path / "source")
    content, _ = holdet.create_backup_bytes(source, now=NOW)
    paths = paths_for(tmp_path / "target")
    if layout == "same":
        paths = replace(paths, config_dir=paths.data_dir)
    elif layout == "nested":
        paths = replace(paths, config_dir=paths.data_dir / "config")
    elif layout == "exports_inside":
        paths = replace(paths, export_dir=paths.data_dir / "exports")
    else:
        paths = replace(paths, accounts_file=paths.data_dir / "accounts.json")
        with pytest.raises(holdet.PayloadError, match="Kontofilen"):
            holdet.create_backup_bytes(paths)
    with pytest.raises(holdet.PayloadError, match="gendannelse|gendannes"):
        holdet.restore_backup(content, paths)
    assert not paths.data_dir.parent.exists()


def test_backup_accepts_disjoint_custom_roots_and_legacy_manifest(tmp_path):
    paths = holdet.resolve_paths(
        overrides=holdet.PathOverrides(
            data_root=tmp_path / "base",
            config_dir=tmp_path / "custom-settings",
            export_dir=tmp_path / "custom-exports",
        ),
        environ={},
    )
    raw = BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr("backup-manifest.json", json.dumps({
            "schema_version": 1, "created_at": NOW.isoformat(),
            "total_bytes": 0, "files": [],
        }))
    result = holdet.restore_backup(raw.getvalue(), paths)
    assert result.rollback_path.is_relative_to(paths.backup_dir)
    assert result.rollback_path.is_file()
    assert paths.config_dir.is_dir() and paths.data_dir.is_dir()


@pytest.mark.parametrize("kind", ["backup", "snapshots"])
def test_archive_total_limit_stops_before_decompression(tmp_path, monkeypatch, kind):
    paths = paths_for(tmp_path / "hub")
    if kind == "backup":
        content, _ = holdet.create_backup_bytes(paths, now=NOW)
        with zipfile.ZipFile(BytesIO(content)) as archive:
            total = sum(info.file_size for info in archive.infolist())
        monkeypatch.setattr(backup, "MAX_BACKUP_TOTAL_BYTES", total - 1)
    else:
        content = snapshot_package((snapshot_bytes("players", NOW),))
        monkeypatch.setattr(imports, "MAX_ARCHIVE_TOTAL_BYTES", 10)

    def unexpected_read(*args, **kwargs):
        pytest.fail("over-budget archive opened a member")

    monkeypatch.setattr(zipfile.ZipFile, "open", unexpected_read)
    result = holdet.preview_import(content, paths, filename="input.zip")
    assert not result.can_apply
    assert any("udpakkede størrelse" in error for error in result.errors)


def test_import_stream_stops_at_input_budget(tmp_path, monkeypatch):
    class BoundedStream(BytesIO):
        def read(self, size=-1):
            assert 0 < size <= 9
            return super().read(size)

    source = BoundedStream(b"test input with extra bytes")
    monkeypatch.setattr(imports, "MAX_IMPORT_BYTES", 8)
    with pytest.raises(holdet.PayloadError, match="Importfilen er større"):
        holdet.preview_import(source, paths_for(tmp_path), filename="notes.txt")
    assert source.tell() == 9


@pytest.mark.parametrize("source_type", ["bytes", "path", "stream"])
def test_standalone_json_obeys_budget_before_decoding(tmp_path, monkeypatch, source_type):
    data = b'{"notes": "example"}'
    if source_type == "path":
        source = tmp_path / "notes.json"
        source.write_bytes(data)
    elif source_type == "stream":
        source = BytesIO(data)
    else:
        source = data
    monkeypatch.setattr(imports, "MAX_CANONICAL_JSON_BYTES", 8)

    def unexpected_preview(*args):
        pytest.fail("oversized content reached the standalone decoder")

    monkeypatch.setattr(imports, "_single_file_preview", unexpected_preview)
    with pytest.raises(holdet.PayloadError, match="Importfilen er større"):
        holdet.preview_import(source, paths_for(tmp_path), filename="notes.json")
    if source_type == "stream":
        assert source.tell() == 9


def test_json_extension_preserves_zip_magic_detection(tmp_path, monkeypatch):
    snapshot = snapshot_bytes("players", NOW)
    raw = snapshot_package((snapshot,))
    monkeypatch.setattr(imports, "MAX_CANONICAL_JSON_BYTES", len(snapshot))
    assert len(raw) > len(snapshot)

    class ShortRead(BytesIO):
        def read(self, size=-1):
            return super().read(min(size, 3))

    result = holdet.preview_import(ShortRead(raw), paths_for(tmp_path), filename="notes.json")
    assert result.kind == "canonical_snapshots" and result.can_apply, result.errors


@pytest.mark.parametrize(("condition", "message"), [
    ("duplicate", "duplikerede filstier"),
    ("missing", "mangler eller har forkert type"),
    ("unexpected", "matcher ikke ZIP-indholdet"),
    ("size", "Checksum eller størrelse matcher ikke"),
    ("invalid_size", "ugyldig filpost"),
    ("invalid_hash", "ugyldig filpost"),
    ("reserved_scope", "mangler eller har forkert type"),
    ("oversize", "snapshot er for stort"),
    ("manifest_limit", "Snapshotmanifestet er for stort"),
    ("schema", "Ukendt snapshotpakkeschema"),
])
def test_snapshot_inventory_is_complete_before_reading_any_body(tmp_path, monkeypatch, condition, message):
    raw = BytesIO()
    body = b"{}"
    entry = {"path": "snapshot.json", "size": len(body), "sha256": hashlib.sha256(body).hexdigest()}
    entries = [entry]
    version = 1
    if condition == "duplicate":
        entries.append(dict(entry))
    elif condition == "missing":
        entries.append(dict(entry, path="missing.json"))
    elif condition == "size":
        entries[0] = dict(entry, size=1)
    elif condition == "invalid_size":
        entries[0] = dict(entry, size=True)
    elif condition == "invalid_hash":
        entries[0] = dict(entry, sha256=["wrong type"])
    elif condition == "reserved_scope":
        entries.append(dict(entry, path="support-manifest.json"))
    elif condition == "oversize":
        monkeypatch.setattr(imports, "MAX_CANONICAL_JSON_BYTES", 1)
    elif condition == "manifest_limit":
        monkeypatch.setattr(imports, "MAX_MANIFEST_BYTES", 1)
    elif condition == "schema":
        version = True
    manifest = "snapshot-package-manifest.json"
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr(manifest, json.dumps({"schema_version": version, "files": entries}))
        archive.writestr("snapshot.json", body)
        if condition == "unexpected":
            archive.writestr("other.json", body)
        elif condition == "reserved_scope":
            archive.writestr("support-manifest.json", body)
    opened = []
    original_open = zipfile.ZipFile.open

    def track_open(archive, member, *args, **kwargs):
        opened.append(member.filename)
        return original_open(archive, member, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", track_open)
    result = holdet.preview_import(raw.getvalue(), paths_for(tmp_path), filename="snapshots.zip")
    assert not result.can_apply and not result.operations
    assert any(message in error for error in result.errors), result.errors
    assert opened == ([] if condition == "manifest_limit" else [manifest])


@pytest.mark.parametrize("version", [True, [], {}])
def test_backup_manifest_with_non_integer_schema_is_invalid(version):
    raw = BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr("backup-manifest.json", json.dumps({"schema_version": version}))
    result = holdet.validate_backup(raw.getvalue())
    assert not result.is_valid and "Ukendt skema for backupmanifest" in result.errors


def test_support_manifest_with_non_string_privacy_profile_is_invalid(tmp_path):
    raw = BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr("support-manifest.json", json.dumps({
            "schema_version": 1, "restorable": False, "privacy_profile": {}, "files": [],
        }))
    result = holdet.preview_import(raw.getvalue(), paths_for(tmp_path), filename="support.zip")
    assert not result.can_apply and any("ukendt eller gendanneligt" in error for error in result.errors)


def test_standalone_json_decoder_depth_failure_is_a_preview_error(tmp_path, monkeypatch):
    def depth_error(*args, **kwargs):
        raise RecursionError("decoder recursion limit")

    monkeypatch.setattr(imports.json, "loads", depth_error)
    result = holdet.preview_import(b"{}", paths_for(tmp_path), filename="notes.json")
    assert not result.can_apply and result.errors == ("Ugyldig JSON: decoder recursion limit",)


@pytest.mark.parametrize("kind", ["players", "teams"])
@pytest.mark.parametrize("packaged", [False, True])
def test_bom_snapshots_round_trip_unchanged(tmp_path, kind, packaged):
    paths = paths_for(tmp_path / "source")
    content = BOM_UTF8 + snapshot_bytes(kind, NOW)
    raw = snapshot_package((content,)) if packaged else content
    filename = "snapshots.zip" if packaged else "snapshot.json"
    first = holdet.preview_import(raw, paths, filename=filename)
    assert first.can_apply, first.errors
    holdet.apply_import(first, raw, paths)
    again = holdet.preview_import(raw, paths, filename=filename)
    assert again.skipped_count == 1
    archive = holdet.create_backup(paths, now=NOW)
    target = paths_for(tmp_path / "target")
    holdet.restore_backup(archive, target)
    store_class = holdet.PlayerStatisticsStore if kind == "players" else holdet.SnapshotStore
    stored, = store_class(target.snapshot_dir).scan().snapshots
    assert stored.path.read_bytes() == content


def test_bom_legacy_import_round_trips_through_backup_and_integrity(tmp_path):
    paths = paths_for(tmp_path / "source")
    content = BOM_UTF8 + b'{"notes":"historical JSON"}'
    preview = holdet.preview_import(content, paths, filename="legacy.json")
    assert preview.can_apply and preview.kind == "legacy_json"
    holdet.apply_import(preview, content, paths)
    relative = preview.operations[0].target.relative_to(paths.data_dir)
    assert not any(issue.code == "schema" for issue in holdet.full_integrity_check(paths).issues)
    archive = holdet.create_backup(paths, now=NOW)
    target = paths_for(tmp_path / "target")
    holdet.restore_backup(archive, target)
    assert (target.data_dir / relative).read_bytes() == content


@pytest.mark.parametrize(("kind", "filename", "store_type"), [
    ("players", "PLAYER-round3_0807_123001.JSON", holdet.PlayerStatisticsStore),
    ("teams", "Team-round3_0807_123001.Json", holdet.SnapshotStore),
])
def test_backup_integrity_and_store_share_legacy_snapshot_filename_recognition(
    tmp_path, kind, filename, store_type
):
    paths = paths_for(tmp_path / "source")
    snapshot = paths.snapshot_dir / "historical" / "backfill" / filename
    snapshot.parent.mkdir(parents=True)
    snapshot.write_bytes(BOM_UTF8 + snapshot_bytes(kind, NOW))
    store = store_type(paths.snapshot_dir)
    assert len(store.scan().snapshots) == 1
    assert holdet.validate_backup(holdet.create_backup_bytes(paths, now=NOW)[0]).is_valid

    snapshot.write_text(json.dumps({"generated_at": NOW.isoformat()}), encoding="utf-8")
    relative = f"data/{snapshot.relative_to(paths.data_dir).as_posix()}"
    assert store.scan().warnings
    assert any(error.startswith(relative) for error in holdet.validate_backup(holdet.create_backup_bytes(paths, now=NOW)[0]).errors)
    assert any(issue.code == "schema" and issue.path == relative for issue in holdet.full_integrity_check(paths).issues)


@pytest.mark.parametrize("kind", ["metadata", "metadata-revision", "fixtures"])
def test_backup_and_integrity_validate_cache_filename_identity(tmp_path, kind):
    paths = paths_for(tmp_path / "source")
    metadata = _metadata(fetched_at=NOW)
    if kind == "fixtures":
        store = holdet.FixtureStore(paths.fixture_dir)
        source = holdet.FixtureSourceProfile("https://example.test/fixtures", NOW.date(), True, True)
        original = store.save(holdet.FixtureSnapshot(metadata.game, (), source, NOW))
    else:
        store = holdet.GameMetadataStore(paths.game_metadata_dir)
        store.save(metadata)
        if kind == "metadata":
            original, = paths.game_metadata_dir.glob("*.json")
        else:
            original, = paths.game_metadata_dir.glob("revisions/*/*.json")
    assert holdet.validate_backup(holdet.create_backup_bytes(paths, now=NOW)[0]).is_valid
    if kind == "metadata-revision":
        renamed = original.parent.rename(original.parent.with_name("da--different")) / original.name
    else:
        renamed = original.rename(original.with_name("da--different.json"))
    expected_path = f"data/{renamed.relative_to(paths.data_dir).as_posix()}"
    content, _ = holdet.create_backup_bytes(paths, now=NOW)
    assert any(error.startswith(expected_path) and "andet spil" in error for error in holdet.validate_backup(content).errors)
    assert any(issue.code == "schema" and issue.path == expected_path for issue in holdet.full_integrity_check(paths).issues)


def test_descriptive_legacy_metadata_filename_remains_valid(tmp_path):
    paths = paths_for(tmp_path / "source")
    store = holdet.GameMetadataStore(paths.game_metadata_dir)
    store.save(_metadata(fetched_at=NOW))
    original, = paths.game_metadata_dir.glob("*.json")
    original.rename(original.with_name("historical-metadata.json"))
    assert len(store.scan()[0]) == 1
    assert holdet.validate_backup(holdet.create_backup_bytes(paths, now=NOW)[0]).is_valid
    assert not any(issue.code == "schema" for issue in holdet.full_integrity_check(paths).issues)


@pytest.mark.parametrize(("filename", "payload"), [
    ("groups.json", {"schema_version": 8}),
    ("seasons.json", {"schema_version": 99}),
    ("seasons.json", {"schema_version": 1, "seasons": "wrong type"}),
])
def test_backup_and_integrity_reject_malformed_runtime_documents(tmp_path, filename, payload):
    paths = paths_for(tmp_path / "source")
    paths.config_dir.mkdir(parents=True)
    (paths.config_dir / filename).write_text(json.dumps(payload), encoding="utf-8")
    content, _ = holdet.create_backup_bytes(paths, now=NOW)
    errors = holdet.validate_backup(content).errors
    assert any(error.startswith(f"config/{filename}:") for error in errors)
    issues = holdet.full_integrity_check(paths).issues
    assert any(issue.code == "schema" and issue.path == f"config/{filename}" for issue in issues)
    with pytest.raises(holdet.PayloadError, match="backup bestod"):
        holdet.create_backup(paths)


def test_backup_validation_checks_referenced_group_revisions(tmp_path):
    paths = paths_for(tmp_path / "source")
    store = holdet.GroupStore(paths.groups_file, paths.group_revision_dir)
    teams = tuple(
        holdet.GroupTeam(identifier, f"Team {identifier}", f"{GAME.original}/fantasyteams/{identifier}")
        for identifier in (101, 202, 303, 404)
    )
    store.create_tournament(
        "Cup", GAME, teams, start_round=1, final_round=5, rounds_per_tie=1,
        group_id="cup", template="swiss", definition_options={
            "seed_rule": "manual", "seed_order": (101, 202, 303, 404), "swiss_rounds": 2,
        },
    )
    store.rebuild_tournament("cup", teams[:2], final_round=5, template="league")
    valid = holdet.create_backup(paths, now=NOW)
    target = paths_for(tmp_path / "target")
    holdet.restore_backup(valid, target)
    assert len(holdet.GroupStore(target.groups_file, target.group_revision_dir).load()[0].archived_revisions) == 1
    (paths.group_revision_dir / "cup" / "revision-1.json").unlink()
    incomplete, _ = holdet.create_backup_bytes(paths, now=NOW)
    assert any("Turneringsrevisionen mangler" in error for error in holdet.validate_backup(incomplete).errors)
    with pytest.raises(holdet.PayloadError, match="Turneringsrevisionen mangler"):
        holdet.restore_backup(incomplete, target)
