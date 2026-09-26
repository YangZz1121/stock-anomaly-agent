"""对话路由：无意义输入、快照、完整报告等待文案、多公司拆分。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.contracts import ResearchWindow
from app.engine.intent import IntentKind, classify_intent, estimate_report_minutes
from app.main import app


def test_classifies_alias_and_simple_name_as_need_window():
    intent = classify_intent("宁王")
    assert intent.kind == IntentKind.NEED_WINDOW
    assert intent.parsed.name_hint == "宁德时代"


def test_classifies_name_plus_window_as_snapshot():
    intent = classify_intent("宁德时代", ResearchWindow.D5)
    assert intent.kind == IntentKind.SNAPSHOT
    assert not intent.wants_full_report


def test_classifies_why_query_as_report_without_eta():
    intent = classify_intent("宁德时代今天为什么跌了")
    assert intent.kind == IntentKind.REPORT
    assert not intent.wants_full_report


def test_classifies_explicit_report_request():
    intent = classify_intent("请写一份宁德时代完整异动分析报告", ResearchWindow.D5)
    assert intent.kind == IntentKind.REPORT
    assert intent.wants_full_report
    assert estimate_report_minutes(ResearchWindow.D5, 1) == 2


def test_classifies_greetings_and_gibberish_as_nonsense():
    assert classify_intent("asdf").kind == IntentKind.NONSENSE
    assert classify_intent("你好").kind == IntentKind.NONSENSE
    assert "您的问题「asdf」" in classify_intent("asdf").message


def test_classifies_why_without_stock_as_need_stock():
    intent = classify_intent("为什么跌了")
    assert intent.kind == IntentKind.NEED_STOCK


def test_http_nonsense_does_not_build_providers(monkeypatch):
    client = TestClient(app)

    def boom(*_args, **_kwargs):
        raise AssertionError("无意义输入不应建立数据源")

    monkeypatch.setattr("app.main.build_providers", boom)
    response = client.post("/api/research", json={"query": "asdf"})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "nonsense"
    assert "您的问题「asdf」" in detail["message"]


def test_http_snapshot_skips_evidence_and_llm():
    client = TestClient(app)
    response = client.post(
        "/api/research", json={"query": "宁德时代", "window": "d5"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "snapshot"
    assert body["why_happened"]["drivers"] == []
    assert body["evidence"] == []
    tools = {call["tool"] for call in body["trace"]["tool_calls"]}
    assert "search_ticker" in tools
    assert "daily_bars" in tools
    assert "search_events" not in tools
    assert body["trace"]["llm_calls"] == []


def test_sse_full_report_emits_notice_before_steps():
    client = TestClient(app)
    with client.stream(
        "GET",
        "/api/research/stream",
        params={
            "query": "请写一份宁德时代完整异动分析报告",
            "window": "d5",
        },
    ) as response:
        events = []
        notice = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                events.append(line[len("event: ") :])
            elif line.startswith("data: ") and events and events[-1] == "notice":
                notice = line[len("data: ") :]

    assert events[0] == "notice"
    assert events.index("start") == 1
    assert "brief" in events
    assert notice is not None
    assert "正在为您撰写分析报告" in notice
    assert "分钟" in notice


def test_http_multi_company_returns_separated_briefs():
    client = TestClient(app)
    response = client.post(
        "/api/research",
        json={"query": "宁德时代和贵州茅台今天异动对比", "window": "today"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "bundle"
    names = [item["subject"]["stock"]["name"] for item in body["briefs"]]
    assert names == ["宁德时代", "贵州茅台"]
    texts = [
        item["what_happened"]["summary"] + item["why_happened"]["priority_reason"]
        for item in body["briefs"]
    ]
    assert "贵州茅台" not in texts[0]
    assert "宁德时代" not in texts[1]
