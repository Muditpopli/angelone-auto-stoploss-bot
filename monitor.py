"""
monitor.py
==========
The actual safety net. Polls the broker's open positions in a tight
loop, computes each leg's MTM loss %, and square-offs *only* the leg(s)
that have crossed the configured threshold -- everything else is left
untouched, exactly as the client's brief asks.

This file is broker-agnostic: it only talks to the `Broker` interface,
so the exact same loop drives MockBroker in tests and AngelOneBroker in
production.
"""
from __future__ import annotations
import time
from datetime import datetime
from typing import Set

from broker.base import Broker
from broker.network_errors import TRANSIENT_NETWORK_ERRORS, describe_error, log_net_event


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


class StopLossMonitor:
    def __init__(self, broker: Broker, stop_loss_pct: float = -10.0,
                 poll_interval: float = 1.0):
        """
        broker         : any Broker implementation (mock or live)
        stop_loss_pct  : trigger threshold. Negative number, e.g. -10.0
                         means "square off once loss reaches 10%".
                         Change this one number to retune the bot -- see
                         README.md "Tweaking the stop-loss percentage".
        poll_interval  : seconds between position refreshes. Angel One's
                         REST endpoints are rate-limited, so don't set
                         this below ~1s in production; the mock broker
                         is fine with 0.
        """
        if stop_loss_pct >= 0:
            raise ValueError(
                "stop_loss_pct must be negative (it's a loss threshold), "
                f"got {stop_loss_pct}"
            )
        self.broker = broker
        self.stop_loss_pct = stop_loss_pct
        self.poll_interval = poll_interval
        self._already_closed: Set[str] = set()  # symbols we've squared off

    def run(self, max_iterations: int | None = None) -> list[dict]:
        """
        Blocking loop. Pass max_iterations for a bounded test run;
        leave it None to run forever (Ctrl+C to stop).
        Returns a list of {symbol, order_id, pnl_pct, ltp} for every
        square-off executed during the run -- handy for tests/logging.
        """
        executed = []
        i = 0
        while max_iterations is None or i < max_iterations:
            i += 1
            try:
                positions = self.broker.get_positions()

                # A symbol that's no longer open (flat right now) is
                # dropped from _already_closed -- otherwise a symbol we
                # ever squared off stays permanently blacklisted for the
                # rest of this process's life, silently leaving any
                # FUTURE position reopened under that same symbol
                # completely unprotected. A prior version of this method
                # also `break`-ed out of the loop entirely once the
                # whole book went flat, which in live mode
                # (max_iterations=None) meant the bot silently exited
                # the instant it had zero open positions after its first
                # square-off -- removed for the same reason.
                self._already_closed &= {p.symbol for p in positions}

                for pos in positions:
                    if pos.symbol in self._already_closed:
                        continue

                    pnl_pct = pos.pnl_pct
                    print(f"[{_ts()}] {pos.symbol:<28} qty={pos.net_qty:>5} "
                          f"avg={pos.avg_price:>8.2f} ltp={pos.ltp:>8.2f} "
                          f"pnl%={pnl_pct:>7.2f}")

                    if pnl_pct <= self.stop_loss_pct:
                        print(f"[{_ts()}] *** THRESHOLD BREACHED on {pos.symbol} "
                              f"({pnl_pct:.2f}% <= {self.stop_loss_pct:.2f}%) "
                              f"-- squaring off now ***")
                        order_id = self.broker.square_off(pos)
                        print(f"[{_ts()}] SQUARED OFF {pos.symbol} -> order id: {order_id}")
                        self._already_closed.add(pos.symbol)
                        executed.append({
                            "symbol": pos.symbol,
                            "order_id": order_id,
                            "pnl_pct": pnl_pct,
                            "ltp": pos.ltp,
                        })
            except TRANSIENT_NETWORK_ERRORS as e:
                # Plain network noise (timeout, reset, DNS blip, gateway
                # error page) -- NOT an auth problem, and not worth dying
                # for. The SDK re-raises whatever `requests` raises, so
                # a bare ReadTimeout used to escape every handler here
                # and kill the whole unattended process. Retry next tick;
                # square_off() is re-evaluated from a fresh position read
                # then, so an ambiguous timeout on an order is resolved
                # from ground truth rather than guessed at.
                log_net_event(
                    f"Transient network error during monitor tick ({describe_error(e)})",
                    "skipping this tick, will retry on the next poll",
                )
            except RuntimeError as e:
                # A broker call can raise RuntimeError if Angel One's
                # session couldn't be refreshed at all (see
                # angelone_session.py's retry-storm protection --
                # already backs off and prints its own loud banner on
                # repeated failures). Log and retry next tick rather
                # than crash the whole unattended process over what may
                # still resolve itself (or keep failing loudly, which
                # the auth layer itself already surfaces).
                print(f"[{_ts()}] ERROR: monitor tick failed ({describe_error(e)}) -- "
                      f"will retry next poll rather than crash")
            except Exception as e:
                # Last resort: an exception type nobody anticipated (this
                # bug class has now escaped a hand-picked catch list
                # twice). An unattended stop-loss bot that dies here
                # abandons every open position, so in live (unbounded)
                # mode log it loudly and keep protecting. A bounded run
                # is a test/demo, where hiding a bug would be worse than
                # crashing, so re-raise there.
                if max_iterations is not None:
                    raise
                print(f"[{_ts()}] UNEXPECTED ERROR in monitor tick "
                      f"({describe_error(e)}) -- continuing; investigate this")

            if max_iterations is None:
                time.sleep(self.poll_interval)
        return executed
