"""公司名称 / 简称库，以及 80% 相似度检索。"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Dict, List, Optional

DEFAULT_THRESHOLD = 0.8


@dataclass(frozen=True)
class CompanyRecord:
    name: str
    thscode: str = ""
    ticker: str = ""
    aliases: List[str] = field(default_factory=list)

    @property
    def labels(self) -> List[str]:
        items = [self.name, *self.aliases, self.ticker, self.thscode]
        if self.thscode and "." in self.thscode:
            items.append(self.thscode.split(".")[0])
        out: List[str] = []
        seen = set()
        for item in items:
            key = (item or "").strip()
            if not key:
                continue
            marker = key.casefold()
            if marker in seen:
                continue
            seen.add(marker)
            out.append(key)
        return out


@dataclass(frozen=True)
class CompanyMatch:
    record: CompanyRecord
    query: str
    score: float
    hit_label: str


@lru_cache(maxsize=1)
def load_company_catalog() -> List[CompanyRecord]:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "fixtures", "market", "company_names.json")
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    rows = data.get("companies", data) if isinstance(data, dict) else data
    catalog: List[CompanyRecord] = []
    seen = set()
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        name = str(row["name"]).strip()
        if name in seen:
            continue
        seen.add(name)
        catalog.append(
            CompanyRecord(
                name=name,
                thscode=str(row.get("thscode") or "").strip(),
                ticker=str(row.get("ticker") or "").strip(),
                aliases=[str(a).strip() for a in (row.get("aliases") or []) if str(a).strip()],
            )
        )
    return catalog


def match_threshold() -> float:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "fixtures", "market", "company_names.json")
    if not os.path.exists(path):
        return DEFAULT_THRESHOLD
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict):
        try:
            return float(data.get("threshold", DEFAULT_THRESHOLD))
        except (TypeError, ValueError):
            return DEFAULT_THRESHOLD
    return DEFAULT_THRESHOLD


def similarity(left: str, right: str) -> float:
    a, b = (left or "").strip(), (right or "").strip()
    if not a or not b:
        return 0.0
    if a.casefold() == b.casefold():
        return 1.0
    return SequenceMatcher(None, a.casefold(), b.casefold()).ratio()


def match_company(
    text: str, threshold: Optional[float] = None
) -> Optional[CompanyMatch]:
    query = (text or "").strip()
    if not query:
        return None
    cutoff = match_threshold() if threshold is None else threshold
    best: Optional[CompanyMatch] = None
    for record in load_company_catalog():
        for label in record.labels:
            score = similarity(query, label)
            if best is None or score > best.score:
                best = CompanyMatch(record=record, query=query, score=score, hit_label=label)
            if score >= 1.0:
                return best
    if best and best.score >= cutoff:
        return best
    return None


def bind_companies(
    mentions: List[str], threshold: Optional[float] = None
) -> List[str]:
    """把抽取结果绑定到库里的正式名称；没有 80% 命中的作废。"""
    seen = set()
    names: List[str] = []
    for mention in mentions:
        hit = match_company(mention, threshold=threshold)
        if hit is None:
            continue
        if hit.record.name in seen:
            continue
        seen.add(hit.record.name)
        names.append(hit.record.name)
    return names


def alias_map() -> Dict[str, str]:
    mapping: Dict[str, str] = {}
    for record in load_company_catalog():
        for label in record.labels:
            mapping[label] = record.name
            if label.isascii():
                mapping[label.lower()] = record.name
    return mapping
