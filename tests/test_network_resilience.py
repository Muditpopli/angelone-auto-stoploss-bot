"""
tests/test_network_resilience.py
==================================
Regression tests for the production crash where a plain network timeout
(requests ReadTimeout on a 7s SSL-handshake stall) killed main_autosl.py.
The loops only caught RuntimeError, and smartapi-python re-raises whatever
`requests` raises, so the exception escaped every handler.

Covers: what counts as "transient network noise"; that both long-running
loops log it and carry on; the two DISTINCT retry paths in
with_session_retry (auth vs network) and the rule that order placement is
never blindly retried; that network errors never count toward the
auth-failure backoff/banner; the HTTP timeout actually being applied; and
that logged network errors can't leak the refresh token.
"""
import os
import socket
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import requests
import urllib3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main_autosl
from broker import angelone_session
from broker.angelone_session import refresh_or_relogin, with_session_retry
from broker.base import Broker
from broker.mock_broker import MockBroker
from broker.network_errors import TRANSIENT_NETWORK_ERRORS, describe_error
from broker.stoploss_placer import PlacementOutcomeUnknown, place_stop_loss
from monitor import StopLossMonitor
from SmartApi.smartExceptions import DataException, NetworkException, TokenException

_ENV = {
    "ANGEL_API_KEY": "k", "ANGEL_CLIENT_CODE": "X", "ANGEL_PASSWORD": "Y",
    "ANGEL_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
}
_SLEEP = "broker.angelone_session.time.sleep"

_NETWORK_SAMPLES = [
    requests.exceptions.ReadTimeout("Read timed out. (read timeout=7)"),
    requests.exceptions.ConnectTimeout("connect timed out"),
    requests.exceptions.ConnectionError("Max retries exceeded"),
    requests.exceptions.SSLError("handshake failed"),
    socket.gaierror("Name or service not known"),   # OSError, but NOT a ConnectionError
    ConnectionResetError("reset by peer"),
    TimeoutError("timed out"),
    urllib3.exceptions.ReadTimeoutError(None, "/x", "Read timed out."),
    DataException("Couldn't parse the JSON response received from the server"),
    NetworkException("network issue between api and OMS"),
]


def _reset_session_state():
    angelone_session._last_refresh_monotonic = 0.0
    angelone_session._consecutive_relogin_failures = 0


def _timeout():
    return requests.exceptions.ReadTimeout("Read timed out. (read timeout=7)")


# -- classification --------------------------------------------------------

@pytest.mark.parametrize("exc", _NETWORK_SAMPLES, ids=lambda e: type(e).__name__)
def test_realistic_sdk_network_failures_are_classified_transient(exc):
    assert isinstance(exc, TRANSIENT_NETWORK_ERRORS)


@pytest.mark.parametrize("exc", [TokenException("Invalid Token"), RuntimeError("x"),
                                 KeyError("k"), ValueError("v")],
                         ids=lambda e: type(e).__name__)
def test_auth_and_logic_errors_are_not_classified_transient(exc):
    # An auth failure mistaken for network noise would never get re-authenticated.
    assert not isinstance(exc, TRANSIENT_NETWORK_ERRORS)


# -- monitor.py poll loop ---------------------------------------------------

class _FlakyBroker(Broker):
    """Raises the queued exceptions (one per get_positions() call) before
    delegating to a real MockBroker."""

    def __init__(self, inner, failures):
        self._inner = inner
        self._failures = list(failures)

    def get_positions(self):
        if self._failures:
            raise self._failures.pop(0)
        return self._inner.get_positions()

    def square_off(self, position):
        return self._inner.square_off(position)


def _breaching_mock():
    inner = MockBroker()
    legs = inner.get_positions()
    inner.set_price_path(legs[0].symbol, [legs[0].avg_price * 0.85])  # -15%: breaches at once
    for leg in legs[1:]:
        inner.set_price_path(leg.symbol, [leg.avg_price])
    return inner, legs[0]


@pytest.mark.parametrize("exc", _NETWORK_SAMPLES, ids=lambda e: type(e).__name__)
def test_monitor_logs_a_network_error_tick_and_keeps_going(exc, capsys):
    inner, target = _breaching_mock()
    mon = StopLossMonitor(_FlakyBroker(inner, [exc]), stop_loss_pct=-10.0, poll_interval=0)

    executed = mon.run(max_iterations=2)  # tick 1 fails; tick 2 must still run

    assert [e["symbol"] for e in executed] == [target.symbol]
    out = capsys.readouterr().out
    assert "[NET]" in out and type(exc).__name__ in out


def test_monitor_unbounded_survives_an_unexpected_exception_but_not_ctrl_c(capsys):
    class _Boom(Broker):
        calls = 0

        def get_positions(self):
            _Boom.calls += 1
            if _Boom.calls == 1:
                raise ValueError("schema changed")
            raise KeyboardInterrupt

        def square_off(self, position):
            raise AssertionError("unreachable")

    mon = StopLossMonitor(_Boom(), poll_interval=0)
    with patch("monitor.time.sleep"), pytest.raises(KeyboardInterrupt):
        mon.run(max_iterations=None)

    out = capsys.readouterr().out
    assert "UNEXPECTED ERROR" in out and "ValueError" in out


def test_monitor_bounded_run_still_surfaces_unexpected_exceptions():
    class _Boom(Broker):
        def get_positions(self):
            raise ValueError("a bug the test should see")

        def square_off(self, position):
            raise AssertionError("unreachable")

    with pytest.raises(ValueError):
        StopLossMonitor(_Boom(), poll_interval=0).run(max_iterations=1)


# -- main_autosl.py safety-net cycle ------------------------------------------

@pytest.mark.parametrize("exc", _NETWORK_SAMPLES, ids=lambda e: type(e).__name__)
def test_safety_net_cycle_logs_network_error_and_the_next_cycle_still_runs(exc, capsys):
    broker = MagicMock()
    broker.get_positions.side_effect = [exc, []]  # cycle 1 fails, cycle 2 is healthy
    smart, protected = MagicMock(), main_autosl.ProtectedSymbols()

    main_autosl.run_safety_net_cycle(broker, smart, -10.0, True, protected)  # must not raise
    assert "[NET]" in capsys.readouterr().out

    main_autosl.run_safety_net_cycle(broker, smart, -10.0, True, protected)
    assert broker.get_positions.call_count == 2


def test_safety_net_cycle_survives_a_timeout_reading_the_order_book():
    """The order book read is idempotent, so it is retried once first; if
    that also times out, the cycle is skipped rather than crashing."""
    pos = MagicMock(net_qty=75, symbol="X", exchange="NFO")
    broker = MagicMock()
    broker.get_positions.return_value = [pos]
    smart = MagicMock()
    smart.orderBook.side_effect = _timeout()

    with patch(_SLEEP):
        main_autosl.run_safety_net_cycle(broker, smart, -10.0, True, main_autosl.ProtectedSymbols())

    assert smart.orderBook.call_count == 2  # original + exactly one retry


def test_safety_net_cycle_survives_runtime_and_unexpected_errors_but_not_ctrl_c(capsys):
    broker, smart, protected = MagicMock(), MagicMock(), main_autosl.ProtectedSymbols()

    broker.get_positions.side_effect = RuntimeError("Could not re-authenticate")
    main_autosl.run_safety_net_cycle(broker, smart, -10.0, True, protected)
    broker.get_positions.side_effect = ValueError("schema changed")
    main_autosl.run_safety_net_cycle(broker, smart, -10.0, True, protected)
    err = capsys.readouterr().err
    assert "safety-net pass failed" in err and "UNEXPECTED ERROR" in err

    broker.get_positions.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        main_autosl.run_safety_net_cycle(broker, smart, -10.0, True, protected)


# -- with_session_retry: two distinct retry paths ---------------------------------

def test_idempotent_call_is_retried_once_on_a_network_timeout_and_logged_as_net(capsys):
    calls = {"n": 0}

    def read_positions():
        calls["n"] += 1
        if calls["n"] == 1:
            raise _timeout()
        return {"status": True, "data": "ok"}

    smart = MagicMock()
    with patch(_SLEEP) as sleeper:
        result = with_session_retry(smart, read_positions, idempotent=True)

    assert result == {"status": True, "data": "ok"} and calls["n"] == 2
    sleeper.assert_called_once()
    out = capsys.readouterr().out
    assert "[NET]" in out and "[AUTH]" not in out  # logged as network, not as an auth retry
    smart.generateToken.assert_not_called()        # a timeout must not trigger a re-login
    smart.generateSession.assert_not_called()


def test_idempotent_retry_happens_exactly_once():
    calls = {"n": 0}

    def always_times_out():
        calls["n"] += 1
        raise _timeout()

    with patch(_SLEEP), pytest.raises(requests.exceptions.ReadTimeout):
        with_session_retry(MagicMock(), always_times_out, idempotent=True)
    assert calls["n"] == 2


def test_order_placement_is_never_blindly_retried_on_a_network_timeout():
    """A timeout on an order is ambiguous -- it may have reached the
    exchange. Retrying could place a duplicate SELL and flip a long into a
    short, so a non-idempotent call must be attempted exactly once."""
    calls = {"n": 0}

    def place_order():
        calls["n"] += 1
        raise _timeout()

    with patch(_SLEEP), pytest.raises(requests.exceptions.ReadTimeout):
        with_session_retry(MagicMock(), place_order)  # idempotent defaults to False
    assert calls["n"] == 1


def test_auth_expiry_retry_still_works_and_is_logged_as_auth(capsys):
    _reset_session_state()
    smart = MagicMock()
    smart.refresh_token = "r"
    smart.generateToken.return_value = {"status": True}
    responses = [{"status": False, "errorcode": "AG8001", "message": "Invalid Token"},
                 {"status": True, "data": "ok"}]

    result = with_session_retry(smart, lambda: responses.pop(0), idempotent=True)

    assert result == {"status": True, "data": "ok"}
    smart.generateToken.assert_called_once()
    out = capsys.readouterr().out
    assert "[AUTH]" in out and "[NET]" not in out


# -- refresh_or_relogin: network noise is not an auth failure ------------------------

def test_network_error_during_lightweight_refresh_is_not_an_auth_failure(capsys):
    _reset_session_state()
    smart = MagicMock()
    smart.refresh_token = "r"
    smart.generateToken.side_effect = _timeout()

    with pytest.raises(requests.exceptions.ReadTimeout):
        refresh_or_relogin(smart)

    assert angelone_session._consecutive_relogin_failures == 0
    smart.generateSession.assert_not_called()  # a timeout says nothing about the token
    assert "[NET]" in capsys.readouterr().out


def test_network_errors_during_full_relogin_never_trigger_backoff_or_banner(capsys):
    _reset_session_state()
    smart = MagicMock()
    smart.refresh_token = None
    smart.generateSession.side_effect = _timeout()

    with patch.dict(os.environ, _ENV), patch(_SLEEP) as sleeper:
        for _ in range(5):
            with pytest.raises(requests.exceptions.ReadTimeout):
                refresh_or_relogin(smart)

    assert angelone_session._consecutive_relogin_failures == 0
    sleeper.assert_not_called()
    assert "REPEATED RE-AUTHENTICATION" not in capsys.readouterr().out


def test_only_auth_failures_count_toward_the_banner_when_mixed_with_network_noise(capsys):
    _reset_session_state()
    bad_pin = {"status": False, "message": "INVALID MPIN. Attempt 1/5."}
    smart = MagicMock()
    smart.refresh_token = None
    smart.generateSession.side_effect = [
        _timeout(), bad_pin, _timeout(), bad_pin, bad_pin,
    ]

    with patch.dict(os.environ, _ENV), patch(_SLEEP) as sleeper:
        for _ in range(5):
            try:
                refresh_or_relogin(smart)
            except (RuntimeError, requests.exceptions.ReadTimeout):
                pass

    assert angelone_session._consecutive_relogin_failures == 3  # 3 auth failures, 2 network errors ignored
    assert "REPEATED RE-AUTHENTICATION" in capsys.readouterr().out
    assert sleeper.call_count == 3  # backoff only once a real auth failure had occurred


# -- login(): timeout applied, token can't leak ------------------------------------

def test_login_applies_the_http_timeout_and_sanitizes_network_errors():
    token = "eyJhbGciOiJIUzUxMiJ9." + "A" * 60 + "." + "B" * 40
    leaky = requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='apiconnect.angelone.in', port=443): Max retries "
        "exceeded with url: /rest/secure/angelbroking/user/v1/getProfile?"
        f"%7B%22refreshToken%22:%20%22{token}%22%7D (Caused by NameResolutionError())"
    )
    with patch.dict(os.environ, _ENV), \
         patch("broker.angelone_session.SmartConnect") as smart_cls, \
         patch("broker.angelone_session.pyotp"):
        smart_cls.return_value.generateSession.side_effect = leaky
        with pytest.raises(RuntimeError) as excinfo:
            angelone_session.login()

    assert smart_cls.call_args.kwargs["timeout"] == angelone_session._HTTP_TIMEOUT_SECONDS
    assert angelone_session._HTTP_TIMEOUT_SECONDS == 15
    assert token not in str(excinfo.value)
    assert "network error" in str(excinfo.value)
    assert excinfo.value.__suppress_context__ and excinfo.value.__cause__ is None


# -- place_stop_loss: unknown outcome vs definite rejection ---------------------------

def _place(smart, **kw):
    with patch("broker.stoploss_placer.get_tick_size", return_value=Decimal("0.05")), \
         patch("broker.stoploss_placer.time.sleep"), patch(_SLEEP):
        return place_stop_loss(smart, "NIFTY24000CE", "1", "NFO", "CARRYFORWARD",
                               75, 100.0, dry_run=False, **kw)


def test_place_stop_loss_reports_unknown_outcome_and_never_retries_a_network_error(capsys):
    smart = MagicMock()
    smart.placeOrderFullResponse.side_effect = _timeout()

    with pytest.raises(PlacementOutcomeUnknown):
        _place(smart)

    assert smart.placeOrderFullResponse.call_count == 1
    out = capsys.readouterr().out
    assert "STATUS UNKNOWN" in out and "NIFTY24000CE" in out


def test_place_stop_loss_still_retries_a_definite_rejection(capsys):
    smart = MagicMock()
    smart.placeOrderFullResponse.return_value = {
        "status": False, "message": "RMS: insufficient margin", "errorcode": "AB1004", "data": None}

    with pytest.raises(RuntimeError) as excinfo:
        _place(smart)

    assert not isinstance(excinfo.value, PlacementOutcomeUnknown)
    assert smart.placeOrderFullResponse.call_count == 3
    assert "PLACEMENT FAILED" in capsys.readouterr().out


def test_place_stop_loss_succeeds_after_a_transient_rejection():
    smart = MagicMock()
    smart.placeOrderFullResponse.side_effect = [
        {"status": False, "message": "busy", "errorcode": "AB2001", "data": None},
        {"status": True, "data": {"orderid": "250928000123"}},
    ]
    assert _place(smart) == "250928000123"
    assert smart.placeOrderFullResponse.call_count == 2


# -- describe_error ----------------------------------------------------------------

def test_describe_error_redacts_query_strings_and_tokens_but_keeps_the_endpoint():
    token = "eyJhbGciOiJIUzUxMiJ9." + "A" * 60 + "." + "B" * 40
    e = requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='apiconnect.angelone.in', port=443): Max retries "
        "exceeded with url: /rest/secure/angelbroking/user/v1/getProfile?"
        f"%7B%22refreshToken%22:%20%22{token}%22%7D (Caused by NameResolutionError())"
    )
    text = describe_error(e)
    assert token not in text and "eyJ" not in text and "refreshToken" not in text
    assert "ConnectionError" in text and "getProfile" in text  # still says WHAT failed

    stray = describe_error(RuntimeError(f"weird failure carrying {token} inline"))
    assert token not in stray and "eyJ" not in stray
    assert len(describe_error(RuntimeError("x" * 5000))) <= 300
