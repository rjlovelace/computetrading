"""Shared helpers for the Streamlit pages."""

from __future__ import annotations

import getpass

import pandas as pd
import streamlit as st

from ctrade import services
from ctrade.db import DatabaseUnavailable, init_db, session_scope

# Column formats: $ and thousands separators on every screen.
MONEY_FMT = "dollar"  # $1,234.57, negatives as -$1,234.57
PRICE_FMT = "$%,.4f"  # $ per GPU-hour or $/MWh
QTY_FMT = "localized"  # 1,234.5


@st.cache_resource
def _setup() -> bool:
    init_db()
    with session_scope() as s:
        services.seed_products(s)
    return True


def setup() -> None:
    try:
        _setup()
    except DatabaseUnavailable as e:
        st.error(str(e))
        st.stop()
    show_flash()


def flash(message: str) -> None:
    """Show a success message after the next rerun."""
    st.session_state["_flash"] = message


def show_flash() -> None:
    if msg := st.session_state.pop("_flash", None):
        st.success(msg)


def user() -> str:
    return getpass.getuser()


def column_config(money=(), price=(), qty=()) -> dict:
    cfg = {}
    for col in money:
        cfg[col] = st.column_config.NumberColumn(col, format=MONEY_FMT)
    for col in price:
        cfg[col] = st.column_config.NumberColumn(col, format=PRICE_FMT)
    for col in qty:
        cfg[col] = st.column_config.NumberColumn(col, format=QTY_FMT)
    return cfg


def table(df: pd.DataFrame, money=(), price=(), qty=(), **kwargs) -> None:
    """A full-width table with $ and thousands formats on the named columns."""
    st.dataframe(df, hide_index=kwargs.pop("hide_index", True), width="stretch",
                 column_config=column_config(money, price, qty), **kwargs)
