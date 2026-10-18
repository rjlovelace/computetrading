"""Common pieces for market data agents.

An agent collects prices from one source and returns them as `Observation`s. It never writes
marks itself: `ctrade.marking` turns stored observations into marks, so every mark can be
traced back to the observations behind it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

USER_AGENT = "ctrade-market-data/0.2 (+https://github.com/rjlovelace/computetrading)"


@dataclass(frozen=True)
class Observation:
    series: str  # e.g. "ORNN.OCPI.H100 SXM"
    obs_date: date
    value: Decimal
    method: str = "api"
    source_url: str = ""
    detail: str = ""
    unit: str = "USD/GPU-hr"


class AgentSkipped(Exception):
    """The agent can't run here (e.g. missing API key); not an error in the source."""


class Agent(Protocol):
    name: str
    description: str

    def collect(self, as_of: date, backfill_days: int = 0) -> list[Observation]:
        """Fetch observations up to `as_of`, plus `backfill_days` of history if supported."""
        ...
