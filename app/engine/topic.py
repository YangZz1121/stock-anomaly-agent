"""多轮对话里的公司主题。

公司名不是唯一槽位：一轮里可以有多家，查找永远用最新出现的值。
只有用户明确要求合并（对比、一起看、再加上）时，才把上一轮主题和
当前点名的公司并在一起，并行分析。改口则只认最新这一批。
"""

from __future__ import annotations

import re
from typing import List, Sequence, Tuple

from app.engine.company_index import bind_companies, scan_query_for_companies
from app.engine.resolver import parse_query

_MERGE = re.compile(
    r"(对比|比起|相比|对照|比较|以及|还有|再加上|也看看|也分析|"
    r"一起看|一起分析|两家|几家|都分析|都看看|都研究)"
)
_REPLACE = re.compile(r"(换成|改看|改成|换到|别看|不要看|换成看)")


def wants_merge(query: str) -> bool:
    raw = (query or "").strip()
    if not raw or _REPLACE.search(raw):
        return False
    return bool(_MERGE.search(raw))


def keys_from_text(text: str) -> List[str]:
    raw = (text or "").strip()
    if not raw:
        return []
    mentions = list(parse_query(raw).search_keys) + scan_query_for_companies(raw)
    return bind_companies(mentions) or _dedupe(mentions)


def latest_topic_companies(context) -> List[str]:
    """取对话里最近一次有效的公司主题，而不是历史并集。"""
    if context is None:
        return []
    for turn in reversed(getattr(context, "turns", None) or []):
        stocks = _clean(getattr(turn, "stocks", None) or [])
        if stocks:
            return bind_companies(stocks) or stocks
        if getattr(turn, "role", "") == "user":
            keys = keys_from_text(getattr(turn, "text", "") or "")
            if keys:
                return keys
    for text in reversed(getattr(context, "queries", None) or []):
        keys = keys_from_text(text)
        if keys:
            return keys
    stocks = _clean(getattr(context, "stocks", None) or [])
    if stocks:
        return bind_companies(stocks) or stocks
    return []


def apply_topic_policy(
    current: Sequence[str],
    query: str,
    context=None,
) -> Tuple[List[str], bool]:
    """用当前句覆盖主题；合并语义下才并入上一轮公司。"""
    now = _dedupe(current)
    latest = latest_topic_companies(context)
    if now:
        if wants_merge(query) and latest:
            extras = [key for key in latest if key not in now]
            return _dedupe(list(now) + extras), True
        return now, False
    if latest:
        return latest, True
    return [], False


def _clean(items: Sequence[str]) -> List[str]:
    return _dedupe(item.strip() for item in items if item and str(item).strip())


def _dedupe(items) -> List[str]:
    seen = set()
    out: List[str] = []
    for item in items:
        key = (item or "").strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out
