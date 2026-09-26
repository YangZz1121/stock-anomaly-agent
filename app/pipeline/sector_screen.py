"""板块扫描：成分股轻量行情 + 异动闸门，不跑个股归因。"""

from __future__ import annotations

import asyncio
from typing import List, Optional, Tuple

from app.config import Settings
from app.engine.anomaly import evaluate_anomaly
from app.engine.industry import CatalogEntry, load_catalog
from app.engine.resolver import resolve_window
from app.engine.sector import resolve_sector_phrase
from app.errors import NeedsWindowChoice, ResearchError
from app.pipeline.snapshot import _cumulative
from app.pipeline.subject import fetch_trading_days
from app.providers.base import Bar, TickerInfo
from app.providers.registry import ProviderBundle
from app.schemas import SectorMover, SectorScreenResult, StockRef
from app.trace import RunRecorder

DISCLAIMER = "板块扫描只做异动检测，不展开归因。点选其中一只公司可继续做个股分析。"


async def run_sector_screen(
    query: str,
    window,
    providers: ProviderBundle,
    settings: Settings,
    recorder: RunRecorder,
    *,
    sector_label: Optional[str] = None,
    sector_names: Optional[List[str]] = None,
) -> SectorScreenResult:
    recorder.step("resolve", "running")
    chosen = window
    if chosen is None:
        recorder.step("resolve", "failed")
        raise NeedsWindowChoice(
            "need_window",
            "识别到板块，但没有指定研究窗口。",
            "请先确认研究窗口：今日 / 最近 3 个交易日 / 最近 5 个交易日。确认后扫描该板块成分股的异动。",
        )

    sector = resolve_sector_phrase(query, settings)
    label = sector_label or (sector.label if sector else "")
    names = list(sector_names or (sector.names if sector else []))
    catalog = load_catalog(settings)
    entries = [
        catalog.by_name[name]
        for name in names
        if name in catalog.by_name
    ]
    if not entries and sector is not None:
        entries = list(sector.entries)
        label = label or sector.label
        names = sector.names
    if not entries:
        recorder.step("resolve", "failed")
        raise ResearchError(
            "need_stock",
            f"未能识别板块「{label or query}」。",
            "请说明板块或行业，例如「科技股」「白酒」「半导体」。",
        )

    days = await fetch_trading_days(providers, recorder)
    resolution = resolve_window(
        chosen, days, baseline_days=settings.turnover_baseline_days
    )
    if resolution is None:
        recorder.step("resolve", "failed")
        raise ResearchError(
            "no_calendar",
            "交易日历不足以覆盖所选研究窗口。",
            "请换一个窗口，或稍后再试。",
        )
    recorder.step("resolve", "done", f"{label} · {resolution.info.label}")

    recorder.step("constituents", "running")
    tickers, capped = await _collect_constituents(
        entries, providers, settings, recorder
    )
    recorder.step(
        "constituents",
        "done",
        f"{len(tickers)} 只成分股" + ("（已截断）" if capped else ""),
    )

    recorder.step("scan", "running")
    market_cum = await _market_cum(providers, settings, resolution)
    movers = await _scan_tickers(
        tickers, resolution, market_cum, providers, settings
    )
    recorder.step("scan", "done", f"有效行情 {len(movers)} 只")

    recorder.step("rank", "running")
    ranked = _rank_movers(movers, settings.sector_screen_top_k)
    note = _screen_note(label, movers, ranked, capped)
    recorder.step("rank", "done", f"列出 {len(ranked)} 只")

    return SectorScreenResult(
        run_id=recorder.run_id,
        query=query,
        sector_label=label,
        industries=names or [entry.name for entry in entries],
        window=resolution.info,
        movers=ranked,
        scanned_count=len(movers),
        capped=capped,
        note=note,
        disclaimers=[DISCLAIMER],
    )


async def _collect_constituents(
    entries: List[CatalogEntry],
    providers: ProviderBundle,
    settings: Settings,
    recorder: RunRecorder,
) -> Tuple[List[Tuple[TickerInfo, str]], bool]:
    seen = set()
    out: List[Tuple[TickerInfo, str]] = []
    limit = settings.sector_screen_max_constituents
    for entry in entries:
        res = await providers.market.index_constituents(entry.thscode)
        recorder.record_tool(
            "index_constituents",
            {"thscode": entry.thscode, "name": entry.name},
            res.status,
            res.provider,
            note=res.note,
        )
        if not res.ok or not res.value:
            continue
        for ticker in res.value:
            if not ticker.thscode or ticker.thscode in seen:
                continue
            seen.add(ticker.thscode)
            out.append((ticker, entry.name))
            if len(out) >= limit:
                return out, True
    return out, False


async def _market_cum(providers, settings, resolution) -> Optional[float]:
    res = await providers.market.index_daily_bars(
        settings.market_index_code,
        resolution.lookback_start,
        resolution.info.actual_end,
    )
    if not res.ok or not res.value:
        return None
    selected = [b for b in res.value if b.date in set(resolution.window_days)]
    return _cumulative(selected)


async def _scan_tickers(
    tickers: List[Tuple[TickerInfo, str]],
    resolution,
    market_cum: Optional[float],
    providers: ProviderBundle,
    settings: Settings,
) -> List[SectorMover]:
    sem = asyncio.Semaphore(max(1, settings.sector_screen_concurrency))

    async def _one(ticker: TickerInfo, industry: str) -> Optional[SectorMover]:
        async with sem:
            res = await providers.market.daily_bars(
                ticker.thscode,
                resolution.lookback_start,
                resolution.info.actual_end,
            )
        if not res.ok or not res.value:
            return None
        return _mover_from_bars(
            ticker, industry, res.value, resolution, market_cum, settings
        )

    rows = await asyncio.gather(*[_one(t, name) for t, name in tickers])
    return [row for row in rows if row is not None]


def _mover_from_bars(
    ticker: TickerInfo,
    industry: str,
    bars: List[Bar],
    resolution,
    market_cum: Optional[float],
    settings: Settings,
) -> Optional[SectorMover]:
    days = set(resolution.window_days)
    selected = [b for b in bars if b.date in days]
    history = [b for b in bars if b.date < resolution.window_days[0]]
    if not selected:
        return None
    cum = _cumulative(selected)
    gate = evaluate_anomaly(
        thscode=ticker.thscode,
        name=ticker.name,
        window_bars=selected,
        history_bars=history,
        stock_pct=cum,
        market_pct=market_cum,
        industry_vs_market=None,
        stock_vs_industry=None,
        stock_abs_threshold=settings.stock_abs_threshold_pct,
        market_move_threshold=settings.market_move_threshold_pct,
        industry_move_threshold=settings.industry_move_threshold_pct,
        divergence_threshold=settings.divergence_threshold_pct,
        z_threshold=settings.anomaly_z_threshold,
    )
    return SectorMover(
        stock=StockRef(
            name=ticker.name,
            thscode=ticker.thscode,
            ticker=ticker.ticker,
            exchange=ticker.exchange,
        ),
        industry_name=industry,
        window_pct=cum,
        is_anomaly=gate.is_anomaly,
        z_score=gate.z_score,
        reasons=gate.reasons,
        limit_state=gate.limit_state,
    )


def _rank_movers(movers: List[SectorMover], top_k: int) -> List[SectorMover]:
    anomalies = [m for m in movers if m.is_anomaly]
    rest = [m for m in movers if not m.is_anomaly]
    anomalies.sort(key=_mover_sort_key, reverse=True)
    if anomalies:
        return anomalies[: max(1, top_k)]
    rest.sort(key=_mover_sort_key, reverse=True)
    return rest[: min(5, max(1, top_k))]


def _mover_sort_key(item: SectorMover) -> Tuple[float, float]:
    z = abs(item.z_score) if item.z_score is not None else 0.0
    pct = abs(item.window_pct) if item.window_pct is not None else 0.0
    return (z, pct)


def _screen_note(
    label: str,
    scanned: List[SectorMover],
    ranked: List[SectorMover],
    capped: bool,
) -> str:
    hits = sum(1 for item in scanned if item.is_anomaly)
    cap = "扫描数量已截断到上限。" if capped else ""
    if not scanned:
        return f"「{label}」没有取到可扫描的成分股行情。{cap}".strip()
    if hits:
        shown = sum(1 for item in ranked if item.is_anomaly)
        extra = f"下面列出前 {shown} 只。" if shown and shown < hits else ""
        return f"「{label}」扫描 {len(scanned)} 只成分股，其中 {hits} 只达到异动门槛。{extra}{cap}".strip()
    return (
        f"「{label}」扫描 {len(scanned)} 只成分股，未见达到异动门槛的标的；"
        f"下列为窗口涨跌相对更大的接近样本。{cap}"
    ).strip()
