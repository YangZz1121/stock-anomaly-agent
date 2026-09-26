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
