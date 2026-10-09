"""
broker/network_errors.py
==========================
What counts as transient network noise -- as opposed to an auth failure or
a real bug -- and how to log it safely.

Why this exists: the long-running loops (monitor.py's poll tick,
main_autosl.py's safety-net cycle) originally caught only RuntimeError.
smartapi-python re-raises whatever `requests` raises (see
SmartApi/smartConnect.py's `_request()`), so a plain read timeout to
apiconnect.angelone.in escaped every handler and killed the process.
Verified from the installed libraries, the exceptions that can come out
of an SDK call are:
  - requests.exceptions.RequestException and subclasses (ReadTimeout,
    ConnectTimeout, ConnectionError, SSLError, ChunkedEncodingError, ...).
    It subclasses OSError.
  - socket.gaierror (raw DNS failure). It is an OSError but NOT a
    ConnectionError, so a hand-picked "ConnectionError/TimeoutError"
    list would miss it.
  - urllib3.exceptions.HTTPError, if one ever escapes requests' wrapping.
  - the SDK's own DataException ("couldn't parse the response", e.g. a
    gateway's HTML error page) and NetworkException. Neither is an
    OSError or a RuntimeError.
TokenException is deliberately NOT here -- that is an auth failure, handled
by angelone_session.with_session_retry, and must never be mistaken for
network noise.

Log messages built from these exceptions go through describe_error():
str() of a requests exception embeds the full request URL, and the SDK
sends some secrets in GET query strings (getProfile's refreshToken), so a
raw `{e}` can print a live refresh token. Verified by reproducing the
SDK's exact GET call against an unresolvable host.
"""
from __future__ import annotations
import datetime
import http.client
import re

import requests
import urllib3

try:
    from SmartApi.smartExceptions import DataException, NetworkException
    _SDK_TRANSIENT = (DataException, NetworkException)
except ImportError:  # pragma: no cover
    _SDK_TRANSIENT = ()

TRANSIENT_NETWORK_ERRORS = (
    requests.exceptions.RequestException,
    urllib3.exceptions.HTTPError,
    http.client.HTTPException,
    OSError,
) + _SDK_TRANSIENT

_QUERY_STRING = re.compile(r"\?\S*")
# JWT-ish / base64url blobs. "/" is deliberately excluded so URL *paths*
# (which say which endpoint failed) survive redaction.
_TOKEN_LIKE = re.compile(r"[A-Za-z0-9_\-\.=+]{40,}")


def describe_error(e: BaseException, limit: int = 300) -> str:
    """One-line, credential-safe, length-capped description of an exception:
    type name, message with URL query strings and token-like blobs redacted."""
    text = f"{type(e).__name__}: {e}"
    text = _QUERY_STRING.sub("?<redacted>", text)
    text = _TOKEN_LIKE.sub("<redacted>", text)
    return " ".join(text.split())[:limit]


def log_net_event(reason: str, outcome: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [NET] {reason} -> {outcome}")
