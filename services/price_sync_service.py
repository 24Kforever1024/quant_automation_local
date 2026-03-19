from __future__ import annotations

from clients.feishu_client import FeishuBitableClient
from utils.periods import normalize_xueqiu_symbol


class PriceSyncService:
    def __init__(self) -> None:
        self.feishu = FeishuBitableClient()

    def run(self) -> None:
        import akshare as ak

        records = self.feishu.list_records(self.feishu.settings.feishu_table_id)
        updates: list[dict] = []
        for item in records:
            record_id = item.get("record_id")
            fields = item.get("fields") or {}
            raw_code = str(fields.get("代码") or "").strip().upper()
            if not raw_code:
                continue

            symbol = normalize_xueqiu_symbol(raw_code)
            if not symbol:
                print(f"⏭️ 忽略无效代码: {raw_code}")
                continue

            try:
                df = ak.stock_individual_spot_xq(symbol=symbol)
                price = self._extract_metric(df, "现价")
                change_percent = self._extract_metric(df, "涨幅")
                market_cap = self._extract_market_cap(df)
                if price is None or change_percent is None or market_cap is None:
                    print(f"⚠️ 指标缺失，跳过: {raw_code}")
                    continue

                updates.append(
                    {
                        "record_id": record_id,
                        "fields": {
                            "实时股价": price,
                            "涨跌幅": change_percent / 100,
                            "总市值": round(market_cap / 100000000, 2),
                        },
                    }
                )
                print(f"✅ 价格同步成功: {raw_code} -> {price}")
            except Exception as exc:
                print(f"⚠️ 价格同步失败 {raw_code}: {exc}")

        self.feishu.batch_update_records(self.feishu.settings.feishu_table_id, updates)
        print(f"🚀 价格同步完成，更新 {len(updates)} 条记录。")

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
