"""Agent 环：规划 → 工具 → 观察。"""

from __future__ import annotations

from app.agent.actions import ActionName
from app.agent.planner import HeuristicPlanner, is_legal, parse_action
from app.agent.memory import AgentMemory
from app.contracts import ResearchPriority, ResearchWindow
from tests.conftest import research


def test_heuristic_plan_follows_research_stages():
    memory = AgentMemory("宁德时代", "t1")
    planner = HeuristicPlanner()
    seen = []
    for _ in range(14):
        action = planner.next(memory)
        seen.append(action.name)
        memory.record(action)
        memory.mark(
            {
                ActionName.RESOLVE_SUBJECT: "subject",
                ActionName.FETCH_SNAPSHOT: "snapshot",
                ActionName.SEARCH_EVIDENCE: "retrieved",
                ActionName.PROPOSE_DRIVERS: "proposed",
                ActionName.ASSESS_MECHANISMS: "assessed",
                ActionName.SEARCH_COUNTER: "counter",
                ActionName.BUILD_TRANSMISSIONS: "transmitted",
                ActionName.ASSEMBLE_BRIEF: "assembled",
            }[action.name]
        )
        if action.name == ActionName.SEARCH_EVIDENCE:
            memory.scopes = ["industry", "company"]
        if action.name == ActionName.ASSEMBLE_BRIEF:
            break
    assert seen == [
        ActionName.RESOLVE_SUBJECT,
        ActionName.FETCH_SNAPSHOT,
        ActionName.SEARCH_EVIDENCE,
        ActionName.PROPOSE_DRIVERS,
        ActionName.ASSESS_MECHANISMS,
        ActionName.SEARCH_COUNTER,
        ActionName.BUILD_TRANSMISSIONS,
        ActionName.ASSEMBLE_BRIEF,
    ]


def test_no_anomaly_skips_attribution():
    memory = AgentMemory("测试", "t-quiet")
    memory.mark("subject")
    memory.mark("snapshot")
    memory.priority = ResearchPriority.NO_ANOMALY
    action = HeuristicPlanner().next(memory)
    assert action.name == ActionName.ASSEMBLE_BRIEF
    assert is_legal(action, memory, extra_limit=2)


def test_heuristic_retries_failed_scope_once():
    memory = AgentMemory("宁德时代", "t2")
    memory.mark("subject")
    memory.mark("snapshot")
    memory.mark("retrieved")
    memory.failed_scopes.add("company")
    memory.scopes = ["industry", "company"]
    action = HeuristicPlanner().next(memory)
    assert action.name == ActionName.SEARCH_EVIDENCE
    assert action.scopes == ["company"]


def test_illegal_planner_action_is_rejected():
    memory = AgentMemory("宁德时代", "t3")
    action = parse_action({"action": "assemble_brief", "reason": "提前结束"})
    assert action is not None
    assert not is_legal(action, memory, extra_limit=2)
    assert parse_action({"action": "not_a_tool"}) is None


def test_research_brief_exposes_agent_plan():
    brief = research("宁德时代", ResearchWindow.D5)
    assert brief.trace.plan
    assert brief.trace.agent_steps
    assert brief.metrics.agent_steps == len(brief.trace.agent_steps)
    names = [step.action for step in brief.trace.agent_steps]
    assert "resolve_subject" in names
    assert "search_evidence" in names
    assert "search_counter_evidence" in names
    assert names[-1] == "assemble_brief"
    extra = [
        call
        for call in brief.trace.tool_calls
        if call.tool == "search_events" and call.params.get("pass") == "additional"
    ]
    assert extra, "反向证据检索应该留下一次补充 search_events"
