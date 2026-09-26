"""研究入口：完整报告走 Agent 环，快照仍走确定性短路径。"""

from __future__ import annotations

import asyncio
from typing import Optional

from pydantic import BaseModel

from app.agent.assemble import build_what_happened
from app.agent.loop import run_research_agent
from app.config import Settings
from app.contracts import ImpactDirection, ResearchPriority, ResearchWindow
from app.engine import guardrails
from app.engine.priority import build_comparison
from app.engine.resolver import parse_query, resolve_window
from app.errors import NeedsWindowChoice, ResearchError
from app.pipeline.snapshot import _build_price_profiles, _fetch_bars
from app.pipeline.subject import fetch_trading_days, resolve_stock
from app.providers.registry import ProviderBundle
from app.schemas import (
    IndustryInfo,
    OpenQuestionsSection,
    OverallVerdict,
    ResearchBrief,
    ResearchTrace,
    RunMetrics,
    SubjectSection,
    WhatItMeansSection,
    WhyHappenedSection,
)
from app.timeutil import now_iso
from app.trace import RunRecorder

__all__ = [
    "NeedsWindowChoice",
    "ResearchError",
    "ResearchRequest",
    "run_research",
    "run_snapshot",
]


class ResearchRequest(BaseModel):
    query: str
    window: Optional[ResearchWindow] = None
    search_key: Optional[str] = None


async def run_research(
    request: ResearchRequest,
    providers: ProviderBundle,
    settings: Settings,
    recorder: RunRecorder,
) -> ResearchBrief:
    return await run_research_agent(request, providers, settings, recorder)


SNAPSHOT_FOLLOWUP = (
    "以上为行情事实快照。如需完整异动分析——含驱动因素验证与基本面含义——请直接说明。"
)


async def run_snapshot(
    request: ResearchRequest,
    providers: ProviderBundle,
    settings: Settings,
    recorder: RunRecorder,
) -> ResearchBrief:
    """简单 query：只识别标的、取日历和日 K，不检索资讯、不调用模型。"""
    recorder.step("resolve", "running")
    parsed = parse_query(request.query)
    key = request.search_key or parsed.search_key
    window = request.window or parsed.window
    if not key:
        recorder.step("resolve", "failed")
        raise ResearchError(
            "no_stock",
            "没有识别出股票名称或代码。",
            "请输入 A 股公司名称或 6 位代码。",
        )
    if window is None:
        recorder.step("resolve", "failed")
        raise NeedsWindowChoice(
            "need_window",
            "识别到股票，但没有指定研究窗口。",
            "请先确认研究窗口：今日 / 最近 3 个交易日 / 最近 5 个交易日。确认后先为您做行情快照。",
        )

    stock, trading_days = await asyncio.gather(
        resolve_stock(key, providers, recorder),
        fetch_trading_days(providers, recorder),
    )
    resolution = resolve_window(
        window, trading_days, baseline_days=settings.turnover_baseline_days
    )
    if resolution is None:
        recorder.step("resolve", "failed")
        raise ResearchError(
            "no_calendar",
            "交易日历数据不足，无法确定研究窗口。",
            "该窗口需要的交易日数量超出了当前可用的日历范围。",
        )
    recorder.step(
        "resolve",
        "done",
        f"{stock.name} {stock.thscode}，"
        f"窗口 {resolution.info.actual_start} ~ {resolution.info.actual_end}",
    )

    recorder.step("quote", "running")
    stock_bars, stock_gap = await _fetch_bars(
        providers.market.daily_bars,
        stock.thscode,
        resolution.lookback_start,
        resolution.info.actual_end,
        recorder,
        "daily_bars",
        f"{stock.name} 日 K",
    )
    if not stock_bars:
        recorder.step("quote", "failed")
        raise ResearchError(
            "no_price_data",
            f"无法获取 {stock.name} 的行情数据，研究无法继续。",
            stock_gap.reason if stock_gap else "",
        )
    window_days = set(resolution.window_days)
    window_bars = [bar for bar in stock_bars if bar.date in window_days]
    history_bars = [bar for bar in stock_bars if bar.date < resolution.info.actual_start]
    if resolution.info.is_intraday and window_bars:
        window_bars[-1].is_intraday = True
    recorder.step("quote", "done", f"取得 {len(stock_bars)} 根日 K")

    recorder.step("profile", "running")
    stock_cum, daily_profile, window_profile = _build_price_profiles(
        window_bars, history_bars, window, settings
    )
    gaps = []
    if stock_gap:
        gaps.append(stock_gap)
    if window_profile:
        gaps.extend(window_profile.gaps)
    if daily_profile:
        gaps.extend(daily_profile.gaps)

    industry = IndustryInfo(
        method="unavailable",
        method_label="快照未查询行业",
        note="行情快照不检索行业与资讯。",
    )
    comparison = build_comparison(
        stock_pct=stock_cum,
        industry_pct=None,
        market_pct=None,
        window_label=resolution.info.label,
    )
    what_happened = build_what_happened(
        daily_profile,
        window_profile,
        comparison,
        industry,
        window_bars,
        stock,
        resolution.info.label,
        "",
        gaps,
    )
    recorder.step("profile", "done")

    brief = ResearchBrief(
        run_id=recorder.run_id,
        created_at=now_iso(),
        kind="snapshot",
        subject=SubjectSection(
            stock=stock,
            user_question=request.query,
            window=resolution.info,
            resolved_note=resolution.info.remap_note,
        ),
        what_happened=what_happened,
        why_happened=WhyHappenedSection(
            priority=ResearchPriority.COMPANY_FIRST,
            priority_label="行情快照",
            priority_reason="本次为快速查询，未展开异动归因。",
        ),
        what_it_means=WhatItMeansSection(
            overall=OverallVerdict(
                direction=ImpactDirection.UNCERTAIN,
                display="本次为行情快照，未形成基本面判断。",
                reason=SNAPSHOT_FOLLOWUP,
                can_summarize=False,
            ),
            note=SNAPSHOT_FOLLOWUP,
        ),
        open_questions=OpenQuestionsSection(questions=[], gaps=gaps),
        evidence=[],
        metrics=RunMetrics(
            time_to_verifiable_insight_ms=recorder.elapsed_ms(),
            tool_calls=len(recorder.tool_calls),
            failed_tool_calls=recorder.failed_tool_calls,
            degraded=providers.degraded,
        ),
        trace=ResearchTrace(
            tool_calls=recorder.tool_calls,
            llm_calls=recorder.llm_calls,
            providers=providers.labels,
        ),
        disclaimers=guardrails.DISCLAIMERS
        + guardrails.degraded_notice(providers.labels, "")
        + [SNAPSHOT_FOLLOWUP],
    )
    brief.what_happened.summary, _ = guardrails.sanitize(brief.what_happened.summary)
    return brief
