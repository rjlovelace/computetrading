from decimal import Decimal

import pandas as pd
import streamlit as st
from common import column_config, flash, setup, table, user
from sqlalchemy import select

from ctrade import marking, services
from ctrade.db import Instrument, Mark, Product, session_scope
from ctrade.formatting import adjustment

st.set_page_config(page_title="Marks", layout="wide")
setup()
st.title("Marks")
st.write(
    "End-of-day marks in $ per GPU-hour. Contracts are marked automatically when prices "
    "load; a mark you enter here always wins over the automatic one."
)

as_of = st.date_input("Mark date")
with session_scope() as s:
    live_ids = s.scalars(select(Instrument.instrument_id)
                         .where(Instrument.last_trade_date >= as_of)
                         .order_by(Instrument.instrument_id)).all()
    latest = {}
    for m in s.scalars(select(Mark).where(Mark.as_of_date == as_of)
                       .order_by(Mark.recorded_at, Mark.id)):
        latest[m.instrument_id] = m

if live_ids:
    st.subheader("Contracts")
    df = pd.DataFrame({
        "instrument": live_ids,
        "mark": [float(latest[i].price) if i in latest else None for i in live_ids],
        "source": [latest[i].source if i in latest else "" for i in live_ids],
    })
    edited = st.data_editor(df, disabled=["instrument", "source"], hide_index=True,
                            width="stretch", column_config=column_config(price=["mark"]))
    source = st.text_input("Source for marks you enter", value="exchange settlement")
    if st.button("Save marks"):
        n = 0
        with session_scope() as s:
            for row in edited.itertuples():
                if pd.notna(row.mark):
                    old = latest.get(row.instrument)
                    new = Decimal(str(row.mark))
                    if old is None or Decimal(old.price) != new:
                        services.set_mark(s, row.instrument, as_of, new, source=source,
                                          entered_by=user())
                        n += 1
            # Contracts proxied off the ones just marked follow them.
            marking.build_marks(s, as_of)
        flash(f"Saved {n} marks")
        st.rerun()
else:
    st.info("No live contracts on this date. Book a trade or add contract months on the "
            "Charts page.")

st.subheader("Proxy marks")
st.write(
    "When a contract has no price of its own (no settlement and no index), its mark is taken "
    "from the first rule below that has a price. For example, mark Nodal H100 off the CME "
    "GPU1 contract for the same month, which follows Silicon Data's H100 index."
)
with session_scope() as s:
    rules = marking.list_proxies(s)
    products = s.scalars(select(Product.code).order_by(Product.code)).all()
    series = marking.known_series(s)
if rules:
    table(pd.DataFrame([{
        "id": r.id, "contract": r.target, "marked from": f"{r.source_kind}: {r.source}",
        "adjustment": adjustment(r.multiplier, r.spread) or "none",
        "priority": r.priority, "comment": r.comment,
    } for r in rules]))
else:
    st.caption("No proxy rules yet.")

with st.form("add_proxy"):
    c1, c2, c3 = st.columns(3)
    target = c1.selectbox("Contract to mark", products,
                          index=products.index("NODAL.H100") if "NODAL.H100" in products else 0,
                          help="Every month of this product. Type an instrument id below "
                               "to set a rule for one contract only.")
    target_one = c1.text_input("Or one instrument id (optional)")
    kind = c2.radio("Mark it from", ["Another product, same month", "A price series",
                                     "A specific instrument"])
    src_product = c2.selectbox("Product", products,
                               index=products.index("CME.GPU1") if "CME.GPU1" in products else 0)
    src_series = c2.selectbox("Series", series)
    src_inst = c2.text_input("Instrument id")
    mult = c3.text_input("Multiplier", value="1")
    spread = c3.text_input("Spread ($ per GPU-hour, added)", value="0")
    priority = c3.number_input("Priority (lower is tried first)", min_value=1, value=1)
    comment = st.text_input("Comment")
    if st.form_submit_button("Add rule"):
        source = {"Another product, same month": src_product,
                  "A price series": marking.SERIES_PREFIX + (src_series or ""),
                  "A specific instrument": src_inst}[kind]
        try:
            with session_scope() as s:
                marking.add_proxy(s, target_one or target, source, mult, spread,
                                  int(priority), comment, user())
                marking.build_marks(s, as_of)
            flash("Rule added, and marks rebuilt for this date.")
            st.rerun()
        except ValueError as e:
            st.error(str(e))

if rules:
    with st.expander("Remove a rule"):
        rid = st.selectbox("Rule id", [r.id for r in rules])
        if st.button("Remove"):
            with session_scope() as s:
                marking.remove_proxy(s, int(rid))
            flash(f"Removed rule {rid}")
            st.rerun()

st.subheader("Mark history for this date")
with session_scope() as s:
    rows = s.scalars(select(Mark).where(Mark.as_of_date == as_of)
                     .order_by(Mark.instrument_id, Mark.recorded_at)).all()
    table(pd.DataFrame([{
        "instrument": m.instrument_id, "price": float(m.price), "source": m.source,
        "note": m.comment, "by": m.entered_by, "recorded": m.recorded_at,
    } for m in rows]), price=["price"])
