from __future__ import annotations

import argparse

from services.price_sync_service import PriceSyncService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync price fields for Feishu watchlist records.")
    parser.add_argument(
        "--market",
        default="",
        help="Optional market filter, for example 美股 / 港股 / A股 / us / hk / a",
    )
    return parser

if __name__ == "__main__":
    args = build_parser().parse_args()
    PriceSyncService().run(market_filter=args.market)
