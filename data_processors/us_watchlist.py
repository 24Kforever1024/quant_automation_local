from __future__ import annotations

import os
import random
import threading
import time
from datetime import date, datetime
from pathlib import Path

from models import FinancialSyncResult
from utils.periods import next_period_label, normalize_us_symbol, quarter_label_from_date

YFINANCE_RETRY_DELAYS_SECONDS = (5, 5, 5, 10, 10)
YFINANCE_SUCCESS_COOLDOWN_RANGE_SECONDS = (1.0, 2.5)
YFINANCE_LOCK = threading.Lock()


class USWatchlistProcessor:
    def analyze(self, code: str, as_of_date: date | None = None) -> FinancialSyncResult:
        import akshare as ak
        import pandas as pd

        symbol = normalize_us_symbol(code)
        df = ak.stock_financial_us_analysis_indicator_em(symbol=symbol, indicator="单季报")
        if df is None or df.empty:
            return FinancialSyncResult(meta={"market": "美股", "code": code})

        keep_columns = [
            column
            for column in ["STD_REPORT_DATE", "CURRENCY", "OPERATE_INCOME", "GROSS_PROFIT", "PARENT_HOLDER_NETPROFIT"]
            if column in df.columns
        ]
        working = df[keep_columns].copy()
        working["STD_REPORT_DATE"] = pd.to_datetime(working["STD_REPORT_DATE"]).dt.strftime("%Y-%m-%d")
        working = working.sort_values("STD_REPORT_DATE", ascending=False).reset_index(drop=True)

        latest_actual = quarter_label_from_date(working.iloc[0]["STD_REPORT_DATE"], "A")
        target_period = next_period_label(latest_actual, "E")
        yoy_base_profit = self._extract_base_profit(working, 3)
        planned_disclosure = self._load_earnings_date_from_yfinance(symbol)

        return FinancialSyncResult(
            latest_actual_period=latest_actual,
            target_period_estimate=target_period,
            planned_disclosure_date=planned_disclosure.date() if isinstance(planned_disclosure, datetime) else planned_disclosure,
            yoy_base_profit=yoy_base_profit,
            meta={
                "market": "美股",
                "code": code,
                "currency": working.iloc[0]["CURRENCY"] if "CURRENCY" in working.columns else None,
                "row_count": len(working),
                "planned_disclosure_source": "yfinance" if planned_disclosure is not None else None,
            },
        )

    @staticmethod
    def _load_earnings_date_from_yfinance(symbol: str) -> datetime | None:
        try:
            import yfinance as yf
        except Exception:
            return None

        cache_dir = USWatchlistProcessor._resolve_yfinance_cache_dir()
        with YFINANCE_LOCK:
            last_error: Exception | None = None
            for attempt in range(len(YFINANCE_RETRY_DELAYS_SECONDS) + 1):
                try:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    yf.set_tz_cache_location(str(cache_dir))
                    ticker = yf.Ticker(symbol)
                    earnings_dates = getattr(ticker, "earnings_dates", None)
                    extracted_date = USWatchlistProcessor._extract_earnings_date(earnings_dates)
                    cooldown_seconds = random.uniform(*YFINANCE_SUCCESS_COOLDOWN_RANGE_SECONDS)
                    print(
                        f"[yfinance] earnings_dates fetched for {symbol}; "
                        f"cooling down for {cooldown_seconds:.2f}s"
                    )
                    time.sleep(cooldown_seconds)
                    return extracted_date
                except Exception as exc:
                    last_error = exc
                    if attempt < len(YFINANCE_RETRY_DELAYS_SECONDS):
                        retry_delay = YFINANCE_RETRY_DELAYS_SECONDS[attempt]
                        print(
                            f"[yfinance] earnings_dates unavailable for {symbol}: {exc}; "
                            f"retrying in {retry_delay}s"
                        )
                        time.sleep(retry_delay)
                        continue
                    print(f"[yfinance] earnings_dates unavailable for {symbol}: {exc}")
                    return None

            print(f"[yfinance] earnings_dates unavailable for {symbol}: {last_error}")
            return None

    @staticmethod
    def _resolve_yfinance_cache_dir() -> Path:
        configured_dir = os.getenv("YFINANCE_CACHE_DIR", "").strip()
        if configured_dir:
            return Path(configured_dir).expanduser()

        runner_temp = os.getenv("RUNNER_TEMP", "").strip()
        if runner_temp:
            return Path(runner_temp) / "yfinance-cache"

        return Path(__file__).resolve().parents[1] / ".cache" / "yfinance"

    @staticmethod
    def _extract_base_profit(df, row_index: int) -> float | None:
        if "PARENT_HOLDER_NETPROFIT" not in df.columns or len(df) <= row_index:
            return None
        try:
            return float(df.iloc[row_index]["PARENT_HOLDER_NETPROFIT"]) / 100000000.0
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _extract_earnings_date(earnings_dates) -> datetime | None:
        if earnings_dates is None or getattr(earnings_dates, "empty", True):
            return None
        first_row = earnings_dates.iloc[0]
        if "Earnings Date" in getattr(earnings_dates, "columns", []):
            value = first_row["Earnings Date"]
            if hasattr(value, "to_pydatetime"):
                return value.to_pydatetime()
            if isinstance(value, datetime):
                return value
        if hasattr(first_row.name, "to_pydatetime"):
            return first_row.name.to_pydatetime()
        if isinstance(first_row.name, datetime):
            return first_row.name
        text = str(first_row.name or "")[:19]
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        return None
