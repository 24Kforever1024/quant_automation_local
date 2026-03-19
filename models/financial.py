from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass
class FinancialSyncResult:
    latest_actual_period: str = ""
    target_period_estimate: str = ""
    earnings_prediction_text: str = ""
    planned_disclosure_date: date | None = None
    yoy_base_profit: float | None = None
    meta: dict[str, Any] = field(default_factory=dict)
