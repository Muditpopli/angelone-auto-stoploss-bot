#!/usr/bin/env python3
"""
main_autosl.py
================
Alternative, EVENT-DRIVEN entry point: the instant a BUY order fills,
places a protective STOPLOSS_LIMIT (SL-LIMIT) SELL order stop_loss_pct
below the fill price -- no MTM%-polling required for the trigger
itself.

    python main_autosl.py --mode paper                  # safe: logs what would happen, no real orders
    python main_autosl.py --mode live                    # real SL-LIMIT orders on real fills
    python main_autosl.py --mode live --stop-loss -7.5    # tweak the threshold

Per client request this places a LIMIT order, not a market order (see
README.md "SL order type: Limit vs Market"). The client explicitly
accepts that a rare gap-through (price jumps past both trigger and
limit before the order fills) means a manual exit on their end -- this
file's stuck-SL watchdog (in safety_net_pass) exists specifically to
make that scenario loud and impossible to miss, not to paper over it
with an automatic market-order fallback.

This works identically for ANY exchange/instrument Angel One supports
(NSE/NFO index options, BSE/BFO, MCX commodities, ...) and at any time
of day the relevant exchange is open -- order_update_listener.py reacts
to any BUY fill it's told about, with no schedule or instrument
restriction baked in. See expiry_calendar.py for why the NIFTY/SENSEX
logic used in mock_broker.py/paper_broker.py has no bearing here.

This is new and purely additive: it does not import or modify main.py,
mock_broker.py, or paper_broker.py, so the existing polling-based tool
(main.py) keeps working exactly as before and remains available as a
fallback/alternative -- see README.md "Auto-SL mode" for how the two
compare. (monitor.py DID need one fix as part of a pre-production
audit -- see its own docstring/comments -- but that fix benefits both
entry points identically.)

Live-tested on a funded account. Still, the order-update websocket's
message schema (order_update_listener.py) and the safety-net's
order-book field reading are built from Angel One's documented
conventions, cross-checked against the SDK source where possible, not
from exhaustive testing across every account/segment combination --
start with a small quantity on any setup you haven't run this against
before. See README.md "Safety & limitations".
"""
from __future__ import annotations
import argparse
import sys
import threading
import time

from dotenv import load_dotenv
load_dotenv()

from broker.angelone_session import login, with_session_retry
from broker.angelone_broker import AngelOneBroker
from broker.network_errors import TRANSIENT_NETWORK_ERRORS, describe_error, log_net_event
from broker.order_update_listener import OrderUpdateListener
from broker.stoploss_placer import place_stop_loss
from config_loader import DEFAULTS as _CONFIG_DEFAULTS, load_config, resolve

# How much further past stop_loss_pct a position's P&L% has to drift,
# while a SL-LIMIT order IS already pending against it, before the
# stuck-SL watchdog decides that order almost certainly gapped through
# and didn't fill -- e.g. stop_loss_pct=-10.0 and margin=2.0 means the
# watchdog fires once P&L% is worse than -12.0%.
DEFAULT_STUCK_SL_MARGIN_PCT = 2.0

# Seconds between safety-net polls -- the ONLY backstop for a BUY fill
# the websocket listener missed entirely (e.g. it was mid-reconnect).
# A real, live, unprotected position sits fully exposed for up to this
# long in that case, so this is deliberately short rather than
# "efficient" -- get_positions()/orderBook() are cheap, read-only REST
# calls; monitor.py's OWN polling loop already defaults to a 1s
# interval for a single account, so 10s here is still 10x gentler on
# rate limits while giving a meaningfully tighter worst-case window for
# a fast-moving option than a much longer interval would.
DEFAULT_SAFETY_NET_INTERVAL_SECONDS = 10.0

# Angel One's documented, genuinely-still-on-the-book order statuses
# (https://smartapi.angelone.in and community forum threads -- see
# README.md). Deliberately a WHITELIST, not a blacklist of e.g.
# ("rejected", "cancelled") -- a blacklist would also (wrongly) treat
# "complete" as still pending, which is exactly how a stale, ALREADY
# FILLED SL-LIMIT left over from a PRIOR, already-closed position under
# the same symbol could mask a brand new, completely unprotected
# reopened position from the "missing SL" check below.
_PENDING_ORDER_STATUSES = {"open", "trigger pending", "open pending", "modified", "pending"}


class ProtectedSymbols:
    """
    Thread-safe, symbol-keyed "have I already protected this position"
    tracker, shared between the websocket listener's own thread and the
    main thread's safety-net poll.

    Reconciled against the CURRENT real position book on every
    safety-net pass (see reconcile()): a symbol no longer open is
    dropped, so a position later reopened under the same symbol (e.g.
    BUY -> SL fills -> BUY again) is treated as fresh and unprotected
    until proven otherwise, rather than permanently suppressed just
    because that symbol was protected once before in this process's
    lifetime. Mirrors the equivalent fix applied to monitor.py's
    _already_closed.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._symbols: set = set()

    def add(self, symbol: str) -> None:
        with self._lock:
            self._symbols.add(symbol)

    def __contains__(self, symbol: str) -> bool:
        with self._lock:
            return symbol in self._symbols

    def reconcile(self, current_symbols) -> None:
        with self._lock:
            self._symbols &= current_symbols


def make_on_fill(smart, stop_loss_pct: float, dry_run: bool, protected: ProtectedSymbols,
                  sl_limit_buffer=None):
    def on_buy_filled(fill: dict):
        try:
            place_stop_loss(
                smart, fill["symbol"], fill["token"], fill["exchange"],
                fill["product_type"], fill["qty"], fill["fill_price"],
                stop_loss_pct=stop_loss_pct, dry_run=dry_run,
                sl_limit_buffer=sl_limit_buffer,
            )
            protected.add(fill["symbol"])
        except RuntimeError as e:
            # place_stop_loss() already retried internally and printed
            # an unmissable banner before raising this -- this catch
            # just keeps that one failure from killing the listener
            # thread, so every OTHER fill still gets protected. The
            # safety-net poll will keep retrying this specific symbol.
            print(f"ERROR: {e}", file=sys.stderr)
    return on_buy_filled


def _print_stuck_sl_banner(symbol: str, pnl_pct: float, stop_loss_pct: float) -> None:
    print("\n" + "!" * 60)
    print("  SL-LIMIT ORDER DID NOT FILL -- MANUAL ACTION REQUIRED")
    print(f"  Symbol       : {symbol}")
    print(f"  Current P&L% : {pnl_pct:+.2f}%  "
          f"(configured threshold: {stop_loss_pct:.2f}%)")
    print("  A pending SL-LIMIT order exists for this position, but its")
    print("  loss has moved well past the configured threshold anyway --")
    print("  price most likely gapped through the limit price without")
    print("  filling it. This bot will NOT auto-cancel or replace it with")
    print("  a market order (per your own instruction). Check Angel One's")
    print("  order book now and decide how to exit this position yourself.")
    print("!" * 60 + "\n")


def safety_net_pass(broker: AngelOneBroker, smart, stop_loss_pct: float,
                     dry_run: bool, protected: ProtectedSymbols,
                     stuck_sl_margin_pct: float = DEFAULT_STUCK_SL_MARGIN_PCT,
                     enabled_exchanges=None, sl_limit_buffer=None) -> None:
    """
    Two independent backstops, both driven off Angel One's own order
    book rather than in-memory state (which only knows about fills the
    websocket listener actually saw):

    1. Missing exit order: a long position with ZERO open sell-side
       orders resting against it at all -- places a fresh SL-LIMIT.
       Backstop for a missed/dropped websocket event.
    2. Stuck SL: a long position whose resting order IS specifically the
       bot's own STOPLOSS_LIMIT, but whose P&L% has drifted past
       stop_loss_pct - stuck_sl_margin_pct anyway -- that specific order
       almost certainly gapped through and didn't fill. Prints a loud
       warning; does NOT cancel/replace the order, since the client
       explicitly wants manual control here.

    Client-reported bug this fixes: "is this position protected?" used to
    require the resting order to be exactly ordertype STOPLOSS_LIMIT --
    the bot's own order type. Angel One implements a manual "modify" as
    cancel-old-order + place-new-order-id under the hood (verified: the
    order id is NOT part of how this matches anything, so a modify that
    keeps the SAME ordertype was never the problem), but the app also
    lets a client change ORDERTYPE while modifying, and a client's own
    manual protective order can be any type at all (plain LIMIT, MARKET,
    STOPLOSS_MARKET, a GTT, ...). None of those are ordertype
    STOPLOSS_LIMIT, so the old check saw "no pending SL", decided the
    position was unprotected, and placed a DUPLICATE order on top of
    whatever the client had just done -- the reported "bot cancels it /
    interferes with it" (this bot has no cancelOrder() call anywhere in
    it; the interference was always duplication, never an actual cancel).

    Fix: "protected" now means ANY open SELL order of ANY ordertype
    resting against the position -- ours or the client's, current or
    just-modified. A fresh SL is placed ONLY when a position has
    genuinely zero open exit orders. Matched by (exchange, tradingsymbol)
    together, not symbol alone, so a same-named symbol on a different
    segment can never be mistaken for coverage it doesn't have.

    enabled_exchanges: optional set of exchange codes (e.g.
    {"NSE", "NFO"}); a position on any OTHER exchange is left alone
    entirely -- not even counted as "needs protection". None (the
    default) means no restriction, matching behavior before this
    parameter existed. See config.example.yaml's "enabled_exchanges".
    """
    positions = broker.get_positions()
    protected.reconcile({p.symbol for p in positions})
    if not positions:
        return

    order_book = with_session_retry(smart, smart.orderBook, idempotent=True)
    open_exit_keys = set()       # {(exchange, tradingsymbol)}: ANY open SELL order, any ordertype
    pending_sl_limit_keys = set()  # same, but specifically the bot's own order type (STOPLOSS_LIMIT)
    if order_book and order_book.get("status"):
        for row in order_book.get("data") or []:
            status = (row.get("status") or "").strip().lower()
            if row.get("transactiontype") == "SELL" and status in _PENDING_ORDER_STATUSES:
                key = (row.get("exchange"), row.get("tradingsymbol"))
                open_exit_keys.add(key)
                if row.get("ordertype") == "STOPLOSS_LIMIT":
                    pending_sl_limit_keys.add(key)

    stuck_threshold = stop_loss_pct - stuck_sl_margin_pct

    for pos in positions:
        if pos.net_qty <= 0:
            continue  # this trader only buys -- a short/flat leg isn't ours to protect
        if enabled_exchanges is not None and pos.exchange not in enabled_exchanges:
            continue  # segment disabled via config -- not ours to touch at all

        key = (pos.exchange, pos.symbol)
        if key in open_exit_keys:
            # Something is already resting against this position -- ours
            # or the client's own manual order/modification. Leave it
            # alone completely: never duplicate it, never touch it. Only
            # warn (never act) if it's specifically our own STOPLOSS_LIMIT
            # type and looks stuck -- a plain LIMIT order simply not
            # having reached its price yet is normal, not "stuck", so
            # that warning stays scoped to the one order type it's
            # actually meaningful for.
            if key in pending_sl_limit_keys and pos.pnl_pct <= stuck_threshold:
                _print_stuck_sl_banner(pos.symbol, pos.pnl_pct, stop_loss_pct)
            continue

        if pos.symbol in protected:
            continue
        print(f"[SAFETY-NET] {pos.symbol} has no open exit order -- "
              f"placing an SL-LIMIT now (this should be rare; if it keeps "
              f"happening, check the order-update websocket connection)")
        try:
            place_stop_loss(
                smart, pos.symbol, pos.token, pos.exchange, pos.product_type,
                abs(pos.net_qty), pos.avg_price, stop_loss_pct=stop_loss_pct,
                dry_run=dry_run, sl_limit_buffer=sl_limit_buffer,
            )
            protected.add(pos.symbol)
        except RuntimeError as e:
            print(f"ERROR: safety-net SL placement failed for {pos.symbol}: {e}",
                  file=sys.stderr)


def run_safety_net_cycle(broker: AngelOneBroker, smart, stop_loss_pct: float,
                          dry_run: bool, protected: ProtectedSymbols,
                          stuck_sl_margin_pct: float = DEFAULT_STUCK_SL_MARGIN_PCT,
                          enabled_exchanges=None, sl_limit_buffer=None) -> None:
    """
    One safety-net pass that can never take the whole process down.

    smartapi-python re-raises whatever `requests` raises, so an ordinary
    read timeout to apiconnect.angelone.in used to escape this loop's
    RuntimeError-only handler and kill the process -- abandoning every
    open position. Failures are sorted into distinct classes and logged
    differently, but every one means "skip this cycle, try the next":

    - transient network noise ([NET] line): timeout, reset, DNS blip,
      gateway error page. Not an auth problem, so it never touches
      angelone_session's auth-failure backoff counter.
    - RuntimeError: an auth-class failure (angelone_session already backs
      off and prints its own banner on repeated ones) or a definite
      placement failure (stoploss_placer already printed its own banner).
    - anything else: a bug or an API change nobody anticipated. Logged
      loudly and survived, because this loop dying is worse than it
      erroring every cycle where the operator can see it. KeyboardInterrupt
      is not an Exception subclass, so Ctrl+C still stops the process.
    """
    try:
        safety_net_pass(broker, smart, stop_loss_pct, dry_run, protected,
                        stuck_sl_margin_pct, enabled_exchanges, sl_limit_buffer)
    except TRANSIENT_NETWORK_ERRORS as e:
        log_net_event(
            f"Transient network error during safety-net pass ({describe_error(e)})",
            "skipping this cycle, will retry on the next one",
        )
    except RuntimeError as e:
        print(f"ERROR: safety-net pass failed ({describe_error(e)}) -- will "
              f"retry next cycle rather than crash", file=sys.stderr)
    except Exception as e:
        print(f"UNEXPECTED ERROR in safety-net pass ({describe_error(e)}) -- "
              f"continuing; investigate this", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Event-driven auto-SL: place a protective SL-LIMIT the instant a BUY fills.")
    parser.add_argument("--mode", choices=["paper", "live"], default="paper",
                         help="paper = real login, real fills detected, but SL-LIMIT "
                              "placement is only logged, never sent. "
                              "live = real SL-LIMIT orders placed on real fills.")
    parser.add_argument("--config", default=None,
                         help="Path to a config.yaml-style file (default: ./config.yaml "
                              "if it exists, else built-in defaults). See config.example.yaml.")
    # These three default to None (not their real defaults) so resolve()
    # below can tell "the user actually passed this flag" apart from
    # "use whatever config.yaml/the built-in default says" -- that's the
    # CLI > config.yaml > built-in-default precedence this project uses.
    parser.add_argument("--stop-loss", type=float, default=None,
                         help="Loss threshold as a percentage below fill price. "
                              "Must be negative. Overrides config.yaml/default "
                              f"({_CONFIG_DEFAULTS['stop_loss_pct']}).")
    parser.add_argument("--safety-net-interval", type=float, default=None,
                         help="Seconds between safety-net polls. Overrides "
                              f"config.yaml/default ({_CONFIG_DEFAULTS['safety_net_interval']}).")
    parser.add_argument("--stuck-sl-margin-pct", type=float, default=None,
                         help="How many extra points of loss past --stop-loss "
                              "before the stuck-SL watchdog fires. Overrides "
                              f"config.yaml/default ({_CONFIG_DEFAULTS['stuck_sl_margin_pct']}).")
    args = parser.parse_args()

    config = load_config(args.config)
    stop_loss_pct = resolve(args.stop_loss, config, "stop_loss_pct")
    safety_net_interval = resolve(args.safety_net_interval, config, "safety_net_interval")
    stuck_sl_margin_pct = resolve(args.stuck_sl_margin_pct, config, "stuck_sl_margin_pct")
    sl_limit_buffer = config["sl_limit_buffer"]
    enabled_exchanges = config["enabled_exchanges"]

    if stop_loss_pct >= 0:
        print(f"ERROR: stop-loss must be negative, got {stop_loss_pct}", file=sys.stderr)
        sys.exit(1)

    dry_run = args.mode == "paper"
    print(f"Starting auto-SL in {args.mode.upper()} mode | "
          f"stop-loss = {stop_loss_pct}% below fill | "
          f"safety-net every {safety_net_interval}s | "
          f"stuck-SL watchdog margin = {stuck_sl_margin_pct} pts | "
          f"enabled exchanges = {sorted(enabled_exchanges)}")
    if not dry_run:
        print("*** LIVE MODE: this will place REAL SL-LIMIT orders on REAL fills. ***")

    smart = login()
    broker = AngelOneBroker(smart=smart)  # reuses this same session, no second login

    protected = ProtectedSymbols()
    on_fill = make_on_fill(smart, stop_loss_pct, dry_run, protected, sl_limit_buffer)
    listener = OrderUpdateListener(smart, on_fill, enabled_exchanges=enabled_exchanges)
    listener.run_forever_in_background()
    print("Order-update listener starting -- watch for the '[WS] ... connected' line. "
          "Waiting for BUY fills... (Ctrl+C to stop)")

    try:
        while True:
            time.sleep(safety_net_interval)
            run_safety_net_cycle(broker, smart, stop_loss_pct, dry_run, protected,
                                  stuck_sl_margin_pct, enabled_exchanges, sl_limit_buffer)
    except KeyboardInterrupt:
        print("\nStopped by user.")


if __name__ == "__main__":
    main()
