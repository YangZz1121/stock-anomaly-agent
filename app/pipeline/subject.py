"""主体解析：问句 → 标的 + 行业 + 研究窗口。

行业尽量在任何行情请求之前用预置清单判定。标的检索和交易日历并行。
清单没命中时，才用模型从同一份 90 项清单里选一次并定向核验成分股。
"""

from __future__ import annotations

import asyncio
from typing import List, Optional

from pydantic import BaseModel

from app.config import Settings
from app.engine.industry import IndustryResolver, guess_industry, load_catalog
from app.engine.resolver import (
    ParsedQuery,
    WindowResolution,
    parse_query,
    resolve_window,
)
from app.errors import NeedsWindowChoice, ResearchError
from app.providers.registry import ProviderBundle
from app.schemas import IndustryInfo, StockRef
from app.trace import RunRecorder


class ResolvedSubject(BaseModel):
    parsed: ParsedQuery
    stock: StockRef
    industry: IndustryInfo
    resolution: WindowResolution
    trading_days: List[str]


async def resolve_subject(
    query: str,
    window,
    providers: ProviderBundle,
    settings: Settings,
    recorder: RunRecorder,
    search_key: Optional[str] = None,
) -> ResolvedSubject:
    recorder.step("resolve", "running")
    parsed = parse_query(query)
    chosen_window = window or parsed.window
    key = search_key or parsed.search_key
    if not key:
        recorder.step("resolve", "failed")
        raise ResearchError(
            "no_stock",
            "没有识别出股票名称或代码。",
            "请输入 A 股公司名称或 6 位代码，例如「宁德时代今天为什么跌了这么多」。",
        )
    if chosen_window is None:
        recorder.step("resolve", "failed")
        raise NeedsWindowChoice(
            "need_window",
            "识别到股票，但没有指定研究窗口。",
            "请先确认研究窗口：今日 / 最近 3 个交易日 / 最近 5 个交易日。确认后先为您做行情快照。",
        )

    catalog = load_catalog(settings)
    name_for_guess = key if not any(ch.isdigit() for ch in key) else parsed.name_hint
    hinted = guess_industry(
        catalog,
        thscode=parsed.code if key == parsed.search_key else None,
        name=name_for_guess,
        query=query,
    )

    stock, trading_days = await asyncio.gather(
        resolve_stock(key, providers, recorder),
        fetch_trading_days(providers, recorder),
    )

    resolver = IndustryResolver(settings, providers.market, providers.llm)
    if hinted is not None:
        industry = resolver.pin(stock.thscode, hinted)
    else:
        industry = await resolver.resolve(
            stock.thscode, stock.name, recorder, query=query
        )

    resolution = resolve_window(
        chosen_window,
        trading_days,
        baseline_days=settings.turnover_baseline_days,
    )
    if resolution is None:
        recorder.step("resolve", "failed")
        raise ResearchError(
            "no_calendar",
            "交易日历数据不足，无法确定研究窗口。",
            "该窗口需要的交易日数量超出了当前可用的日历范围。",
        )

    industry_bit = industry.index_name or "行业未识别"
    recorder.step(
        "resolve",
        "done",
        f"{stock.name} {stock.thscode} · {industry_bit}，"
        f"窗口 {resolution.info.actual_start} ~ {resolution.info.actual_end}",
    )
    return ResolvedSubject(
        parsed=parsed,
        stock=stock,
        industry=industry,
        resolution=resolution,
        trading_days=trading_days,
    )


async def resolve_stock(
    key: str, providers: ProviderBundle, recorder: RunRecorder
) -> StockRef:
    res = await providers.market.search_ticker(key)
    recorder.record_tool(
        "search_ticker", {"q": key}, res.status, res.provider, note=res.note
    )
    if not res.ok or not res.value:
        raise ResearchError(
            "stock_not_found",
            f"未能识别标的：{key}",
            res.note or "请确认输入的是 A 股公司名称或代码。",
        )
    top = res.value[0]
    return StockRef(
        name=top.name, thscode=top.thscode, ticker=top.ticker, exchange=top.exchange
    )


async def fetch_trading_days(
    providers: ProviderBundle, recorder: RunRecorder
) -> List[str]:
    res = await providers.market.trading_days()
    recorder.record_tool(
        "trading_days", {}, res.status, res.provider, note=res.note
    )
    if not res.ok or not res.value:
        raise ResearchError(
            "no_calendar",
            "交易日历不可用，无法确定研究日期。",
            res.note or "",
        )
    return res.value
