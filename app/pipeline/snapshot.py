"""定向行情快照：只拉个股、市场宽基、已判定的那一个行业。"""

from __future__ import annotations

import asyncio
from typing import Any, List, Optional, Tuple

from pydantic import BaseModel, Field

from app.config import Settings
from app.contracts import DataGap, ResearchWindow
from app.engine.patterns import WindowProfile, build_window_profile
from app.engine.price_profile import DailyProfile, build_daily_profile
from app.errors import ResearchError
from app.pipeline.subject import ResolvedSubject
from app.providers.base import Bar
from app.providers.registry import ProviderBundle
from app.trace import RunRecorder


class MarketSnapshot(BaseModel):
    window_bars: List[Bar]
    history_bars: List[Bar]
    stock_bar_count: int
    stock_cum: Optional[float] = None
    daily_profile: Optional[DailyProfile] = None
    window_profile: Optional[WindowProfile] = None
    market_cum: Optional[float] = None
    market_selected: List[Bar] = Field(default_factory=list)
    industry_cum: Optional[float] = None
    industry_selected: List[Bar] = Field(default_factory=list)
    clue_reasons: List[Any] = Field(default_factory=list)
    gaps: List[DataGap] = Field(default_factory=list)


async def fetch_snapshot(
    subject: ResolvedSubject,
    providers: ProviderBundle,
    settings: Settings,
    recorder: RunRecorder,
    window: ResearchWindow,
) -> MarketSnapshot:
    """个股 / 市场 / 单一行业日 K 与异动线索并行拉取。"""
    stock = subject.stock
    industry = subject.industry
    resolution = subject.resolution
    gaps: List[DataGap] = []

    recorder.step("quote", "running")
    recorder.step("market", "running")
    recorder.step("industry", "running")

    jobs: List[Any] = [
        _fetch_bars(
            providers.market.daily_bars,
            stock.thscode,
            resolution.lookback_start,
            resolution.info.actual_end,
            recorder,
            "daily_bars",
            f"{stock.name} 日 K",
        ),
        _fetch_index_bars(
            providers,
            recorder,
            settings.market_index_code,
            settings.market_index_name,
            resolution.window_days,
            resolution.lookback_start,
            "market",
        ),
        _fetch_clue_reasons(providers, recorder, stock.thscode),
    ]
    if industry.index_code:
        jobs.append(
            _fetch_index_bars(
                providers,
                recorder,
                industry.index_code,
                industry.index_name or industry.index_code,
                resolution.window_days,
                resolution.lookback_start,
                "industry",
            )
        )

    fetched = await asyncio.gather(*jobs)
    stock_bars, stock_gap = fetched[0]
    market_cum, market_gap, market_selected = fetched[1]
    clue_reasons = fetched[2]

    if stock_gap:
        gaps.append(stock_gap)
    if not stock_bars:
        recorder.step("quote", "failed")
        raise ResearchError(
            "no_price_data",
            f"无法获取 {stock.name} 的行情数据，研究无法继续。",
            stock_gap.reason if stock_gap else "",
        )

    window_days = set(resolution.window_days)
    window_bars = [b for b in stock_bars if b.date in window_days]
    history_bars = [b for b in stock_bars if b.date < resolution.info.actual_start]
    if resolution.info.is_intraday and window_bars:
        window_bars[-1].is_intraday = True
    recorder.step("quote", "done", f"取得 {len(stock_bars)} 根日 K")

    stock_cum, daily_profile, window_profile = _build_price_profiles(
        window_bars, history_bars, window, settings
    )
    gaps.extend(window_profile.gaps if window_profile else [])
    gaps.extend(daily_profile.gaps if daily_profile else [])

    if market_gap:
        gaps.append(market_gap)
    recorder.step("market", "done" if market_cum is not None else "failed")

    industry_cum: Optional[float] = None
    industry_selected: List[Bar] = []
    if industry.index_code:
        industry_cum, industry_gap, industry_selected = fetched[3]
        if industry_gap:
            gaps.append(industry_gap)
    else:
        gaps.append(
            DataGap(
                field="industry_index",
                reason=industry.note or "未能识别所属行业指数",
                impact="无法进行行业层面的横截面比较，行业类驱动因素的验证会受限",
            )
        )
    recorder.step(
        "industry",
        "done" if industry_cum is not None else "failed",
        industry.note or industry.method_label,
    )

    return MarketSnapshot(
        window_bars=window_bars,
        history_bars=history_bars,
        stock_bar_count=len(stock_bars),
        stock_cum=stock_cum,
        daily_profile=daily_profile,
        window_profile=window_profile,
        market_cum=market_cum,
        market_selected=market_selected,
        industry_cum=industry_cum,
        industry_selected=industry_selected,
        clue_reasons=clue_reasons,
        gaps=gaps,
    )


async def _fetch_bars(
    fetch, code: str, start: str, end: str, recorder: RunRecorder, tool: str, label: str
) -> Tuple[List[Bar], Optional[DataGap]]:
    res = await fetch(code, start, end)
    recorder.record_tool(
        tool,
        {"thscode": code, "start": start, "end": end},
        res.status,
        res.provider,
        note=res.note,
    )
    if not res.ok or not res.value:
        return [], DataGap(
            field=tool,
            reason=f"{label}取数失败：{res.note}",
            impact="相关层级的比较与形态判断无法完成",
            source=res.source,
        )
    return res.value, None


async def _fetch_index_bars(
    providers: ProviderBundle,
    recorder: RunRecorder,
    code: str,
    name: str,
    window_days: List[str],
    lookback_start: str,
    tool_suffix: str,
) -> Tuple[Optional[float], Optional[DataGap], List[Bar]]:
    bars, gap = await _fetch_bars(
        providers.market.index_daily_bars,
        code,
        lookback_start,
        window_days[-1],
        recorder,
        f"index_daily_bars_{tool_suffix}",
        f"{name} 指数日 K",
    )
    if gap:
        return None, gap, []
    selected = [b for b in bars if b.date in set(window_days)]
    cum = _cumulative(selected)
    if cum is None:
        return (
            None,
            DataGap(
                field=f"index_{tool_suffix}",
                reason=f"{name} 在研究窗口内没有完整的日 K 数据",
                impact="无法计算该层级的区间涨跌",
            ),
            [],
        )
    return cum, None, selected


async def _fetch_clue_reasons(
    providers: ProviderBundle, recorder: RunRecorder, thscode: str
) -> List[Any]:
    res = await providers.market.anomaly_reasons([thscode])
    recorder.record_tool(
        "anomaly_reasons",
        {"thscodes": thscode},
        res.status,
        res.provider,
        note=res.note,
    )
    if not res.ok or not res.value:
        return []
    return res.value


def _build_price_profiles(window_bars, history_bars, window, settings):
    daily_profile = None
    if window == ResearchWindow.TODAY and window_bars:
        daily_profile = build_daily_profile(
            window_bars[-1], history_bars, settings.turnover_baseline_days
        )
        cum = daily_profile.pct_change
    else:
        cum = _cumulative(window_bars)
    window_profile = build_window_profile(
        window_bars,
        concentration_threshold=settings.single_day_concentration_threshold,
        consistency_threshold=settings.direction_consistency_threshold,
        path_efficiency_threshold=settings.path_efficiency_threshold,
        reversal_min_segment_pct=settings.reversal_min_segment_pct,
    )
    if cum is None:
        cum = window_profile.cumulative_pct
    return cum, daily_profile, window_profile


def _cumulative(bars: List[Bar]) -> Optional[float]:
    usable = [b for b in bars if b.close is not None and b.prev_close not in (None, 0)]
    if not usable:
        return None
    return usable[-1].close / usable[0].prev_close - 1
