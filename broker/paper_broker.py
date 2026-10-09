"""
broker/paper_broker.py
========================
Hybrid mode: authenticates against your REAL Angel One account and
watches a REAL, currently-listed option contract's live LTP, but the
"position" is a fake long you never actually hold -- net_qty/avg_price
are made up. square_off() NEVER calls placeOrder(); it only prints what
it would have done. This proves the -10% trigger logic fires correctly
against real market movement before it's ever pointed at a real
position.

This trader only ever BUYS options (long positions, never shorts), and
only trades whichever index has its weekly expiry nearest to today (see
expiry_calendar.py). So on startup this broker:
  1. looks up today's nearest-expiry index (NIFTY or SENSEX) and its
     real expiry date,
  2. fetches that index's real live spot price to pick a realistic ATM
     strike,
  3. constructs that contract's real Angel One tradingsymbol and looks
     up its real token via SmartConnect.searchScrip() (so we're never
     guessing a token that doesn't exist on the exchange),
  4. sets a fake avg_price ~12% above the option's real live premium, so
     the -10% trigger breaches on the very first tick -- good for a
     quick demo. Set FAKE_ENTRY_MULTIPLIER closer to 1.0 to instead
     watch it walk down to -10% over several ticks.
"""
from __future__ import annotations
import datetime
import itertools
import sys
from typing import List, Tuple

from .base import Broker, Position
from .angelone_session import login, fetch_ltp
from .expiry_calendar import nearest_expiry_index, nearest_expiry_date

# Windows consoles often default to a legacy code page (cp1252) that
# can't encode the emoji in the trigger banner below; reconfigure stdout
# to UTF-8 so the banner never crashes the run mid-trigger. No-op on
# platforms where stdout doesn't support reconfigure() (e.g. some CI
# runners piping to a file already opened in a fixed encoding).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# Angel One's own index-spot instruments, used only to pick a realistic
# current ATM strike -- these are never traded.
_INDEX_SPOT = {
    "NIFTY": {"exchange": "NSE", "symbol": "Nifty 50", "token": "99926000",
              "strike_step": 50, "option_exchange": "NFO"},
    "SENSEX": {"exchange": "BSE", "symbol": "SENSEX", "token": "99919000",
               "strike_step": 100, "option_exchange": "BFO"},
}

# Fake position sizing -- edit these to test a bigger/smaller fake loss.
# NOTE: pnl_pct for a long is (ltp - avg)/avg * 100, so a multiplier m
# produces a loss of (1 - m)/m * 100 percent -- e.g. 1.12 gives -10.7%,
# not -12%. Keep this comfortably above 1.1112 (the breakeven for
# exactly -10%) so a single tick reliably breaches the threshold.
FAKE_QTY = 1  # in lots' worth of underlying qty; kept small for a paper test
FAKE_ENTRY_MULTIPLIER = 1.12  # fake avg_price = live premium * this


def _build_fake_position(smart, today: datetime.date) -> Tuple[str, str, str, str, int, float]:
    """
    Returns (symbol, token, exchange, product_type, net_qty, avg_price)
    for one fake LONG leg: an ATM call on the real, currently-listed
    contract for whichever index has the nearest expiry to `today`.
    """
    index = nearest_expiry_index(today)
    expiry = nearest_expiry_date(today)
    spot_meta = _INDEX_SPOT[index]

    spot_resp = smart.ltpData(spot_meta["exchange"], spot_meta["symbol"], spot_meta["token"])
    if not spot_resp or not spot_resp.get("status"):
        raise RuntimeError(f"Could not fetch {index} spot LTP: {spot_resp}")
    spot = float(spot_resp["data"]["ltp"])

    step = spot_meta["strike_step"]
    atm_strike = round(spot / step) * step
    tag = expiry.strftime("%d%b%y").upper()
    symbol = f"{index}{tag}{atm_strike}CE"
    option_exchange = spot_meta["option_exchange"]

    search_resp = smart.searchScrip(option_exchange, symbol)
    rows = (search_resp or {}).get("data") or []
    match = next((r for r in rows if r.get("tradingsymbol") == symbol), None)
    if not match:
        raise RuntimeError(
            f"Could not find a live contract for constructed symbol {symbol!r} "
            f"on {option_exchange}. The exchange's strike increments or "
            "expiry-day convention may have changed -- check with "
            "SmartConnect.searchScrip() and adjust _build_fake_position() "
            "in broker/paper_broker.py."
        )
    token = match["symboltoken"]

    live_premium = fetch_ltp(smart, option_exchange, symbol, token)
    fake_avg_price = round(live_premium * FAKE_ENTRY_MULTIPLIER, 2)

    return symbol, token, option_exchange, "CARRYFORWARD", FAKE_QTY, fake_avg_price


class LiveDataPaperBroker(Broker):
    """Real prices, fake position, zero orders."""

    def __init__(self):
        self._smart = login()
        self._order_seq = itertools.count(1)
        self._fake_position = _build_fake_position(self._smart, datetime.date.today())
        symbol, _token, _exchange, _prod, net_qty, avg_price = self._fake_position
        print(f"[PAPER] watching {symbol} (fake qty={net_qty}, fake avg={avg_price:.2f})")

    # -- Broker interface ----------------------------------------------------
    def get_positions(self) -> List[Position]:
        symbol, token, exchange, product_type, net_qty, avg_price = self._fake_position
        ltp = fetch_ltp(self._smart, exchange, symbol, token)
        return [Position(
            symbol=symbol, token=token, exchange=exchange,
            product_type=product_type, net_qty=net_qty,
            avg_price=avg_price, ltp=ltp,
        )]

    def square_off(self, position: Position) -> str:
        order_id = f"PAPER{next(self._order_seq):06d}"
        print("=" * 60)
        print("  PAPER TRADE -- NO REAL ORDER PLACED")
        print("=" * 60)
        print(f"  Symbol      : {position.symbol}")
        print(f"  Fake Qty    : {position.net_qty}")
        print(f"  Fake Entry  : {position.avg_price:.2f}")
        print(f"  Live LTP    : {position.ltp:.2f}")
        print(f"  P&L%        : {position.pnl_pct:.2f}%")
        print(f"  Paper Order : {order_id}")
        print("  \U0001F6D1 SQUARE OFF TRIGGERED: -10% SL Hit! (Paper Trade)")
        print("=" * 60)
        return order_id
