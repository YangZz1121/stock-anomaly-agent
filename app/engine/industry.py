"""行业识别：先判定公司属于哪个预设行业，再定向取数。

不再每次研究都去拉全市场行业指数清单，也不再按成分股扫一遍。
流程是：

1. 用仓库里预置的一级行业清单（约 90 个）做分类空间；
2. 用预置种子表 / 本地缓存直接给出公司所属行业；
3. 种子表没有的，才让模型从同一份清单里选一次，并只验证这一个指数的成分股；
4. 后续行情对比和资讯检索都只针对这个行业。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple

from app.config import Settings
from app.contracts import FetchStatus
from app.providers.base import TickerInfo
from app.schemas import IndustryInfo
from app.trace import RunRecorder

METHOD_LABELS = {
    "structural": "预设清单直接识别",
    "reverse_map": "行业指数成分股定向确认",
    "llm_assisted": "LLM 辅助判断（弱证据）",
    "unavailable": "无法识别",
}

_CATALOG_REL = os.path.join("market", "industry_catalog.json")
_MOCK_INDEXES_REL = os.path.join("market", "indexes.json")


@dataclass(frozen=True)
class CatalogEntry:
    thscode: str
    name: str
    aliases: Tuple[str, ...] = field(default_factory=tuple)


class IndustryResolver:
    def __init__(self, settings: Settings, market: Any, llm: Any) -> None:
        self._settings = settings
        self._market = market
        self._llm = llm
        self._cache_path = _cache_path(settings)
        self._cache = _load_cache(self._cache_path)
        self._catalog = load_catalog(settings)

    async def resolve(
        self,
        thscode: str,
        stock_name: str,
        recorder: RunRecorder,
        query: Optional[str] = None,
    ) -> IndustryInfo:
        # ---- 第一级：预设种子或本地缓存，不打任何行情接口 ----
        preset = guess_industry(self._catalog, thscode=thscode, name=stock_name)
        if preset is not None:
            self._remember(thscode, preset.thscode, preset.name, "structural")
            return IndustryInfo(
                index_name=preset.name,
                index_code=preset.thscode,
                method="structural",
                method_label=METHOD_LABELS["structural"],
                is_weak_evidence=False,
                note=f"由预设行业清单识别为{preset.name}",
            )

        cached = self._cache.get("map", {}).get(thscode)
        if cached:
            entry = self._catalog.by_code.get(cached.get("code", ""))
            if entry is not None:
                return IndustryInfo(
                    index_name=entry.name,
                    index_code=entry.thscode,
                    method=cached.get("method", "structural"),
                    method_label=METHOD_LABELS.get(
                        cached.get("method", "structural"), ""
                    ),
                    is_weak_evidence=cached.get("method") == "llm_assisted",
                    note="来自本地行业映射缓存",
                )

        # ---- 第二级：模型从预设清单里提名，再只验证这一个行业 ----
        guess = await self._classify(stock_name, query, recorder)
        if guess is None:
            return IndustryInfo(
                method="unavailable",
                method_label=METHOD_LABELS["unavailable"],
                note="预设清单与模型均未能确定所属行业",
            )

        verified = await self._confirm_membership(thscode, guess, recorder)
        if verified:
            self._remember(thscode, guess.thscode, guess.name, "reverse_map")
            return IndustryInfo(
                index_name=guess.name,
                index_code=guess.thscode,
                method="reverse_map",
                method_label=METHOD_LABELS["reverse_map"],
                is_weak_evidence=False,
                note=f"按{guess.name}定向核验成分股后确认",
            )

        self._remember(thscode, guess.thscode, guess.name, "llm_assisted")
        return IndustryInfo(
            index_name=guess.name,
            index_code=guess.thscode,
            method="llm_assisted",
            method_label=METHOD_LABELS["llm_assisted"],
            is_weak_evidence=True,
            note=(
                f"模型将{stock_name}归入{guess.name}，但成分股未能当场确认，"
                "属于弱证据，行业层面的结论需要额外谨慎对待"
            ),
        )

    def pin(self, thscode: str, entry: CatalogEntry, method: str = "structural") -> IndustryInfo:
        """把已经判定的行业写进缓存，后续研究不再重复分类。"""
        self._remember(thscode, entry.thscode, entry.name, method)
        return industry_from_entry(entry, method)

    # ------------------------------------------------------------------

    async def _classify(
        self, stock_name: str, query: Optional[str], recorder: RunRecorder
    ) -> Optional[CatalogEntry]:
        if not stock_name or not self._catalog.indexes:
            return None
        catalog = [
            {"code": i.thscode, "name": i.name} for i in self._catalog.indexes
        ]
        question = (query or "").strip()
        started = time.perf_counter()
        res = await self._llm.complete_json(
            purpose="industry_assist",
            system=(
                "你是 A 股行业分类助手。只能从给定的一级行业清单中选择，"
                "不得编造清单之外的指数。若无法判断，返回 code 为 null。"
            ),
            user=(
                f"公司名称：{stock_name}\n"
                + (f"用户问题：{question}\n" if question else "")
                + f"\n候选一级行业清单：\n{json.dumps(catalog, ensure_ascii=False)}\n\n"
                "请选出该公司所属的一个一级行业。"
            ),
            schema_hint={"code": "string | null", "name": "string | null", "reason": "string"},
        )
        recorder.record_llm(
            "industry_assist",
            getattr(self._llm, "model", "none"),
            getattr(self._llm, "name", "none"),
            int((time.perf_counter() - started) * 1000),
            res.ok,
            res.note,
        )
        if not res.ok or not res.value:
            return None
        code = (res.value or {}).get("code")
        name = (res.value or {}).get("name")
        if code:
            hit = self._catalog.by_code.get(code)
            if hit is not None:
                return hit
        if name:
            return self._catalog.by_name.get(name)
        return None

    async def _confirm_membership(
        self, thscode: str, index: CatalogEntry, recorder: RunRecorder
    ) -> bool:
        cached_members = (self._cache.get("constituents") or {}).get(index.thscode)
        if cached_members is not None:
            recorder.record_tool(
                "index_constituents",
                {"thscode": index.thscode, "name": index.name},
                FetchStatus.OK,
                getattr(self._market, "name", "unknown"),
                note="来自本地成分股缓存",
            )
            return thscode in set(cached_members)

        res = await self._market.index_constituents(index.thscode)
        recorder.record_tool(
            "index_constituents",
            {"thscode": index.thscode, "name": index.name},
            res.status,
            getattr(self._market, "name", "unknown"),
            note=res.note,
        )
        if not res.ok or not res.value:
            return False
        members: List[TickerInfo] = res.value
        codes = [ticker.thscode for ticker in members]
        self._cache.setdefault("constituents", {})[index.thscode] = codes
        mapping = self._cache.setdefault("map", {})
        for ticker in members:
            mapping.setdefault(
                ticker.thscode,
                {"code": index.thscode, "name": index.name, "method": "reverse_map"},
            )
        self._flush()
        return thscode in set(codes)

    def _remember(self, thscode: str, code: str, name: str, method: str) -> None:
        self._cache.setdefault("map", {})[thscode] = {
            "code": code,
            "name": name,
            "method": method,
        }
        self._flush()

    def _flush(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
            with open(self._cache_path, "w", encoding="utf-8") as fh:
                json.dump(self._cache, fh, ensure_ascii=False)
        except OSError:
            pass


@dataclass
class IndustryCatalog:
    indexes: List[CatalogEntry]
    seed_by_code: Dict[str, str]
    seed_by_name: Dict[str, str]
    seed_by_ticker: Dict[str, str] = field(default_factory=dict)
    by_code: Dict[str, CatalogEntry] = field(default_factory=dict)
    by_name: Dict[str, CatalogEntry] = field(default_factory=dict)


def industry_from_entry(entry: CatalogEntry, method: str = "structural") -> IndustryInfo:
    return IndustryInfo(
        index_name=entry.name,
        index_code=entry.thscode,
        method=method,
        method_label=METHOD_LABELS.get(method, ""),
        is_weak_evidence=method == "llm_assisted",
        note=f"由预设行业清单识别为{entry.name}",
    )


def guess_industry(
    catalog: IndustryCatalog,
    *,
    thscode: Optional[str] = None,
    name: Optional[str] = None,
    query: Optional[str] = None,
) -> Optional[CatalogEntry]:
    """用预置种子表判定行业，不访问任何行情接口。

    问句里出现「宁德时代」或代码 ``300750`` 时，在标的检索完成之前
    就能定向后续取数，不必等接口返回全称。
    """
    seed = _seed_by_code(catalog, thscode)
    if seed:
        hit = catalog.by_name.get(seed)
        if hit is not None:
            return hit
    for text in (name, query):
        if not text:
            continue
        seed = catalog.seed_by_name.get(text)
        if seed is None:
            seed = _match_seed_name(text, catalog.seed_by_name)
        if seed:
            hit = catalog.by_name.get(seed)
            if hit is not None:
                return hit
    return None


def _seed_by_code(catalog: IndustryCatalog, thscode: Optional[str]) -> Optional[str]:
    if not thscode:
        return None
    direct = catalog.seed_by_code.get(thscode)
    if direct:
        return direct
    digits = thscode.split(".")[0]
    if digits.isdigit():
        return catalog.seed_by_ticker.get(digits)
    return None


def load_catalog(settings: Settings) -> IndustryCatalog:
    """预置一级行业清单。构造数据集下用同名 fixture 指数覆盖代码。"""
    return _load_catalog(settings.fixtures_dir, settings.resolved_market_provider())


@lru_cache(maxsize=8)
def _load_catalog(fixtures_dir: str, provider: str) -> IndustryCatalog:
    fixtures = _fixtures_dir(fixtures_dir)
    raw = _read_json(os.path.join(fixtures, _CATALOG_REL)) or {}
    indexes = [
        CatalogEntry(
            thscode=item["thscode"],
            name=item["name"],
            aliases=tuple(item.get("aliases") or ()),
        )
        for item in raw.get("indexes") or []
        if item.get("thscode") and item.get("name")
    ]
    if provider == "mock":
        indexes = _overlay_mock_indexes(indexes, fixtures)

    by_code = {entry.thscode: entry for entry in indexes}
    by_name: Dict[str, CatalogEntry] = {}
    for entry in indexes:
        by_name[entry.name] = entry
        for alias in entry.aliases:
            by_name.setdefault(alias, entry)

    seed_by_code = dict(raw.get("seed_by_code") or {})
    seed_by_ticker = {
        code.split(".")[0]: seed
        for code, seed in seed_by_code.items()
        if code.split(".")[0].isdigit()
    }
    return IndustryCatalog(
        indexes=indexes,
        seed_by_code=seed_by_code,
        seed_by_name=dict(raw.get("seed_by_name") or {}),
        seed_by_ticker=seed_by_ticker,
        by_code=by_code,
        by_name=by_name,
    )


def _overlay_mock_indexes(
    indexes: List[CatalogEntry], fixtures: str
) -> List[CatalogEntry]:
    mock = _read_json(os.path.join(fixtures, _MOCK_INDEXES_REL)) or {}
    items = mock.get("industry_indexes") or []
    if not items:
        return indexes
    by_name = {entry.name: i for i, entry in enumerate(indexes)}
    out = list(indexes)
    for item in items:
        code, name = item.get("thscode"), item.get("name")
        if not code or not name:
            continue
        replacement = CatalogEntry(thscode=code, name=name, aliases=())
        if name in by_name:
            old = out[by_name[name]]
            out[by_name[name]] = CatalogEntry(
                thscode=code, name=name, aliases=old.aliases
            )
        else:
            out.append(replacement)
            by_name[name] = len(out) - 1
    return out


def _match_seed_name(stock_name: str, seeds: Dict[str, str]) -> Optional[str]:
    """允许「贵州茅台今天为什么跌」里只命中种子名「茅台」。"""
    hits = [key for key in seeds if key and key in stock_name]
    if not hits:
        return None
    return seeds[max(hits, key=len)]


def _fixtures_dir(configured: str) -> str:
    if os.path.isabs(configured):
        return configured
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, configured)


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _cache_path(settings: Settings) -> str:
    base = settings.cache_dir
    if not os.path.isabs(base):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        base = os.path.join(root, base)
    provider = settings.resolved_market_provider()
    return os.path.join(base, f"industry_map.{provider}.json")


_CACHE_MEMO: Dict[str, Dict[str, Any]] = {}


def _load_cache(path: str) -> Dict[str, Any]:
    cached = _CACHE_MEMO.get(path)
    if cached is not None:
        return cached
    if not os.path.exists(path):
        data: Dict[str, Any] = {"map": {}, "constituents": {}}
        _CACHE_MEMO[path] = data
        return data
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("map", {})
        data.setdefault("constituents", {})
    except (OSError, ValueError):
        data = {"map": {}, "constituents": {}}
    _CACHE_MEMO[path] = data
    return data
