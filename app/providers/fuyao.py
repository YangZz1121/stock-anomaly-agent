"""扶摇（同花顺金融数据）REST 客户端。

约定：
* 所有业务错误都走 HTTP 200 + 信封 ``code``，所以必须同时检查两层。
* 日 K 不返回前收盘价，由调用方按上一根 K 线的收盘价推导，因此这里
  会多取一段回溯区间。
* 任何异常都被翻译成 ``Fetched.failure``，不向上抛。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional

import httpx

from app.config import Settings
from app.contracts import FetchStatus, Fetched
from app.providers.base import AnomalyReason, Bar, IndexInfo, RawEvent, TickerInfo
from app.timeutil import from_compact, from_ms, to_ms

# 业务错误码 -> 人类可读说明
CODE_MESSAGES: Dict[int, str] = {
    1001: "缺少必填参数",
    1002: "参数格式错误",
    1003: "参数取值越界",
    1004: "参数冲突",
    2001: "API Key 缺失或无效",
    2003: "API Key 无权调用该能力",
    3001: "标的不存在",
    3002: "数据未就绪",
    3004: "标的类型不支持该能力",
    4001: "触发频率限制",
    5001: "服务内部错误",
    5002: "上游服务超时",
    5003: "数据源不可用",
}

# 这些业务码代表"确实没有数据"，而非调用失败
MISSING_CODES = {3001, 3002, 3004}


class FuyaoProvider:
    name = "fuyao"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client = httpx.AsyncClient(
            base_url=settings.fuyao_base_url,
            timeout=settings.http_timeout_s,
            headers={"X-api-key": settings.fuyao_api_key or ""},
        )
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # 底层调用
    # ------------------------------------------------------------------

    async def _get(
        self, path: str, params: Optional[Dict[str, Any]] = None
    ) -> Fetched[Dict[str, Any]]:
        source = f"fuyao:{path}"
        if not self._settings.fuyao_api_key:
            return Fetched.failure(
                source, self.name, "未配置 FUYAO_API_KEY", FetchStatus.FAILED
            )

        await self._throttle()
        try:
            resp = await self._client.get(path, params=params or {})
        except httpx.TimeoutException:
            return Fetched.failure(source, self.name, "请求超时")
        except httpx.HTTPError as exc:
            return Fetched.failure(source, self.name, f"网络错误：{exc}")

        if resp.status_code == 429:
            return Fetched.failure(source, self.name, "触发频率限制（HTTP 429）")
        if resp.status_code >= 400:
            return Fetched.failure(source, self.name, f"HTTP {resp.status_code}")

        try:
            body = resp.json()
        except ValueError:
            return Fetched.failure(source, self.name, "响应不是合法 JSON")

        code = body.get("code")
        if code != 0:
            note = CODE_MESSAGES.get(code, f"业务错误 code={code}")
            status = FetchStatus.MISSING if code in MISSING_CODES else FetchStatus.FAILED
            return Fetched.failure(source, self.name, note, status)

        data = body.get("data") or {}
        return Fetched.success(
            data, source, self.name, as_of=data.get("timestamp")
        )

    async def _throttle(self, min_interval: float = 0.12) -> None:
        """简单节流，避免短时间集中调用触发限流。"""
        async with self._lock:
            delta = time.monotonic() - self._last_call
            if delta < min_interval:
                await asyncio.sleep(min_interval - delta)
            self._last_call = time.monotonic()

    # ------------------------------------------------------------------
    # 能力实现
    # ------------------------------------------------------------------

    async def search_ticker(self, query: str) -> Fetched[List[TickerInfo]]:
        res = await self._get(
            "/api/meta/tickers/search",
            {"q": query, "asset_type": "a-share", "limit": 10},
        )
        if not res.ok:
            return Fetched(
                status=res.status, source=res.source, provider=self.name, note=res.note
            )
        items = (res.value or {}).get("item") or []
        out = [
            TickerInfo(
                thscode=it.get("thscode", ""),
                name=it.get("name", ""),
                ticker=it.get("ticker"),
                exchange=it.get("exchange"),
                asset_type=it.get("asset_type"),
            )
            for it in items
            if it.get("thscode")
        ]
        if not out:
            return Fetched.failure(
                res.source, self.name, f"未检索到标的：{query}", FetchStatus.MISSING
            )
        return Fetched.success(out, res.source, self.name, as_of=res.as_of)

    async def trading_days(self) -> Fetched[List[str]]:
        res = await self._get("/api/a-share/calendar/trading-days")
        if not res.ok:
            return Fetched(
                status=res.status, source=res.source, provider=self.name, note=res.note
            )
        items = (res.value or {}).get("item") or []
        days: List[str] = []
        for it in items:
            raw = it.get("date")
            if raw:
                try:
                    days.append(from_compact(str(raw)))
                    continue
                except ValueError:
                    pass
            if it.get("date_ms"):
                days.append(from_ms(int(it["date_ms"])))
        days = sorted(set(days))
        if not days:
            return Fetched.failure(
                res.source, self.name, "交易日历为空", FetchStatus.MISSING
            )
        return Fetched.success(
            days, res.source, self.name, as_of=res.as_of, note="近一年 A 股交易日序列"
        )

    async def daily_bars(self, thscode: str, start: str, end: str) -> Fetched[List[Bar]]:
        return await self._bars(
            "/api/a-share/prices/historical",
            {
                "thscode": thscode,
                "interval": "1d",
                "start": to_ms(start),
                "end": to_ms(end),
                "adjust": "forward",
            },
        )

    async def index_daily_bars(
        self, thscode: str, start: str, end: str
    ) -> Fetched[List[Bar]]:
        return await self._bars(
            "/api/a-share-index/prices/historical",
            {
                "thscode": thscode,
                "interval": "1d",
                "start": to_ms(start),
                "end": to_ms(end),
            },
        )

    async def _bars(self, path: str, params: Dict[str, Any]) -> Fetched[List[Bar]]:
        res = await self._get(path, params)
        if not res.ok:
            return Fetched(
                status=res.status, source=res.source, provider=self.name, note=res.note
            )
        items = (res.value or {}).get("item") or []
        bars: List[Bar] = []
        for it in items:
            if not it.get("date_ms"):
                continue
            bars.append(
                Bar(
                    date=from_ms(int(it["date_ms"])),
                    open=_num(it.get("open_price")),
                    high=_num(it.get("high_price")),
                    low=_num(it.get("low_price")),
                    close=_num(it.get("close_price")),
                    volume=_num(it.get("volume")),
                    amount=_num(it.get("turnover")),
                )
            )
        bars.sort(key=lambda b: b.date)
        # 接口不返回前收盘价，按上一根 K 线收盘价补齐
        for i in range(1, len(bars)):
            bars[i].prev_close = bars[i - 1].close
        if not bars:
            return Fetched.failure(
                res.source, self.name, "区间内无 K 线数据", FetchStatus.MISSING
            )
        return Fetched.success(
            bars,
            res.source,
            self.name,
            as_of=res.as_of,
            unit="元",
            note="前复权日线；前收盘价由上一交易日收盘价推导",
        )

    async def snapshot(self, thscodes: List[str]) -> Fetched[List[Dict[str, Any]]]:
        res = await self._get(
            "/api/a-share/prices/snapshot", {"thscodes": ",".join(thscodes)}
        )
        if not res.ok:
            return Fetched(
                status=res.status, source=res.source, provider=self.name, note=res.note
            )
        items = (res.value or {}).get("item") or []
        if not items:
            return Fetched.failure(
                res.source, self.name, "快照无数据", FetchStatus.MISSING
            )
        return Fetched.success(items, res.source, self.name, as_of=res.as_of)

    async def industry_indexes(self) -> Fetched[List[IndexInfo]]:
        res = await self._get(
            "/api/a-share-index/catalog/ths-index-list", {"tag": "industry"}
        )
        if not res.ok:
            return Fetched(
                status=res.status, source=res.source, provider=self.name, note=res.note
            )
        items = (res.value or {}).get("item") or []
        out = [
            IndexInfo(thscode=it["thscode"], name=it.get("name", ""), tag="industry")
            for it in items
            if it.get("thscode")
        ]
        if not out:
            return Fetched.failure(
                res.source, self.name, "行业指数清单为空", FetchStatus.MISSING
            )
        return Fetched.success(out, res.source, self.name, as_of=res.as_of)

    async def index_constituents(self, thscode: str) -> Fetched[List[TickerInfo]]:
        res = await self._get(
            "/api/a-share-index/constituents/ths-stock-list", {"thscode": thscode}
        )
        if not res.ok:
            return Fetched(
                status=res.status, source=res.source, provider=self.name, note=res.note
            )
        items = (res.value or {}).get("item") or []
        out = [
            TickerInfo(
                thscode=it["thscode"], name=it.get("name", ""), ticker=it.get("ticker")
            )
            for it in items
            if it.get("thscode")
        ]
        if not out:
            return Fetched.failure(
                res.source, self.name, "成分股为空", FetchStatus.MISSING
            )
        return Fetched.success(out, res.source, self.name, as_of=res.as_of)

    async def anomaly_reasons(
        self, thscodes: List[str]
    ) -> Fetched[List[AnomalyReason]]:
        res = await self._get(
            "/api/a-share/special-data/anomaly-analysis-stock",
            {"thscodes": ",".join(thscodes)},
        )
        if not res.ok:
            return Fetched(
                status=res.status, source=res.source, provider=self.name, note=res.note
            )
        items = (res.value or {}).get("item") or []
        out = [
            AnomalyReason(
                thscode=it.get("thscode", ""),
                stock_name=it.get("stock_name", ""),
                content=it.get("analysis_content", ""),
                tag_name=it.get("tag_name"),
                keywords=it.get("keyword_list") or [],
            )
            for it in items
            if it.get("thscode")
        ]
        if not out:
            return Fetched.failure(
                res.source, self.name, "当日无异动解读记录", FetchStatus.MISSING
            )
        return Fetched.success(
            out,
            res.source,
            self.name,
            as_of=res.as_of,
            note="第三方当日异动解读，仅作检索线索，不作为结论依据",
        )


class FuyaoEvidenceProvider:
    """扶摇资讯事件库客户端。

    注意：``/api/news/events/search`` 在官方文档中明确标注「该能力暂未开放
    外部接入」。这里保留完整实现，是为了密钥方开通权限后可以零改动启用；
    在未开通的情况下调用会返回 ``code=2003``，被统一翻译成显式失败，
    由上层降级到其他证据源，而不是静默返回空结果。
    """

    name = "fuyao-news"

    def __init__(self, settings: Settings) -> None:
        self._inner = FuyaoProvider(settings)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def search_events(
        self,
        query: str,
        start_date: str,
        end_date: str,
        scope: str = "company",
        limit: int = 20,
        context: Optional[Dict[str, Any]] = None,
    ) -> Fetched[List[RawEvent]]:
        res = await self._inner._get(
            "/api/news/events/search",
            {
                "query": query,
                "search_param": "event_name",
                "standard_start_time": start_date,
                "standard_end_time": end_date,
                "size": min(max(limit, 1), 100),
            },
        )
        if not res.ok:
            return Fetched(
                status=res.status, source=res.source, provider=self.name, note=res.note
            )
        items = (res.value or {}).get("items") or []
        out: List[RawEvent] = []
        for it in items:
            grading = it.get("event_grading") or {}
            out.append(
                RawEvent(
                    event_id=it.get("event_id", ""),
                    title=it.get("event_name", ""),
                    summary=it.get("event_summary", ""),
                    published_at=it.get("standard_time"),
                    source_name="同花顺资讯事件库",
                    source_tier_hint="t2_professional",
                    origin_key=it.get("event_id"),
                    cluster_hint=it.get("event_id"),
                    scope=scope,
                    raw={"event_grading_score": grading.get("score")},
                )
            )
        return Fetched.success(out, res.source, self.name, as_of=res.as_of)


def _num(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
