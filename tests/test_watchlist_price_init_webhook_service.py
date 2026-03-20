import unittest
from types import SimpleNamespace

from services.watchlist_price_init_webhook_service import WatchlistPriceInitWebhookService


class _FakeGitHubDispatchClient:
    def __init__(self) -> None:
        self.payloads: list[dict] = []

    def dispatch(self, client_payload: dict) -> None:
        self.payloads.append(client_payload)


class WatchlistPriceInitWebhookServiceTests(unittest.TestCase):
    def test_handle_event_dispatches_repository_payload(self) -> None:
        client = _FakeGitHubDispatchClient()
        service = WatchlistPriceInitWebhookService(
            github_client=client,
            settings=SimpleNamespace(webhook_shared_secret="top-secret"),
        )

        status, body = service.handle_event(
            {
                "event": {
                    "record_id": "rec_1",
                    "code": "700.hk",
                    "event_time": "2026-03-20T09:30:00+08:00",
                }
            },
            headers={"Authorization": "Bearer top-secret"},
        )

        self.assertEqual(status, 202)
        self.assertEqual(body["code"], "700.HK")
        self.assertEqual(
            client.payloads,
            [
                {
                    "record_id": "rec_1",
                    "code": "700.HK",
                    "event_time": "2026-03-20T09:30:00+08:00",
                    "source": "feishu_watchlist",
                }
            ],
        )

    def test_handle_event_rejects_invalid_secret(self) -> None:
        service = WatchlistPriceInitWebhookService(
            github_client=_FakeGitHubDispatchClient(),
            settings=SimpleNamespace(webhook_shared_secret="top-secret"),
        )

        with self.assertRaises(PermissionError):
            service.handle_event(
                {"record_id": "rec_1", "code": "700.HK"},
                headers={"Authorization": "Bearer wrong-secret"},
            )

    def test_handle_event_requires_record_and_code(self) -> None:
        service = WatchlistPriceInitWebhookService(
            github_client=_FakeGitHubDispatchClient(),
            settings=SimpleNamespace(webhook_shared_secret=""),
        )

        with self.assertRaises(ValueError):
            service.handle_event({"record_id": "rec_1"}, headers={})


if __name__ == "__main__":
    unittest.main()
