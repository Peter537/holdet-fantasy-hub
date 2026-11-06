from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import zipfile

import pytest

import holdet_lib as holdet
import holdet_lib.imports as imports
from tests.test_library_storage import sample_team
from tests.test_player_statistics import sample_statistics


NOW = datetime(2026, 8, 7, 12, 30, 1, 100, tzinfo=timezone.utc)


def paths_for(root: Path) -> holdet.AppPaths:
    return holdet.resolve_paths(
        overrides=holdet.PathOverrides(data_root=root), environ={}
    )


def snapshot_bytes(kind: str, generated_at: datetime, *, locale: str = "da") -> bytes:
    if kind == "players":
        statistics = sample_statistics(3)
        statistics = replace(statistics, game=replace(statistics.game, locale=locale))
        return holdet.player_statistics_to_json(
            statistics, generated_at=generated_at
        ).encode("utf-8")
    team = sample_team()
    team = replace(
        team, reference=replace(team.reference, game=replace(team.reference.game, locale=locale))
    )
    return holdet.team_to_json(team, generated_at=generated_at).encode("utf-8")


def snapshot_package(contents: tuple[bytes, ...]) -> bytes:
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        files = []
        for index, content in enumerate(contents):
            name = f"snapshot-{index}.json"
            archive.writestr(name, content)
            files.append({
                "path": name,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            })
        archive.writestr(
            "snapshot-package-manifest.json",
            json.dumps({"schema_version": 1, "files": files}),
        )
    return output.getvalue()


@pytest.mark.parametrize("kind", ("players", "teams"))
@pytest.mark.parametrize("dimension", ("subseconds", "year", "locale"))
def test_package_preserves_distinct_observations_with_colliding_names(
    tmp_path: Path, kind: str, dimension: str
) -> None:
    paths = paths_for(tmp_path / "hub")
    first = snapshot_bytes(kind, NOW)
    generated = NOW.replace(microsecond=200) if dimension == "subseconds" else NOW
    generated = generated.replace(year=2027) if dimension == "year" else generated
    second = snapshot_bytes(kind, generated, locale="en" if dimension == "locale" else "da")
    raw = snapshot_package((first, second))

    preview = holdet.preview_import(raw, paths, filename="snapshots.zip")
    assert preview.can_apply, preview.errors
    assert not paths.data_dir.exists()
    result = holdet.apply_import(preview, raw, paths)

    assert result.written_files == 2
    targets = tuple(operation.target for operation in preview.operations)
    assert len(set(targets)) == 2
    assert {target.read_bytes() for target in targets} == {first, second}
    store = (
        holdet.PlayerStatisticsStore(paths.snapshot_dir)
        if kind == "players" else holdet.SnapshotStore(paths.snapshot_dir)
    )
    assert len(store.scan().snapshots) == 2
    repeated = snapshot_package((second, first))
    again = holdet.preview_import(repeated, paths, filename="snapshots.zip")
    assert again.can_apply and again.skipped_count == 2
    assert holdet.apply_import(again, repeated, paths).written_files == 0


@pytest.mark.parametrize("kind", ("players", "teams"))
def test_reimport_finds_identical_suffix_when_base_has_been_removed(
    tmp_path: Path, kind: str
) -> None:
    paths = paths_for(tmp_path / "hub")
    first = snapshot_bytes(kind, NOW)
    second = snapshot_bytes(kind, NOW.replace(microsecond=200))
    first_preview = holdet.preview_import(first, paths, filename="first.json")
    holdet.apply_import(first_preview, first, paths)
    second_preview = holdet.preview_import(second, paths, filename="second.json")
    assert second_preview.can_apply, second_preview.errors
    holdet.apply_import(second_preview, second, paths)
    first_preview.operations[0].target.unlink()

    repeated = holdet.preview_import(second, paths, filename="second.json")
    assert repeated.can_apply and repeated.skipped_count == 1
    assert repeated.operations[0].target == second_preview.operations[0].target
    result = holdet.apply_import(repeated, second, paths)
    assert result.written_files == 0 and result.skipped_files == 1
    assert not first_preview.operations[0].target.exists()


@pytest.mark.parametrize("kind", ("players", "teams"))
def test_existing_identity_with_changed_content_is_not_given_another_suffix(
    tmp_path: Path, kind: str
) -> None:
    paths = paths_for(tmp_path / "hub")
    original = snapshot_bytes(kind, NOW)
    preview = holdet.preview_import(original, paths, filename="original.json")
    holdet.apply_import(preview, original, paths)
    changed = json.loads(original)
    changed["entries" if kind == "players" else "roster"][0]["value"] += 1
    raw = json.dumps(changed).encode("utf-8")

    conflict = holdet.preview_import(raw, paths, filename="changed.json")
    assert not conflict.can_apply
    with pytest.raises(holdet.PayloadError):
        holdet.apply_import(conflict, raw, paths)
    assert preview.operations[0].target.read_bytes() == original
    assert len(tuple(paths.snapshot_dir.rglob("*.json"))) == 1


@pytest.mark.parametrize("changed_label", ("team_name", "account_key", "owner_name"))
def test_team_identity_conflict_is_rejected_across_changed_directory_labels(
    tmp_path: Path, changed_label: str
) -> None:
    paths = paths_for(tmp_path / "hub")
    original = snapshot_bytes("teams", NOW)
    payload = json.loads(original)
    if changed_label == "owner_name":
        payload["account"]["key"] = "direct"
        original = json.dumps(payload).encode("utf-8")
    first = holdet.preview_import(original, paths, filename="original.json")
    holdet.apply_import(first, original, paths)
    if changed_label == "team_name":
        payload["team"]["name"] = "Renamed team"
    elif changed_label == "account_key":
        payload["account"]["key"] = "renamed-account"
    else:
        payload["account"]["owner_name"] = "Renamed owner"
    changed = json.dumps(payload).encode("utf-8")

    conflict = holdet.preview_import(changed, paths, filename="changed.json")
    assert not conflict.can_apply
    with pytest.raises(holdet.PayloadError, match="Identitetskollision"):
        holdet.apply_import(conflict, changed, paths)
    assert first.operations[0].target.read_bytes() == original
    assert len(holdet.SnapshotStore(paths.snapshot_dir).scan().snapshots) == 1


def test_team_reimport_skips_identical_observation_under_a_moved_directory(tmp_path: Path) -> None:
    paths = paths_for(tmp_path / "hub")
    original = snapshot_bytes("teams", NOW)
    first = holdet.preview_import(original, paths, filename="original.json")
    holdet.apply_import(first, original, paths)
    old_target = first.operations[0].target
    moved = old_target.parent.parent / "Renamed-team-folder" / old_target.name
    moved.parent.mkdir()
    old_target.rename(moved)

    repeated = holdet.preview_import(original, paths, filename="original.json")
    assert repeated.can_apply and repeated.skipped_count == 1
    assert repeated.operations[0].target == moved.resolve()
    result = holdet.apply_import(repeated, original, paths)
    assert result.written_files == 0 and result.skipped_files == 1
    assert not old_target.exists()
    assert moved.read_bytes() == original


@pytest.mark.parametrize("changed_content", (False, True))
def test_player_import_finds_observations_published_under_recovery_filenames(
    tmp_path: Path, changed_content: bool
) -> None:
    paths = paths_for(tmp_path / "hub")
    original = snapshot_bytes("players", NOW)
    timestamp_target = holdet.preview_import(original, paths, filename="original.json").operations[0].target
    recovered = timestamp_target.with_name("player-round3_recovery-00000000-0000-4000-8000-000000000001.json")
    recovered.parent.mkdir(parents=True)
    recovered.write_bytes(original)
    incoming = original
    if changed_content:
        payload = json.loads(original)
        payload["entries"][0]["value"] += 1
        incoming = json.dumps(payload).encode("utf-8")

    preview = holdet.preview_import(incoming, paths, filename="incoming.json")
    if changed_content:
        assert not preview.can_apply
        with pytest.raises(holdet.PayloadError, match="Identitetskollision"):
            holdet.apply_import(preview, incoming, paths)
    else:
        assert preview.can_apply and preview.skipped_count == 1
        assert preview.operations[0].target == recovered.resolve()
        result = holdet.apply_import(preview, incoming, paths)
        assert result.written_files == 0 and result.skipped_files == 1
    assert recovered.read_bytes() == original
    assert not timestamp_target.exists()
    assert len(holdet.PlayerStatisticsStore(paths.snapshot_dir).scan().snapshots) == 1


@pytest.mark.parametrize("kind", ("players", "teams"))
def test_snapshot_package_scans_existing_observation_identities_once_per_game_and_type(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    paths = paths_for(tmp_path / "hub")
    original = snapshot_bytes(kind, NOW)
    first = holdet.preview_import(original, paths, filename="original.json")
    holdet.apply_import(first, original, paths)
    root = first.operations[0].target.parents[2] if kind == "teams" else first.operations[0].target.parent
    method_name = "rglob" if kind == "teams" else "glob"
    scan = getattr(Path, method_name)
    scans = []

    def count_identity_scans(path, pattern, *args, **kwargs):
        if path == root and pattern in {"team-round*.json", "player-round*.json"}:
            scans.append(pattern)
        return scan(path, pattern, *args, **kwargs)

    monkeypatch.setattr(Path, method_name, count_identity_scans)
    raw = snapshot_package(tuple(
        snapshot_bytes(kind, NOW.replace(second=second)) for second in (2, 3, 4)
    ))
    preview = holdet.preview_import(raw, paths, filename="snapshots.zip")
    assert preview.can_apply and preview.write_count == 3
    assert len(scans) == 1


@pytest.mark.parametrize("problem", ("invalid", "unreadable"))
@pytest.mark.parametrize("location", ("base", "suffix"))
def test_unclassifiable_collision_is_rejected_without_changing_it(
    tmp_path: Path, monkeypatch, problem: str, location: str
) -> None:
    paths = paths_for(tmp_path / "hub")
    raw = snapshot_bytes("players", NOW)
    target = holdet.preview_import(raw, paths, filename="snapshot.json").operations[0].target
    if location == "suffix":
        target = target.with_name(f"{target.stem}_3.json")
    target.parent.mkdir(parents=True)
    original = b"{invalid" if problem == "invalid" else raw
    target.write_bytes(original)
    read_bytes = Path.read_bytes
    if problem == "unreadable":
        def deny_selected(path):
            if path == target:
                raise PermissionError("synthetic read denial")
            return read_bytes(path)
        monkeypatch.setattr(Path, "read_bytes", deny_selected)

    preview = holdet.preview_import(raw, paths, filename="snapshot.json")
    assert not preview.can_apply
    assert read_bytes(target) == original
    assert len(tuple(paths.snapshot_dir.rglob("*.json"))) == 1


def test_team_import_respects_the_paired_text_filename(tmp_path: Path) -> None:
    paths = paths_for(tmp_path / "hub")
    raw = snapshot_bytes("teams", NOW)
    base = holdet.preview_import(raw, paths, filename="team.json").operations[0].target
    paired = base.with_suffix(".txt")
    paired.parent.mkdir(parents=True)
    paired.write_text("existing export", encoding="utf-8")

    preview = holdet.preview_import(raw, paths, filename="team.json")
    assert preview.can_apply
    assert preview.operations[0].target != base
    holdet.apply_import(preview, raw, paths)
    assert not base.exists()
    assert paired.read_text(encoding="utf-8") == "existing export"


def test_apply_rechecks_skipped_targets_after_acquiring_the_data_lease(
    tmp_path: Path, monkeypatch
) -> None:
    paths = paths_for(tmp_path / "hub")
    raw = snapshot_bytes("players", NOW)
    first = holdet.preview_import(raw, paths, filename="snapshot.json")
    holdet.apply_import(first, raw, paths)
    duplicate = holdet.preview_import(raw, paths, filename="snapshot.json")
    assert duplicate.skipped_count == 1
    target = duplicate.operations[0].target
    acquire = imports.exclusive_data_operation

    @contextmanager
    def after_intervening_removal(data_dir):
        with acquire(data_dir):
            target.unlink()
            yield

    monkeypatch.setattr(imports, "exclusive_data_operation", after_intervening_removal)
    result = holdet.apply_import(duplicate, raw, paths)
    assert result.written_files == 1 and result.skipped_files == 0
    assert target.read_bytes() == raw


def test_apply_replans_a_suffix_after_an_intervening_publication(
    tmp_path: Path, monkeypatch
) -> None:
    paths = paths_for(tmp_path / "hub")
    existing = snapshot_bytes("players", NOW)
    incoming = snapshot_bytes("players", NOW.replace(microsecond=200))
    preview = holdet.preview_import(incoming, paths, filename="snapshot.json")
    target = preview.operations[0].target
    acquire = imports.exclusive_data_operation

    @contextmanager
    def after_intervening_publication(data_dir):
        with acquire(data_dir):
            imports.publish_immutable(target, existing)
            yield

    monkeypatch.setattr(imports, "exclusive_data_operation", after_intervening_publication)
    result = holdet.apply_import(preview, incoming, paths)
    assert result.written_files == 1
    assert target.read_bytes() == existing
    assert {path.read_bytes() for path in paths.snapshot_dir.rglob("*.json")} == {
        existing, incoming
    }


def test_duplicate_package_identities_remain_invalid(tmp_path: Path) -> None:
    paths = paths_for(tmp_path / "hub")
    content = snapshot_bytes("players", NOW)
    raw = snapshot_package((content, content))
    preview = holdet.preview_import(raw, paths, filename="snapshots.zip")
    assert not preview.can_apply
    assert any("duplikeret identitet" in error for error in preview.errors)
    assert not paths.data_dir.exists()


def test_failed_package_publication_preserves_existing_files_and_releases_lease(
    tmp_path: Path, monkeypatch
) -> None:
    paths = paths_for(tmp_path / "hub")
    existing = snapshot_bytes("players", NOW)
    first = holdet.preview_import(existing, paths, filename="existing.json")
    holdet.apply_import(first, existing, paths)
    contents = tuple(snapshot_bytes("players", NOW.replace(second=second)) for second in (2, 3))
    raw = snapshot_package(contents)
    preview = holdet.preview_import(raw, paths, filename="snapshots.zip")
    publish = imports.publish_immutable
    calls = 0

    def fail_second(target, content):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic publication failure")
        publish(target, content)

    monkeypatch.setattr(imports, "publish_immutable", fail_second)
    with pytest.raises(OSError, match="synthetic publication failure"):
        holdet.apply_import(preview, raw, paths)
    assert first.operations[0].target.read_bytes() == existing
    assert all(not operation.target.exists() for operation in preview.operations)
    with imports.exclusive_data_operation(paths.data_dir):
        pass


def test_backup_import_delegates_data_ownership_to_restore(tmp_path: Path, monkeypatch) -> None:
    source = paths_for(tmp_path / "source")
    source.import_dir.mkdir(parents=True)
    (source.import_dir / "notes.txt").write_text("saved data", encoding="utf-8")
    raw, _ = holdet.create_backup_bytes(source)
    target = paths_for(tmp_path / "target")
    preview = holdet.preview_import(raw, target, filename="backup.zip")
    assert preview.kind == "backup" and preview.can_apply

    def forbidden_nested_acquisition(_data_dir):
        raise AssertionError("backup import must delegate lease ownership")

    monkeypatch.setattr(imports, "exclusive_data_operation", forbidden_nested_acquisition)
    result = holdet.apply_import(preview, raw, target)
    assert result.kind == "backup" and result.written_files == 1
    assert (target.import_dir / "notes.txt").read_text(encoding="utf-8") == "saved data"
