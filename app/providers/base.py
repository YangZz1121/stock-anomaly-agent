"""三类 Provider 的接口定义。

所有方法一律返回 ``Fetched``，不抛异常、不返回裸数据。实现方负责把
网络错误、限流、空数据都翻译成带 ``status`` 的包裹，让上层统一处理降级。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from app.contracts import Fetched


# --------------------------------------------------------------------------
# 数据模型
# --------------------------------------------------------------------------


class Bar(BaseModel):
    """一根日 K。"""

    date: str  # yyyy-MM-dd
    open: Optional[float] = None
    high: Optional[float] = None
    low: Optional[float] = None
    close: Optional[float] = None
    prev_close: Optional[float] = None
    volume: Optional[float] = None
    amount: Optional[float] = None
    is_intraday: bool = False  # 盘中快照拼接出来的"当日"数据

    @property
    def complete(self) -> bool:
        return None not in (self.open, self.high, self.low, self.close)


class TickerInfo(BaseModel):
    thscode: str
    name: str
    ticker: Optional[str] = None
    exchange: Optional[str] = None
    asset_type: Optional[str] = None


class IndexInfo(BaseModel):
    thscode: str
    name: str
    tag: Optional[str] = None


class RawEvent(BaseModel):
    """证据源返回的一条原始资讯 / 政策 / 公告。"""

    event_id: str
    title: str
    summary: str = ""
    published_at: Optional[str] = None  # yyyy-MM-dd 或含时分
    source_name: str = "未知来源"
    source_url: Optional[str] = None
    source_tier_hint: Optional[str] = None  # t1_authoritative / t2_professional / ...
    origin_key: Optional[str] = None  # 原始信源标识，用于独立来源判定
    cluster_hint: Optional[str] = None  # 同一事件的聚类提示
    scope: Optional[str] = None  # market | industry | company
    related_names: List[str] = Field(default_factory=list)
    raw: Dict[str, Any] = Field(default_factory=dict)


class AnomalyReason(BaseModel):
    """第三方当日异动解读。只作线索，不作结论。"""

    thscode: str
    stock_name: str
    content: str
    tag_name: Optional[str] = None
    keywords: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Protocols
# --------------------------------------------------------------------------


@runtime_checkable
class MarketDataProvider(Protocol):
    name: str

    async def search_ticker(self, query: str) -> Fetched[List[TickerInfo]]:
        """按名称或代码检索 A 股标的。"""

    async def trading_days(self) -> Fetched[List[str]]:
        """近一年 A 股交易日序列，yyyy-MM-dd 升序。"""

    async def daily_bars(
        self, thscode: str, start: str, end: str
    ) -> Fetched[List[Bar]]:
        """个股日 K，[start, end] 为 yyyy-MM-dd 闭区间。"""

    async def index_daily_bars(
        self, thscode: str, start: str, end: str
    ) -> Fetched[List[Bar]]:
        """指数日 K。"""

    async def snapshot(self, thscodes: List[str]) -> Fetched[List[Dict[str, Any]]]:
        """行情快照，用于盘中最新价。"""

    async def industry_indexes(self) -> Fetched[List[IndexInfo]]:
        """同花顺行业指数清单。"""

    async def index_constituents(self, thscode: str) -> Fetched[List[TickerInfo]]:
        """指数成分股。"""

    async def anomaly_reasons(self, thscodes: List[str]) -> Fetched[List[AnomalyReason]]:
        """当日个股异动原因（线索池）。"""

    async def aclose(self) -> None: ...


@runtime_checkable
class EvidenceProvider(Protocol):
    name: str

    async def search_events(
        self,
        query: str,
        start_date: str,
        end_date: str,
        scope: str = "company",
        limit: int = 20,
        context: Optional[Dict[str, Any]] = None,
    ) -> Fetched[List[RawEvent]]:
        """按关键词与时间窗检索资讯事件。

        ``scope`` 取 market / industry / company，用于让实现方选择不同的
        检索策略；``context`` 传入股票名、行业名等辅助信息。
        """

    async def aclose(self) -> None: ...


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str

    async def complete_json(
        self,
        purpose: str,
        system: str,
        user: str,
        schema_hint: Optional[Dict[str, Any]] = None,
    ) -> Fetched[Dict[str, Any]]:
        """要求模型返回 JSON 对象。解析失败视为调用失败，不做容错猜测。"""

    async def aclose(self) -> None: ...
