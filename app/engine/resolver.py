"""输入解析与研究窗口确定。

这一层完全确定性：从自然语言里抽出股票标识、研究窗口和方向性描述，
再把窗口映射到真实交易日。规划文档明确要求"如用户在非交易日选择今日，
系统自动映射到最近一个交易日，并明确展示实际研究日期"。
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from pydantic import BaseModel

from app.contracts import ResearchWindow, WINDOW_LABELS, WINDOW_TRADING_DAYS
from app.schemas import WindowInfo
from app.timeutil import in_trading_session, today_str

# 600519.SH / 300750.SZ / sz300750 / 600519
_CODE_PATTERNS = [
    re.compile(r"\b(\d{6})\.(SH|SZ|BJ)\b", re.IGNORECASE),
    re.compile(r"\b(SH|SZ|BJ)[.\-]?(\d{6})\b", re.IGNORECASE),
    re.compile(r"(?<!\d)(\d{6})(?!\d)"),
]

# 窗口关键词，越靠前优先级越高
_WINDOW_PATTERNS: List[Tuple[re.Pattern, ResearchWindow]] = [
    (re.compile(r"(最近|近|过去)?\s*5\s*(个)?\s*(交易日|天|日)"), ResearchWindow.D5),
    (re.compile(r"(最近|近|过去)?\s*3\s*(个)?\s*(交易日|天|日)"), ResearchWindow.D3),
    (re.compile(r"(最近|近)\s*(几天|一周|这几天)"), ResearchWindow.D5),
    (re.compile(r"(这几天|最近一直|连续几天)"), ResearchWindow.D5),
    (re.compile(r"(今天|今日|当天|当日)"), ResearchWindow.TODAY),
]

_DIRECTION_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"(大跌|跌停|跌了|下跌|跌这么多|跌得|重挫|走弱|回撤)"), "用户认为价格下跌"),
    (re.compile(r"(大涨|涨停|涨了|上涨|涨这么多|走强|拉升|异动)"), "用户认为价格上涨"),
]

# 从查询里剥掉这些词后，剩下的连续中文最可能是公司名
_NOISE_WORDS = [
    "为什么", "为啥", "怎么", "咋", "这么多", "这么", "那么", "一直", "最近", "近期",
    "今天", "今日", "当天", "当日", "过去", "几天", "一周", "交易日", "个",
    "大跌", "大涨", "跌停", "涨停", "跌了", "涨了", "下跌", "上涨", "跌", "涨",
    "帮我", "请", "分析", "研究", "看看", "查", "一下", "的", "了", "吗", "呢",
    "股价", "股票", "走势", "异动", "原因", "发生", "什么", "回撤", "重挫",
    "走弱", "走强", "拉升", "得", "是", "在", "和", "与",
]

_CJK = re.compile(r"[\u4e00-\u9fa5A-Za-z]{2,}")


class ParsedQuery(BaseModel):
    raw: str
    code: Optional[str] = None
    name_hint: Optional[str] = None
    window: Optional[ResearchWindow] = None
    direction_hint: Optional[str] = None

    @property
    def search_key(self) -> Optional[str]:
        return self.code or self.name_hint


def parse_query(text: str) -> ParsedQuery:
    raw = (text or "").strip()
    parsed = ParsedQuery(raw=raw)
    if not raw:
        return parsed

    parsed.code = _extract_code(raw)

    for pattern, window in _WINDOW_PATTERNS:
        if pattern.search(raw):
            parsed.window = window
            break

    for pattern, hint in _DIRECTION_PATTERNS:
        if pattern.search(raw):
            parsed.direction_hint = hint
            break

    parsed.name_hint = _extract_name(raw)
    return parsed


def _extract_code(text: str) -> Optional[str]:
    for i, pattern in enumerate(_CODE_PATTERNS):
        m = pattern.search(text)
        if not m:
            continue
        if i == 0:
            return f"{m.group(1)}.{m.group(2).upper()}"
        if i == 1:
            return f"{m.group(2)}.{m.group(1).upper()}"
        return m.group(1)  # 纯 6 位代码，交由检索接口补后缀
    return None


def _extract_name(text: str) -> Optional[str]:
    cleaned = text
    for word in _NOISE_WORDS:
        cleaned = cleaned.replace(word, " ")
    cleaned = re.sub(r"[\d.。，,！!？?、；;：:\"'“”‘’()（）\[\]【】《》\-—_/\\|~]+", " ", cleaned)
    candidates = _CJK.findall(cleaned)
    if not candidates:
        return None
    return max(candidates, key=len)


# --------------------------------------------------------------------------
# 研究窗口
# --------------------------------------------------------------------------


class WindowResolution(BaseModel):
    info: WindowInfo
    lookback_start: str  # 含基准期的取数起点
    window_days: List[str]


def resolve_window(
    window: ResearchWindow,
    trading_days: List[str],
    baseline_days: int = 20,
    reference_day: Optional[str] = None,
) -> Optional[WindowResolution]:
    """把研究窗口映射到真实交易日。

    ``trading_days`` 为升序的交易日序列。返回 None 表示日历数据不可用，
    调用方必须据此终止研究，而不是自己猜一个日期。
    """
    if not trading_days:
        return None

    today = reference_day or today_str()
    past = [d for d in trading_days if d <= today]
    if not past:
        # 日历整体晚于今天（例如构造数据集），退回使用序列末尾
        past = list(trading_days)

    anchor = past[-1]
    need = WINDOW_TRADING_DAYS[window.value]
    window_days = past[-need:]
    if len(window_days) < need:
        return None

    remapped = anchor != today
    remap_note = None
    if remapped:
        remap_note = (
            f"{today} 不是交易日或当日数据尚未生成，实际研究日期为 {anchor}。"
            if window == ResearchWindow.TODAY
            else f"实际研究区间为 {window_days[0]} 至 {anchor}。"
        )

    is_intraday = (anchor == today) and in_trading_session()

    # 多取 baseline_days 个交易日用于成交额基准和前收盘价推导
    start_idx = max(0, len(past) - need - baseline_days - 1)
    lookback_start = past[start_idx]

    info = WindowInfo(
        window=window,
        label=WINDOW_LABELS[window.value],
        trading_days=window_days,
        actual_start=window_days[0],
        actual_end=anchor,
        is_intraday=is_intraday,
        remapped=remapped,
        remap_note=remap_note,
    )
    return WindowResolution(
        info=info, lookback_start=lookback_start, window_days=window_days
    )
