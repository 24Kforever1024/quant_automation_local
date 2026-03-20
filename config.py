from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


if load_dotenv is not None:
    load_dotenv()


def _env(key: str, default: str = "") -> str:
    value = os.getenv(key, default)
    return value.strip() if isinstance(value, str) else value


def _env_alias(*keys: str, default: str = "") -> str:
    for key in keys:
        value = os.getenv(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default


def _to_bool(value: str | bool | None, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _env_bool_alias(*keys: str, default: bool) -> bool:
    for key in keys:
        value = os.getenv(key)
        if value is not None and str(value).strip():
            return _to_bool(value, default)
    return default


def _env_float(key: str, default: float) -> float:
    value = _env(key, str(default))
    try:
        return max(0.1, float(value))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    feishu_app_id: str
    feishu_app_secret: str
    feishu_app_token: str
    feishu_table_id: str
    feishu_log_table_id: str
    ifind_access_token: str
    ifind_refresh_token: str
    volcengine_enabled: bool
    volcengine_api_key: str
    volcengine_base_url: str
    volcengine_model: str
    siliconflow_enabled: bool
    siliconflow_api_key: str
    siliconflow_base_url: str
    siliconflow_model: str
    hk_llm_timeout_seconds: float
    hk_llm_max_retries: int
    hk_llm_backoff_base_seconds: float
    hk_stock_pool_excel: str
    hk_stock_pool_code_column: str
    financial_sync_workers: int
    hk_sync_workers: int
    non_hk_sync_workers: int
    github_dispatch_token: str
    github_repository_owner: str
    github_repository_name: str
    github_dispatch_event_type: str
    webhook_shared_secret: str
    webhook_host: str
    webhook_port: int


def _env_int(key: str, default: int) -> int:
    value = _env(key, str(default))
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(
        feishu_app_id=_env("FEISHU_APP_ID"),
        feishu_app_secret=_env("FEISHU_APP_SECRET"),
        feishu_app_token=_env("FEISHU_APP_TOKEN"),
        feishu_table_id=_env("FEISHU_TABLE_ID"),
        feishu_log_table_id=_env("FEISHU_LOG_TABLE_ID"),
        ifind_access_token=_env("IFIND_ACCESS_TOKEN"),
        ifind_refresh_token=_env("IFIND_REFRESH_TOKEN"),
        volcengine_enabled=_env_bool_alias("VOLCENGINE_ENABLED", "ARK_ENABLED", default=True),
        volcengine_api_key=_env_alias("VOLCENGINE_API_KEY", "ARK_API_KEY"),
        volcengine_base_url=_env_alias(
            "VOLCENGINE_BASE_URL",
            "ARK_BASE_URL",
            default="https://ark.cn-beijing.volces.com/api/v3",
        ),
        volcengine_model=_env_alias(
            "VOLCENGINE_MODEL",
            "ARK_MODEL",
            default="deepseek-r1-250528",
        ),
        siliconflow_enabled=_env_bool_alias("SILICONFLOW_ENABLED", default=True),
        siliconflow_api_key=_env("SILICONFLOW_API_KEY"),
        siliconflow_base_url=_env("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
        siliconflow_model=_env("SILICONFLOW_MODEL", "deepseek-ai/DeepSeek-V3.2"),
        hk_llm_timeout_seconds=_env_float("HK_LLM_TIMEOUT_SECONDS", 120.0),
        hk_llm_max_retries=_env_int("HK_LLM_MAX_RETRIES", 5),
        hk_llm_backoff_base_seconds=_env_float("HK_LLM_BACKOFF_BASE_SECONDS", 1.0),
        hk_stock_pool_excel=_env("HK_STOCK_POOL_EXCEL", "data/hk_stock_pool.xlsx"),
        hk_stock_pool_code_column=_env("HK_STOCK_POOL_CODE_COLUMN", "code"),
        financial_sync_workers=_env_int("FINANCIAL_SYNC_WORKERS", 4),
        hk_sync_workers=_env_int("HK_SYNC_WORKERS", 2),
        non_hk_sync_workers=_env_int("NON_HK_SYNC_WORKERS", 4),
        github_dispatch_token=_env("GITHUB_DISPATCH_TOKEN"),
        github_repository_owner=_env("GITHUB_REPOSITORY_OWNER"),
        github_repository_name=_env("GITHUB_REPOSITORY_NAME"),
        github_dispatch_event_type=_env("GITHUB_DISPATCH_EVENT_TYPE", "watchlist_price_init"),
        webhook_shared_secret=_env("WEBHOOK_SHARED_SECRET"),
        webhook_host=_env("WEBHOOK_HOST", "0.0.0.0"),
        webhook_port=_env_int("WEBHOOK_PORT", 8787),
    )
