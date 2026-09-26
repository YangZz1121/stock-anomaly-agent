"""人机协作、20 轮上下文、规划器 Function Calling。"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.agent.actions import PLANNER_TOOLS, ActionName
from app.agent.memory import AgentMemory
from app.agent.planner import HeuristicPlanner, is_legal, parse_action
from app.contracts import ResearchWindow
from app.engine.intent import ConversationContext, ConversationTurn, clip_conversation
from app.main import app
from app.providers.llm import _parse_tool_message


def test_config_exposes_twenty_turn_window():
    client = TestClient(app)
    body = client.get("/api/config").json()
    assert body["conversation_max_turns"] == 20


def test_clip_conversation_keeps_last_twenty_user_turns():
    turns = []
    for i in range(25):
        turns.append(ConversationTurn(role="user", text=f"问{i} 宁德时代"))
        turns.append(ConversationTurn(role="assistant", text=f"答{i}", kind="reply"))
    clipped = clip_conversation(
        ConversationContext(
            queries=[f"问{i}" for i in range(25)],
            turns=turns,
            stocks=["贵州茅台"],
        ),
        max_turns=20,
    )
    assert len(clipped.queries) == 20
    assert clipped.queries[0] == "问5"
    user_turns = [t for t in clipped.turns if t.role == "user"]
    assert len(user_turns) == 20
    assert user_turns[0].text == "问5 宁德时代"
    assert clipped.stocks == ["宁德时代"]


def test_clip_conversation_fills_stock_from_recent_turn():
    clipped = clip_conversation(
        ConversationContext(
            turns=[
                ConversationTurn(role="user", text="宁德时代最近怎么了"),
                ConversationTurn(
                    role="assistant",
                    text="快照",
                    kind="snapshot",
                    stocks=["宁德时代"],
                    window=ResearchWindow.D5,
                ),
            ]
        ),
        max_turns=20,
    )
    assert clipped.stocks == ["宁德时代"]
    assert clipped.window == ResearchWindow.D5


def test_heuristic_asks_when_industry_is_weak_evidence():
    memory = AgentMemory("某某股份", "ask1")
    memory.mark("subject")
    memory.mark("snapshot")
    memory.subject = SimpleNamespace(
        industry=SimpleNamespace(is_weak_evidence=True, index_name="专用设备")
    )
    action = HeuristicPlanner().next(memory)
    assert action.name == ActionName.ASK_USER
    assert action.field == "industry"
    assert "专用设备" in action.question


def test_answered_industry_skips_ask_and_continues_search():
    memory = AgentMemory("某某股份", "ask2")
    memory.mark("subject")
    memory.mark("snapshot")
    memory.answers["industry"] = "按弱证据继续"
    memory.subject = SimpleNamespace(
        industry=SimpleNamespace(is_weak_evidence=True, index_name="专用设备")
    )
    memory.scopes = ["industry", "company"]
    action = HeuristicPlanner().next(memory)
    assert action.name == ActionName.SEARCH_EVIDENCE


def test_parse_function_call_message():
    parsed = _parse_tool_message(
        {
            "tool_calls": [
                {
                    "function": {
                        "name": "search_evidence",
                        "arguments": '{"reason":"补检公司公告","scopes":["company"]}',
                    }
                }
            ]
        }
    )
    assert parsed["name"] == "search_evidence"
    action = parse_action(parsed)
    assert action.name == ActionName.SEARCH_EVIDENCE
    assert action.scopes == ["company"]


def test_planner_tools_are_native_function_calling_schema():
    names = {item["function"]["name"] for item in PLANNER_TOOLS}
    assert "ask_user" in names
    assert "search_evidence" in names
    for item in PLANNER_TOOLS:
        assert item["type"] == "function"
        assert "parameters" in item["function"]


def test_ask_user_is_illegal_before_snapshot():
    memory = AgentMemory("宁德时代", "ask3")
    action = parse_action(
        {
            "name": "ask_user",
            "field": "keywords",
            "question": "还要搜什么？",
            "reason": "缺检索词",
        }
    )
    assert not is_legal(action, memory, extra_limit=2)
    memory.mark("snapshot")
    assert is_legal(action, memory, extra_limit=2)


def test_followup_still_inherits_within_twenty_turns():
    client = TestClient(app)
    queries = [f"闲聊{i}" for i in range(19)] + ["宁德时代今天为什么跌了"]
    response = client.post(
        "/api/research",
        json={
            "query": "写一份完整的异动",
            "window": "d5",
            "context_queries": queries,
            "context_stocks": ["宁德时代"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "report"
    assert body["subject"]["stock"]["name"] == "宁德时代"
