#!/usr/bin/env python3
"""
healthcheck.py
===============
Read-only connectivity check: confirms Angel One login works, the live
data feed is flowing, and open positions are visible. Places NO orders
and touches NO positions -- safe to run anytime, including mid-trading.

    python healthcheck.py
"""
from __future__ import annotations
import datetime
import sys
import time

from dotenv import load_dotenv
load_dotenv()

from broker.angelone_session import login, fetch_ltp
from broker.angelone_broker import AngelOneBroker
from broker.expiry_calendar import nearest_expiry_index
from broker.network_errors import describe_error

# Windows consoles often default to a legacy code page that can't
# encode the checkmarks below; reconfigure stdout so this never crashes.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

# NIFTY/SENSEX index-spot instruments, used only to prove the live data
# feed is flowing -- never traded.
_INDEX_SPOT = {
    "NIFTY": ("NSE", "Nifty 50", "99926000"),
    "SENSEX": ("BSE", "SENSEX", "99919000"),
}


def main() -> int:
    start = time.perf_counter()

    try:
        smart = login()
        print("✅ Login successful")
    except Exception as e:
        print(f"❌ ALL SYSTEMS DOWN -- login failed: {describe_error(e)}\n"
              "   Check ANGEL_API_KEY/CLIENT_CODE/PASSWORD/TOTP_SECRET in "
              ".env, TOTP clock drift, or your network connection.")
        return 1

    try:
        profile = (smart.getProfile(smart.refresh_token) or {}).get("data") or {}
        print(f"✅ Account: {profile.get('name', '?')} "
              f"({profile.get('clientcode', '?')})")
    except Exception as e:
        print(f"❌ ALL SYSTEMS DOWN -- profile lookup failed: {describe_error(e)}")
        return 1

    index = nearest_expiry_index(datetime.date.today())
    exchange, symbol, token = _INDEX_SPOT[index]
    try:
        ltp = fetch_ltp(smart, exchange, symbol, token)
        ts = datetime.datetime.now().strftime("%H:%M:%S")
        print(f"✅ Live feed: {index} spot = {ltp} (as of {ts})")
    except Exception as e:
        print(f"❌ ALL SYSTEMS DOWN -- live price feed failed: {describe_error(e)}")
        return 1

    try:
        n = len(AngelOneBroker(smart=smart).get_positions())
        print(f"✅ {n} open position(s) currently being watched" if n
              else "✅ 0 open positions")
    except Exception as e:
        print(f"❌ ALL SYSTEMS DOWN -- fetching open positions failed: {describe_error(e)}")
        return 1

    elapsed = time.perf_counter() - start
    print(f"\nTotal round-trip time: {elapsed:.2f}s")
    print(f"✅ ALL SYSTEMS GO -- API connected, data feed live, "
          f"{n} position(s) visible")
    return 0


if __name__ == "__main__":
    sys.exit(main())
