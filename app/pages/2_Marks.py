from decimal import Decimal

import pandas as pd
import streamlit as st
from common import setup, user
from sqlalchemy import select

from ctrade import services
from ctrade.db import Mark, session_scope

st.set_page_config(page_title="Marks", layout="wide")
setup()
st.title("Marks")
st.write("End-of-day marks in $ per GPU-hour. Use exchange settlement prices where available.")

as_of = st.date_input("Mark date")
with session_scope() as s:
    open_ids = sorted({ln.instrument_id for ln in services.positions(s, as_of)
                       if ln.quantity != 0})
    existing = services.mark_map(s, [as_of])

if open_ids:
    st.subheader("Open instruments")
    df = pd.DataFrame({
        "instrument": open_ids,
        "mark": [float(existing[(i, as_of)]) if (i, as_of) in existing else None
                 for i in open_ids],
    })
    edited = st.data_editor(df, disabled=["instrument"], hide_index=True,
                            width="stretch")
    source = st.text_input("Source", value="exchange settlement")
    if st.button("Save marks"):
        n = 0
        with session_scope() as s:
            for row in edited.itertuples():
                if pd.notna(row.mark):
                    old = existing.get((row.instrument, as_of))
                    new = Decimal(str(row.mark))
                    if old is None or old != new:
                        services.set_mark(s, row.instrument, as_of, new, source=source,
                                          entered_by=user())
                        n += 1
        st.success(f"Saved {n} marks")
else:
    st.info("No open positions on this date.")

with st.expander("Mark any instrument"):
    iid = st.text_input("Instrument id, e.g. ICE.HPR.2026-12")
    price = st.text_input("Price")
    if st.button("Save"):
        try:
            with session_scope() as s:
                services.set_mark(s, iid, as_of, Decimal(price), entered_by=user())
            st.success("Saved")
        except Exception as e:
            st.error(str(e))

st.subheader("Mark history for this date")
with session_scope() as s:
    rows = s.scalars(select(Mark).where(Mark.as_of_date == as_of)
                     .order_by(Mark.instrument_id, Mark.recorded_at)).all()
    st.dataframe(pd.DataFrame([{
        "instrument": m.instrument_id, "price": float(m.price), "source": m.source,
        "by": m.entered_by, "recorded": m.recorded_at,
    } for m in rows]), hide_index=True, width="stretch")
