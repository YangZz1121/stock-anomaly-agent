"""探测 iFinD MCP 的真实契约。

    python scripts/probe_ifind_mcp.py [服务名]

iFinD 给的是一份 MCP 客户端配置（7 个服务，streamable HTTP 传输），
不是一个可以直接 POST 的 URL。这个脚本做完整的握手并列出工具清单，
用来搞清楚"到底有哪些工具、参数长什么样"——在此之前任何对接都是猜测。

它读 .mcp.json（已 gitignore），**不打印任何 Authorization 内容**。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

CONFIG_PATH = ".mcp.json"
DEFAULT_SERVER = "hexin-ifind-ds-news-mcp"

PROTOCOL_VERSION = "2025-06-18"


def load_server(name: str) -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    servers = cfg.get("mcpServers") or cfg
    if name not in servers:
        print(f"配置里没有服务 {name}，可选：{list(servers)}")
        raise SystemExit(2)
    return servers[name]


def parse_sse_or_json(text: str) -> dict:
    """streamable HTTP 的响应可能是纯 JSON，也可能是 SSE 分帧。"""
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[len("data:") :].strip()
            if payload:
                return json.loads(payload)
    raise ValueError(f"无法解析响应：{text[:300]}")


async def main() -> int:
    name = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_SERVER
    server = load_server(name)
    url = server["url"]

    headers = dict(server.get("headers") or {})
    headers["Content-Type"] = "application/json"
    # streamable HTTP 要求同时接受 JSON 和 SSE，少了这个头服务端会拒绝
    headers["Accept"] = "application/json, text/event-stream"

    print(f"服务：{name}")
    print(f"地址：{url}")
    print(f"请求头：{sorted(headers)}（Authorization 内容不显示）\n")

    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        print("[1/3] initialize")
        resp = await client.post(
            url,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "stock-anomaly-agent", "version": "1.0.0"},
                },
            },
        )
        print(f"      HTTP {resp.status_code}  content-type={resp.headers.get('content-type')}")
        if resp.status_code >= 400:
            print(f"      响应：{resp.text[:500]}")
            return 1

        session_id = resp.headers.get("mcp-session-id")
        print(f"      会话 ID：{'已返回' if session_id else '未返回（服务端可能无状态）'}")
        init = parse_sse_or_json(resp.text)
        info = (init.get("result") or {}).get("serverInfo") or {}
        print(f"      服务端：{info}")

        if session_id:
            headers["Mcp-Session-Id"] = session_id
            client.headers["Mcp-Session-Id"] = session_id

        print("\n[2/3] notifications/initialized")
        note = await client.post(
            url, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        print(f"      HTTP {note.status_code}")

        print("\n[3/3] tools/list")
        resp = await client.post(
            url, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        print(f"      HTTP {resp.status_code}")
        if resp.status_code >= 400:
            print(f"      响应：{resp.text[:500]}")
            return 1

        body = parse_sse_or_json(resp.text)
        if "error" in body:
            print(f"      MCP 错误：{body['error']}")
            return 1

        tools = (body.get("result") or {}).get("tools") or []
        print(f"\n共 {len(tools)} 个工具：\n")
        for t in tools:
            print(f"  ● {t.get('name')}")
            desc = (t.get("description") or "").strip().replace("\n", " ")
            if desc:
                print(f"      {desc[:160]}")
            schema = t.get("inputSchema") or {}
            props = schema.get("properties") or {}
            required = set(schema.get("required") or [])
            for pname, pdef in props.items():
                flag = "必填" if pname in required else "可选"
                ptype = pdef.get("type", "?")
                pdesc = (pdef.get("description") or "").replace("\n", " ")[:80]
                print(f"      - {pname} ({ptype}, {flag}) {pdesc}")
            print()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
