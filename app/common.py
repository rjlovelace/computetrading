"""Shared helpers for the Streamlit pages."""

from __future__ import annotations

import getpass

import streamlit as st

from ctrade import services
from ctrade.db import init_db, session_scope


@st.cache_resource
def _setup() -> bool:
    init_db()
    with session_scope() as s:
        services.seed_products(s)
    return True


def setup() -> None:
    _setup()


def user() -> str:
    return getpass.getuser()
