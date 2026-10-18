"""Pure position, valuation and P&L calculations (no database access).

Conventions
- Quantity is signed: + bought, - sold, in contracts.
- Prices and marks are $ per GPU-hour; value = price x hours_per_contract x quantity.
- Positions use average cost: reducing trades realise P&L against the average price.
- After an instrument's last trade date its position is closed at the final settlement
  mark (the mark on the last trade date) and all of its P&L counts as realised.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

ZERO = Decimal("0")
RESIDUAL_TOLERANCE = Decimal("0.01")  # dollars; average-cost division rounds at ~1e-20


@dataclass(frozen=True)
class TradeRow:
    trade_id: int
    book: str
    instrument_id: str
    trade_date: date
    quantity: Decimal  # signed
    price: Decimal
    fees: Decimal = ZERO


@dataclass(frozen=True)
class InstrumentRow:
    instrument_id: str
    hours_per_contract: Decimal
    last_trade_date: date


@dataclass
class Position:
    book: str
    instrument_id: str
    quantity: Decimal = ZERO
    avg_price: Decimal = ZERO
    realized: Decimal = ZERO
    fees: Decimal = ZERO


@dataclass
class PnLLine:
    book: str
    instrument_id: str
    quantity: Decimal
    avg_price: Decimal
    mark: Decimal | None
    realized: Decimal
    unrealized: Decimal
    fees: Decimal
    cumulative: Decimal
    daily: Decimal = ZERO
    new_trades: Decimal = ZERO
    price_move: Decimal = ZERO
    fees_today: Decimal = ZERO
    amendments: Decimal = ZERO
    residual: Decimal = ZERO
    expired: bool = False


@dataclass
class PnLResult:
    as_of: date
    prior: date
    lines: list[PnLLine] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_daily(self) -> Decimal:
        return sum((ln.daily for ln in self.lines), ZERO)

    @property
    def total_cumulative(self) -> Decimal:
        return sum((ln.cumulative for ln in self.lines), ZERO)


def _sign(x: Decimal) -> int:
    return (x > 0) - (x < 0)


def build_positions(
    trades: list[TradeRow], instruments: dict[str, InstrumentRow], as_of: date
) -> dict[tuple[str, str], Position]:
    """Average-cost positions per (book, instrument) from trades dated on or before `as_of`."""
    positions: dict[tuple[str, str], Position] = {}
    for t in sorted(trades, key=lambda t: (t.trade_date, t.trade_id)):
        if t.trade_date > as_of:
            continue
        mult = instruments[t.instrument_id].hours_per_contract
        pos = positions.setdefault(
            (t.book, t.instrument_id), Position(t.book, t.instrument_id)
        )
        pos.fees += t.fees
        q = t.quantity
        if pos.quantity == 0 or _sign(pos.quantity) == _sign(q):
            new_qty = pos.quantity + q
            pos.avg_price = (pos.avg_price * pos.quantity + t.price * q) / new_qty
            pos.quantity = new_qty
            continue
        closed = min(abs(q), abs(pos.quantity))
        pos.realized += (t.price - pos.avg_price) * closed * _sign(pos.quantity) * mult
        new_qty = pos.quantity + q
        if new_qty == 0:
            pos.avg_price = ZERO
        elif _sign(new_qty) != _sign(pos.quantity):
            pos.avg_price = t.price
        pos.quantity = new_qty
    return positions


def effective_mark(
    marks: dict[tuple[str, date], Decimal], inst: InstrumentRow, on: date
) -> Decimal | None:
    """Mark for an instrument on a date; after expiry this is the final settlement."""
    return marks.get((inst.instrument_id, min(on, inst.last_trade_date)))


def value_positions(
    trades: list[TradeRow],
    instruments: dict[str, InstrumentRow],
    marks: dict[tuple[str, date], Decimal],
    as_of: date,
) -> tuple[list[PnLLine], list[str]]:
    lines: list[PnLLine] = []
    errors: list[str] = []
    for (book, iid), pos in sorted(build_positions(trades, instruments, as_of).items()):
        inst = instruments[iid]
        expired = as_of > inst.last_trade_date
        mark = effective_mark(marks, inst, as_of)
        if mark is None and pos.quantity != 0:
            when = "final settlement" if expired else as_of.isoformat()
            errors.append(f"Missing mark for {iid} ({when})")
            unrealized = ZERO
        elif pos.quantity == 0:
            unrealized = ZERO
        else:
            unrealized = (mark - pos.avg_price) * pos.quantity * inst.hours_per_contract
        realized = pos.realized
        if expired:
            realized, unrealized = realized + unrealized, ZERO
        lines.append(
            PnLLine(
                book=book,
                instrument_id=iid,
                quantity=ZERO if expired else pos.quantity,
                avg_price=ZERO if expired else pos.avg_price,
                mark=mark,
                realized=realized,
                unrealized=unrealized,
                fees=pos.fees,
                cumulative=realized + unrealized - pos.fees,
                expired=expired,
            )
        )
    return lines, errors


def daily_pnl(
    trades: list[TradeRow],
    instruments: dict[str, InstrumentRow],
    marks: dict[tuple[str, date], Decimal],
    as_of: date,
    prior: date,
) -> PnLResult:
    """Daily P&L for `as_of` versus `prior`, with an explain that should leave ~0 residual.

    daily      = cumulative(as_of) - cumulative(prior)
    new_trades = sum over trades dated in (prior, as_of] of (mark - price) x qty x hours
    price_move = position at prior x (mark(as_of) - mark(prior)) x hours
    fees_today = -fees on trades dated in (prior, as_of]
    """
    result = PnLResult(as_of=as_of, prior=prior)
    today, errors_today = value_positions(trades, instruments, marks, as_of)
    yesterday, _ = value_positions(trades, instruments, marks, prior)
    result.errors.extend(errors_today)
    prior_by_key = {(ln.book, ln.instrument_id): ln for ln in yesterday}
    prior_positions = build_positions(trades, instruments, prior)

    new_trades: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    fees_today: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO)
    for t in trades:
        if prior < t.trade_date <= as_of:
            inst = instruments[t.instrument_id]
            mark = effective_mark(marks, inst, as_of)
            key = (t.book, t.instrument_id)
            if mark is not None:
                new_trades[key] += (mark - t.price) * t.quantity * inst.hours_per_contract
            fees_today[key] -= t.fees

    for ln in today:
        key = (ln.book, ln.instrument_id)
        inst = instruments[ln.instrument_id]
        prev = prior_by_key.get(key)
        ln.daily = ln.cumulative - (prev.cumulative if prev else ZERO)
        ln.new_trades = new_trades[key]
        ln.fees_today = fees_today[key]
        prior_qty = prior_positions[key].quantity if key in prior_positions else ZERO
        if prior > inst.last_trade_date:
            prior_qty = ZERO  # already closed at final settlement
        if prior_qty != 0:
            m_today = effective_mark(marks, inst, as_of)
            m_prior = effective_mark(marks, inst, prior)
            if m_today is None or m_prior is None:
                result.errors.append(
                    f"Cannot explain price move for {ln.instrument_id}: missing mark"
                )
            else:
                ln.price_move = prior_qty * (m_today - m_prior) * inst.hours_per_contract
        ln.residual = ln.daily - ln.new_trades - ln.price_move - ln.fees_today
        if abs(ln.residual) > RESIDUAL_TOLERANCE and not result.errors:
            result.errors.append(
                f"Unexplained P&L {ln.residual} on {ln.book}/{ln.instrument_id}"
            )
    result.lines = today
    return result
