"""基于构造 fixture 的证据源。

检索逻辑刻意保持朴素（关键词命中 + 时间窗过滤 + scope 过滤），因为这一层
的职责只是"把候选信息取回来"；判断信息是否真的能解释价格变化，是
Agent 层四类验证要做的事，不应该在检索层偷偷完成。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Set, Tuple

from app.config import Settings
from app.contracts import FetchStatus, Fetched
from app.providers.base import RawEvent
from app.timeutil import is_within

MOCK_NOTE = "构造演示证据，非真实资讯"

# path -> 已经切好词的事件列表，避免每次检索重复分词
_INDEXED_EVENTS: Dict[str, List[Tuple[Dict[str, Any], Set[str]]]] = {}


class MockEvidenceProvider:
    name = "mock"

    def __init__(
        self, settings: Settings, faults: Optional[Set[str]] = None
    ) -> None:
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        base = settings.fixtures_dir
        self._path = (
            os.path.join(base, "evidence", "events.json")
            if os.path.isabs(base)
            else os.path.join(root, base, "evidence", "events.json")
        )
        self._faults = faults or set()

    async def aclose(self) -> None:
        return None

    def _load(self) -> List[Tuple[Dict[str, Any], Set[str]]]:
        cached = _INDEXED_EVENTS.get(self._path)
        if cached is not None:
            return cached
        events: List[Dict[str, Any]] = []
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8") as fh:
                events = json.load(fh).get("events", [])
        indexed = [
            (
                ev,
                _tokenize(
                    " ".join(
                        [
                            ev.get("title", ""),
                            ev.get("summary", ""),
                            " ".join(ev.get("keywords") or []),
                            " ".join(ev.get("related_names") or []),
                        ]
                    )
                ),
            )
            for ev in events
        ]
        _INDEXED_EVENTS[self._path] = indexed
        return indexed

    async def search_events(
        self,
        query: str,
        start_date: str,
        end_date: str,
        scope: str = "company",
        limit: int = 20,
        context: Optional[Dict[str, Any]] = None,
    ) -> Fetched[List[RawEvent]]:
        source = "mock:fixtures/evidence/events.json"
        if "search_events" in self._faults or f"search_events:{scope}" in self._faults:
            return Fetched.failure(
                source, self.name, f"注入故障：{scope} 范围检索失败", FetchStatus.FAILED
            )

        context = context or {}
        terms = _tokenize(query)
        # 实体词（股票名 / 行业名）是硬性门槛：公司和行业范围的检索必须命中实体，
        # 否则"行业 政策 供需"这类通用词会把无关板块的新闻全部召回。
        required = [t for t in (context.get("required_terms") or []) if t]
        required_tokens = [_tokenize(t) for t in required]

        hits: List[RawEvent] = []
        for ev, haystack in self._load():
            if ev.get("scope") and scope and ev["scope"] != scope:
                continue
            published = (ev.get("published_at") or "")[:10]
            if published and not is_within(published, start_date, end_date):
                continue
            if required_tokens and not any(rt & haystack for rt in required_tokens):
                continue
            if not required_tokens and terms and not (terms & haystack):
                continue
            hits.append(
                RawEvent(
                    event_id=ev["event_id"],
                    title=ev.get("title", ""),
                    summary=ev.get("summary", ""),
                    published_at=ev.get("published_at"),
                    source_name=ev.get("source_name", "未知来源"),
                    source_url=ev.get("source_url"),
                    source_tier_hint=ev.get("source_tier_hint"),
                    origin_key=ev.get("origin_key"),
                    cluster_hint=ev.get("cluster_hint"),
                    scope=ev.get("scope"),
                    related_names=ev.get("related_names") or [],
                    raw={"keywords": ev.get("keywords") or []},
                )
            )

        if not hits:
            return Fetched.failure(
                source,
                self.name,
                f"{start_date} ~ {end_date} 内未检索到与「{query}」相关的{scope}范围事件",
                FetchStatus.MISSING,
            )
        return Fetched.success(hits[:limit], source, self.name, note=MOCK_NOTE)


def _tokenize(text: str) -> Set[str]:
    """把中文文本切成 2-gram 集合，配合原始词一起做粗匹配。"""
    text = (text or "").strip().lower()
    if not text:
        return set()
    out: Set[str] = set()
    for chunk in text.replace("，", " ").replace("、", " ").split():
        out.add(chunk)
        for n in (2, 3):
            for i in range(len(chunk) - n + 1):
                out.add(chunk[i : i + n])
    return out
