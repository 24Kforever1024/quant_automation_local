import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from models import FinancialSyncResult
from services.financial_sync_service import FinancialSyncService


class FinancialSyncServiceTests(unittest.TestCase):
    def test_run_single_marks_processing_then_done(self) -> None:
        service = FinancialSyncService()
        service.feishu = Mock(
            settings=SimpleNamespace(feishu_table_id="tbl_watchlist", feishu_log_table_id="tbl_log"),
            get_record=Mock(return_value={"record_id": "rec_1", "fields": {"代码": "700.HK", "目标市场": "港股"}}),
            list_records=Mock(return_value=[]),
            batch_update_records=Mock(),
        )
        service.settings = service.feishu.settings

        with patch.object(
            service,
            "_dispatch",
            return_value=FinancialSyncResult(
                latest_actual_period="25Q4",
                target_period_estimate="26Q1",
                planned_disclosure_date=date(2026, 3, 31),
            ),
        ), patch.object(service, "_build_status_text", return_value="status text"):
            ok = service.run_single("rec_1", "700.HK")

        self.assertTrue(ok)
        self.assertEqual(
            service.feishu.batch_update_records.call_args_list,
            [
                call(
                    "tbl_watchlist",
                    [{"record_id": "rec_1", "fields": {"财务初始化状态": "处理中"}}],
                ),
                call(
                    "tbl_watchlist",
                    [
                        {
                            "record_id": "rec_1",
                            "fields": {
                                "最新财报季(A)": "25Q4",
                                "目标财报季(E)": "26Q1",
                                "最近业绩期盈利预测": "status text",
                                "拟披露时间": 1774886400000,
                                "财务初始化状态": "完成",
                            },
                        }
                    ],
                ),
            ],
        )

    def test_run_single_skips_stale_event(self) -> None:
        service = FinancialSyncService()
        service.feishu = Mock(
            settings=SimpleNamespace(feishu_table_id="tbl_watchlist", feishu_log_table_id="tbl_log"),
            get_record=Mock(return_value={"record_id": "rec_1", "fields": {"代码": "9988.HK"}}),
            batch_update_records=Mock(),
        )
        service.settings = service.feishu.settings

        ok = service.run_single("rec_1", "700.HK")

        self.assertFalse(ok)
        service.feishu.batch_update_records.assert_not_called()

    def test_run_single_marks_failed_when_financial_sync_errors(self) -> None:
        service = FinancialSyncService()
        service.feishu = Mock(
            settings=SimpleNamespace(feishu_table_id="tbl_watchlist", feishu_log_table_id="tbl_log"),
            get_record=Mock(return_value={"record_id": "rec_1", "fields": {"代码": "700.HK", "目标市场": "港股"}}),
            list_records=Mock(return_value=[]),
            batch_update_records=Mock(),
        )
        service.settings = service.feishu.settings

        with patch.object(service, "_dispatch", side_effect=RuntimeError("boom")):
            ok = service.run_single("rec_1", "700.HK")

        self.assertFalse(ok)
        self.assertEqual(
            service.feishu.batch_update_records.call_args_list,
            [
                call(
                    "tbl_watchlist",
                    [{"record_id": "rec_1", "fields": {"财务初始化状态": "处理中"}}],
                ),
                call(
                    "tbl_watchlist",
                    [{"record_id": "rec_1", "fields": {"财务初始化状态": "失败"}}],
                ),
            ],
        )


if __name__ == "__main__":
    unittest.main()
