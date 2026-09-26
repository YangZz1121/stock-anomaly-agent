"""时间工具。

扶摇接口统一使用毫秒级 Unix 时间戳，时区按 Asia/Shanghai；产品内部
一律使用 ``yyyy-MM-dd`` 字符串，只在出入接口时转换，避免时区漂移。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional

SHANGHAI = timezone(timedelta(hours=8))

DATE_FMT = "%Y-%m-%d"
COMPACT_FMT = "%Y%m%d"


def today_str() -> str:
    return datetime.now(SHANGHAI).strftime(DATE_FMT)


def now_iso() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")


def now_hm() -> str:
    return datetime.now(SHANGHAI).strftime("%H:%M")


def to_ms(day: str) -> int:
    """yyyy-MM-dd -> 当日 00:00:00 (Asia/Shanghai) 的毫秒戳。"""
    dt = datetime.strptime(day, DATE_FMT).replace(tzinfo=SHANGHAI)
    return int(dt.timestamp() * 1000)


def from_ms(ms: int) -> str:
    """毫秒戳 -> yyyy-MM-dd (Asia/Shanghai)。"""
    return datetime.fromtimestamp(ms / 1000, SHANGHAI).strftime(DATE_FMT)


def from_compact(value: str) -> str:
    """yyyyMMdd -> yyyy-MM-dd。"""
    return datetime.strptime(value, COMPACT_FMT).strftime(DATE_FMT)


def shift_days(day: str, delta: int) -> str:
    d = datetime.strptime(day, DATE_FMT).date() + timedelta(days=delta)
    return d.strftime(DATE_FMT)


def parse_date(value: str) -> Optional[date]:
    for fmt in (DATE_FMT, COMPACT_FMT, "%Y/%m/%d"):
        try:
            return datetime.strptime(value[:10] if fmt == DATE_FMT else value, fmt).date()
        except (ValueError, TypeError):
            continue
    return None


def is_within(day: Optional[str], start: str, end: str) -> bool:
    """day 是否落在 [start, end] 闭区间内。无法解析时返回 False。"""
    if not day:
        return False
    parsed = parse_date(day)
    if parsed is None:
        return False
    return parse_date(start) <= parsed <= parse_date(end)  # type: ignore[operator]


def in_trading_session() -> bool:
    """当前是否处于 A 股连续竞价时段（粗略判断，只用于加"盘中"标记）。"""
    now = datetime.now(SHANGHAI)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    morning = 9 * 60 + 30 <= minutes <= 11 * 60 + 30
    afternoon = 13 * 60 <= minutes <= 15 * 60
    return morning or afternoon
