import pandas as pd
import streamlit as st
from common import setup
from sqlalchemy import select

from ctrade import agents, marking
from ctrade.db import AgentRun, MarketObservation, session_scope

st.set_page_config(page_title="Market data", layout="wide")
setup()
st.title("Market data")
st.write(
    "Agents collect GPU index prices from public sources. **Build marks** turns them into "
    "end-of-day marks for every traded instrument. Marks you enter by hand are never replaced."
)

as_of = st.date_input("Date")
c1, c2, c3 = st.columns(3)
backfill = c3.number_input("Days of history to fetch", min_value=0, max_value=90, value=0)
if c1.button("Run agents"):
    with session_scope() as s:
        runs = agents.run_agents(s, as_of, backfill_days=int(backfill))
        for r in runs:
            msg = f"**{r.agent}**: {r.status}. {r.message}"
            {"ok": st.success, "skipped": st.info, "failed": st.error}[r.status](msg)
if c2.button("Build marks"):
    with session_scope() as s:
        res = marking.build_marks(s, as_of)
    if not res:
        st.info("No traded instruments are live on this date.")
    for r in res:
        if r.status == "no-source":
            st.warning(f"{r.instrument_id}: no mark. {r.note}")
    st.dataframe(pd.DataFrame([{
        "instrument": r.instrument_id, "status": r.status,
        "mark": None if r.price is None else float(r.price), "method": r.method, "note": r.note,
    } for r in res]), hide_index=True, width="stretch")

st.subheader("Agents")
st.dataframe(pd.DataFrame([{"agent": a.name, "source": a.description}
                           for a in agents.all_agents()]), hide_index=True, width="stretch")

st.subheader("Latest prices")
with session_scope() as s:
    latest = agents.latest_observations(s, as_of)
    st.dataframe(pd.DataFrame([{
        "series": o.series, "date": o.obs_date, "value": float(o.value), "unit": o.unit,
        "agent": o.agent, "method": o.method, "evidence": o.detail or o.source_url,
    } for o in latest]), hide_index=True, width="stretch")

    st.subheader("Recent agent runs")
    runs = s.scalars(select(AgentRun).order_by(AgentRun.id.desc()).limit(30)).all()
    st.dataframe(pd.DataFrame([{
        "agent": r.agent, "date": r.as_of_date, "status": r.status, "new": r.observations,
        "message": r.message, "finished": r.finished_at,
    } for r in runs]), hide_index=True, width="stretch")

    with st.expander("Price history"):
        series = sorted({o.series for o in latest})
        if series:
            pick = st.selectbox("Series", series)
            rows = s.scalars(select(MarketObservation).where(MarketObservation.series == pick)
                             .order_by(MarketObservation.obs_date)).all()
            hist = pd.DataFrame([{"date": r.obs_date, "value": float(r.value)} for r in rows])
            hist = hist.drop_duplicates("date", keep="last").set_index("date")
            st.line_chart(hist)
