from datetime import date

import pandas as pd
import streamlit as st
from common import flash, setup, table, user
from sqlalchemy import func, select

from ctrade import energy, power
from ctrade.db import Instrument, Mark, session_scope
from ctrade.formatting import number

st.set_page_config(page_title="Power", layout="wide")
setup()
st.title("Forward power")
st.write(
    "Forward power curves for each ISO's hubs and zones: on-peak, off-peak and "
    "around-the-clock (ATC), day-ahead or real-time, physical or financial. Prices are $/MWh "
    "and a contract is 1 MW for every hour of its time of use in the month. There's no free "
    "public source for forward power prices, so curves are loaded from CSV files."
)

st.subheader("Load prices")
st.markdown(
    "One row per price. Columns: **trade_date** (the curve date), **iso** (PJM, MISO, "
    "ERCOT), **contract_month** (2027-01 or Jan27), **settlement_point** (e.g. Western Hub, "
    "HB_NORTH), **point_type** (hub or zone, optional), **settlement** (Day-Ahead or "
    "Real-Time), **delivery** (Physical or Financial), **time_of_use** (onpeak, offpeak or "
    "ATC) and **price** ($/MWh). Loading marks every contract for each date in the file; "
    "marks you enter by hand are kept."
)
c1, c2 = st.columns([3, 1])
upload = c1.file_uploader("Forward curve CSV", type=["csv"])
c2.download_button("Download a template", power.TEMPLATE, "power_curves_template.csv",
                   "text/csv")
if upload is not None and st.button("Load file"):
    try:
        with session_scope() as s:
            res = power.load_curves(s, upload.getvalue().decode("utf-8-sig"), upload.name)
        flash(f"Loaded {number(res.rows)} prices ({number(res.new_prices)} new) for "
              f"{len(res.products)} curves on {len(res.dates)} date(s), and rebuilt marks.")
        st.rerun()
    except (ValueError, UnicodeDecodeError) as e:
        st.error(f"Nothing was loaded. {e}")

st.subheader("Curves loaded")
with session_scope() as s:
    specs = power.specs(s)
    stats = {code: (n, first, last) for code, n, first, last in s.execute(
        select(Instrument.product_code, func.count(func.distinct(Instrument.instrument_id)),
               func.min(Instrument.delivery_start), func.max(Instrument.delivery_start))
        .where(Instrument.product_code.in_([sp.product_code for sp in specs]))
        .group_by(Instrument.product_code))}
    latest = dict(s.execute(
        select(Instrument.product_code, func.max(Mark.as_of_date))
        .join(Instrument, Mark.instrument_id == Instrument.instrument_id)
        .group_by(Instrument.product_code)).all())
if not specs:
    st.info("No power curves yet. Download the template, fill it in and load it above.")
else:
    table(pd.DataFrame([{
        "product": sp.product_code, "iso": sp.iso, "settlement point": sp.settlement_point,
        "type": sp.point_type, "settlement": sp.market, "delivery": sp.delivery,
        "time of use": power.TOU_NAMES[sp.time_of_use],
        "months": stats.get(sp.product_code, (0,))[0],
        "first month": f"{stats[sp.product_code][1]:%b%y}" if sp.product_code in stats else "",
        "last month": f"{stats[sp.product_code][2]:%b%y}" if sp.product_code in stats else "",
        "latest price date": latest.get(sp.product_code),
    } for sp in specs]), qty=["months"])
    st.caption("Chart them on the Charts page: Term structure for curves, Spreads for "
               "on-peak vs off-peak, hub vs zone and so on, and Energy cost for compute.")

st.subheader("Energy per GPU")
st.write(
    "Used for the energy cost of compute contracts: kW per GPU is the server's power draw "
    "divided by its GPUs, and PUE adds the data centre's cooling and losses. None of the "
    "compute contract specs publish a factor, so the defaults come from NVIDIA DGX system "
    "maximum power. Edit the kW per GPU or PUE columns and press Save."
)
with session_scope() as s:
    factors = energy.factors(s)
fdf = pd.DataFrame([{
    "GPU": f.gpu_model, "kW per GPU": float(f.kw_per_gpu), "PUE": float(f.pue),
    "kWh per GPU-hour": float(f.mwh_per_gpu_hour * 1000), "source": f.comment,
} for f in factors])
edited = st.data_editor(fdf, hide_index=True, width="stretch", num_rows="dynamic",
                        disabled=["kWh per GPU-hour", "source"])
if st.button("Save energy factors"):
    try:
        with session_scope() as s:
            before = {f.gpu_model: f for f in factors}
            for row in edited.itertuples(index=False):
                gpu = str(row.GPU or "").strip().upper()
                if not gpu:
                    continue
                old = before.get(gpu)
                kw, pue = row[1], row[2]
                if old is None or float(old.kw_per_gpu) != kw or float(old.pue) != pue:
                    energy.set_factor(s, gpu, kw, pue, "entered by hand", user())
        flash("Energy factors saved.")
        st.rerun()
    except (ValueError, TypeError) as e:
        st.error(str(e))

st.subheader("Hours by month")
st.write("Hours in each time of use, from the peak definitions in config/isos.toml. Add an "
         "ISO there to load its curves.")
isos = power.load_isos()
c1, c2 = st.columns(2)
iso = c1.selectbox("ISO", list(isos))
year = c2.number_input("Year", min_value=2020, max_value=2040, value=date.today().year + 1)
r = isos[iso]
st.caption(f"{r.name}: peak is HE{r.peak_start}-HE{r.peak_end} "
           f"{'Monday to Friday' if r.peak_days == 'weekdays' else 'Monday to Saturday'}"
           f"{', excluding NERC holidays' if r.holidays == 'nerc' else ''}; "
           f"{'daylight saving applies' if r.dst else 'no daylight saving'}.")
table(pd.DataFrame([{
    "month": date(int(year), m, 1).strftime("%b%y"),
    **{power.TOU_NAMES[t]: power.month_hours(iso, int(year), m, t) for t in power.TOU_CODES},
} for m in range(1, 13)]), qty=list(power.TOU_NAMES.values()))
