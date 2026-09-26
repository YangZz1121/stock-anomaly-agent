"""公司名库与 80% 相似度绑定。"""

from __future__ import annotations

import asyncio

from app.contracts import Fetched
from app.engine.company_index import bind_companies, match_company, similarity
from app.engine.entity import extract_company_mentions, resolve_company_keys
from app.engine.intent import IntentKind, classify_intent


def test_exact_alias_and_official_name_match():
    assert match_company("宁王").record.name == "宁德时代"
    assert match_company("茅台").record.name == "贵州茅台"
    assert match_company("300750").record.name == "宁德时代"
    assert bind_companies(["宁王", "伊利"]) == ["宁德时代", "伊利股份"]


def test_similarity_threshold_rejects_unknown_name():
    assert similarity("不存在的公司", "宁德时代") < 0.8
    assert match_company("不存在的公司") is None
    assert bind_companies(["不存在的公司", "这家公司根本不存在"]) == []


def test_near_miss_name_can_still_hit_at_80_percent():
    hit = match_company("宁德时")
    assert hit is not None
    assert hit.record.name == "宁德时代"
    assert hit.score >= 0.8


def test_classify_unmatched_name_is_missing_required_param():
    intent = classify_intent("不存在的公司", None)
    assert intent.kind == IntentKind.NEED_STOCK


def test_extract_uses_model_then_binds():
    class FakeLLM:
        name = "fake"
        model = "unit"

        async def complete_json(self, purpose, system, user, schema_hint=None):
            assert purpose == "extract_companies"
            return Fetched.success({"companies": ["宁王"]}, "llm:fake", "fake")

    mentions = asyncio.run(extract_company_mentions("看看宁王怎么了", FakeLLM()))
    assert "宁王" in mentions
    keys = asyncio.run(resolve_company_keys("看看宁王怎么了", FakeLLM()))
    assert keys == ["宁德时代"]


def test_extract_model_empty_does_not_invent_from_leftover():
    class EmptyLLM:
        name = "fake"
        model = "unit"

        async def complete_json(self, purpose, system, user, schema_hint=None):
            return Fetched.success({"companies": []}, "llm:fake", "fake")

    keys = asyncio.run(resolve_company_keys("不存在的公司今天跌了", EmptyLLM()))
    assert keys == []


def test_full_legal_name_matches_listed_company():
    hit = match_company("宁德时代新能源科技股份有限公司")
    assert hit is not None
    assert hit.record.name == "宁德时代"
    assert bind_companies(["贵州茅台酒股份有限公司"]) == ["贵州茅台"]


def test_empty_model_still_finds_company_in_query():
    class EmptyLLM:
        name = "fake"
        model = "unit"

        async def complete_json(self, purpose, system, user, schema_hint=None):
            return Fetched.success({"companies": []}, "llm:fake", "fake")

    keys = asyncio.run(resolve_company_keys("宁德时代新能源科技股份有限公司今天为什么跌了", EmptyLLM()))
    assert keys == ["宁德时代"]


def test_model_full_name_still_binds_to_catalog():
    class FullNameLLM:
        name = "fake"
        model = "unit"

        async def complete_json(self, purpose, system, user, schema_hint=None):
            return Fetched.success(
                {"companies": ["宁德时代新能源科技股份有限公司"]},
                "llm:fake",
                "fake",
            )

    keys = asyncio.run(resolve_company_keys("看看这只股票怎么了", FullNameLLM()))
    assert keys == ["宁德时代"]


def test_model_unknown_listed_company_is_kept_as_search_key():
    class OutsideLLM:
        name = "fake"
        model = "unit"

        async def complete_json(self, purpose, system, user, schema_hint=None):
            return Fetched.success({"companies": ["澜起科技"]}, "llm:fake", "fake")

    keys = asyncio.run(resolve_company_keys("澜起科技今天为什么跌了", OutsideLLM()))
    assert keys == ["澜起科技"]


def test_classify_full_legal_name_is_not_missing_param():
    intent = classify_intent("宁德时代新能源科技股份有限公司", None)
    assert intent.kind == IntentKind.NEED_WINDOW
    assert intent.parsed.search_keys == ["宁德时代"]


def test_catalog_covers_a_share_and_hong_kong():
    from collections import Counter

    from app.engine.company_index import load_company_catalog

    records = load_company_catalog()
    markets = Counter(
        record.thscode.split(".")[-1] for record in records if "." in record.thscode
    )
    assert len(records) > 7000
    assert markets["SH"] > 2000
    assert markets["SZ"] > 2000
    assert markets["BJ"] > 200
    assert markets["HK"] > 2000
    assert match_company("澜起科技").record.thscode == "688008.SH"
    assert match_company("腾讯").record.thscode == "00700.HK"
    assert match_company("阿里巴巴").record.thscode == "09988.HK"
