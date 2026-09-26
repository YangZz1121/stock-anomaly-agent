"""核心数据契约。

这里定义的三样东西贯穿整个产品：

1. ``Fetched`` —— 所有外部取数的统一包裹。产品不允许出现"裸数据"，
   每个数值都必须带着来源、时点、单位和口径一起流动。
2. ``Evidence`` —— 证据台账条目。来源可信度与对论点的支持度是两个
   独立维度，不允许互相替代。
3. 各类枚举 —— 把产品规划里的自然语言判断收敛成有限状态，
   避免 LLM 自由发挥出规划之外的结论类型。
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


# --------------------------------------------------------------------------
# 取数状态
# --------------------------------------------------------------------------


class FetchStatus(str, Enum):
    OK = "ok"
    MISSING = "missing"  # 接口正常但该标的无数据
    FAILED = "failed"  # 调用失败 / 超时 / 解析失败
    STALE = "stale"  # 拿到了数据但时点不满足研究窗口要求


class Fetched(BaseModel, Generic[T]):
    """外部取数的统一包裹。

    `status != OK` 时 `value` 必须为 None，调用方需要显式处理缺口，
    不允许把缺失当成正常值继续往下算。
    """

    status: FetchStatus
    value: Optional[T] = None
    source: str  # 形如 "fuyao:/api/a-share/prices/historical"
    provider: str  # "fuyao" | "mock" | "ifind" | ...
    as_of: Optional[int] = None  # 毫秒级 Unix 时间戳
    unit: Optional[str] = None
    note: Optional[str] = None  # 失败原因 / 统计口径说明

    @property
    def ok(self) -> bool:
        return self.status == FetchStatus.OK

    def unwrap(self, default: Optional[T] = None) -> Optional[T]:
        return self.value if self.ok else default

    @classmethod
    def success(
        cls,
        value: T,
        source: str,
        provider: str,
        as_of: Optional[int] = None,
        unit: Optional[str] = None,
        note: Optional[str] = None,
    ) -> "Fetched[T]":
        return cls(
            status=FetchStatus.OK,
            value=value,
            source=source,
            provider=provider,
            as_of=as_of,
            unit=unit,
            note=note,
        )

    @classmethod
    def failure(
        cls,
        source: str,
        provider: str,
        note: str,
        status: FetchStatus = FetchStatus.FAILED,
    ) -> "Fetched[T]":
        return cls(status=status, value=None, source=source, provider=provider, note=note)


# --------------------------------------------------------------------------
# 证据体系
# --------------------------------------------------------------------------


class SourceTier(str, Enum):
    """来源可信度。回答的是"这件事是否真的发生过"。"""

    T1_AUTHORITATIVE = "t1_authoritative"  # 国务院/部委/监管/交易所/公司正式公告
    T2_PROFESSIONAL = "t2_professional"  # 财联社/中证报/上证报/证券时报等
    T3_GENERAL = "t3_general"  # 一般可信媒体，需交叉验证
    T4_UNVERIFIED = "t4_unverified"  # 自媒体/匿名/无原始出处，仅作检索线索
    MARKET_DATA = "market_data"  # 行情接口返回的结构化事实


TIER_LABELS: Dict[str, str] = {
    SourceTier.T1_AUTHORITATIVE.value: "一级 · 原始权威来源",
    SourceTier.T2_PROFESSIONAL.value: "二级 · 专业财经媒体",
    SourceTier.T3_GENERAL.value: "三级 · 一般可信媒体",
    SourceTier.T4_UNVERIFIED.value: "四级 · 来源无法确认",
    SourceTier.MARKET_DATA.value: "结构化行情数据",
}

# 四级来源不能独立支撑核心结论，只能作为检索线索
TIERS_THAT_CANNOT_SUPPORT_CONCLUSIONS = {SourceTier.T4_UNVERIFIED}


class SupportLevel(str, Enum):
    """证据对某个具体论点的支持度。与来源可信度完全独立。"""

    SUPPORTS = "supports"
    WEAKLY_SUPPORTS = "weakly_supports"
    NEUTRAL = "neutral"
    WEAKENS = "weakens"
    REFUTES = "refutes"


SUPPORT_LABELS: Dict[str, str] = {
    SupportLevel.SUPPORTS.value: "支持",
    SupportLevel.WEAKLY_SUPPORTS.value: "弱支持",
    SupportLevel.NEUTRAL.value: "中性",
    SupportLevel.WEAKENS.value: "削弱",
    SupportLevel.REFUTES.value: "证伪",
}


class EvidenceKind(str, Enum):
    MARKET_DATA = "market_data"  # 行情/指数/资金等结构化事实
    NEWS = "news"
    POLICY = "policy"
    ANNOUNCEMENT = "announcement"  # 公司公告
    FILING = "filing"  # 年报/招股书等正式披露
    ANOMALY_TAG = "anomaly_tag"  # 第三方异动解读，仅作线索
    INFERENCE = "inference"  # 模型推断，非事实


class Evidence(BaseModel):
    """证据台账条目。

    ``origin_key`` 用于独立来源判定：10 家媒体转载同一份国务院文件，
    origin_key 相同，本质上仍是一个独立信源。
    """

    id: str
    kind: EvidenceKind
    claim: str  # 这条证据主张了什么事实
    source_tier: SourceTier
    source_name: str
    source_url: Optional[str] = None
    published_at: Optional[str] = None  # ISO 日期或 "yyyy-MM-dd HH:mm"
    provider: str = "mock"
    raw_ref: Optional[Dict[str, Any]] = None  # 原始字段快照，供"回到原文"
    cluster_id: Optional[str] = None  # 事件聚类
    origin_key: Optional[str] = None  # 独立信源标识
    unit: Optional[str] = None
    caliber: Optional[str] = None  # 统计口径

    @property
    def can_support_conclusion(self) -> bool:
        return self.source_tier not in TIERS_THAT_CANNOT_SUPPORT_CONCLUSIONS


class EvidenceRef(BaseModel):
    """结论对证据的引用，携带该证据对"这一条论点"的支持度。"""

    evidence_id: str
    support: SupportLevel
    rationale: Optional[str] = None


# --------------------------------------------------------------------------
# 研究窗口与价格形态
# --------------------------------------------------------------------------


class ResearchWindow(str, Enum):
    TODAY = "today"
    D3 = "d3"
    D5 = "d5"


WINDOW_LABELS: Dict[str, str] = {
    ResearchWindow.TODAY.value: "今日",
    ResearchWindow.D3.value: "最近 3 个交易日",
    ResearchWindow.D5.value: "最近 5 个交易日",
}

WINDOW_TRADING_DAYS: Dict[str, int] = {
    ResearchWindow.TODAY.value: 1,
    ResearchWindow.D3.value: 3,
    ResearchWindow.D5.value: 5,
}


class PricePattern(str, Enum):
    SINGLE_SHOCK = "single_shock"  # 单日冲击
    SUSTAINED = "sustained"  # 持续变化
    REVERSAL = "reversal"  # 反转（仅 5 日窗口）
    MIXED = "mixed"  # 混合变化
    SINGLE_DAY = "single_day"  # 今日窗口，不做多日形态判断


PATTERN_LABELS: Dict[str, str] = {
    PricePattern.SINGLE_SHOCK.value: "单日冲击型",
    PricePattern.SUSTAINED.value: "持续变化型",
    PricePattern.REVERSAL.value: "反转型",
    PricePattern.MIXED.value: "混合变化型",
    PricePattern.SINGLE_DAY.value: "单日窗口",
}


# --------------------------------------------------------------------------
# 驱动因素与基本面判断
# --------------------------------------------------------------------------


class DriverCategory(str, Enum):
    MARKET = "market"  # 市场 / 宏观
    INDUSTRY = "industry"  # 行业
    COMPANY = "company"  # 公司特定事件
    TRADING = "trading"  # 交易 / 关注度


CATEGORY_LABELS: Dict[str, str] = {
    DriverCategory.MARKET.value: "市场 / 宏观",
    DriverCategory.INDUSTRY.value: "行业",
    DriverCategory.COMPANY.value: "公司特定",
    DriverCategory.TRADING.value: "交易 / 关注度",
}


class DriverStatus(str, Enum):
    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    INSUFFICIENT = "insufficient"


DRIVER_STATUS_LABELS: Dict[str, str] = {
    DriverStatus.SUPPORTED.value: "支持",
    DriverStatus.PARTIALLY_SUPPORTED.value: "部分支持",
    DriverStatus.INSUFFICIENT.value: "证据不足",
}


class CheckResult(str, Enum):
    PASS = "pass"
    PARTIAL = "partial"
    FAIL = "fail"
    UNKNOWN = "unknown"  # 缺少判定所需数据


CHECK_RESULT_LABELS: Dict[str, str] = {
    CheckResult.PASS.value: "吻合",
    CheckResult.PARTIAL.value: "部分吻合",
    CheckResult.FAIL.value: "不吻合",
    CheckResult.UNKNOWN.value: "无法判定",
}


class ImpactDirection(str, Enum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    MIXED = "mixed"  # 正负影响并存
    UNCERTAIN = "uncertain"


class ImpactHorizon(str, Enum):
    ONE_OFF = "one_off"  # 短期一次性
    PHASED = "phased"  # 阶段性
    STRUCTURAL = "structural"  # 结构性
    UNCERTAIN = "uncertain"


class EvidenceStrength(str, Enum):
    SUFFICIENT = "sufficient"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class ExposureLevel(str, Enum):
    """公司暴露的确认等级，对应规划 13.2 的证据优先级。"""

    P1_FILING = "p1_filing"  # 年报/公告/招股书/交易所披露
    P2_COMPANY = "p2_company"  # 官网/投资者关系/正式采访
    P3_MEDIA = "p3_media"  # 高可信财经媒体的明确报道
    P4_INFERENCE = "p4_inference"  # 仅行业常识推断，弱证据
    UNCONFIRMED = "unconfirmed"  # 无法确认


EXPOSURE_LABELS: Dict[str, str] = {
    ExposureLevel.P1_FILING.value: "正式披露确认",
    ExposureLevel.P2_COMPANY.value: "公司公开材料确认",
    ExposureLevel.P3_MEDIA.value: "专业媒体报道确认",
    ExposureLevel.P4_INFERENCE.value: "仅行业常识推断（弱证据）",
    ExposureLevel.UNCONFIRMED.value: "无法确认",
}

# 暴露无法确认时，不允许继续形成强基本面结论
EXPOSURE_BLOCKS_STRONG_CONCLUSION = {ExposureLevel.UNCONFIRMED}


class ResearchPriority(str, Enum):
    """第一阶段输出的第二阶段调查顺序。不等于最终因果权重。"""

    MARKET_FIRST = "market_first"
    INDUSTRY_FIRST = "industry_first"
    COMPANY_FIRST = "company_first"
    INDUSTRY_PLUS_COMPANY = "industry_plus_company"


PRIORITY_LABELS: Dict[str, str] = {
    ResearchPriority.MARKET_FIRST.value: "市场 / 宏观优先",
    ResearchPriority.INDUSTRY_FIRST.value: "行业优先",
    ResearchPriority.COMPANY_FIRST.value: "公司特有因素优先",
    ResearchPriority.INDUSTRY_PLUS_COMPANY.value: "行业因素 + 公司特有因素并重",
}


class DataGap(BaseModel):
    """一处显式的数据缺口。产品宁可展示缺口，也不让模型补全。"""

    field: str
    reason: str
    impact: str  # 这个缺口导致哪些判断无法做出
    source: Optional[str] = None


class ToolCall(BaseModel):
    """研究轨迹里的一次工具调用记录。"""

    seq: int
    tool: str
    params: Dict[str, Any] = Field(default_factory=dict)
    status: FetchStatus
    provider: str
    latency_ms: Optional[int] = None
    note: Optional[str] = None
    evidence_ids: List[str] = Field(default_factory=list)
