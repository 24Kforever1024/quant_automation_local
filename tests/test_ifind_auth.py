import unittest
from unittest.mock import MagicMock, patch

import requests

from clients.ifind_client import IFindDataPoolClient
from config import Settings
from data_processors import hk_watchlist_engine as hk_engine


class IFindAuthTests(unittest.TestCase):
    def test_data_pool_retries_after_401(self) -> None:
        settings = Settings(
            feishu_app_id="",
            feishu_app_secret="",
            feishu_app_token="",
            feishu_table_id="",
            feishu_log_table_id="",
            ifind_access_token="stale-token",
            ifind_refresh_token="refresh-token",
            volcengine_enabled=True,
            volcengine_api_key="",
            volcengine_base_url="https://ark.cn-beijing.volces.com/api/v3",
            volcengine_model="deepseek-r1-250528",
            siliconflow_enabled=True,
            siliconflow_api_key="",
            siliconflow_base_url="https://api.siliconflow.cn/v1",
            siliconflow_model="deepseek-ai/DeepSeek-V3.2",
            hk_llm_timeout_seconds=120.0,
            hk_llm_max_retries=5,
            hk_llm_backoff_base_seconds=1.0,
            hk_stock_pool_excel="data/hk_stock_pool.xlsx",
            hk_stock_pool_code_column="code",
            financial_sync_workers=4,
            hk_sync_workers=1,
            non_hk_sync_workers=4,
            github_dispatch_token="",
            github_repository_owner="",
            github_repository_name="",
            github_dispatch_event_type="watchlist_price_init",
            webhook_shared_secret="",
            webhook_host="0.0.0.0",
            webhook_port=8787,
        )
        client = IFindDataPoolClient(settings)

        refresh_response = MagicMock()
        refresh_response.raise_for_status.return_value = None
        refresh_response.json.return_value = {"errorcode": 0, "data": {"access_token": "fresh-token"}}

        unauthorized_response = MagicMock(status_code=401)
        unauthorized_response.raise_for_status.side_effect = requests.HTTPError(response=unauthorized_response)
        unauthorized_response.json.return_value = {}

        success_response = MagicMock(status_code=200)
        success_response.raise_for_status.return_value = None
        success_response.json.return_value = {"tables": [{"table": {"p00210_f001": ["2026-03-30"]}}]}

        with patch("clients.ifind_client.requests.post", side_effect=[unauthorized_response, refresh_response, success_response]) as mock_post:
            result = client.get_scheduled_disclosure_date("600000.SH", "25Q4E")

        self.assertIsNotNone(result)
        self.assertEqual(client._access_token, "fresh-token")
        self.assertIn("/update_access_token", mock_post.call_args_list[1].args[0])

    def test_hk_engine_report_query_retries_after_401(self) -> None:
        refresh_response = MagicMock()
        refresh_response.raise_for_status.return_value = None
        refresh_response.json.return_value = {"errorcode": 0, "data": {"access_token": "fresh-token"}}

        unauthorized_response = MagicMock(status_code=401)
        unauthorized_response.raise_for_status.side_effect = hk_engine.requests.HTTPError(response=unauthorized_response)
        unauthorized_response.json.return_value = {}

        success_response = MagicMock(status_code=200)
        success_response.raise_for_status.return_value = None
        success_response.json.return_value = {"errorcode": 0, "tables": []}

        hk_engine.ACCESS_TOKEN = "stale-token"
        hk_engine.REFRESH_TOKEN = "refresh-token"
        hk_engine.USE_REFRESH_TO_GET_ACCESS = False
        hk_engine._RUNTIME_ACCESS_TOKEN = None

        with patch("data_processors.hk_watchlist_engine.requests.post", side_effect=[unauthorized_response, refresh_response, success_response]) as mock_post:
            payload = hk_engine.report_query("stale-token", "1211.HK", "2024-01-01", "2024-12-31")

        self.assertEqual(payload.get("errorcode"), 0)
        self.assertEqual(hk_engine._RUNTIME_ACCESS_TOKEN, "fresh-token")
        self.assertIn("/update_access_token", mock_post.call_args_list[1].args[0])


if __name__ == "__main__":
    unittest.main()
