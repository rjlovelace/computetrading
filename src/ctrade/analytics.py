"""Chart data: term structures, spreads and moving averages, built from stored marks.

Everything here returns pandas DataFrames ready to chart. Prices are $ per GPU-hour and
each date uses the mark in force for that date (the latest one recorded).
"""

from __future__ import annotations

import calendar as _cal
from datetime import date
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import services
from .db import Instrument, Mark, Product, Spread

MONTHS = {name.lower(): n for n, name in enumerate(_cal.month_abbr) if name}


# --- contract names -----------------------------------------------------------------------


def month_label(instrument: Instrument) -> str:
    """Short contract month name, e.g. Jan27; the instrument id for anything not monthly."""
    if instrument.kind != "future":
        return instrument.instrument_id
    d = instrument.delivery_start
    return f"{_cal.month_abbr[d.month]}{d.year % 100:02d}"


def contract_name(instrument: Instrument) -> str:
    """Product and month, e.g. CME.GPU2 Jan27."""
    if instrument.kind != "future":
        return instrument.instrument_id
    return f"{instrument.product_code} {month_label(instrument)}"


def resolve_contract(session: Session, text: str) -> Instrument:
    """Find a contract from an instrument id (CME.GPU1.2027-01) or a name (CME.GPU1 Jan27)."""
    text = " ".join(text.split())
    inst = session.get(Instrument, text)
    if inst is not None:
        return inst
    product, _, month = text.rpartition(" ")
    if product and session.get(Product, product) is not None:
        key = month.lower()
        if len(key) == 5 and key[:3] in MONTHS and key[3:].isdigit():
            month = f"20{key[3:]}-{MONTHS[key[:3]]:02d}"
        try:
            return services.ensure_future(session, product, month)
        except (ValueError, KeyError):
            pass
    raise ValueError(f"Unknown contract {text!r}: use e.g. CME.GPU1 Jan27 or CME.GPU1.2027-01")


def unit(session: Session, product_code: str) -> str:
    """Price unit: $/MWh for forward power, $/GPU-hour for compute."""
    product = session.get(Product, product_code)
    return "$/MWh" if product is not None and product.hours_rule == "power" else "$/GPU-hour"


# --- term structure -----------------------------------------------------------------------


def curve_dates(session: Session, product_code: str, on_or_before: date | None = None
                ) -> list[date]:
    """Dates on which any contract month of the product has a mark, newest first."""
    query = (select(Mark.as_of_date)
             .join(Instrument, Mark.instrument_id == Instrument.instrument_id)
             .where(Instrument.product_code == product_code, Instrument.kind == "future")
             .distinct())
    if on_or_before is not None:
        query = query.where(Mark.as_of_date <= on_or_before)
    return sorted(session.scalars(query).all(), reverse=True)


def term_structure(session: Session, product_code: str, dates: list[date]) -> pd.DataFrame:
    """One row per date and contract month: columns date, month, contract, instrument, mark.

    Only months still trading on each date are included, ordered by delivery month.
    """
    instruments = session.scalars(
        select(Instrument).where(Instrument.product_code == product_code,
                                 Instrument.kind == "future")
        .order_by(Instrument.delivery_start)
    ).all()
    marks = services.mark_map(session, dates)
    rows = []
    for d in dates:
        for inst in instruments:
            if inst.last_trade_date < d or (inst.instrument_id, d) not in marks:
                continue
            rows.append({"date": d, "month": inst.delivery_start, "contract": month_label(inst),
                         "instrument": inst.instrument_id,
                         "mark": float(marks[(inst.instrument_id, d)])})
    return pd.DataFrame(rows, columns=["date", "month", "contract", "instrument", "mark"])


def curve_changes(session: Session, product_code: str, dates: list[date]) -> pd.DataFrame:
    """Day over day change of the curve: each date's marks less the previous marked date's.

    Columns date, prior, month, contract, instrument, change. Months missing on either date
    are left out.
    """
    all_dates = curve_dates(session, product_code)
    priors = {}
    for d in dates:
        earlier = [x for x in all_dates if x < d]
        if earlier:
            priors[d] = max(earlier)
    curves = term_structure(session, product_code, sorted(set(dates) | set(priors.values())))
    by_key = {(r.date, r.instrument): r.mark for r in curves.itertuples()}
    rows = []
    for r in curves.itertuples():
        if r.date not in priors:
            continue
        prior_mark = by_key.get((priors[r.date], r.instrument))
        if prior_mark is None:
            continue
        rows.append({"date": r.date, "prior": priors[r.date], "month": r.month,
                     "contract": r.contract, "instrument": r.instrument,
                     "change": r.mark - prior_mark})
    return pd.DataFrame(rows, columns=["date", "prior", "month", "contract", "instrument",
                                       "change"])


# --- spreads ------------------------------------------------------------------------------


def parse_legs(text: str) -> list[tuple[str, Decimal]]:
    legs = []
    for line in text.splitlines():
        if line.strip():
            iid, weight = line.rsplit(" ", 1)
            legs.append((iid.strip(), Decimal(weight)))
    return legs


def _weight_text(w: Decimal) -> str:
    return format(w.normalize(), "f")


def describe_legs(session: Session, legs: list[tuple[str, Decimal]]) -> str:
    """e.g. CME.GPU2 Jan27 - CME.GPU1 Jan27, or 2 x A - B for other weights."""
    parts = []
    for i, (iid, w) in enumerate(legs):
        inst = session.get(Instrument, iid)
        name = contract_name(inst) if inst is not None else iid
        size = abs(w)
        term = name if size == 1 else f"{_weight_text(size)} x {name}"
        if i == 0:
            parts.append(term if w > 0 else f"-{term}")
        else:
            parts.append(f"{'+' if w > 0 else '-'} {term}")
    return " ".join(parts)


def add_spread(session: Session, legs: list[tuple[str, Decimal | str | int]],
               name: str = "", entered_by: str = "") -> Spread:
    """Save a spread. Each leg is (contract, weight); a contract is an id or a name like
    CME.GPU1 Jan27. The usual calendar or product spread is A with +1 and B with -1."""
    if len(legs) < 2:
        raise ValueError("A spread needs at least two legs")
    resolved = []
    for contract, weight in legs:
        w = Decimal(str(weight))
        if w == 0:
            raise ValueError("Leg weights can't be zero")
        resolved.append((resolve_contract(session, contract).instrument_id, w))
    if len({iid for iid, _ in resolved}) < len(resolved):
        raise ValueError("Each leg must be a different contract")
    name = (name or describe_legs(session, resolved)).strip()
    if session.scalar(select(Spread).where(Spread.name == name)) is not None:
        raise ValueError(f"A spread called {name!r} already exists")
    spread = Spread(name=name, legs="\n".join(f"{iid} {_weight_text(w)}" for iid, w in resolved),
                    entered_by=entered_by)
    session.add(spread)
    session.flush()
    return spread


def list_spreads(session: Session) -> list[Spread]:
    return session.scalars(select(Spread).order_by(Spread.name)).all()


def remove_spread(session: Session, spread_id: int) -> None:
    spread = session.get(Spread, spread_id)
    if spread is None:
        raise ValueError(f"No spread {spread_id}")
    session.delete(spread)
    session.flush()


def spread_history(session: Session, legs: list[tuple[str, Decimal]], start: date | None = None,
                   end: date | None = None) -> pd.DataFrame:
    """Spread value on each date every leg has a mark, and its change from the previous one.

    Columns date, value, change ($ per GPU-hour). The first row's change is empty.
    """
    weights = dict(legs)
    marks = services.mark_history(session, list(weights), start, end)
    by_date: dict[date, dict[str, Decimal]] = {}
    for m in marks:
        by_date.setdefault(m.as_of_date, {})[m.instrument_id] = Decimal(m.price)
    rows = [{"date": d, "value": float(sum(weights[i] * p[i] for i in weights))}
            for d, p in sorted(by_date.items()) if len(p) == len(weights)]
    df = pd.DataFrame(rows, columns=["date", "value"])
    df["change"] = df["value"].diff()
    return df


# --- moving averages ----------------------------------------------------------------------

MA_KINDS = {"SMA": "simple", "EMA": "exponential"}


def moving_average(values: pd.Series, window: int, kind: str = "SMA") -> pd.Series:
    """Simple (SMA) or exponential (EMA) moving average over `window` observations.

    The SMA is empty until a full window is available; the EMA starts at the first value.
    """
    if window < 1:
        raise ValueError("The window must be at least 1")
    if kind == "SMA":
        return values.rolling(window, min_periods=window).mean()
    if kind == "EMA":
        return values.ewm(span=window, adjust=False).mean()
    raise ValueError(f"Unknown moving average {kind}: use SMA or EMA")


def with_moving_averages(df: pd.DataFrame, value: str, group: str | None,
                         studies: list[tuple[str, int]]) -> pd.DataFrame:
    """Long table for charting: the original rows plus one series per moving average.

    Adds columns `series` (what the line is) and `study` ("" for the data itself). Rows must be
    in date order within each group.
    """
    base = df.copy()
    base["series"] = base[group] if group else value
    base["study"] = ""
    out = [base]
    groups = base.groupby(group, sort=False) if group else [(None, base)]
    for key, part in groups:
        for kind, window in studies:
            ma = part.copy()
            ma[value] = moving_average(part[value], window, kind).to_numpy()
            label = f"{kind} {window}"
            ma["series"] = f"{key} {label}" if key is not None else label
            ma["study"] = label
            out.append(ma.dropna(subset=[value]))
    return pd.concat(out, ignore_index=True)
