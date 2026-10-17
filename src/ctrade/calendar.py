"""Business-day and contract-month helpers.

Weekends are the only non-business days for now; exchange holiday calendars come later.
"""

from __future__ import annotations

import calendar as _cal
from datetime import date, timedelta


def is_business_day(d: date) -> bool:
    return d.weekday() < 5


def previous_business_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


def month_start(year: int, month: int) -> date:
    return date(year, month, 1)


def month_end(year: int, month: int) -> date:
    return date(year, month, _cal.monthrange(year, month)[1])


def last_business_day(year: int, month: int) -> date:
    d = month_end(year, month)
    while not is_business_day(d):
        d -= timedelta(days=1)
    return d


def hours_between(start: date, end: date) -> int:
    """Clock hours from the start of `start` to the end of `end`, ignoring DST."""
    return ((end - start).days + 1) * 24


def parse_month(text: str) -> tuple[int, int]:
    """Parse 'YYYY-MM' into (year, month)."""
    year, month = text.split("-")
    return int(year), int(month)
