"""基于构造 fixture 的行情数据源。

用途有两个：没有扶摇密钥时让主链路仍然完整可跑；以及在测试里通过
``faults`` 注入接口失败，验证产品在数据缺失下不会静默编造结论。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Set

from app.config import Settings
from app.contracts import FetchStatus, Fetched
from app.providers.base import AnomalyReason, Bar, IndexInfo, TickerInfo

MOCK_NOTE = "构造演示数据，非真实行情"

_FIXTURE_FILES: Dict[str, Any] = {}


def _read_fixture(path: str) -> Optional[Dict[str, Any]]:
    if path in _FIXTURE_FILES:
        return _FIXTURE_FILES[path]
    if not os.path.exists(path):
        _FIXTURE_FILES[path] = None
        return None
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    _FIXTURE_FILES[path] = data
    return data


class MockMarketProvider:
    name = "mock"

    def __init__(
        self, settings: Settings, faults: Optional[Set[str]] = None
    ) -> None:
        self._dir = _resolve_fixtures_dir(settings.fixtures_dir)
        self._faults = faults or set()

    async def aclose(self) -> None:
        return None

    # ------------------------------------------------------------------

    def _load(self, rel: str) -> Optional[Dict[str, Any]]:
        return _read_fixture(os.path.join(self._dir, rel))

    def _fault(self, key: str, source: str) -> Optional[Fetched]:
        if key in self._faults:
            return Fetched.failure(
                source, self.name, f"注入故障：{key} 调用失败", FetchStatus.FAILED
            )
        return None

    def _src(self, name: str) -> str:
        return f"mock:fixtures/{name}"

    # ------------------------------------------------------------------

    async def search_ticker(self, query: str) -> Fetched[List[TickerInfo]]:
        source = self._src("market/tickers.json")
        fault = self._fault("search_ticker", source)
        if fault:
            return fault
        data = self._load("market/tickers.json") or {"item": []}
        q = (query or "").strip().upper()
        hits = [
            TickerInfo(**it)
            for it in data["item"]
            if q
            and (
                q in it["name"].upper()
                or q == it["thscode"].upper()
                or q == (it.get("ticker") or "").upper()
            )
        ]
        if not hits:
            return Fetched.failure(
                source,
                self.name,
                f"构造数据集中不存在标的：{query}",
                FetchStatus.MISSING,
            )
        return Fetched.success(hits, source, self.name, note=MOCK_NOTE)

    async def trading_days(self) -> Fetched[List[str]]:
        source = self._src("market/calendar.json")
        fault = self._fault("trading_days", source)
        if fault:
            return fault
        data = self._load("market/calendar.json")
        if not data:
            return Fetched.failure(
                source, self.name, "缺少构造交易日历", FetchStatus.MISSING
            )
        return Fetched.success(
            data["trading_days"], source, self.name, note=MOCK_NOTE
        )

    async def daily_bars(self, thscode: str, start: str, end: str) -> Fetched[List[Bar]]:
        return self._bars(thscode, start, end, "daily_bars")

    async def index_daily_bars(
        self, thscode: str, start: str, end: str
    ) -> Fetched[List[Bar]]:
        return self._bars(thscode, start, end, "index_daily_bars")

    def _bars(self, thscode: str, start: str, end: str, fault_key: str) -> Fetched:
        rel = f"market/bars/{thscode}.json"
        source = self._src(rel)
        fault = self._fault(fault_key, source)
        if fault:
            return fault
        if f"{fault_key}:{thscode}" in self._faults:
            return Fetched.failure(
                source, self.name, f"注入故障：{thscode} K 线不可用"
            )
        data = self._load(rel)
        if not data:
            return Fetched.failure(
                source, self.name, f"构造数据集中无 {thscode} 的 K 线", FetchStatus.MISSING
            )
        bars = [
            Bar(**b) for b in data["bars"] if start <= b["date"] <= end
        ]
        if not bars:
            return Fetched.failure(
                source, self.name, f"{start} ~ {end} 区间内无 K 线", FetchStatus.MISSING
            )
        return Fetched.success(
            bars, source, self.name, unit="元", note=MOCK_NOTE
        )

    async def snapshot(self, thscodes: List[str]) -> Fetched[List[Dict[str, Any]]]:
        source = self._src("market/bars/*.json")
        fault = self._fault("snapshot", source)
        if fault:
            return fault
        out: List[Dict[str, Any]] = []
        for code in thscodes:
            data = self._load(f"market/bars/{code}.json")
            if not data or not data["bars"]:
                continue
            last = data["bars"][-1]
            out.append(
                {
                    "thscode": code,
                    "last_price": last["close"],
                    "open_price": last["open"],
                    "high_price": last["high"],
                    "low_price": last["low"],
                    "prev_price": last["prev_close"],
                    "volume": last["volume"],
                    "turnover": last["amount"],
                }
            )
        if not out:
            return Fetched.failure(source, self.name, "快照无数据", FetchStatus.MISSING)
        return Fetched.success(out, source, self.name, note=MOCK_NOTE)

    async def industry_indexes(self) -> Fetched[List[IndexInfo]]:
        source = self._src("market/indexes.json")
        fault = self._fault("industry_indexes", source)
        if fault:
            return fault
        data = self._load("market/indexes.json")
        if not data:
            return Fetched.failure(
                source, self.name, "缺少指数清单", FetchStatus.MISSING
            )
        out = [
            IndexInfo(thscode=i["thscode"], name=i["name"], tag="industry")
            for i in data["industry_indexes"]
        ]
        return Fetched.success(out, source, self.name, note=MOCK_NOTE)

    async def index_constituents(self, thscode: str) -> Fetched[List[TickerInfo]]:
        source = self._src("market/indexes.json")
        fault = self._fault("index_constituents", source)
        if fault:
            return fault
        data = self._load("market/indexes.json")
        tickers = self._load("market/tickers.json") or {"item": []}
        by_code = {t["thscode"]: t for t in tickers["item"]}
        codes = (data or {}).get("constituents", {}).get(thscode, [])
        if not codes:
            return Fetched.failure(
                source, self.name, f"{thscode} 无成分股记录", FetchStatus.MISSING
            )
        out = [
            TickerInfo(**by_code[c]) for c in codes if c in by_code
        ]
        return Fetched.success(out, source, self.name, note=MOCK_NOTE)

    async def anomaly_reasons(
        self, thscodes: List[str]
    ) -> Fetched[List[AnomalyReason]]:
        source = self._src("market/anomaly_reasons.json")
        fault = self._fault("anomaly_reasons", source)
        if fault:
            return fault
        data = self._load("market/anomaly_reasons.json") or {}
        out: List[AnomalyReason] = []
        for code in thscodes:
            rec = data.get(code)
            if not rec:
                continue
            out.append(
                AnomalyReason(
                    thscode=code,
                    stock_name=rec.get("stock_name", ""),
                    content=rec.get("analysis_content", ""),
                    tag_name=rec.get("tag_name"),
                    keywords=rec.get("keyword_list") or [],
                )
            )
        if not out:
            return Fetched.failure(
                source, self.name, "当日无异动解读记录", FetchStatus.MISSING
            )
        return Fetched.success(
            out,
            source,
            self.name,
            note=f"{MOCK_NOTE}；第三方解读仅作检索线索",
        )

    # 供行业识别层直接读取构造的大盘指数
    def market_index(self) -> Optional[Dict[str, str]]:
        data = self._load("market/indexes.json") or {}
        return data.get("market_index")


def _resolve_fixtures_dir(configured: str) -> str:
    if os.path.isabs(configured):
        return configured
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    root = os.path.dirname(root)  # 退到项目根
    return os.path.join(root, configured)
