"""进程缓存与慢变列表：命中后不再重复计算。"""

from __future__ import annotations

import asyncio

from app.cache import MemoryTtlCache, get_or_fetch, memory_clear, memory_get, memory_set
from app.providers.registry import describe_providers
from app.config import Settings


def test_memory_ttl_cache_expires():
    cache = MemoryTtlCache()
    cache.set("k", ["a", "b"], ttl_s=0.01)
    assert cache.get("k") == ["a", "b"]
    cache._expires["k"] = 0
    assert cache.get("k") is None


def test_memory_set_get_roundtrip():
    memory_clear()
    memory_set("demo", [1, 2, 3], ttl_s=60)
    assert memory_get("demo") == [1, 2, 3]
    memory_clear()
    assert memory_get("demo") is None


def test_get_or_fetch_single_flight_and_copy():
    memory_clear()
    calls = {"n": 0}

    async def fetch():
        calls["n"] += 1
        return ["x", "y"]

    async def _run():
        first, second = await asyncio.gather(
            get_or_fetch("sf", 60, fetch, copy=list),
            get_or_fetch("sf", 60, fetch, copy=list),
        )
        third = await get_or_fetch("sf", 60, fetch, copy=list)
        return first, second, third

    a, b, c = asyncio.run(_run())
    assert calls["n"] == 1
    assert a == b == c == ["x", "y"]
    a.append("z")
    assert memory_get("sf") == ["x", "y"]
    memory_clear()


def test_describe_providers_does_not_need_live_clients():
    status = describe_providers(
        Settings(market_provider="mock", evidence_provider="mock", llm_provider="mock")
    )
    assert status.degraded is True
    assert status.labels["market"] == "构造数据集"
    assert status.labels["llm"] == "确定性启发式推理层"
