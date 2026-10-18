from datetime import date
from decimal import Decimal as D

from ctrade import engine, refdata
from ctrade.engine import InstrumentRow, TradeRow

HPR_NOV = refdata.future_instrument("ICE.HPR", "2026-11")
INSTRUMENTS = {
    HPR_NOV.instrument_id: InstrumentRow(
        HPR_NOV.instrument_id, HPR_NOV.hours_per_contract, HPR_NOV.last_trade_date
    )
}
IID = HPR_NOV.instrument_id


def trade(tid, d, qty, price, fees="0", book="MAIN"):
    return TradeRow(tid, book, IID, d, D(qty), D(price), D(fees))


TRADES = [
    trade(1, date(2026, 11, 2), "10", "2.000", fees="5"),
    trade(2, date(2026, 11, 3), "-4", "2.100"),
]
MARKS = {
    (IID, date(2026, 11, 2)): D("2.050"),
    (IID, date(2026, 11, 3)): D("2.080"),
    (IID, date(2026, 11, 30)): D("2.000"),
}


def test_day_one_new_trade_pnl():
    r = engine.daily_pnl(TRADES, INSTRUMENTS, MARKS, date(2026, 11, 2), date(2026, 10, 30))
    (ln,) = r.lines
    assert r.errors == []
    assert ln.new_trades == D("360")  # (2.05 - 2.00) x 10 x 720
    assert ln.fees_today == D("-5")
    assert ln.daily == D("355")


def test_day_two_partial_close_and_explain():
    r = engine.daily_pnl(TRADES, INSTRUMENTS, MARKS, date(2026, 11, 3), date(2026, 11, 2))
    (ln,) = r.lines
    assert r.errors == []
    assert ln.quantity == D("6")
    assert ln.realized == D("288")  # (2.10 - 2.00) x 4 x 720
    assert ln.unrealized == D("345.6")  # (2.08 - 2.00) x 6 x 720
    assert ln.cumulative == D("628.6")
    assert ln.daily == D("273.6")
    assert ln.price_move == D("216")  # 10 x 0.03 x 720
    assert ln.new_trades == D("57.6")  # (2.08 - 2.10) x -4 x 720
    assert ln.residual == 0


def test_expiry_closes_at_final_settlement():
    r = engine.daily_pnl(TRADES, INSTRUMENTS, MARKS, date(2026, 12, 1), date(2026, 11, 30))
    (ln,) = r.lines
    assert r.errors == []
    assert ln.expired
    assert ln.quantity == 0
    assert ln.unrealized == 0
    assert ln.realized == D("288")  # final settlement equals average price
    assert ln.daily == 0


def test_missing_mark_is_reported_not_zeroed():
    r = engine.daily_pnl(TRADES, INSTRUMENTS, MARKS, date(2026, 11, 4), date(2026, 11, 3))
    assert any("Missing mark" in e for e in r.errors)


def test_position_flip_resets_average_price():
    trades = [trade(1, date(2026, 11, 2), "5", "2.00"), trade(2, date(2026, 11, 2), "-8", "2.10")]
    pos = engine.build_positions(trades, INSTRUMENTS, date(2026, 11, 2))[("MAIN", IID)]
    assert pos.quantity == D("-3")
    assert pos.avg_price == D("2.10")
    assert pos.realized == D("0.10") * 5 * 720


def test_books_are_separate():
    trades = [
        trade(1, date(2026, 11, 2), "5", "2.00", book="A"),
        trade(2, date(2026, 11, 2), "-5", "2.10", book="B"),
    ]
    positions = engine.build_positions(trades, INSTRUMENTS, date(2026, 11, 2))
    assert positions[("A", IID)].quantity == 5
    assert positions[("B", IID)].quantity == -5
    assert positions[("A", IID)].realized == 0
