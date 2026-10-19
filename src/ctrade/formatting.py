"""Number formats shared by the command line and the app: $1,234.56 and 1,234."""

from __future__ import annotations

from decimal import Decimal

PRICE_DP = 4  # $ per GPU-hour; exchange ticks go down to $0.001


def money(x, dp: int = 2) -> str:
    """Currency amount, e.g. -$1,234.56. Blank for None."""
    if x is None:
        return ""
    value = Decimal(x)
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.{dp}f}"


def price(x) -> str:
    """Price per GPU-hour, e.g. $2.5900."""
    return money(x, PRICE_DP)


def number(x) -> str:
    """Quantity with thousands separators and no trailing zeros, e.g. 1,250 or 2.5."""
    if x is None:
        return ""
    text = f"{Decimal(x):,f}"
    return text.rstrip("0").rstrip(".") if "." in text else text


def adjustment(multiplier, spread) -> str:
    """How a proxy rule adjusts its source, e.g. ' x 1.1 - $0.0500'; blank if unadjusted."""
    mult, sprd = Decimal(multiplier), Decimal(spread)
    if (mult, sprd) == (1, 0):
        return ""
    text = "" if mult == 1 else f" x {number(mult)}"
    if sprd:
        text += f" {'-' if sprd < 0 else '+'} {price(abs(sprd))}"
    return text
