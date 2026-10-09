"""
config_loader.py
==================
Loads non-secret tunables from config.yaml (if present), layered over
built-in defaults: loss threshold, SL-LIMIT price buffer, poll
intervals, and which exchanges/segments the bot is allowed to act on.

Secrets never go here. API key, client code, PIN, and TOTP secret stay
in .env only, loaded via python-dotenv (see .env.example) -- this file
never reads or writes that file.

Precedence, implemented via resolve() below: a CLI flag (if the caller
actually passed one) wins over config.yaml, which wins over the
built-in default. config.yaml itself is gitignored; only
config.example.yaml ships in the repo.
"""
from __future__ import annotations
import os
from decimal import Decimal

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

DEFAULT_CONFIG_PATH = "config.yaml"

DEFAULTS = {
    "stop_loss_pct": -10.0,
    "sl_limit_buffer": Decimal("0.5"),
    "poll_interval": 1.0,
    "safety_net_interval": 10.0,
    "stuck_sl_margin_pct": 2.0,
    "enabled_exchanges": {"NSE", "NFO", "BSE", "BFO", "MCX"},
}

_VALID_EXCHANGES = {"NSE", "NFO", "BSE", "BFO", "MCX"}


def load_config(path=None):
    """
    Returns a dict of tunables: DEFAULTS, overridden by whatever is in
    config.yaml if that file exists (explicit `path`, or ./config.yaml
    if present and no `path` given -- no file at all is the normal case
    and silently returns DEFAULTS, since config.yaml is gitignored).

    Raises RuntimeError -- loudly, not silently -- for a config file
    that exists but can't be parsed, sets a key this tool doesn't
    recognize (a typo'd risk parameter like "stop_lose_pct" would
    otherwise just be ignored), or sets enabled_exchanges to something
    invalid.
    """
    config = dict(DEFAULTS)
    path = path or (DEFAULT_CONFIG_PATH if os.path.exists(DEFAULT_CONFIG_PATH) else None)

    if path:
        if not os.path.exists(path):
            raise RuntimeError(f"Config file not found: {path}")
        if yaml is None:
            raise RuntimeError(
                "PyYAML is not installed but a config file was given. "
                "Run: pip install -r requirements.txt"
            )
        with open(path, "r", encoding="utf-8") as f:
            user_config = yaml.safe_load(f) or {}

        unknown = set(user_config) - set(DEFAULTS)
        if unknown:
            raise RuntimeError(
                f"Unknown config key(s) in {path}: {', '.join(sorted(unknown))}. "
                f"Valid keys: {', '.join(sorted(DEFAULTS))}"
            )
        config.update(user_config)
        if "sl_limit_buffer" in user_config:
            # YAML has no Decimal type -- str() first so e.g. 0.5 (a
            # binary float) becomes the exact Decimal("0.5"), matching
            # how every other price computation in this project avoids
            # float-precision artifacts (see broker/stoploss_placer.py).
            config["sl_limit_buffer"] = Decimal(str(user_config["sl_limit_buffer"]))

    exchanges = config.get("enabled_exchanges")
    if not isinstance(exchanges, (list, set, tuple)) or not exchanges:
        raise RuntimeError(f"'enabled_exchanges' must be a non-empty list, got {exchanges!r}")
    bad = sorted(e for e in exchanges if e not in _VALID_EXCHANGES)
    if bad:
        raise RuntimeError(
            f"Unknown exchange(s) in enabled_exchanges: {bad}. "
            f"Valid: {sorted(_VALID_EXCHANGES)}"
        )
    config["enabled_exchanges"] = set(exchanges)

    return config


def resolve(cli_value, config: dict, key: str):
    """
    cli_value if the caller actually passed that CLI flag, else
    config[key] (the config.yaml value, or the built-in default if no
    config file/key set it). Callers must give the relevant argparse
    flag a default of None so "not passed on the command line" is
    distinguishable from "passed, and happens to equal the default".
    """
    return cli_value if cli_value is not None else config[key]
