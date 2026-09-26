"""第一阶段：单日价格画像。

本模块是纯函数，不做任何 I/O，也不引入 LLM。所有口径都写在 ``CALIBERS``
里并随结果一起返回，保证展示层能把每个数字还原成它的计算方式。

规划文档 5.2 里的三条限制在这里强制执行：
* 盘中只能使用最新价，且必须打上"盘中"标记；
* 盘中的部分成交额不与历史完整交易日的成交额直接比较；
* 缺少基准数据时不输出成交活跃度，而不是给一个看起来合理的数。
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field

from app.contracts import DataGap
from app.providers.base import Bar

CALIBERS = {
    "pct_change": "收盘价 / 前收盘价 - 1",
    "gap_pct": "开盘价 / 前收盘价 - 1",
    "post_open_pct": "收盘价 / 开盘价 - 1",
    "amplitude": "(最高价 - 最低价) / 前收盘价",
    "close_position": "(收盘价 - 最低价) / (最高价 - 最低价)",
    "turnover_ratio": "当日成交额 / 最近 20 个完整交易日的日均成交额",
}

INTRADAY_CALIBERS = {
    "pct_change": "最新价 / 前收盘价 - 1（盘中）",
    "post_open_pct": "最新价 / 开盘价 - 1（盘中）",
    "close_position": "(最新价 - 最低价) / (最高价 - 最低价)（盘中）",
}


class DailyProfile(BaseModel):
    date: str
    pct_change: Optional[float] = None
    gap_pct: Optional[float] = None
    post_open_pct: Optional[float] = None
    amplitude: Optional[float] = None
    close_position: Optional[float] = None
    turnover_ratio: Optional[float] = None
    turnover_amount: Optional[float] = None
    is_intraday: bool = False
    gaps: List[DataGap] = Field(default_factory=list)

    def caliber(self, key: str) -> str:
        if self.is_intraday and key in INTRADAY_CALIBERS:
            return INTRADAY_CALIBERS[key]
        return CALIBERS.get(key, "")


def build_daily_profile(
    bar: Bar,
    history: Optional[List[Bar]] = None,
    baseline_days: int = 20,
) -> DailyProfile:
    """计算单个交易日的价格画像。

    ``history`` 是该日之前的完整交易日序列，用于成交活跃度基准。
    """
    gaps: List[DataGap] = []
    profile = DailyProfile(
        date=bar.date,
        is_intraday=bar.is_intraday,
        turnover_amount=bar.amount,
    )

    if bar.prev_close in (None, 0):
        gaps.append(
            DataGap(
                field="prev_close",
                reason="缺少前收盘价",
                impact="无法计算全天涨跌、开盘缺口与日内振幅",
            )
        )
    else:
        if bar.close is not None:
            profile.pct_change = bar.close / bar.prev_close - 1
        if bar.open is not None:
            profile.gap_pct = bar.open / bar.prev_close - 1
        if bar.high is not None and bar.low is not None:
            profile.amplitude = (bar.high - bar.low) / bar.prev_close

    if bar.open not in (None, 0) and bar.close is not None:
        profile.post_open_pct = bar.close / bar.open - 1

    if bar.high is not None and bar.low is not None and bar.close is not None:
        span = bar.high - bar.low
        if span > 0:
            profile.close_position = (bar.close - bar.low) / span
        else:
            # 一字板：最高价等于最低价，收盘位置没有定义
            gaps.append(
                DataGap(
                    field="close_position",
                    reason="当日最高价与最低价相同（一字板或无波动）",
                    impact="收盘位置无定义，不参与形态判断",
                )
            )

    profile.turnover_ratio, turnover_gap = _turnover_ratio(
        bar, history or [], baseline_days
    )
    if turnover_gap is not None:
        gaps.append(turnover_gap)

    profile.gaps = gaps
    return profile


def _turnover_ratio(
    bar: Bar, history: List[Bar], baseline_days: int
):
    """成交活跃度。盘中一律不计算，避免拿半天成交额比整天。"""
    if bar.is_intraday:
        return None, DataGap(
            field="turnover_ratio",
            reason="当前为盘中数据，当日成交额尚未完整",
            impact="不将盘中部分成交额与历史完整交易日成交额比较，本次不输出成交活跃度",
        )
    if bar.amount in (None, 0):
        return None, DataGap(
            field="turnover_ratio",
            reason="缺少当日成交额",
            impact="无法计算成交活跃度",
        )

    amounts = [b.amount for b in history[-baseline_days:] if b.amount]
    if len(amounts) < max(5, baseline_days // 4):
        return None, DataGap(
            field="turnover_ratio",
            reason=f"可用历史成交额仅 {len(amounts)} 个交易日，不足以构成 {baseline_days} 日基准",
            impact="不输出成交活跃度判断",
        )
    avg = sum(amounts) / len(amounts)
    if avg <= 0:
        return None, DataGap(
            field="turnover_ratio",
            reason="历史日均成交额为 0",
            impact="无法计算成交活跃度",
        )
    return bar.amount / avg, None


def describe_turnover(ratio: Optional[float]) -> str:
    if ratio is None:
        return "成交活跃度不可用"
    if ratio >= 2.0:
        return "成交显著放大"
    if ratio >= 1.3:
        return "成交明显放大"
    if ratio <= 0.7:
        return "成交明显萎缩"
    return "成交与近期均值接近"


def describe_close_position(pos: Optional[float]) -> str:
    if pos is None:
        return "收盘位置不可用"
    if pos >= 0.8:
        return "收于当日高位"
    if pos <= 0.2:
        return "收于当日低位"
    return "收于当日中段"
