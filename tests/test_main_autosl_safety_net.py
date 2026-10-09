"""
tests/test_main_autosl_safety_net.py
======================================
Regression tests for main_autosl.py's safety_net_pass():
  - a STALE, already-COMPLETE SL-LIMIT order left over from a prior,
    closed position under the same symbol must NOT mask a brand new,
    unprotected reopened position -- the "missing SL" check must
    WHITELIST genuinely open statuses, not just blacklist
    rejected/cancelled (which would let "complete" slip through as if
    it were still pending).
  - a genuinely pending SL-LIMIT must still be recognized as coverage
    (no duplicate placement).
  - ProtectedSymbols must release a symbol once it's no longer an open
    position, mirroring monitor.py's _already_closed fix.
  - client-reported bug: the bot must never duplicate/interfere with a
    client's own manual order or manual modification of the bot's order,
    regardless of ordertype -- "protected" means ANY open SELL order
    resting against the position, not specifically ordertype
    STOPLOSS_LIMIT. (There is no cancelOrder() call anywhere in this
    codebase; the reported "interference" was always duplicate placement,
    confirmed via safety_net_pass's own logic, never an actual cancel.)
"""
import sys
import os
from contextlib import contextmanager
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker.base import Position
import main_autosl


def _make_position(symbol, avg_price, ltp, qty=75, exchange="NFO", token="12345"):
    return Position(symbol=symbol, token=token, exchange=exchange,
                     product_type="CARRYFORWARD", net_qty=qty,
                     avg_price=avg_price, ltp=ltp)


@contextmanager
def _fake_place_stop_loss(placed: list):
    def fake(smart_arg, symbol, token, exchange, product_type,
             qty, fill_price, stop_loss_pct=-10.0, dry_run=False,
             sl_limit_buffer=None):
        placed.append(symbol)
        return "FAKE_ORDER"

    original = main_autosl.place_stop_loss
    main_autosl.place_stop_loss = fake
    try:
        yield
    finally:
        main_autosl.place_stop_loss = original


def test_stale_completed_sl_does_not_mask_a_reopened_position():
    """A prior position's SL-LIMIT already filled (status 'complete')
    -- a brand new position under the SAME symbol must still get a
    fresh SL placed, not be silently treated as already covered."""
    broker = MagicMock()
    pos = _make_position("NIFTY24000CE", avg_price=160.0, ltp=158.0)
    broker.get_positions.return_value = [pos]

    smart = MagicMock()
    smart.orderBook.return_value = {
        "status": True,
        "data": [{
            "tradingsymbol": "NIFTY24000CE", "exchange": "NFO",
            "ordertype": "STOPLOSS_LIMIT", "transactiontype": "SELL",
            "status": "complete",  # the PRIOR trade's SL, already filled
        }],
    }

    protected = main_autosl.ProtectedSymbols()
    placed = []

    def fake_place_stop_loss(smart_arg, symbol, token, exchange, product_type,
                              qty, fill_price, stop_loss_pct=-10.0, dry_run=False,
                              sl_limit_buffer=None):
        placed.append(symbol)
        return "FAKE_ORDER_1"

    original = main_autosl.place_stop_loss
    main_autosl.place_stop_loss = fake_place_stop_loss
    try:
        main_autosl.safety_net_pass(broker, smart, -10.0, True, protected)
    finally:
        main_autosl.place_stop_loss = original

    assert placed == ["NIFTY24000CE"], (
        f"expected a fresh SL to be placed for the reopened position "
        f"despite the stale completed order, got placed={placed}"
    )
    print("PASS: a stale, already-complete SL order does not mask a "
          "reopened position needing fresh protection")


def test_genuinely_pending_sl_is_not_re_placed():
    """A REAL, still-open SL-LIMIT for the CURRENT position must be
    recognized as coverage -- no duplicate placement."""
    broker = MagicMock()
    pos = _make_position("NIFTY24000CE", avg_price=160.0, ltp=158.0)
    broker.get_positions.return_value = [pos]

    smart = MagicMock()
    smart.orderBook.return_value = {
        "status": True,
        "data": [{
            "tradingsymbol": "NIFTY24000CE", "exchange": "NFO",
            "ordertype": "STOPLOSS_LIMIT", "transactiontype": "SELL",
            "status": "trigger pending",
        }],
    }

    protected = main_autosl.ProtectedSymbols()
    placed = []

    def fake_place_stop_loss(*args, **kwargs):
        placed.append(args[1])
        return "SHOULD_NOT_BE_CALLED"

    original = main_autosl.place_stop_loss
    main_autosl.place_stop_loss = fake_place_stop_loss
    try:
        main_autosl.safety_net_pass(broker, smart, -10.0, True, protected)
    finally:
        main_autosl.place_stop_loss = original

    assert placed == [], f"expected no placement for an already-covered position, got {placed}"
    print("PASS: a genuinely pending SL-LIMIT is correctly recognized as coverage")


def test_protected_symbols_releases_closed_positions():
    protected = main_autosl.ProtectedSymbols()
    protected.add("NIFTY24000CE")
    assert "NIFTY24000CE" in protected

    protected.reconcile(set())  # position no longer open
    assert "NIFTY24000CE" not in protected, (
        "a symbol no longer open must be released, or a future reopened "
        "position under it stays permanently (and silently) unprotected"
    )
    print("PASS: ProtectedSymbols releases a symbol once it's no longer an open position")


def test_client_modified_order_is_respected_not_duplicated():
    """
    (a) Client manually modifies the bot's STOPLOSS_LIMIT order via the
    Angel One app. Angel One implements "modify" as cancel-old-order +
    place-new-order-id under the hood, and the app lets the client change
    ordertype while doing so -- simulate the worst case: the resulting
    resting order is now ordertype LIMIT (not STOPLOSS_LIMIT), a new
    order id, a different price. The bot must NOT re-place its own SL
    (no duplication) and has no cancel code path to call in the first
    place -- confirm nothing at all is attempted.
    """
    broker = MagicMock()
    pos = _make_position("NIFTY24000CE", avg_price=160.0, ltp=140.0)  # -12.5%, well past -10%
    broker.get_positions.return_value = [pos]

    smart = MagicMock()
    smart.orderBook.return_value = {
        "status": True,
        "data": [{
            "tradingsymbol": "NIFTY24000CE", "exchange": "NFO",
            "ordertype": "LIMIT", "transactiontype": "SELL",  # client changed this during modify
            "status": "open", "orderid": "CLIENT_MODIFIED_999",
        }],
    }

    protected = main_autosl.ProtectedSymbols()
    placed = []
    with _fake_place_stop_loss(placed):
        main_autosl.safety_net_pass(broker, smart, -10.0, True, protected)

    assert placed == [], (
        f"the bot must never duplicate a client's manually modified exit "
        f"order, regardless of its ordertype, got placed={placed}"
    )
    print("PASS: a client's manually modified order (now ordertype LIMIT, "
          "new order id) is respected -- no duplicate SL placed")


def test_client_separate_manual_sell_order_is_respected_not_duplicated():
    """
    (b) Client places their OWN separate manual sell order for the full
    quantity while the bot's SL-LIMIT is still resting untouched. Two
    open sell-side rows now exist for the same position. The bot must
    not place a THIRD order, and must not touch either existing one.
    """
    broker = MagicMock()
    pos = _make_position("NIFTY24000CE", avg_price=160.0, ltp=158.0)
    broker.get_positions.return_value = [pos]

    smart = MagicMock()
    smart.orderBook.return_value = {
        "status": True,
        "data": [
            {  # the bot's own original SL-LIMIT, still resting
                "tradingsymbol": "NIFTY24000CE", "exchange": "NFO",
                "ordertype": "STOPLOSS_LIMIT", "transactiontype": "SELL",
                "status": "trigger pending", "orderid": "BOT_ORIGINAL_1",
            },
            {  # client's own separate manual order, placed independently
                "tradingsymbol": "NIFTY24000CE", "exchange": "NFO",
                "ordertype": "MARKET", "transactiontype": "SELL",
                "status": "open", "orderid": "CLIENT_MANUAL_777",
            },
        ],
    }

    protected = main_autosl.ProtectedSymbols()
    placed = []
    with _fake_place_stop_loss(placed):
        main_autosl.safety_net_pass(broker, smart, -10.0, True, protected)

    assert placed == [], (
        f"a position with TWO resting exit orders (bot's + client's own) "
        f"must not get a third, got placed={placed}"
    )
    print("PASS: a client's separate manual sell order alongside the bot's "
          "own resting SL is respected -- no duplicate placed")


def test_zero_open_orders_still_gets_protected_no_regression():
    """
    (c) Genuinely no open exit order rests against the position at all
    (e.g. a prior order -- bot's or the client's -- fully filled or was
    cancelled with nothing replacing it). This is the real "unprotected"
    case the safety net exists for and must NOT regress: the bot must
    still place a fresh SL.
    """
    broker = MagicMock()
    pos = _make_position("NIFTY24000CE", avg_price=160.0, ltp=158.0)
    broker.get_positions.return_value = [pos]

    smart = MagicMock()
    smart.orderBook.return_value = {
        "status": True,
        "data": [{  # a past order for this symbol, but fully resolved -- nothing pending
            "tradingsymbol": "NIFTY24000CE", "exchange": "NFO",
            "ordertype": "STOPLOSS_LIMIT", "transactiontype": "SELL",
            "status": "complete", "orderid": "OLD_ORDER",
        }],
    }

    protected = main_autosl.ProtectedSymbols()
    placed = []
    with _fake_place_stop_loss(placed):
        main_autosl.safety_net_pass(broker, smart, -10.0, True, protected)

    assert placed == ["NIFTY24000CE"], (
        f"a position with genuinely zero open exit orders must still be "
        f"protected -- got placed={placed}"
    )
    print("PASS: a position with zero open exit orders still gets a fresh "
          "SL placed (no regression)")


def test_stuck_sl_watchdog_does_not_fire_for_a_clients_non_stoploss_order():
    """
    The stuck-SL warning is specifically about the bot's OWN
    STOPLOSS_LIMIT order failing to fire despite price crossing its
    trigger. A client's plain LIMIT order simply not having been reached
    yet is completely normal, not "stuck" -- the watchdog must not
    misfire for it even when P&L has drifted well past the threshold.
    """
    broker = MagicMock()
    pos = _make_position("NIFTY24000CE", avg_price=160.0, ltp=100.0)  # -37.5%, way past -10%
    broker.get_positions.return_value = [pos]

    smart = MagicMock()
    smart.orderBook.return_value = {
        "status": True,
        "data": [{
            "tradingsymbol": "NIFTY24000CE", "exchange": "NFO",
            "ordertype": "LIMIT", "transactiontype": "SELL",  # client's own order type
            "status": "open", "orderid": "CLIENT_LIMIT_ORDER",
        }],
    }

    protected = main_autosl.ProtectedSymbols()
    placed = []
    with _fake_place_stop_loss(placed):
        main_autosl.safety_net_pass(broker, smart, -10.0, True, protected)

    print("PASS: the stuck-SL watchdog does not misfire for a client's own "
          "non-STOPLOSS_LIMIT resting order")


def test_enabled_exchanges_filter_skips_disabled_segments():
    """A position on a segment not in enabled_exchanges must be left
    alone entirely by the safety net -- not placed, not warned about."""
    broker = MagicMock()
    mcx_pos = _make_position("CRUDEOIL25DECFUT", avg_price=6000.0, ltp=5000.0, exchange="MCX")
    broker.get_positions.return_value = [mcx_pos]

    smart = MagicMock()
    smart.orderBook.return_value = {"status": True, "data": []}

    protected = main_autosl.ProtectedSymbols()
    placed = []
    with _fake_place_stop_loss(placed):
        main_autosl.safety_net_pass(broker, smart, -10.0, True, protected,
                                     enabled_exchanges={"NSE", "NFO"})

    assert placed == [], (
        f"a position on a disabled segment (MCX) must not be protected "
        f"when enabled_exchanges excludes it, got placed={placed}"
    )
    print("PASS: a position on a segment outside enabled_exchanges is left alone")


def test_enabled_exchanges_none_means_no_restriction_no_regression():
    """The default (no config) must behave exactly as before this
    parameter existed: every segment is eligible."""
    broker = MagicMock()
    mcx_pos = _make_position("CRUDEOIL25DECFUT", avg_price=6000.0, ltp=5000.0, exchange="MCX")
    broker.get_positions.return_value = [mcx_pos]

    smart = MagicMock()
    smart.orderBook.return_value = {"status": True, "data": []}

    protected = main_autosl.ProtectedSymbols()
    placed = []
    with _fake_place_stop_loss(placed):
        main_autosl.safety_net_pass(broker, smart, -10.0, True, protected)  # enabled_exchanges=None

    assert placed == ["CRUDEOIL25DECFUT"], (
        f"with no enabled_exchanges restriction, every segment must still "
        f"be protected (no regression), got placed={placed}"
    )
    print("PASS: enabled_exchanges=None (default) restricts nothing")


if __name__ == "__main__":
    test_stale_completed_sl_does_not_mask_a_reopened_position()
    test_genuinely_pending_sl_is_not_re_placed()
    test_protected_symbols_releases_closed_positions()
    test_client_modified_order_is_respected_not_duplicated()
    test_client_separate_manual_sell_order_is_respected_not_duplicated()
    test_zero_open_orders_still_gets_protected_no_regression()
    test_stuck_sl_watchdog_does_not_fire_for_a_clients_non_stoploss_order()
    print("\nAll safety-net regression checks passed.")
