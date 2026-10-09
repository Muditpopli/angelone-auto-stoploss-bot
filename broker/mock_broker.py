"""
broker/mock_broker.py
======================
A fake broker that stands in for Angel One during development and for
the client's own acceptance test ("close a dummy position within a tick
of -10%, print the order id"). No network, no credentials -- just an
in-memory book of positions whose LTP is nudged on every call to
get_positions(), the same way real prices tick.

This trader only ever BUYS options (long positions, never shorts), and
only trades whichever index has its weekly expiry nearest to today (see
expiry_calendar.py). So the dummy book below is generated fresh for
"today" -- 2-3 long-only legs in NIFTY or SENSEX, whichever the calendar
picks -- instead of a fixed, hard-coded set of symbols.

Two ways to drive prices:
  1. Organic random walk (default) -- realistic, but how long it takes
     any given leg to reach -10% is not guaranteed.
  2. A scripted price path (`price_path=[...]` per symbol) -- deterministic,
     used by tests/test_monitor.py so the acceptance test is repeatable.
"""
from __future__ import annotations
import datetime
import itertools
import random
from typing import Dict, List, Optional, Tuple

from .base import Broker, Position
from .expiry_calendar import nearest_expiry_index, nearest_expiry_date

# Placeholder ATM levels for the dummy book -- these don't need to track
# the real market (MockBroker never talks to Angel One), just look like
# plausible current index levels. Bump them if they drift far from reality.
_ATM_LEVEL = {"NIFTY": 23150, "SENSEX": 73900}
_STRIKE_STEP = {"NIFTY": 50, "SENSEX": 100}
_LOT_SIZE = {"NIFTY": 75, "SENSEX": 20}
_EXCHANGE = {"NIFTY": "NFO", "SENSEX": "BFO"}
_BASE_TOKEN = {"NIFTY": 60000, "SENSEX": 70000}


def _default_book_for(today: datetime.date) -> List[Tuple[str, str, str, str, int, float, float]]:
    """
    Build 3 realistic LONG-only option legs (2 calls + 1 put around the
    money) in whichever index has the nearest expiry to `today`. Every
    net_qty is positive -- this trader never shorts.
    Returns (symbol, token, exchange, product_type, net_qty, avg_price, start_ltp).
    """
    index = nearest_expiry_index(today)
    expiry = nearest_expiry_date(today)
    tag = expiry.strftime("%d%b%y").upper()

    atm = _ATM_LEVEL[index]
    step = _STRIKE_STEP[index]
    lot = _LOT_SIZE[index]
    exchange = _EXCHANGE[index]
    token_base = _BASE_TOKEN[index]

    legs = [
        (f"{index}{tag}{atm}CE", str(token_base + 1), exchange, "CARRYFORWARD", lot, 150.0, 148.0),
        (f"{index}{tag}{atm + step}CE", str(token_base + 2), exchange, "CARRYFORWARD", lot, 95.0, 97.5),
        (f"{index}{tag}{atm - step}PE", str(token_base + 3), exchange, "CARRYFORWARD", lot, 110.0, 108.0),
    ]
    return legs


class MockBroker(Broker):
    def __init__(self, seed: Optional[int] = 42, today: Optional[datetime.date] = None):
        self._rng = random.Random(seed)
        self._order_seq = itertools.count(1)
        self._closed: set[str] = set()

        today = today or datetime.date.today()
        self._book: Dict[str, Position] = {}
        for sym, token, exchange, prod, qty, avg, start in _default_book_for(today):
            self._book[sym] = Position(
                symbol=sym, token=token, exchange=exchange, product_type=prod,
                net_qty=qty, avg_price=avg, ltp=start,
            )

        # optional deterministic override: {symbol: [ltp, ltp, ltp, ...]}
        # each call to get_positions() consumes the next price in the list
        # for that symbol (last value repeats once exhausted).
        self._scripted: Dict[str, List[float]] = {}
        self._scripted_idx: Dict[str, int] = {}

    # -- test hook --------------------------------------------------------
    def set_price_path(self, symbol: str, prices: List[float]) -> None:
        """Force a symbol's LTP to walk through an exact, repeatable
        sequence of prices -- used to make the -10% trigger deterministic
        in tests instead of waiting on a random walk."""
        self._scripted[symbol] = prices
        self._scripted_idx[symbol] = 0

    # -- Broker interface ---------------------------------------------------
    def get_positions(self) -> List[Position]:
        live = []
        for sym, pos in self._book.items():
            if sym in self._closed:
                continue
            pos.ltp = self._next_price(sym, pos.ltp)
            live.append(pos)
        return live

    def square_off(self, position: Position) -> str:
        order_id = f"MOCK{next(self._order_seq):06d}"
        self._closed.add(position.symbol)
        return order_id

    # -- internal ----------------------------------------------------------
    def _next_price(self, symbol: str, current: float) -> float:
        if symbol in self._scripted:
            path = self._scripted[symbol]
            idx = self._scripted_idx[symbol]
            price = path[min(idx, len(path) - 1)]
            self._scripted_idx[symbol] = idx + 1
            return price
        # organic random walk: option premiums are noisy, +/-3% per tick
        drift = self._rng.uniform(-0.03, 0.03)
        return round(max(0.05, current * (1 + drift)), 2)
