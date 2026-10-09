"""
broker/angelone_broker.py
==========================
Thin adapter over Angel One's official `smartapi-python` SDK. This file
does exactly three things against the real account:
  1. log in (API key + client code + password/PIN + TOTP)
  2. pull the live position book and refresh each leg's LTP
  3. place a MARKET square-off order for one leg

Install the SDK first:
    pip install smartapi-python pyotp websocket-client

Credentials are read from environment variables (never hard-code them):
    ANGEL_API_KEY
    ANGEL_CLIENT_CODE
    ANGEL_PASSWORD      (the account PIN, not the trading password)
    ANGEL_TOTP_SECRET   (the base32 secret shown when you set up TOTP
                         in the Angel One app -- NOT a 6-digit code)

See README.md / SETUP.md for how to obtain the API key (SmartAPI portal)
and the TOTP secret.
"""
from __future__ import annotations
from typing import List

from .base import Broker, Position
from .angelone_session import login, fetch_ltp, with_session_retry


class AngelOneBroker(Broker):
    def __init__(self, smart=None):
        # Accepts an already-logged-in SmartConnect session (e.g. from
        # main_autosl.py, which needs the same session for its
        # order-update websocket) so callers aren't forced to log in
        # twice. Defaults to logging in fresh, as before.
        self._smart = smart or login()

    # -- Broker interface ----------------------------------------------------
    def get_positions(self) -> List[Position]:
        resp = with_session_retry(self._smart, self._smart.position, idempotent=True)
        if not resp or not resp.get("status"):
            # An empty/failed response most often just means "no open
            # positions right now" -- treat it as an empty book rather
            # than crashing the monitor loop.
            return []

        positions: List[Position] = []
        for row in resp.get("data") or []:
            net_qty = int(float(row.get("netqty", 0)))
            if net_qty == 0:
                continue  # already flat, nothing to protect

            avg_price = float(row.get("avgnetprice", 0) or 0)
            symbol = row["tradingsymbol"]
            token = row["symboltoken"]
            exchange = row.get("exchange", "NFO")
            product_type = row.get("producttype", "CARRYFORWARD")

            ltp = self._fetch_ltp(exchange, symbol, token)

            positions.append(Position(
                symbol=symbol, token=token, exchange=exchange,
                product_type=product_type, net_qty=net_qty,
                avg_price=avg_price, ltp=ltp,
            ))
        return positions

    def square_off(self, position: Position) -> str:
        # Opposite side of whatever we're currently holding, full quantity.
        transaction_type = "SELL" if position.net_qty > 0 else "BUY"
        order_params = {
            "variety": "NORMAL",
            "tradingsymbol": position.symbol,
            "symboltoken": position.token,
            "transactiontype": transaction_type,
            "exchange": position.exchange,
            "ordertype": "MARKET",
            "producttype": position.product_type,
            "duration": "DAY",
            "price": "0",
            "quantity": str(abs(position.net_qty)),
        }
        # placeOrderFullResponse() hits the exact same endpoint as
        # placeOrder() (identical URL in the SDK's own _routes table) but
        # returns the full response dict on failure too -- placeOrder()
        # silently swallows a failure's errorcode/message and returns
        # None, which would hide an auth failure from with_session_retry.
        # Deliberately NOT idempotent=True: a network timeout on an order
        # is ambiguous (it may have reached the exchange), so it must not
        # be auto-retried here -- a second MARKET SELL could flip a long
        # into a short. The error propagates to monitor.py, which re-reads
        # the real position book on its next tick before deciding anything.
        resp = with_session_retry(self._smart, self._smart.placeOrderFullResponse, order_params)
        if isinstance(resp, dict):
            order_id = (resp.get("data") or {}).get("orderid") or resp.get("orderid")
        else:
            order_id = resp
        if not order_id:
            raise RuntimeError(f"Square-off order for {position.symbol} "
                                f"did not return an order id: {resp}")
        return str(order_id)

    # -- internal --------------------------------------------------------------
    def _fetch_ltp(self, exchange: str, symbol: str, token: str) -> float:
        """
        The position book's own 'ltp' field is frequently stale or zero,
        so we always ask the market-data endpoint for a fresh quote
        immediately before computing MTM.
        """
        return fetch_ltp(self._smart, exchange, symbol, token)
