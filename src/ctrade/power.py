"""Forward power: ISO peak-hour calendars, power products, and loading curves from CSV.

A forward power product is one ISO, settlement point, settlement market (day-ahead or
real-time), delivery type (financial or physical) and time of use (on-peak, off-peak or
around-the-clock), e.g. PJM Western Hub DA financial on-peak. Its contract months are normal
instruments, so marks, positions, P&L, charts and spreads work as for compute. Prices are
$/MWh, quantities are MW, and a contract covers the month's hours in its time of use.

No free public source publishes forward power curves (exchange settlements need a licence),
so curves are loaded from CSV files. See TEMPLATE for the columns.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import tomllib
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import calendar as cal
from .agents import Observation, _store
from .agents.settlements import series_for
from .db import PowerSpec, Product
from .refdata import InstrumentSpec

ISO_CONFIG = Path(__file__).resolve().parents[2] / "config" / "isos.toml"
POWER = "power"  # Product.hours_rule and gpu_model for power products
TICK = Decimal("0.01")

TOU_CODES = {"onpeak": "ONPK", "offpeak": "OFFPK", "atc": "ATC"}
TOU_NAMES = {"onpeak": "On-peak", "offpeak": "Off-peak", "atc": "ATC"}


# --- ISO calendars ------------------------------------------------------------------------


@dataclass(frozen=True)
class IsoRules:
    code: str
    name: str
    peak_days: str = "weekdays"  # weekdays | mon-sat
    peak_start: int = 8  # first peak hour-ending
    peak_end: int = 23  # last peak hour-ending
    holidays: str = "nerc"  # nerc | none
    dst: bool = True

    @property
    def peak_hours_per_day(self) -> int:
        return self.peak_end - self.peak_start + 1


def load_isos(path: Path | None = None) -> dict[str, IsoRules]:
    return dict(_load_isos(str(path or ISO_CONFIG)))


@lru_cache
def _load_isos(path: str) -> tuple[tuple[str, IsoRules], ...]:
    with open(path, "rb") as f:
        data = tomllib.load(f).get("iso", {})
    return tuple((code.upper(), IsoRules(code=code.upper(), **cfg)) for code, cfg in data.items())


def iso_rules(iso: str) -> IsoRules:
    rules = load_isos().get(iso.upper())
    if rules is None:
        raise ValueError(f"Unknown ISO {iso}: add it to config/isos.toml")
    return rules


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """n-th weekday (Mon=0) of the month; n=-1 for the last one."""
    if n > 0:
        d = date(year, month, 1)
        d += timedelta(days=(weekday - d.weekday()) % 7)
        return d + timedelta(weeks=n - 1)
    d = cal.month_end(year, month)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def nerc_holidays(year: int) -> set[date]:
    """NERC off-peak holidays. A holiday on Sunday moves to Monday; Saturday ones stay."""
    fixed = [date(year, 1, 1), date(year, 7, 4), date(year, 12, 25)]
    days = {d + timedelta(days=1) if d.weekday() == 6 else d for d in fixed}
    days |= {
        _nth_weekday(year, 5, 0, -1),  # Memorial Day
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
    }
    return days


def is_peak_day(rules: IsoRules, d: date) -> bool:
    last_day = 4 if rules.peak_days == "weekdays" else 5
    if d.weekday() > last_day:
        return False
    return not (rules.holidays == "nerc" and d in nerc_holidays(d.year))


def month_hours(iso: str, year: int, month: int, time_of_use: str) -> int:
    """Hours in a delivery month for a time of use, e.g. PJM on-peak Jan 2027 = 336."""
    rules = iso_rules(iso)
    start, end = cal.month_start(year, month), cal.month_end(year, month)
    days = (end - start).days + 1
    peak_days = sum(is_peak_day(rules, start + timedelta(days=n)) for n in range(days))
    onpeak = peak_days * rules.peak_hours_per_day
    atc = days * 24 + (rules.dst and {3: -1, 11: 1}.get(month, 0))
    return {"onpeak": onpeak, "offpeak": atc - onpeak, "atc": atc}[time_of_use]


# --- normalising CSV values ---------------------------------------------------------------

_TOU = {
    "onpeak": "onpeak", "on": "onpeak", "peak": "onpeak", "onpk": "onpeak", "pk": "onpeak",
    "5x16": "onpeak", "6x16": "onpeak",
    "offpeak": "offpeak", "off": "offpeak", "offpk": "offpeak", "op": "offpeak",
    "2x16": "offpeak", "7x8": "offpeak", "wrap": "offpeak",
    "atc": "atc", "aroundtheclock": "atc", "7x24": "atc", "flat": "atc", "baseload": "atc",
}
_MARKET = {"da": "DA", "dayahead": "DA", "rt": "RT", "realtime": "RT"}
_DELIVERY = {"financial": "financial", "fin": "financial", "f": "financial",
             "physical": "physical", "phys": "physical", "p": "physical"}
_MONTHS = {m.lower(): n for n, m in enumerate(
    ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])
    if m}


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9x]", "", text.lower())


def _lookup(table: dict[str, str], text: str, what: str) -> str:
    value = table.get(_key(text))
    if value is None:
        raise ValueError(f"unknown {what} {text!r}")
    return value


def parse_time_of_use(text: str) -> str:
    return _lookup(_TOU, text, "time of use (use onpeak, offpeak or ATC)")


def parse_market(text: str) -> str:
    return _lookup(_MARKET, text, "settlement (use Day-Ahead or Real-Time)")


def parse_delivery(text: str) -> str:
    return _lookup(_DELIVERY, text, "delivery (use physical or financial)")


def parse_month(text: str) -> str:
    """'2027-01', 'Jan27', 'Jan-27', 'Jan 2027' or '01/2027' to '2027-01'."""
    t = text.strip()
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})(-\d{1,2})?", t):
        year, month = int(m[1]), int(m[2])
    elif m := re.fullmatch(r"(\d{1,2})/(\d{4})", t):
        year, month = int(m[2]), int(m[1])
    elif (m := re.fullmatch(r"([A-Za-z]{3})[a-z]*[\s\-']?(\d{2}|\d{4})", t)) and \
            m[1].lower() in _MONTHS:
        month, year = _MONTHS[m[1].lower()], int(m[2])
        year += 2000 if year < 100 else 0
    else:
        raise ValueError(f"unknown contract month {text!r} (use e.g. 2027-01 or Jan27)")
    if not 1 <= month <= 12:
        raise ValueError(f"unknown contract month {text!r}")
    return f"{year:04d}-{month:02d}"


# --- products and instruments -------------------------------------------------------------


def _point_code(point: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", point.upper())[:16] or "POINT"


def product_code(iso: str, point: str, market: str, delivery: str, time_of_use: str) -> str:
    """e.g. PJM.WESTERNHUB.DA.F.ONPK (at most 40 characters)."""
    return (f"{iso.upper()[:8]}.{_point_code(point)}.{market}.{delivery[0].upper()}."
            f"{TOU_CODES[time_of_use]}")


def describe(spec: PowerSpec) -> str:
    """e.g. PJM Western Hub DA financial on-peak."""
    market = {"DA": "day-ahead", "RT": "real-time"}[spec.market]
    tou = {"onpeak": "on-peak", "offpeak": "off-peak", "atc": "ATC"}[spec.time_of_use]
    return f"{spec.iso} {spec.settlement_point} {market} {spec.delivery} {tou}"


def ensure_product(session: Session, iso: str, point: str, market: str, delivery: str,
                   time_of_use: str, point_type: str = "") -> Product:
    """The product for this curve, created on first use."""
    iso = iso_rules(iso).code
    point = " ".join(point.split())
    code = product_code(iso, point, market, delivery, time_of_use)
    spec = session.get(PowerSpec, code)
    if spec is not None and spec.settlement_point.upper() != point.upper():
        # Two long names that shorten to the same code: tell them apart with a hash.
        digest = hashlib.sha1(point.upper().encode()).hexdigest()[:3].upper()
        code = code.replace(f".{_point_code(point)}.", f".{_point_code(point)[:13]}{digest}.")
        spec = session.get(PowerSpec, code)
    product = session.get(Product, code)
    if product is None:
        product = Product(code=code, exchange=iso, symbol=TOU_CODES[time_of_use],
                          name="", gpu_model=POWER, index_name="", hours_rule=POWER,
                          fixed_hours=None, tick_size=TICK, provisional=False,
                          notes="Forward power, $/MWh per MW. Loaded from CSV.")
        session.add(product)
        session.flush()
    if spec is None:
        spec = PowerSpec(product_code=code, iso=iso, settlement_point=point,
                         point_type=point_type.strip().lower(), market=market,
                         delivery=delivery, time_of_use=time_of_use)
        session.add(spec)
        product.name = describe(spec)
        product.index_name = f"{iso} {point} {market} LMP"
        session.flush()
    return product


def instrument_spec(session: Session, product_code: str, month: str) -> InstrumentSpec:
    """A monthly power contract: hours for 1 MW in its time of use. Trading stops on the last
    business day before the delivery month, so positions close at that day's mark."""
    spec = session.get(PowerSpec, product_code)
    if spec is None:
        raise ValueError(f"{product_code} is not a power product")
    year, mon = cal.parse_month(month)
    start, end = cal.month_start(year, mon), cal.month_end(year, mon)
    return InstrumentSpec(
        instrument_id=f"{product_code}.{year:04d}-{mon:02d}",
        product_code=product_code,
        kind="future",
        delivery_start=start,
        delivery_end=end,
        last_trade_date=cal.previous_business_day(start),
        hours_per_contract=Decimal(month_hours(spec.iso, year, mon, spec.time_of_use)),
    )


def specs(session: Session) -> list[PowerSpec]:
    return session.scalars(select(PowerSpec).order_by(PowerSpec.product_code)).all()


# --- loading curves from CSV --------------------------------------------------------------

TEMPLATE = """\
trade_date,iso,contract_month,settlement_point,point_type,settlement,delivery,time_of_use,price
2026-10-23,PJM,2027-01,Western Hub,hub,Day-Ahead,Financial,onpeak,78.50
2026-10-23,PJM,2027-01,Western Hub,hub,Day-Ahead,Financial,offpeak,62.25
2026-10-23,PJM,2027-01,Western Hub,hub,Day-Ahead,Financial,ATC,69.80
2026-10-23,MISO,2027-01,Indiana Hub,hub,Real-Time,Financial,onpeak,64.10
2026-10-23,ERCOT,2027-01,HB_NORTH,hub,Real-Time,Financial,onpeak,52.75
2026-10-23,ERCOT,2027-01,LZ_HOUSTON,zone,Real-Time,Physical,ATC,45.40
"""

# Accepted column names, in lower case without spaces or underscores.
COLUMNS = {
    "trade_date": ["tradedate", "date", "asof", "asofdate", "pricedate", "curvedate"],
    "iso": ["iso", "rto", "isorto", "market_operator"],
    "contract_month": ["contractmonth", "month", "deliverymonth", "contract"],
    "settlement_point": ["settlementpoint", "point", "location", "hub", "node", "zone"],
    "point_type": ["pointtype", "locationtype", "type"],
    "settlement": ["settlement", "market", "settlementmarket", "lmp"],
    "delivery": ["delivery", "physicalfinancial", "contracttype", "deliverytype"],
    "time_of_use": ["timeofuse", "tou", "block", "shape", "period"],
    "price": ["price", "settle", "settlementprice", "mark", "value"],
}
REQUIRED = [c for c in COLUMNS if c != "point_type"]


@dataclass
class CurveRow:
    trade_date: date
    iso: str
    month: str
    point: str
    point_type: str
    market: str
    delivery: str
    time_of_use: str
    price: Decimal


@dataclass
class LoadResult:
    rows: int = 0
    new_prices: int = 0
    products: set[str] = field(default_factory=set)
    dates: list[date] = field(default_factory=list)
    marks: int = 0


def _map_columns(header: list[str]) -> dict[str, str]:
    norm = {re.sub(r"[\s_\-/()]", "", h.lower()): h for h in header if h}
    found = {}
    for col, names in COLUMNS.items():
        for name in [col.replace("_", ""), *names]:
            if name in norm:
                found[col] = norm[name]
                break
    missing = [c for c in REQUIRED if c not in found]
    if missing:
        raise ValueError(f"missing column(s): {', '.join(missing)}. Expected: "
                         f"{', '.join(COLUMNS)}")
    return found


def parse_csv(text: str) -> list[CurveRow]:
    """Check every row; raise one ValueError listing the problems (nothing is loaded)."""
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    cols = _map_columns(reader.fieldnames or [])
    rows, errors = [], []
    for n, raw in enumerate(reader, start=2):
        if not any((v or "").strip() for v in raw.values()):
            continue
        get = {c: (raw.get(h) or "").strip() for c, h in cols.items()}
        try:
            try:
                price = Decimal(get["price"].replace("$", "").replace(",", ""))
            except InvalidOperation:
                raise ValueError(f"price {get['price']!r} is not a number") from None
            if not get["settlement_point"]:
                raise ValueError("settlement_point is empty")
            rows.append(CurveRow(
                trade_date=date.fromisoformat(get["trade_date"]),
                iso=iso_rules(get["iso"]).code,
                month=parse_month(get["contract_month"]),
                point=get["settlement_point"],
                point_type=get.get("point_type", ""),
                market=parse_market(get["settlement"]),
                delivery=parse_delivery(get["delivery"]),
                time_of_use=parse_time_of_use(get["time_of_use"]),
                price=price,
            ))
        except ValueError as e:
            errors.append(f"line {n}: {e}")
    if errors:
        more = f" (and {len(errors) - 10} more)" if len(errors) > 10 else ""
        raise ValueError("; ".join(errors[:10]) + more)
    if not rows:
        raise ValueError("the file has no price rows")
    return rows


def load_curves(session: Session, text: str, source: str = "csv upload") -> LoadResult:
    """Load forward power prices and mark every contract for each date in the file.

    Each price is stored as the contract's settlement for its trade date, so it becomes the
    mark unless a mark was entered by hand. Reloading the same file changes nothing.
    """
    from . import marking, services  # marking imports this module's callers

    rows = parse_csv(text)
    result = LoadResult(rows=len(rows))
    observations = []
    for r in rows:
        product = ensure_product(session, r.iso, r.point, r.market, r.delivery, r.time_of_use,
                                 r.point_type)
        inst = services.ensure_future(session, product.code, r.month)
        result.products.add(product.code)
        observations.append(Observation(series=series_for(inst.instrument_id),
                                        obs_date=r.trade_date, value=r.price, method="file",
                                        source_url=source, unit="USD/MWh"))
    result.new_prices = _store(session, SimpleNamespace(name="power-csv"), observations)
    result.dates = sorted({r.trade_date for r in rows})
    for d in result.dates:
        result.marks += sum(res.status == "marked"
                            for res in marking.build_marks(session, d))
    return result
