"""Build end-of-day marks from the observations the agents collected.

For each instrument still live on the mark date, the first rule that applies wins:

1. A mark someone entered by hand for that date is kept; automatic marks never replace it.
2. An exchange settlement price for the instrument and date (from the settlement agent).
3. Futures: the expected monthly average of the settlement index. Index prints already
   published in the contract month count as fixed; every remaining day of the month is
   assumed to print at the latest index value. Before the month starts this is simply the
   latest index value (a flat forward curve). Missing past days carry the previous print.
4. Spot blocks: the latest value of the matching GPU index.
5. Proxy rules you configure (`MarkProxy`), tried in priority order: another product's
   contract for the same delivery period, a specific instrument, or another price series,
   times a multiplier plus a spread. A product is never marked off another one unless you
   set up a rule for it.

An index value older than MAX_STALE_DAYS is not used. Instruments with no usable source get
no mark and are listed in the report, so the P&L run flags them.

`load_prices` runs the agents and then rebuilds marks for every day that got new prices, so
loading prices marks the contracts automatically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import agents as agents_mod
from . import calendar as cal
from . import refdata, services
from .agents import ornn, settlements
from .db import AgentRun, Instrument, Mark, MarketObservation, MarkProxy, Product
from .formatting import adjustment

MAX_STALE_DAYS = 5
AUTO_PREFIX = "auto:"
_Q = Decimal("0.000001")

# Settlement index for each product. A product whose own index has no source stays unmarked
# unless a proxy rule says where else to take its mark from.
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


class Contract(Protocol):
    instrument_id: str
    product_code: str
    kind: str
    delivery_start: date
    delivery_end: date


def _index_mark(session: Session, inst: Contract, series: str, as_of: date) -> MarkResult:
    """Mark from a price series: latest value for spot, expected monthly average for futures."""
    iid = inst.instrument_id
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


def _own_price(session: Session, inst: Contract, as_of: date) -> MarkResult:
    """Exchange settlement, then the product's own index."""
    iid = inst.instrument_id
    settle = _series_values(session, settlements.series_for(iid), as_of, as_of)
    if as_of in settle:
        return MarkResult(iid, "marked", settle[as_of], "settlement", "exchange settlement")
    series = INDEX_SERIES.get(inst.product_code)
    if series is None:
        return MarkResult(iid, "no-source", note=f"no index source for {inst.product_code}")
    return _index_mark(session, inst, series, as_of)


def _manual_mark(session: Session, instrument_id: str, as_of: date) -> Mark | None:
    """The latest mark for the date, if it was entered by hand rather than built here."""
    latest = session.scalars(
        select(Mark).where(Mark.instrument_id == instrument_id, Mark.as_of_date == as_of)
        .order_by(Mark.recorded_at.desc(), Mark.id.desc())
    ).first()
    if latest is None or latest.source.startswith(AUTO_PREFIX):
        return None
    return latest


def _same_period(product_code: str, inst: Contract) -> Contract | None:
    """`product_code`'s contract covering the same delivery period as `inst`."""
    if product_code.startswith("SPOT.") and inst.kind == "spot":
        gpu = product_code.removeprefix("SPOT.")
        return refdata.spot_instrument(gpu, inst.delivery_start, inst.delivery_end, "PROXY")
    if product_code in refdata.PRODUCTS_BY_CODE and inst.kind == "future":
        return refdata.future_instrument(product_code, inst.delivery_start.strftime("%Y-%m"))
    return None


def _contract_mark(session: Session, inst: Contract, as_of: date, seen: frozenset[str]
                   ) -> MarkResult:
    """A source contract's mark: one entered by hand, else built the usual way."""
    manual = _manual_mark(session, inst.instrument_id, as_of)
    if manual is not None:
        return MarkResult(inst.instrument_id, "marked", Decimal(manual.price), "manual",
                          f"{inst.instrument_id} marked by hand")
    return _resolve(session, inst, as_of, seen)


def proxies_for(session: Session, inst: Contract) -> list[MarkProxy]:
    """Rules for this instrument first, then rules for its product, each by priority."""
    rules = session.scalars(
        select(MarkProxy).where(MarkProxy.target.in_([inst.instrument_id, inst.product_code]))
    ).all()
    return sorted(rules, key=lambda r: (r.target != inst.instrument_id, r.priority, r.id))


def _proxy_mark(session: Session, rule: MarkProxy, inst: Contract, as_of: date,
                seen: frozenset[str]) -> MarkResult:
    iid = inst.instrument_id
    if rule.source_kind == "series":
        return _index_mark(session, inst, rule.source, as_of)
    if rule.source_kind == "product":
        source = _same_period(rule.source, inst)
        if source is None:
            return MarkResult(iid, "no-source", note=f"{rule.source} has no contract like {iid}")
    else:
        source = session.get(Instrument, rule.source)
        if source is None:
            return MarkResult(iid, "no-source", note=f"unknown instrument {rule.source}")
    if source.instrument_id in seen:
        return MarkResult(iid, "no-source", note=f"circular proxy via {source.instrument_id}")
    return _contract_mark(session, source, as_of, seen)


def _resolve(session: Session, inst: Contract, as_of: date,
             seen: frozenset[str] = frozenset()) -> MarkResult:
    """Own price first, then each proxy rule in turn."""
    res = _own_price(session, inst, as_of)
    if res.status == "marked":
        return res
    notes = [res.note]
    seen = seen | {inst.instrument_id}
    for rule in proxies_for(session, inst):
        src = _proxy_mark(session, rule, inst, as_of, seen)
        if src.status != "marked":
            notes.append(f"proxy {rule.source}: {src.note}")
            continue
        mult, spread = Decimal(rule.multiplier), Decimal(rule.spread)
        price = (src.price * mult + spread).quantize(_Q)
        return MarkResult(inst.instrument_id, "marked", price, f"proxy-{src.method}"[:34],
                          f"proxy {rule.source}{adjustment(mult, spread)}: {src.note}")
    return MarkResult(inst.instrument_id, "no-source", note="; ".join(notes))


def build_marks(session: Session, as_of: date, entered_by: str = "agents") -> list[MarkResult]:
    """Write automatic marks for every instrument live on `as_of`, traded or not."""
    instruments = session.scalars(
        select(Instrument).where(Instrument.last_trade_date >= as_of)
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
        res = _resolve(session, inst, as_of)
        if res.status == "marked":
            if existing is not None and Decimal(existing.price) == res.price:
                res.status = "unchanged"
            else:
                services.set_mark(session, inst.instrument_id, as_of, res.price,
                                  source=AUTO_PREFIX + res.method, comment=res.note,
                                  entered_by=entered_by)
        results.append(res)
    return results


def build_marks_between(session: Session, start: date, end: date,
                        entered_by: str = "agents") -> dict[date, list[MarkResult]]:
    """Build marks for each business day from start to end, and for `end` itself."""
    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    days = [d for d in days if cal.is_business_day(d) or d == end]
    return {d: build_marks(session, d, entered_by) for d in days}


@dataclass
class LoadResult:
    runs: list[AgentRun]
    marks: list[MarkResult]  # marks for the run date
    dates_marked: list[date]


def load_prices(session: Session, as_of: date, only: list[str] | None = None,
                backfill_days: int = 0, agents: list | None = None) -> LoadResult:
    """Run the agents, then mark every day from the earliest new price up to `as_of`.

    A new price for an earlier day changes later futures marks too (they average the month),
    so every day from there on is rebuilt. Marks entered by hand are kept.
    """
    last_id = session.scalar(select(func.max(MarketObservation.id))) or 0
    runs = agents_mod.run_agents(session, as_of, only=only, backfill_days=backfill_days,
                                 agents=agents)
    first_new = session.scalar(
        select(func.min(MarketObservation.obs_date))
        .where(MarketObservation.id > last_id, MarketObservation.obs_date <= as_of)
    )
    by_day = build_marks_between(session, min(first_new or as_of, as_of), as_of)
    return LoadResult(runs, by_day[as_of], sorted(by_day))


# --- proxy rules ------------------------------------------------------------------------

SERIES_PREFIX = "series:"


def add_proxy(session: Session, target: str, source: str, multiplier: Decimal | str = "1",
              spread: Decimal | str = "0", priority: int = 1, comment: str = "",
              entered_by: str = "") -> MarkProxy:
    """Add a rule. `target`: product code or instrument id. `source`: product code,
    instrument id, or `series:<name>` for a price series such as series:SILICONDATA.H100."""
    target, source = target.strip(), source.strip()
    if session.get(Product, target) is None and session.get(Instrument, target) is None:
        raise ValueError(f"Unknown product or instrument {target}")
    if source.startswith(SERIES_PREFIX):
        kind, source = "series", source.removeprefix(SERIES_PREFIX).strip()
        if not source:
            raise ValueError("Give a series name after series:")
    elif session.get(Product, source) is not None:
        kind = "product"
    elif session.get(Instrument, source) is not None:
        kind = "instrument"
    else:
        raise ValueError(f"Unknown source {source}: use a product code, an instrument id, "
                         "or series:<name>")
    if source == target:
        raise ValueError("A contract can't be its own proxy")
    try:
        mult, sprd = Decimal(str(multiplier)), Decimal(str(spread))
    except InvalidOperation:
        raise ValueError("Multiplier and spread must be numbers") from None
    rule = MarkProxy(target=target, source_kind=kind, source=source, multiplier=mult,
                     spread=sprd, priority=int(priority), comment=comment,
                     entered_by=entered_by)
    session.add(rule)
    session.flush()
    return rule


def list_proxies(session: Session) -> list[MarkProxy]:
    return session.scalars(
        select(MarkProxy).order_by(MarkProxy.target, MarkProxy.priority, MarkProxy.id)
    ).all()


def remove_proxy(session: Session, rule_id: int) -> None:
    rule = session.get(MarkProxy, rule_id)
    if rule is None:
        raise ValueError(f"No proxy rule {rule_id}")
    session.delete(rule)
    session.flush()


def known_series(session: Session) -> list[str]:
    """Price series that have data, for picking a proxy source."""
    stored = session.scalars(select(MarketObservation.series).distinct()).all()
    return sorted(set(stored) | set(INDEX_SERIES.values()))
