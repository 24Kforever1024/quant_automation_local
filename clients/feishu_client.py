from __future__ import annotations

from typing import Iterable

import requests

from config import Settings, get_settings


class FeishuBitableClient:
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
        response = requests.post(url, json=payload, timeout=20)
        response.raise_for_status()
        body = response.json()
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

            response = requests.get(url, headers=self._headers(), timeout=30)
            response.raise_for_status()
            body = response.json()
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
        response = requests.get(url, headers=self._headers(), timeout=30)
        response.raise_for_status()
        body = response.json()
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
            response = requests.post(
                url,
                headers=self._headers(),
                json={"records": chunk},
                timeout=30,
            )
            response.raise_for_status()
            body = response.json()
            if body.get("code") != 0:
                raise RuntimeError(f"Feishu batch update error: {body.get('msg')}")
