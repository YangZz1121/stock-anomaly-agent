"""对话入口：先本地分流，再按公司拆开执行快照或完整研究。"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from app.config import Settings
from app.contracts import ResearchWindow
from app.engine.entity import resolve_company_keys
from app.engine.intent import (
    CAPABILITY_REPLY,
    CHITCHAT_HINT,
    MODEL_REPLY,
    ChatIntent,
    ConversationContext,
    IntentKind,
    classify_intent,
    is_nonsense_text,
)
from app.errors import NeedsWindowChoice, ResearchError
from app.orchestrator import ResearchRequest, run_research, run_snapshot
from app.providers.registry import ProviderBundle
from app.schemas import ResearchBrief
from app.trace import RunRecorder

CHITCHAT_SYSTEM = """你是个股异动研究助手，用简体中文回答用户的闲聊。
可以介绍你能做什么：先确认股票名称或代码、再确认研究窗口，然后给出行情快照或完整异动分析（发生了什么、为什么、对公司意味着什么）。
禁止买卖建议、目标价、确定性股价预测，也不要编造具体行情或新闻。
不要透露具体模型名称、厂商或版本。如果被问到模型，只回答：基于您的提问，我会挑选最合适的模型完成任务。
回复控制在 120 字以内，语气简洁。"""


class ChatResult(BaseModel):
    kind: str
    wants_full_report: bool = False
    briefs: List[ResearchBrief] = Field(default_factory=list)
    tasks: List[Dict[str, str]] = Field(default_factory=list)
    reply: Optional[str] = None
    hint: str = ""


async def gate_intent(
    query: str,
    window: Optional[ResearchWindow] = None,
    context: Optional[ConversationContext] = None,
    llm=None,
    recorder: Optional[RunRecorder] = None,
    resolved_keys: Optional[List[str]] = None,
) -> ChatIntent:
    peek = classify_intent(query, window, context=context)
    if peek.kind == IntentKind.CHITCHAT:
        return peek
    if peek.kind == IntentKind.NONSENSE and is_nonsense_text(query):
        raise ResearchError("nonsense", peek.message, peek.hint)

    keys = resolved_keys
    if keys is None and peek.kind != IntentKind.CHITCHAT:
        keys = await resolve_company_keys(query, llm=llm, recorder=recorder)
    intent = classify_intent(query, window, context=context, resolved_keys=keys)
    if intent.kind == IntentKind.NONSENSE:
        raise ResearchError("nonsense", intent.message, intent.hint)
    if intent.kind == IntentKind.NEED_STOCK:
        raise ResearchError("need_stock", intent.message, intent.hint)
    if intent.kind == IntentKind.NEED_WINDOW:
        raise NeedsWindowChoice("need_window", intent.message, intent.hint)
    return intent


async def answer_chitchat(
    query: str,
    intent: ChatIntent,
    llm,
    recorder: RunRecorder,
) -> str:
    if intent.chitchat_topic == "model":
        return MODEL_REPLY
    result = await llm.complete_text("chitchat", CHITCHAT_SYSTEM, query)
    recorder.record_llm(
        purpose="chitchat",
        model=getattr(llm, "model", "unknown"),
        provider=getattr(llm, "name", "unknown"),
        latency_ms=0,
        ok=result.ok,
        note=result.note,
        prompt_chars=len(query),
        response_chars=len(result.value or "") if result.ok else 0,
    )
    if result.ok and result.value:
        return str(result.value).strip()
    return CAPABILITY_REPLY


async def run_chat(
    query: str,
    window: Optional[ResearchWindow],
    providers: ProviderBundle,
    settings: Settings,
    recorder: RunRecorder,
    context: Optional[ConversationContext] = None,
    intent: Optional[ChatIntent] = None,
) -> ChatResult:
    if intent is None:
        intent = await gate_intent(
            query, window, context=context, llm=providers.llm, recorder=recorder
        )
    if intent.kind == IntentKind.CHITCHAT:
        text = await answer_chitchat(query, intent, providers.llm, recorder)
        return ChatResult(
            kind=intent.kind.value,
            reply=text,
            hint=intent.hint or CHITCHAT_HINT,
        )
    keys = intent.parsed.search_keys
    briefs: List[ResearchBrief] = []
    tasks: List[Dict[str, str]] = []
    seen = set()
    for index, key in enumerate(keys):
        recorder.emit(
            "task",
            {
                "index": index,
                "key": key,
                "total": len(keys),
                "label": key,
            },
        )
        request = ResearchRequest(
            query=query,
            window=window or intent.parsed.window,
            search_key=key,
        )
        if intent.kind == IntentKind.SNAPSHOT:
            brief = await run_snapshot(request, providers, settings, recorder)
        else:
            brief = await run_research(request, providers, settings, recorder)
        code = brief.subject.stock.thscode
        if code in seen:
            continue
        seen.add(code)
        briefs.append(brief)
        tasks.append({"name": brief.subject.stock.name, "thscode": code})
    return ChatResult(
        kind=intent.kind.value,
        wants_full_report=intent.wants_full_report,
        briefs=briefs,
        tasks=tasks,
    )
