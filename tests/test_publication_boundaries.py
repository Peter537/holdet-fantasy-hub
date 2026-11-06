from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import json

import pytest

import holdet_lib as holdet
from holdet_lib import player_exports, team_exports
from holdet_lib.backup import known_schema_error
from holdet_lib.player_serialization import player_statistics_to_json
from tests.test_decision_analysis import NOW
from tests.test_library_storage import sample_team
from tests.test_player_statistics import sample_statistics


@pytest.mark.parametrize("kind", ("players", "teams"))
@pytest.mark.parametrize("failure", ("serialization", "publication", "collision"))
def test_format_sets_preserve_existing_files_and_remove_partial_attempts(tmp_path, kind, failure):
    if kind == "players":
        module = player_exports
        store = holdet.PlayerExportStore(tmp_path)
        document = holdet.build_player_export(sample_statistics(3), holdet.PlayerStatisticsQuery(), generated_at=NOW)
        serializer = "serialize_player_export"
    else:
        module = team_exports
        store = holdet.TeamExportStore(tmp_path)
        document = holdet.build_team_export(sample_team(101), generated_at=NOW)
        serializer = "serialize_team_export"
    original = store.save(document, ("md",))[0]
    before = original.path.read_bytes()
    publish = module.publish_immutable
    serialize = getattr(module, serializer)
    calls = 0
    competitor = None

    def fail_second_publish(path, content):
        nonlocal calls, competitor
        calls += 1
        if calls == 2:
            if failure == "collision":
                competitor = path
                path.write_bytes(b"another publisher")
                raise FileExistsError("synthetic collision")
            raise OSError("synthetic second publication failure")
        return publish(path, content)

    def fail_second_serialize(value, format):
        if format == "json":
            raise ValueError("synthetic second serialization failure")
        return serialize(value, format)

    if failure == "serialization":
        with patch.object(module, serializer, side_effect=fail_second_serialize):
            with pytest.raises(ValueError, match="second serialization"):
                store.save(document, ("txt", "json"))
    elif failure == "publication":
        with patch.object(module, "publish_immutable", side_effect=fail_second_publish):
            with pytest.raises(OSError, match="second publication"):
                store.save(document, ("txt", "json"))
    else:
        with patch.object(module, "publish_immutable", side_effect=fail_second_publish):
            artifacts = store.save(document, ("txt", "json"))
        assert competitor is not None and competitor.read_bytes() == b"another publisher"
        assert not competitor.with_suffix(".txt").exists()
        assert len({item.path.stem for item in artifacts}) == 1
        assert all(item.path.read_bytes() == item.content for item in artifacts)
    assert original.path.read_bytes() == before
    if failure != "collision":
        assert tuple(tmp_path.rglob("*.txt")) == ()
        assert tuple(tmp_path.rglob("*.json")) == ()


def test_refresh_player_stages_survive_backup_restore_and_integrity_scan(tmp_path):
    paths = holdet.resolve_paths(overrides=holdet.PathOverrides(data_root=tmp_path / "source"), environ={})
    run_id, attempt_id = str(uuid4()), str(uuid4())
    stage = paths.refresh_run_dir / run_id / f"player-{attempt_id}.json"
    stage.parent.mkdir(parents=True)
    content = player_statistics_to_json(sample_statistics(3), generated_at=NOW).encode("utf-8")
    stage.write_bytes(content)
    journal = stage.parent / "run.json"
    journal.write_text(json.dumps({
        "schema_version": 2, "run_id": run_id, "games": [],
        "created_at": NOW.isoformat(), "updated_at": NOW.isoformat(), "status": "partial",
    }), encoding="utf-8")
    (stage.parent / "player-unrelated.json").write_text("{}", encoding="utf-8")
    assert known_schema_error(f"data/refresh-runs/{run_id}/{stage.name}", content) is None
    check = holdet.full_integrity_check(paths)
    assert any(item.path.endswith(stage.name) for item in check.entries)
    assert not any(item.path.endswith("player-unrelated.json") for item in check.entries)
    archive = holdet.create_backup(paths)
    target = holdet.resolve_paths(overrides=holdet.PathOverrides(data_root=tmp_path / "target"), environ={})
    holdet.restore_backup(archive, target)
    assert (target.refresh_run_dir / run_id / stage.name).read_bytes() == content
    assert (target.refresh_run_dir / run_id / "run.json").read_bytes() == journal.read_bytes()
    assert not (target.refresh_run_dir / run_id / "player-unrelated.json").exists()


def test_refresh_stage_schema_is_checked_before_backup_publication(tmp_path):
    paths = holdet.resolve_paths(overrides=holdet.PathOverrides(data_root=tmp_path / "hub"), environ={})
    stage = paths.refresh_run_dir / str(uuid4()) / f"player-{uuid4()}.json"
    stage.parent.mkdir(parents=True)
    stage.write_text("{}", encoding="utf-8")
    with pytest.raises(holdet.PayloadError, match="valideringen"):
        holdet.create_backup(paths)
    assert tuple(paths.backup_dir.glob("*.zip")) == ()
