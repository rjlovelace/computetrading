from datetime import date
from decimal import Decimal as D

import pandas as pd
import pytest

from ctrade import analytics, services
from ctrade.db import session_scope

D1, D2, D3 = date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)


def mark(s, iid, d, price):
    services.set_mark(s, iid, d, D(price))


def gpu1_curve(s):
    ids = [i.instrument_id for i in services.ensure_months(s, "CME.GPU1", "2026-12", 3)]
    for d, prices in [(D1, ["2.00", "2.10", "2.20"]), (D2, ["2.05", "2.10", "2.30"])]:
        for iid, p in zip(ids, prices, strict=True):
            mark(s, iid, d, p)
    return ids


def test_month_names_and_lookup(url):
    with session_scope(url) as s:
        inst = services.ensure_future(s, "CME.GPU1", "2027-01")
        assert analytics.month_label(inst) == "Jan27"
        assert analytics.contract_name(inst) == "CME.GPU1 Jan27"
        assert analytics.resolve_contract(s, "CME.GPU1  jan27").instrument_id == inst.instrument_id
        assert analytics.resolve_contract(s, "CME.GPU2 Jan27").instrument_id == "CME.GPU2.2027-01"
        with pytest.raises(ValueError):
            analytics.resolve_contract(s, "CME.GPU9 Jan27")


def test_term_structure_orders_months_per_date(url):
    with session_scope(url) as s:
        gpu1_curve(s)
        df = analytics.term_structure(s, "CME.GPU1", [D2, D1])
        d2 = df[df.date == D2]
        assert list(d2.contract) == ["Dec26", "Jan27", "Feb27"]
        assert list(d2.mark) == [2.05, 2.10, 2.30]
        assert analytics.curve_dates(s, "CME.GPU1") == [D2, D1]


def test_curve_changes_are_day_over_day(url):
    with session_scope(url) as s:
        gpu1_curve(s)
        df = analytics.curve_changes(s, "CME.GPU1", [D2, D1])
        assert set(df.date) == {D2}  # D1 has no earlier curve
        assert list(df.change.round(4)) == [0.05, 0.0, 0.1]
        assert set(df.prior) == {D1}


def test_spread_value_and_daily_change(url):
    with session_scope(url) as s:
        gpu1 = services.ensure_future(s, "CME.GPU1", "2027-01").instrument_id
        gpu2 = services.ensure_future(s, "CME.GPU2", "2027-01").instrument_id
        for d, p1, p2 in [(D1, "2.10", "6.00"), (D2, "2.20", "6.15"), (D3, "2.25", None)]:
            mark(s, gpu1, d, p1)
            if p2:
                mark(s, gpu2, d, p2)
        sp = analytics.add_spread(s, [("CME.GPU2 Jan27", 1), ("CME.GPU1 Jan27", -1)])
        assert sp.name == "CME.GPU2 Jan27 - CME.GPU1 Jan27"
        df = analytics.spread_history(s, analytics.parse_legs(sp.legs))
        assert list(df.date) == [D1, D2]  # D3 lacks the GPU2 leg
        assert list(df.value.round(4)) == [3.9, 3.95]
        assert pd.isna(df.change[0]) and round(df.change[1], 4) == 0.05


def test_spread_rules(url):
    with session_scope(url) as s:
        with pytest.raises(ValueError):
            analytics.add_spread(s, [("CME.GPU1 Jan27", 1)])
        with pytest.raises(ValueError):
            analytics.add_spread(s, [("CME.GPU1 Jan27", 1), ("CME.GPU1 Jan27", -1)])
        sp = analytics.add_spread(s, [("CME.GPU2 Jan27", 2), ("CME.GPU1 Jan27", -1)])
        assert sp.name == "2 x CME.GPU2 Jan27 - CME.GPU1 Jan27"
        with pytest.raises(ValueError, match="already exists"):
            analytics.add_spread(s, [("CME.GPU2 Jan27", 2), ("CME.GPU1 Jan27", -1)])
        analytics.remove_spread(s, sp.id)
        assert analytics.list_spreads(s) == []


def test_moving_averages():
    values = pd.Series([1.0, 2.0, 3.0, 4.0])
    assert analytics.moving_average(values, 2).tolist()[1:] == [1.5, 2.5, 3.5]
    assert pd.isna(analytics.moving_average(values, 2)[0])
    ema = analytics.moving_average(values, 3, "EMA")
    assert ema[0] == 1.0 and ema[1] == 1.5


def test_with_moving_averages_adds_one_series_per_study():
    df = pd.DataFrame({"date": [1, 2, 3, 1, 2, 3], "instrument": list("AAABBB"),
                       "mark": [1.0, 2.0, 3.0, 5.0, 5.0, 8.0]})
    out = analytics.with_moving_averages(df, "mark", "instrument", [("SMA", 2)])
    ma = out[out.series == "B SMA 2"]
    assert ma.mark.tolist() == [5.0, 6.5]
    assert set(out.series) == {"A", "B", "A SMA 2", "B SMA 2"}
