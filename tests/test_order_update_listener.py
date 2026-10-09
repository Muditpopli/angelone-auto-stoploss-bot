"""
tests/test_order_update_listener.py
=====================================
Confirms OrderUpdateListener's fill-detection dedup is keyed by order
id, not by symbol -- two DIFFERENT BUY fills under the SAME symbol
(e.g. a position opened, closed, and reopened) must both fire
independently, while a REDELIVERED message for the SAME order id (e.g.
after a websocket reconnect) must only fire once.

Uses a mocked SmartConnect session and patches SmartWebSocketOrderUpdate
so this never attempts a real network connection.
"""
import json
import sys
import os
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker.order_update_listener import OrderUpdateListener


def _fake_smart():
    smart = MagicMock()
    smart.access_token = "fake-jwt"
    smart.api_key = "fake-key"
    smart.userId = "FAKE123"
    smart.getfeedToken.return_value = "fake-feed-token"
    return smart


def _complete_buy_message(order_id: str, symbol: str, avg_price: float, qty: int) -> str:
    return json.dumps({
        "order-status": "AB05",
        "orderData": {
            "orderid": order_id,
            "transactiontype": "BUY",
            "tradingsymbol": symbol,
            "symboltoken": "12345",
            "exchange": "NFO",
            "producttype": "CARRYFORWARD",
            "filledshares": str(qty),
            "quantity": str(qty),
            "averageprice": str(avg_price),
        },
    })


@patch("broker.order_update_listener.SmartWebSocketOrderUpdate")
def test_two_fills_same_symbol_both_fire_independently(mock_ws_cls):
    mock_ws_cls.return_value = MagicMock()
    fills_seen = []
    listener = OrderUpdateListener(_fake_smart(), on_buy_filled=fills_seen.append)

    listener._handle_message(None, _complete_buy_message("ORDER_A", "NIFTY24000CE", 150.0, 75))
    listener._handle_message(None, _complete_buy_message("ORDER_B", "NIFTY24000CE", 162.0, 75))

    assert len(fills_seen) == 2, (
        f"expected 2 independent fills for the same symbol under different "
        f"order ids, got {fills_seen}"
    )
    assert fills_seen[0]["order_id"] == "ORDER_A"
    assert fills_seen[1]["order_id"] == "ORDER_B"
    assert fills_seen[0]["fill_price"] == 150.0
    assert fills_seen[1]["fill_price"] == 162.0
    print("PASS: two BUY fills under the same symbol (different order ids) "
          "both fire independently")


@patch("broker.order_update_listener.SmartWebSocketOrderUpdate")
def test_redelivered_message_same_order_id_fires_once(mock_ws_cls):
    mock_ws_cls.return_value = MagicMock()
    fills_seen = []
    listener = OrderUpdateListener(_fake_smart(), on_buy_filled=fills_seen.append)

    msg = _complete_buy_message("ORDER_A", "NIFTY24000CE", 150.0, 75)
    listener._handle_message(None, msg)
    listener._handle_message(None, msg)  # redelivered, e.g. after a reconnect

    assert len(fills_seen) == 1, (
        f"expected exactly 1 fill after a redelivered duplicate message, "
        f"got {fills_seen}"
    )
    print("PASS: a redelivered message for the same order id fires only once")


@patch("broker.order_update_listener.SmartWebSocketOrderUpdate")
def test_cancelled_order_with_no_fill_is_ignored(mock_ws_cls):
    """AB02 (cancelled) is only actionable when it carries a real
    partial fill -- a clean, fill-less cancellation must not be
    mistaken for a fill."""
    mock_ws_cls.return_value = MagicMock()
    fills_seen = []
    listener = OrderUpdateListener(_fake_smart(), on_buy_filled=fills_seen.append)

    msg = json.dumps({
        "order-status": "AB02",
        "orderData": {
            "orderid": "ORDER_C",
            "transactiontype": "BUY",
            "tradingsymbol": "NIFTY24000CE",
            "symboltoken": "12345",
            "exchange": "NFO",
            "producttype": "CARRYFORWARD",
            "filledshares": "0",
            "quantity": "75",  # requested 75, but NONE of it filled
            "averageprice": "0",
        },
    })
    listener._handle_message(None, msg)

    assert fills_seen == [], (
        f"a clean cancel with filledshares=0 must not fire a fill, got {fills_seen}"
    )
    print("PASS: a cancelled order with zero fill is correctly ignored")


@patch("broker.order_update_listener.SmartWebSocketOrderUpdate")
def test_fill_handler_exception_is_reported_not_silently_swallowed(mock_ws_cls, capsys):
    """websocket-client swallows exceptions raised inside callbacks and logs
    them to its own handler-less logger, so nothing would print. A fill
    handler that blew up (e.g. a network timeout while placing the SL)
    would otherwise leave a real position unprotected with zero output."""
    import requests

    def boom(fill):
        raise requests.exceptions.ReadTimeout("Read timed out")

    listener = OrderUpdateListener(_fake_smart(), on_buy_filled=boom)
    listener._handle_message(None, _complete_buy_message("ORDER_X", "NIFTY24000CE", 150.0, 75))  # must not raise

    out = capsys.readouterr().out
    assert "ORDER_X" in out and "NIFTY24000CE" in out
    assert "UNPROTECTED" in out and "ReadTimeout" in out


@patch("broker.order_update_listener.SmartWebSocketOrderUpdate")
def test_connection_status_handlers_log_and_redact(mock_ws_cls, capsys):
    """The SDK's own open/error logging is silenced on purpose (credential
    leak), so these lines are the only sign a listener ever connected."""
    import requests
    listener = OrderUpdateListener(_fake_smart(), on_buy_filled=lambda f: None)
    token = "eyJhbGciOiJIUzUxMiJ9." + "A" * 60 + "." + "B" * 40

    listener._handle_open(None)
    listener._handle_error(None, requests.exceptions.ConnectionError(f"failed ?token={token}"))
    listener._handle_close(None, 1006, "abnormal closure")

    out = capsys.readouterr().out
    assert "[WS]" in out and "connected" in out and "closed" in out
    assert "websocket error" in out and token not in out


@patch("broker.order_update_listener.time.sleep")
@patch("broker.order_update_listener.refresh_or_relogin")
@patch("broker.order_update_listener.SmartWebSocketOrderUpdate")
def test_supervisor_keeps_reconnecting_through_network_errors(
        mock_ws_cls, mock_refresh, mock_sleep, capsys):
    """A drop, then a session refresh that hits a network blip, then a
    reconnect attempt that itself fails: the old handler gave up
    permanently on the first of those (leaving the listener dead for the
    rest of the process's life). The supervisor must keep going, backing
    off between attempts instead of spinning."""
    import requests

    class _Done(BaseException):  # not an Exception, so the loop can't swallow it
        pass

    connect_calls = {"n": 0}

    def make_client(*args):
        client = MagicMock()

        def connect():
            connect_calls["n"] += 1
            if connect_calls["n"] == 1:
                return  # first connection simply ends (server closed it)
            if connect_calls["n"] == 2:
                raise requests.exceptions.ReadTimeout("reconnect attempt timed out")
            raise _Done()  # third connection: end the test

        client.connect.side_effect = connect
        return client

    mock_ws_cls.side_effect = make_client
    mock_refresh.side_effect = [requests.exceptions.ConnectionError("dns blip"), None, None]

    listener = OrderUpdateListener(_fake_smart(), on_buy_filled=lambda f: None)
    with pytest.raises(_Done):
        listener._supervise()

    assert connect_calls["n"] == 3  # never gave up
    assert mock_refresh.call_count == 3
    assert [c.args[0] for c in mock_sleep.call_args_list] == [2.0, 4.0, 8.0]  # backing off, not spinning
    out = capsys.readouterr().out
    assert "session refresh failed" in out and "connection loop" in out


if __name__ == "__main__":
    test_two_fills_same_symbol_both_fire_independently()
    test_redelivered_message_same_order_id_fires_once()
    test_cancelled_order_with_no_fill_is_ignored()
    print("\nAll order-update dedup checks passed.")
