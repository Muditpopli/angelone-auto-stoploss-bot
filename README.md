# Angel One Auto Stop-Loss Bot

Automated stop-loss guard for Angel One SmartAPI — auto square-off of a
single option/futures leg at a configurable mark-to-market loss, using a
`STOPLOSS_LIMIT` order with a price buffer. NSE/NFO, BSE/BFO, MCX.

![Python 3.8+](https://img.shields.io/badge/python-3.8%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

Originally built for a freelance client engagement; this is a
generalised, credential-free version that works for any Angel One
account.

## The problem

Retail F&O traders blow up accounts not because their thesis was wrong,
but because they don't honour their own stop-loss: a position that
should have been cut at -10% is left running "just a bit longer" and
turns into a much bigger loss. A bot that watches the position and acts
mechanically, every single time, removes that failure mode — it doesn't
hesitate, doesn't hope, and doesn't get distracted.

## See it work (no credentials needed)

```bash
pip install -r requirements.txt
python demo.py
```

`demo.py` reuses the real trigger logic (`MockBroker`, `Position.pnl_pct`,
the same threshold check) against a scripted price path, so the numbers
below are genuine, not staged:

```
=== Angel One Auto Square-Off — Live Demo ===

[Tick  1] NIFTY29SEP2623150CE    price=  151.50  pnl%=  +1.00%
[Tick  2] NIFTY29SEP2623150CE    price=  153.30  pnl%=  +2.20%
[Tick  3] NIFTY29SEP2623150CE    price=  155.25  pnl%=  +3.50%
[Tick  4] NIFTY29SEP2623150CE    price=  157.50  pnl%=  +5.00%
[Tick  5] NIFTY29SEP2623150CE    price=  159.75  pnl%=  +6.50%
[Tick  6] NIFTY29SEP2623150CE    price=  162.00  pnl%=  +8.00%
[Tick  7] NIFTY29SEP2623150CE    price=  149.25  pnl%=  -0.50%
[Tick  8] NIFTY29SEP2623150CE    price=  146.70  pnl%=  -2.20%
[Tick  9] NIFTY29SEP2623150CE    price=  144.00  pnl%=  -4.00%
[Tick 10] NIFTY29SEP2623150CE    price=  141.00  pnl%=  -6.00%
[Tick 11] NIFTY29SEP2623150CE    price=  138.00  pnl%=  -8.00%
[Tick 12] NIFTY29SEP2623150CE    price=  136.80  pnl%=  -8.80%
[Tick 13] NIFTY29SEP2623150CE    price=  136.20  pnl%=  -9.20%
[Tick 14] NIFTY29SEP2623150CE    price=  135.45  pnl%=  -9.70%
[Tick 15] NIFTY29SEP2623150CE    price=  132.30  pnl%= -11.80%

****************************************************
  POSITION EXITED
  NIFTY29SEP2623150CE
  Exit price : 132.30
  Final loss : -11.80%
  Order id   : MOCK000001
****************************************************

--- Summary ---
  Entry price       : 150.00
  Peak price/profit : 162.00 (+8.00%)
  Exit price        : 132.30
  Final loss        : -11.80%
  Ticks to exit     : 15
```

(the exact symbol depends on the date you run it — NIFTY on Mon/Tue/Fri,
SENSEX on Wed/Thu; see "Supported segments" below.) `demo.py` is
presentation only, not a test — see "Testing" for the real proof.

## What it does

Watches your open option/futures positions on Angel One and, the
instant a single leg's mark-to-market loss reaches a configured
threshold (default **-10%**), places a `STOPLOSS_LIMIT` order to exit
*that leg only* — every other open position is left untouched. No
dashboards, no SMS/email alerts: it watches, it acts, and it prints what
it did.

## How it works

```
 BUY fills  ──▶  fetch/track position  ──▶  compute MTM loss %
                                                   │
                                     loss >= threshold (-10%)?
                                                   │ yes
                                                   ▼
                              place STOPLOSS_LIMIT SELL
                              (trigger price + a tick-rounded buffer)
                                                   │
                                                   ▼
                                  confirm + log the order id
```

Two different ways to drive the left-hand side of that flow — see
"Which mode to use" below.

## Which mode to use

| | `main.py` (polling) | `main_autosl.py` (event-driven) |
|---|---|---|
| **Trigger mechanism** | Polls `position()` every N seconds, computes MTM% itself | Listens to Angel One's order-update websocket; reacts to a BUY fill instantly |
| **Order placed** | `MARKET` (fires the moment the poll loop notices a breach) | `STOPLOSS_LIMIT` (sits on the exchange's own order book from the moment of fill) |
| **Still protects if this process restarts/is briefly down?** | No — nothing watches while it's down | Yes, within limits — the SL-LIMIT order already rests on Angel One's book once placed; a periodic safety-net poll also re-protects anything the websocket missed |
| **Simplicity** | Simpler: one loop, one broker call pattern | More moving parts: a websocket listener thread + a safety-net poll thread |
| **Good fit for** | A small number of positions, or when you want the simplest possible mental model | Positions you open through the day and want protected the instant they fill, with no polling delay |

Both are independent, fully-featured entry points — pick one, or run
both (they don't interfere with each other).

## Design decisions

**STOPLOSS_LIMIT, not STOPLOSS_MARKET.** Per client request, this uses
a LIMIT order (not a market order): the client is wary of market orders
and wants a bounded exit price, and explicitly accepts that in a rare
gap-through scenario (price jumps past both the trigger and the limit
before the order can fill) they will exit manually rather than have the
bot force a market fill. A market order can fill at an unpredictable,
sometimes much worse price during a fast move or on an illiquid option
strike — freak-fill risk a limit order avoids by construction, at the
cost of the (rare) unfilled-order scenario above. That trade-off was
made explicitly, with the client's sign-off, not silently defaulted.

**A price buffer between trigger and limit.** The gap between the
trigger and limit price is `SL_LIMIT_BUFFER = Decimal("0.5")` (in
`broker/stoploss_placer.py`, instrument price units) by default:
`limit_price = trigger_price - SL_LIMIT_BUFFER`, giving the sell order
room to still fill as price continues falling through the trigger,
rather than sitting exactly at (or above) a price the market has
already passed. Configurable via `config.yaml`'s `sl_limit_buffer`.

**Never auto-cancels or replaces a stuck order.** If a position has a
pending SL-LIMIT order but its P&L% has drifted well past the threshold
anyway (the order likely gapped through), `main_autosl.py` prints an
unmissable warning and does nothing else — no automatic market-order
fallback, no cancel. Manual orders and modifications are always
respected too: "is this position protected?" means *any* open SELL
order resting against it, the bot's own or your own, of any order type
— this bot has no `cancelOrder()` call anywhere in it.

**Tick size looked up per instrument, not assumed.** NSE/BSE index
options use a 0.05 tick, but MCX commodities each have their own —
`broker/instrument_master.py` downloads Angel One's public instrument
master and looks up the real tick size for whatever token an order is
being placed against, falling back to 0.05 with a loud warning only if
the lookup fails.

**-10% of what?** Your brief said "the instant its mark-to-market loss
reaches -10%" but didn't specify -10% *of what* — margin blocked,
premium paid, or notional. There's no single industry-standard
convention here, so this bot uses the most common retail-algo convention
and makes it explicit rather than silently guessing: **-10% of the
position's entry value** (`avg_price × quantity`). This lives in one
place — `Position.pnl_pct` in `broker/base.py`.

## Supported segments

NSE, NFO, BSE, BFO, MCX — this reacts to a fill on any exchange/segment
Angel One supports, at any time the relevant exchange is open; there's
no schedule or instrument restriction baked into the production path.
`config.yaml`'s `enabled_exchanges` can restrict this to a subset if you
only want it running against, say, NSE/NFO. (The demo/test data in
`mock_broker.py`/`paper_broker.py` only ever uses NIFTY/SENSEX index
options — that's for realistic-looking demos, not a production
constraint; see `broker/expiry_calendar.py`.)

## Setup

Full walkthrough — getting an API key, generating the TOTP secret,
running unattended for days — is in **[SETUP.md](SETUP.md)**. Quick
version:

```bash
git clone <this repo>
cd angelone-auto-stoploss-bot
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS/Linux

pip install -r requirements.txt       # or requirements-dev.txt to also get pytest
cp .env.example .env                  # then fill in your real credentials
cp config.example.yaml config.yaml    # optional -- tune thresholds/intervals
```

Three Windows `.bat` double-click wrappers are also included as an
optional convenience (`run_healthcheck.bat`, `run_bot_paper.bat`,
`run_bot.bat`) — thin wrappers around the same commands below; not
needed on macOS/Linux.

## Configuration

Two separate mechanisms, deliberately kept apart:

- **Secrets** (`ANGEL_API_KEY`, `ANGEL_CLIENT_CODE`, `ANGEL_PASSWORD`,
  `ANGEL_TOTP_SECRET`) go in `.env` only, loaded via `python-dotenv`.
  Copy `.env.example` and fill in real values; `.env` is gitignored.
- **Tunables** (loss threshold, SL-LIMIT buffer, poll intervals, enabled
  exchanges) go in `config.yaml`, loaded via `config_loader.py`. Copy
  `config.example.yaml`; `config.yaml` is gitignored too.

Precedence for every tunable: a CLI flag (e.g. `--stop-loss -7.5`) wins
over `config.yaml`, which wins over the built-in default. Omit
`config.yaml` entirely and everything just uses the built-in defaults
(same behavior as before this config system existed).

## Usage

```bash
# Safe, no credentials: dummy positions, random-walk prices
python main.py --mode mock --iterations 20

# Dry-run: real login + real live prices, fake position, zero real orders
python main.py --mode paper --poll-interval 2 --iterations 15
python main_autosl.py --mode paper

# Real account, real orders
python main.py --mode live
python main_autosl.py --mode live

# Tweak the threshold (either entry point)
python main_autosl.py --mode live --stop-loss -7.5

# Use a config file
python main_autosl.py --mode live --config config.yaml
```

`--mode paper` is this project's dry-run: it logs exactly what order it
*would* place (symbol, trigger/limit price, quantity) and returns a fake
order id, without ever calling `placeOrder()`. Read SETUP.md before your
first `--mode live` run.

`healthcheck.py` is a standalone, read-only connectivity check — safe to
run anytime, confirms login/data-feed/positions are all working without
touching any order:

```bash
python healthcheck.py
```

## Testing

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

All tests are pure-logic unit tests — mocked brokers/sessions, no
network calls, no credentials needed. They cover: the loss %/trigger
decision (deterministic scripted price paths, `tests/test_monitor.py`),
limit-price-with-buffer tick-rounding (`tests/test_stoploss_placer.py`),
the config loader (`tests/test_config_loader.py`), session
refresh/retry logic, the order-update fill parser, and the safety-net's
duplicate-order protections.

## Safety & limitations

Honest, not hedged:

- **Gap-through-trigger risk.** If price jumps past both the trigger and
  the limit price before the SL-LIMIT order can fill (a fast move or
  thin liquidity), the order sits unfilled and the position needs a
  **manual exit**. A watchdog detects this and warns loudly; it does not
  auto-escalate to a market order.
- **Unfilled limit orders are possible by design** — see "Design
  decisions" above for why this trade-off was chosen over a market order.
- **Single-leg logic — no portfolio/spread awareness.** Each leg is
  evaluated and exited independently, by design (see "What it does"):
  this bot has no concept of a multi-leg position (e.g. a hedged
  spread). Exiting one leg of a hedge on its own loss threshold can
  leave the other leg suddenly unhedged — that's a consequence of the
  single-leg design, not something this bot accounts for.
- **Depends on broker API uptime.** A transient network error is
  retried once for read-only calls and otherwise logged and skipped
  (never silently swallowed, never crashes the process); a sustained
  outage means the bot simply can't act until the API is back.
- **Rate limits.** Angel One's REST endpoints are rate-limited; the
  polling mode's default 1-second interval is a safe starting point for
  a handful of positions, but running many simultaneous legs or setting
  the interval too low can trigger 429 (rate-limit) errors — widen
  `--poll-interval`/`poll_interval` rather than hammering the API if you
  see these; a missed tick still gets caught on the next poll.
- **Websocket reconnect, as implemented:** the event-driven mode's
  order-update listener reconnects automatically on any disconnect, with
  exponential backoff (2s up to 60s). A fill that happens while the
  websocket is down (or mid-reconnect) is still caught by the periodic
  safety-net poll, within its configured interval (default 10s) — not
  instantly. A longer-lived "half-open" connection (no clean
  disconnect signal) can take longer than that to notice at all; the
  safety-net poll is the backstop either way.
- **Not tested across every exchange/account condition.** Live-tested
  on a funded account; field names and message schemas are
  cross-checked against the broker SDK's own source where possible, but
  this hasn't been exercised against every combination of segment,
  product type, and account configuration that exists.

## Disclaimer

For educational purposes. Not financial advice. Trading involves risk
of loss. Use at your own risk.

## Notes on the SmartAPI field names used here

`angelone_broker.py` reads `netqty`, `avgnetprice`, `tradingsymbol`,
`symboltoken`, `exchange`, and `producttype` off the `position()`
response, and always re-fetches LTP via `ltpData()` rather than trusting
the (often stale) `ltp` field inside the position response itself.
Angel One has been known to adjust field names/casing across SDK
versions — if `get_positions()` throws a `KeyError` on your account,
print `resp["data"][0]` once to see your account's actual field names
and adjust the two or three lines that read them; the rest of the file
does not need to change.

`variety="STOPLOSS"` with `ordertype="STOPLOSS_LIMIT"` (plus a
`triggerprice` and a `price`) is confirmed against Angel One's own
official `smartapi-java` SDK source, not guessed — `smartapi-python`'s
`placeOrder()` doesn't validate this combination client-side, so getting
it right here is on this project, not the SDK.

## Project layout

```
main.py                    <- polling entry point (mock / paper / live)
main_autosl.py              <- event-driven entry point (paper / live)
monitor.py                  <- polling loop: positions -> P&L% -> square off
config_loader.py             <- config.yaml loader (CLI > config > default)
healthcheck.py                <- standalone, read-only connectivity check
demo.py                        <- scripted showcase run (fake data, not a test)
run_healthcheck.bat             <- optional Windows: double-click wrapper
run_bot_paper.bat                <- optional Windows: double-click wrapper
run_bot.bat                       <- optional Windows: double-click wrapper
broker/
  base.py                   <- Position dataclass + Broker interface
  expiry_calendar.py         <- which index (NIFTY/SENSEX) demo data uses today
  mock_broker.py             <- fake broker, long-only dummy legs, for demos/tests
  paper_broker.py            <- hybrid broker: real prices, fake position, zero orders
  angelone_session.py        <- login, session refresh/retry, credential-safe logging
  angelone_broker.py         <- real broker: wraps the official smartapi-python SDK
  network_errors.py          <- transient-network classification + safe error logging
  order_update_listener.py   <- event-driven fill detection via the order-update websocket
  stoploss_placer.py         <- places the STOPLOSS_LIMIT order (trigger + buffer)
  instrument_master.py       <- per-instrument tick size lookup
tests/
  test_monitor.py            <- deterministic acceptance test
  test_stoploss_placer.py    <- trigger/limit-price rounding
  test_config_loader.py      <- config precedence and validation
  ...                        <- session, order-update, network-resilience, safety-net tests
```
