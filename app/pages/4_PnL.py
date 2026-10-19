import pandas as pd
import streamlit as st
from common import column_config, setup, table

from ctrade import services
from ctrade.db import session_scope
from ctrade.formatting import money

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
    c1.metric(f"Daily P&L ({r.as_of} vs {r.prior})", money(r.total_daily))
    c2.metric("Cumulative P&L", money(r.total_cumulative))
    df = pd.DataFrame([{
        "book": ln.book, "instrument": ln.instrument_id, "contracts": float(ln.quantity),
        "mark": None if ln.mark is None else float(ln.mark), "daily": float(ln.daily),
        "new trades": float(ln.new_trades), "price move": float(ln.price_move),
        "fees": float(ln.fees_today),
        "amendments": float(ln.amendments), "residual": float(ln.residual),
        "realized": float(ln.realized), "unrealized": float(ln.unrealized),
        "cumulative": float(ln.cumulative), "expired": ln.expired,
    } for ln in r.lines])
    money_cols = ["daily", "new trades", "price move", "fees", "amendments", "residual",
                  "realized", "unrealized", "cumulative"]
    table(df, money=money_cols, price=["mark"], qty=["contracts"])
    if not df.empty:
        st.subheader("By book")
        st.dataframe(df.groupby("book")[["daily", "cumulative"]].sum(), width="stretch",
                     column_config=column_config(money=["daily", "cumulative"]))
