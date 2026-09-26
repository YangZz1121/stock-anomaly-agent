"""Agent 环里规划器可以发出的动作。

动作是控制面，不是计算结果。价格、形态、证据强度仍然由规则层算。
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ActionName(str, Enum):
    RESOLVE_SUBJECT = "resolve_subject"
    FETCH_SNAPSHOT = "fetch_snapshot"
    SEARCH_EVIDENCE = "search_evidence"
    PROPOSE_DRIVERS = "propose_drivers"
    ASSESS_MECHANISMS = "assess_mechanisms"
    SEARCH_COUNTER = "search_counter_evidence"
    BUILD_TRANSMISSIONS = "build_transmissions"
    ASSEMBLE_BRIEF = "assemble_brief"


class AgentAction(BaseModel):
    name: ActionName
    reason: str = ""
    scopes: Optional[List[str]] = None
    extra_terms: List[str] = Field(default_factory=list)


TOOL_SPECS = [
    {
        "name": ActionName.RESOLVE_SUBJECT.value,
        "description": "识别股票、所属行业和研究窗口。行情和资讯检索之前必须完成。",
    },
    {
        "name": ActionName.FETCH_SNAPSHOT.value,
        "description": "拉取个股 / 市场 / 行业日 K，计算价格画像和研究优先级。",
    },
    {
        "name": ActionName.SEARCH_EVIDENCE.value,
        "description": "按范围检索资讯、公告、政策。可指定 scopes 与 extra_terms 做补检。",
        "parameters": {
            "scopes": ["market", "industry", "company"],
            "extra_terms": ["补充检索词"],
        },
    },
    {
        "name": ActionName.PROPOSE_DRIVERS.value,
        "description": "从已检索的事件聚类生成候选驱动因素。",
    },
    {
        "name": ActionName.ASSESS_MECHANISMS.value,
        "description": "对每个候选做机制合理性判断，并由规则层完成时间 / 横截面 / 特异性验证。",
    },
    {
        "name": ActionName.SEARCH_COUNTER.value,
        "description": "主动检索可能削弱现有解释的反向证据。",
    },
    {
        "name": ActionName.BUILD_TRANSMISSIONS.value,
        "description": "为通过验证的驱动因素构建基本面传导链。",
    },
    {
        "name": ActionName.ASSEMBLE_BRIEF.value,
        "description": "把工作记忆装配成研究 Brief，并过合规护栏。",
    },
]
