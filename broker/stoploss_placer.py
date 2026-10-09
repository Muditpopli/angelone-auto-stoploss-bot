"""
broker/stoploss_placer.py
===========================
Given a filled BUY (this trader only ever buys options -- see
expiry_calendar.py / mock_broker.py), places a STOPLOSS_LIMIT (SL-L)
SELL order stop_loss_pct below the fill price. This is the event-driven
alternative to babysitting MTM% via the polling monitor in monitor.py --
see broker/order_update_listener.py for what detects the fill in the
first place.

Per client request, this uses a LIMIT order (not a market order): the
client is wary of market orders and wants a bounded exit price, and
explicitly accepts that in a rare gap-through scenario (price jumps
past both the trigger and the limit before the order can fill) they
will exit manually rather than have the bot force a market fill. See
README.md "SL order type: Limit vs Market" for the full reasoning and
main_autosl.py's stuck-SL watchdog, which exists specifically to make
that rare scenario impossible to miss rather than silently paper over
it with an automatic market fallback.

Angel One requires variety="STOPLOSS" (not "NORMAL") for SL/SL-L
orders, ordertype="STOPLOSS_LIMIT", and both "triggerprice" and "price"
(the limit price) fields. Confirmed against Angel One's own official
smartapi-java SDK source (not guessed): Constants.java defines
VARIETY_STOPLOSS = "STOPLOSS" and ORDER_TYPE_STOPLOSS_LIMIT =
"STOPLOSS_LIMIT", and Examples.java's stop-loss sample sets both
orderParams.price (the limit) and orderParams.triggerprice alongside
variety=STOPLOSS. smartapi-python's own placeOrder() does not validate
variety/ordertype combinations client-side -- it just forwards whatever
dict it's given -- so getting this right here matters.

Tick size is NOT hardcoded here -- see instrument_master.py. NFO/BFO
index options happen to use 0.05, but MCX commodities (crude oil, gold,
silver, natural gas, copper, ...) all have their own tick sizes, so
compute_trigger_price()/compute_limit_price() take the real tick size
for the specific instrument as an explicit, required argument rather
than assuming one.
"""
from __future__ import annotations
import itertools
import time
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .angelone_session import with_session_retry
from .instrument_master import get_tick_size
from .network_errors import TRANSIENT_NETWORK_ERRORS, describe_error


class PlacementOutcomeUnknown(RuntimeError):
    """The placement request hit a network error, so the SL-LIMIT order may
    or may not exist on the exchange. Subclasses RuntimeError so existing
    `except RuntimeError` callers handle it, but is distinct so it can be
    told apart from a definite rejection."""


# A failed SL-LIMIT placement leaves a real, live position with ZERO
# protection -- retry a few times for a transient failure (a momentary
# API hiccup, a fleeting margin/price-band check) before giving up.
# with_session_retry already handles session-expiry failures separately
# and specifically; this is a broader, dumber retry for everything else.
_MAX_PLACEMENT_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = 1.5

# Gap between the trigger price and the limit price, in the
# instrument's own price units (rupees), for the sell-side SL-LIMIT
# order below. Standard SL-order convention on NSE/BSE/MCX: a SELL
# LIMIT fills at its limit price OR BETTER (i.e. higher), so setting
# the limit BELOW the trigger gives the order room to still fill as
# price continues falling through the trigger before the order is
# actually processed -- without this gap, a SELL LIMIT set exactly at
# (or above) the trigger price would very often go unfilled the instant
# price gaps down past it. Tune this per instrument's typical
# volatility/liquidity if 0.5 turns out too tight or too loose.
SL_LIMIT_BUFFER = Decimal("0.5")

_dry_run_order_seq = itertools.count(1)


def _round_up_to_tick(raw: Decimal, tick: Decimal) -> Decimal:
    """
    Round UP (toward the entry price, never down/away from it).

    This is a long-only stop-loss: trigger_price sits below fill_price,
    so rounding it UP moves it closer to fill_price, i.e. shrinks the
    loss. Rounding to the *nearest* tick (or Python's banker's-rounding
    round()) can just as easily round DOWN, which pushes the trigger
    further from entry and lets the position lose strictly more than
    stop_loss_pct before the SL fires -- exactly the failure mode a
    stop-loss exists to prevent. Rounding UP guarantees the real exit
    loss is never worse than configured; at worst it fires a hair
    early (e.g. -9.97% instead of exactly -10%), never late.
    """
    ticks = (raw / tick).to_integral_value(rounding=ROUND_CEILING)
    return ticks * tick


def _round_down_to_tick(raw: Decimal, tick: Decimal) -> Decimal:
    """
    Round DOWN (away from entry, away from the trigger).

    Used for the LIMIT price, not the trigger: the whole point of
    SL_LIMIT_BUFFER is to guarantee the sell-limit order has room to
    fill as price gaps through the trigger. Rounding the limit price UP
    (toward the trigger) would shrink that buffer below what was
    configured, undermining the reason it exists. Rounding DOWN instead
    guarantees the real gap between trigger and limit is never smaller
    than SL_LIMIT_BUFFER, only ever a hair larger.
    """
    ticks = (raw / tick).to_integral_value(rounding=ROUND_FLOOR)
    return ticks * tick


def compute_trigger_price(fill_price: float, stop_loss_pct: float, tick_size: Decimal) -> float:
    """
    trigger_price = fill_price * (1 + stop_loss_pct/100), rounded UP to
    the real tick size for this instrument (see _round_up_to_tick for
    why up, not nearest). stop_loss_pct must be negative (same
    validation as StopLossMonitor in monitor.py). tick_size is required,
    not defaulted -- see instrument_master.py for why it must come from
    the real scrip master rather than an assumed constant.

    Computed in Decimal, not float: fill_price * (1 + pct/100) done in
    binary float arithmetic can land a hair off the "true" decimal value
    (e.g. 97.25 * 0.9 not landing on exactly 87.525), which would then
    round to the wrong tick. Decimal(str(x)) keeps the exact decimal
    value the caller intended.
    """
    if stop_loss_pct >= 0:
        raise ValueError(
            "stop_loss_pct must be negative (it's a loss threshold), "
            f"got {stop_loss_pct}"
        )
    fill = Decimal(str(fill_price))
    pct = Decimal(str(stop_loss_pct))
    raw = fill * (Decimal(1) + pct / Decimal(100))
    return float(_round_up_to_tick(raw, tick_size))


def compute_limit_price(trigger_price: float, tick_size: Decimal,
                         buffer: Decimal = SL_LIMIT_BUFFER) -> float:
    """
    limit_price = trigger_price - buffer, rounded DOWN to the real tick
    size (see _round_down_to_tick for why down, not nearest -- it's the
    opposite direction from compute_trigger_price on purpose).
    """
    raw = Decimal(str(trigger_price)) - buffer
    return float(_round_down_to_tick(raw, tick_size))


def _print_placement_failure_banner(symbol: str, qty: int, trigger_price: float,
                                     limit_price: float, last_response,
                                     outcome_unknown: bool = False) -> None:
    print("\n" + "#" * 60)
    if outcome_unknown:
        print("  SL-LIMIT PLACEMENT STATUS UNKNOWN (NETWORK ERROR)")
        print("  -- POSITION MAY BE UNPROTECTED")
    else:
        print("  SL-LIMIT PLACEMENT FAILED -- POSITION IS UNPROTECTED")
    print(f"  Symbol                 : {symbol}")
    print(f"  Quantity               : {qty}")
    print(f"  Intended trigger/limit : {trigger_price} / {limit_price}")
    print(f"  Last error             : {last_response}")
    if outcome_unknown:
        print("  The request timed out, so the order may or may not have")
        print("  reached the exchange. NOT retrying blindly (a duplicate SELL")
        print("  stop-loss could over-sell this position into a short). The")
        print("  safety-net poll will check the real order book and place one")
        print("  ONLY if none exists. Check Angel One now if it doesn't settle")
        print("  within the next safety-net cycle.")
    else:
        print("  This REAL position currently has NO stop-loss order resting")
        print("  on the exchange. The safety-net poll will keep retrying")
        print("  automatically, but check Angel One now and consider")
        print("  protecting this position manually if it doesn't resolve")
        print("  within the next safety-net cycle.")
    print("#" * 60 + "\n")


def place_stop_loss(smart, symbol: str, token: str, exchange: str,
                     product_type: str, qty: int, fill_price: float,
                     stop_loss_pct: float = -10.0, dry_run: bool = False,
                     sl_limit_buffer=None) -> str:
    """
    Places (or, if dry_run, only logs) a SELL STOPLOSS_LIMIT order for a
    long position that just got filled: trigger stop_loss_pct below
    fill_price, limit sl_limit_buffer further below that. Returns the
    order id.

    sl_limit_buffer: Decimal, or None (the default) to use the module's
    SL_LIMIT_BUFFER constant. Passed explicitly as a parameter (rather
    than only read as compute_limit_price()'s own default argument) so a
    caller can override it per call -- e.g. from a loaded config.yaml --
    without needing to reach into this module's internals. Exposed via
    config.yaml's "sl_limit_buffer" key; see config_loader.py.

    Retries up to _MAX_PLACEMENT_ATTEMPTS times (with a short backoff)
    for a transient failure -- a momentary API hiccup or a fleeting
    margin/price-band rejection can clear up a second later, and
    waiting for main_autosl.py's next safety-net cycle (tens of
    seconds) is a needlessly long time for a real, live, unprotected
    position to sit exposed when a quick retry might fix it immediately.

    Raises RuntimeError if it still fails after every attempt -- a
    silently-missing SL on a live position is the one failure mode that
    must be loud, so this prints an unmissable banner (in addition to
    raising) right before giving up, regardless of which caller
    (the fill listener or the safety-net poll) invoked this.
    """
    buffer = sl_limit_buffer if sl_limit_buffer is not None else SL_LIMIT_BUFFER
    tick_size = get_tick_size(token)
    trigger_price = compute_trigger_price(fill_price, stop_loss_pct, tick_size)
    limit_price = compute_limit_price(trigger_price, tick_size, buffer=buffer)

    if dry_run:
        order_id = f"PAPER-SL-{next(_dry_run_order_seq):06d}"
        print(f"[AUTO-SL] {symbol}: filled BUY @ {fill_price} -> "
              f"WOULD place SL-LIMIT SELL @ trigger {trigger_price} / "
              f"limit {limit_price} (paper mode -- no real order placed), "
              f"fake order id {order_id}")
        return order_id

    order_params = {
        "variety": "STOPLOSS",
        "tradingsymbol": symbol,
        "symboltoken": token,
        "transactiontype": "SELL",
        "exchange": exchange,
        "ordertype": "STOPLOSS_LIMIT",
        "producttype": product_type,
        "duration": "DAY",
        "price": str(limit_price),
        "triggerprice": str(trigger_price),
        "quantity": str(qty),
    }

    last_response = None
    for attempt in range(1, _MAX_PLACEMENT_ATTEMPTS + 1):
        try:
            # placeOrderFullResponse() hits the exact same endpoint as
            # placeOrder() (identical URL in the SDK's own _routes
            # table) but returns the full response dict even on failure
            # -- placeOrder() silently swallows a failure's
            # errorcode/message and returns None, which would hide an
            # auth failure from with_session_retry.
            resp = with_session_retry(smart, smart.placeOrderFullResponse, order_params)
        except TRANSIENT_NETWORK_ERRORS as e:
            # Outcome UNKNOWN: a timeout/reset means the request may well
            # have reached the exchange and the order may exist even
            # though no response came back. Retrying here could place a
            # DUPLICATE SELL stop-loss, which can over-sell the position
            # into an accidental short -- so never retry blindly. Hand off
            # to main_autosl.py's safety net instead: it checks the real
            # order book and only places an SL if none exists. (Contrast
            # the definite-rejection paths below, where the exchange
            # explicitly said no and a retry is safe.)
            detail = describe_error(e)
            _print_placement_failure_banner(symbol, qty, trigger_price,
                                            limit_price, detail, outcome_unknown=True)
            raise PlacementOutcomeUnknown(
                f"SL-LIMIT placement for {symbol} hit a network error ({detail}); "
                "the order may or may not exist -- not retrying blindly"
            ) from None
        except RuntimeError as e:
            # with_session_retry's own reauth attempt failed outright
            # (e.g. account locked, credentials changed -- see
            # angelone_session.py's retry-storm protection). The reauth
            # failed before any order call was made, so nothing was
            # placed and a retry is safe. Treat this exactly like any
            # other definite failure so the retry loop (and the eventual
            # loud banner below) still applies, rather than escaping
            # immediately and skipping both.
            last_response = str(e)
            if attempt < _MAX_PLACEMENT_ATTEMPTS:
                print(f"[AUTO-SL] WARNING: SL-LIMIT placement attempt "
                      f"{attempt}/{_MAX_PLACEMENT_ATTEMPTS} for {symbol} failed "
                      f"({last_response}) -- retrying in {_RETRY_BACKOFF_SECONDS}s...")
                time.sleep(_RETRY_BACKOFF_SECONDS)
            continue

        order_id = (resp.get("data") or {}).get("orderid") or resp.get("orderid") \
            if isinstance(resp, dict) else resp
        if order_id:
            print(f"[AUTO-SL] {symbol}: filled BUY @ {fill_price} -> "
                  f"placed SL-LIMIT SELL @ trigger {trigger_price} / limit "
                  f"{limit_price}, order id {order_id}")
            return str(order_id)

        last_response = resp
        if attempt < _MAX_PLACEMENT_ATTEMPTS:
            print(f"[AUTO-SL] WARNING: SL-LIMIT placement attempt "
                  f"{attempt}/{_MAX_PLACEMENT_ATTEMPTS} for {symbol} failed "
                  f"({resp}) -- retrying in {_RETRY_BACKOFF_SECONDS}s...")
            time.sleep(_RETRY_BACKOFF_SECONDS)

    _print_placement_failure_banner(symbol, qty, trigger_price, limit_price, last_response)
    raise RuntimeError(
        f"Failed to place SL-LIMIT order for {symbol} (qty={qty}, "
        f"trigger={trigger_price}, limit={limit_price}) after "
        f"{_MAX_PLACEMENT_ATTEMPTS} attempts: {last_response}"
    )
