from __future__ import annotations

from datetime import date, datetime
from typing import Any

import requests

from config import Settings, get_settings
from utils.periods import bgqs_from_period_label


class IFindDataPoolClient:
    BASE_URL = "https://quantapi.51ifind.com/api/v1"

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._access_token: str = self.settings.ifind_access_token

    def get_scheduled_disclosure_date(self, stock_code: str, target_period: str) -> datetime | None:
        if not self._access_token and not self.settings.ifind_refresh_token:
            return None
        bg_year = f"20{str(target_period)[:2]}"
        bgqs = bgqs_from_period_label(target_period)
        if not bgqs:
            return None

        payload = {
            "reportname": "p00210",
            "functionpara": {
                "bgyear": bg_year,
                "bgqs": bgqs,
                "IFindKey": stock_code.upper(),
            },
            "outputpara": "jydm,jydm_mc,p00210_f001",
        }
        try:
            response = self._post_with_retry("data_pool", payload, timeout=30)
            return self._extract_first_date(response)
        except requests.RequestException:
            return None

    def get_realtime_quote(self, stock_code: str) -> dict[str, float]:
        payload = {
            "codes": stock_code.upper(),
            "indicators": "latest,totalCapital,changeRatio",
        }
        response = self._post_with_retry(
            "real_time_quotation",
            payload,
            timeout=20,
            extra_headers={"ifindlang": "cn"},
        )
        if response.get("errorcode") not in (None, 0):
            raise requests.RequestException(
                f"IFind realtime quote error: {response.get('errmsg') or response.get('errorinfo') or 'unknown'}"
            )

        tables = response.get("tables") or []
        if not tables:
            raise requests.RequestException("IFind realtime quote missing tables")

        table = tables[0].get("table") or {}
        latest = self._extract_first_number(table.get("latest"))
        total_capital = self._extract_first_number(table.get("totalCapital"))
        change_ratio = self._extract_first_number(table.get("changeRatio"))
        if latest is None or total_capital is None or change_ratio is None:
            raise requests.RequestException("IFind realtime quote missing fields")

        return {
            "latest": latest,
            "totalCapital": total_capital,
            "changeRatio": change_ratio,
        }

    def get_realtime_quote_without_market_cap(self, stock_code: str) -> dict[str, float]:
        payload = {
            "codes": stock_code.upper(),
            "indicators": "latest,changeRatio",
        }
        response = self._post_with_retry(
            "real_time_quotation",
            payload,
            timeout=20,
            extra_headers={"ifindlang": "cn"},
        )
        if response.get("errorcode") not in (None, 0):
            raise requests.RequestException(
                f"IFind realtime quote error: {response.get('errmsg') or response.get('errorinfo') or 'unknown'}"
            )

        tables = response.get("tables") or []
        if not tables:
            raise requests.RequestException("IFind realtime quote missing tables")

        table = tables[0].get("table") or {}
        latest = self._extract_first_number(table.get("latest"))
        change_ratio = self._extract_first_number(table.get("changeRatio"))
        if latest is None or change_ratio is None:
            raise requests.RequestException("IFind realtime quote missing fields")

        return {
            "latest": latest,
            "changeRatio": change_ratio,
        }

    def get_total_shares(self, stock_code: str, as_of_date: date | None = None) -> float:
        date_text = ""
        if as_of_date is not None:
            date_text = as_of_date.strftime("%Y-%m-%d")

        payload = {
            "codes": stock_code.upper(),
            "indipara": [{"indicator": "total_shares", "indiparams": [date_text] if date_text else []}],
        }
        response = self._post_with_retry("basic_data_service", payload, timeout=20)
        if response.get("errorcode") not in (None, 0):
            raise requests.RequestException(
                f"IFind total_shares error: {response.get('errmsg') or response.get('errorinfo') or 'unknown'}"
            )

        tables = response.get("tables") or []
        if not tables:
            raise requests.RequestException("IFind total_shares missing tables")

        table = tables[0].get("table") or {}
        total_shares = self._extract_first_number(table.get("total_shares"))
        if total_shares is None:
            raise requests.RequestException("IFind total_shares missing fields")
        return total_shares

    def _refresh_access_token(self, force_new: bool = False) -> str:
        refresh_token = self.settings.ifind_refresh_token
        if not refresh_token:
            raise requests.RequestException("IFind refresh token missing")
        endpoint = "update_access_token" if force_new else "get_access_token"
        response = requests.post(
            f"{self.BASE_URL}/{endpoint}",
            headers={"Content-Type": "application/json", "refresh_token": refresh_token},
            timeout=20,
        )
        response.raise_for_status()
        body = response.json() or {}
        if body.get("errorcode") not in (None, 0):
            raise requests.RequestException(
                f"IFind access_token error: {body.get('errmsg') or body.get('errorinfo') or 'unknown'}"
            )
        token = (body.get("data") or {}).get("access_token")
        if not token:
            raise requests.RequestException("IFind access_token not found")
        self._access_token = str(token)
        return self._access_token

    def _post_with_retry(
        self,
        endpoint: str,
        payload: dict[str, Any],
        timeout: int,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        last_error: requests.RequestException | None = None
        for attempt in range(2):
            token = self._access_token or self._refresh_access_token(force_new=False)
            try:
                response = requests.post(
                    f"{self.BASE_URL}/{endpoint}",
                    json=payload,
                    headers={
                        "Content-Type": "application/json",
                        "access_token": token,
                        **(extra_headers or {}),
                    },
                    timeout=timeout,
                )
                if response.status_code == 401 and self.settings.ifind_refresh_token and attempt == 0:
                    self._refresh_access_token(force_new=True)
                    continue
                response.raise_for_status()
                return response.json()
            except requests.RequestException as exc:
                last_error = exc
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status == 401 and self.settings.ifind_refresh_token and attempt == 0:
                    self._refresh_access_token(force_new=True)
                    continue
                raise
        if last_error is not None:
            raise last_error
        raise requests.RequestException(f"IFind request failed: {endpoint}")

    def _extract_first_date(self, payload: Any) -> datetime | None:
        value = self._walk_for_key(payload, "p00210_f001")
        if isinstance(value, list):
            for item in value:
                parsed = self._parse_date(item)
                if parsed:
                    return parsed
            return None
        return self._parse_date(value)

    def _walk_for_key(self, payload: Any, target_key: str) -> Any:
        if isinstance(payload, dict):
            if target_key in payload:
                return payload[target_key]
            for value in payload.values():
                found = self._walk_for_key(value, target_key)
                if found is not None:
                    return found
        elif isinstance(payload, list):
            for value in payload:
                found = self._walk_for_key(value, target_key)
                if found is not None:
                    return found
        return None

    @staticmethod
    def _parse_date(value: Any) -> datetime | None:
        if value in (None, "", "None"):
            return None
        text = str(value).strip()
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return datetime.strptime(text[:19], fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def _extract_first_number(value: Any) -> float | None:
        if isinstance(value, list):
            value = value[0] if value else None
        if value in (None, "", "None"):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
