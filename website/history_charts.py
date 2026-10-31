"""Round-based history charts with explicit discrete ticks and honest gaps."""

from __future__ import annotations

import math

import altair as alt
import pandas as pd

from holdet_lib import format_integer


def round_history_chart(frame: pd.DataFrame, column: str, *, change: bool = False):
    data = frame.copy()
    if "Hold-ID" not in data:
        data["Hold-ID"] = data["Hold"]
    identities = data[["Hold-ID", "Hold"]].drop_duplicates().sort_values("Hold-ID")
    duplicate_names = identities["Hold"].duplicated(keep=False)
    labels = {
        row["Hold-ID"]: f"{row['Hold']} ({row['Hold-ID']})" if duplicate else row["Hold"]
        for (_, row), duplicate in zip(identities.iterrows(), duplicate_names)
    }
    data["Serie"] = data["Hold-ID"].map(labels)
    data = data.sort_values(["Hold-ID", "Runde"])
    data["Grafværdi"] = pd.to_numeric(data[column], errors="coerce")
    if change:
        baseline = data.groupby("Hold-ID")["Grafværdi"].transform("first")
        data["Grafværdi"] = data["Grafværdi"] - baseline
    data["Visningsværdi"] = data["Grafværdi"].map(
        lambda value: "Mangler" if pd.isna(value) else format_integer(int(value))
    )
    rounds = sorted(int(value) for value in data["Runde"].unique())
    stride = max(1, math.ceil(len(rounds) / 12))
    ticks = sorted(set((*rounds[::stride], rounds[-1])))
    rank = column in {"Samlet placering", "Rundeplacering", "Grupperang"}
    money = "Enhed" in data and data["Enhed"].eq("money").all()
    millions = money and column in {"Værdi/point", "Rundevækst"}
    title = "Ændring fra første observation" if change else column
    if column == "Værdi/point" and not change:
        title = "Holdværdi" if money else "Point"
    tooltip_title = title
    if millions:
        title += " (mio.)"
        data["Grafværdi"] = data["Grafværdi"] / 1_000_000
    number_label = (
        "replace(format(datum.value, '.1f'), '.', ',')"
        if millions else "replace(format(datum.value, ',.0f'), /,/g, '.')"
    )
    tooltip = [alt.Tooltip("Runde:Q", format="d"), alt.Tooltip("Serie:N", title="Hold"),
               alt.Tooltip("Visningsværdi:N", title=tooltip_title)]
    if "Datastatus" in data:
        tooltip.append(alt.Tooltip("Datastatus:N"))
    return alt.Chart(data).mark_line(
        point=True, strokeWidth=2.5, invalid="break-paths-show-domains",
    ).encode(
        x=alt.X("Runde:Q", title="Runde", scale=alt.Scale(zero=False, nice=False),
                axis=alt.Axis(values=ticks, format="d", tickMinStep=1, grid=False, labelAngle=0)),
        y=alt.Y("Grafværdi:Q", title=title,
                scale=alt.Scale(reverse=rank, zero=change or column == "Rundevækst"),
                axis=alt.Axis(labelExpr=number_label, tickCount=5, tickMinStep=1 if rank else alt.Undefined)),
        color=alt.Color("Serie:N", title="Hold", sort=list(labels.values()), scale=alt.Scale(scheme="tableau10"), legend=alt.Legend(orient="top", symbolType="stroke")),
        strokeDash=alt.StrokeDash("Serie:N", title="Hold", sort=list(labels.values()), legend=alt.Legend(orient="top", symbolType="stroke")),
        detail="Hold-ID:N", order="Runde:Q", tooltip=tooltip,
    ).properties(height=300)
