"""Agent 环：规划 → 执行工具 → 把观察写回工作记忆，直到装配完成或步数用尽。"""

from __future__ import annotations

from app.agent.actions import ActionName
from app.agent.memory import AgentMemory
from app.agent.planner import HeuristicPlanner, build_planner
from app.agent.runtime import ToolRuntime
from app.config import Settings
from app.providers.registry import ProviderBundle
from app.schemas import ResearchBrief
from app.trace import RunRecorder


async def run_research_agent(
    request,
    providers: ProviderBundle,
    settings: Settings,
    recorder: RunRecorder,
) -> ResearchBrief:
    memory = AgentMemory(query=request.query, run_id=recorder.run_id)
    planner = build_planner(
        providers, recorder, extra_search_limit=settings.agent_extra_search_limit
    )
    fallback = HeuristicPlanner(extra_search_limit=settings.agent_extra_search_limit)
    runtime = ToolRuntime(request, providers, settings, recorder)

    for _ in range(settings.agent_max_steps):
        if memory.done:
            break
        nxt = planner.next(memory)
        if hasattr(nxt, "__await__"):
            nxt = await nxt
        await runtime.execute(nxt, memory)
        if nxt.name == ActionName.ASSEMBLE_BRIEF:
            break

    if memory.brief is None:
        recorder.log("Agent 环未在预算内装配 Brief，改用启发式收尾")
        while not memory.done:
            nxt = fallback.next(memory)
            await runtime.execute(nxt, memory)
            if nxt.name == ActionName.ASSEMBLE_BRIEF:
                break

    if memory.brief is None:  # pragma: no cover
        raise RuntimeError("Agent 环结束时没有装配出 Brief")
    return memory.brief
