"""进程级 TTL 缓存。

行情研究里有一批几乎不随提问变化的数据：交易日历、标的检索、行业指数
清单、指数成分股。每次研究都重新打接口，只会把等待时间叠在限流间隔上。

这里提供三件事：
* 进程内 TTL 字典，同一次部署里的后续请求直接复用；
* 可选落盘，避免免费档冷启动把慢变列表再拉一遍；
* 单飞（single-flight），同一 key 的并发请求共用一次上游调用。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Awaitable, Callable, Dict, Optional, TypeVar

T = TypeVar("T")


class MemoryTtlCache:
    def __init__(self) -> None:
        self._items: Dict[str, Any] = {}
        self._expires: Dict[str, float] = {}

    def get(self, key: str) -> Optional[Any]:
        expires = self._expires.get(key)
        if expires is None:
            return None
        if expires < time.time():
            self._items.pop(key, None)
            self._expires.pop(key, None)
            return None
        return self._items.get(key)

    def set(self, key: str, value: Any, ttl_s: float) -> None:
        self._items[key] = value
        self._expires[key] = time.time() + max(ttl_s, 1.0)

    def clear(self) -> None:
        self._items.clear()
        self._expires.clear()


_MEMORY = MemoryTtlCache()
_FLIGHT: Dict[str, asyncio.Future] = {}
_FLIGHT_GUARD = asyncio.Lock()


def memory_get(key: str) -> Optional[Any]:
    return _MEMORY.get(key)


def memory_set(key: str, value: Any, ttl_s: float) -> None:
    _MEMORY.set(key, value, ttl_s)


def memory_clear() -> None:
    _MEMORY.clear()


async def get_or_fetch(
    key: str,
    ttl_s: float,
    fetch: Callable[[], Awaitable[T]],
    *,
    copy: Optional[Callable[[T], T]] = None,
) -> T:
    """命中缓存则返回副本；未命中时同一 key 只允许一次上游调用。"""
    cached = _MEMORY.get(key)
    if cached is not None:
        return copy(cached) if copy else cached

    async with _FLIGHT_GUARD:
        inflight = _FLIGHT.get(key)
        if inflight is None:
            inflight = asyncio.get_running_loop().create_future()
            _FLIGHT[key] = inflight
            owner = True
        else:
            owner = False

    if not owner:
        value = await asyncio.shield(inflight)
        return copy(value) if copy else value

    try:
        value = await fetch()
        _MEMORY.set(key, value, ttl_s)
        inflight.set_result(value)
        return copy(value) if copy else value
    except Exception as exc:
        inflight.set_exception(exc)
        raise
    finally:
        async with _FLIGHT_GUARD:
            _FLIGHT.pop(key, None)


def resolve_cache_dir(cache_dir: str) -> str:
    if os.path.isabs(cache_dir):
        return cache_dir
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(root, cache_dir)


def load_json_file(path: str, ttl_hours: int) -> Optional[Dict[str, Any]]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    built = float(data.get("built_at") or 0)
    if built and (time.time() - built) > ttl_hours * 3600:
        return None
    return data


def save_json_file(path: str, payload: Dict[str, Any]) -> None:
    payload = dict(payload)
    payload.setdefault("built_at", time.time())
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass
