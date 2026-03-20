from __future__ import annotations

from clients.feishu_client import FeishuBitableClient
from utils.periods import normalize_xueqiu_symbol


class PriceSyncService:
    status_field_name = "价格初始化状态"
    status_done = "完成"
    status_failed = "失败"

    def __init__(self) -> None:
        self.feishu = FeishuBitableClient()

    def run(self) -> None:
        records = self.feishu.list_records(self.feishu.settings.feishu_table_id)
        updates: list[dict] = []
        for item in records:
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

        update = self.build_update_for_item(item, include_status=True)
        if update is None:
            self.feishu.batch_update_records(
                table_id,
                [
                    {
                        "record_id": record_id,
                        "fields": {self.status_field_name: self.status_failed},
                    }
                ],
            )
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
        import akshare as ak

        symbol = normalize_xueqiu_symbol(raw_code)
        if not symbol:
            raise RuntimeError(f"无效代码: {raw_code}")

        df = ak.stock_individual_spot_xq(symbol=symbol)
        price = self._extract_metric(df, "现价")
        change_percent = self._extract_metric(df, "涨幅")
        market_cap = self._extract_market_cap(df)
        if price is None or change_percent is None or market_cap is None:
            raise RuntimeError(f"指标缺失: {raw_code}")

        return {
            "实时股价": price,
            "涨跌幅": change_percent / 100,
            "总市值": round(market_cap / 100000000, 2),
        }

    @staticmethod
    def _extract_metric(df, item_name: str) -> float | None:
        try:
            return float(df[df["item"] == item_name]["value"].values[0])
        except Exception:
            return None

    def _extract_market_cap(self, df) -> float | None:
        for item_name in ("资产净值/总市值", "总市值", "总市值(元)"):
            value = self._extract_metric(df, item_name)
            if value is not None:
                return value
        return None
