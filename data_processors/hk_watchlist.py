from __future__ import annotations

import importlib.util
import threading
from datetime import date
from pathlib import Path
from typing import Mapping, Any

from config import get_settings
from models import FinancialSyncResult
from utils.periods import previous_period_label


class HKWatchlistProcessor:
    def __init__(self) -> None:
        self._thread_local = threading.local()

    def _get_engine(self):
        engine = getattr(self._thread_local, "engine", None)
        if engine is not None:
            return engine
        module_path = Path(__file__).with_name("hk_watchlist_engine.py")
        module_name = f"hk_watchlist_engine_{threading.get_ident()}"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load HK watchlist engine from {module_path}")
        engine = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(engine)
        self._thread_local.engine = engine
        return engine

    def analyze(
        self,
        code: str,
        as_of_date: date | None = None,
        llm_api_channel: Mapping[str, Any] | None = None,
    ) -> FinancialSyncResult:
        engine = self._get_engine()
        settings = get_settings()
        engine.VOLCENGINE_API_KEY = settings.volcengine_api_key
        engine.VOLCENGINE_MODEL = settings.volcengine_model or engine.VOLCENGINE_MODEL
        engine.ACCESS_TOKEN = ""
        engine.REFRESH_TOKEN = settings.ifind_refresh_token
        engine.USE_REFRESH_TO_GET_ACCESS = bool(engine.REFRESH_TOKEN)
        engine.TODAY_DATE = (as_of_date or date.today()).strftime("%Y-%m-%d")
        engine.HK_LLM_API_CHANNEL_OVERRIDE = dict(llm_api_channel) if llm_api_channel else None

        _, predicted_date, phase3 = engine.fetch_dynamic_yoy_reports(code.upper())
        phase3 = phase3 or {}
        target_name_short = str(phase3.get("target_name_short") or "")
        target_period = f"{target_name_short}E" if target_name_short else ""
        latest_actual = previous_period_label(target_name_short, "A") if target_name_short else ""

        fye_month = phase3.get("fye_month")
        target_period_name = str(phase3.get("target_period") or "")
        target_year = phase3.get("target_year")
        frequency = str(phase3.get("frequency") or "")
        if isinstance(fye_month, int) and fye_month != 12 and isinstance(target_year, int) and target_period_name:
            fy_target_base = engine.compute_fy_label(int(target_year) - 1, target_period_name, frequency, int(fye_month))
            target_period = f"{fy_target_base}E"
            latest_actual = previous_period_label(fy_target_base, "A")

        planned_disclosure = None
        if predicted_date:
            planned_disclosure = date.fromisoformat(str(predicted_date)[:10])

        return FinancialSyncResult(
            latest_actual_period=latest_actual,
            target_period_estimate=target_period,
            planned_disclosure_date=planned_disclosure,
            yoy_base_profit=(phase3 or {}).get("net_profit"),
            meta={"market": "港股", **(phase3 or {})},
        )
