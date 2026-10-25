"""Operations on the database: reference data, trade entry, marks and P&L runs."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import calendar as cal
from . import engine, power, refdata
from .db import DailyPnL, Instrument, Mark, Product, Trade, utcnow

# --- reference data -------------------------------------------------------------------


def seed_products(session: Session) -> int:
    """Insert or refresh the exchange product catalogue. Returns the number of products."""
    for spec in refdata.PRODUCTS:
        session.merge(
            Product(
                code=spec.code,
                exchange=spec.exchange,
                symbol=spec.symbol,
                name=spec.name,
                gpu_model=spec.gpu_model,
                index_name=spec.index_name,
                hours_rule=spec.hours_rule,
                fixed_hours=spec.fixed_hours,
                tick_size=spec.tick_size,
                provisional=spec.provisional,
                notes=spec.notes,
            )
        )
    return len(refdata.PRODUCTS)


def _save_instrument(session: Session, spec: refdata.InstrumentSpec) -> Instrument:
    existing = session.get(Instrument, spec.instrument_id)
    if existing:
        return existing
    inst = Instrument(**spec.__dict__)
    session.add(inst)
    session.flush()
    return inst


def ensure_future(session: Session, product_code: str, month: str) -> Instrument:
    """The monthly contract for a product (compute or forward power), created on first use."""
    product = session.get(Product, product_code)
    if product is None:
        raise ValueError(f"Unknown product {product_code}; run `ctrade init` first")
    if product.hours_rule == power.POWER:
        return _save_instrument(session, power.instrument_spec(session, product_code, month))
    return _save_instrument(session, refdata.future_instrument(product_code, month))


def ensure_spot(
    session: Session, gpu_model: str, start: date, end: date, venue: str = "OTC"
) -> Instrument:
    code = f"SPOT.{gpu_model}"
    if session.get(Product, code) is None:
        session.add(
            Product(
                code=code,
                exchange="OTC",
                symbol=gpu_model,
                name=f"{gpu_model} spot/term rental",
                gpu_model=gpu_model,
                index_name="",
                hours_rule=refdata.CALENDAR,
                fixed_hours=None,
                tick_size=Decimal("0.0001"),
                provisional=False,
                notes="One contract = one GPU for every hour of the delivery period.",
            )
        )
        session.flush()
    return _save_instrument(session, refdata.spot_instrument(gpu_model, start, end, venue))


def ensure_months(session: Session, product_code: str, first_month: str, count: int
                  ) -> list[Instrument]:
    """Create `count` consecutive monthly contracts starting at `first_month` (YYYY-MM)."""
    year, month = cal.parse_month(first_month)
    out = []
    for _ in range(count):
        out.append(ensure_future(session, product_code, f"{year:04d}-{month:02d}"))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


# --- trades ---------------------------------------------------------------------------


def _check_trade(session: Session, instrument_id: str, side: str, qty: Decimal, price: Decimal):
    inst = session.get(Instrument, instrument_id)
    if inst is None:
        raise ValueError(f"Unknown instrument {instrument_id}")
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    if qty <= 0:
        raise ValueError("quantity must be positive; use side for direction")
    product = session.get(Product, inst.product_code)
    if inst.kind == "future" and price % product.tick_size != 0:
        raise ValueError(f"price {price} is not on the {product.tick_size} tick grid")


def book_trade(
    session: Session,
    *,
    instrument_id: str,
    side: str,
    quantity: Decimal,
    price: Decimal,
    trade_date: date,
    book: str = "MAIN",
    fees: Decimal = Decimal("0"),
    counterparty: str = "",
    external_ref: str = "",
    comment: str = "",
    entered_by: str = "",
) -> Trade:
    _check_trade(session, instrument_id, side, quantity, price)
    next_id = (session.scalar(select(func.max(Trade.trade_id))) or 0) + 1
    trade = Trade(
        trade_id=next_id,
        version=1,
        status="live",
        book=book,
        instrument_id=instrument_id,
        side=side,
        quantity=quantity,
        price=price,
        fees=fees,
        trade_date=trade_date,
        counterparty=counterparty,
        external_ref=external_ref,
        comment=comment,
        entered_by=entered_by,
        recorded_at=utcnow(),
    )
    session.add(trade)
    session.flush()
    return trade


def latest_version(session: Session, trade_id: int) -> Trade:
    trade = session.scalar(
        select(Trade).where(Trade.trade_id == trade_id).order_by(Trade.version.desc()).limit(1)
    )
    if trade is None:
        raise ValueError(f"Unknown trade {trade_id}")
    return trade


_AMENDABLE = {
    "book", "instrument_id", "side", "quantity", "price", "fees", "trade_date",
    "counterparty", "external_ref", "comment",
}


def amend_trade(session: Session, trade_id: int, entered_by: str = "", **changes) -> Trade:
    """Write a new version of a trade with the given fields changed."""
    bad = set(changes) - _AMENDABLE
    if bad:
        raise ValueError(f"Cannot amend {sorted(bad)}")
    current = latest_version(session, trade_id)
    if current.status == "cancelled":
        raise ValueError(f"Trade {trade_id} is cancelled")
    fields = {c: getattr(current, c) for c in _AMENDABLE}
    fields.update(changes)
    _check_trade(session, fields["instrument_id"], fields["side"], fields["quantity"],
                 fields["price"])
    new = Trade(
        trade_id=trade_id,
        version=current.version + 1,
        status="live",
        entered_by=entered_by,
        recorded_at=utcnow(),
        **fields,
    )
    session.add(new)
    session.flush()
    return new


def cancel_trade(session: Session, trade_id: int, entered_by: str = "", comment: str = "") -> Trade:
    current = latest_version(session, trade_id)
    fields = {c: getattr(current, c) for c in _AMENDABLE}
    fields["comment"] = comment or current.comment
    new = Trade(
        trade_id=trade_id,
        version=current.version + 1,
        status="cancelled",
        entered_by=entered_by,
        recorded_at=utcnow(),
        **fields,
    )
    session.add(new)
    session.flush()
    return new


def live_trades(session: Session, known_at: datetime | None = None) -> list[Trade]:
    """Latest version of every trade, excluding cancelled ones.

    With `known_at`, returns the blotter as it was recorded at that moment.
    """
    q = select(Trade.trade_id, func.max(Trade.version).label("v"))
    if known_at is not None:
        q = q.where(Trade.recorded_at <= known_at)
    latest = q.group_by(Trade.trade_id).subquery()
    rows = session.scalars(
        select(Trade)
        .join(latest, (Trade.trade_id == latest.c.trade_id) & (Trade.version == latest.c.v))
        .order_by(Trade.trade_date, Trade.trade_id)
    ).all()
    return [t for t in rows if t.status == "live"]


# --- marks ----------------------------------------------------------------------------


def set_mark(
    session: Session,
    instrument_id: str,
    as_of_date: date,
    price: Decimal,
    source: str = "manual",
    comment: str = "",
    entered_by: str = "",
) -> Mark:
    if session.get(Instrument, instrument_id) is None:
        raise ValueError(f"Unknown instrument {instrument_id}")
    mark = Mark(
        instrument_id=instrument_id,
        as_of_date=as_of_date,
        price=price,
        source=source,
        comment=comment,
        entered_by=entered_by,
        recorded_at=utcnow(),
    )
    session.add(mark)
    session.flush()
    return mark


def mark_map(session: Session, dates: list[date]) -> dict[tuple[str, date], Decimal]:
    """Latest recorded mark per (instrument, date) for the given dates."""
    out: dict[tuple[str, date], tuple[datetime, Decimal]] = {}
    for m in session.scalars(select(Mark).where(Mark.as_of_date.in_(dates))):
        key = (m.instrument_id, m.as_of_date)
        if key not in out or m.recorded_at >= out[key][0]:
            out[key] = (m.recorded_at, m.price)
    return {k: v[1] for k, v in out.items()}


def mark_history(session: Session, instrument_ids: list[str], start: date | None = None,
                 end: date | None = None) -> list[Mark]:
    """The mark in force for each instrument and date (latest recorded), oldest date first."""
    query = select(Mark).where(Mark.instrument_id.in_(instrument_ids))
    if start is not None:
        query = query.where(Mark.as_of_date >= start)
    if end is not None:
        query = query.where(Mark.as_of_date <= end)
    latest: dict[tuple[str, date], Mark] = {}
    for m in session.scalars(query.order_by(Mark.recorded_at, Mark.id)):
        latest[(m.instrument_id, m.as_of_date)] = m
    return sorted(latest.values(), key=lambda m: (m.as_of_date, m.instrument_id))


def marked_instruments(session: Session) -> list[str]:
    """Instruments that have at least one mark."""
    return session.scalars(select(Mark.instrument_id).distinct().order_by(Mark.instrument_id)
                           ).all()


# --- positions and P&L ----------------------------------------------------------------

_MONEY_Q = Decimal("0.0001")
_PRICE_Q = Decimal("0.000001")


def _q(value: Decimal | None, quantum: Decimal) -> Decimal | None:
    return None if value is None else value.quantize(quantum)


def _engine_inputs(session: Session, known_at: datetime | None = None):
    trades = live_trades(session, known_at)
    instruments = {
        i.instrument_id: engine.InstrumentRow(
            i.instrument_id, Decimal(i.hours_per_contract), i.last_trade_date
        )
        for i in session.scalars(select(Instrument))
    }
    rows = [
        engine.TradeRow(
            trade_id=t.trade_id,
            book=t.book,
            instrument_id=t.instrument_id,
            trade_date=t.trade_date,
            quantity=Decimal(t.signed_quantity),
            price=Decimal(t.price),
            fees=Decimal(t.fees or 0),
        )
        for t in trades
    ]
    return rows, instruments


def _mark_dates(session: Session, as_of: date, prior: date) -> list[date]:
    """The P&L dates plus the last trade dates of instruments that have expired by then."""
    expiries = session.scalars(
        select(Instrument.last_trade_date).where(Instrument.last_trade_date < as_of)
    ).all()
    return sorted({as_of, prior, *expiries})


def positions(session: Session, as_of: date) -> list[engine.PnLLine]:
    trades, instruments = _engine_inputs(session)
    marks = mark_map(session, _mark_dates(session, as_of, as_of))
    lines, _ = engine.value_positions(trades, instruments, marks, as_of)
    return lines


def _apply_amendments(session: Session, result: engine.PnLResult) -> None:
    """Measure today's P&L against the prior day as it was reported.

    Trades amended, cancelled or back-dated after the prior day's run change what that day's
    cumulative P&L would be today. The difference is shown as `amendments` and included in
    `daily`, so reported daily figures always add up to the cumulative.
    """
    stored = {
        (r.book, r.instrument_id): Decimal(r.cumulative)
        for r in session.scalars(select(DailyPnL).where(DailyPnL.as_of_date == result.prior))
    }
    if not stored:
        return
    seen = set()
    for ln in result.lines:
        key = (ln.book, ln.instrument_id)
        seen.add(key)
        recomputed_prior = ln.cumulative - ln.daily
        ln.amendments = recomputed_prior - stored.get(key, engine.ZERO)
        ln.daily += ln.amendments
    for (book, iid), cum in stored.items():
        if (book, iid) not in seen and cum != 0:  # every trade on this line was cancelled
            result.lines.append(
                engine.PnLLine(
                    book=book, instrument_id=iid, quantity=engine.ZERO, avg_price=engine.ZERO,
                    mark=None, realized=engine.ZERO, unrealized=engine.ZERO, fees=engine.ZERO,
                    cumulative=engine.ZERO, daily=-cum, amendments=-cum,
                )
            )


def run_daily_pnl(
    session: Session, as_of: date, prior: date | None = None, store: bool = True
) -> engine.PnLResult:
    """Compute (and by default store) the daily P&L. Runs with warnings are never stored."""
    prior = prior or cal.previous_business_day(as_of)
    trades, instruments = _engine_inputs(session)
    marks = mark_map(session, _mark_dates(session, as_of, prior))
    result = engine.daily_pnl(trades, instruments, marks, as_of, prior)
    _apply_amendments(session, result)
    if store and not result.errors:
        session.execute(delete(DailyPnL).where(DailyPnL.as_of_date == as_of))
        for ln in result.lines:
            session.add(
                DailyPnL(
                    as_of_date=as_of,
                    prior_date=prior,
                    book=ln.book,
                    instrument_id=ln.instrument_id,
                    quantity=ln.quantity,
                    avg_price=_q(ln.avg_price, _PRICE_Q),
                    mark=_q(ln.mark, _PRICE_Q),
                    realized=_q(ln.realized, _MONEY_Q),
                    unrealized=_q(ln.unrealized, _MONEY_Q),
                    fees=_q(ln.fees, _MONEY_Q),
                    cumulative=_q(ln.cumulative, _MONEY_Q),
                    daily=_q(ln.daily, _MONEY_Q),
                    new_trades=_q(ln.new_trades, _MONEY_Q),
                    price_move=_q(ln.price_move, _MONEY_Q),
                    fees_today=_q(ln.fees_today, _MONEY_Q),
                    amendments=_q(ln.amendments, _MONEY_Q),
                    residual=_q(ln.residual, _MONEY_Q),
                )
            )
    return result
