"""iFinD MCP 证据源。

对接的是 ``hexin-ifind-ds-news-mcp``，传输方式是 MCP streamable HTTP：
必须先 ``initialize`` 拿到会话 ID，之后每个请求都带上它，否则服务端拒绝。
握手只做一次，之后复用同一个会话。

三个工具的返回结构互不相同，这里逐个适配（见 ``_TOOLS``）：

===================== ================================ ==================
工具                   数据位置                          字段
===================== ================================ ==================
search_news           ``data.data``（JSON 字符串）       资讯标题/资讯内容/日期/URL
search_notice         ``data``（JSON 字符串）            公告标题/公告片段内容/日期
search_trending_news  ``data.result``（已是数组）        资讯标题/资讯内容/URL/信息来源
===================== ================================ ==================

**一个必须记住的数据边界**：三个工具返回的时间都只精确到**天**，没有时分。
产品原本用 15:00 收盘线判断"这条消息是不是收盘后才发的"，接入真实 iFinD
后这个判断做不了。因此 RawEvent 上会带 ``time_precision="date"``，让上层
的时间检验据此降级——而不是默认当天的消息都能解释当天的价格变化。

``search_trending_news`` 连日期都没有，所以它只能进 T4 线索池，不作为证据。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

import httpx

from app.config import Settings
from app.contracts import FetchStatus, Fetched
from app.providers.base import RawEvent

PROTOCOL_VERSION = "2025-06-18"

# 不同 scope 对应的检索侧重，拼进查询词
SCOPE_HINTS = {
    "market": "宏观 市场 政策",
    "industry": "行业 政策 供需",
    "company": "公司 公告 经营",
}

# 域名 -> 来源名。iFinD 的资讯只给 URL 不给来源名，而来源名决定可信度分级，
# 所以这张表是必需的。认不出的域名保持 None，由上层按 T3 处理。
_DOMAIN_SOURCES = {
    "cnstock.com": "中国证券报",
    "stcn.com": "证券时报",
    "zqrb.cn": "证券日报",
    "cls.cn": "财联社",
    "yicai.com": "第一财经",
    "21jingji.com": "21世纪经济报道",
    "nbd.com.cn": "每日经济新闻",
    "jiemian.com": "界面新闻",
    "wallstreetcn.com": "华尔街见闻",
    "xinhuanet.com": "新华社",
    "people.com.cn": "人民日报",
    "ce.cn": "经济日报",
    "thepaper.cn": "澎湃新闻",
    "cfi.cn": "中财网",
    "10jqka.com.cn": "同花顺财经",
    "eastmoney.com": "东方财富",
    "sina.com.cn": "新浪财经",
    "sse.com.cn": "上交所",
    "szse.cn": "深交所",
    "cninfo.com.cn": "巨潮资讯（交易所指定披露平台）",
    "gov.cn": "主管部门官方网站",
    "miit.gov.cn": "工信部",
    "ndrc.gov.cn": "发改委",
    "csrc.gov.cn": "证监会",
    # 公众号文章没有采编责任体系，按未证实处理
    "mp.weixin.qq.com": "自媒体公众号",
}


def _source_from_url(url: Optional[str]) -> str:
    if not url:
        return "iFinD 资讯"
    host = (urlsplit(url).hostname or "").lower()
    for domain, name in _DOMAIN_SOURCES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host or "iFinD 资讯"


class IFindEvidenceProvider:
    name = "ifind"

    def __init__(self, settings: Settings) -> None:
        self._url = settings.ifind_mcp_url or ""
        headers = {
            "Content-Type": "application/json",
            # streamable HTTP 要求同时接受两种响应形式，缺这个头服务端会拒绝
            "Accept": "application/json, text/event-stream",
        }
        if settings.ifind_api_key:
            # iFinD 的 Authorization 是完整值，不加 Bearer 前缀
            headers["Authorization"] = settings.ifind_api_key
        self._client = httpx.AsyncClient(
            timeout=settings.http_timeout_s, headers=headers
        )
        self._id = 0
        self._session_ready = False
        self._handshake_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # MCP 传输层
    # ------------------------------------------------------------------

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    @staticmethod
    def _decode(text: str) -> Dict[str, Any]:
        """响应可能是纯 JSON，也可能是 SSE 分帧，两种都要能读。"""
        stripped = (text or "").lstrip()
        if stripped.startswith("{"):
            return json.loads(stripped)
        for line in (text or "").splitlines():
            if line.startswith("data:"):
                payload = line[len("data:") :].strip()
                if payload:
                    return json.loads(payload)
        raise ValueError("响应既不是 JSON 也不是可解析的 SSE")

    async def _ensure_session(self) -> Optional[str]:
        """完成一次握手。返回错误说明，成功则返回 None。"""
        if self._session_ready:
            return None
        async with self._handshake_lock:
            if self._session_ready:
                return None
            try:
                resp = await self._client.post(
                    self._url,
                    json={
                        "jsonrpc": "2.0",
                        "id": self._next_id(),
                        "method": "initialize",
                        "params": {
                            "protocolVersion": PROTOCOL_VERSION,
                            "capabilities": {},
                            "clientInfo": {
                                "name": "stock-anomaly-agent",
                                "version": "1.0.0",
                            },
                        },
                    },
                )
            except httpx.HTTPError as exc:
                return f"MCP 握手失败：{exc}"
            if resp.status_code >= 400:
                return f"MCP 握手失败：HTTP {resp.status_code}"

            session_id = resp.headers.get("mcp-session-id")
            if session_id:
                self._client.headers["Mcp-Session-Id"] = session_id
            try:
                await self._client.post(
                    self._url,
                    json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                )
            except httpx.HTTPError as exc:
                return f"MCP 握手失败：{exc}"

            self._session_ready = True
            return None

    async def _call_tool(
        self, tool: str, arguments: Dict[str, Any]
    ) -> Fetched[Dict[str, Any]]:
        source = f"ifind-mcp:{tool}"
        if not self._url:
            return Fetched.failure(source, self.name, "未配置 IFIND_MCP_URL")

        err = await self._ensure_session()
        if err:
            return Fetched.failure(source, self.name, err)

        try:
            resp = await self._client.post(
                self._url,
                json={
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments},
                },
            )
        except httpx.TimeoutException:
            return Fetched.failure(source, self.name, "MCP 请求超时")
        except httpx.HTTPError as exc:
            return Fetched.failure(source, self.name, f"MCP 调用失败：{exc}")

        if resp.status_code == 404:
            # 会话过期，下次调用重新握手，不在这里静默重试
            self._session_ready = False
            return Fetched.failure(source, self.name, "MCP 会话已失效")
        if resp.status_code >= 400:
            return Fetched.failure(source, self.name, f"HTTP {resp.status_code}")

        try:
            body = self._decode(resp.text)
        except ValueError as exc:
            return Fetched.failure(source, self.name, f"MCP 响应无法解析：{exc}")
        if "error" in body:
            message = (body.get("error") or {}).get("message")
            return Fetched.failure(source, self.name, f"MCP 错误：{message}")
        return Fetched.success(body.get("result") or {}, source, self.name)

    # ------------------------------------------------------------------
    # 业务层
    # ------------------------------------------------------------------

    @staticmethod
    def _unwrap(result: Dict[str, Any], data_path: str) -> Optional[List[Dict[str, Any]]]:
        """从 MCP 结果里取出事件数组。

        结构是套了三层的：result.content[0].text 是一个 JSON 字符串，
        解出来是业务信封，信封里的数据位置还随工具而变，
        而且有的工具把数组又编码成了字符串。
        """
        blocks = result.get("content")
        if not isinstance(blocks, list) or not blocks:
            return None
        text = blocks[0].get("text")
        if not isinstance(text, str):
            return None
        try:
            envelope = json.loads(text)
        except ValueError:
            return None
        if envelope.get("code") != 1:
            return None

        node: Any = envelope.get("data")
        for key in data_path.split(".") if data_path else []:
            if not isinstance(node, dict):
                return None
            node = node.get(key)
        if isinstance(node, str):
            try:
                node = json.loads(node)
            except ValueError:
                return None
        return node if isinstance(node, list) else None

    async def search_events(
        self,
        query: str,
        start_date: str,
        end_date: str,
        scope: str = "company",
        limit: int = 20,
        context: Optional[Dict[str, Any]] = None,
    ) -> Fetched[List[RawEvent]]:
        size = max(1, min(limit, 20))  # 工具上限 20
        terms = f"{query} {SCOPE_HINTS.get(scope, '')}".strip()

        news = await self._call_tool(
            "search_news",
            {
                "query": terms,
                "time_start": start_date,
                "time_end": end_date,
                "size": size,
            },
        )
        if not news.ok:
            return Fetched(
                status=news.status,
                source=news.source,
                provider=self.name,
                note=news.note,
            )

        events: List[RawEvent] = []
        items = self._unwrap(news.value or {}, "data")
        if items is None:
            return Fetched.failure(
                news.source,
                self.name,
                "search_news 返回结构与已知契约不符，未做字段猜测",
                FetchStatus.FAILED,
            )
        events.extend(self._to_news_events(items, scope))

        # 公司范围额外取公告：公告是 T1 权威来源，也是确认公司业务暴露的主要依据
        if scope == "company":
            notice = await self._call_tool(
                "search_notice",
                {
                    "query": terms,
                    "time_start": start_date,
                    "time_end": end_date,
                    "size": size,
                },
            )
            if notice.ok:
                rows = self._unwrap(notice.value or {}, "")
                if rows:
                    events.extend(self._to_notice_events(rows, scope))

        if not events:
            return Fetched.failure(
                news.source, self.name, "未检索到相关事件", FetchStatus.MISSING
            )
        return Fetched.success(events, news.source, self.name)

    def _to_news_events(
        self, items: List[Dict[str, Any]], scope: str
    ) -> List[RawEvent]:
        out: List[RawEvent] = []
        for it in items:
            title = (it.get("资讯标题") or "").strip()
            if not title:
                continue
            url = it.get("URL")
            source_name = it.get("信息来源") or _source_from_url(url)
            out.append(
                RawEvent(
                    event_id=f"news:{url or title[:48]}",
                    title=title,
                    summary=(it.get("资讯内容") or "").strip(),
                    published_at=it.get("日期"),
                    source_name=source_name,
                    source_url=url,
                    # 同一篇稿件被转载时 URL 不同，只能退而用来源名做去重键；
                    # 这比构造数据集里的 origin_key 粗，已在 README 记为边界。
                    origin_key=source_name,
                    scope=scope,
                    raw=dict(it, time_precision="date"),
                )
            )
        return out

    def _to_notice_events(
        self, rows: List[Dict[str, Any]], scope: str
    ) -> List[RawEvent]:
        out: List[RawEvent] = []
        for it in rows:
            title = (it.get("公告标题") or "").strip()
            if not title:
                continue
            out.append(
                RawEvent(
                    event_id=f"notice:{title[:64]}",
                    title=title,
                    summary=(it.get("公告片段内容") or "").strip(),
                    published_at=it.get("日期"),
                    # 公告归入 T1：它来自交易所指定披露渠道
                    source_name="公司公告",
                    # 这个工具不返回原文链接，所以公告无法一键反查原文
                    source_url=None,
                    origin_key=f"notice:{title[:64]}",
                    scope=scope,
                    raw=dict(it, time_precision="date"),
                )
            )
        return out

    async def trending_clues(
        self, keyword: str, limit: int = 10
    ) -> Fetched[List[RawEvent]]:
        """热门事件。没有日期字段，所以只能当检索线索，不作为结论依据。"""
        res = await self._call_tool(
            "search_trending_news",
            {"keyword": keyword, "size": max(1, min(limit, 20))},
        )
        if not res.ok:
            return Fetched(
                status=res.status, source=res.source, provider=self.name, note=res.note
            )
        rows = self._unwrap(res.value or {}, "result")
        if rows is None:
            return Fetched.failure(
                res.source,
                self.name,
                "search_trending_news 返回结构与已知契约不符",
                FetchStatus.FAILED,
            )
        out = [
            RawEvent(
                event_id=f"trending:{it.get('URL') or (it.get('资讯标题') or '')[:48]}",
                title=(it.get("资讯标题") or "").strip(),
                summary=(it.get("资讯内容") or "").strip(),
                published_at=None,  # 该工具不返回时间
                source_name=it.get("信息来源") or _source_from_url(it.get("URL")),
                source_url=it.get("URL"),
                source_tier_hint="t4_unverified",
                scope="market",
                raw=dict(it, time_precision="none"),
            )
            for it in rows
            if (it.get("资讯标题") or "").strip()
        ]
        if not out:
            return Fetched.failure(
                res.source, self.name, "未检索到热门事件", FetchStatus.MISSING
            )
        return Fetched.success(out, res.source, self.name)
