import pandas as pd
import streamlit as st
from common import setup

from ctrade import services
from ctrade.db import session_scope

st.set_page_config(page_title="Daily P&L", layout="wide")
setup()
st.title("Daily P&L")

as_of = st.date_input("P&L date")
if st.button("Run P&L"):
    with session_scope() as s:
        r = services.run_daily_pnl(s, as_of)
    for e in r.errors:
        st.warning(e)
    if r.errors:
        st.error("This run was not saved. Fix the warnings above and run again.")
    c1, c2 = st.columns(2)
    c1.metric(f"Daily P&L ({r.as_of} vs {r.prior})", f"${r.total_daily:,.2f}")
    c2.metric("Cumulative P&L", f"${r.total_cumulative:,.2f}")
    df = pd.DataFrame([{
        "book": ln.book, "instrument": ln.instrument_id, "contracts": float(ln.quantity),
        "mark": None if ln.mark is None else float(ln.mark), "daily $": float(ln.daily),
        "new trades $": float(ln.new_trades), "price move $": float(ln.price_move),
        "fees $": float(ln.fees_today),
        "amendments $": float(ln.amendments), "residual $": float(ln.residual),
        "realized $": float(ln.realized), "unrealized $": float(ln.unrealized),
        "cumulative $": float(ln.cumulative), "expired": ln.expired,
    } for ln in r.lines])
    st.dataframe(df, hide_index=True, width="stretch")
    if not df.empty:
        st.subheader("By book")
        st.dataframe(df.groupby("book")[["daily $", "cumulative $"]].sum(),
                     width="stretch")
