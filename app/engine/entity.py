"""用模型抽取公司名，再与名称库做相似度绑定。"""

from __future__ import annotations

from typing import List, Optional

from app.engine.company_index import bind_companies
from app.engine.resolver import parse_query
from app.trace import RunRecorder

EXTRACT_SYSTEM = (
    "你从用户提问中抽取 A 股或港股上市公司的名称、简称或代码。"
    "只抽取明确出现的主体，不要脑补，不要解释。"
    "如果没有公司，返回空数组。"
)
EXTRACT_SCHEMA = {"companies": ["公司名称或简称"]}


async def extract_company_mentions(query: str, llm, recorder: Optional[RunRecorder] = None) -> List[str]:
    parsed = parse_query(query)
    mentions: List[str] = list(parsed.codes)
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
        if result.ok and isinstance(result.value, dict):
            used_model = True
            raw = result.value.get("companies") or []
            if isinstance(raw, list):
                mentions.extend(str(item).strip() for item in raw if str(item).strip())
    if not used_model:
        mentions.extend(parsed.name_hints)
    # 去重保序
    seen = set()
    out: List[str] = []
    for item in mentions:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


async def resolve_company_keys(
    query: str,
    llm=None,
    recorder: Optional[RunRecorder] = None,
    threshold: Optional[float] = None,
) -> List[str]:
    mentions = await extract_company_mentions(query, llm, recorder)
    return bind_companies(mentions, threshold=threshold)
