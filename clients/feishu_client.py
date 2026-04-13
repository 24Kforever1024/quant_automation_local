from __future__ import annotations

import time
from typing import Iterable

import requests
from requests import Response
from requests.exceptions import ConnectionError, HTTPError, RequestException, Timeout

from config import Settings, get_settings


class FeishuBitableClient:
    connect_timeout_seconds = 10
    read_timeout_seconds = 60
    max_retries = 3
    retry_backoff_base_seconds = 1.0
    retryable_status_codes = {429, 500, 502, 503, 504}

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._tenant_access_token: str | None = None

    def get_tenant_access_token(self) -> str:
        if self._tenant_access_token:
            return self._tenant_access_token

        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        payload = {
            "app_id": self.settings.feishu_app_id,
            "app_secret": self.settings.feishu_app_secret,
        }
        body = self._request_json(
            "POST",
            url,
            operation="get_tenant_access_token",
            json=payload,
        )
        if body.get("code") != 0:
            raise RuntimeError(f"Feishu tenant token error: {body.get('msg')}")
        self._tenant_access_token = str(body.get("tenant_access_token") or "")
        if not self._tenant_access_token:
            raise RuntimeError("Feishu tenant access token missing")
        return self._tenant_access_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.get_tenant_access_token()}",
            "Content-Type": "application/json",
        }

    def list_records(self, table_id: str, page_size: int = 500) -> list[dict]:
        items: list[dict] = []
        page_token = ""
        while True:
            url = (
                f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
                f"{self.settings.feishu_app_token}/tables/{table_id}/records?page_size={page_size}"
            )
            if page_token:
                url += f"&page_token={page_token}"

            body = self._request_json(
                "GET",
                url,
                operation=f"list_records(table_id={table_id}, page_token={page_token or 'first'})",
                headers=self._headers(),
            )
            if body.get("code") != 0:
                raise RuntimeError(f"Feishu list records error: {body.get('msg')}")

            data = body.get("data") or {}
            items.extend(data.get("items") or [])
            if not data.get("has_more"):
                break
            page_token = str(data.get("page_token") or "")
            if not page_token:
                break
        return items

    def get_record(self, table_id: str, record_id: str) -> dict:
        url = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
            f"{self.settings.feishu_app_token}/tables/{table_id}/records/{record_id}"
        )
        body = self._request_json(
            "GET",
            url,
            operation=f"get_record(table_id={table_id}, record_id={record_id})",
            headers=self._headers(),
        )
        if body.get("code") != 0:
            raise RuntimeError(f"Feishu get record error: {body.get('msg')}")

        data = body.get("data") or {}
        item = data.get("record")
        if not isinstance(item, dict):
            raise RuntimeError(f"Feishu record missing: {record_id}")
        return item

    def batch_update_records(self, table_id: str, records: Iterable[dict], batch_size: int = 100) -> None:
        buffered = list(records)
        if not buffered:
            return

        url = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
            f"{self.settings.feishu_app_token}/tables/{table_id}/records/batch_update"
        )
        for index in range(0, len(buffered), batch_size):
            chunk = buffered[index:index + batch_size]
            body = self._request_json(
                "POST",
                url,
                operation=(
                    f"batch_update_records(table_id={table_id}, "
                    f"batch={index // batch_size + 1}, size={len(chunk)})"
                ),
                headers=self._headers(),
                json={"records": chunk},
            )
            if body.get("code") != 0:
                raise RuntimeError(f"Feishu batch update error: {body.get('msg')}")

    def _request_json(self, method: str, url: str, *, operation: str, **kwargs) -> dict:
        response = self._request_with_retry(method, url, operation=operation, **kwargs)
        return response.json()

    def _request_with_retry(self, method: str, url: str, *, operation: str, **kwargs) -> Response:
        total_attempts = self.max_retries + 1
        timeout = kwargs.pop("timeout", (self.connect_timeout_seconds, self.read_timeout_seconds))
        last_error: Exception | None = None

        for attempt in range(1, total_attempts + 1):
            try:
                response = requests.request(method, url, timeout=timeout, **kwargs)
                if response.status_code in self.retryable_status_codes:
                    raise HTTPError(
                        f"Feishu HTTP {response.status_code}: {self._response_snippet(response)}",
                        response=response,
                    )
                response.raise_for_status()
                return response
            except Timeout as exc:
                last_error = exc
                if attempt >= total_attempts:
                    break
                self._log_retry(operation, attempt, total_attempts, f"timeout: {exc}")
            except ConnectionError as exc:
                last_error = exc
                if attempt >= total_attempts:
                    break
                self._log_retry(operation, attempt, total_attempts, f"connection error: {exc}")
            except HTTPError as exc:
                last_error = exc
                status_code = exc.response.status_code if exc.response is not None else "unknown"
                detail = f"HTTP {status_code}"
                if exc.response is not None:
                    detail = f"{detail}: {self._response_snippet(exc.response)}"
                if status_code not in self.retryable_status_codes or attempt >= total_attempts:
                    break
                self._log_retry(operation, attempt, total_attempts, detail)
            except RequestException as exc:
                raise RuntimeError(f"Feishu request failed for {operation}: {exc}") from exc

            delay_seconds = self.retry_backoff_base_seconds * (2 ** (attempt - 1))
            time.sleep(delay_seconds)

        detail = str(last_error) if last_error is not None else "unknown error"
        print(f"[Feishu] {operation} failed after {total_attempts} attempts: {detail}")
        raise RuntimeError(f"Feishu request failed for {operation} after {total_attempts} attempts: {detail}") from last_error

    def _log_retry(self, operation: str, attempt: int, total_attempts: int, detail: str) -> None:
        next_attempt = attempt + 1
        delay_seconds = self.retry_backoff_base_seconds * (2 ** (attempt - 1))
        print(
            f"[Feishu] {operation} failed on attempt {attempt}/{total_attempts}: "
            f"{detail}. Retrying in {delay_seconds:.1f}s (next attempt {next_attempt}/{total_attempts})."
        )

    @staticmethod
    def _response_snippet(response: Response, limit: int = 200) -> str:
        text = (response.text or "").strip().replace("\n", " ")
        if len(text) > limit:
            return text[:limit] + "..."
        return text or "<empty response>"
