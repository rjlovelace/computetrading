from decimal import Decimal as D

import pytest

from ctrade.db import DatabaseUnavailable, check_connection
from ctrade.formatting import adjustment, money, number, price


def test_money_and_price_use_dollars_and_commas():
    assert money(D("1234567.891")) == "$1,234,567.89"
    assert money(-360) == "-$360.00"
    assert price(D("2.59")) == "$2.5900"
    assert money(None) == ""


def test_number_drops_trailing_zeros():
    assert number(D("1250.0000")) == "1,250"
    assert number(D("2.50")) == "2.5"


def test_adjustment_text():
    assert adjustment(1, 0) == ""
    assert adjustment(1, D("-0.05")) == " - $0.0500"
    assert adjustment(D("1.1"), D("0.02")) == " x 1.1 + $0.0200"


def test_unreachable_database_gives_plain_message():
    url = "postgresql+psycopg://u:secret@127.0.0.1:1/nothing"
    with pytest.raises(DatabaseUnavailable) as e:
        check_connection(url)
    assert "docker compose up -d" in str(e.value) and "secret" not in str(e.value)
