"""
tests/test_monitor.py
======================
Reproduces the client's own acceptance test:
  "when paper-traded with dummy/mock positions, the script must close
   the trade within a tick of crossing -10%, and print the executed
   order id to console."

MockBroker's dummy book is generated fresh for "today" (see
mock_broker.py / expiry_calendar.py), so this test reads the symbols
back off the broker itself rather than hard-coding them -- it stays
correct no matter what date it's run on.

Run it with:
    cd /home/claude/angelone-sl-tool
    python -m pytest tests/ -v
or just:
    python tests/test_monitor.py
"""
import datetime
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker.mock_broker import MockBroker
from broker.expiry_calendar import nearest_expiry_index
from monitor import StopLossMonitor


def test_closes_only_breaching_leg_within_one_tick():
    broker = MockBroker()
    positions = broker.get_positions()
    assert len(positions) >= 2, "need at least one breaching + one untouched leg"

    breaching = positions[0]
    others = positions[1:]

    # Script the first leg's price to walk down through -10% (long-only,
    # so a loss means price dropping below avg_price), holding every
    # other leg flat/healthy so we can prove they're untouched.
    avg = breaching.avg_price
    broker.set_price_path(breaching.symbol, [avg * 0.99, avg * 0.93, avg * 0.87])
    for pos in others:
        broker.set_price_path(pos.symbol, [pos.avg_price, pos.avg_price, pos.avg_price * 1.02])

    mon = StopLossMonitor(broker, stop_loss_pct=-10.0, poll_interval=0)
    executed = mon.run(max_iterations=3)

    assert len(executed) == 1, f"expected exactly one square-off, got {executed}"
    hit = executed[0]
    assert hit["symbol"] == breaching.symbol
    assert hit["order_id"].startswith("MOCK")
    assert hit["pnl_pct"] <= -10.0
    print(f"PASS: closed {hit['symbol']} at {hit['pnl_pct']:.2f}% "
          f"-> order id {hit['order_id']}")

    # confirm the other legs are still open (untouched)
    remaining_symbols = {p.symbol for p in broker.get_positions()}
    for pos in others:
        assert pos.symbol in remaining_symbols
    assert breaching.symbol not in remaining_symbols
    print("PASS: the other open legs were left untouched")


def test_reopened_position_in_same_symbol_gets_independent_protection():
    """
    Regression test: a symbol that was squared off once must NOT be
    permanently blacklisted. A brand new position opened later under
    the same symbol (e.g. BUY -> SL fills -> BUY again) must get its
    own, independent square-off. This used to fail: _already_closed
    only ever grew, so any symbol ever closed stayed unprotected for
    the rest of the process's life -- and a separate bug meant the
    whole run() loop would exit entirely the moment the book went flat
    in live (unbounded) mode, compounding the problem.
    """
    broker = MockBroker()
    positions = broker.get_positions()
    target = positions[0]
    others = positions[1:]

    # -- First position under this symbol: script an immediate breach.
    avg1 = target.avg_price
    broker.set_price_path(target.symbol, [avg1 * 0.85])  # -15%, breaches tick 1
    for pos in others:
        broker.set_price_path(pos.symbol, [pos.avg_price])

    mon = StopLossMonitor(broker, stop_loss_pct=-10.0, poll_interval=0)
    first_run = mon.run(max_iterations=1)
    assert len(first_run) == 1, f"expected the first position to close, got {first_run}"
    assert first_run[0]["symbol"] == target.symbol

    # -- One tick where the symbol is genuinely flat/absent, matching
    # real life: there's always some observable gap between a
    # square-off actually executing and a brand new BUY filling.
    flat_run = mon.run(max_iterations=1)
    assert flat_run == [], f"expected nothing to breach on the flat tick, got {flat_run}"
    assert target.symbol not in mon._already_closed, (
        "the symbol must be released from _already_closed once observed "
        "flat, or a future reopened position stays permanently blacklisted"
    )

    # -- Reopen: a brand new position under the SAME symbol with its
    # own fresh avg_price, breaches independently. MockBroker has no
    # order-placement simulator, so the reopen is nudged directly.
    broker._closed.discard(target.symbol)
    avg2 = avg1 * 1.10  # a different entry price -- genuinely a new fill
    broker._book[target.symbol].avg_price = avg2
    broker.set_price_path(target.symbol, [avg2 * 0.85])
    for pos in others:
        broker.set_price_path(pos.symbol, [pos.avg_price])

    second_run = mon.run(max_iterations=1)
    assert len(second_run) == 1, (
        f"expected the REOPENED position under {target.symbol!r} to also "
        f"close independently, got {second_run} -- if empty, "
        "_already_closed is permanently blacklisting the symbol"
    )
    assert second_run[0]["symbol"] == target.symbol
    assert second_run[0]["order_id"] != first_run[0]["order_id"], (
        "the second square-off must be a distinct order, not a stale replay"
    )
    print("PASS: a reopened position in the same symbol gets its own, "
          "independent square-off after being observed flat")


def test_nearest_expiry_index_matches_weekly_pattern():
    """This trader only trades whichever index has its weekly expiry
    nearest to today -- confirm the calendar rule matches the documented
    Mon-Fri pattern (see broker/expiry_calendar.py)."""
    monday = datetime.date(2026, 9, 28)  # a confirmed real Monday
    expected = {
        0: "NIFTY",   # Monday    -> Tuesday expiry nearest
        1: "NIFTY",   # Tuesday   -> expiry day itself
        2: "SENSEX",  # Wednesday -> Thursday expiry nearest
        3: "SENSEX",  # Thursday  -> expiry day itself
        4: "NIFTY",   # Friday    -> next Tuesday nearest
    }
    for offset, expected_index in expected.items():
        d = monday + datetime.timedelta(days=offset)
        actual = nearest_expiry_index(d)
        assert actual == expected_index, (
            f"{d} ({d.strftime('%A')}): expected {expected_index}, got {actual}"
        )
    print("PASS: nearest_expiry_index() matches the Mon-Fri weekly pattern")


if __name__ == "__main__":
    test_closes_only_breaching_leg_within_one_tick()
    test_reopened_position_in_same_symbol_gets_independent_protection()
    test_nearest_expiry_index_matches_weekly_pattern()
    print("\nAll acceptance checks passed.")
