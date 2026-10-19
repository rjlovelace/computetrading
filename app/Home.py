import streamlit as st
from common import setup
from sqlalchemy import func, select

from ctrade.db import Instrument, Mark, MarketObservation, Trade, describe_url, session_scope
from ctrade.formatting import number

st.set_page_config(page_title="Compute Trading", layout="wide")
setup()

st.title("Compute Trading")
st.write(
    "Trades, positions, marks and daily P&L for GPU compute futures (CME, ICE, Nodal) "
    "and OTC spot rentals. Use the pages in the sidebar."
)
st.markdown(
    """
**Daily routine**
1. **Trades**: book today's trades (or import a broker CSV).
2. **Market data**: load today's prices. Every contract is marked automatically.
3. **Marks**: check the marks, enter any by hand, and set up proxy marks.
4. **P&L**: run the daily P&L and check there are no warnings.
"""
)

st.subheader("Database")
with session_scope() as s:
    counts = {
        "Trades": s.scalar(select(func.count(func.distinct(Trade.trade_id)))),
        "Contracts": s.scalar(select(func.count()).select_from(Instrument)),
        "Marks": s.scalar(select(func.count()).select_from(Mark)),
        "Prices loaded": s.scalar(select(func.count()).select_from(MarketObservation)),
    }
for col, (label, n) in zip(st.columns(len(counts)), counts.items(), strict=True):
    col.metric(label, number(n or 0))
st.caption(f"Stored in the {describe_url()}. Nothing is ever deleted: corrections are kept "
           "as new versions.")
