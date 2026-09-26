"""Agent 工作记忆。

一次研究运行里，规划器读的是这块 scratchpad，而不是重新扫原始接口返回。
它记录已经做过的动作、失败的检索范围、以及装配 Brief 还缺什么。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from app.agent.actions import ActionName, AgentAction
from app.agent.evidence_collect import CollectionResult, EvidenceWindow
from app.agent.reasoner import DriverProposal, MechanismVerdict
from app.agent.validation import MarketContext
from app.contracts import DataGap, ResearchPriority
from app.ledger import EvidenceLedger
from app.pipeline.snapshot import MarketSnapshot
from app.pipeline.subject import ResolvedSubject
from app.schemas import AgentStep, Driver, LayerComparison, ResearchBrief, WhatHappenedSection


class AgentMemory:
    def __init__(self, query: str, run_id: str) -> None:
        self.query = query
        self.run_id = run_id
        self.ledger = EvidenceLedger()
        self.gaps: List[DataGap] = []
        self.actions: List[AgentStep] = []
        self.plan: List[str] = []

        self.subject: Optional[ResolvedSubject] = None
        self.snapshot: Optional[MarketSnapshot] = None
        self.window = None
        self.stock_evidence_id: Optional[str] = None
        self.market_evidence_id: Optional[str] = None
        self.industry_evidence_id: Optional[str] = None
        self.comparison: Optional[LayerComparison] = None
        self.priority: Optional[ResearchPriority] = None
        self.priority_reason: str = ""
        self.scopes: List[str] = []
        self.what_happened: Optional[WhatHappenedSection] = None

        self.clue_pool: List[str] = []
        self.evidence_window: Optional[EvidenceWindow] = None
        self.collection: Optional[CollectionResult] = None
        self.ctx: Optional[MarketContext] = None
        self.failed_scopes: Set[str] = set()
        self.retried_scopes: Set[str] = set()
        self.extra_searches = 0

        self.reasoner: Any = None
        self.proposals: List[DriverProposal] = []
        self.pairs: List[Any] = []
        self.mechanisms: List[MechanismVerdict] = []
        self.drivers: List[Driver] = []
        self.drafts: Dict[str, Any] = {}

        self.brief: Optional[ResearchBrief] = None
        self.ask: Optional[Dict[str, Any]] = None
        self.answers: Dict[str, str] = {}
        self.done = False
        self._flags: Set[str] = set()

    def answered(self, field: str) -> bool:
        return bool((self.answers.get(field) or "").strip())

    def ask_if_needed(self) -> Optional[AgentAction]:
        """启发式只在弱行业且用户还没表态时提问，避免打断主链路测试。"""
        if not self.has("snapshot") or self.has("retrieved"):
            return None
        subject = self.subject
        industry = getattr(subject, "industry", None) if subject else None
        if industry is None or not getattr(industry, "is_weak_evidence", False):
            return None
        if self.answered("industry") or self.has("asked:industry"):
            return None
        guessed = getattr(industry, "index_name", None) or "该行业"
        return AgentAction(
            name=ActionName.ASK_USER,
            reason="行业由弱证据推断，需要用户确认后才继续定向取数",
            field="industry",
            question=f"目前只能弱证据判断所属行业是「{guessed}」。是否按这个行业继续研究？",
            choices=[guessed, "按弱证据继续"],
        )

    def has(self, flag: str) -> bool:
        return flag in self._flags

    def mark(self, flag: str) -> None:
        self._flags.add(flag)

    def record(self, action: AgentAction) -> None:
        step = AgentStep(
            seq=len(self.actions) + 1,
            action=action.name.value,
            reason=action.reason,
        )
        self.actions.append(step)
        self.plan.append(f"{step.seq}. {action.name.value}：{action.reason}")

    def planner_view(self) -> Dict[str, Any]:
        """给规划器看的压缩状态，不塞原始 K 线和新闻正文。"""
        collection = self.collection
        return {
            "query": self.query,
            "has_subject": self.has("subject"),
            "has_snapshot": self.has("snapshot"),
            "stock": self.subject.stock.name if self.subject else None,
            "industry": (
                self.subject.industry.index_name if self.subject else None
            ),
            "priority": self.priority.value if self.priority else None,
            "scopes": self.scopes,
            "retrieved": self.has("retrieved"),
            "cluster_count": len(collection.clusters) if collection else 0,
            "failed_scopes": sorted(self.failed_scopes),
            "retried_scopes": sorted(self.retried_scopes),
            "extra_searches": self.extra_searches,
            "driver_count": len(self.drivers),
            "proposed": self.has("proposed"),
            "assessed": self.has("assessed"),
            "counter_done": self.has("counter"),
            "transmitted": self.has("transmitted"),
            "gap_fields": [g.field for g in self.gaps],
            "answers": dict(self.answers),
            "industry_weak": bool(
                self.subject
                and getattr(self.subject.industry, "is_weak_evidence", False)
            ),
        }
