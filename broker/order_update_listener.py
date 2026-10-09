"""
broker/order_update_listener.py
=================================
Event-driven fill detection: wraps Angel One's order-update websocket
(wss://tns.angelone.in/smart-order-update, SmartWebSocketOrderUpdate in
smartapi-python) and calls back the instant one of YOUR BUY orders goes
to "complete" -- so main_autosl.py can place a protective SL-LIMIT the
moment a fill happens, instead of polling MTM% like monitor.py does.

Message shape: Angel One's own announcement of this websocket
(https://smartapi.angelone.in/smartapi/forum/topic/4041/...) documents
a top-level wrapper with "order-status" (a code like "AB05") and a
nested "orderData" object carrying the actual order fields
(tradingsymbol, symboltoken, exchange, producttype, transactiontype,
quantity, filledshares, averageprice, orderid, ...). There is no local
SDK source for this message schema -- SmartWebSocketOrderUpdate only
logs raw messages by default -- so this file owns the parsing.

Order-status codes (per that same announcement): AB00 after-connection,
AB01 open, AB02 cancelled, AB03 rejected, AB04 modified, AB05 complete,
AB06-AB11 various AMO/pending states.

PARTIAL FILLS: we act on AB05 (complete) primarily, but ALSO on AB02
(cancelled) when it carries a non-zero filled quantity -- this covers
an order that partially fills and then has its remainder cancelled
(e.g. IOC-style behavior, or an EOD auto-cancel), which would otherwise
never reach AB05 at all and would be silently unprotected until the
safety-net poll's next cycle. The existing qty<=0 guard below already
safely no-ops a normal, fill-less cancellation, so broadening the gate
to include AB02 does not change behavior for that common case. There is
no documented AB-code specifically for "partially filled, still open"
(the order hasn't reached a terminal state yet) -- if Angel One sends
such an update, this listener correctly ignores it (waits) since it
isn't AB05/AB02, and acts once a terminal state with filledshares > 0
arrives. This is unverified against a real partial fill; the
safety-net poll in main_autosl.py is independent defense-in-depth here
regardless, since it reasons from the real position book (ground
truth), not from order-status semantics.

NOT YET LIVE-TESTED: this parses a documented but unverified-by-us
message schema. Confirm the exact field names against a real message
the first time you run this live (print the raw message once) before
trusting it unattended -- see README.md "Auto-SL mode".
"""
from __future__ import annotations
import datetime
import json
import threading
import time
from typing import Callable, Set

from SmartApi.smartWebSocketOrderUpdate import SmartWebSocketOrderUpdate

from .angelone_session import (
    get_order_update_credentials, refresh_or_relogin, harden_logzero_logging,
)
from .network_errors import describe_error

# Terminal order-status codes where a BUY might have actually filled
# something -- see the "PARTIAL FILLS" note above for why AB02 is here
# alongside AB05.
_ACTIONABLE_STATUSES = {"AB05", "AB02"}

# Reconnect backoff: wait 2s after the first drop, doubling up to 60s while
# connections keep failing; reset to 2s once a connection stays up for 30s+
# (so an ordinary server-side disconnect after a long healthy session
# reconnects almost immediately, while an outage can't cause a tight loop).
_RECONNECT_BACKOFF_BASE_SECONDS = 2.0
_RECONNECT_BACKOFF_MAX_SECONDS = 60.0
_HEALTHY_CONNECTION_SECONDS = 30.0


def _log_ws(reason: str, outcome: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [WS] {reason} -> {outcome}")


class OrderUpdateListener:
    """
    Calls `on_buy_filled(fill)` exactly once per real fill, where `fill`
    is a dict with: order_id, symbol, token, exchange, product_type,
    qty, fill_price. Only fires for BUY orders that reach "complete" --
    SELL fills, rejections, modifies, and partial/pending states are
    all ignored.

    Maintains its own in-memory set of order ids it has already fired
    on, so a redelivered message after a websocket reconnect never
    triggers a second SL-LIMIT placement for the same fill. This is
    intentionally separate from (and simpler than) the safety-net poll
    in main_autosl.py, which checks Angel One's own order book rather
    than in-memory state -- so a fill this listener never saw at all
    (e.g. the websocket was down) still gets protected.

    Reconnects automatically, forever, whenever the websocket ends for ANY
    reason (server close, network drop, or a reconnect attempt that itself
    fails). A dedicated supervisor thread (_supervise) owns the whole
    lifecycle: connect() blocks until the connection ends, then it backs
    off, refreshes the session, builds a brand-new client with fresh
    credentials, and connects again -- SmartWebSocketOrderUpdate's own
    retry reuses the stale tokens it was constructed with and gives up
    after 2 attempts. Failure at ANY step (including the session refresh
    itself hitting a network error) is logged and retried with backoff;
    the loop never gives up, because a listener that quietly dies on one
    network blip silently loses instant SL placement for the rest of the
    process's life. (main_autosl.py's safety-net poll remains the
    backstop for anything missed in the meantime.)

    Verified against the installed websocket-client 1.9.2: with the
    default reconnect=0, a failed connect attempt calls on_error, then
    on_close, and run_forever() RETURNS (it does not raise) -- so the
    supervisor sees every way a connection can end as "connect() returned".

    Known residual gap: the SDK calls run_forever(ping_interval=10) with
    no ping_timeout, so a half-open connection (NAT/Wi-Fi drop with no RST)
    isn't noticed until the OS gives up on the socket, which can take
    minutes. Fills in that window are caught by the safety-net poll.
    """

    def __init__(self, smart, on_buy_filled: Callable[[dict], None], enabled_exchanges=None):
        """
        enabled_exchanges: optional set of exchange codes (e.g.
        {"NSE", "NFO"}); a BUY fill on any OTHER exchange is ignored
        entirely. None (the default) means no restriction. See
        config.example.yaml's "enabled_exchanges".
        """
        self._smart = smart
        self._on_buy_filled = on_buy_filled
        self._enabled_exchanges = enabled_exchanges
        self._protected_order_ids: Set[str] = set()
        self._lock = threading.Lock()
        self._client = None

    def _build_client(self) -> None:
        auth_token, api_key, client_code, feed_token = get_order_update_credentials(self._smart)
        self._client = SmartWebSocketOrderUpdate(auth_token, api_key, client_code, feed_token)
        # SmartWebSocketOrderUpdate.__init__ unconditionally re-enables
        # smartapi-python's own file-based logging (see
        # harden_logzero_logging()'s docstring) -- redo the mitigation
        # every time a new client is built, since that happens again on
        # every reconnect, not just once at process start.
        harden_logzero_logging()
        # SmartWebSocketOrderUpdate.connect() looks these callbacks up on
        # the instance at connect time, so patching the instance
        # attributes (rather than subclassing) intercepts every event.
        self._client.on_open = self._handle_open
        self._client.on_error = self._handle_error
        self._client.on_message = self._handle_message
        self._client.on_close = self._handle_close

    # The SDK routes its own open/error logging through a logger that
    # harden_logzero_logging() deliberately silences, so without these a
    # listener that never connects would look exactly like a healthy one.
    def _handle_open(self, wsapp) -> None:
        _log_ws("order-update websocket connected", "listening for fills")

    def _handle_error(self, wsapp, error) -> None:
        _log_ws(f"websocket error ({describe_error(error)})", "connection will be re-established")

    def _handle_close(self, wsapp, close_status_code, close_msg) -> None:
        # Deliberately only logs. The SDK's own on_close calls its
        # retry_connect() (recursive, stale tokens, gives up after 2
        # attempts); overriding it hands reconnecting to _supervise instead.
        _log_ws(f"websocket closed (code={close_status_code}, msg={str(close_msg)[:80]!r})",
                "supervisor will reconnect")

    def _supervise(self) -> None:
        backoff = _RECONNECT_BACKOFF_BASE_SECONDS
        first_attempt = True
        while True:
            if not first_attempt:
                _log_ws("reconnecting", f"waiting {backoff:.0f}s, then refreshing the session")
                time.sleep(backoff)
                backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX_SECONDS)
                try:
                    refresh_or_relogin(self._smart)
                except Exception as e:
                    # RuntimeError (auth-class) or a network error -- either
                    # way the right response is the same: keep trying.
                    _log_ws(f"session refresh failed ({describe_error(e)})",
                            "will keep retrying with backoff")
                    continue
            first_attempt = False

            started = time.monotonic()
            try:
                self._build_client()
                self._client.connect()  # blocks until this connection ends
            except Exception as e:
                _log_ws(f"unexpected error in connection loop ({describe_error(e)})",
                        "will retry with backoff")
            if time.monotonic() - started >= _HEALTHY_CONNECTION_SECONDS:
                backoff = _RECONNECT_BACKOFF_BASE_SECONDS

    def _handle_message(self, wsapp, message) -> None:
        if not message:
            return
        try:
            payload = json.loads(message)
        except (TypeError, ValueError):
            return  # heartbeat ("pong") or other non-JSON frame

        order_status = payload.get("order-status") if isinstance(payload, dict) else None
        if order_status not in _ACTIONABLE_STATUSES:
            return

        order = payload.get("orderData") or {}
        if order.get("transactiontype") != "BUY":
            return

        exchange = order.get("exchange")
        if self._enabled_exchanges is not None and exchange not in self._enabled_exchanges:
            return  # segment disabled via config -- not ours to touch at all

        order_id = order.get("orderid")
        if not order_id:
            return

        with self._lock:
            if order_id in self._protected_order_ids:
                return  # already handled this exact fill -- dedup a redelivered message
            self._protected_order_ids.add(order_id)

        try:
            if order_status == "AB05":
                # complete -- filledshares should equal quantity anyway;
                # quantity is a reasonable fallback if filledshares is
                # ever absent.
                qty = int(float(order.get("filledshares") or order.get("quantity") or 0))
            else:
                # AB02 cancelled -- "quantity" is what was REQUESTED, not
                # what happened. Falling back to it here would treat a
                # clean, fill-less cancellation as a full-quantity fill.
                # Only trust filledshares for this status.
                qty = int(float(order.get("filledshares") or 0))
            fill_price = float(order.get("averageprice") or 0)
        except (TypeError, ValueError):
            return

        if qty <= 0 or fill_price <= 0:
            return  # incomplete/malformed update -- nothing usable yet

        fill = {
            "order_id": order_id,
            "symbol": order.get("tradingsymbol"),
            "token": order.get("symboltoken"),
            "exchange": exchange,
            "product_type": order.get("producttype"),
            "qty": qty,
            "fill_price": fill_price,
        }
        try:
            self._on_buy_filled(fill)
        except Exception as e:
            # websocket-client catches exceptions raised inside callbacks
            # and logs them to its own logger (which has no handler, so
            # nothing prints) before calling on_error -- which the SDK
            # routes to a logger we deliberately suppress. Without this,
            # a fill handler that blew up (e.g. a network timeout while
            # placing the SL) would vanish with ZERO output and leave a
            # real position unprotected.
            print(f"\n[WS] ERROR: the fill handler raised while protecting BUY "
                  f"order {order_id} ({fill['symbol']}, qty={qty}): "
                  f"{describe_error(e)}\n"
                  f"     This position may be UNPROTECTED until the safety-net "
                  f"poll picks it up.")

    def run_forever_in_background(self) -> threading.Thread:
        """
        Starts the supervisor on a daemon thread and returns it. The
        supervisor connects, and reconnects forever (see class docstring);
        SmartWebSocketOrderUpdate.connect() blocks (run_forever()), so
        main_autosl.py needs this off the main thread to also run its own
        safety-net poll loop.
        """
        thread = threading.Thread(target=self._supervise, daemon=True,
                                  name="order-update-listener")
        thread.start()
        return thread
