"""Ornn Compute Price Index (OCPI): the index ICE's HPR and BKL futures settle against.

Uses Ornn's public API (https://dashboard.ornnai.com/docs). No key is needed for the free
tier: latest daily values plus three months of history for A100 SXM4, B200, H100 SXM, H200
and RTX 5090. Set ORNN_API_KEY to unlock full history and more GPUs.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta
from decimal import Decimal
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from .base import USER_AGENT, Observation

BASE_URL = "https://api.ornnai.com"
SERIES_PREFIX = "ORNN.OCPI."
# Daily settlements are stamped in UTC (e.g. 20:00Z); the trading date is the US date.
_TRADING_TZ = ZoneInfo("America/New_York")


def series_for(gpu: str) -> str:
    return SERIES_PREFIX + gpu


def _trading_date(timestamp: str) -> date:
    ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return ts.astimezone(_TRADING_TZ).date()


class OrnnAgent:
    name = "ornn"
    description = "Ornn OCPI daily GPU rental index (settles ICE HPR/BKL), public API"

    def __init__(self, client: httpx.Client | None = None, api_key: str | None = None):
        self.api_key = api_key if api_key is not None else os.environ.get("ORNN_API_KEY", "")
        headers = {"User-Agent": USER_AGENT}
        if self.api_key:
            # Not confirmed against Ornn's authentication page; the free tier needs no key.
            headers["Authorization"] = f"Bearer {self.api_key}"
        self.client = client or httpx.Client(base_url=BASE_URL, headers=headers, timeout=30)

    def _get(self, path: str, **params) -> dict:
        resp = self.client.get(path, params=params or None)
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success", False):
            raise RuntimeError(f"Ornn API returned success=false for {path}")
        return body

    def collect(self, as_of: date, backfill_days: int = 0) -> list[Observation]:
        latest = self._get("/api/daily-index/all")
        out = [
            Observation(
                series=series_for(row["gpu_type"]),
                obs_date=_trading_date(row["date"]),
                value=Decimal(str(row["index_value"])),
                source_url=f"{BASE_URL}/api/daily-index/all",
            )
            for row in latest["data"]
            if _trading_date(row["date"]) <= as_of
        ]
        if backfill_days:
            start = as_of - timedelta(days=backfill_days)
            for gpu in sorted({row["gpu_type"] for row in latest["data"]}):
                path = f"/api/gpu/{quote(gpu)}/index-history"
                hist = self._get(path, startDate=start.isoformat(), endDate=as_of.isoformat())
                out.extend(
                    Observation(
                        series=series_for(gpu),
                        obs_date=_trading_date(row["timestamp"]),
                        value=Decimal(str(row["index_value"])),
                        source_url=BASE_URL + path,
                    )
                    for row in hist["data"]
                    if start <= _trading_date(row["timestamp"]) <= as_of
                )
        return out
