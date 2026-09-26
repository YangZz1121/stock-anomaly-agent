"""LLM 驱动的推理层。

每次调用失败或返回不合法时，都退回 ``HeuristicReasoner`` 的对应方法，
并把这次降级记录进轨迹。这样做的理由是：一次模型调用失败不应该让整份
研究报废，但用户必须知道哪一部分不是模型产出的。

模型返回的证据编号会被逐一校验，凡是台账里不存在的编号一律丢弃。
"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from app.agent.evidence_collect import ClusterInfo
from app.agent.prompts import (
    DRIVER_SCHEMA,
    MECHANISM_SCHEMA,
    MECHANISM_SYSTEM,
    PROPOSE_DRIVERS_SYSTEM,
    TRANSMISSION_SCHEMA,
    TRANSMISSION_SYSTEM,
)
from app.agent.reasoner import (
    ChainStep,
    DriverProposal,
    HeuristicReasoner,
    MechanismVerdict,
    TransmissionDraft,
)
from app.agent.validation import MarketContext
from app.contracts import (
    CheckResult,
    DriverCategory,
    ExposureLevel,
    ImpactDirection,
    ImpactHorizon,
)
from app.engine import presenter
from app.engine.chain_logic import ground_chain
from app.ledger import EvidenceLedger
from app.trace import RunRecorder


class LLMReasoner:
    name = "llm"
    is_llm = True
    disclaimer = ""

    def __init__(
        self, llm: Any, recorder: RunRecorder, max_drivers: int = 4
    ) -> None:
        self._llm = llm
        self._recorder = recorder
        self._fallback = HeuristicReasoner(max_drivers=max_drivers)
        self._max_drivers = max_drivers
        self.model = getattr(llm, "model", "unknown")
        self.fallback_count = 0

    # ------------------------------------------------------------------

    async def _call(
        self, purpose: str, system: str, user: str, schema: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        started = time.perf_counter()
        res = await self._llm.complete_json(
            purpose=purpose, system=system, user=user, schema_hint=schema
        )
        self._recorder.record_llm(
            purpose,
            self.model,
            getattr(self._llm, "name", "unknown"),
            int((time.perf_counter() - started) * 1000),
            res.ok,
            res.note,
            prompt_chars=len(system) + len(user),
            response_chars=len(json.dumps(res.value or {}, ensure_ascii=False)),
        )
        if not res.ok:
            self.fallback_count += 1
            self._recorder.log(f"模型调用失败（{purpose}）：{res.note}；该环节回退到启发式规则")
        return res.value if res.ok else None

    # ------------------------------------------------------------------

    async def propose_drivers(
        self, clusters: List[ClusterInfo], ctx: MarketContext, scopes: List[str]
    ) -> List[DriverProposal]:
        if not clusters:
            return []
        # 背景事实（年报等慢变量披露）只作公司暴露证据，不作候选驱动因素
        candidates = [c for c in clusters if not c.is_background]
        if not candidates:
            return []
        payload = await self._call(
            "propose_drivers",
            PROPOSE_DRIVERS_SYSTEM,
            _drivers_prompt(candidates, ctx, scopes),
            DRIVER_SCHEMA,
        )
        if not payload:
            return await self._fallback.propose_drivers(clusters, ctx, scopes)

        valid_ids = {c.cluster_id for c in candidates}
        out: List[DriverProposal] = []
        for raw in (payload.get("drivers") or [])[: self._max_drivers]:
            cluster_id = raw.get("cluster_id")
            if cluster_id not in valid_ids:
                # 模型把事件聚类 id 编错了，这条候选无法回到证据，直接丢弃
                self._recorder.log(
                    f"丢弃一条候选驱动因素：引用了不存在的事件聚类 {cluster_id}"
                )
                continue
            category = _enum(raw.get("category"), DriverCategory, DriverCategory.COMPANY)
            out.append(
                DriverProposal(
                    name=(raw.get("name") or "未命名驱动因素")[:40],
                    category=category,
                    summary=raw.get("summary") or "",
                    cluster_id=cluster_id,
                    relevance=raw.get("relevance") or "",
                )
            )
        if not out:
            self.fallback_count += 1
            return await self._fallback.propose_drivers(clusters, ctx, scopes)
        return out

    async def assess_mechanism(
        self,
        proposal: DriverProposal,
        cluster: ClusterInfo,
        clusters: List[ClusterInfo],
        ctx: MarketContext,
    ) -> MechanismVerdict:
        payload = await self._call(
            "assess_mechanism",
            MECHANISM_SYSTEM,
            _mechanism_prompt(proposal, cluster, clusters, ctx),
            MECHANISM_SCHEMA,
        )
        if not payload:
            return await self._fallback.assess_mechanism(
                proposal, cluster, clusters, ctx
            )
        result = _enum(payload.get("result"), CheckResult, CheckResult.UNKNOWN)
        reasoning = payload.get("reasoning") or "模型未给出判断理由。"
        return MechanismVerdict(result=result, reasoning=reasoning)

    async def build_transmission(
        self,
        proposal: DriverProposal,
        cluster: ClusterInfo,
        clusters: List[ClusterInfo],
        ctx: MarketContext,
        ledger: EvidenceLedger,
    ) -> TransmissionDraft:
        payload = await self._call(
            "build_transmission",
            TRANSMISSION_SYSTEM,
            _transmission_prompt(proposal, cluster, clusters, ctx, ledger),
            TRANSMISSION_SCHEMA,
        )
        if not payload:
            return await self._fallback.build_transmission(
                proposal, cluster, clusters, ctx, ledger
            )

        exposure = _enum(
            payload.get("exposure_level"), ExposureLevel, ExposureLevel.UNCONFIRMED
        )
        direction = _enum(
            payload.get("direction"), ImpactDirection, ImpactDirection.UNCERTAIN
        )
        horizon = _enum(payload.get("horizon"), ImpactHorizon, ImpactHorizon.UNCERTAIN)

        chain = [
            ChainStep(
                text=str(step.get("text", "")).strip(),
                is_conditional=bool(step.get("is_conditional")),
                link=str(step.get("link") or "").strip(),
                evidence_ids=_valid_ids(step.get("evidence_ids"), ledger),
                status=str(step.get("status") or "present").strip() or "present",
            )
            for step in (payload.get("chain") or [])
            if str(step.get("text", "")).strip()
        ]

        exposure_ids = _valid_ids(payload.get("exposure_evidence_ids"), ledger)
        counter_ids = _valid_ids(payload.get("counter_evidence_ids"), ledger)

        # 模型声称公司暴露已确认，却给不出任何有效证据编号 —— 降回未确认
        if exposure != ExposureLevel.UNCONFIRMED and not exposure_ids:
            self._recorder.log(
                f"驱动因素「{proposal.name}」的公司暴露被模型标为"
                f"{exposure.value}，但未提供有效证据编号，已降级为无法确认"
            )
            exposure = ExposureLevel.UNCONFIRMED

        chain = ground_chain(chain, exposure, ledger)

        return TransmissionDraft(
            exposure_level=exposure,
            exposure_basis=payload.get("exposure_basis") or "",
            exposure_evidence_ids=exposure_ids,
            chain=chain,
            offsetting_factors=_str_list(payload.get("offsetting_factors")),
            amplifying_factors=_str_list(payload.get("amplifying_factors")),
            key_unknowns=_str_list(payload.get("key_unknowns")),
            direction=direction,
            direction_reason=payload.get("direction_reason") or "",
            horizon=horizon,
            horizon_reason=payload.get("horizon_reason") or "",
            counter_evidence_ids=counter_ids,
        )


# --------------------------------------------------------------------------
# 提示词拼装
# --------------------------------------------------------------------------


def _price_facts(ctx: MarketContext) -> str:
    fmt = presenter.pct
    lines = [
        f"研究窗口：{ctx.window_days[0]} ~ {ctx.window_days[-1]}（{len(ctx.window_days)} 个交易日）",
        f"个股区间涨跌：{fmt(ctx.stock_pct)}",
        f"行业指数（{ctx.industry_name or '未识别'}）区间涨跌：{fmt(ctx.industry_pct)}",
        f"市场宽基指数区间涨跌：{fmt(ctx.market_pct)}",
        f"个股相对行业：{presenter.pct_points(ctx.stock_vs_industry)}",
        f"行业相对市场：{presenter.pct_points(ctx.industry_vs_market)}",
        f"证据窗口：核心 {ctx.evidence_window.core_start} ~ {ctx.evidence_window.core_end}，"
        f"扩展回溯至 {ctx.evidence_window.extended_start}",
    ]
    if not ctx.has_minute_data:
        lines.append("注意：没有分钟级数据，不要推断某条信息对应某一具体时刻的价格变化。")
    return "\n".join(lines)


def _cluster_lines(clusters: List[ClusterInfo]) -> str:
    out = []
    for c in clusters:
        out.append(
            json.dumps(
                {
                    "cluster_id": c.cluster_id,
                    "scope": c.scope,
                    "title": c.title,
                    "summary": c.summary,
                    "evidence_ids": c.evidence_ids,
                    "source_tier": c.best_tier.value,
                    "independent_sources": c.independent_sources,
                    "report_count": c.report_count,
                    "earliest_published": c.earliest_published,
                    "in_core_window": c.in_core_window,
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(out)


def _drivers_prompt(
    clusters: List[ClusterInfo], ctx: MarketContext, scopes: List[str]
) -> str:
    return f"""目标公司：{ctx.stock_name}

【价格事实】
{_price_facts(ctx)}

【第一阶段确定的调查优先级】
{' → '.join(scopes)}
（该顺序只决定先调查什么，不代表因果权重）

【已检索到的事件聚类】
{_cluster_lines(clusters)}

请据此生成候选驱动因素。注意 source_tier 为 t4_unverified 的事件来源无法确认，
只能作为检索线索，不能独立支撑结论；如果你仍然把它列为候选，请在 relevance 中
明确指出这一点。"""


def _mechanism_prompt(
    proposal: DriverProposal,
    cluster: ClusterInfo,
    clusters: List[ClusterInfo],
    ctx: MarketContext,
) -> str:
    others = [c for c in clusters if c.cluster_id != cluster.cluster_id]
    return f"""目标公司：{ctx.stock_name}

【待验证的驱动因素】
名称：{proposal.name}
类别：{proposal.category.value}
说明：{proposal.summary}
对应事件：{cluster.title}｜{cluster.summary}
证据编号：{cluster.evidence_ids}

【价格事实】
{_price_facts(ctx)}

【其他可用证据】
{_cluster_lines(others)}

请判断该因素与 {ctx.stock_name} 之间是否存在合理的作用机制。
重点在于：公司是否真的暴露在这个因素之下。"""


def _transmission_prompt(
    proposal: DriverProposal,
    cluster: ClusterInfo,
    clusters: List[ClusterInfo],
    ctx: MarketContext,
    ledger: EvidenceLedger,
) -> str:
    evidence_lines = []
    for ev in ledger.all():
        evidence_lines.append(
            json.dumps(
                {
                    "id": ev.id,
                    "kind": ev.kind.value,
                    "tier": ev.source_tier.value,
                    "source": ev.source_name,
                    "published_at": ev.published_at,
                    "claim": ev.claim[:200],
                },
                ensure_ascii=False,
            )
        )
    return f"""目标公司：{ctx.stock_name}

【驱动因素】
名称：{proposal.name}
类别：{proposal.category.value}
说明：{proposal.summary}
对应事件：{cluster.title}｜{cluster.summary}

【价格事实】
{_price_facts(ctx)}

【全部可引用证据】
{chr(10).join(evidence_lines)}

请构建该驱动因素的基本面传导链，并判断影响方向与影响期限。
只能引用上面列出的证据编号。五环必须按 event → exposure → mechanism →
direction → horizon 写全或在缺环处停止。如果公司暴露无法确认，请返回
unconfirmed，并把该环标为 missing，不要继续推导后面的基本面结果。"""


# --------------------------------------------------------------------------


def _enum(value: Any, enum_cls, default):
    try:
        return enum_cls(str(value).strip().lower())
    except (ValueError, AttributeError):
        return default


def _valid_ids(raw: Any, ledger: EvidenceLedger) -> List[str]:
    """只保留台账里真实存在的证据编号，其余丢弃。"""
    if not isinstance(raw, list):
        return []
    return [str(x) for x in raw if str(x) in ledger]


def _str_list(raw: Any) -> List[str]:
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]
