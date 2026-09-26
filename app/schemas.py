"""研究 Brief 的输出结构。

所有数值都走 ``Measure``（带口径与证据 id），所有判断都走 ``*Assessment``
（带证据引用与未解决问题）。前端只渲染这里的字段，不做二次计算。
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from app.contracts import (
    CheckResult,
    DataGap,
    DriverCategory,
    DriverStatus,
    Evidence,
    EvidenceRef,
    EvidenceStrength,
    ExposureLevel,
    FetchStatus,
    ImpactDirection,
    ImpactHorizon,
    PricePattern,
    ResearchPriority,
    ResearchWindow,
    ToolCall,
)


class Measure(BaseModel):
    """一个可追溯的数值。"""

    key: str
    label: str
    value: Optional[float] = None
    display: str  # 已格式化的展示值，缺失时为"数据不可用"
    unit: Optional[str] = None
    caliber: Optional[str] = None  # 计算口径，例如 "收盘价 / 前收盘价 - 1"
    status: FetchStatus = FetchStatus.OK
    evidence_id: Optional[str] = None
    note: Optional[str] = None


class StockRef(BaseModel):
    name: str
    thscode: str
    ticker: Optional[str] = None
    exchange: Optional[str] = None


class WindowInfo(BaseModel):
    window: ResearchWindow
    label: str
    trading_days: List[str] = Field(default_factory=list)  # yyyy-MM-dd 升序
    actual_start: Optional[str] = None
    actual_end: Optional[str] = None
    is_intraday: bool = False  # 盘中数据，需显式标记
    remapped: bool = False  # 用户选"今日"但当天非交易日
    remap_note: Optional[str] = None


class SubjectSection(BaseModel):
    """区域一：研究对象。"""

    stock: StockRef
    user_question: str
    window: WindowInfo
    resolved_note: Optional[str] = None


class IndustryInfo(BaseModel):
    index_name: Optional[str] = None
    index_code: Optional[str] = None
    method: str  # structural | reverse_map | llm_assisted | unavailable
    method_label: str
    is_weak_evidence: bool = False
    note: Optional[str] = None


class LayerComparison(BaseModel):
    """市场 / 行业 / 个股三层对比。只用于确定研究优先级。"""

    stock: Measure
    industry: Optional[Measure] = None
    market: Optional[Measure] = None
    industry_vs_market: Optional[Measure] = None
    stock_vs_industry: Optional[Measure] = None
    disclaimer: str = "三层对比仅用于确定研究优先级，不代表各层对本次价格变化的因果贡献。"


class PricePoint(BaseModel):
    date: str
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    prev_close: Optional[float] = None
    volume: Optional[float] = None
    amount: Optional[float] = None
    pct_change: Optional[float] = None


class WhatHappenedSection(BaseModel):
    """区域二：发生了什么。全部由确定性规则层产出。"""

    measures: List[Measure] = Field(default_factory=list)
    pattern: PricePattern
    pattern_label: str
    pattern_reason: str
    comparison: LayerComparison
    industry: IndustryInfo
    series: List[PricePoint] = Field(default_factory=list)
    summary: str
    gaps: List[DataGap] = Field(default_factory=list)


class DriverCheck(BaseModel):
    """候选驱动因素的一项验证。"""

    key: str  # timing | cross_section | specificity | mechanism
    label: str
    result: CheckResult
    result_label: str
    reasoning: str
    evidence_refs: List[EvidenceRef] = Field(default_factory=list)


class TransmissionStep(BaseModel):
    text: str
    is_conditional: bool = False  # 是否带"若…则…"条件
    evidence_refs: List[EvidenceRef] = Field(default_factory=list)


class FundamentalAssessment(BaseModel):
    """区域四：单个驱动因素的基本面传导与三维判断。"""

    exposure_level: ExposureLevel
    exposure_label: str
    exposure_basis: str
    exposure_refs: List[EvidenceRef] = Field(default_factory=list)
    chain: List[TransmissionStep] = Field(default_factory=list)
    offsetting_factors: List[str] = Field(default_factory=list)
    amplifying_factors: List[str] = Field(default_factory=list)
    key_unknowns: List[str] = Field(default_factory=list)

    # 模型原始判断
    direction: ImpactDirection
    horizon: ImpactHorizon
    strength: EvidenceStrength

    # 经展示约束层处理后的对外表达
    display_direction: str
    display_horizon: str
    display_strength: str
    display_headline: str
    display_suppressed: bool = False  # 是否因证据不足被降级展示
    suppression_reason: Optional[str] = None

    direction_reason: str = ""
    horizon_reason: str = ""
    strength_reason: str = ""


class Driver(BaseModel):
    """区域三：一个候选驱动因素。"""

    id: str
    name: str
    category: DriverCategory
    category_label: str
    status: DriverStatus
    status_label: str
    summary: str
    relevance: str  # 与本次异动的相关程度描述
    supporting_refs: List[EvidenceRef] = Field(default_factory=list)
    contradicting_refs: List[EvidenceRef] = Field(default_factory=list)
    checks: List[DriverCheck] = Field(default_factory=list)
    unresolved: List[str] = Field(default_factory=list)
    assessment: Optional[FundamentalAssessment] = None  # 证据不足的驱动不进入第三阶段


class WhyHappenedSection(BaseModel):
    priority: ResearchPriority
    priority_label: str
    priority_reason: str
    drivers: List[Driver] = Field(default_factory=list)
    clue_pool: List[str] = Field(default_factory=list)  # 第三方异动解读等线索，非结论
    evidence_window_note: str = ""
    gaps: List[DataGap] = Field(default_factory=list)


class OverallVerdict(BaseModel):
    """多驱动因素的总体概括。不做票数或百分比归因。"""

    direction: ImpactDirection
    display: str
    reason: str
    can_summarize: bool


class WhatItMeansSection(BaseModel):
    assessments: List[str] = Field(default_factory=list)  # driver id 列表，保持顺序
    overall: OverallVerdict
    note: str = (
        "各驱动因素独立评估，不做百分比归因，也不对未解释的价格变化部分作因果拆分。"
    )


class OpenQuestionsSection(BaseModel):
    questions: List[str] = Field(default_factory=list)
    gaps: List[DataGap] = Field(default_factory=list)
    note: str = (
        "待观察事项是研究结果的一部分：证据不足以支撑判断时明确列出，不强行补全。"
    )


class RunMetrics(BaseModel):
    """北极星指标与质量护栏指标。"""

    time_to_verifiable_insight_ms: int = 0
    total_conclusions: int = 0
    conclusions_with_evidence: int = 0
    evidence_coverage: float = 0.0  # 有可追溯证据支持的核心结论占比
    unsupported_inference_rate: float = 0.0  # 缺充分证据却被当结论输出的占比
    evidence_count: int = 0
    independent_source_count: int = 0
    tool_calls: int = 0
    failed_tool_calls: int = 0
    agent_steps: int = 0
    degraded: bool = False  # 是否有任何降级 / 缺口


class AgentStep(BaseModel):
    """Agent 环里的一步：规划器选出的动作，以及它为什么选这一步。"""

    seq: int
    action: str
    reason: str = ""


class ResearchTrace(BaseModel):
    tool_calls: List[ToolCall] = Field(default_factory=list)
    llm_calls: List[Dict[str, Any]] = Field(default_factory=list)
    providers: Dict[str, str] = Field(default_factory=dict)
    plan: List[str] = Field(default_factory=list)
    agent_steps: List[AgentStep] = Field(default_factory=list)


class ResearchBrief(BaseModel):
    """最终交付：个股事件研究 Brief。"""

    run_id: str
    created_at: str
    kind: Literal["snapshot", "report"] = "report"
    subject: SubjectSection
    what_happened: WhatHappenedSection
    why_happened: WhyHappenedSection
    what_it_means: WhatItMeansSection
    open_questions: OpenQuestionsSection
    evidence: List[Evidence] = Field(default_factory=list)
    metrics: RunMetrics
    trace: ResearchTrace
    disclaimers: List[str] = Field(default_factory=list)
