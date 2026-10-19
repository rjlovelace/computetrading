from datetime import date
from decimal import Decimal

import pandas as pd
import streamlit as st
from common import setup, table, user
from sqlalchemy import select

from ctrade import services
from ctrade.db import Product, session_scope
from ctrade.formatting import number
from ctrade.formatting import price as fmt_price

st.set_page_config(page_title="Trades", layout="wide")
setup()
st.title("Trades")

with session_scope() as s:
    products = [p.code for p in s.scalars(select(Product).order_by(Product.code))
                if not p.code.startswith("SPOT.")]

kind = st.radio("Contract type", ["Future", "Spot block"], horizontal=True)
with st.form("book"):
    c1, c2, c3, c4 = st.columns(4)
    if kind == "Future":
        product = c1.selectbox("Product", products)
        month = c2.text_input("Contract month (YYYY-MM)", value=date.today().strftime("%Y-%m"))
    else:
        gpu = c1.text_input("GPU model", value="H100")
        start = c2.date_input("Delivery start")
        end = c2.date_input("Delivery end")
        venue = c1.text_input("Venue", value="OTC")
    side = c3.selectbox("Side", ["buy", "sell"])
    qty = c3.number_input("Contracts", min_value=0.0, step=1.0)
    price = c4.text_input("Price ($/GPU-hr)")
    trade_date = c4.date_input("Trade date")
    c5, c6, c7 = st.columns(3)
    book = c5.text_input("Book", value="MAIN")
    fees = c6.text_input("Fees ($)", value="0")
    cpty = c7.text_input("Counterparty / broker")
    ref = c5.text_input("External ref")
    comment = c6.text_input("Comment")
    if st.form_submit_button("Book trade"):
        try:
            with session_scope() as s:
                if kind == "Future":
                    inst = services.ensure_future(s, product, month)
                else:
                    inst = services.ensure_spot(s, gpu, start, end, venue)
                t = services.book_trade(
                    s, instrument_id=inst.instrument_id, side=side,
                    quantity=Decimal(str(qty)), price=Decimal(price), trade_date=trade_date,
                    book=book, fees=Decimal(fees), counterparty=cpty, external_ref=ref,
                    comment=comment, entered_by=user(),
                )
            st.success(f"Booked trade {t.trade_id}: {t.side} {number(t.quantity)} "
                       f"{t.instrument_id} at {fmt_price(t.price)}")
        except Exception as e:  # show validation errors in the page
            st.error(str(e))

st.subheader("Live trades")
with session_scope() as s:
    trades = services.live_trades(s)
    df = pd.DataFrame([{
        "id": t.trade_id, "v": t.version, "date": t.trade_date, "book": t.book,
        "instrument": t.instrument_id, "side": t.side, "qty": float(t.quantity),
        "price": float(t.price), "fees": float(t.fees), "cpty": t.counterparty,
        "ref": t.external_ref, "comment": t.comment,
    } for t in trades])
table(df, money=["fees"], price=["price"], qty=["qty"])

with st.expander("Amend or cancel a trade"):
    tid = st.number_input("Trade id", min_value=1, step=1)
    c1, c2, c3 = st.columns(3)
    new_price = c1.text_input("New price (blank = unchanged)")
    new_qty = c2.text_input("New quantity (blank = unchanged)")
    reason = c3.text_input("Reason")
    a, b = st.columns(2)
    if a.button("Amend"):
        changes = {}
        if new_price:
            changes["price"] = Decimal(new_price)
        if new_qty:
            changes["quantity"] = Decimal(new_qty)
        if reason:
            changes["comment"] = reason
        try:
            with session_scope() as s:
                t = services.amend_trade(s, int(tid), entered_by=user(), **changes)
            st.success(f"Trade {t.trade_id} now version {t.version}")
        except Exception as e:
            st.error(str(e))
    if b.button("Cancel trade"):
        try:
            with session_scope() as s:
                services.cancel_trade(s, int(tid), user(), reason)
            st.success(f"Trade {int(tid)} cancelled")
        except Exception as e:
            st.error(str(e))
