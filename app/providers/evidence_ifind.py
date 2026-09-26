"""iFinD MCP 证据源接入点。

这是一个**明确标注为未验证**的实现。iFinD MCP 的工具名与返回结构需要
在拿到访问权限后按实际契约核对，因此这里做两件事：

1. 把 MCP 的 JSON-RPC 调用封装好，接入点收敛在 ``_call_tool`` 一处；
2. 在无法确认返回结构时返回显式失败，而不是猜测字段名后静默返回空列表。

在没有 ``IFIND_MCP_URL`` 时，Provider 注册表不会选中这个实现。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import httpx

from app.config import Settings
from app.contracts import FetchStatus, Fetched
from app.providers.base import RawEvent

# 不同 scope 对应的检索侧重，拼进查询词
SCOPE_HINTS = {
    "market": "宏观 市场 政策",
    "industry": "行业 政策 供需",
    "company": "公司 公告 经营",
}


class IFindEvidenceProvider:
    name = "ifind"

    def __init__(self, settings: Settings) -> None:
        self._url = settings.ifind_mcp_url or ""
        headers = {"Content-Type": "application/json"}
        if settings.ifind_api_key:
            headers["Authorization"] = f"Bearer {settings.ifind_api_key}"
        self._client = httpx.AsyncClient(
            timeout=settings.http_timeout_s, headers=headers
        )
        self._id = 0

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call_tool(
        self, tool: str, arguments: Dict[str, Any]
    ) -> Fetched[Dict[str, Any]]:
        source = f"ifind-mcp:{tool}"
        if not self._url:
            return Fetched.failure(source, self.name, "未配置 IFIND_MCP_URL")
        self._id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
        try:
            resp = await self._client.post(self._url, json=payload)
        except httpx.HTTPError as exc:
            return Fetched.failure(source, self.name, f"MCP 调用失败：{exc}")
        if resp.status_code >= 400:
            return Fetched.failure(source, self.name, f"HTTP {resp.status_code}")
        try:
            body = resp.json()
        except ValueError:
            return Fetched.failure(source, self.name, "MCP 响应不是合法 JSON")
        if "error" in body:
            return Fetched.failure(
                source, self.name, f"MCP 错误：{body['error'].get('message')}"
            )
        return Fetched.success(body.get("result") or {}, source, self.name)

    async def search_events(
        self,
        query: str,
        start_date: str,
        end_date: str,
        scope: str = "company",
        limit: int = 20,
        context: Optional[Dict[str, Any]] = None,
    ) -> Fetched[List[RawEvent]]:
        res = await self._call_tool(
            "news_search",
            {
                "query": f"{query} {SCOPE_HINTS.get(scope, '')}".strip(),
                "start_date": start_date,
                "end_date": end_date,
                "limit": limit,
            },
        )
        if not res.ok:
            return Fetched(
                status=res.status, source=res.source, provider=self.name, note=res.note
            )

        items = _extract_items(res.value or {})
        if items is None:
            return Fetched.failure(
                res.source,
                self.name,
                "iFinD MCP 返回结构与预期不符，未做字段猜测；请按实际契约调整 evidence_ifind.py",
                FetchStatus.FAILED,
            )
        out: List[RawEvent] = []
        for it in items:
            title = it.get("title") or it.get("name") or ""
            if not title:
                continue
            out.append(
                RawEvent(
                    event_id=str(it.get("id") or it.get("news_id") or title[:32]),
                    title=title,
                    summary=it.get("summary") or it.get("content") or "",
                    published_at=it.get("publish_time") or it.get("date"),
                    source_name=it.get("source") or "iFinD",
                    source_url=it.get("url"),
                    source_tier_hint=None,  # 交由上层按来源名判级
                    origin_key=it.get("source") or None,
                    scope=scope,
                    raw=it,
                )
            )
        if not out:
            return Fetched.failure(
                res.source, self.name, "未检索到相关事件", FetchStatus.MISSING
            )
        return Fetched.success(out, res.source, self.name)


def _extract_items(result: Dict[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """从 MCP 结果里找事件列表。找不到就返回 None，交由调用方报错。"""
    for key in ("items", "data", "records", "list"):
        value = result.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            for inner in ("items", "list", "records"):
                if isinstance(value.get(inner), list):
                    return value[inner]
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("json"), dict):
                return _extract_items(block["json"])
    return None
