#!/usr/bin/env python3
"""
main.py
=======
Entry point. Run in mock mode (no credentials needed, safe to demo) or
live mode (talks to your real Angel One account and places real orders).

    python main.py --mode mock                 # safe demo, dummy positions
    python main.py --mode live                  # real account, real orders
    python main.py --mode live --stop-loss -7.5  # tweak the threshold
    python main.py --mode mock --iterations 20   # bounded run for testing

See README.md for what "-10%" is measured against, and SETUP.md for how
to get live credentials in place.
"""
from __future__ import annotations
import argparse
import sys

from dotenv import load_dotenv
load_dotenv()

from monitor import StopLossMonitor
from config_loader import DEFAULTS as _CONFIG_DEFAULTS, load_config, resolve


def build_broker(mode: str):
    if mode == "mock":
        from broker.mock_broker import MockBroker
        return MockBroker()
    elif mode == "paper":
        from broker.paper_broker import LiveDataPaperBroker
        return LiveDataPaperBroker()
    elif mode == "live":
        from broker.angelone_broker import AngelOneBroker
        return AngelOneBroker()
    raise ValueError(f"Unknown mode: {mode}")


def main():
    parser = argparse.ArgumentParser(description="Angel One auto square-off bot")
    parser.add_argument("--mode", choices=["mock", "paper", "live"], default="mock",
                         help="mock = dummy positions, no credentials needed. "
                              "paper = real Angel One login + real live prices, "
                              "but a hard-coded fake position and no real orders. "
                              "live = your real Angel One account, real orders.")
    parser.add_argument("--config", default=None,
                         help="Path to a config.yaml-style file (default: ./config.yaml "
                              "if it exists, else built-in defaults). See config.example.yaml.")
    # None (not the real default) so resolve() can tell "the user passed
    # this flag" apart from "fall back to config.yaml/the built-in
    # default" -- CLI > config.yaml > built-in-default precedence.
    parser.add_argument("--stop-loss", type=float, default=None,
                         help="Loss threshold as a percentage. Must be negative. "
                              "Overrides config.yaml/default "
                              f"({_CONFIG_DEFAULTS['stop_loss_pct']}).")
    parser.add_argument("--poll-interval", type=float, default=None,
                         help="Seconds between position refreshes (live/paper mode). "
                              f"Overrides config.yaml/default ({_CONFIG_DEFAULTS['poll_interval']}).")
    parser.add_argument("--iterations", type=int, default=None,
                         help="Run for N polls then stop (useful for testing). "
                              "Omit to run forever.")
    args = parser.parse_args()

    config = load_config(args.config)
    stop_loss_pct = resolve(args.stop_loss, config, "stop_loss_pct")
    poll_interval = resolve(args.poll_interval, config, "poll_interval")

    print(f"Starting in {args.mode.upper()} mode | "
          f"stop-loss threshold = {stop_loss_pct}% | "
          f"poll interval = {poll_interval}s")
    if args.mode == "live":
        print("*** LIVE MODE: this will place REAL orders on your REAL account. ***")

    try:
        broker = build_broker(args.mode)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    monitor = StopLossMonitor(
        broker=broker,
        stop_loss_pct=stop_loss_pct,
        poll_interval=poll_interval if args.mode in ("live", "paper") else 0,
    )
    try:
        monitor.run(max_iterations=args.iterations)
    except KeyboardInterrupt:
        print("\nStopped by user.")


if __name__ == "__main__":
    main()
