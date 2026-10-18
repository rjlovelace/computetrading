from datetime import date
from decimal import Decimal as D

import pytest

from ctrade import services
from ctrade.db import DailyPnL, session_scope


def test_book_amend_cancel_keeps_history(url):
    with session_scope(url) as s:
        iid = services.ensure_future(s, "ICE.HPR", "2026-11").instrument_id
        t = services.book_trade(s, instrument_id=iid, side="buy", quantity=D(10),
                                price=D("2.000"), trade_date=date(2026, 11, 2))
        services.amend_trade(s, t.trade_id, price=D("2.010"))
        assert [x.price for x in services.live_trades(s)] == [D("2.010")]
        services.cancel_trade(s, t.trade_id)
        assert services.live_trades(s) == []
        assert services.latest_version(s, t.trade_id).version == 3


def test_price_must_be_on_tick(url):
    with session_scope(url) as s:
        iid = services.ensure_future(s, "CME.GPU1", "2026-11").instrument_id
        with pytest.raises(ValueError, match="tick"):
            services.book_trade(s, instrument_id=iid, side="buy", quantity=D(1),
                                price=D("2.005"), trade_date=date(2026, 11, 2))


def test_latest_mark_wins_and_pnl_is_stored(url):
    with session_scope(url) as s:
        iid = services.ensure_future(s, "ICE.HPR", "2026-11").instrument_id
        services.book_trade(s, instrument_id=iid, side="buy", quantity=D(10),
                            price=D("2.000"), trade_date=date(2026, 11, 2), fees=D(5))
        services.set_mark(s, iid, date(2026, 11, 2), D("9.999"))
        services.set_mark(s, iid, date(2026, 11, 2), D("2.050"))  # correction
        r = services.run_daily_pnl(s, date(2026, 11, 2))
    assert r.errors == []
    assert r.total_daily == D("355")
    with session_scope(url) as s:
        rows = s.query(DailyPnL).all()
        assert len(rows) == 1
        assert D(rows[0].daily) == D("355")


def test_spot_trade_values_over_delivery_hours(url):
    with session_scope(url) as s:
        inst = services.ensure_spot(s, "H100", date(2026, 11, 1), date(2026, 11, 7))
        services.book_trade(s, instrument_id=inst.instrument_id, side="buy", quantity=D(8),
                            price=D("2.10"), trade_date=date(2026, 10, 30))
        services.set_mark(s, inst.instrument_id, date(2026, 10, 30), D("2.20"))
        (ln,) = services.positions(s, date(2026, 10, 30))
    assert ln.unrealized == D("0.10") * 8 * 168


def _day_one(s):
    iid = services.ensure_future(s, "ICE.HPR", "2026-11").instrument_id
    t = services.book_trade(s, instrument_id=iid, side="buy", quantity=D(10),
                            price=D("2.000"), trade_date=date(2026, 11, 2))
    services.set_mark(s, iid, date(2026, 11, 2), D("2.050"))
    services.set_mark(s, iid, date(2026, 11, 3), D("2.050"))
    assert services.run_daily_pnl(s, date(2026, 11, 2)).total_daily == D("360")
    return t


def test_amendment_after_reporting_shows_in_next_day(url):
    with session_scope(url) as s:
        t = _day_one(s)
        services.amend_trade(s, t.trade_id, price=D("2.010"))
        r = services.run_daily_pnl(s, date(2026, 11, 3))
    (ln,) = r.lines
    assert ln.amendments == D("-72")  # 0.01 x 10 x 720 less than reported
    assert ln.daily == D("-72")
    assert ln.cumulative == D("288")


def test_cancelled_trade_reverses_reported_pnl(url):
    with session_scope(url) as s:
        t = _day_one(s)
        services.cancel_trade(s, t.trade_id)
        r = services.run_daily_pnl(s, date(2026, 11, 3))
    assert r.total_daily == D("-360")
    assert r.total_cumulative == 0


def test_run_with_missing_marks_is_not_stored(url):
    with session_scope(url) as s:
        iid = services.ensure_future(s, "ICE.HPR", "2026-11").instrument_id
        services.book_trade(s, instrument_id=iid, side="buy", quantity=D(1),
                            price=D("2.000"), trade_date=date(2026, 11, 2))
        r = services.run_daily_pnl(s, date(2026, 11, 2))
        assert r.errors
        assert s.query(DailyPnL).count() == 0
