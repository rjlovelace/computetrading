"""Reference data: the compute contracts listed (or announced) on CME, ICE and Nodal.

Specs come from exchange notices and announcements as of 2026-10-17 (see docs/design.md,
section 9). Anything not yet published by the exchange is marked `provisional=True` and
must be checked against the final rulebook before trading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from . import calendar as cal

FIXED = "fixed"  # every contract covers the same number of hours
CALENDAR = "calendar"  # contract covers the clock hours in its month


@dataclass(frozen=True)
class ProductSpec:
    code: str
    exchange: str
    symbol: str
    name: str
    gpu_model: str
    index_name: str
    hours_rule: str
    fixed_hours: int | None
    tick_size: Decimal
    provisional: bool
    notes: str = ""


PRODUCTS: list[ProductSpec] = [
    ProductSpec(
        code="CME.GPU1",
        exchange="CME",
        symbol="GPU1",
        name="Silicon Data H100 Rental Index Futures",
        gpu_model="H100",
        index_name="Silicon Data Daily H100 Rental Index (SDH100RT)",
        hours_rule=FIXED,
        fixed_hours=730,
        tick_size=Decimal("0.01"),
        provisional=True,
        notes="730 GPU-hours and $7.30/tick published; last trade day assumed last business day.",
    ),
    ProductSpec(
        code="CME.GPU2",
        exchange="CME",
        symbol="GPU2",
        name="Silicon Data B200 Rental Index Futures",
        gpu_model="B200",
        index_name="Silicon Data Daily B200 Rental Index (SDRB200RT)",
        hours_rule=FIXED,
        fixed_hours=730,
        tick_size=Decimal("0.01"),
        provisional=True,
        notes="Specs not yet published; assumed to match GPU1.",
    ),
    ProductSpec(
        code="ICE.HPR",
        exchange="ICE",
        symbol="HPR",
        name="OCPI H100 Future",
        gpu_model="H100",
        index_name="Ornn Compute Price Index H100",
        hours_rule=CALENDAR,
        fixed_hours=None,
        tick_size=Decimal("0.001"),
        provisional=False,
        notes="Per ICE notice 2026-09-29; final settlement = average of daily OCPI in month.",
    ),
    ProductSpec(
        code="ICE.BKL",
        exchange="ICE",
        symbol="BKL",
        name="OCPI B200 Future",
        gpu_model="B200",
        index_name="Ornn Compute Price Index B200",
        hours_rule=CALENDAR,
        fixed_hours=None,
        tick_size=Decimal("0.001"),
        provisional=False,
        notes="Per ICE notice 2026-09-29; final settlement = average of daily OCPI in month.",
    ),
    ProductSpec(
        code="ICE.COIL",
        exchange="ICE",
        symbol="COIL",
        name="GPU compute future on NATIVX COIL index",
        gpu_model="MULTI",
        index_name="NATIVX COIL (energy-normalised)",
        hours_rule=CALENDAR,
        fixed_hours=None,
        tick_size=Decimal("0.001"),
        provisional=True,
        notes="Announced 2026-07-01; symbol and specs are placeholders.",
    ),
    ProductSpec(
        code="NODAL.H100",
        exchange="NODAL",
        symbol="H100",
        name="Compute Desk Hopper GPU future",
        gpu_model="H100",
        index_name="Compute Desk daily blended Hopper index",
        hours_rule=CALENDAR,
        fixed_hours=None,
        tick_size=Decimal("0.001"),
        provisional=True,
        notes="Announced 2026-09-04; symbol and specs are placeholders.",
    ),
    ProductSpec(
        code="NODAL.B200",
        exchange="NODAL",
        symbol="B200",
        name="Compute Desk Blackwell GPU future",
        gpu_model="B200",
        index_name="Compute Desk daily blended Blackwell index",
        hours_rule=CALENDAR,
        fixed_hours=None,
        tick_size=Decimal("0.001"),
        provisional=True,
        notes="Announced 2026-09-04; symbol and specs are placeholders.",
    ),
]

PRODUCTS_BY_CODE = {p.code: p for p in PRODUCTS}


@dataclass(frozen=True)
class InstrumentSpec:
    instrument_id: str
    product_code: str
    kind: str  # "future" or "spot"
    delivery_start: date
    delivery_end: date
    last_trade_date: date
    hours_per_contract: Decimal


def future_instrument(product_code: str, month: str) -> InstrumentSpec:
    """Build the monthly futures instrument for a product, e.g. ('ICE.HPR', '2026-12')."""
    product = PRODUCTS_BY_CODE[product_code]
    year, mon = cal.parse_month(month)
    start, end = cal.month_start(year, mon), cal.month_end(year, mon)
    if product.hours_rule == FIXED:
        hours = Decimal(product.fixed_hours)
    else:
        hours = Decimal(cal.hours_between(start, end))
    return InstrumentSpec(
        instrument_id=f"{product_code}.{year:04d}-{mon:02d}",
        product_code=product_code,
        kind="future",
        delivery_start=start,
        delivery_end=end,
        last_trade_date=cal.last_business_day(year, mon),
        hours_per_contract=hours,
    )


def spot_instrument(gpu_model: str, start: date, end: date, venue: str = "OTC") -> InstrumentSpec:
    """A spot/term rental block: one contract = one GPU for every hour from start to end."""
    return InstrumentSpec(
        instrument_id=f"SPOT.{venue}.{gpu_model}.{start.isoformat()}.{end.isoformat()}",
        product_code=f"SPOT.{gpu_model}",
        kind="spot",
        delivery_start=start,
        delivery_end=end,
        last_trade_date=end,
        hours_per_contract=Decimal(cal.hours_between(start, end)),
    )
