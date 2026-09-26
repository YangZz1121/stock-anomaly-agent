"""对话意图分类。纯本地、无 I/O，不调用行情、MCP 或模型。"""

from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import BaseModel

from app.contracts import ResearchWindow
from app.engine.resolver import ParsedQuery, parse_query

_FULL_REPORT = re.compile(
    r"(完整.{0,12}报告|分析报告|研究报告|撰写报告|写一份.{0,16}报告)"
)
_ANALYSIS = re.compile(
    r"(为什么|为啥|怎么了|怎么一直|怎么跌|怎么涨|异动|分析|研究|原因|对比|比起|相比)"
)
_PUNCT = re.compile(r"[\s\d.。，,！!？?、；;：:\"'“”‘’()（）\[\]【】《》\-—_/\\|~·…]+")
_GREETINGS = {
    "你好",
    "您好",
    "哈喽",
    "嗨",
    "在吗",
    "在么",
    "hello",
    "hi",
    "hey",
    "哈哈",
    "哈哈哈",
    "测试",
    "test",
    "嗯",
    "啊",
    "哦",
    "好的",
    "谢谢",
}

NONSENSE_MESSAGE = "您的问题「{query}」没有可识别的研究意图。请输入 A 股公司名称或代码，例如「宁德时代今天为什么跌了」。"
NEED_STOCK_MESSAGE = "您的问题「{query}」缺少股票或公司名称。"
NEED_STOCK_HINT = "请补充 A 股公司名称或 6 位代码，例如「宁德时代」。"
NEED_WINDOW_HINT_SNAPSHOT = (
    "请先确认研究窗口：今日 / 最近 3 个交易日 / 最近 5 个交易日。确认后先为您做行情快照。"
)
NEED_WINDOW_HINT_REPORT = (
    "请先确认研究窗口：今日 / 最近 3 个交易日 / 最近 5 个交易日。确认后开始分析。"
)


class IntentKind(str, Enum):
    NONSENSE = "nonsense"
    NEED_STOCK = "need_stock"
    NEED_WINDOW = "need_window"
    SNAPSHOT = "snapshot"
    REPORT = "report"


class ChatIntent(BaseModel):
    kind: IntentKind
    parsed: ParsedQuery
    wants_full_report: bool = False
    message: str = ""
    hint: str = ""


def classify_intent(
    query: str, window: Optional[ResearchWindow] = None
) -> ChatIntent:
    raw = (query or "").strip()
    parsed = parse_query(raw)
    keys = [
        key
        for key in parsed.search_keys
        if key.strip().lower() not in _GREETINGS
    ]
    has_entity = bool(keys)
    chosen = window or parsed.window
    wants_full = bool(raw and _FULL_REPORT.search(raw))
    wants_analysis = wants_full or bool(raw and _ANALYSIS.search(raw))

    if not has_entity:
        if wants_analysis:
            return ChatIntent(
                kind=IntentKind.NEED_STOCK,
                parsed=parsed,
                wants_full_report=wants_full,
                message=NEED_STOCK_MESSAGE.format(query=raw or query),
                hint=NEED_STOCK_HINT,
            )
        return ChatIntent(
            kind=IntentKind.NONSENSE,
            parsed=parsed,
            message=NONSENSE_MESSAGE.format(query=raw or query or ""),
            hint=NEED_STOCK_HINT,
        )

    if chosen is None:
        hint = NEED_WINDOW_HINT_REPORT if wants_analysis else NEED_WINDOW_HINT_SNAPSHOT
        return ChatIntent(
            kind=IntentKind.NEED_WINDOW,
            parsed=parsed,
            wants_full_report=wants_full,
            message="识别到股票，但没有指定研究窗口。",
            hint=hint,
        )

    if wants_analysis:
        return ChatIntent(
            kind=IntentKind.REPORT,
            parsed=parsed,
            wants_full_report=wants_full,
        )
    return ChatIntent(kind=IntentKind.SNAPSHOT, parsed=parsed)


def is_nonsense_text(text: str) -> bool:
    raw = (text or "").strip()
    if not raw:
        return True
    compact = _PUNCT.sub("", raw).lower()
    if not compact:
        return True
    if compact in _GREETINGS:
        return True
    return compact.isascii() and compact.isalpha() and len(compact) <= 12


def estimate_report_minutes(
    window: Optional[ResearchWindow], company_count: int
) -> int:
    base = 1 if window == ResearchWindow.TODAY else 2
    return max(1, base * max(1, company_count))


def notice_text(minutes: int) -> str:
    return f"正在为您撰写分析报告，预计等待 {minutes} 分钟。"
