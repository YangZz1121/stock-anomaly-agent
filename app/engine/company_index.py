"""公司名称 / 简称库，以及 80% 相似度检索。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Dict, List, Optional, Tuple

DEFAULT_THRESHOLD = 0.8
_LEGAL_TAIL = re.compile(
    r"(集团股份有限公司|股份有限公司|有限责任公司|控股有限公司|股份公司|有限公司|集团)$"
)
_GENERIC_TOKENS = {
    "公司",
    "股份",
    "集团",
    "科技",
    "控股",
    "有限",
    "国际",
    "中国",
    "银行",
    "证券",
    "保险",
    "汽车",
    "发展",
    "电子",
    "医药",
    "食品",
    "网络",
    "信息",
    "工业",
    "能源",
    "投资",
    "企业",
    "有限公司",
    "股份公司",
}
_A_SHARE = {"SH", "SZ", "BJ"}


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
        if self.thscode.endswith(".HK"):
            digits = self.thscode.split(".")[0].lstrip("0") or "0"
            items.append(f"{digits.zfill(4)}.HK")
            items.append(digits.zfill(4))
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


@dataclass
class _Catalog:
    records: List[CompanyRecord]
    exact: Dict[str, List[CompanyRecord]]
    scan_labels: List[Tuple[str, str]]
    names: List[CompanyRecord]
    by_prefix: Dict[str, List[CompanyRecord]]
    threshold: float


def _prefer(records: List[CompanyRecord]) -> CompanyRecord:
    def rank(record: CompanyRecord) -> Tuple[int, str]:
        suffix = record.thscode.split(".")[-1] if "." in record.thscode else ""
        return (0 if suffix in _A_SHARE else 1, record.thscode)

    return sorted(records, key=rank)[0]


@lru_cache(maxsize=1)
def _catalog() -> _Catalog:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(root, "fixtures", "market", "company_names.json")
    records: List[CompanyRecord] = []
    threshold = DEFAULT_THRESHOLD
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            try:
                threshold = float(data.get("threshold", DEFAULT_THRESHOLD))
            except (TypeError, ValueError):
                threshold = DEFAULT_THRESHOLD
            rows = data.get("companies", [])
        else:
            rows = data
        seen = set()
        for row in rows or []:
            if not isinstance(row, dict) or not row.get("name"):
                continue
            name = str(row["name"]).strip()
            code = str(row.get("thscode") or "").strip()
            marker = code or name
            if not marker or marker in seen:
                continue
            seen.add(marker)
            records.append(
                CompanyRecord(
                    name=name,
                    thscode=code,
                    ticker=str(row.get("ticker") or "").strip(),
                    aliases=[str(a).strip() for a in (row.get("aliases") or []) if str(a).strip()],
                )
            )
    exact: Dict[str, List[CompanyRecord]] = {}
    scan: List[Tuple[str, str]] = []
    by_prefix: Dict[str, List[CompanyRecord]] = {}
    for record in records:
        by_prefix.setdefault(record.name[:1], []).append(record)
        for label in record.labels:
            exact.setdefault(label.casefold(), []).append(record)
        if len(record.name) >= 3:
            scan.append((record.name, record.name))
        for alias in record.aliases:
            if alias.isdigit() or len(alias) < 2:
                continue
            scan.append((alias, record.name))
    scan.sort(key=lambda item: len(item[0]), reverse=True)
    return _Catalog(
        records=records,
        exact=exact,
        scan_labels=scan,
        names=records,
        by_prefix=by_prefix,
        threshold=threshold,
    )


def load_company_catalog() -> List[CompanyRecord]:
    return _catalog().records


def match_threshold() -> float:
    return _catalog().threshold


def similarity(left: str, right: str) -> float:
    a, b = (left or "").strip(), (right or "").strip()
    if not a or not b:
        return 0.0
    if a.casefold() == b.casefold():
        return 1.0
    return SequenceMatcher(None, a.casefold(), b.casefold()).ratio()


def _normalize_company_text(text: str) -> str:
    raw = (text or "").strip()
    raw = re.sub(r"[（(].*?[）)]", "", raw)
    raw = _LEGAL_TAIL.sub("", raw)
    return raw.strip()


def _pair_score(query: str, label: str) -> float:
    """全称包含简称、或简称包含于库内名称，都按命中处理。"""
    variants = [query, _normalize_company_text(query)]
    labels = [label, _normalize_company_text(label)]
    best = 0.0
    for raw_q in variants:
        q = raw_q.casefold()
        if not q or q in _GENERIC_TOKENS:
            continue
        for raw_lab in labels:
            lab = raw_lab.casefold()
            if not lab or lab in _GENERIC_TOKENS:
                continue
            if q == lab:
                return 1.0
            if len(lab) >= 2 and lab in q:
                best = max(best, max(0.8, len(lab) / len(q)))
                continue
            if len(q) >= 3 and q in lab:
                best = max(best, max(0.8, len(q) / len(lab)))
                continue
            if len(q) == 2 and q in lab and len(lab) <= 6:
                best = max(best, max(0.8, len(q) / len(lab)))
                continue
            best = max(best, similarity(raw_q, raw_lab))
    return best


def match_company(
    text: str, threshold: Optional[float] = None
) -> Optional[CompanyMatch]:
    query = (text or "").strip()
    if not query:
        return None
    catalog = _catalog()
    cutoff = catalog.threshold if threshold is None else threshold
    folded = query.casefold()
    normalized = _normalize_company_text(query).casefold()
    hits = catalog.exact.get(folded) or catalog.exact.get(normalized)
    if hits:
        record = _prefer(hits)
        return CompanyMatch(record=record, query=query, score=1.0, hit_label=record.name)

    best: Optional[CompanyMatch] = None

    def _consider(record: CompanyRecord, label: str, score: float) -> None:
        nonlocal best
        if best is None or score > best.score or (
            score == best.score and len(label) > len(best.hit_label)
        ):
            best = CompanyMatch(record=record, query=query, score=score, hit_label=label)

    # 包含匹配：只看「标签出现在问句里」，避免 8000 家全量模糊扫描
    for label, records in catalog.exact.items():
        if label in _GENERIC_TOKENS or len(label) < 2:
            continue
        if label in folded or (normalized and label in normalized):
            record = _prefer(records)
            _consider(record, label, max(0.8, len(label) / max(len(folded), 1)))
        elif len(folded) >= 3 and folded in label and len(label) <= 12:
            record = _prefer(records)
            _consider(record, label, max(0.8, len(folded) / len(label)))

    if best and best.score >= cutoff:
        return best

    # 近形匹配只在同首字、长度接近的正式名称上做，控制全量库的耗时
    prefix = query[:1]
    for record in catalog.by_prefix.get(prefix, []):
        if abs(len(record.name) - len(query)) > 3:
            continue
        score = _pair_score(query, record.name)
        if score >= cutoff:
            _consider(record, record.name, score)
    if best and best.score >= cutoff:
        return best
    return None


def scan_query_for_companies(text: str) -> List[str]:
    """在原句里按最长标签扫描名称库，避免模型漏抽时整句失效。"""
    raw = text or ""
    if not raw:
        return []
    folded = raw.casefold()
    occupied = [False] * len(raw)
    found: List[str] = []
    seen = set()
    for label, name in _catalog().scan_labels:
        start = 0
        needle = label.casefold()
        if len(needle) < 2:
            continue
        while True:
            idx = folded.find(needle, start)
            if idx < 0:
                break
            end = idx + len(label)
            if end <= len(occupied) and not any(occupied[idx:end]):
                for i in range(idx, end):
                    occupied[i] = True
                if name not in seen:
                    seen.add(name)
                    found.append(name)
                break
            start = idx + 1
    return found


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
        for label in (record.name, *record.aliases):
            key = (label or "").strip()
            if not key or key.isdigit() or key.replace(".", "").isdigit():
                continue
            mapping[key] = record.name
            if key.isascii():
                mapping[key.lower()] = record.name
    return mapping
