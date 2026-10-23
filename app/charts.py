"""Altair chart builders shared by the pages: $ axes, moving-average studies."""

from __future__ import annotations

import altair as alt
import pandas as pd
import streamlit as st

from ctrade import analytics

PRICE_AXIS = "$,.2f"
CHANGE_AXIS = "$,.3f"  # changes are often fractions of a cent
PRICE_TIP = "$,.4f"


def _legend(title: str) -> alt.Legend:
    return alt.Legend(title=title, orient="bottom", columns=2, labelLimit=500)
PRESETS = ["SMA 5", "SMA 10", "SMA 20", "EMA 5", "EMA 10", "EMA 20"]


def study_picker(key: str) -> list[tuple[str, int]]:
    """Moving-average controls; returns [(kind, window), ...] such as [("SMA", 20)]."""
    c1, c2 = st.columns([2, 1])
    picked = c1.multiselect("Moving averages", PRESETS, key=f"{key}_ma",
                            help="SMA: simple average of the last N marks. EMA: exponential, "
                                 "weighting recent marks more.")
    custom = c2.text_input("Other (e.g. SMA 50, EMA 3)", key=f"{key}_ma_custom")
    studies = []
    for text in picked + [t for t in custom.replace(";", ",").split(",") if t.strip()]:
        kind, _, window = text.strip().upper().partition(" ")
        if kind in analytics.MA_KINDS and window.strip().isdigit() and int(window) > 0:
            studies.append((kind, int(window)))
        else:
            st.warning(f"Ignored {text.strip()!r}: write SMA or EMA and a number of days.")
    return list(dict.fromkeys(studies))


def time_series(df: pd.DataFrame, value: str, group: str | None, studies, title: str,
                legend: str = "Series", bars: bool = False, axis_format: str = PRICE_AXIS
                ) -> alt.Chart:
    """Lines (or bars) over date, with dashed moving-average lines for each study."""
    long = analytics.with_moving_averages(df, value, group, studies)
    long["date"] = pd.to_datetime(long["date"])
    long["line"] = long["study"].map(lambda s: "moving average" if s else "mark")
    x = alt.X("date:T", title=None)
    y = alt.Y(f"{value}:Q", title=title, axis=alt.Axis(format=axis_format),
              scale=alt.Scale(zero=bars))
    tips = [alt.Tooltip("date:T"), alt.Tooltip("series:N", title=legend),
            alt.Tooltip(f"{value}:Q", format=PRICE_TIP)]
    data = long[long["study"] == ""]
    studies_df = long[long["study"] != ""]
    if bars:
        base = alt.Chart(data).mark_bar(opacity=0.7).encode(
            x=x, y=y, color=alt.Color("series:N", legend=_legend(legend)), tooltip=tips)
    else:
        base = alt.Chart(data).mark_line(point=True).encode(
            x=x, y=y, color=alt.Color("series:N", legend=_legend(legend)), tooltip=tips)
    if studies_df.empty:
        return base
    lines = alt.Chart(studies_df).mark_line(strokeDash=[6, 4]).encode(
        x=x, y=y, color=alt.Color("series:N", legend=_legend(legend)), tooltip=tips)
    return base + lines


def curves(df: pd.DataFrame, value: str, title: str, legend: str) -> alt.Chart:
    """One line per date across contract months, nearest month on the left (e.g. Jan27)."""
    order = list(df.sort_values("month")["contract"].drop_duplicates())
    axis_format = CHANGE_AXIS if value == "change" else PRICE_AXIS
    chart = alt.Chart(df).mark_line(point=True).encode(
        x=alt.X("contract:N", title="Contract month", sort=order,
                axis=alt.Axis(labelAngle=0)),
        y=alt.Y(f"{value}:Q", title=title, axis=alt.Axis(format=axis_format),
                scale=alt.Scale(zero=False)),
        color=alt.Color("label:N", legend=_legend(legend), sort=None),
        tooltip=[alt.Tooltip("label:N", title=legend), alt.Tooltip("contract:N"),
                 alt.Tooltip(f"{value}:Q", format=PRICE_TIP)],
    )
    if value == "change":
        chart += alt.Chart(pd.DataFrame({"y": [0]})).mark_rule(color="gray").encode(y="y:Q")
    return chart
