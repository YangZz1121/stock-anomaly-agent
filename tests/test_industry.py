"""行业识别：先对照预置清单判定公司所属行业，再定向取数。"""

from __future__ import annotations

import asyncio
from typing import Dict, List

from app.config import Settings
from app.contracts import FetchStatus, Fetched
from app.engine.industry import IndustryResolver, guess_industry, load_catalog
from app.providers.base import TickerInfo
from app.providers.llm import DisabledLLM
from app.trace import RunRecorder


class FakeMarket:
    name = "fake"

    def __init__(self, constituents: Dict[str, List[str]]) -> None:
        self._constituents = constituents
        self.calls: List[str] = []

    async def index_constituents(self, thscode: str):
        self.calls.append(thscode)
        codes = self._constituents.get(thscode) or []
        if not codes:
            return Fetched.failure(
                "fake", self.name, "成分股为空", FetchStatus.MISSING
            )
        return Fetched.success(
            [TickerInfo(thscode=c, name=c, ticker=c.split(".")[0]) for c in codes],
            "fake",
            self.name,
        )


class FakeLLM:
    name = "fake"
    model = "fake"

    def __init__(self, code: str | None) -> None:
        self._code = code
        self.calls = 0

    async def complete_json(self, **kwargs):
        self.calls += 1
        return Fetched.success(
            {"code": self._code, "name": None, "reason": ""}, "fake", self.name
        )


def _resolve(
    tmp_path,
    market,
    llm,
    thscode="300750.SZ",
    name="宁德时代",
    query="宁德时代最近5个交易日怎么一直跌",
    provider="mock",
):
    settings = Settings(
        market_provider=provider,
        evidence_provider="mock",
        llm_provider="mock",
        cache_dir=str(tmp_path),
    )

    async def _run():
        resolver = IndustryResolver(settings, market, llm)
        return await resolver.resolve(thscode, name, RunRecorder("t"), query=query)

    return asyncio.run(_run()), market, llm


def test_guess_industry_from_query_or_code_without_market_call():
    catalog = load_catalog(
        Settings(market_provider="mock", evidence_provider="mock", llm_provider="mock")
    )
    by_query = guess_industry(catalog, query="宁德时代最近5个交易日怎么一直跌")
    by_code = guess_industry(catalog, thscode="300750")
    by_short = guess_industry(catalog, query="茅台今天为什么跌了")
    assert by_query is not None and by_query.name == "电池"
    assert by_code is not None and by_code.name == "电池"
    assert by_short is not None and by_short.name == "白酒"
    assert by_query.thscode == "886041.TI"


def test_preset_catalog_is_level1_only():
    catalog = load_catalog(
        Settings(market_provider="fuyao", evidence_provider="mock", llm_provider="mock")
    )
    assert len(catalog.indexes) == 90
    assert all(i.thscode.startswith("881") for i in catalog.indexes)
    assert any(i.name == "电池" and i.thscode == "881281.TI" for i in catalog.indexes)
    assert catalog.by_code["881281.TI"].name == "电池"
    assert catalog.by_name["动力电池"].name == "电池"


def test_mock_overlay_rewrites_demo_industries_to_fixture_codes():
    catalog = load_catalog(
        Settings(market_provider="mock", evidence_provider="mock", llm_provider="mock")
    )
    by_name = {i.name: i.thscode for i in catalog.indexes}
    assert by_name["电池"] == "886041.TI"
    assert by_name["白酒"] == "886042.TI"
    assert by_name["乳制品"] == "886055.TI"
    assert by_name["汽车整车"] == "886060.TI"
    assert by_name["半导体"] == "886070.TI"


def test_seed_resolves_without_any_market_or_llm_call(tmp_path):
    market = FakeMarket({})
    llm = FakeLLM("881281.TI")
    info, market, llm = _resolve(tmp_path, market, llm)

    assert info.index_name == "电池"
    assert info.index_code == "886041.TI"
    assert info.method == "structural"
    assert info.is_weak_evidence is False
    assert market.calls == []
    assert llm.calls == 0


def test_short_company_name_in_query_hits_seed(tmp_path):
    info, market, llm = _resolve(
        tmp_path,
        FakeMarket({}),
        FakeLLM("881273.TI"),
        thscode="600519.SH",
        name="贵州茅台",
        query="茅台今天为什么跌了",
    )
    assert info.index_name == "白酒"
    assert info.index_code == "886042.TI"
    assert market.calls == []
    assert llm.calls == 0


def test_unknown_stock_is_classified_then_verified_against_one_index(tmp_path):
    market = FakeMarket({"881155.TI": ["601398.SH"]})
    llm = FakeLLM("881155.TI")
    info, market, llm = _resolve(
        tmp_path,
        market,
        llm,
        thscode="601398.SH",
        name="工商银行",
        query="工商银行今天为什么跌了",
        provider="fuyao",
    )
    assert info.index_name == "银行"
    assert info.index_code == "881155.TI"
    assert info.method == "reverse_map"
    assert market.calls == ["881155.TI"]
    assert llm.calls == 1


def test_unverified_llm_guess_is_marked_weak_and_does_not_scan_other_industries(
    tmp_path,
):
    market = FakeMarket({"881281.TI": ["300750.SZ"]})
    info, market, llm = _resolve(
        tmp_path,
        market,
        FakeLLM("881126.TI"),
        thscode="601398.SH",
        name="工商银行",
        provider="fuyao",
    )
    assert info.index_code == "881126.TI"
    assert info.index_name == "汽车零部件"
    assert info.method == "llm_assisted"
    assert info.is_weak_evidence is True
    assert market.calls == ["881126.TI"]
    assert llm.calls == 1


def test_same_industry_members_are_cached_after_first_verify(tmp_path):
    market = FakeMarket({"881155.TI": ["601398.SH", "601288.SH"]})
    _resolve(
        tmp_path,
        market,
        FakeLLM("881155.TI"),
        thscode="601398.SH",
        name="工商银行",
        provider="fuyao",
    )
    assert market.calls == ["881155.TI"]

    info, market, llm = _resolve(
        tmp_path,
        market,
        FakeLLM("881155.TI"),
        thscode="601288.SH",
        name="农业银行",
        provider="fuyao",
    )
    assert info.index_code == "881155.TI"
    assert info.note == "来自本地行业映射缓存"
    assert market.calls == ["881155.TI"]
    assert llm.calls == 0


def test_disabled_llm_without_seed_does_not_scan_the_catalog(tmp_path):
    market = FakeMarket({"881281.TI": ["300750.SZ"]})
    info, market, _ = _resolve(
        tmp_path,
        market,
        DisabledLLM(),
        thscode="601398.SH",
        name="工商银行",
        provider="fuyao",
    )
    assert info.method == "unavailable"
    assert info.index_code is None
    assert market.calls == []
