"""
broker/angelone_session.py
============================
Shared Angel One SmartAPI login + LTP-fetch helpers, PLUS automatic
session refresh/re-login so a long-running process (main.py --mode
live/paper, main_autosl.py) never needs a human to re-authenticate.

Angel One's own forum guidance is that clients are expected to log in
"every day" -- session tokens are short-lived by design, not a bug to
work around once. `with_session_retry()` below is the mechanism every
real API call should go through so an expired session self-heals
instead of crashing the poll loop.

Failure-detection contract (verified against the installed SDK source,
not guessed): reading SmartApi/smartConnect.py's `_request()`, Angel
One's API surfaces an auth failure in TWO different shapes depending on
the endpoint:
  1. If the raw HTTP response includes an "error_type" field, the SDK
     itself raises a Python exception -- `TokenException` for auth
     issues specifically (smartExceptions.py). This exception's own
     message is built from ONLY Angel One's `data["message"]` (a short
     status string like "Invalid Token") -- never the request headers
     or params -- so logging str(e) anywhere in this file is safe.
  2. Otherwise (no "error_type" in the response), the SDK returns a
     perfectly normal-looking dict with `status: False` and
     `errorcode: "AG8001"` ("Invalid Token") -- no exception at all.
     Angel One's own response body in this shape has likewise never
     been observed (across every failure this project has actually
     triggered, including live) to echo back the submitted
     password/PIN/TOTP -- only status/message/errorcode/data. Logging
     the raw response dict (e.g. in refresh_or_relogin()'s
     RuntimeError) is safe for the same reason.
Both are checked below; relying on only one would miss real cases. The
credential-bearing log line this project has to actively guard against
lives INSIDE the SDK itself, not in either shape above -- see
harden_logzero_logging().
"""
from __future__ import annotations
import datetime
import os
import threading
import time

from .network_errors import TRANSIENT_NETWORK_ERRORS, describe_error, log_net_event

try:
    from SmartApi import SmartConnect
    from SmartApi.smartExceptions import TokenException
    import pyotp
except ImportError:  # pragma: no cover
    SmartConnect = None
    TokenException = ()  # empty tuple -> isinstance(e, TokenException) below is always False
    pyotp = None

# Per-request HTTP timeout, in seconds, applied to every SDK call. The SDK
# defaults to 7 (SmartConnect._default_timeout) and only overrides it if
# we pass timeout= to the constructor, which we previously never did --
# 7s is tight enough that an ordinary SSL-handshake stall to
# apiconnect.angelone.in trips it. `requests` applies this value to the
# connect phase (TCP + TLS handshake) and to each socket read separately.
_HTTP_TIMEOUT_SECONDS = 15

# Pause before the single automatic retry of a read-only call that hit a
# transient network error (see with_session_retry).
_NETWORK_RETRY_DELAY_SECONDS = 1.0


def harden_logzero_logging() -> None:
    """
    Suppresses smartapi-python's own internal logzero-based logging.

    Verified by reading the SDK source: smartConnect.py's _request()
    logs failures via `logger.error(f"... Headers: {headers}, Request:
    {params}, Response: {data}")` -- headers includes the bare
    "Authorization: Bearer <token>" string, and params for a login
    request includes the account PASSWORD/PIN and the computed TOTP
    code, all in plain text. That logger is logzero's shared global
    singleton (`from logzero import logger`), and
    SmartWebSocketOrderUpdate.__init__ unconditionally attaches a
    RotatingFileHandler to it (writing to ./logs/<date>/app.log) AND
    lowers the global log level to INFO -- on every construction,
    including every websocket reconnect -- which is why this must be
    called again after each one, not just once at import time (see
    order_update_listener.py's _build_client()).

    Our own error handling/reporting throughout this project (loud
    banners, RuntimeError messages, [AUTH]/[TICK-SIZE] logs) is
    independent of the SDK's internal logger, so suppressing it trades
    away some redundant SDK-internal debug detail for eliminating a
    real credential-leak vector -- both to stderr (which some
    unattended deployments redirect to a persistent log file) and to
    logzero's own file handler.
    """
    try:
        import logging
        import logzero
        logzero.loglevel(logging.CRITICAL)
        logzero.logfile(None)
    except ImportError:
        pass


harden_logzero_logging()  # apply immediately at import time, before any SDK call

# AG8001 "Invalid Token" is the documented auth-error code (Angel One
# SmartAPI forum). AB1007 confirmed live in production: a real
# safety-net SL-LIMIT placement returned
# {"status": False, "errorcode": "AB1007", "message": "Invalid Token"}.
# AB1007 has also been observed for "INVALID MPIN" (a bad password
# during login), so it is NOT exclusively an auth-expiry code -- but an
# invalid-session AB1007 must still trigger reauth, so it's listed
# explicitly here rather than relying solely on the message text
# matching _AUTH_ERROR_KEYWORDS below (belt and suspenders: match on
# EITHER the code or the message, since Angel One reuses codes across
# unrelated failure reasons and reuses wording inconsistently across
# endpoints). The keyword fallback catches further undocumented variants.
_AUTH_ERROR_CODES = {"AG8001", "AB1007"}
_AUTH_ERROR_KEYWORDS = ("invalid token", "session expired", "token expired")

# A failure message containing any of these almost always means the
# system clock has drifted relative to Angel One's server, not bad
# credentials -- TOTP codes are time-derived, so even a correct
# ANGEL_TOTP_SECRET produces a wrong code once the clocks disagree by
# more than the algorithm's tolerance window (typically ~30s).
_TOTP_DRIFT_KEYWORDS = ("totp", "2fa")

# Debounce: skip a redundant refresh if one just succeeded moments ago.
# main_autosl.py has at least three things that can hit an expired
# session around the same time (the order-update listener, the
# safety-net poll, and a concurrently-run healthcheck.py) -- the lock
# below already prevents them from mutating `smart` at the same instant,
# but without this debounce a SECOND thread would still queue up behind
# the lock and immediately fire its OWN redundant generateToken()/
# generateSession() call the moment the first one releases it. Two
# refreshes issued back-to-back for the same account is exactly the
# kind of thing that risks one invalidating the other's freshly-issued
# token, and it's pure waste even if Angel One tolerates it fine.
_REFRESH_DEBOUNCE_SECONDS = 5.0

# Retry-storm protection for a full re-login: back off progressively
# longer after each consecutive failure (capped), and print an
# impossible-to-miss banner once failures repeat enough times to look
# like a genuinely locked/disabled account rather than a one-off
# network blip -- rather than hammering the login endpoint every single
# poll tick, or crashing the process with a bare traceback.
_MAX_CONSECUTIVE_FAILURES_BEFORE_BANNER = 3
_BACKOFF_BASE_SECONDS = 5.0
_BACKOFF_MAX_SECONDS = 300.0

_reauth_lock = threading.Lock()
_last_refresh_monotonic = 0.0
_consecutive_relogin_failures = 0

# Identifies "which token is currently in use" -- incremented every time a
# refresh (lightweight or full) actually succeeds. See refresh_or_relogin's
# `known_stale_generation` parameter: this is what lets it tell apart "a
# DIFFERENT caller already refreshed since MY failure was observed, so I can
# trust the debounce and skip" from "the token is still the exact one that
# just failed on ME, so the debounce must not apply -- retrying with it
# again is pointless." Read/written only under _reauth_lock.
_session_generation = 0


def get_session_generation() -> int:
    """
    Snapshot of "which token is currently live" for a caller about to make
    a request. If that request then fails on auth grounds, pass this same
    value back in as refresh_or_relogin()'s known_stale_generation so it
    can force a real refresh through the debounce when nothing has changed
    since -- see that parameter's docstring.
    """
    return _session_generation


def log_auth_event(reason: str, outcome: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [AUTH] {reason} -> {outcome}")


def _check_totp_drift(failure) -> None:
    """
    Angel One returns the same generic shape for every login failure
    (status/message/errorcode) -- a bad TOTP looks structurally
    identical to a bad password unless something specifically calls out
    the word "totp" in the message. During unattended multi-day
    operation, a clock that's drifted is easy to misdiagnose as
    "credentials suddenly stopped working" days after the fact; this
    makes it impossible to miss in the log at the moment it happens.
    """
    text = str(failure).lower()
    if any(k in text for k in _TOTP_DRIFT_KEYWORDS):
        log_auth_event(
            "*** POSSIBLE TOTP CLOCK DRIFT ***",
            "login failed with a TOTP-related error -- check this machine's "
            "system clock is synced with a time server (e.g. `w32tm /resync` "
            "on Windows, `timedatectl` / `chronyc` on Linux). A few seconds "
            "of drift is enough to make every TOTP code wrong even with a "
            "correct ANGEL_TOTP_SECRET.",
        )


def _print_reauth_failure_banner(consecutive_failures: int, last_error) -> None:
    print("\n" + "@" * 60)
    print("  REPEATED RE-AUTHENTICATION FAILURES -- MANUAL ACTION LIKELY NEEDED")
    print(f"  Consecutive failures : {consecutive_failures}")
    print(f"  Last error           : {last_error}")
    print("  This does not look like a one-off network blip. Possible")
    print("  causes: account locked/disabled, password or PIN changed,")
    print("  2FA/TOTP reset, or API key revoked. This process will keep")
    print("  retrying with backoff, but every open position is currently")
    print("  running with NO working connection to Angel One -- check")
    print("  your account status and .env credentials now.")
    print("@" * 60 + "\n")


def login() -> "SmartConnect":
    """
    Log into Angel One using ANGEL_API_KEY / ANGEL_CLIENT_CODE /
    ANGEL_PASSWORD / ANGEL_TOTP_SECRET env vars (see SETUP.md).
    Returns an authenticated SmartConnect session.
    """
    if SmartConnect is None:
        raise RuntimeError(
            "smartapi-python / pyotp not installed. Run:\n"
            "    pip install smartapi-python pyotp websocket-client"
        )

    api_key = os.environ.get("ANGEL_API_KEY")
    client_code = os.environ.get("ANGEL_CLIENT_CODE")
    password = os.environ.get("ANGEL_PASSWORD")
    totp_secret = os.environ.get("ANGEL_TOTP_SECRET")
    missing = [n for n, v in [
        ("ANGEL_API_KEY", api_key), ("ANGEL_CLIENT_CODE", client_code),
        ("ANGEL_PASSWORD", password), ("ANGEL_TOTP_SECRET", totp_secret),
    ] if not v]
    if missing:
        raise RuntimeError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "See SETUP.md."
        )

    smart = SmartConnect(api_key=api_key, timeout=_HTTP_TIMEOUT_SECONDS)
    totp_code = pyotp.TOTP(totp_secret).now()
    try:
        session = smart.generateSession(client_code, password, totp_code)
    except TRANSIENT_NETWORK_ERRORS as e:
        # `from None`: generateSession() also calls getProfile(), which
        # puts the refresh token in a GET query string, so the raw
        # exception's text -- and a default traceback of its chain --
        # can contain a live token.
        raise RuntimeError(
            f"Angel One login failed: network error ({describe_error(e)})"
        ) from None
    if not session or not session.get("status"):
        _check_totp_drift(session)
        raise RuntimeError(f"Angel One login failed: {session}")
    return smart


def refresh_or_relogin(smart: "SmartConnect", known_stale_generation: int | None = None) -> None:
    """
    Refreshes an expired/invalid session, fully automatically -- no
    human input needed since ANGEL_TOTP_SECRET is already in .env.

    known_stale_generation: pass get_session_generation(), captured right
    before the call that then failed on auth grounds (this is exactly what
    with_session_retry does). It lets this function tell apart the two
    situations the plain time-based debounce alone cannot:
      - Someone ELSE already refreshed since that failure was observed
        (the generation has moved on) -- the debounce is trusted as-is and
        this call skips, since the caller's upcoming retry will pick up
        that newer token automatically.
      - The token is STILL the exact one that failure happened against (the
        generation hasn't moved) -- the debounce is bypassed and a real
        refresh is forced through, even if one technically completed
        moments ago for some unrelated reason. Without this, a single
        caller's own retry loop (e.g. stoploss_placer.py's 3 placement
        attempts, ~1.5s apart) would have its 2nd and 3rd attempts see
        "a refresh JUST happened" and skip re-authenticating entirely,
        silently retrying with the same dead token every time -- this was
        an actual production incident (AB1007 "Invalid Token" on all 3
        attempts, generateToken() called only once).
    Passing None (the default) preserves the plain time-based debounce for
    callers with no specific failure to correlate against (e.g. the
    websocket listener reconnecting after on_close, which isn't reacting
    to a with_session_retry failure).

    This is evaluated fresh, inside the lock, against the CURRENT
    generation -- not a value cached before acquiring it -- so the
    retry-storm protection this debounce exists for is unaffected: if two
    DIFFERENT callers hit a fresh, unrelated auth failure at the same
    generation, whichever acquires the lock first refreshes and bumps the
    generation; by the time the second one is actually checked, its
    known_stale_generation no longer matches, so it still skips normally.

    Tries a lightweight refresh first via generateToken(refresh_token)
    (no TOTP required, cheaper than a full login). generateToken() has
    no error handling of its own (see smartConnect.py source) -- on any
    failure shape (a raised TokenException, or a plain failure dict
    lacking response["data"]) it always raises SOME exception rather
    than returning a value that could be silently misread as success,
    so the broad `except Exception` below reliably catches every one of
    those shapes and falls through to a full generateSession() with a
    freshly computed TOTP code, identical to the initial login() above
    -- this isn't limited to "only when the ORIGINAL calling API call
    fails"; it independently handles the refresh token itself being
    dead, however this function got invoked.

    Mutates `smart` IN PLACE: SmartConnect stores access_token/
    feed_token/refresh_token as plain instance attributes that every
    API call reads off `self`, so every existing reference to this same
    `smart` object (a broker's self._smart, a listener's captured
    session, etc.) automatically sees the refreshed credentials -- no
    caller anywhere needs to swap out an object reference.

    Serialized with a lock so two threads hitting an expired session at
    the same moment (e.g. the auto-SL listener thread and the
    safety-net poll) don't both mutate `smart` at once. On top of that,
    a short debounce skips a SECOND thread's refresh entirely if one
    just succeeded moments ago (see _REFRESH_DEBOUNCE_SECONDS) --
    avoiding two redundant back-to-back re-logins for the same account,
    which is wasteful at best and, if Angel One's session semantics
    ever turn out to invalidate a still-fresh token on a second refresh,
    could otherwise undo the first thread's success.

    If refreshing/re-logging-in itself keeps failing across repeated
    calls (a genuinely locked/disabled account, changed credentials, a
    revoked API key -- not a transient blip), backs off progressively
    longer between full-relogin attempts and prints an unmissable
    banner once that streak crosses _MAX_CONSECUTIVE_FAILURES_BEFORE_BANNER,
    rather than hammering the login endpoint on every single poll tick.

    Raises one of two DISTINCT failure classes -- callers must handle both:
      - RuntimeError: an auth-class failure (the login was refused, or
        anything else that isn't a network problem). Counts toward the
        backoff/banner streak above.
      - a TRANSIENT_NETWORK_ERRORS exception (timeout, connection reset,
        DNS failure, ...), propagated unchanged. This is NOT an auth
        problem: it never touches the streak counter, never triggers the
        backoff sleep or the "account may be locked" banner, and a network
        error during the lightweight refresh does not fall through to a
        pointless full re-login.
    """
    global _last_refresh_monotonic, _consecutive_relogin_failures, _session_generation
    with _reauth_lock:
        since_last_refresh = time.monotonic() - _last_refresh_monotonic
        force = (known_stale_generation is not None
                 and known_stale_generation == _session_generation)
        if since_last_refresh < _REFRESH_DEBOUNCE_SECONDS and not force:
            log_auth_event(
                "Refresh/re-login request",
                f"skipped -- another thread refreshed the session "
                f"{since_last_refresh:.1f}s ago, within the "
                f"{_REFRESH_DEBOUNCE_SECONDS}s debounce window",
            )
            return
        if force and since_last_refresh < _REFRESH_DEBOUNCE_SECONDS:
            log_auth_event(
                "Refresh/re-login request",
                f"forcing a real refresh despite the {_REFRESH_DEBOUNCE_SECONDS}s "
                f"debounce window -- session is still at generation "
                f"{_session_generation}, the same one this caller's own "
                f"failed call just observed as broken",
            )

        if smart.refresh_token:
            try:
                smart.generateToken(smart.refresh_token)
                log_auth_event("Lightweight token refresh", "succeeded")
                _last_refresh_monotonic = time.monotonic()
                _consecutive_relogin_failures = 0
                _session_generation += 1
                return
            except TRANSIENT_NETWORK_ERRORS as e:
                # A timeout says nothing about whether the refresh token is
                # still good, so don't burn a full re-login on it, and don't
                # touch the auth-failure counter. Propagate as-is so the
                # caller classifies it as network noise.
                log_net_event(
                    f"Lightweight token refresh hit a network error ({describe_error(e)})",
                    "not an auth failure -- leaving the session as-is, will retry on the next request",
                )
                raise
            except Exception as e:
                _check_totp_drift(e)
                log_auth_event("Lightweight token refresh",
                                 f"failed ({describe_error(e)}) -- falling back to full re-login")

        if _consecutive_relogin_failures > 0:
            backoff = min(
                _BACKOFF_BASE_SECONDS * (2 ** (_consecutive_relogin_failures - 1)),
                _BACKOFF_MAX_SECONDS,
            )
            log_auth_event(
                "Full re-login",
                f"backing off {backoff:.0f}s before attempt "
                f"#{_consecutive_relogin_failures + 1} "
                f"({_consecutive_relogin_failures} consecutive failure(s) so far)",
            )
            time.sleep(backoff)

        try:
            client_code = os.environ.get("ANGEL_CLIENT_CODE")
            password = os.environ.get("ANGEL_PASSWORD")
            totp_secret = os.environ.get("ANGEL_TOTP_SECRET")
            totp_code = pyotp.TOTP(totp_secret).now()
            session = smart.generateSession(client_code, password, totp_code)
            if not session or not session.get("status"):
                _check_totp_drift(session)
                raise RuntimeError(f"Angel One re-login failed: {session}")
            log_auth_event("Full re-login", "succeeded")
            _last_refresh_monotonic = time.monotonic()
            _consecutive_relogin_failures = 0
            _session_generation += 1
        except TRANSIENT_NETWORK_ERRORS as e:
            # Not an auth problem: must NOT count toward the consecutive-
            # failure streak (which drives the backoff sleeps and the
            # "account may be locked" banner -- a false alarm during a
            # plain outage). Propagate as-is.
            log_net_event(
                f"Full re-login hit a network error ({describe_error(e)})",
                "not counted as an auth failure -- will retry on the next request",
            )
            raise
        except Exception as e:
            _consecutive_relogin_failures += 1
            log_auth_event("Full re-login",
                            f"failed ({describe_error(e)}) -- "
                            f"{_consecutive_relogin_failures} consecutive failure(s)")
            if _consecutive_relogin_failures >= _MAX_CONSECUTIVE_FAILURES_BEFORE_BANNER:
                _print_reauth_failure_banner(_consecutive_relogin_failures, describe_error(e))
            raise RuntimeError(
                f"Could not re-authenticate with Angel One: {describe_error(e)}"
            ) from e


def _looks_like_auth_failure(result) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get("errorcode") in _AUTH_ERROR_CODES:
        return True
    if result.get("status") is False:
        message = str(result.get("message", "")).lower()
        return any(k in message for k in _AUTH_ERROR_KEYWORDS)
    return False


def _call_with_network_retry(fn_name: str, idempotent: bool, func, args, kwargs):
    """
    One call to `func`. On a transient network error: if `idempotent`,
    retry exactly once after a short pause; otherwise re-raise untouched.
    """
    try:
        return func(*args, **kwargs)
    except TRANSIENT_NETWORK_ERRORS as e:
        if not idempotent:
            raise
        log_net_event(
            f"Transient network error calling {fn_name} ({describe_error(e)})",
            f"retrying once in {_NETWORK_RETRY_DELAY_SECONDS:g}s (safe: read-only call)",
        )
        time.sleep(_NETWORK_RETRY_DELAY_SECONDS)
        return func(*args, **kwargs)


def with_session_retry(smart: "SmartConnect", func, *args, idempotent: bool = False, **kwargs):
    """
    Calls func(*args, **kwargs), recovering from two DISTINCT kinds of
    transient failure -- each handled and logged separately:

    1. Auth expiry (logged [AUTH]): a raised TokenException, or a plain
       {"status": False, "errorcode": "AG8001", ...} response (Angel One
       uses both shapes depending on the endpoint, see module docstring).
       Refreshes/re-logs-in `smart` once and retries the call exactly
       once. Safe for ANY call, including order placement: an
       invalid-token request is rejected before it is processed, so it
       cannot have taken effect.

    2. Network noise (logged [NET]): a timeout, reset, DNS failure, etc.
       (see network_errors.TRANSIENT_NETWORK_ERRORS). Retried exactly
       once after a short pause -- but ONLY when the caller passes
       idempotent=True (read-only calls: position(), orderBook(),
       ltpData()). It must never be blindly retried for order
       placement: a timeout means the request MAY have reached the
       exchange and the order MAY exist even though no response came
       back, so an automatic retry could place a duplicate -- and a
       duplicate SELL stop-loss or square-off can over-sell a long
       position into an accidental short. For non-idempotent calls the
       network error simply propagates; callers resolve the ambiguity
       from ground truth (main_autosl.py's safety net checks the real
       order book; monitor.py re-reads the real position book).

    A second failure of either kind after its one retry is propagated
    as-is. Every real Angel One API call in this project goes through
    this, so a single expired session or network blip doesn't crash
    monitor.py's poll loop or main_autosl.py's listener/safety-net.

    Captures get_session_generation() BEFORE making the call, and passes
    it to refresh_or_relogin() as known_stale_generation if the call then
    fails on auth grounds -- this is what lets a caller invoking
    with_session_retry repeatedly in its own retry loop (e.g.
    stoploss_placer.py's 3 placement attempts) force a real refresh on
    EACH attempt when the session is genuinely still broken, rather than
    the plain time-based debounce silently starving attempts 2 and 3 of a
    real refresh just because attempt 1's refresh technically "just
    happened." See refresh_or_relogin's docstring for the full mechanism.
    """
    fn_name = getattr(func, "__name__", repr(func))
    generation_before_call = get_session_generation()
    try:
        result = _call_with_network_retry(fn_name, idempotent, func, args, kwargs)
        if not _looks_like_auth_failure(result):
            return result
        reason = f"errorcode={result.get('errorcode')!r} message={result.get('message')!r}"
    except TokenException as e:
        reason = str(e)

    log_auth_event(f"Auth failure calling {fn_name} ({reason})", "attempting reauth")
    # May raise RuntimeError (auth-class failure) or a network exception --
    # see refresh_or_relogin's docstring. Let either propagate.
    refresh_or_relogin(smart, known_stale_generation=generation_before_call)
    log_auth_event(f"Retrying {fn_name} after reauth", "in progress")
    return _call_with_network_retry(fn_name, idempotent, func, args, kwargs)


def fetch_ltp(smart: "SmartConnect", exchange: str, symbol: str, token: str) -> float:
    """
    Always ask the market-data endpoint for a fresh quote rather than
    trusting a broker response's own (often stale) ltp field.
    """
    resp = with_session_retry(smart, smart.ltpData, exchange, symbol, token, idempotent=True)
    if resp and resp.get("status"):
        return float(resp["data"]["ltp"])
    raise RuntimeError(f"Could not fetch LTP for {symbol}: {resp}")


def get_order_update_credentials(smart: "SmartConnect"):
    """
    Returns (auth_token, api_key, client_code, feed_token) for
    SmartApi.smartWebSocketOrderUpdate.SmartWebSocketOrderUpdate.

    auth_token needs a "Bearer " prefix: Angel One's own
    generateSession() response carries "Bearer " + jwtToken (see
    smartConnect.py), and the official smartapi-python test suite
    (test/api_test.py) passes that exact value straight through to this
    websocket class with no further prefixing. login() above only
    returns the SmartConnect session, not that raw response dict, so
    the prefix is reconstructed here from the same raw access_token
    SmartConnect already stores on itself. Called again after a
    refresh_or_relogin(), this picks up the new token automatically
    since it's read live off `smart`, not cached.
    """
    auth_token = f"Bearer {smart.access_token}"
    api_key = smart.api_key
    client_code = smart.userId
    feed_token = smart.getfeedToken()
    return auth_token, api_key, client_code, feed_token
