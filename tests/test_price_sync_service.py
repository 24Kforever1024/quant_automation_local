import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from services.price_sync_service import PriceSyncService


class PriceSyncServiceTests(unittest.TestCase):
    def test_run_filters_records_by_market_field(self) -> None:
        service = PriceSyncService()
        service.feishu = Mock(
            settings=SimpleNamespace(feishu_table_id="tbl_watchlist"),
            list_records=Mock(
                return_value=[
                    {"record_id": "rec_us", "fields": {"代码": "AAPL.O", "目标市场": "美股"}},
                    {"record_id": "rec_hk", "fields": {"代码": "700.HK", "目标市场": "港股"}},
                ]
            ),
            batch_update_records=Mock(),
        )

        with patch.object(
            service,
            "build_update_for_item",
            side_effect=[
                {"record_id": "rec_us", "fields": {"实时股价": 188.0}},
            ],
        ) as mock_build:
            service.run(market_filter="美股")

        mock_build.assert_called_once_with(
            {"record_id": "rec_us", "fields": {"代码": "AAPL.O", "目标市场": "美股"}}
        )
        service.feishu.batch_update_records.assert_called_once_with(
            "tbl_watchlist",
            [{"record_id": "rec_us", "fields": {"实时股价": 188.0}}],
        )

    def test_run_accepts_ascii_market_alias(self) -> None:
        service = PriceSyncService()
        service.feishu = Mock(
            settings=SimpleNamespace(feishu_table_id="tbl_watchlist"),
            list_records=Mock(
                return_value=[
                    {"record_id": "rec_us", "fields": {"代码": "AAPL.O", "目标市场": "美股"}},
                    {"record_id": "rec_hk", "fields": {"代码": "700.HK", "目标市场": "港股"}},
                ]
            ),
            batch_update_records=Mock(),
        )

        with patch.object(
            service,
            "build_update_for_item",
            return_value={"record_id": "rec_us", "fields": {"实时股价": 188.0}},
        ) as mock_build:
            service.run(market_filter="us")

        mock_build.assert_called_once_with(
            {"record_id": "rec_us", "fields": {"代码": "AAPL.O", "目标市场": "美股"}}
        )

    def test_run_single_marks_processing_then_done(self) -> None:
        service = PriceSyncService()
        service.feishu = Mock(
            settings=SimpleNamespace(feishu_table_id="tbl_watchlist"),
            get_record=Mock(return_value={"record_id": "rec_1", "fields": {"代码": "700.HK"}}),
            batch_update_records=Mock(),
        )

        with patch.object(
            service,
            "_fetch_price_fields",
            return_value={"实时股价": 123.4, "涨跌幅": 0.015, "总市值": 999.99},
        ):
            ok = service.run_single("rec_1", "700.HK")

        self.assertTrue(ok)
        self.assertEqual(
            service.feishu.batch_update_records.call_args_list,
            [
                call(
                    "tbl_watchlist",
                    [{"record_id": "rec_1", "fields": {"价格初始化状态": "处理中"}}],
                ),
                call(
                    "tbl_watchlist",
                    [
                        {
                            "record_id": "rec_1",
                            "fields": {
                                "实时股价": 123.4,
                                "涨跌幅": 0.015,
                                "总市值": 999.99,
                                "价格初始化状态": "完成",
                            },
                        }
                    ],
                ),
            ],
        )

    def test_run_single_skips_stale_event(self) -> None:
        service = PriceSyncService()
        service.feishu = Mock(
            settings=SimpleNamespace(feishu_table_id="tbl_watchlist"),
            get_record=Mock(return_value={"record_id": "rec_1", "fields": {"代码": "9988.HK"}}),
            batch_update_records=Mock(),
        )

        ok = service.run_single("rec_1", "700.HK")

        self.assertFalse(ok)
        service.feishu.batch_update_records.assert_not_called()

    def test_run_single_marks_failed_when_price_sync_errors(self) -> None:
        service = PriceSyncService()
        service.feishu = Mock(
            settings=SimpleNamespace(feishu_table_id="tbl_watchlist"),
            get_record=Mock(return_value={"record_id": "rec_1", "fields": {"代码": "700.HK"}}),
            batch_update_records=Mock(),
        )

        with patch.object(service, "_fetch_price_fields", side_effect=RuntimeError("boom")):
            ok = service.run_single("rec_1", "700.HK")

        self.assertFalse(ok)
        self.assertEqual(
            service.feishu.batch_update_records.call_args_list,
            [
                call(
                    "tbl_watchlist",
                    [{"record_id": "rec_1", "fields": {"价格初始化状态": "处理中"}}],
                ),
                call(
                    "tbl_watchlist",
                    [{"record_id": "rec_1", "fields": {"价格初始化状态": "失败"}}],
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
