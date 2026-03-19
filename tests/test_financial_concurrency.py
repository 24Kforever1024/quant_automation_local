import threading
import unittest
from datetime import date

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

    def test_hk_tasks_are_balanced_into_two_api_queues(self) -> None:
        service = FinancialSyncService()
        captured_batches: list[list[str]] = []
        captured_other_batch: list[tuple[int, int]] = []

        def fake_run_task_batch(tasks, max_workers, log_lookup, today):
            captured_other_batch.append((len(tasks), max_workers))
            return []

        def fake_run_hk_api_channel_queue(tasks, assignment_map, log_lookup, today):
            captured_batches.append([f"{task.code}:{assignment_map[task.code]['name']}" for task in tasks])
            return []

        service._run_task_batch = fake_run_task_batch  # type: ignore[method-assign]
        service._run_hk_api_channel_queue = fake_run_hk_api_channel_queue  # type: ignore[method-assign]
        service._run_tasks(
            [
                FinancialTask(record_id="1", code="1211.HK", market="港股"),
                FinancialTask(record_id="2", code="AAPL.O", market="美股"),
                FinancialTask(record_id="3", code="9988.HK", market="港股"),
            ],
            {},
            date(2026, 3, 16),
        )

        self.assertEqual(captured_other_batch, [(1, min(service.settings.financial_sync_workers, service.settings.non_hk_sync_workers))])
        self.assertEqual(len(captured_batches), 2)

        flat_batches = [item for batch in captured_batches for item in batch]
        self.assertIn("1211.HK:deepseek_official", flat_batches)
        self.assertIn("9988.HK:siliconflow", flat_batches)


if __name__ == "__main__":
    unittest.main()
