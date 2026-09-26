"""第一阶段：多日价格形态。

对应规划文档 5.3。所有阈值都从 ``Settings`` 传入，代码里不出现裸数字，
因为 60% 的单日集中度只是 MVP 的启发式规则，不是行业标准，必须是可调的。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from pydantic import BaseModel, Field

from app.contracts import DataGap, PricePattern
from app.providers.base import Bar

CALIBERS = {
    "cumulative_pct": "区间末收盘价 / 区间首日前收盘价 - 1",
    "concentration": "最大单日绝对收益 ÷ 窗口内所有交易日绝对收益之和",
    "direction_consistency": "与区间总体方向同向的交易日数 ÷ 有效交易日数",
    "path_efficiency": "|区间净价格变化| ÷ 期间逐日价格变化绝对值之和",
    "max_drawdown": "区间内收盘价从阶段高点回落的最大幅度",
}


class DailyReturn(BaseModel):
    date: str
    pct: float


class WindowProfile(BaseModel):
    days: int
    cumulative_pct: Optional[float] = None
    daily_returns: List[DailyReturn] = Field(default_factory=list)
    max_single_day: Optional[DailyReturn] = None
    concentration: Optional[float] = None
    direction_consistency: Optional[float] = None
    same_direction_days: int = 0
    path_efficiency: Optional[float] = None
    max_drawdown: Optional[float] = None
    reversal_split_date: Optional[str] = None
    pattern: PricePattern = PricePattern.MIXED
    pattern_reason: str = ""
    gaps: List[DataGap] = Field(default_factory=list)


def build_window_profile(
    bars: List[Bar],
    *,
    concentration_threshold: float,
    consistency_threshold: float,
    path_efficiency_threshold: float,
    reversal_min_segment_pct: float,
) -> WindowProfile:
    """由窗口内的日 K 序列计算价格形态。

    ``bars`` 必须按日期升序，且首根 K 线需带 ``prev_close``。
    """
    gaps: List[DataGap] = []
    profile = WindowProfile(days=len(bars))

    usable = [b for b in bars if b.close is not None and b.prev_close not in (None, 0)]
    if len(usable) < len(bars):
        missing = len(bars) - len(usable)
        gaps.append(
            DataGap(
                field="daily_returns",
                reason=f"{missing} 个交易日缺少收盘价或前收盘价（可能停牌）",
                impact="这些交易日不计入形态判断，区间统计口径相应缩小",
            )
        )

    if not usable:
        profile.gaps = gaps + [
            DataGap(
                field="window_profile",
                reason="窗口内没有任何可用交易日数据",
                impact="无法生成价格形态，第一阶段结论不成立",
            )
        ]
        profile.pattern_reason = "窗口内无可用数据"
        return profile

    profile.daily_returns = [
        DailyReturn(date=b.date, pct=b.close / b.prev_close - 1) for b in usable
    ]

    first, last = usable[0], usable[-1]
    profile.cumulative_pct = last.close / first.prev_close - 1

    pcts = [r.pct for r in profile.daily_returns]
    abs_sum = sum(abs(p) for p in pcts)
    profile.max_single_day = max(profile.daily_returns, key=lambda r: abs(r.pct))

    if abs_sum > 0:
        profile.concentration = abs(profile.max_single_day.pct) / abs_sum
    else:
        gaps.append(
            DataGap(
                field="concentration",
                reason="窗口内所有交易日收益均为 0",
                impact="无法计算单日集中度",
            )
        )

    total_sign = _sign(profile.cumulative_pct)
    if total_sign != 0:
        same = sum(1 for p in pcts if _sign(p) == total_sign)
        profile.same_direction_days = same
        profile.direction_consistency = same / len(pcts)
    else:
        gaps.append(
            DataGap(
                field="direction_consistency",
                reason="区间累计涨跌为 0，没有总体方向",
                impact="无法计算方向一致性",
            )
        )

    profile.path_efficiency, eff_gap = _path_efficiency(usable)
    if eff_gap:
        gaps.append(eff_gap)

    if len(usable) >= 5:
        profile.max_drawdown = _max_drawdown([b.close for b in usable])

    profile.reversal_split_date = _detect_reversal(
        usable, reversal_min_segment_pct
    )

    profile.pattern, profile.pattern_reason = _classify(
        profile,
        window_len=len(bars),
        concentration_threshold=concentration_threshold,
        consistency_threshold=consistency_threshold,
        path_efficiency_threshold=path_efficiency_threshold,
    )
    profile.gaps = gaps
    return profile


def _path_efficiency(bars: List[Bar]) -> Tuple[Optional[float], Optional[DataGap]]:
    """净价格变化 ÷ 累计绝对价格变化。越接近 1 路径越单一。"""
    prices = [bars[0].prev_close] + [b.close for b in bars]
    total_abs = sum(abs(prices[i] - prices[i - 1]) for i in range(1, len(prices)))
    if total_abs <= 0:
        return None, DataGap(
            field="path_efficiency",
            reason="区间内价格没有发生任何变化",
            impact="无法计算路径效率",
        )
    net = abs(prices[-1] - prices[0])
    return net / total_abs, None


def _max_drawdown(closes: List[float]) -> Optional[float]:
    if len(closes) < 2:
        return None
    peak = closes[0]
    worst = 0.0
    for c in closes[1:]:
        peak = max(peak, c)
        if peak > 0:
            worst = min(worst, c / peak - 1)
    return worst


def _detect_reversal(bars: List[Bar], min_segment_pct: float) -> Optional[str]:
    """在第 2 或第 3 个交易日切分，判断前后两段方向是否明显相反。"""
    if len(bars) < 4:
        return None
    for split in (2, 3):
        if split >= len(bars):
            continue
        head = bars[:split]
        tail = bars[split:]
        head_pct = head[-1].close / head[0].prev_close - 1
        tail_pct = tail[-1].close / tail[0].prev_close - 1
        if _sign(head_pct) == 0 or _sign(tail_pct) == 0:
            continue
        if _sign(head_pct) == _sign(tail_pct):
            continue
        # 两段都必须有实质幅度，否则只是噪声
        if abs(head_pct) < min_segment_pct or abs(tail_pct) < min_segment_pct:
            continue
        return bars[split - 1].date
    return None


def _classify(
    profile: WindowProfile,
    *,
    window_len: int,
    concentration_threshold: float,
    consistency_threshold: float,
    path_efficiency_threshold: float,
) -> Tuple[PricePattern, str]:
    if window_len <= 1:
        return PricePattern.SINGLE_DAY, "单日窗口不做多日形态判断"

    conc = profile.concentration
    if conc is not None and conc >= concentration_threshold:
        day = profile.max_single_day.date if profile.max_single_day else "该日"
        return (
            PricePattern.SINGLE_SHOCK,
            f"单日集中度 {conc:.0%} ≥ 阈值 {concentration_threshold:.0%}，"
            f"区间变化主要来自 {day} 这一个交易日",
        )

    # 反转只在 5 日窗口判定
    if window_len >= 5 and profile.reversal_split_date:
        return (
            PricePattern.REVERSAL,
            f"以 {profile.reversal_split_date} 为切分点，前后两段累计收益方向相反"
            f"且均非微小波动",
        )

    consistency = profile.direction_consistency
    efficiency = profile.path_efficiency
    if (
        consistency is not None
        and efficiency is not None
        and consistency >= consistency_threshold
        and efficiency >= path_efficiency_threshold
    ):
        return (
            PricePattern.SUSTAINED,
            f"方向一致性 {consistency:.0%}、路径效率 {efficiency:.0%}，"
            f"{profile.same_direction_days}/{len(profile.daily_returns)} 个交易日同向，"
            f"价格沿单一方向持续变化",
        )

    parts = []
    if conc is not None:
        parts.append(f"单日集中度 {conc:.0%} 未达 {concentration_threshold:.0%}")
    if consistency is not None:
        parts.append(f"方向一致性 {consistency:.0%}")
    if efficiency is not None:
        parts.append(f"路径效率 {efficiency:.0%}")
    return (
        PricePattern.MIXED,
        "；".join(parts) + "，区间内涨跌交替，不属于单日冲击或单向持续"
        if parts
        else "可用指标不足以归入其他形态",
    )


def _sign(value: Optional[float], eps: float = 1e-12) -> int:
    if value is None or abs(value) < eps:
        return 0
    return 1 if value > 0 else -1
