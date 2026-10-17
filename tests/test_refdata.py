from datetime import date
from decimal import Decimal as D

from ctrade import calendar as cal
from ctrade import refdata


def test_ice_contract_uses_calendar_hours():
    assert refdata.future_instrument("ICE.HPR", "2027-02").hours_per_contract == D(672)
    assert refdata.future_instrument("ICE.BKL", "2026-12").hours_per_contract == D(744)


def test_cme_contract_uses_fixed_730_hours():
    inst = refdata.future_instrument("CME.GPU1", "2027-02")
    assert inst.hours_per_contract == D(730)
    assert inst.instrument_id == "CME.GPU1.2027-02"


def test_last_trade_date_is_last_business_day():
    # 31 Oct 2026 is a Saturday
    assert refdata.future_instrument("ICE.HPR", "2026-10").last_trade_date == date(2026, 10, 30)


def test_spot_block_hours():
    inst = refdata.spot_instrument("H100", date(2026, 11, 1), date(2026, 11, 7))
    assert inst.hours_per_contract == D(168)


def test_previous_business_day_skips_weekend():
    assert cal.previous_business_day(date(2026, 11, 2)) == date(2026, 10, 30)
