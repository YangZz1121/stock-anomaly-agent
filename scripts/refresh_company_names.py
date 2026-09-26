"""从扶摇 / iFinD 刷新 A 股 + 港股名称库。

    python scripts/refresh_company_names.py

密钥只从环境 / .env 读取，不打印。写出 fixtures/market/company_names.json，
并保留 fixtures/market/company_aliases.json 里的口语别名。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Settings
from app.providers.fuyao import FuyaoProvider

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_PATH = os.path.join(ROOT, "fixtures", "market", "company_names.json")
ALIAS_PATH = os.path.join(ROOT, "fixtures", "market", "company_aliases.json")
A_EXCHANGES = {"SH", "SZ", "BJ"}
HK_EXCHANGES = {"HK"}
_HK_CLASS = re.compile(r"[－\-][A-Za-zＷＳｗｓ]{1,2}$")


async def _list_fuyao(provider: FuyaoProvider, asset_type: str, limit: int = 2000) -> Tuple[List[Dict[str, Any]], str]:
    items: List[Dict[str, Any]] = []
    offset = 0
    note = ""
    while True:
        res = await provider._get(
            "/api/meta/tickers/list",
            {"asset_type": asset_type, "limit": limit, "offset": offset},
        )
        if not res.ok:
            return items, res.note or f"{asset_type} 拉取失败"
        page = (res.value or {}).get("item") or []
        items.extend(page)
        note = res.source
        if len(page) < limit:
            break
        offset += limit
    return items, note


def _load_aliases() -> Dict[str, List[str]]:
    if not os.path.exists(ALIAS_PATH):
        return {}
    with open(ALIAS_PATH, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    rows = data.get("aliases", data) if isinstance(data, dict) else {}
    out: Dict[str, List[str]] = {}
    if isinstance(rows, dict):
        for key, value in rows.items():
            if isinstance(value, list):
                out[str(key)] = [str(item).strip() for item in value if str(item).strip()]
            elif isinstance(value, str) and value.strip():
                out[str(key)] = [value.strip()]
    return out


def _normalize_row(raw: Dict[str, Any], fallback_type: str) -> Dict[str, str] | None:
    name = str(raw.get("name") or "").strip()
    thscode = str(raw.get("thscode") or "").strip().upper()
    if not name or not thscode:
        return None
    ticker = str(raw.get("ticker") or "").strip()
    if not ticker and "." in thscode:
        ticker = thscode.split(".")[0]
    exchange = str(raw.get("exchange") or "").strip().upper()
    if not exchange and "." in thscode:
        exchange = thscode.split(".")[-1]
    return {
        "name": name,
        "thscode": thscode,
        "ticker": ticker,
        "exchange": exchange,
        "asset_type": str(raw.get("asset_type") or fallback_type),
    }


def _merge(rows: List[Dict[str, str]], aliases: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    by_code: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        code = row["thscode"]
        if code in by_code:
            continue
        extra = []
        extra.extend(aliases.get(code, []))
        extra.extend(aliases.get(row["name"], []))
        extra.extend(aliases.get(row["ticker"], []))
        if row.get("exchange") == "HK" or code.endswith(".HK"):
            extra.extend(_hk_code_variants(code))
            stripped = _HK_CLASS.sub("", row["name"]).strip()
            if stripped and stripped != row["name"]:
                extra.append(stripped)
        seen = {row["name"], row["ticker"], code}
        clean = []
        for item in extra:
            if item in seen:
                continue
            seen.add(item)
            clean.append(item)
        by_code[code] = {
            "name": row["name"],
            "thscode": code,
            "ticker": row["ticker"],
            "aliases": clean,
        }
    for key, extras in aliases.items():
        suffix = key.split(".")[-1] if "." in key else ""
        variants = [key]
        if suffix == "HK":
            variants.extend(_hk_code_variants(key))
        if any(item in by_code for item in variants):
            continue
        if suffix not in A_EXCHANGES | HK_EXCHANGES:
            continue
        name = next((item for item in extras if 2 < len(item) < 10 and not item.endswith("公司")), None)
        if not name:
            name = extras[0] if extras else key
        ticker = key.split(".")[0]
        by_code[key] = {
            "name": name,
            "thscode": key,
            "ticker": ticker,
            "aliases": [item for item in extras if item != name],
        }
    # A 股优先、同名港股靠后，便于名称命中时先落到 A 股
    def _rank(item: Dict[str, Any]) -> Tuple[int, str]:
        code = item["thscode"]
        suffix = code.split(".")[-1] if "." in code else ""
        market = 0 if suffix in A_EXCHANGES else 1 if suffix in HK_EXCHANGES else 2
        return market, code

    return sorted(by_code.values(), key=_rank)


_HK_SHARDS = [
    "港股主板和GEM全部上市公司，返回股票代码和中文名称",
    "股票代码介于0001.HK与0800.HK之间的港股",
    "股票代码介于0801.HK与1600.HK之间的港股",
    "股票代码介于1601.HK与2400.HK之间的港股",
    "股票代码介于2401.HK与3999.HK之间的港股",
    "股票代码介于8000.HK与8999.HK之间的港股",
    "股票代码介于9000.HK与9999.HK之间的港股",
    "股票代码介于09900.HK与09999.HK之间的港股",
    "港股创业板GEM全部股票",
]


def _hk_digits(thscode: str) -> str:
    raw = (thscode or "").upper().replace(" ", "")
    digits = "".join(ch for ch in raw.split(".")[0] if ch.isdigit()).lstrip("0") or "0"
    return digits


def _hk_code_variants(thscode: str) -> List[str]:
    raw = (thscode or "").upper().replace(" ", "")
    digits = _hk_digits(raw)
    if digits == "0" and not any(ch.isdigit() for ch in raw):
        return [raw] if raw else []
    return sorted({f"{digits.zfill(4)}.HK", f"{digits.zfill(5)}.HK"})


def _normalize_hk_row(code: str, name: str) -> Dict[str, str] | None:
    name = (name or "").strip()
    digits = _hk_digits(code)
    if not name or digits == "0" and not any(ch.isdigit() for ch in (code or "")):
        return None
    canon = f"{digits.zfill(5)}.HK"
    return {
        "name": name,
        "thscode": canon,
        "ticker": digits.zfill(5),
        "exchange": "HK",
        "asset_type": "hk-share",
    }


def _parse_hk_table(text: str) -> List[Dict[str, str]]:
    import re

    rows: List[Dict[str, str]] = []
    for match in re.finditer(r"(\d{3,5}\.HK)\s*[|,，]\s*([^\n|,]+)", text or "", re.I):
        row = _normalize_hk_row(match.group(1), match.group(2))
        if row:
            rows.append(row)
    return rows


async def _download_csv_rows(client, url: str) -> List[Dict[str, str]]:
    resp = await client.get(url, follow_redirects=True)
    if resp.status_code >= 400:
        return []
    text = None
    for enc in ("utf-8-sig", "utf-8", "gb18030", "gbk"):
        try:
            text = resp.content.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        return []
    rows: List[Dict[str, str]] = []
    for line in text.splitlines()[1:]:
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        row = _normalize_hk_row(parts[0], parts[1])
        if row:
            rows.append(row)
    return rows


async def _try_ifind_hk() -> Tuple[List[Dict[str, str]], str]:
    """用 iFinD search_securities 按代码段拉取港股，避免单次截断。"""
    mcp_path = os.path.join(ROOT, ".mcp.json")
    if not os.path.exists(mcp_path):
        return [], "未找到 .mcp.json"
    try:
        from scripts.probe_ifind_mcp import PROTOCOL_VERSION, load_server, parse_sse_or_json
    except Exception:
        return [], "无法复用 iFinD 探测脚本"
    try:
        server = load_server("hexin-ifind-ds-mcp")
    except SystemExit:
        return [], "没有 hexin-ifind-ds-mcp"
    import re
    import httpx

    url = server["url"]
    headers = dict(server.get("headers") or {})
    headers["Content-Type"] = "application/json"
    headers["Accept"] = "application/json, text/event-stream"
    found: Dict[str, Dict[str, str]] = {}
    async with httpx.AsyncClient(timeout=90.0, headers=headers) as client:
        init = await client.post(
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
        if init.status_code >= 400:
            return [], f"iFinD MCP HTTP {init.status_code}"
        session_id = init.headers.get("mcp-session-id")
        if session_id:
            client.headers["Mcp-Session-Id"] = session_id
        await client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        req_id = 10
        for query in _HK_SHARDS:
            req_id += 1
            call = await client.post(
                url,
                json={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "method": "tools/call",
                    "params": {
                        "name": "search_securities",
                        "arguments": {"market": "港股", "query": query},
                    },
                },
            )
            if call.status_code >= 400:
                continue
            payload = parse_sse_or_json(call.text)
            raw = payload.get("result") or {}
            texts = [
                item.get("text", "")
                for item in (raw.get("content") or [])
                if isinstance(item, dict)
            ]
            blob = "\n".join(texts)
            try:
                parsed_blob = json.loads(blob)
            except ValueError:
                parsed_blob = None
            if isinstance(parsed_blob, dict):
                inner = parsed_blob.get("data") or parsed_blob
                if isinstance(inner, dict) and isinstance(inner.get("answer"), str):
                    blob = inner["answer"]
            for row in _parse_hk_table(blob):
                found[row["thscode"]] = row
            for csv_url in re.findall(r"https?://[^\s\\]+?\.csv", blob):
                for row in await _download_csv_rows(client, csv_url):
                    found[row["thscode"]] = row
    return list(found.values()), f"ifind:search_securities({len(found)})"


async def _fetch_sina_hk() -> Tuple[List[Dict[str, str]], str]:
    """东财不稳定时，用新浪港股代码表补主板 + 创业板。"""
    import httpx

    rows: Dict[str, Dict[str, str]] = {}
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://finance.sina.com.cn",
    }
    base = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php"
    async with httpx.AsyncClient(timeout=40.0, headers=headers) as client:
        for node in ("qbgg_hk", "cyb_hk"):
            count_resp = await client.get(f"{base}/Market_Center.getHKStockCount", params={"node": node})
            try:
                total = int(str(count_resp.text).strip().strip('"') or "0")
            except ValueError:
                total = 0
            page = 1
            fetched = 0
            while True:
                resp = await client.get(
                    f"{base}/Market_Center.getHKStockData",
                    params={
                        "page": page,
                        "num": 80,
                        "sort": "symbol",
                        "asc": 1,
                        "node": node,
                    },
                )
                if resp.status_code >= 400:
                    break
                try:
                    items = resp.json()
                except ValueError:
                    break
                if not isinstance(items, list) or not items:
                    break
                for item in items:
                    row = _normalize_hk_row(str(item.get("symbol") or ""), str(item.get("name") or ""))
                    if row:
                        rows[row["thscode"]] = row
                fetched += len(items)
                page += 1
                if total and fetched >= total:
                    break
                if page > 80:
                    break
    return list(rows.values()), f"sina:hk({len(rows)})"


async def _fetch_eastmoney_hk() -> Tuple[List[Dict[str, str]], str]:
    """iFinD 单次最多 1000 条，用东财港股代码表补全主板+创业板。"""
    import httpx

    rows: Dict[str, Dict[str, str]] = {}
    headers = {"User-Agent": "Mozilla/5.0"}
    hosts = (
        "https://push2.eastmoney.com/api/qt/clist/get",
        "https://80.push2.eastmoney.com/api/qt/clist/get",
        "https://82.push2.eastmoney.com/api/qt/clist/get",
    )
    async with httpx.AsyncClient(timeout=40.0, headers=headers) as client:
        for fs in ("m:128+t:3", "m:128+t:4"):
            page = 1
            page_size = 200
            total = None
            while True:
                data = None
                for attempt, url in enumerate(hosts):
                    try:
                        resp = await client.get(
                            url,
                            params={
                                "np": 1,
                                "fltt": 1,
                                "invt": 2,
                                "fid": "f12",
                                "po": 0,
                                "pn": page,
                                "pz": page_size,
                                "fs": fs,
                                "fields": "f12,f14",
                            },
                        )
                        if resp.status_code >= 400:
                            continue
                        data = (resp.json() or {}).get("data") or {}
                        break
                    except httpx.HTTPError:
                        if attempt == len(hosts) - 1:
                            data = None
                if not data:
                    break
                total = data.get("total") or 0
                diff = data.get("diff") or []
                if not diff:
                    break
                for item in diff:
                    row = _normalize_hk_row(str(item.get("f12") or ""), str(item.get("f14") or ""))
                    if row:
                        rows[row["thscode"]] = row
                if page * page_size >= total:
                    break
                page += 1
                await asyncio.sleep(0.05)
    return list(rows.values()), f"eastmoney:hk({len(rows)})"


async def main() -> int:
    settings = Settings()
    if not settings.fuyao_api_key:
        print("未配置 FUYAO_API_KEY，无法拉取全量代码表。")
        return 2
    provider = FuyaoProvider(settings)
    sources: List[str] = []
    rows: List[Dict[str, str]] = []
    try:
        a_items, a_note = await _list_fuyao(provider, "a-share")
        sources.append(a_note)
        for item in a_items:
            row = _normalize_row(item, "a-share")
            if row and row["exchange"] in A_EXCHANGES:
                rows.append(row)
        print(f"扶摇 A 股：{len(a_items)} 条原始，{sum(1 for r in rows if r['exchange'] in A_EXCHANGES)} 条纳入")

        hk_try, hk_try_note = await _list_fuyao(provider, "hk-share")
        if hk_try:
            for item in hk_try:
                row = _normalize_row(item, "hk-share")
                if row:
                    rows.append(row)
            sources.append(hk_try_note)
            print(f"扶摇 hk-share：{len(hk_try)} 条")
        else:
            print(f"扶摇不提供港股代码表（{hk_try_note}）")
            hk_rows, hk_note = await _try_ifind_hk()
            sources.append(hk_note)
            rows.extend(hk_rows)
            print(f"iFinD 港股：{len(hk_rows)} 条（{hk_note}）")
            em_rows, em_note = await _fetch_eastmoney_hk()
            sources.append(em_note)
            rows.extend(em_rows)
            print(f"东财港股补全：{len(em_rows)} 条（{em_note}）")
            if len(em_rows) < 2000:
                sina_rows, sina_note = await _fetch_sina_hk()
                sources.append(sina_note)
                rows.extend(sina_rows)
                print(f"新浪港股补全：{len(sina_rows)} 条（{sina_note}）")
    finally:
        await provider.aclose()

    aliases = _load_aliases()
    companies = _merge(rows, aliases)
    counts = defaultdict(int)
    for item in companies:
        suffix = item["thscode"].split(".")[-1] if "." in item["thscode"] else "?"
        counts[suffix] += 1
    payload = {
        "threshold": 0.8,
        "source": " + ".join(s for s in sources if s),
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "counts": dict(counts),
        "companies": companies,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    print(f"已写入 {OUT_PATH}")
    print("分市场：", dict(counts))
    print(f"合计 {len(companies)} 家")
    if counts.get("SH", 0) + counts.get("SZ", 0) + counts.get("BJ", 0) < 4000:
        print("A 股数量明显偏低，请检查扶摇返回。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
