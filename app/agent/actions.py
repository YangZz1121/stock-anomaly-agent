"""Agent 环里规划器可以发出的动作。

动作是控制面，不是计算结果。价格、形态、证据强度仍然由规则层算。
规划器通过原生 Function Calling 从这张清单里选下一步。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class ActionName(str, Enum):
    RESOLVE_SUBJECT = "resolve_subject"
    FETCH_SNAPSHOT = "fetch_snapshot"
    SEARCH_EVIDENCE = "search_evidence"
    PROPOSE_DRIVERS = "propose_drivers"
    ASSESS_MECHANISMS = "assess_mechanisms"
    SEARCH_COUNTER = "search_counter_evidence"
    BUILD_TRANSMISSIONS = "build_transmissions"
    ASK_USER = "ask_user"
    ASSEMBLE_BRIEF = "assemble_brief"


class AgentAction(BaseModel):
    name: ActionName
    reason: str = ""
    scopes: Optional[List[str]] = None
    extra_terms: List[str] = Field(default_factory=list)
    field: str = ""
    question: str = ""
    choices: List[str] = Field(default_factory=list)


def _tool(name: str, description: str, properties: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


_REASON = {
    "reason": {
        "type": "string",
        "description": "为什么现在做这一步，一句话即可",
    }
}

PLANNER_TOOLS: List[Dict[str, Any]] = [
    _tool(
        ActionName.RESOLVE_SUBJECT.value,
        "识别股票、所属行业和研究窗口。行情和资讯检索之前必须完成。",
        _REASON,
        ["reason"],
    ),
    _tool(
        ActionName.FETCH_SNAPSHOT.value,
        "拉取个股 / 市场 / 行业日 K，计算价格画像和研究优先级。",
        _REASON,
        ["reason"],
    ),
    _tool(
        ActionName.SEARCH_EVIDENCE.value,
        "按范围检索资讯、公告、政策。可指定 scopes 与 extra_terms 做补检。",
        {
            **_REASON,
            "scopes": {
                "type": "array",
                "items": {"type": "string", "enum": ["market", "industry", "company"]},
                "description": "检索范围，默认沿用研究优先级",
            },
            "extra_terms": {
                "type": "array",
                "items": {"type": "string"},
                "description": "补充检索词，必须来自已有证据或用户原话",
            },
        },
        ["reason"],
    ),
    _tool(
        ActionName.PROPOSE_DRIVERS.value,
        "从已检索的事件聚类生成候选驱动因素。",
        _REASON,
        ["reason"],
    ),
    _tool(
        ActionName.ASSESS_MECHANISMS.value,
        "对每个候选做机制合理性判断，并由规则层完成时间 / 横截面 / 特异性验证。",
        _REASON,
        ["reason"],
    ),
    _tool(
        ActionName.SEARCH_COUNTER.value,
        "主动检索可能削弱现有解释的反向证据。",
        _REASON,
        ["reason"],
    ),
    _tool(
        ActionName.BUILD_TRANSMISSIONS.value,
        "为通过验证的驱动因素构建基本面传导链。",
        _REASON,
        ["reason"],
    ),
    _tool(
        ActionName.ASK_USER.value,
        "研究中途缺少关键信息时，暂停并向用户提问。用户回答后才会继续。",
        {
            **_REASON,
            "field": {
                "type": "string",
                "enum": ["industry", "keywords", "window", "continue"],
                "description": "用户需要补充的字段",
            },
            "question": {"type": "string", "description": "向用户提出的问题"},
            "choices": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选快捷回答，可空",
            },
        },
        ["reason", "field", "question"],
    ),
    _tool(
        ActionName.ASSEMBLE_BRIEF.value,
        "把工作记忆装配成研究 Brief，并过合规护栏。",
        _REASON,
        ["reason"],
    ),
]

TOOL_SPECS = [
    {
        "name": item["function"]["name"],
        "description": item["function"]["description"],
    }
    for item in PLANNER_TOOLS
]
