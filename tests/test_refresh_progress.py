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


def test_sessions_follow_active_runs_and_observe_fast_terminal_results(tmp_path):
    def run(run_id, status, updated, title):
        return {
            "run_id": run_id, "updated_at": updated, "status": status,
            "games": [{"game": {"name": title, "game": "example"}, "status": status}],
        }

    old = run("old", "partial", "2026-10-31T19:00:00+00:00", "Tidligere kørsel")
    newer = run("newer", "success", "2026-11-01T18:00:00+00:00", "Seneste tidligere kørsel")
    external = run("current", "running", "2026-11-01T19:00:00+00:00", "Aktuel kørsel")
    fast = run("fast", "success", "2026-11-01T20:00:00+00:00", "Hurtig kørsel")
    runs = [newer, old]
    coordinator = SimpleNamespace(scan_runs=lambda: (runs, []), resume=Mock(), cancel=Mock())
    with (
        patch.object(workflows, "_coordinator", return_value=coordinator),
        patch.object(workflows.st.cache_data, "clear") as clear,
    ):
        first = AppTest.from_function(progress_app, args=(tmp_path,))
        second = AppTest.from_function(progress_app, args=(tmp_path,))
        first.run(timeout=15)
        first.selectbox(key="refresh-history:newer").set_value("old").run(timeout=15)
        assert first.button(key="resume-refresh:old")

        runs[:] = [external, newer, old]
        for app in (first, second):
            app.run(timeout=15)
            assert not app.exception
            assert any("Aktuel kørsel" in item.value for item in app.markdown)
            assert app.button(key="cancel-refresh:current")

        runs[:] = [fast, newer, old]
        before = clear.call_count
        for app in (first, second):
            app.run(timeout=15)
            assert not app.exception
            assert any("Hurtig kørsel" in item.value for item in app.markdown)
            assert not any(button.key == "cancel-refresh:current" for button in app.button)
        assert clear.call_count == before + 2

        first.selectbox(key="refresh-history:fast").set_value("old").run(timeout=15)
        assert first.button(key="resume-refresh:old")
        assert clear.call_count == before + 2
