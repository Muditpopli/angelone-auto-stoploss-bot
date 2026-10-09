"""
tests/test_config_loader.py
=============================
Unit tests for config_loader.py: pure logic, no network, no real files
outside a pytest tmp_path fixture.
"""
import os
import sys
from decimal import Decimal

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config_loader import DEFAULTS, load_config, resolve


def test_no_config_file_returns_defaults():
    config = load_config(path=None)
    assert config["stop_loss_pct"] == DEFAULTS["stop_loss_pct"]
    assert config["sl_limit_buffer"] == Decimal("0.5")
    assert isinstance(config["sl_limit_buffer"], Decimal)
    assert config["enabled_exchanges"] == {"NSE", "NFO", "BSE", "BFO", "MCX"}
    print("PASS: no config.yaml falls back cleanly to built-in defaults")


def test_missing_explicit_path_raises(tmp_path):
    with pytest.raises(RuntimeError, match="not found"):
        load_config(str(tmp_path / "does_not_exist.yaml"))
    print("PASS: an explicitly-given, missing config path raises clearly")


def test_partial_config_overrides_only_the_keys_it_sets(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("stop_loss_pct: -7.5\npoll_interval: 2.0\n")

    config = load_config(str(cfg_file))

    assert config["stop_loss_pct"] == -7.5
    assert config["poll_interval"] == 2.0
    # untouched keys keep their built-in defaults
    assert config["safety_net_interval"] == DEFAULTS["safety_net_interval"]
    assert config["sl_limit_buffer"] == DEFAULTS["sl_limit_buffer"]
    print("PASS: a partial config file overrides only the keys it sets")


def test_sl_limit_buffer_becomes_an_exact_decimal_not_a_float(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("sl_limit_buffer: 0.75\n")

    config = load_config(str(cfg_file))

    assert config["sl_limit_buffer"] == Decimal("0.75")
    assert isinstance(config["sl_limit_buffer"], Decimal), (
        "sl_limit_buffer must be a Decimal (matching every other price "
        "computation in this project), not a raw YAML-parsed float"
    )
    print("PASS: sl_limit_buffer is converted to an exact Decimal")


def test_unknown_key_raises_loudly(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("stop_lose_pct: -5.0\n")  # typo'd key

    with pytest.raises(RuntimeError, match="Unknown config key"):
        load_config(str(cfg_file))
    print("PASS: an unrecognized config key raises instead of being silently ignored")


def test_enabled_exchanges_subset_is_accepted_and_becomes_a_set(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("enabled_exchanges:\n  - NSE\n  - NFO\n")

    config = load_config(str(cfg_file))

    assert config["enabled_exchanges"] == {"NSE", "NFO"}
    print("PASS: a restricted enabled_exchanges list is accepted and normalized to a set")


def test_invalid_exchange_name_raises(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("enabled_exchanges:\n  - NSE\n  - NASDAQ\n")

    with pytest.raises(RuntimeError, match="Unknown exchange"):
        load_config(str(cfg_file))
    print("PASS: an unrecognized exchange name raises instead of being silently accepted")


def test_empty_enabled_exchanges_raises(tmp_path):
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("enabled_exchanges: []\n")

    with pytest.raises(RuntimeError, match="non-empty"):
        load_config(str(cfg_file))
    print("PASS: an empty enabled_exchanges list raises rather than silently disabling everything")


def test_resolve_cli_flag_wins_over_config():
    config = {"stop_loss_pct": -10.0}
    assert resolve(-7.5, config, "stop_loss_pct") == -7.5
    print("PASS: resolve() prefers an explicitly-passed CLI value over config")


def test_resolve_falls_back_to_config_when_cli_flag_not_passed():
    config = {"stop_loss_pct": -12.0}
    assert resolve(None, config, "stop_loss_pct") == -12.0
    print("PASS: resolve() falls back to the config value when the CLI flag is None (not passed)")


if __name__ == "__main__":
    import tempfile
    test_no_config_file_returns_defaults()
    test_resolve_cli_flag_wins_over_config()
    test_resolve_falls_back_to_config_when_cli_flag_not_passed()
    print("\nNon-tmp_path checks passed (run via pytest for the full suite).")
