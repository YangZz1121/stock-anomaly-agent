"""数据源连通性自检。

    python scripts/check_providers.py

配好密钥之后先跑这个，逐条确认哪个数据源真的通了。
它只打印调用结果，**不打印任何密钥**——只说明某个密钥"已配置 / 未配置"。

每条检查都会打印出这次调用真正落到了哪个 source，这样一眼能看出
产品到底走的是真实接口还是降级到了构造数据集。
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import get_settings
from app.contracts import FetchStatus
from app.providers.registry import build_providers
from app.timeutil import shift_days, today_str

OK = "\033[32m通\033[0m"
BAD = "\033[31m不通\033[0m"
WARN = "\033[33m降级\033[0m"


def _mask(value) -> str:
    return "已配置" if value else "未配置"


def _report(label: str, fetched) -> bool:
    if fetched.status == FetchStatus.OK:
        size = len(fetched.value) if hasattr(fetched.value, "__len__") else 1
        print(f"  {OK}  {label}  ({size} 条)  source={fetched.source}")
        return True
    flag = WARN if fetched.status == FetchStatus.MISSING else BAD
    print(f"  {flag}  {label}  {fetched.note or fetched.status}  source={fetched.source}")
    return False


async def main() -> int:
    settings = get_settings()

    # 日期用相对今天算，别写死——写死的日期过几周就会让自检报假故障
    end = today_str()
    start = shift_days(end, -20)

    print("配置状态（不显示密钥内容）")
    print(f"  FUYAO_API_KEY : {_mask(settings.fuyao_api_key)}")
    print(f"  IFIND_MCP_URL : {_mask(settings.ifind_mcp_url)}")
    print(f"  IFIND_API_KEY : {_mask(settings.ifind_api_key)}")
    print(f"  LLM_API_KEY   : {_mask(settings.llm_api_key)}  model={settings.llm_model}")

    providers = build_providers(settings)
    print("\n实际启用的数据源")
    for key, label in providers.labels.items():
        print(f"  {key:9s}: {label}")
    if providers.degraded:
        print("  （处于降级模式，页面会显示降级横幅）")

    healthy = True
    try:
        print("\n行情层")
        healthy &= _report("交易日历", await providers.market.trading_days())
        tickers = await providers.market.search_ticker("宁德时代")
        healthy &= _report("标的检索", tickers)

        code = "300750.SZ"
        if tickers.status == FetchStatus.OK and tickers.value:
            code = tickers.value[0].thscode
        healthy &= _report(
            f"日 K（{code}）",
            await providers.market.daily_bars(code, start, end),
        )
        healthy &= _report(
            "市场指数",
            await providers.market.index_daily_bars(
                settings.market_index_code, start, end
            ),
        )
        healthy &= _report("行业指数列表", await providers.market.industry_indexes())
        # 异动原因只是检索线索，拿不到不影响主链路
        _report("异动原因（线索池）", await providers.market.anomaly_reasons([code]))

        print("\n证据层")
        healthy &= _report(
            "事件检索",
            await providers.evidence.search_events(
                "动力电池 出口 政策",
                shift_days(end, -7),
                end,
                scope="company",
                context={"stock_name": "宁德时代", "required_terms": ["宁德时代"]},
            ),
        )

        print("\n推理层")
        if settings.resolved_llm_provider() == "mock":
            # 没配密钥是预期内的降级，不是故障，别让它看起来像红灯
            print(f"  {WARN}  LLM 未配置，使用确定性启发式推理层（能力弱于 LLM，页面会声明）")
        else:
            _report(
                "LLM 连通性",
                await providers.llm.complete_json(
                    purpose="connectivity_check",
                    system="你是一个只返回 JSON 的接口。",
                    user='返回 {"ok": true}',
                    schema_hint={"ok": "bool"},
                ),
            )
    finally:
        await providers.aclose()

    print()
    if healthy:
        print("主链路所需的数据源全部可用。")
        return 0
    print("有数据源不可用。产品仍可运行（会自动降级并在页面上声明），")
    print("但相关结论会显示为数据缺口，而不是静默生成。")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
