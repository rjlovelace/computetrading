"""Settlement-file agent: exchange settlement prices from CSV files you drop in a folder.

The compute futures aren't trading yet, and exchange settlement data needs a licence or a
broker feed. Until then, put settlement files (from your broker's daily statement or an
exchange download) in data/settlements/. Columns: instrument_id, date, settle. Example:

    instrument_id,date,settle
    ICE.HPR.2026-12,2026-11-02,2.415
"""

from __future__ import annotations

import csv
import os
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from .base import AgentSkipped, Observation

SERIES_PREFIX = "SETTLE."
DEFAULT_DIR = Path(os.environ.get("CTRADE_SETTLEMENTS_DIR", "data/settlements"))


def series_for(instrument_id: str) -> str:
    return SERIES_PREFIX + instrument_id


class SettlementFileAgent:
    name = "settlements"
    description = "Exchange settlement prices from CSV files in data/settlements/"

    def __init__(self, folder: Path = DEFAULT_DIR):
        self.folder = Path(folder)

    def collect(self, as_of: date, backfill_days: int = 0) -> list[Observation]:
        files = sorted(self.folder.glob("*.csv")) if self.folder.exists() else []
        if not files:
            raise AgentSkipped(f"no settlement files in {self.folder}/")
        start = as_of - timedelta(days=backfill_days)
        out = []
        for path in files:
            with open(path, newline="") as f:
                for row in csv.DictReader(f):
                    d = date.fromisoformat(row["date"].strip())
                    if start <= d <= as_of:
                        out.append(
                            Observation(
                                series=series_for(row["instrument_id"].strip()),
                                obs_date=d,
                                value=Decimal(row["settle"].strip()),
                                method="file",
                                source_url=str(path),
                            )
                        )
        return out
