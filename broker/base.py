"""
broker/base.py
==============
The contract every broker backend must satisfy. Keeping this tiny and
explicit means monitor.py can drive a MockBroker in tests and an
AngelOneBroker in production with zero code changes -- only the
constructor call in main.py changes.
"""
from __future__ import annotations
from dataclasses import dataclass
from abc import ABC, abstractmethod
from typing import List


@dataclass
class Position:
    """
    One open option leg, normalised into the shape monitor.py needs.
    Every broker implementation is responsible for translating its own
    native response into this shape.

    symbol         : tradingsymbol, e.g. "NIFTY28NOV24C24000"
    token          : broker's internal instrument token (needed to place
                     the square-off order and to fetch LTP)
    exchange       : "NFO" for F&O, kept generic in case of BFO etc.
    product_type   : broker's product code (e.g. "CARRYFORWARD",
                     "INTRADAY") -- the square-off order must reuse this
    net_qty        : signed quantity. Positive = net long, negative = net
                     short. Zero-qty positions are filtered out upstream.
    avg_price      : the position's average entry price (buy avg for a
                     long, sell avg for a short)
    ltp            : last traded price, refreshed on every poll
    """
    symbol: str
    token: str
    exchange: str
    product_type: str
    net_qty: int
    avg_price: float
    ltp: float

    # ---- derived numbers used by the stop-loss logic -------------------
    @property
    def entry_value(self) -> float:
        """Absolute rupee value committed to open this leg."""
        return abs(self.net_qty) * self.avg_price

    @property
    def unrealized_pnl(self) -> float:
        """
        Mark-to-market P&L in rupees.
        Long  (net_qty > 0): profit when ltp rises above avg_price.
        Short (net_qty < 0): profit when ltp falls below avg_price.
        Both cases collapse to the same formula because net_qty carries
        the sign.
        """
        return (self.ltp - self.avg_price) * self.net_qty

    @property
    def pnl_pct(self) -> float:
        """
        MTM loss/gain as a percentage of the entry value.
        This is the number the -10% threshold is measured against.
        See README.md "What does -10% mean?" for why this base was chosen.
        """
        if self.entry_value == 0:
            return 0.0
        return (self.unrealized_pnl / self.entry_value) * 100.0


class Broker(ABC):
    """Minimal interface the monitor loop depends on."""

    @abstractmethod
    def get_positions(self) -> List[Position]:
        """Return all currently open (non-zero qty) positions, with a
        freshly-fetched LTP on each one."""
        raise NotImplementedError

    @abstractmethod
    def square_off(self, position: Position) -> str:
        """
        Close exactly this one leg with a MARKET order in the opposite
        direction, full quantity. Must return the broker's order id.
        """
        raise NotImplementedError
