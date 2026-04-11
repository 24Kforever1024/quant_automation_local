from __future__ import annotations

from datetime import date

from clients.feishu_client import FeishuBitableClient
from clients.ifind_client import IFindDataPoolClient
from utils.periods import infer_market_from_code, normalize_market_label


class PriceSyncService:
    status_field_name = "价格初始化状态"
    status_retry = "重新拉取"
    status_processing = "处理中"
    status_done = "完成"
    status_failed = "失败"

    def __init__(self) -> None:
        self.feishu = FeishuBitableClient()
        self.ifind = IFindDataPoolClient(self.feishu.settings)

    def run(self, market_filter: str | None = None) -> None:
        records = self.feishu.list_records(self.feishu.settings.feishu_table_id)
        updates: list[dict] = []
        normalized_market_filter = self._normalize_market_filter(market_filter)
        for item in records:
            if normalized_market_filter and not self._matches_market_filter(item, normalized_market_filter):
                continue
            update = self.build_update_for_item(item)
            if update is not None:
                updates.append(update)

        self.feishu.batch_update_records(self.feishu.settings.feishu_table_id, updates)
        print(f"价格同步完成，更新 {len(updates)} 条记录。")

    def run_single(self, record_id: str, expected_code: str) -> bool:
        table_id = self.feishu.settings.feishu_table_id
        normalized_expected_code = str(expected_code or "").strip().upper()
        item = self.feishu.get_record(table_id, record_id)
        fields = item.get("fields") or {}
        current_code = str(fields.get("代码") or "").strip().upper()

        if not current_code:
            print(f"当前记录代码为空，跳过: {record_id}")
            return False

        if normalized_expected_code and current_code != normalized_expected_code:
            print(
                f"事件代码已过期，跳过: {record_id} "
                f"(expected={normalized_expected_code}, current={current_code})"
            )
            return False

        self._update_status(record_id, self.status_processing)

        update = self.build_update_for_item(item, include_status=True)
        if update is None:
            self._update_status(record_id, self.status_failed)
            return False

        self.feishu.batch_update_records(table_id, [update])
        return True

    def build_update_for_item(self, item: dict, include_status: bool = False) -> dict | None:
        record_id = str(item.get("record_id") or "").strip()
        fields = item.get("fields") or {}
        raw_code = str(fields.get("代码") or "").strip().upper()
        if not record_id or not raw_code:
            return None

        try:
            price_fields = self._fetch_price_fields(raw_code)
        except Exception as exc:
            print(f"价格同步失败 {raw_code}: {exc}")
            return None

        update_fields = dict(price_fields)
        if include_status:
            update_fields[self.status_field_name] = self.status_done

        print(f"价格同步成功: {raw_code} -> {price_fields['实时股价']}")
        return {"record_id": record_id, "fields": update_fields}

    def _fetch_price_fields(self, raw_code: str) -> dict[str, float]:
        market = infer_market_from_code(raw_code)
        if market == "美股":
            return self._fetch_us_price_fields(raw_code)

        quote = self.ifind.get_realtime_quote(raw_code)
        return {
            "实时股价": quote["latest"],
            "涨跌幅": quote["changeRatio"] / 100,
            "总市值": round(quote["totalCapital"] / 100000000, 2),
        }

    def _fetch_us_price_fields(self, raw_code: str) -> dict[str, float]:
        quote = self.ifind.get_realtime_quote_without_market_cap(raw_code)
        total_shares = self.ifind.get_total_shares(raw_code, as_of_date=date.today())
        market_capital = quote["latest"] * total_shares
        return {
            "实时股价": quote["latest"],
            "涨跌幅": quote["changeRatio"] / 100,
            "总市值": round(market_capital / 100000000, 2),
        }

    def _update_status(self, record_id: str, status: str) -> None:
        self.feishu.batch_update_records(
            self.feishu.settings.feishu_table_id,
            [{"record_id": record_id, "fields": {self.status_field_name: status}}],
        )

    @staticmethod
    def _normalize_market_filter(market_filter: str | None) -> str:
        normalized = normalize_market_label(market_filter)
        return normalized or str(market_filter or "").strip()

    @staticmethod
    def _matches_market_filter(item: dict, market_filter: str) -> bool:
        fields = item.get("fields") or {}
        raw_code = str(fields.get("代码") or "").strip().upper()
        market = normalize_market_label(fields.get("目标市场")) or infer_market_from_code(raw_code)
        return market == market_filter
