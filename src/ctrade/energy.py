"""Energy cost of compute: what the power behind a GPU-hour costs at forward power prices.

    energy $/GPU-hour = power price $/MWh x kW per GPU x PUE / 1,000

`kW per GPU` is the server's power draw divided by its GPUs (GPU, CPUs, memory, network,
fans), and PUE adds the data centre's cooling and losses. None of the compute contract specs
publish a conversion factor, so the defaults below come from NVIDIA's DGX system maximum power
(per GPU) and a typical PUE. Change them to your own figures with `set_factor`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import analytics, services
from .db import EnergyFactor, Instrument, Product, utcnow

DEFAULT_PUE = Decimal("1.3")
# kW per GPU at the server: DGX system maximum power / 8 GPUs.
DEFAULT_KW: dict[str, tuple[Decimal, str]] = {
    "H100": (Decimal("1.275"), "DGX H100: 10.2 kW max for 8 GPUs"),
    "H200": (Decimal("1.275"), "DGX H200: 10.2 kW max for 8 GPUs"),
    "B200": (Decimal("1.8"), "DGX B200: about 14.3 kW max for 8 GPUs"),
    "A100": (Decimal("0.8125"), "DGX A100: 6.5 kW max for 8 GPUs"),
}


@dataclass(frozen=True)
class Factor:
    gpu_model: str
    kw_per_gpu: Decimal
    pue: Decimal
    comment: str
    is_default: bool

    @property
    def mwh_per_gpu_hour(self) -> Decimal:
        return self.kw_per_gpu * self.pue / 1000


def factor(session: Session, gpu_model: str) -> Factor:
    row = session.get(EnergyFactor, gpu_model)
    if row is not None:
        return Factor(gpu_model, Decimal(row.kw_per_gpu), Decimal(row.pue), row.comment, False)
    if gpu_model in DEFAULT_KW:
        kw, note = DEFAULT_KW[gpu_model]
        return Factor(gpu_model, kw, DEFAULT_PUE, f"default: {note}, PUE {DEFAULT_PUE}", True)
    raise ValueError(f"No energy factor for {gpu_model}: set one with kW per GPU and PUE")


def factors(session: Session) -> list[Factor]:
    models = set(DEFAULT_KW) | set(session.scalars(select(EnergyFactor.gpu_model)))
    return [factor(session, m) for m in sorted(models)]


def set_factor(session: Session, gpu_model: str, kw_per_gpu, pue=DEFAULT_PUE,
               comment: str = "", entered_by: str = "") -> Factor:
    kw, p = Decimal(str(kw_per_gpu)), Decimal(str(pue))
    if kw <= 0 or p < 1:
        raise ValueError("kW per GPU must be positive and PUE at least 1")
    row = session.get(EnergyFactor, gpu_model) or EnergyFactor(gpu_model=gpu_model)
    row.kw_per_gpu, row.pue, row.comment = kw, p, comment
    row.entered_by, row.updated_at = entered_by, utcnow()
    session.merge(row)
    session.flush()
    return factor(session, gpu_model)


def reset_factor(session: Session, gpu_model: str) -> None:
    row = session.get(EnergyFactor, gpu_model)
    if row is not None:
        session.delete(row)
        session.flush()


def _gpu_model(session: Session, compute_product: str) -> str:
    product = session.get(Product, compute_product)
    if product is None:
        raise ValueError(f"Unknown product {compute_product}")
    return product.gpu_model


COLUMNS = ["month", "contract", "compute", "power", "energy", "total", "hours",
           "energy_per_contract", "total_per_contract"]


def cost_curve(session: Session, compute_product: str, power_product: str, as_of: date
               ) -> pd.DataFrame:
    """Energy cost and all-in cost for each compute contract month that has a power price for the
    same delivery month on `as_of`.

    Columns: month, contract, compute ($/GPU-hr mark), power ($/MWh), energy ($/GPU-hr),
    total (all-in cost: compute + energy), hours (GPU-hours per contract),
    energy_per_contract and total_per_contract ($).
    """
    f = factor(session, _gpu_model(session, compute_product))
    compute = analytics.term_structure(session, compute_product, [as_of])
    power = analytics.term_structure(session, power_product, [as_of])
    power_by_month = dict(zip(power["month"], power["mark"], strict=True))
    hours = {i.instrument_id: Decimal(i.hours_per_contract) for i in session.scalars(
        select(Instrument).where(Instrument.product_code == compute_product))}
    rows = []
    for r in compute.itertuples():
        if r.month not in power_by_month:
            continue
        energy = Decimal(str(power_by_month[r.month])) * f.mwh_per_gpu_hour
        mark = Decimal(str(r.mark))
        h = hours[r.instrument]
        rows.append({"month": r.month, "contract": r.contract, "compute": float(mark),
                     "power": power_by_month[r.month], "energy": float(energy),
                     "total": float(mark + energy), "hours": float(h),
                     "energy_per_contract": float(energy * h),
                     "total_per_contract": float((mark + energy) * h)})
    return pd.DataFrame(rows, columns=COLUMNS)


def cost_history(session: Session, compute_instrument: str, power_instrument: str,
                 start: date | None = None, end: date | None = None) -> pd.DataFrame:
    """Daily compute mark, energy cost ($/GPU-hr) and all-in cost for one pair of contracts, on
    dates where both have a mark. Columns: date, compute, energy, total
    (compute + energy)."""
    inst = session.get(Instrument, compute_instrument)
    if inst is None:
        raise ValueError(f"Unknown instrument {compute_instrument}")
    f = factor(session, _gpu_model(session, inst.product_code))
    by_date: dict[date, dict[str, Decimal]] = {}
    for m in services.mark_history(session, [compute_instrument, power_instrument], start, end):
        by_date.setdefault(m.as_of_date, {})[m.instrument_id] = Decimal(m.price)
    rows = []
    for d, p in sorted(by_date.items()):
        if len(p) == 2:
            energy = p[power_instrument] * f.mwh_per_gpu_hour
            rows.append({"date": d, "compute": float(p[compute_instrument]),
                         "energy": float(energy),
                         "total": float(p[compute_instrument] + energy)})
    return pd.DataFrame(rows, columns=["date", "compute", "energy", "total"])
