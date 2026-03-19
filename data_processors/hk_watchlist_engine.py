# -*- coding: utf-8 -*-
import sys
import re
import ssl
sys.stdout.reconfigure(encoding='utf-8')
#import google.generativeai as genai
import json
import requests
import pandas as pd
from datetime import datetime
import statistics
from typing import Any, Dict, List, Optional, Tuple
import concurrent.futures
import time
import random
import zhconv  # 必须安装: pip install zhconv
import fitz    # 必须安装: pip install pymupdf
import tempfile
import os
from dateutil.relativedelta import relativedelta
import calendar
from datetime import timedelta
from urllib.parse import urljoin
import urllib3
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from clients.hk_llm_client import build_hk_llm_api_channels, run_routed_chat_request

# =========================================================
# 1) 配置
# =========================================================
BASE_URL = "https://quantapi.51ifind.com/api/v1"
VOLCENGINE_API_KEY = os.getenv("VOLCENGINE_API_KEY", "")
VOLCENGINE_MODEL = os.getenv("VOLCENGINE_MODEL", "ep-20250215093751-2ttw8")
VOLCENGINE_BASE_URL = os.getenv("VOLCENGINE_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
# GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
HKEX_STOCK_MAPPING_URL = "https://www1.hkexnews.hk/ncms/script/eds/activestock_sehk_e.json"
HKEX_TITLE_SEARCH_URL = "https://www1.hkexnews.hk/search/titlesearch.xhtml"
HKEX_PHASE1_LOOKBACK_DAYS = 400
HKEX_PHASE1_NOTICE_LIMIT = 2
HKEX_MAX_RETRIES = 3
HKEX_RETRY_DELAY = 1.0
USE_COMPACT_PROMPT = True    # True=精简Prompt；False=原始长Prompt
HKEX_BOARD_KEYWORD = "董事会会议"
HKEX_BOARD_KEYWORD_FALLBACK = "董事会"
HKEX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://www1.hkexnews.hk/",
    "Origin": "https://www1.hkexnews.hk",
}

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
_HKEX_STOCK_MAPPING_CACHE: Dict[str, Dict[str, Any]] = {}
_HKEX_STOCK_MAPPING_LOADED = False
_HKEX_SESSION: Optional[requests.Session] = None
_LLM_TOKEN_STATS: Dict[str, Dict[str, float]] = {}
_RUNTIME_ACCESS_TOKEN: Optional[str] = None
HK_LLM_API_CHANNEL_OVERRIDE: Optional[Dict[str, Any]] = None

# 运行时配置
ACCESS_TOKEN = os.getenv("IFIND_ACCESS_TOKEN", "")
REFRESH_TOKEN = os.getenv("IFIND_REFRESH_TOKEN", "")
USE_REFRESH_TO_GET_ACCESS = not bool(ACCESS_TOKEN)

STOCK_CODE = os.getenv("HK_WATCHLIST_TEST_CODE", "9866.HK")
TODAY_DATE = os.getenv("HK_WATCHLIST_TODAY", datetime.now().strftime('%Y-%m-%d'))
#TODAY_DATE = "2025-10-11"

# 包含杂项属性，放开自愿披露
REPORT_TYPE = "904002001,904002002,904002003,904001003,904001001"
OUTPUT_PARA = "reportDate:Y,reportTitle:Y,pdfURL:Y"
IFIND_MAX_RETRIES = 4
IFIND_RETRY_BASE_DELAY = 1.0

# =========================================================
# 2) 接口工具函数
# =========================================================
def _ifind_backoff_sleep(attempt: int) -> None:
    delay = IFIND_RETRY_BASE_DELAY * (2 ** max(0, attempt)) + random.uniform(0, IFIND_RETRY_BASE_DELAY)
    print(f"   ⏳ [iFind] 第{attempt+1}次失败，{delay:.1f} 秒后重试...")
    time.sleep(delay)


def get_access_token(refresh_token: str, force_new: bool = False) -> str:
    endpoint = "update_access_token" if force_new else "get_access_token"
    url = f"{BASE_URL}/{endpoint}"
    last_error: Optional[Exception] = None
    for attempt in range(IFIND_MAX_RETRIES):
        try:
            resp = requests.post(
                url,
                headers={"Content-Type": "application/json", "refresh_token": refresh_token},
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json() or {}
            if body.get("errorcode") not in (None, 0):
                raise RuntimeError(f"IFind access_token error: {body.get('errmsg') or body.get('errorinfo') or 'unknown'}")
            token = (body.get("data") or {}).get("access_token")
            if not token:
                raise RuntimeError("access_token not found")
            return token
        except (requests.Timeout, requests.ConnectionError, requests.exceptions.SSLError) as exc:
            last_error = exc
            if attempt >= IFIND_MAX_RETRIES - 1:
                break
            _ifind_backoff_sleep(attempt)
    if last_error is not None:
        raise last_error
    raise RuntimeError("IFind access_token request failed")


def get_runtime_access_token(force_refresh: bool = False) -> str:
    global _RUNTIME_ACCESS_TOKEN
    if force_refresh:
        if not REFRESH_TOKEN:
            raise RuntimeError("IFind refresh token missing")
        _RUNTIME_ACCESS_TOKEN = get_access_token(REFRESH_TOKEN, force_new=True)
        return _RUNTIME_ACCESS_TOKEN
    if _RUNTIME_ACCESS_TOKEN:
        return _RUNTIME_ACCESS_TOKEN
    if ACCESS_TOKEN and not USE_REFRESH_TO_GET_ACCESS:
        _RUNTIME_ACCESS_TOKEN = ACCESS_TOKEN
        return _RUNTIME_ACCESS_TOKEN
    if REFRESH_TOKEN:
        _RUNTIME_ACCESS_TOKEN = get_access_token(REFRESH_TOKEN, force_new=False)
        return _RUNTIME_ACCESS_TOKEN
    if ACCESS_TOKEN:
        _RUNTIME_ACCESS_TOKEN = ACCESS_TOKEN
        return _RUNTIME_ACCESS_TOKEN
    raise RuntimeError("IFind access token missing")


def _post_ifind_json(endpoint: str, payload: Dict[str, Any], timeout: int = 30) -> Dict[str, Any]:
    last_error: Optional[Exception] = None
    for attempt in range(max(2, IFIND_MAX_RETRIES)):
        force_refresh = attempt > 0
        token = get_runtime_access_token(force_refresh=force_refresh)
        try:
            resp = requests.post(
                f"{BASE_URL}/{endpoint}",
                json=payload,
                headers={"Content-Type": "application/json", "access_token": token},
                timeout=timeout,
            )
            if resp.status_code == 401 and REFRESH_TOKEN and attempt == 0:
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                last_error = RuntimeError(f"IFind http error {resp.status_code}: {resp.text[:300]}")
                if attempt < max(2, IFIND_MAX_RETRIES) - 1:
                    _ifind_backoff_sleep(attempt)
                    continue
            resp.raise_for_status()
            return resp.json()
        except requests.HTTPError as exc:
            last_error = exc
            if getattr(exc.response, "status_code", None) == 401 and REFRESH_TOKEN and attempt == 0:
                continue
            raise
        except (requests.Timeout, requests.ConnectionError, requests.exceptions.SSLError) as exc:
            last_error = exc
            if attempt < max(2, IFIND_MAX_RETRIES) - 1:
                _ifind_backoff_sleep(attempt)
                continue
            raise
        except requests.RequestException as exc:
            last_error = exc
            raise
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"IFind request failed: {endpoint}")

def get_company_short_name_http(stock_code: str, token: str) -> str:
    """HTTP 方式拉取公司简称，使用 corp_short_name 指标"""
    payload = {
        "codes": stock_code,
        "indipara": [{"indicator": "corp_short_name", "indiparams": []}]  # 核心修改 1：参数名改为 corp_short_name
    }
    try:
        global _RUNTIME_ACCESS_TOKEN
        _RUNTIME_ACCESS_TOKEN = token or _RUNTIME_ACCESS_TOKEN
        data = _post_ifind_json("basic_data_service", payload, timeout=20)
        if data.get("errorcode") != 0: return ""
        tables = data.get("tables", [])
        if not tables: return ""
        
        table_data = tables[0].get("table", {})
        if isinstance(table_data, dict):
            # 核心修改 2：解析返回 JSON 时使用新的 Key
            names = table_data.get("corp_short_name")
            if isinstance(names, list) and len(names) > 0: return str(names[0])
        elif isinstance(table_data, list):
            for item in table_data:
                # 核心修改 3：兼容列表格式返回时的 Key
                if "corp_short_name" in item: return str(item["corp_short_name"])
        return ""
    except Exception:
        return ""

def report_query(access_token: str, code: str, begin_time: str, end_time: str) -> Dict[str, Any]:
    payload = {
        "codes": code,
        "functionpara": {"begincTime": begin_time, "endcTime": end_time, "reportType": REPORT_TYPE},
        "outputpara": OUTPUT_PARA,
    }
    global _RUNTIME_ACCESS_TOKEN
    _RUNTIME_ACCESS_TOKEN = access_token or _RUNTIME_ACCESS_TOKEN
    return _post_ifind_json("report_query", payload, timeout=30)


def call_ds_llm(
    stock_code: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.0,
    max_retries: int = 10,
    request_tag: str = "",
    prompt_only_token_estimate: Optional[int] = None,
    pdf_token_estimate: int = 0,
) -> str:
    """统一的 DS LLM 调用封装，使用 http.client + DEEPSEEK_API_HOST + Bearer。"""
    api_channels = build_hk_llm_api_channels()
    preferred_api_channel = dict(HK_LLM_API_CHANNEL_OVERRIDE) if HK_LLM_API_CHANNEL_OVERRIDE else None

    prompt_len = len(user_prompt or "")
    # 输入分项估算：用于把 input_tokens 拆分为「提示词」与「PDF正文」
    est_total_prompt_tokens = _estimate_tokens_rough((system_prompt or "") + "\n" + (user_prompt or ""))
    est_pdf_tokens = max(0, int(pdf_token_estimate or 0))
    if isinstance(prompt_only_token_estimate, int) and prompt_only_token_estimate >= 0:
        est_prompt_only_tokens = int(prompt_only_token_estimate)
    else:
        est_prompt_only_tokens = max(0, est_total_prompt_tokens - est_pdf_tokens)
    if est_prompt_only_tokens + est_pdf_tokens <= 0:
        est_prompt_only_tokens = est_total_prompt_tokens
        est_pdf_tokens = 0

    for attempt in range(max_retries):
        try:
            t0 = time.perf_counter()
            routed_response = run_routed_chat_request(
                stock_code=stock_code,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=temperature,
                max_tokens=32768,
                request_tag=request_tag,
                api_channels=api_channels,
                preferred_api_channel=preferred_api_channel,
            )
            data = routed_response["raw_response"]
            routed_channel = routed_response["api_channel"]
            status = 200
            if "choices" in data and data["choices"]:
                content = (data["choices"][0].get("message") or {}).get("content", "")
                if isinstance(content, list):
                    text_parts = []
                    for item in content:
                        if isinstance(item, dict) and item.get("type") == "text":
                            text_parts.append(str(item.get("text", "")))
                    content = "".join(text_parts)
                usage = data.get("usage", {}) if isinstance(data, dict) else {}
                pt = usage.get("prompt_tokens")
                ct = usage.get("completion_tokens")
                tt = usage.get("total_tokens")
                usage_source = "api"
                if not isinstance(pt, int) or not isinstance(ct, int):
                    pt = _estimate_tokens_rough((system_prompt or "") + "\n" + (user_prompt or ""))
                    ct = _estimate_tokens_rough(str(content or ""))
                    tt = pt + ct
                    usage_source = "estimate"
                elif not isinstance(tt, int):
                    tt = pt + ct
                tag = request_tag or "llm"
                prompt_part_tokens, pdf_part_tokens = _split_input_token_parts(
                    int(pt or 0),
                    est_prompt_only_tokens,
                    est_pdf_tokens,
                )
                _record_llm_token_stat(
                    tag,
                    pt,
                    ct,
                    tt,
                    usage_source,
                    prompt_part_tokens,
                    pdf_part_tokens,
                )
                elapsed = time.perf_counter() - t0
                print(
                    f"   ⏱️ [LLM:{tag}] provider={routed_channel.get('name')} prompt_len={prompt_len} resp_len={len(str(content))} "
                    f"prompt_tokens={pt} completion_tokens={ct} total_tokens={tt} "
                    f"input_prompt={prompt_part_tokens} input_pdf={pdf_part_tokens} "
                    f"usage={usage_source} status={status} elapsed={elapsed:.2f}s"
                )
                return str(content or "").strip()

            err_obj = data.get("error", data)
            elapsed = time.perf_counter() - t0
            print(
                f"   ⚠️ [港股LLM 第{attempt+1}次尝试失败] "
                f"provider={routed_channel.get('name')} status={status} elapsed={elapsed:.2f}s "
                f"{json.dumps(err_obj, ensure_ascii=False)}"
            )
        except Exception as e:
            print(f"   ⚠️ [港股LLM 第{attempt+1}次连接异常] code={stock_code} {e}")

        if attempt < max_retries - 1:
            print("   ⏳ 等待 3 秒后重试...")
            time.sleep(3)

    raise RuntimeError(f"港股 LLM API 在 {max_retries} 次尝试后仍然失败")


def _estimate_tokens_rough(text: str) -> int:
    """
    粗略 token 估算：
    - CJK 字符按 1 token
    - 非 CJK 字符按 1/4 token
    """
    s = str(text or "")
    if not s:
        return 0
    cjk = 0
    for ch in s:
        if "\u4e00" <= ch <= "\u9fff":
            cjk += 1
    non_cjk = len(s) - cjk
    return int(round(cjk + non_cjk / 4.0))


def _reset_llm_token_stats() -> None:
    _LLM_TOKEN_STATS.clear()


def _split_input_token_parts(actual_prompt_tokens: int, prompt_only_est: int, pdf_est: int) -> Tuple[int, int]:
    actual = max(0, int(actual_prompt_tokens or 0))
    p_est = max(0, int(prompt_only_est or 0))
    d_est = max(0, int(pdf_est or 0))
    est_total = p_est + d_est
    if actual <= 0:
        return 0, 0
    if est_total <= 0:
        return actual, 0
    prompt_part = int(round(actual * (p_est / est_total)))
    prompt_part = max(0, min(actual, prompt_part))
    return prompt_part, actual - prompt_part


def _record_llm_token_stat(
    tag: str,
    prompt_tokens: int,
    completion_tokens: int,
    total_tokens: int,
    usage_source: str,
    prompt_input_tokens: int = 0,
    pdf_input_tokens: int = 0,
) -> None:
    stat = _LLM_TOKEN_STATS.setdefault(tag, {
        "calls": 0,
        "prompt_tokens": 0,
        "prompt_input_tokens": 0,
        "pdf_input_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "api_usage_calls": 0,
        "estimate_usage_calls": 0,
    })
    stat["calls"] += 1
    stat["prompt_tokens"] += int(prompt_tokens or 0)
    stat["prompt_input_tokens"] += int(prompt_input_tokens or 0)
    stat["pdf_input_tokens"] += int(pdf_input_tokens or 0)
    stat["completion_tokens"] += int(completion_tokens or 0)
    stat["total_tokens"] += int(total_tokens or 0)
    if usage_source == "api":
        stat["api_usage_calls"] += 1
    else:
        stat["estimate_usage_calls"] += 1


def _print_llm_token_stats() -> None:
    if not _LLM_TOKEN_STATS:
        print("📊 [LLM Token统计] 本次运行无LLM调用。")
        return
    print("\n📊 [LLM Token统计-按Phase]")
    print("phase_tag | calls | input_tokens | prompt_tokens | pdf_tokens | output_tokens | total_tokens | usage(api/est)")
    total_calls = total_in = total_prompt_in = total_pdf_in = total_out = total_tok = 0
    for tag, stat in _LLM_TOKEN_STATS.items():
        calls = int(stat["calls"])
        tin = int(stat["prompt_tokens"])
        t_prompt = int(stat["prompt_input_tokens"])
        t_pdf = int(stat["pdf_input_tokens"])
        tout = int(stat["completion_tokens"])
        tt = int(stat["total_tokens"])
        api_c = int(stat["api_usage_calls"])
        est_c = int(stat["estimate_usage_calls"])
        print(f"{tag} | {calls} | {tin} | {t_prompt} | {t_pdf} | {tout} | {tt} | {api_c}/{est_c}")
        total_calls += calls
        total_in += tin
        total_prompt_in += t_prompt
        total_pdf_in += t_pdf
        total_out += tout
        total_tok += tt
    print(f"TOTAL | {total_calls} | {total_in} | {total_prompt_in} | {total_pdf_in} | {total_out} | {total_tok}")


def build_compact_prompt(
    task_title: str,
    rules: List[str],
    input_blocks: List[Tuple[str, str]],
    output_schema_json: str,
) -> str:
    """构建精简版 Prompt：任务 + 硬规则 + 输入 + 固定JSON输出。"""
    lines = [f"任务：{task_title}", "规则："]
    for idx, rule in enumerate(rules, 1):
        lines.append(f"{idx}. {rule}")
    lines.append("输入：")
    for header, content in input_blocks:
        lines.append(f"【{header}】")
        lines.append(content)
    lines.append("输出要求：仅返回 JSON，不要Markdown，不要解释，不要额外字段。")
    lines.append("JSON Schema 示例：")
    lines.append(output_schema_json)
    return "\n".join(lines)


def _truncate_for_prompt(text: str, max_chars: int) -> str:
    return (text or "")[:max_chars]


def _safe_to_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip().replace(",", "").replace("，", "")
    if not s:
        return None
    s = s.replace("−", "-")
    # 保留负号、小数点、数字
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def _unit_divisor_to_yi(unit_text: str) -> float:
    s = zhconv.convert(str(unit_text or ""), "zh-cn").lower()
    if "亿元" in s or "亿" == s.strip():
        return 1.0
    if "百万元" in s or "million" in s:
        return 100.0
    if "千元" in s or "'000" in s or "thousand" in s:
        return 100000.0
    if "万元" in s:
        return 10000.0
    # 默认按“元”
    return 100000000.0


def _convert_financial_values_to_yi(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """
    将 LLM 提取到的金额按 detected_unit 统一换算为“亿元”。
    只处理 revenue / gross_profit / net_profit，不处理毛利率。
    """
    unit = str(parsed.get("detected_unit", "") or "")
    divisor = _unit_divisor_to_yi(unit)

    for block_name in ("cumulative", "single_quarter"):
        block = parsed.get(block_name)
        if not isinstance(block, dict):
            continue
        for key in ("revenue", "gross_profit", "net_profit"):
            raw_val = _safe_to_float(block.get(key))
            if raw_val is None:
                block[key] = None
                continue
            block[key] = round(raw_val / divisor, 2)

    parsed["normalized_unit"] = "亿元"
    parsed["unit_divisor_to_yi"] = divisor
    return parsed


def _derive_margin_pct(gross_profit: Any, revenue: Any) -> Optional[float]:
    gross = _safe_to_float(gross_profit)
    rev = _safe_to_float(revenue)
    if gross is None or rev is None or rev == 0:
        return None
    return round(gross / rev * 100.0, 2)


def _enrich_financial_margin_fields(parsed: Dict[str, Any]) -> Dict[str, Any]:
    """
    兜底补全毛利率：
    - 优先保留 LLM 返回的 single_quarter.gross_margin_pct
    - 若缺失且有 single_quarter.revenue + gross_profit，则自动推导
    """
    if not isinstance(parsed, dict):
        return parsed
    sq = parsed.get("single_quarter")
    if not isinstance(sq, dict):
        return parsed

    raw_margin = _safe_to_float(sq.get("gross_margin_pct"))
    if raw_margin is not None:
        sq["gross_margin_pct"] = round(raw_margin, 2)
        return parsed

    derived = _derive_margin_pct(sq.get("gross_profit"), sq.get("revenue"))
    if derived is not None:
        sq["gross_margin_pct"] = derived
    return parsed


def _extract_rows_from_obj(obj: Any) -> List[Dict[str, Any]]:
    rows = []
    if isinstance(obj, list):
        for item in obj: rows.extend(_extract_rows_from_obj(item))
        return rows
    if not isinstance(obj, dict): return rows
    if "table" in obj and isinstance(obj["table"], list):
        for r in obj["table"]: 
            if isinstance(r, dict): rows.append(r)
    if "reportTitle" in obj and "reportDate" in obj:
        rt, ct, pu = obj.get("reportTitle"), obj.get("reportDate"), obj.get("pdfURL")
        if isinstance(rt, list) and isinstance(ct, list):
            n = min(len(rt), len(ct))
            pu_list = pu if isinstance(pu, list) and len(pu) >= n else [None] * n
            for i in range(n): rows.append({"reportDate": ct[i], "reportTitle": rt[i], "pdfURL": pu_list[i]})
        else:
            rows.append({"reportDate": ct, "reportTitle": rt, "pdfURL": pu})
    for v in obj.values():
        if isinstance(v, (dict, list)): rows.extend(_extract_rows_from_obj(v))
    return rows

def normalize_ifind_rows(resp_json: Dict[str, Any]) -> List[Dict[str, str]]:
    if resp_json.get("errorcode") not in (None, 0): raise RuntimeError("iFind error")
    raw_rows = _extract_rows_from_obj(resp_json.get("tables", []))
    out, seen = [], set()
    for r in raw_rows:
        reportDate, title = str(r.get("reportDate", "") or "").strip(), str(r.get("reportTitle", "") or "").strip()
        pdf_url = str(r.get("pdfURL", "") or "").strip()
        if not reportDate and not title: continue
        key = (reportDate, title)
        if key in seen: continue
        seen.add(key)
        out.append({"reportDate": reportDate, "reportTitle": title, "pdfURL": pdf_url})
    out.sort(key=lambda x: x["reportDate"], reverse=True)
    return out

# =========================================================
# 3) 终极实体鉴权与噪音过滤 (支持简繁统一)
# =========================================================
def evaluate_announcement(title: str, company_name: str = "") -> bool:
    """
    初筛：关键字排除部分保留，业绩关键词判断保留，二者起冲突，以后面为准。
    正则切分标题结构去掉，交给LLM。
    """
    # 1. 简繁大一统 & 转大写
    t_simp = zhconv.convert(title.strip(), 'zh-cn').upper()
    
    # 判定是否有业绩关键词
    finance_keywords = [
        "业绩", "财务", "营运", "数据", "收益", "利润", "季度更新", "营运更新", 
        "季度报告", "季报", "中报", "年报", "中期报告", "年度报告", "中期业绩"
    ]
    has_finance_data = any(kw in t_simp for kw in finance_keywords)
    if not has_finance_data and any(kw in t_simp for kw in ["FINANCIAL", "RESULTS", "OPERATIONAL", "UPDATE"]):
        has_finance_data = True

    # 基础脏话黑名单（这部分如果仅包含噪音而没有业绩数据，则过滤；但如果同时有业绩数据，以业绩优先放行）
    noise_keywords = [
        "证券变动", "股份发行", "购股权", "授出", "注销", 
        "董事", "辞任", "委任", "代表委任表格", "股东周年大会", "章程",
        "关连交易", "主要交易", "出售", "停牌", "复牌", "股价波动", 
        "股息", "派息", "通函", "海外监管", "附属", "联营"
    ]
    has_secondary_noise = any(nk in t_simp for nk in noise_keywords)

    # 致命噪音：只要出现这类词（通常是几百页的长报告或是纯路演PPT材料），就算有业绩词也直接拦截！我们要找的是短平快的【业绩公告】
    fatal_noise_keywords = [
        "月报表", "演示材料", "推介材料", "一图看懂", "图解", "路演", "PPT", "简报",
        "中期报告", "年度报告", "全年报告", "报表", 
        "董事会召开日期", "董事会会议通告", "电话会议", "发布会", "说明会", "海外监管公告", "利润分配方案", "交付数据", "销量"
    ]
    has_fatal_noise = any(fn in t_simp for fn in fatal_noise_keywords)

    # 致命噪音直接一票否决
    if has_fatal_noise:
        return False
        
    # 业绩关键词命中则直接放行，哪怕带有附带议案如“股息”“章程”
    if has_finance_data:
        return True
    
    # 其余未命中的普通模糊公告默认过滤掉（因为我们要抓的是定期财报，非财报且非噪音基本无用，可以拦截）
    return False

# =========================================================
def normalize_reportDate(reportDate: str) -> str:
    if not reportDate: return reportDate
    c = reportDate.strip()
    formats = ["%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y/%m/%d"]
    for fmt in formats:
        try: return datetime.strptime(c[:19], fmt).strftime("%Y-%m-%d %H:%M:%S")
        except Exception: continue
    return c

# =========================================================
# 5) 数据拉取与清洗封装
# =========================================================
def fetch_and_normalize(code: str, token: str, begin_time: str, end_time: str) -> pd.DataFrame:
    """封装原本的请求、归一化、初筛流程"""
    try:
        resp_json = report_query(token, code, begin_time, end_time)
        rows = normalize_ifind_rows(resp_json)
        out = []
        for r in rows:
            title = r["reportTitle"]
            if not evaluate_announcement(title): # 简化初筛
                continue 
            reportDate = normalize_reportDate(r["reportDate"])
            out.append({"reportDate": reportDate, "reportTitle": title, "pdfURL": r.get("pdfURL", "")})
        df = pd.DataFrame(out)
        if not df.empty:
            df = df.drop_duplicates(subset=["reportDate", "reportTitle"]).copy()
        else:
            df = pd.DataFrame(columns=["reportDate", "reportTitle", "pdfURL"])
        return df.sort_values("reportDate", ascending=False).reset_index(drop=True)
    except Exception as e:
        print(f"❌ 数据拉取失败 [{begin_time} 至 {end_time}]: {e}")
        return pd.DataFrame(columns=["reportDate", "reportTitle", "pdfURL"])

# =========================================================
def normalize_hkex_stock_code(stock_code: str) -> str:
    """将 9901.HK / 700 / 00700 统一为 HKEX 的 5 位代码。"""
    raw = str(stock_code or "").strip().upper()
    raw = raw.replace(".HK", "")
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return ""
    return digits[-5:].zfill(5)


class HKEXTLSAdapter(HTTPAdapter):
    def _build_ssl_context(self) -> ssl.SSLContext:
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        try:
            ssl_context.set_ciphers("DEFAULT@SECLEVEL=1")
        except ssl.SSLError:
            ssl_context.set_ciphers("DEFAULT")
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        return ssl_context

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = self._build_ssl_context()
        return super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, *args, **kwargs):
        kwargs["ssl_context"] = self._build_ssl_context()
        return super().proxy_manager_for(*args, **kwargs)


def get_hkex_session() -> requests.Session:
    global _HKEX_SESSION
    if _HKEX_SESSION is not None:
        return _HKEX_SESSION

    session = requests.Session()
    session.headers.update(HKEX_HEADERS)
    adapter = HKEXTLSAdapter(max_retries=Retry(total=0, connect=0, read=0, redirect=0, status=0))
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    _HKEX_SESSION = session
    return session


def hkex_get(url: str, *, context: str, **kwargs) -> requests.Response:
    last_error: Optional[Exception] = None
    session = get_hkex_session()

    for attempt in range(1, HKEX_MAX_RETRIES + 1):
        try:
            resp = session.get(url, timeout=30, verify=False, **kwargs)
            resp.raise_for_status()
            return resp
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                requests.exceptions.SSLError) as exc:
            last_error = exc
        except requests.exceptions.HTTPError as exc:
            last_error = exc
            status_code = exc.response.status_code if exc.response is not None else None
            if status_code not in {429, 500, 502, 503, 504}:
                raise

        if attempt < HKEX_MAX_RETRIES:
            wait_seconds = HKEX_RETRY_DELAY * (2 ** (attempt - 1))
            print(f"⚠️ {context} 第 {attempt}/{HKEX_MAX_RETRIES} 次失败，{wait_seconds:.1f}s 后重试...")
            time.sleep(wait_seconds)

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{context} 请求失败")


def load_hkex_stock_mapping() -> Dict[str, Dict[str, Any]]:
    """拉取 HKEX 股票映射，键为 5 位股票代码，值含 stockId 与公司名。"""
    global _HKEX_STOCK_MAPPING_LOADED, _HKEX_STOCK_MAPPING_CACHE
    if _HKEX_STOCK_MAPPING_LOADED and _HKEX_STOCK_MAPPING_CACHE:
        return _HKEX_STOCK_MAPPING_CACHE

    try:
        resp = hkex_get(HKEX_STOCK_MAPPING_URL, context="HKEX 股票映射加载")
        data = resp.json()
        mapping: Dict[str, Dict[str, Any]] = {}
        for item in data:
            code = str(item.get("c", "")).zfill(5)
            stock_id = item.get("i")
            if code and stock_id is not None:
                mapping[code] = {"stockId": stock_id, "name": item.get("n", "")}
        _HKEX_STOCK_MAPPING_CACHE = mapping
        _HKEX_STOCK_MAPPING_LOADED = True
    except Exception as e:
        print(f"⚠️ HKEX 股票映射加载失败: {e}")
        _HKEX_STOCK_MAPPING_CACHE = {}
        _HKEX_STOCK_MAPPING_LOADED = False

    return _HKEX_STOCK_MAPPING_CACHE


def search_hkex_titles_by_stockid(stock_id: int, lookback_days: int = HKEX_PHASE1_LOOKBACK_DAYS) -> str:
    """调用 titlesearch.xhtml 获取指定 stockId 的公告列表页面。"""
    end_date = datetime.strptime(TODAY_DATE, "%Y-%m-%d")
    start_date = end_date - timedelta(days=lookback_days)
    params = {
        "lang": "zh",
        "category": "0",
        "market": "SEHK",
        "searchType": "0",
        "documentType": "-1",
        "t1code": "-2",
        "t2Gcode": "-2",
        "t2code": "-2",
        "stockId": str(stock_id),
        "from": start_date.strftime("%Y%m%d"),
        "to": end_date.strftime("%Y%m%d"),
    }
    try:
        resp = hkex_get(HKEX_TITLE_SEARCH_URL, context=f"HKEX 标题检索(stockId={stock_id})", params=params)
        return resp.text
    except Exception as e:
        print(f"⚠️ HKEX 标题检索失败(stockId={stock_id}): {e}")
        return ""


def _parse_hkex_date(date_text: str) -> Optional[datetime]:
    s = str(date_text or "").strip()
    if not s:
        return None
    s = s.split()[0].replace(".", "-")
    for fmt in ("%Y/%m/%d", "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue

    m = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", s)
    if m:
        y, mm, dd = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(y, mm, dd)
        except Exception:
            return None
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", s)
    if m:
        dd, mm, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            return datetime(y, mm, dd)
        except Exception:
            return None
    return None


def parse_hkex_titles_table(html: str) -> List[Dict[str, str]]:
    """解析 titlesearch 返回表格，提取 date/title/url。"""
    results: List[Dict[str, str]] = []
    if not html:
        return results

    try:
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table", class_="table")
        if not table:
            return results
        rows = table.find_all("tr")
        for row in rows[1:]:
            cols = row.find_all("td")
            if len(cols) < 4:
                continue
            date_text = cols[0].get_text(" ", strip=True)
            dt = _parse_hkex_date(date_text)
            title_col = cols[3]
            title = title_col.get_text(" ", strip=True)
            link = title_col.find("a", href=True)
            if not title or not link:
                continue
            href = str(link.get("href", "")).strip()
            if href.startswith("//"):
                full_url = "https:" + href
            elif href.startswith("/"):
                full_url = "https://www1.hkexnews.hk" + href
            elif href.startswith("http"):
                full_url = href
            else:
                full_url = urljoin("https://www1.hkexnews.hk/", href)
            results.append({
                "date": dt.strftime("%Y-%m-%d") if dt else "",
                "title": title,
                "url": full_url,
            })
    except Exception as e:
        print(f"⚠️ HKEX 公告表解析失败: {e}")
        return []

    results.sort(key=lambda x: x.get("date", ""), reverse=True)
    return results


def _filter_board_notices_with_fallback(docs: List[Dict[str, str]]) -> Tuple[List[Dict[str, str]], str]:
    """
    先按“董事会会议”过滤；若命中为0，再按“董事会”过滤。
    返回：命中公告列表 + 实际使用的关键词。
    """
    primary_hits: List[Dict[str, str]] = []
    for item in docs:
        title_simp = zhconv.convert(str(item.get("title", "")), "zh-cn")
        if HKEX_BOARD_KEYWORD in title_simp:
            primary_hits.append(item)
    if primary_hits:
        return primary_hits, HKEX_BOARD_KEYWORD

    fallback_hits: List[Dict[str, str]] = []
    for item in docs:
        title_simp = zhconv.convert(str(item.get("title", "")), "zh-cn")
        if HKEX_BOARD_KEYWORD_FALLBACK in title_simp:
            fallback_hits.append(item)
    return fallback_hits, HKEX_BOARD_KEYWORD_FALLBACK


def find_recent_board_meeting_notices(
    stock_code: str,
    lookback_days: int = HKEX_PHASE1_LOOKBACK_DAYS,
    limit: int = HKEX_PHASE1_NOTICE_LIMIT,
) -> List[Dict[str, str]]:
    """
    在 HKEX 最近 lookback_days 天公告里，查找“董事会会议”相关公告，按日期倒序返回前 limit 条。
    """
    norm_code = normalize_hkex_stock_code(stock_code)
    mapping = load_hkex_stock_mapping()
    if norm_code not in mapping:
        print(f"⚠️ HKEX 映射中未找到股票代码: {stock_code} -> {norm_code}")
        return []

    stock_meta = mapping[norm_code]
    html = search_hkex_titles_by_stockid(stock_meta["stockId"], lookback_days)
    docs = parse_hkex_titles_table(html)
    if not docs:
        return []

    filtered_docs, used_keyword = _filter_board_notices_with_fallback(docs)
    keyword_docs: List[Dict[str, str]] = []
    for item in filtered_docs:
        d = dict(item)
        d["stock_code"] = norm_code
        d["company_name"] = stock_meta.get("name", "")
        keyword_docs.append(d)

    if not keyword_docs:
        return []
    if used_keyword != HKEX_BOARD_KEYWORD:
        print(f"ℹ️ [HKEX] 关键词降级：'{HKEX_BOARD_KEYWORD}' 未命中，改用 '{used_keyword}'")
    return keyword_docs[:max(1, limit)]


def fetch_hkex_notice_text(url: str, max_pages: int = 30) -> str:
    """下载并提取董事会会议公告正文（PDF/HTML）。"""
    if not url:
        return ""
    try:
        resp = requests.get(url, headers=HKEX_HEADERS, timeout=30, verify=False)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "").lower()
        if "pdf" in content_type or url.lower().endswith(".pdf"):
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                tmp.write(resp.content)
                tmp_path = tmp.name
            try:
                doc = fitz.open(tmp_path)
                text = []
                for i, page in enumerate(doc):
                    if i >= max_pages:
                        break
                    text.append(page.get_text())
                doc.close()
                return "\n".join(text)
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

        soup = BeautifulSoup(resp.text, "html.parser")
        return soup.get_text("\n", strip=True)
    except Exception as e:
        print(f"⚠️ HKEX 公告正文提取失败: {e}")
        return ""


def _safe_load_json_from_text(raw_text: str) -> Optional[Dict[str, Any]]:
    text = str(raw_text or "").strip()
    if not text:
        return None
    cleaned = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(cleaned)
        return data if isinstance(data, dict) else None
    except Exception:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start >= 0 and end > start:
        try:
            data = json.loads(cleaned[start:end + 1])
            return data if isinstance(data, dict) else None
        except Exception:
            return None
    return None


def normalize_date_ymd(date_text: Any) -> str:
    s = str(date_text or "").strip()
    if not s:
        return ""
    s = s.replace("年", "-").replace("月", "-").replace("日", "").replace(".", "-").replace("/", "-")
    s = re.sub(r"\s+", " ", s)
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(s[:19], fmt).strftime("%Y-%m-%d")
        except Exception:
            continue
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).strftime("%Y-%m-%d")
        except Exception:
            return ""
    return ""


def normalize_target_period(period_text: Any) -> str:
    s = zhconv.convert(str(period_text or ""), "zh-cn").strip().lower()
    if not s:
        return ""
    if any(x in s for x in ["q1", "第一季度", "一季度", "一季报", "首季度"]):
        return "Q1"
    if any(x in s for x in ["q3", "第三季度", "三季度", "三季报"]):
        return "Q3"
    if any(x in s for x in ["interim", "中期", "半年度", "半年"]):
        return "Interim"
    if any(x in s for x in ["annual", "年度", "全年", "年报"]):
        return "Annual"
    return ""


def normalize_frequency(freq_text: Any, target_period: str = "") -> str:
    s = zhconv.convert(str(freq_text or ""), "zh-cn").strip().lower()
    if "季度" in s or s in {"quarterly", "quarter"}:
        return "季度披露"
    if "半年度" in s or "半年度披露" in s or "中期" in s or s in {"semiannual", "semi-annual", "half-year"}:
        return "半年度披露"
    period = normalize_target_period(target_period)
    if period in {"Q1", "Q3"}:
        return "季度披露"
    return ""


def _contains_quarter_period_keywords(text: str) -> bool:
    s = zhconv.convert(str(text or ""), "zh-cn").lower()
    kws = ["三个月", "九个月", "第一季度", "第三季度", "q1", "q3", "three months", "nine months"]
    return any(k in s for k in kws)


def _contains_half_or_full_year_keywords(text: str) -> bool:
    s = zhconv.convert(str(text or ""), "zh-cn").lower()
    kws = ["六个月", "十二个月", "半年", "全年", "中期", "年度", "six months", "twelve months", "interim", "annual"]
    return any(k in s for k in kws)


def _contains_interim_period_keywords(text: str) -> bool:
    s = zhconv.convert(str(text or ""), "zh-cn").lower()
    kws = ["六个月", "6个月", "半年", "中期", "six months", "interim", "half year", "half-year"]
    return any(k in s for k in kws)


def is_quarterly_frequency(freq_text: Any) -> bool:
    s = zhconv.convert(str(freq_text or ""), "zh-cn").strip().lower()
    return "季度" in s or s in {"quarterly", "quarter"}


def normalize_fye_month(raw_value: Any) -> Optional[int]:
    if raw_value is None:
        return None
    if isinstance(raw_value, int):
        return raw_value if 1 <= raw_value <= 12 else None
    s = zhconv.convert(str(raw_value), "zh-cn")
    m = re.search(r"(\d{1,2})", s)
    if m:
        v = int(m.group(1))
        if 1 <= v <= 12:
            return v
    return None


def _wrap_month(month: int) -> Optional[int]:
    try:
        v = int(month)
    except Exception:
        return None
    while v <= 0:
        v += 12
    while v > 12:
        v -= 12
    return v if 1 <= v <= 12 else None


def _parse_year_value(raw_value: Any) -> Optional[int]:
    if isinstance(raw_value, int):
        return raw_value if 1900 <= raw_value <= 2200 else None
    s = str(raw_value or "").strip()
    if s.isdigit():
        y = int(s)
        return y if 1900 <= y <= 2200 else None
    m = re.search(r"(19|20)\d{2}", s)
    if m:
        y = int(m.group(0))
        return y if 1900 <= y <= 2200 else None
    return None


def _parse_month_value(raw_value: Any) -> Optional[int]:
    if isinstance(raw_value, int):
        return raw_value if 1 <= raw_value <= 12 else None
    s = str(raw_value or "").strip()
    m = re.search(r"(\d{1,2})", s)
    if not m:
        return None
    v = int(m.group(1))
    return v if 1 <= v <= 12 else None


def _extract_month_from_english_year_ended(text: str) -> Optional[int]:
    s = str(text or "").lower()
    month_en_map = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    m = re.search(
        r"(?:year|annual).*?ended.*?(january|february|march|april|may|june|july|august|september|october|november|december)",
        s,
    )
    if not m:
        return None
    return month_en_map.get(m.group(1))


def _detect_notice_span_months(text: str) -> Optional[int]:
    s = zhconv.convert(str(text or ""), "zh-cn").strip().lower()
    if not s:
        return None
    span_candidates = []
    if any(k in s for k in ["年度", "全年", "年报", "十二个月", "12个月", "annual", "year ended", "full year", "twelve months"]):
        span_candidates.append(12)
    if any(k in s for k in ["九个月", "9个月", "三季", "第三季", "三季度", "q3", "third quarter", "nine months"]):
        span_candidates.append(9)
    if any(k in s for k in ["六个月", "6个月", "中期", "半年", "中报", "interim", "half year", "half-year", "six months"]):
        span_candidates.append(6)
    if any(k in s for k in ["一季", "第一季", "一季度", "q1", "first quarter", "三个月", "3个月", "three months"]):
        span_candidates.append(3)
    return max(span_candidates) if span_candidates else None


def _is_pure_three_month_notice(text: str) -> bool:
    s = zhconv.convert(str(text or ""), "zh-cn").strip().lower()
    if _detect_notice_span_months(s) != 3:
        return False
    if any(k in s for k in ["一季", "第一季", "一季度", "q1", "first quarter"]):
        return False
    if any(k in s for k in ["九个月", "9个月", "三季", "第三季", "三季度", "q3", "third quarter", "nine months"]):
        return False
    if any(k in s for k in ["六个月", "6个月", "中期", "半年", "中报", "interim", "half year", "half-year", "six months"]):
        return False
    if any(k in s for k in ["年度", "全年", "年报", "十二个月", "12个月", "annual", "year ended", "full year", "twelve months"]):
        return False
    return any(k in s for k in ["三个月", "3个月", "three months"])


def infer_three_month_period_from_notice_pair(
    notice1_xxxx: str,
    notice2_period: str,
    notice1_fy: Optional[int],
    notice2_fy: Optional[int],
) -> str:
    if not _is_pure_three_month_notice(notice1_xxxx):
        return ""
    p2 = normalize_target_period(notice2_period)
    if p2 == "Interim" and isinstance(notice1_fy, int) and isinstance(notice2_fy, int) and notice1_fy == notice2_fy:
        return "Q3"
    if p2 == "Annual" and isinstance(notice1_fy, int) and isinstance(notice2_fy, int) and notice1_fy == notice2_fy + 1:
        return "Q1"
    return ""


def infer_three_month_period_from_fye_and_end_month(
    notice_month: Optional[int],
    fye_month: Optional[int],
) -> str:
    month = _parse_month_value(notice_month)
    fye = normalize_fye_month(fye_month)
    if month is None or fye is None:
        return ""
    q1_month = _wrap_month(fye + 3)
    q3_month = _wrap_month(fye + 9)
    if month == q1_month:
        return "Q1"
    if month == q3_month:
        return "Q3"
    return ""


def derive_fye_from_notice(
    notice1_year: Optional[int],
    notice1_month: Optional[int],
    notice1_xxxx: str,
    notice1_three_month_period_hint: str = "",
    notice1_text: str = "",
    fye_hint: Optional[int] = None,
) -> Optional[int]:
    """
    根据公告1提取到的 yyyy/mm/xxxx 按规则反推 FYE。
    若无法规则判定，最后使用 fye_hint；仍无则返回 None。
    """
    month = _parse_month_value(notice1_month)
    if month is None:
        return normalize_fye_month(fye_hint)

    s = zhconv.convert(str(notice1_xxxx or ""), "zh-cn").strip().lower()
    span_months = _detect_notice_span_months(s)

    annual_kws = ["年度", "全年", "年报", "十二个月", "12个月", "annual", "year ended", "year"]
    interim_kws = ["六个月", "6个月", "中期", "半年", "中报", "interim", "half year", "half-year", "six months"]
    q3_kws = ["九个月", "9个月", "三季", "第三季", "三季度", "q3", "third quarter", "nine months"]
    q1_kws = ["一季", "第一季", "一季度", "q1", "first quarter"]

    if span_months == 12 or any(k in s for k in annual_kws):
        return month

    m_en = _extract_month_from_english_year_ended(notice1_xxxx) or _extract_month_from_english_year_ended(notice1_text)
    if m_en is not None and ("annual" in str(notice1_xxxx or "").lower() or "year" in str(notice1_xxxx or "").lower()):
        return m_en

    if span_months == 6 or any(k in s for k in interim_kws):
        return _wrap_month(month + 6)

    if span_months == 9 or any(k in s for k in q3_kws):
        return _wrap_month(month + 3)

    if any(k in s for k in q1_kws):
        return _wrap_month(month - 3)

    if span_months == 3 or any(k in s for k in ["三个月", "3个月", "three months"]):
        normalized_fye_hint = normalize_fye_month(fye_hint)
        if normalized_fye_hint is not None:
            return normalized_fye_hint
        hint = normalize_target_period(notice1_three_month_period_hint)
        if hint == "Q1":
            return _wrap_month(month - 3)
        if hint == "Q3":
            return _wrap_month(month + 3)

    return normalize_fye_month(fye_hint)


def derive_fy(notice1_year: Optional[int], notice1_month: Optional[int], fye_month: Optional[int]) -> Optional[int]:
    y = _parse_year_value(notice1_year)
    m = _parse_month_value(notice1_month)
    fye = normalize_fye_month(fye_month)
    if y is None or m is None or fye is None:
        return None
    return int(y) if m <= fye else int(y + 1)


def derive_notice1_period(notice1_xxxx: str, notice1_three_month_period_hint: str = "") -> str:
    s = zhconv.convert(str(notice1_xxxx or ""), "zh-cn").strip().lower()
    span_months = _detect_notice_span_months(s)

    if span_months == 12 or any(k in s for k in ["年度", "全年", "年报", "十二个月", "12个月", "annual", "year ended"]):
        return "Annual"
    if span_months == 6 or any(k in s for k in ["六个月", "6个月", "中期", "半年", "中报", "interim", "half year", "half-year", "six months"]):
        return "Interim"
    if span_months == 9 or any(k in s for k in ["九个月", "9个月", "三季", "第三季", "三季度", "q3", "third quarter", "nine months"]):
        return "Q3"
    if any(k in s for k in ["一季", "第一季", "一季度", "q1", "first quarter"]):
        return "Q1"

    if span_months == 3 or any(k in s for k in ["三个月", "3个月", "three months"]):
        hint = normalize_target_period(notice1_three_month_period_hint)
        if hint in {"Q1", "Q3"}:
            return hint
    return ""


def derive_target_from_notice1(
    notice1_period: str,
    fy_year: Optional[int],
    predicted_date_str: str,
    frequency: str,
) -> Tuple[str, Optional[int]]:
    period = normalize_target_period(notice1_period)
    if not period or not isinstance(fy_year, int):
        return "", None

    freq = normalize_frequency(frequency, period)
    if predicted_date_str:
        return period, int(fy_year)

    next_period, next_year = advance_target_period(period, int(fy_year), freq)
    return next_period, int(next_year)


def _is_pure_annual_notice_xxxx(raw_text: str) -> bool:
    s = zhconv.convert(str(raw_text or ""), "zh-cn").strip().lower()
    if not s:
        return False
    has_annual = any(k in s for k in ["年度", "全年", "年报", "十二个月", "12个月", "annual", "year ended", "full year", "twelve months"])
    has_interim = any(k in s for k in ["六个月", "6个月", "中期", "半年", "中报", "interim", "half year", "half-year", "six months"])
    has_quarter = _contains_quarter_period_keywords(s) or any(k in s for k in ["季度", "third quarter", "first quarter"])
    return has_annual and (not has_interim) and (not has_quarter)


def _is_pure_interim_notice_xxxx(raw_text: str) -> bool:
    s = zhconv.convert(str(raw_text or ""), "zh-cn").strip().lower()
    if not s:
        return False
    has_interim = any(k in s for k in ["六个月", "6个月", "中期", "半年", "中报", "interim", "half year", "half-year", "six months"])
    has_annual = any(k in s for k in ["年度", "全年", "年报", "十二个月", "12个月", "annual", "year ended", "full year", "twelve months"])
    has_quarter = _contains_quarter_period_keywords(s) or any(k in s for k in ["季度", "third quarter", "first quarter"])
    return has_interim and (not has_annual) and (not has_quarter)


def infer_frequency_from_notice_pair(
    notice1_period: str,
    notice1_fy: Optional[int],
    notice2_period: str = "",
    notice2_fy: Optional[int] = None,
    explicit_frequency: str = "",
    notice1_xxxx: str = "",
    notice2_xxxx: str = "",
) -> str:
    freq = normalize_frequency(explicit_frequency, "")
    if freq:
        return freq

    p1 = normalize_target_period(notice1_period)
    p2 = normalize_target_period(notice2_period)
    fy1 = int(notice1_fy) if isinstance(notice1_fy, int) else None
    fy2 = int(notice2_fy) if isinstance(notice2_fy, int) else None

    if p1 in {"Q1", "Q3"} or p2 in {"Q1", "Q3"}:
        return "季度披露"

    # 半年度披露的两个典型相邻组合：
    # 1) 最新公告是 Annual，上一份是同一财年的 Interim
    # 2) 最新公告是 Interim，上一份是上一财年的 Annual
    if (
        p1 == "Annual"
        and p2 == "Interim"
        and _is_pure_annual_notice_xxxx(notice1_xxxx)
        and _is_pure_interim_notice_xxxx(notice2_xxxx)
        and fy1 is not None
        and fy2 is not None
        and fy1 == fy2
    ):
        return "半年度披露"
    if (
        p1 == "Interim"
        and p2 == "Annual"
        and _is_pure_interim_notice_xxxx(notice1_xxxx)
        and _is_pure_annual_notice_xxxx(notice2_xxxx)
        and fy1 is not None
        and fy2 is not None
        and fy1 == fy2 + 1
    ):
        return "半年度披露"

    return normalize_frequency("", p1)


def build_target_name_short(target_year: int, target_period: str, freq: str, fye_month: int = 12) -> str:
    natural_year = int(target_year) if int(fye_month) == 12 else int(target_year) - 1
    yy = str(natural_year)[-2:]
    if is_quarterly_frequency(freq):
        mapping = {"Q1": "Q1", "Interim": "Q2", "Q3": "Q3", "Annual": "Q4"}
    else:
        mapping = {"Interim": "H1", "Annual": "H2"}
    suffix = mapping.get(target_period, "")
    return f"{yy}{suffix}" if suffix else ""


def adjust_target_year_after_phase1(
    target_year: int,
    target_period: str,
    fye_month: int,
    predicted_date_str: str,
    target_name_short: str,
    ref_year: int,
) -> int:
    """
    在新版 Phase1 后统一进行一次 FY 口径纠偏。
    优先使用 predicted_date_str 落窗，缺失时回退 target_name_short。
    """
    year = int(target_year)
    if int(fye_month) == 12 or target_period not in {"Q1", "Interim", "Q3", "Annual"}:
        return year

    s = str(target_name_short or "").strip().upper()
    m = re.search(r"(?:FY)?\s*(\d{2})\s*(?:Q[1-4]|H[12])\b", s)
    if m:
        yy = int(m.group(1))
        y1_candidates = [1900 + yy, 2000 + yy, 2100 + yy]
        y1_year = min(y1_candidates, key=lambda y: abs(y - (int(ref_year) - 1)))
        year = int(y1_year + 1)

    if not predicted_date_str:
        return year

    try:
        predicted_dt = datetime.strptime(predicted_date_str, "%Y-%m-%d")
    except Exception:
        return year

    period_month_offset = {"Annual": 0, "Interim": 6, "Q1": 3, "Q3": 9}

    def _window_for(fy_year: int) -> Tuple[datetime, datetime]:
        natural_y = int(fy_year) if int(fye_month) == 12 else int(fy_year) - 1
        end_month = ((int(fye_month) - 1 + period_month_offset[target_period]) % 12) + 1
        if int(fye_month) == 12:
            end_cal_year = natural_y
        else:
            end_cal_year = natural_y + 1 if end_month <= int(fye_month) else natural_y
        last_day = calendar.monthrange(end_cal_year, end_month)[1]
        period_end = datetime(end_cal_year, end_month, last_day)
        delay_months = 4 if target_period == "Annual" else 3
        return period_end, period_end + relativedelta(months=delay_months)

    cur_start, cur_end = _window_for(year)
    nxt_start, nxt_end = _window_for(year + 1)
    in_cur = cur_start <= predicted_dt <= cur_end
    in_nxt = nxt_start <= predicted_dt <= nxt_end
    if (not in_cur) and in_nxt:
        year += 1
    return int(year)


def advance_target_period(target_period: str, target_year: int, freq: str) -> Tuple[str, int]:
    """当董事会会议公告日期已兑现时，推进到下一财报季。"""
    if is_quarterly_frequency(freq):
        order = ["Q1", "Interim", "Q3", "Annual"]
    else:
        order = ["Interim", "Annual"]
    if target_period not in order:
        return target_period, target_year
    idx = order.index(target_period)
    if idx == len(order) - 1:
        return order[0], target_year + 1
    return order[idx + 1], target_year


def llm_extract_board_meeting_notice(
    stock_code: str,
    company_name: str,
    notice1_title: str,
    notice1_date: str,
    notice1_text: str,
    notice2_title: str = "",
    notice2_date: str = "",
    notice2_text: str = "",
) -> Dict[str, Any]:
    """
    直接用 LLM 解析“最近两份”董事会会议公告，抽取公告1关键字段。
    注意：target_period/target_year/fye 由本地规则推导，不由 LLM 直接输出。
    """
    body1 = _truncate_for_prompt(notice1_text, 8000 if USE_COMPACT_PROMPT else 25000)
    body2 = _truncate_for_prompt(notice2_text, 4000 if USE_COMPACT_PROMPT else 15000)
    prompt = f"""
你是资深港股财务披露分析师。今天是 {TODAY_DATE}。
你将看到同一家公司最近两份“董事会会议”公告（公告1较新，公告2较旧）。
请只提取公告1关键字段，不要自行推导目标期。

公司简称：{company_name}

【公告1（最新）】
标题：{notice1_title}
日期：{notice1_date}
正文（可能截断）：
{body1}

【公告2（上一份，可能为空）】
标题：{notice2_title}
日期：{notice2_date}
正文（可能截断）：
{body2}

要求：
1. 先从公告1正文中定位关键句，格式通常为：
   “截至yyyy年mm月dd日xxxx业绩/财报”
   其中 xxxx 常见：三个月、六个月、九个月、中期、半年、全年、中报、年报 等。
2. 披露日提取（predicted_date_str）：
   - 优先提取公告1正文里明确指向“未来披露日/刊发日/发布日/公布日”的日期语句，例如“将于YYYY年MM月DD日刊发/发布/公布…”；
   - 严禁把“截至yyyy年mm月dd日xxxx”里的日期当作 predicted_date_str；
   - 提醒：用于 notice1_year/notice1_month/notice1_xxxx 的日期通常前面有“截至”；
   - 若公告1正文没有明确刊发/发布日期语句，predicted_date_str 必须等于董事会会议日。
3. 频率判定（沿用旧规则）：
   - 若公告1或公告2任意一份出现“三个月/第一季度/Q1”或“九个月/第三季度/Q3”（含英文 Three months / First quarter / Nine months / Third quarter），即可判定为“季度披露”；
   - 即使同一句或同一标题同时出现“六个月/九个月/全年/年度”等其他更长期口径，只要任意一份公告出现“三个月”或“九个月”，frequency 仍判定为“季度披露”；
   - 若公告1和公告2都仅有“六个月/十二个月/半年/全年/中期/年度”等口径，仍不能直接判频；frequency 留空。
   - 无法判断时可留空。
4. 字段抽取：
   - 从公告1正文输出 notice1_year, notice1_month, notice1_xxxx
   - 从公告2正文输出 notice2_year, notice2_month, notice2_xxxx（若公告2为空则留空）
   - notice1_xxxx 必须保留完整期别短语；若同一句同时出现“三个月及六个月”/“三个月及九个月”/“三个月及全年”，或英文 three months and six months / nine months / full year，必须完整保留，不得只截取前半句“三个月”。
   - 只有当 notice1_xxxx 是“纯三个月”表述时，若无法直接区分 Q1/Q3，才结合公告2判断 notice1_three_month_period_hint（仅允许 Q1 或 Q3）。
   - 同时可输出 fye_hint（1-12 或 null），用于“纯三个月”场景辅助判断。
5. 若公告1/公告2能组成一对“纯Annual + 纯Interim”或“纯Interim + 纯Annual”（结合 notice1_year / notice2_year 判断为相邻半年度披露组合），frequency 应输出“半年度披露”。
   - 这里的“纯Annual/纯Interim”是指 xxxx 本身只体现年度/中期口径；
   - 若 xxxx 前面包含“三季度/九个月/Q3”等季度口径，则不算半年度配对。
6. 无法识别字段必须置空或 null，不要编造。只返回结论，不要解释。

仅返回 JSON，不要 Markdown，不要解释。格式如下：
{{
  "predicted_date_str": "",
  "frequency": "",
  "notice1_year": null,
  "notice1_month": null,
  "notice1_xxxx": "",
  "notice2_year": null,
  "notice2_month": null,
  "notice2_xxxx": "",
  "notice1_three_month_period_hint": "",
  "fye_hint": null
}}

"""

    if USE_COMPACT_PROMPT:
        prompt = build_compact_prompt(
            task_title=f"根据两份董事会会议公告抽取目标财报期（today={TODAY_DATE}）",
            rules=[
                "公告1优先；从公告1正文抽取 notice1_year/notice1_month/notice1_xxxx；从公告2正文抽取 notice2_year/notice2_month/notice2_xxxx。",
                "predicted_date_str 优先来自正文中明确指向未来披露日的语句，如“将于某日刊发/发布/公布”；不能取“截至yyyy年mm月dd日xxxx”中的日期。",
                "若正文无明确刊发/发布日期语句，predicted_date_str=董事会会议日。",
                "公告1或公告2任意一份出现“三个月/第一季度/Q1”或“九个月/第三季度/Q3”(含英文) => frequency=季度披露。",
                "即使同一句同时出现“六个月/九个月/全年/年度”等其他更长期口径，只要任意一份公告出现“三个月”或“九个月”，frequency 仍=季度披露。",
                "若公告1/公告2构成一对 纯Annual+纯Interim 或 纯Interim+纯Annual，且年份能对应相邻半年度披露组合 => frequency=半年度披露。",
                "若 xxxx 前面包含‘三季度/九个月/Q3’等季度口径，则不算半年度配对。",
                "若同一句同时出现“三个月”和更长期口径（如六个月/九个月/全年，或 six months / nine months / full year / annual），notice1_xxxx 必须保留完整组合，不要只写“三个月”。",
                "若公告1和公告2都仅含“六个月/十二个月/半年/全年/中期/年度”等口径，不能直接判频；frequency 留空。",
                "只有当 notice1_xxxx=纯三个月 时，才结合公告2输出 notice1_three_month_period_hint=Q1 或 Q3。",
                "可输出 fye_hint(1-12) 作为纯三个月场景辅助；无把握则给 null。",
                "无法识别字段必须留空或null，不得编造。",
            ],
            input_blocks=[
                ("公司简称", str(company_name or "")),
                ("公告1", f"标题: {notice1_title}\n日期: {notice1_date}\n正文:\n{body1}"),
                ("公告2", f"标题: {notice2_title}\n日期: {notice2_date}\n正文:\n{body2}"),
            ],
            output_schema_json="""{
  "predicted_date_str": "",
  "frequency": "",
  "notice1_year": null,
  "notice1_month": null,
  "notice1_xxxx": "",
  "notice2_year": null,
  "notice2_month": null,
  "notice2_xxxx": "",
  "notice1_three_month_period_hint": "",
  "fye_hint": null
}""",
        )

    result = {
        "predicted_date_str": "",
        "frequency": "",
        "notice1_year": None,
        "notice1_month": None,
        "notice1_xxxx": "",
        "notice2_year": None,
        "notice2_month": None,
        "notice2_xxxx": "",
        "notice1_three_month_period_hint": "",
        "fye_hint": None,
    }
    raw_text = ""
    try:
        user_prompt_est_tokens = _estimate_tokens_rough(prompt)
        pdf_est_tokens = _estimate_tokens_rough(body1) + _estimate_tokens_rough(body2)
        prompt_only_est_tokens = max(0, user_prompt_est_tokens - pdf_est_tokens)
        raw_text = call_ds_llm(
            stock_code=stock_code,
            system_prompt="你是一位资深的港股/A股财务专家。严格返回JSON格式数据。",
            user_prompt=prompt,
            temperature=0.0,
            request_tag="phase1_hkex_board",
            prompt_only_token_estimate=prompt_only_est_tokens,
            pdf_token_estimate=pdf_est_tokens,
        )
        parsed = _safe_load_json_from_text(raw_text)
        if not parsed:
            raise ValueError("LLM 返回无法解析为 JSON 对象")

        frequency = normalize_frequency(parsed.get("frequency"), "")
        notice1_year = _parse_year_value(parsed.get("notice1_year"))
        notice1_month = _parse_month_value(parsed.get("notice1_month"))
        notice1_xxxx = str(parsed.get("notice1_xxxx", "") or "").strip()
        notice2_year = _parse_year_value(parsed.get("notice2_year"))
        notice2_month = _parse_month_value(parsed.get("notice2_month"))
        notice2_xxxx = str(parsed.get("notice2_xxxx", "") or "").strip()
        three_month_hint = normalize_target_period(parsed.get("notice1_three_month_period_hint"))
        if three_month_hint not in {"Q1", "Q3"}:
            three_month_hint = ""
        fye_hint = normalize_fye_month(parsed.get("fye_hint"))

        predicted_date = normalize_date_ymd(parsed.get("predicted_date_str"))

        result.update({
            "predicted_date_str": predicted_date,
            "frequency": frequency,
            "notice1_year": notice1_year,
            "notice1_month": notice1_month,
            "notice1_xxxx": notice1_xxxx,
            "notice2_year": notice2_year,
            "notice2_month": notice2_month,
            "notice2_xxxx": notice2_xxxx,
            "notice1_three_month_period_hint": three_month_hint,
            "fye_hint": fye_hint,
        })
        print(
            f"📌 [phase1_hkex_board] predicted_date_str={result['predicted_date_str']}, "
            f"frequency={result['frequency']}, notice1_year={result['notice1_year']}, "
            f"notice1_month={result['notice1_month']}, notice1_xxxx={result['notice1_xxxx']}, "
            f"notice2_year={result['notice2_year']}, notice2_month={result['notice2_month']}, "
            f"notice2_xxxx={result['notice2_xxxx']}, "
            f"notice1_three_month_period_hint={result['notice1_three_month_period_hint']}, "
            f"fye_hint={result['fye_hint']}"
        )
    except Exception as e:
        print(f"⚠️ 董事会会议公告 LLM 解析失败: {e}")
        if raw_text:
            print(f"   [DEBUG] 原始返回: {raw_text[:400]}...")
    return result


# =========================================================
# 6) LLM 智能解析 - 动态两步雷达
# =========================================================
# import google.generativeai as genai # Removed as per instruction

# genai.configure(api_key=GOOGLE_API_KEY) # Removed as per instruction
# model = genai.GenerativeModel('gemini-2.0-flash') # Removed as per instruction
# generation_config = genai.types.GenerationConfig(temperature=0.0) # Removed as per instruction

def infer_fye_from_titles(df: pd.DataFrame) -> Optional[int]:
    """
    从公告标题中按优先级推断财年截止月份。
    优先级：年报 > 中报/半年度 > 季报 > 发布日期推测 > 默认12。
    一旦在某优先级命中就立即返回。
    """
    if df.empty:
        return None
    
    # ====== 优先级1：年报标题直接提取 ======
    # 匹配"截至...X月X日止年度/全年/十二个月" 或 "年度业绩/年度报告"
    for _, row in df.iterrows():
        title = zhconv.convert(str(row.get('reportTitle', '')), 'zh-cn')
        # "截至...X月X日止年度" / "截至...X月X日止全年"
        m = re.search(r'截至.*?(\d{1,2})\s*月\s*\d{1,2}\s*日\s*止\s*(年度|全年|十二个月)', title)
        if m:
            month = int(m.group(1))
            if 1 <= month <= 12:
                print(f"   ✅ [财年推断] 从年报标题提取: fye={month} (标题: {title[:40]}...)")
                return month
        # 英文: "year ended XX December/March..."
        month_en_map = {
            'january': 1, 'february': 2, 'march': 3, 'april': 4,
            'may': 5, 'june': 6, 'july': 7, 'august': 8,
            'september': 9, 'october': 10, 'november': 11, 'december': 12
        }
        m_en = re.search(r'(?:year|annual).*?ended.*?(\d{1,2})\s+(january|february|march|april|may|june|july|august|september|october|november|december)', title.lower())
        if not m_en:
            m_en = re.search(r'(?:year|annual).*?ended.*?(january|february|march|april|may|june|july|august|september|october|november|december)', title.lower())
        if m_en:
            month_name = m_en.group(m_en.lastindex)
            month = month_en_map.get(month_name)
            if month:
                print(f"   ✅ [财年推断] 从年报英文标题提取: fye={month} (标题: {title[:40]}...)")
                return month

    # ====== 优先级2：中期/半年度标题反推 ======
    # 匹配"截至...X月X日止六个月/中期" → 提取月份 + 6
    for _, row in df.iterrows():
        title = zhconv.convert(str(row.get('reportTitle', '')), 'zh-cn')
        m = re.search(r'截至.*?(\d{1,2})\s*月\s*\d{1,2}\s*日\s*止\s*(六个月|中期|半年)', title)
        if m:
            interim_month = int(m.group(1))
            fye = interim_month + 6
            if fye > 12:
                fye -= 12
            if 1 <= fye <= 12:
                print(f"   ✅ [财年推断] 从中期标题反推: 中期截止月={interim_month} → fye={fye} (标题: {title[:40]}...)")
                return fye
        # 标题含"中期业绩/中期报告/半年度"但无"截至"的情况，跳过（无月份线索）

    # ====== 优先级3：季报标题反推 ======
    for _, row in df.iterrows():
        title = zhconv.convert(str(row.get('reportTitle', '')), 'zh-cn')
        # "截至...X月X日止三个月" → 一季度，截止月-3 = 上一年度末
        m_q1 = re.search(r'截至.*?(\d{1,2})\s*月\s*\d{1,2}\s*日\s*止\s*(三个月|一季|第一季)', title)
        if m_q1:
            q1_month = int(m_q1.group(1))
            fye = q1_month - 3
            if fye <= 0:
                fye += 12
            if 1 <= fye <= 12:
                print(f"   ✅ [财年推断] 从Q1标题反推: Q1截止月={q1_month} → fye={fye} (标题: {title[:40]}...)")
                return fye
        # "截至...X月X日止九个月" → 三季度，截止月+3 = 年度末
        m_q3 = re.search(r'截至.*?(\d{1,2})\s*月\s*\d{1,2}\s*日\s*止\s*(九个月|三季|第三季)', title)
        if m_q3:
            q3_month = int(m_q3.group(1))
            fye = q3_month + 3
            if fye > 12:
                fye -= 12
            if 1 <= fye <= 12:
                print(f"   ✅ [财年推断] 从Q3标题反推: Q3截止月={q3_month} → fye={fye} (标题: {title[:40]}...)")
                return fye

    # ====== 优先级4：根据发布日期推测 ======
    # 逻辑：年报通常在财年结束后 3-4 个月发布
    for _, row in df.iterrows():
        title = zhconv.convert(str(row.get('reportTitle', '')), 'zh-cn')
        report_date = str(row.get('reportDate', ''))
        # 只对含"年度/全年/年报"关键词的公告使用此推测
        if any(kw in title for kw in ['年度业绩', '全年业绩', '年度报告', '年报', '全年报告']):
            try:
                dt = datetime.strptime(report_date[:10], "%Y-%m-%d")
                pub_month = dt.month
                # 3-4月发布 → fye=12; 6-7月发布 → fye=3; 9-10月发布 → fye=6; 10-11月发布 → fye=8
                if pub_month in (3, 4):
                    fye = 12
                elif pub_month in (6, 7):
                    fye = 3
                elif pub_month in (9, 10):
                    fye = 6
                elif pub_month in (10, 11):
                    fye = 8
                else:
                    continue
                print(f"   ✅ [财年推断] 从年报发布日期反推: 发布月={pub_month} → fye={fye} (日期: {report_date[:10]})")
                return fye
            except Exception:
                continue

    # ====== 优先级5：默认兜底 ======
    return None  # 返回None，让调用方使用LLM兜底或默认12


def llm_identify_target_period(company_name: str, df: pd.DataFrame, stock_code: str) -> Optional[Dict]:
    """Phase 1: 找出预期目标期数及对应年份"""
    if df.empty: return None
    df_for_llm = df.copy()
    df_for_llm['id'] = range(len(df_for_llm))
    records = df_for_llm[['id', 'reportDate', 'reportTitle']].to_dict('records')
    
    prompt = f"""
    你是一位资深的港股/A股财务专家。今天是 {TODAY_DATE}。
    这是【{company_name}】过去近半年多的财报公告名称列表。
    
    【任务】：推断本公司未来最近一个需披露业绩的【目标财报季】及【对应归属年份】。
    
    【推理逻辑 - 请严格按以下步骤推理】：
    
    第一步：判断披露频率
    - 观察列表，仅当存在【正式季度业绩公告】时，才判断为【季度披露】。
      正式季度业绩的标志：标题含"一季度""三季度""三个月""Q1""Q3""三季报"等字样的《业绩公告》或《经审业绩》。
    - 【关键区别】："季度营运更新""季度交付量""季度订单"等《营运数据》公告，即便要求季度更新也不代表该公司发布季度财务报表。这种公司仍判定为【半年度披露】。
    - 如果近半年多只有中报或年报相关，则为【半年度披露】
    
    第二步：识别列表中最新已发布的母公司财报是哪一期
    - 从公告标题中找到覆盖最晚财务期间的那条记录（注意排除子公司/附属公司的公告）
    - 例如标题含"九月三十日止九個月"或"第三季度"说明Q3已发布；含"六月三十日止六個月"说明Interim已发布
    
    第三步：根据已发布的最新财报，推算下一个未发布的财报季
    - 季度披露的顺序循环为：Q1 → Interim → Q3 → Annual → Q1(下年) → ...
    - 半年度披露的顺序循环为：Interim → Annual → Interim(下年) → ...
    - 找到已发布的最新一期在循环中的位置，取下一个即为目标
    - target_year 是目标财报数据所归属的财务年份
    
    举例（假设今天是2026年3月1日）：
    - 如果列表中最新母公司财报是2025年Q3（即九月三十日止九个月），那么下一个就是2025 Annual（target_year=2025）
    - 如果列表中最新母公司财报是2025年Annual（全年业绩），那么下一个就是2026 Q1（target_year=2026，季度）或2026 Interim（target_year=2026，半年度）
    
    【返回格式】：必须且只能返回一个纯 JSON 对象，不要含有任何 Markdown。包含以下字段：
    "frequency": "季度披露" 或 "半年度披露"
    "target_period": "Q1", "Interim", "Q3", "Annual" 之一
    "fiscal_year_end_month": 公司财年结束的月份（整数 1-12）。请将中文数字转换为阿拉伯数字，并严格按以下优先级推断：
      - 如有年报：直接提取年报：若公告标题含“截至X月X日止年度/全年业绩/年度报告”，直接提取该月份。
      - 如有中报：反推中期/半年度：若标题含“中期/半年度/截至X月X日止六个月”，提取公告标题中的X月份并加 6（若结果 >12 则减 12）。
      - 如有季报：反推季度：若标题为“一季度”且截至X月，提取该月份并减 3（若 ≤0 则加 12）；若为“三季度”且截至X月，提取该月份并加 3（若 >12 则减 12）。
      - 结合发布日期：若标题完全无月份线索，根据发布日期所在月份推测：3-4月发布对应 12；6-7月发布对应 3；9-10月发布对应 6；10-11月发布对应 8。默认兜底：若以上条件均不满足，默认输出 12。大部分公司是12（自然年），但港股常见：3（如阿里巴巴、联想）、5（如新东方）、6（部分地产/教育）、8（部分教育）。如果标题中找不到线索，默认为12。
    "target_year": 预期财报的归属年份 (整数，代表财务数据的所属年份)，推理逻辑为，对比标题中“截至yyyy年mm月dd日x个月业绩”
      - 如果mm≤fiscal_year_end_month，财年=yyyy，否则财年=yyyy+1
    "target_name_short": 直接拼接出【最终将被测算出来的这一期独立单期名】，提取的实际发生财务数据通常属于 target_year - 1 的数据（即 Y-1 期）。格式为 “Y-1年份后两位 + Q/H”。
      - 规则：如果 frequency 是“半年度披露”，目标Annual由于要扣除Interim(中报)，请标注为【(target_year-1)的两位数+H2】。目标Interim无需扣减，标为【(target_year-1)的两位数+H1】。
      - 如果 frequency 是“季度披露”，目标Q1是【(target_year-1)+Q1】，目标Interim扣了Q1是【(target_year-1)+Q2】，目标Q3扣了Interim是【(target_year-1)+Q3】，目标Annual扣了Q3是【(target_year-1)+Q4】。
      - 例如如果 target_year 是 2025，那么说明我们在推算测取过去 2024 年的业绩。如果它是半年度披露且 target_period 是 Annual：返回 "24H2"。季度披露且 target_period 是 Interim(需要扣一季报)：返回 "24Q2"。
    
    【待处理 JSON】：
    {json.dumps(records, ensure_ascii=False)}
    """
    if USE_COMPACT_PROMPT:
        prompt = build_compact_prompt(
            task_title=f"识别公司下一个目标财报季与归属年份（today={TODAY_DATE}）",
            rules=[
                "根据公告标题判断频率：存在正式季报关键词才判季度披露，否则半年度披露。",
                "先识别最新已发布财报期，再推下一个未发布财报期。",
                "target_period 仅允许 Q1/Interim/Q3/Annual。",
                "target_year 为目标财报所属年份（整数）。",
                "fiscal_year_end_month 输出 1-12，无法确定可给12。",
                "仅输出JSON，不要解释。",
            ],
            input_blocks=[
                ("公司简称", str(company_name or "")),
                ("公告列表JSON", json.dumps(records, ensure_ascii=False)),
            ],
            output_schema_json="""{
  "frequency": "",
  "target_period": "",
  "target_year": null,
  "fiscal_year_end_month": null,
  "target_name_short": ""
}""",
        )
    print("\n⏳ [阶段一] 正在呼叫 DS 识别预期基准期数及年份...")
    text = None
    try:
        prompt_est_tokens = _estimate_tokens_rough(prompt)
        text = call_ds_llm(
            stock_code=stock_code,
            system_prompt="你是一位资深的港股/A股财务专家。严格返回JSON格式数据。",
            user_prompt=prompt,
            temperature=0.0,
            request_tag="phase1_ifind_fallback",
            prompt_only_token_estimate=prompt_est_tokens,
            pdf_token_estimate=0,
        )
        print(f"\n[DEBUG Phase1] 原生 LLM 返回:\n{text}\n")
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(text)
        print(f"🎯 LLM 锁定目标期数为：[{result['target_year']}年 {result['target_period']}]")
        return result
    except Exception as e:
        err_msg = text if text is not None else 'None'
        print(f"❌ LLM 解析失败: {e}\n原文: {err_msg}")
        return None

def llm_find_historical_reports(
    company_name: str,
    df: pd.DataFrame,
    target_years: List[int],
    target_period: str,
    prior_target_period: Optional[str] = None,
    stock_code: str = "",
    **kwargs,
) -> List[int]:
    """Phase 2: 在狭窄区间找同期的历史"""
    if df.empty: return []
    df_for_llm = df.copy()
    df_for_llm['id'] = range(len(df_for_llm))
    records = df_for_llm[['id', 'reportDate', 'reportTitle']].to_dict('records')
    fye_month_raw = kwargs.get("fye_month")
    fye_month = None
    try:
        if fye_month_raw is not None and str(fye_month_raw).strip() != "":
            fye_month = int(fye_month_raw)
    except Exception:
        fye_month = None
    
    years_str = "、".join([str(y) for y in target_years if isinstance(y, int)])
    if years_str:
        target_str = f"查找主要目标：{years_str}年对应的 [{target_period}] 财务业绩公告或运营数据。"
    else:
        target_str = f"查找主要目标：对应 [{target_period}] 财务业绩公告或运营数据。"
    y1_ref = target_years[0] if (target_years and isinstance(target_years[0], int)) else "目标年"
    if prior_target_period:
         target_str += f"\n此外，还需要提取出 {y1_ref} 年的 [{prior_target_period}] 业绩报告或其核心营运数据公告，用于后续扣减计算。"
    fye_desc = str(fye_month) if (isinstance(fye_month, int) and 1 <= fye_month <= 12) else "未知"
    fye_mapping_note = ""
    if isinstance(fye_month, int) and 1 <= fye_month <= 12:
        period_to_end_month = {
            "Q1": ((fye_month - 1 + 3) % 12) + 1,
            "Interim": ((fye_month - 1 + 6) % 12) + 1,
            "Q3": ((fye_month - 1 + 9) % 12) + 1,
            "Annual": fye_month,
        }
        fye_mapping_note = (
            f"\n财年口径提示（FYE={fye_month}月）:"
            f"\n- Q1期末月={period_to_end_month['Q1']}月"
            f"\n- Interim期末月={period_to_end_month['Interim']}月"
            f"\n- Q3期末月={period_to_end_month['Q3']}月"
            f"\n- Annual期末月={period_to_end_month['Annual']}月"
        )
    
    prompt = f"""
    你是一个深谙港股披露规则的助理。
    现在有【{company_name}】的以下合并公告列表（已按时间倒序粗筛完成）。
    {target_str}
    当前已知财年结束月 FYE={fye_desc}（若非12，必须按财年口径理解标题）{fye_mapping_note}
    
    注意：
    1. 不一定每年都有对应的标题，没有则找最符合的主季度或主半年度财报。
    2. 如果标题中出现了完全不相干的其他公司名字（而不是该上市公司的子公司通常表述），这说明有可能是上市公司的投资对象的财务数据，我们不需要，请标false。
    3. 当针对同一个财报截止日（如 12月31日），列表中同时出现了两份公告：一份是纯粹的“年度业绩/年报”，另一份在标题中明确带有“季度”、“第四季度”或“三个月”等字样（例如“第四季度及全年未经审计财务业绩”），请【只保留】带有“季度”字样的那一份（标为 true），并将纯年度公告过滤掉（标为 false）。
    4. 我们需要的是这家上市公司自身的财务数据业绩报告。如果标题明确指明是 "一间附属公司的业绩" 等单纯子公司的剥离披露，我们必须过滤掉（标为 false）。
    5. 目标期是按“财务期间口径”匹配，不按公告发布日期月份匹配。尤其在非12月财年时，不要按自然年误判期别。
    6. 若标题为“截至X月X日止季度/九个月/六个月/十二个月”等，请优先按截止月和FYE映射判断其属于 Q1/Interim/Q3/Annual。
    7. 示例：若 FYE=3 月，则“截至12月31日止季度/九个月”属于 Q3；“截至3月31日止十二个月/年度”属于 Annual。
    
    【返回格式】：必须且只能返回纯 JSON，不要Markdown。只返回结论，不要解释。
    返回的 JSON 应是一个列表，其中的 object 形如 {{"id": x, "match": true/false}}
    
    【待处理 JSON】：
    {json.dumps(records, ensure_ascii=False)}
    """
    if USE_COMPACT_PROMPT:
        prompt = build_compact_prompt(
            task_title=f"在公告列表中匹配 YoY 同期目标（{years_str}, 目标期={target_period}）",
            rules=[
                "只保留上市公司自身财务业绩相关公告，过滤明显无关公司或纯子公司剥离口径。",
                "同截止日若同时有季度版和纯年度版，优先季度版，年度版标 false。",
                "必须使用 FYE 财年口径判断期别，不按自然年直觉判断。",
                "若标题出现‘截至X月X日止季度/九个月/六个月/十二个月’，优先按截止月与 FYE 映射到 Q1/Interim/Q3/Annual。",
                "例如 FYE=3 时，12月期末通常对应 Q3，3月期末通常对应 Annual。",
                "缺失年份允许不匹配，不要硬凑。",
                "只返回 id+match JSON 列表。",
            ],
            input_blocks=[
                ("公司简称", str(company_name or "")),
                ("目标说明", target_str),
                ("候选公告JSON", json.dumps(records, ensure_ascii=False)),
            ],
            output_schema_json='[{"id": 0, "match": true}]',
        )
    year_count = len([y for y in target_years if isinstance(y, int)])
    print(f"\n⏳ [阶段二] 正在呼叫 DS 一次性判定 {year_count} 年 ({years_str}) 的 YoY 同期匹配度...")
    text = None
    try:
        prompt_est_tokens = _estimate_tokens_rough(prompt)
        text = call_ds_llm(
            stock_code=stock_code,
            system_prompt="你是一位资深的港股/A股财务专家。严格返回JSON格式数据。",
            user_prompt=prompt,
            temperature=0.0,
            request_tag="phase2_hist_match",
            prompt_only_token_estimate=prompt_est_tokens,
            pdf_token_estimate=0,
        )
        print(f"\n[DEBUG] 原生 LLM 返回内容:\n{text}\n")
        text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        if text is not None:
             res_list = json.loads(text)
             print(f"[DEBUG] JSON 解析结果:\n{res_list}\n")
             return [item['id'] for item in res_list if item.get('match') is True]
    except Exception as e:
        err_msg = text if text is not None else 'None'
        print(f"LLM 解析错误 Phase 2: {e}\n原文: {err_msg}")
    return []

def download_and_extract_pdf_text_for_financials(url: str, max_pages: int = 30) -> str:
    """下载 PDF 并使用 PyMuPDF 提取前 N 页文本进行 Phase 3 分析"""
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(response.content)
            tmp_path = tmp.name
        
        text = ""
        doc = fitz.open(tmp_path)
        for i, page in enumerate(doc):
            if i >= max_pages: break
            text += page.get_text()
        doc.close()
        os.remove(tmp_path)
        return text
    except Exception as e:
        print(f"Error extracting PDF {url}: {e}")
        return ""

def llm_extract_financial_data(text: str, is_main: bool, period_name: str, stock_code: str) -> dict:
    if not text:
        return {}

    prompt = f"""
    你是一个资深的港股/A股财务分析师。这是一份财务报告（前30页文字提取）。
    你需要从中提取本期（{period_name}）的财务数据，分为【累计数据】和【单季度数据】两组。

    ===== 第一步：确认报告中财务报表使用的单位和货币 =====
    在财务报表/损益表中找到单位声明：
    - "千元" / "RMB'000" → 单位是"千元"
    - "百万元" / "RMB million" / "US$ million" → 单位是"百万元"
    - "元" / "RMB" → 单位是"元"
    - "亿元" → 单位是"亿元"
    请将识别到的单位填入 detected_unit。
    
    同时识别报表使用的计价货币：
    - 人民币/RMB/CNY → "RMB"
    - 港币/港元/HK$/HKD → "HKD"
    - 美元/美金/US$/USD → "USD"
    - 其他货币请直接写货币代码
    请将识别到的货币填入 detected_currency。

    ===== 第二步：提取数据并统一转为亿元 =====
    根据第一步确认的单位，转换规则：
    - "元"：÷ 100,000,000
    - "千元"：÷ 100,000
    - "百万元"：÷ 100
    - "亿元"：直接使用
    
    示例：报告单位为"千元"，数值=502,251,312 → 502,251,312 ÷ 100,000 = 5022.51 亿元

    需要提取的三个核心指标：
    1. revenue (营业收入/營業收入/Revenue)
    2. gross_profit (毛利/Gross Profit)
       ⚠️ 注意：部分A股报表中没有单独的"毛利"行，此时必须用"营业收入 - 营业成本"计算得出毛利。
       同理，港股报表中有时用"Cost of Sales/销售成本/營業成本"项，毛利 = Revenue - Cost of Sales。
       请务必尝试通过以上方式计算出 gross_profit，不要轻易填 null。
    3. net_profit (归属于母公司所有者的净利润/本公司拥有人应占溢利，如果没有则用净利润/净亏损)

    ===== 第三步：分别提取累计和单季度两组数据 =====
    
    【累计数据 (cumulative)】：年初至期末的累计金额。
    - 对于年报(Annual)，就是全年数据
    - 对于Q3/Interim/Q1，就是年初至该季末的累计
    
    【单季度数据 (single_quarter)】：仅本季度/本期独立时段的数据。
    - 比如报告标题含"第四季度"，则提取仅第四季度的数据
    - 比如出现"截至XX止三个月/Three months ended"的独立数据
    - ⚠️极度关键：很多公司的【年度报告】或【全年业绩】中，并没有显式的一张表叫"第四季度"，而是会在文字描述、图表或管理层讨论（MD&A）中提到"第四季度收入为 XXX"、"第四季度经调整净利润为 XXX"、"单季毛利率为XX%"。
    - ⚠️极度关键：请你务必像侦探一样，在全文搜索"第四季"、"第四季度"、"Q4"、"最后三个月"、"单季"等字眼！只要文中提到了这三个核心指标中任何一个的**单季度具体金额**，请务必提取到 `single_quarter` 中，绝对不要漏掉！
    - 如果报告中提到了单季度的毛利率（如"第四季度毛利率为13.3%"），请填入 gross_margin_pct
    
    【完整度判定】：
    - "false"：该组数据完全没有找到
    - "partially_true"：找到了1~2个核心指标（或找到了毛利率但没有毛利金额）
    - "totally_true"：3个核心指标全部找到

    ===== 其他重要规则 =====
    - ⚠️ 符号处理："净亏损/淨虧損/Net Loss" → net_profit 填负数。"毛損/Gross Loss" → gross_profit 填负数。
    - 归母净利润容错：优先找"归属于母公司所有者的净利润/本公司拥有人应占溢利"，退而求其次用"净利润/净亏损"。
    - ⚠️ 自检：三个指标必须使用同一单位基准转换。毛利通常是营收的5%~40%。
    - ⚠️ 自检：仔细检查文字段落，单季度数据可能隐藏在某一句"第四季度，我们的收入达到..."中。
    - 所有数值保留两位小数。找不到的填 null。

    【返回格式】：必须且只能返回纯 JSON，不要 Markdown。只返回结论，不要解释。
    {{
      "detected_unit": "千元",
      "detected_currency": "RMB",
      "cumulative": {{
        "revenue": 321.64,
        "gross_profit": 26.94,
        "net_profit": -28.21
      }},
      "single_quarter": {{
        "revenue": null,
        "gross_profit": null,
        "net_profit": 0.8,
        "gross_margin_pct": 13.3
      }},
      "has_cumulative_data": "totally_true",
      "has_single_quarter_data": "partially_true"
    }}
    """
    if USE_COMPACT_PROMPT:
        prompt = build_compact_prompt(
            task_title=f"提取{period_name}财务三指标（累计+单季度）",
            rules=[
                "先识别单位和货币：元/千元/百万元/亿元；RMB/HKD/USD等。",
                "金额最终必须统一换算为“亿元”后再输出：元÷1e8，千元÷1e5，百万元÷100，亿元不变。",
                "提取 revenue、gross_profit、net_profit；毛利缺失可尝试 revenue-cost 计算。",
                "single_quarter.gross_margin_pct 必须尽量给出；若文本未直接给毛利率但有单季 revenue 与 gross_profit，请按 gross_profit/revenue*100 计算。",
                "若报告文字出现“毛利率/毛利率为/综合毛利率/gross margin”并对应本期，请优先提取该百分比。",
                "single_quarter 允许来自文字段落/MD&A 的“第四季度/Q4/三个月”披露，不要求必须在财务报表主表中出现。",
                "净亏损/Net Loss 填负数；毛损/Gross Loss 填负数；优先“归母净利润/本公司拥有人应占溢利”。",
                "同时给 cumulative 与 single_quarter，找不到填 null。",
                "输出 has_cumulative_data 与 has_single_quarter_data，取值 false/partially_true/totally_true。",
                "所有金额保留两位小数；不得凭空估算。",
                "仅返回 JSON，不要解释。",
            ],
            input_blocks=[
                ("期别", period_name),
                ("是否主期", "true" if is_main else "false"),
            ],
            output_schema_json="""{
  "detected_unit": "",
  "detected_currency": "",
  "cumulative": {"revenue": null, "gross_profit": null, "net_profit": null},
  "single_quarter": {"revenue": null, "gross_profit": null, "net_profit": null, "gross_margin_pct": null},
  "has_cumulative_data": "false",
  "has_single_quarter_data": "false"
}""",
        )

    report_text_for_prompt = _truncate_for_prompt(text, 12000 if USE_COMPACT_PROMPT else len(text))
    try:
        user_prompt_for_call = prompt + "\n【报告内容截取】\n" + report_text_for_prompt
        user_prompt_est_tokens = _estimate_tokens_rough(user_prompt_for_call)
        pdf_est_tokens = _estimate_tokens_rough(report_text_for_prompt)
        prompt_only_est_tokens = max(0, user_prompt_est_tokens - pdf_est_tokens)
        res_text = call_ds_llm(
            stock_code=stock_code,
            system_prompt="你是一位资深的港股/A股财务专家。严格返回JSON格式数据。",
            user_prompt=user_prompt_for_call,
            temperature=0.0,
            request_tag="phase3_fin_extract",
            prompt_only_token_estimate=prompt_only_est_tokens,
            pdf_token_estimate=pdf_est_tokens,
        )
        res_text = res_text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        parsed = json.loads(res_text)
        if isinstance(parsed, dict):
            parsed = _enrich_financial_margin_fields(parsed)
        return parsed
    except Exception as e:
        print(f"LLM 财务提取错误: {e}")
        return {}

def _to_float(val):
    """安全转浮点"""
    return float(val) if val is not None else None

def compute_fy_label(natural_year: int, period: str, freq: str, fye_month: int) -> str:
    """
    将自然年度的单期映射到财年(FY)口径名称。
    原则：以该期期末所在日历年份为 FY 年度。

    关键逻辑：财年 natural_year 表示财务年度（非日历年）。
    对于非标准财年(fye != 12)：
      - Annual  期末 = (natural_year+1)年的 fye_month  月
      - Q3     期末 = natural_year   年的 (fye_month - 3) 月或跨年
      - Interim 期末 = natural_year   年的 (fye_month - 6) 月或跨年
      - Q1     期末 = natural_year   年的 (fye_month - 9) 月或跨年

    举例：
      联想（fye=3）：自然年 2024 Annual -> 期末 2025-03(FY25) -> FY25Q4
      联想（fye=3）：自然年 2024 Q3 -> 期末 2024-12(FY25) -> FY25Q3
      联想（fye=3）：自然年 2024 Interim -> 期末 2024-09(FY25) -> FY25Q2
      联想（fye=3）：自然年 2024 Q1 -> 期末 2024-06(FY25) -> FY25Q1
      标准年（fye=12）：自然年 2024 Annual -> 期末 2024-12(FY24) -> FY24Q4
    """
    if fye_month == 12:
        # 标准财年：期末年 == 自然年
        fy_year = natural_year
    else:
        # 非标准财年：财年第一天 = (natural_year)年的 fye_month+1 月
        # Annual 期末永远是 (natural_year+1)年的 fye_month 月
        # 其他期根据期末月判断是否在同一日历年
        # 期末月区间： Annual=fye, Q3=fye-3, Interim=fye-6, Q1=fye-9
        period_offset = {"Annual": 0, "Q3": 3, "Interim": 6, "Q1": 9}  # 小于 Annual 的月数
        offset = period_offset.get(period, 0)
        # Annual 期末在下一个日历年
        if offset == 0:
            fy_year = natural_year + 1
        else:
            # 其他期：期末日历月 = fye_month - offset
            end_month = fye_month - offset
            if end_month <= 0:
                end_month += 12
                fy_year = natural_year + 1
            else:
                fy_year = natural_year  # 期末还在财务年自身的日历年
            # 但注意：这个日历年对应的财年 = natural_year + 1
            # 因为财年 natural_year 的 Q1/Q2/Q3 期末都在
            # (natural_year)年的 (fye_month+1)月 到 (natural_year+1)年的 fye_month月 之间
            # 即都属于财年 natural_year+1
            fy_year = natural_year + 1

    fy_yy = str(fy_year)[-2:]
    period_to_qnum = {"Annual": 4, "Q3": 3, "Interim": 2, "Q1": 1}
    period_to_hnum = {"Annual": 2, "Interim": 1}

    if freq == "季度披露":
        q = period_to_qnum.get(period, 4)
        return f"FY{fy_yy}Q{q}"
    else:
        h = period_to_hnum.get(period, 2)
        return f"FY{fy_yy}H{h}"

def resolve_financials(main_data: dict, prior_data: dict, prior_period: str):
    """
    逐字段最优化解算。
    优先级：
      1. 主期 single_quarter totally_true → 全用单季
      2. 主期 cum totally_true + 附期 cum totally_true → 全用累计扣减
      3. 逐字段混合：单季值(如有) → 扣减值(如有) → 毛利率派生 → None
    """
    main_sq = main_data.get("single_quarter", {}) or {}
    main_cum = main_data.get("cumulative", {}) or {}
    prior_sq = prior_data.get("single_quarter", {}) or {}
    prior_cum = prior_data.get("cumulative", {}) or {}

    main_sq_status = main_data.get("has_single_quarter_data", "false")
    main_cum_status = main_data.get("has_cumulative_data", "false")
    prior_cum_status = prior_data.get("has_cumulative_data", "false")

    print(f"   [状态] 主期: cum={main_cum_status}, sq={main_sq_status}")
    if prior_period:
        print(f"   [状态] 附期: cum={prior_cum_status}, sq={prior_data.get('has_single_quarter_data', 'false')}")

    final_revenue, final_gross, final_net = None, None, None

    if not prior_period:
        final_revenue = _to_float(main_cum.get("revenue"))
        final_gross = _to_float(main_cum.get("gross_profit"))
        final_net = _to_float(main_cum.get("net_profit"))
        print("   [路径] 无需扣减，直接使用累计数据。")
        return final_revenue, final_gross, final_net, None

    # 准备路径A：单季直取
    sq_rev = _to_float(main_sq.get("revenue"))
    sq_gross = _to_float(main_sq.get("gross_profit"))
    sq_net = _to_float(main_sq.get("net_profit"))
    sq_margin = _to_float(main_sq.get("gross_margin_pct"))

    # 准备路径B：累计扣减
    ded_rev, ded_gross, ded_net = None, None, None
    can_deduct = (main_cum_status in ["totally_true", "partially_true"] and
                  prior_cum_status in ["totally_true", "partially_true"])
    if can_deduct:
        mc_rev, mc_gross, mc_net = _to_float(main_cum.get("revenue")), _to_float(main_cum.get("gross_profit")), _to_float(main_cum.get("net_profit"))
        pc_rev, pc_gross, pc_net = _to_float(prior_cum.get("revenue")), _to_float(prior_cum.get("gross_profit")), _to_float(prior_cum.get("net_profit"))
        if mc_rev is not None and pc_rev is not None: ded_rev = round(mc_rev - pc_rev, 2)
        if mc_gross is not None and pc_gross is not None: ded_gross = round(mc_gross - pc_gross, 2)
        if mc_net is not None and pc_net is not None: ded_net = round(mc_net - pc_net, 2)

    # 优先级1：主期单季全部齐全
    if main_sq_status == "totally_true":
        print("   [路径] 主报告单季数据完整(totally_true)，直接使用。")
        final_revenue, final_gross, final_net = sq_rev, sq_gross, sq_net
    # 优先级2：累计扣减全部可行
    elif can_deduct and ded_rev is not None and ded_gross is not None and ded_net is not None:
        print("   [路径] 主附期累计数据均完整，使用累计扣减。")
        final_revenue, final_gross, final_net = ded_rev, ded_gross, ded_net
    # 优先级3：逐字段混合
    else:
        print("   [路径] 进入逐字段混合解算...")
        for field, sq_val, ded_val, label in [
            ("revenue", sq_rev, ded_rev, "收入"),
            ("net_profit", sq_net, ded_net, "净利"),
        ]:
            if sq_val is not None:
                print(f"      · {label}: 使用单季值 {sq_val}")
                if field == "revenue": final_revenue = sq_val
                else: final_net = sq_val
            elif ded_val is not None:
                print(f"      · {label}: 使用扣减值 {ded_val}")
                if field == "revenue": final_revenue = ded_val
                else: final_net = ded_val
            else:
                print(f"      · {label}: 无法获取")

        if sq_gross is not None:
            final_gross = sq_gross
            print(f"      · 毛利: 使用单季值 {sq_gross}")
        elif ded_gross is not None:
            final_gross = ded_gross
            print(f"      · 毛利: 使用扣减值 {ded_gross}")
        elif sq_margin is not None and final_revenue is not None:
            final_gross = round(final_revenue * sq_margin / 100, 2)
            print(f"      · 毛利: 由毛利率({sq_margin}%) × 收入({final_revenue})派生 = {final_gross}")
        else:
            print(f"      · 毛利: 无法获取")

    missing = [l for v, l in [(final_revenue, "收入"), (final_gross, "毛利"), (final_net, "净利")] if v is None]
    if missing:
        print(f"   ⚠️ [数据不全] 以下单季指标无法获取: {', '.join(missing)}")

    return final_revenue, final_gross, final_net, sq_margin

# =========================================================
# 7) 动态核心调度器
# =========================================================
def fetch_dynamic_yoy_reports(code: str) -> Tuple[pd.DataFrame, str, dict]:
    _reset_llm_token_stats()

    def _return_with_token_stats(df_ret: pd.DataFrame, pred_ret: str, p3_ret: dict) -> Tuple[pd.DataFrame, str, dict]:
        _print_llm_token_stats()
        return df_ret, pred_ret, p3_ret

    def _build_predicted_date_meta(date_type: Optional[str]) -> Dict[str, Any]:
        return {
            "predicted_date_type": date_type,
            "predicted_date_source": (
                "HKEX board meeting notice" if date_type == "official"
                else "historical median estimate" if date_type == "estimated"
                else None
            ),
        }

    token = get_access_token(REFRESH_TOKEN) if (USE_REFRESH_TO_GET_ACCESS or not ACCESS_TOKEN) else ACCESS_TOKEN
    company_name = get_company_short_name_http(code, token)
    print(f"\n🎯 智能雷达锁定：标的 {code} 母公司识别为 [{company_name}]\n" + "-"*60)

    now = datetime.strptime(TODAY_DATE, "%Y-%m-%d")

    # -------------------------
    # Phase 1: HKEX 董事会会议优先
    # -------------------------
    phase1_source = "HKEX"
    predicted_date_str = ""
    predicted_date_type: Optional[str] = None
    hkex_phase1: Dict[str, Any] = {}

    # 新版 Phase1：回看 400 天，使用最近两份董事会会议公告联合判定
    notices = find_recent_board_meeting_notices(
        code,
        lookback_days=HKEX_PHASE1_LOOKBACK_DAYS,
        limit=HKEX_PHASE1_NOTICE_LIMIT,
    )
    if notices:
        notice1 = notices[0]
        notice2 = notices[1] if len(notices) > 1 else {}

        notice1_text = fetch_hkex_notice_text(notice1.get("url", ""))
        notice2_text = fetch_hkex_notice_text(notice2.get("url", "")) if notice2 else ""

        hkex_phase1 = llm_extract_board_meeting_notice(
            stock_code=code,
            company_name=company_name or notice1.get("company_name", ""),
            notice1_title=notice1.get("title", ""),
            notice1_date=notice1.get("date", ""),
            notice1_text=notice1_text,
            notice2_title=notice2.get("title", "") if notice2 else "",
            notice2_date=notice2.get("date", "") if notice2 else "",
            notice2_text=notice2_text,
        )

        # 本地规则兜底：
        # 1) 公告1或公告2任意一份只要出现“三个月/九个月”即判季频；
        # 2) 两份公告都没有季度关键词时，频率保持为空，交由后续回退或其他证据判断。
        notice1_mix = f"{notice1.get('title', '')}\n{notice1_text[:8000]}"
        notice2_mix = f"{notice2.get('title', '')}\n{notice2_text[:8000]}" if notice2 else ""
        if _contains_quarter_period_keywords(notice1_mix) or _contains_quarter_period_keywords(notice2_mix):
            hkex_phase1["frequency"] = "季度披露"

        predicted_date_str = normalize_date_ymd(hkex_phase1.get("predicted_date_str", ""))
        hkex_phase1["predicted_date_str"] = predicted_date_str
        if predicted_date_str:
            predicted_date_type = "official"

        # ===== Phase1-HKEX 本地推导：FY/FYE/period/target =====
        notice1_year = _parse_year_value(hkex_phase1.get("notice1_year"))
        notice1_month = _parse_month_value(hkex_phase1.get("notice1_month"))
        notice1_xxxx = str(hkex_phase1.get("notice1_xxxx", "") or "").strip()
        notice2_year = _parse_year_value(hkex_phase1.get("notice2_year"))
        notice2_month = _parse_month_value(hkex_phase1.get("notice2_month"))
        notice2_xxxx = str(hkex_phase1.get("notice2_xxxx", "") or "").strip()
        three_month_hint = normalize_target_period(hkex_phase1.get("notice1_three_month_period_hint"))
        if three_month_hint not in {"Q1", "Q3"}:
            three_month_hint = ""
        fye_hint = normalize_fye_month(hkex_phase1.get("fye_hint"))

        notice1_period = derive_notice1_period(notice1_xxxx, three_month_hint)
        notice1_is_pure_three_month = _is_pure_three_month_notice(notice1_xxxx)
        derived_fye = derive_fye_from_notice(
            notice1_year=notice1_year,
            notice1_month=notice1_month,
            notice1_xxxx=notice1_xxxx,
            notice1_three_month_period_hint=three_month_hint,
            notice1_text=notice1_text,
            fye_hint=fye_hint,
        )
        notice2_period = derive_notice1_period(notice2_xxxx, "")
        notice2_fye = derive_fye_from_notice(
            notice1_year=notice2_year,
            notice1_month=notice2_month,
            notice1_xxxx=notice2_xxxx,
            notice1_three_month_period_hint="",
            notice1_text=notice2_text,
            fye_hint=derived_fye,
        )
        if notice1_is_pure_three_month and notice2_fye is not None:
            if derived_fye is None or (not three_month_hint and normalize_fye_month(derived_fye) != normalize_fye_month(notice2_fye)):
                derived_fye = notice2_fye
        derived_fy = derive_fy(notice1_year, notice1_month, derived_fye)
        notice2_fy = derive_fy(notice2_year, notice2_month, notice2_fye)
        direct_period_from_fye = ""
        if notice1_is_pure_three_month:
            direct_period_from_fye = infer_three_month_period_from_fye_and_end_month(
                notice_month=notice1_month,
                fye_month=derived_fye or fye_hint,
            )
            if direct_period_from_fye in {"Q1", "Q3"}:
                notice1_period = direct_period_from_fye
        pair_inferred_period = ""
        if notice1_is_pure_three_month and notice2_fye is not None:
            # 对“纯三个月”公告，优先相信同财年相邻公告对的本地关系推断。
            # 这类信息比 LLM 输出的 Q1/Q3 hint 更稳定，尤其能修正阿里(FYE=3)这类场景。
            pair_notice1_fy = derive_fy(notice1_year, notice1_month, notice2_fye)
            pair_inferred_period = infer_three_month_period_from_notice_pair(
                notice1_xxxx=notice1_xxxx,
                notice2_period=notice2_period,
                notice1_fy=pair_notice1_fy,
                notice2_fy=notice2_fy,
            )
            if pair_inferred_period in {"Q1", "Q3"}:
                notice1_period = pair_inferred_period
                derived_fye = notice2_fye
                derived_fy = pair_notice1_fy
        if not notice1_period:
            notice1_period = infer_three_month_period_from_notice_pair(
                notice1_xxxx=notice1_xxxx,
                notice2_period=notice2_period,
                notice1_fy=derived_fy,
                notice2_fy=notice2_fy,
            )
        derived_freq = infer_frequency_from_notice_pair(
            notice1_period=notice1_period,
            notice1_fy=derived_fy,
            notice2_period=notice2_period,
            notice2_fy=notice2_fy,
            explicit_frequency=hkex_phase1.get("frequency", ""),
            notice1_xxxx=notice1_xxxx,
            notice2_xxxx=notice2_xxxx,
        )
        derived_target_period, derived_target_year = derive_target_from_notice1(
            notice1_period=notice1_period,
            fy_year=derived_fy,
            predicted_date_str=predicted_date_str,
            frequency=derived_freq,
        )

        hkex_phase1.update({
            "frequency": derived_freq,
            "fiscal_year_end_month": derived_fye,
            "target_period": derived_target_period,
            "target_year": derived_target_year,
        })

        print(
            "📌 [phase1_hkex_derived] "
            f"notice1_year={notice1_year}, notice1_month={notice1_month}, notice1_xxxx={notice1_xxxx}, "
            f"notice2_year={notice2_year}, notice2_month={notice2_month}, notice2_xxxx={notice2_xxxx}, "
            f"three_month_hint={three_month_hint}, fye_hint={fye_hint}, "
            f"direct_period_from_fye={direct_period_from_fye}, pair_inferred_period={pair_inferred_period}, "
            f"derived_fye={derived_fye}, derived_fy={derived_fy}, "
            f"notice2_period={notice2_period}, notice2_fy={notice2_fy}, "
            f"target_period={derived_target_period}, target_year={derived_target_year}, freq={derived_freq}"
        )

    def is_phase1_core_complete(info: Dict[str, Any]) -> bool:
        period = normalize_target_period(info.get("target_period"))
        year = info.get("target_year")
        freq_ = normalize_frequency(info.get("frequency"), period)
        fye_ = normalize_fye_month(info.get("fiscal_year_end_month"))
        predicted_date = normalize_date_ymd(info.get("predicted_date_str"))
        return bool(period and isinstance(year, int) and freq_ and fye_ and predicted_date)

    hkex_core_complete = is_phase1_core_complete(hkex_phase1)
    need_ifind_fallback = not hkex_core_complete
    df_latest = pd.DataFrame(columns=["reportDate", "reportTitle", "pdfURL"])
    latest_info: Dict[str, Any] = {}

    if need_ifind_fallback:
        phase1_source = "IFIND_FALLBACK"
        predicted_date_str = ""
        predicted_date_type = None
        hkex_phase1["predicted_date_str"] = ""
        t0_end = now.strftime("%Y-%m-%d %H:%M:%S")
        t0_begin = (now - timedelta(days=300)).strftime("%Y-%m-%d 00:00:00")
        df_latest = fetch_and_normalize(code, token, t0_begin, t0_end)

        latest_info = llm_identify_target_period(company_name, df_latest, stock_code=code) or {}
        if not latest_info and not hkex_phase1:
            return _return_with_token_stats(pd.DataFrame(), "", {})

    # Phase1 采用二选一策略：
    # - HKEX 完整且自洽：全用 HKEX
    # - HKEX 不完整或低置信度：全用 iFind
    phase1_info = hkex_phase1 if hkex_core_complete else latest_info

    target_period = normalize_target_period(phase1_info.get("target_period"))

    target_year = phase1_info.get("target_year")
    if not isinstance(target_year, int):
        if isinstance(phase1_info.get("target_year"), int):
            target_year = int(phase1_info.get("target_year"))
        elif isinstance(latest_info.get("target_year"), int):
            target_year = int(latest_info.get("target_year"))
        elif str(phase1_info.get("target_year", "")).isdigit():
            target_year = int(str(phase1_info.get("target_year")))
        elif str(latest_info.get("target_year", "")).isdigit():
            target_year = int(str(latest_info.get("target_year")))
        else:
            target_year = now.year

    freq = normalize_frequency(phase1_info.get("frequency"), target_period)
    if not freq:
        if target_period in {"Q1", "Q3"}:
            freq = "季度披露"

    fye = normalize_fye_month(phase1_info.get("fiscal_year_end_month"))
    if fye is None:
        fye = normalize_fye_month(latest_info.get("fiscal_year_end_month"))
    if fye is None:
        fye = 12

    # 标题正则作为财年结束月的最终校正层，尤其用于非12月财年公司
    if not df_latest.empty:
        regex_fye = infer_fye_from_titles(df_latest)
        if regex_fye is not None and regex_fye != fye:
            print(f"   ✅ [财年修正] 标题正则 FYE={regex_fye} 覆盖当前 FYE={fye}")
            fye = regex_fye

    target_name_short = str(phase1_info.get("target_name_short", "") or "").strip()

    if isinstance(target_year, int) and need_ifind_fallback:
        target_year = adjust_target_year_after_phase1(
            target_year=int(target_year),
            target_period=target_period,
            fye_month=int(fye),
            predicted_date_str=predicted_date_str,
            target_name_short=target_name_short,
            ref_year=now.year,
        )

    canonical_target_name_short = build_target_name_short(int(target_year), target_period, freq, int(fye))
    if not target_name_short:
        target_name_short = canonical_target_name_short
    else:
        target_name_short = canonical_target_name_short

    # HKEX 准确日期若已兑现，则推进到下一财报季，预测日期回退为历史中位数法
    if hkex_core_complete and predicted_date_str:
        try:
            predicted_dt = datetime.strptime(predicted_date_str, "%Y-%m-%d")
            if predicted_dt < now:
                target_period, target_year = advance_target_period(target_period, int(target_year), freq)
                target_name_short = build_target_name_short(int(target_year), target_period, freq, int(fye))
                predicted_date_str = ""
                predicted_date_type = None
        except Exception:
            predicted_date_str = ""
            predicted_date_type = None

    if not target_period or not target_year or not freq:
        print(f"❌ Phase1 字段不足: target_period={target_period}, target_year={target_year}, freq={freq}")
        return _return_with_token_stats(pd.DataFrame(), "", {})

    target_year = int(target_year)
    fye = int(fye)
    fye_label = f"{fye}月" if fye != 12 else "12月(自然年)"
    print(
        f"🎯 Phase1[{phase1_source}] 锁定目标期：[{target_year}年 {target_period}] -> 预期独立单期 [{target_name_short}] "
        f"(判断频率: {freq}, 财年结束: {fye_label})"
    )
    if predicted_date_str:
        print(f"   · predicted_date_str={predicted_date_str} [{predicted_date_type or 'unknown'}]")

    # 调试断点：仅当设置环境变量 DS_BREAKPOINT_AFTER_PHASE1=1 时触发
    if os.getenv("DS_BREAKPOINT_AFTER_PHASE1", "0") == "1":
        print(
            "\n🛑 [DEBUG断点] 已停在 Phase1 结束处（即将进入 Phase2）。"
            f"\n   target_period={target_period}, target_year={target_year}, freq={freq}, fye={fye}, predicted_date_str={predicted_date_str}"
        )
        breakpoint()

    # 第二步：将目标期数转换为严格的日历提取窗口，拉取目标年的 Y-1, Y-2, Y-3 数据
    def get_calendar_window(t_year, period, offset, fye_month=12):
        """
        根据财年结束月 fye_month 动态计算报告发布的日历搜索窗口。
        思路：先算出该期财务数据的期末日期，再加 1~4 个月的发布延迟作为搜索窗口。
        """
        # 将财年编号(target_year)映射到自然年
        # 对标准财年(fye=12)：财年编号 = 自然年，直接减 offset
        # 对非标财年(fye≠12)：财年N始于自然年N-1，需额外减1
        #   例：阿里(fye=3) FY2026 Q3 → 自然年=2025 → 期末Dec 2025
        #        FY2025 Q3(Y-1) → 自然年=2024 → 期末Dec 2024
        if fye_month == 12:
            y = t_year - offset
        else:
            y = t_year - offset - 1

        # 计算该 period 的期末月份
        period_month_offset = {"Annual": 0, "Interim": 6, "Q1": 3, "Q3": 9}
        end_month = ((fye_month - 1 + period_month_offset[period]) % 12) + 1

        # 计算期末所在的日历年
        # 对标准财年(fye=12): Annual期末在y年12月, Interim在y年6月, Q1在y年3月, Q3在y年9月
        # 对非标财年(fye=3):  Annual期末在y+1年3月, Interim在y年9月, Q1在y年6月, Q3在y年12月
        if fye_month == 12:
            end_cal_year = y
        else:
            # 非标财年：财年y横跨 y年(fye+1)月 到 (y+1)年fye月
            # 如果 end_month <= fye_month，说明已在下一个日历年
            if end_month <= fye_month:
                end_cal_year = y + 1
            else:
                end_cal_year = y

        # 期末日 = 该月最后一天
        last_day = calendar.monthrange(end_cal_year, end_month)[1]
        period_end = datetime(end_cal_year, end_month, last_day)

        # 发布窗口 = 期末日起，年报延后4个月，其他延后3个月
        window_start = period_end
        delay_months = 4 if period == "Annual" else 3
        window_end = period_end + relativedelta(months=delay_months)

        return window_start.strftime("%Y-%m-%d %H:%M:%S"), window_end.strftime("%Y-%m-%d 23:59:59")

    # 推断 Y-1 年度由于需要计算当季而非累计，所必须额外提取的上一期基准
    def get_prior_period_for_deduction(period, freq):
        if freq == "季度披露":
            if period == "Annual": return "Q3"
            elif period == "Q3": return "Interim"
            elif period == "Interim": return "Q1"
            elif period == "Q1": return None # Q1无需扣减
        else:
            if period == "Annual": return "Interim"
            elif period == "Interim": return None
        return None

    prior_period = get_prior_period_for_deduction(target_period, freq)

    use_predicted_two_window_mode = bool(predicted_date_str and prior_period)

    if use_predicted_two_window_mode:
        windows = [
            ("Y-1", get_calendar_window(target_year, target_period, 1, fye)),
            (f"Y-1_{prior_period}", get_calendar_window(target_year, prior_period, 1, fye)),
        ]
        target_years = [target_year - 1]
    else:
        windows = [
            ("Y-1", get_calendar_window(target_year, target_period, 1, fye)),
            ("Y-2", get_calendar_window(target_year, target_period, 2, fye)),
            ("Y-3", get_calendar_window(target_year, target_period, 3, fye))
        ]
        if prior_period:
            windows.append((f"Y-1_{prior_period}", get_calendar_window(target_year, prior_period, 1, fye)))
        target_years = [target_year - 1, target_year - 2, target_year - 3]

    print(f"📡 [历史虫洞] 发出硬核日历请求计算结果:")
    for tag, win in windows:
        if tag == "Y-1":
            label = f"Y-1  ({target_year-1} {target_period})"
        elif tag == "Y-2":
            label = f"Y-2  ({target_year-2} {target_period})"
        elif tag == "Y-3":
            label = f"Y-3  ({target_year-3} {target_period})"
        elif tag.startswith("Y-1_"):
            pp = tag.split("_", 1)[1]
            label = f"Y-1附({target_year-1} {pp})"
        else:
            label = tag
        suffix = " (用于单季扣减)" if tag.startswith("Y-1_") else ""
        print(f"   ➡️ {label}: {win[0][:10]} ~ {win[1][:10]}{suffix}")

    # 防呆：Y-1 主期窗口上限如果在今天之后，说明 LLM 目标期判断有误
    y1_window_end = datetime.strptime(windows[0][1][1][:10], "%Y-%m-%d")
    if y1_window_end > now:
        print(f"\n❌ [防呆拦截] Y-1 主期窗口上限 ({windows[0][1][1][:10]}) 超过今天 ({TODAY_DATE})！")
        print(f"   这说明 LLM 阶段一判断的目标期 [{target_year}年 {target_period}] 可能有误——该期的 Y-1 报告尚未到发布窗口。")
        print(f"   请检查 LLM 的 target_period / target_year 是否正确。")
        return _return_with_token_stats(pd.DataFrame(), "", {})

    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, len(windows))) as executor:
        futures = [(w[0], executor.submit(fetch_and_normalize, code, token, w[1][0], w[1][1])) for w in windows]

    all_hist_dfs = []
    for win_idx, future in futures:
        df_hist = future.result()
        if not df_hist.empty:
            all_hist_dfs.append(df_hist)

    final_rows = []
    if all_hist_dfs:
        df_combined = pd.concat(all_hist_dfs, ignore_index=True)
        # 去重并按时间倒序排列
        df_combined = df_combined.drop_duplicates(subset=["reportDate", "reportTitle"]).sort_values("reportDate", ascending=False).reset_index(drop=True)

        print("\n" + "="*60)
        print(f"📋 [DEBUG] 进入阶段二 LLM 判断前，合并获取的三年历史公告列表 (df_combined)：")
        print(df_combined.to_string(index=False))
        print("="*60 + "\n")

        # 优化点 1：基于目标期的数量推断合理的最低结果数阈值
        expected_count = 2 if use_predicted_two_window_mode else (4 if prior_period else 3)
        if len(df_combined) <= expected_count:
            print(f"🚀 [阶段二] 备选公告数量({len(df_combined)}) <= 预期提取期数({expected_count})，判定为极少数目，直接全量采纳！")
            for i in range(len(df_combined)):
                final_rows.append(df_combined.iloc[i].to_dict())
        else:
            hist_ids = llm_find_historical_reports(
                company_name,
                df_combined,
                target_years,
                target_period,
                prior_target_period=prior_period,
                stock_code=code,
                fye_month=fye,
            )
            for i in hist_ids:
                if i < len(df_combined):
                    final_rows.append(df_combined.iloc[i].to_dict())

    df_final = pd.DataFrame(final_rows)
    # 按时间倒序最后输出清洗
    if not df_final.empty:
        df_final = df_final.drop_duplicates(subset=["reportDate", "reportTitle"]).sort_values("reportDate", ascending=False).reset_index(drop=True)

        pd.set_option('display.max_colwidth', None)
        print(f"\n✅ 成功提取 {code} 的跨周期核心公告：\n")
        print(df_final.to_string(index=False))

        # 预测发布日期优先级：HKEX 未兑现准确日 > 历史中位数法
        if not predicted_date_str:
            main_windows = [w[1] for w in windows if not str(w[0]).startswith("Y-1_")]

            def is_main_target(date_str):
                dt = datetime.strptime(date_str[:19], "%Y-%m-%d %H:%M:%S")
                for wb, we in main_windows:
                    if datetime.strptime(wb, "%Y-%m-%d %H:%M:%S") <= dt <= datetime.strptime(we, "%Y-%m-%d %H:%M:%S"):
                        return True
                return False

            main_target_dates = [r["reportDate"] for _, r in df_final.iterrows() if is_main_target(r["reportDate"])]
            if main_target_dates:
                dummy_year_timestamps = []
                for d in main_target_dates:
                    dt = datetime.strptime(d[:10], "%Y-%m-%d")
                    dummy_dt = datetime(2004, dt.month, dt.day) # 2004 闰年安全处理 02-29
                    dummy_year_timestamps.append(dummy_dt.timestamp())

                median_ts = statistics.median(dummy_year_timestamps)
                median_dt = datetime.fromtimestamp(median_ts)
                # 预测发布年份：取历史最近一期的发布年份 + 1（兼容非标财年）
                max_hist_year = max(int(d[:4]) for d in main_target_dates)
                predicted_pub_year = max_hist_year + 1
                predicted_date_str = f"{predicted_pub_year}-{median_dt.strftime('%m-%d')}"
                predicted_date_type = "estimated"

        # >>> Phase 3: 提取独立指标 <<<
        phase3_data = {}
        expected_phase3_count = 2 if use_predicted_two_window_mode else (4 if prior_period else 3)
        if not df_final.empty:
            actual_count = len(df_final)
            if actual_count != expected_phase3_count:
                print(f"\n⚠️ [Phase 3 校验] 进入提取的公告数量({actual_count}) ≠ 预期({expected_phase3_count})，可能存在匹配遗漏或多余")
            else:
                print(f"\n✅ [Phase 3 校验] 公告数量 = {actual_count}，符合预期")
            print("⏳ [阶段三] 正在并发提取 Y-1 及前置期的核心财务数据(收入、毛利、净利润)..."
)

            y1_main_window = [w[1] for w in windows if w[0] == "Y-1"][0]
            y1_prior_window = [w[1] for w in windows if w[0] == f"Y-1_{prior_period}"][0] if prior_period else None

            def in_window(date_str, window):
                dt = datetime.strptime(date_str[:19], "%Y-%m-%d %H:%M:%S")
                wb = datetime.strptime(window[0], "%Y-%m-%d %H:%M:%S")
                we = datetime.strptime(window[1], "%Y-%m-%d %H:%M:%S")
                return wb <= dt <= we

            # 同一个季报日有多个链接时，优先选择单季报口径
            sq_keywords = ["第四季度", "第三季度", "第二季度", "第一季度", "Q4", "Q3", "Q2", "Q1", "单季", "当季"]
            def is_single_quarter_title(title):
                t = zhconv.convert(title, 'zh-cn')
                return any(kw in t for kw in sq_keywords)

            main_candidates = []
            for _, r in df_final.iterrows():
                if in_window(r["reportDate"], y1_main_window):
                    main_candidates.append(r)

            def pick_best(candidates):
                if not candidates: return None
                sq = [c for c in candidates if is_single_quarter_title(c["reportTitle"])]
                return sq[0] if sq else candidates[0]

            prior_row = None
            if prior_period and y1_prior_window:
                if use_predicted_two_window_mode:
                    prior_candidates = []
                    for _, r in df_final.iterrows():
                        if in_window(r["reportDate"], y1_prior_window):
                            prior_candidates.append(r)
                    prior_best = pick_best(prior_candidates)
                    if prior_best is not None:
                        prior_row = prior_best
                        print(f"   [DEBUG] 附期(由Phase2结果)匹配: {prior_row['reportDate'][:10]} | {prior_row['reportTitle'][:50]}")
                    else:
                        print(f"   ⚠️ 附期(由Phase2结果)未匹配到候选行")
                else:
                    # 旧逻辑：Y-1附 prior 直接从原始 API 独立抓取，绕开 Phase 2 过滤
                    print(f"   🔍 独立抓取 Y-1附 ({prior_period}) 窗口: {y1_prior_window[0][:10]} ~ {y1_prior_window[1][:10]}")
                    df_prior_raw = fetch_and_normalize(code, token, y1_prior_window[0], y1_prior_window[1])
                    if not df_prior_raw.empty:
                        prior_candidates_raw = list(df_prior_raw.iterrows())
                        prior_best = pick_best([r for _, r in prior_candidates_raw])
                        if prior_best is not None:
                            prior_row = prior_best
                            print(f"   [DEBUG] 附期独立抓取匹配: {prior_row['reportDate'][:10]} | {prior_row['reportTitle'][:50]}")
                        else:
                            print(f"   ⚠️ 附期独立抓取: 窗口内有数据但无法匹配最优行")
                    else:
                        print(f"   ⚠️ 附期独立抓取: 窗口 [Y-1_{prior_period}] 内未返回任何公告，疑似公司不发布此期报告")

            main_row = pick_best(main_candidates)

            # DEBUG: 显示匹配到的行
            if main_row is not None:
                print(f"   [DEBUG] 主期匹配行: {main_row['reportDate'][:10]} | {main_row['reportTitle'][:40]}")
            else:
                print(f"   ⚠️ 未找到主期(Y-1 {target_period})匹配行!")
            if prior_period:
                if prior_row is not None:
                    print(f"   [DEBUG] 附期匹配行: {prior_row['reportDate'][:10]} | {prior_row['reportTitle'][:40]}")
                else:
                    print(f"   ⚠️ 未找到附期(Y-1 {prior_period})匹配行!")

            def process_pdf(row_series, is_main, period_name):
                url = row_series["pdfURL"]
                print(f"   📥 正在下载与解析报告 (目标={period_name} | {'主期' if is_main else '附期'})...")
                txt = download_and_extract_pdf_text_for_financials(url)
                return llm_extract_financial_data(txt, is_main, period_name, stock_code=code)

            main_data, prior_data = {}, {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                future_main = executor.submit(process_pdf, main_row, True, target_period) if main_row is not None else None
                future_prior = executor.submit(process_pdf, prior_row, False, prior_period) if prior_row is not None else None

                if future_main: main_data = future_main.result()
                if future_prior: prior_data = future_prior.result()

            print(f"   [基准主期] 解析结果: {json.dumps(main_data, ensure_ascii=False)}")
            if prior_row is not None:
                print(f"   [扣减附期] 解析结果: {json.dumps(prior_data, ensure_ascii=False)}")

            final_revenue, final_gross, final_net, raw_margin_pct = resolve_financials(main_data, prior_data, prior_period)
            margin = None

            if final_revenue and final_revenue > 0 and final_gross is not None:
                margin = round(final_gross / final_revenue, 4)
            elif raw_margin_pct is not None:
                margin = round(raw_margin_pct / 100, 4)

            phase3_data = {
                "revenue": final_revenue,
                "gross_profit": final_gross,
                "net_profit": final_net,
                "margin_percent": margin * 100 if margin is not None else None,
                "target_name_short": target_name_short,
                "frequency": freq,
                "fye_month": fye,
                "target_period": target_period,
                "target_year": target_year,
                **_build_predicted_date_meta(predicted_date_type),
            }

            # 识别报表本币
            currency = main_data.get("detected_currency", "RMB")
            currency_map = {"RMB": "人民币", "HKD": "港元", "USD": "美元", "EUR": "欧元", "JPY": "日元", "GBP": "英镑"}
            currency_label = currency_map.get(currency, currency)

            y1_natural = (target_year - 1) if fye == 12 else (target_year - 2)
            fy_label = compute_fy_label(y1_natural, target_period, freq, fye)
            print(f"\n📊 [{fy_label} 最终抽取指标] (单位: 亿{currency_label})"
                  + (f"  [自然年口径: {target_name_short}]" if fye != 12 else ""))
            print(f"   · 营业收入: {final_revenue}")
            print(f"   · 毛利金额: {final_gross} (毛利率: {round(margin * 100, 2) if margin is not None else 'N/A'} %)")
            print(f"   · 归母净利: {final_net}")
            if predicted_date_str:
                period_cn = {"Annual": "年报", "Interim": "中报", "Q1": "一季报", "Q3": "三季报"}.get(target_period, target_period)
                target_natural = target_year if fye == 12 else (target_year - 1)
                fy_next = compute_fy_label(target_natural, target_period, freq, fye)
                print(f"\n🔮 [雷达预测] {fy_next} {period_cn}预计发布时间: {predicted_date_str} [{predicted_date_type or 'unknown'}]")

        if "target_name_short" not in phase3_data:
            phase3_data["target_name_short"] = target_name_short
        if "frequency" not in phase3_data:
            phase3_data["frequency"] = freq
        if "fye_month" not in phase3_data:
            phase3_data["fye_month"] = fye
        if "target_period" not in phase3_data:
            phase3_data["target_period"] = target_period
        if "target_year" not in phase3_data:
            phase3_data["target_year"] = target_year
        return _return_with_token_stats(df_final, predicted_date_str, phase3_data)
    return _return_with_token_stats(
        df_final,
        predicted_date_str,
        {
            "target_name_short": target_name_short,
            "frequency": freq,
            "fye_month": fye,
            "target_period": target_period,
            "target_year": target_year,
            **_build_predicted_date_meta(predicted_date_type),
        },
    )

if __name__ == "__main__":
    _t0 = time.perf_counter()
    df_final, predicted_date, p3_data = fetch_dynamic_yoy_reports(STOCK_CODE)
    _elapsed = time.perf_counter() - _t0
    if not df_final.empty:
        df_final.to_csv('test_result.csv', index=False, encoding='utf-8-sig')
    else:
        print("\n❌ 未能完成跨周期财报提取。")
    print(f"\n⏱️ 总运行时间: {_elapsed:.1f} 秒")

