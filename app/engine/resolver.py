"""输入解析与研究窗口确定。

这一层完全确定性：从自然语言里抽出股票标识、研究窗口和方向性描述，
再把窗口映射到真实交易日。规划文档明确要求"如用户在非交易日选择今日，
系统自动映射到最近一个交易日，并明确展示实际研究日期"。
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

from pydantic import BaseModel, Field

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
    "走弱", "走强", "拉升", "得", "是", "在", "和", "与", "以及", "及",
    "对比", "完整", "撰写", "一份", "报告", "行情", "多少", "快照", "请问",
    "相比", "比较", "写",
]

_CJK = re.compile(r"[\u4e00-\u9fa5A-Za-z]{2,}")
_PUNCT = re.compile(r"[\d.。，,！!？?、；;：:\"'“”‘’()（）\[\]【】《》\-—_/\\|~]+")


class ParsedQuery(BaseModel):
    raw: str
    code: Optional[str] = None
    codes: List[str] = Field(default_factory=list)
    name_hint: Optional[str] = None
    name_hints: List[str] = Field(default_factory=list)
    window: Optional[ResearchWindow] = None
    direction_hint: Optional[str] = None

    @property
    def search_key(self) -> Optional[str]:
        keys = self.search_keys
        return keys[0] if keys else None

    @property
    def search_keys(self) -> List[str]:
        seen = set()
        out: List[str] = []
        for key in list(self.codes) + list(self.name_hints):
            if key and key not in seen:
                seen.add(key)
                out.append(key)
        return out


def parse_query(text: str) -> ParsedQuery:
    raw = (text or "").strip()
    parsed = ParsedQuery(raw=raw)
    if not raw:
        return parsed

    parsed.codes = _extract_codes(raw)
    parsed.code = parsed.codes[0] if parsed.codes else None

    for pattern, window in _WINDOW_PATTERNS:
        if pattern.search(raw):
            parsed.window = window
            break

    for pattern, hint in _DIRECTION_PATTERNS:
        if pattern.search(raw):
            parsed.direction_hint = hint
            break

    parsed.name_hints = _extract_names(raw)
    parsed.name_hint = parsed.name_hints[0] if parsed.name_hints else None
    return parsed


def canonicalize_name(text: str) -> str:
    """把口语/简称映射到检索用的正式名称；没有命中则原样返回。"""
    raw = (text or "").strip()
    if not raw:
        return raw
    aliases = load_aliases()
    direct = aliases.get(raw) or aliases.get(raw.lower())
    if direct:
        return direct
    mapped, _ = _scan_aliases(raw)
    return mapped[0] if mapped else raw


def _extract_codes(text: str) -> List[str]:
    occupied = [False] * len(text)
    codes: List[str] = []
    for i, pattern in enumerate(_CODE_PATTERNS):
        for match in pattern.finditer(text):
            if any(occupied[j] for j in range(match.start(), match.end())):
                continue
            for j in range(match.start(), match.end()):
                occupied[j] = True
            if i == 0:
                codes.append(f"{match.group(1)}.{match.group(2).upper()}")
            elif i == 1:
                codes.append(f"{match.group(2)}.{match.group(1).upper()}")
            else:
                codes.append(match.group(1))
    return codes


def _extract_names(text: str) -> List[str]:
    mapped, spans = _scan_aliases(text)
    masked = _mask_spans(text, spans)
    for pattern in _CODE_PATTERNS:
        masked = pattern.sub(" ", masked)
    cleaned = masked
    for word in _NOISE_WORDS:
        cleaned = cleaned.replace(word, " ")
    cleaned = _PUNCT.sub(" ", cleaned)
    leftovers: List[str] = []
    greetings = {"你好", "您好", "哈喽", "嗨", "hello", "hi", "hey", "哈哈", "哈哈哈", "测试", "test"}
    aliases = load_aliases()
    for token in _CJK.findall(cleaned):
        if token.lower() in greetings:
            continue
        if token.isascii() and token not in aliases and token.lower() not in aliases:
            continue
        leftovers.append(canonicalize_name(token))
    return _dedupe(mapped + leftovers)


def _mask_spans(text: str, spans: List[Tuple[int, int]]) -> str:
    if not spans:
        return text
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = " "
    return "".join(chars)


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


@lru_cache(maxsize=1)
def load_aliases() -> Dict[str, str]:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "fixtures", "market", "aliases.json")
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        return {}
    aliases: Dict[str, str] = {}
    for key, value in data.items():
        if not key or not value:
            continue
        aliases[str(key)] = str(value)
        if str(key).isascii():
            aliases[str(key).lower()] = str(value)
    return aliases


def _alias_pairs() -> List[Tuple[str, str]]:
    # 每个正式映射只保留原始大小写键，避免 CATL / catl 扫两遍
    seen = set()
    pairs: List[Tuple[str, str]] = []
    for key, value in load_aliases().items():
        marker = key.lower()
        if marker in seen:
            continue
        seen.add(marker)
        pairs.append((key, value))
    pairs.sort(key=lambda item: len(item[0]), reverse=True)
    return pairs


def _scan_aliases(text: str) -> Tuple[List[str], List[Tuple[int, int]]]:
    names: List[str] = []
    spans: List[Tuple[int, int]] = []
    if not text:
        return names, spans
    pairs = _alias_pairs()
    i = 0
    n = len(text)
    while i < n:
        hit: Optional[Tuple[str, int]] = None
        for key, canon in pairs:
            end = i + len(key)
            if end <= n and text[i:end].lower() == key.lower():
                hit = (canon, end)
                break
        if hit:
            names.append(hit[0])
            spans.append((i, hit[1]))
            i = hit[1]
        else:
            i += 1
    return names, spans


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
