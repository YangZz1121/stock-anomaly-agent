"""规划器：根据工作记忆决定下一步。

启发式规划器复现原来的三阶段顺序，保证无 LLM 时测试与主链路不变。
有模型时走原生 Function Calling；非法动作回退到启发式。
"""

from __future__ import annotations

from typing import Any, Optional

from app.agent.actions import PLANNER_TOOLS, TOOL_SPECS, ActionName, AgentAction
from app.agent.memory import AgentMemory
from app.agent.prompts import PLANNER_SCHEMA, PLANNER_SYSTEM


class HeuristicPlanner:
    name = "heuristic"

    def __init__(self, extra_search_limit: int = 2) -> None:
        self.extra_search_limit = extra_search_limit

    def next(self, memory: AgentMemory) -> AgentAction:
        if not memory.has("subject"):
            return AgentAction(
                name=ActionName.RESOLVE_SUBJECT,
                reason="先确认股票、行业和研究窗口",
            )
        if not memory.has("snapshot"):
            return AgentAction(
                name=ActionName.FETCH_SNAPSHOT,
                reason="先量化价格变化并确定研究优先级",
            )
        asked = memory.ask_if_needed()
        if asked is not None:
            return asked
        if not memory.has("retrieved"):
            return AgentAction(
                name=ActionName.SEARCH_EVIDENCE,
                reason="按研究优先级检索市场 / 行业 / 公司证据",
                scopes=list(memory.scopes),
            )
        failed = memory.failed_scopes - memory.retried_scopes
        if failed and memory.extra_searches < self.extra_search_limit:
            return AgentAction(
                name=ActionName.SEARCH_EVIDENCE,
                reason="对上次失败的范围做一次降级再检索",
                scopes=sorted(failed),
            )
        if not memory.has("proposed"):
            return AgentAction(
                name=ActionName.PROPOSE_DRIVERS,
                reason="从事件聚类生成候选驱动因素",
            )
        if not memory.has("assessed"):
            return AgentAction(
                name=ActionName.ASSESS_MECHANISMS,
                reason="验证每个候选的时间、横截面、特异性和机制",
            )
        if not memory.has("counter"):
            return AgentAction(
                name=ActionName.SEARCH_COUNTER,
                reason="主动寻找可能削弱现有解释的反向证据",
            )
        if not memory.has("transmitted"):
            return AgentAction(
                name=ActionName.BUILD_TRANSMISSIONS,
                reason="为通过验证的因素构建基本面传导链",
            )
        return AgentAction(
            name=ActionName.ASSEMBLE_BRIEF,
            reason="工作记忆已齐，装配研究 Brief",
        )


class LLMPlanner:
    name = "llm"

    def __init__(self, llm: Any, recorder: Any, extra_search_limit: int = 2) -> None:
        self._llm = llm
        self._recorder = recorder
        self._fallback = HeuristicPlanner(extra_search_limit=extra_search_limit)
        self.extra_search_limit = extra_search_limit

    async def next(self, memory: AgentMemory) -> AgentAction:
        forced = self._fallback.next(memory)
        if forced.name in (ActionName.RESOLVE_SUBJECT, ActionName.FETCH_SNAPSHOT):
            return forced
        if forced.name == ActionName.ASK_USER:
            return forced
        if memory.has("transmitted") or forced.name == ActionName.ASSEMBLE_BRIEF:
            return forced

        payload = await self._llm.complete_tools(
            purpose="plan_next",
            system=PLANNER_SYSTEM,
            user=_planner_prompt(memory),
            tools=PLANNER_TOOLS,
            tool_choice="required",
        )
        if not payload.ok and hasattr(self._llm, "complete_json"):
            payload = await self._llm.complete_json(
                purpose="plan_next",
                system=PLANNER_SYSTEM,
                user=_planner_prompt(memory),
                schema_hint=PLANNER_SCHEMA,
            )
        self._recorder.record_llm(
            purpose="plan_next",
            model=getattr(self._llm, "model", "unknown"),
            provider=getattr(self._llm, "name", "unknown"),
            latency_ms=0,
            ok=payload.ok,
            note=payload.note,
        )
        if not payload.ok or not payload.value:
            return forced

        action = parse_action(payload.value)
        if action is None or not is_legal(action, memory, self.extra_search_limit):
            self._recorder.log("规划器返回了非法动作，已回退到启发式下一步")
            return forced
        if not action.reason:
            action.reason = forced.reason
        return action


def build_planner(providers: Any, recorder: Any, extra_search_limit: int) -> Any:
    if getattr(providers.llm, "name", "mock") == "mock":
        return HeuristicPlanner(extra_search_limit=extra_search_limit)
    return LLMPlanner(providers.llm, recorder, extra_search_limit=extra_search_limit)


def parse_action(raw: dict) -> Optional[AgentAction]:
    name = str(raw.get("action") or raw.get("name") or "").strip()
    try:
        action_name = ActionName(name)
    except ValueError:
        return None
    scopes = raw.get("scopes") or None
    if scopes is not None:
        scopes = [s for s in scopes if s in ("market", "industry", "company")]
        if not scopes:
            scopes = None
    extra = [str(t).strip() for t in (raw.get("extra_terms") or []) if str(t).strip()]
    choices = [str(c).strip() for c in (raw.get("choices") or []) if str(c).strip()]
    return AgentAction(
        name=action_name,
        reason=str(raw.get("reason") or ""),
        scopes=scopes,
        extra_terms=extra[:8],
        field=str(raw.get("field") or ""),
        question=str(raw.get("question") or ""),
        choices=choices[:6],
    )


def is_legal(action: AgentAction, memory: AgentMemory, extra_limit: int) -> bool:
    if action.name == ActionName.RESOLVE_SUBJECT:
        return not memory.has("subject")
    if action.name == ActionName.FETCH_SNAPSHOT:
        return memory.has("subject") and not memory.has("snapshot")
    if action.name == ActionName.SEARCH_EVIDENCE:
        if not memory.has("snapshot"):
            return False
        if not memory.has("retrieved"):
            return True
        return memory.extra_searches < extra_limit
    if action.name == ActionName.PROPOSE_DRIVERS:
        return memory.has("retrieved") and not memory.has("proposed")
    if action.name == ActionName.ASSESS_MECHANISMS:
        return memory.has("proposed") and not memory.has("assessed")
    if action.name == ActionName.SEARCH_COUNTER:
        return memory.has("assessed") and not memory.has("counter")
    if action.name == ActionName.BUILD_TRANSMISSIONS:
        return memory.has("assessed") and not memory.has("transmitted")
    if action.name == ActionName.ASK_USER:
        if not memory.has("snapshot") or memory.has("transmitted"):
            return False
        field = action.field or "continue"
        return field not in memory.answers and not memory.has(f"asked:{field}")
    if action.name == ActionName.ASSEMBLE_BRIEF:
        return memory.has("transmitted")
    return False


def _planner_prompt(memory: AgentMemory) -> str:
    import json

    return (
        "当前研究工作记忆：\n"
        f"{json.dumps(memory.planner_view(), ensure_ascii=False, indent=2)}\n\n"
        "用 Function Calling 选择下一步工具：\n"
        + "\n".join(f"- {spec['name']}：{spec['description']}" for spec in TOOL_SPECS)
    )
