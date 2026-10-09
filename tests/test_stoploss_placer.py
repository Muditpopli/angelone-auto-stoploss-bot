"""
tests/test_stoploss_placer.py
================================
Confirms:
  - compute_trigger_price() always rounds toward the entry price (up,
    for a long) -- see broker/stoploss_placer.py for why nearest-tick
    rounding (Python's round(), which uses banker's rounding) can
    violate the "never more loss than configured" guarantee.
  - compute_limit_price() places the SELL SL-LIMIT's limit price at
    least SL_LIMIT_BUFFER below the trigger, rounded the opposite
    direction (down), so that buffer is never smaller than configured.
  - Both hold for tick sizes other than the NFO/BFO 0.05 default, since
    MCX commodities use their own tick sizes (see instrument_master.py).

Run with:
    python -m pytest tests/test_stoploss_placer.py -v
or:
    python tests/test_stoploss_placer.py
"""
from decimal import Decimal
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker.stoploss_placer import compute_trigger_price, compute_limit_price, SL_LIMIT_BUFFER

_INDEX_OPTION_TICK = Decimal("0.05")  # NFO/BFO, per instrument_master.py
_MCX_STYLE_TICK = Decimal("1.0")      # stand-in for a coarser commodity tick

# Fill prices deliberately chosen to NOT land on a clean tick boundary
# once -10% is applied (e.g. 97.25 * 0.9 = 87.525 for the 0.05 tick).
_FILL_PRICES = [97.25, 149.97, 83.33, 121.37, 45.68, 116.70, 130.70]
_TICKS = (_INDEX_OPTION_TICK, _MCX_STYLE_TICK)


def _is_valid_tick(price: float, tick: Decimal) -> bool:
    # Decimal(str(price)) avoids binary-float artifacts (e.g. treating
    # 87.55 as 87.54999999999999) that would make an exact price look
    # like it's off-tick when it isn't.
    d = Decimal(str(price)) / tick
    return d == d.to_integral_value()


def _actual_loss_pct(fill_price: float, trigger_price: float) -> float:
    return (trigger_price - fill_price) / fill_price * 100.0


def test_trigger_price_is_always_a_valid_tick():
    for tick in _TICKS:
        for fill_price in _FILL_PRICES:
            trigger = compute_trigger_price(fill_price, -10.0, tick)
            assert _is_valid_tick(trigger, tick), (
                f"{trigger} (from fill {fill_price}, tick {tick}) is not "
                "a valid tick -- the exchange would reject this trigger price"
            )
    print("PASS: every trigger price lands on a valid tick, for multiple tick sizes")


def test_trigger_price_never_allows_more_loss_than_configured():
    for tick in _TICKS:
        for fill_price in _FILL_PRICES:
            trigger = compute_trigger_price(fill_price, -10.0, tick)
            actual_loss = _actual_loss_pct(fill_price, trigger)
            assert actual_loss >= -10.0, (
                f"fill={fill_price} tick={tick}: trigger={trigger} implies "
                f"{actual_loss:.4f}% loss -- worse than the configured -10.0%"
            )
    print("PASS: rounding never lets the real exit loss exceed -10% "
          "(may trigger a hair early, never late)")


def test_known_example_rounds_up_not_down():
    """The exact motivating case: 97.25 * 0.9 = 87.525 is not a 0.05
    multiple. Its neighboring ticks are 87.50 (down -> MORE loss than
    -10%) and 87.55 (up -> LESS loss) -- must pick 87.55."""
    trigger = compute_trigger_price(97.25, -10.0, _INDEX_OPTION_TICK)
    assert trigger == 87.55, f"expected 87.55, got {trigger}"
    print("PASS: 97.25 @ -10% rounds up to 87.55, not down to 87.50")


def test_limit_price_is_always_a_valid_tick_and_below_trigger():
    for tick in _TICKS:
        for fill_price in _FILL_PRICES:
            trigger = compute_trigger_price(fill_price, -10.0, tick)
            limit = compute_limit_price(trigger, tick)
            assert _is_valid_tick(limit, tick), f"{limit} (tick {tick}) is not a valid tick"
            assert limit < trigger, (
                f"limit {limit} must sit below trigger {trigger} for a "
                "sell-side SL-LIMIT to have room to fill"
            )
    print("PASS: every limit price is a valid tick and sits below its trigger")


def test_limit_price_buffer_is_never_smaller_than_configured():
    for tick in _TICKS:
        for fill_price in _FILL_PRICES:
            trigger = compute_trigger_price(fill_price, -10.0, tick)
            limit = compute_limit_price(trigger, tick)
            gap = Decimal(str(trigger)) - Decimal(str(limit))
            assert gap >= SL_LIMIT_BUFFER, (
                f"gap {gap} between trigger {trigger} and limit {limit} is "
                f"smaller than the configured buffer {SL_LIMIT_BUFFER} -- "
                "rounding must have gone the wrong direction"
            )
    print(f"PASS: trigger-to-limit gap is always >= the configured "
          f"{SL_LIMIT_BUFFER} buffer")


if __name__ == "__main__":
    test_trigger_price_is_always_a_valid_tick()
    test_trigger_price_never_allows_more_loss_than_configured()
    test_known_example_rounds_up_not_down()
    test_limit_price_is_always_a_valid_tick_and_below_trigger()
    test_limit_price_buffer_is_never_smaller_than_configured()
    print("\nAll stop-loss rounding checks passed.")
