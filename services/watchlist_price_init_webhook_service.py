from __future__ import annotations

from typing import Any, Mapping

from clients.github_dispatch_client import GitHubDispatchClient
from config import Settings, get_settings


class WatchlistPriceInitWebhookService:
    def __init__(
        self,
        github_client: GitHubDispatchClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.github_client = github_client or GitHubDispatchClient(self.settings)

    def handle_event(self, payload: Mapping[str, Any], headers: Mapping[str, str] | None = None) -> tuple[int, dict]:
        self._assert_authorized(payload, headers or {})

        event = self._extract_event_payload(payload)
        record_id = str(event.get("record_id") or "").strip()
        code = str(event.get("code") or "").strip().upper()
        event_time = str(event.get("event_time") or "").strip()
        source = str(event.get("source") or "feishu_watchlist").strip() or "feishu_watchlist"
        if not record_id or not code:
            raise ValueError("record_id and code are required")

        dispatch_payload = {
            "record_id": record_id,
            "code": code,
            "event_time": event_time,
            "source": source,
        }
        self.github_client.dispatch(dispatch_payload)
        return 202, {"ok": True, "dispatched": True, "record_id": record_id, "code": code}

    def _assert_authorized(self, payload: Mapping[str, Any], headers: Mapping[str, str]) -> None:
        expected_secret = self.settings.webhook_shared_secret
        if not expected_secret:
            return

        provided_secret = self._extract_secret(payload, headers)
        if provided_secret != expected_secret:
            raise PermissionError("invalid webhook secret")

    @staticmethod
    def _extract_event_payload(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        event = payload.get("event")
        if isinstance(event, Mapping):
            return event
        return payload

    @staticmethod
    def _extract_secret(payload: Mapping[str, Any], headers: Mapping[str, str]) -> str:
        authorization = str(headers.get("Authorization") or "").strip()
        if authorization.lower().startswith("bearer "):
            return authorization[7:].strip()

        for header_name in ("X-Webhook-Token", "X-Feishu-Webhook-Secret"):
            token = str(headers.get(header_name) or "").strip()
            if token:
                return token

        return str(payload.get("secret") or "").strip()
