"""推理层接口与确定性启发式实现。

产品把"不确定性判断"交给推理层，但**不包括**证据强度——证据强度衡量的是
证据链的完整程度，由规则层根据客观事实计算（见 ``app/engine/verdict.py``），
不允许模型自评置信度。

``HeuristicReasoner`` 在没有 LLM 密钥时接管推理层。它是一组明确写死的
启发式规则，能力显著弱于 LLM：它只能做关键词层面的匹配，无法真正理解
业务。这一点会在 Brief 和 README 中显式声明，绝不冒充 Agent 的推理结果。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, Sequence

from pydantic import BaseModel, Field

from app.agent.evidence_collect import ClusterInfo
from app.agent.validation import MarketContext
from app.contracts import (
    CheckResult,
    DriverCategory,
    EvidenceKind,
    ExposureLevel,
    ImpactDirection,
    ImpactHorizon,
    SourceTier,
)
from app.ledger import EvidenceLedger

SCOPE_TO_CATEGORY = {
    "market": DriverCategory.MARKET,
    "industry": DriverCategory.INDUSTRY,
    "company": DriverCategory.COMPANY,
    "trading": DriverCategory.TRADING,
}


class DriverProposal(BaseModel):
    name: str
    category: DriverCategory
    summary: str
    cluster_id: str
    relevance: str


class MechanismVerdict(BaseModel):
    result: CheckResult
    reasoning: str


class ChainStep(BaseModel):
    text: str
    is_conditional: bool = False
    link: str = ""
    evidence_ids: List[str] = Field(default_factory=list)
    status: str = "present"


class TransmissionDraft(BaseModel):
    exposure_level: ExposureLevel
    exposure_basis: str
    exposure_evidence_ids: List[str] = Field(default_factory=list)
    chain: List[ChainStep] = Field(default_factory=list)
    offsetting_factors: List[str] = Field(default_factory=list)
    amplifying_factors: List[str] = Field(default_factory=list)
    key_unknowns: List[str] = Field(default_factory=list)
    direction: ImpactDirection = ImpactDirection.UNCERTAIN
    direction_reason: str = ""
    horizon: ImpactHorizon = ImpactHorizon.UNCERTAIN
    horizon_reason: str = ""
    counter_evidence_ids: List[str] = Field(default_factory=list)


class Reasoner(Protocol):
    name: str

    async def propose_drivers(
        self, clusters: List[ClusterInfo], ctx: MarketContext, scopes: List[str]
    ) -> List[DriverProposal]: ...

    async def assess_mechanism(
        self, proposal: DriverProposal, cluster: ClusterInfo,
        clusters: List[ClusterInfo], ctx: MarketContext,
    ) -> MechanismVerdict: ...

    async def build_transmission(
        self, proposal: DriverProposal, cluster: ClusterInfo,
        clusters: List[ClusterInfo], ctx: MarketContext, ledger: EvidenceLedger,
    ) -> TransmissionDraft: ...


# --------------------------------------------------------------------------
# 关键词词表
# --------------------------------------------------------------------------

# 对公司基本面偏负面的措辞
_NEGATIVE_MARKERS = (
    "下调", "下修", "下滑", "下降", "减少", "削减", "亏损", "罚款", "处罚",
    "限制", "禁止", "门槛", "准入", "关税", "反倾销", "调查", "涨价", "上涨",
    "成本", "承压", "减持", "退市", "停产", "召回", "诉讼", "违规", "走弱",
)
# 对公司基本面偏正面的措辞
_POSITIVE_MARKERS = (
    "签署", "中标", "订单", "扩产", "补贴", "减税", "降价", "增长", "提价",
    "合作", "获批", "突破", "回购", "增持", "超预期", "投产", "放开",
)
# 抵消因素
_OFFSET_MARKERS = {
    "长协": "存在长期协议采购，可缓冲短期价格波动",
    "长期协议": "存在长期协议安排，可缓冲短期冲击",
    "框架协议": "已签署长期供货框架协议，对需求端形成一定保障",
    "套期保值": "存在套期保值安排，可对冲价格波动",
    "库存": "库存可提供缓冲期",
    "自有": "存在自有产能或自有资源，降低外部依赖",
    "牧场": "自有牧场可覆盖部分原料需求",
    "过渡期": "政策设置过渡期，短期内不会立即生效",
    "提价": "存在向下游提价的可能性，可转嫁部分成本",
    "替代": "存在替代供应来源",
}
# 放大因素
_AMPLIFY_MARKERS = {
    "价格战": "行业存在价格战，削弱转嫁成本的能力",
    "需求下降": "下游需求同步走弱，放大影响",
    "产能过剩": "产能过剩加剧竞争压力",
    "占比": "相关业务占比较高，放大影响幅度",
    "依赖": "对单一市场或客户依赖度较高",
}
# 期限判断线索
_ONE_OFF_MARKERS = ("一次性", "单次", "罚款", "短暂", "临时停产", "单笔")
_PHASED_MARKERS = ("过渡期", "阶段性", "临时", "周期", "季节", "年内", "短期")
# 刻意不含"长期"：「长期协议」「长期客户」这类说法极常见，
# 但都不构成规划 16 章所要求的结构性变化。
_STRUCTURAL_MARKERS = ("准入", "永久", "结构性", "退出市场", "牌照", "资质", "禁止")

# 正式披露类证据，用于确认公司暴露
_FILING_KINDS = (EvidenceKind.FILING, EvidenceKind.ANNOUNCEMENT)


class HeuristicReasoner:
    """没有 LLM 时的确定性兜底推理层。"""

    name = "heuristic"
    model = "rule-based"
    is_llm = False
    disclaimer = (
        "当前未配置大模型，第二、三阶段由确定性启发式规则生成。"
        "启发式规则只能做关键词层面的匹配，无法真正理解业务语义，"
        "其结论的可靠性明显低于接入模型后的结果。"
    )

    def __init__(self, max_drivers: int = 4) -> None:
        self._max_drivers = max_drivers

    async def propose_drivers(
        self, clusters: List[ClusterInfo], ctx: MarketContext, scopes: List[str]
    ) -> List[DriverProposal]:
        order = {s: i for i, s in enumerate(scopes)}
        # 背景事实（年报等慢变量披露）只作公司暴露证据，不作候选驱动因素
        candidates = [c for c in clusters if not c.is_background]
        ranked = sorted(
            candidates,
            key=lambda c: (
                order.get(c.scope, 99),
                not c.in_core_window,
                -c.independent_sources,
            ),
        )
        out: List[DriverProposal] = []
        for cluster in ranked[: self._max_drivers]:
            out.append(
                DriverProposal(
                    name=_shorten(cluster.title),
                    category=SCOPE_TO_CATEGORY.get(cluster.scope, DriverCategory.COMPANY),
                    summary=cluster.summary or cluster.title,
                    cluster_id=cluster.cluster_id,
                    relevance=_relevance_text(cluster),
                )
            )
        return out

    async def assess_mechanism(
        self,
        proposal: DriverProposal,
        cluster: ClusterInfo,
        clusters: List[ClusterInfo],
        ctx: MarketContext,
    ) -> MechanismVerdict:
        if proposal.category == DriverCategory.COMPANY:
            if cluster.best_tier in (SourceTier.T1_AUTHORITATIVE, SourceTier.T2_PROFESSIONAL):
                return MechanismVerdict(
                    result=CheckResult.PASS,
                    reasoning=(
                        "该事件由公司正式披露或专业财经媒体明确报道，"
                        "事件主体即目标公司本身，事件与公司之间的关系直接成立。"
                    ),
                )
            return MechanismVerdict(
                result=CheckResult.UNKNOWN,
                reasoning=(
                    "该事件缺少可确认的原始出处，无法判断它与目标公司之间"
                    "是否存在真实关系。"
                ),
            )

        exposure = _find_exposure_cluster(proposal, clusters, ctx)
        if exposure is not None:
            return MechanismVerdict(
                result=CheckResult.PASS,
                reasoning=(
                    f"公司正式披露中存在与该因素相关的业务暴露（{_shorten(exposure.title)}），"
                    "事件与公司之间存在可确认的作用路径。"
                ),
            )
        return MechanismVerdict(
            result=CheckResult.PARTIAL,
            reasoning=(
                "该因素作用于市场或行业整体，方向上与个股变化一致，"
                "但在现有证据中未能确认目标公司在该因素上的具体业务暴露，"
                "因此作用机制只能算部分成立。"
            ),
        )

    async def build_transmission(
        self,
        proposal: DriverProposal,
        cluster: ClusterInfo,
        clusters: List[ClusterInfo],
        ctx: MarketContext,
        ledger: EvidenceLedger,
    ) -> TransmissionDraft:
        text = f"{cluster.title} {cluster.summary}"
        exposure_cluster = _find_exposure_cluster(proposal, clusters, ctx)

        if proposal.category == DriverCategory.COMPANY and cluster.best_tier in (
            SourceTier.T1_AUTHORITATIVE,
        ):
            exposure_level = ExposureLevel.P1_FILING
            exposure_basis = "事件本身即来自公司正式披露，公司暴露不存在疑问。"
            exposure_ids = list(cluster.evidence_ids)
        elif exposure_cluster is not None:
            exposure_level = (
                ExposureLevel.P1_FILING
                if exposure_cluster.best_tier == SourceTier.T1_AUTHORITATIVE
                else ExposureLevel.P3_MEDIA
            )
            exposure_basis = f"依据「{_shorten(exposure_cluster.title)}」确认公司相关业务暴露。"
            exposure_ids = list(exposure_cluster.evidence_ids)
        else:
            exposure_level = ExposureLevel.UNCONFIRMED
            exposure_basis = (
                "该因素作用于市场整体，对所有上市公司普遍成立，"
                "不构成目标公司特有的业务暴露，因此不按公司暴露确认处理。"
                if proposal.category == DriverCategory.MARKET
                else "现有证据中没有公司年报、公告或其他正式披露可以确认公司"
                "在该因素上的业务暴露。"
            )
            exposure_ids = []

        direction, direction_reason = _infer_direction(text)
        horizon, horizon_reason = _infer_horizon(text)

        offsetting = _scan(clusters, _OFFSET_MARKERS)
        amplifying = _scan(clusters, _AMPLIFY_MARKERS)

        chain = _build_chain(
            proposal, cluster, exposure_level, exposure_ids, direction, horizon
        )

        unknowns: List[str] = []
        if exposure_level == ExposureLevel.UNCONFIRMED:
            unknowns.append(
                f"「{proposal.name}」属于市场层面的普遍因素，"
                "无法据此判断它对本公司基本面的具体影响。"
                if proposal.category == DriverCategory.MARKET
                else f"无法确认公司在「{proposal.name}」上的具体业务暴露规模与占比。"
            )
        if not offsetting:
            unknowns.append("未能确认是否存在长协、套期保值、库存等抵消安排。")
        if horizon == ImpactHorizon.UNCERTAIN:
            unknowns.append("缺少判断影响持续时间所需的终止条件或恢复机制信息。")
        if ctx.stock_vs_industry and abs(ctx.stock_vs_industry) >= ctx.divergence_threshold:
            unknowns.append(
                "该因素不足以解释个股相对行业的全部额外变化，仍有未被解释的部分。"
            )

        return TransmissionDraft(
            exposure_level=exposure_level,
            exposure_basis=exposure_basis,
            exposure_evidence_ids=exposure_ids,
            chain=chain,
            offsetting_factors=offsetting,
            amplifying_factors=amplifying,
            key_unknowns=unknowns,
            direction=direction,
            direction_reason=direction_reason,
            horizon=horizon,
            horizon_reason=horizon_reason,
        )


# --------------------------------------------------------------------------
# 启发式内部逻辑
# --------------------------------------------------------------------------


def _relevance_text(cluster: ClusterInfo) -> str:
    bits = []
    bits.append("发生在核心证据窗口内" if cluster.in_core_window else "发生在扩展证据窗口内")
    bits.append(f"{cluster.independent_sources} 个独立信源确认")
    bits.append(f"共 {cluster.report_count} 篇报道")
    if cluster.best_tier == SourceTier.T4_UNVERIFIED:
        bits.append("来源无法确认，仅作检索线索")
    return "；".join(bits) + "。"


def _find_exposure_cluster(
    proposal: DriverProposal, clusters: Sequence[ClusterInfo], ctx: MarketContext
) -> Optional[ClusterInfo]:
    """在公司正式披露里找与该驱动因素相关的业务暴露证据。"""
    # 市场/宏观因素不走这条路。风险偏好回落对每一家上市公司都成立，
    # 拿一份公司公告去"确认"它只会伪造出一条实际上不存在的证据链；
    # 这类因素的机制只能停在部分成立。
    if proposal.category == DriverCategory.MARKET:
        return None

    keywords = _keywords(proposal.name + proposal.summary)
    best: Optional[ClusterInfo] = None
    for cluster in clusters:
        if cluster.cluster_id == proposal.cluster_id:
            continue
        if cluster.scope != "company":
            continue
        if cluster.best_tier not in (
            SourceTier.T1_AUTHORITATIVE,
            SourceTier.T2_PROFESSIONAL,
        ):
            continue
        text = f"{cluster.title} {cluster.summary}"
        # 只重合一个二字词（"海外""主要"这类）几乎总是巧合，
        # 用它确认业务暴露等于凭空造证据，所以要求至少两个词重合。
        if len(_keywords(text) & keywords) < _MIN_EXPOSURE_OVERLAP:
            continue
        if best is None or cluster.best_tier == SourceTier.T1_AUTHORITATIVE:
            best = cluster
    return best


def _infer_direction(text: str):
    neg = [m for m in _NEGATIVE_MARKERS if m in text]
    pos = [m for m in _POSITIVE_MARKERS if m in text]
    if neg and pos:
        return (
            ImpactDirection.MIXED,
            f"事件描述中同时出现偏负面措辞（{'、'.join(neg[:3])}）"
            f"和偏正面措辞（{'、'.join(pos[:3])}），当前无法判断哪一方占主导。",
        )
    if neg:
        return (
            ImpactDirection.NEGATIVE,
            f"事件描述中出现「{'、'.join(neg[:3])}」等指向经营条件恶化的表述，"
            "主要传导路径偏向负面。",
        )
    if pos:
        return (
            ImpactDirection.POSITIVE,
            f"事件描述中出现「{'、'.join(pos[:3])}」等指向经营条件改善的表述，"
            "主要传导路径偏向正面。",
        )
    return (
        ImpactDirection.UNCERTAIN,
        "事件描述中没有足以判断基本面影响方向的明确表述。",
    )


def _infer_horizon(text: str):
    if any(m in text for m in _ONE_OFF_MARKERS):
        hit = next(m for m in _ONE_OFF_MARKERS if m in text)
        return (
            ImpactHorizon.ONE_OFF,
            f"事件描述包含「{hit}」，指向一次性或短期冲击，不持续改变经营条件。",
        )
    if any(m in text for m in _PHASED_MARKERS):
        hit = next(m for m in _PHASED_MARKERS if m in text)
        return (
            ImpactHorizon.PHASED,
            f"事件描述包含「{hit}」，存在明确的时间边界或恢复机制，"
            "影响更可能是阶段性的。",
        )
    if any(m in text for m in _STRUCTURAL_MARKERS):
        hit = next(m for m in _STRUCTURAL_MARKERS if m in text)
        return (
            ImpactHorizon.STRUCTURAL,
            f"事件描述包含「{hit}」，可能涉及市场准入或长期经营结构的变化。",
        )
    return (
        ImpactHorizon.UNCERTAIN,
        "现有信息中没有关于终止条件、可逆性或恢复路径的描述，无法判断影响期限。",
    )


def _scan(clusters: Sequence[ClusterInfo], markers: Dict[str, str]) -> List[str]:
    out: List[str] = []
    for cluster in clusters:
        text = f"{cluster.title} {cluster.summary}"
        for marker, description in markers.items():
            if marker in text and description not in out:
                out.append(description)
    return out


def _build_chain(
    proposal: DriverProposal,
    cluster: ClusterInfo,
    exposure: ExposureLevel,
    exposure_ids: List[str],
    direction: ImpactDirection,
    horizon: ImpactHorizon,
) -> List[ChainStep]:
    steps = [
        ChainStep(
            text=f"事件事实：{proposal.name}",
            link="event",
            evidence_ids=list(cluster.evidence_ids),
            status="present",
        )
    ]
    if exposure == ExposureLevel.UNCONFIRMED:
        steps.append(
            ChainStep(
                text="公司暴露：现有证据无法确认公司在该因素上的业务暴露",
                link="exposure",
                status="missing",
            )
        )
        steps.append(
            ChainStep(
                text="由于公司暴露未确认，传导链在此中断，不继续推导基本面结果",
                is_conditional=True,
                link="mechanism",
                status="missing",
            )
        )
        return steps

    steps.append(
        ChainStep(
            text="公司暴露：已由正式披露确认存在相关业务暴露",
            link="exposure",
            evidence_ids=list(exposure_ids),
            status="present",
        )
    )
    if direction == ImpactDirection.NEGATIVE:
        steps.append(
            ChainStep(
                text="若该因素持续，相关业务的经营条件面临压力",
                is_conditional=True,
                link="mechanism",
                evidence_ids=list(cluster.evidence_ids),
                status="present",
            )
        )
    elif direction == ImpactDirection.POSITIVE:
        steps.append(
            ChainStep(
                text="若该因素兑现，相关业务的经营条件有望改善",
                is_conditional=True,
                link="mechanism",
                evidence_ids=list(cluster.evidence_ids),
                status="present",
            )
        )
    elif direction == ImpactDirection.MIXED:
        steps.append(
            ChainStep(
                text="正负两条传导路径同时存在，当前无法判断净影响方向",
                is_conditional=True,
                link="mechanism",
                evidence_ids=list(cluster.evidence_ids),
                status="present",
            )
        )
        steps.append(
            ChainStep(
                text="影响方向：现有证据不足以判断净影响",
                link="direction",
                status="missing",
            )
        )
        return steps
    else:
        steps.append(
            ChainStep(
                text="影响方向：事件描述中没有足以判断基本面方向的明确表述",
                link="direction",
                status="missing",
            )
        )
        return steps

    steps.append(
        ChainStep(
            text=f"影响方向：{_direction_phrase(direction)}",
            link="direction",
            evidence_ids=list(cluster.evidence_ids),
            status="present",
        )
    )
    if horizon == ImpactHorizon.UNCERTAIN:
        steps.append(
            ChainStep(
                text="影响期限：缺少终止条件或恢复机制，无法判断",
                link="horizon",
                status="missing",
            )
        )
        return steps
    steps.append(
        ChainStep(
            text=f"影响期限：{_horizon_phrase(horizon)}",
            link="horizon",
            evidence_ids=list(cluster.evidence_ids),
            status="present",
        )
    )
    return steps


def _direction_phrase(direction: ImpactDirection) -> str:
    return {
        ImpactDirection.POSITIVE: "经营条件偏向改善",
        ImpactDirection.NEGATIVE: "经营条件偏向承压",
        ImpactDirection.MIXED: "正负影响并存",
        ImpactDirection.UNCERTAIN: "尚无法判断",
    }[direction]


def _horizon_phrase(horizon: ImpactHorizon) -> str:
    return {
        ImpactHorizon.ONE_OFF: "更可能是一次性或短期冲击",
        ImpactHorizon.PHASED: "存在时间边界，更可能是阶段性影响",
        ImpactHorizon.STRUCTURAL: "可能改变长期经营结构",
        ImpactHorizon.UNCERTAIN: "尚无法判断",
    }[horizon]


_MIN_EXPOSURE_OVERLAP = 2


def _keywords(text: str) -> set:
    """抽取 2-gram 关键词，用于粗粒度的相关性匹配。"""
    cleaned = "".join(ch for ch in (text or "") if ch.strip())
    return {cleaned[i : i + 2] for i in range(len(cleaned) - 1)}


def _shorten(text: str, limit: int = 40) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"
