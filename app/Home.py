import streamlit as st
from common import setup

from ctrade.db import database_url

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
2. **Marks**: enter today's settlement price for every open instrument.
3. **P&L**: run the daily P&L and check there are no warnings.
"""
)
url = database_url()
st.caption(f"Database: {url.split('@')[-1] if '@' in url else url}")
