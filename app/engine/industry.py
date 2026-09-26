"""行业识别。

规划文档 5.5 的原则是：**行业分类首先是数据问题，最后才是语义推理问题。**
因此这里严格按三级优先级降级，并且只有走到第三级（LLM 辅助）时才会把
结果标记为弱证据。

扶摇没有"股票 → 所属行业"的正向接口，只有行业指数清单和指数成分股，
所以第二级要反向建映射。这个映射会落盘缓存，避免每次研究都把成分股
接口打一遍。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from app.config import Settings
from app.contracts import FetchStatus
from app.providers.base import IndexInfo
from app.schemas import IndustryInfo
from app.trace import RunRecorder

METHOD_LABELS = {
    "structural": "结构化数据直接识别",
    "reverse_map": "行业指数成分股反向映射",
    "llm_assisted": "LLM 辅助判断（弱证据）",
    "unavailable": "无法识别",
}


class IndustryResolver:
    def __init__(self, settings: Settings, market: Any, llm: Any) -> None:
        self._settings = settings
        self._market = market
        self._llm = llm
        self._cache_path = _cache_path(settings)
        self._cache = _load_cache(self._cache_path)

    async def resolve(
        self, thscode: str, stock_name: str, recorder: RunRecorder
    ) -> IndustryInfo:
        # ---- 第一级：缓存命中（等价于结构化数据直接识别）----
        cached = self._cache.get("map", {}).get(thscode)
        if cached and not _expired(self._cache, self._settings.industry_map_ttl_hours):
            return IndustryInfo(
                index_name=cached["name"],
                index_code=cached["code"],
                method=cached.get("method", "reverse_map"),
                method_label=METHOD_LABELS.get(
                    cached.get("method", "reverse_map"), ""
                ),
                is_weak_evidence=cached.get("method") == "llm_assisted",
                note="来自本地行业映射缓存",
            )

        indexes = await self._market.industry_indexes()
        recorder.record_tool(
            "industry_indexes", {}, indexes.status, indexes.provider, note=indexes.note
        )
        if not indexes.ok or not indexes.value:
            return IndustryInfo(
                method="unavailable",
                method_label=METHOD_LABELS["unavailable"],
                note=f"行业指数清单不可用：{indexes.note}",
            )

        # ---- 第二级：成分股反向映射 ----
        hit = await self._reverse_lookup(thscode, indexes.value, recorder)
        if hit is not None:
            self._remember(thscode, hit.thscode, hit.name, "reverse_map")
            return IndustryInfo(
                index_name=hit.name,
                index_code=hit.thscode,
                method="reverse_map",
                method_label=METHOD_LABELS["reverse_map"],
                is_weak_evidence=False,
                note="通过行业指数成分股清单反向确认",
            )

        # ---- 第三级：LLM 辅助 ----
        guess = await self._llm_assist(stock_name, indexes.value, recorder)
        if guess is not None:
            self._remember(thscode, guess.thscode, guess.name, "llm_assisted")
            return IndustryInfo(
                index_name=guess.name,
                index_code=guess.thscode,
                method="llm_assisted",
                method_label=METHOD_LABELS["llm_assisted"],
                is_weak_evidence=True,
                note=(
                    "结构化数据未能确认所属行业，由模型根据公司主营业务推断，"
                    "属于弱证据，行业层面的结论需要额外谨慎对待"
                ),
            )

        return IndustryInfo(
            method="unavailable",
            method_label=METHOD_LABELS["unavailable"],
            note="结构化映射与模型推断均未能确定所属行业指数",
        )

    # ------------------------------------------------------------------

    async def _reverse_lookup(
        self, thscode: str, indexes: List[IndexInfo], recorder: RunRecorder
    ) -> Optional[IndexInfo]:
        """逐个行业指数拉成分股，边拉边把整张映射表缓存下来。"""
        budget = self._settings.industry_map_max_calls
        found: Optional[IndexInfo] = None
        calls = 0

        for index in indexes:
            if calls >= budget:
                recorder.log(
                    f"行业反向映射达到本次运行的调用预算（{budget} 次），"
                    f"剩余行业未遍历"
                )
                break
            res = await self._market.index_constituents(index.thscode)
            calls += 1
            if not res.ok or not res.value:
                continue
            codes = {t.thscode for t in res.value}
            for code in codes:
                self._cache.setdefault("map", {}).setdefault(
                    code, {"code": index.thscode, "name": index.name, "method": "reverse_map"}
                )
            if thscode in codes:
                found = index
                break

        recorder.record_tool(
            "index_constituents_scan",
            {"scanned": calls, "budget": budget},
            FetchStatus.OK if found else FetchStatus.MISSING,
            getattr(self._market, "name", "unknown"),
            note=f"反向映射扫描 {calls} 个行业指数" + ("，已命中" if found else "，未命中"),
        )
        self._flush()
        return found

    async def _llm_assist(
        self, stock_name: str, indexes: List[IndexInfo], recorder: RunRecorder
    ) -> Optional[IndexInfo]:
        if not stock_name:
            return None
        catalog = [{"code": i.thscode, "name": i.name} for i in indexes[:200]]
        started = time.perf_counter()
        res = await self._llm.complete_json(
            purpose="industry_assist",
            system=(
                "你是 A 股行业分类助手。只能从给定的行业指数清单中选择，"
                "不得编造清单之外的指数。若无法判断，返回 code 为 null。"
            ),
            user=(
                f"公司名称：{stock_name}\n\n"
                f"候选行业指数清单：\n{json.dumps(catalog, ensure_ascii=False)}\n\n"
                "请选出与该公司主营业务最相关的一个行业指数。"
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
        if not code:
            return None
        # 只接受确实存在于清单里的指数，杜绝模型编造代码
        for index in indexes:
            if index.thscode == code:
                return index
        return None

    # ------------------------------------------------------------------

    def _remember(self, thscode: str, code: str, name: str, method: str) -> None:
        self._cache.setdefault("map", {})[thscode] = {
            "code": code,
            "name": name,
            "method": method,
        }
        self._cache["built_at"] = time.time()
        self._flush()

    def _flush(self) -> None:
        self._cache.setdefault("built_at", time.time())
        try:
            os.makedirs(os.path.dirname(self._cache_path), exist_ok=True)
            with open(self._cache_path, "w", encoding="utf-8") as fh:
                json.dump(self._cache, fh, ensure_ascii=False)
        except OSError:
            # 缓存写不进去不影响研究本身，静默跳过
            pass


def _cache_path(settings: Settings) -> str:
    base = settings.cache_dir
    if not os.path.isabs(base):
        root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        base = os.path.join(root, base)
    provider = settings.resolved_market_provider()
    return os.path.join(base, f"industry_map.{provider}.json")


def _load_cache(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {"map": {}, "built_at": 0.0}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("map", {})
        data.setdefault("built_at", 0.0)
        return data
    except (OSError, ValueError):
        return {"map": {}, "built_at": 0.0}


def _expired(cache: Dict[str, Any], ttl_hours: int) -> bool:
    built = cache.get("built_at") or 0
    return (time.time() - built) > ttl_hours * 3600
