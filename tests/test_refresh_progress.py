"""Refresh progress distinguishes completed attempts from fresh data."""

from types import SimpleNamespace
from unittest.mock import Mock, patch

from streamlit.testing.v1 import AppTest

from website import workflows


def progress_app(root):
    from holdet_lib.paths import PathOverrides, resolve_paths
    from website.workflows import render_run_progress

    render_run_progress(resolve_paths(overrides=PathOverrides(data_root=root)))


def show_progress(tmp_path, status, game_statuses):
    run = {
        "run_id": "00000000-0000-0000-0000-000000000001",
        "updated_at": "2026-10-31T19:00:00+00:00",
        "status": status,
        "games": [
            {"game": {"name": f"Spil {index}", "game": f"game-{index}"},
             "status": value, "progress": None, "detail": None}
            for index, value in enumerate(game_statuses)
        ],
    }
    coordinator = SimpleNamespace(
        scan_runs=lambda: ([run], []), snapshot=lambda _: run,
        resume=Mock(), cancel=Mock(),
    )
    with patch.object(workflows, "_coordinator", return_value=coordinator):
        app = AppTest.from_function(progress_app, args=(tmp_path,)).run(timeout=15)
    assert not app.exception
    coordinator.resume.assert_not_called()
    coordinator.cancel.assert_not_called()
    assert not list(tmp_path.iterdir())
    return app


def test_stopped_update_shows_attempted_progress_and_games_waiting_for_resume(tmp_path):
    app = show_progress(tmp_path, "cancelled", ["partial", "partial", "cancelled", *(["unstarted"] * 7)])
    progress = app.get("progress")[0].proto
    assert progress.value == 30
    assert progress.text == "3 af 10 spil behandlet · 0 fuldt opdateret · 2 med mangler"
    rows = [item.value for item in app.markdown]
    assert sum("Afventer genoptagelse" in row for row in rows) == 7
    assert not any("Forbereder opdatering" in row for row in rows)
    assert any("7 spil afventer genoptagelse" in caption.value for caption in app.caption)


def test_running_update_keeps_waiting_games_queued(tmp_path):
    app = show_progress(tmp_path, "running", ["success", "running", "unstarted"])
    assert app.get("progress")[0].proto.value == 33
    assert any("I kø" in item.value for item in app.markdown)
    assert not any("afventer genoptagelse" in caption.value for caption in app.caption)
