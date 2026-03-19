import hashlib
import os
import unittest
from unittest.mock import AsyncMock, patch

import pandas as pd

from clients.hk_llm_client import (
    build_balanced_hk_api_assignments,
    build_hk_llm_api_channels,
    load_latest_hk_stock_pool_stub,
    post_chat_completion,
    route_api_by_stock_code,
)
from config import Settings, get_settings


class _FakeResponse:
    def __init__(self, status: int, text: str, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._text = text
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def read(self) -> bytes:
        return self._text.encode("utf-8")


class _FakeSession:
    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses

    def post(self, *args, **kwargs):
        if not self._responses:
            raise AssertionError("test response queue is empty")
        return self._responses.pop(0)


class HKLLMRouterTests(unittest.TestCase):
    def test_route_api_by_stock_code_uses_md5_and_is_stable(self) -> None:
        api_channels = [
            {"name": "volcengine"},
            {"name": "siliconflow"},
        ]
        stock_code = "00700.HK"
        expected_index = int(hashlib.md5(stock_code.encode("utf-8")).hexdigest(), 16) % len(api_channels)

        with patch("builtins.hash", side_effect=AssertionError("built-in hash() should not be used")):
            first_route = route_api_by_stock_code(stock_code, api_channels)
            second_route = route_api_by_stock_code(stock_code, api_channels)

        self.assertEqual(first_route["name"], api_channels[expected_index]["name"])
        self.assertEqual(first_route, second_route)

    def test_load_latest_hk_stock_pool_stub_reads_excel_and_attaches_channel(self) -> None:
        api_channels = [
            {"name": "volcengine"},
            {"name": "siliconflow"},
        ]
        fake_df = pd.DataFrame(
            {
                "code": ["00700.hk", " 09988.HK ", None, ""],
                "name": ["Tencent", "Alibaba", "Invalid", "Blank"],
            }
        )

        with patch("clients.hk_llm_client.pd.read_excel", return_value=fake_df) as mock_read_excel:
            result = load_latest_hk_stock_pool_stub(
                excel_path="data/hk_stock_pool.xlsx",
                code_column="code",
                api_channels=api_channels,
            )

        mock_read_excel.assert_called_once()
        self.assertEqual(result["code"].tolist(), ["00700.HK", "09988.HK"])
        self.assertIn("llm_channel", result.columns)
        self.assertEqual(len(result), 2)

    def test_build_balanced_hk_api_assignments_balances_current_stock_pool(self) -> None:
        api_channels = [
            {"name": "volcengine"},
            {"name": "siliconflow"},
        ]

        result = build_balanced_hk_api_assignments(
            ["9988.HK", "1211.HK", "00700.HK", "01810.HK"],
            api_channels,
        )

        assigned_names = [result[code]["name"] for code in ["9988.HK", "1211.HK", "00700.HK", "01810.HK"]]
        self.assertEqual(assigned_names.count("volcengine"), 2)
        self.assertEqual(assigned_names.count("siliconflow"), 2)
        self.assertEqual(len(result), 4)

    def test_build_hk_llm_api_channels_allows_single_configured_provider(self) -> None:
        settings = Settings(
            feishu_app_id="",
            feishu_app_secret="",
            feishu_app_token="",
            feishu_table_id="",
            feishu_log_table_id="",
            ifind_access_token="",
            ifind_refresh_token="",
            volcengine_enabled=True,
            volcengine_api_key="ark-test-key",
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
            hk_sync_workers=2,
            non_hk_sync_workers=4,
        )

        channels = build_hk_llm_api_channels(settings)

        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]["name"], "volcengine")

    def test_build_hk_llm_api_channels_respects_enabled_flags(self) -> None:
        settings = Settings(
            feishu_app_id="",
            feishu_app_secret="",
            feishu_app_token="",
            feishu_table_id="",
            feishu_log_table_id="",
            ifind_access_token="",
            ifind_refresh_token="",
            volcengine_enabled=False,
            volcengine_api_key="ark-test-key",
            volcengine_base_url="https://ark.cn-beijing.volces.com/api/v3",
            volcengine_model="deepseek-r1-250528",
            siliconflow_enabled=True,
            siliconflow_api_key="sf-test-key",
            siliconflow_base_url="https://api.siliconflow.cn/v1",
            siliconflow_model="deepseek-ai/DeepSeek-V3.2",
            hk_llm_timeout_seconds=120.0,
            hk_llm_max_retries=5,
            hk_llm_backoff_base_seconds=1.0,
            hk_stock_pool_excel="data/hk_stock_pool.xlsx",
            hk_stock_pool_code_column="code",
            financial_sync_workers=4,
            hk_sync_workers=2,
            non_hk_sync_workers=4,
        )

        channels = build_hk_llm_api_channels(settings)

        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0]["name"], "siliconflow")

    def test_get_settings_supports_ark_env_aliases(self) -> None:
        with patch.dict(
            os.environ,
            {
                "ARK_ENABLED": "false",
                "ARK_API_KEY": "ark-key",
                "ARK_BASE_URL": "https://ark.cn-beijing.volces.com/api/v3",
                "ARK_MODEL": "deepseek-r1-250528",
            },
            clear=True,
        ):
            get_settings.cache_clear()
            try:
                settings = get_settings()
            finally:
                get_settings.cache_clear()

        self.assertFalse(settings.volcengine_enabled)
        self.assertEqual(settings.volcengine_api_key, "ark-key")
        self.assertEqual(settings.volcengine_base_url, "https://ark.cn-beijing.volces.com/api/v3")
        self.assertEqual(settings.volcengine_model, "deepseek-r1-250528")


class HKLLMRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_chat_completion_retries_on_429_with_backoff_and_jitter(self) -> None:
        api_channel = {
            "name": "siliconflow",
            "api_key": "test-key",
            "model": "deepseek-ai/DeepSeek-V3.2",
            "timeout_seconds": 30,
            "max_retries": 3,
            "backoff_base_seconds": 1,
            "chat_completions_url": "https://api.siliconflow.cn/v1/chat/completions",
        }
        fake_session = _FakeSession(
            responses=[
                _FakeResponse(status=429, text='{"error":"busy"}', headers={"Retry-After": "2"}),
                _FakeResponse(
                    status=200,
                    text='{"choices":[{"message":{"content":"ok"}}],"usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}',
                ),
            ]
        )

        with patch("clients.hk_llm_client.random.uniform", return_value=0.0), patch(
            "clients.hk_llm_client.asyncio.sleep",
            new=AsyncMock(),
        ) as mock_sleep:
            result = await post_chat_completion(
                session=fake_session,
                api_channel=api_channel,
                messages=[{"role": "user", "content": "test"}],
            )

        self.assertEqual(result["content"], "ok")
        mock_sleep.assert_awaited_once_with(2.0)

    async def test_post_chat_completion_surfaces_4xx_response_body(self) -> None:
        api_channel = {
            "name": "volcengine",
            "api_key": "test-key",
            "model": "deepseek-r1-250528",
            "timeout_seconds": 30,
            "max_retries": 3,
            "backoff_base_seconds": 1,
            "chat_completions_url": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        }
        fake_session = _FakeSession(
            responses=[
                _FakeResponse(status=400, text='{"error":{"message":"invalid model"}}'),
            ]
        )

        with self.assertRaisesRegex(RuntimeError, "invalid model"):
            await post_chat_completion(
                session=fake_session,
                api_channel=api_channel,
                messages=[{"role": "user", "content": "test"}],
            )


if __name__ == "__main__":
    unittest.main()
