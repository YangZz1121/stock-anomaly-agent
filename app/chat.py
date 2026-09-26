"""对话入口：先本地分流，再按公司拆开执行快照或完整研究。"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from app.config import Settings
from app.contracts import ResearchWindow
from app.engine.intent import ChatIntent, IntentKind, classify_intent
from app.errors import NeedsWindowChoice, ResearchError
from app.orchestrator import ResearchRequest, run_research, run_snapshot
from app.providers.registry import ProviderBundle
from app.schemas import ResearchBrief
from app.trace import RunRecorder


class ChatResult(BaseModel):
    kind: str
    wants_full_report: bool = False
    briefs: List[ResearchBrief] = Field(default_factory=list)
    tasks: List[Dict[str, str]] = Field(default_factory=list)


def gate_intent(query: str, window: Optional[ResearchWindow] = None) -> ChatIntent:
    intent = classify_intent(query, window)
    if intent.kind == IntentKind.NONSENSE:
        raise ResearchError("nonsense", intent.message, intent.hint)
    if intent.kind == IntentKind.NEED_STOCK:
        raise ResearchError("need_stock", intent.message, intent.hint)
    if intent.kind == IntentKind.NEED_WINDOW:
        raise NeedsWindowChoice("need_window", intent.message, intent.hint)
    return intent


async def run_chat(
    query: str,
    window: Optional[ResearchWindow],
    providers: ProviderBundle,
    settings: Settings,
    recorder: RunRecorder,
) -> ChatResult:
    intent = gate_intent(query, window)
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
        request = ResearchRequest(query=query, window=window, search_key=key)
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
