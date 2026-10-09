"""
tests/test_angelone_session.py
=================================
Regression tests for broker/angelone_session.py's session-persistence
machinery, from the "runs for days without manual re-login" audit:

  - a failed generateToken() (the refresh token itself is dead) falls
    through to a full generateSession() with fresh TOTP, not just when
    the ORIGINAL calling API failed.
  - a TOTP-specific login failure is flagged distinctly (possible clock
    drift), not indistinguishable from any other auth failure.
  - a redundant refresh moments after a successful one is debounced,
    so two near-simultaneous callers don't both re-login.
  - repeated full-relogin failures back off progressively and print an
    unmissable banner, rather than hammering the login endpoint or
    silently retrying forever.

Every test resets the module's shared state (_last_refresh_monotonic,
_consecutive_relogin_failures) at the start, so these are independent
of execution order.
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from broker import angelone_session

_FAKE_ENV = {
    "ANGEL_CLIENT_CODE": "X", "ANGEL_PASSWORD": "Y",
    "ANGEL_TOTP_SECRET": "JBSWY3DPEHPK3PXP",
}


def _reset_module_state():
    angelone_session._last_refresh_monotonic = 0.0
    angelone_session._consecutive_relogin_failures = 0
    angelone_session._session_generation = 0


def test_generateToken_failure_falls_back_to_full_relogin():
    _reset_module_state()
    smart = MagicMock()
    smart.refresh_token = "fake-refresh-token"
    smart.generateToken.side_effect = KeyError("jwtToken")  # dead refresh token
    smart.generateSession.return_value = {"status": True}

    with patch.dict(os.environ, _FAKE_ENV):
        angelone_session.refresh_or_relogin(smart)

    smart.generateToken.assert_called_once()
    smart.generateSession.assert_called_once()
    print("PASS: a failed generateToken() correctly falls back to a full generateSession()")


def test_totp_drift_is_flagged_in_logs(capsys):
    _reset_module_state()
    smart = MagicMock()
    smart.refresh_token = None  # skip straight to full relogin
    smart.generateSession.return_value = {"status": False, "message": "Invalid TOTP"}

    with patch.dict(os.environ, _FAKE_ENV):
        try:
            angelone_session.refresh_or_relogin(smart)
        except RuntimeError:
            pass

    out = capsys.readouterr().out
    assert "POSSIBLE TOTP CLOCK DRIFT" in out, (
        "a TOTP-specific failure must be flagged distinctly so it isn't "
        "misdiagnosed as generic credential failure during unattended operation"
    )
    print("PASS: a TOTP-specific login failure is flagged distinctly as possible clock drift")


def test_non_totp_failure_is_not_mislabeled_as_drift(capsys):
    _reset_module_state()
    smart = MagicMock()
    smart.refresh_token = None
    smart.generateSession.return_value = {"status": False, "message": "INVALID MPIN. Attempt 1/5."}

    with patch.dict(os.environ, _FAKE_ENV):
        try:
            angelone_session.refresh_or_relogin(smart)
        except RuntimeError:
            pass

    out = capsys.readouterr().out
    assert "POSSIBLE TOTP CLOCK DRIFT" not in out
    print("PASS: a non-TOTP failure (bad PIN) is not mislabeled as clock drift")


def test_redundant_refresh_is_debounced():
    _reset_module_state()
    smart = MagicMock()
    smart.refresh_token = "fake-refresh-token"
    smart.generateToken.return_value = {"status": True}

    angelone_session.refresh_or_relogin(smart)
    assert smart.generateToken.call_count == 1

    angelone_session.refresh_or_relogin(smart)  # immediately again
    assert smart.generateToken.call_count == 1, (
        "a second refresh moments after a successful one should be "
        "debounced, not re-attempted -- two near-simultaneous re-logins "
        "for the same account risk one invalidating the other"
    )
    print("PASS: a redundant refresh moments after a successful one is debounced")


def test_repeated_failures_trigger_backoff_and_banner(capsys):
    _reset_module_state()
    smart = MagicMock()
    smart.refresh_token = None  # go straight to full relogin every time
    smart.generateSession.return_value = {"status": False, "message": "Invalid Token"}

    with patch.dict(os.environ, _FAKE_ENV), \
         patch("broker.angelone_session.time.sleep") as mock_sleep:
        for _ in range(3):
            try:
                angelone_session.refresh_or_relogin(smart)
            except RuntimeError:
                pass

    out = capsys.readouterr().out
    assert "REPEATED RE-AUTHENTICATION FAILURES" in out, (
        "3 consecutive failures must print the unmissable retry-storm banner"
    )
    assert mock_sleep.call_count == 2, (
        f"expected backoff sleeps before attempts 2 and 3 (not attempt 1), "
        f"got {mock_sleep.call_count} sleep calls"
    )
    print("PASS: repeated re-login failures trigger backoff and an unmissable banner")


def test_ab1007_invalid_token_triggers_reauth_and_retry():
    """
    Regression test for a live incident: a real safety-net SL-LIMIT
    placement returned {"status": False, "errorcode": "AB1007",
    "message": "Invalid Token"} on all 3 of stoploss_placer.py's
    attempts, unchanged, meaning with_session_retry never re-authenticated.
    AB1007 is now an explicit entry in _AUTH_ERROR_CODES (previously only
    AG8001) -- confirm with_session_retry detects it, calls
    refresh_or_relogin(), and retries the wrapped call with the refreshed
    session, returning the SUCCESSFUL result rather than propagating the
    same AB1007 response unchanged.
    """
    _reset_module_state()
    smart = MagicMock()
    smart.refresh_token = "fake-refresh-token"
    smart.generateToken.return_value = {"status": True}

    responses = [
        {"status": False, "errorcode": "AB1007", "message": "Invalid Token"},
        {"status": True, "data": {"orderid": "250929000123"}},
    ]
    call_count = {"n": 0}

    def place_order():
        call_count["n"] += 1
        return responses[call_count["n"] - 1]

    result = angelone_session.with_session_retry(smart, place_order, idempotent=True)

    smart.generateToken.assert_called_once()  # refresh_or_relogin() was actually invoked
    assert call_count["n"] == 2, "expected exactly one retry after reauth, not 3 unchanged attempts"
    assert result == {"status": True, "data": {"orderid": "250929000123"}}, (
        f"expected the retry's successful response, got {result!r} -- looks like the "
        f"same AB1007 error propagated unchanged"
    )
    print("PASS: AB1007 'Invalid Token' triggers reauth and the retry succeeds "
          "with the new session, instead of repeating unchanged")


def test_ab1007_is_matched_by_errorcode_even_without_the_message_keyword():
    """Belt-and-suspenders check for the actual code change: AB1007 must be
    recognized from the errorcode alone, not only because "invalid token"
    happens to also appear in the message text (Angel One reuses AB1007
    for unrelated failures like a bad login PIN, where the message won't
    contain that phrase)."""
    _reset_module_state()
    smart = MagicMock()
    smart.refresh_token = "fake-refresh-token"
    smart.generateToken.return_value = {"status": True}

    responses = [
        {"status": False, "errorcode": "AB1007", "message": "Something unrelated to tokens"},
        {"status": True, "data": "ok"},
    ]

    result = angelone_session.with_session_retry(
        smart, lambda: responses.pop(0), idempotent=True)

    smart.generateToken.assert_called_once()
    assert result == {"status": True, "data": "ok"}
    print("PASS: AB1007 triggers reauth from the errorcode alone, "
          "independent of the message wording")


class _FakeClock:
    """Deterministic stand-in for time.monotonic() -- advances only when
    told to, so a test can reproduce "~1.5s apart" spacing exactly and
    fast, with no real sleeping."""

    def __init__(self, start: float = 1_000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def test_each_with_session_retry_call_forces_its_own_reauth_attempt(capsys):
    """
    Regression test for a real production incident: stoploss_placer.py's
    3 placement attempts (~1.5s apart) each call with_session_retry
    independently. All 3 returned {"status": False, "errorcode": "AB1007",
    "message": "Invalid Token"} UNCHANGED, and generateToken() was called
    only ONCE total -- the 5s debounce silently skipped re-authenticating
    on attempts 2 and 3 because attempt 1's refresh had "just" succeeded,
    even though the token it produced never actually worked.

    Reproduces the exact timing (a fake clock advances ~1.5s between each
    call, well inside the 5s debounce window) and asserts a real
    generateToken() attempt happens before attempt 2 and attempt 3 too,
    not just attempt 1.
    """
    _reset_module_state()
    smart = MagicMock()
    smart.refresh_token = "fake-refresh-token"
    smart.generateToken.return_value = {"status": True}
    AB1007 = {"status": False, "errorcode": "AB1007", "message": "Invalid Token"}

    clock = _FakeClock()
    with patch("broker.angelone_session.time.monotonic", clock):
        for attempt in range(1, 4):
            # Each attempt is its own fresh call, exactly like
            # stoploss_placer.py's retry loop -- the account is still
            # broken every time (AB1007 persists even after a refresh).
            result = angelone_session.with_session_retry(smart, lambda: AB1007, idempotent=True)
            assert result == AB1007, f"attempt {attempt}: expected the (still-failing) response back"
            clock.advance(1.5)

    assert smart.generateToken.call_count == 3, (
        f"expected a real reauth attempt before EACH of the 3 placement "
        f"attempts, got {smart.generateToken.call_count} -- the debounce is "
        f"starving attempt 2 and/or 3 of a real refresh"
    )
    out = capsys.readouterr().out
    assert "forcing a real refresh despite" in out  # attempts 2 & 3 explicitly bypassed the debounce
    print("PASS: each of 3 spaced with_session_retry calls forces its own "
          "reauth attempt instead of being silently debounced")


def test_forced_reauth_lets_a_later_attempt_succeed_once_the_session_is_fixed():
    """
    Companion to the above: once the account is genuinely fine again,
    the NEXT attempt (still well within the plain 5s debounce window)
    must actually succeed rather than repeating the same stale-token
    failure the debounce would otherwise have caused.
    """
    _reset_module_state()
    smart = MagicMock()
    smart.refresh_token = "fake-refresh-token"
    smart.generateToken.return_value = {"status": True}
    AB1007 = {"status": False, "errorcode": "AB1007", "message": "Invalid Token"}
    OK = {"status": True, "data": {"orderid": "250929000456"}}

    clock = _FakeClock()
    with patch("broker.angelone_session.time.monotonic", clock):
        # Attempt 1: fails, refreshes (generation 0 -> 1), retry still
        # fails -- the account was genuinely still broken at this point.
        attempt_1 = angelone_session.with_session_retry(smart, lambda: AB1007, idempotent=True)
        assert attempt_1 == AB1007

        clock.advance(1.5)  # well within the 5s debounce window

        # Attempt 2: the account has now been fixed on Angel One's side.
        # Its own first call still sees the (old, cached-until-now) failure,
        # but the forced reauth this time actually fixes things.
        responses = iter([AB1007, OK])
        attempt_2 = angelone_session.with_session_retry(
            smart, lambda: next(responses), idempotent=True)

    assert attempt_2 == OK, (
        f"expected attempt 2 to succeed after being forced through a real "
        f"reauth, got {attempt_2!r} -- looks like it was debounced into "
        f"repeating the stale failure instead"
    )
    assert smart.generateToken.call_count == 2  # one real attempt per placement attempt
    print("PASS: once the session is genuinely fixed, a later attempt within "
          "the debounce window still succeeds instead of repeating the failure")


def test_debounce_still_protects_two_concurrent_callers_with_a_fresh_failure():
    """
    The retry-storm case the debounce exists for must still work: two
    DIFFERENT callers hitting a FRESH, unrelated auth failure at the same
    session generation -- only the first should actually refresh; the
    second, evaluated after the lock is free, sees the generation has
    already moved and skips, exactly as before this fix.
    """
    _reset_module_state()
    smart = MagicMock()
    smart.refresh_token = "fake-refresh-token"
    smart.generateToken.return_value = {"status": True}

    generation_seen_by_both_callers = angelone_session.get_session_generation()  # both capture 0

    angelone_session.refresh_or_relogin(
        smart, known_stale_generation=generation_seen_by_both_callers)
    angelone_session.refresh_or_relogin(
        smart, known_stale_generation=generation_seen_by_both_callers)  # "concurrent" 2nd caller

    assert smart.generateToken.call_count == 1, (
        "two callers observing the SAME fresh failure must still result in "
        "exactly one real refresh -- the retry-storm protection must be intact"
    )
    print("PASS: two callers sharing one fresh failure still only trigger one "
          "real refresh (retry-storm protection unaffected)")


if __name__ == "__main__":
    import pytest as _pytest_marker  # noqa: F401 -- capsys tests need pytest to run
    print("Run these via `python -m pytest tests/test_angelone_session.py -v` "
          "(some use the capsys fixture, which needs pytest).")
    test_generateToken_failure_falls_back_to_full_relogin()
    test_redundant_refresh_is_debounced()
    test_ab1007_invalid_token_triggers_reauth_and_retry()
    test_ab1007_is_matched_by_errorcode_even_without_the_message_keyword()
    test_forced_reauth_lets_a_later_attempt_succeed_once_the_session_is_fixed()
    test_debounce_still_protects_two_concurrent_callers_with_a_fresh_failure()
    print("\nNon-capsys checks passed.")
