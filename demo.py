#!/usr/bin/env python3
"""
demo.py
========
Polished, portfolio-facing showcase run -- NOT part of the real tool or
its tests, purely for a screen recording / GIF. Reuses the real
MockBroker + StopLossMonitor (same P&L math via Position.pnl_pct, same
-10% trigger threshold, same square_off()) -- only the console
presentation is custom, since StopLossMonitor.run() owns its own
plain-text prints and can't be reformatted without touching monitor.py.

    python demo.py
"""
from __future__ import annotations
import sys
import time

try:
    import colorama
    colorama.init()
except ImportError:
    pass  # plain ANSI codes below still work on most terminals without it

# Windows consoles often default to a legacy code page that can't
# encode the em dash below; reconfigure stdout so this never garbles.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from broker.mock_broker import MockBroker
from monitor import StopLossMonitor

GREEN, YELLOW, RED, BOLD, RESET = (
    "\033[92m", "\033[93m", "\033[91m", "\033[1m", "\033[0m"
)

# Ticks 1-6: rising, modest profit. 7-10: drifting down, small losses.
# 11-15: losses accelerate, crossing -10% at tick 15.
_FACTORS = [
    1.010, 1.022, 1.035, 1.050, 1.065, 1.080,
    0.995, 0.978, 0.960, 0.940,
    0.920, 0.912, 0.908, 0.903, 0.882,
]

_TICK_DELAY_SECONDS = 0.6


def _color_for(pnl_pct: float) -> str:
    if pnl_pct >= 0:
        return GREEN
    if pnl_pct > -7.0:
        return YELLOW
    return RED


def main() -> None:
    print(f"{BOLD}=== Angel One Auto Square-Off — Live Demo ==={RESET}\n")

    broker = MockBroker()
    hero = broker.get_positions()[0]
    entry_price = hero.avg_price
    prices = [round(entry_price * f, 2) for f in _FACTORS]
    broker.set_price_path(hero.symbol, prices)

    mon = StopLossMonitor(broker, stop_loss_pct=-10.0, poll_interval=0)

    peak_price, peak_pct = entry_price, 0.0
    exit_info = None

    for tick in range(1, len(prices) + 1):
        pos = next(p for p in mon.broker.get_positions() if p.symbol == hero.symbol)
        pnl_pct = pos.pnl_pct
        color = _color_for(pnl_pct)

        if pnl_pct > peak_pct:
            peak_pct, peak_price = pnl_pct, pos.ltp

        print(f"{color}[Tick {tick:>2}] {pos.symbol:<22} "
              f"price={pos.ltp:>8.2f}  pnl%={pnl_pct:>+7.2f}%{RESET}")

        if pnl_pct <= mon.stop_loss_pct:
            order_id = mon.broker.square_off(pos)
            exit_info = {"order_id": order_id, "exit_price": pos.ltp,
                         "pnl_pct": pnl_pct, "ticks": tick}
            print(f"\n{RED}{BOLD}{'*' * 52}\n"
                  f"  POSITION EXITED\n"
                  f"  {pos.symbol}\n"
                  f"  Exit price : {pos.ltp:.2f}\n"
                  f"  Final loss : {pnl_pct:+.2f}%\n"
                  f"  Order id   : {order_id}\n"
                  f"{'*' * 52}{RESET}\n")
            break

        time.sleep(_TICK_DELAY_SECONDS)

    print(f"{BOLD}--- Summary ---{RESET}")
    print(f"  Entry price       : {entry_price:.2f}")
    print(f"  Peak price/profit : {peak_price:.2f} ({peak_pct:+.2f}%)")
    print(f"  Exit price        : {exit_info['exit_price']:.2f}")
    print(f"  Final loss        : {exit_info['pnl_pct']:+.2f}%")
    print(f"  Ticks to exit     : {exit_info['ticks']}")

    print(f"\nThis bot watches every open option position in real time and "
          f"automatically exits any leg the instant it crosses a "
          f"configurable loss threshold -- leaving all other open "
          f"positions untouched.")


if __name__ == "__main__":
    main()
