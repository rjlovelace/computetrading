import pandas as pd
import streamlit as st
from common import setup
from sqlalchemy import select

from ctrade.db import Product, session_scope

st.set_page_config(page_title="Products", layout="wide")
setup()
st.title("Products")
st.write("Contracts marked provisional have specs the exchange has not yet published.")
with session_scope() as s:
    rows = s.scalars(select(Product).order_by(Product.code)).all()
    st.dataframe(pd.DataFrame([{
        "code": p.code, "exchange": p.exchange, "name": p.name, "gpu": p.gpu_model,
        "index": p.index_name, "hours rule": p.hours_rule, "fixed hours": p.fixed_hours,
        "tick": float(p.tick_size), "provisional": p.provisional, "notes": p.notes,
    } for p in rows]), hide_index=True, width="stretch")
