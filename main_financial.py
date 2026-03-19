import sys
sys.stdout.reconfigure(encoding='utf-8')
from services.financial_sync_service import FinancialSyncService


if __name__ == "__main__":
    FinancialSyncService().run()
