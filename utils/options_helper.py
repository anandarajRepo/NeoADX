"""Utilities for Nifty option strike and expiry calculations."""

from datetime import date, timedelta
from typing import Tuple

from config.settings import EXPIRY_DAY

NIFTY_STRIKE_STEP = 50   # Nifty strikes are in multiples of 50


def get_atm_strike(spot_price: float, step: int = NIFTY_STRIKE_STEP) -> int:
    """Round *spot_price* to the nearest option strike."""
    return int(round(spot_price / step) * step)


def get_weekly_expiry(reference_date: date | None = None) -> date:
    """
    Return the nearest upcoming weekly expiry (Tuesday by default).
    If today is the expiry day the same day is returned.
    """
    ref = reference_date or date.today()
    days_ahead = (EXPIRY_DAY - ref.weekday()) % 7
    return ref + timedelta(days=days_ahead)


def expiry_to_breeze_format(expiry: date) -> str:
    """Convert a date to the format expected by the Breeze API, e.g. '2024-05-14T06:00:00.000Z'."""
    return expiry.strftime("%Y-%m-%dT06:00:00.000Z")


def get_option_details(spot_price: float) -> Tuple[int, str, str]:
    """
    Derive ATM strike and weekly expiry from *spot_price*.

    Returns:
        (strike, expiry_breeze_str, expiry_label)
    """
    strike = get_atm_strike(spot_price)
    expiry = get_weekly_expiry()
    expiry_str   = expiry_to_breeze_format(expiry)
    expiry_label = expiry.strftime("%d%b%Y").upper()
    return strike, expiry_str, expiry_label
