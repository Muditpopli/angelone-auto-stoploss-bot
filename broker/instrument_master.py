"""
broker/instrument_master.py
=============================
Fetches and caches Angel One's public instrument/scrip master
(OpenAPIScripMaster.json) so stoploss_placer.py can look up the REAL
tick size for whatever token it's placing an SL-LIMIT order against,
instead of assuming a constant. This matters once MCX commodities are
in scope -- crude oil, gold, silver, natural gas, copper etc. all have
different tick sizes, and even NFO/BFO index options being 0.05 is a
convention this file lets us actually confirm per instrument rather
than hope holds.

Verified live against the real file before writing this (33MB, ~142k
rows): every price-shaped field in it (strike, tick_size) is expressed
in PAISE, not rupees -- e.g. the real NIFTY29SEP2623150CE contract's
tick_size reads "5.000000" (5 paise = Rs 0.05, matching the documented
real NSE index-option tick) and its strike reads "2315000.000000"
(paise for Rs 23150, matching the actual ATM strike at the time this
was checked). This file divides tick_size by 100 accordingly -- getting
this wrong would make every trigger/limit price 100x too large.

This is a large, public, UNAUTHENTICATED file (no Angel One login
needed to fetch it -- it's a plain HTTPS GET) that changes rarely.
Cached in memory for the life of the process, and on disk for the rest
of the calendar day, so a process restart mid-day doesn't force a
30+ MB re-download.
"""
from __future__ import annotations
import datetime
import json
import os
from decimal import Decimal, InvalidOperation
from typing import Dict, Optional

import requests

from .network_errors import describe_error

_SCRIP_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
_CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".cache")
FALLBACK_TICK_SIZE = Decimal("0.05")  # last resort only -- see get_tick_size()

_memory_cache: Optional[Dict[str, Decimal]] = None  # token -> tick size in rupees


def _log(reason: str, outcome: str) -> None:
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] [TICK-SIZE] {reason} -> {outcome}")


def _print_fallback_banner(token: str, reason: str) -> None:
    print("\n" + "?" * 60)
    print("  TICK-SIZE LOOKUP FAILED -- USING FALLBACK, VERIFY MANUALLY")
    print(f"  Token   : {token}")
    print(f"  Reason  : {reason}")
    print(f"  Falling back to a hardcoded {FALLBACK_TICK_SIZE} tick size.")
    print("  If this instrument's real tick size is different, every")
    print("  SL-LIMIT trigger/limit price computed for it will be WRONG,")
    print("  and may be rejected by the exchange (or accepted at an")
    print("  unintended price). Verify the real tick size for this")
    print("  instrument before trusting an order placed under it.")
    print("?" * 60 + "\n")


def _cache_file_for_today() -> str:
    return os.path.join(_CACHE_DIR, f"scrip_master_{datetime.date.today().isoformat()}.json")


def _prune_old_cache_files(keep_path: str) -> None:
    try:
        for name in os.listdir(_CACHE_DIR):
            path = os.path.join(_CACHE_DIR, name)
            if name.startswith("scrip_master_") and path != keep_path:
                os.remove(path)
    except OSError:
        pass  # best-effort only -- a stray old cache file is harmless


def _load_tick_sizes_by_token() -> Dict[str, Decimal]:
    cache_path = _cache_file_for_today()
    if os.path.exists(cache_path):
        with open(cache_path, "r", encoding="utf-8") as f:
            rows = json.load(f)
        _log("Loaded scrip master", f"from today's on-disk cache ({cache_path})")
    else:
        # (connect, read): a TCP/TLS handshake stall must not block an SL
        # placement for a minute, but a 33MB body legitimately needs a
        # generous per-read window.
        resp = requests.get(_SCRIP_MASTER_URL, timeout=(10, 60))
        resp.raise_for_status()
        rows = resp.json()
        os.makedirs(_CACHE_DIR, exist_ok=True)
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(rows, f)
        _prune_old_cache_files(cache_path)
        _log("Downloaded scrip master", f"{len(rows)} instruments, cached to {cache_path}")

    by_token: Dict[str, Decimal] = {}
    for row in rows:
        token = row.get("token")
        raw_tick = row.get("tick_size")
        if not token or not raw_tick:
            continue
        try:
            tick_paise = Decimal(raw_tick)
        except InvalidOperation:
            continue
        if tick_paise <= 0:
            continue  # indices etc. report "0.000000" -- not a tradable tick
        by_token[token] = tick_paise / Decimal(100)
    return by_token


def get_tick_size(token: str) -> Decimal:
    """
    Returns the real tick size (in rupees) for this instrument token,
    looked up from Angel One's own scrip master (cached -- see module
    docstring). Falls back to 0.05 with a LOUD warning if the master
    can't be fetched at all, or the token isn't found in it -- never
    silently guesses, since an SL-LIMIT order at the wrong tick gets
    rejected by the exchange (or worse, accepted at a materially wrong
    price if some future SDK version is more lenient).
    """
    global _memory_cache
    if _memory_cache is None:
        try:
            _memory_cache = _load_tick_sizes_by_token()
        except Exception as e:
            # Deliberately NOT cached. This used to store an empty dict, so
            # a single transient network blip at first use silently forced
            # the 0.05 fallback onto EVERY token for the rest of the
            # process's life. Leaving the cache unset means the next call
            # retries the fetch (this call still gets the loud fallback).
            _print_fallback_banner(
                token, f"could not fetch/parse the scrip master ({describe_error(e)})")
            return FALLBACK_TICK_SIZE

    tick = _memory_cache.get(token)
    if tick is None:
        _print_fallback_banner(token, "token not found in Angel One's scrip master")
        return FALLBACK_TICK_SIZE
    return tick
