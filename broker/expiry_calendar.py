"""
broker/expiry_calendar.py
===========================
DEMO/TEST DATA ONLY -- this file has no bearing on production
behavior. It exists purely to make mock_broker.py's and
paper_broker.py's dummy data look like a specific real trader's actual
buy-only, NIFTY/SENSEX-only style, for demos and safe testing. The real
production path (order_update_listener.py reacting to a live BUY fill,
main_autosl.py placing the resulting SL-LIMIT) has no schedule and no
instrument restriction at all -- it reacts to any fill, on any
exchange/segment Angel One supports (NSE/NFO, BSE/BFO, MCX, ...), at
any time the relevant exchange is open. Don't mistake the Mon-Fri
NIFTY/SENSEX rule below for something the live bot enforces.

This trader's own demo/test data only ever trades whichever index has
its weekly expiry nearest to the current day:

    Monday    -> NIFTY   (Tuesday expiry is nearest)
    Tuesday   -> NIFTY   (expiry day itself)
    Wednesday -> SENSEX  (Thursday expiry is nearest)
    Thursday  -> SENSEX  (expiry day itself)
    Friday    -> NIFTY   (next Tuesday expiry is nearest)

NIFTY's weekly expiry is Tuesday; SENSEX's weekly expiry is Thursday.
Weekends aren't part of the trader's own rule, but nearest_expiry_date()
still needs an answer for any date -- Saturday/Sunday fall forward to
the same "next Tuesday" case as Friday, since Tuesday is still the
nearest upcoming expiry.

If the exchanges ever change these expiry weekdays, update
_EXPIRY_WEEKDAY below -- everything else derives from it.
"""
from __future__ import annotations
import datetime

# Python's date.weekday(): Monday=0, Tuesday=1, ..., Sunday=6.
_EXPIRY_WEEKDAY = {
    "NIFTY": 1,    # Tuesday
    "SENSEX": 3,   # Thursday
}


def nearest_expiry_index(d: datetime.date) -> str:
    """Return "NIFTY" or "SENSEX" -- whichever has its weekly expiry
    nearest to date `d`, per the Mon-Fri pattern documented above."""
    weekday = d.weekday()
    if weekday in (0, 1):        # Monday, Tuesday
        return "NIFTY"
    elif weekday in (2, 3):       # Wednesday, Thursday
        return "SENSEX"
    else:                          # Friday, Saturday, Sunday
        return "NIFTY"


def nearest_expiry_date(d: datetime.date) -> datetime.date:
    """The actual expiry date nearest_expiry_index(d) refers to -- the
    next occurrence (today counts) of that index's expiry weekday."""
    index = nearest_expiry_index(d)
    target_weekday = _EXPIRY_WEEKDAY[index]
    days_ahead = (target_weekday - d.weekday()) % 7
    return d + datetime.timedelta(days=days_ahead)
