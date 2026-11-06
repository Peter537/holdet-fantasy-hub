from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from streamlit.testing.v1 import AppTest

import holdet_lib as holdet
from holdet_lib.integrity import canonical_local_files
from tests.test_player_statistics import sample_statistics
from website import data_sections


@pytest.fixture(autouse=True)
def app_script_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from streamlit.testing.v1 import app_test

    monkeypatch.setattr(app_test, "TMP_DIR", SimpleNamespace(name=str(tmp_path)))


def _integrity_app(root: str) -> None:
    from pathlib import Path

    from holdet_lib.paths import PathOverrides, resolve_paths
    from website.data_sections import _integrity_panel

    _integrity_panel(resolve_paths(overrides=PathOverrides(data_root=Path(root)), environ={}))


def _paths(root: Path) -> holdet.AppPaths:
    return holdet.resolve_paths(overrides=holdet.PathOverrides(data_root=root), environ={})


def _seed_integrity_state(paths: holdet.AppPaths) -> dict[str, object]:
    return {
        "full-integrity-result": holdet.full_integrity_check(paths),
        "integrity-repair-preview": holdet.preview_integrity_repair(paths),
        "unrelated-selection": "keep",
    }


@pytest.mark.parametrize("corrupt_json", (False, True))
def test_successful_repair_discards_old_full_check_until_explicit_recheck(
    tmp_path: Path, corrupt_json: bool,
) -> None:
    paths = _paths(tmp_path)
    source = paths.import_dir / ("broken.json" if corrupt_json else "notes.txt")
    source.parent.mkdir(parents=True)
    source.write_text("invalid json" if corrupt_json else "local note", encoding="utf-8")
    original = source.read_bytes()
    with patch.object(data_sections, "full_integrity_check", wraps=holdet.full_integrity_check) as full_check:
        app = AppTest.from_function(_integrity_app, args=(str(tmp_path),), default_timeout=15).run()
        app.button(key="run-full-integrity").click().run()
        assert any("Fuld kontrol fandt" in item.value for item in app.warning)
        app.button(key="preview-integrity-repair").click().run()
        app.checkbox(key="confirm-integrity-repair").check().run()
        app.button(key="apply-integrity-repair").click().run()

        assert not app.exception
        assert source.read_bytes() == original
        assert paths.integrity_index_file.exists()
        assert "full-integrity-result" not in app.session_state
        assert "integrity-repair-preview" not in app.session_state
        assert not any("Fuld kontrol" in item.value for item in (*app.warning, *app.success))
        assert full_check.call_count == 1

        app.button(key="run-full-integrity").click().run()
        assert not app.exception
        assert full_check.call_count == 2
        result = app.session_state["full-integrity-result"]
        assert {issue.code for issue in result.issues} == ({"schema"} if corrupt_json else set())
        if corrupt_json:
            assert any("Fuld kontrol fandt 1 afvigelser" in item.value for item in app.warning)
        else:
            assert any("Fuld kontrol gennemførte 1 filer uden fejl" in item.value for item in app.success)


def test_failed_repair_preserves_full_check_and_preview(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    source = paths.import_dir / "notes.txt"
    source.parent.mkdir(parents=True)
    source.write_text("local note", encoding="utf-8")
    app = AppTest.from_function(_integrity_app, args=(str(tmp_path),), default_timeout=15).run()
    app.button(key="run-full-integrity").click().run()
    app.button(key="preview-integrity-repair").click().run()
    app.checkbox(key="confirm-integrity-repair").check().run()
    original = {key: app.session_state[key] for key in _seed_integrity_state(paths) if key != "unrelated-selection"}

    with holdet.exclusive_data_operation(paths.data_dir):
        app.button(key="apply-integrity-repair").click().run()

    assert not app.exception
    assert not paths.integrity_index_file.exists()
    assert any("Indekset blev ikke repareret" in item.value for item in app.error)
    assert all(app.session_state[key] == value for key, value in original.items())


@pytest.mark.parametrize("restore", (False, True))
@pytest.mark.parametrize("fails", (False, True))
def test_import_and_restore_invalidate_integrity_only_after_success(
    tmp_path: Path, restore: bool, fails: bool,
) -> None:
    paths = _paths(tmp_path / "target")
    if restore:
        source = _paths(tmp_path / "source")
        source.config_dir.mkdir(parents=True)
        source.groups_file.write_text('{"schema_version":8,"games":[],"groups":[]}', encoding="utf-8")
        archive = holdet.create_backup(source)
        filename, raw = archive.name, archive.read_bytes()
    else:
        filename, raw = "notes.txt", b"local note"
    ui = MagicMock()
    original = _seed_integrity_state(paths)
    ui.session_state = dict(original)
    ui.file_uploader.return_value = SimpleNamespace(name=filename, getvalue=lambda: raw)
    ui.checkbox.return_value = True
    ui.button.side_effect = lambda _label, **kwargs: kwargs.get("key") == "apply-data-import"

    with patch.object(data_sections, "st", ui), patch.object(data_sections, "dataframe"):
        if fails:
            with patch.object(data_sections, "apply_import", side_effect=OSError("local write failure")):
                data_sections.render_import_backup(paths)
        else:
            data_sections.render_import_backup(paths)

    if fails:
        assert ui.session_state == original
        ui.rerun.assert_not_called()
        assert ui.error.called
    else:
        assert "full-integrity-result" not in ui.session_state
        assert "integrity-repair-preview" not in ui.session_state
        assert ui.session_state["unrelated-selection"] == "keep"
        assert canonical_local_files(paths)
        ui.rerun.assert_called_once()


@pytest.mark.parametrize("fails", (False, True))
def test_retention_archive_invalidates_integrity_only_after_success(tmp_path: Path, fails: bool) -> None:
    paths = _paths(tmp_path)
    store = holdet.PlayerStatisticsStore(paths.snapshot_dir)
    now = datetime(2026, 11, 6, 12, tzinfo=timezone.utc)
    older = store.save(sample_statistics(), now=now)
    newest = store.save(sample_statistics(), now=now + timedelta(minutes=1))
    plan = holdet.plan_snapshot_retention(paths)
    assert len(plan.candidates) == 1
    ui = MagicMock()
    original = _seed_integrity_state(paths)
    ui.session_state = dict(original)
    ui.button.return_value = True
    with patch.object(data_sections, "st", ui):
        if fails:
            with holdet.exclusive_data_operation(paths.data_dir):
                data_sections._archive_dialog.__wrapped__(paths, plan.candidates, len(plan.retained))
        else:
            data_sections._archive_dialog.__wrapped__(paths, plan.candidates, len(plan.retained))

    assert newest.exists()
    if fails:
        assert older.exists()
        assert ui.session_state == original
        ui.rerun.assert_not_called()
        assert ui.error.called
    else:
        assert not older.exists()
        assert "full-integrity-result" not in ui.session_state
        assert "integrity-repair-preview" not in ui.session_state
        assert ui.session_state["unrelated-selection"] == "keep"
        ui.rerun.assert_called_once()
