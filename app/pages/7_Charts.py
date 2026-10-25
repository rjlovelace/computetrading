from datetime import date, timedelta

import pandas as pd
import streamlit as st
from charts import CHANGE_AXIS, curves, study_picker, time_series
from common import flash, setup, table, user
from sqlalchemy import select

from ctrade import analytics, energy, marking, power, services
from ctrade.db import Instrument, Product, session_scope
from ctrade.formatting import number

st.set_page_config(page_title="Charts", layout="wide")
setup()
st.title("Charts")
st.write("Marks over time, term structures, spreads and the energy cost of compute. Compute "
         "is priced in \\$ per GPU-hour and forward power in \\$/MWh.")

with session_scope() as s:
    marked = services.marked_instruments(s)
    futures = s.scalars(select(Product.code).where(~Product.code.startswith("SPOT."))
                        .order_by(Product.code)).all()
    contracts = {analytics.contract_name(i): i.instrument_id for i in s.scalars(
        select(Instrument).where(Instrument.kind == "future")
        .order_by(Instrument.product_code, Instrument.delivery_start))}

history_tab, curve_tab, spread_tab, energy_tab = st.tabs(
    ["Mark history", "Term structure", "Spreads", "Energy cost"])

# --- mark history -----------------------------------------------------------------------
with history_tab:
    if not marked:
        st.info("No marks yet. Load prices on the Market data page, or enter marks on the "
                "Marks page.")
    else:
        c1, c2, c3 = st.columns([3, 1, 1])
        picked = c1.multiselect("Contracts", marked, default=marked[: min(4, len(marked))])
        start = c2.date_input("From", value=date.today() - timedelta(days=90), key="h_from")
        end = c3.date_input("To", value=date.today(), key="h_to")
        studies = study_picker("history")
        if picked:
            with session_scope() as s:
                df = pd.DataFrame([{
                    "date": m.as_of_date, "instrument": m.instrument_id,
                    "mark": float(m.price), "source": m.source,
                } for m in services.mark_history(s, picked, start, end)])
            if df.empty:
                st.info("No marks for these contracts in this date range.")
            else:
                with session_scope() as s:
                    units = {analytics.unit(s, i.rsplit(".", 1)[0]) for i in picked}
                label = f"Mark ({units.pop()})" if len(units) == 1 else "Mark ($)"
                st.altair_chart(time_series(df, "mark", "instrument", studies, label,
                                            "Contract"),
                                width="stretch")
                wide = df.pivot_table(index="date", columns="instrument", values="mark",
                                      aggfunc="last").sort_index(ascending=False).reset_index()
                table(wide, price=picked)

    with st.expander("Add contract months to mark"):
        st.write("Contracts are created when you trade them. Add months here to mark and "
                 "chart them before you trade.")
        c1, c2, c3 = st.columns(3)
        add_products = c1.multiselect("Products", futures)
        first = c2.text_input("First month (YYYY-MM)", value=date.today().strftime("%Y-%m"))
        count = c3.number_input("Months", min_value=1, max_value=36, value=12)
        if st.button("Add contracts and mark them"):
            try:
                with session_scope() as s:
                    made = [i.instrument_id for p in add_products
                            for i in services.ensure_months(s, p, first, int(count))]
                    # Mark the last 30 days from prices already loaded, so there's a history.
                    marking.build_marks_between(s, date.today() - timedelta(days=30),
                                                date.today())
                flash(f"{len(made)} contracts ready, marked for the last 30 days where "
                      "prices exist.")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

# --- term structure ---------------------------------------------------------------------
with curve_tab:
    product = st.selectbox("Product", futures,
                           index=futures.index("CME.GPU1") if "CME.GPU1" in futures else 0)
    with session_scope() as s:
        dates = analytics.curve_dates(s, product)
        unit = analytics.unit(s, product)
    if not dates:
        st.info(f"No marks for {product} yet. Add contract months on the Mark history tab.")
    else:
        picked_dates = st.multiselect(
            "Curve dates", dates, default=dates[:3], format_func=lambda d: d.strftime("%b %d, %Y"),
            help="The latest three marked dates are shown to start. Add or remove dates to "
                 "compare more curves.")
        if picked_dates:
            picked_dates = sorted(picked_dates, reverse=True)
            with session_scope() as s:
                curve = analytics.term_structure(s, product, picked_dates)
                changes = analytics.curve_changes(s, product, picked_dates)
            curve["label"] = curve["date"].map(lambda d: d.strftime("%b %d"))
            st.subheader(f"{product} term structure")
            st.altair_chart(curves(curve, "mark", f"Mark ({unit})", "Curve date"),
                            width="stretch")
            if changes.empty:
                st.info("Day over day changes need a marked date before each curve date.")
            else:
                changes["label"] = [f"{d:%b %d} vs {p:%b %d}"
                                    for d, p in zip(changes["date"], changes["prior"],
                                                    strict=True)]
                st.subheader("Day over day change")
                st.altair_chart(curves(changes, "change", f"Change ({unit})", "Change"),
                                width="stretch")
            wide = curve.pivot_table(index=["month", "contract"], columns="label",
                                     values="mark", aggfunc="last")
            wide = wide[[c for c in dict.fromkeys(curve["label"]) if c in wide.columns]]
            if not changes.empty:
                for lbl, part in changes.groupby("label", sort=False):
                    wide[f"chg {lbl}"] = part.set_index(["month", "contract"])["change"]
            wide = wide.reset_index().drop(columns="month")
            table(wide, price=[c for c in wide.columns if c != "contract"])

# --- spreads ----------------------------------------------------------------------------
with spread_tab:
    with session_scope() as s:
        spreads = {sp.name: (sp.id, analytics.parse_legs(sp.legs))
                   for sp in analytics.list_spreads(s)}
    names = list(spreads)

    with st.expander("Create a spread", expanded=not names):
        st.write("A spread is the first contract minus the second, e.g. CME.GPU2 Jan27 - "
                 "CME.GPU1 Jan27. Change the weights for ratio spreads, or add a third leg "
                 "for a butterfly. Type a contract (e.g. CME.GPU2 Jan27) if it isn't listed.")
        options = list(contracts)
        legs_in = []
        for n, (label, default_w) in enumerate([("First leg", 1.0), ("Second leg", -1.0),
                                                ("Third leg (optional)", 0.0)]):
            c1, c2, c3 = st.columns([2, 2, 1])
            pick = c1.selectbox(label, [""] + options, key=f"leg{n}")
            typed = c2.text_input("or type a contract", key=f"leg{n}_typed")
            w = c3.number_input("Weight", value=default_w, step=1.0, key=f"leg{n}_w")
            if (typed or pick) and w:
                legs_in.append((typed or pick, w))
        name = st.text_input("Name (optional)", help="Defaults to the legs, e.g. "
                             "CME.GPU2 Jan27 - CME.GPU1 Jan27")
        if st.button("Save spread"):
            try:
                with session_scope() as s:
                    sp = analytics.add_spread(s, legs_in, name, user())
                flash(f"Saved spread {sp.name}")
                st.rerun()
            except ValueError as e:
                st.error(str(e))

    if names:
        c1, c2, c3 = st.columns([3, 1, 1])
        chosen = c1.multiselect("Spreads", names, default=names[:1])
        start = c2.date_input("From", value=date.today() - timedelta(days=90), key="s_from")
        end = c3.date_input("To", value=date.today(), key="s_to")
        studies = study_picker("spread")
        frames = []
        with session_scope() as s:
            for nm in chosen:
                hist = analytics.spread_history(s, spreads[nm][1], start, end)
                frames.append(hist.assign(spread=nm))
        df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        if chosen and df.empty:
            st.info("No dates in this range where every leg has a mark.")
        elif chosen:
            st.subheader("Spread")
            st.altair_chart(time_series(df, "value", "spread", studies,
                                        "Spread ($ per unit)", "Spread"), width="stretch")
            st.subheader("Daily change")
            chg = df.dropna(subset=["change"])
            if chg.empty:
                st.info("Daily changes need at least two dates.")
            else:
                st.altair_chart(time_series(chg, "change", "spread", studies,
                                            "Daily change ($ per unit)", "Spread",
                                            bars=len(chosen) == 1,
                                            axis_format=CHANGE_AXIS), width="stretch")
            table(df.sort_values(["date", "spread"], ascending=[False, True])
                  [["date", "spread", "value", "change"]], price=["value", "change"])

        with st.expander("Remove a spread"):
            gone = st.selectbox("Spread", names, key="rm_spread")
            if st.button("Remove spread"):
                with session_scope() as s:
                    analytics.remove_spread(s, spreads[gone][0])
                flash(f"Removed {gone}")
                st.rerun()

# --- energy cost --------------------------------------------------------------------------
with energy_tab:
    with session_scope() as s:
        power_products = [sp.product_code for sp in power.specs(s)]
        compute_products = [p for p in futures if p not in power_products]
    if not power_products:
        st.info("Load forward power curves on the Power page to see the energy cost of "
                "compute.")
    else:
        st.write("What the electricity behind each GPU-hour costs at forward power prices: "
                 "power $/MWh x kW per GPU x PUE / 1,000. A GPU runs around the clock, so "
                 "an ATC curve is the usual choice. Set kW per GPU and PUE on the Power page.")
        c1, c2, c3 = st.columns([1, 2, 1])
        compute_product = c1.selectbox("Compute product", compute_products,
                                       index=compute_products.index("CME.GPU1")
                                       if "CME.GPU1" in compute_products else 0)
        atc = [p for p in power_products if p.endswith(".ATC")]
        power_product = c2.selectbox("Power curve", power_products,
                                     index=power_products.index(atc[0]) if atc else 0)
        with session_scope() as s:
            dates = analytics.curve_dates(s, compute_product)
        if not dates:
            st.info(f"No marks for {compute_product} yet.")
        else:
            as_of = c3.selectbox("Date", dates, format_func=lambda d: d.strftime("%b %d, %Y"))
            try:
                with session_scope() as s:
                    f = energy.factor(s, s.get(Product, compute_product).gpu_model)
                    df = energy.cost_curve(s, compute_product, power_product, as_of)
            except ValueError as e:
                st.warning(str(e))
                df = None
            if df is not None:
                st.caption(f"{f.gpu_model}: {number(f.kw_per_gpu)} kW per GPU x PUE "
                           f"{number(f.pue)} = {number(f.mwh_per_gpu_hour * 1000)} kWh per "
                           f"GPU-hour ({f.comment or 'set on the Power page'}).")
                if df.empty:
                    st.info(f"{power_product} has no prices for {compute_product}'s contract "
                            f"months on {as_of}.")
                else:
                    long = df.melt(id_vars=["month", "contract"],
                                   value_vars=["compute", "energy", "margin"],
                                   var_name="label", value_name="price")
                    long["label"] = long["label"].map({
                        "compute": f"{compute_product} mark", "energy": "Energy cost",
                        "margin": "Margin (mark - energy)"})
                    st.altair_chart(curves(long, "price", "$ per GPU-hour", "Line"),
                                    width="stretch")
                    table(df.drop(columns="month").rename(columns={
                        "compute": "compute mark", "power": "power $/MWh",
                        "energy": "energy $/GPU-hr", "margin": "margin $/GPU-hr",
                        "hours": "GPU-hours", "energy_per_contract": "energy $/contract",
                        "margin_per_contract": "margin $/contract"}),
                          price=["compute mark", "energy $/GPU-hr", "margin $/GPU-hr"],
                          money=["power $/MWh", "energy $/contract", "margin $/contract"],
                          qty=["GPU-hours"])
