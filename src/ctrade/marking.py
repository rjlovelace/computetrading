"""Build end-of-day marks from the observations the agents collected.

For each traded instrument still live on the mark date, the first rule that applies wins:

1. A mark someone entered by hand for that date is kept; automatic marks never replace it.
2. An exchange settlement price for the instrument and date (from the settlement agent).
3. Futures: the expected monthly average of the settlement index. Index prints already
   published in the contract month count as fixed; every remaining day of the month is
   assumed to print at the latest index value. Before the month starts this is simply the
   latest index value (a flat forward curve). Missing past days carry the previous print.
4. Spot blocks: the latest value of the matching GPU index.

An index value older than MAX_STALE_DAYS is not used. Instruments with no usable source get
no mark and are listed in the report, so the P&L run flags them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import services
from .agents import ornn, settlements
from .db import Instrument, Mark, MarketObservation

MAX_STALE_DAYS = 5
AUTO_PREFIX = "auto:"
_Q = Decimal("0.000001")

# Settlement index for each product. No proxies: a product whose own index has no source
# stays unmarked rather than being marked off a different index.
INDEX_SERIES: dict[str, str] = {
    "ICE.HPR": ornn.series_for("H100 SXM"),
    "ICE.BKL": ornn.series_for("B200"),
    "CME.GPU1": "SILICONDATA.H100",
    "CME.GPU2": "SILICONDATA.B200",
    "SPOT.H100": ornn.series_for("H100 SXM"),
    "SPOT.H200": ornn.series_for("H200"),
    "SPOT.B200": ornn.series_for("B200"),
    "SPOT.A100": ornn.series_for("A100 SXM4"),
    "SPOT.RTX5090": ornn.series_for("RTX 5090"),
}


@dataclass
class MarkResult:
    instrument_id: str
    status: str  # marked | kept-manual | unchanged | no-source
    price: Decimal | None = None
    method: str = ""
    note: str = ""


def _series_values(session: Session, series: str, start: date, end: date) -> dict[date, Decimal]:
    """Latest-fetched value per date for a series between start and end inclusive."""
    rows = session.scalars(
        select(MarketObservation)
        .where(MarketObservation.series == series,
               MarketObservation.obs_date >= start,
               MarketObservation.obs_date <= end)
        .order_by(MarketObservation.fetched_at, MarketObservation.id)
    )
    return {r.obs_date: Decimal(r.value) for r in rows}


def _latest(values: dict[date, Decimal], on: date) -> tuple[date, Decimal] | None:
    eligible = [d for d in values if d <= on]
    if not eligible:
        return None
    d = max(eligible)
    return d, values[d]


def expected_average(values: dict[date, Decimal], start: date, end: date, on: date,
                     latest: Decimal) -> tuple[Decimal, int]:
    """Expected average over [start, end] given prints up to `on`. Returns (value, fixed days)."""
    total = Decimal(0)
    fixed = 0
    before = _latest(values, start - timedelta(days=1))
    carry: Decimal | None = before[1] if before else None
    d = start
    while d <= end:
        if d <= on:
            if d in values:
                carry = values[d]
                fixed += 1
            total += carry if carry is not None else latest
        else:
            total += latest
        d += timedelta(days=1)
    return total / ((end - start).days + 1), fixed


def _mark_one(session: Session, inst: Instrument, as_of: date) -> MarkResult:
    iid = inst.instrument_id
    settle = _series_values(session, settlements.series_for(iid), as_of, as_of)
    if as_of in settle:
        return MarkResult(iid, "marked", settle[as_of], "settlement", "exchange settlement")

    series = INDEX_SERIES.get(inst.product_code)
    if series is None:
        return MarkResult(iid, "no-source", note=f"no index source for {inst.product_code}")
    lookback = min(inst.delivery_start, as_of) - timedelta(days=MAX_STALE_DAYS)
    values = _series_values(session, series, lookback, as_of)
    latest = _latest(values, as_of)
    if latest is None or (as_of - latest[0]).days > MAX_STALE_DAYS:
        return MarkResult(iid, "no-source", note=f"no {series} value in the last "
                                                  f"{MAX_STALE_DAYS} days")
    latest_date, latest_value = latest

    if inst.kind == "spot":
        return MarkResult(iid, "marked", latest_value, "index-spot",
                          f"{series} {latest_value} on {latest_date}")
    if as_of < inst.delivery_start:
        return MarkResult(iid, "marked", latest_value, "index-flat-forward",
                          f"{series} {latest_value} on {latest_date}, month not started")
    avg, fixed = expected_average(values, inst.delivery_start, inst.delivery_end, as_of,
                                  latest_value)
    days = (inst.delivery_end - inst.delivery_start).days + 1
    return MarkResult(iid, "marked", avg.quantize(_Q), "index-average",
                      f"{series}: {fixed} of {days} days published, rest at "
                      f"{latest_value} ({latest_date})")


def build_marks(session: Session, as_of: date, entered_by: str = "agents") -> list[MarkResult]:
    """Write automatic marks for every traded instrument live on `as_of`."""
    traded = {t.instrument_id for t in services.live_trades(session)}
    instruments = session.scalars(
        select(Instrument).where(Instrument.instrument_id.in_(traded),
                                 Instrument.last_trade_date >= as_of)
        .order_by(Instrument.instrument_id)
    ).all()
    results = []
    for inst in instruments:
        existing = session.scalars(
            select(Mark).where(Mark.instrument_id == inst.instrument_id,
                               Mark.as_of_date == as_of)
            .order_by(Mark.recorded_at.desc(), Mark.id.desc())
        ).first()
        if existing is not None and not existing.source.startswith(AUTO_PREFIX):
            results.append(MarkResult(inst.instrument_id, "kept-manual", Decimal(existing.price),
                                      existing.source, "entered by hand"))
            continue
        res = _mark_one(session, inst, as_of)
        if res.status == "marked":
            if existing is not None and Decimal(existing.price) == res.price:
                res.status = "unchanged"
            else:
                services.set_mark(session, inst.instrument_id, as_of, res.price,
                                  source=AUTO_PREFIX + res.method, comment=res.note,
                                  entered_by=entered_by)
        results.append(res)
    return results
