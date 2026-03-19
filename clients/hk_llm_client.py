from __future__ import annotations

import asyncio
import hashlib
import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import aiohttp
import pandas as pd

from config import Settings, get_settings

DEFAULT_MAX_TOKENS = 32768


@dataclass(frozen=True)
class HKLLMApiChannel:
    name: str
    provider: str
    api_key: str
    base_url: str
    model: str
    timeout_seconds: float = 120.0
    max_retries: int = 5
    backoff_base_seconds: float = 1.0

    @property
    def chat_completions_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/chat/completions"

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "provider": self.provider,
            "api_key": self.api_key,
            "base_url": self.base_url.rstrip("/"),
            "model": self.model,
            "timeout_seconds": float(self.timeout_seconds),
            "max_retries": int(self.max_retries),
            "backoff_base_seconds": float(self.backoff_base_seconds),
            "chat_completions_url": self.chat_completions_url,
        }


def _normalize_stock_code(stock_code: str) -> str:
    return str(stock_code or "").strip().upper()


def _md5_hash_int(value: str) -> int:
    normalized_value = str(value or "").strip().upper()
    if not normalized_value:
        raise ValueError("hash input cannot be empty")
    return int(hashlib.md5(normalized_value.encode("utf-8")).hexdigest(), 16)


def build_hk_llm_api_channels(settings: Settings | None = None) -> list[dict[str, Any]]:
    cfg = settings or get_settings()
    candidate_channels = [
        HKLLMApiChannel(
            name="volcengine",
            provider="volcengine",
            api_key=cfg.volcengine_api_key,
            base_url=cfg.volcengine_base_url,
            model=cfg.volcengine_model,
            timeout_seconds=cfg.hk_llm_timeout_seconds,
            max_retries=cfg.hk_llm_max_retries,
            backoff_base_seconds=cfg.hk_llm_backoff_base_seconds,
        ),
        HKLLMApiChannel(
            name="siliconflow",
            provider="siliconflow",
            api_key=cfg.siliconflow_api_key,
            base_url=cfg.siliconflow_base_url,
            model=cfg.siliconflow_model,
            timeout_seconds=cfg.hk_llm_timeout_seconds,
            max_retries=cfg.hk_llm_max_retries,
            backoff_base_seconds=cfg.hk_llm_backoff_base_seconds,
        ),
    ]

    enabled_providers = {
        "volcengine": cfg.volcengine_enabled,
        "siliconflow": cfg.siliconflow_enabled,
    }
    channels = [
        channel
        for channel in candidate_channels
        if enabled_providers.get(channel.provider, True) and channel.api_key
    ]
    if not channels:
        raise RuntimeError(
            "No HK LLM channel is enabled and configured. Check `VOLCENGINE_ENABLED` / "
            "`SILICONFLOW_ENABLED` and the corresponding API keys."
        )

    return [channel.to_api_dict() for channel in channels]


def route_api_by_stock_code(stock_code: str, api_channels: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    normalized_code = _normalize_stock_code(stock_code)
    if not normalized_code:
        raise ValueError("stock_code cannot be empty")
    if not api_channels:
        raise ValueError("api_channels cannot be empty")

    route_index = _md5_hash_int(normalized_code) % len(api_channels)
    return dict(api_channels[route_index])


def build_balanced_hk_api_assignments(
    stock_codes: Sequence[str],
    api_channels: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not api_channels:
        raise ValueError("api_channels cannot be empty")

    normalized_codes: list[str] = []
    seen: set[str] = set()
    for stock_code in stock_codes:
        normalized_code = _normalize_stock_code(stock_code)
        if not normalized_code or normalized_code in seen:
            continue
        seen.add(normalized_code)
        normalized_codes.append(normalized_code)

    sorted_codes = sorted(normalized_codes, key=lambda code: (_md5_hash_int(code), code))
    channel_list = [dict(channel) for channel in api_channels]
    channel_loads = {str(channel["name"]): 0 for channel in channel_list}
    assignment_map: dict[str, dict[str, Any]] = {}

    for stock_code in sorted_codes:
        ranked_channels = sorted(
            channel_list,
            key=lambda channel: (
                channel_loads[str(channel["name"])],
                -_md5_hash_int(f"{stock_code}::{channel['name']}"),
                str(channel["name"]),
            ),
        )
        assigned_channel = dict(ranked_channels[0])
        assignment_map[stock_code] = assigned_channel
        channel_loads[str(assigned_channel["name"])] += 1

    return assignment_map


def load_latest_hk_stock_pool_stub(
    excel_path: str | Path,
    code_column: str = "code",
    sheet_name: str | int = 0,
    api_channels: Sequence[Mapping[str, Any]] | None = None,
) -> pd.DataFrame:
    excel_file = Path(excel_path).expanduser()
    df = pd.read_excel(excel_file, sheet_name=sheet_name)
    if code_column not in df.columns:
        raise ValueError(f"Excel does not contain stock code column: {code_column}")

    stock_pool = df.copy()
    stock_pool[code_column] = stock_pool[code_column].fillna("").astype(str).str.strip().str.upper()
    stock_pool = stock_pool[stock_pool[code_column] != ""].reset_index(drop=True)

    if api_channels:
        assignment_map = build_balanced_hk_api_assignments(stock_pool[code_column].tolist(), api_channels)
        stock_pool["llm_channel"] = stock_pool[code_column].map(
            lambda stock_code: assignment_map.get(_normalize_stock_code(stock_code), {}).get("name", "")
        )

    return stock_pool


def _build_backoff_delay(
    attempt: int,
    base_seconds: float,
    retry_after_seconds: float | None = None,
) -> float:
    exponential_delay = float(base_seconds) * (2 ** max(0, attempt))
    jitter = random.uniform(0, float(base_seconds))
    if retry_after_seconds is not None:
        return max(float(retry_after_seconds), exponential_delay) + jitter
    return exponential_delay + jitter


def _parse_retry_after_seconds(header_value: str | None) -> float | None:
    if not header_value:
        return None
    try:
        return max(0.0, float(header_value))
    except (TypeError, ValueError):
        return None


def _extract_message_content(response_payload: Mapping[str, Any]) -> str:
    choices = response_payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("LLM response is missing choices")

    message = choices[0].get("message") if isinstance(choices[0], Mapping) else None
    content = message.get("content") if isinstance(message, Mapping) else ""
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            if isinstance(item, Mapping) and item.get("type") == "text":
                text_parts.append(str(item.get("text", "")))
        return "".join(text_parts).strip()
    return str(content or "").strip()


async def post_chat_completion(
    session: aiohttp.ClientSession,
    api_channel: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    temperature: float = 0.0,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> dict[str, Any]:
    timeout = aiohttp.ClientTimeout(total=float(api_channel.get("timeout_seconds", 120)))
    max_retries = max(1, int(api_channel.get("max_retries", 5)))
    backoff_base_seconds = float(api_channel.get("backoff_base_seconds", 1.0))
    effective_max_tokens = max(1, min(int(max_tokens), DEFAULT_MAX_TOKENS))
    request_body = {
        "model": api_channel["model"],
        "messages": list(messages),
        "temperature": temperature,
        "max_tokens": effective_max_tokens,
    }
    request_headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Accept-Encoding": "gzip, deflate",
        "Authorization": f"Bearer {api_channel['api_key']}",
    }
    request_url = str(api_channel["chat_completions_url"])

    last_error: Exception | None = None
    for attempt in range(max_retries):
        try:
            async with session.post(
                request_url,
                json=request_body,
                headers=request_headers,
                timeout=timeout,
            ) as response:
                response_bytes = await response.read()
                response_text = response_bytes.decode("utf-8", errors="replace")

                if response.status == 429:
                    last_error = RuntimeError(
                        f"{api_channel['name']} returned HTTP 429 Too Many Requests: {response_text}"
                    )
                    if attempt >= max_retries - 1:
                        break
                    retry_after = _parse_retry_after_seconds(response.headers.get("Retry-After"))
                    await asyncio.sleep(_build_backoff_delay(attempt, backoff_base_seconds, retry_after))
                    continue

                if response.status >= 500:
                    last_error = RuntimeError(
                        f"{api_channel['name']} returned HTTP {response.status}: {response_text}"
                    )
                    if attempt >= max_retries - 1:
                        break
                    await asyncio.sleep(_build_backoff_delay(attempt, backoff_base_seconds))
                    continue

                if response.status >= 400:
                    raise RuntimeError(
                        f"{api_channel['name']} returned HTTP {response.status}: {response_text}"
                    )

                try:
                    payload = json.loads(response_text)
                except json.JSONDecodeError as exc:
                    raise RuntimeError(
                        f"{api_channel['name']} returned invalid JSON: {response_text[:500]}"
                    ) from exc

                return {
                    "api_channel": dict(api_channel),
                    "content": _extract_message_content(payload),
                    "raw_response": payload,
                }
        except asyncio.TimeoutError as exc:
            last_error = exc
            if attempt >= max_retries - 1:
                break
            await asyncio.sleep(_build_backoff_delay(attempt, backoff_base_seconds))
        except aiohttp.ClientError as exc:
            last_error = exc
            if attempt >= max_retries - 1:
                break
            await asyncio.sleep(_build_backoff_delay(attempt, backoff_base_seconds))

    channel_name = str(api_channel.get("name", "unknown"))
    raise RuntimeError(f"{channel_name} failed after {max_retries} attempts: {last_error}") from last_error


async def dispatch_routed_chat_requests(
    tasks: Sequence[Mapping[str, Any]],
    api_channels: Sequence[Mapping[str, Any]] | None = None,
    max_concurrency: int = 4,
) -> list[dict[str, Any]]:
    channels = list(api_channels or build_hk_llm_api_channels())
    semaphore = asyncio.Semaphore(max(1, int(max_concurrency)))

    async with aiohttp.ClientSession() as session:
        async def _run_single(task: Mapping[str, Any]) -> dict[str, Any]:
            stock_code = _normalize_stock_code(task.get("stock_code") or "")
            if not stock_code:
                raise ValueError("task.stock_code cannot be empty")

            preferred_api_channel = task.get("preferred_api_channel")
            api_channel = (
                dict(preferred_api_channel)
                if isinstance(preferred_api_channel, Mapping)
                else route_api_by_stock_code(stock_code, channels)
            )

            async with semaphore:
                response = await post_chat_completion(
                    session=session,
                    api_channel=api_channel,
                    messages=task.get("messages") or [],
                    temperature=float(task.get("temperature", 0.0)),
                    max_tokens=int(task.get("max_tokens", DEFAULT_MAX_TOKENS)),
                )

            return {
                "stock_code": stock_code,
                "request_tag": str(task.get("request_tag") or ""),
                "api_channel": api_channel,
                "content": response["content"],
                "raw_response": response["raw_response"],
            }

        return list(await asyncio.gather(*(_run_single(task) for task in tasks)))


def run_routed_chat_request(
    stock_code: str,
    messages: Sequence[Mapping[str, Any]],
    temperature: float = 0.0,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    request_tag: str = "",
    api_channels: Sequence[Mapping[str, Any]] | None = None,
    preferred_api_channel: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(
            dispatch_routed_chat_requests(
                tasks=[
                    {
                        "stock_code": stock_code,
                        "messages": list(messages),
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "request_tag": request_tag,
                        "preferred_api_channel": dict(preferred_api_channel) if preferred_api_channel else None,
                    }
                ],
                api_channels=api_channels,
                max_concurrency=1,
            )
        )[0]

    raise RuntimeError("event loop is already running; use await dispatch_routed_chat_requests(...)")
