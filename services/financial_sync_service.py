from __future__ import annotations

import concurrent.futures
import threading
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from clients.feishu_client import FeishuBitableClient
from clients.hk_llm_client import build_balanced_hk_api_assignments, build_hk_llm_api_channels
from data_processors.ashare_watchlist import AShareWatchlistProcessor
from data_processors.hk_watchlist import HKWatchlistProcessor
from data_processors.us_watchlist import USWatchlistProcessor
from models import FinancialSyncResult
from utils.periods import calculate_yoy_text, infer_market_from_code, normalize_market_label, to_feishu_timestamp_ms


@dataclass(frozen=True)
class FinancialTask:
    record_id: str
    code: str
    market: str


class FinancialSyncService:
    def __init__(self) -> None:
        self.feishu = FeishuBitableClient()
        self.settings = self.feishu.settings
        self.hk_processor = HKWatchlistProcessor()
        self.a_processor = AShareWatchlistProcessor()
        self.us_processor = USWatchlistProcessor()

    def run(self) -> None:
        started_at = time.perf_counter()
        main_records = self.feishu.list_records(self.settings.feishu_table_id)
        log_records = self.feishu.list_records(self.settings.feishu_log_table_id)
        log_lookup = self._build_log_lookup(log_records)
        tasks = self._build_tasks(main_records)
        updates = self._run_tasks(tasks, log_lookup, date.today())
        self.feishu.batch_update_records(self.settings.feishu_table_id, updates)
        elapsed_seconds = time.perf_counter() - started_at
        print(f"财务同步完成，更新 {len(updates)} 条记录。")

        print(f"耗时 {elapsed_seconds:.2f} 秒")

    def _build_tasks(self, main_records: list[dict]) -> list[FinancialTask]:
        tasks: list[FinancialTask] = []
        for item in main_records:
            record_id = str(item.get("record_id") or "").strip()
            fields = item.get("fields") or {}
            raw_code = str(fields.get("代码") or "").strip().upper()
            if not record_id or not raw_code:
                continue
            market = normalize_market_label(fields.get("目标市场")) or infer_market_from_code(raw_code)
            tasks.append(FinancialTask(record_id=record_id, code=raw_code, market=market))
        return tasks

    def _run_tasks(
        self,
        tasks: list[FinancialTask],
        log_lookup: dict[tuple[str, str], float],
        today: date,
    ) -> list[dict]:
        hk_tasks = [task for task in tasks if task.market == "港股"]
        other_tasks = [task for task in tasks if task.market != "港股"]

        updates: list[dict] = []
        if other_tasks:
            updates.extend(
                self._run_task_batch(
                    other_tasks,
                    max_workers=min(self.settings.financial_sync_workers, self.settings.non_hk_sync_workers),
                    log_lookup=log_lookup,
                    today=today,
                )
            )
        if hk_tasks:
            updates.extend(self._run_hk_task_batches(hk_tasks, log_lookup=log_lookup, today=today))
        return updates

    def _run_hk_task_batches(
        self,
        tasks: list[FinancialTask],
        log_lookup: dict[tuple[str, str], float],
        today: date,
    ) -> list[dict]:
        if not tasks:
            return []

        import queue
        api_channels = build_hk_llm_api_channels(self.settings)
        assignment_map = build_balanced_hk_api_assignments([task.code for task in tasks], api_channels)
        
        # Build a queue for each API channel
        queues: dict[str, queue.Queue] = {channel["name"]: queue.Queue() for channel in api_channels}
        for task in tasks:
            api_channel = assignment_map.get(task.code.upper())
            if api_channel is None:
                continue
            channel_name = api_channel["name"]
            if channel_name in queues:
                queues[channel_name].put(task)
            else:
                queues.setdefault(channel_name, queue.Queue()).put(task)

        updates: list[dict] = []
        updates_lock = threading.Lock()
        
        # Max out the thread workers strictly bounded by number of available channels or max config
        max_workers = min(
            max(2, self.settings.hk_sync_workers),
            max(1, len(api_channels)),
        )

        def _worker_loop(worker_channel: dict[str, Any]) -> None:
            worker_name = worker_channel["name"]
            channel_names = list(queues.keys())
            
            while True:
                task = None
                
                # 1. Try to fetch from affinity queue (non-blocking)
                try:
                    task = queues[worker_name].get_nowait()
                except queue.Empty:
                    # 2. If empty, try stealing from other queues (non-blocking)
                    for other_name in channel_names:
                        if other_name == worker_name:
                            continue
                        try:
                            task = queues[other_name].get_nowait()
                            break
                        except queue.Empty:
                            pass
                
                # If all queues are empty, worker is done
                if task is None:
                    break
                
                try:
                    update_dict = self._process_task(
                        task,
                        log_lookup,
                        today,
                        hk_api_channel=assignment_map.get(task.code.upper()),
                    )
                    if update_dict is not None:
                        with updates_lock:
                            updates.append(update_dict)
                finally:
                    # Mark task as done in the queue it was pulled from (though we don't strictly use join)
                    pass

        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Assing an API channel to each worker.
            # If workers > channels, loop over channels cyclically
            future_list = []
            for i in range(max_workers):
                ch = api_channels[i % len(api_channels)]
                future_list.append(executor.submit(_worker_loop, ch))
                
            concurrent.futures.wait(future_list)

        return updates

    def _run_task_batch(
        self,
        tasks: list[FinancialTask],
        max_workers: int,
        log_lookup: dict[tuple[str, str], float],
        today: date,
    ) -> list[dict]:
        if not tasks:
            return []

        updates: list[dict] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, max_workers)) as executor:
            future_map = {
                executor.submit(self._process_task, task, log_lookup, today): task
                for task in tasks
            }
            for future in concurrent.futures.as_completed(future_map):
                update = future.result()
                if update is not None:
                    updates.append(update)
        return updates

    def _process_task(
        self,
        task: FinancialTask,
        log_lookup: dict[tuple[str, str], float],
        today: date,
        hk_api_channel: Mapping[str, Any] | None = None,
    ) -> dict | None:
        try:
            result = self._dispatch(task.market, task.code, today, hk_api_channel=hk_api_channel)
            target_e = result.target_period_estimate
            estimate_profit = log_lookup.get((task.code, target_e))
            result.earnings_prediction_text = self._build_status_text(target_e, estimate_profit, result.yoy_base_profit)

            if task.market == "港股" and hk_api_channel is not None:
                print(f"✅ 财务同步成功: {task.code} [{task.market}] -> {hk_api_channel.get('name')}")
            else:
                print(f"✅ 财务同步成功: {task.code} [{task.market}]")

            return {
                "record_id": task.record_id,
                "fields": {
                    "最新财报季(A)": result.latest_actual_period or None,
                    "目标财报季(E)": result.target_period_estimate or None,
                    "最近业绩期盈利预测": result.earnings_prediction_text or None,
                    "拟披露时间": to_feishu_timestamp_ms(result.planned_disclosure_date),
                },
            }
        except Exception as exc:
            print(f"⚠️ 财务同步失败 {task.code} [{task.market}]: {exc}")
            return None

    @staticmethod
    def _build_log_lookup(log_records: list[dict]) -> dict[tuple[str, str], float]:
        lookup: dict[tuple[str, str], float] = {}
        for item in log_records:
            fields = item.get("fields") or {}
            code = str(fields.get("代码") or "").strip().upper()
            period = str(fields.get("预测财报季") or "").strip().upper()
            profit = fields.get("净利润")
            if not code or not period:
                continue
            try:
                lookup[(code, period)] = float(profit)
            except (TypeError, ValueError):
                continue
        return lookup

    def _dispatch(
        self,
        market: str,
        code: str,
        as_of_date: date,
        hk_api_channel: Mapping[str, Any] | None = None,
    ) -> FinancialSyncResult:
        if market == "港股":
            return self.hk_processor.analyze(code, as_of_date, llm_api_channel=hk_api_channel)
        if market == "A股":
            return self.a_processor.analyze(code, as_of_date)
        if market == "美股":
            return self.us_processor.analyze(code, as_of_date)
        return FinancialSyncResult(meta={"market": market or "其他", "code": code})

    @staticmethod
    def _build_status_text(target_period: str, estimate_profit: float | None, yoy_base_profit: float | None) -> str:
        if not target_period:
            return ""
        if estimate_profit is None:
            return f"[{target_period}]利润：待补充"
        if yoy_base_profit is None:
            return f"[{target_period}]利润：{estimate_profit}亿元，同比基数缺失"

        yoy_text = calculate_yoy_text(estimate_profit, yoy_base_profit)
        if "%" in yoy_text:
            return f"[{target_period}]利润：{estimate_profit}亿元，同比{yoy_text}"
        return f"[{target_period}]利润：{estimate_profit}亿元，{yoy_text}"
