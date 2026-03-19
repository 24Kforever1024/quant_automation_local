from __future__ import annotations

import re
from datetime import date, datetime, time
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def normalize_market_label(value) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("text") or value.get("name") or value.get("value") or ""
    text = str(value or "").strip()
    mapping = {
        "港股": "港股",
        "A股": "A股",
        "美股": "美股",
        "其他": "其他",
    }
    return mapping.get(text, "")


def infer_market_from_code(code: str) -> str:
    normalized = str(code or "").strip().upper()
    if normalized.endswith(".HK"):
        return "港股"
    if normalized.endswith((".O", ".N", ".US")):
        return "美股"
    if normalized.endswith((".SH", ".SZ", ".BJ")) or re.fullmatch(r"\d{6}", normalized):
        return "A股"
    return "其他"


def normalize_hk_symbol(code: str) -> str:
    digits = "".join(ch for ch in str(code or "").upper() if ch.isdigit())
    return digits.zfill(5)


def normalize_xueqiu_symbol(code: str) -> str:
    normalized = str(code or "").strip().upper()
    if normalized.endswith(".HK"):
        return normalize_hk_symbol(normalized)
    if normalized.startswith("6"):
        return f"SH{normalized[:6]}"
    if normalized.startswith(("0", "3")):
        return f"SZ{normalized[:6]}"
    if normalized.endswith((".O", ".N", ".US")):
        return normalized.split(".")[0]
    return normalized


def normalize_us_symbol(code: str) -> str:
    return str(code or "").strip().upper().split(".")[0]


def _parse_date(date_text: str | datetime | date) -> date:
    if isinstance(date_text, date) and not isinstance(date_text, datetime):
        return date_text
    if isinstance(date_text, datetime):
        return date_text.date()
    text = str(date_text).strip()[:10]
    return datetime.strptime(text, "%Y-%m-%d").date()


def quarter_label_from_date(date_text: str | datetime | date, suffix: str = "") -> str:
    parsed = _parse_date(date_text)
    quarter = ((parsed.month - 1) // 3) + 1
    return f"{str(parsed.year)[2:]}Q{quarter}{suffix}"


def half_label_from_date(date_text: str | datetime | date, suffix: str = "") -> str:
    parsed = _parse_date(date_text)
    half = "H1" if parsed.month <= 6 else "H2"
    return f"{str(parsed.year)[2:]}{half}{suffix}"


def strip_period_suffix(label: str) -> str:
    value = str(label or "").strip().upper()
    return value[:-1] if value.endswith(("A", "E")) else value


def next_period_label(label: str, suffix: str = "E") -> str:
    base = strip_period_suffix(label)
    match = re.fullmatch(r"(FY)?(\d{2})(Q[1-4]|H[12])", base)
    if not match:
        return ""
    prefix = match.group(1) or ""
    year = int(match.group(2))
    period = match.group(3)
    if period.startswith("Q"):
        quarter = int(period[1])
        if quarter == 4:
            year += 1
            period = "Q1"
        else:
            period = f"Q{quarter + 1}"
    else:
        if period == "H2":
            year += 1
            period = "H1"
        else:
            period = "H2"
    return f"{prefix}{year:02d}{period}{suffix}"


def previous_period_label(label: str, suffix: str = "A") -> str:
    base = strip_period_suffix(label)
    match = re.fullmatch(r"(FY)?(\d{2})(Q[1-4]|H[12])", base)
    if not match:
        return ""
    prefix = match.group(1) or ""
    year = int(match.group(2))
    period = match.group(3)
    if period.startswith("Q"):
        quarter = int(period[1])
        if quarter == 1:
            year -= 1
            period = "Q4"
        else:
            period = f"Q{quarter - 1}"
    else:
        if period == "H1":
            year -= 1
            period = "H2"
        else:
            period = "H1"
    return f"{prefix}{year:02d}{period}{suffix}"


def disclosure_year_from_period_label(label: str) -> int:
    base = strip_period_suffix(label)
    year = int(f"20{base[-4:-2] if base.startswith('FY') else base[:2]}")
    if base.endswith(("Q4", "H2")):
        return year + 1
    return year


def bgqs_from_period_label(label: str) -> str:
    base = strip_period_suffix(label)
    if base.endswith("Q1"):
        return "一季报"
    if base.endswith("Q2") or base.endswith("H1"):
        return "半年报"
    if base.endswith("Q3"):
        return "三季报"
    if base.endswith("Q4") or base.endswith("H2"):
        return "年报"
    return ""


def median_mmdd(values: list[str | datetime | date]) -> tuple[int, int] | None:
    ordinals: list[int] = []
    for value in values:
        if value in (None, ""):
            continue
        parsed = _parse_date(value)
        ordinals.append(date(2024, parsed.month, parsed.day).timetuple().tm_yday)
    if not ordinals:
        return None
    ordinals.sort()
    middle = ordinals[len(ordinals) // 2]
    parsed = date(2024, 1, 1).fromordinal(date(2024, 1, 1).toordinal() + middle - 1)
    return parsed.month, parsed.day


def to_feishu_timestamp_ms(value: date | datetime | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        value = datetime.combine(value, time.min)
    assert isinstance(value, datetime)
    if value.tzinfo is None:
        value = value.replace(tzinfo=SHANGHAI_TZ)
    return int(value.timestamp() * 1000)


def calculate_yoy_text(e_val: float | int | str, a_val: float | int | str) -> str:
    try:
        estimate = float(e_val)
        actual = float(a_val)
        if actual >= 0:
            if actual == 0:
                return "同比无法计算(基数为0)"
            return f"{(estimate / actual) - 1:.2%}"
        if estimate >= 0:
            return "同比转正"
        return "亏损同比扩大" if abs(estimate) > abs(actual) else "亏损同比收窄"
    except (TypeError, ValueError, ZeroDivisionError):
        return "数据不足"
