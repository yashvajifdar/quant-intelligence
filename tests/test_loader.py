"""Tests for etl.loader orchestration logic.

Does NOT make live network calls — all ingestion functions are mocked at the
module boundary. Tests verify which steps run for each mode and that the
returned quality report dict has the right shape and values.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import MagicMock, call, patch

import pytest

from etl.loader import run


# ── fake return type matching IngestResult ────────────────────────────────────

def _fake_universe(rows: int = 5):
    obj = MagicMock()
    obj.rows_written = rows
    return obj


def _fake_prices(rows: int = 1000, batches_failed: int = 0, tickers: int = 5):
    obj = MagicMock()
    obj.rows_written = rows
    obj.batches_failed = batches_failed
    obj.tickers_attempted = tickers
    return obj


def _fake_macro(rows: int = 100):
    obj = MagicMock()
    obj.rows_written = rows
    return obj


def _fake_fundamentals(rows: int = 5, failed: int = 0, attempted: int = 5):
    obj = MagicMock()
    obj.rows_written = rows
    obj.tickers_failed = failed
    obj.tickers_attempted = attempted
    return obj


# Patch targets — all live in etl.loader's namespace after import
_PATCHES = {
    "validate":       "etl.loader._validate_env",
    "schema":         "etl.loader.initialize_schema",
    "universe":       "etl.loader.load_universe",
    "prices":         "etl.loader.load_prices",
    "macro":          "etl.loader.load_macro",
    "fundamentals":   "etl.loader.load_fundamentals",
    "mkdir":          "etl.loader.Path",
}


# ── incremental mode ──────────────────────────────────────────────────────────

def test_incremental_runs_prices_and_macro_not_fundamentals():
    with (
        patch("etl.loader._validate_env"),
        patch("etl.loader.initialize_schema"),
        patch("etl.loader.Path"),
        patch("etl.loader.load_universe", return_value=_fake_universe()) as mock_u,
        patch("etl.loader.load_prices",   return_value=_fake_prices())   as mock_p,
        patch("etl.loader.load_macro",    return_value=_fake_macro())    as mock_m,
        patch("etl.loader.load_fundamentals")                             as mock_f,
    ):
        result = run(full_refresh=False, with_fundamentals=False)

    mock_u.assert_called_once()
    mock_p.assert_called_once()
    mock_m.assert_called_once()
    mock_f.assert_not_called()
    assert result["fundamentals_rows"] is None
    assert result["fundamentals_failed"] is None


def test_incremental_mode_string():
    with (
        patch("etl.loader._validate_env"),
        patch("etl.loader.initialize_schema"),
        patch("etl.loader.Path"),
        patch("etl.loader.load_universe",      return_value=_fake_universe()),
        patch("etl.loader.load_prices",        return_value=_fake_prices()),
        patch("etl.loader.load_macro",         return_value=_fake_macro()),
        patch("etl.loader.load_fundamentals"),
    ):
        result = run(full_refresh=False)

    assert result["mode"] == "INCREMENTAL"


# ── fundamentals-only mode ────────────────────────────────────────────────────

def test_fundamentals_only_skips_prices_and_macro():
    with (
        patch("etl.loader._validate_env"),
        patch("etl.loader.initialize_schema"),
        patch("etl.loader.Path"),
        patch("etl.loader.load_universe",    return_value=_fake_universe()) as mock_u,
        patch("etl.loader.load_prices")                                     as mock_p,
        patch("etl.loader.load_macro")                                      as mock_m,
        patch("etl.loader.load_fundamentals", return_value=_fake_fundamentals()) as mock_f,
    ):
        result = run(fundamentals_only=True)

    mock_u.assert_called_once()
    mock_p.assert_not_called()
    mock_m.assert_not_called()
    mock_f.assert_called_once()


def test_fundamentals_only_mode_string():
    with (
        patch("etl.loader._validate_env"),
        patch("etl.loader.initialize_schema"),
        patch("etl.loader.Path"),
        patch("etl.loader.load_universe",     return_value=_fake_universe()),
        patch("etl.loader.load_fundamentals", return_value=_fake_fundamentals()),
    ):
        result = run(fundamentals_only=True)

    assert result["mode"] == "FUNDAMENTALS ONLY"


def test_fundamentals_only_report_has_none_for_prices_and_macro():
    with (
        patch("etl.loader._validate_env"),
        patch("etl.loader.initialize_schema"),
        patch("etl.loader.Path"),
        patch("etl.loader.load_universe",     return_value=_fake_universe()),
        patch("etl.loader.load_fundamentals", return_value=_fake_fundamentals(rows=7, failed=1)),
    ):
        result = run(fundamentals_only=True)

    assert result["price_rows"] is None
    assert result["price_batches_failed"] is None
    assert result["macro_rows"] is None
    assert result["fundamentals_rows"] == 7
    assert result["fundamentals_failed"] == 1


# ── with-fundamentals mode (incremental + fundamentals) ───────────────────────

def test_with_fundamentals_runs_all_steps():
    with (
        patch("etl.loader._validate_env"),
        patch("etl.loader.initialize_schema"),
        patch("etl.loader.Path"),
        patch("etl.loader.load_universe",     return_value=_fake_universe()) as mock_u,
        patch("etl.loader.load_prices",       return_value=_fake_prices())   as mock_p,
        patch("etl.loader.load_macro",        return_value=_fake_macro())    as mock_m,
        patch("etl.loader.load_fundamentals", return_value=_fake_fundamentals()) as mock_f,
    ):
        result = run(full_refresh=False, with_fundamentals=True)

    mock_u.assert_called_once()
    mock_p.assert_called_once()
    mock_m.assert_called_once()
    mock_f.assert_called_once()
    assert result["fundamentals_rows"] is not None


# ── full-refresh mode ─────────────────────────────────────────────────────────

def test_full_refresh_mode_string():
    with (
        patch("etl.loader._validate_env"),
        patch("etl.loader.initialize_schema"),
        patch("etl.loader.Path"),
        patch("etl.loader.load_universe", return_value=_fake_universe()),
        patch("etl.loader.load_prices",   return_value=_fake_prices()),
        patch("etl.loader.load_macro",    return_value=_fake_macro()),
        patch("etl.loader.load_fundamentals"),
    ):
        result = run(full_refresh=True)

    assert result["mode"] == "FULL REFRESH"


def test_full_refresh_passes_flag_to_prices():
    with (
        patch("etl.loader._validate_env"),
        patch("etl.loader.initialize_schema"),
        patch("etl.loader.Path"),
        patch("etl.loader.load_universe", return_value=_fake_universe()),
        patch("etl.loader.load_prices",   return_value=_fake_prices()) as mock_p,
        patch("etl.loader.load_macro",    return_value=_fake_macro())  as mock_m,
        patch("etl.loader.load_fundamentals"),
    ):
        run(full_refresh=True)

    _, pkwargs = mock_p.call_args
    assert pkwargs.get("full_refresh") is True or mock_p.call_args[0][1] is True
