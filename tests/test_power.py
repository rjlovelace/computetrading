from datetime import date
from decimal import Decimal as D

import pytest

from ctrade import analytics, energy, power, services
from ctrade.db import Mark, session_scope

CSV = power.TEMPLATE + """\
2026-10-23,PJM,Feb27,Western Hub,hub,DA,Financial,on peak,74.00
2026-10-26,PJM,2027-01,Western Hub,hub,DA,Financial,onpeak,"$80.00"
"""
WH_ON = "PJM.WESTERNHUB.DA.F.ONPK"


def test_peak_hours_follow_each_iso():
    # Jan 2027: 21 weekdays less New Year's Day = 20 peak days of 16 hours.
    assert power.month_hours("PJM", 2027, 1, "onpeak") == 320
    assert power.month_hours("PJM", 2027, 1, "offpeak") == 424
    # July 4 2027 is a Sunday, so Monday July 5 is the NERC holiday.
    assert date(2027, 7, 5) in power.nerc_holidays(2027)
    assert power.month_hours("ERCOT", 2027, 7, "onpeak") == 336
    # Daylight saving: PJM loses an hour in March and gains one in November; MISO doesn't.
    assert power.month_hours("PJM", 2027, 3, "atc") == 743
    assert power.month_hours("PJM", 2027, 11, "atc") == 721
    assert power.month_hours("MISO", 2027, 11, "atc") == 720
    with pytest.raises(ValueError, match="isos.toml"):
        power.iso_rules("XYZ")


def test_csv_values_are_normalised():
    assert power.parse_month("Jan-27") == power.parse_month("01/2027") == "2027-01"
    assert power.parse_time_of_use("7x24") == "atc"
    assert power.parse_time_of_use("Off-Peak") == "offpeak"
    assert power.parse_market("Real-Time") == "RT"
    assert power.parse_delivery("P") == "physical"


def test_bad_rows_are_all_reported_and_nothing_loads(url):
    bad = power.TEMPLATE.replace("onpeak,78.50", "midday,78.50").replace("2027-01,HB", "x,HB")
    with session_scope(url) as s:
        with pytest.raises(ValueError) as e:
            power.load_curves(s, bad)
        assert "line 2" in str(e.value) and "line 6" in str(e.value)
        assert power.specs(s) == []
    with pytest.raises(ValueError, match="missing column"):
        power.parse_csv("date,price\n2026-10-23,5\n")


def test_loading_creates_contracts_and_marks_them(url):
    with session_scope(url) as s:
        res = power.load_curves(s, CSV)
        assert res.rows == 8 and res.dates == [date(2026, 10, 23), date(2026, 10, 26)]
        assert WH_ON in res.products and len(res.products) == 6
        inst = services.ensure_future(s, WH_ON, "2027-01")
        assert inst.hours_per_contract == 320
        assert inst.last_trade_date == date(2026, 12, 31)
        marks = services.mark_map(s, [date(2026, 10, 23), date(2026, 10, 26)])
        assert marks[(inst.instrument_id, date(2026, 10, 23))] == D("78.50")
        assert marks[(inst.instrument_id, date(2026, 10, 26))] == D("80.00")
        assert s.get(power.Product, WH_ON).name == \
            "PJM Western Hub day-ahead financial on-peak"
        # Reloading the same file adds nothing.
        n = s.query(Mark).count()
        assert power.load_curves(s, CSV).new_prices == 0
        assert s.query(Mark).count() == n


def test_power_curves_chart_and_spread_like_compute(url):
    with session_scope(url) as s:
        power.load_curves(s, CSV)
        curve = analytics.term_structure(s, WH_ON, [date(2026, 10, 23)])
        assert list(curve.contract) == ["Jan27", "Feb27"]
        assert analytics.unit(s, WH_ON) == "$/MWh"
        sp = analytics.add_spread(s, [(f"{WH_ON} Jan27", 1),
                                      ("PJM.WESTERNHUB.DA.F.OFFPK Jan27", -1)])
        hist = analytics.spread_history(s, analytics.parse_legs(sp.legs))
        assert round(hist.value[0], 2) == 16.25


def test_power_trade_values_over_peak_hours(url):
    with session_scope(url) as s:
        power.load_curves(s, CSV)
        iid = f"{WH_ON}.2027-01"
        services.book_trade(s, instrument_id=iid, side="buy", quantity=D(25),
                            price=D("77.50"), trade_date=date(2026, 10, 23))
        (ln,) = services.positions(s, date(2026, 10, 23))
        assert ln.unrealized == D("1.00") * 25 * 320  # $1/MWh x 25 MW x 320 hours


def test_energy_cost_per_compute_contract(url):
    with session_scope(url) as s:
        power.load_curves(s, CSV)
        gpu1 = services.ensure_future(s, "CME.GPU1", "2027-01").instrument_id
        services.set_mark(s, gpu1, date(2026, 10, 23), D("2.20"))
        f = energy.factor(s, "H100")
        assert f.is_default and f.mwh_per_gpu_hour == D("1.275") * D("1.3") / 1000
        energy.set_factor(s, "H100", "1.0", "1.25")
        df = energy.cost_curve(s, "CME.GPU1", "PJM.WESTERNHUB.DA.F.ATC", date(2026, 10, 23))
        (row,) = df.itertuples()
        assert row.contract == "Jan27"
        assert round(row.energy, 6) == round(69.80 * 1.0 * 1.25 / 1000, 6)  # $0.08725
        assert round(row.margin, 5) == round(2.20 - 0.08725, 5)
        assert row.hours == 730 and round(row.energy_per_contract, 4) == round(0.08725 * 730, 4)
        hist = energy.cost_history(s, gpu1, "PJM.WESTERNHUB.DA.F.ATC.2027-01")
        assert list(hist.date) == [date(2026, 10, 23)]
        with pytest.raises(ValueError):
            energy.factor(s, "MULTI")
        energy.reset_factor(s, "H100")
        assert energy.factor(s, "H100").is_default
