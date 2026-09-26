"""对话意图分类。纯本地、无 I/O，不调用行情、MCP 或模型。"""

from __future__ import annotations

import re
from enum import Enum
from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from app.contracts import ResearchWindow
from app.engine.company_index import (
    bind_companies,
    is_screen_query,
    scan_query_for_companies,
)
from app.engine.resolver import ParsedQuery, parse_query
from app.engine.sector import SectorMatch, resolve_sector_phrase
from app.engine.topic import apply_topic_policy, latest_topic_companies

_FULL_REPORT = re.compile(
    r"(完整.{0,12}报告|分析报告|研究报告|撰写报告|写一份.{0,16}报告|完整的?异动|完整分析)"
)
_ANALYSIS = re.compile(
    r"(为什么|为啥|怎么了|怎么一直|怎么跌|怎么涨|异动|分析|研究|原因|对比|比起|相比)"
)
_MODEL_Q = re.compile(
    r"(底层模型|什么模型|哪个模型|用的?什么模型|大模型|基座模型|"
    r"模型是什么|你是什么模型|\bgpt\b|\bllm\b|glm-?\d*)",
    re.IGNORECASE,
)
_CAPABILITY_Q = re.compile(
    r"(你可以做什么|你能做什么|你会什么|有什么功能|怎么用你|"
    r"你是谁|介绍一下你|能帮我做什么|支持什么|你能干什么|你会干什么)"
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
NEED_SECTOR_MESSAGE = (
    "您的问题「{query}」是在问板块异动，但没有识别出具体板块。"
    "请说明板块或行业，例如「科技股」「白酒」「半导体」。"
)
NEED_SECTOR_HINT = "也可以直接点名公司，例如「宁德时代最近5个交易日」。"
NEED_WINDOW_HINT_SNAPSHOT = (
    "请先确认研究窗口：今日 / 最近 3 个交易日 / 最近 5 个交易日。确认后先为您做行情快照。"
)
NEED_WINDOW_HINT_REPORT = (
    "请先确认研究窗口：今日 / 最近 3 个交易日 / 最近 5 个交易日。确认后开始分析。"
)
NEED_WINDOW_HINT_SECTOR = (
    "请先确认研究窗口：今日 / 最近 3 个交易日 / 最近 5 个交易日。"
    "确认后扫描该板块成分股的异动。"
)
MODEL_REPLY = "基于您的提问，我会挑选最合适的模型完成任务"
CAPABILITY_REPLY = (
    "我是个股异动研究助手。你可以用公司名称或代码提问，也可以问某个板块"
    "最近有没有明显异动，例如「科技股最近有明显异动的股票么」。"
    "我会先确认研究窗口，再给出行情快照、板块扫描或完整异动分析。"
    "不做买卖建议，也不对股价做确定性预测。"
)
CHITCHAT_HINT = "可以直接问一只股票，例如「宁德时代今天为什么跌了」。"


class IntentKind(str, Enum):
    NONSENSE = "nonsense"
    CHITCHAT = "chitchat"
    NEED_STOCK = "need_stock"
    NEED_WINDOW = "need_window"
    SNAPSHOT = "snapshot"
    REPORT = "report"
    SECTOR_SCREEN = "sector_screen"


class ConversationTurn(BaseModel):
    """当前对话窗口里的一轮发言。"""

    role: str
    text: str = ""
    kind: str = ""
    stocks: List[str] = Field(default_factory=list)
    window: Optional[ResearchWindow] = None


class ConversationContext(BaseModel):
    """当前对话窗口的上下文。查询列表按轮次截断，默认最多 20 轮。"""

    stocks: List[str] = Field(default_factory=list)
    queries: List[str] = Field(default_factory=list)
    window: Optional[ResearchWindow] = None
    turns: List[ConversationTurn] = Field(default_factory=list)
    answers: Dict[str, str] = Field(default_factory=dict)


def clip_conversation(
    context: Optional[ConversationContext],
    max_turns: int = 20,
) -> ConversationContext:
    """只保留最近 max_turns 轮用户发言及其后的助手回复。"""
    if context is None:
        return ConversationContext()
    queries = list(context.queries or [])[-max_turns:]
    turns = list(context.turns or [])
    user_idx = [i for i, item in enumerate(turns) if item.role == "user"]
    if len(user_idx) > max_turns:
        turns = turns[user_idx[-max_turns] :]
    stocks = latest_topic_companies(
        context.model_copy(update={"queries": queries, "turns": turns})
    )
    window = context.window
    if window is None:
        for turn in reversed(turns):
            if turn.window:
                window = turn.window
                break
    return context.model_copy(
        update={"queries": queries, "turns": turns, "stocks": stocks, "window": window}
    )


class ChatIntent(BaseModel):
    kind: IntentKind
    parsed: ParsedQuery
    wants_full_report: bool = False
    inherited_from_context: bool = False
    chitchat_topic: Optional[str] = None
    message: str = ""
    hint: str = ""
    sector_label: Optional[str] = None
    sector_names: List[str] = Field(default_factory=list)


def classify_intent(
    query: str,
    window: Optional[ResearchWindow] = None,
    context: Optional[ConversationContext] = None,
    resolved_keys: Optional[List[str]] = None,
) -> ChatIntent:
    raw = (query or "").strip()
    parsed = parse_query(raw)
    topic = _chitchat_topic(raw)
    if topic:
        return ChatIntent(
            kind=IntentKind.CHITCHAT,
            parsed=parsed,
            chitchat_topic=topic,
            message=MODEL_REPLY if topic == "model" else "",
            hint=CHITCHAT_HINT,
        )
    explicit = [
        key
        for key in scan_query_for_companies(raw)
        if key.strip().lower() not in _GREETINGS
    ]
    screen = is_screen_query(raw)
    local = bind_companies(
        [
            key
            for key in list(parsed.search_keys) + explicit
            if key.strip().lower() not in _GREETINGS
        ]
    )
    if screen and not explicit:
        local = []
    if resolved_keys is not None:
        keys = [
            key
            for key in resolved_keys
            if key and key.strip().lower() not in _GREETINGS
        ]
        inherited = bool(keys) and not local
        if screen and not explicit:
            keys, inherited = [], False
    else:
        if screen and not explicit:
            keys, inherited = [], False
        else:
            keys, inherited = apply_topic_policy(local, raw, context)
            if inherited and not local and not _can_inherit(raw):
                keys, inherited = [], False
    parsed = parsed.model_copy(
        update={"name_hints": keys, "name_hint": keys[0] if keys else None}
    )
    if parsed.window is None and context and context.window:
        parsed = parsed.model_copy(update={"window": context.window})
    has_entity = bool(keys)
    chosen = window or parsed.window
    wants_full = bool(raw and _FULL_REPORT.search(raw))
    wants_analysis = wants_full or bool(raw and _ANALYSIS.search(raw))

    if not has_entity:
        sector = resolve_sector_phrase(raw)
        if sector is not None:
            return _sector_intent(parsed, sector, chosen, wants_full, inherited=False)
        if screen:
            return ChatIntent(
                kind=IntentKind.NEED_STOCK,
                parsed=parsed,
                wants_full_report=wants_full,
                message=NEED_SECTOR_MESSAGE.format(query=raw or query),
                hint=NEED_SECTOR_HINT,
            )
        if wants_analysis or not is_nonsense_text(raw):
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
            inherited_from_context=inherited,
            message="识别到股票，但没有指定研究窗口。",
            hint=hint,
        )

    if wants_analysis:
        return ChatIntent(
            kind=IntentKind.REPORT,
            parsed=parsed,
            wants_full_report=wants_full,
            inherited_from_context=inherited,
        )
    return ChatIntent(
        kind=IntentKind.SNAPSHOT,
        parsed=parsed,
        inherited_from_context=inherited,
    )


def _chitchat_topic(raw: str) -> Optional[str]:
    if not raw:
        return None
    if _MODEL_Q.search(raw):
        return "model"
    if _CAPABILITY_Q.search(raw):
        return "general"
    return None


def _sector_intent(
    parsed: ParsedQuery,
    sector: SectorMatch,
    window: Optional[ResearchWindow],
    wants_full: bool,
    inherited: bool,
) -> ChatIntent:
    if window is None:
        return ChatIntent(
            kind=IntentKind.NEED_WINDOW,
            parsed=parsed,
            wants_full_report=wants_full,
            inherited_from_context=inherited,
            message=f"识别到板块「{sector.label}」，但没有指定研究窗口。",
            hint=NEED_WINDOW_HINT_SECTOR,
            sector_label=sector.label,
            sector_names=sector.names,
        )
    return ChatIntent(
        kind=IntentKind.SECTOR_SCREEN,
        parsed=parsed,
        wants_full_report=wants_full,
        inherited_from_context=inherited,
        sector_label=sector.label,
        sector_names=sector.names,
    )


def _can_inherit(raw: str) -> bool:
    if not raw or is_nonsense_text(raw) or _chitchat_topic(raw):
        return False
    return True


def _context_keys(context: Optional[ConversationContext]) -> List[str]:
    return latest_topic_companies(context)


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


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
