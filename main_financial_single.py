from __future__ import annotations

import argparse

from services.financial_sync_service import FinancialSyncService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Sync financial fields for one Feishu watchlist record.")
    parser.add_argument("--record-id", required=True, help="Feishu record_id to sync")
    parser.add_argument("--code", required=True, help="Expected stock code for stale-event protection")
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    service = FinancialSyncService()
    ok = service.run_single(record_id=args.record_id, expected_code=args.code)
    raise SystemExit(0 if ok else 1)
