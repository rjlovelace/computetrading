from datetime import date, timedelta

import altair as alt
import pandas as pd
import streamlit as st
from common import flash, setup, table
from sqlalchemy import select

from ctrade import marking, services
from ctrade.db import Product, session_scope

st.set_page_config(page_title="Mark history", layout="wide")
setup()
st.title("Mark history")
st.write("Each contract's end-of-day mark over time, in $ per GPU-hour.")

with session_scope() as s:
    marked = services.marked_instruments(s)
    products = s.scalars(select(Product.code).where(~Product.code.startswith("SPOT."))
                         .order_by(Product.code)).all()

if not marked:
    st.info("No marks yet. Load prices on the Market data page, or enter marks on the "
            "Marks page.")
else:
    c1, c2, c3 = st.columns([3, 1, 1])
    picked = c1.multiselect("Contracts", marked, default=marked[: min(4, len(marked))])
    start = c2.date_input("From", value=date.today() - timedelta(days=90))
    end = c3.date_input("To", value=date.today())
    if picked:
        with session_scope() as s:
            rows = services.mark_history(s, picked, start, end)
            df = pd.DataFrame([{
                "date": m.as_of_date, "instrument": m.instrument_id, "mark": float(m.price),
                "source": m.source,
            } for m in rows])
        if df.empty:
            st.info("No marks for these contracts in this date range.")
        else:
            chart_df = df.assign(date=pd.to_datetime(df["date"]))
            st.altair_chart(
                alt.Chart(chart_df).mark_line(point=True).encode(
                    x=alt.X("date:T", title=None),
                    y=alt.Y("mark:Q", title="Mark ($ per GPU-hour)",
                            axis=alt.Axis(format="$,.2f"), scale=alt.Scale(zero=False)),
                    color=alt.Color("instrument:N", title="Contract"),
                    tooltip=[alt.Tooltip("date:T"), "instrument:N",
                             alt.Tooltip("mark:Q", format="$,.4f"), "source:N"],
                ),
                width="stretch",
            )
            wide = df.pivot_table(index="date", columns="instrument", values="mark",
                                  aggfunc="last").sort_index(ascending=False).reset_index()
            table(wide, price=picked)

with st.expander("Add contract months to mark"):
    st.write("Contracts are created when you trade them. Add months here to mark and chart "
             "them before you trade.")
    c1, c2, c3 = st.columns(3)
    add_products = c1.multiselect("Products", products)
    first = c2.text_input("First month (YYYY-MM)", value=date.today().strftime("%Y-%m"))
    count = c3.number_input("Months", min_value=1, max_value=36, value=12)
    if st.button("Add contracts and mark them"):
        try:
            with session_scope() as s:
                made = [i.instrument_id for p in add_products
                        for i in services.ensure_months(s, p, first, int(count))]
                # Mark the last 30 days from the prices already loaded, so there is a history.
                marking.build_marks_between(s, date.today() - timedelta(days=30), date.today())
            flash(f"{len(made)} contracts ready, marked for the last 30 days where prices "
                  "exist.")
            st.rerun()
        except ValueError as e:
            st.error(str(e))
