import types
import unittest
from datetime import datetime
from pathlib import Path
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pandas as pd

from data_processors.us_watchlist import (
    USWatchlistProcessor,
    YFINANCE_RETRY_DELAYS_SECONDS,
    YFINANCE_SUCCESS_COOLDOWN_RANGE_SECONDS,
)


class USWatchlistProcessorTests(unittest.TestCase):
    def test_us_watchlist_leaves_disclosure_empty_when_yfinance_unavailable(self) -> None:
        fake_df = pd.DataFrame(
            [
                {"STD_REPORT_DATE": "2025-12-31", "CURRENCY": "USD", "PARENT_HOLDER_NETPROFIT": 40000000000},
                {"STD_REPORT_DATE": "2025-09-30", "CURRENCY": "USD", "PARENT_HOLDER_NETPROFIT": 30000000000},
                {"STD_REPORT_DATE": "2025-06-30", "CURRENCY": "USD", "PARENT_HOLDER_NETPROFIT": 20000000000},
                {"STD_REPORT_DATE": "2024-12-31", "CURRENCY": "USD", "PARENT_HOLDER_NETPROFIT": 10000000000},
            ]
        )
        fake_ak = types.SimpleNamespace(
            stock_financial_us_analysis_indicator_em=lambda symbol, indicator: fake_df
        )

        with patch.dict("sys.modules", {"akshare": fake_ak}), patch.object(
            USWatchlistProcessor,
            "_load_earnings_date_from_yfinance",
            return_value=None,
        ):
            result = USWatchlistProcessor().analyze("MU.O")

        self.assertEqual(result.meta.get("market"), "美股")
        self.assertIsNone(result.meta.get("planned_disclosure_source"))
        self.assertIsNone(result.planned_disclosure_date)
        self.assertEqual(result.target_period_estimate, "26Q1E")

    def test_resolve_yfinance_cache_dir_prefers_explicit_env(self) -> None:
        with patch.dict("os.environ", {"YFINANCE_CACHE_DIR": "C:/tmp/yf-cache"}, clear=True):
            cache_dir = USWatchlistProcessor._resolve_yfinance_cache_dir()

        self.assertEqual(cache_dir, Path("C:/tmp/yf-cache"))

    def test_resolve_yfinance_cache_dir_uses_runner_temp_on_github(self) -> None:
        with patch.dict("os.environ", {"RUNNER_TEMP": "/tmp/runner"}, clear=True):
            cache_dir = USWatchlistProcessor._resolve_yfinance_cache_dir()

        self.assertEqual(cache_dir, Path("/tmp/runner/yfinance-cache"))

    def test_load_earnings_date_from_yfinance_sets_cache_location(self) -> None:
        fake_earnings_dates = pd.DataFrame(
            {"Earnings Date": [pd.Timestamp("2026-03-25 00:00:00")]}
        )
        fake_ticker = types.SimpleNamespace(earnings_dates=fake_earnings_dates)
        fake_yf = types.SimpleNamespace(
            set_tz_cache_location=lambda path: setattr(fake_yf, "cache_path", path),
            Ticker=lambda symbol: fake_ticker,
        )

        tmp_dir = Path("tests/.tmp_yfinance_cache")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        try:
            with patch.dict("sys.modules", {"yfinance": fake_yf}), patch.dict(
                "os.environ",
                {"YFINANCE_CACHE_DIR": str(tmp_dir)},
                clear=True,
            ), patch("data_processors.us_watchlist.random.uniform", return_value=1.25), patch(
                "data_processors.us_watchlist.time.sleep"
            ) as mock_sleep:
                value = USWatchlistProcessor._load_earnings_date_from_yfinance("MU")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertEqual(value, datetime(2026, 3, 25))
        self.assertEqual(Path(fake_yf.cache_path), tmp_dir)
        mock_sleep.assert_called_once_with(1.25)

    def test_load_earnings_date_from_yfinance_retries_with_expected_schedule(self) -> None:
        fake_earnings_dates = pd.DataFrame(
            {"Earnings Date": [pd.Timestamp("2026-03-25 00:00:00")]}
        )
        attempts = {"count": 0}

        class _FakeTicker:
            @property
            def earnings_dates(self):
                attempts["count"] += 1
                if attempts["count"] < 4:
                    raise RuntimeError(f"temporary failure {attempts['count']}")
                return fake_earnings_dates

        fake_yf = types.SimpleNamespace(
            set_tz_cache_location=lambda path: setattr(fake_yf, "cache_path", path),
            Ticker=lambda symbol: _FakeTicker(),
        )

        tmp_dir = Path("tests/.tmp_yfinance_cache_retry")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        try:
            with patch.dict("sys.modules", {"yfinance": fake_yf}), patch.dict(
                "os.environ",
                {"YFINANCE_CACHE_DIR": str(tmp_dir)},
                clear=True,
            ), patch("data_processors.us_watchlist.random.uniform", return_value=1.5), patch(
                "data_processors.us_watchlist.time.sleep"
            ) as mock_sleep:
                value = USWatchlistProcessor._load_earnings_date_from_yfinance("MU")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertEqual(value, datetime(2026, 3, 25))
        self.assertEqual(attempts["count"], 4)
        self.assertEqual(
            [call.args[0] for call in mock_sleep.call_args_list],
            [*YFINANCE_RETRY_DELAYS_SECONDS[:3], 1.5],
        )

    def test_load_earnings_date_from_yfinance_applies_success_cooldown(self) -> None:
        fake_earnings_dates = pd.DataFrame(
            {"Earnings Date": [pd.Timestamp("2026-03-25 00:00:00")]}
        )
        fake_ticker = types.SimpleNamespace(earnings_dates=fake_earnings_dates)
        fake_yf = types.SimpleNamespace(
            set_tz_cache_location=lambda path: setattr(fake_yf, "cache_path", path),
            Ticker=lambda symbol: fake_ticker,
        )

        tmp_dir = Path("tests/.tmp_yfinance_cache_cooldown")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        try:
            with patch.dict("sys.modules", {"yfinance": fake_yf}), patch.dict(
                "os.environ",
                {"YFINANCE_CACHE_DIR": str(tmp_dir)},
                clear=True,
            ), patch("data_processors.us_watchlist.random.uniform", return_value=2.25) as mock_uniform, patch(
                "data_processors.us_watchlist.time.sleep"
            ) as mock_sleep:
                value = USWatchlistProcessor._load_earnings_date_from_yfinance("AAPL")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertEqual(value, datetime(2026, 3, 25))
        mock_uniform.assert_called_once_with(*YFINANCE_SUCCESS_COOLDOWN_RANGE_SECONDS)
        mock_sleep.assert_called_once_with(2.25)

    def test_load_earnings_date_from_yfinance_serializes_concurrent_calls(self) -> None:
        fake_earnings_dates = pd.DataFrame(
            {"Earnings Date": [pd.Timestamp("2026-03-25 00:00:00")]}
        )
        state_lock = threading.Lock()
        state = {"active": 0, "max_active": 0}

        class _FakeTicker:
            @property
            def earnings_dates(self):
                with state_lock:
                    state["active"] += 1
                    state["max_active"] = max(state["max_active"], state["active"])
                threading.Event().wait(0.05)
                with state_lock:
                    state["active"] -= 1
                return fake_earnings_dates

        fake_yf = types.SimpleNamespace(
            set_tz_cache_location=lambda path: setattr(fake_yf, "cache_path", path),
            Ticker=lambda symbol: _FakeTicker(),
        )

        tmp_dir = Path("tests/.tmp_yfinance_cache_lock")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        try:
            with patch.dict("sys.modules", {"yfinance": fake_yf}), patch.dict(
                "os.environ",
                {"YFINANCE_CACHE_DIR": str(tmp_dir)},
                clear=True,
            ), patch("data_processors.us_watchlist.random.uniform", return_value=1.0), patch(
                "data_processors.us_watchlist.time.sleep"
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(USWatchlistProcessor._load_earnings_date_from_yfinance, "MU"),
                        executor.submit(USWatchlistProcessor._load_earnings_date_from_yfinance, "AAPL"),
                    ]
                    values = [future.result() for future in futures]
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        self.assertEqual(values, [datetime(2026, 3, 25), datetime(2026, 3, 25)])
        self.assertEqual(state["max_active"], 1)


if __name__ == "__main__":
    unittest.main()
