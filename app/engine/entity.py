"""用模型抽取公司名，再与名称库做相似度绑定。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from app.engine.company_index import (
    bind_companies,
    is_screen_query,
    scan_query_for_companies,
)
from app.engine.resolver import parse_query
from app.engine.topic import apply_topic_policy
from app.trace import RunRecorder

EXTRACT_SYSTEM = (
    "你从用户最新一句提问中抽取 A 股或港股上市公司的名称、简称或代码。"
    "只抽取这一句里明确出现的主体，不要把更早对话里的公司再塞进来，不要脑补，不要解释。"
    "优先抽取简称或常用名，例如「宁德时代」而不是全称。"
    "一句里可以有多家，全部返回；如果没有公司，返回空数组。"
)
EXTRACT_SCHEMA = {"companies": ["公司名称或简称"]}


@dataclass
class ExtractedCompanies:
    mentions: List[str] = field(default_factory=list)
    model_mentions: List[str] = field(default_factory=list)
    used_model: bool = False


def _flatten_company_item(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for key in ("name", "company", "stock", "query", "thscode", "ticker"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return str(item).strip() if item is not None else ""


def _companies_from_payload(value: Any) -> List[str]:
    if isinstance(value, list):
        raw_items = value
    elif isinstance(value, dict):
        raw_items = None
        for key in ("companies", "company", "names", "entities", "stocks"):
            raw = value.get(key)
            if isinstance(raw, str) and raw.strip():
                raw_items = [raw]
                break
            if isinstance(raw, list):
                raw_items = raw
                break
        if raw_items is None:
            return []
    else:
        return []
    out: List[str] = []
    seen = set()
    for item in raw_items:
        key = _flatten_company_item(item)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _dedupe(items: List[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        key = (item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


async def extract_companies(
    query: str, llm, recorder: Optional[RunRecorder] = None
) -> ExtractedCompanies:
    parsed = parse_query(query)
    local = _dedupe(list(parsed.codes) + list(parsed.name_hints) + scan_query_for_companies(query))
    model_mentions: List[str] = []
    used_model = False
    if llm is not None:
        result = await llm.complete_json(
            "extract_companies",
            EXTRACT_SYSTEM,
            f"用户提问：{query}",
            schema_hint=EXTRACT_SCHEMA,
        )
        if recorder is not None:
            recorder.record_llm(
                purpose="extract_companies",
                model=getattr(llm, "model", "unknown"),
                provider=getattr(llm, "name", "unknown"),
                latency_ms=0,
                ok=result.ok,
                note=result.note,
                prompt_chars=len(query or ""),
                response_chars=len(str(result.value or "")),
            )
        if result.ok:
            used_model = True
            model_mentions = _companies_from_payload(result.value)
    return ExtractedCompanies(
        mentions=_dedupe(local + model_mentions),
        model_mentions=model_mentions,
        used_model=used_model,
    )


async def extract_company_mentions(query: str, llm, recorder: Optional[RunRecorder] = None) -> List[str]:
    return (await extract_companies(query, llm, recorder)).mentions


async def resolve_company_keys(
    query: str,
    llm=None,
    recorder: Optional[RunRecorder] = None,
    threshold: Optional[float] = None,
    context=None,
) -> List[str]:
    extracted = await extract_companies(query, llm, recorder)
    bound = bind_companies(extracted.mentions, threshold=threshold)
    current = list(bound)
    if not current:
        extras: List[str] = []
        seen = set()
        for item in extracted.model_mentions:
            key = (item or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            extras.append(key)
        for item in parse_query(query).codes:
            key = (item or "").strip()
            if not key or key in seen:
                continue
            seen.add(key)
            extras.append(key)
        current = extras
    if is_screen_query(query) and not scan_query_for_companies(query):
        return []
    keys, _ = apply_topic_policy(current, query, context)
    return keys
