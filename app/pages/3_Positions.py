import pandas as pd
import streamlit as st
from common import setup

from ctrade import services
from ctrade.db import session_scope

st.set_page_config(page_title="Positions", layout="wide")
setup()
st.title("Positions")

as_of = st.date_input("As of")
with session_scope() as s:
    lines = services.positions(s, as_of)
open_lines = [ln for ln in lines if ln.quantity != 0]
if not open_lines:
    st.info("No open positions.")
else:
    df = pd.DataFrame([{
        "book": ln.book, "instrument": ln.instrument_id, "contracts": float(ln.quantity),
        "avg price": float(ln.avg_price),
        "mark": None if ln.mark is None else float(ln.mark),
        "unrealized $": float(ln.unrealized), "realized $": float(ln.realized),
    } for ln in open_lines])
    st.dataframe(df, hide_index=True, width="stretch")
    missing = [ln.instrument_id for ln in open_lines if ln.mark is None]
    if missing:
        st.warning("Missing marks: " + ", ".join(missing))
