from __future__ import annotations

from datetime import date, datetime

from clients.ifind_client import IFindDataPoolClient
from models import FinancialSyncResult
from utils.periods import (
    disclosure_year_from_period_label,
    median_mmdd,
    next_period_label,
    quarter_label_from_date,
)


class AShareWatchlistProcessor:
    def __init__(self) -> None:
        self.ifind_client = IFindDataPoolClient()

    def analyze(self, code: str, as_of_date: date | None = None) -> FinancialSyncResult:
        import akshare as ak
        import pandas as pd

        report_df = ak.stock_financial_analysis_indicator_em(symbol=code.upper(), indicator="按报告期")
        single_df = ak.stock_financial_analysis_indicator_em(symbol=code.upper(), indicator="按单季度")

        report_df = self._prepare_report_df(report_df, pd)
        single_df = self._prepare_single_df(single_df, pd)
        if single_df.empty:
            return FinancialSyncResult(meta={"market": "A股", "code": code})

        merged = single_df.merge(report_df, on="REPORT_DATE", how="left")
        merged = merged.sort_values("REPORT_DATE", ascending=False).reset_index(drop=True)

        latest_report_date = merged.iloc[0]["REPORT_DATE"]
        latest_actual = quarter_label_from_date(latest_report_date, "A")
        target_period = next_period_label(latest_actual, "E")
        yoy_base_profit = self._extract_base_profit(merged, "PARENTNETPROFIT", 3)

        planned_disclosure = self.ifind_client.get_scheduled_disclosure_date(code.upper(), target_period)
        if planned_disclosure is None:
            planned_disclosure = self._estimate_disclosure_from_history(merged, target_period)

        return FinancialSyncResult(
            latest_actual_period=latest_actual,
            target_period_estimate=target_period,
            planned_disclosure_date=planned_disclosure.date() if isinstance(planned_disclosure, datetime) else planned_disclosure,
            yoy_base_profit=yoy_base_profit,
            meta={
                "market": "A股",
                "code": code,
                "row_count": len(merged),
            },
        )

    @staticmethod
    def _prepare_report_df(df, pd):
        if df is None or df.empty:
            return pd.DataFrame(columns=["REPORT_DATE", "NOTICE_DATE"])
        subset = [column for column in ["REPORT_DATE", "NOTICE_DATE"] if column in df.columns]
        output = df[subset].copy()
        if "REPORT_DATE" in output.columns:
            output["REPORT_DATE"] = pd.to_datetime(output["REPORT_DATE"]).dt.strftime("%Y-%m-%d")
        if "NOTICE_DATE" in output.columns:
            output["NOTICE_DATE"] = pd.to_datetime(output["NOTICE_DATE"], errors="coerce").dt.strftime("%Y-%m-%d")
        return output

    @staticmethod
    def _prepare_single_df(df, pd):
        if df is None or df.empty:
            return pd.DataFrame(columns=["REPORT_DATE", "TOTALOPERATEREVE", "GROSS_PROFIT", "PARENTNETPROFIT"])
        subset = [
            column
            for column in ["REPORT_DATE", "TOTALOPERATEREVE", "GROSS_PROFIT", "PARENTNETPROFIT"]
            if column in df.columns
        ]
        output = df[subset].copy()
        if "REPORT_DATE" in output.columns:
            output["REPORT_DATE"] = pd.to_datetime(output["REPORT_DATE"]).dt.strftime("%Y-%m-%d")
        return output

    @staticmethod
    def _extract_base_profit(df, column: str, row_index: int) -> float | None:
        if column not in df.columns or len(df) <= row_index:
            return None
        try:
            return float(df.iloc[row_index][column]) / 100000000.0
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _estimate_disclosure_from_history(df, target_period: str) -> date | None:
        if "NOTICE_DATE" not in df.columns:
            return None
        picked = []
        for index in (3, 7, 11):
            if len(df) > index:
                value = df.iloc[index]["NOTICE_DATE"]
                if value not in (None, "", "NaT"):
                    picked.append(str(value))
        median = median_mmdd(picked)
        if not median:
            return None
        month, day = median
        year = disclosure_year_from_period_label(target_period)
        return date(year, month, day)
