import altair as alt
import pandas as pd
import streamlit as st
from common import setup, table
from sqlalchemy import select

from ctrade import agents, marking
from ctrade.db import AgentRun, MarketObservation, session_scope
from ctrade.formatting import number

st.set_page_config(page_title="Market data", layout="wide")
setup()
st.title("Market data")
st.write(
    "Agents collect GPU index prices from public sources. Loading prices marks every contract "
    "automatically, for each day that got new prices. Marks you enter by hand are never "
    "replaced."
)


def show_marks(res) -> None:
    for r in res:
        if r.status == "no-source":
            st.warning(f"{r.instrument_id}: no mark. {r.note}")
    table(pd.DataFrame([{
        "instrument": r.instrument_id, "status": r.status,
        "mark": None if r.price is None else float(r.price), "method": r.method, "note": r.note,
    } for r in res]), price=["mark"])


as_of = st.date_input("Date")
c1, c2, c3 = st.columns(3)
backfill = c3.number_input("Days of history to fetch", min_value=0, max_value=90, value=0)
if c1.button("Load prices and mark"):
    with session_scope() as s:
        loaded = marking.load_prices(s, as_of, backfill_days=int(backfill))
        for r in loaded.runs:
            msg = f"**{r.agent}**: {r.status}. {r.message}"
            {"ok": st.success, "skipped": st.info, "failed": st.error}[r.status](msg)
    days = loaded.dates_marked
    st.write(f"Marked {number(len(days))} day(s), {days[0]} to {days[-1]}. Marks for {as_of}:")
    if not loaded.marks:
        st.info("No contracts are live on this date.")
    show_marks(loaded.marks)
if c2.button("Rebuild marks only"):
    with session_scope() as s:
        res = marking.build_marks(s, as_of)
    if not res:
        st.info("No contracts are live on this date.")
    show_marks(res)

st.subheader("Agents")
table(pd.DataFrame([{"agent": a.name, "source": a.description} for a in agents.all_agents()]))

st.subheader("Latest prices")
with session_scope() as s:
    latest = agents.latest_observations(s, as_of)
    table(pd.DataFrame([{
        "series": o.series, "date": o.obs_date, "value": float(o.value), "unit": o.unit,
        "agent": o.agent, "method": o.method, "evidence": o.detail or o.source_url,
    } for o in latest]), price=["value"])

    st.subheader("Recent agent runs")
    runs = s.scalars(select(AgentRun).order_by(AgentRun.id.desc()).limit(30)).all()
    table(pd.DataFrame([{
        "agent": r.agent, "date": r.as_of_date, "status": r.status, "new": r.observations,
        "message": r.message, "finished": r.finished_at,
    } for r in runs]), qty=["new"])

    with st.expander("Price history"):
        series = sorted({o.series for o in latest})
        if series:
            pick = st.selectbox("Series", series)
            rows = s.scalars(select(MarketObservation).where(MarketObservation.series == pick)
                             .order_by(MarketObservation.obs_date, MarketObservation.id)).all()
            hist = pd.DataFrame([{"date": r.obs_date, "value": float(r.value)} for r in rows])
            hist = hist.drop_duplicates("date", keep="last")
            hist["date"] = pd.to_datetime(hist["date"])
            st.altair_chart(
                alt.Chart(hist).mark_line(point=True).encode(
                    x=alt.X("date:T", title=None),
                    y=alt.Y("value:Q", title="$ per GPU-hour", axis=alt.Axis(format="$,.2f"),
                            scale=alt.Scale(zero=False)),
                    tooltip=[alt.Tooltip("date:T"), alt.Tooltip("value:Q", format="$,.4f")],
                ),
                width="stretch",
            )
