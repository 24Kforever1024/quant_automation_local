import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from requests.exceptions import Timeout

from clients.feishu_client import FeishuBitableClient


class FeishuBitableClientTests(unittest.TestCase):
    def test_list_records_retries_on_timeout_with_longer_read_timeout(self) -> None:
        client = FeishuBitableClient(
            settings=SimpleNamespace(
                feishu_app_id="app_id",
                feishu_app_secret="app_secret",
                feishu_app_token="app_token",
            )
        )
        client._tenant_access_token = "tenant_token"

        success_response = Mock()
        success_response.status_code = 200
        success_response.json.return_value = {
            "code": 0,
            "data": {
                "items": [{"record_id": "rec_1", "fields": {"code": "700.HK"}}],
                "has_more": False,
            },
        }
        success_response.raise_for_status.return_value = None

        with patch(
            "clients.feishu_client.requests.request",
            side_effect=[Timeout("timed out"), success_response],
        ) as mock_request, patch("clients.feishu_client.time.sleep") as mock_sleep:
            records = client.list_records("tbl_watchlist")

        self.assertEqual(records, [{"record_id": "rec_1", "fields": {"code": "700.HK"}}])
        self.assertEqual(mock_request.call_count, 2)
        self.assertEqual(mock_request.call_args_list[0].args[:2], ("GET", unittest.mock.ANY))
        self.assertEqual(mock_request.call_args_list[0].kwargs["timeout"], (10, 60))
        mock_sleep.assert_called_once_with(1.0)

    def test_list_records_raises_clear_error_after_retry_exhausted(self) -> None:
        client = FeishuBitableClient(
            settings=SimpleNamespace(
                feishu_app_id="app_id",
                feishu_app_secret="app_secret",
                feishu_app_token="app_token",
            )
        )
        client._tenant_access_token = "tenant_token"

        with patch(
            "clients.feishu_client.requests.request",
            side_effect=Timeout("timed out"),
        ), patch("clients.feishu_client.time.sleep"):
            with self.assertRaises(RuntimeError) as context:
                client.list_records("tbl_watchlist")

        self.assertIn("list_records", str(context.exception))
        self.assertIn("after 4 attempts", str(context.exception))


if __name__ == "__main__":
    unittest.main()
