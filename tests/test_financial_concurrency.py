import threading
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

from data_processors.hk_watchlist import HKWatchlistProcessor
from services.financial_sync_service import FinancialSyncService, FinancialTask


class FinancialConcurrencyTests(unittest.TestCase):
    def test_hk_engine_is_thread_local(self) -> None:
        processor = HKWatchlistProcessor()
        main_engine = processor._get_engine()
        self.assertIs(main_engine, processor._get_engine())

        result: list[object] = []

        def worker() -> None:
            result.append(processor._get_engine())

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()

        self.assertEqual(len(result), 1)
        self.assertIsNot(main_engine, result[0])

    def test_run_tasks_splits_hk_and_non_hk_batches(self) -> None:
        service = FinancialSyncService()
        service.settings = SimpleNamespace(financial_sync_workers=4, non_hk_sync_workers=3, hk_sync_workers=2)
        captured_hk_codes: list[str] = []
        captured_other_batch: list[tuple[list[str], int]] = []

        def fake_run_task_batch(tasks, max_workers, log_lookup, today):
            captured_other_batch.append(([task.code for task in tasks], max_workers))
            return []

        def fake_run_hk_task_batches(tasks, log_lookup, today):
            captured_hk_codes.extend(task.code for task in tasks)
            return []

        service._run_task_batch = fake_run_task_batch  # type: ignore[method-assign]
        service._run_hk_task_batches = fake_run_hk_task_batches  # type: ignore[method-assign]

        service._run_tasks(
            [
                FinancialTask(record_id="1", code="1211.HK", market="港股"),
                FinancialTask(record_id="2", code="AAPL.O", market="美股"),
                FinancialTask(record_id="3", code="9988.HK", market="港股"),
            ],
            {},
            date(2026, 3, 16),
        )

        self.assertEqual(captured_hk_codes, ["1211.HK", "9988.HK"])
        self.assertEqual(captured_other_batch, [(["AAPL.O"], 3)])

    def test_run_hk_task_batches_uses_assigned_api_channel(self) -> None:
        service = FinancialSyncService()
        service.settings = SimpleNamespace(hk_sync_workers=2)
        seen_assignments: list[tuple[str, str]] = []

        def fake_process_task(task, log_lookup, today, hk_api_channel=None, include_status=False):
            seen_assignments.append((task.code, hk_api_channel["name"]))
            return {"record_id": task.record_id, "fields": {}}

        service._process_task = fake_process_task  # type: ignore[method-assign]

        api_channels = [{"name": "deepseek_official"}, {"name": "siliconflow"}]
        assignment_map = {
            "1211.HK": {"name": "deepseek_official"},
            "9988.HK": {"name": "siliconflow"},
        }

        with patch("services.financial_sync_service.build_hk_llm_api_channels", return_value=api_channels), patch(
            "services.financial_sync_service.build_balanced_hk_api_assignments",
            return_value=assignment_map,
        ):
            service._run_hk_task_batches(
                [
                    FinancialTask(record_id="1", code="1211.HK", market="港股"),
                    FinancialTask(record_id="2", code="9988.HK", market="港股"),
                ],
                {},
                date(2026, 3, 16),
            )

        self.assertCountEqual(
            seen_assignments,
            [("1211.HK", "deepseek_official"), ("9988.HK", "siliconflow")],
        )


if __name__ == "__main__":
    unittest.main()
