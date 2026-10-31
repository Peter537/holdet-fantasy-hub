"""Whole-round history charts retain identity, gaps and historical scope."""

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest


def chart_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"Runde": number, "Hold": "United", "Hold-ID": team_id,
             "Enhed": "money", "Værdi/point": value,
             "Samlet placering": rank}
            for team_id, values in (
                (11, ((1, None, None), (2, 50_100_000, 10), (3, None, None), (4, 50_300_000, 8))),
                (22, ((1, 51_000_000, 12), (2, None, None), (3, None, None), (4, 51_400_000, 9))),
            )
            for number, value, rank in values
        ]
    )


def chart_parts(chart):
    """Read the line and its actual dataset without relying on Altair dataset IDs."""

    spec = chart.to_dict()

    def find_line(node):
        mark = node.get("mark")
        if mark == "line" or isinstance(mark, dict) and mark.get("type") == "line":
            return node
        for child in node.get("layer", ()):
            found = find_line(child)
            if found is not None:
                return found
        return None

    line = find_line(spec)
    assert line is not None, "The chart must contain a line series"
    data = line.get("data", spec.get("data", {}))
    values = data.get("values")
    if values is None:
        values = spec.get("datasets", {}).get(data.get("name"))
    assert values is not None, "The line dataset must be inspectable"
    return pd.DataFrame(values), line["encoding"], line.get("mark")


def test_round_axis_has_explicit_whole_round_ticks():
    from website.history_charts import round_history_chart

    _, encoding, _ = chart_parts(round_history_chart(chart_frame(), "Værdi/point"))
    axis = encoding["x"]["axis"]
    assert axis["values"]
    assert all(isinstance(value, int) and not isinstance(value, bool) for value in axis["values"])
    assert {1, 2, 4} <= set(axis["values"])
    assert axis.get("format") == "d"


def test_duplicate_names_keep_deterministic_distinct_team_series_without_input_mutation():
    from website.history_charts import round_history_chart

    source = chart_frame()
    before = source.copy(deep=True)
    data, encoding, _ = chart_parts(round_history_chart(source, "Værdi/point"))
    reversed_data, reversed_encoding, _ = chart_parts(
        round_history_chart(source.iloc[::-1].reset_index(drop=True), "Værdi/point")
    )
    series_field = encoding["color"]["field"]
    reversed_field = reversed_encoding["color"]["field"]
    by_team = data.groupby("Hold-ID")[series_field].unique()
    reversed_by_team = reversed_data.groupby("Hold-ID")[reversed_field].unique()
    assert all(len(labels) == 1 for labels in by_team)
    assert len({labels[0] for labels in by_team}) == 2
    assert {key: labels[0] for key, labels in by_team.items()} == {
        key: labels[0] for key, labels in reversed_by_team.items()
    }
    pd.testing.assert_frame_equal(source, before)


def test_missing_rounds_break_lines_instead_of_connecting_observations():
    from website.history_charts import round_history_chart

    data, encoding, mark = chart_parts(round_history_chart(chart_frame(), "Værdi/point"))
    details = encoding.get("detail", ())
    if isinstance(details, dict):
        details = (details,)
    detail_fields = [item["field"] for item in details]
    value_field = encoding["y"]["field"]
    team = data[data["Hold-ID"] == 11]
    second = team[team["Runde"] == 2].iloc[0]
    fourth = team[team["Runde"] == 4].iloc[0]
    separate_segments = any(second[field] != fourth[field] for field in detail_fields)
    if not separate_segments:
        assert isinstance(mark, dict) and str(mark.get("invalid", "")).startswith("break-paths")
        gap = team[team["Runde"] == 3]
        assert not gap.empty and gap[value_field].isna().all()


def test_rank_axis_is_reversed_with_integer_labels():
    from website.history_charts import round_history_chart

    _, encoding, _ = chart_parts(round_history_chart(chart_frame(), "Samlet placering"))
    assert encoding["y"]["scale"]["reverse"] is True
    axis = encoding["y"]["axis"]
    assert axis.get("format") == "d" or ".0f" in axis.get("labelExpr", "")
    assert encoding["y"]["axis"].get("tickMinStep", 1) >= 1


def test_value_axis_is_zoomed_and_explicit_about_money_units():
    from website.history_charts import round_history_chart

    data, encoding, _ = chart_parts(round_history_chart(chart_frame(), "Værdi/point"))
    assert encoding["y"]["scale"]["zero"] is False
    assert "mio" in encoding["y"]["title"].casefold()
    assert 50.1 in data[encoding["y"]["field"]].tolist()
    value_tooltip = next(item for item in encoding["tooltip"] if item.get("field") == "Visningsværdi")
    assert "50.100.000" in data[value_tooltip["field"]].tolist()
    assert "mio" not in value_tooltip["title"].casefold()


def test_change_view_uses_each_teams_first_available_value():
    from website.history_charts import round_history_chart

    data, encoding, _ = chart_parts(
        round_history_chart(chart_frame(), "Værdi/point", change=True)
    )
    value_field = encoding["y"]["field"]
    actual = {
        (row["Hold-ID"], row["Runde"]): row[value_field]
        for _, row in data.iterrows()
        if pd.notna(row[value_field])
    }
    assert actual == {(11, 2): 0, (11, 4): 0.2, (22, 1): 0, (22, 4): 0.4}


def history_fixture():
    import holdet_lib as holdet
    from tests.test_library_storage import sample_team

    first = sample_team(11, name="United", current_round=4, total=50_300_000, history_rounds=(4, 2, 1))
    second = sample_team(22, name="United", current_round=4, total=51_400_000, history_rounds=(4, 2))
    game = first.reference.game
    group = holdet.GroupDefinition(
        "history-test", "History", game,
        tuple(holdet.GroupTeam(team.reference.team_id, team.team_name, team.reference.source_url)
              for team in (first, second)),
    )
    snapshots = holdet.SnapshotIndex(tuple(
        holdet.TeamSnapshot(Path(f"unused-{team.reference.team_id}.json"),
                            datetime(2026, 8, 7, 12, tzinfo=timezone.utc), team)
        for team in (first, second)
    ))
    return game, group, snapshots


def _history_app(group_view: bool, through_round: int | None):
    from tests.test_round_history_ui import history_fixture
    from website.hub_pages import group_history_panel, history_panel

    game, group, snapshots = history_fixture()
    if group_view:
        group_history_panel(group, snapshots, through_round=through_round)
    else:
        history_panel(game, snapshots, (11, 22), scope="test", through_round=through_round)


@pytest.mark.parametrize("group_view", (False, True))
def test_historical_panels_bound_charts_and_tables_before_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, group_view: bool
):
    from streamlit.testing.v1 import app_test
    from website import hub_pages
    from website.history_charts import round_history_chart

    monkeypatch.setattr(app_test, "TMP_DIR", SimpleNamespace(name=str(tmp_path)))
    seen = []

    def capture_chart(frame, column, *, change=False):
        seen.append((frame.copy(deep=True), change))
        return round_history_chart(frame, column, change=change)

    monkeypatch.setattr(hub_pages, "round_history_chart", capture_chart)
    app = AppTest.from_function(_history_app, args=(group_view, 2)).run(timeout=20)
    assert not app.exception
    assert seen and all(frame["Runde"].max() <= 2 for frame, _ in seen)
    assert app.dataframe
    for table in app.dataframe:
        if "Runde" in table.value:
            assert table.value["Runde"].max() <= 2

    next(item for item in app.segmented_control if item.label == "Sammenlign udvikling").set_value(
        "Ændring fra første runde"
    ).run(timeout=20)
    assert not app.exception
    assert seen[-1][1] is True
    assert all(frame["Runde"].max() <= 2 for frame, _ in seen)

    seen.clear()
    app = AppTest.from_function(_history_app, args=(group_view, None)).run(timeout=20)
    assert not app.exception
    assert seen and any(frame["Runde"].max() == 4 for frame, _ in seen)
