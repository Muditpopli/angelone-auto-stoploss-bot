# Setup Guide

## 1. Get Angel One API credentials

1. Go to https://smartapi.angelbroking.com and log in with your Angel
   One account.
2. Create a new app under "My Apps" → note down the **API Key**.
3. Your **Client Code** is your existing Angel One login id (e.g. `A123456`).
4. Your **Password** here is your 4-digit trading PIN, not your website login password.
5. Enable TOTP-based 2FA on your Angel One account (Profile → Settings
   → Enable 2FA) if you haven't already, and save the **TOTP secret**
   shown at setup time (a long base32 string, looks like
   `JBSWY3DPEHPK3PXP`) — you scan a QR code into an authenticator app,
   but the *secret behind that QR code* is what this bot needs, not a
   rotating 6-digit code.

## 2. Install dependencies

```bash
cd angelone-auto-stoploss-bot
python -m venv venv

# Windows:
venv\Scripts\activate
# macOS/Linux:
source venv/bin/activate

pip install -r requirements.txt
# or, to also get pytest for running the test suite:
pip install -r requirements-dev.txt
```

## 3. Set credentials (never commit these)

Copy `.env.example` to `.env` and fill in your real values:

```bash
cp .env.example .env
```

```
ANGEL_API_KEY=your_api_key_here
ANGEL_CLIENT_CODE=A123456
ANGEL_PASSWORD=your_4_digit_pin
ANGEL_TOTP_SECRET=JBSWY3DPEHPK3PXP
```

`.env` is already gitignored — **never** put real credentials directly
in any `.py` file or commit `.env` to git. `load_dotenv()` picks it up
automatically; every entry point already calls it at startup.

**Optional: tune thresholds via config.yaml.** Copy
`config.example.yaml` to `config.yaml` to change the loss threshold,
SL-LIMIT buffer, poll intervals, or which exchanges the bot acts on,
without touching code. See README.md "Configuration". Skip this step
entirely to just use the built-in defaults.

## 4. Run it

Demo first, always:
```bash
python main.py --mode mock --iterations 20
```

Then live, once you're confident:
```bash
python main.py --mode live
```
Stop any time with `Ctrl+C`.

## 5. Running on a lightweight cloud instance (optional)

If you'd rather this run 24/7 without your laptop staying on during
market hours:

1. Spin up the smallest instance on any provider (a 1 vCPU / 1GB RAM
   box is plenty — this script does almost no computation) — e.g. an
   AWS `t3.micro`, a DigitalOcean/Linode/Vultr $4-6/month droplet, or
   Oracle Cloud's free tier.
2. `ssh` in, install Python 3.10+, `git clone` (or `scp`) this folder over.
3. Repeat steps 2-3 above on the server.
4. Run it under a process supervisor so it restarts if it crashes or
   the box reboots — simplest option, `tmux`:
   ```bash
   tmux new -s squareoff
   python main.py --mode live
   # Ctrl+B then D to detach; `tmux attach -t squareoff` to check on it later
   ```
   For a more robust setup, use `systemd` or `pm2` instead of `tmux` —
   ask if you'd like a ready-made service file for either.
5. Make sure the server's clock is correct (`timedatectl` on Linux) —
   TOTP codes are time-based and drift causes login failures.

## 6. Windows Task Scheduler (optional, alternative to keeping a terminal open)

If you'd rather not use a cloud box: Task Scheduler → Create Task →
Trigger "At log on" → Action: run `python.exe` with argument
`C:\path\to\main.py --mode live`, "Start in" set to the project folder.
Note this still requires the PC to be on and awake through market hours.

## 7. Running unattended for days at a time

Angel One's own guidance is that sessions are expected to be re-logged
into "every day" — tokens are short-lived by design. This bot handles
that automatically now, so a process left running across multiple
days doesn't need anyone to type in a fresh TOTP code partway through:

- **Every real API call** (`position()`, `ltpData()`, `placeOrder()`,
  `orderBook()`) goes through `broker/angelone_session.py`'s
  `with_session_retry()`. If Angel One reports the session as expired
  or invalid, it automatically refreshes the token — first trying a
  lightweight `generateToken()` refresh (no TOTP needed), and only
  falling back to a full fresh login (new TOTP code, computed from
  `ANGEL_TOTP_SECRET` in `.env`, same as the very first login) if that
  lightweight refresh also fails — then retries the call once and
  continues. None of this needs a human.
- **`main_autosl.py`'s order-update websocket** does the same on
  disconnect: it refreshes the session before reconnecting, rather than
  blindly retrying with tokens that may have gone stale.
- Every re-authentication attempt is logged with a timestamp, what
  triggered it, and whether it succeeded — look for `[AUTH]` lines in
  the console/log file. This is expected to happen roughly once a day
  under normal conditions; if you see it firing every few minutes,
  something else is wrong (clock drift, network flakiness, a revoked
  API key) and it's worth investigating rather than trusting the
  auto-recovery to keep masking it indefinitely.

**Network hiccups are handled separately from auth failures.** A read
timeout, connection reset, DNS blip or gateway error page is not an auth
problem and must never kill the process (an earlier version let a plain
7-second SSL-handshake timeout crash the whole bot). Every request now
has a 15-second timeout (the SDK's default is 7), and a network error is
logged as a `[NET]` line, not `[AUTH]`:

- **Read-only calls** (positions, order book, quotes) are retried once
  automatically. The poll loops otherwise just skip that cycle and try
  again on the next one.
- **Orders are deliberately NOT auto-retried after a timeout.** A timeout
  means the request may already have reached the exchange, so a retry
  could place a duplicate — and a duplicate SELL stop-loss or square-off
  can turn a long position into an accidental short. Instead you'll see a
  `SL-LIMIT PLACEMENT STATUS UNKNOWN` banner, and the safety-net poll
  (default every 10s) checks the real order book and places a stop-loss
  only if none exists.
- Network errors never count toward the "repeated re-authentication
  failures" banner or its backoff — that banner is reserved for genuine
  login problems (locked account, changed PIN, revoked key).
- The order-update websocket reconnects forever with a 2s → 60s backoff.
  Watch for `[WS] ... connected` lines; a `[WS] ... reconnecting` streak
  means the network is down, not that the bot has stopped.

**This auto-refresh handles session expiry, not a process crash.** If
the Python process itself dies (an unhandled exception, the machine
rebooting, `Ctrl+C` from someone else's session on the same box), no
amount of in-process retry logic brings it back — something outside
the process has to restart it. Run it under a process supervisor with
restart-on-failure, so a hard crash self-heals without anyone touching
the machine:

- **Windows Task Scheduler**: Create Task → Trigger "At log on" (as in
  step 6) → on the **Settings** tab, tick "Restart the task if it
  fails", set a short restart interval (e.g. 1 minute), and a high
  restart-attempt count.
- **`systemd`** (Linux cloud box, alternative to the `tmux` approach in
  step 5): a unit file with `Restart=always` and a short
  `RestartSec=` restarts the bot automatically on any crash, including
  after a reboot if also `enable`d. Ask if you'd like a ready-made unit
  file.
- **`pm2`**: `pm2 start main_autosl.py --interpreter python3 --name autosl`
  restarts on crash by default; `pm2 save` + `pm2 startup` also
  survives a reboot.

## Troubleshooting

- **Login fails / "Invalid Totp"**: your server/PC clock is off, or you
  used a live 6-digit code instead of the base32 secret. Re-check step 1.
- **`KeyError` reading positions**: Angel One occasionally tweaks field
  names between SDK versions. See the note at the bottom of README.md.
- **Rate-limit errors (429) in live mode**: increase `--poll-interval`.
